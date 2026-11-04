"""BalanceGuard: hide lots the provider wallet can't cover, restore them later.

For the FazerCards provider, each lot's cost in USD is the sum of its base
packs' prices. When the wallet balance drops below a lot's cost, the lot is
hidden on FunPay (deactivated); when the balance recovers past cost + margin,
the lot is restored. It only ever re-shows lots IT hid (``auto_hidden``), so a
lot the seller disabled by hand is never touched.

Two modes:
  * ``monitor``  - compute + notify only, never edits FunPay (safe first run);
  * ``enforce``  - actually hide/show lots.

A balance that can't be read leaves every lot untouched (never hide on a
transient error). Runs on its own daemon thread; also pokeable after a delivery.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from ..funpay import orders as funpay_orders
from ..utils.logger import get_logger

log = get_logger("guard")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BalanceGuard:
    def __init__(self, config, db, fazer_client, cardinal, *,
                 notifier: Optional[Callable[[str], None]] = None,
                 async_mode: bool = True, sleep_fn: Optional[Callable[[float], None]] = None):
        self.cfg = config
        self.db = db
        self.fazer = fazer_client
        self.cardinal = cardinal
        self._notify = notifier or (lambda text: log.info("[guard] %s", text))
        self.async_mode = async_mode
        self._sleep = sleep_fn or time.sleep
        self._wake = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if not self.async_mode or self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="pubg-uc-balance-guard", daemon=True)
        self._thread.start()
        log.info("[guard] worker started")

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def poke(self) -> None:
        """Request a check soon (e.g. right after a delivery drops the balance)."""
        self._wake.set()

    def _loop(self) -> None:
        while self._running:
            try:
                if self.cfg.guard_on():
                    self.check_once()
            except Exception:
                log.exception("[guard] check failed")
            self._wake.wait(timeout=max(30.0, float(self.cfg.balance_guard_interval)))
            self._wake.clear()

    # ------------------------------------------------------------------ #
    def lot_cost(self, lot) -> Optional[float]:
        """USD cost of ONE unit of a lot via FazerCards (sum of its base packs)."""
        try:
            return self.fazer.lot_cost_usd(lot.base_picks())
        except Exception:
            log.debug("[guard] cost calc failed for %s", getattr(lot, "lot_id", "?"), exc_info=True)
            return None

    def check_once(self) -> dict:
        """One evaluation pass. Returns a summary dict (also used by the status
        screen). Only edits FunPay in enforce mode. Guard applies to FazerCards."""
        result = {"provider": self.cfg.provider(), "balance": None, "lots": [],
                  "hidden": 0, "shown": 0, "skipped": ""}
        if self.cfg.provider() != "fazercards":
            result["skipped"] = "провайдер не FazerCards"
            return result
        try:
            balance = self.fazer.balance()
        except Exception as exc:
            log.warning("[guard] balance read failed: %s", exc)
            result["skipped"] = "баланс не прочитан"
            return result
        if balance is None:
            result["skipped"] = "баланс не прочитан"
            return result
        result["balance"] = balance

        enforce = self.cfg.guard_enforce()
        for lot in self.cfg.lots.values():
            cost = self.lot_cost(lot)
            row = self._row(lot.lot_id)
            was_hidden = bool(row and row["auto_hidden"])
            entry = {"lot_id": lot.lot_id, "product": lot.product, "cost": cost,
                     "afford": None, "auto_hidden": was_hidden, "action": ""}
            if cost is None:
                entry["action"] = "цена неизвестна — пропуск"
                result["lots"].append(entry)
                continue
            affordable = balance >= cost
            show_ok = balance >= self.cfg.guard_show_threshold(cost)
            entry["afford"] = affordable

            # Decide the desired action with hysteresis + manual-hide respect.
            if not affordable:
                # Needs hiding (if currently shown / not already auto-hidden).
                if not was_hidden:
                    entry["action"] = "hide"
            else:
                # Affordable again: restore only lots WE hid, and only past margin.
                if was_hidden and show_ok:
                    entry["action"] = "show"

            if entry["action"] == "hide":
                if enforce:
                    ok = funpay_orders.set_lot_active(self.cardinal, lot.lot_id, False)
                    if ok:
                        self._set_row(lot.lot_id, auto_hidden=1, cost=cost, action="hidden")
                        result["hidden"] += 1
                        self._notify(f"⚠️ Лот «{lot.product}» СКРЫТ — баланс ${balance:g} "
                                     f"< себестоимость ${cost:g}.")
                    else:
                        entry["action"] = "hide-failed"
                else:  # monitor
                    self._set_row(lot.lot_id, auto_hidden=0, cost=cost, action="monitor")
                    self._notify(f"👀 На лот «{lot.product}» не хватает: баланс ${balance:g} "
                                 f"< ${cost:g} (режим monitor — лот не скрыт).")
            elif entry["action"] == "show":
                if enforce:
                    ok = funpay_orders.set_lot_active(self.cardinal, lot.lot_id, True)
                    if ok:
                        self._set_row(lot.lot_id, auto_hidden=0, cost=cost, action="shown")
                        result["shown"] += 1
                        self._notify(f"✅ Лот «{lot.product}» возвращён — баланс ${balance:g} "
                                     f"покрывает ${cost:g}.")
                    else:
                        entry["action"] = "show-failed"
            else:
                # no change; keep the recorded cost fresh
                self._set_row(lot.lot_id, auto_hidden=1 if was_hidden else 0,
                              cost=cost, action=(row["last_action"] if row else ""))
            result["lots"].append(entry)
        return result

    # ------------------------------------------------------------------ #
    def status_text(self) -> str:
        enforce = self.cfg.guard_enforce()
        mode = "enforce (скрывает лоты)" if enforce else "monitor (только уведомления)"
        on = "ВКЛ" if self.cfg.guard_on() else "ВЫКЛ"
        r = self.check_once()
        lines = [f"🛡 Страж баланса: {on} · режим: {mode}",
                 f"Провайдер: {r['provider']}"]
        if r["skipped"]:
            lines.append(f"(пропуск: {r['skipped']})")
            return "\n".join(lines)
        lines.append(f"Баланс FazerCards: ${r['balance']:g}\n")
        for e in r["lots"]:
            if e["cost"] is None:
                lines.append(f"  • {e['product']}: цена неизвестна")
                continue
            if e["auto_hidden"] or e["action"] in ("hide", "hide-failed"):
                mark = "🔴 скрыт"
            elif e["afford"]:
                mark = "🟢 ок"
            else:
                mark = "🟠 не хватает"
            lines.append(f"  • {e['product']}: ${e['cost']:g} — {mark}")
        return "\n".join(lines)

    # ---- DB ---- #
    def _row(self, lot_id):
        return self.db.query_one("SELECT * FROM lot_guard WHERE lot_id = ?", (str(lot_id),))

    def _set_row(self, lot_id, *, auto_hidden: int, cost: float, action: str) -> None:
        self.db.execute(
            """INSERT INTO lot_guard (lot_id, auto_hidden, last_cost_usd, last_action, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(lot_id) DO UPDATE SET
                 auto_hidden=excluded.auto_hidden, last_cost_usd=excluded.last_cost_usd,
                 last_action=excluded.last_action, updated_at=excluded.updated_at""",
            (str(lot_id), int(auto_hidden), float(cost or 0), action or "", _now()))
