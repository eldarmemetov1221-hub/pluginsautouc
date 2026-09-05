"""Repository: all DB access, with idempotent writes (sections 5, 10, 20).

Business logic never touches SQL directly - it goes through this layer, which
guarantees:

* an order is created at most once per ``funpay_order_id`` (INSERT OR IGNORE);
* a code is stored at most once per ``(order_id, code_hash)``;
* FunPay events are processed at most once (``processed_events``);
* order status changes obey the FSM (:func:`models.can_transition`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from ..errors import CriticalError
from ..utils.logger import get_logger
from .db import Database
from .models import (
    CodeRecord,
    CodeStatus,
    OrderRecord,
    OrderStatus,
    can_transition,
)

log = get_logger("repo")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ #
    # Idempotency: FunPay events
    # ------------------------------------------------------------------ #
    def mark_event_processed(self, event_key: str) -> bool:
        """Return True if this is the first time we see ``event_key``.

        Uses INSERT OR IGNORE + rowcount so it is atomic and safe against
        duplicate event delivery (section 20).
        """
        with self.db.lock:
            cur = self.db.conn.execute(
                "INSERT OR IGNORE INTO processed_events(event_key, created_at) VALUES (?, ?)",
                (event_key, _now()),
            )
            self.db.conn.commit()
            return cur.rowcount == 1

    def is_event_processed(self, event_key: str) -> bool:
        row = self.db.query_one(
            "SELECT 1 FROM processed_events WHERE event_key = ?", (event_key,)
        )
        return row is not None

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    def create_order(self, order: OrderRecord) -> OrderRecord:
        """Idempotently create an order. Returns the stored record either way."""
        now = _now()
        with self.db.lock:
            self.db.conn.execute(
                """INSERT OR IGNORE INTO orders
                   (funpay_order_id, lot_id, buyer_id, buyer_username, quantity,
                    status, chat_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    order.funpay_order_id,
                    order.lot_id,
                    order.buyer_id,
                    order.buyer_username,
                    order.quantity,
                    order.status or OrderStatus.NEW.value,
                    order.chat_id,
                    now,
                    now,
                ),
            )
            self.db.conn.commit()
        stored = self.get_order_by_funpay_id(order.funpay_order_id)
        if stored is None:  # pragma: no cover - would indicate a DB failure
            raise CriticalError("Failed to persist order")
        return stored

    def get_order_by_funpay_id(self, funpay_order_id: str) -> Optional[OrderRecord]:
        row = self.db.query_one(
            "SELECT * FROM orders WHERE funpay_order_id = ?", (funpay_order_id,)
        )
        return self._row_to_order(row) if row else None

    def get_order(self, order_id: int) -> Optional[OrderRecord]:
        row = self.db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
        return self._row_to_order(row) if row else None

    def get_active_orders_for_buyer(self, buyer_id: str) -> List[OrderRecord]:
        """Orders of a buyer that can still accept a UID from an incoming message.

        NOTE: ERROR is intentionally excluded - once an order is escalated to the
        seller (repeated UID error / out of stock / critical), the bot stops
        auto-processing new UIDs for it until an admin acts.
        """
        rows = self.db.query_all(
            """SELECT * FROM orders
               WHERE buyer_id = ? AND status IN (?,?,?,?,?)
               ORDER BY created_at ASC""",
            (
                buyer_id,
                OrderStatus.WAITING_FOR_CODE.value,
                OrderStatus.INVALID.value,
                OrderStatus.ALREADY_USED.value,
                OrderStatus.ACCOUNT_NOT_FOUND.value,
                OrderStatus.TEMPORARY_ERROR.value,
            ),
        )
        return [self._row_to_order(r) for r in rows]

    def get_unfinished_orders(self) -> List[OrderRecord]:
        """For restart recovery (section 19): orders not in a final state."""
        rows = self.db.query_all(
            "SELECT * FROM orders WHERE status NOT IN (?, ?)",
            (OrderStatus.VALID.value, OrderStatus.CANCELLED.value),
        )
        return [self._row_to_order(r) for r in rows]

    def set_order_status(
        self, order_id: int, new_status: OrderStatus, *, force: bool = False
    ) -> bool:
        """Transition an order's status, honouring the FSM unless ``force``.

        Returns True on success, False if the transition is illegal.
        """
        order = self.get_order(order_id)
        if order is None:
            raise CriticalError(f"Order {order_id} not found")
        current = OrderStatus(order.status)
        if not force and not can_transition(current, new_status):
            log.warning(
                "Rejected illegal order transition %s -> %s (order id=%s)",
                current.value,
                new_status.value,
                order_id,
            )
            return False
        self.db.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (new_status.value, _now(), order_id),
        )
        return True

    # ------------------------------------------------------------------ #
    # Codes
    # ------------------------------------------------------------------ #
    def find_code(self, order_id: int, code_hash: str) -> Optional[CodeRecord]:
        row = self.db.query_one(
            "SELECT * FROM codes WHERE order_id = ? AND code_hash = ?",
            (order_id, code_hash),
        )
        return self._row_to_code(row) if row else None

    def find_code_by_hash_any_order(self, code_hash: str) -> Optional[CodeRecord]:
        """Global lookup - has this exact code been seen on ANY order?"""
        row = self.db.query_one(
            "SELECT * FROM codes WHERE code_hash = ? ORDER BY id DESC LIMIT 1",
            (code_hash,),
        )
        return self._row_to_code(row) if row else None

    def create_code(self, code: CodeRecord) -> tuple[CodeRecord, bool]:
        """Idempotently insert a code for an order.

        Returns ``(record, created)`` where ``created`` is False if the same
        code already existed for this order (duplicate - section 10).
        """
        now = _now()
        with self.db.lock:
            cur = self.db.conn.execute(
                """INSERT OR IGNORE INTO codes
                   (code, code_hash, order_id, funpay_order_id, buyer_id, product,
                    status, spark_status, error_message, attempts, source,
                    message_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    code.code,
                    code.code_hash,
                    code.order_id,
                    code.funpay_order_id,
                    code.buyer_id,
                    code.product,
                    code.status or CodeStatus.RECEIVED.value,
                    code.spark_status,
                    code.error_message,
                    code.attempts,
                    code.source,
                    code.message_id,
                    now,
                    now,
                ),
            )
            self.db.conn.commit()
            created = cur.rowcount == 1
        stored = self.find_code(code.order_id, code.code_hash)
        if stored is None:  # pragma: no cover
            raise CriticalError("Failed to persist code")
        return stored, created

    def get_code(self, code_id: int) -> Optional[CodeRecord]:
        row = self.db.query_one("SELECT * FROM codes WHERE id = ?", (code_id,))
        return self._row_to_code(row) if row else None

    def update_code(
        self,
        code_id: int,
        *,
        status: Optional[CodeStatus] = None,
        spark_status: Optional[str] = None,
        error_message: Optional[str] = None,
        attempts: Optional[int] = None,
        checked: bool = False,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list = [_now()]
        if status is not None:
            sets.append("status = ?")
            params.append(status.value)
        if spark_status is not None:
            sets.append("spark_status = ?")
            params.append(spark_status)
        if error_message is not None:
            sets.append("error_message = ?")
            params.append(error_message)
        if attempts is not None:
            sets.append("attempts = ?")
            params.append(attempts)
        if checked:
            sets.append("checked_at = ?")
            params.append(_now())
        params.append(code_id)
        self.db.execute(f"UPDATE codes SET {', '.join(sets)} WHERE id = ?", tuple(params))

    def increment_code_attempts(self, code_id: int) -> int:
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE codes SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (_now(), code_id),
            )
            self.db.conn.commit()
        code = self.get_code(code_id)
        return code.attempts if code else 0

    def get_retriable_codes(self) -> List[CodeRecord]:
        """Codes to resume on restart: ONLY genuinely interrupted checks
        (status CHECKING). Codes that already exhausted their retries are marked
        FAILED and are intentionally NOT resumed - they never auto-redeem again
        (only an admin /uc_recheck can).
        """
        rows = self.db.query_all(
            "SELECT * FROM codes WHERE status = ?",
            (CodeStatus.CHECKING.value,),
        )
        return [self._row_to_code(r) for r in rows]

    def get_codes_for_order(self, order_id: int) -> List[CodeRecord]:
        rows = self.db.query_all(
            "SELECT * FROM codes WHERE order_id = ? ORDER BY id ASC", (order_id,)
        )
        return [self._row_to_code(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Logs
    # ------------------------------------------------------------------ #
    def add_log(
        self,
        event: str,
        message: str = "",
        *,
        level: str = "INFO",
        order_id: Optional[int] = None,
        code_id: Optional[int] = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO logs(order_id, code_id, level, event, message, created_at)
               VALUES (?,?,?,?,?,?)""",
            (order_id, code_id, level, event, message, _now()),
        )

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #
    def order_status_counts(self, today_only: bool = False) -> dict:
        sql = "SELECT status, COUNT(*) c FROM orders"
        params: tuple = ()
        if today_only:
            sql += " WHERE created_at LIKE ?"
            params = (f"{_now()[:10]}%",)
        sql += " GROUP BY status"
        return {r["status"]: r["c"] for r in self.db.query_all(sql, params)}

    def code_status_counts(self, today_only: bool = False) -> dict:
        sql = "SELECT status, COUNT(*) c FROM codes"
        params: tuple = ()
        if today_only:
            sql += " WHERE created_at LIKE ?"
            params = (f"{_now()[:10]}%",)
        sql += " GROUP BY status"
        return {r["status"]: r["c"] for r in self.db.query_all(sql, params)}

    def get_logs_for_order(self, order_id: int) -> List[dict]:
        rows = self.db.query_all(
            "SELECT * FROM logs WHERE order_id = ? ORDER BY id ASC", (order_id,)
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Row mappers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_order(row) -> OrderRecord:
        return OrderRecord(**{k: row[k] for k in row.keys()})

    @staticmethod
    def _row_to_code(row) -> CodeRecord:
        return CodeRecord(**{k: row[k] for k in row.keys()})
