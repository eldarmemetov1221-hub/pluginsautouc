"""FunPay order parsing & lot matching (task spec, sections 2 & 8).

Turns a FunPayCardinal ``NewOrderEvent.order`` (a ``FunPayAPI.types.OrderShortcut``)
into our :class:`OrderRecord`.

IMPORTANT (verified against sidor0912/FunPayAPI): a FunPay order object does NOT
expose the originating offer/lot id. ``OrderShortcut`` provides ``id``,
``description`` (the lot title), ``subcategory`` / ``subcategory_name``,
``buyer_id``, ``buyer_username``, ``amount``, ``chat_id``. So a lot is matched by
its **description** (configurable keywords), not by the numeric lot id. The
configured ``lot_id`` is kept only as our own identifier/label for the matched
lot.
"""

from __future__ import annotations

import re
from typing import Optional

from ..config import Config, LotConfig
from ..database.models import OrderRecord
from ..utils.logger import get_logger

log = get_logger("funpay.orders")


def _matches(lot: LotConfig, description: str) -> bool:
    desc = (description or "").lower()
    if not desc:
        return False
    if lot.keywords:
        return all(str(k).lower() in desc for k in lot.keywords)
    # Default: the advertised UC as a WHOLE number (so "60" != "660") + "uc".
    uc = str(lot.uc)
    if uc and not re.search(rf"(?<!\d){re.escape(uc)}(?!\d)", desc):
        return False
    return "uc" in desc


def match_lot(cfg: Config, order_shortcut) -> Optional[LotConfig]:
    """Return the configured lot whose description keywords match, else None."""
    description = getattr(order_shortcut, "description", "") or ""
    for lot in cfg.lots.values():
        if _matches(lot, description):
            return lot
    return None


def build_order_record(order_shortcut, lot: LotConfig) -> OrderRecord:
    """Build an :class:`OrderRecord` from the NewOrderEvent's order shortcut."""

    def g(attr, default=""):
        v = getattr(order_shortcut, attr, None)
        return v if v not in (None, "") else default

    amount = g("amount", 1)
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 1

    return OrderRecord(
        funpay_order_id=str(g("id")),
        lot_id=lot.lot_id,
        buyer_id=str(g("buyer_id")),
        buyer_username=str(g("buyer_username")),
        quantity=amount,
        chat_id=str(g("chat_id")),
    )
