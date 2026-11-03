"""SQLite-Persistenz: taegliche Snapshots von Score + TVL pro Vault."""
from __future__ import annotations

import gzip
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "vaults.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    vault_address TEXT    NOT NULL,
    name          TEXT,
    leader        TEXT,
    score         REAL,
    apr           REAL,
    tvl           REAL,
    sharpe        REAL,
    sortino       REAL,
    max_drawdown  REAL,
    age_days      REAL,
    leader_frac   REAL,
    passed        INTEGER,
    reasons       TEXT,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_vault ON snapshots(vault_address, ts);
CREATE INDEX IF NOT EXISTS idx_snap_ts    ON snapshots(ts);

CREATE TABLE IF NOT EXISTS runs (
    ts            INTEGER PRIMARY KEY,
    total_vaults  INTEGER,
    candidates    INTEGER,
    passed        INTEGER,
    note          TEXT
);

-- Komplette Vault-Liste pro Tag, inkl. geschlossener Vaults. Spaeter die
-- Grundlage fuer ein Backtest-Universum ohne Survivorship-Bias.
CREATE TABLE IF NOT EXISTS vault_list_snapshots (
    day           TEXT PRIMARY KEY,  -- YYYY-MM-DD (UTC)
    ts            INTEGER NOT NULL,
    vaults        BLOB    NOT NULL   -- gzip-JSON, rund 700 KB pro Tag
);

-- Papertrading: virtuelles Portfolio, kein echtes Geld ------------------
CREATE TABLE IF NOT EXISTS paper_portfolio (
    id            INTEGER PRIMARY KEY CHECK (id = 1),  -- nur ein Portfolio
    created_ts    INTEGER NOT NULL,
    start_capital REAL    NOT NULL,
    cash          REAL    NOT NULL   -- noch nicht investiertes virtuelles USD
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_address TEXT    NOT NULL,
    name          TEXT,
    usd_in        REAL    NOT NULL,  -- eingezahltes virtuelles USD (Kostenbasis)
    entry_ts      INTEGER NOT NULL,  -- fuer Lockup + Return-Referenz
    hwm_ratio     REAL    NOT NULL DEFAULT 1.0,  -- High-Water-Mark fuer Profit-Share
    open          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_pp_vault ON paper_positions(vault_address, open);

CREATE TABLE IF NOT EXISTS paper_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    action        TEXT    NOT NULL,  -- init/deposit/withdraw/exit/fee/mark
    vault_address TEXT,
    name          TEXT,
    usd           REAL,
    note          TEXT
);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_run(conn: sqlite3.Connection, results: list, total_vaults: int,
             note: str = "") -> int:
    ts = int(time.time() * 1000)
    conn.execute(
        "INSERT OR REPLACE INTO runs (ts,total_vaults,candidates,passed,note) "
        "VALUES (?,?,?,?,?)",
        (ts, total_vaults, len(results),
         sum(1 for r in results if r.passed), note),
    )
    conn.executemany(
        "INSERT INTO snapshots (ts,vault_address,name,leader,score,apr,tvl,"
        "sharpe,sortino,max_drawdown,age_days,leader_frac,passed,reasons,raw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                ts, r.address, r.name, r.leader, r.score, r.apr, r.tvl,
                r.metrics.sharpe if r.metrics else None,
                r.metrics.sortino if r.metrics else None,
                r.metrics.max_drawdown if r.metrics else None,
                r.age_days, r.leader_fraction, int(r.passed),
                "; ".join(r.reasons),
                # Commission mit sichern: die API liefert keinen Verlauf davon
                json.dumps({**(r.metrics.as_row() if r.metrics else {}),
                            "leader_commission": r.leader_commission}),
            )
            for r in results
        ],
    )
    conn.commit()
    return ts


def save_vault_list(conn: sqlite3.Connection, raw_vaults: list[dict]) -> str:
    """Speichert die Vault-Liste von heute. Ein Lauf am selben Tag ersetzt sie."""
    ts = int(time.time() * 1000)
    day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
    rows = []
    for v in raw_vaults:
        s = v.get("summary") or {}
        rows.append({
            "address": s.get("vaultAddress"), "name": s.get("name"),
            "leader": s.get("leader"), "tvl": s.get("tvl"),
            "closed": bool(s.get("isClosed")), "apr": v.get("apr"),
            "created": s.get("createTimeMillis"),
            "child": (s.get("relationship") or {}).get("type") == "child",
        })
    conn.execute(
        "INSERT OR REPLACE INTO vault_list_snapshots (day,ts,vaults) VALUES (?,?,?)",
        (day, ts, gzip.compress(json.dumps(rows).encode())))
    conn.commit()
    return day


def load_vault_list(conn: sqlite3.Connection, day: str) -> list[dict] | None:
    row = conn.execute("SELECT vaults FROM vault_list_snapshots WHERE day=?",
                       (day,)).fetchone()
    return json.loads(gzip.decompress(row["vaults"])) if row else None


def history(conn: sqlite3.Connection, address: str, limit: int = 60) -> list[dict]:
    cur = conn.execute(
        "SELECT ts,score,tvl,apr,sharpe,max_drawdown FROM snapshots "
        "WHERE vault_address=? ORDER BY ts DESC LIMIT ?",
        (address, limit),
    )
    return [dict(r) for r in cur.fetchall()]


def last_run_ts(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(ts) AS t FROM runs").fetchone()
    return row["t"] if row and row["t"] else None


def top_at(conn: sqlite3.Connection, ts: int, n: int = 10) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM snapshots WHERE ts=? AND passed=1 "
        "ORDER BY score DESC LIMIT ?", (ts, n),
    )
    return [dict(r) for r in cur.fetchall()]
