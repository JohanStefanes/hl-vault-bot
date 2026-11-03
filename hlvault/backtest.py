"""Walk-Forward-Backtest fuer das Vault-Scoring.

Die Kernfrage von Phase 2: taugt der Score ueberhaupt was? Anders gefragt -
haetten die vom Score bevorzugten Vaults, ausgewaehlt AUSSCHLIESSLICH mit Daten
bis zu einem Stichtag T, danach tatsaechlich besser abgeschnitten als eine
naive Auswahl (nach bisheriger Rendite) und als der Gleichgewichts-Durchschnitt?

Vorgehen (walk-forward, kein In-Sample):
  1. Fuer mehrere Stichtage T:
     - schneide jede Vault-Serie auf t <= T ab  (nur Vergangenheit sichtbar)
     - berechne Metriken + Score NUR aus diesen Daten
     - ranke die Kandidaten
  2. Miss die REALISIERTE Rendite jedes Vaults im Fenster (T, T+H].
  3. Vergleiche mehrere Auswahl-Strategien anhand dieser Forward-Rendite,
     nach Leader-Profit-Share:
     - Score-Top-K        (das getestete Modell)
     - CAGR-Top-K gefiltert (hoechste bisherige Rendite unter den Qualifizierten)
     - CAGR-Top-K naiv    (dasselbe ohne Risikofilter, die Versuchung)
     - Gleichgewicht      (alle qualifizierten - der ehrliche Benchmark)
     - Gleichgewicht ohne Leader-Filter (Gegenprobe zum Lookahead unten)
     - Score-Bottom-K     (Gegenprobe: sind die schlechten wirklich schlechter?)
     - Universum          (alle, die am Stichtag den Vorfilter bestehen)

Datenquelle ist der Detail-Cache (data/backtest_cache/snapshot.json), damit der
Backtest reproduzierbar ohne Netz laeuft. Erzeugen mit:
    python3 -m hlvault.cli fetch-cache         nur heutige Kandidaten
    python3 -m hlvault.cli fetch-cache --all   inkl. geschlossener Vaults

Ehrliche Einschraenkungen (auch im README):
  - leaderFraction/leaderCommission gibt es nur als aktuellen Wert, nicht als
    Verlauf. Fuer historische Stichtage wird der HEUTIGE Wert benutzt -> ein
    Lookahead-Rest in den Leader-Filtern. Darum die Gegenprobe ohne sie.
  - accountValueHistory ist gebinnt, nicht taeglich. Kurze Forward-Fenster sind
    dadurch grob. H >= 60 Tage nehmen.
  - Survivorship-Bias: ohne --all fehlen geschlossene und geschrumpfte Vaults,
    und der Backtest ueberschaetzt die Klasse strukturell. Mit --all bleibt ein
    Rest (Vaults ohne nennenswerten PnL fehlen), bis genug Listen-Snapshots aus
    db.save_vault_list da sind.
"""
from __future__ import annotations

import json
import random
import statistics
import time
from dataclasses import dataclass, field, replace

from . import metrics as m, screener
from .config import CONFIG, Config

MS_DAY = 86_400_000


# --------------------------------------------------------------------------
# Serien-Handling
# --------------------------------------------------------------------------
def _series(portfolio: list, key: str) -> dict:
    for name, payload in portfolio:
        if name == key:
            return payload
    return {}


def _clip_detail(detail: dict, cutoff_ms: int, period: str = "allTime") -> dict | None:
    """Kopie des detail-dicts, deren allTime-Serie auf t <= cutoff beschnitten ist.

    Alle anderen Felder bleiben unveraendert. Gibt None zurueck wenn nach dem
    Schnitt zu wenig Historie uebrig ist.
    """
    pf = detail.get("portfolio") or []
    block = _series(pf, period)
    avh = block.get("accountValueHistory") or []
    pnl = block.get("pnlHistory") or []
    if len(avh) < 5 or len(pnl) != len(avh):
        return None

    avh_c = [p for p in avh if int(p[0]) <= cutoff_ms]
    pnl_c = pnl[: len(avh_c)]
    if len(avh_c) < 5:
        return None

    new_pf = []
    for name, payload in pf:
        if name == period:
            np = dict(payload)
            np["accountValueHistory"] = avh_c
            np["pnlHistory"] = pnl_c
            new_pf.append([name, np])
        else:
            new_pf.append([name, payload])
    d = dict(detail)
    d["portfolio"] = new_pf
    return d


