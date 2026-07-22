"""Email-шаблоны для WEBHOOK_* уведомлений (email-only юзеры).

Все WEBHOOK_* типы ходят через notification_delivery_service, который для
email-only юзеров шлёт письмо, — но ни один тип не был зареган в
email_templates.template_map, и почта молча пропускалась. Эти тесты
гарантируют, что каждый WEBHOOK_* тип enum'а имеет шаблон на всех языках
письма (ru/en/zh/ua) и что подстановка устройства не оставляет сырых
плейсхолдеров.
"""

from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.services.notification_delivery_service import NotificationType


WEBHOOK_TYPES = [t for t in NotificationType if t.value.startswith('webhook_')]
EMAIL_LANGUAGES = ('ru', 'en', 'zh', 'ua')


def test_every_webhook_type_has_email_template_in_every_language():
    """Новый WEBHOOK_* тип без email-шаблона — регресс к «почта молча пропущена»."""
    templates = EmailNotificationTemplates()
    missing = []
    for notification_type in WEBHOOK_TYPES:
        for language in EMAIL_LANGUAGES:
            template = templates.get_template(notification_type, language, {})
            if not template or not template.get('subject') or not template.get('body_html'):
                missing.append(f'{notification_type.value}/{language}')
    assert not missing, 'WEBHOOK_* типы без email-шаблона: ' + ', '.join(missing)


def test_webhook_email_language_fallback_to_ru():
    templates = EmailNotificationTemplates()
    fallback = templates.get_template(NotificationType.WEBHOOK_SUB_EXPIRED, 'fa', {})
    ru = templates.get_template(NotificationType.WEBHOOK_SUB_EXPIRED, 'ru', {})
    assert fallback['subject'] == ru['subject']


def test_webhook_email_localized_subjects_differ_from_ru():
    """zh/ua — не заглушки: тема отличается от русской."""
    templates = EmailNotificationTemplates()
    ru = templates.get_template(NotificationType.WEBHOOK_SUB_EXPIRED, 'ru', {})
    for language in ('en', 'zh', 'ua'):
        localized = templates.get_template(NotificationType.WEBHOOK_SUB_EXPIRED, language, {})
        assert localized['subject'] != ru['subject'], language


def test_device_name_substitution_and_placeholder_hygiene():
    templates = EmailNotificationTemplates()

    with_device = templates.get_template(NotificationType.WEBHOOK_DEVICE_ADDED, 'ru', {'device': 'iPhone 15'})
    assert 'iPhone 15' in with_device['body_html']

    # Вебхук-хендлеры шлют '—' как «имя неизвестно» — суффикс не подставляется
    dash = templates.get_template(NotificationType.WEBHOOK_DEVICE_ADDED, 'ru', {'device': '—'})
    assert '— —' not in dash['body_html']

    # Ни в одном типе/языке не остаётся сырых {device}-плейсхолдеров
    for notification_type in WEBHOOK_TYPES:
        for language in EMAIL_LANGUAGES:
            template = templates.get_template(notification_type, language, {})
            assert '{device}' not in template['body_html'], f'{notification_type.value}/{language}'


def test_device_name_is_html_escaped():
    templates = EmailNotificationTemplates()
    template = templates.get_template(
        NotificationType.WEBHOOK_DEVICE_ADDED, 'ru', {'device': '<script>alert(1)</script>'}
    )
    assert '<script>' not in template['body_html']
    assert '&lt;script&gt;' in template['body_html']


# --- Winback / lifecycle email-шаблоны (#3079) ---

WINBACK_TYPES = [
    NotificationType.WINBACK_EXPIRED_1D,
    NotificationType.WINBACK_DISCOUNT,
    NotificationType.WINBACK_TRIAL_ENDING,
]


def test_winback_types_have_email_template_in_every_language():
    templates = EmailNotificationTemplates()
    missing = []
    for notification_type in WINBACK_TYPES:
        for language in EMAIL_LANGUAGES:
            template = templates.get_template(notification_type, language, {'percent': 20})
            if not template or not template.get('subject') or not template.get('body_html'):
                missing.append(f'{notification_type.value}/{language}')
    assert not missing, 'WINBACK типы без email-шаблона: ' + ', '.join(missing)


def test_winback_discount_renders_percent_everywhere():
    templates = EmailNotificationTemplates()
    for language in EMAIL_LANGUAGES:
        template = templates.get_template(NotificationType.WINBACK_DISCOUNT, language, {'percent': 25})
        assert '25%' in template['subject'], language
        assert '25%' in template['body_html'], language


def test_winback_expired_1d_escapes_end_date():
    templates = EmailNotificationTemplates()
    template = templates.get_template(NotificationType.WINBACK_EXPIRED_1D, 'ru', {'end_date': '<b>x</b>'})
    assert '<b>x</b>' not in template['body_html']
