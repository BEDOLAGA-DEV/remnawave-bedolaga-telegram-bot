"""Email-уведомление об ответе поддержки для юзеров без Telegram.

``notify_user_about_ticket_reply`` — единая воронка всех путей ответа админа
(бот-админка, кабинет, мобильный WS-мост, webapi). Для юзера без
``telegram_id`` (регистрация по email) она раньше писала warning и выходила:
человек узнавал об ответе, только если сам заходил в кабинет.

Теперь такой пользователь получает письмо через мультиканальный роутер
``notification_delivery_service``. Тумблер уведомлений остаётся один и тот же
(``SupportSettingsService.get_user_ticket_notifications_enabled``): он
проверяется в начале воронки и гейтит оба канала.
"""

from types import SimpleNamespace

import pytest

from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.handlers.admin import tickets as admin_tickets
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service,
)


def _user(**overrides):
    defaults = {
        'id': 7,
        'telegram_id': None,
        'username': None,
        'email': 'user@test.dev',
        'email_verified': True,
        'language': 'ru',
        'auth_type': 'email',
        'status': 'active',
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _ticket(user, ticket_id: int = 42):
    return SimpleNamespace(id=ticket_id, user=user, user_id=user.id)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Перехватывает send_notification роутера."""
    calls: list[dict] = []

    async def fake_send_notification(user, notification_type, context, **kwargs):
        calls.append({'user': user, 'type': notification_type, 'context': context, 'kwargs': kwargs})
        return True

    monkeypatch.setattr(notification_delivery_service, 'send_notification', fake_send_notification)
    return calls


@pytest.fixture(autouse=True)
def _notifications_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        admin_tickets.SupportSettingsService,
        'get_user_ticket_notifications_enabled',
        classmethod(lambda cls: True),
    )


@pytest.fixture
def last_message(monkeypatch: pytest.MonkeyPatch):
    """Подменяет чтение последнего сообщения тикета (проверка на фото)."""

    def _install(message=None):
        async def fake_get_last_message(db, ticket_id):
            return message

        monkeypatch.setattr(admin_tickets.TicketMessageCRUD, 'get_last_message', fake_get_last_message)

    _install()
    return _install


# ============ доставка ============


async def test_email_user_gets_ticket_reply_email(sent, last_message) -> None:
    user = _user()

    await admin_tickets.notify_user_about_ticket_reply(None, _ticket(user), 'Проверьте настройки', None)

    assert len(sent) == 1
    call = sent[0]
    assert call['type'] == NotificationType.TICKET_REPLY
    assert call['context']['ticket_id'] == 42
    assert call['context']['reply_preview'] == 'Проверьте настройки'
    assert call['context']['has_photo'] is False
    # Кабинет шлёт своё WS-событие ticket.admin_reply — второе дало бы дубль.
    assert call['kwargs']['use_websocket'] is False


async def test_photo_reply_marked_in_context(sent, last_message) -> None:
    last_message(SimpleNamespace(has_media=True, media_type='photo', is_from_admin=True, media_file_id='f1'))

    await admin_tickets.notify_user_about_ticket_reply(None, _ticket(_user()), 'Смотрите скриншот', None)

    assert sent[0]['context']['has_photo'] is True


async def test_long_reply_is_previewed(sent, last_message) -> None:
    await admin_tickets.notify_user_about_ticket_reply(None, _ticket(_user()), 'а' * 5000, None)

    preview = sent[0]['context']['reply_preview']
    assert len(preview) < 5000
    assert preview.endswith('...')


async def test_telegram_user_does_not_get_email(sent, last_message, monkeypatch: pytest.MonkeyPatch) -> None:
    """Юзеру с Telegram ответ уже ушёл в бот — письмо было бы дублем."""
    sends: list = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sends.append(kwargs)

        async def send_photo(self, **kwargs):
            sends.append(kwargs)

    await admin_tickets.notify_user_about_ticket_reply(FakeBot(), _ticket(_user(telegram_id=12345)), 'Ответ', None)

    assert sends, 'Telegram-юзер должен получить сообщение в бот'
    assert sent == []


async def test_disabled_toggle_blocks_email(sent, last_message, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        admin_tickets.SupportSettingsService,
        'get_user_ticket_notifications_enabled',
        classmethod(lambda cls: False),
    )

    await admin_tickets.notify_user_about_ticket_reply(None, _ticket(_user()), 'Ответ', None)

    assert sent == []


async def test_global_notifications_switch_does_not_mute_support_replies(last_message, monkeypatch) -> None:
    """ENABLE_NOTIFICATIONS не должен глушить ответ поддержки только email-юзеру.

    Telegram-ветка шлёт уведомление мимо роутера и глобальный тумблер не смотрит:
    её единственный гейт — user_ticket_notifications_enabled. Если email-канал
    гейтить ещё и глобальным тумблером, при ENABLE_NOTIFICATIONS=false Telegram-юзер
    ответ получит, а email-юзер молча останется без него.
    """
    from app.config import settings

    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', False)

    emails: list = []

    async def fake_email(user, notification_type, context):
        emails.append(notification_type)
        return True

    monkeypatch.setattr(notification_delivery_service, '_send_email_notification', fake_email)

    telegram: list = []

    class FakeBot:
        async def send_message(self, **kwargs):
            telegram.append(kwargs)

        async def send_photo(self, **kwargs):
            telegram.append(kwargs)

    await admin_tickets.notify_user_about_ticket_reply(None, _ticket(_user()), 'Ответ', None)
    await admin_tickets.notify_user_about_ticket_reply(
        FakeBot(), _ticket(_user(telegram_id=12345), ticket_id=43), 'Ответ', None
    )

    assert telegram, 'Telegram-юзер получает ответ независимо от глобального тумблера'
    assert emails == [NotificationType.TICKET_REPLY], 'email-юзер должен получить тот же ответ'


async def test_user_without_verified_email_is_skipped(sent, last_message) -> None:
    await admin_tickets.notify_user_about_ticket_reply(None, _ticket(_user(email_verified=False)), 'Ответ', None)

    assert sent == []


async def test_delivery_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch, last_message) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError('smtp down')

    monkeypatch.setattr(notification_delivery_service, 'send_notification', boom)

    # Ответ админа уже сохранён в БД — сбой доставки не должен ронять запрос.
    await admin_tickets.notify_user_about_ticket_reply(None, _ticket(_user()), 'Ответ', None)


# ============ шаблон ============


@pytest.mark.parametrize('language', ['ru', 'en', 'zh', 'fa'])
def test_template_renders_for_supported_languages(language: str) -> None:
    template = EmailNotificationTemplates().get_template(
        NotificationType.TICKET_REPLY,
        language,
        {'ticket_id': 42, 'reply_preview': 'Проверьте настройки', 'has_photo': False},
    )

    assert template
    assert '42' in template['subject']
    assert 'Проверьте настройки' in template['body_html']


def test_template_escapes_html_in_preview() -> None:
    """Ответ поддержки вида «откройте <config>» не должен ломать вёрстку письма."""
    template = EmailNotificationTemplates().get_template(
        NotificationType.TICKET_REPLY,
        'ru',
        {'ticket_id': 42, 'reply_preview': 'откройте <config> и <b>жмите</b>', 'has_photo': False},
    )

    assert '&lt;config&gt;' in template['body_html']
    assert '<b>жмите</b>' not in template['body_html']


def test_template_mentions_photo_when_reply_has_one() -> None:
    template = EmailNotificationTemplates().get_template(
        NotificationType.TICKET_REPLY,
        'ru',
        {'ticket_id': 42, 'reply_preview': 'Смотрите скриншот', 'has_photo': True},
    )

    assert 'изображение' in template['body_html']
