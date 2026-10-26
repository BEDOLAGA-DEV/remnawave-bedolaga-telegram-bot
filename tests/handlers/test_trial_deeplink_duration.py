from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, Subscription, Tariff, User
from app.handlers import start
from tests.fixtures.sqlite_memory import memory_session


@pytest.fixture
def free_trial(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', False)
    monkeypatch.setattr(settings, 'TRIAL_TARIFF_ID', 0)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(start, 'SubscriptionService', lambda: SimpleNamespace(is_configured=False))


async def activate(db, lookup, days, expected):
    from app.cabinet.routes.subscription_modules.purchase import get_trial_info

    user = User(id=42, telegram_id=42, first_name='Test', language='ru', status='active', subscriptions=[])
    db.add(user)
    if lookup != 'missing':
        db.add(
            Tariff(
                id=1,
                name='Trial',
                is_active=False,
                is_trial_available=lookup == 'flag',
                trial_duration_days=days,
                traffic_limit_gb=10,
                device_limit=2,
                allowed_squads=['test-squad'],
            )
        )
    await db.commit()
    # The cabinet's existing preview is part of the same product contract.
    preview = await get_trial_info(user=user, db=db)
    assert preview.duration_days == expected
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_trial': True}), update_data=AsyncMock())
    await start._activate_pending_trial(db, state, user, AsyncMock(return_value=None))
    subscriptions = (await db.scalars(select(Subscription))).all()
    assert len(subscriptions) == 1
    subscription = subscriptions[0]
    assert subscription.is_trial is True
    assert (subscription.end_date - subscription.start_date).total_seconds() == pytest.approx(expected * 86400, abs=1)
    if lookup != 'missing':
        assert subscription.tariff_id == 1
        assert subscription.traffic_limit_gb == 10
        assert subscription.device_limit == 2


@pytest.mark.parametrize(
    ('lookup', 'days', 'expected'),
    [
        ('flag', 7, 7),
        ('fallback', 7, 7),
        ('flag', None, 3),
        ('fallback', None, 3),
        ('flag', 0, 3),
        ('missing', None, 3),
    ],
)
async def test_deeplink_trial_matches_tariff_and_cabinet_preview(monkeypatch, free_trial, lookup, days, expected):
    if lookup == 'fallback':
        monkeypatch.setattr(settings, 'TRIAL_TARIFF_ID', 1)
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        await activate(db, lookup, days, expected)


@pytest.mark.postgres
@pytest.mark.parametrize('lookup', ['flag', 'fallback'])
async def test_deeplink_trial_duration_persisted_on_postgres(postgres_database, monkeypatch, free_trial, lookup):
    from tests.fixtures.postgres_db import postgres_session

    if lookup == 'fallback':
        monkeypatch.setattr(settings, 'TRIAL_TARIFF_ID', 1)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await activate(db, lookup, 7, 7)


@pytest.mark.parametrize('gate', ['no_pending', 'duration_zero', 'disabled_all', 'disabled_telegram', 'paid', 'used'])
async def test_deeplink_trial_keeps_activation_gates(monkeypatch, free_trial, gate):
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        user = User(
            id=42,
            telegram_id=42,
            first_name='Test',
            language='ru',
            status='active',
            auth_type='telegram',
            subscriptions=[],
            has_had_paid_subscription=gate == 'used',
        )
        db.add(user)
        await db.commit()
        if gate == 'duration_zero':
            monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 0)
        if gate in {'disabled_all', 'disabled_telegram'}:
            monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', gate.removeprefix('disabled_'))
        if gate == 'paid':
            monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', True)
            monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 100)
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={'pending_trial': gate != 'no_pending'}), update_data=AsyncMock()
        )
        await start._activate_pending_trial(db, state, user, AsyncMock(return_value=None))
        assert (await db.scalars(select(Subscription))).all() == []
