"""Письма рассылок идут в общей обёртке писем.

Рассылка уходила голым HTML и обходила обёртку, которую админ настроил для
всех остальных писем. Теперь фрагмент HTML встаёт в обёртку (полный документ —
как есть), а превью в кабинете рендерится тем же кодом (email-render).
"""

from types import SimpleNamespace

import pytest

from app.cabinet.services.email_layout import reset_email_layout_cache, set_cached_email_layouts
from app.services.broadcast_service import EmailBroadcastService, _EmailRecipient


@pytest.fixture(autouse=True)
def _clean_layout_cache():
    reset_email_layout_cache()
    yield
    reset_email_layout_cache()


def _recipient(**overrides) -> _EmailRecipient:
    base = dict(email='vasya@example.com', user_name='Вася', user_id=7, language='ru')
    base.update(overrides)
    return _EmailRecipient(**base)


def test_fragment_gets_branded_layout_with_recipient_and_unsubscribe():
    subject, body = EmailBroadcastService.render_email(
        'Привет, {{user_name}}', '<p>Новости для {{user_name}} ({{email}})</p>', _recipient(), 'https://x/unsub?u=7'
    )
    assert subject == 'Привет, Вася'
    assert body.lower().count('<!doctype') == 1
    assert '<p>Новости для Вася (vasya@example.com)</p>' in body
    assert 'https://x/unsub?u=7' in body and 'Отписаться от рассылок' in body


def test_custom_layout_applies_to_broadcasts_too():
    set_cached_email_layouts({'ru': '<!DOCTYPE html><html><body class="mine">{username}: {content}</body></html>'})
    _subject, body = EmailBroadcastService.render_email('s', '<p>x</p>', _recipient())
    assert body.startswith('<!DOCTYPE html><html><body class="mine">Вася: ')
    assert '<p>x</p>' in body


def test_full_document_is_sent_as_is_with_unsubscribe_placeholder():
    html = '<!DOCTYPE html><html><body>mine <a href="{{unsubscribe_url}}">off</a></body></html>'
    _subject, body = EmailBroadcastService.render_email('s', html, _recipient(), 'https://x/unsub')
    assert body == '<!DOCTYPE html><html><body>mine <a href="https://x/unsub">off</a></body></html>'


def test_system_broadcast_has_no_unsubscribe_link():
    _subject, body = EmailBroadcastService.render_email('s', '<p>x</p>', _recipient())
    assert 'Отписаться' not in body


@pytest.mark.asyncio
async def test_email_render_endpoint_matches_delivery():
    from app.cabinet.routes.admin_broadcasts import render_email_broadcast
    from app.cabinet.schemas.broadcasts import EmailRenderRequest

    admin = SimpleNamespace(id=1, email='admin@example.com', username='admin', first_name='Admin')
    result = await render_email_broadcast(
        EmailRenderRequest(subject='Тема {{user_name}}', html_content='<p>Текст {{user_name}}</p>', language='ru'),
        admin=admin,
    )
    assert result.subject == 'Тема admin'
    assert '<p>Текст admin</p>' in result.body_html
    assert result.body_html.lower().count('<!doctype') == 1
