"""Tests for trial_conversion_service idempotency key + parsing helpers."""

from datetime import UTC, datetime

import pytest

from app.services.trial_conversion_service import (
    TRIAL_CONVERT_PAYMENT_PREFIX,
    build_idempotency_key,
    parse_subscription_id,
)


class TestBuildIdempotencyKey:
    def test_format_uses_daily_granularity(self) -> None:
        now = datetime(2026, 5, 25, 12, 30, 0, tzinfo=UTC)
        assert build_idempotency_key(10148, now=now) == 'trial_convert_10148_20260525'

    def test_two_calls_same_day_produce_same_key(self) -> None:
        now1 = datetime(2026, 5, 25, 0, 0, 1, tzinfo=UTC)
        now2 = datetime(2026, 5, 25, 23, 59, 59, tzinfo=UTC)
        assert build_idempotency_key(42, now=now1) == build_idempotency_key(42, now=now2)

    def test_different_days_produce_different_keys(self) -> None:
        a = build_idempotency_key(42, now=datetime(2026, 5, 25, 12, 0, tzinfo=UTC))
        b = build_idempotency_key(42, now=datetime(2026, 5, 26, 12, 0, tzinfo=UTC))
        assert a != b

    def test_default_now_resolves_to_today(self) -> None:
        today = datetime.now(UTC).strftime('%Y%m%d')
        key = build_idempotency_key(7)
        assert key == f'trial_convert_7_{today}'


class TestParseSubscriptionId:
    @pytest.mark.parametrize(
        ('payment_id', 'expected'),
        [
            ('trial_convert_10148_20260525', 10148),
            ('trial_convert_7_20260101', 7),
            ('trial_convert_999999_20991231', 999999),
        ],
    )
    def test_extracts_subscription_id(self, payment_id: str, expected: int) -> None:
        assert parse_subscription_id(payment_id) == expected

    @pytest.mark.parametrize(
        'payment_id',
        [
            '',
            None,
            'recurrent_10148_5_20260525',
            'random_payment_id',
            'trial_convert_',
            'trial_convert_abc_20260525',
            'trial_convert__20260525',
        ],
    )
    def test_returns_none_for_invalid_input(self, payment_id) -> None:
        assert parse_subscription_id(payment_id) is None

    def test_prefix_constant_matches_real_format(self) -> None:
        assert TRIAL_CONVERT_PAYMENT_PREFIX == 'trial_convert_'

    def test_roundtrip_build_then_parse(self) -> None:
        key = build_idempotency_key(5555)
        assert parse_subscription_id(key) == 5555