def forward_curve(detail: dict, t_start_ms: int, t_end_ms: int,
                  period: str = "allTime") -> list[tuple[int, float]] | None:
    """Wertverlauf (ts, Wert ab 1.0) im Fenster (t_start, t_end].

    Genau die Renditedefinition aus metrics.period_return.
    Basis ist der letzte Punkt <= t_start; gezaehlt werden Punkte in
    (t_start, t_end]. None wenn das Fenster keine verwertbaren Punkte hat.
    """
    pf = detail.get("portfolio") or []
    block = _series(pf, period)
    avh = block.get("accountValueHistory") or []
    pnl = block.get("pnlHistory") or []
    if len(avh) < 2 or len(pnl) != len(avh):
        return None

    ts = [int(t) for t, _ in avh]
    equity = [float(v) for _, v in avh]
    cum = [float(v) for _, v in pnl]

    # Startindex: letzter Punkt <= t_start
    start = None
    for i, t in enumerate(ts):
        if t <= t_start_ms:
            start = i
        else:
            break
    if start is None:
        return None

    acc = 1.0
    curve = [(ts[start], 1.0)]
    for i in range(start + 1, len(ts)):
        if ts[i] > t_end_ms:
            break
        r = m.period_return(equity[i - 1], equity[i], cum[i - 1], cum[i])
        if r is None:
            continue
        acc *= (1.0 + r)
        curve.append((ts[i], acc))
    return curve if len(curve) > 1 else None


def forward_return(detail: dict, t_start_ms: int, t_end_ms: int,
                   period: str = "allTime") -> float | None:
    """Realisierte Rendite im Fenster (t_start, t_end], equity-relativ."""
    curve = forward_curve(detail, t_start_ms, t_end_ms, period)
    return curve[-1][1] - 1.0 if curve else None


def net_of_fee(gross: float, commission: float) -> float:
    """Rendite nach Leader-Profit-Share, wie paper.value_position.

    Jedes Fenster zaehlt als frischer Einstieg (HWM = Einstand). Das ist
    konservativ: wer durchhaelt, zahlt nach Verlustfenstern real weniger.
    """
    return gross - commission * max(gross, 0.0)


def basket_drawdown(curves: list[list[tuple[int, float]]]) -> float:
    """Max. Drawdown eines gleichgewichteten Buy-and-Hold-Korbs (negativ)."""
    if not curves:
        return 0.0
    points = sorted({t for c in curves for t, _ in c})
    idx = [0] * len(curves)
    peak, max_dd = 0.0, 0.0
    for t in points:
        for j, c in enumerate(curves):
            while idx[j] + 1 < len(c) and c[idx[j] + 1][0] <= t:
                idx[j] += 1
        value = statistics.fmean(c[idx[j]][1] for j, c in enumerate(curves))
        peak = max(peak, value)
        if peak > 0:
            max_dd = min(max_dd, value / peak - 1.0)
    return max_dd


# --------------------------------------------------------------------------
# Ein Stichtag
# --------------------------------------------------------------------------
STRATEGIES = ("score_top", "cagr_top_filtered", "cagr_top_naive", "equal_qualified",
              "equal_no_leader", "score_bottom", "universe")


@dataclass
class Pick:
    address: str
    name: str
    score: float
    cagr_hist: float
    sharpe_hist: float
    maxdd_hist: float
    commission: float
    fwd: float | None


@dataclass
class WindowResult:
    cutoff_ms: int
    horizon_days: int
    picks: list[Pick]
    qualified: int
    # strategie -> Rendite nach Fee / vor Fee / max. Drawdown im Fenster
    strat: dict = field(default_factory=dict)
    strat_gross: dict = field(default_factory=dict)
    strat_dd: dict = field(default_factory=dict)


