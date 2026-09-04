"""SQLite connection + schema management (task spec, section 6).

A dedicated database file - it never touches FunPayCardinal's own storage.
Thread-safe for the plugin's usage pattern (FPC listener thread + the retry
worker thread) via ``check_same_thread=False`` plus a coarse write lock.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Optional

from ..utils.logger import get_logger

log = get_logger("db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    funpay_order_id   TEXT NOT NULL UNIQUE,
    lot_id            TEXT NOT NULL,
    buyer_id          TEXT,
    buyer_username    TEXT,
    quantity          INTEGER DEFAULT 1,
    status            TEXT NOT NULL,
    chat_id           TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT NOT NULL,
    code_hash         TEXT NOT NULL,
    order_id          INTEGER REFERENCES orders(id),
    funpay_order_id   TEXT,
    buyer_id          TEXT,
    product           TEXT,
    status            TEXT NOT NULL,
    spark_status      TEXT,
    error_message     TEXT,
    attempts          INTEGER DEFAULT 0,
    source            TEXT,
    message_id        TEXT,
    created_at        TEXT NOT NULL,
    checked_at        TEXT,
    updated_at        TEXT NOT NULL,
    UNIQUE(order_id, code_hash)
);

CREATE TABLE IF NOT EXISTS logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER,
    code_id           INTEGER,
    level             TEXT,
    event             TEXT,
    message           TEXT,
    created_at        TEXT NOT NULL
);

-- Idempotency guard for FunPay events (section 10 & 20): a processed
-- message id is stored once; re-delivered events are ignored.
CREATE TABLE IF NOT EXISTS processed_events (
    event_key         TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codes_hash ON codes(code_hash);
CREATE INDEX IF NOT EXISTS idx_codes_order ON codes(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()
        log.info("Database ready at %s", path)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple = ()):  # write helper
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchone()

    def query_all(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
