"""SQLite 持久层：批次、档表、档三级结构，全部存整数，按档不逐粒。

并发上传按 **批号** 用文件锁串行化（跨 gunicorn 进程、跨线程）：
同一批号的「查已有档表 → 相容性校验 → 写入」必须作为一个整体，否则两个请求
可能同时看到旧快照而各自通过校验；不同批号使用不同的锁文件，互不阻塞。
锁只在单次上传期间持有，进程退出时由内核自动释放。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from .core import Bin, BinTable, compatible, parse_table

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
        # WAL：读写互不阻塞，多个工作进程并发上传不同批次时更稳。
        # journal_mode 是持久化在库文件上的设置，重复执行无副作用。
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass  # 例如只读文件系统；退回默认 journal 仍可工作
        conn.executescript(SCHEMA)


@contextmanager
def connect(path: str):
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 按批号串行化：文件锁（fcntl.flock），跨 gunicorn 进程与线程均有效
# ---------------------------------------------------------------------------

def _locks_dir(db_path: str) -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(db_path)), "locks")
    os.makedirs(d, exist_ok=True)
    return d


@contextmanager
def batch_lock(db_path: str, batch_name: str):
    """持有批号 batch_name 的排它锁；不同批号互不阻塞。

    同一批号的「读现有档表 → 相容性校验 → 写入」必须在锁内完成，否则并发
    补报会各自基于旧快照通过校验，把彼此冲突的档表都写进同一批。
    """
    import fcntl

    digest = hashlib.sha256(batch_name.encode("utf-8")).hexdigest()
    path = os.path.join(_locks_dir(db_path), digest + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(frozen=True)
class UploadOutcome:
    """一次上传的最终结果。existing_count 为本次写入前的同批档表数。"""

    batch_id: int
    ok: bool
    created_batch: bool
    existing_count: int
    table_id: int | None = None
    reason: str = ""


def add_table_to_batch(
    db_path: str, name: str, table: BinTable, note: str
) -> UploadOutcome:
    """在批号锁内完成「找/建批次 → 与当时库内全部同批档表校验 → 写入」。

    - 新批号只建一个批次（重复上传竞争由批号锁消除，SQL 再用 ON CONFLICT 兜底）；
    - 每张表入库前都与当时库内该批的全部档表做整体相容性校验；
    - 不相容则整张表不入库，已入库档表不受影响、仍可用于核算。
    """
    with batch_lock(db_path, name), connect(db_path) as conn:
        row = get_batch_by_name(conn, name)
        created = False
        if row is None:
            cur = conn.execute(
                "INSERT INTO batches(name) VALUES (?) ON CONFLICT(name) DO NOTHING",
                (name,),
            )
            created = cur.rowcount > 0
            row = get_batch_by_name(conn, name)
        if row is None:  # 理论上不可达：并发建批且被别的写者删掉
            raise RuntimeError(f"无法创建批次 {name!r}")
        batch_id = int(row["id"])

        existing = load_batch_tables(conn, batch_id)
        if existing:
            ok, reason = compatible(existing + [table])
            if not ok:
                return UploadOutcome(
                    batch_id=batch_id,
                    ok=False,
                    created_batch=False,
                    existing_count=len(existing),
                    reason=reason,
                )
        table_id = insert_table(conn, batch_id, table, note)
        return UploadOutcome(
            batch_id=batch_id,
            ok=True,
            created_batch=created,
            existing_count=len(existing),
            table_id=table_id,
        )



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
