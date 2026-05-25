"""Tests for subscription_days promocode extension of expired subscriptions.

Covers the multi-tariff selection logic in PromoCodeService._apply_promocode_effects:
which subscription becomes the target, when the picker is raised, and which
statuses are excluded.

Strategy: monkeypatch get_extendable_subscriptions_by_user_id to inject a
crafted list of SimpleNamespace subscriptions; monkeypatch extend_subscription
and the remnawave update so the service flow completes without touching the
database. Assertions then check either the dict returned by activate_promocode
or the picker payload it returned.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.database.models import PromoCodeType, SubscriptionStatus
from app.services.promocode_service import PromoCodeService


def _make_sub(sub_id, status, days_left=10, is_daily=False):
    tariff = SimpleNamespace(name=f'Tariff{sub_id}', is_daily=is_daily)
    return SimpleNamespace(
        id=sub_id,
        status=status,
        days_left=days_left,
        is_trial=False,
        tariff=tariff,
        tariff_id=sub_id,
    )


@pytest.fixture
def subscription_promocode():
    """A valid SUBSCRIPTION_DAYS promocode."""
    return SimpleNamespace(
        id=99,
        code='TESTSUB',
        type=PromoCodeType.SUBSCRIPTION_DAYS.value,
        balance_bonus_kopeks=0,
        subscription_days=30,
        max_uses=10,
        current_uses=0,
        is_active=True,
        is_valid=True,
        promo_group_id=None,
        promo_group=None,
        valid_from=datetime.now(UTC) - timedelta(days=1),
        valid_until=datetime.now(UTC) + timedelta(days=30),
        first_purchase_only=False,
        created_at=datetime.now(UTC),
        tariff_id=None,
    )


@pytest.fixture
def user_with_id_42():
    return SimpleNamespace(
        id=42,
        telegram_id=999,
        username='someuser',
        full_name='Some User',
        email=None,
        balance_kopeks=0,
        language='ru',
        has_had_paid_subscription=True,
        promo_offer_discount_percent=0,
        promo_offer_discount_source=None,
        promo_offer_discount_expires_at=None,
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def patched_service(monkeypatch, user_with_id_42, subscription_promocode):
    """Patch all CRUD dependencies of activate_promocode to return harmless defaults.

    Tests override patches for the helper / extend_subscription as needed.
    """
    monkeypatch.setattr(
        'app.services.promocode_service.get_user_by_id',
        AsyncMock(return_value=user_with_id_42),
    )
    monkeypatch.setattr(
        'app.services.promocode_service.get_promocode_by_code',
        AsyncMock(return_value=subscription_promocode),
    )
    monkeypatch.setattr(
        'app.services.promocode_service.check_user_promocode_usage',
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        'app.database.crud.promocode.count_user_recent_activations',
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        'app.services.promocode_service.create_promocode_use',
        AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    # Patch on the class (not the instance) so Pydantic's strict
    # field-only assignment doesn't reject the override.
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    # extend_subscription is imported at module top in promocode_service,
    # so patch it where it lives in the service namespace.
    monkeypatch.setattr(
        'app.services.promocode_service.extend_subscription',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService.update_remnawave_user',
        AsyncMock(return_value=None),
    )
    service = PromoCodeService()
    return service


def _db_mock():
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    db.delete = AsyncMock()
    db.execute = AsyncMock()
    return db


# --- scenarios -----------------------------------------------------------------


async def test_single_active_sub_applies_silently(monkeypatch, patched_service):
    """One ACTIVE sub → applied without picker."""
    sub = _make_sub(1, SubscriptionStatus.ACTIVE.value, days_left=5)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_extendable_subscriptions_by_user_id',
        AsyncMock(return_value=[sub]),
    )

    result = await patched_service.activate_promocode(_db_mock(), user_id=42, code='TESTSUB')

    assert result['success'] is True


async def test_single_expired_sub_applies_silently(monkeypatch, patched_service):
    """One EXPIRED sub → applied without picker (revival case)."""
    sub = _make_sub(1, SubscriptionStatus.EXPIRED.value, days_left=0)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_extendable_subscriptions_by_user_id',
        AsyncMock(return_value=[sub]),
    )

    result = await patched_service.activate_promocode(_db_mock(), user_id=42, code='TESTSUB')

    assert result['success'] is True


async def test_active_plus_expired_triggers_picker_with_status(monkeypatch, patched_service):
    """1 ACTIVE + 1 EXPIRED → picker, payload exposes status for each."""
    subs = [
        _make_sub(1, SubscriptionStatus.ACTIVE.value, days_left=5),
        _make_sub(2, SubscriptionStatus.EXPIRED.value, days_left=-3),
    ]
    monkeypatch.setattr(
        'app.database.crud.subscription.get_extendable_subscriptions_by_user_id',
        AsyncMock(return_value=subs),
    )

    result = await patched_service.activate_promocode(_db_mock(), user_id=42, code='TESTSUB')

    assert result['success'] is False
    assert result['error'] == 'select_subscription'
    payload = result['eligible_subscriptions']
    assert len(payload) == 2
    assert {p['status'] for p in payload} == {
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.EXPIRED.value,
    }


async def test_active_plus_disabled_triggers_picker(monkeypatch, patched_service):
    """1 ACTIVE + 1 DISABLED → picker.

    DISABLED is reachable from user-facing flows (deactivate via cabinet,
    daily insufficient balance, panel webhook), so the promocode picker
    must offer it as a revival target — not silently apply to the active
    one only. This matches the bug report from manual testing.
    """
    subs = [
        _make_sub(1, SubscriptionStatus.ACTIVE.value, days_left=5),
        _make_sub(2, SubscriptionStatus.DISABLED.value, days_left=0),
    ]
    monkeypatch.setattr(
        'app.database.crud.subscription.get_extendable_subscriptions_by_user_id',
        AsyncMock(return_value=subs),
    )

    result = await patched_service.activate_promocode(_db_mock(), user_id=42, code='TESTSUB')

    assert result['success'] is False
    assert result['error'] == 'select_subscription'
    assert {p['status'] for p in result['eligible_subscriptions']} == {
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.DISABLED.value,
    }


async def test_two_active_still_triggers_picker(monkeypatch, patched_service):
    """2 ACTIVE → picker (preserves existing behavior)."""
    subs = [
        _make_sub(1, SubscriptionStatus.ACTIVE.value),
        _make_sub(2, SubscriptionStatus.ACTIVE.value),
    ]
    monkeypatch.setattr(
        'app.database.crud.subscription.get_extendable_subscriptions_by_user_id',
        AsyncMock(return_value=subs),
    )

    result = await patched_service.activate_promocode(_db_mock(), user_id=42, code='TESTSUB')

    assert result['error'] == 'select_subscription'
    assert len(result['eligible_subscriptions']) == 2


async def test_two_expired_triggers_picker(monkeypatch, patched_service):
    """2 EXPIRED → picker (new behavior: user chooses which to revive)."""
    subs = [
        _make_sub(1, SubscriptionStatus.EXPIRED.value, days_left=-2),
        _make_sub(2, SubscriptionStatus.EXPIRED.value, days_left=-5),
    ]
    monkeypatch.setattr(
        'app.database.crud.subscription.get_extendable_subscriptions_by_user_id',
        AsyncMock(return_value=subs),
    )

    result = await patched_service.activate_promocode(_db_mock(), user_id=42, code='TESTSUB')

    assert result['error'] == 'select_subscription'
    assert all(p['status'] == SubscriptionStatus.EXPIRED.value for p in result['eligible_subscriptions'])


async def test_zero_eligible_returns_no_subscription_error(monkeypatch, patched_service):
    """Zero eligible subs → ValueError → returned as no_subscription_for_days."""
    monkeypatch.setattr(
        'app.database.crud.subscription.get_extendable_subscriptions_by_user_id',
        AsyncMock(return_value=[]),
    )

    result = await patched_service.activate_promocode(_db_mock(), user_id=42, code='TESTSUB')

    assert result['success'] is False
    assert result['error'] == 'no_subscription_for_days'


async def test_explicit_subscription_id_pointing_at_expired_succeeds(monkeypatch, patched_service):
    """User passes subscription_id for an EXPIRED sub explicitly → applied."""
    subs = [
        _make_sub(1, SubscriptionStatus.ACTIVE.value),
        _make_sub(2, SubscriptionStatus.EXPIRED.value),
    ]
    monkeypatch.setattr(
        'app.database.crud.subscription.get_extendable_subscriptions_by_user_id',
        AsyncMock(return_value=subs),
    )

    result = await patched_service.activate_promocode(
        _db_mock(), user_id=42, code='TESTSUB', subscription_id=2
    )

    assert result['success'] is True


async def test_explicit_subscription_id_not_in_eligible_returns_not_found(monkeypatch, patched_service):
    """User passes subscription_id that's not in eligible (e.g. DISABLED) → subscription_not_found."""
    sub = _make_sub(1, SubscriptionStatus.ACTIVE.value)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_extendable_subscriptions_by_user_id',
        AsyncMock(return_value=[sub]),
    )

    result = await patched_service.activate_promocode(
        _db_mock(), user_id=42, code='TESTSUB', subscription_id=999
    )

    assert result['success'] is False
    assert result['error'] == 'subscription_not_found'


async def test_daily_tariff_filtered_out(monkeypatch, patched_service):
    """A daily-tariff sub stays excluded from `eligible` and doesn't trigger picker alone."""
    subs = [
        _make_sub(1, SubscriptionStatus.ACTIVE.value, is_daily=True),
        _make_sub(2, SubscriptionStatus.ACTIVE.value, is_daily=False),
    ]
    monkeypatch.setattr(
        'app.database.crud.subscription.get_extendable_subscriptions_by_user_id',
        AsyncMock(return_value=subs),
    )

    result = await patched_service.activate_promocode(_db_mock(), user_id=42, code='TESTSUB')

    # Only non-daily is eligible → len == 1 → applied silently to sub 2
    assert result['success'] is True
