"""Unit tests: validators, masking, FSM, parser."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pubg_uc_spark.database.models import OrderStatus, can_transition
from pubg_uc_spark.spark import parser
from pubg_uc_spark.spark.models import UnifiedStatus
from pubg_uc_spark.utils.logger import mask_code
from pubg_uc_spark.utils.validators import (
    code_hash,
    extract_first_code,
    is_valid_format,
)

PATTERN = r"[0-9]{9,11}"     # PUBG UID: digits, 9-11 long
UID = "123456789"            # 9 digits


def test_extract_uid_from_message():
    assert extract_first_code(f"вот мой id: {UID} спасибо", PATTERN) == UID
    assert extract_first_code("нет id тут", PATTERN) is None
    # A 12-digit run is NOT a valid UID and must not be partially matched.
    assert extract_first_code("123456789012", PATTERN) is None


def test_is_valid_format():
    assert is_valid_format(UID, PATTERN)
    assert is_valid_format("12345678901", PATTERN)       # 11 digits
    assert not is_valid_format("12345", PATTERN)          # too short
    assert not is_valid_format("123456789012", PATTERN)   # too long
    assert not is_valid_format("12ab56789", PATTERN)      # not digits
    assert not is_valid_format("", PATTERN)


def test_code_hash_stable_and_masking():
    assert code_hash("ABCDEFGH") == code_hash(" ABCDEFGH ")  # trimmed
    assert mask_code("ABCDEFGH1234") == "ABCD****1234"
    assert mask_code("short") == "*****"
    assert mask_code("") == "<empty>"


def test_lot_matching_by_description():
    from types import SimpleNamespace

    from pubg_uc_spark.config import Config, LotConfig
    from pubg_uc_spark.funpay import orders as fo

    cfg = Config()
    cfg.lots = {"37330959": LotConfig("37330959", "PUBG Mobile 60 UC", "60")}

    def sc(desc):
        return SimpleNamespace(description=desc)

    assert fo.match_lot(cfg, sc("PUBG Mobile 60 UC для ID игрока")) is not None
    # denomination matched as a whole number: 660 UC must NOT match the 60 lot
    assert fo.match_lot(cfg, sc("PUBG Mobile 660 UC")) is None
    assert fo.match_lot(cfg, sc("Brawl Stars gems")) is None
    # explicit keywords override
    cfg.lots = {"1": LotConfig("1", "X", "60", keywords=["pubg", "60 uc"])}
    assert fo.match_lot(cfg, sc("PUBG top-up 60 UC")) is not None
    assert fo.match_lot(cfg, sc("PUBG 120 UC")) is None


def test_parse_job_and_boolean_validity():
    # A finished job with a per-code result row.
    valid_job = {"status": "done", "result": {"results": [{"code": "x", "valid": True}]}}
    assert parser.parse_job(valid_job).status is UnifiedStatus.VALID
    invalid_job = {"status": "done", "result": {"results": [{"code": "x", "valid": False}]}}
    assert parser.parse_job(invalid_job).status is UnifiedStatus.INVALID
    # A negative reason in text wins over a (missing) bool flag.
    noacc = {"status": "done", "result": {"results": [{"message": "account does not exist"}]}}
    assert parser.parse_job(noacc).status is UnifiedStatus.ACCOUNT_NOT_FOUND
    # results[] as a bare object (no wrapper).
    bare = {"status": "done", "result": {"valid": True}}
    assert parser.parse_job(bare).status is UnifiedStatus.VALID


def test_parser_structured_error_codes():
    # Real Spark error shape: {"detail": {"error": CODE, "message": ...}}
    anf = {"detail": {"error": "INVALID_PLAYER_ID", "message": "Invalid player identifier."}}
    assert parser.parse(anf).status is UnifiedStatus.ACCOUNT_NOT_FOUND
    assert parser.parse_job(anf).status is UnifiedStatus.ACCOUNT_NOT_FOUND
    assert parser.parse({"detail": {"error": "OUT_OF_STOCK"}}).status is UnifiedStatus.ERROR
    assert parser.parse({"detail": {"error": "PLAYER_NOT_FOUND"}}).status is UnifiedStatus.ACCOUNT_NOT_FOUND


def test_parse_real_stock_redeem_success_job():
    # Real finished job captured from the live API (trimmed).
    job = {
        "type": "stock_redeem",
        "status": "done",
        "result": {
            "ok_count": 1,
            "results": [
                {
                    "code": "53HwbW8K2E2d4dZ0R5",  # stock cdkey - must NOT be read as an error code
                    "denomination": 60,
                    "success": True,
                    "msg": "",
                    "err_code": "",
                    "cdkey_name": "60 UC",
                    "charac_name": "Balabechka",
                    "order_details": {"status": "Success", "charac_name": "Balabechka"},
                }
            ],
            "player_id": "5782609572",
        },
    }
    r = parser.parse_job(job)
    assert r.status is UnifiedStatus.VALID
    assert r.player_name == "Balabechka"


def test_parse_stock_redeem_failure_row():
    # A per-code failure row uses success=false + err_code.
    job = {"status": "done", "result": {"results": [
        {"success": False, "err_code": "INVALID_PLAYER_ID", "msg": "bad id"}]}}
    assert parser.parse_job(job).status is UnifiedStatus.ACCOUNT_NOT_FOUND
    job2 = {"status": "done", "result": {"results": [
        {"success": False, "err_code": "OUT_OF_STOCK", "msg": ""}]}}
    assert parser.parse_job(job2).status is UnifiedStatus.ERROR
    job3 = {"status": "done", "result": {"results": [
        {"success": False, "err_code": "SOME_OTHER", "msg": "declined"}]}}
    assert parser.parse_job(job3).status is UnifiedStatus.INVALID


def test_fsm_transitions():
    assert can_transition(OrderStatus.NEW, OrderStatus.WAITING_FOR_CODE)
    assert can_transition(OrderStatus.CHECKING, OrderStatus.VALID)
    assert can_transition(OrderStatus.TEMPORARY_ERROR, OrderStatus.CHECKING)
    # illegal
    assert not can_transition(OrderStatus.NEW, OrderStatus.VALID)
    assert not can_transition(OrderStatus.VALID, OrderStatus.CHECKING)
    # idempotent no-op allowed
    assert can_transition(OrderStatus.VALID, OrderStatus.VALID)


def test_parser_mapping():
    assert parser.parse({"message": "account does not exist"}).status is UnifiedStatus.ACCOUNT_NOT_FOUND
    assert parser.parse({"message": "code already used"}).status is UnifiedStatus.ALREADY_USED
    assert parser.parse({"status": "success"}).status is UnifiedStatus.VALID
    assert parser.parse({"message": "invalid code"}).status is UnifiedStatus.INVALID
    assert parser.parse({"foo": "bar"}).status is UnifiedStatus.UNKNOWN
