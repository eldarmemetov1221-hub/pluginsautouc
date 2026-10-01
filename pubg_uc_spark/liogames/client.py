"""LioGamesClient: signed HTTP adapter for the Distribution Hub API.

Public surface used by the drip buyer:

    client = LioGamesClient(config)
    pid   = client.resolve_product_id()            # PUBG Mobile Code (Global)
    vid   = client.resolve_variation_id("60")      # 60 UC -> variation id
    bal   = client.balance()                        # wallet USD (float) or None
    found = client.order_status(client_ref=ref)     # dict or None (not found)
    res   = client.order_create(vid, ref, pid)      # {order_id, status, raw}
    code  = client.extract_code(status_dict)        # voucher code / SN or None

Signing (from the LioGames docs): HMAC-SHA256 over the EXACT raw JSON body,
header ``x-liog-sign``; optional ``X-LIOG-KEY-ID`` for a scoped key. The body
that is signed must be the byte-for-byte body that is sent, so we serialise once
and post the raw string (never ``json=`` which would re-serialise it).

Response envelope: ``{"ok": bool, "code": str, "message": str, "data": {...}}``.
Error codes seen in the docs: INSUFFICIENT_BALANCE, INVALID_SIGNATURE,
NOT_ALLOWED, PROCESSING.

Mock mode (``config.liog_mock``) returns deterministic fake codes with no
network, for tests/dev. It never spends anything.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Dict, List, Optional

from ..errors import LiogCriticalError, LiogInsufficientBalance, LiogTemporaryError
from ..utils.logger import get_logger, mask_code

log = get_logger("liogames")

try:  # requests is a FunPayCardinal dependency; optional for tests/mock
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore


# Keys whose string values are likely to BE the delivered voucher code / serial.
_CODE_KEYS = re.compile(
    r"(voucher|redeem|serial|^sn$|pin|code|secret|card|cd_?key|key)", re.I
)
# Keys that merely reference the order, not the code - never treat as the code.
_NON_CODE_KEYS = re.compile(r"(client_ref|order_?id|product|variation|status|member)", re.I)


class LioGamesClient:
    def __init__(self, config):
        self.cfg = config
        self._product_id: Optional[int] = None
        self._variation_cache: Dict[str, str] = dict(getattr(config, "liog_variations", {}) or {})
        self._products_cache: Optional[List[dict]] = None
        self._product_obj: Optional[dict] = None

    # ------------------------------------------------------------------ #
    # Signing & transport
    # ------------------------------------------------------------------ #
    def _sign(self, raw_body: str) -> str:
        secret = (self.cfg.liog_secret or "").encode("utf-8")
        return hmac.new(secret, raw_body.encode("utf-8"), hashlib.sha256).hexdigest()

    def _post(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Signed POST. Serialises once (compact, slashes unescaped) and signs
        exactly those bytes, matching PHP ``json_encode($p, JSON_UNESCAPED_SLASHES)``."""
        if requests is None:  # pragma: no cover
            raise LiogCriticalError("requests is not installed")
        if not self.cfg.liog_member_code:
            raise LiogCriticalError("LIOG_MEMBER_CODE is not configured")
        if not self.cfg.liog_secret:
            raise LiogCriticalError("LIOG_SECRET is not configured")
        # Python's json.dumps does not escape '/', matching JSON_UNESCAPED_SLASHES.
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-liog-sign": self._sign(raw),
        }
        if self.cfg.liog_key_id:
            headers["X-LIOG-KEY-ID"] = self.cfg.liog_key_id
        try:
            resp = requests.post(
                url, data=raw.encode("utf-8"), headers=headers, timeout=self.cfg.liog_timeout
            )
        except Exception as exc:
            raise LiogTemporaryError(f"LioGames POST {_short(url)} failed: {exc}") from exc
        return self._handle(resp, url)

    def _get(self, url: str, params: Optional[dict] = None) -> Dict[str, Any]:
        if requests is None:  # pragma: no cover
            raise LiogCriticalError("requests is not installed")
        headers = {"Accept": "application/json"}
        if self.cfg.liog_key_id:
            headers["X-LIOG-KEY-ID"] = self.cfg.liog_key_id
        try:
            resp = requests.get(url, params=params or {}, headers=headers, timeout=self.cfg.liog_timeout)
        except Exception as exc:
            raise LiogTemporaryError(f"LioGames GET {_short(url)} failed: {exc}") from exc
        return self._handle(resp, url)

    def _handle(self, resp, url: str = "") -> Dict[str, Any]:
        code = resp.status_code
        where = _short(url)
        snippet = ""
        try:
            snippet = (resp.text or "").strip().replace("\n", " ")[:200]
        except Exception:
            pass
        if code in (401, 403):
            raise LiogCriticalError(f"LioGames auth error HTTP {code} ({where}): {snippet}")
        if code == 429 or code >= 500:
            raise LiogTemporaryError(f"LioGames HTTP {code} ({where}): {snippet}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise LiogCriticalError(
                f"LioGames non-JSON body HTTP {code} ({where}): {snippet or exc}") from exc
        if not isinstance(body, dict):
            return {"ok": True, "data": body}
        # Map documented error codes to the plugin's taxonomy.
        err = str(body.get("code") or "").upper()
        if err == "INSUFFICIENT_BALANCE":
            raise LiogInsufficientBalance(body.get("message") or "insufficient balance")
        if err == "INVALID_SIGNATURE":
            raise LiogCriticalError("LioGames INVALID_SIGNATURE - check LIOG_SECRET / signing")
        if err == "PROCESSING":
            # Not an error: the order simply isn't terminal yet.
            body.setdefault("ok", True)
        return body

    # ------------------------------------------------------------------ #
    # Catalogue resolution (tolerant of a flaky /products endpoint)
    # ------------------------------------------------------------------ #
    def _raw_get(self, url: str, params: dict = None):
        """GET without raising: returns (status, json_or_None, text_snippet)."""
        if requests is None:  # pragma: no cover
            return (None, None, "requests missing")
        headers = {"Accept": "application/json"}
        if self.cfg.liog_key_id:
            headers["X-LIOG-KEY-ID"] = self.cfg.liog_key_id
        try:
            r = requests.get(url, params=params or {}, headers=headers, timeout=self.cfg.liog_timeout)
        except Exception as exc:
            return (None, None, str(exc))
        snippet = ""
        try:
            snippet = (r.text or "").strip().replace("\n", " ")[:200]
        except Exception:
            pass
        js = None
        try:
            js = r.json()
        except Exception:
            js = None
        return (r.status_code, js, snippet)

    def _catalogue_sources(self):
        base = self.cfg.liog_base_url
        root = base.split("/api/")[0]
        # Several shapes seen in the wild; /products sometimes 500s, so fall back.
        return [
            base + "/products",
            base + "/products/",
            base + "/catalog.json",
            root + "/catalog.json",
        ]

    @staticmethod
    def _items_from(js) -> List[dict]:
        items = _as_list(js)
        if not items and isinstance(js, dict):
            # catalog.json is sometimes a map keyed by product id
            vals = [v for v in js.values() if isinstance(v, dict)]
            if vals:
                items = vals
        return items

    def _load_products(self) -> List[dict]:
        if self._products_cache is not None:
            return self._products_cache
        tried = []
        for url in self._catalogue_sources():
            status, js, snippet = self._raw_get(url)
            if status == 200 and js is not None:
                items = self._items_from(js)
                if items:
                    self._products_cache = items
                    log.info("[LioGames] catalogue from %s (%d products)", url, len(items))
                    return items
            tried.append(f"{_short(url)}→{status}")
        raise LiogCriticalError(
            "LioGames каталог не читается (" + ", ".join(tried) + "). "
            "Задай вручную LIOG_PRODUCT_ID и LIOG_VARIATIONS в .env."
        )

    def resolve_product_id(self) -> int:
        if self._product_id:
            return self._product_id
        if self.cfg.liog_product_id:
            self._product_id = int(self.cfg.liog_product_id)
            return self._product_id
        if self.cfg.liog_mock:
            self._product_id = 99001
            return self._product_id
        items = self._load_products()
        want = (self.cfg.liog_product_name or "").lower()
        best = None
        for it in items:
            name = str(it.get("name") or it.get("title") or "").lower()
            pid = it.get("product_id") or it.get("id")
            if not pid:
                continue
            if name == want:
                best, self._product_obj = pid, it
                break
            if ("pubg" in name and "code" in name) or (want and want in name):
                if best is None:
                    best, self._product_obj = pid, it
        if not best:
            names = ", ".join(str(it.get("name") or it.get("title") or "?") for it in items[:15])
            raise LiogCriticalError(
                f"Товар '{self.cfg.liog_product_name}' не найден среди {len(items)}: {names}. "
                f"Задай LIOG_PRODUCT_ID."
            )
        self._product_id = int(best)
        log.info("[LioGames] product_id=%s (%s)", self._product_id, self.cfg.liog_product_name)
        return self._product_id

    def _load_variations(self, product_id) -> List[dict]:
        # Variations are often embedded in the product object from the catalogue.
        if self._product_obj:
            for k in ("variations", "packs", "denominations", "options", "variants"):
                v = self._product_obj.get(k)
                if isinstance(v, list) and v:
                    return [x for x in v if isinstance(x, dict)]
        # Otherwise hit the dedicated endpoint (also tolerant of a 500).
        status, js, snippet = self._raw_get(self.cfg.liog_variations_url(product_id))
        if status == 200 and js is not None:
            items = self._items_from(js)
            if items:
                return items
        raise LiogCriticalError(
            f"Список вариаций товара {product_id} не читается (HTTP {status}). "
            f"Задай LIOG_VARIATIONS в .env."
        )

    def resolve_variation_id(self, denom: str) -> str:
        denom = str(denom)
        if denom in self._variation_cache:
            return self._variation_cache[denom]
        if self.cfg.liog_mock:
            vid = f"v{denom}"
            self._variation_cache[denom] = vid
            return vid
        pid = self.resolve_product_id()
        items = self._load_variations(pid)
        match = None
        for it in items:
            label = str(it.get("name") or it.get("label") or it.get("title") or "")
            vid = it.get("variation_id") or it.get("id")
            if not vid:
                continue
            if re.search(rf"(?<!\d){re.escape(denom)}(?!\d)", label):
                match = vid
                if "uc" in label.lower():
                    break
        if not match:
            labels = ", ".join(str(it.get("name") or it.get("label") or "?") for it in items[:20])
            raise LiogCriticalError(
                f"Вариация {denom} UC не найдена. Доступно: {labels}. Задай LIOG_VARIATIONS."
            )
        self._variation_cache[denom] = str(match)
        log.info("[LioGames] %s UC -> variation_id=%s", denom, match)
        return str(match)

    def unit_price(self, variation_id: str) -> Optional[float]:
        """Best-effort published unit price for a variation (for the confirm
        card). Returns None if it can't be read - never blocks a purchase."""
        if self.cfg.liog_mock:
            return 0.88
        try:
            pid = self.resolve_product_id()
            status, body, _ = self._raw_get(f"{self.cfg.liog_base_url}/products/{pid}/price-matrix")
            if status != 200 or body is None:
                return None
        except Exception:
            return None
        for it in _as_list(body):
            vid = str(it.get("variation_id") or it.get("id") or "")
            if vid == str(variation_id):
                for k in ("gold", "GOLD", "price", "normal", "Normal"):
                    v = it.get(k)
                    if v is not None:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
        return None

    # ------------------------------------------------------------------ #
    # Wallet / orders
    # ------------------------------------------------------------------ #
    def balance(self) -> Optional[float]:
        """Wallet balance as a float, or None if it can't be parsed."""
        if self.cfg.liog_mock:
            return 9999.0
        body = self._post(self.cfg.liog_balance_url(), {"member_code": self.cfg.liog_member_code})
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        for k in ("balance", "wallet", "amount", "available"):
            v = data.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    def diagnose(self) -> str:
        """Hit a few endpoints raw (no raising) and report status + body snippet,
        so a failing /uc_buy can be pinpointed without spending anything."""
        if self.cfg.liog_mock:
            return "LioGames в MOCK-режиме (LIOG_SECRET пуст) — реальные запросы не идут."
        if requests is None:  # pragma: no cover
            return "requests не установлен."
        lines = [f"base: {self.cfg.liog_base_url}",
                 f"member_code: {'задан' if self.cfg.liog_member_code else 'ПУСТО'}",
                 f"secret: {'задан' if self.cfg.liog_secret else 'ПУСТО'}",
                 f"key_id: {self.cfg.liog_key_id or '—'}  sandbox: {self.cfg.liog_sandbox}",
                 f"product_id(env): {self.cfg.liog_product_id or '—'}  "
                 f"variations(env): {self.cfg.liog_variations or '—'}"]

        st, _js, snip = self._raw_get(f"{self.cfg.liog_base_url}/ping")
        lines.append(f"GET /ping: HTTP {st} | {snip}")
        st, _js, snip = self._raw_get(f"{self.cfg.liog_base_url}/routes")
        lines.append(f"GET /routes: HTTP {st} | {snip}")

        # Probe every catalogue source; report which (if any) returns a list.
        ok_source = None
        for url in self._catalogue_sources():
            st, js, snip = self._raw_get(url)
            n = len(self._items_from(js)) if js is not None else 0
            lines.append(f"GET {_short(url)}: HTTP {st} | items={n} | {snip if st != 200 else ''}".rstrip())
            if st == 200 and n and ok_source is None:
                ok_source = url

        # balance is a signed POST - exercises signing end-to-end
        try:
            lines.append(f"POST /balance: OK balance={self.balance()}")
        except Exception as exc:
            lines.append(f"POST /balance: {type(exc).__name__}: {exc}")

        # If a catalogue is readable, show the resolved PUBG ids so they can be
        # pinned in .env even if /products stays flaky.
        try:
            pid = self.resolve_product_id()
            lines.append(f"→ product_id={pid}")
            vs = self._load_variations(pid)
            shown = []
            for it in vs[:16]:
                label = it.get("name") or it.get("label") or it.get("title") or "?"
                vid = it.get("variation_id") or it.get("id")
                shown.append(f"{label}={vid}")
            lines.append("→ variations: " + "; ".join(shown))
        except Exception as exc:
            lines.append(f"→ каталог: {type(exc).__name__}: {exc}")
        return "\n".join(lines)

    def order_create(self, variation_id: str, client_ref: str, product_id=None) -> Dict[str, Any]:
        """Create ONE voucher order. Idempotent on LioGames' side by client_ref."""
        pid = int(product_id or self.resolve_product_id())
        if self.cfg.liog_mock:
            return {"order_id": f"MOCK-{client_ref}", "status": "processing", "raw": {}}
        payload = {
            "member_code": self.cfg.liog_member_code,
            "product_id": pid,
            "variation_id": _maybe_int(variation_id),
            "client_ref": client_ref,
        }
        body = self._post(self.cfg.liog_order_create_url(), payload)
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        return {
            "order_id": str(data.get("order_id") or data.get("id") or ""),
            "status": str(data.get("status") or body.get("code") or "processing").lower(),
            "raw": body,
        }

    def order_status_raw(self, order_id: str = None, client_ref: str = None,
                         live: bool = False) -> Dict[str, Any]:
        """Signed order-status returning the FULL raw body (diagnostics). When
        ``live`` is True it targets the live endpoint even in sandbox mode, so a
        past real order can be inspected to discover product/variation ids and
        the exact field that carries the voucher code."""
        url = (f"{self.cfg.liog_base_url}/order-status" if live
               else self.cfg.liog_order_status_url())
        payload: Dict[str, Any] = {"member_code": self.cfg.liog_member_code}
        if order_id:
            payload["order_id"] = order_id
        if client_ref:
            payload["client_ref"] = client_ref
        return self._post(url, payload)

    def order_status(self, client_ref: str = None, order_id: str = None) -> Optional[Dict[str, Any]]:
        """Fetch an order's status. Returns the response dict, or None if the
        order does not exist yet (so the caller knows it is safe to create)."""
        if self.cfg.liog_mock:
            if client_ref:
                return {"ok": True, "status": "completed",
                        "data": {"voucher": _mock_code(client_ref), "status": "completed"}}
            return None
        payload: Dict[str, Any] = {"member_code": self.cfg.liog_member_code}
        if order_id:
            payload["order_id"] = order_id
        if client_ref:
            payload["client_ref"] = client_ref
        try:
            body = self._post(self.cfg.liog_order_status_url(), payload)
        except LiogCriticalError:
            # Some gateways answer "not found" with a 4xx; treat as not-existing.
            return None
        code = str(body.get("code") or "").upper()
        if code in ("NOT_FOUND", "ORDER_NOT_FOUND", "NO_ORDER"):
            return None
        if body.get("ok") is False and not body.get("data"):
            return None
        return body

    # ------------------------------------------------------------------ #
    @staticmethod
    def status_is_failed(body: Dict[str, Any]) -> bool:
        s = _status_str(body)
        return s in (
            "failed", "error", "cancelled", "canceled", "rejected", "declined",
            "refund", "refunded", "partially_refunded", "partial_refund",
            "chargeback", "void", "voided", "expired", "timeout",
        )

    @staticmethod
    def status_is_terminal_ok(body: Dict[str, Any]) -> bool:
        s = _status_str(body)
        return s in ("completed", "complete", "done", "delivered", "success", "fulfilled")

    def extract_code(self, body: Dict[str, Any]) -> Optional[str]:
        """Dig the delivered voucher code / serial out of an order-status body.

        The docs only say completed orders "include delivery or S/N data", not the
        exact field, so this searches defensively and returns the first plausible
        code-like string. Returns None if nothing code-shaped is present yet."""
        found: List[str] = []
        _walk_for_codes(body.get("data", body), found)
        return found[0] if found else None


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _status_str(body: Dict[str, Any]) -> str:
    if not isinstance(body, dict):
        return ""
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    return str(body.get("status") or data.get("status") or body.get("code") or "").lower()


