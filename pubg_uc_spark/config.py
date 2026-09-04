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


@dataclass
class LotConfig:
    """Metadata for a single tracked FunPay lot."""

    lot_id: str
    product: str
    quantity: int = 1


def _default_lots() -> Dict[str, LotConfig]:
    """Default single-lot config for the task's PUBG 60 UC offer.

    Overridable via the ``LOTS`` env var (JSON), e.g.::

        LOTS={"37330959": {"product": "PUBG 60 UC", "quantity": 60},
              "40000000": {"product": "PUBG 120 UC", "quantity": 120}}
    """
    raw = os.environ.get("LOTS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            return {
                str(lid): LotConfig(
                    lot_id=str(lid),
                    product=str(meta.get("product", "")),
                    quantity=int(meta.get("quantity", 1)),
                )
                for lid, meta in data.items()
            }
        except (ValueError, AttributeError, TypeError):
            # Fall through to the single-lot default on malformed JSON.
            pass

    lot_id = _get("FUNPAY_LOT_ID", "37330959")
    return {
        lot_id: LotConfig(
            lot_id=lot_id,
            product=_get("PRODUCT_NAME", "PUBG Mobile 60 UC"),
            quantity=_get_int("PRODUCT_QUANTITY", 60),
        )
    }


@dataclass
class Messages:
    """Buyer-facing message templates (task spec, section 24.11).

    NOTE: these are placeholders supplied by the developer until the real
    texts are provided. Edit here or override via env vars ``MSG_*``.
    ``{order_id}``, ``{product}``, ``{code_masked}`` placeholders are allowed.
    """

    ask_code: str = field(
        default_factory=lambda: _get(
            "MSG_ASK_CODE",
            "Здравствуйте! Спасибо за заказ #{order_id} ({product}).\n"
            "Пришлите, пожалуйста, код пополнения одним сообщением.",
        )
    )
    valid: str = field(
        default_factory=lambda: _get(
            "MSG_VALID",
            "Код по заказу #{order_id} успешно проверен ✅. Спасибо за покупку!",
        )
    )
    invalid: str = field(
        default_factory=lambda: _get(
            "MSG_INVALID",
            "Код по заказу #{order_id} не прошёл проверку ❌ (недействителен). "
            "Проверьте, пожалуйста, и пришлите корректный код.",
        )
    )
    already_used: str = field(
        default_factory=lambda: _get(
            "MSG_ALREADY_USED",
            "Код по заказу #{order_id} уже был использован ранее ⚠️. "
            "Пришлите, пожалуйста, неиспользованный код.",
        )
    )
    account_not_found: str = field(
        default_factory=lambda: _get(
            "MSG_ACCOUNT_NOT_FOUND",
            "По заказу #{order_id}: аккаунт не найден. Проверьте, пожалуйста, "
            "данные и свяжитесь с продавцом.",
        )
    )
    bad_format: str = field(
        default_factory=lambda: _get(
            "MSG_BAD_FORMAT",
            "Не удалось распознать код в сообщении по заказу #{order_id}. "
            "Пришлите код в правильном формате.",
        )
    )
    temporary_error: str = field(
        default_factory=lambda: _get(
            "MSG_TEMPORARY_ERROR",
            "Проверка кода по заказу #{order_id} временно недоступна, "
            "повторим автоматически. Пожалуйста, подождите.",
        )
    )
    duplicate: str = field(
        default_factory=lambda: _get(
            "MSG_DUPLICATE",
            "Этот код по заказу #{order_id} уже принят в обработку, ожидайте "
            "результат.",
        )
    )


@dataclass
class Config:
    """Top-level plugin configuration."""

    # FunPay / lots
    lots: Dict[str, LotConfig] = field(default_factory=_default_lots)

    # Spark HTTP API
    spark_api_url: str = field(default_factory=lambda: _get("SPARK_API_URL", ""))
    spark_api_key: str = field(default_factory=lambda: _get("SPARK_API_KEY", ""))
    spark_timeout: float = field(default_factory=lambda: _get_float("SPARK_TIMEOUT", 20.0))
    # When True (or when no URL is configured) the SparkChecker runs in mock
    # mode - no network calls. Flip to False once the real endpoint/schema
    # (docs from api.pubgredeemerbot.com) are wired into spark/parser.py.
    spark_mock: bool = field(
        default_factory=lambda: _get_bool("SPARK_MOCK", not bool(_get("SPARK_API_URL")))
    )

    # Retry (section 12)
    max_retries: int = field(default_factory=lambda: _get_int("MAX_RETRIES", 3))
    retry_delay: float = field(default_factory=lambda: _get_float("RETRY_DELAY", 5.0))
    retry_backoff: float = field(default_factory=lambda: _get_float("RETRY_BACKOFF", 2.0))

    # Code format (section 9). Single source of truth for the pattern.
    # Placeholder pattern until the real PUBG 60 UC format is provided.
    code_pattern: str = field(
        default_factory=lambda: _get("CODE_PATTERN", r"[A-Za-z0-9]{8,32}")
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
