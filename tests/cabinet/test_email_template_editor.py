"""
Тесты редактора email-шаблонов.

Баг-репорт: https://t.me/c/2941121338/6/667043 — после сохранения шаблона
в редакторе боевые письма уходили со ссылкой на example.com, потому что
редактор отдавал дефолтные шаблоны уже отрендеренными sample-значениями.
"""

import html
import re
from urllib.parse import urlsplit

import pytest

from app.cabinet.routes.admin_email_templates import (
    SAMPLE_CONTEXTS,
    TEMPLATE_TYPES,
    EmailTemplatePreviewRequest,
    _get_default_template,
    _placeholder_context,
    preview_template,
)
from app.cabinet.services.email_template_overrides import (
    COMMON_CONTEXT_VARS,
    build_common_context,
    get_rendered_override,
    substitute_context_vars,
)
from app.cabinet.services.email_templates import EmailNotificationTemplates


ALL_TYPE_KEYS = [t['type'] for t in TEMPLATE_TYPES]


# ============ Полнота списка редактора ============


def test_editor_exposes_every_email_template_type():
    """Жалоба из «Багов»: письма уходят, а поменять их шаблон нельзя.

    «Пробная подписка скоро закончится» (winback_trial_ending), два других
    winback-письма, промо-предложение и 14 уведомлений по вебхукам Remnawave
    рендерились и отправлялись, но в списке редактора их не было — админ видел
    только стандартный шаблон. Список редактора обязан совпадать с реестром
    шаблонов: второй рукописный реестр рядом с первым неизбежно отстаёт.
    """
    renderable = {t.value for t in EmailNotificationTemplates().supported_types()}
    exposed = set(ALL_TYPE_KEYS)
    assert renderable - exposed == set(), f'есть email-шаблон, но в редакторе нет: {sorted(renderable - exposed)}'
    assert exposed - renderable == set(), f'в редакторе есть, но email-шаблона нет: {sorted(exposed - renderable)}'


# ============ Выдача шаблонов в редактор ============


def test_every_template_type_has_sample_context_for_all_vars():
    for tpl in TEMPLATE_TYPES:
        sample = SAMPLE_CONTEXTS.get(tpl['type'])
        assert sample is not None, f'Нет SAMPLE_CONTEXTS для {tpl["type"]}'
        missing = [v for v in tpl['context_vars'] if v not in sample]
        assert not missing, f'{tpl["type"]}: в sample-контексте нет {missing}'


@pytest.mark.parametrize('type_key', ALL_TYPE_KEYS)
def test_default_template_renders_for_editor(type_key):
    """Каждый тип из списка редактора должен иметь рабочий дефолтный шаблон."""
    template = _get_default_template(type_key, 'ru', _placeholder_context(type_key))
    assert template is not None, f'Нет дефолтного шаблона для {type_key}'
    assert template['subject']
    assert template['body_html']


# Sample-значения, утечка которых в редактор и означает баг
SAMPLE_LEAK_MARKERS = ['verify?token=abc123', 'reset?token=abc123', 'SecurePass123', 'buy/success/abc123']

# Критичные плейсхолдеры: без них письмо бесполезно, редактор обязан их отдать
CRITICAL_PLACEHOLDERS = {
    'email_verification': '{verification_url}',
    'password_reset': '{reset_url}',
    'email_change_code': '{code}',
    'guest_cabinet_credentials': '{cabinet_password}',
}


@pytest.mark.parametrize('type_key', ALL_TYPE_KEYS)
def test_editor_payload_keeps_placeholders_not_sample_values(type_key):
    """Редактор получает {placeholder}-токены, а не подставленные примеры.

    Если бы отдавались примеры, админ сохранял бы шаблон с зашитой ссылкой
    https://example.com/verify?token=abc123 — и боевые письма вели бы в никуда.
    """
    template = _get_default_template(type_key, 'ru', _placeholder_context(type_key))
    payload = template['subject'] + template['body_html']
    for marker in SAMPLE_LEAK_MARKERS:
        assert marker not in payload, f'{type_key}: sample-значение «{marker}» утекло в редактор'

    critical = CRITICAL_PLACEHOLDERS.get(type_key)
    if critical:
        assert critical in template['body_html'], (
            f'{type_key}: критичный плейсхолдер {critical} потерян при рендере для редактора'
        )


