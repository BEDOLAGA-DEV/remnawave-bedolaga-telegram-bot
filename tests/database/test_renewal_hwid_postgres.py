"""#2968: same-tariff renewal must preserve HWID unless reset is explicitly enabled."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import purchase as cabinet
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.external.remnawave_api import RemnaWaveAPI
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


class Panel(RemnaWaveAPI):
    def __init__(self, end):
        super().__init__(base_url='https://panel.example', api_key='test')
        self.devices = {9001: ['phone', 'laptop'], 9002: ['other']}
        self.updated = []
        self.account = SimpleNamespace(
            id=9001,
            short_uuid='test-short',
            subscription_url='https://vpn.example/sub/test',
            happ_crypto_link=None,
            expire_at=end,
            telegram_id=101,
        )

    async def get_user_by_id(self, user_id):
        assert user_id == 9001
        return self.account

    async def update_user(self, user_id, *args, **kwargs):
        assert user_id == 9001
        self.updated.append(kwargs)
        self.account.expire_at = kwargs.get('expire_at', self.account.expire_at)
        return self.account

    async def reset_user_traffic(self, user_id):
        assert user_id == 9001
        return self.account

    async def _make_request(self, method, path, data=None, **kwargs):
        # Keep reset_user_devices real: an unintended call removes observable HWID.
        assert (method, path) == ('POST', '/api/hwid/devices/delete-all')
        self.devices[data['userId']] = []
        return {'response': {'total': 0, 'devices': []}}


@pytest.mark.parametrize(('flow', 'reset_devices'), [('auto', False), ('cabinet', False), ('auto', True)])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('expired', [False, True])
@pytest.mark.parametrize('trial', [False, True])
async def test_same_tariff_90_day_purchase_preserves_hwid(
    postgres_database, monkeypatch, flow, reset_devices, multi, expired, trial
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', reset_devices)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    now = datetime.now(UTC)
    end = now + timedelta(days=-1 if expired else 5)
    panel = Panel(end)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(
                    id=1,
                    telegram_id=101,
                    language='ru',
                    status='active',
                    balance_kopeks=10000,
                    remnawave_id=9002 if multi else 9001,
                ),
                User(id=2, telegram_id=202, language='ru', status='active'),
                Tariff(
                    id=1,
                    name='Test',
                    is_active=True,
                    traffic_limit_gb=100,
                    device_limit=2,
                    period_prices={'30': 300, '90': 900},
                    allowed_squads=['test-squad'],
                ),
            ]
        )
        await db.commit()
        for sub_id, owner, panel_id in [(10, 1, 9001), (20, 1 if multi else 2, 9002)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    tariff_id=1 if sub_id == 10 else None,
                    remnawave_id=panel_id,
                    remnawave_short_id=f'test-{sub_id}',
                    status='expired' if expired and sub_id == 10 else 'active',
                    is_trial=trial if sub_id == 10 else False,
                    start_date=now - timedelta(days=30),
                    end_date=end if sub_id == 10 else now + timedelta(days=50),
                    traffic_limit_gb=100,
                    traffic_used_gb=17,
                    device_limit=2,
                    connected_squads=['test-squad'],
                    subscription_url='https://vpn.example/sub/test',
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        if flow == 'auto':
            assert await auto._auto_purchase_tariff(
                db, user, {'tariff_id': 1, 'period_days': 90, 'subscription_id': 10}
            )
        else:
            response = await cabinet.purchase_tariff(
                cabinet.TariffPurchaseRequest(tariff_id=1, period_days=90, subscription_id=10), user, db
            )
            assert response['success']
        sub = await db.get(Subscription, 10)
        await db.refresh(sub)
        await db.refresh(user)
        assert panel.updated, 'Panel sync must succeed, not silently skip preserving devices'
        assert panel.updated[-1]['hwid_device_limit'] == 2
        assert panel.devices[9001] == ([] if reset_devices else ['phone', 'laptop'])
        assert panel.devices[9002] == ['other']
        assert sub.status == 'active' and not sub.is_trial and sub.tariff_id == 1
        assert sub.device_limit == 2 and sub.traffic_used_gb == 0
        expected_end = (now if expired else end) + timedelta(days=90)
        assert abs((sub.end_date - expected_end).total_seconds()) < 10
        assert abs((panel.account.expire_at - sub.end_date).total_seconds()) < 1
        assert user.balance_kopeks == 9100
        transactions = (await db.scalars(select(Transaction))).all()
        assert len(transactions) == 1 and transactions[0].amount_kopeks == -900
        assert (await db.get(Subscription, 20)).end_date == now + timedelta(days=50)
