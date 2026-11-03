"""Combined traffic promo must not commit days before the traffic grant succeeds."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PromoCodeType
from app.services.promocode_service import PromoCodeService


@pytest.fixture
def setup(monkeypatch):
    service = PromoCodeService.__new__(PromoCodeService)
    service.subscription_service = SimpleNamespace(update_remnawave_user=AsyncMock(), enable_remnawave_user=AsyncMock())
    user = SimpleNamespace(id=1, telegram_id=1, email=None, remnawave_id='masked-1')
    sub = SimpleNamespace(id=2, traffic_limit_gb=40, status='active', remnawave_id='masked-1', tariff=None)
    promo = SimpleNamespace(
        id=3,
        code='TRAFFIC',
        type=PromoCodeType.BALANCE_AND_DAYS.value,
        subscription_days=7,
        traffic_gb=40,
        balance_bonus_kopeks=100,
        promo_group_id=None,
    )
    panel = object()
    extend = AsyncMock()
    balance = AsyncMock(return_value=True)
    grant = AsyncMock()
    reactivate = AsyncMock()
    monkeypatch.setattr('app.services.promocode_service.extend_subscription', extend)
    monkeypatch.setattr('app.services.promocode_service.add_user_balance', balance)
    monkeypatch.setattr('app.services.traffic_purchase_service.add_subscription_traffic', grant)
    monkeypatch.setattr('app.database.crud.subscription.reactivate_subscription', reactivate)
    monkeypatch.setattr('app.database.crud.transaction.create_transaction', AsyncMock())
    return service, user, sub, promo, panel, extend, balance, grant, reactivate


async def test_managed_combined_promo_stages_all_effects_until_caller_commit(setup):
    service, user, sub, promo, panel, extend, balance, grant, reactivate = setup
    db = AsyncMock()
    result = await service._apply_promocode_effects(db, user, promo, traffic_context=(sub, panel))
    assert '40 ГБ' in result
    assert extend.await_args.kwargs['commit'] is False
    assert balance.await_args.kwargs['commit'] is False
    assert grant.await_args.kwargs == {'panel': panel, 'commit': False}
    assert reactivate.await_args.kwargs['commit'] is False
    db.commit.assert_not_awaited()
    service.subscription_service.update_remnawave_user.assert_not_awaited()
    service.subscription_service.enable_remnawave_user.assert_not_awaited()


async def test_failed_managed_grant_does_not_commit_days_or_credit_balance(setup):
    service, user, sub, promo, panel, extend, balance, grant, _ = setup
    db = AsyncMock()
    grant.side_effect = ValueError('accounting conflict')
    with pytest.raises(ValueError, match='accounting conflict'):
        await service._apply_promocode_effects(db, user, promo, traffic_context=(sub, panel))
    assert extend.await_args.kwargs['commit'] is False
    balance.assert_not_awaited()
    db.commit.assert_not_awaited()
    service.subscription_service.update_remnawave_user.assert_not_awaited()


async def test_sqlite_duplicate_reservation_keeps_outer_transaction_rollbackable(monkeypatch):
    from sqlalchemy import func, select

    from app.database.crud.promocode import create_promocode_use
    from app.database.models import Base, PromoCode, PromoCodeUse, User
    from tests.fixtures.sqlite_memory import memory_session

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1))
        db.add(PromoCode(id=1, code='ROLLBACK', type=PromoCodeType.BALANCE.value, max_uses=10))
        await db.commit()
        assert await create_promocode_use(db, 1, 1) is not None
        assert await create_promocode_use(db, 1, 1) is None
        await db.rollback()
        assert await db.scalar(select(func.count()).select_from(PromoCodeUse)) == 0


@pytest.mark.parametrize('failure_stage', ['preflight', 'grant'])
async def test_activation_rolls_back_real_days_and_usage_reservation_on_grant_failure(monkeypatch, failure_stage):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from app.database.models import Base, PromoCode, PromoCodeUse, Subscription, User
    from tests.fixtures.sqlite_memory import memory_session

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        now = datetime.now(UTC)
        end = now + timedelta(days=10)
        user = User(id=1, balance_kopeks=500)
        sub = Subscription(
            id=2, user_id=1, traffic_limit_gb=40, status='active', is_trial=False, start_date=now, end_date=end
        )
        promo = PromoCode(
            id=3,
            code='ATOMIC_TRAFFIC',
            type=PromoCodeType.BALANCE_AND_DAYS.value,
            traffic_gb=40,
            subscription_days=7,
            balance_bonus_kopeks=100,
            max_uses=10,
            current_uses=0,
            is_active=True,
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=1),
        )
        db.add_all([user, sub, promo])
        await db.commit()
        prepare = AsyncMock(return_value=(sub, object()))
        if failure_stage == 'preflight':
            prepare.side_effect = ValueError('panel unavailable')
        monkeypatch.setattr('app.services.traffic_purchase_service.prepare_traffic_purchase', prepare)
        monkeypatch.setattr(
            'app.services.traffic_purchase_service.add_subscription_traffic',
            AsyncMock(side_effect=ValueError('accounting conflict')),
        )
        service = PromoCodeService()
        result = await service.activate_promocode(db, 1, 'ATOMIC_TRAFFIC')
        assert result == {'success': False, 'error': 'server_error'}
        await db.refresh(sub)
        await db.refresh(promo)
        assert sub.end_date.replace(tzinfo=UTC) == end
        assert user.balance_kopeks == 500
        assert promo.current_uses == 0
        assert await db.scalar(select(func.count()).select_from(PromoCodeUse)) == 0


@pytest.mark.parametrize('changed,expected', [('first_purchase', 'not_first_purchase'), ('disabled', 'inactive')])
async def test_activation_rechecks_eligibility_after_panel_preflight(monkeypatch, changed, expected):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from app.database.models import Base, PromoCode, PromoCodeUse, Subscription, User
    from tests.fixtures.sqlite_memory import memory_session

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        now = datetime.now(UTC)
        user = User(id=1, balance_kopeks=500, has_had_paid_subscription=False)
        sub = Subscription(
            id=2,
            user_id=1,
            traffic_limit_gb=40,
            status='active',
            is_trial=False,
            start_date=now,
            end_date=now + timedelta(days=10),
        )
        promo = PromoCode(
            id=3,
            code='CHECK_AGAIN',
            type=PromoCodeType.BALANCE_AND_DAYS.value,
            traffic_gb=40,
            subscription_days=0,
            balance_bonus_kopeks=0,
            first_purchase_only=True,
            max_uses=10,
            current_uses=0,
            is_active=True,
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=1),
        )
        db.add_all([user, sub, promo])
        await db.commit()

        async def preflight(*_):
            # A committed change while the remote preflight was in progress.
            if changed == 'first_purchase':
                user.has_had_paid_subscription = True
            else:
                promo.is_active = False
            await db.commit()
            return sub, object()

        monkeypatch.setattr('app.services.traffic_purchase_service.prepare_traffic_purchase', preflight)
        grant = AsyncMock()
        monkeypatch.setattr('app.services.traffic_purchase_service.add_subscription_traffic', grant)
        result = await PromoCodeService().activate_promocode(db, 1, 'CHECK_AGAIN')
        assert result == {'success': False, 'error': expected}
        grant.assert_not_awaited()
        assert promo.current_uses == 0
        assert await db.scalar(select(func.count()).select_from(PromoCodeUse)) == 0
