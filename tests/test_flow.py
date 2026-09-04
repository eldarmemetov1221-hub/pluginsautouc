"""End-to-end flow tests for the UID -> Spark stock-redeem flow.

Flow: buyer pays (bot stays silent) -> buyer sends PUBG UID -> validate it is a
UID (digits, 9-11) -> redeem from Spark stock -> report result.

The mock SparkChecker keys its outcome off the leading digit of the UID:
  1x -> success, 2x -> account not found, 3x -> temporary, 4x -> critical,
  5x -> out of stock (ERROR), 7x -> invalid/declined.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import FakeMessage, make_order  # noqa: E402

from pubg_uc_spark.database.models import CodeStatus, OrderStatus  # noqa: E402

VALID = "100000000"
INVALID = "700000000"
NOACC = "200000000"
TEMP = "300000000"
CRIT = "400000000"
STOCK = "500000000"
VALID2 = "100000001"


def _order_status(plugin, funpay_id):
    return plugin.repo.get_order_by_funpay_id(funpay_id).status


def _msg(plugin, cardinal, buyer_id, chat_id, text, mid):
    plugin.on_new_message(FakeMessage(mid, buyer_id, chat_id, text))


def _codes(plugin, funpay_id):
    return plugin.repo.get_codes_for_order(plugin.repo.get_order_by_funpay_id(funpay_id).id)


# 1. New order -> WAITING_FOR_CODE, and NO message is sent to the buyer.
def test_new_order_is_silent(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1001"))
    assert _order_status(plugin, "1001") == OrderStatus.WAITING_FOR_CODE.value
    assert cardinal.sent == []  # nothing sent after payment


# 1b. Untracked lot is skipped entirely.
def test_untracked_lot_skipped(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1002", lot_id="99999999"))
    assert plugin.repo.get_order_by_funpay_id("1002") is None


# 2. Message without a UID is ignored, order stays waiting, no message.
def test_message_without_uid_ignored(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1003"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", "привет, когда пополнение?", "m1")
    assert _order_status(plugin, "1003") == OrderStatus.WAITING_FOR_CODE.value
    assert cardinal.sent == []


# 2b. A malformed "UID" (not 9-11 digits) -> bad_format reply, no redeem.
def test_bad_uid_format(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1003b"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", "мой id 12345", "mbf")  # too short
    assert _order_status(plugin, "1003b") == OrderStatus.WAITING_FOR_CODE.value
    assert any("игровой ID" in t for t in cardinal.texts_to("chat-1"))


# 3. Valid UID -> VALID + success message mentioning the UID.
def test_valid_uid(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1004"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", VALID, "m2")
    assert _order_status(plugin, "1004") == OrderStatus.VALID.value
    assert _codes(plugin, "1004")[0].status == CodeStatus.VALID.value
    success = [t for t in cardinal.texts_to("chat-1") if "Успешное пополнение" in t]
    assert success, cardinal.sent
    # UID and resolved player name are rendered into the message.
    assert VALID in success[0]
    assert "MockPlayer" in success[0]


# 4. Redeem declined -> INVALID.
def test_invalid_redeem(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1005"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", INVALID, "m3")
    assert _order_status(plugin, "1005") == OrderStatus.INVALID.value


# 5. Account not found -> ACCOUNT_NOT_FOUND + reason stored.
def test_account_not_found(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1006"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", NOACC, "m4")
    order = plugin.repo.get_order_by_funpay_id("1006")
    assert order.status == OrderStatus.ACCOUNT_NOT_FOUND.value
    code = plugin.repo.get_codes_for_order(order.id)[0]
    assert code.status == CodeStatus.ACCOUNT_NOT_FOUND.value
    assert code.error_message == "account_not_found"


# 6. Out of stock -> ERROR + admin, buyer gets the error message.
def test_out_of_stock(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1007"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", STOCK, "m5")
    assert _order_status(plugin, "1007") == OrderStatus.ERROR.value
    assert _codes(plugin, "1007")[0].status == CodeStatus.FAILED.value


# 7. Same UID sent twice -> only one redeem, one duplicate notice.
def test_duplicate_uid_message(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1008"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", VALID, "m6")
    before = len(_codes(plugin, "1008"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", VALID, "m7")
    after = len(_codes(plugin, "1008"))
    assert before == after == 1


# 8a. Duplicate NEW_ORDER event -> single order row, still silent.
def test_duplicate_order_event(plugin, cardinal):
    shortcut = make_order(cardinal, "1009")
    plugin.on_new_order(shortcut)
    plugin.on_new_order(shortcut)  # replay
    assert plugin.repo.get_order_by_funpay_id("1009") is not None
    assert cardinal.sent == []


# 8b. Duplicate NEW_MESSAGE event (same message_id) -> processed once.
def test_duplicate_message_event(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1010"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", VALID, "same-id")
    _msg(plugin, cardinal, "buyer-1", "chat-1", VALID, "same-id")  # replay
    assert len(_codes(plugin, "1010")) == 1


# 9/10/11. Temporary Spark error -> retries then TEMPORARY_ERROR.
def test_temporary_error_retries(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1011"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", TEMP, "m8")
    order = plugin.repo.get_order_by_funpay_id("1011")
    assert order.status == OrderStatus.TEMPORARY_ERROR.value
    code = plugin.repo.get_codes_for_order(order.id)[0]
    assert code.status == CodeStatus.TEMPORARY_ERROR.value
    assert code.attempts == plugin.cfg.max_retries


# 12. Critical / unknown Spark response -> FAILED + ERROR, no retry.
def test_critical_error(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1012"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", CRIT, "m9")
    order = plugin.repo.get_order_by_funpay_id("1012")
    assert order.status == OrderStatus.ERROR.value
    code = plugin.repo.get_codes_for_order(order.id)[0]
    assert code.status == CodeStatus.FAILED.value
    assert code.attempts == 1


# 13. Restart recovery: a stuck redeem is re-run.
def test_restart_recovery(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1013"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", TEMP, "m10")
    order = plugin.repo.get_order_by_funpay_id("1013")
    assert order.status == OrderStatus.TEMPORARY_ERROR.value
    code = plugin.repo.get_codes_for_order(order.id)[0]
    plugin.db.execute("UPDATE codes SET code = ? WHERE id = ?", (VALID2, code.id))
    resumed = plugin.orders.resume_unfinished()
    assert resumed == 1
    assert _order_status(plugin, "1013") == OrderStatus.VALID.value


# 14. Two orders (two buyers) processed independently.
def test_two_orders_concurrent(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "2001", buyer_id="A", chat_id="cA"))
    plugin.on_new_order(make_order(cardinal, "2002", buyer_id="B", chat_id="cB"))
    _msg(plugin, cardinal, "A", "cA", VALID, "mA")
    _msg(plugin, cardinal, "B", "cB", INVALID, "mB")
    assert _order_status(plugin, "2001") == OrderStatus.VALID.value
    assert _order_status(plugin, "2002") == OrderStatus.INVALID.value


# 15. One buyer with multiple orders -> UID applies to the oldest active order.
def test_one_buyer_multiple_orders(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "3001", buyer_id="C", chat_id="cC"))
    plugin.on_new_order(make_order(cardinal, "3002", buyer_id="C", chat_id="cC"))
    _msg(plugin, cardinal, "C", "cC", VALID, "mC")
    assert _order_status(plugin, "3001") == OrderStatus.VALID.value
    assert _order_status(plugin, "3002") == OrderStatus.WAITING_FOR_CODE.value
