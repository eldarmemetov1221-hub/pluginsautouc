"""Retry service: runs Spark checks off the FunPay listener thread.

Responsibilities (task spec, sections 12 & 21):

* never block the FunPay listener - checks run on a worker thread;
* retry only *temporary* errors, with bounded attempts and backoff;
* hand every terminal outcome to a result handler (order_service).

Design:

    enqueue(code_id)  ->  worker thread  ->  SparkChecker.check_code()
                                          ->  result_handler(code_id, result, error, attempts)

Set ``async_mode=False`` (and inject ``sleep_fn``) to run inline in tests.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

from ..errors import CriticalError, SparkCriticalError, SparkTemporaryError
from ..spark.models import SparkResult
from ..utils.logger import get_logger

log = get_logger("retry")

ResultHandler = Callable[[int, Optional[SparkResult], Optional[Exception], int], None]


class RetryService:
    def __init__(
        self,
        config,
        check_fn: Callable[[int], "SparkResult"],
        result_handler: ResultHandler,
        *,
        async_mode: bool = True,
        sleep_fn: Callable[[float], None] | None = None,
    ):
        self.cfg = config
        # check_fn(code_id) performs the actual Spark redeem and returns a
        # SparkResult, or raises SparkTemporaryError / SparkCriticalError.
        self.check_fn = check_fn
        self.result_handler = result_handler
        self.async_mode = async_mode
        import time

        self._sleep = sleep_fn or time.sleep
        self._queue: "queue.Queue[Optional[int]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if not self.async_mode or self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._worker, name="pubg-uc-spark-retry", daemon=True
        )
        self._thread.start()
        log.info("Retry worker started")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._queue.put(None)  # wake the worker so it can exit

    def enqueue(self, code_id: int) -> None:
        if self.async_mode:
            self._queue.put(code_id)
        else:  # inline (tests)
            self._process(code_id)

    # ------------------------------------------------------------------ #
    def _worker(self) -> None:
        while self._running:
            code_id = self._queue.get()
            if code_id is None:
                break
            try:
                self._process(code_id)
            except Exception:  # never let the worker die on one bad job
                log.exception("Unhandled error processing code_id=%s", code_id)

    def _process(self, code_id: int) -> None:
        """Run the check with retry for temporary errors."""
        max_attempts = max(1, self.cfg.max_retries)
        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                result = self.check_fn(code_id)
                self.result_handler(code_id, result, None, attempt)
                return
            except SparkTemporaryError as exc:
                last_error = exc
                log.warning(
                    "[Spark] Temporary error (attempt %s/%s) code_id=%s: %s",
                    attempt,
                    max_attempts,
                    code_id,
                    exc,
                )
                if attempt < max_attempts:
                    delay = self.cfg.retry_delay * (self.cfg.retry_backoff ** (attempt - 1))
                    self._sleep(delay)
                    continue
            except SparkCriticalError as exc:
                log.error("[Spark] Critical error code_id=%s: %s", code_id, exc)
                self.result_handler(code_id, None, exc, attempt)
                return
            except Exception as exc:  # unknown -> critical, no retry
                log.exception("[Spark] Unexpected error code_id=%s", code_id)
                self.result_handler(code_id, None, CriticalError(str(exc)), attempt)
                return

        # Retries exhausted on a temporary error.
        self.result_handler(code_id, None, last_error, max_attempts)
