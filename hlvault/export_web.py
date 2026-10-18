"""Exportiert den aktuellen Bot-Zustand als eine JSON-Datei fuers Web-Dashboard.

Vercel ist serverless und kann weder auf die lokale SQLite noch den 14MB-Screen
zugreifen. Deshalb: der Bot erzeugt hier einen statischen Snapshot, das
Dashboard liest nur diese Datei. Neu erzeugen + neu deployen = aktualisieren.

    python3 -m hlvault.export_web            # nur DB-Stand (schnell, kein Netz)
    python3 -m hlvault.export_web --screen   # frisch screenen (Netz, ~15s)
"""
from __future__ import annotations

import argparse
import json
import os
import time

from . import db, paper, screener
from .config import CONFIG

OUT = os.path.join(os.path.dirname(__file__), "..", "web", "public", "data.json")


def _screener_block(do_screen: bool) -> dict:
    """Aktuelles Ranking. Entweder frisch screenen oder aus letztem DB-Snapshot."""
    conn = db.connect()
    if do_screen:
        results, total = screener.run(CONFIG, verbose=True)
        rows = [{
            "rank": i + 1, "name": r.name, "address": r.address,
            "score": r.score, "passed": r.passed,
            "sharpe": round(r.metrics.sharpe, 2) if r.metrics else None,
            "cagr": round(r.metrics.cagr, 4) if r.metrics else None,
            "max_drawdown": round(r.metrics.max_drawdown, 4) if r.metrics else None,
            "vol": round(r.metrics.vol_annual, 4) if r.metrics else None,
            "tvl": round(r.tvl, 0), "age_days": round(r.age_days, 0),
            "leader_fraction": round(r.leader_fraction, 4),
            "reasons": r.reasons,
        } for i, r in enumerate(results) if r.passed]
        return {"total_vaults": total, "passed": len(rows),
                "generated_ts": int(time.time() * 1000), "vaults": rows}

    # Fallback: letzter Snapshot aus der DB
    last = db.last_run_ts(conn)
    if not last:
        return {"total_vaults": 0, "passed": 0, "generated_ts": None, "vaults": []}
    rows = db.top_at(conn, last, n=50)
    return {
        "generated_ts": last, "passed": len(rows),
        "vaults": [{
            "rank": i + 1, "name": r["name"], "address": r["vault_address"],
            "score": r["score"], "passed": True,
            "sharpe": r["sharpe"], "cagr": r["apr"],
            "max_drawdown": r["max_drawdown"], "vol": None,
            "tvl": r["tvl"], "age_days": r["age_days"],
            "leader_fraction": r["leader_frac"], "reasons": [],
        } for i, r in enumerate(rows)],
    }


def _paper_block() -> dict | None:
    conn = db.connect()
    pf = paper.get_portfolio(conn)
    if not pf:
        return None
    try:
        mtm = paper.mark_to_market(conn, {}, CONFIG)
    except Exception as e:  # Netzfehler bei Bewertung -> ohne Live-Wert
        return {"error": str(e), "start_capital": pf["start_capital"],
                "cash": pf["cash"]}
    mtm["ledger"] = paper.ledger(conn, 30)
    return mtm


def build(do_screen: bool) -> dict:
    return {
        "generated_at": int(time.time() * 1000),
        "config": {
            "max_drawdown": CONFIG.filters.max_drawdown,
            "max_vol_annual": CONFIG.filters.max_vol_annual,
            "min_age_days": CONFIG.filters.min_age_days,
            "max_positions": CONFIG.portfolio.max_positions,
            "equal_weight": CONFIG.portfolio.equal_weight,
        },
        "screener": _screener_block(do_screen),
        "paper": _paper_block(),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="export_web")
    ap.add_argument("--screen", action="store_true",
                    help="frisch screenen (Netz) statt letzten DB-Snapshot")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)

    data = build(args.screen)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(data, fh, indent=2)
    p = data["paper"]
    sc = data["screener"]
    print(f"Export -> {os.path.abspath(args.out)}")
    print(f"  Screener: {sc['passed']} qualifizierte Vaults")
    if p and "total" in p:
        print(f"  Paper:    ${p['total']:,.2f} ({p['total_return_pct']:+.2%})")
    elif p:
        print("  Paper:    Portfolio vorhanden, Bewertung uebersprungen")
    else:
        print("  Paper:    kein Portfolio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
