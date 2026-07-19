"""Кастомные кнопки рассылки поддерживают icon_custom_emoji_id (#3025).

Кабинет присылает кастомные кнопки как {label, action_type, action_value};
поле icon_custom_emoji_id необязательно и, если задано, должно доходить до
InlineKeyboardButton (custom emoji перед текстом кнопки, Bot API).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.cabinet.schemas.broadcasts import CustomBroadcastButton
from app.handlers.admin.messages import create_broadcast_keyboard


EMOJI_ID = '5368324170671202286'


def test_keyboard_passes_icon_custom_emoji_id():
    keyboard = create_broadcast_keyboard(
        [],
        custom_buttons=[
            {
                'label': 'Открыть',
                'action_type': 'url',
                'action_value': 'https://example.com',
                'icon_custom_emoji_id': EMOJI_ID,
            },
            {'label': 'Меню', 'action_type': 'callback', 'action_value': 'menu_main'},
        ],
    )

    url_button = keyboard.inline_keyboard[0][0]
    assert url_button.url == 'https://example.com'
    assert url_button.icon_custom_emoji_id == EMOJI_ID

    # Без поля кнопка остаётся обычной
    callback_button = keyboard.inline_keyboard[1][0]
    assert callback_button.callback_data == 'menu_main'
    assert callback_button.icon_custom_emoji_id is None


def test_schema_roundtrips_icon_custom_emoji_id():
    button = CustomBroadcastButton(
        label='Открыть',
        action_type='url',
        action_value='https://example.com',
        icon_custom_emoji_id=EMOJI_ID,
    )
    # model_dump — именно так кнопки уходят в конфиг рассылки (admin_broadcasts.py)
    assert button.model_dump()['icon_custom_emoji_id'] == EMOJI_ID


def test_schema_defaults_to_none_and_rejects_garbage():
    button = CustomBroadcastButton(label='Меню', action_value='menu_main')
    assert button.icon_custom_emoji_id is None

    with pytest.raises(ValidationError):
        CustomBroadcastButton(label='Меню', action_value='menu_main', icon_custom_emoji_id='not-a-number')
