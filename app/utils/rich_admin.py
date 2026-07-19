"""Rich-рендер сообщений админ-чата (Bot API 10.1, aiogram 3.29+).

Общий слой для всех отправок в админ-чат: уведомления
(AdminNotificationService._send_message), error-логи
(send_error_to_admin_chat), стартовое сообщение и отчёты. Даёт заголовки,
таблицы, сворачиваемые details-блоки с трейсбеками в <pre><code> и лимит
rich-сообщений 32768 символов вместо 4096 у классических.

Fallback-модель как у rich-меню (app/utils/rich_menu.py): после первого ответа
сервера «метод неизвестен» модуль запоминает недоступность до рестарта, и все
вызывающие пути возвращаются к классическим HTML-отправкам.

АНТИЛУП: этот модуль вызывается в том числе из конвейера отправки error-логов
в Telegram — любые собственные сбои логируются НЕ ВЫШЕ warning и строкой
(без объекта исключения), иначе получится усиление через
TelegramNotifierProcessor и flood control.
"""

import html
import re
from datetime import UTC, datetime

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from aiogram.types import InlineKeyboardMarkup, InputRichMessage

from app.config import settings
from app.utils.rich_menu import _looks_like_unsupported


logger = structlog.get_logger(__name__)

# Официальный лимит rich-сообщений — 32768 UTF-8 символов; держим запас
# на служебную разметку.
RICH_TEXT_LIMIT = 30_000

# Сервер не поддерживает rich-сообщения — латч до рестарта (отдельный от
# rich-меню: включаться/выключаться они могут независимо).
_rich_unavailable = False

# Классические админ-тексты используют <blockquote expandable> — rich-HTML
# такого атрибута не знает и отклонил бы всё сообщение.
_EXPANDABLE_QUOTE_RE = re.compile(r'<blockquote\s+expandable>', re.IGNORECASE)
# Первая строка классического уведомления — кандидат в заголовок h6
# («<b>💎 ПОКУПКА</b>» / «🔧 <b>ТЕХРАБОТЫ</b>»), если содержит жирный текст.
_LEADING_TITLE_RE = re.compile(r'^(?P<title>[^\n]{1,160})[ \t]*\n+')
# Сегментация по блочным тегам: внутри цитат переносы строк конвертируются в
# <br>, pre-блоки не трогаются вовсе (сохраняют форматирование), остальной
# текст — в абзацы.
_BLOCK_SPLIT_RE = re.compile(r'(<blockquote>.*?</blockquote>|<pre>.*?</pre>)', re.IGNORECASE | re.DOTALL)
_PRE_SPLIT_RE = re.compile(r'(<pre>.*?</pre>)', re.IGNORECASE | re.DOTALL)


def is_rich_admin_enabled() -> bool:
    return bool(settings.ADMIN_NOTIFICATIONS_RICH_ENABLED) and not _rich_unavailable


def _reset_rich_admin_availability() -> None:
    """Сбрасывает латч недоступности (используется в тестах)."""
    global _rich_unavailable
    _rich_unavailable = False


def _mark_rich_admin_unavailable(error: Exception) -> None:
    global _rich_unavailable
    if not _rich_unavailable:
        logger.warning(
            'Bot API сервер не поддерживает rich-сообщения — админ-уведомления переключены на классический вид',
            error=str(error),
        )
    _rich_unavailable = True


def rich_footer_now(label: str = 'Remnawave Bedolaga Bot') -> str:
    """Футер с меткой и временем: tg-time рендерится в таймзоне админа."""
    now = datetime.now(UTC)
    stamp = f'<tg-time unix="{int(now.timestamp())}" format="dt">{now.strftime("%d.%m.%Y %H:%M")} UTC</tg-time>'
    return f'<footer>{html.escape(label)} · {stamp}</footer>'


def rich_kv_table(rows: list[tuple[str, str]]) -> str:
    """Таблица «показатель → значение» (bordered/striped). Значения — сырой HTML."""
    body = ''.join(f'<tr><td>{html.escape(key)}</td><td align="right">{value}</td></tr>' for key, value in rows)
    return f'<table bordered striped>{body}</table>'


