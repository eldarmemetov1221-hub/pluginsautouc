"""Spark response -> UnifiedStatus (task spec, sections 3 & 4).

    Spark response  ->  parser  ->  unified status  ->  business logic

THIS IS THE ONLY PLACE that knows Spark's wire format. Spark is a job API, so
the input is a finished job document; :func:`parse_job` reduces it to the
per-code result row and :func:`parse` classifies that row.

    job = {
        "status": "done",
        "result": {"results": [ {<per-code row>}, ... ]}
    }

The exact field names of a per-code row are NOT in the OpenAPI spec (its result
schemas are empty). Until a real finished-job sample is provided, classification
is defensive: an explicit boolean validity field is honoured first, otherwise a
keyword match over the row's text fields decides. Adjust the tables / field
lists below once the real shape is known - nothing else changes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..utils.logger import get_logger
from .models import SparkResult, UnifiedStatus

log = get_logger("spark.parser")

# --------------------------------------------------------------------------- #
# Field names that carry an explicit boolean validity. Checked before text.
_BOOL_VALID_KEYS = ("valid", "is_valid", "redeemable", "success", "ok")

# Keyword tables over the row's textual fields. Adjust to Spark's vocabulary.
_ACCOUNT_NOT_FOUND = (
    "account does not exist",
    "account not found",
    "player not found",
    "user not found",
    "no such account",
    "invalid player id",
    "invalid uid",
)
_ALREADY_USED = (
    "already used",
    "already redeemed",
    "code used",
    "used code",
    "already claimed",
)
# Definitive operational failure - not the buyer's fault, needs admin/restock.
_OUT_OF_STOCK = (
    "out of stock",
    "no stock",
    "insufficient stock",
    "sold out",
    "stock empty",
    "no codes",
)
_INVALID = (
    "invalid",
    "not valid",
    "incorrect",
    "expired",
    "wrong code",
    "not found",
    "does not exist",
)
_VALID = (
    "redeemed successfully",
    "redeemable",
    "success",
    "valid",
    "delivered",
    "completed",
)

# Textual fields scanned for keywords.
_TEXT_KEYS = ("status", "result", "state", "message", "msg", "error", "reason", "detail", "code_status")


def _text_of(payload: Dict[str, Any]) -> str:
    parts = []
    for key in _TEXT_KEYS:
        val = payload.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, bool):
            continue  # booleans handled separately
        elif val is not None:
            parts.append(str(val))
    return " ".join(parts).lower()


def _match(text: str, needles) -> bool:
    return any(n in text for n in needles)


def _bool_validity(payload: Dict[str, Any]) -> Optional[bool]:
    for key in _BOOL_VALID_KEYS:
        val = payload.get(key)
        if isinstance(val, bool):
            return val
    return None


def parse(payload: Dict[str, Any], http_status: int | None = None) -> SparkResult:
    """Classify a single per-code result row into a :class:`SparkResult`."""
    if not isinstance(payload, dict):
        return SparkResult(UnifiedStatus.UNKNOWN, raw={"_raw": payload}, http_status=http_status)

    text = _text_of(payload)
    msg = str(payload.get("message") or payload.get("msg") or payload.get("error") or "")

    # Specific negative reasons win regardless of any boolean flag.
    if _match(text, _ACCOUNT_NOT_FOUND):
        status = UnifiedStatus.ACCOUNT_NOT_FOUND
    elif _match(text, _OUT_OF_STOCK):
        status = UnifiedStatus.ERROR
    elif _match(text, _ALREADY_USED):
        status = UnifiedStatus.ALREADY_USED
    else:
        flag = _bool_validity(payload)
        if flag is True:
            status = UnifiedStatus.VALID
        elif flag is False:
            status = UnifiedStatus.INVALID
        elif _match(text, _INVALID):
            status = UnifiedStatus.INVALID
        elif _match(text, _VALID):
            status = UnifiedStatus.VALID
        else:
            status = UnifiedStatus.UNKNOWN
            log.warning("Unrecognised Spark row: keys=%s", list(payload.keys()))

    return SparkResult(status=status, raw=payload, message=msg, http_status=http_status)


def _first_row(job: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a finished job document to the single per-code result row."""
    result = job.get("result", job)
    if isinstance(result, dict):
        rows = result.get("results")
        if isinstance(rows, list) and rows:
            first = rows[0]
            return first if isinstance(first, dict) else {"_raw": first}
        # No results[] wrapper - treat the result object itself as the row.
        return result
    if isinstance(result, list) and result:
        first = result[0]
        return first if isinstance(first, dict) else {"_raw": first}
    return job


def parse_job(job: Dict[str, Any], http_status: int | None = None) -> SparkResult:
    """Parse a finished Spark job document into a :class:`SparkResult`."""
    if not isinstance(job, dict):
        return SparkResult(UnifiedStatus.UNKNOWN, raw={"_raw": job}, http_status=http_status)
    row = _first_row(job)
    return parse(row, http_status=http_status)
