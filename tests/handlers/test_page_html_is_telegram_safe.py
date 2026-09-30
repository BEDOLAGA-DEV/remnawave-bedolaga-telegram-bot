"""Тексты страниц уходят в Telegram только в разметке, которую он понимает.

Правила, политика, оферта и FAQ редактируются как произвольный HTML, а Telegram
знает восемь тегов. Один `<p>` из вставленной вёрстки — и отправка падает с
«Bad Request: can't parse entities: Unsupported start tag "p"», то есть раздел
перестаёт открываться целиком: ошибка не в одной строке, а на весь экран.

Проверяется исходящий текст, а не промежуточная функция: падало именно то, что
уходит в edit_text, и любая новая страница, собранная мимо преобразователя,
здесь покраснеет.
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import menu
from app.utils.telegram_html import TELEGRAM_ALLOWED_TAGS


# Вёрстка из редактора: абзацы, заголовок, список и картинка.
RICH_HTML = (
    '<h2>Как оплатить</h2>'
    '<p>Откройте раздел <strong>Баланс</strong> и выберите способ.</p>'
    '<ul><li>Карта</li><li>СБП</li></ul>'
    '<img src="https://example.com/a.png">'
    '<p>Готово.</p>'
)

_TAG_RE = re.compile(r'<\s*/?\s*([a-zA-Z][\w-]*)')


def _disallowed_tags(text: str) -> set[str]:
    return {match.group(1).lower() for match in _TAG_RE.finditer(text)} - set(TELEGRAM_ALLOWED_TAGS)


class _Callback:
    """Минимальный callback: интересует только текст, ушедший в edit_text."""

    def __init__(self, data: str = ''):
        self.data = data
        self.sent: list[str] = []
        self.answer = AsyncMock()
        self.message = SimpleNamespace(edit_text=self._edit_text)

    async def _edit_text(self, text, **_kwargs):
        self.sent.append(text)
        return SimpleNamespace()


def _user(language: str = 'ru'):
    return SimpleNamespace(id=1, telegram_id=1, language=language)


@pytest.fixture
def visible(monkeypatch):
    """Все инфо-разделы показываются в боте."""
    from app.config import settings

    for key in (
        'FAQ_DISPLAY_MODE',
        'PRIVACY_POLICY_DISPLAY_MODE',
        'PUBLIC_OFFER_DISPLAY_MODE',
        'SERVICE_RULES_DISPLAY_MODE',
    ):
        monkeypatch.setattr(settings, key, 'both')
    return settings


@pytest.mark.asyncio
async def test_faq_page_drops_unsupported_markup(monkeypatch, visible):
    page = SimpleNamespace(id=7, title='Оплата', content=RICH_HTML, is_active=True)
    monkeypatch.setattr(menu.FaqService, 'get_page', AsyncMock(return_value=page))

    callback = _Callback('menu_faq_page:7:1')
    await menu.show_faq_page(callback, _user(), db=AsyncMock())

    assert callback.sent, 'страница не отправлена'
    assert _disallowed_tags(callback.sent[0]) == set()
    assert 'Как оплатить' in callback.sent[0]
    assert '• Карта' in callback.sent[0]


@pytest.mark.asyncio
async def test_privacy_policy_drops_unsupported_markup(monkeypatch, visible):
    policy = SimpleNamespace(content=RICH_HTML)
    monkeypatch.setattr(menu.PrivacyPolicyService, 'get_active_policy', AsyncMock(return_value=policy))

    callback = _Callback('menu_privacy_policy')
    await menu.show_privacy_policy(callback, _user(), db=AsyncMock())

    assert callback.sent
    assert _disallowed_tags(callback.sent[0]) == set()


@pytest.mark.asyncio
async def test_public_offer_drops_unsupported_markup(monkeypatch, visible):
    offer = SimpleNamespace(content=RICH_HTML)
    monkeypatch.setattr(menu.PublicOfferService, 'get_active_offer', AsyncMock(return_value=offer))

    callback = _Callback('menu_public_offer')
    await menu.show_public_offer(callback, _user(), db=AsyncMock())

    assert callback.sent
    assert _disallowed_tags(callback.sent[0]) == set()


@pytest.mark.asyncio
async def test_service_rules_drop_unsupported_markup(monkeypatch, visible):
    import app.database.crud.rules as rules_crud

    monkeypatch.setattr(rules_crud, 'get_current_rules_content', AsyncMock(return_value=RICH_HTML))

    callback = _Callback('menu_rules')
    await menu.show_service_rules(callback, _user(), db=AsyncMock())

    assert callback.sent
    assert _disallowed_tags(callback.sent[0]) == set()


@pytest.mark.asyncio
async def test_allowed_formatting_survives(monkeypatch, visible):
    """Преобразование не должно съедать разметку, ради которой её и писали."""
    page = SimpleNamespace(
        id=7,
        title='Оплата',
        content='<p><b>Жирный</b> и <a href="https://example.com">ссылка</a></p>',
        is_active=True,
    )
    monkeypatch.setattr(menu.FaqService, 'get_page', AsyncMock(return_value=page))

    callback = _Callback('menu_faq_page:7:1')
    await menu.show_faq_page(callback, _user(), db=AsyncMock())

    assert '<b>Жирный</b>' in callback.sent[0]
    assert '<a href="https://example.com">ссылка</a>' in callback.sent[0]


@pytest.mark.asyncio
async def test_long_page_is_split_without_breaking_a_tag(monkeypatch, visible):
    """Нарезка идёт по преобразованному тексту, иначе тег рвётся посередине."""
    body = '<p><b>' + ('слово ' * 900) + '</b></p>'
    page = SimpleNamespace(id=7, title='Длинная', content=body, is_active=True)
    monkeypatch.setattr(menu.FaqService, 'get_page', AsyncMock(return_value=page))

    callback = _Callback('menu_faq_page:7:1')
    await menu.show_faq_page(callback, _user(), db=AsyncMock())

    sent = callback.sent[0]
    assert _disallowed_tags(sent) == set()
    assert sent.count('<b>') == sent.count('</b>')
    assert len(sent) <= 4096


@pytest.mark.asyncio
async def test_admin_privacy_preview_drops_unsupported_markup(monkeypatch):
    """Экран «Текущий текст политики» показывает то же, что увидит пользователь."""
    import inspect

    from app.handlers.admin import privacy_policy as admin_privacy

    policy = SimpleNamespace(content=RICH_HTML)
    monkeypatch.setattr(admin_privacy.PrivacyPolicyService, 'get_policy', AsyncMock(return_value=policy))

    callback = _Callback('admin_privacy_policy_view')
    # Мимо @admin_required: он проверяет тип реального CallbackQuery, а проверяется
    # не право доступа, а разметка отправленного текста.
    handler = inspect.unwrap(admin_privacy.view_privacy_policy)
    await handler(callback, db_user=_user(), db=AsyncMock())

    assert callback.sent
    assert _disallowed_tags(callback.sent[0]) == set()


@pytest.mark.asyncio
async def test_admin_offer_preview_drops_unsupported_markup(monkeypatch):
    import inspect

    from app.handlers.admin import public_offer as admin_offer

    offer = SimpleNamespace(content=RICH_HTML)
    monkeypatch.setattr(admin_offer.PublicOfferService, 'get_offer', AsyncMock(return_value=offer))

    callback = _Callback('admin_public_offer_view')
    handler = inspect.unwrap(admin_offer.view_public_offer)
    await handler(callback, db_user=_user(), db=AsyncMock())

    assert callback.sent
    assert _disallowed_tags(callback.sent[0]) == set()


# ------------------------------------------------------------------ регистрация
# При регистрации правила и политика показываются из start.py, мимо меню. Тот же
# <h1> из редактора ронял шаг «политика после правил»: «Unsupported start tag "h1"».


class _RegistrationCallback:
    def __init__(self):
        self.sent: list[str] = []
        self.from_user = SimpleNamespace(id=1)
        self.message = SimpleNamespace(edit_text=self._send, answer=self._send)

    async def _send(self, text, **_kwargs):
        self.sent.append(text)
        return SimpleNamespace()


@pytest.mark.asyncio
async def test_registration_privacy_policy_drops_unsupported_markup(monkeypatch):
    from app.handlers import start

    policy = SimpleNamespace(is_enabled=True, content='<h1>Политика</h1>' + RICH_HTML)
    monkeypatch.setattr(start.PrivacyPolicyService, 'get_policy', AsyncMock(return_value=policy))
    callback = _RegistrationCallback()
    state = AsyncMock()

    shown = await start._show_privacy_policy_after_rules(callback, state, AsyncMock(), 'ru')

    assert shown is True
    assert callback.sent, 'политика не отправлена'
    assert not _disallowed_tags(''.join(callback.sent)), callback.sent
    assert '<b>Политика</b>' in callback.sent[0]
    state.set_state.assert_awaited()


@pytest.mark.asyncio
async def test_registration_privacy_policy_of_bare_tags_falls_back_to_default(monkeypatch):
    from app.handlers import start

    policy = SimpleNamespace(is_enabled=True, content='<p> </p><h1></h1>')
    monkeypatch.setattr(start.PrivacyPolicyService, 'get_policy', AsyncMock(return_value=policy))
    monkeypatch.setattr(start, 'get_privacy_policy', lambda _language: '<p>Политика <b>по умолчанию</b></p>')
    callback = _RegistrationCallback()

    shown = await start._show_privacy_policy_after_rules(callback, AsyncMock(), AsyncMock(), 'ru')

    assert shown is True
    assert callback.sent == ['Политика <b>по умолчанию</b>']


@pytest.mark.asyncio
async def test_registration_rules_drop_unsupported_markup(monkeypatch):
    from app.handlers import start

    monkeypatch.setattr(start, 'get_rules', AsyncMock(return_value='<h1>Правила</h1>' + RICH_HTML))

    text = await start._rules_for_telegram('ru')

    assert not _disallowed_tags(text), text
    assert '<b>Правила</b>' in text


@pytest.mark.asyncio
async def test_registration_rules_of_bare_tags_fall_back_to_default(monkeypatch):
    from app.handlers import start

    monkeypatch.setattr(start, 'get_rules', AsyncMock(return_value='<p></p>'))
    monkeypatch.setattr(start, 'get_default_rules', lambda _language: '<h2>Правила по умолчанию</h2>')

    assert await start._rules_for_telegram('ru') == '<b>Правила по умолчанию</b>'


def test_registration_sends_rules_only_through_the_converter():
    """Любая новая отправка правил из start.py обязана идти через _rules_for_telegram."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / 'app' / 'handlers' / 'start.py').read_text(encoding='utf-8')
    raw_calls = re.findall(r'await get_rules\(', source)

    assert len(raw_calls) == 1, 'правила из get_rules() уходят в Telegram мимо _rules_for_telegram'
