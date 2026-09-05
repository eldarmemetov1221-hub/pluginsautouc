"""Startup reconciliation / backfill tests.

Simulate Cardinal coming back after an outage: FunPay has open sales the plugin
never saw (no NEW_ORDER was replayed), some with the buyer's UID already in the
chat history. Backfill must register the missed orders and redeem the UIDs, once
and only once, without touching closed or untracked sales.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config, LotConfig  # noqa: E402
from pubg_uc_spark.database.models import CodeStatus, OrderStatus  # noqa: E402
from pubg_uc_spark.plugin import Plugin  # noqa: E402

from conftest import FakeMessage, FakeOrderShortcut  # noqa: E402


class FakeSale(FakeOrderShortcut):
    """An OrderShortcut plus a FunPay sale ``status`` (paid/closed/refunded)."""

    def __init__(self, *args, status="paid", **kwargs):
        super().__init__(*args, **kwargs)
        self.status = status


class BackfillAccount:
    """FunPay account exposing get_sales() + get_chat_history() for backfill."""

    def __init__(self, sales, histories=None, next_id=None):
        self.id = "seller-1"
        self._sales = sales
        self._histories = histories or {}   # chat_id -> [FakeMessage]
        self._next_id = next_id
        self.sales_calls = 0
        self.history_calls = []

    def get_sales(self, start_from=None, include_closed=True, include_refunded=True):
        self.sales_calls += 1
        # Single page: return (next_order_id, sales). next=None -> no more pages.
        return (self._next_id, list(self._sales))

    def get_chat_history(self, chat_id, *a, **kw):
        self.history_calls.append(str(chat_id))
        return list(self._histories.get(str(chat_id), []))


class BackfillCardinal:
    def __init__(self, account):
        self.account = account
        self.telegram = None
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((str(chat_id), text))

    def texts_to(self, chat_id):
        return [t for c, t in self.sent if c == str(chat_id)]


def _cfg(tmp_path):
    c = Config()
    c.spark_mock = True
    c.database_path = str(tmp_path / "bf.db")
    c.max_retries = 1
    c.retry_delay = 0.0
    c.retry_backoff = 1.0
    c.code_pattern = r"[0-9]{9,11}"
    # description "PUBG Mobile 60 UC ..." matches this lot
    c.lots = {"37330959": LotConfig("37330959", "PUBG Mobile 60 UC", "60")}
    c.admin_ids = [111]
    c.backfill_on_start = True
    c.backfill_read_history = True
    return c


def _paid_sale(order_id, buyer_id="buyer-1", chat_id="chat-1", tracked=True, status="paid"):
    desc = "PUBG Mobile 60 UC (id игрока)" if tracked else "Some other product 999"
    return FakeSale(order_id, desc, buyer_id, "john", 1, chat_id, status=status)


def _plugin(cfg, cardinal):
    p = Plugin(cardinal, cfg, async_mode=False)
    p.start()
    return p


# --------------------------------------------------------------------------- #
def test_backfill_registers_missed_order_and_redeems_uid(tmp_path):
    # Buyer already sent a (valid, mock-success) UID during the outage.
    uid = "123456789"  # leading '1' => mock success
    acct = BackfillAccount(
        sales=[_paid_sale("HZAW24QP")],
        histories={"chat-1": [FakeMessage("m1", "buyer-1", "chat-1", f"мой id {uid}")]},
    )
    p = _plugin(_cfg(tmp_path), BackfillCardinal(acct))
    stats = p.run_backfill()

    assert stats["registered"] == 1
    assert stats["uid_recovered"] == 1
    order = p.repo.get_order_by_funpay_id("HZAW24QP")
    assert order is not None
    assert order.status == OrderStatus.VALID.value
    codes = p.repo.get_codes_for_order(order.id)
    assert len(codes) == 1 and codes[0].status == CodeStatus.VALID.value
    p.stop()


def test_backfill_registers_but_waits_when_no_uid_in_history(tmp_path):
    acct = BackfillAccount(
        sales=[_paid_sale("ORD2")],
        histories={"chat-1": [FakeMessage("m1", "buyer-1", "chat-1", "когда пополните?")]},
    )
    p = _plugin(_cfg(tmp_path), BackfillCardinal(acct))
    stats = p.run_backfill()

    assert stats["registered"] == 1
    assert stats["uid_recovered"] == 0
    order = p.repo.get_order_by_funpay_id("ORD2")
    assert order.status == OrderStatus.WAITING_FOR_CODE.value
    assert p.repo.get_codes_for_order(order.id) == []
    p.stop()


def test_backfill_then_live_message_delivers(tmp_path):
    # Order registered by backfill (no UID yet); buyer sends UID afterwards.
    acct = BackfillAccount(sales=[_paid_sale("ORD3")], histories={"chat-1": []})
    p = _plugin(_cfg(tmp_path), BackfillCardinal(acct))
    p.run_backfill()
    p.on_new_message(FakeMessage("m9", "buyer-1", "chat-1", "id 123456789"))

    order = p.repo.get_order_by_funpay_id("ORD3")
    assert order.status == OrderStatus.VALID.value
    p.stop()


def test_backfill_skips_closed_and_untracked(tmp_path):
    acct = BackfillAccount(sales=[
        _paid_sale("CLOSED1", status="closed"),
        _paid_sale("REFUND1", status="refunded"),
        _paid_sale("OTHER1", tracked=False),
    ])
    p = _plugin(_cfg(tmp_path), BackfillCardinal(acct))
    stats = p.run_backfill()

    assert stats["registered"] == 0
    assert stats["tracked"] == 0
    assert p.repo.get_order_by_funpay_id("CLOSED1") is None
    assert p.repo.get_order_by_funpay_id("OTHER1") is None
    p.stop()


def test_backfill_is_idempotent_no_double_redeem(tmp_path):
    uid = "123456789"
    acct = BackfillAccount(
        sales=[_paid_sale("ORD4")],
        histories={"chat-1": [FakeMessage("m1", "buyer-1", "chat-1", uid)]},
    )
    p = _plugin(_cfg(tmp_path), BackfillCardinal(acct))
    p.run_backfill()
    s2 = p.run_backfill()   # second run (e.g. another restart)

    assert s2["registered"] == 0          # already known
    assert s2["already_known"] == 1
    order = p.repo.get_order_by_funpay_id("ORD4")
    codes = p.repo.get_codes_for_order(order.id)
    assert len(codes) == 1                 # exactly one redemption, ever
    assert order.status == OrderStatus.VALID.value
    p.stop()


def test_backfill_ignores_our_own_success_echo(tmp_path):
    # Our own delivered-message echo contains a UID; it must NOT be picked up as
    # the buyer's input (would deliver to the wrong/known account).
    seller_echo = FakeMessage("s1", "seller-1", "chat-1",
                              "✅ ID игрока: 999888777", by_bot=True)
    acct = BackfillAccount(
        sales=[_paid_sale("ORD5")],
        histories={"chat-1": [seller_echo]},
    )
    p = _plugin(_cfg(tmp_path), BackfillCardinal(acct))
    stats = p.run_backfill()

    assert stats["uid_recovered"] == 0
    order = p.repo.get_order_by_funpay_id("ORD5")
    assert order.status == OrderStatus.WAITING_FOR_CODE.value
    p.stop()


def test_backfill_takes_newest_uid_from_history(tmp_path):
    # Buyer sent a bad-account UID first (leading '2' => mock account-not-found),
    # then corrected it (leading '1' => success). Newest valid wins.
    acct = BackfillAccount(
        sales=[_paid_sale("ORD6")],
        histories={"chat-1": [
            FakeMessage("m1", "buyer-1", "chat-1", "234567890"),
            FakeMessage("m2", "buyer-1", "chat-1", "123456789"),
        ]},
    )
    p = _plugin(_cfg(tmp_path), BackfillCardinal(acct))
    p.run_backfill()

    order = p.repo.get_order_by_funpay_id("ORD6")
    assert order.status == OrderStatus.VALID.value
    p.stop()


def test_backfill_disabled(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.backfill_on_start = False
    acct = BackfillAccount(sales=[_paid_sale("ORD7")])
    p = _plugin(cfg, BackfillCardinal(acct))
    stats = p.run_backfill()

    assert stats.get("enabled") is False
    assert acct.sales_calls == 0
    assert p.repo.get_order_by_funpay_id("ORD7") is None
    p.stop()


def test_reconciler_parses_plain_list_of_shortcuts(tmp_path):
    # Some API variants return a bare list rather than (next, list).
    class ListAccount(BackfillAccount):
        def get_sales(self, *a, **kw):
            self.sales_calls += 1
            return list(self._sales)   # bare list, no next tuple

    acct = ListAccount(sales=[_paid_sale("ORD8")], histories={"chat-1": []})
    p = _plugin(_cfg(tmp_path), BackfillCardinal(acct))
    stats = p.run_backfill()

    assert stats["registered"] == 1
    p.stop()
