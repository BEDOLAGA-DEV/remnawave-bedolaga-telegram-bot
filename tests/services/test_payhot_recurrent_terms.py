"""Pure consent and merchant scheduling invariants, no provider/network/storage."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.payhot_recurrent_terms import (
    PayHotRecurrentTerms,
    consent_terms_match,
    next_charge_due_at,
    renewed_paid_until,
)


def terms(**changes):
    return PayHotRecurrentTerms(
        amount_kopeks=changes.pop('amount_kopeks', 10000),
        period_days=changes.pop('period_days', 30),
        tariff_id=changes.pop('tariff_id', 1),
        device_limit=changes.pop('device_limit', 0),
        payment_method=changes.pop('payment_method', 'card'),
        service_snapshot=changes.pop('service_snapshot', {'squads': ['a', 'b'], 'traffic_gb': 0}),
        **changes,
    )


@pytest.mark.parametrize('period', [1, 2, 6, 7, 360, 365, 366, 10000])
def test_arbitrary_integral_period_supported(period):
    assert terms(period_days=period).period_days == period


@pytest.mark.parametrize(
    'changes',
    [
        {'amount_kopeks': True},
        {'amount_kopeks': 1.0},
        {'amount_kopeks': 0},
        {'amount_kopeks': 2147483648},
        {'period_days': False},
        {'period_days': 1.5},
        {'period_days': 0},
        {'period_days': 3652059},
        {'device_limit': True},
        {'device_limit': 1.0},
        {'device_limit': -1},
        {'tariff_id': False},
        {'tariff_id': 0},
        {'tariff_id': 1.0},
        {'payment_method': 'crypto'},
        {'payment_method': 'applepay'},
        {'currency': 'EUR'},
        {'is_daily': 1},
        {'is_daily': True, 'period_days': 30},
        {'service_snapshot': {'price': float('nan')}},
        {'service_snapshot': {1: 'invalid'}},
    ],
)
def test_invalid_terms_fail_closed(changes):
    with pytest.raises(ValueError):
        terms(**changes)


def test_classic_daily_unlimited_and_max_amount():
    result = terms(tariff_id=None, period_days=1, is_daily=True, payment_method='sbp', amount_kopeks=2147483647)
    assert result.tariff_id is None and result.device_limit == 0 and result.is_daily


def test_snapshot_canonical_immutable_and_not_aliased():
    snapshot = {'z': [{'flag': True}], 'a': None}
    consent = terms(service_snapshot=snapshot)
    other = terms(service_snapshot={'a': None, 'z': [{'flag': True}]})
    assert consent_terms_match(consent, other)
    snapshot['z'].append('later')
    assert consent_terms_match(consent, other)
    with pytest.raises(FrozenInstanceError):
        consent.period_days = 10


@pytest.mark.parametrize(
    'change',
    [
        {'amount_kopeks': 10001},
        {'period_days': 31},
        {'tariff_id': 2},
        {'device_limit': 2},
        {'payment_method': 'sbp'},
        {'service_snapshot': {'squads': ['other'], 'traffic_gb': 0}},
    ],
)
def test_any_changed_consent_requires_new_agreement(change):
    assert not consent_terms_match(terms(), terms(**change))


def test_due_time_is_paid_until_not_monitor_clock_and_normalizes_utc():
    end = datetime(2030, 1, 1, 12, tzinfo=timezone(timedelta(hours=3)))
    assert next_charge_due_at(end) == datetime(2030, 1, 1, 9, tzinfo=UTC)
    assert next_charge_due_at(end).tzinfo is UTC


@pytest.mark.parametrize('period,daily', [(1, True), (1, False), (6, False), (10000, False)])
def test_active_renewal_preserves_remaining_paid_time(period, daily):
    now = datetime(2030, 1, 1, tzinfo=UTC)
    end = now + timedelta(hours=12)
    expected = max(end, now + timedelta(days=1)) if daily else end + timedelta(days=period)
    assert renewed_paid_until(end, accepted_at=now, terms=terms(period_days=period, is_daily=daily)) == expected


def test_late_success_renews_from_acceptance_like_existing_subscription_model():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert renewed_paid_until(now - timedelta(days=10), accepted_at=now, terms=terms()) == now + timedelta(days=30)


@pytest.mark.parametrize('value', [datetime(2030, 1, 1), None, True, '2030-01-01'])
def test_invalid_timestamps_rejected(value):
    with pytest.raises(ValueError):
        next_charge_due_at(value)
    with pytest.raises(ValueError):
        renewed_paid_until(datetime(2030, 1, 1, tzinfo=UTC), accepted_at=value, terms=terms())


def test_calendar_overflow_rejected_before_charge():
    with pytest.raises(ValueError):
        renewed_paid_until(
            datetime.max.replace(tzinfo=UTC), accepted_at=datetime(2030, 1, 1, tzinfo=UTC), terms=terms()
        )
