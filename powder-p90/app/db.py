"""SQLite 持久层：批次、档表、档三级结构，全部存整数，按档不逐粒。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from .core import Bin, BinTable, parse_table

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tables (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    total       INTEGER NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS bins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id    INTEGER NOT NULL REFERENCES tables(id) ON DELETE CASCADE,
    lo          INTEGER NOT NULL,
    hi          INTEGER NOT NULL,
    count       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tables_batch ON tables(batch_id);
CREATE INDEX IF NOT EXISTS idx_bins_table ON bins(table_id);
"""


def init_db(path: str) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(path: str):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT b.id, b.name, b.created_at,
                  (SELECT COUNT(*) FROM tables t WHERE t.batch_id = b.id) AS table_count,
                  (SELECT total FROM tables t WHERE t.batch_id = b.id
                     ORDER BY t.id LIMIT 1) AS total
             FROM batches b ORDER BY b.id"""
    ).fetchall()


def get_batch_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE name = ?", (name,)).fetchone()


def create_batch(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute("INSERT INTO batches(name) VALUES (?)", (name,))
    return int(cur.lastrowid)


def list_tables(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tables WHERE batch_id = ? ORDER BY id", (batch_id,)
    ).fetchall()


def load_table(conn: sqlite3.Connection, table_id: int) -> BinTable:
    rows = conn.execute(
        "SELECT lo, hi, count FROM bins WHERE table_id = ? ORDER BY id", (table_id,)
    ).fetchall()
    bins = tuple(Bin(int(r["lo"]), int(r["hi"]), int(r["count"])) for r in rows)
    total = sum(b.count for b in bins)
    cuts = [bins[0].lo]
    cum = [0]
    for b in bins:
        cuts.append(b.hi + 1)
        cum.append(cum[-1] + b.count)
    return BinTable(bins, total, tuple(cuts), tuple(cum))


def load_batch_tables(conn: sqlite3.Connection, batch_id: int) -> list[BinTable]:
    trows = list_tables(conn, batch_id)
    return [load_table(conn, int(r["id"])) for r in trows]


def insert_table(conn: sqlite3.Connection, batch_id: int, table: BinTable, note: str) -> int:
    cur = conn.execute(
        "INSERT INTO tables(batch_id, total, note) VALUES (?, ?, ?)",
        (batch_id, table.total, note),
    )
    table_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO bins(table_id, lo, hi, count) VALUES (?, ?, ?, ?)",
        [(table_id, b.lo, b.hi, b.count) for b in table.bins],
    )
    return table_id


def parse_and_preview(text: str) -> BinTable:
    return parse_table(text)
