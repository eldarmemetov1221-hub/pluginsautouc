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

# Fields that may carry the resolved player/character name.
# ("charac_name" is what stock-redeem returns per code, incl. in order_details.)
_NAME_KEYS = ("charac_name", "player_name", "character_name", "nickname", "nick",
              "ign", "name", "username")

# Spark returns structured errors as {"detail": {"error": "CODE", "message": ...}}
# (FastAPI style). Mapping the CODE is far more reliable than free-text matching.
_ERROR_CODE_MAP = {
    # UID / account problems -> ACCOUNT_NOT_FOUND
    "INVALID_PLAYER_ID": UnifiedStatus.ACCOUNT_NOT_FOUND,
    "PLAYER_NOT_FOUND": UnifiedStatus.ACCOUNT_NOT_FOUND,
    "ACCOUNT_NOT_FOUND": UnifiedStatus.ACCOUNT_NOT_FOUND,
    "UNKNOWN_PLAYER": UnifiedStatus.ACCOUNT_NOT_FOUND,
    "INVALID_UID": UnifiedStatus.ACCOUNT_NOT_FOUND,
    # stock problems -> operational ERROR (seller must restock)
    "OUT_OF_STOCK": UnifiedStatus.ERROR,
    "NO_STOCK": UnifiedStatus.ERROR,
    "INSUFFICIENT_STOCK": UnifiedStatus.ERROR,
    "STOCK_EMPTY": UnifiedStatus.ERROR,
    # already redeemed
    "ALREADY_USED": UnifiedStatus.ALREADY_USED,
    "ALREADY_REDEEMED": UnifiedStatus.ALREADY_USED,
}


def error_code(payload) -> str:
    """Extract a structured error CODE from a Spark payload, if any."""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail")
    if isinstance(detail, dict) and detail.get("error"):
        return str(detail["error"])
    # stock-redeem per-code rows use "err_code" (empty string on success).
    for key in ("err_code", "error_code", "code_error"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def extract_player_name(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in _NAME_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Sometimes nested under order_details / player.
    for nest in ("order_details", "player", "account"):
        sub = payload.get(nest)
        if isinstance(sub, dict):
            name = extract_player_name(sub)
            if name:
                return name
    return ""


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
    # Include the nested detail.message (FastAPI error bodies).
    detail = payload.get("detail")
    if isinstance(detail, dict):
        for k in ("message", "msg", "error"):
            v = detail.get(k)
            if isinstance(v, str):
                parts.append(v)
    elif isinstance(detail, str):
        parts.append(detail)
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

    # Structured error code wins over everything (most reliable signal).
    code = error_code(payload)
    if code:
        mapped = _ERROR_CODE_MAP.get(code.upper())
        if mapped is not None:
            detail = payload.get("detail")
            dmsg = detail.get("message") if isinstance(detail, dict) else ""
            return SparkResult(
                status=mapped, raw=payload, message=str(dmsg or msg or code),
                http_status=http_status, player_name=extract_player_name(payload),
            )

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

    return SparkResult(
        status=status,
        raw=payload,
        message=msg,
        http_status=http_status,
        player_name=extract_player_name(payload),
    )


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
    """Parse a finished Spark job document into a :class:`SparkResult`.

    Handles multi-quantity orders: stock-redeem returns one row per pack in
    ``result.results[]``. The outcome is aggregated:
      * every row succeeded            -> VALID
      * none succeeded                 -> the (shared) negative reason
      * some succeeded, some did not   -> PARTIAL (delivered/total set)
    """
    if not isinstance(job, dict):
        return SparkResult(UnifiedStatus.UNKNOWN, raw={"_raw": job}, http_status=http_status)

    result_obj = job.get("result")
    rows = result_obj.get("results") if isinstance(result_obj, dict) else None

    if isinstance(rows, list) and rows:
        parsed = [parse(r, http_status=http_status) for r in rows]
        total = len(parsed)
        ok = sum(1 for p in parsed if p.status is UnifiedStatus.VALID)
        name = next((p.player_name for p in parsed if p.player_name), "") or \
            extract_player_name(job) or extract_player_name(result_obj)

        if ok == total:
            status = UnifiedStatus.VALID
        elif ok == 0:
            # All failed - surface the first non-VALID reason (same UID => same
            # reason in practice, e.g. account not found).
            status = next((p.status for p in parsed if p.status is not UnifiedStatus.VALID),
                          UnifiedStatus.UNKNOWN)
        else:
            status = UnifiedStatus.PARTIAL

        msg = next((p.message for p in parsed if p.message), "")
        return SparkResult(status=status, raw=job, message=msg, http_status=http_status,
                           player_name=name, delivered=ok, total=total)

    # No results[] wrapper (e.g. a synchronous detail.error body) - single parse.
    row = _first_row(job)
    result = parse(row, http_status=http_status)
    if not result.player_name:
        result.player_name = extract_player_name(job) or extract_player_name(
            result_obj if isinstance(result_obj, dict) else {}
        )
    result.total = 1
    result.delivered = 1 if result.status is UnifiedStatus.VALID else 0
    return result
