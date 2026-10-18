"""Allokations-Vorschlag aus den Screener-Ergebnissen.

Phase 1: nur Vorschlag / Diff gegen die aktuelle Position. Keine Transfers.
"""
from __future__ import annotations

from .config import CONFIG, Config


def propose(results, capital_usd: float, cfg: Config = CONFIG) -> list[dict]:
    """Allokation ueber die Qualifizierten, mit Cap pro Vault.

    Default (portfolio.equal_weight=True): GLEICHGEWICHT. Der Backtest zeigt,
    dass Score-Ranking innerhalb der Qualifizierten nicht trennt und Gleich-
    gewicht ueber mehr Vaults den Tail-Risk drueckt. Score dient nur als
    Reihenfolge fuer die Kappung auf max_positions.
    """
    p = cfg.portfolio
    picks = [r for r in results if r.passed][: p.max_positions]
    if not picks:
        return []

    if p.equal_weight:
        w = 1.0 / len(picks)
        weights = {r.address: w for r in picks}
    else:
        raw = {r.address: max(r.score, 1.0) for r in picks}
        total = sum(raw.values())
        weights = {a: v / total for a, v in raw.items()}

    # Cap durchsetzen und Ueberhang auf die anderen umverteilen
    for _ in range(10):
        over = {a: w for a, w in weights.items() if w > p.max_weight_per_vault}
        if not over:
            break
        excess = sum(w - p.max_weight_per_vault for w in over.values())
        for a in over:
            weights[a] = p.max_weight_per_vault
        rest = [a for a in weights if a not in over]
        if not rest:
            break
        pool = sum(weights[a] for a in rest) or 1.0
        for a in rest:
            weights[a] += excess * (weights[a] / pool)

    out = []
    for r in picks:
        usd = capital_usd * weights[r.address]
        out.append({
            "name": r.name,
            "address": r.address,
            "weight": round(weights[r.address], 4),
            "usd": round(usd, 2),
            "score": r.score,
            "below_min": usd < p.min_deposit_usd,
        })
    return out


def diff(current: dict[str, float], target: list[dict],
         cfg: Config = CONFIG) -> list[dict]:
    """Was muesste man tun? Mit Hysterese: kleine Abweichungen ignorieren.

    current: {vault_address: usd}
    """
    p = cfg.portfolio
    tgt = {t["address"]: t for t in target}
    actions = []
    for addr, t in tgt.items():
        have = current.get(addr, 0.0)
        delta = t["usd"] - have
        if abs(delta) < max(p.min_deposit_usd, t["usd"] * 0.10):
            continue
        actions.append({
            "action": "deposit" if delta > 0 else "withdraw",
            "name": t["name"], "address": addr, "usd": round(abs(delta), 2),
        })
    for addr, have in current.items():
        if addr not in tgt and have > 0:
            actions.append({
                "action": "exit", "name": addr[:10], "address": addr,
                "usd": round(have, 2),
            })
    return actions
