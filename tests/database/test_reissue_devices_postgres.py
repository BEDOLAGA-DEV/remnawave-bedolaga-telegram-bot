"""#3086: reissue resets only the selected account and reports partial failure."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes.subscription_modules import revoke as cabinet
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, User
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError
from app.handlers.subscription import revoke as telegram
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres
NEW_URL = 'https://new.example/sub/reissued'


class Panel(RemnaWaveAPI):
    def __init__(self, outcome):
        super().__init__(base_url='https://panel.example', api_key='test')
        self.outcome = outcome
        self.devices = {9001: ['a', 'b'], 9002: ['c']}
        self.revoked = []

    async def revoke_user_subscription(self, panel_id):
        if self.outcome == 'revoke_error':
            raise RemnaWaveAPIError('Revoke failed', 500)
        self.revoked.append(panel_id)
        return SimpleNamespace(
            short_uuid='reissued',
            subscription_url=None if self.outcome == 'empty_url' else NEW_URL,
            happ_crypto_link='happ://crypt5/test-new',
        )

    async def _make_request(self, method, path, data=None, **kwargs):
        assert (method, path) == ('POST', '/api/hwid/devices/delete-all')
        panel_id = data['userId']
        if self.outcome == 'error':
            raise RemnaWaveAPIError('Reset failed', 500)
        self.devices[panel_id] = ['b'] if self.outcome == 'partial' else []
        if self.outcome == 'gone':
            raise RemnaWaveAPIError('Not found', 404)
        return {'response': {'total': len(self.devices[panel_id]), 'devices': self.devices[panel_id]}}


@pytest.mark.parametrize('flow', ['service', 'cabinet', 'bot'])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('outcome', ['ok', 'partial', 'error', 'gone', 'revoke_error', 'empty_url', 'missing_id'])
async def test_reissue_resets_selected_hwid_and_preserves_new_link(
    postgres_database, monkeypatch, flow, multi, outcome
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_REVOKE_ENABLED', True)
    panel = Panel(outcome)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, status='active', language='ru', remnawave_id=9002 if multi else 9001),
                User(id=2, telegram_id=202, status='active', language='ru'),
            ]
        )
        for sub_id, owner, panel_id in [(10, 1, 9001), (20, 1 if multi else 2, 9002)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    remnawave_id=panel_id,
                    remnawave_short_id=f'test-{sub_id}',
                    remnawave_short_uuid=f'short-{sub_id}',
                    status='active',
                    is_trial=False,
                    end_date=datetime.now(UTC) + timedelta(days=30),
                    subscription_url='https://old.example/sub/test',
                    subscription_crypto_link='happ://crypt5/test-old',
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        sub = await db.get(Subscription, 10)
        if outcome == 'missing_id':
            if multi:
                sub.remnawave_id = None
            else:
                user.remnawave_id = None
            await db.commit()
        callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        if flow == 'service':
            result = await SubscriptionService().revoke_subscription(db, sub)
        elif flow == 'cabinet':
            if outcome in {'revoke_error', 'empty_url', 'missing_id'}:
                from fastapi import HTTPException

                with pytest.raises(HTTPException):
                    await cabinet.revoke_subscription(10 if multi else None, user, db)
                result = None
            else:
                result = await cabinet.revoke_subscription(10 if multi else None, user, db)
        else:
            handler = telegram.confirm_subscription_revoke
            while hasattr(handler, '__wrapped__'):
                handler = handler.__wrapped__
            state = SimpleNamespace(
                get_data=AsyncMock(return_value={'revoke_sub_id': 10} if multi else {}), update_data=AsyncMock()
            )
            await handler(callback, user, db, state)
            result = None
        await db.refresh(sub)
        other = await db.get(Subscription, 20, populate_existing=True)
        assert panel.devices[9002] == ['c']
        assert other.subscription_url == 'https://old.example/sub/test'
        if outcome in {'revoke_error', 'empty_url', 'missing_id'}:
            assert sub.subscription_url == 'https://old.example/sub/test'
            assert panel.devices[9001] == ['a', 'b']
            return
        assert panel.revoked == [9001]
        assert sub.subscription_url == NEW_URL
        assert sub.subscription_crypto_link == 'happ://crypt5/test-new'
        reset = outcome in {'ok', 'gone'}
        assert panel.devices[9001] == ([] if reset else ['b'] if outcome == 'partial' else ['a', 'b'])
        if flow == 'service':
            assert result.devices_reset is reset
        elif flow == 'cabinet':
            assert result['devices_reset'] is reset
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as exc:
                await cabinet.revoke_subscription(10 if multi else None, user, db)
            assert exc.value.status_code == 429
            assert panel.revoked == [9001]
        else:
            text = callback.message.edit_text.await_args.args[0]
            if reset:
                assert 'Все устройства были отключены' in text
            else:
                assert 'Все устройства были отключены' not in text
                assert 'не удалось' in text
            await handler(callback, user, db, state)
            assert panel.revoked == [9001]


@pytest.mark.parametrize('flow', ['cabinet', 'bot'])
@pytest.mark.parametrize('selected', [None, 20, 999])
async def test_reissue_never_guesses_or_accepts_a_foreign_subscription(postgres_database, monkeypatch, flow, selected):
    from fastapi import HTTPException

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SUBSCRIPTION_REVOKE_ENABLED', True)
    panel = Panel('ok')

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, status='active', language='ru'),
                User(id=2, telegram_id=202, status='active', language='ru'),
            ]
        )
        for sub_id, owner, panel_id in [(10, 1, 9001), (11, 1, 9002), (20, 2, 9003)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    remnawave_id=panel_id,
                    remnawave_short_id=f'test-{sub_id}',
                    status='active',
                    is_trial=False,
                    end_date=datetime.now(UTC) + timedelta(days=30),
                    subscription_url='https://old.example/sub/test',
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        if flow == 'cabinet':
            with pytest.raises(HTTPException) as exc:
                await cabinet.revoke_subscription(selected, user, db)
            assert exc.value.status_code == (400 if selected is None else 404)
        else:
            callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
            state = SimpleNamespace(
                get_data=AsyncMock(return_value={'revoke_sub_id': selected}), update_data=AsyncMock()
            )
            handler = telegram.confirm_subscription_revoke
            while hasattr(handler, '__wrapped__'):
                handler = handler.__wrapped__
            await handler(callback, user, db, state)
        assert panel.revoked == []
        assert panel.devices == {9001: ['a', 'b'], 9002: ['c']}
