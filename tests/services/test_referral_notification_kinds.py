"""Три вида реферальных уведомлений email-пользователю: награда, новый реферал,
приветствие приглашённого.

До правки у ``notification_delivery_service`` был единственный реферальный
метод ``notify_referral_bonus`` — и все события уходили письмом «Реферальный
бонус: +0 ₽», хотя шаблон «Новый реферал» существовал и был доступен в
редакторе. Здесь закрепляются собственный тип у каждого события, состав
контекста и общий выключатель реферальных уведомлений.
"""

from types import SimpleNamespace

import pytest

from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.config import settings
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service as svc,
)


def _capture_send(monkeypatch) -> dict:
    captured: dict = {}

    async def fake_send(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(svc, 'send_notification', fake_send)
    return captured


@pytest.mark.asyncio
async def test_registered_notice_carries_its_own_type(monkeypatch):
    captured = _capture_send(monkeypatch)
    user = SimpleNamespace(id=1, language='ru')

    ok = await svc.notify_referral_registered(user=user, referral_name='Иван', telegram_message='msg')

    assert ok is True
    assert captured['user'] is user
    assert captured['notification_type'] is NotificationType.REFERRAL_REGISTERED
    assert captured['context'] == {'referral_name': 'Иван'}
    assert captured['telegram_message'] == 'msg'


@pytest.mark.asyncio
async def test_welcome_notice_carries_referrer_and_promise(monkeypatch):
    captured = _capture_send(monkeypatch)

    await svc.notify_referral_welcome(
        user=SimpleNamespace(id=1, language='ru'),
        referrer_name='Пётр',
        bonus_promise='7 дн. подписки',
    )

    assert captured['notification_type'] is NotificationType.REFERRAL_WELCOME
    assert captured['context'] == {'referrer_name': 'Пётр', 'bonus_promise': '7 дн. подписки'}


@pytest.mark.parametrize(
    'notification_type',
    [NotificationType.REFERRAL_BONUS, NotificationType.REFERRAL_REGISTERED, NotificationType.REFERRAL_WELCOME],
)
def test_every_referral_kind_obeys_referral_switch(monkeypatch, notification_type):
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', False)

    assert svc._is_allowed_by_preferences(SimpleNamespace(id=1), notification_type) is False

    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)
    assert svc._is_allowed_by_preferences(SimpleNamespace(id=1), notification_type) is True


def test_registered_template_never_mentions_money():
    rendered = EmailNotificationTemplates().get_template(
        NotificationType.REFERRAL_REGISTERED, 'ru', {'referral_name': 'Иван'}
    )

    assert '+0' not in rendered['subject']
    assert '₽' not in rendered['subject']
    assert 'Иван' in rendered['body_html']


@pytest.mark.parametrize(('lang', 'expected_lead'), [('ru', 'Добро пожаловать'), ('en', 'Welcome')])
def test_welcome_template_names_referrer_and_promise(lang, expected_lead):
    rendered = EmailNotificationTemplates().get_template(
        NotificationType.REFERRAL_WELCOME,
        lang,
        {'referrer_name': 'Пётр <b>', 'bonus_promise': '7 дн. подписки'},
    )

    assert expected_lead in rendered['subject']
    assert 'Пётр &lt;b&gt;' in rendered['body_html']
    assert '7 дн. подписки' in rendered['body_html']
    assert '+0' not in rendered['body_html']


def test_welcome_template_without_promise_skips_bonus_line():
    rendered = EmailNotificationTemplates().get_template(
        NotificationType.REFERRAL_WELCOME, 'ru', {'referrer_name': 'Пётр', 'bonus_promise': ''}
    )

    assert 'Пётр' in rendered['body_html']
    assert 'Ваш бонус' not in rendered['body_html']
