"""Risiko-Kennzahlen aus den Vault-Zeitreihen.

Wichtig: Rendite wird aus pnlHistory RELATIV zum accountValueHistory berechnet,
nicht aus dem rohen PnL. Sonst sieht ein Vault der einfach nur viel Kapital
eingesammelt hat automatisch gut aus.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

MS_PER_YEAR = 365.0 * 24 * 3600 * 1000


@dataclass
class Metrics:
    n: int
    period_returns: list[float]
    span_days: float
    total_return: float
    cagr: float
    vol_annual: float
    sharpe: float
    sortino: float
    max_drawdown: float       # negativ, z.B. -0.14
    worst_period: float
    win_rate: float
    tvl_now: float
    tvl_change_30d: float

    def as_row(self) -> dict:
        return {
            "n": self.n,
            "span_days": round(self.span_days, 1),
            "total_return": round(self.total_return, 4),
            "cagr": round(self.cagr, 4),
            "vol_annual": round(self.vol_annual, 4),
            "sharpe": round(self.sharpe, 3),
            "sortino": round(self.sortino, 3),
            "max_drawdown": round(self.max_drawdown, 4),
            "worst_period": round(self.worst_period, 4),
            "win_rate": round(self.win_rate, 3),
            "tvl_now": round(self.tvl_now, 2),
            "tvl_change_30d": round(self.tvl_change_30d, 4),
        }


def _series(portfolio: list, key: str) -> dict:
    for name, payload in portfolio:
        if name == key:
            return payload
    return {}


def compute(detail: dict, period: str = "allTime") -> Metrics | None:
    """Kennzahlen aus vaultDetails. None wenn die Datenlage zu duenn ist."""
    pf = detail.get("portfolio") or []
    block = _series(pf, period)
    avh = block.get("accountValueHistory") or []
    pnl = block.get("pnlHistory") or []
    if len(avh) < 5 or len(pnl) != len(avh):
        return None

    ts = [int(t) for t, _ in avh]
    equity = [float(v) for _, v in avh]
    cum_pnl = [float(v) for _, v in pnl]

    rets: list[float] = []
    for i in range(1, len(equity)):
        base = equity[i - 1]
        if base <= 1.0:            # Vault war praktisch leer -> Rendite bedeutungslos
            continue
        rets.append((cum_pnl[i] - cum_pnl[i - 1]) / base)
    if len(rets) < 3:
        return None

    span_ms = ts[-1] - ts[0]
    span_days = span_ms / 86_400_000 if span_ms > 0 else 0.0
    if span_days <= 0:
        return None
    periods_per_year = len(rets) / (span_days / 365.0)

    mean = statistics.fmean(rets)
    sd = statistics.pstdev(rets)
    downside = [r for r in rets if r < 0]
    dsd = statistics.pstdev(downside) if len(downside) > 1 else 0.0

    sqrt_ppy = math.sqrt(periods_per_year)
    sharpe = (mean / sd) * sqrt_ppy if sd > 0 else 0.0
    sortino = (mean / dsd) * sqrt_ppy if dsd > 0 else 0.0
    vol_annual = sd * sqrt_ppy

    # Kumulierte Equity-Kurve aus den Renditen -> echter Drawdown
    curve, acc = [1.0], 1.0
    for r in rets:
        acc *= (1.0 + r)
        curve.append(acc)
    peak, max_dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1.0)

    total_return = curve[-1] - 1.0
    years = span_days / 365.0
    if years > 0 and curve[-1] > 0:
        cagr = curve[-1] ** (1.0 / years) - 1.0
    else:
        cagr = 0.0

    tvl_now = equity[-1]
    cutoff = ts[-1] - 30 * 86_400_000
    past = [e for t, e in zip(ts, equity) if t <= cutoff]
    ref = past[-1] if past else equity[0]
    tvl_change = (tvl_now / ref - 1.0) if ref > 0 else 0.0

    return Metrics(
        n=len(rets),
        period_returns=rets,
        span_days=span_days,
        total_return=total_return,
        cagr=cagr,
        vol_annual=vol_annual,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        worst_period=min(rets),
        win_rate=sum(1 for r in rets if r > 0) / len(rets),
        tvl_now=tvl_now,
        tvl_change_30d=tvl_change,
    )
