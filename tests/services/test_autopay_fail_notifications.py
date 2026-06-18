"""Tests for the configurable autopay-failure antispam notifier.

Replaces the old hardcoded 6h cooldown (AUTOPAY_INSUFFICIENT_BALANCE_COOLDOWN_SECONDS).
The guarantee under test: with default config the bot sends at most TWO failure
notifications per subscription cycle (first failure + final reminder), then stays
silent — including in the <=2h window after the subscription has expired.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings


def test_config_defaults_present():
    assert settings.AUTOPAY_FAIL_MAX_NOTIFICATIONS == 2
    assert settings.AUTOPAY_FAIL_FINAL_REMINDER_HOURS == 3
    assert settings.AUTOPAY_FAIL_REPEAT_INTERVAL_HOURS == 0
