"""Перенос инлайн-клавиатуры внутрь полотна rich-сообщения (Bot API 10.3).

Разметку разбирает сервер, поэтому проверяем именно сгенерированный HTML:
имена тегов и атрибутов должны совпадать со спецификацией
https://core.telegram.org/bots/api#rich-message-formatting-options
(<tg-button type=... style=... url=... data=... query=... text=...>,
ряды <tg-button-row align=...> по 1-8 кнопок).
"""

import pytest
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LoginUrl,
    SwitchInlineQueryChosenChat,
    WebAppInfo,
)

from app.utils.rich_buttons import MAX_BUTTONS_PER_ROW, render_keyboard_as_rich_html


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


@pytest.mark.parametrize(
    ('button', 'expected'),
    [
        (
            InlineKeyboardButton(text='Меню', callback_data='menu'),
            '<tg-button type="callback_data" data="menu">Меню</tg-button>',
        ),
        (
            InlineKeyboardButton(text='Сайт', url='https://example.com'),
            '<tg-button type="url" url="https://example.com">Сайт</tg-button>',
        ),
        (
            InlineKeyboardButton(text='Кабинет', web_app=WebAppInfo(url='https://cab.example')),
            '<tg-button type="web_app" url="https://cab.example">Кабинет</tg-button>',
        ),
        (
            InlineKeyboardButton(text='Копировать', copy_text=CopyTextButton(text='ABC-1')),
            '<tg-button type="copy_text" text="ABC-1">Копировать</tg-button>',
        ),
        (
            InlineKeyboardButton(text='Поиск', switch_inline_query='запрос'),
            '<tg-button type="switch_inline_query" query="запрос">Поиск</tg-button>',
        ),
        (
            InlineKeyboardButton(text='Тут', switch_inline_query_current_chat='q'),
            '<tg-button type="switch_inline_query_current_chat" query="q">Тут</tg-button>',
        ),
        # Кнопка без действия — в rich это валидное состояние disabled.
        (
            InlineKeyboardButton(text='Заголовок'),
            '<tg-button type="disabled">Заголовок</tg-button>',
        ),
    ],
)
def test_button_types_map_to_spec_tags(button: InlineKeyboardButton, expected: str) -> None:
    assert render_keyboard_as_rich_html(_kb([button])) == f'<tg-button-row>{expected}</tg-button-row>'


def test_login_url_carries_its_flags() -> None:
    button = InlineKeyboardButton(
        text='Войти',
        login_url=LoginUrl(url='https://t.me', forward_text='вперёд', request_write_access=True),
    )

    html = render_keyboard_as_rich_html(_kb([button]))

    assert 'type="login_url"' in html
    assert 'url="https://t.me"' in html
    assert 'forward-text="вперёд"' in html
    assert 'request-write-access' in html


def test_chosen_chat_flags_are_emitted_as_bare_attributes() -> None:
    button = InlineKeyboardButton(
        text='Выбрать',
        switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
            query='q', allow_user_chats=True, allow_group_chats=True
        ),
    )

    html = render_keyboard_as_rich_html(_kb([button]))

    assert 'allow-user-chats' in html
    assert 'allow-group-chats' in html
    assert 'allow-bot-chats' not in html


def test_rows_are_preserved_and_align_applied() -> None:
    kb = _kb(
        [InlineKeyboardButton(text='a', callback_data='a'), InlineKeyboardButton(text='b', callback_data='b')],
        [InlineKeyboardButton(text='c', callback_data='c')],
    )

    html = render_keyboard_as_rich_html(kb, align='center')

    assert html.count('<tg-button-row align="center">') == 2
    assert html.count('<tg-button ') == 3


def test_long_row_is_split_by_spec_limit() -> None:
    """InputRichBlockButtons: «List of 1-8 buttons». Более длинный ряд сервер отвергнет."""
    kb = _kb([InlineKeyboardButton(text=str(i), callback_data=str(i)) for i in range(MAX_BUTTONS_PER_ROW + 3)])

    html = render_keyboard_as_rich_html(kb)

    assert html.count('<tg-button-row>') == 2
    first_row = html.split('</tg-button-row>')[0]
    assert first_row.count('<tg-button ') == MAX_BUTTONS_PER_ROW


@pytest.mark.parametrize(
    'button',
    [
        InlineKeyboardButton(text='Оплатить', pay=True),
        InlineKeyboardButton(text='Игра', callback_game={}),
    ],
)
def test_unrepresentable_button_keeps_whole_keyboard_outside(button: InlineKeyboardButton) -> None:
    """Всё или ничего: половина кнопок внутри — это потерянные кнопки."""
    kb = _kb([InlineKeyboardButton(text='ok', callback_data='ok')], [button])

    assert render_keyboard_as_rich_html(kb) is None


def test_web_app_is_refused_outside_private_chat() -> None:
    """Mini App открывается только в личке — в группе такая кнопка не сработает."""
    kb = _kb([InlineKeyboardButton(text='Кабинет', web_app=WebAppInfo(url='https://cab.example'))])

    assert render_keyboard_as_rich_html(kb, allow_web_app=True) is not None
    assert render_keyboard_as_rich_html(kb, allow_web_app=False) is None


def test_link_style_only_survives_on_callback_buttons() -> None:
    """style="link" спецификация разрешает только callback-кнопкам."""
    callback = InlineKeyboardButton(text='a', callback_data='a', style='link')
    link = InlineKeyboardButton(text='b', url='https://example.com', style='link')

    assert 'style="link"' in render_keyboard_as_rich_html(_kb([callback]))
    assert 'style=' not in render_keyboard_as_rich_html(_kb([link]))


def test_unknown_style_is_dropped_not_forwarded() -> None:
    button = InlineKeyboardButton(text='a', callback_data='a', style='rainbow')

    assert 'style=' not in render_keyboard_as_rich_html(_kb([button]))


def test_custom_emoji_icon_moves_into_button_text() -> None:
    """У RichMessageButton нет icon_custom_emoji_id — иконка переносится в текст."""
    button = InlineKeyboardButton(text='Меню', callback_data='m', icon_custom_emoji_id='5368324170671202286')

    html = render_keyboard_as_rich_html(_kb([button]))

    assert '<tg-emoji emoji-id="5368324170671202286"></tg-emoji>Меню' in html


def test_text_and_attributes_are_escaped() -> None:
    button = InlineKeyboardButton(text='<b>жирный</b> & "кавычки"', url='https://e.com/?a=1&b=2')

    html = render_keyboard_as_rich_html(_kb([button]))

    assert '&lt;b&gt;' in html
    assert 'https://e.com/?a=1&amp;b=2' in html
    assert '<b>' not in html


@pytest.mark.parametrize('keyboard', [None, InlineKeyboardMarkup(inline_keyboard=[])])
def test_nothing_to_move_returns_none(keyboard) -> None:
    assert render_keyboard_as_rich_html(keyboard) is None
