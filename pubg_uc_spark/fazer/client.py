"""FazerCards API adapter (https://api.fzr.cards) - alternative top-up provider.

FazerCards credits UC directly to a player's UID (like Spark), via a per-offer
top-up order. Auth is a single ``X-API-Key`` header. Response envelope:
``{"ok": true, ...}``. All endpoints require the key.

Top-up flow:
    GET  /api/v2/topups                         -> games (category_id, name)
    GET  /api/v2/topups/offers?category_id=..    -> offers (offer_id, name, price_usd) + fields
    POST /api/v2/topups/validate-id {category_id, fields}   -> player_name/region
    POST /api/v2/topups/order {category_id, offer_id, fields} (+ idempotency-key header)
    GET  /api/v2/orders/{orderId}                -> order status + delivery

The order object is free-form in the spec, so status/delivery parsing is
defensive and the raw body is logged on the first orders so the exact fields
can be confirmed. Mock mode (``fazer_mock``) needs no network.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..errors import SparkCriticalError, SparkTemporaryError
from ..utils.logger import get_logger, mask_code

log = get_logger("fazer")

try:  # requests is a FunPayCardinal dependency; optional for tests/mock
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

# Order statuses (defensive; confirmed/extended from real responses).
_OK = {"completed", "complete", "done", "delivered", "success", "fulfilled", "paid_out"}
_FAIL = {"failed", "error", "cancelled", "canceled", "rejected", "declined",
         "refunded", "refund", "void", "voided", "expired", "timeout"}
_PENDING = {"pending", "processing", "in_progress", "queued", "new", "created", "paid", "accepted"}

_INVALID_UID = re.compile(r"(invalid|not\s*found|wrong|unknown).*(id|player|uid|account)"
                          r"|(id|player|uid|account).*(invalid|not\s*found|wrong|unknown)", re.I)


class FazerClient:
    def __init__(self, config):
        self.cfg = config
        self._category_id: Optional[str] = None
        self._offers_cache: Optional[List[dict]] = None
        self._offer_by_denom: Dict[str, str] = dict(getattr(config, "fazer_offers", {}) or {})
        self._fields_cache: Optional[List[dict]] = None

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    def _headers(self, extra: dict = None) -> Dict[str, str]:
        h = {"Accept": "application/json", "X-API-Key": self.cfg.fazer_api_key}
        if extra:
            h.update(extra)
        return h

    def _req(self, method: str, url: str, *, params=None, json_body=None, headers=None) -> Dict[str, Any]:
        if requests is None:  # pragma: no cover
            raise SparkCriticalError("requests is not installed")
        if not self.cfg.fazer_api_key:
            raise SparkCriticalError("FAZER_API_KEY is not configured")
        try:
            resp = requests.request(
                method, url, params=params, json=json_body,
                headers=self._headers(headers), timeout=self.cfg.fazer_timeout)
        except Exception as exc:
            raise SparkTemporaryError(f"FazerCards {method} failed: {exc}") from exc
        code = resp.status_code
        snippet = ""
        try:
            snippet = (resp.text or "").strip().replace("\n", " ")[:200]
        except Exception:
            pass
        if code in (401, 403):
            raise SparkCriticalError(f"FazerCards auth error HTTP {code}: {snippet}")
        if code == 429 or code >= 500:
            raise SparkTemporaryError(f"FazerCards HTTP {code}: {snippet}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise SparkCriticalError(f"FazerCards non-JSON HTTP {code}: {snippet or exc}") from exc
        if isinstance(body, dict) and body.get("ok") is False and code >= 400:
            # a definitive 4xx error body
            raise SparkCriticalError(f"FazerCards error HTTP {code}: {body.get('error') or snippet}")
        return body if isinstance(body, dict) else {"ok": True, "data": body}

    # ------------------------------------------------------------------ #
    # Catalogue resolution
    # ------------------------------------------------------------------ #
    def resolve_category_id(self) -> str:
        if self._category_id:
            return self._category_id
        if self.cfg.fazer_category_id:
            self._category_id = str(self.cfg.fazer_category_id)
            return self._category_id
        if self.cfg.fazer_mock:
            self._category_id = "mock-pubg"
            return self._category_id
        want = (self.cfg.fazer_category_name or "pubg").lower()
        items = self._paged(self.cfg.fazer_topups_url(), "items")
        best = None
        for it in items:
            name = str(it.get("name") or "").lower()
            cid = it.get("category_id")
            if not cid:
                continue
            if want in name:
                best = cid
                if "pubg" in name:
                    break
        if not best:
            names = ", ".join(str(it.get("name") or "?") for it in items[:20])
            raise SparkCriticalError(
                f"FazerCards: категория '{self.cfg.fazer_category_name}' не найдена "
                f"среди: {names}. Задай FAZER_CATEGORY_ID.")
        self._category_id = str(best)
        log.info("[Fazer] category_id=%s (%s)", self._category_id, self.cfg.fazer_category_name)
        return self._category_id

    def _load_offers(self) -> List[dict]:
        if self._offers_cache is not None:
            return self._offers_cache
        cid = self.resolve_category_id()
        body = self._req("GET", self.cfg.fazer_offers_url(), params={"category_id": cid})
        self._offers_cache = [o for o in (body.get("offers") or []) if isinstance(o, dict)]
        self._fields_cache = [f for f in (body.get("fields") or []) if isinstance(f, dict)]
        return self._offers_cache

    def required_field_keys(self) -> List[str]:
        """Keys the offer needs in `fields` (e.g. ['user_id'] or ['user_id','server_id'])."""
        if self.cfg.fazer_mock:
            return ["user_id"]
        if self._fields_cache is None:
            self._load_offers()
        keys = [str(f.get("key")) for f in (self._fields_cache or []) if f.get("key")]
        return keys or ["user_id"]

    def resolve_offer_id(self, denom: str) -> str:
        denom = str(denom)
        if denom in self._offer_by_denom:
            return self._offer_by_denom[denom]
        if self.cfg.fazer_mock:
            oid = f"mock-{denom}"
            self._offer_by_denom[denom] = oid
            return oid
        offers = self._load_offers()
        match = None
        for o in offers:
            name = str(o.get("name") or "")
            oid = o.get("offer_id")
            if not oid:
                continue
            if re.search(rf"(?<!\d){re.escape(denom)}(?!\d)", name):
                match = oid
                if "uc" in name.lower():
                    break
        if not match:
            names = ", ".join(str(o.get("name") or "?") for o in offers[:20])
            raise SparkCriticalError(
                f"FazerCards: оффер {denom} UC не найден. Доступно: {names}. "
                f"Задай FAZER_OFFERS.")
        self._offer_by_denom[denom] = str(match)
        log.info("[Fazer] %s UC -> offer_id=%s", denom, match)
        return str(match)

    # ------------------------------------------------------------------ #
    # Wallet / orders
    # ------------------------------------------------------------------ #
    def balance(self) -> Optional[float]:
        if self.cfg.fazer_mock:
            return 9999.0
        body = self._req("GET", self.cfg.fazer_balance_url())
        v = body.get("balance")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def validate_id(self, player_id: str, server_id: str = "") -> Dict[str, Any]:
        """Validate the player UID. Returns {'valid': bool, 'player_name': str}."""
        if self.cfg.fazer_mock:
            head = (player_id or "0")[0]
            return {"valid": head != "2", "player_name": "MockPlayer" if head != "2" else ""}
        cid = self.resolve_category_id()
        fields = self._build_fields(player_id, server_id)
        body = self._req("POST", self.cfg.fazer_validate_url(),
                         json_body={"category_id": cid, "fields": fields})
        return {"valid": bool(body.get("valid")),
                "player_name": body.get("player_name") or "",
                "region": body.get("region") or ""}

    def create_order(self, offer_id: str, player_id: str, server_id: str,
                     idempotency_key: str) -> Dict[str, Any]:
        """Place ONE top-up order. Returns {'order_id': str, 'order': {...}}."""
        if self.cfg.fazer_mock:
            return {"order_id": f"MOCK-{idempotency_key}", "order": {"status": "completed"}}
        cid = self.resolve_category_id()
        fields = self._build_fields(player_id, server_id)
        body = self._req("POST", self.cfg.fazer_order_url(),
                         json_body={"category_id": cid, "offer_id": str(offer_id), "fields": fields},
                         headers={"idempotency-key": idempotency_key})
        order = body.get("order") if isinstance(body.get("order"), dict) else body
        return {"order_id": str(order.get("order_id") or order.get("id") or ""), "order": order}

    def order_status(self, order_id: str) -> Dict[str, Any]:
        if self.cfg.fazer_mock:
            return {"status": "completed", "player_name": "MockPlayer"}
        body = self._req("GET", self.cfg.fazer_order_status_url(order_id))
        order = body.get("order") if isinstance(body.get("order"), dict) else body
        return order if isinstance(order, dict) else {}

    def order_status_raw(self, order_id: str) -> Dict[str, Any]:
        """Full raw order body (diagnostics) to confirm the delivery fields."""
        if self.cfg.fazer_mock:
            return {"ok": True, "order": {"status": "completed", "player_name": "MockPlayer"}}
        return self._req("GET", self.cfg.fazer_order_status_url(order_id))

    # ------------------------------------------------------------------ #
    # Status classification (defensive)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _status_of(order: dict) -> str:
        for k in ("status", "state", "order_status"):
            v = order.get(k)
            if v:
                return str(v).lower()
        return ""

    def is_ok(self, order: dict) -> bool:
        return self._status_of(order) in _OK

    def is_failed(self, order: dict) -> bool:
        return self._status_of(order) in _FAIL

    def is_bad_player(self, order: dict) -> bool:
        txt = " ".join(str(order.get(k) or "") for k in ("error", "message", "reason", "status"))
        return bool(_INVALID_UID.search(txt))

    @staticmethod
    def player_name_of(order: dict) -> str:
        for k in ("player_name", "playerName", "nickname", "nick", "character"):
            v = order.get(k)
            if v:
                return str(v)
        return ""

    # ------------------------------------------------------------------ #
    def diagnose(self) -> str:
        if self.cfg.fazer_mock:
            return "FazerCards в MOCK-режиме (FAZER_API_KEY пуст)."
        lines = [f"base: {self.cfg.fazer_api_url}",
                 f"key: {'задан' if self.cfg.fazer_api_key else 'ПУСТО'}",
                 f"category(env): {self.cfg.fazer_category_id or '—'}  "
                 f"offers(env): {self.cfg.fazer_offers or '—'}"]
        try:
            lines.append(f"balance: ${self.balance()}")
        except Exception as exc:
            lines.append(f"balance: {type(exc).__name__}: {exc}")
        try:
            cid = self.resolve_category_id()
            lines.append(f"category_id={cid}")
            offers = self._load_offers()
            shown = [f"{o.get('name')}={o.get('offer_id')}" for o in offers[:16]]
            lines.append("offers: " + "; ".join(shown))
            lines.append("required fields: " + ", ".join(self.required_field_keys()))
        except Exception as exc:
            lines.append(f"catalogue: {type(exc).__name__}: {exc}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def _build_fields(self, player_id: str, server_id: str = "") -> Dict[str, str]:
        keys = self.required_field_keys()
        fields: Dict[str, str] = {}
        for k in keys:
            kl = k.lower()
            if "server" in kl or "zone" in kl:
                if server_id:
                    fields[k] = str(server_id)
            else:
                fields[k] = str(player_id)
        if not fields:  # safety: at least pass the uid under the first key
            fields[keys[0]] = str(player_id)
        return fields

    def _paged(self, url: str, key: str) -> List[dict]:
        """Follow cursor pagination, collecting list items under ``key``."""
        out: List[dict] = []
        cursor = None
        for _ in range(20):  # hard page cap
            params = {"limit": "100"}
            if cursor:
                params["cursor"] = cursor
            body = self._req("GET", url, params=params)
            items = body.get(key) or body.get("items") or []
            out += [x for x in items if isinstance(x, dict)]
            meta = body.get("meta") or {}
            cursor = meta.get("next_cursor")
            if not cursor or not meta.get("has_more"):
                break
        return out
