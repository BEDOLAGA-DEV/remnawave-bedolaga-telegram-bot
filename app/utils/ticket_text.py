"""Ограничения длины и постраничная разбивка текста тикетов.

Сообщения тикетов лежат в Text-колонке без ограничения длины, а кабинет и
webapi принимают до 4000 символов (см. app/cabinet/schemas/tickets.py). Бот
обязан показывать такие сообщения целиком, поэтому разбивка страниц:

  * никогда не режет HTML-сущности (`&quot;` -> `&qu` + `ot;`) — иначе Telegram
    отвечает "can't parse entities" и страница не отображается вообще;
  * всегда оставляет положительный бюджет под содержимое, даже если шапка
    тикета длинная (иначе бюджет уходил в ноль и разбивка зацикливалась);
  * не теряет ни одного символа: склейка всех страниц равна исходному тексту.
"""

from app.utils.telegram_html import trim_broken_markup


# Максимальная длина одного сообщения тикета. Совпадает с max_length в схемах
# кабинета и webapi, чтобы бот и веб-кабинет вели себя одинаково.
TICKET_MESSAGE_MAX_LENGTH = 4000

# Лимит Telegram — 4096 символов; берём запас на служебные строки и разметку.
TICKET_PAGE_MAX_LEN = 3500

# Длина превью сообщения в уведомлениях (полный текст — в карточке тикета).
TICKET_PREVIEW_MAX_LENGTH = 500

# Минимальный бюджет под содержимое страницы, чтобы длинная шапка не съела всё.
_MIN_CONTENT_BUDGET = 500


def preview_text(text: str | None, limit: int = TICKET_PREVIEW_MAX_LENGTH) -> str:
    """Короткое превью сообщения для уведомлений (полный текст — в карточке тикета)."""
    value = (text or '').strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + '...'


def split_long_block(block: str, max_len: int) -> list[str]:
    """Режет блок по границам строк/слов, не разрывая HTML-теги и сущности."""
    max_len = max(1, max_len)
    if len(block) <= max_len:
        return [block]

    parts: list[str] = []
    remaining = block
    while remaining:
        if len(remaining) <= max_len:
            parts.append(remaining)
            break

        cut_at = max_len
        newline_pos = remaining.rfind('\n', 0, max_len)
        space_pos = remaining.rfind(' ', 0, max_len)

        if newline_pos > max_len // 2:
            cut_at = newline_pos + 1
        elif space_pos > max_len // 2:
            cut_at = space_pos + 1

        # Кусок мог оборваться на половине `&quot;` — откатываемся до целого
        # символа. Фоллбек на сырой кусок гарантирует прогресс цикла.
        piece = trim_broken_markup(remaining[:cut_at]) or remaining[:cut_at]
        parts.append(piece)
        remaining = remaining[len(piece) :]

    return parts


def build_ticket_pages(
    header: str,
    message_blocks: list[str],
    max_len: int = TICKET_PAGE_MAX_LEN,
) -> list[str]:
    """Собирает страницы «шапка + сообщения», не теряя ни одного символа."""
    header = header or ''
    budget = max(_MIN_CONTENT_BUDGET, max_len - len(header))

    pages: list[str] = []
    current = ''

    for block in message_blocks:
        for part in split_long_block(block, budget):
            if current and len(current) + len(part) > budget:
                pages.append(header + current)
                current = part
            else:
                current += part

    if current.strip():
        pages.append(header + current)

    return pages or [header]
