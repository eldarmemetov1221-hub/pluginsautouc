"""Spark response -> UnifiedStatus (task spec, sections 3 & 4).

    Spark response  ->  parser  ->  unified status  ->  business logic

THIS IS THE ONLY PLACE that knows Spark's wire format. When the real
api.pubgredeemerbot.com schema is available, edit ONLY this file - the rest of
the plugin keeps working unchanged.

Until the real examples are provided, the parser is intentionally *heuristic*:
it inspects common fields (``status`` / ``result`` / ``message`` / ``error`` /
``code``) and keyword-matches them to a UnifiedStatus. The keyword tables below
are the single knob to tune.
"""

from __future__ import annotations

from typing import Any, Dict

from ..utils.logger import get_logger
from .models import SparkResult, UnifiedStatus

log = get_logger("spark.parser")

# --------------------------------------------------------------------------- #
# Keyword tables. Adjust these to Spark's real vocabulary. Order matters only
# in that ACCOUNT_NOT_FOUND / ALREADY_USED are checked before the generic
# INVALID so a more specific reason wins.
# --------------------------------------------------------------------------- #
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
)
_INVALID = (
    "invalid",
    "not valid",
    "incorrect",
    "expired",
    "wrong code",
    "does not exist",  # generic invalid-code phrasing
)
_VALID = (
    "success",
    "redeemed successfully",
    "valid",
    "delivered",
    "completed",
)


def _text_of(payload: Dict[str, Any]) -> str:
    """Concatenate the human-readable fields for keyword matching."""
    parts = []
    for key in ("status", "result", "state", "message", "msg", "error", "reason", "detail"):
        val = payload.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif val is not None:
            parts.append(str(val))
    return " ".join(parts).lower()


def _match(text: str, needles) -> bool:
    return any(n in text for n in needles)


def parse(payload: Dict[str, Any], http_status: int | None = None) -> SparkResult:
    """Map a decoded Spark JSON payload to a :class:`SparkResult`.

    A payload that matches nothing yields :attr:`UnifiedStatus.UNKNOWN`, which
    the client treats as a critical (unrecognised) response rather than
    silently succeeding.
    """
    if not isinstance(payload, dict):
        return SparkResult(UnifiedStatus.UNKNOWN, raw={"_raw": payload}, http_status=http_status)

    text = _text_of(payload)
    msg = str(payload.get("message") or payload.get("msg") or payload.get("error") or "")

    # Most specific reasons first.
    # Most specific / most negative reasons first; "invalid" before "valid"
    # (the former contains the latter as a substring).
    if _match(text, _ACCOUNT_NOT_FOUND):
        status = UnifiedStatus.ACCOUNT_NOT_FOUND
    elif _match(text, _ALREADY_USED):
        status = UnifiedStatus.ALREADY_USED
    elif _match(text, _INVALID):
        status = UnifiedStatus.INVALID
    elif _match(text, _VALID):
        status = UnifiedStatus.VALID
    else:
        status = UnifiedStatus.UNKNOWN
        log.warning("Unrecognised Spark payload: keys=%s", list(payload.keys()))

    return SparkResult(status=status, raw=payload, message=msg, http_status=http_status)
