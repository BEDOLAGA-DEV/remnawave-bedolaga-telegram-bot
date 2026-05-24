"""Partner / affiliate click_id shared helpers."""

from __future__ import annotations

import re


CLICK_ID_PATTERN = r'^[A-Za-z0-9._:-]{1,128}$'
CLICK_ID_RE = re.compile(CLICK_ID_PATTERN)


def is_valid_click_id(value: str | None) -> bool:
    return bool(value) and bool(CLICK_ID_RE.match(value))


def extract_click_id_from_start_param(
    start_param: str | None,
) -> tuple[str | None, str | None]:
    """Split a /start deep-link payload into (campaign_part, click_id).

    Convention: ``<campaign>_clk_<click_id>``. Telegram caps the payload at
    64 chars but we accept up to 128 in case the limit is ever raised. The
    rightmost ``_clk_`` is used so campaign names that themselves contain
    ``_clk_`` parse correctly. If the suffix fails validation, the original
    payload is returned unchanged and click_id is ``None``.
    """
    if not start_param or '_clk_' not in start_param:
        return start_param, None
    try:
        campaign_part, click_part = start_param.rsplit('_clk_', 1)
    except ValueError:
        return start_param, None
    if not is_valid_click_id(click_part):
        return start_param, None
    return (campaign_part or None), click_part
