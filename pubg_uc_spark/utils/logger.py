"""Logging helpers (task spec, sections 13 & 17).

* Human-readable, levelled logs (INFO / WARNING / ERROR / CRITICAL).
* Secret / code masking so full codes and keys never hit the logs.
"""

from __future__ import annotations

import logging
from typing import Optional

_CONFIGURED = False


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    from ..config import get_config

    cfg = get_config()
    level = getattr(logging, str(cfg.log_level).upper(), logging.INFO)

    logger = logging.getLogger("pubg_uc_spark")
    logger.setLevel(level)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not logger.handlers:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        logger.addHandler(stream)

        if cfg.log_file:
            try:
                fileh = logging.FileHandler(cfg.log_file, encoding="utf-8")
                fileh.setFormatter(fmt)
                logger.addHandler(fileh)
            except OSError:
                logger.warning("Could not open log file %s", cfg.log_file)

    _CONFIGURED = True


def get_logger(name: str = "") -> logging.Logger:
    _configure_root()
    base = "pubg_uc_spark"
    return logging.getLogger(f"{base}.{name}" if name else base)


def mask_code(code: Optional[str], visible: int = 4) -> str:
    """Mask a code for logging: ``ABCD********1234`` (section 17).

    Keeps the first and last ``visible`` chars; masks the middle. Short codes
    are fully masked.
    """
    if not code:
        return "<empty>"
    code = str(code)
    if len(code) <= visible * 2:
        return "*" * len(code)
    middle = "*" * (len(code) - visible * 2)
    return f"{code[:visible]}{middle}{code[-visible:]}"


def reset_logging() -> None:
    """Testing helper - drop handlers so config changes take effect."""
    global _CONFIGURED
    logger = logging.getLogger("pubg_uc_spark")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    _CONFIGURED = False
