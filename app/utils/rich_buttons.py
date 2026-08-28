"""Перенос инлайн-клавиатуры внутрь полотна rich-сообщения (Bot API 10.3).

Bot API 10.3 добавил кнопки прямо в разметку rich-сообщений: ``<tg-button>`` для
одиночной кнопки и ``<tg-button-row>`` для ряда из 1-8 штук. Разметку разбирает
сервер, поэтому здесь достаточно собрать HTML — типизированные блоки
``InputRichBlockButtons`` не нужны, а главное, они и не подошли бы: у
``InputRichMessage`` ровно одно из ``html``/``markdown``/``blocks``, а всё меню
собирается конкатенацией HTML.

Правило «всё или ничего». Если хотя бы одна кнопка клавиатуры непредставима в
rich-виде, модуль возвращает ``None`` и вызывающий оставляет обычную клавиатуру
под сообщением целиком. Наполовину перенесённая клавиатура — это потерянные
кнопки, а не компромисс.

Непредставимы:
* ``pay`` и ``callback_game`` — у ``RichMessageButton`` таких полей нет вовсе;
* ``web_app`` вне приватного чата — Mini App открывается только в личке, в
  групповом админ-чате такая кнопка не сработает.
"""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# InputRichBlockButtons: «List of 1-8 buttons to send». Более длинный ряд сервер
# отвергнет, поэтому режем сами — иначе одна широкая строка меню уронила бы всё
# сообщение целиком.
MAX_BUTTONS_PER_ROW = 8

# style="link" спецификация разрешает только callback-кнопкам.
_LINK_STYLE = 'link'
_ALLOWED_STYLES = frozenset({'danger', 'success', 'primary', _LINK_STYLE})


def _attr(value: str) -> str:
    """Значение атрибута: кавычки и угловые скобки обязаны быть экранированы."""
    return html.escape(str(value), quote=True)


def _button_text_html(button: InlineKeyboardButton) -> str:
    """Текст кнопки. Кастомная иконка переносится как <tg-emoji>.

    У ``RichMessageButton`` нет ``icon_custom_emoji_id``, но текст кнопки —
    это RichText, в который ``RichTextCustomEmoji`` входит. Так иконка не теряется.
    """
    text = html.escape(button.text or '')
    if button.icon_custom_emoji_id:
        return f'<tg-emoji emoji-id="{_attr(button.icon_custom_emoji_id)}"></tg-emoji>{text}'
    return text


def _render_button(button: InlineKeyboardButton, *, allow_web_app: bool) -> str | None:
    """Одна кнопка в ``<tg-button>``. ``None`` — представить нельзя."""
    if button.pay or button.callback_game is not None:
        return None

    attrs: list[str] = []

    if button.url:
        attrs.append('type="url"')
        attrs.append(f'url="{_attr(button.url)}"')
    elif button.callback_data:
        attrs.append('type="callback_data"')
        attrs.append(f'data="{_attr(button.callback_data)}"')
    elif button.web_app is not None:
        if not allow_web_app:
            return None
        attrs.append('type="web_app"')
        attrs.append(f'url="{_attr(button.web_app.url)}"')
    elif button.login_url is not None:
        attrs.append('type="login_url"')
        attrs.append(f'url="{_attr(button.login_url.url)}"')
        if button.login_url.forward_text:
            attrs.append(f'forward-text="{_attr(button.login_url.forward_text)}"')
        if button.login_url.request_write_access:
            attrs.append('request-write-access')
    elif button.copy_text is not None:
        attrs.append('type="copy_text"')
        attrs.append(f'text="{_attr(button.copy_text.text)}"')
    elif button.switch_inline_query is not None:
        attrs.append('type="switch_inline_query"')
        attrs.append(f'query="{_attr(button.switch_inline_query)}"')
    elif button.switch_inline_query_current_chat is not None:
        attrs.append('type="switch_inline_query_current_chat"')
        attrs.append(f'query="{_attr(button.switch_inline_query_current_chat)}"')
    elif button.switch_inline_query_chosen_chat is not None:
        chosen = button.switch_inline_query_chosen_chat
        attrs.append('type="switch_inline_query_chosen_chat"')
        if chosen.query is not None:
            attrs.append(f'query="{_attr(chosen.query)}"')
        for flag, name in (
            (chosen.allow_user_chats, 'allow-user-chats'),
            (chosen.allow_bot_chats, 'allow-bot-chats'),
            (chosen.allow_group_chats, 'allow-group-chats'),
            (chosen.allow_channel_chats, 'allow-channel-chats'),
        ):
            if flag:
                attrs.append(name)
    else:
        # Кнопка без действия — в rich это осмысленное состояние, а не ошибка.
        attrs.append('type="disabled"')

    style = (button.style or '').strip().lower()
    if style in _ALLOWED_STYLES:
        # link допустим только у callback-кнопок; на остальных сервер отвергнет.
        if style != _LINK_STYLE or button.callback_data:
            attrs.append(f'style="{_attr(style)}"')

    return f'<tg-button {" ".join(attrs)}>{_button_text_html(button)}</tg-button>'


def render_keyboard_as_rich_html(
    keyboard: InlineKeyboardMarkup | None,
    *,
    allow_web_app: bool = True,
    align: str | None = None,
) -> str | None:
    """Клавиатура целиком в виде рядов ``<tg-button-row>``.

    ``None`` — клавиатуру перенести нельзя, вызывающий обязан оставить её под
    сообщением как есть. Пустая клавиатура тоже даёт ``None``: переносить нечего.
    """
    if keyboard is None or not keyboard.inline_keyboard:
        return None

    align_attr = f' align="{_attr(align)}"' if align else ''
    rows_html: list[str] = []

    for row in keyboard.inline_keyboard:
        rendered = [_render_button(button, allow_web_app=allow_web_app) for button in row]
        if any(item is None for item in rendered):
            return None
        if not rendered:
            continue
        for start in range(0, len(rendered), MAX_BUTTONS_PER_ROW):
            chunk = rendered[start : start + MAX_BUTTONS_PER_ROW]
            rows_html.append(f'<tg-button-row{align_attr}>{"".join(chunk)}</tg-button-row>')

    if not rows_html:
        return None
    return ''.join(rows_html)
