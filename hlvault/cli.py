"""CLI: screen / alloc / hist / watch.

Phase 1 ist bewusst read-only - es werden keine Transaktionen gesendet.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import allocator, db, screener
from .config import CONFIG


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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
