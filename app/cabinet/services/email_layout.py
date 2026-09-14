"""
Общая обёртка писем (layout): один редактируемый HTML-каркас для всех писем.

Тексты писем редактировались, а обёртка — шапка с названием сервиса, цвета,
подвал — была зашита в код; поменять её можно было только вставив полный HTML
в каждый из 50 типов на каждом из 5 языков. Теперь обёртка хранится как
псевдо-тип ``email_layout`` в той же таблице email_templates и редактируется в
том же редакторе. Плейсхолдер ``{content}`` обязателен — в него встаёт тело
письма; без сохранённой обёртки работает встроенная DEFAULT_EMAIL_LAYOUT.

Рендер писем синхронный, а хранилище асинхронное, поэтому обёртка живёт в
кэше процесса: его обновляет каждый чокпойнт отправки (get_rendered_override)
не чаще раза в LAYOUT_CACHE_TTL_SECONDS и сразу — сохранение в редакторе.
Ошибка базы — это «обёртки нет», а не потерянное письмо.
"""

import html
import time
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)

EMAIL_LAYOUT_TYPE = 'email_layout'
CONTENT_PLACEHOLDER = '{content}'
LAYOUT_CACHE_TTL_SECONDS = 60

# Плейсхолдеры обёртки в порядке подстановки: content первым, чтобы переменные
# внутри вставленного письма ({cabinet_url} и т.п.) тоже подставились.
LAYOUT_CONTEXT_VARS = [
    'content',
    'service_name',
    'footer_text',
    'unsubscribe_block',
    'unsubscribe_url',
    'cabinet_url',
    'support_username',
    # Данные получателя: рендер письма передаёт их в обёртку вместе с телом.
    'username',
    'email',
    'date',
]
# HTML-значения — не экранируем.
_RAW_VARS = frozenset({'content', 'unsubscribe_block'})

# Маркеры вокруг тела письма: по ним редактор достаёт фрагмент из готового
# письма независимо от того, как выглядит обёртка.
CONTENT_START = '<!-- email-content-start -->'
CONTENT_END = '<!-- email-content-end -->'

FOOTER_TEXTS = {
    'ru': 'Это автоматическое сообщение. Пожалуйста, не отвечайте на это письмо.',
    'en': 'This is an automated message. Please do not reply to this email.',
    'zh': '这是一封自动发送的邮件，请勿回复。',
    'ua': 'Це автоматичне повідомлення. Будь ласка, не відповідайте на цей лист.',
    'fa': 'این یک پیام خودکار است. لطفاً به این ایمیل پاسخ ندهید.',
}

UNSUBSCRIBE_TEXTS = {
    'ru': 'Отписаться от рассылок',
    'en': 'Unsubscribe from marketing emails',
    'zh': '退订营销邮件',
    'ua': 'Відписатися від розсилок',
    'fa': 'لغو اشتراک ایمیل‌های تبلیغاتی',
}

