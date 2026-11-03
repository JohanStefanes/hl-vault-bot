"""CLI: screen / alloc / hist / watch.

Phase 1 ist bewusst read-only - es werden keine Transaktionen gesendet.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import allocator, db, screener
from .config import CONFIG

CACHE_PATH = "data/backtest_cache/snapshot.json"


def _fmt_usd(x: float) -> str:
    if x >= 1_000_000:
        return f"{x/1_000_000:.1f}M"
    if x >= 1_000:
        return f"{x/1_000:.0f}k"
    return f"{x:.0f}"


def print_table(results, limit: int, show_failed: bool = False) -> None:
    rows = [r for r in results if r.passed or show_failed][:limit]
    hdr = (f"{'#':>2} {'Vault':26}{'Score':>7}{'Sharpe':>8}{'CAGR':>9}"
           f"{'MaxDD':>8}{'TVL':>9}{'Age':>6}{'Ldr%':>7}{'Foll':>6}")
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(rows, 1):
        mt = r.metrics
        flag = "" if r.passed else " !"
        print(f"{i:>2} {r.name[:25]:26}{r.score:>7.1f}"
              f"{(mt.sharpe if mt else 0):>8.2f}"
              f"{(mt.cagr if mt else 0):>8.0%}"
              f"{(mt.max_drawdown if mt else 0):>8.1%}"
              f"{_fmt_usd(r.tvl):>9}{r.age_days:>6.0f}"
              f"{r.leader_fraction:>7.1%}{r.followers:>6}{flag}")
        if not r.passed and r.reasons:
            print(f"     -> {'; '.join(r.reasons)}")


def cmd_screen(args) -> int:
    results, total = screener.run(CONFIG, verbose=not args.quiet)
    passed = [r for r in results if r.passed]
    print(f"\n{total} Vaults gesamt | {len(results)} bewertet | "
          f"{len(passed)} bestehen alle Filter\n")
    print_table(results, args.limit, show_failed=args.show_failed)

    if not args.no_save:
        conn = db.connect()
        ts = db.save_run(conn, results, total, note="screen")
        print(f"\nSnapshot gespeichert (ts={ts}) -> {db.DB_PATH}")

    if args.json:
        out = [{
            "name": r.name, "address": r.address, "score": r.score,
            "passed": r.passed, "parts": r.parts, "tvl": r.tvl,
            "age_days": round(r.age_days, 1),
            "leader_fraction": r.leader_fraction,
            "metrics": r.metrics.as_row() if r.metrics else None,
            "reasons": r.reasons,
        } for r in results]
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"JSON -> {args.json}")
    return 0


def cmd_alloc(args) -> int:
    results, total = screener.run(CONFIG, verbose=not args.quiet)
    plan = allocator.propose(results, args.capital, CONFIG)
    if not plan:
        print("Kein Vault besteht die Filter - kein Allokationsvorschlag.")
        return 1
    print(f"\nVorschlag fuer ${args.capital:,.0f} "
          f"(max {CONFIG.portfolio.max_positions} Positionen, "
          f"max {CONFIG.portfolio.max_weight_per_vault:.0%} pro Vault):\n")
    for p in plan:
        warn = "  [unter 20 USDC Minimum!]" if p["below_min"] else ""
        print(f"  {p['weight']:>6.1%}  ${p['usd']:>12,.2f}  "
              f"{p['name'][:28]:30} score {p['score']:.1f}{warn}")
        print(f"          {p['address']}")
    print("\nHINWEIS: nur ein Vorschlag. Phase 1 sendet keine Transaktionen.")
    return 0


def cmd_hist(args) -> int:
    conn = db.connect()
    rows = db.history(conn, args.address, args.limit)
    if not rows:
        print("Keine Snapshots fuer diese Adresse.")
        return 1
    print(f"{'Datum':22}{'Score':>7}{'TVL':>13}{'Sharpe':>8}{'MaxDD':>8}")
    import datetime as _dt
    for r in rows:
        d = _dt.datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d %H:%M")
        print(f"{d:22}{r['score'] or 0:>7.1f}{r['tvl'] or 0:>13,.0f}"
              f"{r['sharpe'] or 0:>8.2f}{(r['max_drawdown'] or 0):>8.1%}")
    return 0


def cmd_watch(args) -> int:
    """Reissleinen-Check gegen den letzten Snapshot."""
    conn = db.connect()
    prev_ts = db.last_run_ts(conn)
    results, total = screener.run(CONFIG, verbose=not args.quiet)
    by_addr = {r.address: r for r in results}
    alerts = []

    if prev_ts:
        for old in db.top_at(conn, prev_ts, n=20):
            cur = by_addr.get(old["vault_address"])
            if cur is None:
                alerts.append(f"VERSCHWUNDEN aus Kandidaten: {old['name']}")
                continue
            if not cur.passed:
                alerts.append(f"FAELLT DURCH: {cur.name} -> {'; '.join(cur.reasons)}")
            if old["tvl"] and cur.tvl / old["tvl"] - 1 < -CONFIG.portfolio.panic_tvl_drop:
                alerts.append(
                    f"TVL-EINBRUCH: {cur.name} {_fmt_usd(old['tvl'])} "
                    f"-> {_fmt_usd(cur.tvl)}")
            if cur.metrics and cur.metrics.max_drawdown < -CONFIG.portfolio.panic_drawdown:
                alerts.append(
                    f"DRAWDOWN: {cur.name} {cur.metrics.max_drawdown:.1%}")

    db.save_run(conn, results, total, note="watch")
    if alerts:
        print("\n!!! ALERTS !!!")
        for a in alerts:
            print("  - " + a)
    else:
        print("\nKeine Alerts.")
    print()
    print_table(results, args.limit)
    return 0


def cmd_fetch_cache(args) -> int:
    """Detaildaten der Kandidaten einmal holen und cachen (fuer backtest)."""
    import os
    import time

    from . import api

    print("Lade Vault-Liste ...", flush=True)
    raw = api.fetch_all_vaults()
    if args.all:
        wanted = [v["summary"] for v in
                  screener.backtest_universe(raw, args.min_pnl, CONFIG)]
        print(f"{len(raw)} Vaults -> {len(wanted)} fuers Universum "
              f"(davon {sum(bool(s.get('isClosed')) for s in wanted)} geschlossen)",
              flush=True)
        meta = {s["vaultAddress"]: {"name": s.get("name", "?"),
                                    "leader": s.get("leader", "")} for s in wanted}
    else:
        cands = screener.prefilter(raw, CONFIG)
        print(f"{len(raw)} Vaults -> {len(cands)} nach Vorfilter", flush=True)
        cands = cands[: CONFIG.top_n_detail]
        meta = {c.address: {"name": c.name, "leader": c.leader} for c in cands}

    def save() -> None:
        tmp = args.out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"details": details, "meta": meta,
                       "fetched_at": fetched_at}, fh)
        os.replace(tmp, args.out)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    details: dict = {}
    fetched_at = int(time.time() * 1000)
    if args.all and os.path.exists(args.out):
        # Abgebrochenen Lauf fortsetzen. Der alte Zeitstempel bleibt, sonst
        # wuerden die Stichtage ueber das Datenende der fruehen Abrufe rutschen.
        with open(args.out) as fh:
            prev = json.load(fh)
        details, fetched_at = prev["details"], prev["fetched_at"]
        print(f"Setze fort: {len(details)} schon im Cache", flush=True)

    todo = [a for a in meta if a not in details]
    # shortcut: alle 200 Vaults speichern, ein Abbruch kostet hoechstens so viele
    for start in range(0, len(todo), 200):
        chunk = todo[start:start + 200]
        details.update(api.fetch_details_bulk(chunk))
        save()
        print(f"  Details {start + len(chunk)}/{len(todo)}", flush=True)
    save()

    missing = len(meta) - len(details)
    print(f"\n{len(details)} Details gecacht -> {args.out}")
    if missing:
        print(f"WARNUNG: {missing} Vaults fehlen (Abruf fehlgeschlagen). "
              f"Erneut starten holt sie nach." if args.all else
              f"WARNUNG: {missing} Vaults fehlen (Abruf fehlgeschlagen).")
    return 0


def cmd_backtest(args) -> int:
    """Walk-Forward-Backtest: schlaegt der Score APR und Gleichgewicht?"""
    import datetime as _dt
    import os

    from . import backtest as bt

    if not os.path.exists(args.cache):
        print(f"Kein Cache unter {args.cache}. Erst:  "
              f"python3 -m hlvault.cli fetch-cache")
        return 1

    horizons = tuple(int(x) for x in args.horizons.split(","))
    res = bt.walk_forward(args.cache, horizons=horizons,
                          n_cutoffs=args.cutoffs, gap_days=args.gap, k=args.k)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(res, fh, indent=2)

    def d(ms):
        return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).date()

    def f(x):
        return f"{x*100:+8.1f}%" if x is not None else "     n/a"

    print(f"\nWalk-Forward-Backtest | Cache {res['n_vaults']} Vaults, davon "
          f"{res['n_closed']} geschlossen | K={res['k']} | Stichtage={args.cutoffs}")
    print("Ranking je Stichtag NUR aus Daten bis dahin. Renditen nach Profit-Share.\n")
    for H, blk in res["horizons"].items():
        print(f"===== Forward-Horizont {H} Tage, Abstand {blk['gap_days']} Tage =====")
        if blk["overlapping"]:
            print("ACHTUNG: Fenster ueberlappen, die Stichtage sind nicht unabhaengig.")
        print(f"{'Stichtag':12} {'qual':>4} {'Score-Top':>10} {'CAGR-Filt':>10}"
              f" {'CAGR-Naiv':>10} {'Gleichgew':>10} {'Score-Bot':>10} {'Universum':>10}")
        for w in blk["windows"]:
            s = w["strat"]
            print(f"{str(d(w['cutoff_ms'])):12} {w['qualified']:>4}  "
                  f"{f(s.get('score_top'))}  {f(s.get('cagr_top_filtered'))}  "
                  f"{f(s.get('cagr_top_naive'))}  {f(s.get('equal_qualified'))}  "
                  f"{f(s.get('score_bottom'))}  {f(s.get('universe'))}")
        sm = blk["summary"]
        labels = {"score_top": "Score-Top-K", "cagr_top_filtered": "CAGR-Top-K gefiltert",
                  "cagr_top_naive": "CAGR-Top-K naiv", "equal_qualified": "Gleichgewicht",
                  "equal_no_leader": "Gleichgew. o. Leader",
                  "score_bottom": "Score-Bottom-K", "universe": "Universum"}
        print(f"\n  {'Strategie':22}{'Mittel':>9}{'Median':>9}{'Worst':>9}"
              f"{'MaxDD':>9}{'Brutto':>9}  {'90%-Intervall':>17}   n")
        for name, label in labels.items():
            if name in sm:
                x = sm[name]
                print(f"  {label:22}{f(x['mean_fwd'])} {f(x['median'])} {f(x['worst'])} "
                      f"{f(x['worst_dd'])} {f(x['mean_gross'])}  "
                      f"{x['ci90_low']*100:+7.1f}..{x['ci90_high']*100:+.1f}%"
                      f"   {x['windows']}")
        print()
    if args.json:
        print(f"JSON -> {args.json}")
    return 0


def cmd_paper(args) -> int:
    """Papertrading: virtuelles Portfolio gegen echte Vault-Renditen."""
    import datetime as _dt

    from . import paper

    conn = db.connect()

    if args.paper_cmd == "init":
        paper.init_portfolio(conn, args.capital)
        print(f"Virtuelles Portfolio angelegt: ${args.capital:,.2f} Startkapital.")
        print("Naechster Schritt:  python3 -m hlvault.cli paper rebalance")
        return 0

    pf = paper.get_portfolio(conn)
    if not pf:
        print("Kein Portfolio. Erst:  python3 -m hlvault.cli paper init <kapital>")
        return 1

    if args.paper_cmd == "rebalance":
        results, total = screener.run(CONFIG, verbose=not args.quiet)
        # Positionen werden in paper einzeln gegen Live-Detaildaten bewertet.
        actions = paper.rebalance(conn, results, {}, CONFIG)
        print("\nRebalance (virtuell, keine echten Transaktionen):")
        for a in actions:
            usd = f"${a['usd']:>10,.2f}" if a["usd"] else " " * 11
            print(f"  {a['action']:12} {a['name'][:28]:30}{usd}  {a.get('note','')}")
        _print_paper_status(conn, paper, _dt)
        return 0

    if args.paper_cmd == "status":
        _print_paper_status(conn, paper, _dt)
        return 0

    if args.paper_cmd == "ledger":
        rows = paper.ledger(conn, args.limit)
        print(f"{'Datum':20}{'Aktion':12}{'USD':>12}  Vault / Notiz")
        for r in rows:
            d = _dt.datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d %H:%M")
            usd = f"${r['usd']:,.2f}" if r["usd"] is not None else ""
            nm = r["name"] or ""
            print(f"{d:20}{r['action']:12}{usd:>12}  {nm} {r['note'] or ''}")
        return 0
    return 0


def _print_paper_status(conn, paper, _dt) -> None:
    mtm = paper.mark_to_market(conn, {}, CONFIG)
    if not mtm:
        print("Kein Portfolio.")
        return
    created = _dt.datetime.fromtimestamp(mtm["created_ts"] / 1000).strftime("%Y-%m-%d")
    print(f"\n=== Papertrading-Status (seit {created}) ===")
    print(f"  Startkapital : ${mtm['start_capital']:>12,.2f}")
    print(f"  Cash         : ${mtm['cash']:>12,.2f}")
    print(f"  Investiert   : ${mtm['invested_value']:>12,.2f}")
    print(f"  GESAMTWERT   : ${mtm['total']:>12,.2f}  "
          f"({mtm['total_return_pct']:+.2%})")
    if mtm["positions"]:
        print(f"\n  {'Vault':30}{'Einsatz':>12}{'Wert':>12}{'Rendite':>10}"
              f"{'Alter':>7}{'Fee':>9}")
        for p in sorted(mtm["positions"], key=lambda x: -x["value"]):
            lock = " (Lockup)" if p["locked"] else ""
            print(f"  {p['name'][:28]:30}${p['usd_in']:>10,.2f}"
                  f"${p['value']:>10,.2f}{p['return_pct']:>+9.1%}"
                  f"{p['age_days']:>6.0f}d${p['fee']:>7,.2f}{lock}")
    print("\n  Kein echtes Geld. Rendite = reale Vault-Performance seit Einstieg.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hlvault",
                                description="Hyperliquid Vault Screener (read-only)")
    p.add_argument("-q", "--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen", help="Vaults screenen und ranken")
    s.add_argument("-n", "--limit", type=int, default=15)
    s.add_argument("--show-failed", action="store_true")
    s.add_argument("--json", help="Ergebnis als JSON speichern")
    s.add_argument("--no-save", action="store_true")
    s.set_defaults(func=cmd_screen)

    a = sub.add_parser("alloc", help="Allokationsvorschlag")
    a.add_argument("capital", type=float)
    a.set_defaults(func=cmd_alloc)

    h = sub.add_parser("hist", help="Score-Historie eines Vaults")
    h.add_argument("address")
    h.add_argument("-n", "--limit", type=int, default=30)
    h.set_defaults(func=cmd_hist)

    w = sub.add_parser("watch", help="Taeglicher Check + Alerts")
    w.add_argument("-n", "--limit", type=int, default=10)
    w.set_defaults(func=cmd_watch)

    fc = sub.add_parser("fetch-cache",
                        help="Detaildaten einmal holen und cachen (fuer backtest)")
    fc.add_argument("--out", default=CACHE_PATH)
    fc.add_argument("--all", action="store_true",
                    help="auch geschlossene/geschrumpfte Vaults (lange Laufzeit, fortsetzbar)")
    fc.add_argument("--min-pnl", type=float, default=5000.0,
                    help="mit --all: Mindest-|PnL| fuer Vaults unter dem TVL-Filter")
    fc.set_defaults(func=cmd_fetch_cache)

    bt = sub.add_parser("backtest",
                        help="Walk-Forward-Backtest gegen historische PnL-Serien")
    bt.add_argument("--cache", default=CACHE_PATH)
    bt.add_argument("--horizons", default="60,90",
                    help="Forward-Fenster in Tagen, komma-getrennt")
    bt.add_argument("--cutoffs", type=int, default=6, help="Anzahl Stichtage")
    bt.add_argument("--gap", type=int, default=None,
                    help="Abstand der Stichtage in Tagen (default: Horizont, ohne Ueberlappung)")
    bt.add_argument("-k", type=int, default=4, help="Portfoliogroesse (Top-K)")
    bt.add_argument("--json", help="Rohergebnis als JSON speichern")
    bt.set_defaults(func=cmd_backtest)

    pp = sub.add_parser("paper",
                        help="Papertrading: virtuelles Portfolio, kein echtes Geld")
    psub = pp.add_subparsers(dest="paper_cmd", required=True)
    pi = psub.add_parser("init", help="Virtuelles Portfolio anlegen")
    pi.add_argument("capital", type=float)
    psub.add_parser("rebalance", help="Screenen + virtuell allozieren/rebalancen")
    psub.add_parser("status", help="Aktueller Portfoliowert (mark-to-market)")
    pl = psub.add_parser("ledger", help="Transaktions-/Ereignis-Log")
    pl.add_argument("-n", "--limit", type=int, default=50)
    pp.set_defaults(func=cmd_paper)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
