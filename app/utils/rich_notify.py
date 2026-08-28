"""Отправка пользовательских уведомлений rich-сообщением (Bot API 10.3).

Уведомления исторически уходят обычным ``send_message`` с ``parse_mode='HTML'``
и клавиатурой под сообщением. В rich-режиме это выбивается из общего вида: меню
у пользователя уже rich, а уведомления — нет.

Главная тонкость перевода — переносы строк. Классический Telegram-HTML разбивает
текст по ``\\n``, а rich-разметка блочная: спецификация про свой набор тегов прямо
отмечает «all the text above was on the same line». Отданный как есть текст
уведомления слипся бы в сплошное полотно. Поэтому пустая строка становится
границей абзаца, одиночный перенос — ``<br>``.

Дисциплина «всё или ничего», как и в остальном rich-коде: если текст содержит
блочную разметку, которую нельзя перенести дословно, функция возвращает ``None``,
и вызывающий отправляет классическое сообщение. Тихо испортить вид уведомления
хуже, чем не превращать его в rich.
"""

from __future__ import annotations

import asyncio
import html
import re

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from aiogram.types import InlineKeyboardMarkup, InputRichMessage

from app.config import settings

# Тот же предел, что и у rich-уведомлений админ-чата: ограничение самого Telegram,
# а не наше — держим его в одном месте, чтобы значения не разъехались.
from app.utils.rich_admin import RICH_TEXT_LIMIT
from app.utils.rich_buttons import render_keyboard_as_rich_html
from app.utils.rich_menu import (
    _is_media_fetch_error,
    _looks_like_unsupported,
    _mark_logo_unavailable_once,
    _mark_rich_unavailable,
    _resolve_rich_logo_url,
    is_rich_menu_enabled,
)


logger = structlog.get_logger(__name__)

# Блочные конструкции классического HTML, которые в rich ведут себя иначе или
# требуют перестройки дерева. Встретив такое, честнее отдать классику.
_BLOCK_MARKUP_RE = re.compile(r'</?(?:pre|blockquote|ul|ol|li|table|h[1-6]|p|div)\b', re.IGNORECASE)

