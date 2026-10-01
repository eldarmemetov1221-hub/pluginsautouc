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
    price             REAL DEFAULT 0,
    cost              REAL DEFAULT 0,
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

-- LioGames bulk voucher purchasing (standalone /uc_buy drip buyer). These
-- tables are independent of the FunPay order/delivery flow above.
CREATE TABLE IF NOT EXISTS buy_batches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    denom             TEXT NOT NULL,
    variation_id      TEXT,
    quantity          INTEGER NOT NULL,
    status            TEXT NOT NULL,       -- PENDING_CONFIRM/RUNNING/PAUSED/DONE/STOPPED
    unit_price        REAL DEFAULT 0,
    admin_id          TEXT,                -- Telegram id to send progress + file to
    note              TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buy_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES buy_batches(id),
    seq               INTEGER NOT NULL,
    client_ref        TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL,       -- QUEUED/ORDERED/DELIVERED/FAILED
    denom             TEXT,                -- per-item denomination (multi-denom batches)
    variation_id      TEXT,                -- per-item LioGames variation id
    liog_order_id     TEXT,
    code              TEXT,
    error_message     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(batch_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_codes_hash ON codes(code_hash);
CREATE INDEX IF NOT EXISTS idx_codes_order ON codes(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_buy_items_batch ON buy_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_buy_batches_status ON buy_batches(status);
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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (idempotent)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(orders)")}
        if "price" not in cols:
            # Older DBs have no price; add it (existing rows -> 0, i.e. counted
            # as "no price" in finance stats until re-captured on new orders).
            self._conn.execute("ALTER TABLE orders ADD COLUMN price REAL DEFAULT 0")
            log.info("Migrated: added orders.price column")
        if "cost" not in cols:
            # Frozen cost-of-goods snapshot taken when the order arrives, so that
            # later changes to pack costs never retroactively recompute old
            # orders. Legacy rows -> 0, and finance falls back to the current
            # calc for those until they age out.
            self._conn.execute("ALTER TABLE orders ADD COLUMN cost REAL DEFAULT 0")
            log.info("Migrated: added orders.cost column")
        # buy_items gained per-item denom/variation_id for multi-denomination
        # batches; add them to DBs that created buy_items before this change.
        try:
            bcols = {r["name"] for r in self._conn.execute("PRAGMA table_info(buy_items)")}
        except Exception:
            bcols = set()
        if bcols and "denom" not in bcols:
            self._conn.execute("ALTER TABLE buy_items ADD COLUMN denom TEXT")
            log.info("Migrated: added buy_items.denom column")
        if bcols and "variation_id" not in bcols:
            self._conn.execute("ALTER TABLE buy_items ADD COLUMN variation_id TEXT")
            log.info("Migrated: added buy_items.variation_id column")

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
