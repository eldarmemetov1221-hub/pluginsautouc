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
        """One evaluation pass (variant C - dynamic stock). For each lot the live
        наличие is capped to ``min(desired, floor(balance/cost))``; 0 -> hide.
        Only edits FunPay in enforce mode. Applies to the FazerCards provider."""
        result = {"provider": self.cfg.provider(), "balance": None, "lots": [],
                  "hidden": 0, "changed": 0, "skipped": ""}
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
            entry = self._eval_lot(lot, balance, enforce)
            if entry.get("_hidden"):
                result["hidden"] += 1
            if entry.get("_changed"):
                result["changed"] += 1
            result["lots"].append(entry)
        return result

    def _eval_lot(self, lot, balance: float, enforce: bool) -> dict:
        lot_id = lot.lot_id
        cost = self.lot_cost(lot)
        row = self._row(lot_id)
        auto_hidden = bool(row and row["auto_hidden"])
        entry = {"lot_id": lot_id, "product": lot.product, "cost": cost,
                 "desired": None, "afford": None, "target": None, "cur": None,
                 "auto_hidden": auto_hidden, "action": ""}
        if not cost or cost <= 0:
            entry["action"] = "цена неизвестна"
            return entry

        amount, active = funpay_orders.read_lot(self.cardinal, lot_id)
        entry["cur"] = amount
        managed = int(row["managed_amount"]) if row and row["managed_amount"] is not None else -1
        desired_stored = int(row["desired_amount"]) if row and row["desired_amount"] else 0

        # Seller's intended stock ceiling.
        if int(getattr(lot, "stock", 0) or 0) > 0:
            desired = int(lot.stock)                      # pinned in config
        elif amount is not None and amount != managed and active is not False and amount > 0:
            desired = amount                              # seller changed наличие -> follow
        elif desired_stored > 0:
            desired = desired_stored
        elif amount is not None and amount > 0:
            desired = amount                              # first capture
        else:
            desired = 1
        entry["desired"] = desired

        affordable = int(balance // cost)                 # whole units the wallet covers
        target = max(0, min(desired, affordable))
        entry["afford"] = affordable
        entry["target"] = target

        if target <= 0:
            # Can't cover even one unit -> hide (deactivate).
            entry["action"] = "hide"
            if enforce:
                if active is not False:
                    ok = funpay_orders.set_lot_active(self.cardinal, lot_id, False)
                    if ok:
                        self._notify(f"⚠️ Лот «{lot.product}» СКРЫТ — баланс ${balance:g} "
                                     f"не покрывает $ {cost:g}/шт.")
                        entry["_hidden"] = True
                    else:
                        entry["action"] = "hide-failed"
                self._save(lot_id, auto_hidden=1, cost=cost, desired=desired,
                           managed=0, action="hidden")
            else:
                self._notify(f"👀 На «{lot.product}» не хватает даже на 1 шт "
                             f"(баланс ${balance:g} < ${cost:g}) — monitor, лот не тронут.")
                self._save(lot_id, auto_hidden=0, cost=cost, desired=desired,
                           managed=managed, action="monitor")
            return entry

        # Affordable: desired stock capped to what the wallet covers.
        entry["action"] = "ok" if target >= desired else "cap"
        if enforce:
            need = (amount != target) or (active is False)
            if need:
                ok = funpay_orders.set_lot_amount(self.cardinal, lot_id, target, active=True)
                if ok:
                    if auto_hidden or active is False:
                        self._notify(f"✅ Лот «{lot.product}» возвращён — наличие {target}.")
                    elif target < desired:
                        self._notify(f"✂️ Лот «{lot.product}»: наличие урезано до {target} "
                                     f"(хватает только на {affordable}).")
                    entry["_changed"] = True
                    self._save(lot_id, auto_hidden=0, cost=cost, desired=desired,
                               managed=target, action=("capped" if target < desired else "shown"))
                else:
                    entry["action"] = "set-failed"
            else:
                self._save(lot_id, auto_hidden=0, cost=cost, desired=desired,
                           managed=target, action=entry["action"])
        else:
            self._save(lot_id, auto_hidden=0, cost=cost, desired=desired,
                       managed=managed, action="monitor")
            if target < desired:
                self._notify(f"👀 «{lot.product}»: хватает на {target} из {desired} "
                             f"(monitor — наличие не меняю).")
        return entry

    # ------------------------------------------------------------------ #
    def status_text(self) -> str:
        enforce = self.cfg.guard_enforce()
        mode = "enforce (меняет наличие/скрывает)" if enforce else "monitor (только уведомления)"
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
            if e["target"] is not None and e["target"] <= 0:
                mark = "🔴 скрыт"
            elif e["target"] is not None and e["desired"] is not None and e["target"] < e["desired"]:
                mark = f"🟠 {e['target']}/{e['desired']} шт"
            else:
                mark = f"🟢 {e['target']} шт"
            lines.append(f"  • {e['product']}: ${e['cost']:g}/шт — {mark}")
        return "\n".join(lines)

    # ---- DB ---- #
    def _row(self, lot_id):
        return self.db.query_one("SELECT * FROM lot_guard WHERE lot_id = ?", (str(lot_id),))

    def _save(self, lot_id, *, auto_hidden: int, cost: float, desired: int,
              managed: int, action: str) -> None:
        self.db.execute(
            """INSERT INTO lot_guard
               (lot_id, auto_hidden, last_cost_usd, desired_amount, managed_amount,
                last_action, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(lot_id) DO UPDATE SET
                 auto_hidden=excluded.auto_hidden, last_cost_usd=excluded.last_cost_usd,
                 desired_amount=excluded.desired_amount, managed_amount=excluded.managed_amount,
                 last_action=excluded.last_action, updated_at=excluded.updated_at""",
            (str(lot_id), int(auto_hidden), float(cost or 0), int(desired),
             int(managed), action or "", _now()))
