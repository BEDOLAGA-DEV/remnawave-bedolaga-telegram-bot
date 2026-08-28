"""Гарантии постраничной разбивки тикетов: ничего не теряется и не ломается.

Ловит реальные баги старой разбивки:
  * длинная шапка обнуляла бюджет блока -> зацикливание/пустые страницы;
  * разрез посреди `&quot;` -> Telegram отвечает "can't parse entities" и
    страница вообще не показывается;
  * хвост сообщения не попадал ни на одну страницу.
"""

import html
import re

from app.utils.ticket_text import (
    TICKET_MESSAGE_MAX_LENGTH,
    TICKET_PAGE_MAX_LEN,
    build_ticket_pages,
    preview_text,
    split_long_block,
)


HEADER = '🎫 Тикет #1\n\n📝 Заголовок: тест\n\n'
BROKEN_ENTITY_RE = re.compile(r'&[#\w]{0,9}$')


def _blocks(*texts: str) -> list[str]:
    return [f'👤 Вы (01.01 10:00):\n{html.escape(text)}\n\n' for text in texts]


def test_short_ticket_stays_on_one_page():
    pages = build_ticket_pages(HEADER, _blocks('короткое сообщение'))
    assert len(pages) == 1
    assert pages[0].startswith(HEADER)
    assert 'короткое сообщение' in pages[0]


def test_long_message_is_fully_visible_across_pages():
    message = ' '.join(f'слово{index}' for index in range(1200))
    pages = build_ticket_pages(HEADER, _blocks(message))

    assert len(pages) > 1
    joined = ''.join(page.removeprefix(HEADER) for page in pages)
    assert joined.count('слово0 ') == 1
    assert 'слово1199' in joined
    for index in range(1200):
        assert f'слово{index}' in joined


def test_no_page_exceeds_telegram_limit():
    message = 'а' * (TICKET_MESSAGE_MAX_LENGTH * 3)
    pages = build_ticket_pages(HEADER, _blocks(message, message))
    assert all(len(page) <= 4096 for page in pages)


def test_split_never_breaks_html_entities():
    # Сплошные кавычки: каждый символ превращается в шестисимвольный `&quot;`
    block = html.escape('"' * 4000)
    for part in split_long_block(block, 500):
        assert not BROKEN_ENTITY_RE.search(part), part
    assert ''.join(split_long_block(block, 500)) == block


def test_long_header_still_leaves_room_for_content():
    huge_header = 'ш' * (TICKET_PAGE_MAX_LEN + 100)
    pages = build_ticket_pages(huge_header, _blocks('текст сообщения'))

    assert len(pages) == 1
    assert 'текст сообщения' in pages[0]


def test_every_page_repeats_header():
    pages = build_ticket_pages(HEADER, _blocks('б' * 9000))
    assert len(pages) > 1
    assert all(page.startswith(HEADER) for page in pages)


def test_empty_ticket_returns_header_page():
    assert build_ticket_pages(HEADER, []) == [HEADER]


def test_preview_marks_cut_text():
    assert preview_text('коротко') == 'коротко'

    long_text = 'д' * 900
    preview = preview_text(long_text, limit=100)
    assert preview.endswith('...')
    assert len(preview) == 103


def test_bot_limit_matches_cabinet_and_webapi():
    """Ровно это расхождение и породило баг: бот резал 500, кабинет принимал 4000.

    Схемы — источник правды: пользователь пишет одно и то же сообщение из двух
    клиентов одной системы и должен получать одинаковый результат.
    """
    from app.cabinet.schemas.tickets import TicketCreateRequest, TicketMessageCreateRequest
    from app.webapi.schemas.tickets import TicketReplyRequest

    fields = [
        TicketCreateRequest.model_fields['message'],
        TicketMessageCreateRequest.model_fields['message'],
        TicketReplyRequest.model_fields['message_text'],
    ]
    schema_limits = {
        constraint.max_length
        for field in fields
        for constraint in field.metadata
        if getattr(constraint, 'max_length', None) is not None
    }

    assert schema_limits == {TICKET_MESSAGE_MAX_LENGTH}
