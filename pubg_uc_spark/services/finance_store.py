"""Runtime-editable finance settings (Spark pack costs + FunPay commission).

Edits from the Telegram /uc_prices menu are applied to the live Config AND
persisted to a JSON file, so they take effect immediately and survive restarts.
On startup the file (if present) overrides the env-seeded defaults; if absent it
is seeded from the current config so it becomes editable.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional

from ..utils.logger import get_logger

log = get_logger("finance")


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class FinanceStore:
    def __init__(self, config):
        self.cfg = config
        self.path = getattr(config, "finance_config_file", "") or ""

    # ------------------------------------------------------------------ #
    def load_into_cfg(self) -> None:
        """Apply persisted settings to cfg; seed the file if it doesn't exist."""
        data = self._read()
        if data is None:
            self._write(self._current())   # first run -> create editable file
            return
        c = _num(data.get("commission_percent"))
        if c is not None:
            self.cfg.commission_percent = c
        pc = data.get("pack_costs")
        if isinstance(pc, dict):
            costs: Dict[str, float] = {}
            for k, v in pc.items():
                n = _num(v)
                if n is not None:
                    costs[str(k)] = n
            self.cfg.pack_costs = costs
        # Only override the env default when the key is actually present as a
        # bool (older finance.json files won't have it -> keep cfg's default).
        ad = data.get("auto_delivery")
        if isinstance(ad, bool):
            self.cfg.auto_delivery = ad
        fr = data.get("finance_reset_at")
        if isinstance(fr, str):
            self.cfg.finance_reset_at = fr
        dp = data.get("delivery_provider")
        if isinstance(dp, str) and dp:
            self.cfg.delivery_provider = dp
        bg = data.get("balance_guard_enabled")
        if isinstance(bg, bool):
            self.cfg.balance_guard_enabled = bg
        bgm = data.get("balance_guard_mode")
        if isinstance(bgm, str) and bgm:
            self.cfg.balance_guard_mode = bgm
        log.info("Finance settings loaded (commission=%s%%, packs=%s, auto_delivery=%s, "
                 "provider=%s, reset_at=%s)",
                 self.cfg.commission_percent, self.cfg.pack_costs,
                 getattr(self.cfg, "auto_delivery", True),
                 getattr(self.cfg, "delivery_provider", "spark"),
                 getattr(self.cfg, "finance_reset_at", "") or "-")

    # ------------------------------------------------------------------ #
    def set_pack_cost(self, denom: str, value: float) -> bool:
        # cfg.pack_costs may be shared; copy-modify to be safe.
        costs = dict(self.cfg.pack_costs)
        costs[str(denom)] = float(value)
        self.cfg.pack_costs = costs
        return self._write(self._current())

    def set_commission(self, pct: float) -> bool:
        self.cfg.commission_percent = float(pct)
        return self._write(self._current())

    def set_auto_delivery(self, enabled: bool) -> bool:
        """Flip the master auto-redeem switch and persist it."""
        self.cfg.auto_delivery = bool(enabled)
        return self._write(self._current())

    def set_finance_reset(self, iso_ts: str) -> bool:
        """Set (or clear, with "") the statistics epoch and persist it."""
        self.cfg.finance_reset_at = iso_ts or ""
        return self._write(self._current())

    def set_delivery_provider(self, provider: str) -> bool:
        """Switch the active delivery provider ('spark'|'fazercards') and persist."""
        p = str(provider or "spark").lower()
        self.cfg.delivery_provider = "fazercards" if p in ("fazer", "fazercards", "fzr") else "spark"
        return self._write(self._current())

    def set_balance_guard(self, enabled: bool = None, mode: str = None) -> bool:
        """Toggle the balance guard and/or its mode ('monitor'|'enforce'); persist."""
        if enabled is not None:
            self.cfg.balance_guard_enabled = bool(enabled)
        if mode is not None:
            m = str(mode).lower()
            self.cfg.balance_guard_mode = "enforce" if m == "enforce" else "monitor"
        return self._write(self._current())

    # ------------------------------------------------------------------ #
    def _current(self) -> Dict:
        return {
            "commission_percent": self.cfg.commission_percent,
            "pack_costs": dict(self.cfg.pack_costs),
            "auto_delivery": bool(getattr(self.cfg, "auto_delivery", True)),
            "delivery_provider": getattr(self.cfg, "delivery_provider", "spark"),
            "balance_guard_enabled": bool(getattr(self.cfg, "balance_guard_enabled", False)),
            "balance_guard_mode": getattr(self.cfg, "balance_guard_mode", "monitor"),
            "finance_reset_at": getattr(self.cfg, "finance_reset_at", "") or "",
        }

    def _read(self) -> Optional[Dict]:
        if not self.path or not os.path.isfile(self.path):
            return None
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            log.warning("Could not read finance file %s", self.path)
            return None

    def _write(self, data: Dict) -> bool:
        if not self.path:
            return False
        try:
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return True
        except OSError:
            log.exception("Could not write finance file %s", self.path)
            return False
