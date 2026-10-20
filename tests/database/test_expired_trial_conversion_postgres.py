"""#3308: buying another tariff after an expired trial keeps its row and panel link."""

import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import purchase as cabinet
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError
from app.handlers.subscription import tariff_purchase as telegram
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


class Panel(RemnaWaveAPI):
    def __init__(self, now, missing=False):
        super().__init__(base_url='https://panel.example', api_key='test')
        self.missing = missing
        self.patches = []
        self.creates = 0
        self.traffic_used = 17
        self.account = dict(
            id=9001,
            shortUuid='test-short',
            username='test-user',
            status='EXPIRED',
            expireAt=(now - timedelta(days=1)).isoformat(),
            createdAt=now.isoformat(),
            updatedAt=now.isoformat(),
            telegramId=101,
            subscriptionUrl='https://vpn.example/sub/test',
            hwidDeviceLimit=1,
        )

    async def get_user_by_id(self, user_id):
        if user_id == 9003:
            return self._parse_user(self.new_account)
        assert user_id == 9001
        return None if self.missing else self._parse_user(self.account)

    async def create_user(self, **kwargs):
        self.creates += 1
        self.create_kwargs = kwargs.copy()
        self.new_account = dict(
            self.account,
            id=9003,
            expireAt=kwargs['expire_at'].isoformat(),
            subscriptionUrl='https://vpn.example/sub/recreated',
            status='ACTIVE',
        )
        return self._parse_user(self.new_account)

    async def get_user_by_short_uuid(self, short_uuid):
        return None

    async def find_users_by_telegram_id(self, telegram_id):
        return []

    async def get_user_by_username(self, username):
        return None

    async def enrich_user_with_happ_link(self, user):
        return user

    async def _make_request(self, method, path, data=None, **kwargs):
        if (method, path) == ('PATCH', '/api/users'):
            assert data['id'] == 9001
            if self.missing:
                raise RemnaWaveAPIError('User not found', 404, {'errorCode': 'A025'})
            self.patches.append(data.copy())
            self.account.update(data)
            return {'response': self.account}
        assert method == 'POST' and path in [
            '/api/users/9001/actions/reset-traffic',
            '/api/users/9003/actions/reset-traffic',
        ]
        self.traffic_used = 0
        return {'response': self.account if '9001' in path else self.new_account}


@pytest.mark.parametrize('flow', ['auto', 'cabinet', 'telegram'])
@pytest.mark.parametrize('daily', [False, True])
async def test_expired_trial_purchase_preserves_identity(postgres_database, monkeypatch, flow, daily, missing=False):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(
        'app.bot_factory.create_bot', lambda: SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    )
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', False)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr(telegram.user_cart_service, 'delete_user_cart', AsyncMock())
    monkeypatch.setattr(telegram.user_cart_service, 'delete_subscription_cart', AsyncMock())
    notification = AsyncMock()
    monkeypatch.setattr(telegram.AdminNotificationService, 'send_subscription_purchase_notification', notification)

    async def notify(callback):
        await callback(SimpleNamespace(send_subscription_purchase_notification=notification))

    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', notify)
    now = datetime.now(UTC)
    panel = Panel(now, missing=missing)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101, language='ru', status='active', balance_kopeks=10000))
        for tariff_id in [1, 2, 3]:
            db.add(
                Tariff(
                    id=tariff_id,
                    name=f'Test {tariff_id}',
                    is_active=True,
                    traffic_limit_gb=100,
                    device_limit=2,
                    period_prices={'30': 300},
                    is_daily=daily if tariff_id == 1 else False,
                    daily_price_kopeks=100,
                    allowed_squads=['test-squad'],
                )
            )
        db.add_all(
            [
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=2,
                    remnawave_id=9001,
                    remnawave_short_id='test-trial',
                    subscription_url='https://vpn.example/sub/test',
                    status='expired',
                    is_trial=True,
                    end_date=now - timedelta(days=1),
                    traffic_used_gb=17,
                    device_limit=1,
                ),
                Subscription(
                    id=20,
                    user_id=1,
                    tariff_id=3,
                    remnawave_id=9002,
                    remnawave_short_id='test-neighbor',
                    status='active',
                    is_trial=False,
                    end_date=now + timedelta(days=50),
                    traffic_used_gb=7,
                    device_limit=3,
                ),
            ]
        )
        await db.commit()
        user = await get_user_by_id(db, 1)
        if flow == 'auto':
            handler = auto._auto_purchase_daily_tariff if daily else auto._auto_purchase_tariff
            assert await handler(db, user, {'tariff_id': 1, 'period_days': 30})
        elif flow == 'cabinet':
            response = await cabinet.purchase_tariff(
                cabinet.TariffPurchaseRequest(tariff_id=1, period_days=1 if daily else 30), user, db
            )
            assert response['success']
        else:
            state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
            callback = SimpleNamespace(
                data='daily_tariff_confirm:1' if daily else 'tariff_confirm:1:30',
                answer=AsyncMock(),
                bot=SimpleNamespace(send_message=AsyncMock()),
                message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock(), chat=SimpleNamespace(id=101)),
            )
            handler = telegram.confirm_daily_tariff_purchase if daily else telegram.confirm_tariff_purchase
            await inspect.unwrap(handler)(callback, user, db, state)
        subs = (await db.scalars(select(Subscription).where(Subscription.user_id == 1))).all()
        assert len(subs) == 2, 'Expired trial was left behind and a second paid row was inserted'
        sub = await db.get(Subscription, 10)
        await db.refresh(sub)
        await db.refresh(user)
        assert sub.tariff_id == 1 and not sub.is_trial and sub.status == 'active'
        assert sub.remnawave_id == (9003 if missing else 9001)
        assert sub.subscription_url == (
            'https://vpn.example/sub/recreated' if missing else 'https://vpn.example/sub/test'
        )
        assert sub.traffic_used_gb == 0 and sub.device_limit == 2
        assert abs((sub.end_date - (now + timedelta(days=1 if daily else 30))).total_seconds()) < 10
        assert user.balance_kopeks == 10000 - (100 if daily else 300)
        payments = (await db.scalars(select(Transaction))).all()
        assert len(payments) == 1 and payments[0].amount_kopeks == -(100 if daily else 300)
        assert (await db.get(Subscription, 20)).end_date == now + timedelta(days=50)
        assert (await db.get(Subscription, 20)).traffic_used_gb == 7
        assert panel.traffic_used == 0
        assert panel.creates == int(missing) and len(panel.patches) == (0 if missing else 1)
        if missing:
            assert panel.create_kwargs['hwid_device_limit'] == 2
        else:
            assert panel.patches[0]['hwidDeviceLimit'] == 2
        account = panel.new_account if missing else panel.account
        assert abs((datetime.fromisoformat(account['expireAt']) - sub.end_date).total_seconds()) < 1
        notification.assert_awaited_once()
        call = notification.await_args
        converted = call.kwargs.get('was_trial_conversion', call.args[5] if len(call.args) > 5 else None)
        assert converted is True