# Классический Telegram-HTML помечает спойлер span-ом, rich — своим тегом.
_SPOILER_SPAN_RE = re.compile(
    r'<span\s+class=(["\'])tg-spoiler\1[^>]*>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
_BLANK_LINE_RE = re.compile(r'\n\s*\n+')
# [^<>], а не [^>]: на строке из одних '<' второй вариант перебирает хвост
# заново с каждой позиции — квадратичное время на тексте, который приходит
# от пользователя. Заодно честнее: '1 < 2' остаётся текстом, а не съедается
# как открытый тег.
_TAG_RE = re.compile(r'<[^<>]+>')

# Первая строка уведомления почти всегда служит заголовком («⚠️ <b>Подписка
# истекает</b>»), и в стиле меню ей место в <h4>. Но если строка длинная, это уже
# не заголовок, а первый абзац текста — делать из него огромный заголовок хуже,
# чем оставить абзацем.
_TITLE_MAX_LENGTH = 80


def _visible_length(value: str) -> int:
    """Длина без учёта разметки: <b>Тариф</b> — это пять символов, а не двенадцать."""
    return len(_TAG_RE.sub('', value).strip())


def _paragraphs_html(lines: list[str]) -> list[str]:
    """Строки → абзацы: пустая строка разделяет, одиночный перенос даёт <br>."""
    chunks = [chunk.strip('\n') for chunk in _BLANK_LINE_RE.split('\n'.join(lines))]
    return [f'<p>{chunk.replace(chr(10), "<br>")}</p>' for chunk in chunks if chunk.strip()]


def build_notification_rich_html(text: str, *, logo_url: str = '') -> str | None:
    """Текст уведомления → rich-разметка в стиле главного меню.

    Повторяет визуальный язык ``build_main_menu_rich_html``: шапка с логотипом,
    заголовок в ``<h4>``, ``<hr/>`` под ним и абзацы содержимого. Так уведомление
    выглядит частью того же интерфейса, а не чужеродным текстом.

    ``None`` — превратить в rich нельзя, вызывающий шлёт классическое сообщение.
    """
    if not text or not text.strip():
        return None

    if _BLOCK_MARKUP_RE.search(text):
        return None

    value = _SPOILER_SPAN_RE.sub(r'<tg-spoiler>\2</tg-spoiler>', text)
    lines = value.split('\n')

    blocks: list[str] = []
    if logo_url:
        blocks.append(f'<img src="{html.escape(logo_url, quote=True)}"/>')

    # Заголовок отделяем, только если под ним реально есть содержимое: иначе
    # однострочное уведомление превратилось бы в заголовок без текста.
    first_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        return None

    title = lines[first_index].strip()
    rest = lines[first_index + 1 :]
    if _visible_length(title) <= _TITLE_MAX_LENGTH and any(line.strip() for line in rest):
        blocks.append(f'<h4>{title}</h4>')
        blocks.append('<hr/>')
        blocks.extend(_paragraphs_html(rest))
    else:
        blocks.extend(_paragraphs_html(lines[first_index:]))

    if not any(block.startswith(('<h4>', '<p>')) for block in blocks):
        return None

    return ''.join(blocks)


async def try_send_rich_notification(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    keyboard: InlineKeyboardMarkup | None = None,
    with_logo: bool = False,
    timeout: float | None = None,
) -> bool:
    """Шлёт уведомление rich-сообщением. ``False`` — отправить классическое.

    Без ретраев: их делает классический путь, на который вызывающий обязан
    откатиться при ``False``. Уведомления уходят в личный чат, поэтому Mini App
    среди переносимых кнопок допустим.

    ``with_logo`` вставляет шапку с логотипом тем же способом, что и rich-меню:
    публичной ссылкой в ``<img>``, а не загрузкой файла. Так уведомления
    мониторинга не теряют логотип при переходе на rich.

    ``timeout`` ограничивает ОДНУ попытку. Он обязателен там, где отправка идёт
    по списку получателей: без него залипший запрос держит await до таймаута
    сессии и блокирует хвост цикла.
    """
    if not settings.USER_NOTIFICATIONS_RICH_ENABLED or not is_rich_menu_enabled():
        return False

    logo_url = _resolve_rich_logo_url() if with_logo else ''
    rich_html = build_notification_rich_html(text, logo_url=logo_url)
    if rich_html is None or len(rich_html) > RICH_TEXT_LIMIT:
        return False

    reply_markup = keyboard
    if keyboard is not None and settings.MAIN_MENU_RICH_INLINE_BUTTONS:
        buttons_html = render_keyboard_as_rich_html(keyboard, allow_web_app=True)
        if buttons_html is not None:
            rich_html += buttons_html
            reply_markup = None

    kwargs: dict = {
        'chat_id': chat_id,
        'rich_message': InputRichMessage(html=rich_html, skip_entity_detection=True),
    }
    if reply_markup is not None:
        kwargs['reply_markup'] = reply_markup

    try:
        if timeout is not None:
            await asyncio.wait_for(bot.send_rich_message(**kwargs), timeout=timeout)
        else:
            await bot.send_rich_message(**kwargs)
        return True
    except TimeoutError:
        # Пробрасываем наверх: вызывающий по списку получателей сам решает,
        # пропустить получателя или пробовать классику — повторная попытка с тем
        # же таймаутом удвоила бы бюджет цикла.
        raise
    except TelegramForbiddenError:
        # Пользователь заблокировал бота — классика упрётся в то же самое, но
        # пусть отработает её штатная обработка (там свой учёт и метрики).
        return False
    except (TelegramNotFound, TelegramBadRequest) as error:
        if logo_url and _is_media_fetch_error(error):
            # Логотип не скачался — единственный повтор уже без него, флаг взводится
            # глобально, как в rich-меню, чтобы не долбить недоступную ссылку.
            _mark_logo_unavailable_once(error)
            return await try_send_rich_notification(
                bot, chat_id, text, keyboard=keyboard, with_logo=False, timeout=timeout
            )
        if _looks_like_unsupported(error):
            # Сервер не знает про rich — гасим его глобально тем же флагом, что и меню.
            # Без этого рассылка на тысячи получателей удвоила бы число запросов:
            # каждому сначала неудачный rich, потом классика.
            _mark_rich_unavailable(error)
            return False
        logger.warning('Rich-уведомление не отправлено, фоллбек на классику', error=str(error), chat_id=chat_id)
        return False
    except Exception as error:
        # В том числе ClientDecodeError: он наследуется от AiogramError, а не от
        # TelegramAPIError, и иначе прошёл бы мимо всех except выше.
        logger.warning('Непредвиденная ошибка rich-уведомления', error=str(error), chat_id=chat_id)
        return False
