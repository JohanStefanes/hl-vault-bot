"""SQLite-Persistenz: taegliche Snapshots von Score + TVL pro Vault."""
from __future__ import annotations

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
                json.dumps(r.metrics.as_row() if r.metrics else {}),
            )
            for r in results
        ],
    )
    conn.commit()
    return ts


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
