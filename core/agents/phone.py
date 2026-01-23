from __future__ import annotations

import re
from typing import Optional


def normalize_phone(number: str, *, default_country_code: Optional[str] = None) -> str:
    """Normalize a phone number for matching.

    - Keeps digits only
    - If default_country_code is given and the number starts with 0 (local IL)
      then it is converted to <cc><rest>.
    """
    digits = re.sub(r"\D+", "", number or "")
    if not digits:
        return ""

    if default_country_code and digits.startswith("0"):
        digits = f"{default_country_code}{digits[1:]}"

    return digits


def phones_equivalent(a: str, b: str, *, default_country_code: Optional[str] = None) -> bool:
    """Best-effort equivalence check.

    WhatsApp may report numbers in full international format, while
    users might store +972... or 05... . We normalize and then compare.
    """
    na = normalize_phone(a, default_country_code=default_country_code)
    nb = normalize_phone(b, default_country_code=default_country_code)
    if not na or not nb:
        return False
    return na == nb
