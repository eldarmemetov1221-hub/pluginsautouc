"""Code format validation & extraction (task spec, section 9).

The regex lives in a single config parameter (``CODE_PATTERN``); business logic
never hard-codes the format, so it can be changed without touching services.
"""

from __future__ import annotations

import hashlib
import re
from typing import List, Optional


def compile_pattern(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern)


def extract_codes(text: str, pattern: str) -> List[str]:
    """Return all standalone tokens in ``text`` that match ``pattern``.

    Used to pull a UID/code out of a free-form buyer message. The pattern is
    wrapped in ``(?<!\\w)...(?!\\w)`` boundaries so a longer run is NOT partially
    matched - e.g. with ``[0-9]{9,11}`` a 12-digit string yields no match rather
    than its first 11 digits. Matches are returned in order, de-duplicated.
    """
    if not text:
        return []
    rx = re.compile(rf"(?<!\w)(?:{pattern})(?!\w)")
    seen: set[str] = set()
    out: List[str] = []
    for m in rx.finditer(text):
        val = m.group(0)
        if val not in seen:
            seen.add(val)
            out.append(val)
    return out


def extract_first_code(text: str, pattern: str) -> Optional[str]:
    codes = extract_codes(text, pattern)
    return codes[0] if codes else None


def is_valid_format(code: str, pattern: str) -> bool:
    """Full-string match: the whole token must satisfy the pattern."""
    if not code:
        return False
    rx = compile_pattern(pattern)
    m = rx.fullmatch(code)
    return m is not None


def code_hash(code: str) -> str:
    """Stable hash of a code for dedup / safe storage (section 5, 17).

    Lets us dedup and reference a code without necessarily storing it in clear
    text everywhere. The raw code is still stored in ``codes.code`` for admin
    lookup, but logs and cross-references use the hash.
    """
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