def test_verification_template_placeholder_survives_roundtrip():
    """Сценарий бага: сохранить дефолтный шаблон как override и отправить письмо."""
    template = _get_default_template('email_verification', 'ru', _placeholder_context('email_verification'))
    real_url = 'https://vpn.example.org/cabinet/verify-email?token=deadbeef'
    rendered = substitute_context_vars(
        template['body_html'],
        {'username': 'Egor', 'verification_url': real_url, 'expire_hours': '24'},
    )
    assert html.escape(real_url) in rendered
    assert '{verification_url}' not in rendered
    assert 'example.com' not in rendered


# ============ substitute_context_vars ============


def test_substitute_escapes_html_in_body():
    out = substitute_context_vars('<p>{username}</p>', {'username': '<script>x</script>'})
    assert out == '<p>&lt;script&gt;x&lt;/script&gt;</p>'


def test_substitute_subject_strips_newlines_without_escaping():
    out = substitute_context_vars('Hi {username}', {'username': 'A&B\r\nC'}, escape=False)
    assert out == 'Hi A&BC'


def test_substitute_none_value_becomes_empty():
    assert substitute_context_vars('x{gift_message}y', {'gift_message': None}) == 'xy'


# ============ Страховка required_vars ============


@pytest.mark.asyncio
async def test_override_without_required_var_falls_back_to_default(monkeypatch):
    """Сломанный override (без {verification_url}) отбрасывается → дефолт."""

    async def fake_override(*_args, **_kwargs):
        return {
            'subject': 'Подтвердите почту',
            'body_html': '<p>Перейдите по ссылке: https://example.com/verify?token=abc123</p>',
        }

    monkeypatch.setattr(
        'app.cabinet.services.email_template_overrides.get_template_override',
        fake_override,
    )

    rendered = await get_rendered_override(
        'email_verification',
        'ru',
        context={'verification_url': 'https://real.host/verify-email?token=xyz'},
        required_vars=['verification_url'],
    )
    assert rendered is None


@pytest.mark.asyncio
async def test_override_with_required_var_is_used(monkeypatch):
    async def fake_override(*_args, **_kwargs):
        return {
            'subject': 'Подтвердите почту',
            'body_html': '<p><a href="{verification_url}">Подтвердить</a></p>',
        }

    monkeypatch.setattr(
        'app.cabinet.services.email_template_overrides.get_template_override',
        fake_override,
    )

    url = 'https://real.host/verify-email?token=xyz'
    rendered = await get_rendered_override(
        'email_verification',
        'ru',
        context={'verification_url': url},
        required_vars=['verification_url'],
    )
    assert rendered is not None
    subject, body = rendered
    assert subject == 'Подтвердите почту'
    assert html.escape(url) in body


@pytest.mark.asyncio
async def test_required_var_with_empty_value_does_not_reject_override(monkeypatch):
    """Пустое значение переменной не должно отбрасывать override."""

    async def fake_override(*_args, **_kwargs):
        return {'subject': 'S', 'body_html': '<p>Без пароля</p>'}

    monkeypatch.setattr(
        'app.cabinet.services.email_template_overrides.get_template_override',
        fake_override,
    )

    rendered = await get_rendered_override(
        'guest_cabinet_credentials',
        'ru',
        context={'cabinet_password': None, 'cabinet_email': ''},
        required_vars=['cabinet_email', 'cabinet_password'],
    )
    assert rendered is not None


# ============ Превью ============


