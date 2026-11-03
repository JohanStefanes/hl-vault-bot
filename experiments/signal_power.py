"""Experiment-Harness: welche Signale sagen die Forward-Performance vorher?

Nicht die Gewichte raten - messen. Fuer jeden Stichtag sammeln wir pro Vault
die historischen Signale (aus Daten bis T) UND die realisierte Forward-Rendite
im Fenster (T, T+H]. Dann Spearman-Rangkorrelation Signal<->Zukunft, gepoolt
ueber alle Stichtage.

Positive Korrelation = hoeheres Signal ging mit besserer Zukunft einher =
das Signal taugt als Ranking-Input. Negative/Null = raus damit.
"""
from __future__ import annotations

import json
import math
import statistics
import sys

from hlvault import backtest as bt, metrics as m
from hlvault.config import CONFIG

MS_DAY = 86_400_000


def spearman(xs, ys):
    """Rangkorrelation ohne scipy. Bindungen ueber Durchschnittsraenge."""
    n = len(xs)
    if n < 4:
        return None

    def ranks(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def collect(snapshot_path, horizon_days, n_cutoffs=6, gap_days=60):
    snap = json.load(open(snapshot_path))
    details = snap["details"]
    meta = snap.get("meta") or {}
    fetched = snap["fetched_at"]

    last_cut = fetched - horizon_days * MS_DAY
    cutoffs = [last_cut - i * gap_days * MS_DAY for i in range(n_cutoffs)][::-1]

    # pro Vault-Zeile: dict der Signale + fwd
    rows = []
    for cut in cutoffs:
        end = cut + horizon_days * MS_DAY
        for addr, det in details.items():
            clipped = bt._clip_detail(det, cut)
            if clipped is None:
                continue
            mt = m.compute(clipped, "allTime")
            if mt is None or mt.n < CONFIG.filters.min_history_points:
                continue
            fwd = bt.forward_return(det, cut, end)
            if fwd is None:
                continue
            # Alter zum Stichtag
            pf = det.get("portfolio") or []
            b = bt._series(pf, "allTime")
            avh = b.get("accountValueHistory") or []
            t0 = int(avh[0][0]) if avh else cut
            age = max((cut - t0) / MS_DAY, 1.0)
            rows.append({
                "sharpe": mt.sharpe,
                "sortino": mt.sortino,
                "maxdd": mt.max_drawdown,          # negativ; hoeher(=naeher 0) besser
                "cagr": mt.cagr,
                "vol": mt.vol_annual,              # niedriger besser -> erwart. neg. Korr
                "win_rate": mt.win_rate,
                "tvl_change": mt.tvl_change_30d,
                "age": age,
                "worst": mt.worst_period,
                "fwd": fwd,
            })
    return rows


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/backtest_cache/snapshot.json"
    for H in (60, 90):
        rows = collect(path, H)
        fwd = [r["fwd"] for r in rows]
        print(f"\n===== Horizont {H} Tage | {len(rows)} Vault-Stichtag-Paare =====")
        print("Spearman-Korrelation Signal -> Forward-Rendite:")
        signals = ["sharpe", "sortino", "maxdd", "cagr", "vol",
                   "win_rate", "tvl_change", "age", "worst"]
        cors = []
        for sig in signals:
            xs = [r[sig] for r in rows]
            rho = spearman(xs, fwd)
            cors.append((sig, rho))
        for sig, rho in sorted(cors, key=lambda x: -(x[1] or -9)):
            bar = ""
            if rho is not None:
                fill = int(abs(rho) * 30)
                bar = ("+" if rho >= 0 else "-") * fill
            val = f"{rho:+.3f}" if rho is not None else "  n/a"
            print(f"  {sig:12} {val}  {bar}")


if __name__ == "__main__":
    main()
