"""FazerChecker: a Spark-compatible delivery provider backed by FazerCards.

Exposes the same ``redeem(player_id, picks, idempotency_prefix)`` surface as
:class:`SparkChecker`, returning a :class:`SparkResult`, so the order pipeline
uses it interchangeably. A lot's picks (base-pack combination) are fulfilled as
one FazerCards top-up order per pack, so combo lots place several orders:

    picks {"8100": 1, "325": 1}  ->  order(8100) + order(325)

Idempotency: each sub-order uses a deterministic key derived from the prefix
(the plugin passes the code id), so a retry never double-tops-up. The aggregate
is VALID only if every sub-order succeeds; some-but-not-all -> PARTIAL.
"""

from __future__ import annotations

import time
from typing import Dict

from ..errors import SparkCriticalError, SparkTemporaryError
from ..spark.models import SparkResult, UnifiedStatus
from ..utils.logger import get_logger, mask_code
from .client import FazerClient

log = get_logger("fazer.checker")


class FazerChecker:
    def __init__(self, config, client: FazerClient = None):
        self.cfg = config
        self.client = client or FazerClient(config)

    # ------------------------------------------------------------------ #
    def redeem(self, player_id: str, picks: Dict[str, int],
               idempotency_prefix: str = "") -> SparkResult:
        picks = {str(k): int(v) for k, v in (picks or {}).items() if int(v) > 0}
        total = sum(picks.values()) or 1
        log.info("[Fazer] Top-up picks=%s uid=%s (total=%s)", picks, mask_code(player_id), total)

        # Validate the UID once up front (cheap, avoids spending on a bad id).
        # Validation is best-effort: if it is unsupported for this category, or
        # errors in a non-transient way, proceed to order anyway (the order
        # itself rejects a bad UID). Only an explicit valid=False stops us.
        if getattr(self.cfg, "fazer_validate_id", True):
            try:
                v = self.client.validate_id(player_id)
            except SparkTemporaryError:
                raise
            except Exception as exc:
                log.warning("[Fazer] validate-id ошибка, продолжаю без неё: %s", exc)
                v = {"valid": True, "supported": False}
            if v.get("supported", True) and not v.get("valid"):
                return SparkResult(status=UnifiedStatus.ACCOUNT_NOT_FOUND,
                                   message="account_not_found", total=total)

        prefix = idempotency_prefix or f"uid{player_id}"
        delivered = 0
        player_name = ""
        seq = 0
        bad_player = False
        last_err = ""
        for denom, count in picks.items():
            offer_id = self.client.resolve_offer_id(denom)
            for _ in range(int(count)):
                seq += 1
                idem = f"{prefix}:{denom}:{seq}"
                order = self._place_and_wait(offer_id, player_id, idem)
                if order.get("_ok"):
                    delivered += 1
                    player_name = player_name or order.get("_player_name", "")
                elif order.get("_bad_player"):
                    bad_player = True
                    last_err = order.get("_msg", "invalid player")
                    break
                else:
                    last_err = order.get("_msg", "failed")
            if bad_player:
                break

        if delivered == total:
            return SparkResult(status=UnifiedStatus.VALID, player_name=player_name,
                               delivered=delivered, total=total, message="ok")
        if delivered == 0:
            if bad_player:
                return SparkResult(status=UnifiedStatus.ACCOUNT_NOT_FOUND,
                                   message="account_not_found", total=total)
            return SparkResult(status=UnifiedStatus.ERROR, message=last_err or "topup failed",
                               delivered=0, total=total)
        # Partial delivery of a multi-pack order.
        return SparkResult(status=UnifiedStatus.PARTIAL, player_name=player_name,
                           delivered=delivered, total=total,
                           message=f"partial {delivered}/{total}")

    # ------------------------------------------------------------------ #
    def _place_and_wait(self, offer_id: str, player_id: str, idem: str) -> dict:
        """Create one top-up order and poll it to a terminal state.

        Returns {_ok|_bad_player, _player_name, _msg}. Raises SparkTemporaryError
        for transport issues (so the retry layer can act), like SparkChecker."""
        created = self.client.create_order(offer_id, player_id, "", idem)
        order_id = created.get("order_id")
        order = created.get("order") or {}
        # Some deployments return a terminal order inline.
        if self.client.is_ok(order):
            return {"_ok": True, "_player_name": self.client.player_name_of(order)}
        if self.client.is_bad_player(order):
            return {"_bad_player": True, "_msg": "invalid player"}
        if self.client.is_failed(order):
            return {"_ok": False, "_msg": str(order.get("message") or order.get("error") or "failed")}
        if not order_id:
            raise SparkCriticalError("FazerCards: order has no id and is not terminal")

        attempts = max(1, int(self.cfg.fazer_poll_attempts))
        for i in range(attempts):
            o = self.client.order_status(order_id)
            if self.client.is_ok(o):
                return {"_ok": True, "_player_name": self.client.player_name_of(o)}
            if self.client.is_bad_player(o):
                return {"_bad_player": True, "_msg": "invalid player"}
            if self.client.is_failed(o):
                return {"_ok": False, "_msg": str(o.get("message") or o.get("error") or "failed")}
            if i < attempts - 1:
                time.sleep(self.cfg.fazer_poll_interval)
        # Not terminal within budget -> transient, let the caller decide/retry.
        raise SparkTemporaryError(f"FazerCards order {order_id} did not finish in time")

    # ------------------------------------------------------------------ #
    def stock_summary(self):  # parity with SparkChecker; FazerCards has no stock API
        raise SparkCriticalError("FazerCards не предоставляет остатки стока")
