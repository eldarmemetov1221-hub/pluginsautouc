"""Plugin wiring for FunPayCardinal (task spec, sections 15, 16, 22).

Builds the object graph and exposes handlers that the FPC loader binds to
events. It does NOT create its own polling loop - it rides FPC's existing
event loop (section 16). Everything transport-facing is duck-typed so the
core is testable without FunPayCardinal installed.
"""

from __future__ import annotations

from typing import Optional

from .config import Config, SPARK_BASE_DENOMINATIONS, get_config
from .database.db import Database
from .database.repository import Repository
from .errors import SparkCriticalError
from .funpay import orders as funpay_orders
from .funpay.messenger import FunPayMessenger
from .services.admin_service import AdminService
from .services.finance_store import FinanceStore
from .services.order_service import OrderService
from .services.retry_service import RetryService
from .spark.client import SparkChecker
from .utils.logger import get_logger

log = get_logger("plugin")

# Must match UUID in plugins/pubg_uc_spark.py - used to catch FPC's plugin-card
# "Настройки" callback for THIS plugin.
PLUGIN_UUID = "8f3a2c10-9b7e-4d5a-8c21-1f6e37330959"


class Plugin:
    """Holds the wired object graph for one FPC instance."""

    def __init__(self, cardinal, config: Optional[Config] = None, *, async_mode: bool = True):
        self.cardinal = cardinal
        self.cfg = config or get_config()
        self.db = Database(self.cfg.database_path)
        self.repo = Repository(self.db)
        self.messenger = FunPayMessenger(cardinal, self.cfg, self.repo)
        self.checker = SparkChecker(self.cfg)
        # retry calls _perform_check(code_id) and reports via _on_result.
        self.retry = RetryService(
            self.cfg, self._perform_check, self._on_result, async_mode=async_mode
        )
        self.orders = OrderService(self.cfg, self.repo, self.messenger, self.retry)
        self.admin = AdminService(self.cfg, self.repo, self.orders)
        self.finance_store = FinanceStore(self.cfg)

    # ------------------------------------------------------------------ #
    def _on_result(self, code_id, result, error, attempts):
        self.orders.apply_result(code_id, result, error, attempts)

    def _perform_check(self, code_id: int):
        """Build the Spark redeem request for a stored UID and run it."""
        code = self.repo.get_code(code_id)
        if code is None:
            raise SparkCriticalError(f"code_id {code_id} vanished")
        order = self.repo.get_order(code.order_id) if code.order_id else None
        lot = self.cfg.lot(order.lot_id) if order else None
        quantity = order.quantity if order else 1
        # Spark picks are the lot's base combination multiplied by the quantity.
        picks = lot.picks_for(quantity) if lot else {"60": max(1, quantity)}
        return self.checker.redeem(code.code, picks)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.retry.start()
        self.finance_store.load_into_cfg()
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

        # FunPay orders carry no offer id, so match the tracked lot by the
        # order description (configurable keywords / denomination + "uc").
        lot = funpay_orders.match_lot(self.cfg, order_shortcut)
        if lot is None:
            desc = getattr(order_shortcut, "description", "") or ""
            log.info("[FunPay] Order #%s not a tracked lot (desc=%r), skipped",
                     funpay_order_id, desc[:80])
            return
        record = funpay_orders.build_order_record(order_shortcut, lot)
        if not record.funpay_order_id:
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
        # parse_mode="" forces plain text (None would use the bot's HTML default,
        # which rejects any '<...>' in usage hints with a 400 error).
        try:
            bot.reply_to(message, text, parse_mode="")
        except Exception:
            log.exception("Failed to reply to admin command")

    def _args(message):
        parts = (getattr(message, "text", "") or "").split()
        return parts[1:]

    try:
        @bot.message_handler(commands=["uc_help"])
        def _help(message):  # pragma: no cover - requires telebot
            if not guard(message):
                return
            reply(message, admin.help_text())

        @bot.message_handler(commands=["uc_stats"])
        def _stats(message):  # pragma: no cover
            if not guard(message):
                return
            reply(message, admin.stats())

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

        @bot.message_handler(commands=["uc_skip"])
        def _skip(message):  # pragma: no cover
            if not guard(message):
                return
            a = _args(message)
            reply(message, admin.skip(a[0]) if a else "Usage: /uc_skip <funpay_order_id>")

        @bot.message_handler(commands=["uc_addorder"])
        def _addorder(message):  # pragma: no cover - requires telebot
            if not guard(message):
                return
            a = _args(message)
            if len(a) < 3:
                reply(message, "Usage: /uc_addorder <order_id> <lot_id> <uid> [price]")
                return
            price = a[3] if len(a) >= 4 else None
            reply(message, admin.add_order(a[0], a[1], a[2], price=price))

        @bot.message_handler(commands=["uc_finance"])
        def _finance(message):  # pragma: no cover - requires telebot
            if not guard(message):
                return
            import re
            a = _args(message)
            if a:
                arg = a[0]
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
                    reply(message, admin.finance_period(day=arg)); return
                if arg.isdigit():
                    reply(message, admin.finance_period(days=int(arg))); return
            reply(message, admin.finance()
                  + "\n\n📅 Период: /uc_finance <дней> (напр. /uc_finance 7)"
                  + "\n📆 Конкретный день: /uc_finance ГГГГ-ММ-ДД"
                  + "\n⚙️ Настроить цены: /uc_prices")

        # ---- Interactive price/commission menu (/uc_prices) ---- #
        def _fmt_money(v):
            try:
                return f"{float(v):g}"
            except (TypeError, ValueError):
                return "0"

        def _menu_markup():
            from telebot import types
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(types.InlineKeyboardButton(
                f"Комиссия: {_fmt_money(cfg.commission_percent)}%", callback_data="ucfin:comm"))
            btns = [
                types.InlineKeyboardButton(
                    f"{d}: {_fmt_money(cfg.pack_costs.get(d, 0))}₽",
                    callback_data=f"ucfin:pack:{d}")
                for d in SPARK_BASE_DENOMINATIONS
            ]
            kb.add(*btns)
            kb.add(
                types.InlineKeyboardButton("💰 Отчёт", callback_data="ucfin:report"),
                types.InlineKeyboardButton("📦 Сток", callback_data="ucfin:stock"),
            )
            kb.add(
                types.InlineKeyboardButton("🔄 Обновить", callback_data="ucfin:refresh"),
                types.InlineKeyboardButton("⬅️ Назад", callback_data="ucfin:root"),
            )
            return kb

        def _menu_text():
            lines = "\n".join(
                f"  {d} UC: {_fmt_money(cfg.pack_costs.get(d, 0))} ₽"
                for d in SPARK_BASE_DENOMINATIONS
            )
            return (
                "📊 Статистика и цены\n\n"
                f"Комиссия FunPay: {_fmt_money(cfg.commission_percent)}%\n"
                "Себестоимость пачек Spark:\n"
                f"{lines}\n\n"
                "Нажми «💰 Отчёт» для прибыли, или кнопку значения, чтобы изменить."
            )

        # ---- Root menu (opened from the plugin card / /uc_prices) ---- #
        def _auto_on():
            return bool(getattr(cfg, "auto_delivery", True))

        def _root_markup():
            from telebot import types
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                types.InlineKeyboardButton("📊 Статистика", callback_data="ucfin:menu"),
                types.InlineKeyboardButton("📦 Сток", callback_data="ucfin:stock"),
            )
            toggle = ("⏸ Выключить автовыдачу" if _auto_on()
                      else "▶️ Включить автовыдачу")
            kb.add(types.InlineKeyboardButton(toggle, callback_data="ucfin:toggle"))
            kb.add(types.InlineKeyboardButton("❌ Закрыть", callback_data="ucfin:close"))
            return kb

        def _root_text():
            state = "🟢 ВКЛ" if _auto_on() else "🔴 ВЫКЛ — ручная выдача"
            return ("🎮 PUBG UC Spark\n\n"
                    f"Автовыдача: {state}\n\n"
                    "📊 Статистика — прибыль, выручка, себестоимость, цены\n"
                    "📦 Сток — остатки Spark и каких пачек не хватает\n"
                    "⏸/▶️ — вкл/выкл автоматическое начисление")

        def _back_markup():
            from telebot import types
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                types.InlineKeyboardButton("⬅️ Назад", callback_data="ucfin:root"),
                types.InlineKeyboardButton("❌ Закрыть", callback_data="ucfin:close"),
            )
            return kb

        def _fin_markup():
            """Period picker under the finance report."""
            from telebot import types
            kb = types.InlineKeyboardMarkup(row_width=3)
            kb.add(
                types.InlineKeyboardButton("Сегодня", callback_data="ucfin:fin:1"),
                types.InlineKeyboardButton("5 дней", callback_data="ucfin:fin:5"),
                types.InlineKeyboardButton("7 дней", callback_data="ucfin:fin:7"),
            )
            kb.add(
                types.InlineKeyboardButton("20 дней", callback_data="ucfin:fin:20"),
                types.InlineKeyboardButton("Всё время", callback_data="ucfin:fin:all"),
                types.InlineKeyboardButton("📅 День", callback_data="ucfin:finpick"),
            )
            kb.add(
                types.InlineKeyboardButton("🎟 Коды", callback_data="ucfin:codes:1"),
                types.InlineKeyboardButton("🧹 Сбросить статистику", callback_data="ucfin:resetask"),
            )
            kb.add(
                types.InlineKeyboardButton("⬅️ Назад", callback_data="ucfin:root"),
                types.InlineKeyboardButton("❌ Закрыть", callback_data="ucfin:close"),
            )
            return kb

        def _codes_markup():
            """Period picker for the 'activated codes' report."""
            from telebot import types
            kb = types.InlineKeyboardMarkup(row_width=3)
            kb.add(
                types.InlineKeyboardButton("Сегодня", callback_data="ucfin:codes:1"),
                types.InlineKeyboardButton("5 дней", callback_data="ucfin:codes:5"),
                types.InlineKeyboardButton("7 дней", callback_data="ucfin:codes:7"),
            )
            kb.add(
                types.InlineKeyboardButton("20 дней", callback_data="ucfin:codes:20"),
                types.InlineKeyboardButton("Всё время", callback_data="ucfin:codes:all"),
                types.InlineKeyboardButton("📅 День", callback_data="ucfin:codespick"),
            )
            kb.add(
                types.InlineKeyboardButton("💰 Финансы", callback_data="ucfin:report"),
                types.InlineKeyboardButton("❌ Закрыть", callback_data="ucfin:close"),
            )
            return kb

        def _force_reply():
            from telebot import types
            return types.ForceReply(selective=False)

        def _parse_num(text):
            import re
            m = re.search(r"[0-9]+(?:[.,][0-9]+)?", str(text or ""))
            return float(m.group(0).replace(",", ".")) if m else None

        def _show_menu(chat_id):
            bot.send_message(chat_id, _menu_text(), reply_markup=_menu_markup())

        def _show_root(chat_id):
            bot.send_message(chat_id, _root_text(), reply_markup=_root_markup())

        def _stock_text():
            try:
                stock = plugin.checker.stock_summary()
            except Exception as exc:
                return f"Не удалось получить сток Spark: {exc}"
            try:
                amounts = funpay_orders.get_lot_amounts(cardinal, list(cfg.lots.keys()))
            except Exception:
                amounts = {}
            return admin.stock_report(stock, amounts)

        @bot.message_handler(commands=["uc_stock"])
        def _stock(message):  # pragma: no cover - requires telebot
            if not guard(message):
                return
            reply(message, _stock_text())

        @bot.message_handler(commands=["uc_prices"])
        def _prices(message):  # pragma: no cover - requires telebot
            if not guard(message):
                return
            _show_root(message.chat.id)

        @bot.message_handler(commands=["uc_finance_reset"])
        def _fin_reset(message):  # pragma: no cover - requires telebot
            if not guard(message):
                return
            a = _args(message)
            if a and a[0].lower() in ("off", "отмена", "undo", "reset"):
                reply(message, admin.finance_reset_off())
            else:
                reply(message, admin.finance_reset())

        @bot.message_handler(commands=["uc_codes"])
        def _codes(message):  # pragma: no cover - requires telebot
            if not guard(message):
                return
            import re
            a = _args(message)
            if a:
                arg = a[0]
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
                    reply(message, admin.codes_activated(day=arg)); return
                if arg.isdigit():
                    reply(message, admin.codes_activated(days=int(arg))); return
            reply(message, admin.codes_activated(days=1)
                  + "\n\n📅 Период: /uc_codes <дней> (напр. /uc_codes 7)"
                  + "\n📆 День: /uc_codes ГГГГ-ММ-ДД")

        @bot.message_handler(commands=["uc_pause"])
        def _pause(message):  # pragma: no cover - requires telebot
            if not guard(message):
                return
            plugin.finance_store.set_auto_delivery(False)
            reply(message,
                  "⏸ Автовыдача ВЫКЛЮЧЕНА.\n"
                  "Покупатели присылают UID как обычно, но начисление НЕ идёт — "
                  "плагин копит заказы и присылает тебе UID для ручной выдачи.\n"
                  "После выдачи помечай: /uc_setstatus <order_id> VALID\n"
                  "Включить обратно: /uc_resume")

        @bot.message_handler(commands=["uc_resume"])
        def _resume(message):  # pragma: no cover - requires telebot
            if not guard(message):
                return
            plugin.finance_store.set_auto_delivery(True)
            reply(message,
                  "▶️ Автовыдача ВКЛЮЧЕНА. Новые заказы начисляются автоматически.\n"
                  "⚠️ Заказы, пришедшие на паузе, автоматически НЕ до-начисляются — "
                  "проверь их вручную (/uc_stats).")

        def _set_commission(message):  # pragma: no cover
            if not guard(message):
                return
            v = _parse_num(getattr(message, "text", ""))
            if v is None:
                reply(message, "Не похоже на число. Изменение отменено.")
                return
            plugin.finance_store.set_commission(v)
            reply(message, f"✅ Комиссия FunPay: {_fmt_money(v)}%")
            _show_menu(message.chat.id)

        def _set_pack(message, denom):  # pragma: no cover
            if not guard(message):
                return
            v = _parse_num(getattr(message, "text", ""))
            if v is None:
                reply(message, "Не похоже на число. Изменение отменено.")
                return
            plugin.finance_store.set_pack_cost(denom, v)
            reply(message, f"✅ Себестоимость {denom} UC: {_fmt_money(v)} ₽")
            _show_menu(message.chat.id)

        def _finance_day(message):  # pragma: no cover
            if not guard(message):
                return
            import re
            t = (getattr(message, "text", "") or "").strip()
            m = re.search(r"\d{4}-\d{2}-\d{2}", t)
            if not m:
                reply(message, "Не похоже на дату. Нужен формат ГГГГ-ММ-ДД, например 2026-09-15. Отменено.")
                return
            bot.send_message(message.chat.id, admin.finance_period(day=m.group(0)),
                             reply_markup=_fin_markup())

        def _codes_day(message):  # pragma: no cover
            if not guard(message):
                return
            import re
            t = (getattr(message, "text", "") or "").strip()
            m = re.search(r"\d{4}-\d{2}-\d{2}", t)
            if not m:
                reply(message, "Не похоже на дату. Нужен формат ГГГГ-ММ-ДД, например 2026-09-15. Отменено.")
                return
            bot.send_message(message.chat.id, admin.codes_activated(day=m.group(0)),
                             reply_markup=_codes_markup())

        @bot.callback_query_handler(func=lambda c: (getattr(c, "data", "") or "").startswith("ucfin:"))
        def _fin_cb(call):  # pragma: no cover - requires telebot
            uid = getattr(getattr(call, "from_user", None), "id", None)
            if not cfg.is_admin(uid):
                return
            data = call.data or ""
            chat_id = call.message.chat.id
            try:
                if data == "ucfin:close":
                    bot.answer_callback_query(call.id)
                    try:
                        bot.delete_message(chat_id, call.message.message_id)
                    except Exception:
                        pass
                    return
                if data in ("ucfin:refresh", "ucfin:menu"):
                    bot.answer_callback_query(call.id, "Обновлено" if data == "ucfin:refresh" else None)
                    try:
                        bot.edit_message_text(_menu_text(), chat_id, call.message.message_id,
                                              reply_markup=_menu_markup())
                    except Exception:
                        _show_menu(chat_id)
                    return
                if data == "ucfin:root":
                    bot.answer_callback_query(call.id)
                    try:
                        bot.edit_message_text(_root_text(), chat_id, call.message.message_id,
                                              reply_markup=_root_markup())
                    except Exception:
                        _show_root(chat_id)
                    return
                if data == "ucfin:toggle":
                    new_state = not _auto_on()
                    plugin.finance_store.set_auto_delivery(new_state)
                    bot.answer_callback_query(
                        call.id,
                        "▶️ Автовыдача включена" if new_state else "⏸ Автовыдача выключена")
                    try:
                        bot.edit_message_text(_root_text(), chat_id, call.message.message_id,
                                              reply_markup=_root_markup())
                    except Exception:
                        _show_root(chat_id)
                    return
                if data == "ucfin:report":
                    bot.answer_callback_query(call.id)
                    bot.send_message(chat_id, admin.finance(), reply_markup=_fin_markup())
                    return
                if data.startswith("ucfin:fin:"):
                    arg = data.split(":", 2)[2]
                    bot.answer_callback_query(call.id)
                    txt = (admin.finance_period() if arg == "all"
                           else admin.finance_period(days=int(arg)))
                    try:
                        bot.edit_message_text(txt, chat_id, call.message.message_id,
                                              reply_markup=_fin_markup())
                    except Exception:
                        bot.send_message(chat_id, txt, reply_markup=_fin_markup())
                    return
                if data == "ucfin:finpick":
                    bot.answer_callback_query(call.id)
                    m = bot.send_message(
                        chat_id, "Введите дату в формате ГГГГ-ММ-ДД (например 2026-09-15):",
                        reply_markup=_force_reply())
                    bot.register_next_step_handler(m, _finance_day)
                    return
                if data.startswith("ucfin:codes:"):
                    arg = data.split(":", 2)[2]
                    bot.answer_callback_query(call.id)
                    txt = (admin.codes_activated() if arg == "all"
                           else admin.codes_activated(days=int(arg)))
                    try:
                        bot.edit_message_text(txt, chat_id, call.message.message_id,
                                              reply_markup=_codes_markup())
                    except Exception:
                        bot.send_message(chat_id, txt, reply_markup=_codes_markup())
                    return
                if data == "ucfin:codespick":
                    bot.answer_callback_query(call.id)
                    m = bot.send_message(
                        chat_id, "Введите дату в формате ГГГГ-ММ-ДД (например 2026-09-15):",
                        reply_markup=_force_reply())
                    bot.register_next_step_handler(m, _codes_day)
                    return
                if data == "ucfin:resetask":
                    from telebot import types
                    kb = types.InlineKeyboardMarkup()
                    kb.add(
                        types.InlineKeyboardButton("✅ Да, обнулить", callback_data="ucfin:resetyes"),
                        types.InlineKeyboardButton("❌ Отмена", callback_data="ucfin:report"),
                    )
                    bot.answer_callback_query(call.id)
                    bot.edit_message_text(
                        "🧹 Обнулить всю статистику?\n\nПрибыль и счётчики начнут "
                        "считаться заново с этого момента. Старые заказы останутся "
                        "в базе (защита от повторной выдачи), просто не будут "
                        "учитываться в статистике.",
                        chat_id, call.message.message_id, reply_markup=kb)
                    return
                if data == "ucfin:resetyes":
                    bot.answer_callback_query(call.id, "Статистика обнулена")
                    try:
                        bot.edit_message_text(admin.finance_reset(), chat_id,
                                              call.message.message_id, reply_markup=_fin_markup())
                    except Exception:
                        bot.send_message(chat_id, admin.finance_reset(), reply_markup=_fin_markup())
                    return
                if data == "ucfin:stock":
                    bot.answer_callback_query(call.id, "Запрашиваю сток...")
                    bot.send_message(chat_id, _stock_text(), reply_markup=_back_markup())
                    return
                if data == "ucfin:comm":
                    bot.answer_callback_query(call.id)
                    m = bot.send_message(chat_id, "Введите комиссию FunPay в % (например 3):",
                                         reply_markup=_force_reply())
                    bot.register_next_step_handler(m, _set_commission)
                    return
                if data.startswith("ucfin:pack:"):
                    denom = data.split(":", 2)[2]
                    bot.answer_callback_query(call.id)
                    m = bot.send_message(
                        chat_id, f"Введите себестоимость пачки {denom} UC в ₽ (число):",
                        reply_markup=_force_reply())
                    bot.register_next_step_handler(m, lambda msg: _set_pack(msg, denom))
                    return
            except Exception:
                log.exception("Finance menu callback failed")

        # FPC's plugin-card "⚙️ Настройки" button (shown because SETTINGS_PAGE=True)
        # -> open our stats/prices menu. Resolve FPC's callback prefix at runtime.
        try:
            from tg_bot import CBT as _CBT  # type: ignore
            _PS = getattr(_CBT, "PLUGIN_SETTINGS", "plugin_settings")
        except Exception:
            _PS = "plugin_settings"

        @bot.callback_query_handler(
            func=lambda c: (getattr(c, "data", "") or "").startswith(f"{_PS}:")
            and PLUGIN_UUID in (c.data or "")
        )
        def _open_settings(call):  # pragma: no cover - requires telebot
            if not cfg.is_admin(getattr(getattr(call, "from_user", None), "id", None)):
                bot.answer_callback_query(call.id, "Нет доступа")
                return
            bot.answer_callback_query(call.id)
            _show_root(call.message.chat.id)

        log.info("Admin commands registered")
    except Exception:
        log.exception("Failed to register admin commands")
