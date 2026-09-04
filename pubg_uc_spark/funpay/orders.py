"""FunPay order parsing & lot resolution (task spec, sections 2 & 8).

Turns a FunPayCardinal ``NewOrderEvent`` into our :class:`OrderRecord`. Lot
matching is by ``lot_id`` (per the chosen strategy): we fetch the full order
and resolve its offer id, then compare against the configured ``LOTS``.

IMPORTANT / INTEGRATION NOTE:
FunPay's order object does not always expose the originating offer (lot) id in a
single stable attribute across FunPayAPI versions. :func:`resolve_lot_id` tries
the common locations and finally scans the order description/html for an
``offer?id=`` link. Verify this against the FunPayAPI version installed in the
target FPC before enabling in production; it is the one spot to adjust.
"""

from __future__ import annotations

import re
from typing import Optional

from ..database.models import OrderRecord
from ..utils.logger import get_logger

log = get_logger("funpay.orders")

_OFFER_ID_RE = re.compile(r"offer\?id=(\d+)")


def resolve_lot_id(full_order, order_shortcut) -> Optional[str]:
    """Best-effort extraction of the FunPay lot/offer id from an order."""
    for obj in (full_order, order_shortcut):
        if obj is None:
            continue
        for attr in ("lot_id", "offer_id", "subcategory_id"):
            val = getattr(obj, attr, None)
            if val:
                return str(val)
        # Scan any textual description for an offer link.
        for attr in ("full_description", "description", "html", "title"):
            text = getattr(obj, attr, None)
            if isinstance(text, str):
                m = _OFFER_ID_RE.search(text)
                if m:
                    return m.group(1)
    return None


def build_order_record(cardinal, order_shortcut) -> Optional[OrderRecord]:
    """Build an :class:`OrderRecord` from a NewOrderEvent's order shortcut.

    Returns ``None`` if the lot id cannot be resolved (caller then skips it).
    """
    funpay_order_id = str(getattr(order_shortcut, "id", "") or "")
    full_order = None
    try:
        account = getattr(cardinal, "account", None)
        if account is not None and funpay_order_id:
            full_order = account.get_order(funpay_order_id)
    except Exception:
        log.exception("Could not fetch full order %s", funpay_order_id)

    lot_id = resolve_lot_id(full_order, order_shortcut)

    def pick(*attrs, default=""):
        for src in (full_order, order_shortcut):
            for a in attrs:
                v = getattr(src, a, None) if src is not None else None
                if v not in (None, ""):
                    return v
        return default

    buyer_id = str(pick("buyer_id"))
    buyer_username = str(pick("buyer_username", "buyer_name"))
    quantity = pick("amount", "quantity", default=1)
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        quantity = 1
    chat_id = str(pick("chat_id"))

    return OrderRecord(
        funpay_order_id=funpay_order_id,
        lot_id=str(lot_id) if lot_id else "",
        buyer_id=buyer_id,
        buyer_username=buyer_username,
        quantity=quantity,
        chat_id=chat_id,
    )
