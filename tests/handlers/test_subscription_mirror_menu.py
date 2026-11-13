from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardMarkup

from app.handlers.subscription import links
from app.keyboards import inline


def test_selector_uses_explicit_subscription(monkeypatch):
    monkeypatch.setattr(
        inline, 'get_subscription_mirrors', lambda url: [{'id': 'a' * 16, 'label': 'Mirror', 'url': url}], raising=False
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[])
    result = inline.add_subscription_mirror_selector(
        markup, SimpleNamespace(id=47, subscription_url='https://example.org/token'), 'en'
    )
    assert result.inline_keyboard[0][0].callback_data == 'sub_mirrors:47:0:main'


@pytest.mark.asyncio
@pytest.mark.parametrize('data', ['sub_mirrors:99:2:main', 'sub_mirror_pick:99:aaaaaaaaaaaaaaaa'])
async def test_foreign_subscription_never_falls_back(monkeypatch, data):
    from app.database.crud import subscription as crud

    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(crud, 'get_subscription_by_id_for_user', lookup)
    callback = SimpleNamespace(data=data, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await links.handle_subscription_mirrors(callback, SimpleNamespace(id=8, language='en'), object())
    assert lookup.await_args.args[1:] == (99, 8)
    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'data',
    [
        'sub_mirrors:2:bad:main',
        'sub_mirrors:0:0:main',
        'sub_mirrors:2:-1:main',
        'sub_mirror_pick:2:https://evil.test',
        'sub_mirrors:2:0:main:3',
    ],
)
async def test_malformed_callbacks_rejected(monkeypatch, data):
    from app.database.crud import subscription as crud

    lookup = AsyncMock()
    monkeypatch.setattr(crud, 'get_subscription_by_id_for_user', lookup)
    callback = SimpleNamespace(data=data, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await links.handle_subscription_mirrors(callback, SimpleNamespace(id=8, language='en'), object())
    lookup.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['link', 'guide', 'miniapp_subscription'])
async def test_paging_and_pick_regenerate_current_link(monkeypatch, mode):
    monkeypatch.setattr(links.settings, 'CONNECT_BUTTON_MODE', mode)
    from app.database.crud import subscription as crud

    sub = SimpleNamespace(id=47, subscription_url='https://primary.test/fresh')
    monkeypatch.setattr(crud, 'get_subscription_by_id_for_user', AsyncMock(return_value=sub))
    monkeypatch.setattr(
        links,
        'get_subscription_mirrors',
        lambda url: [{'id': f'{i:016x}', 'label': f'Mirror {i}', 'url': f'https://m{i}.test/fresh'} for i in range(20)],
        raising=False,
    )
    callback = SimpleNamespace(
        data='sub_mirrors:47:2:main', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    user = SimpleNamespace(id=8, language='en')
    await links.handle_subscription_mirrors(callback, user, object())
    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    choices = [
        b for row in keyboard.inline_keyboard for b in row if (b.callback_data or '').startswith('sub_mirror_pick:')
    ]
    assert len(choices) == 6
    assert choices[0].callback_data == 'sub_mirror_pick:47:000000000000000b'
    callback.data = 'sub_mirror_pick:47:000000000000000b'
    await links.handle_subscription_mirrors(callback, user, object())
    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    connect = keyboard.inline_keyboard[0][0]
    assert (connect.web_app.url if mode == 'miniapp_subscription' else connect.url) == 'https://m11.test/fresh'
    assert keyboard.inline_keyboard[1][0].callback_data == 'sub_mirrors:47:0:000000000000000b'


@pytest.mark.parametrize('mode', ['guide', 'link', 'miniapp_subscription', 'miniapp_custom', 'happ_cryptolink'])
@pytest.mark.parametrize('hidden', [False, True])
@pytest.mark.parametrize('count', [0, 1, 20])
def test_selector_visibility_modes_and_counts(monkeypatch, mode, hidden, count):
    from app.config import Settings

    monkeypatch.setattr(inline.settings, 'CONNECT_BUTTON_MODE', mode)
    monkeypatch.setattr(Settings, 'should_hide_subscription_link', lambda self: hidden)
    monkeypatch.setattr(
        inline.settings, 'SUBSCRIPTION_MIRROR_LINKS', '\n'.join(f'Mirror {i}|https://m{i}.test' for i in range(count))
    )
    sub = SimpleNamespace(id=47, subscription_url='https://primary.test/token')
    result = inline.add_subscription_mirror_selector(InlineKeyboardMarkup(inline_keyboard=[]), sub, 'en')
    assert bool(result.inline_keyboard) == (
        count > 0 and not hidden and mode in {'guide', 'link', 'miniapp_subscription'}
    )
    if result.inline_keyboard:
        assert len(result.inline_keyboard) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('data', ['sub_mirror_pick:47:ffffffffffffffff', 'sub_mirrors:47:99:main'])
async def test_removed_choice_and_out_of_range_page_do_not_render(monkeypatch, data):
    from app.database.crud import subscription as crud

    monkeypatch.setattr(
        crud,
        'get_subscription_by_id_for_user',
        AsyncMock(return_value=SimpleNamespace(id=47, subscription_url='https://primary.test/new')),
    )
    monkeypatch.setattr(
        links,
        'get_subscription_mirrors',
        lambda url: [{'id': 'a' * 16, 'label': 'Mirror', 'url': 'https://mirror.test/new'}],
    )
    callback = SimpleNamespace(data=data, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await links.handle_subscription_mirrors(callback, SimpleNamespace(id=8, language='en'), object())
    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.asyncio
async def test_oversized_subscription_id_rejected_before_database(monkeypatch):
    from app.database.crud import subscription as crud

    lookup = AsyncMock()
    monkeypatch.setattr(crud, 'get_subscription_by_id_for_user', lookup)
    callback = SimpleNamespace(
        data='sub_mirrors:9999999999999999999:0:main',
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await links.handle_subscription_mirrors(callback, SimpleNamespace(id=8, language='en'), object())
    lookup.assert_not_awaited()


def test_selector_follows_connection_button_not_unrelated_first_row(monkeypatch):
    from aiogram.types import InlineKeyboardButton

    monkeypatch.setattr(
        inline, 'get_subscription_mirrors', lambda url: [{'id': 'a' * 16, 'label': 'Mirror', 'url': url}]
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='News', callback_data='news')],
            [InlineKeyboardButton(text='Connect', url='https://primary.test/token')],
            [InlineKeyboardButton(text='Back', callback_data='back_to_menu')],
        ]
    )
    result = inline.add_subscription_mirror_selector(
        markup, SimpleNamespace(id=47, subscription_url='https://primary.test/token'), 'en'
    )
    assert result.inline_keyboard[2][0].callback_data == 'sub_mirrors:47:0:main'


def test_selector_preserves_subscription_context_for_existing_connect(monkeypatch):
    from aiogram.types import InlineKeyboardButton

    monkeypatch.setattr(
        inline, 'get_subscription_mirrors', lambda url: [{'id': 'a' * 16, 'label': 'Mirror', 'url': url}]
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='Connect', callback_data='subscription_connect')]]
    )
    result = inline.add_subscription_mirror_selector(
        markup, SimpleNamespace(id=47, subscription_url='https://primary.test/token'), 'en'
    )
    assert result.inline_keyboard[0][0].callback_data == 'subscription_connect:47'
    assert markup.inline_keyboard[0][0].callback_data == 'subscription_connect'
