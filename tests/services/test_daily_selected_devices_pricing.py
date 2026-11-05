from types import SimpleNamespace

import pytest

from app.database.models import Tariff
from app.services.pricing_engine import PricingEngine


@pytest.mark.parametrize(
    'version,period_pct,devices_pct,expected', [(0, 20, 50, 800), (1, 20, 50, 810), (1, 100, 100, 0), (1, 100, 0, 20)]
)
def test_daily_saved_device_price(version, period_pct, devices_pct, expected):
    tariff = Tariff(is_daily=True, daily_price_kopeks=1000, device_limit=1, device_price_kopeks=300)
    group = SimpleNamespace(
        id=1, get_discount_percent=lambda category, days: period_pct if category == 'period' else devices_pct
    )
    user = SimpleNamespace(promo_group=group, promo_offer_discount_percent=90)
    subscription = SimpleNamespace(device_limit=3, daily_devices_billing_version=version)
    assert PricingEngine.daily_subscription_price(subscription, tariff, user) == expected


@pytest.mark.parametrize('base,selected,expected', [(0, 0, 1000), (0, 5, 1000), (3, 1, 1000), (1, 4, 1030)])
async def test_daily_first_and_later_charge_agree_without_offer(base, selected, expected):
    tariff = Tariff(is_daily=True, daily_price_kopeks=1000, device_limit=base, device_price_kopeks=300)
    subscription = SimpleNamespace(device_limit=selected, daily_devices_billing_version=1)
    initial = await PricingEngine()._calculate_tariff_core(tariff, 1, selected, daily_devices_billing_version=1)
    assert initial.final_total == expected
    assert PricingEngine.daily_subscription_price(subscription, tariff, None) == expected


@pytest.mark.parametrize('version,expected', [(0, 1000), (1, 1020)])
@pytest.mark.parametrize('entrypoint', ['scheduler', 'cabinet'])
async def test_daily_wallet_charge_uses_saved_policy(monkeypatch, version, expected, entrypoint):
    from unittest.mock import AsyncMock

    import app.cabinet.routes.subscription_modules.daily as cabinet_daily
    import app.services.subscription_service as service_module
    from app.config import settings
    from app.database.models import Base, Subscription, User
    from app.services.daily_subscription_service import DailySubscriptionService
    from tests.fixtures.sqlite_memory import memory_session
    from tests.services.test_daily_charge_traffic_reset import _FakePanelSync, _subscription, _tariff, _user
    from tests.services.test_daily_resume_traffic_reset import _FakePanelSync as ResumePanel

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(service_module, 'SubscriptionService', _FakePanelSync)
    monkeypatch.setattr(cabinet_daily, 'SubscriptionService', ResumePanel)
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        tariff = _tariff('NO_RESET')
        tariff.device_price_kopeks = 300
        subscription = _subscription(status='disabled' if entrypoint == 'cabinet' else 'active')
        subscription.device_limit = 3
        subscription.daily_devices_billing_version = version
        db.add_all([_user(), tariff, subscription])
        await db.commit()
        if entrypoint == 'scheduler':
            service = DailySubscriptionService()
            subscription = await service._reload_daily_subscription(db, 10)
            assert await service._process_single_charge(db, subscription) == 'charged'
            # A retry must not charge the same day again.
            assert await service._process_single_charge(db, subscription) == 'skipped'
        else:
            user = await db.get(User, 1)
            await cabinet_daily.toggle_subscription_pause(user=user, db=db, subscription_id=10, paused=False)
            await cabinet_daily.toggle_subscription_pause(user=user, db=db, subscription_id=10, paused=False)
        user = await db.get(User, 1)
        await db.refresh(user)
        assert user.balance_kopeks == 100_000 - expected
        subscription = await db.get(Subscription, 10)
        assert subscription.daily_devices_billing_version == version
        assert subscription.device_limit == 3
        from app.cabinet.routes.subscription_modules.helpers import _subscription_to_response

        await db.refresh(subscription, attribute_names=['tariff'])
        assert _subscription_to_response(subscription, user=user).daily_price_kopeks == expected


@pytest.mark.parametrize('version,expected', [(0, 1000), (1, 1020)])
@pytest.mark.parametrize('entrypoint', ['bot', 'miniapp'])
async def test_other_daily_resume_entrypoints_charge_saved_devices(monkeypatch, version, expected, entrypoint):
    from app.database.models import Base, Subscription, User
    from tests.fixtures.sqlite_memory import memory_session

    if entrypoint == 'bot':
        from tests.handlers import test_daily_bot_resume_traffic_reset as fixture_module

        resume = fixture_module._resume_from_bot
    else:
        from tests.services import test_daily_resume_traffic_reset as fixture_module

        resume = fixture_module._resume_from_miniapp
    original_rows = fixture_module._rows

    def rows(*args, **kwargs):
        result = original_rows(*args, **kwargs)
        tariff = next(row for row in result if isinstance(row, Tariff))
        tariff.device_price_kopeks = 300
        subscription = next(row for row in result if isinstance(row, Subscription))
        subscription.device_limit = 3
        subscription.daily_devices_billing_version = version
        return result

    monkeypatch.setattr(fixture_module, '_rows', rows)
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        await resume(db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=False)
        user = await db.get(User, 1)
        await db.refresh(user)
        assert user.balance_kopeks == 100_000 - expected


async def test_offer_applies_only_to_initial_daily_charge():
    tariff = Tariff(id=1, is_daily=True, daily_price_kopeks=1000, device_limit=1, device_price_kopeks=300)
    group = SimpleNamespace(id=1, get_discount_percent=lambda category, days: 20 if category == 'period' else 50)
    user = SimpleNamespace(promo_group=group, promo_offer_discount_percent=50)
    subscription = SimpleNamespace(device_limit=3, daily_devices_billing_version=1)
    initial = await PricingEngine()._calculate_tariff_core(tariff, 1, 3, user=user, daily_devices_billing_version=1)
    assert initial.final_total == 405
    assert PricingEngine.daily_subscription_price(subscription, tariff, user) == 810


def test_daily_checkout_and_later_charge_reject_ineligible_group_discount():
    tariff = Tariff(is_daily=True, daily_price_kopeks=1000, device_limit=1, device_price_kopeks=300)
    tariff.is_available_for_promo_group = lambda group_id: False
    user = SimpleNamespace(promo_group=SimpleNamespace(id=1, get_discount_percent=lambda *args: 100))
    subscription = SimpleNamespace(device_limit=3, daily_devices_billing_version=1)
    assert PricingEngine.daily_subscription_price(subscription, tariff, user) == 1020


@pytest.mark.parametrize('unit,extra', [(301, 2), (29, 2), (1, 1)])
async def test_selected_daily_public_checkout_prorates_monthly_unit_with_floor(unit, extra):
    tariff = Tariff(id=1, is_daily=True, daily_price_kopeks=1000, device_limit=1, device_price_kopeks=unit)
    engine = PricingEngine()
    legacy = await engine.calculate_tariff_purchase_price(tariff, 1, device_limit=1 + extra)
    selected = await engine.calculate_tariff_purchase_price(
        tariff, 1, device_limit=1 + extra, daily_devices_billing_version=1
    )
    assert legacy.devices_price == unit * extra
    assert selected.devices_price == unit * extra // 30
    subscription = SimpleNamespace(tariff_id=1, tariff=tariff, device_limit=1 + extra, daily_devices_billing_version=1)
    renewal = await engine.calculate_renewal_price(None, subscription, 1)
    assert renewal.final_total == selected.final_total
    assert engine.daily_subscription_price(subscription, tariff, None) == selected.final_total