@pytest.mark.asyncio
async def test_preview_substitutes_sample_values_into_custom_body():
    data = EmailTemplatePreviewRequest(
        language='ru',
        subject='Привет, {username}',
        body_html='<p>Ссылка: <a href="{verification_url}">тут</a>, истекает через {expire_hours} ч.</p>',
    )
    result = await preview_template('email_verification', data, _admin=None)
    sample_url = SAMPLE_CONTEXTS['email_verification']['verification_url']
    assert html.escape(sample_url) in result['body_html']
    assert '{verification_url}' not in result['body_html']
    assert result['subject'] == 'Привет, John'


@pytest.mark.asyncio
async def test_preview_substitutes_common_vars_into_custom_body():
    """Общие переменные ({cabinet_url}, {service_name}) работают в любом шаблоне."""
    data = EmailTemplatePreviewRequest(
        language='ru',
        subject='От {service_name}',
        body_html='<p>Кабинет: <a href="{cabinet_url}">{cabinet_url}</a>, команда {service_name}</p>',
    )
    result = await preview_template('subscription_expired', data, _admin=None)
    common = build_common_context()
    assert '{cabinet_url}' not in result['body_html']
    assert '{service_name}' not in result['body_html']
    assert common['service_name'] in result['body_html']
    assert result['subject'] == f'От {common["service_name"]}'


@pytest.mark.asyncio
async def test_override_render_injects_common_vars(monkeypatch):
    """Боевой рендер override подставляет общие переменные без участия вызывающего кода."""

    async def fake_override(*_args, **_kwargs):
        return {'subject': '{service_name}', 'body_html': '<p>{cabinet_url} / {service_name}</p>'}

    monkeypatch.setattr(
        'app.cabinet.services.email_template_overrides.get_template_override',
        fake_override,
    )

    rendered = await get_rendered_override('subscription_expired', 'ru', context={})
    assert rendered is not None
    subject, body = rendered
    common = build_common_context()
    assert subject == common['service_name']
    assert '{cabinet_url}' not in body
    assert '{service_name}' not in body


def test_common_vars_exposed_to_editor():
    """Редактор получает список общих переменных для всех типов."""
    assert COMMON_CONTEXT_VARS == [
        'service_name',
        'cabinet_url',
        'support_username',
        'username',
        'email',
        'date',
        'unsubscribe_url',
    ]
    common = build_common_context()
    assert set(common) == set(COMMON_CONTEXT_VARS)
    # Инстанс-уровень заполнен сразу; получательские — пустые дефолты,
    # их обязан передать отправляющий код
    assert common['service_name']
    assert common['date']
    assert common['username'] == ''
    assert common['email'] == ''
    # Ссылку отписки подставляет отправляющий код и только маркетинговым письмам.
    assert common['unsubscribe_url'] == ''


@pytest.mark.asyncio
async def test_recipient_common_vars_never_leak_as_literals(monkeypatch):
    """Даже если отправитель не передал username/email — литерал {username} не уходит в письмо."""

    async def fake_override(*_args, **_kwargs):
        return {'subject': 'S', 'body_html': '<p>Привет, {username}! Письмо для {email}, дата {date}</p>'}

    monkeypatch.setattr(
        'app.cabinet.services.email_template_overrides.get_template_override',
        fake_override,
    )

    rendered = await get_rendered_override('subscription_expired', 'ru', context={})
    assert rendered is not None
    _, body = rendered
    assert '{username}' not in body
    assert '{email}' not in body
    assert '{date}' not in body


@pytest.mark.asyncio
async def test_preview_default_template_uses_sample_values():
    data = EmailTemplatePreviewRequest(language='ru')
    result = await preview_template('email_verification', data, _admin=None)
    assert '{verification_url}' not in result['body_html']
    link = re.search(r'https?://[^\s"<>]+', result['body_html'])
    assert link is not None, 'в превью нет ссылки подтверждения'
    assert urlsplit(link.group(0)).hostname == 'example.com'


# ============ Обязательные плейсхолдеры проверяются при сохранении ============


