"""Кнопки заявки на вывод — по роли получателя, и все они живые.

Уведомление о заявке уходило в групповой админ-чат с «Одобрить»/«Отклонить»,
которые фильтр чатов там глушил, и с «Профиль пользователя» на callback
``admin_user_<telegram_id>``, у которого обработчика нет вовсе — кнопка мёртвая
и в личке. Кабинетная заявка приходила без кнопок совсем. Теперь клавиатура
одна на все пути: в группе — только действия, в личке админа — плюс профиль
(по id из базы, как в карточке пользователя) и навигация.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.database.models import WithdrawalRequestStatus
from app.keyboards.group_callbacks import is_group_safe_callback
from app.keyboards.withdrawal import get_withdrawal_request_keyboard
from app.services.admin_notification_service import AdminNotificationService


PENDING = WithdrawalRequestStatus.PENDING.value
APPROVED = WithdrawalRequestStatus.APPROVED.value
REJECTED = WithdrawalRequestStatus.REJECTED.value


def _callbacks(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_group_gets_only_actions_and_all_of_them_pass_the_chat_filter():
    for status_value in (PENDING, APPROVED):
        callbacks = _callbacks(get_withdrawal_request_keyboard(5, status_value, user_db_id=42, role='group'))
        assert callbacks, status_value
        assert not [c for c in callbacks if c.startswith(('admin_user_', 'admin_withdrawal_requests'))], callbacks
        assert all(is_group_safe_callback(c) for c in callbacks), callbacks


def test_pending_offers_approve_and_reject_and_approved_offers_complete():
    assert _callbacks(get_withdrawal_request_keyboard(5, PENDING, role='group')) == [
        'admin_withdrawal_approve_5',
        'admin_withdrawal_reject_5',
    ]
    assert _callbacks(get_withdrawal_request_keyboard(5, APPROVED, role='group')) == ['admin_withdrawal_complete_5']
    assert get_withdrawal_request_keyboard(5, REJECTED, role='group') is None, 'делать больше нечего — кнопок нет'


def test_admin_in_private_gets_profile_by_db_id_not_telegram_id():
    callbacks = _callbacks(get_withdrawal_request_keyboard(5, PENDING, user_db_id=42, role='admin'))
    assert 'admin_user_manage_42' in callbacks
    assert not any(c.startswith('admin_user_') and not c.startswith('admin_user_manage_') for c in callbacks)


def test_navigation_only_where_asked():
    plain = _callbacks(get_withdrawal_request_keyboard(5, REJECTED, user_db_id=42, role='admin'))
    listed = _callbacks(get_withdrawal_request_keyboard(5, REJECTED, user_db_id=42, role='admin', navigation=True))
    assert 'admin_withdrawal_requests' not in plain
    assert 'admin_withdrawal_requests' in listed


def test_moderator_and_outsider_get_nothing():
    """Одобрение — только для админа (@admin_required); модератору кнопки бы отвечали «нет доступа»."""
    assert get_withdrawal_request_keyboard(5, PENDING, user_db_id=42, role='moderator') is None
    assert get_withdrawal_request_keyboard(5, PENDING, user_db_id=42, role='none') is None


@pytest.fixture
def admin_chat(monkeypatch):
    monkeypatch.setattr(Settings, 'is_admin', lambda self, user_id: True)
    monkeypatch.setattr(Settings, 'format_price', lambda self, kopeks, **kw: f'{kopeks / 100:.0f} ₽')
    service = AdminNotificationService(MagicMock())
    service.enabled = True
    service.chat_id = -100123
    service._send_message = AsyncMock(return_value=True)
    return service


def _user() -> MagicMock:
    user = MagicMock()
    user.id = 42
    user.telegram_id = 777
    user.username = 'author'
    user.full_name = 'Автор'
    user.email = None
    user.balance_kopeks = 100_000
    return user


@pytest.mark.asyncio
async def test_cabinet_withdrawal_notification_carries_the_same_buttons(admin_chat):
    assert await admin_chat.send_withdrawal_request_notification(_user(), 50_000, 'карта', request_id=5) is True

    markup = admin_chat._send_message.await_args.kwargs['reply_markup']
    assert _callbacks(markup) == ['admin_withdrawal_approve_5', 'admin_withdrawal_reject_5'], 'группа: только действия'


@pytest.mark.asyncio
async def test_notification_without_request_id_stays_plain(admin_chat):
    assert await admin_chat.send_withdrawal_request_notification(_user(), 50_000, 'карта') is True

    assert admin_chat._send_message.await_args.kwargs.get('reply_markup') is None
