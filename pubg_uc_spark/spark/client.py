"""SparkChecker: the code-checking adapter (task spec, sections 3, 4, 11).

Public contract - the ONLY thing business logic calls:

    checker = SparkChecker(config)
    result: SparkResult = checker.check_code(code)   # -> UnifiedStatus

Transport concerns (HTTP, timeouts, 5xx, auth) are handled here and mapped to
the plugin's error taxonomy:

* transient transport failures  -> raise SparkTemporaryError (retryable)
* auth / unrecognised payloads   -> raise SparkCriticalError
* understood outcomes            -> return SparkResult

Mock mode (``config.spark_mock``) needs no network and drives deterministic
behaviour from magic substrings in the code - used by the test suite and for
local development before the real endpoint is wired in.
"""

from __future__ import annotations

from typing import Any, Dict

from ..errors import SparkCriticalError, SparkTemporaryError
from ..utils.logger import get_logger, mask_code
from . import parser
from .models import SparkResult, UnifiedStatus

log = get_logger("spark.client")

try:  # requests is a FunPayCardinal dependency; keep import optional for tests
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore


class SparkChecker:
    def __init__(self, config):
        self.cfg = config

    # ------------------------------------------------------------------ #
    def check_code(self, code: str) -> SparkResult:
        """Check a single code and return a normalised result.

        Raises :class:`SparkTemporaryError` for retryable failures and
        :class:`SparkCriticalError` for unrecoverable ones.
        """
        log.info("[Spark] Checking code %s", mask_code(code))
        if self.cfg.spark_mock:
            return self._check_mock(code)
        return self._check_http(code)

    # ------------------------------------------------------------------ #
    # Real HTTP transport.
    #
    # NOTE: request shape (path, method, field names, auth header) is a
    # placeholder until the api.pubgredeemerbot.com docs are provided. It is
    # deliberately isolated here so only this method + parser.py change when
    # the real schema arrives.
    # ------------------------------------------------------------------ #
    def _check_http(self, code: str) -> SparkResult:
        if requests is None:  # pragma: no cover
            raise SparkCriticalError("requests is not installed")
        if not self.cfg.spark_api_url:
            raise SparkCriticalError("SPARK_API_URL is not configured")

        headers = {"Accept": "application/json"}
        if self.cfg.spark_api_key:
            headers["Authorization"] = f"Bearer {self.cfg.spark_api_key}"

        payload = {"code": code}

        try:
            resp = requests.post(
                self.cfg.spark_api_url,
                json=payload,
                headers=headers,
                timeout=self.cfg.spark_timeout,
            )
        except Exception as exc:  # network / timeout / connection reset
            raise SparkTemporaryError(f"Spark request failed: {exc}") from exc

        # 5xx / 429 -> transient. 401/403 -> critical (auth). Others parsed.
        if resp.status_code >= 500 or resp.status_code == 429:
            raise SparkTemporaryError(f"Spark HTTP {resp.status_code}")
        if resp.status_code in (401, 403):
            raise SparkCriticalError(f"Spark auth error HTTP {resp.status_code}")

        try:
            data: Dict[str, Any] = resp.json()
        except ValueError as exc:
            raise SparkCriticalError(f"Spark returned non-JSON body: {exc}") from exc

        result = parser.parse(data, http_status=resp.status_code)
        if result.status is UnifiedStatus.UNKNOWN:
            raise SparkCriticalError(
                f"Unrecognised Spark response (HTTP {resp.status_code}): "
                f"keys={list(data.keys())}"
            )
        return result

    # ------------------------------------------------------------------ #
    # Deterministic mock (no network). Magic substrings drive the outcome.
    # ------------------------------------------------------------------ #
    def _check_mock(self, code: str) -> SparkResult:
        up = code.upper()
        if "NOACC" in up:
            payload = {"status": "error", "message": "account does not exist"}
        elif "USED" in up:
            payload = {"status": "error", "message": "code already used"}
        elif "TEMP" in up or "ERR500" in up:
            raise SparkTemporaryError("Mock temporary error")
        elif "CRIT" in up:
            raise SparkCriticalError("Mock critical error")
        elif "WEIRD" in up:
            payload = {"foo": "bar"}  # -> UNKNOWN -> critical
        elif "VALID" in up or "GOOD" in up:
            payload = {"status": "success", "message": "redeemed successfully"}
        else:
            payload = {"status": "error", "message": "invalid code"}

        result = parser.parse(payload, http_status=200)
        if result.status is UnifiedStatus.UNKNOWN:
            raise SparkCriticalError("Unrecognised (mock) Spark response")
        return result