@pytest.mark.parametrize('case', ['latest', 'live', 'same', 'revive', 'none'])
async def test_conversion_candidate_priority_and_exclusions(postgres_database, monkeypatch, case):
    from app.database.crud.subscription import resolve_trial_conversion_candidate

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all([User(id=1, telegram_id=101), User(id=2, telegram_id=202)])
        for tariff_id in range(1, 8):
            db.add(Tariff(id=tariff_id, name=f'Test {tariff_id}'))
        # Later disabled, pending, paid and foreign records must never win selection.
        rows = [
            (10, 1, 2, 'expired', True, -3),
            (11, 1, 3, 'expired', True, -1),
            (20, 1, 4, 'disabled', True, 20),
            (21, 1, 5, 'pending', True, 20),
            (22, 1, 6, 'expired', False, 10),
            (23, 2, 7, 'expired', True, 30),
        ]
        if case == 'none':
            rows = rows[2:]
        if case in ['live', 'same']:
            rows.append((12, 1, 1 if case == 'same' else 7, 'active', True, 3))
        if case == 'revive':
            rows.append((13, 1, 1, 'expired', False, -5))
        for sub_id, owner, tariff_id, status, trial, days in rows:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    tariff_id=tariff_id,
                    status=status,
                    is_trial=trial,
                    end_date=now + timedelta(days=days),
                    remnawave_short_id=f'test-{sub_id}',
                )
            )
        await db.commit()
        result = await resolve_trial_conversion_candidate(db, 1, 1)
        expected = None if case in ['revive', 'none'] else 12 if case in ['live', 'same'] else 11
        assert (result.id if result else None) == expected


async def test_concurrent_purchases_do_not_convert_same_trial_twice(postgres_database, monkeypatch):
    import asyncio

    from app.database.crud import subscription as crud
    from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_sessions

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    now = datetime.now(UTC)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as (
        first,
        second,
        observer,
    ):
        first.add(User(id=1, telegram_id=101))
        for tariff_id in [1, 2, 3]:
            first.add(Tariff(id=tariff_id, name=f'Test {tariff_id}'))
        first.add(
            Subscription(
                id=10,
                user_id=1,
                tariff_id=2,
                status='expired',
                is_trial=True,
                end_date=now - timedelta(days=1),
                remnawave_id=9001,
                remnawave_short_id='test-trial',
            )
        )
        await first.commit()
        candidates = [await first.get(Subscription, 10), await second.get(Subscription, 10)]
        await second.commit()  # End the pre-load transaction, retaining its ORM candidate.
        original_lock = crud._lock_subscription_row
        holder_ready = asyncio.Event()
        entries = 0
        observed = False

        async def lock(db, subscription):
            nonlocal entries, observed
            entries += 1
            if entries == 1:
                await original_lock(db, subscription)
                holder_ready.set()
                observed = await lock_waiter_appeared(observer)
            else:
                await holder_ready.wait()
                await original_lock(db, subscription)

        monkeypatch.setattr(crud, '_lock_subscription_row', lock)

        async def purchase(db, candidate, tariff_id):
            try:
                result = await crud.create_paid_subscription(db, 1, 30, tariff_id=tariff_id, conversion_trial=candidate)
                return result.id
            finally:
                await db.rollback()

        ids = await asyncio.wait_for(
            asyncio.gather(purchase(first, candidates[0], 1), purchase(second, candidates[1], 3)), timeout=15
        )
        assert observed, 'PostgreSQL must observe the second purchase waiting on the first row lock'
        assert len(set(ids)) == 2 and 10 in ids
        rows = (await observer.scalars(select(Subscription).where(Subscription.user_id == 1))).all()
        assert len(rows) == 2 and {row.tariff_id for row in rows} == {1, 3}
        assert all(not row.is_trial for row in rows)
        assert next(row for row in rows if row.id == 10).remnawave_id == 9001


@pytest.mark.parametrize('flow', ['auto', 'cabinet', 'telegram'])
async def test_missing_trial_panel_account_is_recreated_without_a_second_database_row(
    postgres_database, monkeypatch, flow
):
    await test_expired_trial_purchase_preserves_identity(postgres_database, monkeypatch, flow, False, missing=True)
