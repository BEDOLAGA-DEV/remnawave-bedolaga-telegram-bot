"""
Тест фильтра по статусу в источнике пользователей для проверок трафика.

Регрессия: суточная/быстрая проверки дёргали bandwidth-stats для ВСЕХ юзеров
панели, включая DISABLED (в т.ч. осиротевшие «хвосты» от удаления через бота,
когда панельное удаление зафолбэчилось в деактивацию) и EXPIRED. Их мёртвые
UUID вешали запрос статистики в таймаут → ежедневный спам ошибкой в админ-чат.
``get_all_users_with_traffic`` теперь отсекает DISABLED/EXPIRED в самом источнике.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.external.remnawave_api import UserStatus
from app.services.traffic_monitoring_service import (
    _NON_MONITORED_STATUSES,
    TrafficMonitoringServiceV2,
)


@pytest.fixture
def service():
    return TrafficMonitoringServiceV2()


def _make_user(uuid: str, status: UserStatus) -> MagicMock:
    user = MagicMock()
    user.uuid = uuid
    user.status = status
    return user


def _mock_api_client(service, batches: list[list[MagicMock]]) -> AsyncMock:
    """Подменяет get_api_client() контекст-менеджером, чьи get_all_users
    отдают переданные батчи по порядку (последний короткий батч завершает цикл).
    """
    api = MagicMock()
    api.get_all_users = AsyncMock(side_effect=[{'users': batch, 'total': len(batch)} for batch in batches])

    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=api)
    acm.__aexit__ = AsyncMock(return_value=False)
    service.remnawave_service.get_api_client = MagicMock(return_value=acm)
    # Большой batch_size, чтобы один короткий батч завершил пагинацию
    service.get_batch_size = MagicMock(return_value=100)
    return api


async def test_disabled_and_expired_are_filtered_out(service):
    """DISABLED/EXPIRED отсекаются, ACTIVE/LIMITED остаются."""
    users = [
        _make_user('active-1', UserStatus.ACTIVE),
        _make_user('disabled-1', UserStatus.DISABLED),  # «хвост» от удаления через бота
        _make_user('limited-1', UserStatus.LIMITED),
        _make_user('expired-1', UserStatus.EXPIRED),
    ]
    _mock_api_client(service, [users])

    result = await service.get_all_users_with_traffic()

    uuids = [u.uuid for u in result]
    assert uuids == ['active-1', 'limited-1']
    assert all(u.status not in _NON_MONITORED_STATUSES for u in result)


async def test_all_active_pass_through(service):
    """Когда все активны — ничего не теряется."""
    users = [
        _make_user('a', UserStatus.ACTIVE),
        _make_user('b', UserStatus.ACTIVE),
        _make_user('c', UserStatus.LIMITED),
    ]
    _mock_api_client(service, [users])

    result = await service.get_all_users_with_traffic()

    assert {u.uuid for u in result} == {'a', 'b', 'c'}


async def test_all_inactive_returns_empty(service):
    """Сплошь DISABLED/EXPIRED → пустой список (никого не проверяем)."""
    users = [
        _make_user('ghost-1', UserStatus.DISABLED),
        _make_user('ghost-2', UserStatus.EXPIRED),
    ]
    _mock_api_client(service, [users])

    result = await service.get_all_users_with_traffic()

    assert result == []


async def test_filter_applies_across_paginated_batches(service):
    """Фильтр работает на каждом батче; пагинация — по сырому размеру страницы."""
    batch_full = [_make_user(f'a{i}', UserStatus.ACTIVE) for i in range(99)]
    batch_full.append(_make_user('disabled-mid', UserStatus.DISABLED))  # 100 шт → ещё страница
    batch_last = [
        _make_user('active-last', UserStatus.ACTIVE),
        _make_user('expired-last', UserStatus.EXPIRED),
    ]
    api = _mock_api_client(service, [batch_full, batch_last])

    result = await service.get_all_users_with_traffic()

    # 99 активных из первого батча + 1 активный из второго; disabled/expired убраны
    assert len(result) == 100
    assert 'disabled-mid' not in {u.uuid for u in result}
    assert 'expired-last' not in {u.uuid for u in result}
    assert 'active-last' in {u.uuid for u in result}
    # Должно быть две страницы (первый батч был «полным» = 100)
    assert api.get_all_users.call_count == 2