def _load_meta(snap: dict) -> dict:
    return snap.get("meta") or {}


def _eligible(tvl: float, age_days: float, cfg: Config) -> bool:
    """Der Vorfilter, angewandt auf den Stand am Stichtag statt auf heute."""
    f = cfg.filters
    return f.min_tvl <= tvl <= f.max_tvl and age_days >= f.min_age_days


def score_at(detail_by_addr: dict, meta: dict, cutoff_ms: int,
             cfg: Config = CONFIG) -> list[screener.Result]:
    """Score aller Vaults NUR mit Daten bis cutoff. Reproduziert score_one."""
    results: list[screener.Result] = []
    for addr, det in detail_by_addr.items():
        clipped = _clip_detail(det, cutoff_ms)
        if clipped is None:
            continue
        mt = m.compute(clipped, "allTime")
        if mt is None:
            continue
        md = meta.get(addr, {})
        # Alter zum Stichtag: aus erstem Serienpunkt (Proxy, kein Lookahead)
        pf = det.get("portfolio") or []
        b = _series(pf, "allTime")
        avh = b.get("accountValueHistory") or []
        t0 = int(avh[0][0]) if avh else cutoff_ms
        age_days = max((cutoff_ms - t0) / MS_DAY, 1.0)

        r = screener.Result(
            address=addr,
            name=md.get("name", "?"),
            leader=md.get("leader", ""),
            tvl=mt.tvl_now,
            # NICHT das APR aus dem Cache: das ist der Stand von heute (Lookahead)
            apr=mt.cagr,
            age_days=age_days,
        )
        if not _eligible(r.tvl, r.age_days, cfg):
            r.reasons.append("Vorfilter am Stichtag nicht bestanden")
        # leaderFraction/-commission: nur aktueller Wert vorhanden (siehe Docstring)
        r.leader_fraction = float(det.get("leaderFraction") or 0.0)
        r.leader_commission = float(det.get("leaderCommission") or 0.0)
        r.metrics = mt
        screener.score_one(r, cfg)
        results.append(r)
    results.sort(key=lambda x: (-x.passed, -x.score))
    return results


def _without_leader_filters(cfg: Config) -> Config:
    """Gegenprobe zum Lookahead-Rest: Leader-Werte kennen wir nur von heute."""
    return replace(cfg, filters=replace(cfg.filters, min_leader_fraction=0.0,
                                        max_leader_commission=1.0))


def run_window(detail_by_addr: dict, meta: dict, cutoff_ms: int,
               horizon_days: int, k: int = 4,
               cfg: Config = CONFIG) -> WindowResult:
    end_ms = cutoff_ms + horizon_days * MS_DAY
    scored = score_at(detail_by_addr, meta, cutoff_ms, cfg)
    relaxed = {r.address for r in score_at(detail_by_addr, meta, cutoff_ms,
                                           _without_leader_filters(cfg)) if r.passed}

    curves: dict[str, list] = {}
    picks: list[Pick] = []
    for r in scored:
        curve = forward_curve(detail_by_addr[r.address], cutoff_ms, end_ms)
        if curve is None:
            continue  # nur Vaults die eine Forward-Rendite HABEN sind vergleichbar
        curves[r.address] = curve
        picks.append(Pick(
            address=r.address, name=r.name, score=r.score,
            cagr_hist=r.metrics.cagr, sharpe_hist=r.metrics.sharpe,
            maxdd_hist=r.metrics.max_drawdown, commission=r.leader_commission,
            fwd=curve[-1][1] - 1.0,
        ))

    by_addr = {r.address: r for r in scored}
    qualified = [p for p in picks if by_addr[p.address].passed]
    eligible = [p for p in picks
                if _eligible(by_addr[p.address].tvl, by_addr[p.address].age_days, cfg)]
    by_cagr = lambda ps: sorted(ps, key=lambda p: -p.cagr_hist)[:k]

    baskets = {
        "score_top": qualified[:k],
        "cagr_top_filtered": by_cagr(qualified),
        # "naiv": hoechste bisherige Rendite ohne Risikofilter
        "cagr_top_naive": by_cagr(eligible),
        "equal_qualified": qualified,
        "equal_no_leader": [p for p in picks if p.address in relaxed],
        "score_bottom": sorted(qualified, key=lambda p: p.score)[:k],
        "universe": eligible,
    }
    res = WindowResult(cutoff_ms=cutoff_ms, horizon_days=horizon_days,
                       picks=baskets["score_top"], qualified=len(qualified))
    for name, basket in baskets.items():
        if not basket:
            continue
        res.strat_gross[name] = statistics.fmean(p.fwd for p in basket)
        res.strat[name] = statistics.fmean(net_of_fee(p.fwd, p.commission)
                                           for p in basket)
        res.strat_dd[name] = basket_drawdown([curves[p.address] for p in basket])
    return res


