"""End-to-end flow tests covering task spec section 19 scenarios.

Codes are exactly 18 chars ([A-Za-z0-9]{18}) per the Spark API spec; the mock
SparkChecker keys its outcome off magic substrings in the code.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import FakeMessage, make_order  # noqa: E402

from pubg_uc_spark.database.models import CodeStatus, OrderStatus  # noqa: E402

# 18-character test codes (mock keys off the leading magic token).
VALID = "GOODaaaaaaaaaaaaaa"
INVALID = "BADXaaaaaaaaaaaaaa"
NOACC = "NOACCaaaaaaaaaaaaa"
USED = "USEDaaaaaaaaaaaaaa"
TEMP = "TEMPaaaaaaaaaaaaaa"
CRIT = "CRITaaaaaaaaaaaaaa"
VALID2 = "GOODbbbbbbbbbbbbbb"

for _c in (VALID, INVALID, NOACC, USED, TEMP, CRIT, VALID2):
    assert len(_c) == 18, _c


def _order_status(plugin, funpay_id):
    return plugin.repo.get_order_by_funpay_id(funpay_id).status


def _msg(plugin, cardinal, buyer_id, chat_id, text, mid):
    plugin.on_new_message(FakeMessage(mid, buyer_id, chat_id, text))


# 1. New order -> WAITING_FOR_CODE + single ask_code message.
def test_new_order_asks_for_code(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1001"))
    assert _order_status(plugin, "1001") == OrderStatus.WAITING_FOR_CODE.value
    asks = cardinal.texts_to("chat-1")
    assert len(asks) == 1 and "1001" in asks[0]


# 1b. Untracked lot is skipped entirely.
def test_untracked_lot_skipped(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1002", lot_id="99999999"))
    assert plugin.repo.get_order_by_funpay_id("1002") is None


# 2. Message without a code is ignored, order stays waiting.
def test_message_without_code_ignored(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1003"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", "привет, когда код?", "m1")
    assert _order_status(plugin, "1003") == OrderStatus.WAITING_FOR_CODE.value


# 3. Valid code -> VALID + success message.
def test_valid_code(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1004"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", VALID, "m2")
    assert _order_status(plugin, "1004") == OrderStatus.VALID.value
    code = plugin.repo.get_codes_for_order(plugin.repo.get_order_by_funpay_id("1004").id)[0]
    assert code.status == CodeStatus.VALID.value
    assert any("успешно" in t or "✅" in t for t in cardinal.texts_to("chat-1"))


# 4. Invalid code -> INVALID.
def test_invalid_code(plugin, cardinal):
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


# 6. Already used code -> ALREADY_USED.
def test_already_used(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1007"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", USED, "m5")
    assert _order_status(plugin, "1007") == OrderStatus.ALREADY_USED.value


# 7. Same code sent twice -> only one check, one duplicate notice.
def test_duplicate_code_message(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1008"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", VALID, "m6")
    before = len(plugin.repo.get_codes_for_order(plugin.repo.get_order_by_funpay_id("1008").id))
    _msg(plugin, cardinal, "buyer-1", "chat-1", VALID, "m7")
    after = len(plugin.repo.get_codes_for_order(plugin.repo.get_order_by_funpay_id("1008").id))
    assert before == after == 1  # no second code row


# 8a. Duplicate NEW_ORDER event -> single order row, asked once.
def test_duplicate_order_event(plugin, cardinal):
    shortcut = make_order(cardinal, "1009")
    plugin.on_new_order(shortcut)
    plugin.on_new_order(shortcut)  # replay
    assert len(cardinal.texts_to("chat-1")) == 1


# 8b. Duplicate NEW_MESSAGE event (same message_id) -> processed once.
def test_duplicate_message_event(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "1010"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", VALID, "same-id")
    _msg(plugin, cardinal, "buyer-1", "chat-1", VALID, "same-id")  # replay
    order = plugin.repo.get_order_by_funpay_id("1010")
    assert len(plugin.repo.get_codes_for_order(order.id)) == 1


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


# 13. Restart recovery: a stuck code is re-run.
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


# 15. One buyer with multiple orders -> code applies to the oldest active order.
def test_one_buyer_multiple_orders(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "3001", buyer_id="C", chat_id="cC"))
    plugin.on_new_order(make_order(cardinal, "3002", buyer_id="C", chat_id="cC"))
    _msg(plugin, cardinal, "C", "cC", VALID, "mC")
    assert _order_status(plugin, "3001") == OrderStatus.VALID.value
    assert _order_status(plugin, "3002") == OrderStatus.WAITING_FOR_CODE.value


# Ask-code message is not spammed on repeated non-code chatter.
def test_ask_code_not_spammed(plugin, cardinal):
    plugin.on_new_order(make_order(cardinal, "4001"))
    _msg(plugin, cardinal, "buyer-1", "chat-1", "когда?", "x1")
    _msg(plugin, cardinal, "buyer-1", "chat-1", "ну как?", "x2")
    assert len(cardinal.texts_to("chat-1")) == 1
