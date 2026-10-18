"""Walk-Forward-Backtest fuer das Vault-Scoring.

Die Kernfrage von Phase 2: taugt der Score ueberhaupt was? Anders gefragt -
haetten die vom Score bevorzugten Vaults, ausgewaehlt AUSSCHLIESSLICH mit Daten
bis zu einem Stichtag T, danach tatsaechlich besser abgeschnitten als eine
naive Auswahl (nach APR) und als der Gleichgewichts-Durchschnitt?

Vorgehen (walk-forward, kein In-Sample):
  1. Fuer mehrere Stichtage T:
     - schneide jede Vault-Serie auf t <= T ab  (nur Vergangenheit sichtbar)
     - berechne Metriken + Score NUR aus diesen Daten
     - ranke die Kandidaten
  2. Miss die REALISIERTE Rendite jedes Vaults im Fenster (T, T+H].
  3. Vergleiche mehrere Auswahl-Strategien anhand dieser Forward-Rendite:
     - Score-Top-K      (das getestete Modell)
     - APR-Top-K        (naiv, die Versuchung)
     - Gleichgewicht    (alle qualifizierten - der ehrliche Benchmark)
     - Score-Bottom-K   (Gegenprobe: sind die schlechten wirklich schlechter?)

Datenquelle ist der Detail-Cache (data/backtest_cache/snapshot.json), damit der
Backtest reproduzierbar ohne Netz laeuft. Erzeugen mit:
    python3 -m hlvault.cli fetch-cache        (siehe cli.py)

Ehrliche Einschraenkungen (auch im README):
  - leaderFraction/leaderCommission gibt es nur als aktuellen Wert, nicht als
    Verlauf. Fuer historische Stichtage wird der HEUTIGE Wert benutzt -> ein
    Lookahead-Rest, der die Filter leicht schoenrechnet. Score/Ranking selbst
    ist sauber out-of-sample.
  - accountValueHistory ist gebinnt, nicht taeglich. Kurze Forward-Fenster sind
    dadurch grob. H >= 60 Tage nehmen.
  - Survivorship-Bias: geblowte/geschlossene Vaults sind aus der Liste raus.
    Der Backtest ueberschaetzt die Klasse dadurch strukturell. Nicht wegzudenken
    ohne historische Listen-Snapshots.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass, field

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


def forward_return(detail: dict, t_start_ms: int, t_end_ms: int,
                   period: str = "allTime") -> float | None:
    """Realisierte Rendite im Fenster (t_start, t_end], equity-relativ.

    Genau die Renditedefinition aus metrics: r = dPnL / equity_vorher.
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
    counted = 0
    for i in range(start + 1, len(ts)):
        if ts[i] > t_end_ms:
            break
        base = equity[i - 1]
        if base <= 1.0:
            continue
        r = (cum[i] - cum[i - 1]) / base
        acc *= (1.0 + r)
        counted += 1
    if counted == 0:
        return None
    return acc - 1.0


# --------------------------------------------------------------------------
# Ein Stichtag
# --------------------------------------------------------------------------
@dataclass
class Pick:
    address: str
    name: str
    score: float
    apr: float
    cagr_hist: float
    sharpe_hist: float
    maxdd_hist: float
    fwd: float | None


@dataclass
class WindowResult:
    cutoff_ms: int
    horizon_days: int
    picks: list[Pick]
    qualified: int
    # strategie -> mittlere forward-rendite
    strat: dict = field(default_factory=dict)


def _load_meta(snap: dict) -> dict:
    return snap.get("meta") or {}


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
            apr=float(md.get("apr") or 0.0),
            age_days=age_days,
        )
        # leaderFraction/-commission: nur aktueller Wert vorhanden (siehe Docstring)
        r.leader_fraction = float(det.get("leaderFraction") or 0.0)
        r.leader_commission = float(det.get("leaderCommission") or 0.0)
        r.metrics = mt
        screener.score_one(r, cfg)
        results.append(r)
    results.sort(key=lambda x: (-x.passed, -x.score))
    return results


