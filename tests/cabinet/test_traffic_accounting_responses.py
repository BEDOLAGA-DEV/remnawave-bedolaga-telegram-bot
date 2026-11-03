"""Cabinet DTOs expose settled quota rather than technical panel ceilings."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cabinet.routes.subscription_modules import helpers, multi_tariff, status, traffic
from app.cabinet.schemas.subscription import TrafficPurchaseInfo
from tests.cabinet.test_subscription_requires_tariff_flag import _subscription


@pytest.fixture
def summary(monkeypatch):
    result = dict(
        traffic_accounting_mode='managed',
        traffic_remaining_gb=23.5,
        base_traffic_remaining_gb=10.0,
        next_traffic_refresh_at=datetime(2027, 1, 1, tzinfo=UTC),
        purchase_remaining={7: 13.5},
    )
    monkeypatch.setattr(
        'app.services.traffic_accounting_service.traffic_accounting_summary', AsyncMock(return_value=result)
    )
    monkeypatch.setattr(traffic.SubscriptionService, '_persisted_traffic_mode', AsyncMock(return_value='managed'))
    monkeypatch.setattr(traffic.SubscriptionService, 'sync_subscription_usage', AsyncMock(return_value=True))
    return result


def subscription():
    sub = _subscription(tariff_id=None)
    sub.traffic_limit_gb = 100
    sub.traffic_used_gb = 800
    return sub


def test_legacy_serialization_has_no_invented_remaining():
    data = helpers._subscription_to_response(subscription()).model_dump()
    assert data['traffic_accounting_mode'] == 'legacy'
    assert data['traffic_remaining_gb'] is None
    now = datetime.now(UTC)
    purchase = TrafficPurchaseInfo(
        id=7, traffic_gb=50, expires_at=now, created_at=now, days_remaining=0, progress_percent=100
    )
    assert purchase.model_dump()['remaining_gb'] is None


async def test_info_exposes_settled_package_remaining(monkeypatch, summary):
    sub = subscription()
    user = SimpleNamespace(id=1)
    monkeypatch.setattr('app.database.crud.user.get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(status, 'resolve_subscription', AsyncMock(return_value=sub))
    now = datetime.now(UTC)
    purchase = SimpleNamespace(
        id=7, traffic_gb=50, expires_at=now + timedelta(days=20), created_at=now - timedelta(days=10)
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [purchase]
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    response = await status.get_subscription(user=user, db=db, subscription_id=1)
    data = response.subscription
    assert data.traffic_accounting_mode == 'managed'
    assert data.traffic_remaining_gb == 23.5
    assert data.traffic_used_gb == 76.5
    assert data.traffic_used_percent == 76.5
    assert data.traffic_purchases[0].remaining_gb == 13.5


async def test_list_and_detail_expose_managed_quota(monkeypatch, summary):
    sub = subscription()
    monkeypatch.setattr(multi_tariff, 'get_all_subscriptions_by_user_id', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(multi_tariff, 'get_subscription_by_id_for_user', AsyncMock(return_value=sub))
    user = SimpleNamespace(id=1)
    response = await multi_tariff.list_subscriptions(user=user, db=None)
    detail = await multi_tariff.get_subscription_detail(1, user=user, db=None)
    for item in [response.subscriptions[0], detail]:
        assert item.traffic_accounting_mode == 'managed'
        assert item.traffic_remaining_gb == 23.5
        assert item.traffic_used_gb == 76.5


@pytest.mark.parametrize('source', ['database', 'remnawave', 'cached'])
async def test_refresh_all_paths_preserve_managed_quota(monkeypatch, summary, source):
    sub = subscription()
    user = SimpleNamespace(id=1, telegram_id=123, remnawave_id=None)
    monkeypatch.setattr(traffic, 'resolve_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr(traffic.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=source == 'cached'))
    monkeypatch.setattr(
        traffic.cache, 'get', AsyncMock(return_value={'traffic_used_gb': 800, 'traffic_limit_bytes': 999 * 1024**3})
    )
    monkeypatch.setattr(traffic.cache, 'set', AsyncMock())
    stats = {'used_traffic_gb': 800, 'used_traffic_bytes': 800 * 1024**3, 'traffic_limit_bytes': 999 * 1024**3}
    service = SimpleNamespace(get_user_traffic_stats=AsyncMock(return_value=stats if source == 'remnawave' else None))
    monkeypatch.setattr(traffic, 'RemnaWaveService', lambda: service)
    monkeypatch.setattr(type(traffic.settings), 'is_multi_tariff_enabled', lambda self: False)
    synced = AsyncMock(return_value=source == 'remnawave')
    monkeypatch.setattr(traffic.SubscriptionService, 'sync_subscription_usage', synced)
    result = await traffic.refresh_traffic(
        user=user, db=SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock()), subscription_id=1
    )
    if source == 'cached':
        synced.assert_not_awaited()
    else:
        synced.assert_awaited_once()
    service.get_user_traffic_stats.assert_not_awaited()
    assert result['traffic_accounting_mode'] == 'managed'
    assert result['traffic_remaining_gb'] == 23.5
    assert result['traffic_used_gb'] == 76.5
    assert result['traffic_used_bytes'] == int(76.5 * 1024**3)
    assert result['traffic_limit_bytes'] == 100 * 1024**3
    assert 'purchase_remaining' not in result


async def test_summary_projection_reads_persisted_grants(monkeypatch):
    from app.services.traffic_accounting import Allowance, TrafficAccount
    from app.services.traffic_accounting_store import initialize_account
    from tests.services.test_traffic_accounting_store import database

    now = datetime.now(UTC)
    async with database(monkeypatch) as db:
        from app.database.models import Subscription

        (await db.get(Subscription, 1)).traffic_limit_gb = 100
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                'test',
                0,
                0,
                (
                    Allowance('base:test', 10 * 1024**3, now + timedelta(days=30)),
                    Allowance('purchase:7', 13 * 1024**3, now + timedelta(days=10)),
                ),
            ),
            strategy='MONTH',
            timezone='UTC',
            anchor=now,
            next_refresh_at=now + timedelta(days=30),
        )
        state.phase = 'ready'
        await db.flush()
        fields = await helpers._load_traffic_accounting_fields(db, subscription())
        assert fields['traffic_accounting_mode'] == 'managed'
        assert fields['traffic_remaining_gb'] == 23
        assert fields['base_traffic_remaining_gb'] == 10
        assert fields['traffic_used_gb'] == 77


async def test_detail_does_not_read_accounting_for_unowned_subscription(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(multi_tariff, 'get_subscription_by_id_for_user', AsyncMock(return_value=None))
    read_summary = AsyncMock()
    monkeypatch.setattr('app.services.traffic_accounting_service.traffic_accounting_summary', read_summary)
    with pytest.raises(HTTPException) as error:
        await multi_tariff.get_subscription_detail(999, user=SimpleNamespace(id=1), db=None)
    assert error.value.status_code == 404
    read_summary.assert_not_awaited()


@pytest.mark.parametrize('endpoint', [status.get_subscription, traffic.refresh_traffic])
async def test_single_endpoints_preserve_ownership_guard(monkeypatch, endpoint):
    from fastapi import HTTPException

    user = SimpleNamespace(id=1)
    monkeypatch.setattr('app.database.crud.user.get_user_by_id', AsyncMock(return_value=user))
    module = status if endpoint is status.get_subscription else traffic
    monkeypatch.setattr(module, 'resolve_subscription', AsyncMock(side_effect=HTTPException(status_code=404)))
    read_summary = AsyncMock()
    monkeypatch.setattr('app.services.traffic_accounting_service.traffic_accounting_summary', read_summary)
    with pytest.raises(HTTPException) as error:
        await endpoint(user=user, db=None, subscription_id=999)
    assert error.value.status_code == 404
    read_summary.assert_not_awaited()


def test_managed_unlimited_has_no_finite_remaining():
    sub = subscription()
    sub.traffic_limit_gb = 0
    fields = helpers._traffic_accounting_response_fields(
        sub,
        dict(
            traffic_accounting_mode='managed',
            traffic_remaining_gb=None,
            base_traffic_remaining_gb=None,
            next_traffic_refresh_at=None,
            purchase_remaining={},
        ),
    )
    assert fields['traffic_remaining_gb'] is None
    assert fields['is_unlimited'] is True
    assert fields['traffic_limit_bytes'] == 0
    assert fields['traffic_used_percent'] == 0


@pytest.mark.parametrize('limit', [0, 100])
async def test_unknown_managed_transition_is_safe_in_all_responses(monkeypatch, summary, limit):
    sub = subscription()
    sub.traffic_limit_gb = limit
    summary.update(
        traffic_remaining_gb=None, base_traffic_remaining_gb=None, next_traffic_refresh_at=None, purchase_remaining={}
    )
    monkeypatch.setattr(multi_tariff, 'get_all_subscriptions_by_user_id', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(multi_tariff, 'get_subscription_by_id_for_user', AsyncMock(return_value=sub))
    monkeypatch.setattr(status, 'resolve_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr(traffic, 'resolve_subscription', AsyncMock(return_value=sub))
    user = SimpleNamespace(id=1, telegram_id=123, remnawave_id=None)
    monkeypatch.setattr('app.database.crud.user.get_user_by_id', AsyncMock(return_value=user))
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    info = await status.get_subscription(user=user, db=db, subscription_id=1)
    listing = await multi_tariff.list_subscriptions(user=user, db=db)
    detail = await multi_tariff.get_subscription_detail(1, user=user, db=db)
    for item in [info.subscription, listing.subscriptions[0], detail]:
        assert item.traffic_accounting_mode == 'managed'
        assert item.traffic_remaining_gb is None
        assert item.base_traffic_remaining_gb is None
        assert item.traffic_used_gb == 0
    monkeypatch.setattr(traffic.cache, 'set', AsyncMock())
    monkeypatch.setattr(traffic.cache, 'get', AsyncMock(return_value={'traffic_used_gb': 800}))
    monkeypatch.setattr(type(traffic.settings), 'is_multi_tariff_enabled', lambda self: False)
    for source in ['cached', 'database', 'remnawave']:
        monkeypatch.setattr(traffic.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=source == 'cached'))
        stats = {'used_traffic_gb': 800, 'used_traffic_bytes': 800 * 1024**3, 'traffic_limit_bytes': 999 * 1024**3}
        service = SimpleNamespace(
            get_user_traffic_stats=AsyncMock(return_value=stats if source == 'remnawave' else None)
        )
        monkeypatch.setattr(traffic, 'RemnaWaveService', lambda: service)
        refreshed = await traffic.refresh_traffic(user=user, db=db, subscription_id=1)
        assert refreshed['traffic_remaining_gb'] is None
        assert refreshed['traffic_used_gb'] == 0
        assert refreshed['traffic_limit_gb'] == limit


@pytest.mark.parametrize('old_limit,new_limit,phase', [(0, 100, 'unlimited'), (100, 0, 'ready')])
async def test_persisted_pending_mode_switch_reports_unknown(monkeypatch, old_limit, new_limit, phase):
    from app.database.models import Subscription
    from app.services.traffic_accounting import Allowance, TrafficAccount
    from app.services.traffic_accounting_service import traffic_accounting_summary
    from app.services.traffic_accounting_store import initialize_account
    from app.services.traffic_base_grant_service import stage_paid_base_grant
    from tests.services.test_traffic_accounting_store import database

    now = datetime.now(UTC)
    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.traffic_limit_gb = old_limit
        state = await initialize_account(
            db,
            1,
            TrafficAccount('masked-transition', 0, 0, (Allowance('base:old', 50 * 1024**3, now + timedelta(days=30)),)),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=now,
            next_refresh_at=None,
        )
        state.phase = phase
        sub.traffic_limit_gb = new_limit
        await stage_paid_base_grant(db, sub, operation_key='paid-transition:1')
        await db.commit()
        result = await traffic_accounting_summary(db, 1)
        assert result == dict(
            traffic_accounting_mode='managed',
            traffic_remaining_gb=None,
            base_traffic_remaining_gb=None,
            next_traffic_refresh_at=None,
            purchase_remaining={},
        )
        fields = helpers._traffic_accounting_response_fields(sub, result)
        assert fields['traffic_used_gb'] == 0
        assert fields['is_unlimited'] is (new_limit == 0)
