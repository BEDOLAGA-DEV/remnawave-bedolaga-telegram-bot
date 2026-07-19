"""
Целостность рендера email-шаблонов.

Гарантии:
- каждый тип из редактора рендерится на всех языках без артефактов
  (ровно один HTML-документ, без неподставленных переменных);
- дефолтные письма верификации/сброса пароля/кода смены email уходят из
  ЕДИНОГО источника (EmailNotificationTemplates) — того же, что показывает
  редактор и использует сервис доставки уведомлений;
- обёртка override-шаблонов не дублирует документ.
"""

import pytest

from app.cabinet.routes.admin_email_templates import (
    AVAILABLE_LANGUAGES,
    SAMPLE_CONTEXTS,
    TEMPLATE_TYPES,
    _get_default_template,
    _placeholder_context,
)
from app.cabinet.services.email_service import EmailService
from app.cabinet.services.email_templates import EmailNotificationTemplates


ALL_TYPE_KEYS = [t['type'] for t in TEMPLATE_TYPES]


def _assert_single_document(body: str, label: str) -> None:
    lowered = body.lower()
    assert lowered.count('<!doctype') == 1, f'{label}: документ должен содержать ровно один DOCTYPE'
    assert lowered.count('<html') == 1, f'{label}: документ должен содержать ровно один <html>'
    assert lowered.count('</html>') == 1, f'{label}: документ должен содержать ровно один </html>'


# ============ Дефолтные шаблоны: все типы × все языки ============


@pytest.mark.parametrize('type_key', ALL_TYPE_KEYS)
@pytest.mark.parametrize('lang', AVAILABLE_LANGUAGES)
def test_default_template_renders_clean_for_every_language(type_key, lang):
    sample = SAMPLE_CONTEXTS[type_key]
    template = _get_default_template(type_key, lang, sample)
    assert template is not None, f'{type_key}/{lang}: шаблон не отрендерился'

    subject = template['subject']
    body = template['body_html']
    assert subject.strip(), f'{type_key}/{lang}: пустая тема'
    assert '\n' not in subject and '\r' not in subject, f'{type_key}/{lang}: перенос строки в теме'
    assert body.strip(), f'{type_key}/{lang}: пустое тело'
    _assert_single_document(body, f'{type_key}/{lang}')

    # Все переменные, для которых есть sample-значения, должны быть подставлены
    leftover = [var for var in sample if f'{{{var}}}' in body or f'{{{var}}}' in subject]
    assert not leftover, f'{type_key}/{lang}: неподставленные переменные {leftover}'
    assert 'None' not in subject, f'{type_key}/{lang}: артефакт None в теме'


# ============ Единый источник дефолтов в email_service ============


@pytest.fixture
def captured_send(monkeypatch):
    """Перехватывает send_email и притворяется, что SMTP настроен."""
    sent: dict = {}

    def fake_send_email(self, to_email, subject, body_html, body_text=None):
        sent.update({'to': to_email, 'subject': subject, 'body': body_html})
        return True

    monkeypatch.setattr(EmailService, 'send_email', fake_send_email)
    return sent


def test_verification_email_uses_unified_template(captured_send):
    service = EmailService()
    ok = service.send_verification_email(
        to_email='user@test.dev',
        verification_token='tok123',
        verification_url='https://vpn.test/verify-email',
        username='Egor',
        language='ru',
    )
    assert ok
    body = captured_send['body']
    assert 'https://vpn.test/verify-email?token=tok123' in body
    assert 'Egor' in body
    _assert_single_document(body, 'verification')

    # Письмо собрано из того же шаблона, что показывает редактор
    from app.config import settings
    from app.services.notification_delivery_service import NotificationType

    expected = EmailNotificationTemplates().get_template(
        NotificationType.EMAIL_VERIFICATION,
        'ru',
        {
            'username': 'Egor',
            'verification_url': 'https://vpn.test/verify-email?token=tok123',
            'expire_hours': settings.get_cabinet_email_verification_expire_hours(),
        },
    )
    assert captured_send['subject'] == expected['subject']
    assert body == expected['body_html']


def test_password_reset_email_uses_unified_template(captured_send):
    service = EmailService()
    ok = service.send_password_reset_email(
        to_email='user@test.dev',
        reset_token='tok456',
        reset_url='https://vpn.test/reset-password',
        username='Egor',
        language='en',
    )
    assert ok
    body = captured_send['body']
    assert 'https://vpn.test/reset-password?token=tok456' in body
    assert captured_send['subject'] == 'Reset your password'
    _assert_single_document(body, 'password_reset')


def test_email_change_code_uses_unified_template(captured_send):
    service = EmailService()
    ok = service.send_email_change_code(
        to_email='user@test.dev',
        code='123456',
        username='Egor',
        language='fa',
    )
    assert ok
    body = captured_send['body']
    assert '123456' in body
    assert captured_send['subject'] == 'کد تایید تغییر ایمیل'
    _assert_single_document(body, 'email_change_code')


def test_custom_override_bypasses_default_rendering(captured_send):
    service = EmailService()
    ok = service.send_verification_email(
        to_email='user@test.dev',
        verification_token='tok',
        verification_url='https://vpn.test/verify-email',
        custom_subject='Custom subject',
        custom_body_html='<html><body>custom</body></html>',
    )
    assert ok
    assert captured_send['subject'] == 'Custom subject'
    assert captured_send['body'] == '<html><body>custom</body></html>'


# ============ Обёртка override-шаблонов ============


def test_wrap_full_document_is_not_double_wrapped():
    templates = EmailNotificationTemplates()
    full_doc = _get_default_template('email_verification', 'ru', SAMPLE_CONTEXTS['email_verification'])['body_html']
    wrapped = templates._wrap_override_template(full_doc, 'ru')
    _assert_single_document(wrapped, 'tier-1 full doc')
    assert wrapped == full_doc.strip()


def test_wrap_fragment_gets_base_template_once():
    templates = EmailNotificationTemplates()
    wrapped = templates._wrap_override_template('<h2>Привет</h2><p>текст</p>', 'ru')
    _assert_single_document(wrapped, 'tier-3 fragment')
    assert '<h2>Привет</h2>' in wrapped
    assert 'class="footer"' in wrapped


def test_wrap_styled_fragment_gets_minimal_wrapper():
    templates = EmailNotificationTemplates()
    content = '<style>p{color:red}</style><p>styled</p>'
    wrapped = templates._wrap_override_template(content, 'ru')
    _assert_single_document(wrapped, 'tier-2 styled')
    assert 'class="footer"' not in wrapped


# ============ Редактор ↔ отправка: roundtrip ============


@pytest.mark.parametrize('type_key', ALL_TYPE_KEYS)
def test_editor_default_roundtrips_through_override_render(type_key):
    """Сохранение дефолта из редактора как override не ломает письмо."""
    from app.cabinet.services.email_template_overrides import substitute_context_vars

    templates = EmailNotificationTemplates()
    editor_payload = _get_default_template(type_key, 'ru', _placeholder_context(type_key))
    sample = SAMPLE_CONTEXTS[type_key]

    body = substitute_context_vars(editor_payload['body_html'], sample)
    rendered = templates._wrap_override_template(body, 'ru')
    subject = substitute_context_vars(editor_payload['subject'], sample, escape=False)

    _assert_single_document(rendered, f'roundtrip {type_key}')
    assert subject.strip()
    leftover = [var for var in sample if f'{{{var}}}' in rendered]
    assert not leftover, f'{type_key}: после подстановки остались переменные {leftover}'
