"""Strategie-Varianten empirisch vergleichen (walk-forward, out-of-sample).

Der Signal-Test (signal_power.py) hat gezeigt: kein Einzelsignal sagt die
Forward-Rendite vorher (|Spearman| < 0.1). Also NICHT an den Gewichten drehen -
das waere Overfitting auf Rauschen. Der belegte Edge ist Risikovermeidung.

Hier werden konkrete Portfolio-Regeln gegeneinander getestet, jeweils nur mit
Daten bis zum Stichtag:

  A  Baseline      alte Filter (250k-250M, 120d, maxDD<25%), Score-Top-4
  B  Breiter EW    weite Filter, ALLE Qualifizierten gleichgewichtet
  C  Risk-tight    weite Filter + strengerer maxDD<15% + vol-Cap, EW
  D  Risk-tight-8  wie C, aber auf 8 Positionen begrenzt (max Diversifik.)

Metriken je Strategie ueber die Stichtage: mittlere Forward-Rendite UND das
schlechteste Einzelfenster (Tail-Risk zaehlt hier mehr als der Schnitt).
"""
from __future__ import annotations

import json
import statistics
import sys

from hlvault import backtest as bt, metrics as m

MS_DAY = 86_400_000


def qualify(mt, age, cfg):
    """Harte Ausschluesse. cfg ist ein dict mit den Schwellen."""
    if mt.n < cfg["min_pts"]:
        return False
    if mt.max_drawdown < -cfg["max_dd"]:
        return False
    if mt.cagr <= 0:
        return False
    if age < cfg["min_age"]:
        return False
    if cfg.get("max_vol") and mt.vol_annual > cfg["max_vol"]:
        return False
    return True


def basket_at(details, meta, cut, cfg):
    """Qualifizierte Vaults am Stichtag + ihre Metriken (nur Vergangenheit)."""
    out = []
    for addr, det in details.items():
        clipped = bt._clip_detail(det, cut)
        if clipped is None:
            continue
        mt = m.compute(clipped, "allTime")
        if mt is None:
            continue
        pf = det.get("portfolio") or []
        b = bt._series(pf, "allTime")
        avh = b.get("accountValueHistory") or []
        t0 = int(avh[0][0]) if avh else cut
        age = max((cut - t0) / MS_DAY, 1.0)
        if not qualify(mt, age, cfg):
            continue
        out.append((addr, mt, age))
    return out


def eval_strategy(details, meta, cutoffs, H, cfg, select):
    """select(basket) -> Liste (addr, weight). Gibt Fenster-Renditen zurueck."""
    wins = []
    for cut in cutoffs:
        end = cut + H * MS_DAY
        basket = basket_at(details, meta, cut, cfg)
        picks = select(basket)
        if not picks:
            continue
        tot_w = sum(w for _, w in picks)
        acc = 0.0
        used = 0.0
        for addr, w in picks:
            fwd = bt.forward_return(details[addr], cut, end)
            if fwd is None:
                continue
            acc += (w / tot_w) * fwd
            used += w / tot_w
        if used > 0:
            wins.append(acc / used)  # renormiert auf tatsaechlich messbare
    return wins


def equal_weight(basket, k=None):
    b = sorted(basket, key=lambda x: -x[1].sharpe)  # nur fuer Kappung
    if k:
        b = b[:k]
    return [(addr, 1.0) for addr, _, _ in b]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/backtest_cache/wide.json"
    snap = json.load(open(path))
    details, meta, fetched = snap["details"], snap.get("meta") or {}, snap["fetched_at"]

    strategies = {
        "A Baseline (alt, DD<25, Top4)": (
            {"min_pts": 20, "max_dd": 0.25, "min_age": 120, "max_vol": None},
            lambda b: equal_weight(b, k=4)),
        "B Breiter EW (alle qual.)": (
            {"min_pts": 20, "max_dd": 0.25, "min_age": 90, "max_vol": None},
            lambda b: equal_weight(b)),
        "C Risk-tight EW (DD<15,vol<1.0)": (
            {"min_pts": 20, "max_dd": 0.15, "min_age": 90, "max_vol": 1.0},
            lambda b: equal_weight(b)),
        "D Risk-tight, max 8 Pos": (
            {"min_pts": 20, "max_dd": 0.15, "min_age": 90, "max_vol": 1.0},
            lambda b: equal_weight(b, k=8)),
    }

    for H in (60, 90):
        last_cut = fetched - H * MS_DAY
        cutoffs = [last_cut - i * 60 * MS_DAY for i in range(6)][::-1]
        print(f"\n===== Horizont {H} Tage (6 Stichtage) =====")
        print(f"{'Strategie':34}{'Mittel':>9}{'Median':>9}{'Worst':>9}{'#Pos~':>7}")
        print("-" * 68)
        for name, (cfg, sel) in strategies.items():
            wins = eval_strategy(details, meta, cutoffs, H, cfg, sel)
            # durchschnittl. Korbgroesse
            sizes = [len(sel(basket_at(details, meta, c, cfg))) for c in cutoffs]
            avg_sz = statistics.fmean(sizes) if sizes else 0
            if not wins:
                print(f"{name:34}{'n/a':>9}")
                continue
            mean = statistics.fmean(wins)
            med = statistics.median(wins)
            worst = min(wins)
            print(f"{name:34}{mean*100:>+8.1f}%{med*100:>+8.1f}%"
                  f"{worst*100:>+8.1f}%{avg_sz:>7.1f}")


if __name__ == "__main__":
    main()