def _as_list(body) -> List[dict]:
    """Pull a list of catalogue objects out of various response shapes."""
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for key in ("data", "products", "variations", "items", "results"):
            v = body.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        if body.get("product_id") or body.get("variation_id") or body.get("id"):
            return [body]
    return []


def _short(url: str) -> str:
    """The path (and sandbox marker) of a URL, for compact error messages."""
    if not url:
        return ""
    try:
        return "/" + url.split("://", 1)[1].split("/", 1)[1]
    except Exception:
        return url


def _maybe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _walk_for_codes(node, out: List[str], depth: int = 0) -> None:
    if depth > 6 or node is None:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                _walk_for_codes(v, out, depth + 1)
            elif isinstance(v, str):
                key = str(k)
                if _NON_CODE_KEYS.search(key):
                    continue
                if _CODE_KEYS.search(key) and _looks_like_code(v):
                    out.append(v.strip())
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, str) and _looks_like_code(item):
                out.append(item.strip())
            else:
                _walk_for_codes(item, out, depth + 1)


def _looks_like_code(v: str) -> bool:
    v = (v or "").strip()
    # A voucher/serial: at least 6 chars, contains an alphanumeric run; reject
    # pure words / short status strings.
    return len(v) >= 6 and bool(re.search(r"[A-Za-z0-9]{6,}", v.replace("-", "").replace(" ", "")))


def _mock_code(seed: str) -> str:
    h = hashlib.sha256(seed.encode()).hexdigest().upper()
    return f"LIOG-{h[:4]}-{h[4:8]}-{h[8:12]}"
