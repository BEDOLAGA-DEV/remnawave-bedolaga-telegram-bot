"""TG120: fresh proof of all deleted squads must refuse before wallet mutation."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import purchase, tariff_switch
from app.cabinet.schemas.subscription import TariffPurchaseRequest
from app.database.models import Subscription, Tariff, Transaction, User
from app.services import checkout_squad_validation as preflight
from tests.database.test_multi_tariff_switch_postgres import switch_data


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('flow', ['purchase', 'daily', 'switch', 'miniapp_purchase', 'miniapp_switch'])
@pytest.mark.parametrize('catalog_state', ['all_deleted', 'partial', 'unavailable'])
async def test_tariff_checkout_checks_live_squads_before_debit(postgres_database, monkeypatch, flow, catalog_state):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        target = await db.get(Tariff, 3)
        target.allowed_squads = ['masked-deleted', 'masked-surviving']
        target.is_daily = flow == 'daily'
        target.daily_price_kopeks = 100
        user.promo_offer_discount_source = 'test'
        user.promo_offer_discount_percent = 17
        user.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(days=1)
        await db.commit()
        before_user = (await db.execute(select(User.__table__).where(User.id == user.id))).mappings().one()
        before_subs = list((await db.execute(select(Subscription.__table__).order_by(Subscription.id))).mappings())
        panel = SimpleNamespace(create_remnawave_user=AsyncMock(), update_remnawave_user=AsyncMock())
        monkeypatch.setattr(purchase, 'SubscriptionService', lambda: panel)
        monkeypatch.setattr(tariff_switch, 'SubscriptionService', lambda: panel)

        async def get_catalog():
            if catalog_state == 'unavailable':
                raise TimeoutError('temporary panel outage')
            return [] if catalog_state == 'all_deleted' else [SimpleNamespace(uuid='masked-surviving')]

        @asynccontextmanager
        async def api():
            yield SimpleNamespace(get_internal_squads=get_catalog)

        monkeypatch.setattr(preflight, 'RemnaWaveService', lambda: SimpleNamespace(get_api_client=api))
        request = TariffPurchaseRequest(tariff_id=3, period_days=30)
        from app.services.subscription_service import SubscriptionService
        from app.webapi.routes import miniapp
        from app.webapi.schemas.miniapp import MiniAppTariffPurchaseRequest, MiniAppTariffSwitchRequest

        monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', AsyncMock())
        monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', AsyncMock())
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
        await db.refresh(user, ['subscriptions'])

        async def checkout():
            if flow == 'miniapp_purchase':
                return await miniapp.purchase_tariff_endpoint(
                    MiniAppTariffPurchaseRequest(initData='masked', tariffId=3, periodDays=30), db
                )
            if flow == 'miniapp_switch':
                return await miniapp.switch_tariff_endpoint(
                    MiniAppTariffSwitchRequest(init_data='masked', tariff_id=3, subscriptionId=11), db
                )
            if flow == 'switch':
                return await tariff_switch.switch_tariff(request, user, db, 11)
            return await purchase.purchase_tariff(request, user, db)

        if catalog_state == 'all_deleted':
            with pytest.raises(HTTPException) as exc:
                await checkout()
            assert exc.value.status_code == 409
            assert exc.value.detail['code'] == 'tariff_servers_unavailable'
            assert (await db.execute(select(User.__table__).where(User.id == 1))).mappings().one() == before_user
            assert (
                list((await db.execute(select(Subscription.__table__).order_by(Subscription.id))).mappings())
                == before_subs
            )
            assert list(await db.scalars(select(Transaction))) == []
            panel.create_remnawave_user.assert_not_awaited()
            panel.update_remnawave_user.assert_not_awaited()
        else:
            result = await checkout()
            assert result['success'] if isinstance(result, dict) else result.success
            await db.refresh(user)
            assert user.balance_kopeks < 10000
            assert len(list(await db.scalars(select(Transaction)))) == 1


@pytest.mark.parametrize(
    'flow',
    [
        'telegram_daily',
        'telegram_extend',
        'telegram_switch',
        'telegram_daily_switch',
        'telegram_instant',
        'saved_period',
        'saved_daily',
    ],
)
async def test_bot_and_saved_checkout_refuse_all_deleted_squads(postgres_database, monkeypatch, flow):
    import inspect

    from app.handlers.subscription import tariff_purchase as telegram
    from app.services import subscription_auto_purchase_service as auto
    from app.services.subscription_service import SubscriptionService

    async with switch_data(postgres_database, monkeypatch) as (db, user):
        target = await db.get(Tariff, 3)
        target.allowed_squads = ['masked-deleted']
        target.is_daily = flow in ('telegram_daily', 'telegram_daily_switch', 'saved_daily')
        target.daily_price_kopeks = 100
        source = await db.get(Subscription, 11)
        source.connected_squads = ['masked-deleted']
        await db.commit()
        before_user = (await db.execute(select(User.__table__).where(User.id == 1))).mappings().one()
        before_subs = list((await db.execute(select(Subscription.__table__).order_by(Subscription.id))).mappings())
        monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', AsyncMock())
        monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', AsyncMock())
        monkeypatch.setattr(telegram, 'should_create_panel_account', AsyncMock(return_value=True))
        monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
        monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
        monkeypatch.setattr(auto, '_notify_email_user_auto_purchase', AsyncMock())

        @asynccontextmanager
        async def api():
            yield SimpleNamespace(get_internal_squads=AsyncMock(return_value=[]))

        monkeypatch.setattr(preflight, 'RemnaWaveService', lambda: SimpleNamespace(get_api_client=api))
        bot = SimpleNamespace(send_message=AsyncMock())
        if flow.startswith('saved'):
            handler = auto._auto_purchase_daily_tariff if flow == 'saved_daily' else auto._auto_purchase_tariff
            assert not await handler(db, user, {'tariff_id': 3, 'period_days': 30}, bot=bot)
            assert 'Серверы' in bot.send_message.await_args.kwargs['text']
        else:
            handler, data = {
                'telegram_daily': (telegram.confirm_daily_tariff_purchase, 'tariff_daily_confirm:3'),
                'telegram_extend': (telegram.confirm_tariff_extend, 'tariff_ext_confirm:11:1:30'),
                'telegram_switch': (telegram.confirm_tariff_switch, 'tariff_switch_confirm:3:30'),
                'telegram_daily_switch': (telegram.confirm_daily_tariff_switch, 'tariff_daily_switch:3'),
                'telegram_instant': (telegram.confirm_instant_switch, 'tariff_instant_switch:3'),
            }[flow]
            callback = SimpleNamespace(
                data=data, answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()), bot=bot
            )
            state = SimpleNamespace(
                get_data=AsyncMock(return_value={'active_subscription_id': 11}),
                get_state=AsyncMock(return_value=None),
                update_data=AsyncMock(),
            )
            await inspect.unwrap(handler)(callback, user, db, state)
            assert any(
                'Серверы' in str(call)
                for call in callback.answer.await_args_list + callback.message.edit_text.await_args_list
            )
        assert (await db.execute(select(User.__table__).where(User.id == 1))).mappings().one() == before_user
        assert (
            list((await db.execute(select(Subscription.__table__).order_by(Subscription.id))).mappings()) == before_subs
        )
        assert list(await db.scalars(select(Transaction))) == []


@pytest.mark.parametrize('flow', ['cabinet', 'telegram', 'miniapp'])
@pytest.mark.parametrize('paid', [False, True])
@pytest.mark.parametrize('trial_config', ['flag', 'setting'])
async def test_paid_trial_refuses_before_charge_and_trial_creation(
    postgres_database, monkeypatch, flow, paid, trial_config
):
    import inspect

    from app.config import settings
    from app.handlers.subscription import purchase as telegram
    from app.services.subscription_service import SubscriptionService
    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppSubscriptionTrialRequest

    async with switch_data(postgres_database, monkeypatch) as (db, _existing):
        user = User(id=4, telegram_id=4, language='ru', balance_kopeks=10000)
        db.add(user)
        target = await db.get(Tariff, 3)
        target.allowed_squads = ['masked-deleted']
        target.is_trial_available = trial_config == 'flag'
        monkeypatch.setattr(settings, 'TRIAL_TARIFF_ID', 3 if trial_config == 'setting' else 0)
        await db.commit()
        await db.refresh(user, ['subscriptions'])
        monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', paid)
        monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 100)
        monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', AsyncMock())
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))

        @asynccontextmanager
        async def api():
            yield SimpleNamespace(get_internal_squads=AsyncMock(return_value=[]))

        monkeypatch.setattr(preflight, 'RemnaWaveService', lambda: SimpleNamespace(get_api_client=api))
        if flow == 'telegram':
            callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
            handler = telegram.handle_trial_pay_with_balance if paid else telegram.activate_trial
            await inspect.unwrap(handler)(callback, user, db)
            assert any(
                'Серверы' in str(call)
                for call in callback.answer.await_args_list + callback.message.edit_text.await_args_list
            )
        else:
            with pytest.raises(HTTPException) as exc:
                if flow == 'cabinet':
                    await purchase.activate_trial(None, user, db)
                else:
                    await miniapp.activate_subscription_trial_endpoint(
                        MiniAppSubscriptionTrialRequest(init_data='masked'), db
                    )
            assert exc.value.status_code == 409
            assert exc.value.detail['code'] == 'tariff_servers_unavailable'
        await db.refresh(user)
        assert user.balance_kopeks == 10000
        assert not user.has_had_paid_subscription
        assert list(await db.scalars(select(Subscription).where(Subscription.user_id == 4))) == []
        assert list(await db.scalars(select(Transaction).where(Transaction.user_id == 4))) == []


async def test_classic_cabinet_trial_still_uses_configured_inactive_tariff(postgres_database, monkeypatch):
    """A stored trial tariff is also used by cabinet classic provisioning, unlike Telegram."""
    from app.config import settings
    from app.services.subscription_service import SubscriptionService

    async with switch_data(postgres_database, monkeypatch) as (db, _existing):
        user = User(id=4, telegram_id=4, language='ru', balance_kopeks=10000)
        db.add(user)
        target = await db.get(Tariff, 3)
        target.is_active = False
        target.is_trial_available = False
        target.allowed_squads = ['masked-deleted']
        await db.commit()
        await db.refresh(user, ['subscriptions'])
        monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
        monkeypatch.setattr(settings, 'TRIAL_TARIFF_ID', 3)
        monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', True)
        monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 100)
        monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', AsyncMock())
        # Demonstrate the existing provisioning selection independently of preflight.
        monkeypatch.setattr(preflight, 'validate_trial_checkout_squads', AsyncMock())
        await purchase.activate_trial(None, user, db)
        subscription = await db.scalar(select(Subscription).where(Subscription.user_id == 4))
        assert subscription.tariff_id == 3
        assert subscription.connected_squads == ['masked-deleted']
        await db.refresh(user)
        assert user.balance_kopeks == 9900
