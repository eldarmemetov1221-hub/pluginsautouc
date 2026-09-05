"""Liveness heartbeat: proof the FunPay event runner is still delivering events.

Why
---
The failure mode we hit was not a crash: Cardinal's process stayed alive (its
screen session too), but the FunPay poll thread blocked on a half-open socket
after a network flap - it stopped delivering events (no new orders, no
messages) and logged nothing. From outside, ``pgrep`` sees a healthy process.
So process-liveness is not enough; we need *runner*-liveness.

How
---
The plugin bumps this file at startup and on **every** FunPay runner event
(NEW_MESSAGE / NEW_ORDER - even ones we ignore), so a fresh timestamp means the
runner is still polling FunPay. An external watchdog reads it: if the file has
not been updated for longer than a threshold while the process is alive, the
runner has stalled and the process must be restarted (see tools/watchdog.sh).

The file content is a single integer - Unix epoch seconds of the last beat - so
it is trivial to parse from a shell script.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from .logger import get_logger

log = get_logger("heartbeat")


class Heartbeat:
    def __init__(self, path: str, min_interval: float = 1.0):
        self.path = path or ""
        # Coalesce bursts (many events in the same second) into one disk write.
        self._min_interval = max(0.0, min_interval)
        self._last = 0.0

    def beat(self, force: bool = False) -> None:
        if not self.path:
            return
        now = time.time()
        if not force and (now - self._last) < self._min_interval:
            return
        self._last = now
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(str(int(now)))
            os.replace(tmp, self.path)  # atomic
        except OSError:
            log.debug("heartbeat write failed for %s", self.path)

    def age(self) -> Optional[float]:
        """Seconds since the last beat, or None if unreadable/missing."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                ts = int((fh.read() or "0").strip())
            return max(0.0, time.time() - ts)
        except (OSError, ValueError):
            return None