DEFAULT_EMAIL_LAYOUT = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            background-color: #f5f5f5;
            margin: 0;
            padding: 0;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
            background-color: #ffffff;
        }
        .header {
            text-align: center;
            padding: 20px 0;
            border-bottom: 2px solid #007bff;
        }
        .header h1 {
            color: #007bff;
            margin: 0;
            font-size: 24px;
        }
        .content {
            padding: 30px 20px;
        }
        .highlight {
            background-color: #f8f9fa;
            border-left: 4px solid #007bff;
            padding: 15px;
            margin: 20px 0;
        }
        .success {
            border-left-color: #28a745;
        }
        .warning {
            border-left-color: #ffc107;
        }
        .danger {
            border-left-color: #dc3545;
        }
        .button {
            display: inline-block;
            padding: 12px 24px;
            background-color: #007bff;
            color: white !important;
            text-decoration: none;
            border-radius: 5px;
            margin: 20px 0;
            font-weight: bold;
        }
        .button:hover {
            background-color: #0056b3;
        }
        .footer {
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #eee;
            font-size: 12px;
            color: #666;
            text-align: center;
        }
        .amount {
            font-size: 24px;
            font-weight: bold;
            color: #28a745;
        }
        .amount.negative {
            color: #dc3545;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{service_name}</h1>
        </div>
        <div class="content">
            {content}
        </div>
        <div class="footer">
            <p>&copy; {service_name}</p>
            <p>{footer_text}</p>
            {unsubscribe_block}
        </div>
    </div>
</body>
</html>"""


def layout_is_valid(layout_html: str) -> bool:
    """Обёртка без {content} проглотила бы тело каждого письма."""
    return CONTENT_PLACEHOLDER in (layout_html or '')


def unsubscribe_block(unsubscribe_url: str, language: str) -> str:
    """Ссылка отписки — только у маркетинговых писем, у остальных пусто."""
    if not unsubscribe_url:
        return ''
    label = UNSUBSCRIBE_TEXTS.get(language, UNSUBSCRIBE_TEXTS['ru'])
    return f'<p><a href="{html.escape(unsubscribe_url, quote=True)}" style="color: #666;">{label}</a></p>'


def build_layout_context(language: str, *, content: str = '', unsubscribe_url: str = '') -> dict[str, str]:
    """Реальные значения плейсхолдеров обёртки для языка.

    Данные получателя здесь пустые — их подмешивает рендер письма; пустая
    строка гарантирует, что литерал {username} не уйдёт в письмо.
    """
    from datetime import UTC, datetime

    from app.utils.timezone import format_email_datetime

    return {
        'content': content,
        'service_name': settings.SMTP_FROM_NAME or 'VPN Service',
        'footer_text': FOOTER_TEXTS.get(language, FOOTER_TEXTS['ru']),
        'unsubscribe_block': unsubscribe_block(unsubscribe_url, language),
        # Голая ссылка — для своего текста отписки вместо готового блока.
        'unsubscribe_url': unsubscribe_url or '',
        'cabinet_url': getattr(settings, 'CABINET_URL', '') or '',
        'support_username': getattr(settings, 'SUPPORT_USERNAME', '') or '',
        'username': '',
        'email': '',
        'date': format_email_datetime(datetime.now(UTC), fmt='%d.%m.%Y'),
    }


def render_email_layout(
    layout_html: str,
    language: str,
    context: dict[str, Any] | None = None,
    *,
    mark_content: bool = True,
) -> str:
    """Подставляет плейсхолдеры обёртки. Недостающие берутся из build_layout_context.

    ``content`` и ``unsubscribe_block`` — HTML, остальное экранируется.
    ``mark_content`` обрамляет тело маркерами для extract_content_fragment;
    редактор обёртки получает дефолт без них.
    """
    given = context or {}
    values = {**build_layout_context(language, unsubscribe_url=str(given.get('unsubscribe_url') or '')), **given}
    result = layout_html
    for key in LAYOUT_CONTEXT_VARS:
        raw = values.get(key)
        value = '' if raw is None else str(raw)
        if key == 'content' and mark_content:
            value = f'{CONTENT_START}{value}{CONTENT_END}'
        elif key not in _RAW_VARS:
            value = html.escape(value)
        result = result.replace(f'{{{key}}}', value)
    return result


def extract_content_fragment(rendered_html: str) -> str | None:
    """Тело письма из готового HTML по маркерам, None если маркеров нет."""
    start = rendered_html.find(CONTENT_START)
    end = rendered_html.find(CONTENT_END)
    if start < 0 or end < 0 or end < start:
        return None
    return rendered_html[start + len(CONTENT_START) : end].strip()


# ============ Кэш сохранённой обёртки ============

_cache: dict[str, str] = {}
_loaded_at: float | None = None


def _monotonic() -> float:
    return time.monotonic()


def reset_email_layout_cache() -> None:
    global _cache, _loaded_at
    _cache = {}
    _loaded_at = None


def set_cached_email_layouts(layouts: dict[str, str]) -> None:
    """Заменяет кэш; обёртки без {content} отбрасываются с предупреждением."""
    global _cache, _loaded_at
    valid = {lang: body for lang, body in layouts.items() if layout_is_valid(body)}
    for lang in set(layouts) - set(valid):
        logger.warning('Сохранённая обёртка писем без {content} пропущена', language=lang)
    _cache = valid
    _loaded_at = _monotonic()


def get_cached_email_layout(language: str) -> str | None:
    """Обёртка для языка; нет своей — русская; нет и её — None (встроенная)."""
    return _cache.get(language) or _cache.get('ru')


def resolve_email_layout(language: str) -> str:
    return get_cached_email_layout(language) or DEFAULT_EMAIL_LAYOUT


def is_layout_cache_stale() -> bool:
    return _loaded_at is None or _monotonic() - _loaded_at > LAYOUT_CACHE_TTL_SECONDS


async def _load_layouts(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(
        text(
            'SELECT language, body_html FROM email_templates WHERE notification_type = :ntype AND is_active = :active'
        ),
        {'ntype': EMAIL_LAYOUT_TYPE, 'active': True},
    )
    return {row[0]: row[1] for row in result.fetchall()}


async def refresh_email_layout_cache(db: AsyncSession | None = None, *, force: bool = False) -> None:
    """Подтягивает сохранённые обёртки, если кэш устарел (или force после сохранения)."""
    global _loaded_at
    if not force and not is_layout_cache_stale():
        return
    try:
        if db is not None:
            layouts = await _load_layouts(db)
        else:
            async with AsyncSessionLocal() as session:
                layouts = await _load_layouts(session)
    except Exception as e:
        # Старый кэш остаётся, но время загрузки обновляем — иначе каждое письмо
        # заново долбило бы недоступную базу.
        logger.warning('Не удалось загрузить обёртку писем — остаётся текущая', error=e)
        _loaded_at = _monotonic()
        return
    set_cached_email_layouts(layouts)
