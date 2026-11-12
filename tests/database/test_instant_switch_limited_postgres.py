"""PR #3360: an actual tariff switch must recover traffic access in billing and grace."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.schemas.subscription import TariffPurchaseRequest
from app.config import settings
from app.database.models import GraceAccessSessionModel, Subscription, Tariff, Transaction
from app.external.remnawave_api import UserStatus
from app.handlers.subscription import tariff_purchase
from app.services import grace_access_runtime as rt
from app.services.grace_access_service import (
    GraceAccessMode,
    GraceAccessSession,
    GraceBillingState,
    GracePanelOverlay,
    GracePanelSnapshot,
    GraceReason,
    GraceSessionState,
)
from app.services.subscription_service import SubscriptionService
from app.webapi.routes import miniapp
from app.webapi.schemas.miniapp import MiniAppTariffSwitchRequest
from tests.database.test_multi_tariff_switch_postgres import route, switch_data
from tests.services.test_grace_access_runtime import FakeRemnawaveApi, make_panel_user


pytestmark = pytest.mark.postgres
GIB = 1024**3
REGULAR = '11111111-1111-1111-1111-111111111111'
GRACE = '22222222-2222-2222-2222-222222222222'


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize(
    ('limit', 'used', 'reset', 'expected_status', 'expected_used'),
    [
        (300, 150.03, False, 'active', 150.03),
        (0, 900, False, 'active', 900),
        (100, 150.03, False, 'limited', 150.03),
        (100, 150.03, True, 'active', 0),
    ],
)
async def test_switch_persists_recovered_status_without_free_traffic(
    postgres_database, monkeypatch, multi, limit, used, reset, expected_status, expected_used
):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
        monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', reset)
        source = await db.get(Subscription, 11)
        source.status = 'limited'
        source.traffic_used_gb = used
        (await db.get(Tariff, 3)).traffic_limit_gb = limit
        if not multi:
            await db.delete(await db.get(Subscription, 12))
        await db.commit()
        result = await route.switch_tariff(TariffPurchaseRequest(tariff_id=3, period_days=30), user, db, 11)
        await db.refresh(source)
        await db.refresh(user)
        assert source.status == expected_status
        assert source.traffic_used_gb == expected_used
        assert source.traffic_limit_gb == limit
        assert result['charged_kopeks'] == 666
        assert user.balance_kopeks == 9334


@pytest.mark.parametrize('flow', ['cabinet', 'telegram'])
async def test_switch_completes_grace_and_restores_new_tariff_on_panel(postgres_database, monkeypatch, flow):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', False)
        monkeypatch.setattr(rt.grace_access_runtime, '_mode', GraceAccessMode.ACTIVE)
        monkeypatch.setattr(rt, 'announce_grace_event', AsyncMock())
        source = await db.get(Subscription, 11)
        source.status = 'limited'
        source.traffic_used_gb = 150.03
        source.traffic_limit_gb = 150
        source.remnawave_id = 4242
        source.connected_squads = [REGULAR]
        (await db.get(Tariff, 3)).allowed_squads = [REGULAR]
        now = datetime.now(UTC)
        end = source.end_date
        overlay_end = now + timedelta(days=1)
        billing = GraceBillingState(
            subscription_id=11,
            remnawave_id=4242,
            status='limited',
            end_at=end,
            traffic_limit_bytes=150 * GIB,
            used_traffic_bytes=int(150.03 * GIB),
            device_limit=2,
            squad_uuids=(REGULAR,),
        )
        await db.commit()
        await rt.SQLAlchemyGraceSessionStore(db, subscription_id=11).create(
            GraceAccessSession(
                id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                subscription_id=11,
                remnawave_id=4242,
                reason=GraceReason.LIMITED,
                incident_key='limited:test-switch',
                state=GraceSessionState.ACTIVE,
                billing_before=billing,
                panel_before=GracePanelSnapshot(
                    remnawave_id=4242,
                    status='LIMITED',
                    expire_at=end,
                    traffic_limit_bytes=150 * GIB,
                    used_traffic_bytes=int(150.03 * GIB),
                    squad_uuids=(REGULAR,),
                ),
                overlay=GracePanelOverlay(
                    status='ACTIVE', expire_at=overlay_end, traffic_limit_bytes=151 * GIB, squad_uuids=(GRACE,)
                ),
                started_at=now,
                grace_until=overlay_end,
                updated_at=now,
            )
        )
        await db.commit()
        panel_user = make_panel_user(
            status=UserStatus.ACTIVE, expire_at=overlay_end, traffic_limit_bytes=151 * GIB, squad_uuids=(GRACE,)
        )
        panel_user.subscription_url = 'https://sub.example/test'
        panel_user.happ_crypto_link = None
        api = FakeRemnawaveApi(panel_user)
        api.reset_user_devices = AsyncMock(return_value=True)
        api.get_internal_squads = AsyncMock(return_value=[SimpleNamespace(uuid=REGULAR)])

        @asynccontextmanager
        async def client(_self):
            yield api

        monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
        monkeypatch.setattr(route, 'SubscriptionService', SubscriptionService)
        from app.services.remnawave_service import RemnaWaveService

        monkeypatch.setattr(RemnaWaveService, 'get_api_client', client)
        if flow == 'cabinet':
            await route.switch_tariff(TariffPurchaseRequest(tariff_id=3, period_days=30), user, db, 11)
        else:
            callback = MagicMock(data='instant_sw_confirm:3')
            callback.answer = AsyncMock()
            callback.message.edit_text = AsyncMock()
            state = AsyncMock()
            state.get_data.return_value = {'active_subscription_id': 11}
            await tariff_purchase.confirm_instant_switch(callback, user, db, state)
        await db.refresh(source)
        grace = (await db.scalars(select(GraceAccessSessionModel))).one()
        assert source.status == 'active'
        assert source.traffic_used_gb == 150.03
        assert grace.state == 'completed'
        assert panel_user.traffic_limit_bytes == 300 * GIB
        assert panel_user.expire_at == end
        assert panel_user.active_internal_squads == [{'uuid': REGULAR}]
        assert panel_user.status == UserStatus.ACTIVE


@pytest.mark.parametrize('flow', ['cabinet', 'miniapp'])
@pytest.mark.parametrize('action', ['preview', 'switch'])
@pytest.mark.parametrize('kind', ['limited', 'expired_limited', 'disabled', 'trial'])
async def test_switch_entrypoints_validate_paid_term(postgres_database, monkeypatch, flow, action, kind):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        source = await db.get(Subscription, 11)
        source.status = 'disabled' if kind == 'disabled' else 'limited'
        source.traffic_used_gb = 150.03
        source.is_trial = kind == 'trial'
        if kind == 'expired_limited':
            source.end_date = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        await db.refresh(user, ['subscriptions'])
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
        monkeypatch.setattr(miniapp.SubscriptionService, 'update_remnawave_user', AsyncMock())
        if flow == 'cabinet':
            handler = route.preview_tariff_switch if action == 'preview' else route.switch_tariff

            def call():
                return handler(TariffPurchaseRequest(tariff_id=3, period_days=30), user, db, 11)
        else:
            handler = miniapp.preview_tariff_switch_endpoint if action == 'preview' else miniapp.switch_tariff_endpoint

            def call():
                return handler(MiniAppTariffSwitchRequest(init_data='test', tariff_id=3, subscriptionId=11), db)

        if kind == 'limited':
            await call()
            await db.refresh(source)
            assert source.status == ('active' if action == 'switch' else 'limited')
        else:
            with pytest.raises(HTTPException) as exc:
                await call()
            assert exc.value.status_code == 400
            await db.refresh(user)
            assert user.balance_kopeks == 10000
            assert (await db.scalars(select(Transaction))).all() == []


@pytest.mark.parametrize('kind', ['trial', 'disabled', 'expired_limited'])
async def test_telegram_stale_switch_button_cannot_charge_ineligible_subscription(postgres_database, monkeypatch, kind):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        source = await db.get(Subscription, 11)
        source.status = 'disabled' if kind == 'disabled' else 'limited'
        source.is_trial = kind == 'trial'
        if kind == 'expired_limited':
            source.end_date = datetime.now(UTC) - timedelta(days=1)
        await db.commit()
        monkeypatch.setattr(tariff_purchase.SubscriptionService, 'create_remnawave_user', AsyncMock())
        monkeypatch.setattr(tariff_purchase.SubscriptionService, 'update_remnawave_user', AsyncMock())
        callback = MagicMock(data='instant_sw_confirm:3')
        callback.answer = AsyncMock()
        callback.message.edit_text = AsyncMock()
        state = AsyncMock()
        state.get_data.return_value = {'active_subscription_id': 11}
        await tariff_purchase.confirm_instant_switch(callback, user, db, state)
        await db.refresh(source)
        await db.refresh(user)
        assert source.tariff_id == 1
        assert user.balance_kopeks == 10000
        assert (await db.scalars(select(Transaction))).all() == []


@pytest.mark.parametrize('action', ['preview', 'switch'])
async def test_miniapp_does_not_guess_between_active_and_limited_subscriptions(postgres_database, monkeypatch, action):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        (await db.get(Subscription, 11)).status = 'limited'
        await db.commit()
        await db.refresh(user, ['subscriptions'])
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
        handler = miniapp.preview_tariff_switch_endpoint if action == 'preview' else miniapp.switch_tariff_endpoint
        with pytest.raises(HTTPException) as exc:
            await handler(MiniAppTariffSwitchRequest(init_data='test', tariff_id=3), db)
        assert exc.value.status_code == 400
        await db.refresh(user)
        assert user.balance_kopeks == 10000
        assert (await db.scalars(select(Transaction))).all() == []
        assert (await db.get(Subscription, 11)).tariff_id == 1
        assert (await db.get(Subscription, 12)).tariff_id == 2


async def test_miniapp_preview_quotes_explicit_limited_subscription(postgres_database, monkeypatch):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        (await db.get(Subscription, 11)).status = 'limited'
        await db.commit()
        await db.refresh(user, ['subscriptions'])
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
        result = await miniapp.preview_tariff_switch_endpoint(
            MiniAppTariffSwitchRequest(init_data='test', tariff_id=3, subscriptionId=11), db
        )
        assert result.current_tariff_id == 1
        assert result.upgrade_cost_kopeks == 666
        assert result.remaining_days == 10
        await db.refresh(user)
        assert user.balance_kopeks == 10000
