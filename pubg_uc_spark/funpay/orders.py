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
    # This plugin ONLY fulfils "Пополнение по ID" lots. Never match a code /
    # gift-code lot ("Пополнение кодом"), even if the UC amount and "uc" line up
    # - otherwise a code order whose buyer sends a UID would be wrongly redeemed.
    if any(w in desc for w in ("код", "code", "промокод")):
        return False
    if lot.keywords:
        return all(str(k).lower() in desc for k in lot.keywords)
    # Default: the advertised UC as a WHOLE number (so "60" != "660") + "uc",
    # and the description must mark it as an ID top-up ("... по ID").
    uc = str(lot.uc)
    if uc and not re.search(rf"(?<!\d){re.escape(uc)}(?!\d)", desc):
        return False
    return "uc" in desc and "id" in desc


def get_lot_amounts(cardinal, lot_ids) -> dict:
    """Return {lot_id: amount} = FunPay "Наличие" for each offer id.

    Reads the seller's own lot fields via FunPayAPI (account.get_lot_fields).
    A lot whose amount can't be read (error / unlimited / no field) maps to None.
    Fully duck-typed and defensive: one failing lot never breaks the rest.
    """
    out: dict = {}
    account = getattr(cardinal, "account", None)
    getter = getattr(account, "get_lot_fields", None)
    for lid in lot_ids:
        amt = None
        if callable(getter):
            try:
                key = int(lid) if str(lid).isdigit() else lid
                lf = getter(key)
                amt = getattr(lf, "amount", None)
                if amt is None:
                    fields = getattr(lf, "fields", None)
                    if isinstance(fields, dict):
                        raw = fields.get("amount")
                        amt = int(raw) if str(raw).strip().isdigit() else None
                elif not isinstance(amt, int):
                    amt = int(amt) if str(amt).strip().isdigit() else None
            except Exception:
                log.debug("get_lot_fields failed for %s", lid, exc_info=True)
                amt = None
        out[str(lid)] = amt
    return out


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

    # Paid price (RUB) for finance stats. FunPay's OrderShortcut carries the
    # order total; be tolerant of it being a number or a string like "123,45 ₽".
    raw_price = getattr(order_shortcut, "price", None)
    price = 0.0
    if raw_price is not None:
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            m = re.search(r"[0-9]+(?:[.,][0-9]+)?", str(raw_price))
            price = float(m.group(0).replace(",", ".")) if m else 0.0

    return OrderRecord(
        funpay_order_id=str(g("id")),
        lot_id=lot.lot_id,
        buyer_id=str(g("buyer_id")),
        buyer_username=str(g("buyer_username")),
        quantity=amount,
        chat_id=str(g("chat_id")),
        price=price,
    )
