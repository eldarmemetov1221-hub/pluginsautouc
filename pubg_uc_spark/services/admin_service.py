"""Admin operations (task spec, section 18).

Pure, transport-agnostic methods returning human-readable text. The plugin
wires these to FPC Telegram commands; access is gated by the admin whitelist
(``ADMIN_IDS``) at the call site.
"""

from __future__ import annotations

from ..database.models import CodeStatus, OrderStatus
from ..utils.logger import get_logger, mask_code

log = get_logger("admin")


class AdminService:
    def __init__(self, config, repo, order_service):
        self.cfg = config
        self.repo = repo
        self.orders = order_service

    # ---- read ---- #
    def order_status(self, funpay_order_id: str) -> str:
        order = self.repo.get_order_by_funpay_id(str(funpay_order_id))
        if not order:
            return f"Order #{funpay_order_id} not found."
        codes = self.repo.get_codes_for_order(order.id)
        lines = [
            f"Order #{order.funpay_order_id}",
            f"  lot={order.lot_id} buyer={order.buyer_username} ({order.buyer_id})",
            f"  qty={order.quantity} status={order.status}",
            f"  created={order.created_at} updated={order.updated_at}",
            f"  codes: {len(codes)}",
        ]
        for c in codes:
            lines.append(
                f"    #{c.id} {mask_code(c.code)} status={c.status} "
                f"spark={c.spark_status} attempts={c.attempts}"
            )
        return "\n".join(lines)

    def code_status(self, code_id: int) -> str:
        c = self.repo.get_code(int(code_id))
        if not c:
            return f"Code #{code_id} not found."
        return (
            f"Code #{c.id} order=#{c.funpay_order_id}\n"
            f"  {mask_code(c.code)} status={c.status} spark={c.spark_status}\n"
            f"  attempts={c.attempts} error={c.error_message}\n"
            f"  created={c.created_at} checked={c.checked_at}"
        )

    def history(self, funpay_order_id: str) -> str:
        order = self.repo.get_order_by_funpay_id(str(funpay_order_id))
        if not order:
            return f"Order #{funpay_order_id} not found."
        logs = self.repo.get_logs_for_order(order.id)
        if not logs:
            return f"Order #{funpay_order_id}: no log entries."
        return "\n".join(
            f"[{l['created_at']}] {l['level']} {l['event']}: {l['message']}" for l in logs
        )

    # ---- write ---- #
    def recheck(self, code_id: int) -> str:
        """Force a re-check of a code, overriding final-negative guard."""
        c = self.repo.get_code(int(code_id))
        if not c:
            return f"Code #{code_id} not found."
        self.repo.update_code(c.id, status=CodeStatus.CHECKING, error_message="")
        if c.order_id:
            self.repo.set_order_status(c.order_id, OrderStatus.CHECKING, force=True)
        self.repo.add_log("admin_recheck", f"code #{c.id}", order_id=c.order_id, code_id=c.id)
        self.orders.retry.enqueue(c.id)
        return f"Code #{c.id} re-queued for checking."

    def cancel_retry(self, code_id: int) -> str:
        """Stop future automatic retries for a code."""
        c = self.repo.get_code(int(code_id))
        if not c:
            return f"Code #{code_id} not found."
        self.repo.update_code(c.id, status=CodeStatus.FAILED, error_message="cancelled by admin")
        self.repo.add_log("admin_cancel", f"code #{c.id}", order_id=c.order_id, code_id=c.id)
        return f"Code #{c.id} retries cancelled (marked FAILED)."

    def set_status(self, funpay_order_id: str, status: str) -> str:
        order = self.repo.get_order_by_funpay_id(str(funpay_order_id))
        if not order:
            return f"Order #{funpay_order_id} not found."
        try:
            new_status = OrderStatus(status.upper())
        except ValueError:
            return f"Unknown status '{status}'. Valid: {', '.join(s.value for s in OrderStatus)}"
        self.repo.set_order_status(order.id, new_status, force=True)
        self.repo.add_log("admin_set_status", new_status.value, order_id=order.id)
        return f"Order #{funpay_order_id} status forced to {new_status.value}."

    def resend_ask(self, funpay_order_id: str) -> str:
        """Manually ask the buyer for their UID (the bot never does this auto)."""
        order = self.repo.get_order_by_funpay_id(str(funpay_order_id))
        if not order:
            return f"Order #{funpay_order_id} not found."
        lot = self.cfg.lot(order.lot_id)
        text = self.cfg.messages.ask_uid.format(
            order_id=order.funpay_order_id, product=(lot.product if lot else ""), uid=""
        )
        # Force send (bypass the once-guard) since the admin explicitly asked.
        ok = self.orders.messenger.send(order.chat_id, text)
        self.repo.add_log("admin_resend", "ask_code", order_id=order.id)
        return "Message re-sent." if ok else "Failed to send (no chat_id?)."
