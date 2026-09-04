"""Data models, enums and the order state machine (task spec, section 7).

The FSM guards against uncontrolled transitions: :func:`can_transition`
enforces the allowed edges. Admin actions may force a transition explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Set


class OrderStatus(str, Enum):
    """Order lifecycle (section 7)."""

    NEW = "NEW"
    WAITING_FOR_CODE = "WAITING_FOR_CODE"
    CODE_RECEIVED = "CODE_RECEIVED"
    CHECKING = "CHECKING"
    TEMPORARY_ERROR = "TEMPORARY_ERROR"
    VALID = "VALID"
    INVALID = "INVALID"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ALREADY_USED = "ALREADY_USED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


#: Terminal-ish states that still allow a fresh code to be submitted by the
#: buyer (negative outcomes that the buyer can retry with a new code).
RESUBMITTABLE = {
    OrderStatus.INVALID,
    OrderStatus.ALREADY_USED,
    OrderStatus.ACCOUNT_NOT_FOUND,
}

#: Fully final states (no automatic action).
FINAL = {OrderStatus.VALID, OrderStatus.CANCELLED}

# Allowed transitions. Anything not listed is rejected unless forced.
_ALLOWED: Dict[OrderStatus, Set[OrderStatus]] = {
    OrderStatus.NEW: {OrderStatus.WAITING_FOR_CODE, OrderStatus.CANCELLED},
    OrderStatus.WAITING_FOR_CODE: {OrderStatus.CODE_RECEIVED, OrderStatus.CANCELLED},
    OrderStatus.CODE_RECEIVED: {OrderStatus.CHECKING, OrderStatus.CANCELLED},
    OrderStatus.CHECKING: {
        OrderStatus.VALID,
        OrderStatus.INVALID,
        OrderStatus.ACCOUNT_NOT_FOUND,
        OrderStatus.ALREADY_USED,
        OrderStatus.ERROR,
        OrderStatus.TEMPORARY_ERROR,
    },
    OrderStatus.TEMPORARY_ERROR: {OrderStatus.CHECKING, OrderStatus.ERROR, OrderStatus.CANCELLED},
    # Negative outcomes: buyer may send a new code -> back to waiting.
    OrderStatus.INVALID: {OrderStatus.WAITING_FOR_CODE, OrderStatus.CODE_RECEIVED, OrderStatus.CANCELLED},
    OrderStatus.ALREADY_USED: {OrderStatus.WAITING_FOR_CODE, OrderStatus.CODE_RECEIVED, OrderStatus.CANCELLED},
    OrderStatus.ACCOUNT_NOT_FOUND: {OrderStatus.WAITING_FOR_CODE, OrderStatus.CODE_RECEIVED, OrderStatus.CANCELLED},
    OrderStatus.ERROR: {OrderStatus.CHECKING, OrderStatus.WAITING_FOR_CODE, OrderStatus.CANCELLED},
    OrderStatus.VALID: set(),
    OrderStatus.CANCELLED: set(),
}


def can_transition(current: OrderStatus, new: OrderStatus) -> bool:
    if current == new:
        return True  # idempotent no-op is always allowed
    return new in _ALLOWED.get(current, set())


class CodeStatus(str, Enum):
    """Per-code processing status."""

    RECEIVED = "RECEIVED"
    CHECKING = "CHECKING"
    VALID = "VALID"
    INVALID = "INVALID"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ALREADY_USED = "ALREADY_USED"
    TEMPORARY_ERROR = "TEMPORARY_ERROR"
    FAILED = "FAILED"          # final technical failure (retries exhausted)
    DUPLICATE = "DUPLICATE"    # same code seen again for the same order


#: Code statuses considered a *successful, final* processing result. Such a
#: code must never be re-checked (section 5).
CODE_SUCCESS = {CodeStatus.VALID}

#: Code statuses that are *definitively negative* - not re-checked without an
#: explicit admin action (section 5).
CODE_FINAL_NEGATIVE = {
    CodeStatus.INVALID,
    CodeStatus.ACCOUNT_NOT_FOUND,
    CodeStatus.ALREADY_USED,
    CodeStatus.FAILED,
}

#: Statuses eligible for an automatic retry (section 5 & 12).
CODE_RETRIABLE = {CodeStatus.TEMPORARY_ERROR}


@dataclass
class OrderRecord:
    id: Optional[int] = None
    funpay_order_id: str = ""
    lot_id: str = ""
    buyer_id: str = ""
    buyer_username: str = ""
    quantity: int = 1
    status: str = OrderStatus.NEW.value
    chat_id: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class CodeRecord:
    id: Optional[int] = None
    code: str = ""
    code_hash: str = ""
    order_id: Optional[int] = None          # FK -> orders.id
    funpay_order_id: str = ""
    buyer_id: str = ""
    product: str = ""
    status: str = CodeStatus.RECEIVED.value
    spark_status: str = ""
    error_message: str = ""
    attempts: int = 0
    source: str = "funpay_message"
    message_id: str = ""
    created_at: str = ""
    checked_at: str = ""
    updated_at: str = ""


@dataclass
class LogRecord:
    id: Optional[int] = None
    order_id: Optional[int] = None
    code_id: Optional[int] = None
    level: str = "INFO"
    event: str = ""
    message: str = ""
    created_at: str = ""
