"""Письма о grace-доступе: в реестре редактора, на всех языках, без прибитого «Telegram».

Владелец (2026-09-14): «тут напрашивается шаблон для email; оператор захочет
дать доступ не только к Telegram, а к кабинету, сайту, чему угодно; у Telegram
есть разметка — нужно спроектировать». Что доступно — фраза оператора
({allowed}); она и имя тарифа экранируются, потому что попадают в HTML.
"""

from __future__ import annotations

import pytest

from app.cabinet.routes.admin_email_templates import AVAILABLE_LANGUAGES, SAMPLE_CONTEXTS, TEMPLATE_TYPES
from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.services.notification_types import NotificationType


CONTEXT = {
    'allowed': 'Telegram и личный кабинет',
    'hours': 72,
    'traffic_gb': '1',
    'until': '17.09.2026 12:00',
    'reason': 'expired',
    'tariff_name': 'Стартовый',
}


def test_grace_types_are_in_the_editor_with_their_variables():
    by_type = {entry['type']: entry for entry in TEMPLATE_TYPES}
    for key in ('grace_access_granted', 'grace_access_ended'):
        assert key in by_type, key
        assert set(by_type[key]['context_vars']) == set(CONTEXT), key
        assert set(CONTEXT) <= set(SAMPLE_CONTEXTS[key]), key


@pytest.mark.parametrize('language', AVAILABLE_LANGUAGES)
def test_granted_email_names_what_stays_reachable(language):
    template = EmailNotificationTemplates().get_template(NotificationType.GRACE_ACCESS_GRANTED, language, CONTEXT)

    assert template and template['subject'] and template['body_html']
    body = template['body_html']
    assert 'Telegram и личный кабинет' in body
    assert '72' in body and '17.09.2026 12:00' in body and 'Стартовый' in body
    assert not any(f'{{{var}}}' in body for var in CONTEXT), 'все переменные подставлены'


def test_limited_reason_changes_the_wording():
    templates = EmailNotificationTemplates()
    expired = templates.get_template(NotificationType.GRACE_ACCESS_GRANTED, 'ru', CONTEXT)['body_html']
    limited = templates.get_template(NotificationType.GRACE_ACCESS_GRANTED, 'ru', {**CONTEXT, 'reason': 'limited'})[
        'body_html'
    ]

    assert 'закончилась' in expired and 'исчерпала трафик' in limited


def test_operator_phrase_and_tariff_are_escaped_in_email():
    template = EmailNotificationTemplates().get_template(
        NotificationType.GRACE_ACCESS_ENDED,
        'ru',
        {**CONTEXT, 'allowed': '<script>x</script>', 'tariff_name': 'A&B'},
    )

    assert '<script>' not in template['body_html']
    assert '&lt;script&gt;' in template['body_html'] and 'A&amp;B' in template['body_html']


def test_ended_email_says_access_is_closed():
    template = EmailNotificationTemplates().get_template(NotificationType.GRACE_ACCESS_ENDED, 'en', CONTEXT)

    assert 'closed' in template['body_html'] and 'Telegram и личный кабинет' in template['body_html']


@pytest.mark.parametrize('event', ['granted', 'ended'])
@pytest.mark.parametrize('language', ['ru', 'en', 'zh', 'ua'])
def test_operator_phrase_stands_in_a_case_neutral_slot_in_email(event, language):
    """Фразу оператора нельзя склонять: слот после двоеточия подходит любой фразе."""
    import re

    _subject, body, _why = EmailNotificationTemplates.GRACE_EMAIL_COPY[event][language]

    assert re.search(r'[:：]\s*<strong>\{allowed\}</strong>', body), f'{event}/{language}: {body}'
