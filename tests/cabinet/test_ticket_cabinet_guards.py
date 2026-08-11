"""Кабинетные тикет-роуты обходили блокировку поддержки и лимит открытых тикетов.

Бот-путь (``app/handlers/tickets.py``) перед созданием тикета проверяет две
вещи через ``TicketCRUD``:

* ``is_user_globally_blocked`` — админ мог заблокировать пользователя в
  поддержке (навсегда или до даты);
* ``user_has_active_ticket`` — один незакрытый тикет на пользователя.

``app/cabinet/routes/tickets.py`` не проверял ни того, ни другого: единственным
условием было ``settings.is_support_tickets_enabled()``. Заблокированный
пользователь открывал кабинет и создавал тикеты пачками, лимит «один открытый
тикет» тоже обходился.

Дополнительно ``get_ticket`` и ``add_ticket_message`` вообще не смотрели на
режим поддержки: при ``SUPPORT_SYSTEM_MODE=contact`` (тикеты выключены) список
тикетов отдавал 403, а чтение конкретного тикета и дозапись в него продолжали
работать.

Тесты дёргают функции-роуты напрямую: guard'ы обязаны сработать до любых
обращений к БД, поэтому ``db`` — простой ``AsyncMock``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes import tickets as tickets_route
from app.cabinet.schemas.tickets import TicketCreateRequest, TicketMessageCreateRequest
from app.database.models import User


PERMANENT_BLOCK = datetime.max.replace(tzinfo=UTC)


@pytest.fixture
def user() -> User:
    return User(id=42, telegram_id=123456)


def _set_mode(monkeypatch, mode: str) -> None:
    """Выставить persisted-режим поддержки.

    Guard читает режим у SupportSettingsService (а не у ``settings``), поэтому
    подменяем его загруженные данные — реальная цепочка _load -> get_system_mode
    -> is_tickets_enabled при этом исполняется целиком.
    """
    monkeypatch.setattr(tickets_route.SupportSettingsService, '_loaded', True)
    monkeypatch.setattr(tickets_route.SupportSettingsService, '_data', {'system_mode': mode})


@pytest.fixture
def _tickets_enabled(monkeypatch) -> None:
    """Режим поддержки с включёнными тикетами (дефолт ``both``)."""
    _set_mode(monkeypatch, 'both')


@pytest.fixture
def _tickets_disabled(monkeypatch) -> None:
    """Режим ``contact`` — тикеты выключены."""
    _set_mode(monkeypatch, 'contact')


def _patch_crud(monkeypatch, *, blocked_until=None, has_active: bool = False) -> None:
    monkeypatch.setattr(
        tickets_route.TicketCRUD,
        'is_user_globally_blocked',
        AsyncMock(return_value=blocked_until),
    )
    monkeypatch.setattr(
        tickets_route.TicketCRUD,
        'user_has_active_ticket',
        AsyncMock(return_value=has_active),
    )


def _create_request() -> TicketCreateRequest:
    return TicketCreateRequest(title='Не работает VPN', message='Помогите, пожалуйста')


def _message_request() -> TicketMessageCreateRequest:
    return TicketMessageCreateRequest(message='Есть новости?')


# --- Блокировка в поддержке -------------------------------------------------


@pytest.mark.parametrize(
    'blocked_until',
    [
        pytest.param(PERMANENT_BLOCK, id='permanent'),
        pytest.param(datetime.now(UTC) + timedelta(days=3), id='temporary'),
    ],
)
@pytest.mark.usefixtures('_tickets_enabled')
async def test_create_ticket_rejects_blocked_user(monkeypatch, user, blocked_until):
    """REGRESSION: заблокированный в поддержке пользователь не должен создавать
    тикеты через кабинет — бот-путь его останавливает, кабинет пропускал."""
    _patch_crud(monkeypatch, blocked_until=blocked_until)

    with pytest.raises(HTTPException) as exc_info:
        await tickets_route.create_ticket(request=_create_request(), user=user, db=AsyncMock())

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert 'blocked' in str(exc_info.value.detail).lower()


@pytest.mark.usefixtures('_tickets_enabled')
async def test_add_message_rejects_blocked_user(monkeypatch, user):
    """REGRESSION: дозапись в тикет тоже должна упираться в глобальную блокировку."""
    _patch_crud(monkeypatch, blocked_until=PERMANENT_BLOCK)

    with pytest.raises(HTTPException) as exc_info:
        await tickets_route.add_ticket_message(ticket_id=1, request=_message_request(), user=user, db=AsyncMock())

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert 'blocked' in str(exc_info.value.detail).lower()


@pytest.mark.usefixtures('_tickets_enabled')
async def test_expired_block_does_not_reject(monkeypatch, user):
    """Истёкшая блокировка не считается: CRUD возвращает None, guard молчит."""
    _patch_crud(monkeypatch, blocked_until=None)

    await tickets_route._ensure_not_blocked(AsyncMock(), user)  # не должно бросить


# --- Лимит «один открытый тикет» -------------------------------------------


@pytest.mark.usefixtures('_tickets_enabled')
async def test_create_ticket_rejects_second_open_ticket(monkeypatch, user):
    """REGRESSION: второй незакрытый тикет через кабинет создать нельзя."""
    _patch_crud(monkeypatch, has_active=True)

    with pytest.raises(HTTPException) as exc_info:
        await tickets_route.create_ticket(request=_create_request(), user=user, db=AsyncMock())

    assert exc_info.value.status_code == status.HTTP_409_CONFLICT
    assert 'open ticket' in str(exc_info.value.detail).lower()


@pytest.mark.usefixtures('_tickets_enabled')
async def test_add_message_not_limited_by_open_ticket_check(monkeypatch, user):
    """Лимит открытых тикетов относится только к созданию: отвечать в свой
    открытый тикет пользователь обязан мочь."""
    _patch_crud(monkeypatch, has_active=True)
    db = AsyncMock()
    # scalar_one_or_none() синхронный — иначе AsyncMock вернёт корутину
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    with pytest.raises(HTTPException) as exc_info:
        await tickets_route.add_ticket_message(ticket_id=1, request=_message_request(), user=user, db=db)

    # 404 (а не 409) => guard'ы пройдены, дошли до поиска тикета
    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


# --- Режим поддержки --------------------------------------------------------


@pytest.mark.usefixtures('_tickets_disabled')
async def test_get_ticket_respects_disabled_mode(user):
    """REGRESSION: при SUPPORT_SYSTEM_MODE=contact чтение тикета должно давать
    403, как и список тикетов, а не отдавать содержимое."""
    with pytest.raises(HTTPException) as exc_info:
        await tickets_route.get_ticket(ticket_id=1, user=user, db=AsyncMock())

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == 'Support tickets are disabled'


@pytest.mark.usefixtures('_tickets_disabled')
async def test_add_message_respects_disabled_mode(user):
    """REGRESSION: при выключенных тикетах дозапись в существующий тикет
    должна отклоняться."""
    with pytest.raises(HTTPException) as exc_info:
        await tickets_route.add_ticket_message(ticket_id=1, request=_message_request(), user=user, db=AsyncMock())

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == 'Support tickets are disabled'


@pytest.mark.usefixtures('_tickets_disabled')
async def test_create_ticket_respects_disabled_mode(user):
    """Пин существующего поведения create_ticket."""
    with pytest.raises(HTTPException) as exc_info:
        await tickets_route.create_ticket(request=_create_request(), user=user, db=AsyncMock())

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == 'Support tickets are disabled'


@pytest.mark.usefixtures('_tickets_disabled')
async def test_get_tickets_respects_disabled_mode(user):
    """Пин существующего поведения списка тикетов."""
    with pytest.raises(HTTPException) as exc_info:
        await tickets_route.get_tickets(user=user, db=AsyncMock())

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == 'Support tickets are disabled'
