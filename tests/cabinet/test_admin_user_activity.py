"""Тесты таймлайна активности пользователя (GET /cabinet/admin/users/{id}/activity).

House pattern админ-роутов: smoke регистрации роутов + прямые вызовы хендлера
с fake db (`require_permission`/`get_cabinet_db` обходятся передачей уже
разрешённых аргументов).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_users import _activity_sources, get_user_activity


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def test_activity_route_registered() -> None:
    from app.cabinet.routes import router

    paths = {route.path: getattr(route, 'methods', set()) for route in router.routes}
    assert 'GET' in paths.get('/cabinet/admin/users/{user_id}/activity', set())


def test_activity_sources_shape() -> None:
    sources = _activity_sources(1)

    assert set(sources) == {
        'transaction',
        'event',
        'promocode',
        'coupon',
        'ticket',
        'wheel_spin',
        'poll',
        'gift_sent',
        'gift_received',
        'referral_earning',
        'cabinet_login',
        'withdrawal',
        'button_click',
        'cabinet_action',
    }
    for query, count_query, ts_column, mapper in sources.values():
        assert callable(mapper)
        assert ts_column is not None


def test_activity_dedup_filters_in_sql() -> None:
    """Транзакции, покрытые событиями/начислениями, исключаются на уровне SQL;
    события promocode_activation уступают PromoCodeUse."""
    sources = _activity_sources(1)

    transactions_sql = str(sources['transaction'][0])
    assert 'NOT IN' in transactions_sql.upper()
    assert 'subscription_events' in transactions_sql
    assert 'referral_earnings' in transactions_sql

    events_sql = str(sources['event'][0])
    assert 'event_type' in events_sql


def _db_returning_empty() -> AsyncMock:
    db = AsyncMock()

    async def execute(query):
        result = MagicMock()
        result.scalar.return_value = 0
        result.all.return_value = []
        return result

    db.execute = AsyncMock(side_effect=execute)
    return db


async def test_activity_unknown_user_404() -> None:
    db = AsyncMock()
    with patch('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await get_user_activity(user_id=1, offset=0, limit=50, types=None, admin=SimpleNamespace(id=1), db=db)
    assert exc.value.status_code == 404


async def test_activity_unknown_type_400() -> None:
    db = _db_returning_empty()
    user = SimpleNamespace(id=1)
    with patch('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=user)):
        with pytest.raises(HTTPException) as exc:
            await get_user_activity(
                user_id=1, offset=0, limit=50, types='transaction,nonsense', admin=SimpleNamespace(id=1), db=db
            )
    assert exc.value.status_code == 400
    assert 'nonsense' in exc.value.detail


async def test_activity_merges_and_sorts_desc() -> None:
    """Записи из разных источников сливаются и сортируются по времени убыванию."""
    user = SimpleNamespace(id=1)

    transaction = SimpleNamespace(
        type='deposit',
        description='Пополнение',
        amount_kopeks=10000,
        created_at=NOW - timedelta(hours=1),
        payment_method='stars',
        is_completed=True,
    )
    ticket = SimpleNamespace(id=9, status='open', title='Помогите', created_at=NOW - timedelta(minutes=5))

    call_index = 0

    async def execute(query):
        nonlocal call_index
        call_index += 1
        result = MagicMock()
        sql = str(query)
        if 'count' in sql.lower():
            result.scalar.return_value = 1 if ('transactions' in sql or 'tickets' in sql) else 0
            return result
        if 'FROM transactions' in sql:
            result.all.return_value = [(transaction,)]
        elif 'FROM tickets' in sql:
            result.all.return_value = [(ticket,)]
        else:
            result.all.return_value = []
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute)

    with patch('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=user)):
        response = await get_user_activity(
            user_id=1, offset=0, limit=50, types=None, admin=SimpleNamespace(id=1), db=db
        )

    assert response.total == 2
    assert [item.type for item in response.items] == ['ticket', 'transaction']
    assert response.items[0].title == 'Помогите'
    assert response.items[1].amount_kopeks == 10000
    assert response.items[1].meta == {'payment_method': 'stars', 'is_completed': True}


async def test_activity_types_filter_limits_sources() -> None:
    user = SimpleNamespace(id=1)
    executed_sql: list[str] = []

    async def execute(query):
        executed_sql.append(str(query))
        result = MagicMock()
        result.scalar.return_value = 0
        result.all.return_value = []
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute)

    with patch('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=user)):
        response = await get_user_activity(
            user_id=1, offset=0, limit=50, types='ticket', admin=SimpleNamespace(id=1), db=db
        )

    assert response.total == 0
    assert response.items == []
    # Только счётчик и выборка тикетов — другие таблицы не опрашиваются
    assert len(executed_sql) == 2
    assert all('tickets' in sql for sql in executed_sql)


def test_button_click_sources_split_by_type() -> None:
    """Клики бота и действия кабинета — раздельные источники одной таблицы."""
    sources = _activity_sources(1)

    bot_sql = str(sources['button_click'][0])
    cabinet_sql = str(sources['cabinet_action'][0])
    assert 'button_click_logs' in bot_sql
    assert 'button_click_logs' in cabinet_sql
    assert 'button_type' in bot_sql
    assert 'button_type' in cabinet_sql
