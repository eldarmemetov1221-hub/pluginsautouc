"""Plugin wiring for FunPayCardinal (task spec, sections 15, 16, 22).

Builds the object graph and exposes handlers that the FPC loader binds to
events. It does NOT create its own polling loop - it rides FPC's existing
event loop (section 16). Everything transport-facing is duck-typed so the
core is testable without FunPayCardinal installed.
"""

from __future__ import annotations

from typing import Optional

from .config import Config, get_config
from .database.db import Database
from .database.repository import Repository
from .funpay import orders as funpay_orders
from .funpay.messenger import FunPayMessenger
from .services.admin_service import AdminService
from .services.order_service import OrderService
from .services.retry_service import RetryService
from .spark.client import SparkChecker
from .utils.logger import get_logger

log = get_logger("plugin")


class Plugin:
    """Holds the wired object graph for one FPC instance."""

    def __init__(self, cardinal, config: Optional[Config] = None, *, async_mode: bool = True):
        self.cardinal = cardinal
        self.cfg = config or get_config()
        self.db = Database(self.cfg.database_path)
        self.repo = Repository(self.db)
        self.messenger = FunPayMessenger(cardinal, self.cfg, self.repo)
        self.checker = SparkChecker(self.cfg)
        # retry needs the order service's result handler; break the cycle by
        # constructing retry first with a late-bound handler.
        self.retry = RetryService(
            self.cfg, self.checker, self._on_result, async_mode=async_mode
        )
        self.orders = OrderService(self.cfg, self.repo, self.messenger, self.retry)
        self.admin = AdminService(self.cfg, self.repo, self.orders)
        self.retry.set_code_resolver(self._resolve_code)

    # ------------------------------------------------------------------ #
    def _on_result(self, code_id, result, error, attempts):
        self.orders.apply_result(code_id, result, error, attempts)

    def _resolve_code(self, code_id: int) -> str:
        code = self.repo.get_code(code_id)
        if code is None:
            raise RuntimeError(f"code_id {code_id} vanished")
        return code.code

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.retry.start()
        resumed = self.orders.resume_unfinished()
        log.info(
            "Plugin started (mock=%s, lots=%s, resumed=%s)",
            self.cfg.spark_mock,
            list(self.cfg.lots.keys()),
            resumed,
        )

    def stop(self) -> None:
        self.retry.stop()
        self.db.close()

    # ------------------------------------------------------------------ #
    # Event handlers
    # ------------------------------------------------------------------ #
    def on_new_order(self, order_shortcut) -> None:
        funpay_order_id = str(getattr(order_shortcut, "id", "") or "")
        if not funpay_order_id:
            return
        # Idempotent event guard (section 20).
        if not self.repo.mark_event_processed(f"order:{funpay_order_id}"):
            log.info("[FunPay] Duplicate NEW_ORDER event #%s ignored", funpay_order_id)
            return

        record = funpay_orders.build_order_record(self.cardinal, order_shortcut)
        if record is None or not record.funpay_order_id:
            return
        # Lot matching strictly by lot_id (chosen strategy).
        if not self.cfg.is_tracked_lot(record.lot_id):
            log.info(
                "[FunPay] Order #%s lot=%s not tracked, skipped",
                record.funpay_order_id, record.lot_id or "?",
            )
            return
        self.orders.handle_new_order(record)

    def on_new_message(self, message) -> None:
        message_id = str(getattr(message, "id", "") or "")
        author_id = str(getattr(message, "author_id", "") or "")
        chat_id = str(getattr(message, "chat_id", "") or "")
        text = getattr(message, "text", "") or ""

        # Ignore our own / bot messages.
        if getattr(message, "by_bot", False):
            return
        our_id = str(getattr(getattr(self.cardinal, "account", None), "id", "") or "")
        if our_id and author_id == our_id:
            return

        if message_id and not self.repo.mark_event_processed(f"msg:{message_id}"):
            return  # duplicate event (section 10 & 20)

        self.orders.handle_message(author_id, chat_id, text, message_id)


# --------------------------------------------------------------------------- #
# Module-level singleton used by the FPC loader.
# --------------------------------------------------------------------------- #
_plugin: Optional[Plugin] = None


def init(cardinal, *args) -> Plugin:
    global _plugin
    if _plugin is None:
        _plugin = Plugin(cardinal)
        _plugin.start()
        _register_admin_commands(cardinal, _plugin)
    return _plugin


def on_new_order(cardinal, event, *args) -> None:
    if _plugin is None:
        init(cardinal)
    _plugin.on_new_order(getattr(event, "order", event))


def on_new_message(cardinal, event, *args) -> None:
    if _plugin is None:
        init(cardinal)
    _plugin.on_new_message(getattr(event, "message", event))


# --------------------------------------------------------------------------- #
# Admin Telegram commands (section 18) - best effort; skipped if FPC has no
# telegram bot. Guarded by the ADMIN_IDS whitelist.
# --------------------------------------------------------------------------- #
def _register_admin_commands(cardinal, plugin: Plugin) -> None:
    tg = getattr(cardinal, "telegram", None)
    bot = getattr(tg, "bot", None)
    if bot is None:
        log.info("Telegram bot unavailable - admin commands not registered")
        return

    cfg = plugin.cfg
    admin = plugin.admin

    def guard(message):
        return cfg.is_admin(getattr(getattr(message, "from_user", None), "id", None))

    def reply(message, text):
        try:
            bot.reply_to(message, text)
        except Exception:
            log.exception("Failed to reply to admin command")

    def _args(message):
        parts = (getattr(message, "text", "") or "").split()
        return parts[1:]

    try:
        @bot.message_handler(commands=["uc_order"])
        def _order(message):  # pragma: no cover - requires telebot
            if not guard(message):
                return
            a = _args(message)
            reply(message, admin.order_status(a[0]) if a else "Usage: /uc_order <funpay_order_id>")

        @bot.message_handler(commands=["uc_code"])
        def _code(message):  # pragma: no cover
            if not guard(message):
                return
            a = _args(message)
            reply(message, admin.code_status(a[0]) if a else "Usage: /uc_code <code_id>")

        @bot.message_handler(commands=["uc_history"])
        def _hist(message):  # pragma: no cover
            if not guard(message):
                return
            a = _args(message)
            reply(message, admin.history(a[0]) if a else "Usage: /uc_history <funpay_order_id>")

        @bot.message_handler(commands=["uc_recheck"])
        def _recheck(message):  # pragma: no cover
            if not guard(message):
                return
            a = _args(message)
            reply(message, admin.recheck(a[0]) if a else "Usage: /uc_recheck <code_id>")

        @bot.message_handler(commands=["uc_cancel"])
        def _cancel(message):  # pragma: no cover
            if not guard(message):
                return
            a = _args(message)
            reply(message, admin.cancel_retry(a[0]) if a else "Usage: /uc_cancel <code_id>")

        @bot.message_handler(commands=["uc_setstatus"])
        def _setstatus(message):  # pragma: no cover
            if not guard(message):
                return
            a = _args(message)
            reply(
                message,
                admin.set_status(a[0], a[1]) if len(a) >= 2
                else "Usage: /uc_setstatus <funpay_order_id> <STATUS>",
            )

        @bot.message_handler(commands=["uc_resend"])
        def _resend(message):  # pragma: no cover
            if not guard(message):
                return
            a = _args(message)
            reply(message, admin.resend_ask(a[0]) if a else "Usage: /uc_resend <funpay_order_id>")

        log.info("Admin commands registered")
    except Exception:
        log.exception("Failed to register admin commands")
