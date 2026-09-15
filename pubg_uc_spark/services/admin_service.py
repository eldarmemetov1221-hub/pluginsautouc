"""Admin operations (task spec, section 18).

Pure, transport-agnostic methods returning human-readable text. The plugin
wires these to FPC Telegram commands; access is gated by the admin whitelist
(``ADMIN_IDS``) at the call site.
"""

from __future__ import annotations

from ..config import SPARK_BASE_DENOMINATIONS
from ..database.models import CodeStatus, OrderStatus
from ..utils.logger import get_logger, mask_code

log = get_logger("admin")


class AdminService:
    def __init__(self, config, repo, order_service):
        self.cfg = config
        self.repo = repo
        self.orders = order_service

    # ---- help / stats ---- #
    def help_text(self) -> str:
        return (
            "🛠 Команды PUBG UC Spark:\n\n"
            "📋 Просмотр:\n"
            "/uc_help — этот список\n"
            "/uc_stats — сводка по заказам (всего и за сегодня)\n"
            "/uc_order <order_id> — статус заказа и его коды\n"
            "/uc_code <code_id> — детали кода\n"
            "/uc_history <order_id> — журнал событий заказа\n\n"
            "🔧 Действия:\n"
            "/uc_recheck <code_id> — повторить проверку/начисление\n"
            "/uc_cancel <code_id> — отменить автоповторы (FAILED)\n"
            "/uc_setstatus <order_id> <СТАТУС> — сменить статус заказа\n"
            "/uc_resend <order_id> — попросить покупателя прислать UID\n"
            "/uc_skip <order_id> — не начислять (если выдали вручную)\n\n"
            "⏯ Автовыдача:\n"
            "/uc_pause — выключить автоначисление (ручная выдача)\n"
            "/uc_resume — снова включить автоначисление\n\n"
            "💰 Финансы:\n"
            "/uc_finance — прибыль (всё время + сегодня)\n"
            "/uc_finance <дней> — за период (напр. /uc_finance 7)\n"
            "/uc_finance ГГГГ-ММ-ДД — за конкретный день\n"
            "/uc_prices — меню: цены, период, себестоимость, комиссия\n"
            "/uc_stock — остатки стока Spark и каких пачек не хватает"
        )

    def stock_report(self, stock: dict, lot_amounts: dict = None) -> str:
        """Compare Spark stock to what the active lots' FunPay availability needs.

        ``stock``       = {denom: available_in_spark}
        ``lot_amounts`` = {lot_id: наличие} (units the seller listed on FunPay);
                          a lot with None is counted as "наличие неизвестно".

        For every active lot: needed packs = наличие × its pack combo. Summed per
        pack and compared to Spark stock -> how many of each to restock.
        """
        lot_amounts = lot_amounts or {}

        need: dict = {}          # denom -> packs required to cover listed наличие
        unknown = []             # lots whose наличие couldn't be read
        for lot in self.cfg.lots.values():
            amt = lot_amounts.get(str(lot.lot_id))
            if amt is None:
                unknown.append(lot.product)
                continue
            for denom, cnt in lot.base_picks().items():
                need[str(denom)] = need.get(str(denom), 0) + cnt * int(amt)

        # Packs used by active lots (stable order by base denomination).
        used = [d for d in SPARK_BASE_DENOMINATIONS
                if any(d in lot.base_picks() for lot in self.cfg.lots.values())]

        rows = []                # (denom, have, req, restock)
        for d in used:
            have = int(stock.get(d, 0))
            req = int(need.get(d, 0))
            rows.append((d, have, req, max(0, req - have)))
        rows.sort(key=lambda r: (-r[3], r[1]))   # biggest shortage first

        out = ["📦 Сток Spark vs наличие лотов", ""]
        to_buy = []
        for d, have, req, restock in rows:
            if restock > 0:
                out.append(f"🟥 {d} UC — нужно {req}, в Spark {have} → не хватает {restock}")
                to_buy.append(f"{d}×{restock}")
            else:
                out.append(f"🟢 {d} UC — нужно {req}, в Spark {have}")

        # Total cost value of everything currently in Spark stock.
        stock_value = 0.0
        for d in SPARK_BASE_DENOMINATIONS:
            stock_value += int(stock.get(d, 0)) * float(self.cfg.pack_costs.get(d, 0) or 0)
        out.append("")
        out.append(f"💵 Себестоимость всего стока: {stock_value:.2f} ₽")

        out.append("")
        if to_buy:
            out.append("🛒 Докупить: " + ", ".join(to_buy))
        else:
            out.append("✅ Стока хватает под всё выставленное наличие.")

        if unknown:
            out.append("")
            out.append("⚠️ Не удалось прочитать наличие: " + ", ".join(unknown))
        return "\n".join(out)

    def _finance_calc(self, rows):
        revenue = cost = 0.0
        priced = skipped = 0
        for r in rows:
            p = float(r.get("price") or 0)
            if p <= 0:
                skipped += 1              # order captured before price tracking
                continue
            priced += 1
            revenue += p
            # Use the cost frozen when the order arrived; fall back to the
            # current calc only for legacy rows that have no snapshot (0).
            c = float(r.get("cost") or 0)
            if c <= 0:
                c = self.cfg.order_cost(r.get("lot_id"), r.get("quantity") or 1)
            cost += c
        commission = revenue * (self.cfg.commission_percent / 100.0)
        net = revenue - commission - cost
        return priced, skipped, revenue, commission, cost, net

    def _finance_block(self, t) -> str:
        priced, skipped, rev, com, cost, net = t
        head = f"  Заказов: {priced}" + (f" (+{skipped} без цены)" if skipped else "")
        return (
            f"{head}\n"
            f"  Выручка: {rev:.2f} ₽\n"
            f"  Комиссия {self.cfg.commission_percent:g}%: −{com:.2f} ₽\n"
            f"  Себестоимость: −{cost:.2f} ₽\n"
            f"  Чистая прибыль: {net:.2f} ₽"
        )

    def _cost_warn(self) -> str:
        if not self.cfg.pack_costs:
            return "\n\n⚠️ PACK_COSTS не заданы — себестоимость считается как 0."
        return ""

    def finance(self) -> str:
        """Overview: revenue / commission / cost / net for all time and today."""
        tz = getattr(self.cfg, "stats_tz_offset", 0)
        all_time = self._finance_calc(self.repo.finance_orders())
        today = self._finance_calc(self.repo.finance_orders(today_only=True, tz_offset=tz))
        return (
            "💰 Финансы PUBG UC (выполненные заказы)\n\n"
            f"За всё время:\n{self._finance_block(all_time)}\n\n"
            f"Сегодня:\n{self._finance_block(today)}" + self._cost_warn()
        )

    def finance_period(self, days: int = None, day: str = None) -> str:
        """Single-scope finance report: last ``days`` days, or a specific ``day``
        (YYYY-MM-DD), or all time. Days/day boundaries use the local timezone."""
        tz = getattr(self.cfg, "stats_tz_offset", 0)
        if day:
            rows = self.repo.finance_orders(day=day, tz_offset=tz)
            title = f"💰 Финансы PUBG UC — {day}"
        elif days and int(days) == 1:
            rows = self.repo.finance_orders(today_only=True, tz_offset=tz)
            title = "💰 Финансы PUBG UC — сегодня"
        elif days:
            rows = self.repo.finance_orders(days=int(days), tz_offset=tz)
            title = f"💰 Финансы PUBG UC — за {int(days)} дн."
        else:
            rows = self.repo.finance_orders()
            title = "💰 Финансы PUBG UC — за всё время"
        return f"{title}\n\n{self._finance_block(self._finance_calc(rows))}" + self._cost_warn()

    def stats(self) -> str:
        o_all = self.repo.order_status_counts()
        o_day = self.repo.order_status_counts(today_only=True)
        c_all = self.repo.code_status_counts()

        def line(d: dict) -> str:
            if not d:
                return "  —"
            return "  " + ", ".join(f"{k}={v}" for k, v in sorted(d.items()))

        return (
            "📊 Статистика PUBG UC Spark\n\n"
            f"Заказы (всего): {sum(o_all.values())}\n{line(o_all)}\n\n"
            f"Заказы (сегодня): {sum(o_day.values())}\n{line(o_day)}\n\n"
            f"Коды (всего): {sum(c_all.values())}\n{line(c_all)}"
        )

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

    def skip(self, funpay_order_id: str) -> str:
        """Pre-mark an order as CANCELLED so the plugin never auto-redeems it.

        Use this when you fulfilled an order MANUALLY (e.g. during a FunPay/
        network outage): once connectivity returns and FunPayCardinal replays the
        missed NEW_ORDER / NEW_MESSAGE events, the plugin will see the order is
        cancelled and do nothing - no double top-up. Works even if the order is
        not in the DB yet (it is created in CANCELLED state).
        """
        oid = str(funpay_order_id)
        order = self.repo.get_order_by_funpay_id(oid)
        if order is None:
            from ..database.models import OrderRecord
            order = self.repo.create_order(
                OrderRecord(funpay_order_id=oid, lot_id="", status=OrderStatus.CANCELLED.value)
            )
        self.repo.set_order_status(order.id, OrderStatus.CANCELLED, force=True)
        self.repo.add_log("admin_skip", "manual fulfilment - auto-redeem disabled",
                          order_id=order.id)
        return (f"Order #{oid} marked CANCELLED - the plugin will NOT auto-redeem it "
                f"(use this after manual fulfilment).")

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
