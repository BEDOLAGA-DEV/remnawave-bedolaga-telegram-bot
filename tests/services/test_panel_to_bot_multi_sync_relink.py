"""Синхронизация из панели (мультитариф): пересозданный аккаунт перепривязывается, а не дублируется.

Репорт: аккаунт панели пересоздали с новым id, в ``subscriptions.remnawave_id``
остался старый. Проход не находил строку по новому id, считал аккаунт новым и
вставлял вторую активную подписку той же пары (user_id, tariff_id) →
IntegrityError по ``uq_subscriptions_user_tariff_active`` → откат всей
транзакции: не синхронизировался никто.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, User
from app.services.remnawave_service import RemnaWaveService, _relink_existing_subscription
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)
DEAD_ID = 555
NEW_ID = 777
NOW = datetime.now(UTC)


def _panel_account(panel_id: int, *, username: str, short_uuid: str, email: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=panel_id,
        short_uuid=short_uuid,
        username=username,
        status=SimpleNamespace(value='ACTIVE'),
        telegram_id=None,
        email=email,
        expire_at=NOW + timedelta(days=30),
        used_traffic_bytes=0,
        traffic_limit_bytes=0,
        hwid_device_limit=2,
        subscription_url=f'https://panel/{panel_id}',
        happ_crypto_link='',
        active_internal_squads=[],
    )


def _service(monkeypatch, accounts: list[SimpleNamespace]) -> RemnaWaveService:
    api = AsyncMock()
    api.get_all_users_page_stream.return_value = {'users': accounts, 'hasMore': False, 'nextCursor': None}

    @contextlib.asynccontextmanager
    async def _client(self):
        yield api

    monkeypatch.setattr(RemnaWaveService, 'get_api_client', _client)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(
        'app.services.grace_access_runtime.get_open_grace_subscription_ids', AsyncMock(return_value=set())
    )
    return RemnaWaveService()


async def _seed(db, *, email: str, short_id: str, panel_id: int | None) -> tuple[User, Subscription]:
    user = User(email=email, email_verified=True, first_name='U', language='ru', status='active')
    db.add(user)
    await db.flush()
    sub = Subscription(
        user_id=user.id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=NOW,
        end_date=NOW + timedelta(days=10),
        traffic_limit_gb=0,
        device_limit=2,
        connected_squads=[],
        remnawave_id=panel_id,
        remnawave_short_id=short_id,
    )
    db.add(sub)
    await db.commit()
    return user, sub


@pytest.mark.asyncio
async def test_recreated_account_is_relinked_to_the_existing_row(monkeypatch):
    service = _service(
        monkeypatch, [_panel_account(NEW_ID, username='user_1_abc123', short_uuid='su-new', email='a@example.com')]
    )
    async with memory_session(monkeypatch, TABLES) as db:
        _, sub = await _seed(db, email='a@example.com', short_id='abc123', panel_id=DEAD_ID)

        stats = await service._sync_users_from_panel_multi(db, 'all')

        rows = (await db.execute(select(Subscription))).scalars().all()
        assert len(rows) == 1, 'вместо перепривязки вставлена вторая подписка'
        assert rows[0].id == sub.id
        assert rows[0].remnawave_id == NEW_ID
        assert stats['created'] == 0
        assert stats['errors'] == 0


@pytest.mark.asyncio
async def test_row_update_reads_the_payment_date_of_its_own_user(monkeypatch):
    """Строка нашлась по id сразу: проход не должен падать на несуществующей переменной."""
    service = _service(
        monkeypatch, [_panel_account(NEW_ID, username='user_1_abc123', short_uuid='su', email='a@example.com')]
    )
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, email='a@example.com', short_id='abc123', panel_id=NEW_ID)

        stats = await service._sync_users_from_panel_multi(db, 'all')

        assert stats == {'created': 0, 'updated': 1, 'errors': 0, 'deleted': 0}


def _row(**kw) -> SimpleNamespace:
    base = {'id': 1, 'remnawave_id': DEAD_ID, 'remnawave_short_id': 'abc123', 'remnawave_short_uuid': None}
    return SimpleNamespace(**(base | kw))


def test_row_bound_to_a_live_account_is_never_relinked():
    row = _row(remnawave_id=DEAD_ID)
    account = {'username': 'user_1_abc123', 'shortUuid': 'su'}

    assert _relink_existing_subscription([row], account, NEW_ID, live_panel_ids={DEAD_ID, NEW_ID}) is None
    assert row.remnawave_id == DEAD_ID


def test_unrelated_account_of_the_same_person_is_not_relinked():
    row = _row()
    account = {'username': 'user_1_zzz999', 'shortUuid': 'other'}

    assert _relink_existing_subscription([row], account, NEW_ID, live_panel_ids={NEW_ID}) is None


def test_short_uuid_match_relinks():
    row = _row(remnawave_short_id=None, remnawave_short_uuid='su')

    assert _relink_existing_subscription([row], {'username': 'x', 'shortUuid': 'su'}, NEW_ID, live_panel_ids={NEW_ID})
    assert row.remnawave_id == NEW_ID
