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

PATTERN = r"[A-Za-z0-9]{18}"
CODE18 = "a1B2c3D4e5F6g7H8i9"  # exactly 18 chars, per Spark spec


def test_extract_code_from_message():
    assert extract_first_code(f"вот код: {CODE18} спасибо", PATTERN) == CODE18
    assert extract_first_code("нет кода тут", PATTERN) is None
    assert extract_first_code("short", PATTERN) is None


def test_is_valid_format():
    assert is_valid_format(CODE18, PATTERN)
    assert not is_valid_format("tooshort123", PATTERN)   # < 18
    assert not is_valid_format("bad code with spaces!!", PATTERN)
    assert not is_valid_format("", PATTERN)


def test_code_hash_stable_and_masking():
    assert code_hash("ABCDEFGH") == code_hash(" ABCDEFGH ")  # trimmed
    assert mask_code("ABCDEFGH1234") == "ABCD****1234"
    assert mask_code("short") == "*****"
    assert mask_code("") == "<empty>"


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
