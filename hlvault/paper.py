"""Papertrading: virtuelles Portfolio gegen echte Vault-Renditen.

Kein Key, keine Transaktionen, kein Risiko. Es simuliert genau das, was die
Strategie in echt tun wuerde, damit man Wochen laufen lassen und die
tatsaechliche Performance sehen kann, bevor Kapital fliesst.

Modell:
  - init legt ein virtuelles Portfolio mit Startkapital an (alles Cash).
  - rebalance laeuft wie der echte Prozess: screenen, Zielallokation rechnen,
    dann virtuelle Deposits/Withdraws ausfuehren. Es respektiert die HARTEN
    Hyperliquid-Regeln:
        * min. 20 USDC pro Deposit
        * 1 Tag Lockup nach jedem Deposit (kein Rebalance-Exit davor)
        * Leader-Profit-Share auf Gewinn ueber der High-Water-Mark
  - status bewertet mark-to-market: fuer jede offene Position wird die REALE
    Rendite des Vaults seit Einstieg aus den Live-Detaildaten geholt
    (dieselbe Renditedefinition wie Backtest/metrics) und auf die Kostenbasis
    angewandt, abzueglich Profit-Share.

Bewusst ehrlich:
  - Die Vault-Rendite kommt aus accountValueHistory/pnlHistory und ist gebinnt,
    nicht sekundengenau. Kurzfristige Werte sind daher grob.
  - Slippage/Gas gibt es bei Vault-Transfers real kaum; Profit-Share ist die
    dominante Kostenstelle und wird modelliert.
"""
from __future__ import annotations

import sqlite3
import time

from . import allocator, api, backtest as bt, db, screener
from .config import CONFIG, Config

MS_DAY = 86_400_000


