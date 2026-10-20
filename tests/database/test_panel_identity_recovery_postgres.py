"""#2953: sync repairs dead panel identities without guessing across subscriptions."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.cabinet.routes.subscription_modules import devices
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, User
from app.external.remnawave_api import RemnaWaveTransientError
from app.handlers.subscription.devices import _get_panel_user_id
from app.services.panel_sync import push_subscription
from app.services.panel_sync.identity import PanelAccountOwnedByAnotherUser
from app.services.remnawave_service import RemnaWaveService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


class Panel:
    def __init__(self, state, end):
        self.state = state
        self.updated = []
        self.created = []
        self.account = SimpleNamespace(
            id=9002 if state == 'foreign' else 9001,
            username='test_account',
            short_uuid='test-short',
            subscription_url='https://vpn.example/sub/test',
            happ_crypto_link=None,
            expire_at=end,
        )

    async def get_user_by_id(self, panel_id):
        if self.state == 'timeout':
            raise RemnaWaveTransientError('test panel unavailable')
        return self.account if panel_id == self.account.id else None

    async def get_user_by_short_uuid(self, short_uuid):
        assert short_uuid == 'test-short'
        return self.account

    async def find_users_by_telegram_id(self, telegram_id):
        return []

    async def find_users_by_email(self, email):
        return []

    async def update_user(self, *, user_id, **kwargs):
        self.updated.append(user_id)
        return self.account

    async def create_user(self, **kwargs):
        self.created.append(kwargs)
        raise AssertionError('Existing identity must not create a duplicate')

    async def get_user_devices_all(self, panel_id):
        assert panel_id == 9001
        return {'total': 1, 'devices': [{'hwid': 'test-phone', 'platform': 'iOS', 'deviceModel': 'Test'}]}


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('state', ['missing_user', 'stale_both', 'stale_user', 'timeout', 'foreign'])
async def test_identity_sync_repairs_only_proven_dead_links(postgres_database, monkeypatch, multi, state):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    end = datetime.now(UTC) + timedelta(days=30)
    panel = Panel(state, end)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(RemnaWaveService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        profile_id = None if state == 'missing_user' else 999
        sub_id = 999 if state in {'stale_both', 'timeout'} else None if state == 'foreign' else 9001
        db.add_all(
            [
                User(id=1, telegram_id=101, status='active', language='ru', remnawave_id=profile_id),
                User(id=2, telegram_id=202, status='active', language='ru', remnawave_id=9002),
                Subscription(
                    id=10,
                    user_id=1,
                    status='active',
                    is_trial=False,
                    end_date=end,
                    remnawave_id=sub_id,
                    remnawave_short_id='test-10',
                    remnawave_short_uuid='test-short',
                    device_limit=2,
                    traffic_limit_gb=100,
                    connected_squads=[],
                ),
                Subscription(
                    id=20,
                    user_id=2,
                    status='active',
                    is_trial=False,
                    end_date=end,
                    remnawave_id=9002,
                    remnawave_short_id='test-20',
                    device_limit=3,
                ),
            ]
        )
        await db.commit()
        user = await get_user_by_id(db, 1)
        sub = await db.get(Subscription, 10)
        if state in {'timeout', 'foreign'}:
            error = RemnaWaveTransientError if state == 'timeout' else PanelAccountOwnedByAnotherUser
            with pytest.raises(error):
                await push_subscription(panel, user, sub, db=db, multi_tariff=multi)
            assert panel.updated == panel.created == []
            assert user.remnawave_id == profile_id and sub.remnawave_id == sub_id
        else:
            result = await push_subscription(panel, user, sub, db=db, multi_tariff=multi)
            await db.commit()
            await db.refresh(user)
            await db.refresh(sub)
            assert result.action == 'updated' and panel.updated == [9001] and panel.created == []
            assert sub.remnawave_id == 9001
            # Multi does not probe or overwrite the profile's different account.
            assert user.remnawave_id == (999 if multi and state == 'stale_user' else 9001)
            response = await devices.get_devices(10, user, db)
            assert response['total'] == 1 and response['devices'][0]['hwid'] == 'test-phone'
            assert _get_panel_user_id(sub, user) == 9001
        other = await db.get(Subscription, 20)
        assert other.remnawave_id == 9002 and other.end_date == end
        assert sub.end_date == end
