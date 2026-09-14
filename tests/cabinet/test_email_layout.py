"""Общая обёртка писем (layout) — один редактируемый каркас для всех писем.

Жалоба из «Багов»: «нужно, чтобы ВСЁ можно было кастомить». Тексты писем
редактировались, а обёртка (шапка с названием сервиса, цвета, подвал) была
зашита в код — поменять её можно было только вставив полный HTML в каждый из
50 типов на каждом из 5 языков. Теперь обёртка — отдельный шаблон с
плейсхолдером {content}: сохранённая в редакторе применяется ко всем
дефолтным письмам и к фрагментам из редактора; без неё работает встроенная.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_email_templates import (
    EDITOR_TEMPLATE_TYPES,
    SAMPLE_CONTEXTS,
    EmailTemplatePreviewRequest,
    EmailTemplateUpdate,
    _get_default_template,
    _placeholder_context,
    _validate_template_type,
    preview_template,
    update_template,
)
from app.cabinet.services import email_layout, email_template_overrides
from app.cabinet.services.email_layout import (
    DEFAULT_EMAIL_LAYOUT,
    EMAIL_LAYOUT_TYPE,
    get_cached_email_layout,
    render_email_layout,
    reset_email_layout_cache,
    set_cached_email_layouts,
)
from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.services.notification_delivery_service import NotificationType


CUSTOM = (
    '<!DOCTYPE html><html><body><div id="brand">{service_name}</div>'
    '{content}<footer>{footer_text}{unsubscribe_block}</footer></body></html>'
)
OTHER = '<!DOCTYPE html><html><body class="other">{content}</body></html>'


@pytest.fixture(autouse=True)
def _clean_layout_cache():
    reset_email_layout_cache()
    yield
    reset_email_layout_cache()


# ============ Встроенная обёртка ============


def test_default_layout_is_the_previous_wrapper():
    """Без сохранённой обёртки письма выглядят ровно как раньше."""
    body = EmailNotificationTemplates()._get_base_template('<p>hi</p>', 'en')
    assert body.lower().count('<!doctype') == 1
    assert '<p>hi</p>' in body
    assert 'class="container"' in body
    assert 'This is an automated message' in body
    assert 'Unsubscribe' not in body

    with_unsub = EmailNotificationTemplates()._get_base_template('<p>hi</p>', 'en', 'https://x/unsub?a=1&b=2')
    assert 'Unsubscribe from marketing emails' in with_unsub
    assert 'https://x/unsub?a=1&amp;b=2' in with_unsub


def test_default_layout_contains_every_placeholder_the_editor_offers():
    for var in ('content', 'service_name', 'footer_text', 'unsubscribe_block'):
        assert f'{{{var}}}' in DEFAULT_EMAIL_LAYOUT, var


# ============ Сохранённая обёртка применяется ко всем письмам ============


def test_custom_layout_applies_to_default_templates():
    set_cached_email_layouts({'ru': CUSTOM})
    template = EmailNotificationTemplates().get_template(NotificationType.WINBACK_TRIAL_ENDING, 'ru', {})
    body = template['body_html']
    assert body.startswith('<!DOCTYPE html><html><body><div id="brand">')
    assert 'Пробная подписка скоро закончится' in body
    assert 'Это автоматическое сообщение' in body
    assert '{content}' not in body and '{footer_text}' not in body


def test_custom_layout_falls_back_to_ru_for_other_languages():
    set_cached_email_layouts({'ru': CUSTOM})
    en = EmailNotificationTemplates().get_template(NotificationType.WINBACK_TRIAL_ENDING, 'en', {})['body_html']
    assert en.startswith('<!DOCTYPE html><html><body><div id="brand">')
    assert 'This is an automated message' in en

    set_cached_email_layouts({'ru': CUSTOM, 'en': OTHER})
    en = EmailNotificationTemplates().get_template(NotificationType.WINBACK_TRIAL_ENDING, 'en', {})['body_html']
    assert 'class="other"' in en
    ru = EmailNotificationTemplates().get_template(NotificationType.WINBACK_TRIAL_ENDING, 'ru', {})['body_html']
    assert 'id="brand"' in ru


def test_custom_layout_applies_to_editor_fragments_but_not_full_documents():
    set_cached_email_layouts({'ru': CUSTOM})
    templates = EmailNotificationTemplates()
    assert templates._wrap_override_template('<p>x</p>', 'ru').startswith('<!DOCTYPE html><html><body><div id="brand">')
    full = '<!DOCTYPE html><html><body>mine</body></html>'
    assert templates._wrap_override_template(full, 'ru') == full


def test_layout_without_content_slot_is_ignored():
    """Обёртка без {content} проглотила бы тело каждого письма — её не берём."""
    set_cached_email_layouts({'ru': '<html><body>no slot</body></html>'})
    assert get_cached_email_layout('ru') is None
    body = EmailNotificationTemplates()._get_base_template('<p>hi</p>', 'ru')
    assert 'class="container"' in body and '<p>hi</p>' in body


def test_render_keeps_content_raw_and_escapes_text_values():
    rendered = render_email_layout(
        '<a>{service_name}</a>{content}{unsubscribe_block}',
        'ru',
        {'content': '<b>x</b>', 'service_name': 'A & B', 'unsubscribe_block': '<i>u</i>'},
    )
    assert rendered.startswith('<a>A &amp; B</a>')
    assert '<b>x</b>' in rendered and '&lt;b&gt;' not in rendered
    assert rendered.endswith('<i>u</i>')
    # Тело обрамлено маркерами — по ним редактор достаёт фрагмент из готового письма.
    assert email_layout.extract_content_fragment(rendered) == '<b>x</b>'


def test_layout_can_use_bare_unsubscribe_url_for_its_own_link_text():
    """Свой текст отписки вместо готового блока: {unsubscribe_url} доступен в обёртке."""
    set_cached_email_layouts({'ru': '<html><body>{content}<a href="{unsubscribe_url}">Хватит писем</a></body></html>'})
    templates = EmailNotificationTemplates()
    marketing = templates._get_base_template('<p>x</p>', 'ru', 'https://x/unsub?a=1&b=2')
    assert 'href="https://x/unsub?a=1&amp;b=2">Хватит писем' in marketing
    transactional = templates._get_base_template('<p>x</p>', 'ru')
    assert 'href="">Хватит писем' in transactional


def test_layout_sees_recipient_vars_from_default_and_override_paths():
    """{username}/{email} доступны в обёртке — рендер письма отдаёт ей получателя."""
    set_cached_email_layouts({'ru': '<html><body><p>Привет, {username} ({email})</p>{content}</body></html>'})
    templates = EmailNotificationTemplates()
    default = templates.get_template(
        NotificationType.SUBSCRIPTION_EXPIRED, 'ru', {'username': 'Вася', 'email': 'v@x.ru'}
    )['body_html']
    assert 'Привет, Вася (v@x.ru)' in default
    fragment = templates._wrap_override_template('<p>x</p>', 'ru', context={'username': 'Петя', 'email': 'p@x.ru'})
    assert 'Привет, Петя (p@x.ru)' in fragment
    # Без получателя плейсхолдеры не утекают литералом.
    assert '{username}' not in templates._get_base_template('<p>x</p>', 'ru')


def test_override_fragment_keeps_unsubscribe_link():
    """Маркетинговый override третьего уровня раньше терял ссылку отписки в подвале."""
    body = EmailNotificationTemplates()._wrap_override_template(
        '<p>promo</p>', 'ru', unsubscribe_url='https://x/unsub?u=1'
    )
    assert 'https://x/unsub?u=1' in body and 'Отписаться от рассылок' in body


# ============ Кэш обёртки ============


def test_cache_is_stale_until_loaded_and_fresh_after(monkeypatch):
    assert email_layout.is_layout_cache_stale()
    set_cached_email_layouts({})
    assert not email_layout.is_layout_cache_stale()
    real_monotonic = email_layout._monotonic
    monkeypatch.setattr(
        email_layout, '_monotonic', lambda: real_monotonic() + email_layout.LAYOUT_CACHE_TTL_SECONDS + 1
    )
    assert email_layout.is_layout_cache_stale()


@pytest.mark.asyncio
async def test_rendered_override_refreshes_layout_cache_first(monkeypatch):
    """Каждая отправка проходит через get_rendered_override — там и подтягивается обёртка."""
    calls = []

    async def fake_refresh(db=None, *, force=False):
        calls.append(force)

    async def no_override(*a, **k):
        return None

    monkeypatch.setattr(email_template_overrides, 'refresh_email_layout_cache', fake_refresh)
    monkeypatch.setattr(email_template_overrides, 'get_template_override', no_override)
    assert await email_template_overrides.get_rendered_override('subscription_expired', 'ru', {}) is None
    assert calls == [False]


@pytest.mark.asyncio
async def test_refresh_survives_database_errors(monkeypatch):
    """Недоступная база — это «обёртки нет», а не потерянное письмо."""

    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError('db down')

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(email_layout, 'AsyncSessionLocal', lambda: BrokenSession())
    await email_layout.refresh_email_layout_cache(force=True)
    assert get_cached_email_layout('ru') is None
    assert not email_layout.is_layout_cache_stale(), 'после ошибки не долбить базу на каждом письме'


# ============ Редактор ============


def test_editor_lists_layout_first_with_placeholders_intact():
    assert EDITOR_TEMPLATE_TYPES[0]['type'] == EMAIL_LAYOUT_TYPE
    meta = _validate_template_type(EMAIL_LAYOUT_TYPE)
    assert 'content' in meta['context_vars']
    default = _get_default_template(EMAIL_LAYOUT_TYPE, 'ru', _placeholder_context(EMAIL_LAYOUT_TYPE))
    body = default['body_html']
    for var in ('content', 'service_name', 'footer_text', 'unsubscribe_block'):
        assert f'{{{var}}}' in body, var
    assert 'class="container"' in body
    assert SAMPLE_CONTEXTS[EMAIL_LAYOUT_TYPE]['content'].startswith('<')


@pytest.mark.asyncio
async def test_layout_preview_puts_sample_letter_inside_custom_layout():
    data = EmailTemplatePreviewRequest(language='ru', subject='', body_html=CUSTOM)
    result = await preview_template(EMAIL_LAYOUT_TYPE, data, _admin=None)
    body = result['body_html']
    assert body.startswith('<!DOCTYPE html><html><body><div id="brand">')
    assert SAMPLE_CONTEXTS[EMAIL_LAYOUT_TYPE]['content'] in body
    assert '{content}' not in body and '{footer_text}' not in body
    assert 'Это автоматическое сообщение' in body

    default_preview = await preview_template(
        EMAIL_LAYOUT_TYPE, EmailTemplatePreviewRequest(language='en', subject='', body_html=''), _admin=None
    )
    assert 'class="container"' in default_preview['body_html']
    assert SAMPLE_CONTEXTS[EMAIL_LAYOUT_TYPE]['content'] in default_preview['body_html']


@pytest.mark.asyncio
async def test_saving_layout_without_content_slot_is_rejected():
    data = EmailTemplateUpdate(subject='x', body_html='<html><body>no slot</body></html>')
    with pytest.raises(HTTPException) as exc:
        await update_template(EMAIL_LAYOUT_TYPE, 'ru', data, admin=SimpleNamespace(id=1), db=None)
    assert exc.value.status_code == 400
    assert '{content}' in exc.value.detail


@pytest.mark.asyncio
async def test_saving_layout_refreshes_cache_and_uses_fixed_subject(monkeypatch):
    saved = {}

    async def fake_save(**kwargs):
        saved.update(kwargs)
        return {**kwargs, 'is_active': True}

    refreshed = []

    async def fake_refresh(db=None, *, force=False):
        refreshed.append(force)

    from app.cabinet.routes import admin_email_templates as routes

    monkeypatch.setattr(routes, 'save_template_override', fake_save)
    monkeypatch.setattr(routes, 'refresh_email_layout_cache', fake_refresh)
    data = EmailTemplateUpdate(subject='ignored', body_html=CUSTOM)
    await update_template(EMAIL_LAYOUT_TYPE, 'ru', data, admin=SimpleNamespace(id=1), db=None)
    assert saved['notification_type'] == EMAIL_LAYOUT_TYPE
    assert saved['subject'] == EMAIL_LAYOUT_TYPE
    assert saved['body_html'] == CUSTOM
    assert refreshed == [True]
