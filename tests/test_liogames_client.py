"""LioGamesClient parsing, against the REAL order-status shape captured from a
completed live order (so extract_code never silently breaks)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.config import Config  # noqa: E402
from pubg_uc_spark.liogames.client import LioGamesClient  # noqa: E402

# Verbatim completed-order body from LioGames (order 536857, 60 UC voucher).
COMPLETED = {
    "ok": True, "code": "ORDER_STATUS", "message": "Order status fetched",
    "data": {
        "order_id": 536857, "order_number": "575551", "status": "completed",
        "status_label": "Completed", "result": "SUCCESS", "is_paid": True,
        "currency": "USD", "total": 0.88,
        "items": [{"item_id": 5101, "name": "PUBG Mobile Code (Global) - 60 UC",
                   "product_id": 66599, "variation_id": 534124, "qty": 1,
                   "total": 0.88, "meta": []}],
        "sn": "aYVQtqZs2E27YdH38d",
        "delivery_code": "aYVQtqZs2E27YdH38d",
        "delivery": {"ready": True, "codes": ["aYVQtqZs2E27YdH38d"]},
    },
}

PROCESSING = {
    "ok": True, "code": "ORDER_STATUS", "message": "Order status fetched",
    "data": {"order_id": 999, "status": "processing", "result": "PENDING", "items": []},
}


def _client():
    return LioGamesClient(Config())


def test_extract_code_from_real_completed_order():
    c = _client()
    assert c.extract_code(COMPLETED) == "aYVQtqZs2E27YdH38d"
    assert c.status_is_terminal_ok(COMPLETED) is True
    assert c.status_is_failed(COMPLETED) is False


def test_extract_code_ignores_ids_and_status_words():
    """order_id/product_id/variation_id/result(SUCCESS)/status must never be
    mistaken for the voucher code."""
    c = _client()
    code = c.extract_code(COMPLETED)
    assert code not in ("575551", "66599", "534124", "536857", "SUCCESS", "completed")


def test_refund_and_other_terminal_failures_detected():
    c = _client()
    for s in ("refunded", "refund", "declined", "void", "expired", "cancelled", "rejected"):
        body = {"data": {"status": s}}
        assert c.status_is_failed(body) is True, s
        assert c.status_is_terminal_ok(body) is False, s


def test_processing_order_has_no_code_yet():
    c = _client()
    assert c.extract_code(PROCESSING) is None
    assert c.status_is_terminal_ok(PROCESSING) is False
    assert c.status_is_failed(PROCESSING) is False


def test_signing_is_hmac_sha256_over_raw_body():
    import hmac
    import hashlib
    import json
    cfg = Config()
    cfg.liog_secret = "S3CR3T"
    c = LioGamesClient(cfg)
    payload = {"member_code": "M", "product_id": 66599, "variation_id": 534124,
               "client_ref": "UCBUY-1-1"}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    assert c._sign(raw) == hmac.new(b"S3CR3T", raw.encode(), hashlib.sha256).hexdigest()