def run_window(detail_by_addr: dict, meta: dict, cutoff_ms: int,
               horizon_days: int, k: int = 4,
               cfg: Config = CONFIG) -> WindowResult:
    end_ms = cutoff_ms + horizon_days * MS_DAY
    scored = score_at(detail_by_addr, meta, cutoff_ms, cfg)

    picks: list[Pick] = []
    for r in scored:
        fwd = forward_return(detail_by_addr[r.address], cutoff_ms, end_ms)
        picks.append(Pick(
            address=r.address, name=r.name, score=r.score, apr=r.apr,
            cagr_hist=r.metrics.cagr, sharpe_hist=r.metrics.sharpe,
            maxdd_hist=r.metrics.max_drawdown, fwd=fwd,
        ))

    # nur Vaults die eine Forward-Rendite HABEN sind vergleichbar
    valid = [p for p in picks if p.fwd is not None]
    qualified_valid = [p for p in valid
                       if next(x for x in scored if x.address == p.address).passed]

    def mean_fwd(ps: list[Pick]) -> float | None:
        xs = [p.fwd for p in ps if p.fwd is not None]
        return statistics.fmean(xs) if xs else None

    # Score-Top-K: nur qualifizierte, nach Score
    score_top = qualified_valid[:k]
    # Score-Bottom-K: qualifizierte, schlechtester Score zuerst
    score_bottom = sorted(qualified_valid, key=lambda p: p.score)[:k]
    # APR-Top-K: naiv, alle validen nach APR
    apr_top = sorted(valid, key=lambda p: -p.apr)[:k]
    # Gleichgewicht: alle qualifizierten
    strat = {
        "score_top": mean_fwd(score_top),
        "apr_top": mean_fwd(apr_top),
        "equal_qualified": mean_fwd(qualified_valid),
        "score_bottom": mean_fwd(score_bottom),
        "universe": mean_fwd(valid),
    }
    return WindowResult(
        cutoff_ms=cutoff_ms, horizon_days=horizon_days,
        picks=score_top, qualified=len(qualified_valid), strat=strat,
    )


# --------------------------------------------------------------------------
# Walk-forward ueber mehrere Stichtage
# --------------------------------------------------------------------------
def walk_forward(snapshot_path: str, horizons=(90,), n_cutoffs: int = 6,
                 gap_days: int = 60, k: int = 4,
                 cfg: Config = CONFIG) -> dict:
    snap = json.load(open(snapshot_path))
    details = snap["details"]
    meta = _load_meta(snap)
    fetched = snap.get("fetched_at") or int(time.time() * 1000)

    out = {"horizons": {}, "fetched_at": fetched, "k": k,
           "n_vaults": len(details)}

    for H in horizons:
        # Stichtage: gleichmaessig, letzter so dass H noch komplett in die
        # Daten passt (fetched - H). Rueckwaerts in gap-Schritten.
        last_cut = fetched - H * MS_DAY
        cutoffs = [last_cut - i * gap_days * MS_DAY
                   for i in range(n_cutoffs)][::-1]
        windows = [run_window(details, meta, c, H, k, cfg) for c in cutoffs]

        # aggregiert ueber alle Stichtage
        agg: dict[str, list[float]] = {}
        for w in windows:
            for name, val in w.strat.items():
                if val is not None:
                    agg.setdefault(name, []).append(val)
        summary = {name: {
            "mean_fwd": statistics.fmean(vals),
            "windows": len(vals),
        } for name, vals in agg.items()}
        out["horizons"][str(H)] = {
            "cutoffs": cutoffs,
            "windows": [{
                "cutoff_ms": w.cutoff_ms,
                "qualified": w.qualified,
                "strat": w.strat,
                "top": [{"name": p.name, "score": p.score, "fwd": p.fwd}
                        for p in w.picks],
            } for w in windows],
            "summary": summary,
        }
    return out


__all__ = ["walk_forward", "run_window", "score_at", "forward_return"]
