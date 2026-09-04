"""SparkChecker: the code-checking adapter (task spec, sections 3, 4, 11).

Public contract - the ONLY thing business logic calls:

    checker = SparkChecker(config)
    result: SparkResult = checker.check_code(code)   # -> UnifiedStatus

Spark (api.pubgredeemerbot.com) is an ASYNCHRONOUS job API:

    1. POST /v1/jobs/check-code   {"codes": ["<code>"]}      -> {job_id, status}
    2. GET  /v1/jobs/{job_id}?wait=25                        -> poll until
                                                               status done/failed
    3. parse the finished job's result row -> UnifiedStatus

Auth is the ``X-API-Key`` header. Transport failures map to the plugin's error
taxonomy:

* network / timeout / 429 / 5xx  -> SparkTemporaryError (retryable)
* 401 / 403 (auth / plan)         -> SparkCriticalError
* understood job result           -> SparkResult

Mock mode (``config.spark_mock``) needs no network and drives deterministic
behaviour from magic substrings in the code - used by tests and local dev.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from ..errors import SparkCriticalError, SparkTemporaryError
from ..utils.logger import get_logger, mask_code
from . import parser
from .models import SparkResult, UnifiedStatus

log = get_logger("spark.client")

try:  # requests is a FunPayCardinal dependency; keep import optional for tests
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

# Job lifecycle states (from the API docs).
_DONE = {"done"}
_FAILED = {"failed"}
_PENDING = {"pending", "running", "queued", ""}


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
    # Real HTTP transport (async job API).
    # ------------------------------------------------------------------ #
    def _check_http(self, code: str) -> SparkResult:
        if requests is None:  # pragma: no cover
            raise SparkCriticalError("requests is not installed")
        if not self.cfg.spark_api_url:
            raise SparkCriticalError("SPARK_API_URL is not configured")
        if not self.cfg.spark_api_key:
            raise SparkCriticalError("SPARK_API_KEY is not configured")

        job = self._create_job(code)
        job_id = self._extract_job_id(job)
        if not job_id:
            # Some deployments may answer synchronously with the result inline.
            result = parser.parse_job(job, http_status=200)
            if result.status is UnifiedStatus.UNKNOWN:
                raise SparkCriticalError(f"No job_id in Spark response: keys={list(job.keys())}")
            return result
        return self._poll_job(job_id)

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": self.cfg.spark_api_key,
        }

    def _create_job(self, code: str) -> Dict[str, Any]:
        try:
            resp = requests.post(
                self.cfg.spark_check_code_url(),
                json={"codes": [code]},
                headers=self._headers(),
                timeout=self.cfg.spark_timeout,
            )
        except Exception as exc:
            raise SparkTemporaryError(f"Spark POST failed: {exc}") from exc
        self._raise_for_transport(resp)
        return self._json(resp)

    def _poll_job(self, job_id: str) -> SparkResult:
        deadline = time.monotonic() + self.cfg.spark_max_wait
        while True:
            try:
                resp = requests.get(
                    self.cfg.spark_job_url(job_id),
                    params={"wait": self.cfg.spark_job_wait},
                    headers=self._headers(),
                    timeout=self.cfg.spark_timeout + self.cfg.spark_job_wait,
                )
            except Exception as exc:
                raise SparkTemporaryError(f"Spark job poll failed: {exc}") from exc
            self._raise_for_transport(resp)
            job = self._json(resp)
            status = str(job.get("status") or job.get("state") or "").lower()

            if status in _DONE:
                result = parser.parse_job(job, http_status=resp.status_code)
                if result.status is UnifiedStatus.UNKNOWN:
                    raise SparkCriticalError(
                        f"Unrecognised finished-job result: keys={list(job.keys())}"
                    )
                return result
            if status in _FAILED:
                # A failed job is a Spark-side failure. Treat as temporary so it
                # can be retried; parser may still classify a code-level reason.
                result = parser.parse_job(job, http_status=resp.status_code)
                if result.is_final_negative:
                    return result
                raise SparkTemporaryError(f"Spark job failed: {job.get('error') or job}")

            # Still pending/running. The long-poll already waited; loop unless
            # we've exhausted the overall budget.
            if time.monotonic() >= deadline:
                raise SparkTemporaryError(f"Spark job {job_id} did not finish in time")

    # ------------------------------------------------------------------ #
    def _raise_for_transport(self, resp) -> None:
        code = resp.status_code
        if code in (401, 403):
            raise SparkCriticalError(f"Spark auth/plan error HTTP {code}")
        if code == 429 or code >= 500:
            raise SparkTemporaryError(f"Spark HTTP {code}")
        if code == 404:
            raise SparkCriticalError(f"Spark job not found HTTP {code}")

    @staticmethod
    def _json(resp) -> Dict[str, Any]:
        try:
            return resp.json()
        except ValueError as exc:
            raise SparkCriticalError(f"Spark returned non-JSON body: {exc}") from exc

    @staticmethod
    def _extract_job_id(job: Dict[str, Any]) -> Optional[str]:
        for key in ("job_id", "id", "_id", "jobId"):
            val = job.get(key)
            if val:
                return str(val)
        return None

    # ------------------------------------------------------------------ #
    # Deterministic mock (no network). Magic substrings drive the outcome.
    # ------------------------------------------------------------------ #
    def _check_mock(self, code: str) -> SparkResult:
        up = code.upper()
        if "NOACC" in up:
            row = {"code": code, "status": "error", "message": "account does not exist"}
        elif "USED" in up:
            row = {"code": code, "status": "used", "message": "code already used"}
        elif "TEMP" in up or "ERR500" in up:
            raise SparkTemporaryError("Mock temporary error")
        elif "CRIT" in up:
            raise SparkCriticalError("Mock critical error")
        elif "WEIRD" in up:
            row = {"code": code, "foo": "bar"}  # -> UNKNOWN -> critical
        elif "GOOD" in up or "VALID" in up:
            row = {"code": code, "valid": True, "message": "redeemable"}
        else:
            row = {"code": code, "valid": False, "message": "invalid code"}

        job = {"status": "done", "result": {"results": [row]}}
        result = parser.parse_job(job, http_status=200)
        if result.status is UnifiedStatus.UNKNOWN:
            raise SparkCriticalError("Unrecognised (mock) Spark response")
        return result
