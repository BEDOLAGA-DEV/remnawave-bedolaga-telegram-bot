"""Tests for the cabinet coupons API (phase 2 of the wholesale coupons feature).

Follows the house pattern for admin-route tests: a route-registration smoke
test on the aggregate router plus direct handler calls with hand-built fakes
(`admin=SimpleNamespace(...)`, `db=AsyncMock()`) — `require_permission` and
`get_cabinet_db` are bypassed by passing the already-resolved arguments.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services.coupon_service import CouponRedemptionError, CouponRedemptionResult


VALID_TOKEN = 'a1' * 16


def _batch(**overrides) -> SimpleNamespace:
    base = {
        'id': 5,
        'name': 'OptTG Partner',
        'tariff_id': 3,
        'tariff': SimpleNamespace(name='Basic'),
        'period_days': 30,
        'coupons_total': 50,
        'wholesale_price_kopeks': 15000,
        'valid_until': None,
        'is_revoked': False,
        'created_at': datetime.now(UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- Wiring ----------------------------------------------------------------


def test_coupon_routes_registered() -> None:
    from app.cabinet.routes import router

    methods_by_path: dict[str, set[str]] = {}
    for route in router.routes:
        if 'coupon' in route.path:
            methods_by_path.setdefault(route.path, set()).update(route.methods)

    assert methods_by_path.get('/cabinet/admin/coupons') == {'GET', 'POST'}
    assert methods_by_path.get('/cabinet/admin/coupons/{batch_id}') == {'GET'}
    assert methods_by_path.get('/cabinet/admin/coupons/{batch_id}/links') == {'GET'}
    assert methods_by_path.get('/cabinet/admin/coupons/{batch_id}/revoke') == {'POST'}
    assert methods_by_path.get('/cabinet/coupon/redeem') == {'POST'}
    assert methods_by_path.get('/cabinet/coupon/{token}/status') == {'GET'}


def test_coupons_permissions_registered() -> None:
    from app.services.permission_service import PERMISSION_REGISTRY, get_all_permissions

    assert 'coupons' in PERMISSION_REGISTRY
    all_permissions = get_all_permissions()
    for permission in ('coupons:read', 'coupons:create', 'coupons:edit'):
        assert permission in all_permissions


# --- Admin: create batch ---------------------------------------------------


@pytest.mark.asyncio
async def test_create_batch_returns_links_and_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cabinet.routes import admin_coupons
    from app.cabinet.schemas.coupons import CouponBatchCreateRequest

    monkeypatch.setattr(type(settings), 'get_bot_username', lambda self: 'testbot')
    tariff = SimpleNamespace(id=3, name='Basic', is_active=True)
    batch = _batch()

    with (
        patch.object(admin_coupons, 'get_tariff_by_id', AsyncMock(return_value=tariff)),
        patch.object(admin_coupons, 'create_coupon_batch', AsyncMock(return_value=batch)) as create_mock,
        patch.object(admin_coupons, 'get_batch_coupon_tokens', AsyncMock(return_value=[VALID_TOKEN])),
        patch.object(admin_coupons, 'get_batch_status_counts', AsyncMock(return_value={'active': 50})),
    ):
        response = await admin_coupons.create_coupon_batch_endpoint(
            payload=CouponBatchCreateRequest(
                name='  OptTG Partner  ', tariff_id=3, period_days=30, coupons_count=50, valid_days=90
            ),
            admin=SimpleNamespace(id=1),
            db=AsyncMock(),
        )

    assert response.links == [f'https://t.me/testbot?start=coupon_{VALID_TOKEN}']
    assert response.tokens == [VALID_TOKEN]
    assert response.active_count == 50
    kwargs = create_mock.await_args.kwargs
    assert kwargs['name'] == 'OptTG Partner', 'name must be stripped before persisting'
    assert kwargs['created_by'] == 1
    assert kwargs['valid_until'] is not None


@pytest.mark.asyncio
async def test_create_batch_rejects_inactive_tariff() -> None:
    from app.cabinet.routes import admin_coupons
    from app.cabinet.schemas.coupons import CouponBatchCreateRequest

    tariff = SimpleNamespace(id=3, name='Basic', is_active=False)
    with patch.object(admin_coupons, 'get_tariff_by_id', AsyncMock(return_value=tariff)):
        with pytest.raises(HTTPException) as exc_info:
            await admin_coupons.create_coupon_batch_endpoint(
                payload=CouponBatchCreateRequest(name='X', tariff_id=3, period_days=30, coupons_count=1),
                admin=SimpleNamespace(id=1),
                db=AsyncMock(),
            )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_create_batch_rejects_blank_name() -> None:
    from app.cabinet.routes import admin_coupons
    from app.cabinet.schemas.coupons import CouponBatchCreateRequest

    with pytest.raises(HTTPException) as exc_info:
        await admin_coupons.create_coupon_batch_endpoint(
            payload=CouponBatchCreateRequest(name='   ', tariff_id=3, period_days=30, coupons_count=1),
            admin=SimpleNamespace(id=1),
            db=AsyncMock(),
        )
    assert exc_info.value.status_code == 400


# --- Admin: card / links / revoke -----------------------------------------


@pytest.mark.asyncio
async def test_get_batch_404_when_missing() -> None:
    from app.cabinet.routes import admin_coupons

    with patch.object(admin_coupons, 'get_coupon_batch_by_id', AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await admin_coupons.get_coupon_batch(batch_id=999, admin=SimpleNamespace(id=1), db=AsyncMock())
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_links_export_counts_active_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cabinet.routes import admin_coupons

    monkeypatch.setattr(type(settings), 'get_bot_username', lambda self: 'testbot')
    tokens_mock = AsyncMock(return_value=[VALID_TOKEN, 'b2' * 16])
    with (
        patch.object(admin_coupons, 'get_coupon_batch_by_id', AsyncMock(return_value=_batch())),
        patch.object(admin_coupons, 'get_batch_coupon_tokens', tokens_mock),
    ):
        response = await admin_coupons.export_coupon_batch_links(
            batch_id=5, admin=SimpleNamespace(id=1), db=AsyncMock()
        )

    assert response.count == 2
    assert len(response.links) == 2
    assert tokens_mock.await_args.kwargs.get('status') == 'active', 'export must include only still-active coupons'


@pytest.mark.asyncio
async def test_revoke_returns_count_and_updated_card() -> None:
    from app.cabinet.routes import admin_coupons

    batch = _batch(is_revoked=True)
    with (
        patch.object(admin_coupons, 'get_coupon_batch_by_id', AsyncMock(return_value=batch)),
        patch.object(admin_coupons, 'revoke_batch_coupons', AsyncMock(return_value=37)),
        patch.object(admin_coupons, 'get_batch_status_counts', AsyncMock(return_value={'revoked': 37, 'redeemed': 13})),
    ):
        response = await admin_coupons.revoke_coupon_batch(batch_id=5, admin=SimpleNamespace(id=1), db=AsyncMock())

    assert response.revoked_count == 37
    assert response.batch.revoked_count == 37
    assert response.batch.is_revoked is True


# --- User: redeem ----------------------------------------------------------


def _redeem_result(renewed: bool = False) -> CouponRedemptionResult:
    return CouponRedemptionResult(
        tariff_name='Basic',
        period_days=30,
        renewed=renewed,
        end_date=datetime.now(UTC) + timedelta(days=30),
        traffic_limit_gb=100,
        device_limit=2,
    )


@pytest.mark.asyncio
async def test_redeem_success_for_telegram_user_sends_no_email() -> None:
    from app.cabinet.routes import coupon as coupon_routes
    from app.cabinet.schemas.coupons import CouponRedeemRequest

    user = SimpleNamespace(id=5, telegram_id=123, email=None, email_verified=False)
    send_mock = AsyncMock()
    with (
        patch.object(coupon_routes, 'redeem_coupon', AsyncMock(return_value=_redeem_result())),
        patch.object(coupon_routes.notification_delivery_service, 'send_notification', send_mock),
    ):
        response = await coupon_routes.redeem_coupon_endpoint(
            request=CouponRedeemRequest(token=VALID_TOKEN), user=user, db=AsyncMock()
        )

    assert response.success is True
    assert response.tariff_name == 'Basic'
    assert response.period_days == 30
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_redeem_notifies_email_only_user() -> None:
    from app.cabinet.routes import coupon as coupon_routes
    from app.cabinet.schemas.coupons import CouponRedeemRequest
    from app.services.notification_delivery_service import NotificationType

    user = SimpleNamespace(id=5, telegram_id=None, email='user@example.com', email_verified=True)
    send_mock = AsyncMock()
    with (
        patch.object(coupon_routes, 'redeem_coupon', AsyncMock(return_value=_redeem_result(renewed=True))),
        patch.object(coupon_routes.notification_delivery_service, 'send_notification', send_mock),
    ):
        response = await coupon_routes.redeem_coupon_endpoint(
            request=CouponRedeemRequest(token=VALID_TOKEN), user=user, db=AsyncMock()
        )

    assert response.renewed is True
    send_mock.assert_awaited_once()
    kwargs = send_mock.await_args.kwargs
    assert kwargs['notification_type'] == NotificationType.SUBSCRIPTION_RENEWED
    assert kwargs['context']['tariff_name'] == 'Basic'
    assert kwargs['context']['new_expires_at']


@pytest.mark.asyncio
async def test_redeem_maps_service_errors_to_structured_contract() -> None:
    from app.cabinet.routes import coupon as coupon_routes
    from app.cabinet.schemas.coupons import CouponRedeemRequest

    user = SimpleNamespace(id=5, telegram_id=123, email=None, email_verified=False)

    for code, expected_status in (
        ('invalid', 400),
        ('expired', 400),
        ('already_redeemed_by_you', 400),
        ('internal', 500),
    ):
        with patch.object(coupon_routes, 'redeem_coupon', AsyncMock(side_effect=CouponRedemptionError(code))):
            with pytest.raises(HTTPException) as exc_info:
                await coupon_routes.redeem_coupon_endpoint(
                    request=CouponRedeemRequest(token=VALID_TOKEN), user=user, db=AsyncMock()
                )
        assert exc_info.value.status_code == expected_status
        assert exc_info.value.detail['code'] == code
        assert exc_info.value.detail['message']


# --- Public: status --------------------------------------------------------


def _status_request() -> MagicMock:
    return MagicMock()


@pytest.mark.asyncio
async def test_public_status_returns_offer_for_active_coupon(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cabinet.routes import coupon as coupon_routes

    monkeypatch.setattr(type(settings), 'get_bot_username', lambda self: 'testbot')
    coupon = SimpleNamespace(
        status='active',
        batch=SimpleNamespace(is_expired=False, period_days=30, valid_until=None, tariff=SimpleNamespace(name='Basic')),
    )
    with (
        patch.object(coupon_routes, 'get_client_ip', lambda request: '1.2.3.4'),
        patch.object(coupon_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(coupon_routes, 'get_coupon_by_token', AsyncMock(return_value=coupon)),
    ):
        response = await coupon_routes.coupon_status(
            token=VALID_TOKEN.upper(), raw_request=_status_request(), db=AsyncMock()
        )

    assert response.tariff_name == 'Basic'
    assert response.period_days == 30
    assert response.bot_link == f'https://t.me/testbot?start=coupon_{VALID_TOKEN}'


@pytest.mark.asyncio
async def test_public_status_is_uniform_404_for_consumed_coupon() -> None:
    from app.cabinet.routes import coupon as coupon_routes

    coupon = SimpleNamespace(status='redeemed', batch=SimpleNamespace(is_expired=False))
    with (
        patch.object(coupon_routes, 'get_client_ip', lambda request: '1.2.3.4'),
        patch.object(coupon_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(coupon_routes, 'get_coupon_by_token', AsyncMock(return_value=coupon)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await coupon_routes.coupon_status(token=VALID_TOKEN, raw_request=_status_request(), db=AsyncMock())
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_public_status_rate_limited() -> None:
    from app.cabinet.routes import coupon as coupon_routes

    with (
        patch.object(coupon_routes, 'get_client_ip', lambda request: '1.2.3.4'),
        patch.object(coupon_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=True)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await coupon_routes.coupon_status(token=VALID_TOKEN, raw_request=_status_request(), db=AsyncMock())
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_public_status_rejects_malformed_token_without_db_hit() -> None:
    from app.cabinet.routes import coupon as coupon_routes

    lookup = AsyncMock()
    with (
        patch.object(coupon_routes, 'get_client_ip', lambda request: '1.2.3.4'),
        patch.object(coupon_routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False)),
        patch.object(coupon_routes, 'get_coupon_by_token', lookup),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await coupon_routes.coupon_status(
                token='definitely-not-a-token', raw_request=_status_request(), db=AsyncMock()
            )
    assert exc_info.value.status_code == 404
    lookup.assert_not_called()
