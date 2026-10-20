"""#3325: admin reset must extinguish the panel date before the next import."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import UpdateSubscriptionRequest
from app.config import settings
from app.database.crud.subscription import reactivate_subscription
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Transaction, TransactionType, User
from app.external.remnawave_api import RemnaWaveAPI
from app.services.panel_sync import ADMIN_PULL, BULK_SNAPSHOT, ROUTINE, project_onto_subscription, read_panel_user
from app.services.subscription_service import SubscriptionService, reset_subscription_with_panel
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


class Panel(RemnaWaveAPI):
    def __init__(self, now, expiry_error=False):
        super().__init__(base_url='https://panel.example', api_key='test')
        self.expiry_error = expiry_error
        self.patches = []
        self.reads = []
        self.echoes = []
        self.accounts = {
            key: dict(
                id=key,
                shortUuid=f'test-{key}',
                username=f'test-{key}',
                expireAt=(now + timedelta(days=30)).isoformat(),
                createdAt=now.isoformat(),
                updatedAt=now.isoformat(),
                telegramId=101,
                status='ACTIVE',
            )
            for key in [9001, 9002]
        }

    async def get_user_by_id(self, user_id):
        self.reads.append(user_id)
        return self._parse_user(self.accounts[user_id])

    async def enrich_user_with_happ_link(self, user):
        return user

    async def _make_request(self, method, path, data=None, **kwargs):
        if method == 'PATCH' and path == '/api/users':
            self.patches.append(data.copy())
            if self.expiry_error is True:
                raise RuntimeError('test panel write unavailable')
            if self.expiry_error != 'ignored':
                self.accounts[data['id']].update(data)
            self.echoes.append(self.accounts[data['id']].copy())
            return {'response': self.accounts[data['id']]}
        assert method == 'POST' and path.endswith('/actions/disable')
        key = int(path.split('/')[3])
        self.accounts[key]['status'] = 'DISABLED'
        return {'response': self.accounts[key]}


@pytest.mark.parametrize('flow', ['telegram', 'cabinet'])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('policy', [ROUTINE, BULK_SNAPSHOT, ADMIN_PULL], ids=lambda p: p.name)
async def test_reset_does_not_regain_paid_days_from_panel(
    postgres_database, monkeypatch, flow, multi, policy, expiry_error=False
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    now = datetime.now(UTC)
    panel = Panel(now, expiry_error=expiry_error)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, language='ru', remnawave_id=9002 if multi else 9001),
                User(id=2, telegram_id=202, language='ru'),
            ]
        )
        for sub_id, owner, panel_id in [(10, 1, 9001), (20, 1 if multi else 2, 9002)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    remnawave_id=panel_id,
                    remnawave_short_id=f'test-{sub_id}',
                    status='active',
                    is_trial=False,
                    end_date=now + timedelta(days=30),
                    device_limit=1,
                    autopay_enabled=True,
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        sub = await db.get(Subscription, 10)
        if flow == 'telegram':
            result = await reset_subscription_with_panel(db, user, sub)
            assert result['panel_disabled']
            assert result['panel_expiry_extinguished'] is (not expiry_error)
        else:
            result = await admin_users.update_user_subscription(
                1, UpdateSubscriptionRequest(action='reset', subscription_id=10 if multi else None), user, db
            )
            assert result.success
        if expiry_error:
            assert sub.status == 'disabled'
            assert sub.end_date <= datetime.now(UTC)
            assert not sub.autopay_enabled
            assert panel.accounts[9001]['status'] == 'DISABLED'
            assert datetime.fromisoformat(panel.accounts[9001]['expireAt']) == now + timedelta(days=30)
            return
        snapshot = read_panel_user(panel._parse_user(panel.accounts[9001]))
        project_onto_subscription(sub, snapshot, policy=policy)
        await db.commit()
        assert sub.end_date < now + timedelta(minutes=6), 'Panel import resurrected paid days after reset'
        # The artificial future date needed by panel validation must not restore 30 paid days.
        await reactivate_subscription(db, sub)
        assert sub.end_date < now + timedelta(minutes=6)
        assert not sub.autopay_enabled
        assert (await db.get(Subscription, 20)).end_date == now + timedelta(days=30)
        assert panel.accounts[9002]['status'] == 'ACTIVE'
        assert len(panel.patches) == 1
        assert set(panel.patches[0]) == {'id', 'status', 'expireAt'}


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('read_failure', [False, True])
async def test_expired_update_sends_closing_date_in_first_patch(postgres_database, monkeypatch, live, read_failure):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    now = datetime.now(UTC)
    panel = Panel(now)
    original_read = panel.get_user_by_id

    async def read(user_id):
        if read_failure:
            raise RuntimeError('test panel read unavailable')
        return await original_read(user_id)

    panel.get_user_by_id = read

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101, language='ru', remnawave_id=9001))
        sub = Subscription(
            id=10,
            user_id=1,
            remnawave_id=9001,
            remnawave_short_id='test',
            status='active' if live else 'expired',
            is_trial=False,
            end_date=now + timedelta(days=10 if live else -2),
            device_limit=1,
        )
        db.add(sub)
        await db.commit()
        assert await SubscriptionService().update_remnawave_user(db, sub)
        if live:
            assert panel.reads == []
            assert len(panel.patches) == 1
        elif read_failure:
            assert len(panel.patches) == 2  # Preserve the old fallback if GET is unavailable.
        else:
            assert panel.reads == [9001]
            assert len(panel.patches) == 1, 'Two PATCHes emit an initial webhook with the old paid date'
            assert datetime.fromisoformat(panel.patches[0]['expireAt']) < now + timedelta(minutes=6)
        assert sub.end_date == now + timedelta(days=10 if live else -2)
        if not live and not read_failure:
            from app.services.remnawave_retry_queue import remnawave_retry_queue
            from app.services.remnawave_webhook_service import RemnaWaveWebhookService

            db.add(
                Transaction(
                    user_id=1,
                    type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                    amount_kopeks=-1000,
                    is_completed=True,
                    created_at=now - timedelta(minutes=2),
                )
            )
            await db.commit()
            enqueue = MagicMock()
            monkeypatch.setattr(remnawave_retry_queue, 'enqueue', enqueue)
            user = await get_user_by_id(db, 1)
            webhook = RemnaWaveWebhookService(bot=MagicMock())
            for echo in panel.echoes:
                await webhook._handle_user_modified(db, user, sub, echo)
            # Until the panel's five-minute closing date passes, its ACTIVE echo
            # can be projected locally; the paid 30-day term must never return.
            assert sub.end_date < now + timedelta(minutes=6)
            enqueue.assert_not_called()


@pytest.mark.parametrize('flow', ['telegram', 'cabinet'])
@pytest.mark.parametrize('failure', [True, 'ignored'])
async def test_reset_preserves_local_reset_when_expiry_patch_fails(postgres_database, monkeypatch, flow, failure):
    await test_reset_does_not_regain_paid_days_from_panel(
        postgres_database, monkeypatch, flow, True, ROUTINE, expiry_error=failure
    )
