"""Кликабельность @username в админ-уведомлениях (issue #3106).

Админ-уведомления уходят rich-сообщением со ``skip_entity_detection=True``
(``app/utils/rich_admin.py``), поэтому Telegram не подсвечивает в них упоминания
сам — ссылку обязан поставить код, который собирает текст. Здесь закреплены
места, где голый ``@username`` был бы мёртвым текстом.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.utils.user_utils import format_referrer_info


def _referred_user(*, username: str | None, referred_by_id: int | None = 17, telegram_id: int | None = 555):
    referrer = SimpleNamespace(username=username, telegram_id=telegram_id)
    return SimpleNamespace(referred_by_id=referred_by_id, referrer=referrer)


def test_referrer_info_links_the_referrer_username():
    """Реферер в уведомлении о пополнении — та же ссылка, что и у плательщика.

    ``format_referrer_info`` — близнец ``AdminNotificationService._get_referrer_info``
    и единственный источник строки ``🔗 Реферер:`` для всех платёжных адаптеров.
    Если он отстанет от близнеца, в ОДНОМ сообщении плательщик окажется кликабельным,
    а реферер — нет.
    """
    assert format_referrer_info(_referred_user(username='sponsor')) == (
        '<a href="https://t.me/sponsor">@sponsor</a> (ID: 17)'
    )


def test_referrer_info_keeps_non_telegram_login_as_text():
    """OAuth-логин из кабинета не является Telegram-логином — ссылки быть не должно."""
    assert format_referrer_info(_referred_user(username='ivan.petrov')) == '@ivan.petrov (ID: 17)'


def test_referrer_info_without_username_falls_back_to_id():
    """Без логина остаётся прежний вид — идентификатор, без пустой ссылки."""
    assert format_referrer_info(_referred_user(username=None)) == 'ID 555'
    assert format_referrer_info(_referred_user(username=None, referred_by_id=None)) == 'Нет'


@pytest.mark.asyncio
async def test_new_ticket_notification_links_username(monkeypatch):
    """Текст уведомления о новом тикете содержит ссылку, а не голый @логин."""
    from app.handlers import tickets

    user = SimpleNamespace(id=3, telegram_id=555, email=None, username='durov', full_name='Егор')

    async def fake_get_user_by_id(db, user_id):
        return user

    async def fake_get_first_message(db, ticket_id):
        return None

    captured: dict[str, str] = {}

    class FakeService:
        def __init__(self, bot):
            self.bot = bot

        async def send_ticket_event_notification(self, text, keyboard=None, **kwargs):
            captured['text'] = text
            return True

    monkeypatch.setattr(tickets, 'get_user_by_id', fake_get_user_by_id)
    monkeypatch.setattr(tickets.TicketMessageCRUD, 'get_first_message', staticmethod(fake_get_first_message))
    monkeypatch.setattr(tickets, 'AdminNotificationService', FakeService)
    monkeypatch.setattr(tickets, '_build_ticket_notification_keyboard', lambda service, ticket, user: None)

    from app.services.maintenance_service import maintenance_service

    monkeypatch.setattr(maintenance_service, '_bot', object(), raising=False)

    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_admin_notifications_enabled', lambda self: True)

    ticket = SimpleNamespace(id=42, user_id=3, title='Не открывается', created_at=None)
    monkeypatch.setattr(tickets, 'format_local_datetime', lambda value, fmt: '01.01.2026 00:00')

    await tickets.notify_admins_about_new_ticket(ticket, db=None)

    assert '<b>Username:</b> <a href="https://t.me/durov">@durov</a>' in captured['text']
    assert '@@' not in captured['text']


@pytest.mark.asyncio
async def test_new_ticket_notification_without_username_has_no_stray_at(monkeypatch):
    """Без логина строка остаётся текстом-заглушкой, без осиротевшей собаки."""
    from app.handlers import tickets

    user = SimpleNamespace(id=3, telegram_id=555, email=None, username=None, full_name='Егор')

    async def fake_get_user_by_id(db, user_id):
        return user

    async def fake_get_first_message(db, ticket_id):
        return None

    captured: dict[str, str] = {}

    class FakeService:
        def __init__(self, bot):
            self.bot = bot

        async def send_ticket_event_notification(self, text, keyboard=None, **kwargs):
            captured['text'] = text
            return True

    monkeypatch.setattr(tickets, 'get_user_by_id', fake_get_user_by_id)
    monkeypatch.setattr(tickets.TicketMessageCRUD, 'get_first_message', staticmethod(fake_get_first_message))
    monkeypatch.setattr(tickets, 'AdminNotificationService', FakeService)
    monkeypatch.setattr(tickets, '_build_ticket_notification_keyboard', lambda service, ticket, user: None)
    monkeypatch.setattr(tickets, 'format_local_datetime', lambda value, fmt: '01.01.2026 00:00')

    from app.services.maintenance_service import maintenance_service

    monkeypatch.setattr(maintenance_service, '_bot', object(), raising=False)

    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_admin_notifications_enabled', lambda self: True)

    ticket = SimpleNamespace(id=42, user_id=3, title='Не открывается', created_at=None)

    await tickets.notify_admins_about_new_ticket(ticket, db=None)

    assert '<b>Username:</b> отсутствует' in captured['text']
    assert '@отсутствует' not in captured['text']
