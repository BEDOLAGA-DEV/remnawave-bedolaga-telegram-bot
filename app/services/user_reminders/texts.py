"""Тексты напоминаний: проверка, выбор языка, сообщение для бота и карточка для кабинета.

Текст — простой, без разметки: бот экранирует его и делает жирным только заголовок,
кабинет выводит как текст. Так поле админки не может подсунуть HTML ни туда, ни туда.
"""

from __future__ import annotations

import html
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.utils.miniapp_buttons import build_cabinet_url


LANGUAGES = ('ru', 'en', 'ua', 'zh', 'fa')
DEFAULT_LANGUAGE = 'ru'
BUTTON_KINDS = ('none', 'cabinet', 'url')


class ReminderText(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=1000)
    button: str | None = Field(None, max_length=40)

    @field_validator('button')
    @classmethod
    def _empty_button_is_none(cls, value: str | None) -> str | None:
        return value or None


def validate_texts(raw: dict) -> dict[str, dict]:
    if not isinstance(raw, dict) or DEFAULT_LANGUAGE not in raw:
        raise ValueError('texts.ru is required')
    unknown = set(raw) - set(LANGUAGES)
    if unknown:
        raise ValueError(f'unsupported languages: {sorted(unknown)}')
    try:
        return {lang: ReminderText.model_validate(value).model_dump() for lang, value in raw.items()}
    except ValidationError as error:
        raise ValueError(str(error)) from error


def validate_button(kind: str, target: str | None, texts: dict) -> None:
    if kind not in BUTTON_KINDS:
        raise ValueError(f'unknown button kind: {kind}')
    if kind == 'none':
        if target:
            raise ValueError('button_target must be empty when there is no button')
        return
    if not (texts.get(DEFAULT_LANGUAGE) or {}).get('button'):
        raise ValueError('texts.ru.button is required when there is a button')
    if not target:
        raise ValueError('button_target is required')
    if kind == 'cabinet':
        if (
            not target.startswith('/')
            or target.startswith('//')
            or any(ch.isspace() for ch in target)
            or '\\' in target
        ):
            raise ValueError('cabinet path must start with a single "/" and contain no spaces')
        return
    parts = urlsplit(target)
    if parts.scheme != 'https' or not parts.netloc:
        raise ValueError('url must be an https:// link')


def _language(language: str | None) -> str:
    return (language or DEFAULT_LANGUAGE).split('-')[0].split('_')[0].lower()


def pick_text(texts: dict, language: str | None) -> dict:
    base = texts[DEFAULT_LANGUAGE]
    chosen = texts.get(_language(language)) or base
    return {
        'title': chosen['title'],
        'body': chosen['body'],
        'button': chosen.get('button') or base.get('button'),
    }


def _bot_button(kind: str, target: str | None, text: str | None) -> InlineKeyboardButton | None:
    if not text or not target:
        return None
    if kind == 'url':
        return InlineKeyboardButton(text=text, url=target)
    if kind == 'cabinet':
        url = build_cabinet_url(target)
        return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url)) if url else None
    return None


def render_bot_message(reminder, language: str | None) -> tuple[str, InlineKeyboardMarkup | None]:
    text = pick_text(reminder.texts, language)
    message = f'<b>{html.escape(text["title"])}</b>\n\n{html.escape(text["body"])}'
    button = _bot_button(reminder.button_kind, reminder.button_target, text['button'])
    return message, InlineKeyboardMarkup(inline_keyboard=[[button]]) if button else None


def render_card(reminder, language: str | None) -> dict:
    text = pick_text(reminder.texts, language)
    button = None
    # ReminderCardButton.kind — Literal['cabinet', 'url']: неизвестный сохранённый
    # button_kind (например, отключённый в будущем вид) должен просто не дать кнопку,
    # а не завалить ответ /cabinet/reminders/active валидацией.
    if reminder.button_kind in ('cabinet', 'url') and reminder.button_target and text['button']:
        button = {'kind': reminder.button_kind, 'target': reminder.button_target, 'text': text['button']}
    return {'id': reminder.id, 'title': text['title'], 'body': text['body'], 'button': button}
