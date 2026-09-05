"""Configuration for the PUBG UC / Spark plugin (task spec, section 14).

All tunable parameters live here. Values are read from environment variables
(optionally loaded from a ``.env`` file if python-dotenv is installed). Nothing
sensitive is hard-coded; see ``.env.example``.

Multi-lot ready: ``LOTS`` maps a FunPay ``lot_id`` -> product metadata, so new
denominations (120 UC, 180 UC, ...) are added by config only (section 14).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List

# Optional .env loading. Never required at import time.
try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_admin_ids() -> List[int]:
    raw = os.environ.get("ADMIN_IDS", "").strip()
    ids: List[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


#: Base UC denominations the Spark bot can redeem from stock.
SPARK_BASE_DENOMINATIONS = ("60", "325", "660", "1800", "3850", "8100")


@dataclass
class LotConfig:
    """Metadata for a single tracked FunPay lot.

    A FunPay lot advertises some UC amount (``uc``) which may be delivered as a
    COMBINATION of Spark base packs (``picks``). Examples:
        "60"  -> {"60": 1}
        "120" -> {"60": 2}
        "300" -> {"325": 1}          (buyer pays 300, gets 325)
        "445" -> {"325": 1, "60": 2}
        "1045"-> {"660": 1, "325": 1, "60": 1}
    For an order of N units, each pick count is multiplied by N.
    """

    lot_id: str
    product: str          # buyer-facing label, e.g. "300 UC"
    uc: str = "60"        # advertised amount (used for description matching)
    # Spark stock combination. If empty, defaults to {uc: 1}.
    picks: dict = field(default_factory=dict)
    # Keywords matched (AND, case-insensitive) against the FunPay order
    # description to recognise this lot. FunPay orders do NOT expose the offer
    # id, so matching is by description. Empty -> match by ``uc`` (as a whole
    # number) + "uc".
    keywords: list = field(default_factory=list)

    def base_picks(self) -> dict:
        """The Spark picks for ONE unit of this lot."""
        if self.picks:
            return {str(k): int(v) for k, v in self.picks.items()}
        return {str(self.uc): 1}

    def picks_for(self, quantity: int) -> dict:
        """Spark picks for ``quantity`` units (counts multiplied)."""
        q = max(1, int(quantity or 1))
        return {k: v * q for k, v in self.base_picks().items()}


def _default_lots() -> Dict[str, LotConfig]:
    """Default single-lot config for the task's PUBG 60 UC offer.

    Overridable via the ``LOTS`` env var (JSON). Each lot gives the advertised
    ``uc`` (for matching) and the Spark ``picks`` combination, e.g.::

        LOTS={
          "37330959": {"product": "60 UC",  "uc": "60",  "picks": {"60": 1}},
          "id120":    {"product": "120 UC", "uc": "120", "picks": {"60": 2}},
          "id300":    {"product": "300 UC", "uc": "300", "picks": {"325": 1}},
          "id445":    {"product": "445 UC", "uc": "445", "picks": {"325": 1, "60": 2}}
        }
    """
    raw = os.environ.get("LOTS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            return {
                str(lid): LotConfig(
                    lot_id=str(lid),
                    product=str(meta.get("product", "")),
                    uc=str(meta.get("uc", meta.get("denomination", "60"))),
                    picks={str(k): int(v) for k, v in (meta.get("picks", {}) or {}).items()},
                    keywords=list(meta.get("keywords", []) or []),
                )
                for lid, meta in data.items()
            }
        except (ValueError, AttributeError, TypeError):
            # Fall through to the single-lot default on malformed JSON.
            pass

    lot_id = _get("FUNPAY_LOT_ID", "37330959")
    uc = _get("UC", _get("DENOMINATION", "60"))
    picks: Dict[str, int] = {}
    picks_raw = os.environ.get("PICKS", "").strip()
    if picks_raw:
        try:
            picks = {str(k): int(v) for k, v in json.loads(picks_raw).items()}
        except (ValueError, AttributeError, TypeError):
            picks = {}
    return {
        lot_id: LotConfig(
            lot_id=lot_id,
            product=_get("PRODUCT_NAME", "60 UC"),
            uc=uc,
            picks=picks,
        )
    }


@dataclass
class Messages:
    """Buyer-facing message templates (task spec, section 24.11).

    Flow: after payment the bot stays silent; the buyer sends their PUBG UID and
    the bot redeems UC from Spark stock and reports the result.

    Placeholders available in every template: ``{order_id}``, ``{product}``,
    ``{uid}``. Override any text via the matching ``MSG_*`` env var. These are
    working drafts - confirm/adjust the wording.
    """

    # Sent ONLY on an explicit admin resend (/uc_resend). The bot never messages
    # the buyer automatically after payment.
    ask_uid: str = field(
        default_factory=lambda: _get(
            "MSG_ASK_UID",
            "По заказу #{order_id} ({product}) пришлите, пожалуйста, ваш PUBG ID "
            "(только цифры, 9–11 знаков).",
        )
    )
    valid: str = field(
        default_factory=lambda: _get(
            "MSG_VALID",
            "✅ Успешное пополнение {product}\n\n"
            "🆔 ID игрока: {uid}\n"
            "👤 Имя игрока: {player_name}\n\n"
            "Спасибо за покупку! Не забудьте подтвердить оплату в разделе "
            "покупки!🤝",
        )
    )
    invalid: str = field(
        default_factory=lambda: _get(
            "MSG_INVALID",
            "Не удалось начислить {product} на ID {uid} (заказ #{order_id}). "
            "Проверьте ID или свяжитесь с продавцом.",
        )
    )
    # 1st UID error (account not found): ask the buyer to re-check and resend.
    account_not_found: str = field(
        default_factory=lambda: _get(
            "MSG_ACCOUNT_NOT_FOUND",
            "Ошибка UID! Проверьте правильность UID и попробуйте ещё раз.",
        )
    )
    # 2nd UID error on the same order: stop and hand over to the seller.
    account_not_found_final: str = field(
        default_factory=lambda: _get(
            "MSG_ACCOUNT_NOT_FOUND_FINAL",
            "Повторная ошибка в UID. Ожидайте ответ продавца!",
        )
    )
    bad_format: str = field(
        default_factory=lambda: _get(
            "MSG_BAD_FORMAT",
            "Это не похоже на игровой ID. Пришлите, пожалуйста, ваш PUBG ID — "
            "только цифры, 9–11 знаков (заказ #{order_id}).",
        )
    )
    error: str = field(
        default_factory=lambda: _get(
            "MSG_ERROR",
            "При начислении по заказу #{order_id} возникла ошибка. Продавец "
            "уведомлён, ожидайте, пожалуйста.",
        )
    )
    # Multi-pack order where only part was delivered.
    partial: str = field(
        default_factory=lambda: _get(
            "MSG_PARTIAL",
            "Заказ #{order_id} ({product}): начислено частично ({delivered} из "
            "{total} пакетов). По остатку продавец свяжется с вами.",
        )
    )
    temporary_error: str = field(
        default_factory=lambda: _get(
            "MSG_TEMPORARY_ERROR",
            "Начисление по заказу #{order_id} временно задерживается, повторяем "
            "автоматически. Пожалуйста, подождите.",
        )
    )
    duplicate: str = field(
        default_factory=lambda: _get(
            "MSG_DUPLICATE",
            "Этот ID по заказу #{order_id} уже принят в обработку, ожидайте "
            "результат.",
        )
    )


@dataclass
class Config:
    """Top-level plugin configuration."""

    # FunPay / lots
    lots: Dict[str, LotConfig] = field(default_factory=_default_lots)

    # Spark HTTP API (api.pubgredeemerbot.com). SPARK_API_URL is the BASE url;
    # endpoints are derived from it (/v1/jobs/stock-redeem, /v1/jobs/{id}).
    spark_api_url: str = field(default_factory=lambda: _get("SPARK_API_URL", "").rstrip("/"))
    spark_api_key: str = field(default_factory=lambda: _get("SPARK_API_KEY", ""))
    spark_timeout: float = field(default_factory=lambda: _get_float("SPARK_TIMEOUT", 30.0))
    # Spark is a job API: POST creates a job, GET polls it. spark_job_wait is
    # the per-poll long-poll hold (<=60); spark_max_wait bounds total polling.
    spark_job_wait: float = field(default_factory=lambda: _get_float("SPARK_JOB_WAIT", 25.0))
    spark_max_wait: float = field(default_factory=lambda: _get_float("SPARK_MAX_WAIT", 120.0))
    # When True (or when no URL is configured) the SparkChecker runs in mock
    # mode - no network calls. Flip to False once a real code sample confirms
    # the finished-job result shape in spark/parser.py.
    spark_mock: bool = field(
        default_factory=lambda: _get_bool("SPARK_MOCK", not bool(_get("SPARK_API_URL")))
    )

    # Retry (section 12)
    max_retries: int = field(default_factory=lambda: _get_int("MAX_RETRIES", 3))
    retry_delay: float = field(default_factory=lambda: _get_float("RETRY_DELAY", 5.0))
    retry_backoff: float = field(default_factory=lambda: _get_float("RETRY_BACKOFF", 2.0))

    # UID format (section 9). Single source of truth for the pattern.
    # A PUBG player UID is digits only, 9-11 long. Kept configurable so the
    # rule can change without touching business logic. (Env: UID_PATTERN, with
    # legacy CODE_PATTERN accepted as a fallback.)
    code_pattern: str = field(
        default_factory=lambda: _get("UID_PATTERN", _get("CODE_PATTERN", r"[0-9]{9,11}"))
    )

    # Database (section 6). Separate file, does NOT touch FPC's own storage.
    database_path: str = field(
        default_factory=lambda: _get("DATABASE_PATH", "storage/pubg_uc_spark.db")
    )

    # Admin whitelist (section 18) - Telegram user IDs.
    admin_ids: List[int] = field(default_factory=_get_admin_ids)

    # Logging
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: _get("LOG_FILE", ""))

    messages: Messages = field(default_factory=Messages)

    # Spark endpoint helpers (derived from the base url).
    def spark_stock_redeem_url(self) -> str:
        return f"{self.spark_api_url}/v1/jobs/stock-redeem"

    def spark_job_url(self, job_id: str) -> str:
        return f"{self.spark_api_url}/v1/jobs/{job_id}"

    def lot(self, lot_id) -> LotConfig | None:
        return self.lots.get(str(lot_id))

    def is_tracked_lot(self, lot_id) -> bool:
        return str(lot_id) in self.lots

    def is_admin(self, user_id) -> bool:
        try:
            return int(user_id) in self.admin_ids
        except (TypeError, ValueError):
            return False


# Singleton-style accessor so every module reads the same instance.
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """Testing helper: force re-read of env on next :func:`get_config`."""
    global _config
    _config = None
