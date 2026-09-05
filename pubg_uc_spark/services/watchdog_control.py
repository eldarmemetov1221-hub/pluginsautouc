"""Watchdog control file: lets the admin steer the external watchdog at runtime.

The watchdog (tools/watchdog.sh) runs from cron and reads this file on every
tick, so the admin can enable/disable it and change the "silence" threshold from
Telegram without editing crontab or the script. The plugin never restarts
anything itself - it only writes this file; the shell watchdog acts on it.

Format is simple ``KEY=VALUE`` lines so the shell script can parse it trivially::

    WATCHDOG_ENABLED=1
    STALL_MINUTES=15
"""

from __future__ import annotations

import os
from typing import Dict

from ..utils.logger import get_logger

log = get_logger("watchdog")


class WatchdogControl:
    def __init__(self, config):
        self.cfg = config
        self.path = getattr(config, "watchdog_control_file", "") or ""

    # ------------------------------------------------------------------ #
    def _defaults(self) -> Dict:
        return {
            "enabled": True,
            "stall_minutes": int(getattr(self.cfg, "watchdog_stall_minutes", 15)),
        }

    def read(self) -> Dict:
        """Current settings (file values merged over defaults)."""
        data = self._defaults()
        if not self.path or not os.path.isfile(self.path):
            return data
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key, val = key.strip().upper(), val.strip()
                    if key == "WATCHDOG_ENABLED":
                        data["enabled"] = val not in ("0", "false", "no", "off", "")
                    elif key == "STALL_MINUTES":
                        try:
                            data["stall_minutes"] = max(1, int(val))
                        except ValueError:
                            pass
        except OSError:
            log.warning("Could not read watchdog control file %s", self.path)
        return data

    def _write(self, data: Dict) -> bool:
        if not self.path:
            return False
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(
                    "# Managed by /uc_watchdog - read by tools/watchdog.sh\n"
                    f"WATCHDOG_ENABLED={'1' if data['enabled'] else '0'}\n"
                    f"STALL_MINUTES={int(data['stall_minutes'])}\n"
                )
            os.replace(tmp, self.path)
            return True
        except OSError:
            log.exception("Could not write watchdog control file %s", self.path)
            return False

    def ensure_file(self) -> None:
        """Create the control file with defaults if it does not exist yet."""
        if self.path and not os.path.exists(self.path):
            self._write(self._defaults())

    def set_enabled(self, enabled: bool) -> bool:
        data = self.read()
        data["enabled"] = bool(enabled)
        return self._write(data)

    def set_stall_minutes(self, minutes: int) -> bool:
        data = self.read()
        data["stall_minutes"] = max(1, int(minutes))
        return self._write(data)
