"""SparkChecker: the redeem adapter (task spec, sections 3, 4, 11).

Flow (per the buyer sending their PUBG UID): the bot redeems UC from Spark
stock onto the buyer's account. Public contract:

    checker = SparkChecker(config)
    result: SparkResult = checker.redeem(player_id, picks)   # -> UnifiedStatus

Spark (api.pubgredeemerbot.com) is an ASYNCHRONOUS job API:

    1. POST /v1/jobs/stock-redeem  {"player_id": uid, "picks": {"60": 1}}
                                                              -> {job_id, status}
    2. GET  /v1/jobs/{job_id}?wait=25   -> poll until status done/failed
    3. parse the finished job's result -> UnifiedStatus

Auth is the ``X-API-Key`` header. Transport failures map to the plugin's error
taxonomy:

* network / timeout / 429 / 5xx  -> SparkTemporaryError (retryable)
* 401 / 403 (auth / plan)         -> SparkCriticalError
* understood job result           -> SparkResult

Mock mode (``config.spark_mock``) needs no network and drives deterministic
behaviour from the leading digit of the UID - used by tests and local dev.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from ..config import SPARK_BASE_DENOMINATIONS
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


class SparkChecker:
    def __init__(self, config):
        self.cfg = config

    # ------------------------------------------------------------------ #
    def redeem(self, player_id: str, picks: Dict[str, int]) -> SparkResult:
        """Redeem ``picks`` (e.g. {"60": 1}) onto ``player_id``.

        Raises :class:`SparkTemporaryError` for retryable failures and
        :class:`SparkCriticalError` for unrecoverable ones.
        """
        log.info("[Spark] Redeem picks=%s uid=%s", picks, mask_code(player_id))
        if self.cfg.spark_mock:
            return self._redeem_mock(player_id, picks)
        return self._redeem_http(player_id, picks)

    # ------------------------------------------------------------------ #
    def stock_summary(self) -> Dict[str, int]:
        """Return current Spark stock as {denomination: available_count},
        e.g. {"60": 42, "325": 17, "660": 5}. Raises on transport/auth errors."""
        if self.cfg.spark_mock:
            return {d: 0 for d in SPARK_BASE_DENOMINATIONS}
        if requests is None:  # pragma: no cover
            raise SparkCriticalError("requests is not installed")
        if not self.cfg.spark_api_url:
            raise SparkCriticalError("SPARK_API_URL is not configured")
        url = f"{self.cfg.spark_api_url}/v1/stock/summary"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.cfg.spark_timeout)
        except Exception as exc:
            raise SparkTemporaryError(f"Spark stock request failed: {exc}") from exc
        self._raise_for_transport(resp)
        data = self._json(resp)
        raw = data.get("by_denomination_uc") or {}
        out: Dict[str, int] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                out[str(k)] = 0
        return out

    # ------------------------------------------------------------------ #
    # Real HTTP transport (async job API).
    # ------------------------------------------------------------------ #
    def _redeem_http(self, player_id: str, picks: Dict[str, int]) -> SparkResult:
        if requests is None:  # pragma: no cover
            raise SparkCriticalError("requests is not installed")
        if not self.cfg.spark_api_url:
            raise SparkCriticalError("SPARK_API_URL is not configured")
        if not self.cfg.spark_api_key:
            raise SparkCriticalError("SPARK_API_KEY is not configured")

        # Straight to stock-redeem - no separate lookup call (each request is
        # billed and quota-limited). Account-not-found is read from the redeem
        # result itself; the player name too, if the result carries it.
        job = self._post_job(
            self.cfg.spark_stock_redeem_url(),
            {"player_id": str(player_id), "picks": picks},
        )
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

    def _post_job(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = requests.post(
                url, json=payload, headers=self._headers(), timeout=self.cfg.spark_timeout
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
                # The job is ALREADY created on Spark's side; the redeem may well
                # have completed. A dropped/timed-out poll must NOT be reported as
                # a failure - re-poll the SAME job_id (an idempotent GET: no new
                # redeem, no double top-up) until the overall deadline. Only give
                # up if we keep failing past spark_max_wait.
                if time.monotonic() >= deadline:
                    raise SparkTemporaryError(f"Spark job poll failed: {exc}") from exc
                log.warning("[Spark] poll transport error, re-polling job %s: %s", job_id, exc)
                time.sleep(3)
                continue
            # 429 / 5xx while polling: transient too - keep re-polling the same
            # job until the deadline instead of aborting (again, no new redeem).
            if resp.status_code == 429 or resp.status_code >= 500:
                if time.monotonic() >= deadline:
                    raise SparkTemporaryError(f"Spark HTTP {resp.status_code}")
                log.warning("[Spark] poll HTTP %s, re-polling job %s", resp.status_code, job_id)
                time.sleep(3)
                continue
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
                # A failed job: if the parser recognises a definitive reason
                # (account not found / declined) surface it; otherwise retry.
                result = parser.parse_job(job, http_status=resp.status_code)
                if result.is_final_negative or result.status is UnifiedStatus.ERROR:
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
    # Deterministic mock (no network). Leading digit of the UID drives it.
    # ------------------------------------------------------------------ #
    def _redeem_mock(self, player_id: str, picks: Dict[str, int]) -> SparkResult:
        head = (player_id or "0")[0]
        if head == "2":
            # mirror the real Spark error shape: {"detail": {"error": CODE}}
            row = {"detail": {"error": "INVALID_PLAYER_ID", "message": "Invalid player identifier."}}
        elif head == "3":
            raise SparkTemporaryError("Mock temporary error")
        elif head == "4":
            raise SparkCriticalError("Mock critical error")
        elif head == "5":
            row = {"player_id": player_id, "status": "error", "message": "out of stock"}
        elif head == "6":
            row = {"player_id": player_id, "foo": "bar"}  # -> UNKNOWN -> critical
        elif head == "7":
            row = {"player_id": player_id, "success": False, "message": "redeem declined"}
        else:
            row = {
                "player_id": player_id,
                "success": True,
                "message": "redeemed successfully",
                "charac_name": "MockPlayer",
            }

        n = sum(int(v) for v in picks.values()) or 1
        if head == "8" and n > 1:
            # partial: first pack ok, the rest out of stock
            ok_row = {"player_id": player_id, "success": True, "charac_name": "MockPlayer"}
            fail_row = {"success": False, "err_code": "OUT_OF_STOCK"}
            rows = [ok_row] + [fail_row] * (n - 1)
        else:
            rows = [row] * n

        job = {"status": "done", "result": {"results": rows}}
        result = parser.parse_job(job, http_status=200)
        if result.status is UnifiedStatus.UNKNOWN:
            raise SparkCriticalError("Unrecognised (mock) Spark response")
        return result
