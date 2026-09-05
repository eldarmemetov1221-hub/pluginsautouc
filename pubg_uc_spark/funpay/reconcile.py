"""Startup reconciliation: recover orders missed while Cardinal was offline.

Why this exists
---------------
FunPayCardinal does **not** replay orders that arrived while it was down. On
startup it treats whatever sales already exist as a baseline and only emits a
``NEW_ORDER`` event for sales placed *after* it comes up. So an order paid
during a network / DNS outage is never delivered to the plugin, and when the
buyer later sends their PUBG UID it is ignored - ``handle_message`` finds no
registered order for that buyer and returns silently. The order then hangs,
unfulfilled and unanswered, until someone notices. A watchdog that restarts
Cardinal does not help: the restart re-establishes the same baseline.

What this does
--------------
Run once after the account is logged in (FPC ``post_init``) and on demand via
``/uc_backfill``:

1. Pull the seller's currently **open** sales from FunPay.
2. Match tracked lots (by description, as everywhere else).
3. Register any that are missing from our DB (-> ``WAITING_FOR_CODE``), so the
   buyer's next / previous UID message can be acted on.
4. If enabled, read each order's chat history and pick up a UID the buyer
   already sent during the outage, routing it through the normal intake path
   (validation, dedup, enqueue) so nothing is delivered twice.

Everything FunPay-facing is duck-typed and wrapped defensively: an unexpected
API shape or a failed call degrades (skip that step / that order) rather than
crashing startup. All double-delivery guards are the ordinary ones - order
dedup by ``funpay_order_id``, code dedup by ``(order_id, code_hash)``,
``send_once`` for messages, and ``processed_events`` for the recovered message -
so running backfill repeatedly (e.g. on every restart) is safe and idempotent.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..config import Config
from ..database.models import OrderStatus
from ..utils.logger import get_logger
from ..utils.validators import extract_first_code
from . import orders as funpay_orders

log = get_logger("reconcile")


# Substrings in a FunPay sale status that mean "already finished" - we must
# never touch these, only sales still awaiting fulfilment.
_CLOSED_MARKERS = ("closed", "complete", "refund", "cancel")

# Order states that can still accept a recovered UID from chat history.
_UID_RECOVERABLE = {
    OrderStatus.WAITING_FOR_CODE,
    OrderStatus.ACCOUNT_NOT_FOUND,
    OrderStatus.INVALID,
    OrderStatus.ALREADY_USED,
}


def _status_text(shortcut) -> str:
    status = getattr(shortcut, "status", None)
    if status is None:
        return ""
    return str(getattr(status, "value", status)).lower()


def order_is_open(shortcut) -> bool:
    """True if a sale still awaits fulfilment (not closed/refunded/cancelled).

    Unknown/absent status -> treated as open; the DB dedup guards still prevent
    delivering an order we already completed.
    """
    text = _status_text(shortcut)
    if not text:
        return True
    return not any(m in text for m in _CLOSED_MARKERS)


class Reconciler:
    """Recovers missed orders. ``orders_service`` is the live OrderService, so
    recovered orders/UIDs go through exactly the same code path as live ones."""

    def __init__(self, cardinal, config: Config, repo, orders_service):
        self.cardinal = cardinal
        self.cfg = config
        self.repo = repo
        self.orders = orders_service

    # ------------------------------------------------------------------ #
    def run(self) -> dict:
        stats = {
            "scanned": 0,
            "tracked": 0,
            "registered": 0,
            "uid_recovered": 0,
            "already_known": 0,
        }
        if not self.cfg.backfill_on_start:
            log.info("[Backfill] Disabled (BACKFILL_ON_START=0)")
            stats["enabled"] = False
            return stats

        try:
            sales = self._fetch_sales()
        except Exception:  # pragma: no cover - defensive
            log.exception("[Backfill] Fatal error fetching sales - skipped")
            return stats

        for shortcut in sales:
            stats["scanned"] += 1
            fid = str(getattr(shortcut, "id", "") or "")
            if not fid or not order_is_open(shortcut):
                continue
            lot = funpay_orders.match_lot(self.cfg, shortcut)
            if lot is None:
                continue
            stats["tracked"] += 1

            existing = self.repo.get_order_by_funpay_id(fid)
            if existing is not None:
                stats["already_known"] += 1
                # Known but still waiting? The buyer may have sent the UID during
                # the outage - try to recover it.
                if (
                    self.cfg.backfill_read_history
                    and OrderStatus(existing.status) in _UID_RECOVERABLE
                    and self._try_recover_uid(existing, shortcut)
                ):
                    stats["uid_recovered"] += 1
                continue

            # A genuinely missed order: register it, then try its UID.
            record = funpay_orders.build_order_record(shortcut, lot)
            if not record.funpay_order_id:
                continue
            # Mark the order event as seen so a (hypothetical) live NEW_ORDER for
            # the same id becomes a no-op; create_order is idempotent regardless.
            self.repo.mark_event_processed(f"order:{fid}")
            order = self.orders.handle_new_order(record)
            stats["registered"] += 1
            log.info(
                "[Backfill] Registered missed order #%s (lot=%s buyer=%s qty=%s)",
                fid, lot.lot_id, record.buyer_username, record.quantity,
            )
            if self.cfg.backfill_read_history and order is not None:
                if self._try_recover_uid(order, shortcut):
                    stats["uid_recovered"] += 1

        if stats["registered"] or stats["uid_recovered"]:
            log.info("[Backfill] Recovered: %s", stats)
            self._notify_admin(stats)
        else:
            log.info(
                "[Backfill] Nothing to recover (%s scanned, %s tracked lots)",
                stats["scanned"], stats["tracked"],
            )
        return stats

    # ------------------------------------------------------------------ #
    # UID recovery from chat history
    # ------------------------------------------------------------------ #
    def _try_recover_uid(self, order, shortcut) -> bool:
        chat_id = order.chat_id or str(getattr(shortcut, "chat_id", "") or "")
        buyer_id = order.buyer_id or str(getattr(shortcut, "buyer_id", "") or "")
        found = self._fetch_uid_from_history(chat_id, buyer_id)
        if not found:
            return False
        text, mid = found
        # Process this message once across restarts (mirrors the live
        # NEW_MESSAGE path, which marks msg:<id> before handling).
        event_key = f"msg:{mid}" if mid else f"backfill-uid:{order.funpay_order_id}"
        if not self.repo.mark_event_processed(event_key):
            return False
        log.info(
            "[Backfill] Recovered UID for order #%s from chat history",
            order.funpay_order_id,
        )
        # Route through the normal intake path (format check, dedup, enqueue).
        self.orders.handle_message(buyer_id, chat_id, text, mid or event_key)
        return True

    def _fetch_uid_from_history(self, chat_id, buyer_id) -> Optional[Tuple[str, str]]:
        """Newest buyer message text + id that contains a valid-format UID."""
        account = self._account()
        if account is None or not chat_id or not hasattr(account, "get_chat_history"):
            return None
        try:
            msgs = list(self._call_get_history(account, chat_id))
        except Exception:
            log.exception("[Backfill] get_chat_history failed for chat %s", chat_id)
            return None

        recent = msgs[-max(1, self.cfg.backfill_history_limit):]
        # Newest first: take the most recent (possibly corrected) UID.
        for m in reversed(recent):
            if getattr(m, "by_bot", False):
                continue  # never read our own auto-messages
            author = str(getattr(m, "author_id", "") or "")
            # Only the buyer's own messages. Skip if we can't confirm authorship
            # (prevents extracting a UID out of our own success echo).
            if buyer_id and author != str(buyer_id):
                continue
            if not buyer_id and not author:
                continue
            text = getattr(m, "text", "") or ""
            if extract_first_code(text, self.cfg.code_pattern):
                mid = str(getattr(m, "id", "") or "")
                return text, mid
        return None

    # ------------------------------------------------------------------ #
    # FunPay account access (duck-typed / version-tolerant)
    # ------------------------------------------------------------------ #
    def _account(self):
        return getattr(self.cardinal, "account", None)

    def _fetch_sales(self) -> List:
        account = self._account()
        if account is None or not hasattr(account, "get_sales"):
            log.info("[Backfill] FunPay account/get_sales unavailable - skipped")
            return []
        collected: List = []
        seen_ids: set = set()
        start_from = None
        for page in range(max(1, self.cfg.backfill_max_pages)):
            try:
                result = self._call_get_sales(account, start_from)
            except Exception:
                log.exception("[Backfill] get_sales failed on page %s", page + 1)
                break
            next_from, sales = self._parse_sales_result(result)
            added = 0
            for s in sales:
                sid = str(getattr(s, "id", "") or "")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    collected.append(s)
                    added += 1
            if not next_from or added == 0:
                break
            start_from = next_from
        log.info("[Backfill] Fetched %s open sale(s) from FunPay", len(collected))
        return collected

    @staticmethod
    def _call_get_sales(account, start_from):
        """Call get_sales tolerating signature differences across FunPayAPI
        versions. Prefer excluding closed/refunded sales when supported."""
        for kwargs in (
            {"start_from": start_from, "include_closed": False, "include_refunded": False},
            {"start_from": start_from},
            {},
        ):
            try:
                return account.get_sales(**kwargs)
            except TypeError:
                continue
        return account.get_sales()

    @staticmethod
    def _call_get_history(account, chat_id):
        try:
            return account.get_chat_history(chat_id)
        except TypeError:
            return account.get_chat_history(chat_id, None)

    @staticmethod
    def _parse_sales_result(result) -> Tuple[Optional[str], List]:
        """Normalise get_sales() return into ``(next_order_id, [shortcut,...])``.

        FunPayAPI returns ``(next_order_id, [OrderShortcut])``; be liberal.
        """
        if isinstance(result, (tuple, list)):
            seq = list(result)
            # (next, [shortcuts]) or (next, [shortcuts], ...)
            for item in seq:
                if isinstance(item, (list, tuple)) and item and hasattr(item[0], "id"):
                    nxt = seq[0] if seq and isinstance(seq[0], str) else None
                    return nxt, list(item)
            # a plain list of shortcuts
            if seq and hasattr(seq[0], "id"):
                return None, seq
        return None, []

    # ------------------------------------------------------------------ #
    def _notify_admin(self, stats: dict) -> None:
        notifier = getattr(getattr(self.orders, "messenger", None), "notify_admin", None)
        if callable(notifier):
            try:
                notifier(
                    "♻️ Восстановление после простоя: "
                    f"новых заказов {stats['registered']}, "
                    f"из них с найденным UID {stats['uid_recovered']} "
                    f"(просмотрено продаж: {stats['scanned']})."
                )
            except Exception:  # pragma: no cover
                log.exception("[Backfill] admin notify failed")
