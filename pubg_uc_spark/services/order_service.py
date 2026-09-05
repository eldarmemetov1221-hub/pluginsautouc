"""Order orchestration: the FSM driver + messaging + result handling.

Ties together the repository, code_service, messenger, checker and
retry_service. This is where order state transitions happen and where buyers
are messaged. It is transport-agnostic: the ``messenger`` is any object with
``send(chat_id, text)`` and ``send_once(key, chat_id, text) -> bool``.
"""

from __future__ import annotations

from typing import Optional

from ..database.models import (
    CodeStatus,
    OrderRecord,
    OrderStatus,
)
from ..database.repository import Repository
from ..errors import SparkTemporaryError
from ..services.code_service import CodeService, IntakeAction
from ..spark.models import SparkResult, UnifiedStatus
from ..utils.logger import get_logger

log = get_logger("order")


# Spark UnifiedStatus -> (code status, order status, message attribute)
# UnifiedStatus -> (code status, order status, buyer-message attribute).
# ACCOUNT_NOT_FOUND is handled separately (two-strike escalation), not here.
_RESULT_MAP = {
    UnifiedStatus.VALID: (CodeStatus.VALID, OrderStatus.VALID, "valid"),
    UnifiedStatus.INVALID: (CodeStatus.INVALID, OrderStatus.INVALID, "invalid"),
    UnifiedStatus.ALREADY_USED: (CodeStatus.ALREADY_USED, OrderStatus.ALREADY_USED, "invalid"),
}