def rich_traceback_details(summary: str, traceback_text: str, *, open_by_default: bool = False) -> str:
    """Сворачиваемый traceback: <details> + <pre><code class="language-python">."""
    open_attr = ' open' if open_by_default else ''
    return (
        f'<details{open_attr}><summary>{html.escape(summary)}</summary>'
        f'<pre><code class="language-python">{html.escape(traceback_text)}</code></pre></details>'
    )


def _inline_newlines_to_rich(text: str) -> str:
    """Переносы строк классического текста → rich-разметка.

    Rich-HTML живёт по правилам HTML: `\\n` схлопывается в пробел, и без
    конвертации многострочное уведомление превращается в одну кашу-строку.
    Пустая строка = граница абзаца (<p>), одиночный перенос = <br>; внутри
    blockquote — только <br> (цитата остаётся одним блоком).
    """
    segments = _BLOCK_SPLIT_RE.split(text)
    rendered: list[str] = []
    for segment in segments:
        if not segment or not segment.strip():
            continue
        lowered = segment.lower()
        if lowered.startswith('<pre'):
            # pre сохраняет форматирование — переносы не трогаем
            rendered.append(segment)
            continue
        if lowered.startswith('<blockquote'):
            inner = segment[len('<blockquote>') : -len('</blockquote>')]
            pieces: list[str] = []
            for piece in _PRE_SPLIT_RE.split(inner):
                if not piece or not piece.strip():
                    continue
                if piece.lower().startswith('<pre'):
                    pieces.append(piece)
                    continue
                lines = [line for line in piece.split('\n') if line.strip()]
                if lines:
                    pieces.append('<br>'.join(lines))
            rendered.append('<blockquote>' + ''.join(pieces) + '</blockquote>')
            continue
        for paragraph in re.split(r'\n{2,}', segment):
            lines = [line for line in paragraph.split('\n') if line.strip()]
            if lines:
                rendered.append(f'<p>{"<br>".join(lines)}</p>')
    return ''.join(rendered)


def classic_admin_html_to_rich(text: str, *, footer_label: str | None = None) -> str:
    """Конвертирует классическое HTML-уведомление в rich-разметку.

    Консервативно: содержимое не переписывается, только оформление —
    первая строка с жирным заголовком становится h6 с разделителем,
    неподдерживаемый rich-HTML атрибут expandable у blockquote убирается,
    переносы строк конвертируются в абзацы/<br> (в rich-HTML голый `\\n`
    схлопывается), в конец добавляется footer с временем.
    """
    rich = _EXPANDABLE_QUOTE_RE.sub('<blockquote>', text.strip())

    header = ''
    match = _LEADING_TITLE_RE.match(rich)
    if match and '<b>' in match.group('title'):
        header = f'<h6>{match.group("title").strip()}</h6><hr/>'
        rich = rich[match.end() :]

    body = _inline_newlines_to_rich(rich)

    footer = rich_footer_now(footer_label) if footer_label else rich_footer_now()
    return f'{header}{body}<hr/>{footer}'


async def try_send_rich_admin_message(
    bot: Bot,
    chat_id: int | str,
    rich_html: str,
    *,
    thread_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Отправляет rich-сообщение в админ-чат. False — слать классический вариант.

    Без ретраев: ретраи и обработку flood control делает классический путь,
    на который вызывающий код обязан откатиться при False.
    """
    if not is_rich_admin_enabled():
        return False
    if len(rich_html) > RICH_TEXT_LIMIT:
        return False

    kwargs: dict = {
        'chat_id': chat_id,
        'rich_message': InputRichMessage(html=rich_html, skip_entity_detection=True),
    }
    if thread_id:
        kwargs['message_thread_id'] = thread_id
    if reply_markup:
        kwargs['reply_markup'] = reply_markup

    try:
        await bot.send_rich_message(**kwargs)
        return True
    except (TelegramNotFound, TelegramBadRequest) as error:
        if _looks_like_unsupported(error):
            _mark_rich_admin_unavailable(error)
        else:
            logger.warning('Не удалось отправить rich-сообщение в админ-чат', error=str(error))
        return False
    except TelegramForbiddenError as error:
        # Бот не может писать в чат — классический путь упрётся в то же самое,
        # но пусть отработает его штатная обработка.
        logger.warning('Rich-сообщение в админ-чат запрещено', error=str(error))
        return False
    except Exception as error:
        logger.warning('Ошибка отправки rich-сообщения в админ-чат', error=str(error))
        return False
