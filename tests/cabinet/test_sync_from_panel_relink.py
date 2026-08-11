"""Repair path: the admin "panel -> bot" sync must re-link a subscription whose
remnawave_id was wiped (by the spurious sibling-expiry webhook) to its live
panel user and restore status + connected_squads.

Before the fix it raised HTTP 400 ("not linked to panel yet") and, even when it
found the panel user, the squad extraction (.uuid/str only) matched nothing
because active_internal_squads is a list[dict] — so squads were never restored.

Панельный двойник строится с ``spec=RemnaWaveAPI``: в 3.0.0 маршрутов
``by-telegram-id``/``by-email`` больше нет, и мок обязан падать на них
AttributeError, а не молча возвращать заготовленный ответ, — иначе тест снова
зеленел бы поверх полностью сломанного продакшена.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import app.cabinet.routes.admin_users as au
from app.cabinet.schemas.users import SyncFromPanelRequest
from app.database.models import SubscriptionStatus
from app.external.remnawave_api import RemnaWaveAPI


def _panel_user(panel_id: int, squads=('sq1', 'sq2')):
    """Панельный пользователь 3.0.0: запись идентифицируется числовым id, поля uuid нет."""
    return SimpleNamespace(
        id=panel_id,
        short_uuid=f'short-{panel_id}',
        username='u',
        status=SimpleNamespace(value='ACTIVE'),
        expire_at=datetime.now(UTC) + timedelta(days=900),
        traffic_limit_bytes=61 * 1024**3,
        used_traffic_bytes=0,
        hwid_device_limit=100,
        subscription_url=f'https://panel/{panel_id}',
        happ_crypto_link=None,
        active_internal_squads=[{'uuid': s} for s in squads],
    )


def _wiped_sub(sub_id: int = 84):
    return SimpleNamespace(
        id=sub_id,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=datetime.now(UTC) + timedelta(days=900),
        remnawave_id=None,
        remnawave_short_uuid=None,
        connected_squads=[],
        subscription_url=None,
        subscription_crypto_link=None,
        traffic_limit_gb=61,
        traffic_used_gb=0.0,
        device_limit=100,
        is_active=False,
    )


def _api(*, by_telegram_id=(), by_email=()):
    """Строгий двойник клиента: несуществующие в 3.0.0 методы дают AttributeError."""
    api = MagicMock(spec=RemnaWaveAPI)
    api.get_user_by_id = AsyncMock(return_value=None)
    api.find_users_by_telegram_id = AsyncMock(return_value=list(by_telegram_id))
    api.find_users_by_email = AsyncMock(return_value=list(by_email))
    return api


def _service(api):
    @asynccontextmanager
    async def _client():
        yield api

    svc = MagicMock()
    svc.is_configured = True
    svc.get_api_client = _client
    return svc


async def _call(user, sub_id, api):
    db = AsyncMock()
    with (
        patch.object(type(au.settings), 'is_multi_tariff_enabled', MagicMock(return_value=True)),
        patch.object(au, 'get_user_by_id', AsyncMock(return_value=user)),
        patch('app.services.remnawave_service.RemnaWaveService', return_value=_service(api)),
    ):
        return await au.sync_user_from_panel(
            user_id=user.id, subscription_id=sub_id, request=SyncFromPanelRequest(), admin=MagicMock(), db=db
        )


@pytest.mark.asyncio
async def test_relinks_id_wiped_sub_and_restores_status_and_squads():
    sub = _wiped_sub(84)
    user = SimpleNamespace(
        id=7,
        telegram_id=123,
        email=None,
        remnawave_id=None,
        subscriptions=[sub],
        last_remnawave_sync=None,
        updated_at=None,
    )
    panel = _panel_user(44, squads=('sq1', 'sq2'))
    api = _api(by_telegram_id=[panel])

    resp = await _call(user, 84, api)

    assert resp.success is True
    api.find_users_by_telegram_id.assert_awaited_once_with(123)  # замена удалённому by-telegram-id
    assert sub.remnawave_id == 44  # re-linked to the live panel user
    assert sub.status == SubscriptionStatus.ACTIVE.value  # restored from panel ACTIVE
    assert sub.connected_squads == ['sq1', 'sq2']  # squads restored (dict extraction fixed)
    assert sub.subscription_url == 'https://panel/44'
    assert sub.remnawave_short_uuid == 'short-44'


@pytest.mark.asyncio
async def test_relinks_by_email_when_the_account_has_no_telegram_id():
    sub = _wiped_sub(84)
    user = SimpleNamespace(
        id=7,
        telegram_id=None,
        email='oauth@example.com',
        remnawave_id=None,
        subscriptions=[sub],
        last_remnawave_sync=None,
        updated_at=None,
    )
    api = _api(by_email=[_panel_user(55)])

    resp = await _call(user, 84, api)

    assert resp.success is True
    api.find_users_by_email.assert_awaited_once_with('oauth@example.com')  # замена удалённому by-email
    assert sub.remnawave_id == 55


@pytest.mark.asyncio
async def test_does_not_steal_panel_user_already_linked_to_sibling():
    wiped = _wiped_sub(84)
    sibling = _wiped_sub(85)
    sibling.remnawave_id = 772  # already linked
    user = SimpleNamespace(
        id=7,
        telegram_id=123,
        email=None,
        remnawave_id=None,
        subscriptions=[wiped, sibling],
        last_remnawave_sync=None,
        updated_at=None,
    )
    api = _api(by_telegram_id=[_panel_user(771), _panel_user(772)])

    resp = await _call(user, 84, api)

    assert resp.success is True
    assert wiped.remnawave_id == 771  # picked the orphan, not the sibling's panel user
    assert sibling.remnawave_id == 772  # sibling link untouched


@pytest.mark.asyncio
async def test_ambiguous_orphans_refuse_to_relink():
    sub = _wiped_sub(84)
    user = SimpleNamespace(
        id=7,
        telegram_id=123,
        email=None,
        remnawave_id=None,
        subscriptions=[sub],
        last_remnawave_sync=None,
        updated_at=None,
    )
    api = _api(by_telegram_id=[_panel_user(1001), _panel_user(1002)])

    with pytest.raises(HTTPException) as exc:
        await _call(user, 84, api)
    assert exc.value.status_code == 409
    assert sub.remnawave_id is None  # untouched — never guesses among multiple