# --------------------------------------------------------------------------
# Walk-forward ueber mehrere Stichtage
# --------------------------------------------------------------------------
def summarize(vals: list[float], seed: int = 42, n_boot: int = 2000) -> dict:
    """Mittel, Median, Extremwerte + Bootstrap-90%-Intervall des Mittels."""
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(vals, k=len(vals)))
                   for _ in range(n_boot))
    return {
        "mean_fwd": statistics.fmean(vals),
        "median": statistics.median(vals),
        "worst": min(vals),
        "best": max(vals),
        "share_positive": sum(v > 0 for v in vals) / len(vals),
        "ci90_low": means[int(0.05 * n_boot)],
        "ci90_high": means[int(0.95 * n_boot)],
        "windows": len(vals),
    }


def walk_forward(snapshot_path: str, horizons=(90,), n_cutoffs: int = 6,
                 gap_days: int | None = None, k: int = 4,
                 cfg: Config = CONFIG) -> dict:
    """gap_days=None: Abstand = Horizont, die Fenster ueberlappen also nicht."""
    with open(snapshot_path) as fh:
        snap = json.load(fh)
    details = snap["details"]
    meta = _load_meta(snap)
    fetched = snap.get("fetched_at") or int(time.time() * 1000)

    out = {"horizons": {}, "fetched_at": fetched, "k": k,
           "n_vaults": len(details),
           "n_closed": sum(bool(d.get("isClosed")) for d in details.values())}

    for H in horizons:
        gap = gap_days or H
        # Stichtage: gleichmaessig, letzter so dass H noch komplett in die
        # Daten passt (fetched - H). Rueckwaerts in gap-Schritten.
        last_cut = fetched - H * MS_DAY
        cutoffs = [last_cut - i * gap * MS_DAY
                   for i in range(n_cutoffs)][::-1]
        windows = [run_window(details, meta, c, H, k, cfg) for c in cutoffs]

        summary = {}
        for name in STRATEGIES:
            vals = [w.strat[name] for w in windows if name in w.strat]
            if not vals:
                continue
            summary[name] = summarize(vals)
            summary[name]["mean_gross"] = statistics.fmean(
                w.strat_gross[name] for w in windows if name in w.strat)
            summary[name]["worst_dd"] = min(
                w.strat_dd[name] for w in windows if name in w.strat)
        out["horizons"][str(H)] = {
            "cutoffs": cutoffs,
            "gap_days": gap,
            "overlapping": gap < H,
            "windows": [{
                "cutoff_ms": w.cutoff_ms,
                "qualified": w.qualified,
                "strat": w.strat,
                "strat_gross": w.strat_gross,
                "strat_dd": w.strat_dd,
                "top": [{"name": p.name, "score": p.score, "fwd": p.fwd}
                        for p in w.picks],
            } for w in windows],
            "summary": summary,
        }
    return out


__all__ = ["walk_forward", "run_window", "score_at", "forward_return",
           "forward_curve", "net_of_fee", "basket_drawdown", "summarize"]