class OrderService:
    def __init__(self, config, repo: Repository, messenger, retry_service):
        self.cfg = config
        self.repo = repo
        self.messenger = messenger
        self.retry = retry_service
        self.codes = CodeService(config, repo)

    # ------------------------------------------------------------------ #
    # Message formatting helpers
    # ------------------------------------------------------------------ #
    def _product(self, order: OrderRecord) -> str:
        """Buyer-facing product label, quantity-aware (e.g. '60 UC ×3')."""
        lot = self.cfg.lot(order.lot_id)
        base = lot.product if lot else ""
        qty = order.quantity or 1
        return f"{base} ×{qty}" if qty > 1 else base

    def _fmt(self, order: OrderRecord, template: str, uid: str = "", player_name: str = "",
             delivered=None, total=None) -> str:
        lot = self.cfg.lot(order.lot_id)
        uc = lot.uc if lot else ""
        qty = order.quantity or 1
        try:
            total_uc = int(str(uc)) * qty
        except (TypeError, ValueError):
            total_uc = ""
        return template.format(
            order_id=order.funpay_order_id,
            order=order.funpay_order_id,  # alias for the FunPay order URL
            product=self._product(order),
            uid=uid,
            player_name=player_name,
            quantity=qty,
            uc=uc,
            denomination=uc,  # backward-compat alias
            total_uc=total_uc,
            delivered=(delivered if delivered is not None else qty),
            total=(total if total is not None else qty),
        )

    # ------------------------------------------------------------------ #
    # New order (section 2)
    # ------------------------------------------------------------------ #
    def handle_new_order(self, info: OrderRecord) -> Optional[OrderRecord]:
        """Idempotently register a new order and wait (silently) for the UID.

        Per the sale flow, the bot sends NOTHING to the buyer after payment; it
        just moves the order to WAITING_FOR_CODE and waits for the buyer to send
        their PUBG UID.
        """
        order = self.repo.create_order(info)
        self.repo.add_log(
            "order_seen",
            f"lot={order.lot_id} buyer={order.buyer_username} qty={order.quantity}",
            order_id=order.id,
        )
        log.info(
            "[FunPay] Order #%s lot=%s buyer=%s qty=%s",
            order.funpay_order_id,
            order.lot_id,
            order.buyer_username,
            order.quantity,
        )

        if OrderStatus(order.status) == OrderStatus.NEW:
            self.repo.set_order_status(order.id, OrderStatus.WAITING_FOR_CODE)
            log.info("[Order #%s] Waiting for UID (no message sent)", order.funpay_order_id)
        return self.repo.get_order(order.id)

    # ------------------------------------------------------------------ #
    # Incoming buyer message (section 2 steps 5-7, section 8)
    # ------------------------------------------------------------------ #
    def handle_message(self, buyer_id: str, chat_id: str, text: str, message_id: str) -> None:
        orders = self.repo.get_active_orders_for_buyer(buyer_id)
        if not orders:
            # Never grab a code from a chat with no active order (section 8).
            return

        order = orders[0]  # oldest active order; one code -> one order
        # Keep chat_id fresh (buyer may message from the order chat).
        if chat_id and not order.chat_id:
            self.repo.db.execute(
                "UPDATE orders SET chat_id = ? WHERE id = ?", (chat_id, order.id)
            )
            order.chat_id = chat_id

        result = self.codes.process(order, text, message_id)
        action = result.action

        if action == IntakeAction.NO_CODE:
            return  # ordinary chatter, ignore silently
        if action == IntakeAction.BAD_FORMAT:
            # Send the format hint once per order; stay silent on repeats.
            self.messenger.send_once(
                f"badfmt:{order.id}", chat_id, self._fmt(order, self.cfg.messages.bad_format)
            )
            return
        if action in (
            IntakeAction.DUPLICATE_INFLIGHT,
            IntakeAction.ALREADY_PROCESSED,
            IntakeAction.ALREADY_NEGATIVE,
        ):
            # Anti-spam: one duplicate notice per UID (section 10).
            uid = result.code.code if result.code else ""
            key = f"dup:{result.code.code_hash}" if result.code else f"dup:{order.id}"
            self.messenger.send_once(key, chat_id, self._fmt(order, self.cfg.messages.duplicate, uid))
            log.info("[Order #%s] Duplicate/known UID (%s)", order.funpay_order_id, action.value)
            return

        # action == ENQUEUE
        code = result.code
        self.repo.add_log("uid_received", "", order_id=order.id, code_id=code.id)
        self.repo.set_order_status(order.id, OrderStatus.CODE_RECEIVED)
        self.repo.set_order_status(order.id, OrderStatus.CHECKING)
        self.repo.update_code(code.id, status=CodeStatus.CHECKING)
        log.info("[Order #%s] Enqueue redeem", order.funpay_order_id)
        self.retry.enqueue(code.id)

    # ------------------------------------------------------------------ #
    # Result handler - called by retry_service (sections 4, 11, 12)
    # ------------------------------------------------------------------ #
    def apply_result(
        self,
        code_id: int,
        result: Optional[SparkResult],
        error: Optional[Exception],
        attempts: int,
    ) -> None:
        code = self.repo.get_code(code_id)
        if code is None:
            log.error("apply_result: code_id=%s not found", code_id)
            return
        order = self.repo.get_order(code.order_id) if code.order_id else None
        oid = order.funpay_order_id if order else "?"

        self.repo.update_code(code_id, attempts=attempts)

        uid = code.code

        # --- UID does not exist: two-strike escalation ---
        if result is not None and result.status is UnifiedStatus.ACCOUNT_NOT_FOUND:
            self._handle_account_not_found(code_id, code, order, oid, uid, result)
            return

        # --- Success / definitive buyer-facing outcomes ---
        if result is not None and result.status in _RESULT_MAP:
            code_status, order_status, msg_attr = _RESULT_MAP[result.status]
            reason = "account_not_found" if result.status is UnifiedStatus.ACCOUNT_NOT_FOUND else ""
            self.repo.update_code(
                code_id,
                status=code_status,
                spark_status=result.status.value,
                error_message=reason or result.message,
                checked=True,
            )
            if order:
                self.repo.set_order_status(order.id, order_status)
            log.info("[Spark] Result: %s [Order #%s]", result.status.value, oid)
            self.repo.add_log(
                "spark_result", result.status.value, order_id=code.order_id, code_id=code_id
            )
            if order and order.chat_id:
                text = self._fmt(
                    order, getattr(self.cfg.messages, msg_attr), uid, player_name=result.player_name
                )
                self.messenger.send_once(f"result:{code_id}", order.chat_id, text)
            self.repo.add_log("buyer_notified", msg_attr, order_id=code.order_id, code_id=code_id)
            return

        # --- Multi-pack order partially delivered ---
        if result is not None and result.status is UnifiedStatus.PARTIAL:
            self.repo.update_code(
                code_id,
                status=CodeStatus.FAILED,
                spark_status=result.status.value,
                error_message=f"partial {result.delivered}/{result.total}",
                checked=True,
            )
            if order:
                self.repo.set_order_status(order.id, OrderStatus.ERROR)
            log.warning("[Order #%s] Partial delivery %s/%s", oid, result.delivered, result.total)
            self.repo.add_log(
                "partial", f"{result.delivered}/{result.total}", level="WARNING",
                order_id=code.order_id, code_id=code_id,
            )
            if order and order.chat_id:
                self.messenger.send_once(
                    f"result:{code_id}", order.chat_id,
                    self._fmt(order, self.cfg.messages.partial, uid,
                              delivered=result.delivered, total=result.total),
                )
            self._notify_admin(
                f"🛑 Order #{oid}: partial delivery {result.delivered}/{result.total} (uid={uid})"
            )
            return

        # --- Operational error we understand (e.g. out of stock) ---
        if result is not None and result.status is UnifiedStatus.ERROR:
            self.repo.update_code(
                code_id,
                status=CodeStatus.FAILED,
                spark_status=result.status.value,
                error_message=result.message or "operational error",
                checked=True,
            )
            if order:
                self.repo.set_order_status(order.id, OrderStatus.ERROR)
            log.error("[Order #%s] Operational error: %s", oid, result.message)
            self.repo.add_log(
                "spark_error", result.message, level="ERROR",
                order_id=code.order_id, code_id=code_id,
            )
            if order and order.chat_id:
                self.messenger.send_once(
                    f"result:{code_id}", order.chat_id, self._fmt(order, self.cfg.messages.error, uid)
                )
            self._notify_admin(f"🛑 Order #{oid}: Spark error: {result.message}")
            return

        # --- Temporary error, retries exhausted -> FINAL failure ---
        # After MAX_RETRIES the code is marked FAILED (not TEMPORARY_ERROR), so it
        # is NEVER auto-redeemed again - not on restart, not on a repeat message.
        # Only an explicit admin /uc_recheck can retry it.
        if isinstance(error, SparkTemporaryError):
            self.repo.update_code(
                code_id,
                status=CodeStatus.FAILED,
                spark_status="TEMPORARY_ERROR",
                error_message=f"retries exhausted: {error}",
                attempts=attempts,
                checked=True,
            )
            if order:
                self.repo.set_order_status(order.id, OrderStatus.ERROR)
            log.error("[Order #%s] FAILED after %s attempts (no auto-retry): %s", oid, attempts, error)
            self.repo.add_log(
                "failed_retries_exhausted", str(error), level="ERROR",
                order_id=code.order_id, code_id=code_id,
            )
            if order and order.chat_id:
                self.messenger.send_once(
                    f"result:{code_id}",
                    order.chat_id,
                    self._fmt(order, self.cfg.messages.error, uid),
                )
            self._notify_admin(
                f"🛑 Order #{oid}: FAILED after {attempts} attempts (manual action needed): {error}"
            )
            return

        # --- Critical / unknown -> final failure; tell buyer + admin ---
        self.repo.update_code(
            code_id,
            status=CodeStatus.FAILED,
            spark_status=(result.status.value if result else "UNKNOWN"),
            error_message=str(error) if error else "unknown Spark response",
            checked=True,
        )
        if order:
            self.repo.set_order_status(order.id, OrderStatus.ERROR)
        log.error("[Order #%s] Critical failure: %s", oid, error)
        self.repo.add_log(
            "critical_error", str(error), level="CRITICAL",
            order_id=code.order_id, code_id=code_id,
        )
        if order and order.chat_id:
            self.messenger.send_once(
                f"result:{code_id}", order.chat_id, self._fmt(order, self.cfg.messages.error, uid)
            )
        self._notify_admin(f"🛑 Order #{oid}: critical error: {error}")

    # ------------------------------------------------------------------ #
    def _handle_account_not_found(self, code_id, code, order, oid, uid, result) -> None:
        """UID not found: 1st time ask to retry; 2nd time escalate to seller."""
        # Count prior account-not-found codes on this order (before this one).
        prior = 0
        if order:
            prior = sum(
                1
                for c in self.repo.get_codes_for_order(order.id)
                if c.id != code_id and c.status == CodeStatus.ACCOUNT_NOT_FOUND.value
            )
        self.repo.update_code(
            code_id,
            status=CodeStatus.ACCOUNT_NOT_FOUND,
            spark_status=result.status.value,
            error_message="account_not_found",
            checked=True,
        )
        self.repo.add_log(
            "account_not_found", f"attempt={prior + 1}",
            order_id=code.order_id, code_id=code_id,
        )

        if prior == 0:
            # First strike: buyer may resend a corrected UID.
            if order:
                self.repo.set_order_status(order.id, OrderStatus.ACCOUNT_NOT_FOUND)
            log.info("[Order #%s] UID not found (1st) -> ask to retry", oid)
            if order and order.chat_id:
                self.messenger.send_once(
                    f"result:{code_id}", order.chat_id,
                    self._fmt(order, self.cfg.messages.account_not_found, uid),
                )
        else:
            # Second strike: stop auto-processing, hand over to the seller.
            if order:
                self.repo.set_order_status(order.id, OrderStatus.ERROR)
            log.warning("[Order #%s] UID not found (2nd) -> escalate to seller", oid)
            if order and order.chat_id:
                self.messenger.send_once(
                    f"result:{code_id}", order.chat_id,
                    self._fmt(order, self.cfg.messages.account_not_found_final, uid),
                )
            self._notify_admin(f"🛑 Order #{oid}: repeated UID error (uid={uid}), seller action needed")

    # ------------------------------------------------------------------ #
    # Restart recovery (section 19 & 20)
    # ------------------------------------------------------------------ #
    def resume_unfinished(self) -> int:
        """Re-enqueue codes that were mid-check / retriable when we stopped."""
        resumed = 0
        for code in self.repo.get_retriable_codes():
            log.info(
                "[Recovery] Re-enqueue code_id=%s (order #%s, status=%s)",
                code.id, code.funpay_order_id, code.status,
            )
            if code.order_id:
                self.repo.set_order_status(code.order_id, OrderStatus.CHECKING)
            self.repo.update_code(code.id, status=CodeStatus.CHECKING)
            self.retry.enqueue(code.id)
            resumed += 1
        if resumed:
            log.info("[Recovery] Resumed %s unfinished code check(s)", resumed)
        return resumed

    # ------------------------------------------------------------------ #
    def _notify_admin(self, text: str) -> None:
        notifier = getattr(self.messenger, "notify_admin", None)
        if callable(notifier):
            try:
                notifier(text)
            except Exception:  # pragma: no cover - admin channel best-effort
                log.exception("Failed to notify admin")
