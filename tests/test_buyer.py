"""Tests for the standalone LioGames drip buyer (/uc_buy).

Uses a fake LioGames client (no network) and the real BuyerService + DB in
inline mode, so the queue/idempotency/pause/file logic is exercised directly.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config  # noqa: E402
from pubg_uc_spark.database.db import Database  # noqa: E402
from pubg_uc_spark.services import buyer_service as BS  # noqa: E402
from pubg_uc_spark.services.buyer_service import BuyerService  # noqa: E402


class FakeClient:
    """Deterministic LioGames stand-in. order_status returns a code once the ref
    has been 'created' (or pre-seeded in ``already``)."""

    def __init__(self, *, balance=9999.0, unit_price=0.0, already=None, fail_refs=None):
        self._balance = balance
        self._unit_price = unit_price
        self.created = set(already or [])
        self.fail_refs = set(fail_refs or [])
        self.create_calls = []

    def resolve_variation_id(self, denom):
        return f"v{denom}"

    def unit_price(self, variation_id):
        return self._unit_price

    def balance(self):
        return self._balance

    def order_create(self, variation_id, client_ref, product_id=None):
        self.create_calls.append(client_ref)
        self.created.add(client_ref)
        return {"order_id": f"OID-{client_ref}", "status": "processing", "raw": {}}

    def order_status(self, client_ref=None, order_id=None):
        ref = client_ref or ""
        if ref in self.fail_refs:
            return {"ok": True, "data": {"status": "failed", "message": "declined"}}
        if ref in self.created:
            return {"ok": True, "data": {"status": "completed", "voucher": f"CODE-{ref}"}}
        return None

    def extract_code(self, body):
        data = body.get("data", body)
        v = data.get("voucher")
        return v or None

    @staticmethod
    def status_is_failed(body):
        data = body.get("data", body)
        return str(data.get("status") or "").lower() == "failed"

    @staticmethod
    def status_is_terminal_ok(body):
        data = body.get("data", body)
        return str(data.get("status") or "").lower() in ("completed", "done")


def _mk(tmp_path, client, **notes):
    c = Config()
    c.database_path = str(tmp_path / "buy.db")
    c.liog_buy_interval = 0.0
    c.liog_poll_interval = 0.0
    c.liog_poll_attempts = 2
    db = Database(c.database_path)
    notified, files = [], []
    svc = BuyerService(
        c, db, client,
        notifier=lambda a, t: notified.append((a, t)),
        file_sender=lambda a, b, txt, csv: files.append((a, b, txt, csv)),
        async_mode=False, sleep_fn=lambda _s: None,
    )
    return c, db, svc, notified, files


def _drain(svc, batch_id, limit=200):
    """Run the inline worker until the batch leaves RUNNING."""
    for _ in range(limit):
        b = svc._batch(batch_id)
        if not b or b["status"] != BS.RUNNING:
            return
        svc.run_once(batch_id)


def test_full_batch_delivers_codes_and_file(tmp_path):
    client = FakeClient()
    c, db, svc, notified, files = _mk(tmp_path, client)
    bid = svc.create_pending("60", 3, admin_id="A")
    svc.confirm(bid)
    _drain(svc, bid)

    b = svc._batch(bid)
    assert b["status"] == BS.DONE
    assert svc._count(bid, BS.DELIVERED) == 3
    assert len(client.create_calls) == 3           # exactly 3 purchases, no more
    # one result-file delivery happened
    assert len(files) == 1
    txt_path = files[0][2]
    with open(txt_path) as fh:
        codes = [ln.strip() for ln in fh if ln.strip()]
    assert len(codes) == 3 and all(x.startswith("CODE-") for x in codes)
    db.close()


def test_idempotent_never_double_charges(tmp_path):
    # Simulate a restart mid-buy: the order for seq 1 already exists on LioGames.
    client = FakeClient(already={"UCBUY-1-1"})
    c, db, svc, notified, files = _mk(tmp_path, client)
    bid = svc.create_pending("60", 2, admin_id="A")
    assert bid == 1
    svc.confirm(bid)
    _drain(svc, bid)

    assert svc._count(bid, BS.DELIVERED) == 2
    # seq 1 was already bought -> only seq 2 is actually purchased
    assert "UCBUY-1-1" not in client.create_calls
    assert client.create_calls == ["UCBUY-1-2"]
    db.close()


def test_insufficient_balance_pauses_then_resumes(tmp_path):
    client = FakeClient(balance=0.5, unit_price=0.88)
    c, db, svc, notified, files = _mk(tmp_path, client)
    bid = svc.create_pending("60", 2, admin_id="A", unit_price=0.88)
    svc.confirm(bid)
    svc.run_once(bid)  # first item: balance too low -> pause

    assert svc._batch(bid)["status"] == BS.PAUSED
    assert any("ПРИОСТАНОВЛЕНА" in t for _a, t in notified)
    assert client.create_calls == []               # nothing bought
    # top up and resume
    client._balance = 100.0
    svc.resume(bid)
    _drain(svc, bid)
    assert svc._batch(bid)["status"] == BS.DONE
    assert svc._count(bid, BS.DELIVERED) == 2
    db.close()


def test_failed_item_is_recorded_not_fatal(tmp_path):
    client = FakeClient(fail_refs={"UCBUY-1-2"})
    c, db, svc, notified, files = _mk(tmp_path, client)
    bid = svc.create_pending("60", 3, admin_id="A")
    svc.confirm(bid)
    _drain(svc, bid)

    b = svc._batch(bid)
    assert b["status"] == BS.DONE
    assert svc._count(bid, BS.DELIVERED) == 2
    assert svc._count(bid, BS.FAILED) == 1
    db.close()


def test_qty_cap_rejected(tmp_path):
    client = FakeClient()
    c, db, svc, notified, files = _mk(tmp_path, client)
    c.liog_max_batch = 10
    import pytest
    with pytest.raises(Exception):
        svc.create_pending("60", 11, admin_id="A")
    db.close()


def test_confirm_required_before_buying(tmp_path):
    client = FakeClient()
    c, db, svc, notified, files = _mk(tmp_path, client)
    bid = svc.create_pending("60", 2, admin_id="A")
    # Without confirm the batch is PENDING_CONFIRM and run_once does nothing.
    assert svc.run_once(bid) is False
    assert client.create_calls == []
    assert svc.cancel_pending(bid).startswith("❌")
    assert svc._batch(bid)["status"] == BS.STOPPED
    db.close()
