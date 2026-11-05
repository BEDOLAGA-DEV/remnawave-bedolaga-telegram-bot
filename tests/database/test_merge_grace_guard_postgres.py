"""A blocked account merge must preserve both accounts and its retry token."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import account_linking
from app.config import settings
from app.database.models import Base, GraceAccessSessionModel, Subscription, Transaction, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('grace_owner', [1, 2])
@pytest.mark.parametrize('state', ['pending', 'active', 'restoring'])
async def test_open_grace_merge_rejection_preserves_identity_money_and_retry(
    postgres_database, monkeypatch, multi, grace_owner, state
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    now = datetime.now(UTC)
    token_data = {'primary_user_id': 1, 'secondary_user_id': 2}
    restore = AsyncMock()
    panel_delete = AsyncMock()
    monkeypatch.setattr(account_linking, 'consume_merge_token', AsyncMock(return_value=token_data))
    monkeypatch.setattr(account_linking, 'restore_merge_token', restore)
    monkeypatch.setattr(account_linking, 'flush_remnawave_deletions', panel_delete)
    monkeypatch.setattr(account_linking.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        primary = User(id=1, email='primary@example.com', email_verified=True, balance_kopeks=1200, status='active')
        secondary = User(id=2, telegram_id=102, balance_kopeks=800, status='active')
        db.add_all([primary, secondary])
        await db.flush()
        for ident in (1, 2):
            db.add(
                Subscription(
                    id=ident,
                    user_id=ident,
                    status='active',
                    start_date=now,
                    end_date=now + timedelta(days=30),
                    remnawave_id=9000 + ident,
                    remnawave_short_id=f'masked-{ident}',
                    connected_squads=['masked-squad'],
                    traffic_limit_gb=100,
                    device_limit=5,
                )
            )
        await db.flush()
        db.add(
            GraceAccessSessionModel(
                id='masked-grace',
                subscription_id=grace_owner,
                remnawave_id=9000 + grace_owner,
                reason='limited',
                incident_key='masked-incident',
                state=state,
                billing_before={'masked': 'billing'},
                panel_before={'masked': 'panel'},
                overlay={'masked': 'overlay'},
                started_at=now,
                updated_at=now,
                grace_until=now + timedelta(hours=1),
            )
        )
        await db.commit()
        app = FastAPI()
        app.include_router(account_linking.merge_router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = lambda: db
        app.dependency_overrides[get_current_cabinet_user] = lambda: primary
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/cabinet/auth/merge/' + 'a' * 32, json={'keep_subscription_from': 2})
        assert response.status_code == 409, response.text
        restore.assert_awaited_once_with('a' * 32, token_data)
        panel_delete.assert_not_awaited()
        await db.refresh(primary)
        await db.refresh(secondary)
        assert (primary.email, primary.telegram_id, primary.balance_kopeks, primary.status) == (
            'primary@example.com',
            None,
            1200,
            'active',
        )
        assert (secondary.telegram_id, secondary.balance_kopeks, secondary.status) == (102, 800, 'active')
        subs = list(await db.scalars(select(Subscription).order_by(Subscription.id)))
        assert [(sub.id, sub.user_id, sub.remnawave_id) for sub in subs] == [(1, 1, 9001), (2, 2, 9002)]
        assert all(sub.end_date == now + timedelta(days=30) and sub.traffic_limit_gb == 100 for sub in subs)
        grace = await db.get(GraceAccessSessionModel, 'masked-grace')
        assert (grace.state, grace.subscription_id, grace.overlay) == (state, grace_owner, {'masked': 'overlay'})
        assert list(await db.scalars(select(Transaction))) == []
