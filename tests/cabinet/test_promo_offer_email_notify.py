"""Промопредложения для email-only юзеров (прод-репорт 2026-07-13).

Раньше шаблоны промопредложений уходили только юзерам с telegram_id:
кабинетный /admin/promo-offers/broadcast создавал оффер, но фан-аут
уведомлений скипал получателей без Telegram; бот-рассылка шаблонов скипала
их целиком (даже без создания оффера). Теперь оба пути шлют письмо на
подтверждённую почту через общий app/services/promo_offer_email.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock

from app.cabinet.routes import admin_promo_offers as route_module
from app.services import promo_offer_email as m


# `import app.cabinet.services.email_service as X` вернул бы ИНСТАНС EmailService:
# пакетный __init__ шэдоуит имя сабмодуля одноимённым синглтоном. importlib
# отдаёт настоящий модуль из sys.modules — патчим его атрибуты.
email_service_module = importlib.import_module('app.cabinet.services.email_service')
overrides_module = importlib.import_module('app.cabinet.services.email_template_overrides')


async def test_send_promo_offer_email_skips_when_smtp_not_configured(monkeypatch):
    email_service = MagicMock()
    email_service.is_configured = MagicMock(return_value=False)
    monkeypatch.setattr(email_service_module, 'email_service', email_service)

    ok = await m.send_promo_offer_email(
        email='u@example.com',
        language='ru',
        message_text='hi',
        valid_hours=24,
    )

    assert ok is False
    email_service.send_email.assert_not_called()


async def test_send_promo_offer_email_renders_template_and_sends(monkeypatch):
    email_service = MagicMock()
    email_service.is_configured = MagicMock(return_value=True)
    email_service.send_email = MagicMock(return_value=True)
    monkeypatch.setattr(email_service_module, 'email_service', email_service)
    # Без DB-override — падаем на дефолтный шаблон
    monkeypatch.setattr(overrides_module, 'get_rendered_override', AsyncMock(return_value=None))

    ok = await m.send_promo_offer_email(
        email='u@example.com',
        language='ru',
        username='Вася',
        message_text='🔥 Скидка <b>20%</b>\nна подписку',
        valid_hours=24,
        discount_percent=20,
    )

    assert ok is True
    email_service.send_email.assert_called_once()
    kwargs = email_service.send_email.call_args.kwargs
    assert kwargs['to_email'] == 'u@example.com'
    assert '20%' in kwargs['subject']
    # Telegram-текст конвертирован в HTML-фрагмент (переносы → <br>)
    assert 'Скидка <b>20%</b><br>на подписку' in kwargs['body_html']


async def test_send_promo_offer_email_prefers_db_override(monkeypatch):
    email_service = MagicMock()
    email_service.is_configured = MagicMock(return_value=True)
    email_service.send_email = MagicMock(return_value=True)
    monkeypatch.setattr(email_service_module, 'email_service', email_service)
    monkeypatch.setattr(
        overrides_module,
        'get_rendered_override',
        AsyncMock(return_value=('SUBJ', '<p>OVERRIDE</p>')),
    )

    ok = await m.send_promo_offer_email(
        email='u@example.com',
        language='ru',
        message_text='hi',
        valid_hours=1,
    )

    assert ok is True
    kwargs = email_service.send_email.call_args.kwargs
    assert kwargs['subject'] == 'SUBJ'
    assert kwargs['body_html'] == '<p>OVERRIDE</p>'


async def test_promo_offer_email_template_registered():
    """Дефолтный шаблон promo_offer должен резолвиться из EmailNotificationTemplates."""
    from app.cabinet.services.email_templates import EmailNotificationTemplates
    from app.services.notification_delivery_service import NotificationType

    template = EmailNotificationTemplates().get_template(
        NotificationType.PROMO_OFFER,
        'ru',
        {'message_html': 'ТЕКСТ-МАРКЕР', 'valid_hours': 24, 'discount_percent': 15},
    )

    assert template is not None
    assert '15%' in template['subject']
    assert 'ТЕКСТ-МАРКЕР' in template['body_html']
    assert '24' in template['body_html']  # срок действия попал в тело


async def test_email_fanout_counts_sent_and_failed(monkeypatch):
    """Фан-аут работает на скалярных таргетах (email, language, username) и
    честно считает sent/failed — как Telegram-собрат."""
    calls: list[str] = []

    async def fake_send(**kwargs):
        calls.append(kwargs['email'])
        return kwargs['email'] != 'bad@example.com'

    monkeypatch.setattr('app.services.promo_offer_email.send_promo_offer_email', fake_send)

    sent, failed = await route_module._send_promo_email_notifications(
        [
            ('a@example.com', 'ru', 'A'),
            ('bad@example.com', 'en', 'B'),
            ('c@example.com', 'ru', 'C'),
        ],
        message_text='hi',
        discount_percent=10,
        bonus_amount_kopeks=0,
        valid_hours=24,
    )

    assert (sent, failed) == (2, 1)
    assert sorted(calls) == ['a@example.com', 'bad@example.com', 'c@example.com']


async def test_email_fanout_empty_targets_noop():
    sent, failed = await route_module._send_promo_email_notifications(
        [],
        message_text=None,
        discount_percent=0,
        bonus_amount_kopeks=0,
        valid_hours=1,
    )
    assert (sent, failed) == (0, 0)
