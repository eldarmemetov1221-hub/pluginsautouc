"""Unified Spark result model (task spec, section 3).

Business logic depends ONLY on :class:`UnifiedStatus`, never on Spark's raw
wire format. The mapping from raw -> unified lives in ``parser.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class UnifiedStatus(str, Enum):
    """Project-internal, Spark-agnostic statuses (section 3)."""

    VALID = "VALID"
    INVALID = "INVALID"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ALREADY_USED = "ALREADY_USED"
    ERROR = "ERROR"        # definitive error we understand but can't act on
    UNKNOWN = "UNKNOWN"    # unrecognised response -> treat as critical


@dataclass
class SparkResult:
    """Normalised outcome of a single code check."""

    status: UnifiedStatus
    raw: Dict[str, Any] = field(default_factory=dict)
    message: str = ""
    http_status: Optional[int] = None

    @property
    def is_success(self) -> bool:
        return self.status is UnifiedStatus.VALID

    @property
    def is_final_negative(self) -> bool:
        return self.status in {
            UnifiedStatus.INVALID,
            UnifiedStatus.ACCOUNT_NOT_FOUND,
            UnifiedStatus.ALREADY_USED,
        }
