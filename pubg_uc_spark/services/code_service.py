"""Code intake: extract, validate, dedup (task spec, sections 8, 9, 10).

Given an order that is expecting a code and a buyer message, decide what to do.
This service NEVER sends messages or calls Spark - it only classifies the
incoming code and persists it idempotently. The order_service acts on the
returned :class:`IntakeResult`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..database.models import (
    CODE_FINAL_NEGATIVE,
    CODE_SUCCESS,
    CodeRecord,
    CodeStatus,
    OrderRecord,
)
from ..utils.logger import get_logger, mask_code
from ..utils.validators import code_hash, extract_first_code, is_valid_format

log = get_logger("code")


class IntakeAction(str, Enum):
    NO_CODE = "NO_CODE"                # no code-shaped token in the message
    BAD_FORMAT = "BAD_FORMAT"          # token found but fails full validation
    ENQUEUE = "ENQUEUE"               # new code stored, ready to check
    DUPLICATE_INFLIGHT = "DUPLICATE_INFLIGHT"   # same code already being checked
    ALREADY_PROCESSED = "ALREADY_PROCESSED"     # same code already VALID here
    ALREADY_NEGATIVE = "ALREADY_NEGATIVE"       # same code already final-negative
    GLOBAL_USED = "GLOBAL_USED"       # this code was already VALID on another order


@dataclass
class IntakeResult:
    action: IntakeAction
    code: Optional[CodeRecord] = None


class CodeService:
    def __init__(self, config, repo):
        self.cfg = config
        self.repo = repo

    def process(self, order: OrderRecord, text: str, message_id: str) -> IntakeResult:
        raw = extract_first_code(text, self.cfg.code_pattern)
        if not raw:
            return IntakeResult(IntakeAction.NO_CODE)
        if not is_valid_format(raw, self.cfg.code_pattern):
            log.info("[Order #%s] Code has bad format", order.funpay_order_id)
            return IntakeResult(IntakeAction.BAD_FORMAT)

        h = code_hash(raw)

        # Same code already seen for THIS order?
        existing = self.repo.find_code(order.id, h)
        if existing is not None:
            status = CodeStatus(existing.status)
            if status in CODE_SUCCESS:
                return IntakeResult(IntakeAction.ALREADY_PROCESSED, existing)
            if status in CODE_FINAL_NEGATIVE:
                return IntakeResult(IntakeAction.ALREADY_NEGATIVE, existing)
            # RECEIVED / CHECKING / TEMPORARY_ERROR -> still in flight.
            return IntakeResult(IntakeAction.DUPLICATE_INFLIGHT, existing)

        # Same code already redeemed successfully on a DIFFERENT order?
        anywhere = self.repo.find_code_by_hash_any_order(h)
        if anywhere is not None and CodeStatus(anywhere.status) in CODE_SUCCESS:
            log.warning(
                "[Order #%s] Code %s already VALID on order_id=%s",
                order.funpay_order_id,
                mask_code(raw),
                anywhere.order_id,
            )
            return IntakeResult(IntakeAction.GLOBAL_USED, anywhere)

        record = CodeRecord(
            code=raw,
            code_hash=h,
            order_id=order.id,
            funpay_order_id=order.funpay_order_id,
            buyer_id=order.buyer_id,
            product="",  # set from lot config below
            status=CodeStatus.RECEIVED.value,
            source="funpay_message",
            message_id=message_id,
        )
        lot = self.cfg.lot(order.lot_id)
        record.product = lot.product if lot else ""

        stored, created = self.repo.create_code(record)
        if not created:
            # Race: another event created it first -> treat as in-flight dup.
            return IntakeResult(IntakeAction.DUPLICATE_INFLIGHT, stored)

        log.info("[Order #%s] Received code %s", order.funpay_order_id, mask_code(raw))
        return IntakeResult(IntakeAction.ENQUEUE, stored)