# --------------------------------------------------------------------------
# Portfolio-Grundzustand
# --------------------------------------------------------------------------
def get_portfolio(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM paper_portfolio WHERE id=1").fetchone()
    return dict(row) if row else None


def init_portfolio(conn: sqlite3.Connection, capital: float) -> None:
    ts = int(time.time() * 1000)
    conn.execute("DELETE FROM paper_portfolio")
    conn.execute("DELETE FROM paper_positions")
    conn.execute("DELETE FROM paper_ledger")
    conn.execute(
        "INSERT INTO paper_portfolio (id,created_ts,start_capital,cash) "
        "VALUES (1,?,?,?)", (ts, capital, capital))
    conn.execute(
        "INSERT INTO paper_ledger (ts,action,usd,note) VALUES (?,?,?,?)",
        (ts, "init", capital, "Portfolio angelegt"))
    conn.commit()


def open_positions(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM paper_positions WHERE open=1")
    return [dict(r) for r in cur.fetchall()]


def _ledger(conn, ts, action, usd, vault=None, name=None, note=""):
    conn.execute(
        "INSERT INTO paper_ledger (ts,action,vault_address,name,usd,note) "
        "VALUES (?,?,?,?,?,?)", (ts, action, vault, name, round(usd, 2), note))


# --------------------------------------------------------------------------
# Bewertung (mark-to-market) gegen echte Vault-Renditen
# --------------------------------------------------------------------------
def value_position(pos: dict, detail: dict, now_ms: int,
                   cfg: Config = CONFIG) -> dict:
    """Aktueller Wert einer Position inkl. Profit-Share-Abzug.

    Return seit Einstieg = forward_return(detail, entry_ts, now).
    gross = usd_in * (1+r). Profit-Share nur auf Gewinn ueber HWM.
    """
    r = bt.forward_return(detail, pos["entry_ts"], now_ms)
    if r is None:
        # keine neuen Datenpunkte -> Wert unveraendert
        gross = pos["usd_in"]
        ratio = 1.0
    else:
        ratio = 1.0 + r
        gross = pos["usd_in"] * ratio

    commission = float(detail.get("leaderCommission") or 0.0)
    # Profit-Share nur auf den Teil ueber der bisherigen HWM
    hwm_value = pos["usd_in"] * pos["hwm_ratio"]
    profit_over_hwm = max(0.0, gross - hwm_value)
    fee = profit_over_hwm * commission
    net = gross - fee
    return {
        "gross": gross,
        "net": net,
        "fee": fee,
        "ratio": ratio,
        "return_pct": (net / pos["usd_in"] - 1.0) if pos["usd_in"] > 0 else 0.0,
        "new_hwm_ratio": max(pos["hwm_ratio"], gross / pos["usd_in"]
                             if pos["usd_in"] > 0 else pos["hwm_ratio"]),
    }


def mark_to_market(conn: sqlite3.Connection, details: dict,
                   cfg: Config = CONFIG) -> dict:
    """Gesamtwert = Cash + Summe aller Positionswerte (netto)."""
    pf = get_portfolio(conn)
    if not pf:
        return {}
    now = int(time.time() * 1000)
    rows = []
    invested_value = 0.0
    for pos in open_positions(conn):
        det = details.get(pos["vault_address"])
        if not det:
            det = api.fetch_vault_details(pos["vault_address"])
        v = value_position(pos, det, now, cfg)
        invested_value += v["net"]
        age_days = (now - pos["entry_ts"]) / MS_DAY
        rows.append({
            "name": pos["name"], "address": pos["vault_address"],
            "usd_in": pos["usd_in"], "value": v["net"], "fee": v["fee"],
            "return_pct": v["return_pct"], "age_days": age_days,
            "locked": age_days < cfg.portfolio.lockup_days,
        })
    total = pf["cash"] + invested_value
    return {
        "start_capital": pf["start_capital"],
        "cash": pf["cash"],
        "invested_value": invested_value,
        "total": total,
        "total_return_pct": total / pf["start_capital"] - 1.0,
        "created_ts": pf["created_ts"],
        "positions": rows,
    }


# --------------------------------------------------------------------------
# Rebalance: virtuelle Deposits/Withdraws nach Zielallokation
# --------------------------------------------------------------------------
def rebalance(conn: sqlite3.Connection, results, details: dict,
              cfg: Config = CONFIG) -> list[dict]:
    """Fuehrt die Zielallokation virtuell aus. Gibt die Aktionsliste zurueck."""
    pf = get_portfolio(conn)
    if not pf:
        raise RuntimeError("Kein Portfolio - erst 'paper init <kapital>'.")
    now = int(time.time() * 1000)
    p = cfg.portfolio

    # aktueller Gesamtwert = Basis fuer die Zielgewichte
    mtm = mark_to_market(conn, details, cfg)
    total = mtm["total"]

    plan = allocator.propose(results, total, cfg)
    target = {t["address"]: t for t in plan}

    positions = {pos["vault_address"]: pos for pos in open_positions(conn)}
    actions = []
    cash = pf["cash"]

    # 1. Positionen abbauen/schliessen, die NICHT mehr im Ziel sind
    for addr, pos in positions.items():
        age_days = (now - pos["entry_ts"]) / MS_DAY
        if addr in target:
            continue
        if age_days < p.lockup_days:
            actions.append({"action": "hold-locked", "name": pos["name"],
                            "usd": 0.0, "note": f"Lockup {p.lockup_days}d aktiv"})
            continue
        det = details.get(addr) or api.fetch_vault_details(addr)
        v = value_position(pos, det, now, cfg)
        cash += v["net"]
        conn.execute("UPDATE paper_positions SET open=0 WHERE id=?", (pos["id"],))
        _ledger(conn, now, "exit", v["net"], addr, pos["name"],
                f"Rendite {v['return_pct']:+.1%}")
        actions.append({"action": "exit", "name": pos["name"],
                        "usd": v["net"], "note": f"{v['return_pct']:+.1%}"})

    # 2. Ziel-Positionen auf-/abbauen
    for addr, t in target.items():
        want = t["usd"]
        pos = positions.get(addr)
        if pos and pos["open"]:
            # aktuellen Wert bestimmen
            det = details.get(addr) or api.fetch_vault_details(addr)
            v = value_position(pos, det, now, cfg)
            have = v["net"]
            delta = want - have
            # Hysterese: kleine Abweichungen ignorieren
            if abs(delta) < max(p.min_deposit_usd, want * 0.10):
                actions.append({"action": "keep", "name": t["name"],
                                "usd": have, "note": "im Zielband"})
                continue
            age_days = (now - pos["entry_ts"]) / MS_DAY
            if delta > 0:
                if delta < p.min_deposit_usd:
                    continue
                if delta > cash:
                    delta = cash
                if delta < p.min_deposit_usd:
                    continue
                # Nachkauf: gewichteter HWM-Reset (neue Kostenbasis anteilig)
                new_in = pos["usd_in"] + delta
                conn.execute(
                    "UPDATE paper_positions SET usd_in=?, entry_ts=?, hwm_ratio=1.0 "
                    "WHERE id=?", (new_in, now, pos["id"]))
                cash -= delta
                _ledger(conn, now, "deposit", delta, addr, t["name"], "Nachkauf")
                actions.append({"action": "deposit", "name": t["name"],
                                "usd": delta, "note": "Nachkauf"})
            else:  # abbauen
                if age_days < p.lockup_days:
                    actions.append({"action": "hold-locked", "name": t["name"],
                                    "usd": 0.0, "note": "Lockup aktiv"})
                    continue
                cut = min(-delta, have)
                frac = cut / have if have > 0 else 0.0
                new_in = pos["usd_in"] * (1 - frac)
                cash += cut
                conn.execute("UPDATE paper_positions SET usd_in=? WHERE id=?",
                             (new_in, pos["id"]))
                _ledger(conn, now, "withdraw", cut, addr, t["name"], "Teilabbau")
                actions.append({"action": "withdraw", "name": t["name"],
                                "usd": cut, "note": "Teilabbau"})
        else:
            # neue Position eroeffnen
            amount = min(want, cash)
            if amount < p.min_deposit_usd:
                actions.append({"action": "skip", "name": t["name"], "usd": amount,
                                "note": f"< {p.min_deposit_usd:.0f} USDC Minimum"})
                continue
            conn.execute(
                "INSERT INTO paper_positions (vault_address,name,usd_in,entry_ts,"
                "hwm_ratio,open) VALUES (?,?,?,?,1.0,1)",
                (addr, t["name"], amount, now))
            cash -= amount
            _ledger(conn, now, "deposit", amount, addr, t["name"], "Neu eroeffnet")
            actions.append({"action": "open", "name": t["name"],
                            "usd": amount, "note": "neu"})

    conn.execute("UPDATE paper_portfolio SET cash=? WHERE id=1", (round(cash, 2),))
    conn.commit()
    return actions


def ledger(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM paper_ledger ORDER BY ts DESC LIMIT ?", (limit,))
    return [dict(r) for r in cur.fetchall()]


__all__ = ["init_portfolio", "get_portfolio", "rebalance", "mark_to_market",
           "open_positions", "ledger"]
