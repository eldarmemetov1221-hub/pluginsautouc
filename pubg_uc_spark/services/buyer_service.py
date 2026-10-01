"""BuyerService: the standalone LioGames "drip" voucher buyer (/uc_buy).

Completely separate from the Spark/FunPay delivery pipeline. The admin asks for
N vouchers of one denomination; this service buys them ONE order at a time
(LioGames order-create has no quantity field and enforces a gap between
purchases), then hands all the codes back as a single file.

Safety model (real money is spent):

* **Confirm before spend** - a batch starts in PENDING_CONFIRM and only runs
  after an explicit confirm.
* **Idempotent per unit** - every unit has a deterministic ``client_ref``
  (``UCBUY-<batch>-<seq>``). Before creating an order the worker asks
  order-status by that ref; if it already exists (e.g. after a restart mid-buy)
  it reuses it instead of buying again - no double charge.
* **Crash-safety** - the item is marked ORDERED *before* the create call, so a
  crash between the HTTP call and recording the id is reconciled (not re-bought)
  on the next pass.
* **Balance aware** - on INSUFFICIENT_BALANCE the batch pauses and pings the
  admin to top up; /uc_buy_resume continues from where it stopped.
* **Hard cap** - a batch cannot exceed ``liog_max_batch`` units.

Set ``async_mode=False`` (and inject ``sleep_fn``) to drive it inline in tests.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from ..errors import LiogCriticalError, LiogInsufficientBalance, LiogTemporaryError
from ..utils.logger import get_logger

log = get_logger("buyer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Batch statuses
PENDING_CONFIRM = "PENDING_CONFIRM"
RUNNING = "RUNNING"
PAUSED = "PAUSED"
DONE = "DONE"
STOPPED = "STOPPED"
# Item statuses
QUEUED = "QUEUED"
ORDERED = "ORDERED"
DELIVERED = "DELIVERED"
FAILED = "FAILED"

_MAX_RECONCILE_PASSES = 3


@dataclass
class _PollResult:
    code: Optional[str] = None
    order_id: Optional[str] = None
    failed: bool = False
    message: str = ""


class BuyerService:
    def __init__(
        self,
        config,
        db,
        client,
        *,
        notifier: Optional[Callable[[object, str], None]] = None,
        file_sender: Optional[Callable[[object, dict, str, str], None]] = None,
        async_mode: bool = True,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        self.cfg = config
        self.db = db
        self.client = client
        self._notify = notifier or (lambda admin_id, text: log.info("[BUY->%s] %s", admin_id, text))
        self._send_files = file_sender or (lambda admin_id, batch, txt, csv: log.info(
            "[BUY-FILES->%s] %s %s", admin_id, txt, csv))
        self.async_mode = async_mode
        self._sleep = sleep_fn or time.sleep
        self._wake = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._passes: dict = {}
        self.export_dir = os.path.join(os.path.dirname(self.db.path) or ".", "buy_exports")

    # ------------------------------------------------------------------ #
    # Public API (called from the Telegram command handlers)
    # ------------------------------------------------------------------ #
    def estimate(self, denom: str, qty: int) -> dict:
        """Resolve the variation + price/balance for a confirmation card.
        Raises on bad input or when the denomination can't be resolved."""
        qty = self._validate_qty(qty)
        variation_id = self.client.resolve_variation_id(str(denom))
        unit_price = None
        balance = None
        try:
            unit_price = self.client.unit_price(variation_id)
        except Exception:  # price is best-effort only
            log.warning("Could not read unit price for %s UC", denom)
        try:
            balance = self.client.balance()
        except Exception:
            log.warning("Could not read LioGames balance")
        total = (unit_price * qty) if unit_price is not None else None
        return {
            "denom": str(denom), "qty": qty, "variation_id": variation_id,
            "unit_price": unit_price, "total": total, "balance": balance,
        }

    def create_pending(self, denom: str, qty: int, admin_id, variation_id: str = None,
                       unit_price: float = 0.0) -> int:
        """Create a PENDING_CONFIRM batch with QUEUED items. Returns batch_id."""
        qty = self._validate_qty(qty)
        if variation_id is None:
            variation_id = self.client.resolve_variation_id(str(denom))
        now = _now()
        with self.db.lock:
            cur = self.db.conn.execute(
                """INSERT INTO buy_batches
                   (denom, variation_id, quantity, status, unit_price, admin_id, note,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (str(denom), str(variation_id), qty, PENDING_CONFIRM,
                 float(unit_price or 0), str(admin_id or ""), "", now, now),
            )
            batch_id = cur.lastrowid
            rows = [
                (batch_id, seq, f"UCBUY-{batch_id}-{seq}", QUEUED, now, now)
                for seq in range(1, qty + 1)
            ]
            self.db.conn.executemany(
                """INSERT INTO buy_items
                   (batch_id, seq, client_ref, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                rows,
            )
            self.db.conn.commit()
        log.info("[BUY] Batch #%s created: %s UC x%s (pending confirm)", batch_id, denom, qty)
        return batch_id

    def confirm(self, batch_id: int) -> str:
        b = self._batch(batch_id)
        if not b:
            return f"Партия #{batch_id} не найдена."
        if b["status"] != PENDING_CONFIRM:
            return f"Партия #{batch_id} уже в статусе {b['status']}."
        self._set_batch(batch_id, status=RUNNING)
        self._ensure_worker()
        self._wake.set()
        return (f"✅ Запущена закупка #{batch_id}: {b['denom']} UC × {b['quantity']} шт.\n"
                f"Покупаю по одной (~{int(self.cfg.liog_buy_interval)}с между заказами). "
                f"Пришлю файл, когда закончу. Прогресс: /uc_buy_status")

    def cancel_pending(self, batch_id: int) -> str:
        b = self._batch(batch_id)
        if not b:
            return f"Партия #{batch_id} не найдена."
        if b["status"] != PENDING_CONFIRM:
            return f"Партию #{batch_id} уже нельзя отменить (статус {b['status']}). /uc_buy_stop"
        self._set_batch(batch_id, status=STOPPED, note="отменено до подтверждения")
        return f"❌ Партия #{batch_id} отменена (ничего не куплено)."

    def stop(self, batch_id: int = None) -> str:
        b = self._batch(batch_id) if batch_id else self._active_batch()
        if not b:
            return "Нет активной закупки."
        if b["status"] not in (RUNNING, PAUSED, PENDING_CONFIRM):
            return f"Партия #{b['id']} уже завершена (статус {b['status']})."
        self._set_batch(b["id"], status=STOPPED, note="остановлено вручную")
        self._wake.set()
        bought = self._count(b["id"], DELIVERED)
        return (f"🛑 Закупка #{b['id']} остановлена. Уже куплено: {bought}. "
                f"Новые заказы не создаются. Готовые коды: /uc_buy_status")

    def resume(self, batch_id: int = None) -> str:
        b = self._batch(batch_id) if batch_id else self._paused_batch()
        if not b:
            return "Нет приостановленной закупки для возобновления."
        if b["status"] != PAUSED:
            return f"Партия #{b['id']} в статусе {b['status']}, возобновлять нечего."
        self._set_batch(b["id"], status=RUNNING, note="")
        self._ensure_worker()
        self._wake.set()
        left = self._count(b["id"], QUEUED) + self._count(b["id"], ORDERED)
        return f"▶️ Закупка #{b['id']} возобновлена. Осталось купить/дозабрать: {left}."

    def status_text(self, batch_id: int = None) -> str:
        b = self._batch(batch_id) if batch_id else self._latest_batch()
        if not b:
            return ("Закупок ещё не было.\n"
                    "Запусти: /uc_buy <номинал> <кол-во>  (напр. /uc_buy 60 50)")
        # Reconcile any ORDERED-but-unknown items on demand (no new purchases).
        if b["status"] in (RUNNING, PAUSED, DONE, STOPPED):
            recovered = self._reconcile(b["id"])
            b = self._batch(b["id"])
            # A finished batch that just gained codes -> resend the updated file.
            if recovered and b["status"] in (DONE, STOPPED):
                try:
                    txt, csv = self._write_files(b)
                    self._send_files(b["admin_id"], b, txt, csv)
                    self._notify(b["admin_id"],
                                 f"📦 Закупка #{b['id']}: дозабрал {recovered} код(ов), "
                                 f"отправил обновлённый файл.")
                except Exception:
                    log.exception("[BUY] resend after reconcile failed for #%s", b["id"])
        delivered = self._count(b["id"], DELIVERED)
        failed = self._count(b["id"], FAILED)
        queued = self._count(b["id"], QUEUED)
        ordered = self._count(b["id"], ORDERED)
        lines = [
            f"🧾 Закупка #{b['id']} — {b['denom']} UC",
            f"Статус: {b['status']}" + (f" ({b['note']})" if b.get("note") else ""),
            f"Всего: {b['quantity']} | ✅ куплено: {delivered} | ⏳ в очереди: {queued} | "
            f"🔄 в обработке: {ordered} | ❌ ошибок: {failed}",
        ]
        if b["status"] == PAUSED:
            lines.append("⏸ Приостановлено. Пополни баланс и жми /uc_buy_resume")
        if delivered and b["status"] in (DONE, STOPPED):
            lines.append("Файл с кодами уже отправлен (если нет — /uc_buy_status обновит).")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Worker lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """On plugin start: resume any batch left RUNNING (continue the drip)."""
        if not self.async_mode:
            return
        self._ensure_worker()
        if self._active_batch():
            self._wake.set()

    def stop_worker(self) -> None:
        self._running = False
        self._wake.set()

    def _ensure_worker(self) -> None:
        if not self.async_mode or self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="pubg-uc-liog-buyer", daemon=True)
        self._thread.start()
        log.info("[BUY] Worker started")

    def _loop(self) -> None:
        while self._running:
            batch = self._active_batch()
            if not batch:
                self._wake.wait(timeout=30)
                self._wake.clear()
                continue
            self._wake.clear()
            self.run_once(batch["id"])

    # ------------------------------------------------------------------ #
    # One processing step (also the inline entrypoint for tests)
    # ------------------------------------------------------------------ #
    def run_once(self, batch_id: int) -> bool:
        """Process the next pending item of a RUNNING batch. Returns True if a
        NEW purchase was issued (caller paces by the buy interval)."""
        batch = self._batch(batch_id)
        if not batch or batch["status"] != RUNNING:
            return False
        item = self._next_pending_item(batch_id)
        if item is None:
            self._finalize(batch)
            return False
        try:
            issued_new = self._process_item(batch, item)
        except LiogInsufficientBalance as exc:
            self._pause_batch(batch, str(exc))
            return False
        except LiogCriticalError as exc:
            self._set_item(item["id"], status=FAILED, error_message=str(exc))
            log.error("[BUY] Item #%s critical: %s", item["id"], exc)
            self._notify(batch["admin_id"],
                         f"⚠️ Закупка #{batch_id}: ошибка на позиции {item['seq']}: {exc}")
            return False
        except LiogTemporaryError as exc:
            log.warning("[BUY] Item #%s temporary: %s", item["id"], exc)
            self._sleep(min(self.cfg.liog_buy_interval, 15))
            return False

        self._maybe_progress(batch_id)
        if issued_new and self.async_mode:
            self._interruptible_sleep(self.cfg.liog_buy_interval)
        return issued_new

    def _process_item(self, batch: dict, item: dict) -> bool:
        """Buy (or reconcile) one unit. Returns True if a new order was created."""
        if item["status"] == DELIVERED:
            return False
        cref = item["client_ref"]

        # Idempotency: does this order already exist on LioGames?
        existing = self.client.order_status(client_ref=cref)
        issued_new = False
        if existing is None:
            # Balance guard (best-effort) before spending.
            if batch.get("unit_price"):
                bal = self.client.balance()
                if bal is not None and bal + 1e-9 < float(batch["unit_price"]):
                    raise LiogInsufficientBalance(
                        f"баланс {bal} < цена {batch['unit_price']}")
            # Mark ORDERED *before* the call so a crash is reconciled, not re-bought.
            self._set_item(item["id"], status=ORDERED)
            created = self.client.order_create(batch["variation_id"], cref)
            self._set_item(item["id"], liog_order_id=created.get("order_id") or "")
            item["liog_order_id"] = created.get("order_id") or ""
            issued_new = True

        final = self._poll_for_code(cref, item.get("liog_order_id"))
        if final.code:
            self._set_item(item["id"], status=DELIVERED, code=final.code,
                           liog_order_id=final.order_id or item.get("liog_order_id") or "")
            self._passes.pop(item["id"], None)
        elif final.failed:
            self._set_item(item["id"], status=FAILED, error_message=final.message or "failed")
            self._passes.pop(item["id"], None)
        else:
            # Not terminal yet: count the pass, give up tracking after a few so the
            # batch can finalize (the order_id is recorded for manual lookup).
            n = self._passes.get(item["id"], 0) + 1
            self._passes[item["id"]] = n
            if n >= _MAX_RECONCILE_PASSES:
                self._set_item(
                    item["id"], status=FAILED,
                    error_message=f"still processing after {n} passes; check LioGames "
                                  f"order {item.get('liog_order_id') or cref}")
                self._passes.pop(item["id"], None)
        return issued_new

    def _poll_for_code(self, client_ref: str, order_id: str = None) -> _PollResult:
        attempts = max(1, int(self.cfg.liog_poll_attempts))
        for i in range(attempts):
            body = self.client.order_status(client_ref=client_ref, order_id=order_id)
            if body is not None:
                code = self.client.extract_code(body)
                if code:
                    oid = self._order_id_from(body) or order_id
                    return _PollResult(code=code, order_id=oid)
                if self.client.status_is_failed(body):
                    return _PollResult(failed=True, message=self._msg_from(body))
            if i < attempts - 1:
                self._sleep(self.cfg.liog_poll_interval)
        return _PollResult()  # not terminal within budget

    # ------------------------------------------------------------------ #
    # Finalisation & reconciliation
    # ------------------------------------------------------------------ #
    def _finalize(self, batch: dict) -> None:
        batch_id = batch["id"]
        delivered = self._count(batch_id, DELIVERED)
        failed = self._count(batch_id, FAILED)
        txt, csv = self._write_files(batch)
        note = f"куплено {delivered}/{batch['quantity']}" + (f", ошибок {failed}" if failed else "")
        self._set_batch(batch_id, status=DONE, note=note)
        try:
            self._send_files(batch["admin_id"], self._batch(batch_id), txt, csv)
        except Exception:
            log.exception("[BUY] Failed to send result files for batch #%s", batch_id)
        summary = (f"✅ Закупка #{batch_id} завершена: {batch['denom']} UC\n"
                   f"Куплено: {delivered}/{batch['quantity']}"
                   + (f", ошибок: {failed}" if failed else "")
                   + "\nФайл с кодами отправлен.")
        self._notify(batch["admin_id"], summary)
        log.info("[BUY] Batch #%s finalized: %s", batch_id, note)

    def _reconcile(self, batch_id: int) -> int:
        """Re-check non-delivered items via order-status (no new purchases).

        Covers ORDERED items AND items we earlier gave up on (FAILED with a
        recorded liog_order_id) - so a code from a slow order that completes
        after our poll window is still recovered, never lost. Returns the number
        of items newly recovered to DELIVERED."""
        recovered = 0
        for item in self._items(batch_id):
            if item["status"] == DELIVERED:
                continue
            # Only items that actually reached LioGames can be looked up.
            if not item.get("liog_order_id") and item["status"] != ORDERED:
                continue
            try:
                body = self.client.order_status(
                    client_ref=item["client_ref"], order_id=item.get("liog_order_id"))
            except Exception:
                continue
            if body is None:
                continue
            code = self.client.extract_code(body)
            if code:
                self._set_item(item["id"], status=DELIVERED, code=code,
                               liog_order_id=self._order_id_from(body) or item.get("liog_order_id") or "",
                               error_message="")
                self._passes.pop(item["id"], None)
                recovered += 1
            elif self.client.status_is_failed(body) and item["status"] != FAILED:
                self._set_item(item["id"], status=FAILED, error_message=self._msg_from(body))
        return recovered

    def _write_files(self, batch: dict) -> tuple:
        os.makedirs(self.export_dir, exist_ok=True)
        base = f"liog_{batch['denom']}uc_batch{batch['id']}"
        txt_path = os.path.join(self.export_dir, base + ".txt")
        csv_path = os.path.join(self.export_dir, base + ".csv")
        items = self._items(batch["id"])
        with open(txt_path, "w", encoding="utf-8") as fh:
            for it in items:
                if it["status"] == DELIVERED and it["code"]:
                    fh.write(it["code"] + "\n")
        import csv as _csv
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(["seq", "denom", "status", "code", "liog_order_id", "client_ref", "error"])
            for it in items:
                w.writerow([it["seq"], batch["denom"], it["status"], it.get("code") or "",
                            it.get("liog_order_id") or "", it["client_ref"],
                            it.get("error_message") or ""])
        return txt_path, csv_path

    # ------------------------------------------------------------------ #
    # Small helpers
    # ------------------------------------------------------------------ #
    def _validate_qty(self, qty) -> int:
        try:
            q = int(qty)
        except (TypeError, ValueError):
            raise LiogCriticalError("Количество должно быть числом.")
        if q < 1:
            raise LiogCriticalError("Количество должно быть ≥ 1.")
        if q > self.cfg.liog_max_batch:
            raise LiogCriticalError(
                f"Слишком много за раз ({q}). Лимит партии: {self.cfg.liog_max_batch}.")
        return q

    def _pause_batch(self, batch: dict, reason: str) -> None:
        self._set_batch(batch["id"], status=PAUSED, note=f"пауза: {reason}")
        bought = self._count(batch["id"], DELIVERED)
        self._notify(
            batch["admin_id"],
            f"⏸ Закупка #{batch['id']} ПРИОСТАНОВЛЕНА — {reason}.\n"
            f"Куплено пока: {bought}/{batch['quantity']}.\n"
            f"Пополни баланс LioGames и продолжи: /uc_buy_resume")
        log.warning("[BUY] Batch #%s paused: %s", batch["id"], reason)

    def _maybe_progress(self, batch_id: int) -> None:
        b = self._batch(batch_id)
        if not b:
            return
        delivered = self._count(batch_id, DELIVERED)
        total = b["quantity"]
        # Ping every 10 and on the last one.
        if delivered and (delivered % 10 == 0):
            self._notify(b["admin_id"], f"📦 Закупка #{batch_id}: {delivered}/{total} куплено…")

    def _interruptible_sleep(self, seconds: float) -> None:
        # Wake early if stopped/paused.
        end = time.monotonic() + max(0.0, seconds)
        while self._running and time.monotonic() < end:
            if self._wake.wait(timeout=min(2.0, end - time.monotonic())):
                self._wake.clear()
                return

    @staticmethod
    def _order_id_from(body: dict) -> Optional[str]:
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        for k in ("order_id", "id", "liog_order_id"):
            v = data.get(k)
            if v:
                return str(v)
        return None

    @staticmethod
    def _msg_from(body: dict) -> str:
        return str(body.get("message") or body.get("code") or "failed")

    # ---- DB access ---- #
    def _batch(self, batch_id) -> Optional[dict]:
        if not batch_id:
            return None
        row = self.db.query_one("SELECT * FROM buy_batches WHERE id = ?", (batch_id,))
        return dict(row) if row else None

    def _active_batch(self) -> Optional[dict]:
        row = self.db.query_one(
            "SELECT * FROM buy_batches WHERE status = ? ORDER BY id ASC LIMIT 1", (RUNNING,))
        return dict(row) if row else None

    def _paused_batch(self) -> Optional[dict]:
        row = self.db.query_one(
            "SELECT * FROM buy_batches WHERE status = ? ORDER BY id DESC LIMIT 1", (PAUSED,))
        return dict(row) if row else None

    def _latest_batch(self) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM buy_batches ORDER BY id DESC LIMIT 1")
        return dict(row) if row else None

    def _set_batch(self, batch_id, **fields) -> None:
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE buy_batches SET {cols} WHERE id = ?",
                        tuple(fields.values()) + (batch_id,))

    def _next_pending_item(self, batch_id) -> Optional[dict]:
        # QUEUED first (new buys), then ORDERED that haven't exhausted reconcile.
        row = self.db.query_one(
            "SELECT * FROM buy_items WHERE batch_id = ? AND status = ? ORDER BY seq ASC LIMIT 1",
            (batch_id, QUEUED))
        if row:
            return dict(row)
        for r in self.db.query_all(
                "SELECT * FROM buy_items WHERE batch_id = ? AND status = ? ORDER BY seq ASC",
                (batch_id, ORDERED)):
            if self._passes.get(r["id"], 0) < _MAX_RECONCILE_PASSES:
                return dict(r)
        return None

    def _items(self, batch_id, status: str = None) -> list:
        if status:
            rows = self.db.query_all(
                "SELECT * FROM buy_items WHERE batch_id = ? AND status = ? ORDER BY seq ASC",
                (batch_id, status))
        else:
            rows = self.db.query_all(
                "SELECT * FROM buy_items WHERE batch_id = ? ORDER BY seq ASC", (batch_id,))
        return [dict(r) for r in rows]

    def _set_item(self, item_id, **fields) -> None:
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE buy_items SET {cols} WHERE id = ?",
                        tuple(fields.values()) + (item_id,))

    def _count(self, batch_id, status: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) c FROM buy_items WHERE batch_id = ? AND status = ?",
            (batch_id, status))
        return int(row["c"]) if row else 0
