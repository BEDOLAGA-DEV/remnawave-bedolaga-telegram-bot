"""Admin trial conversion targets the selected subscription and preserves its cap."""

import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Base, Subscription, User
from app.external.remnawave_api import RemnaWaveAPI
from app.handlers.admin import users
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


class Panel(RemnaWaveAPI):
    def __init__(self, now):
        super().__init__(base_url='https://panel.example', api_key='test')
        self.patches = []
        self.accounts = {
            panel_id: {
                'id': panel_id,
                'shortUuid': f'test-{panel_id}',
                'username': f'test-{panel_id}',
                'expireAt': (now + timedelta(days=10)).isoformat(),
                'createdAt': now.isoformat(),
                'updatedAt': now.isoformat(),
                'telegramId': 101,
                'hwidDeviceLimit': 1,
            }
            for panel_id in [9001, 9002]
        }

    async def get_user_by_id(self, user_id):
        return self._parse_user(self.accounts[user_id])

    async def enrich_user_with_happ_link(self, user):
        return user

    async def _make_request(self, method, path, data=None, **kwargs):
        assert (method, path) == ('PATCH', '/api/users')
        self.patches.append(data.copy())
        self.accounts[data['id']].update(data)
        return {'response': self.accounts[data['id']]}


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('cap', [0, 1, 3])
async def test_admin_trial_conversion_sends_selected_cap(postgres_database, monkeypatch, multi, cap, selected=10):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', True)
    now = datetime.now(UTC)
    panel = Panel(now)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all([User(id=1, telegram_id=101, language='ru', remnawave_id=9001), User(id=2, telegram_id=202)])
        for sub_id, owner, days in [(10, 1, 5), (20, 1 if multi and selected != 20 else 2, 30)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    remnawave_id=9001 if sub_id == 10 else 9002,
                    remnawave_short_id=f'test-{sub_id}',
                    is_trial=True,
                    status='active',
                    end_date=now + timedelta(days=days),
                    device_limit=cap if sub_id == 10 else 7,
                )
            )
        await db.commit()
        callback = SimpleNamespace(
            data=f'admin_sub_type_paid_1_s{selected}' if multi else 'admin_sub_type_paid_1',
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        handler = inspect.unwrap(users.change_subscription_type_confirm)
        await handler(callback, SimpleNamespace(id=99), db)
        await db.refresh(await db.get(Subscription, 10))
        await db.refresh(await db.get(Subscription, 20))
        valid = selected == 10
        assert (await db.get(Subscription, 10)).is_trial is not valid
        assert (await db.get(Subscription, 20)).is_trial
        assert (await db.get(Subscription, 10)).device_limit == cap
        assert (await db.get(Subscription, 20)).device_limit == 7
        assert len(panel.patches) == int(valid)
        if valid:
            assert panel.patches[0]['id'] == 9001
            assert panel.patches[0]['hwidDeviceLimit'] == cap
            # An already-converted subscription must not trigger another panel write.
            await handler(callback, SimpleNamespace(id=99), db)
            assert len(panel.patches) == 1


@pytest.mark.parametrize('selected', [20, 999])
async def test_explicit_foreign_or_missing_subscription_never_falls_back(postgres_database, monkeypatch, selected):
    await test_admin_trial_conversion_sends_selected_cap(postgres_database, monkeypatch, True, 1, selected=selected)


@pytest.mark.parametrize('subscription_id', [0, 10])
async def test_explicit_multi_callback_after_mode_change_is_rejected(postgres_database, monkeypatch, subscription_id):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101))
        db.add(
            Subscription(
                id=10,
                user_id=1,
                is_trial=True,
                status='active',
                remnawave_short_id='test',
                end_date=datetime.now(UTC) + timedelta(days=5),
            )
        )
        await db.commit()
        assert not await users._change_subscription_type(db, 1, 'paid', 99, subscription_id)
        await db.refresh(await db.get(Subscription, 10))
        assert (await db.get(Subscription, 10)).is_trial
