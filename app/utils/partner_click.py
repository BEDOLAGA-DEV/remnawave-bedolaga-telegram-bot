"""Partner / affiliate click_id shared helpers."""

from __future__ import annotations

import re


_CLICK_ID_PATTERN = r'^[A-Za-z0-9._:-]{1,128}$'
CLICK_ID_RE = re.compile(_CLICK_ID_PATTERN)


def is_valid_click_id(value: str | None) -> bool:
    return bool(value) and bool(CLICK_ID_RE.match(value))
