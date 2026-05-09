"""Smoke tests for bio-reward analytics pure helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.bio_reward_analytics import bucket_for_month, bucket_for_week


def test_bucket_for_month_format():
    assert bucket_for_month(datetime(2026, 5, 2, tzinfo=UTC)) == '2026-05'
    assert bucket_for_month(datetime(2026, 1, 31, tzinfo=UTC)) == '2026-01'
    assert bucket_for_month(datetime(2026, 12, 1, tzinfo=UTC)) == '2026-12'


def test_bucket_for_week_iso_format():
    # 2026-05-02 is a Saturday -> ISO week 18 of 2026
    out = bucket_for_week(datetime(2026, 5, 2, tzinfo=UTC))
    assert out == '2026-W18'

    # First week of January edge case
    out2 = bucket_for_week(datetime(2026, 1, 1, tzinfo=UTC))
    assert out2.startswith('20')
    assert '-W' in out2


def test_bucket_for_week_pads_single_digit():
    # Early January -> ISO week 02 (year 2026)
    out = bucket_for_week(datetime(2026, 1, 5, tzinfo=UTC))
    assert out == '2026-W02'
