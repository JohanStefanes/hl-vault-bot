"""Screener: Vorfilter auf der Vault-Liste, dann Scoring auf Detaildaten."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from . import api, db, metrics as m
from .config import CONFIG, Config


@dataclass
class Result:
    address: str
    name: str
    leader: str
    tvl: float
    apr: float
    age_days: float
    leader_fraction: float = 0.0
    leader_commission: float = 0.0
    followers: int = 0
    metrics: m.Metrics | None = None
    score: float = 0.0
    parts: dict = field(default_factory=dict)
    passed: bool = False
    reasons: list[str] = field(default_factory=list)


def prefilter(raw_vaults: list[dict], cfg: Config = CONFIG) -> list[Result]:
    """Billiger Vorfilter auf der 9.5k-Liste - spart tausende Detail-Requests."""
    f = cfg.filters
    now_ms = time.time() * 1000
    out: list[Result] = []
    for v in raw_vaults:
        s = v.get("summary") or {}
        if f.require_open and s.get("isClosed"):
            continue
        if (s.get("relationship") or {}).get("type") == "child":
            continue
        try:
            tvl = float(s.get("tvl", 0))
        except (TypeError, ValueError):
            continue
        if not (f.min_tvl <= tvl <= f.max_tvl):
            continue
        age = (now_ms - s.get("createTimeMillis", now_ms)) / 86_400_000
        if age < f.min_age_days:
            continue
        out.append(Result(
            address=s["vaultAddress"], name=s.get("name", "?"),
            leader=s.get("leader", ""), tvl=tvl,
            apr=float(v.get("apr") or 0.0), age_days=age,
        ))
    out.sort(key=lambda r: -r.tvl)
    return out


def backtest_universe(raw_vaults: list[dict], min_pnl: float,
                      cfg: Config = CONFIG) -> list[dict]:
    """Vaults fuer den Backtest, inkl. geschlossener und geschrumpfter.

    Der Vorfilter sieht nur den Stand von heute und wirft damit genau die
    Vaults raus, die zwischenzeitlich abgestuerzt sind (Survivorship-Bias).
    Darum zaehlt hier auch |PnL| >= min_pnl: das behaelt Gewinner wie Verlierer,
    die mal echtes Kapital hatten, und spart tausende leere Vaults.
    """
    now_ms = time.time() * 1000
    out = []
    for v in raw_vaults:
        s = v.get("summary") or {}
        if (s.get("relationship") or {}).get("type") == "child":
            continue
        age = (now_ms - s.get("createTimeMillis", now_ms)) / 86_400_000
        if age < cfg.filters.min_age_days:
            continue
        pnl = dict(v.get("pnls") or []).get("allTime") or []
        peak_pnl = max((abs(float(x)) for x in pnl), default=0.0)
        if float(s.get("tvl", 0)) >= cfg.filters.min_tvl or peak_pnl >= min_pnl:
            out.append(v)
    return out


def _norm(x: float, lo: float, hi: float) -> float:
    """Auf 0..100 clampen."""
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) * 100.0


def score_one(r: Result, cfg: Config = CONFIG) -> Result:
    f, w = cfg.filters, cfg.weights
    mt = r.metrics
    if mt is None:
        r.passed = False
        r.reasons.append("keine verwertbare Historie")
        return r

    # --- harte Kriterien ---
    if mt.n < f.min_history_points:
        r.reasons.append(f"nur {mt.n} Datenpunkte")
    if mt.max_drawdown < -f.max_drawdown:
        r.reasons.append(f"MaxDD {mt.max_drawdown:.1%} > {f.max_drawdown:.0%}")
    if mt.vol_annual > f.max_vol_annual:
        r.reasons.append(f"Vol {mt.vol_annual:.0%} > {f.max_vol_annual:.0%} (Leverage-Casino)")
    if r.leader_fraction < f.min_leader_fraction:
        r.reasons.append(f"Leader nur {r.leader_fraction:.1%} eigenes Kapital")
    if r.leader_commission > f.max_leader_commission:
        r.reasons.append(f"Commission {r.leader_commission:.0%} zu hoch")
    if mt.cagr <= 0:
        r.reasons.append("CAGR negativ")

    # --- Score-Komponenten (jeweils 0..100) ---
    parts = {
        "sharpe": _norm(mt.sharpe, 0.0, 3.0),
        # weniger Drawdown = besser
        "drawdown": _norm(-mt.max_drawdown, f.max_drawdown, 0.0),
        "age": _norm(math.log10(max(r.age_days, 1)), math.log10(120), math.log10(900)),
        # TVL-Stabilitaet: Abfluss straft, moderater Zufluss belohnt
        "tvl_stability": _norm(mt.tvl_change_30d, -0.40, 0.30),
        # APR bewusst gedeckelt, damit 1000%-Leverage-Vaults nicht gewinnen
        "apr": _norm(min(mt.cagr, 2.0), 0.0, 1.0),
    }
    r.parts = {k: round(v, 1) for k, v in parts.items()}
    r.score = round(
        parts["sharpe"] * w.sharpe
        + parts["drawdown"] * w.drawdown
        + parts["age"] * w.age
        + parts["tvl_stability"] * w.tvl_stability
        + parts["apr"] * w.apr, 2)
    r.passed = not r.reasons
    return r


def run(cfg: Config = CONFIG, verbose: bool = True) -> tuple[list[Result], int]:
    """Kompletter Durchlauf: fetch -> prefilter -> details -> score."""
    if verbose:
        print("Lade Vault-Liste ...", flush=True)
    raw = api.fetch_all_vaults()
    db.save_vault_list(db.connect(), raw)
    cands = prefilter(raw, cfg)
    if verbose:
        print(f"{len(raw)} Vaults -> {len(cands)} nach Vorfilter", flush=True)

    cands = cands[: cfg.top_n_detail]

    def prog(i, n):
        if verbose and (i % 10 == 0 or i == n):
            print(f"  Details {i}/{n}", flush=True)

    details = api.fetch_details_bulk([c.address for c in cands], on_progress=prog)

    for c in cands:
        d = details.get(c.address)
        if not d:
            c.reasons.append("Detailabruf fehlgeschlagen")
            continue
        if cfg.filters.require_open and not d.get("allowDeposits", True):
            c.reasons.append("nimmt keine Deposits an")
        c.leader_fraction = float(d.get("leaderFraction") or 0.0)
        c.leader_commission = float(d.get("leaderCommission") or 0.0)
        c.followers = len(d.get("followers") or [])
        c.metrics = m.compute(d, "allTime")
        if c.metrics:
            c.tvl = c.metrics.tvl_now
        score_one(c, cfg)

    cands.sort(key=lambda r: (-r.passed, -r.score))
    return cands, len(raw)