@pytest.mark.asyncio
async def test_saving_template_without_required_placeholder_is_rejected_with_clear_error():
    """Раньше такой override молча подменялся стандартным письмом при отправке —
    админ видел «мой шаблон не работает». Теперь редактор отказывает сразу."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.cabinet.routes.admin_email_templates import EmailTemplateUpdate, update_template

    data = EmailTemplateUpdate(subject='Подтвердите почту', body_html='<p>Без ссылки</p>')
    with pytest.raises(HTTPException) as exc:
        await update_template('email_verification', 'ru', data, admin=SimpleNamespace(id=1), db=None)
    assert exc.value.status_code == 400
    assert '{verification_url}' in exc.value.detail


@pytest.mark.asyncio
async def test_saving_template_with_required_placeholder_passes(monkeypatch):
    from types import SimpleNamespace

    from app.cabinet.routes import admin_email_templates as routes

    saved = {}

    async def fake_save(**kwargs):
        saved.update(kwargs)
        return {**kwargs, 'is_active': True}

    monkeypatch.setattr(routes, 'save_template_override', fake_save)
    data = routes.EmailTemplateUpdate(
        subject='Подтвердите почту', body_html='<a href="{verification_url}">Подтвердить</a>'
    )
    await routes.update_template('email_verification', 'ru', data, admin=SimpleNamespace(id=1), db=None)
    assert saved['body_html'] == data.body_html


# ============ Старые имена плейсхолдеров ============


def test_legacy_placeholder_names_resolve_in_sample_context():
    """7 марта редактор переименовал переменные; шаблоны на старых именах должны жить."""
    from app.cabinet.routes.admin_email_templates import _build_sample_context

    renewed = _build_sample_context('subscription_renewed')
    assert renewed['new_end_date'] == renewed['new_expires_at']
    activated = _build_sample_context('subscription_activated')
    assert activated['end_date'] == activated['expires_at']
    topup = _build_sample_context('balance_topup')
    assert topup['amount'] == topup['formatted_amount'] and topup['balance'] == topup['formatted_balance']
    low = _build_sample_context('autopay_insufficient_funds')
    assert low['formatted_required'] == low['required_amount'] and low['balance'] == low['current_balance']


@pytest.mark.asyncio
async def test_override_with_legacy_placeholder_renders_value(monkeypatch):
    """Шаблон пользователя с {new_end_date} — в письме дата, а не литерал."""
    from app.cabinet.services import email_template_overrides as overrides

    async def fake_override(*a, **k):
        return {
            'subject': 'Продлено до {new_end_date}',
            'body_html': '<p>Активна до {new_end_date}, тариф {tariff_name}</p>',
        }

    monkeypatch.setattr(overrides, 'get_template_override', fake_override)
    subject, body = await overrides.get_rendered_override(
        'subscription_renewed', 'ru', {'new_expires_at': '28.02.2026', 'tariff_name': 'Premium'}
    )
    assert subject == 'Продлено до 28.02.2026'
    assert 'Активна до 28.02.2026, тариф Premium' in body
    assert '{new_end_date}' not in body


@pytest.mark.asyncio
async def test_saving_template_with_unknown_placeholder_is_rejected():
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.cabinet.routes.admin_email_templates import EmailTemplateUpdate, update_template

    data = EmailTemplateUpdate(subject='x', body_html='<p>До {new_end_dat}</p>')
    with pytest.raises(HTTPException) as exc:
        await update_template('subscription_renewed', 'ru', data, admin=SimpleNamespace(id=1), db=None)
    assert exc.value.status_code == 400 and '{new_end_dat}' in exc.value.detail


@pytest.mark.asyncio
async def test_saving_template_with_legacy_placeholder_is_allowed(monkeypatch):
    from types import SimpleNamespace

    from app.cabinet.routes import admin_email_templates as routes

    saved = {}

    async def fake_save(**kwargs):
        saved.update(kwargs)
        return {**kwargs, 'is_active': True}

    monkeypatch.setattr(routes, 'save_template_override', fake_save)
    data = routes.EmailTemplateUpdate(subject='x', body_html='<p>До {new_end_date}, {service_name}</p>')
    await routes.update_template('subscription_renewed', 'ru', data, admin=SimpleNamespace(id=1), db=None)
    assert saved['body_html'] == data.body_html
