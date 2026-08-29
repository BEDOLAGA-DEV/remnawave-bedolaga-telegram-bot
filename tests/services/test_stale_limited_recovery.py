"""Страховка от залипшего LIMITED после апдейта панели.

Крон панели ``findExceededUsers`` (``*/45 * * * * *``) ставит LIMITED всем, у кого
``status=ACTIVE AND usedTraffic >= trafficLimit``. Если он успевает сработать между
чтением и записью нашего PATCH, юзер остаётся LIMITED уже с ПОДНЯТЫМ лимитом:
``users.service.ts::updateUser`` снимает статус только когда до записи юзер был не
ACTIVE, либо был LIMITED и лимит вырос. В окне гонки не выполняется ни то, ни другое,
а обратного пересчёта «used < limit ⇒ снять LIMITED» в панели нет ни в одном кроне.

Один PATCH на операцию сужает окно, но не закрывает его: крон может встать между
чтением и записью самого этого PATCH. Поэтому по ответу панели проверяем статус и
дожимаем ``POST /api/users/{id}/actions/enable``, когда лимит уже больше
израсходованного. Лечит гонку для всех повышений лимита, а не только для колеса.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import UserStatus
from app.services.subscription_service import SubscriptionService


def _panel_user(status: UserStatus, limit_bytes: int, used_bytes: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1620,
        status=status,
        traffic_limit_bytes=limit_bytes,
        user_traffic=None if used_bytes is None else SimpleNamespace(used_traffic_bytes=used_bytes),
    )


GB = 1024**3


def _api(enabled_user=None) -> AsyncMock:
    api = AsyncMock()
    api.enable_user = AsyncMock(return_value=enabled_user)
    api.get_user_by_id = AsyncMock(return_value=None)
    return api


@pytest.mark.asyncio
async def test_enables_when_limit_now_exceeds_used() -> None:
    """Реальный кейс: лимит поднят до 410 ГиБ, израсходовано 400.07 — LIMITED залип."""
    svc = SubscriptionService()
    limited = _panel_user(UserStatus.LIMITED, 410 * GB, int(400.07 * GB))
    active = _panel_user(UserStatus.ACTIVE, 410 * GB, int(400.07 * GB))
    api = _api(enabled_user=active)

    result = await svc._clear_stale_limited_status(api, 1620, limited, intended_active=True)

    api.enable_user.assert_awaited_once_with(1620)
    assert result.status == UserStatus.ACTIVE


@pytest.mark.asyncio
async def test_enables_when_limit_is_unlimited() -> None:
    svc = SubscriptionService()
    limited = _panel_user(UserStatus.LIMITED, 0, 500 * GB)
    api = _api(enabled_user=_panel_user(UserStatus.ACTIVE, 0, 500 * GB))

    await svc._clear_stale_limited_status(api, 1620, limited, intended_active=True)

    api.enable_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_leaves_genuinely_exceeded_user_limited() -> None:
    """Трафик реально исчерпан — LIMITED заслуженный, снимать его нельзя."""
    svc = SubscriptionService()
    limited = _panel_user(UserStatus.LIMITED, 400 * GB, int(400.07 * GB))
    api = _api()

    result = await svc._clear_stale_limited_status(api, 1620, limited, intended_active=True)

    api.enable_user.assert_not_awaited()
    assert result.status == UserStatus.LIMITED


@pytest.mark.asyncio
async def test_ignores_non_limited_statuses() -> None:
    svc = SubscriptionService()
    for status in (UserStatus.ACTIVE, UserStatus.DISABLED, UserStatus.EXPIRED):
        api = _api()
        await svc._clear_stale_limited_status(api, 1620, _panel_user(status, 410 * GB, 1 * GB), intended_active=True)
        api.enable_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_does_nothing_when_we_did_not_intend_active() -> None:
    """Подписка истекла — мы сами отправили DISABLED, снимать статус не наше дело."""
    svc = SubscriptionService()
    api = _api()

    await svc._clear_stale_limited_status(
        api, 1620, _panel_user(UserStatus.LIMITED, 410 * GB, 1 * GB), intended_active=False
    )

    api.enable_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_asks_panel_for_used_traffic_when_response_omits_it() -> None:
    """Ответ на PATCH может не содержать userTraffic — тогда спрашиваем отдельно."""
    svc = SubscriptionService()
    limited = _panel_user(UserStatus.LIMITED, 410 * GB, None)
    api = _api(enabled_user=_panel_user(UserStatus.ACTIVE, 410 * GB, int(400.07 * GB)))
    api.get_user_by_id = AsyncMock(return_value=_panel_user(UserStatus.LIMITED, 410 * GB, int(400.07 * GB)))

    await svc._clear_stale_limited_status(api, 1620, limited, intended_active=True)

    api.get_user_by_id.assert_awaited_once_with(1620)
    api.enable_user.assert_awaited_once_with(1620)


@pytest.mark.asyncio
async def test_stays_put_when_used_traffic_is_unknowable() -> None:
    """Расход выяснить не удалось — не трогаем: воскресить исчерпавшего хуже."""
    svc = SubscriptionService()
    limited = _panel_user(UserStatus.LIMITED, 410 * GB, None)
    api = _api()

    await svc._clear_stale_limited_status(api, 1620, limited, intended_active=True)

    api.enable_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_enable_failure_does_not_break_the_caller() -> None:
    """Приз уже начислен и закоммичен — падать на страховке нельзя."""
    svc = SubscriptionService()
    limited = _panel_user(UserStatus.LIMITED, 410 * GB, int(400.07 * GB))
    api = _api()
    api.enable_user = AsyncMock(side_effect=RuntimeError('panel down'))

    result = await svc._clear_stale_limited_status(api, 1620, limited, intended_active=True)

    assert result is limited
