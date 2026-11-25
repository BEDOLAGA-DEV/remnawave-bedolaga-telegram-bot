import re
import time

import structlog

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.services import hub_events, settings_store


logger = structlog.get_logger(__name__)

_last_sent: dict[str, float] = {}
_bot_ref = None


def register_bot(bot) -> None:
    global _bot_ref
    _bot_ref = bot


def _escape(value: str) -> str:
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _throttled(key: str) -> bool:
    window = settings_store.get_int('ALERT_THROTTLE_SECONDS') or 900
    now = time.time()
    last = _last_sent.get(key, 0.0)
    if now - last < window:
        return True
    _last_sent[key] = now
    if len(_last_sent) > 200:
        for stale_key in [k for k, ts in _last_sent.items() if now - ts > window * 4]:
            _last_sent.pop(stale_key, None)
    return False


_DEFAULT_HINT = 'Бот продолжает отвечать, но без части данных — проверьте логи и схему БД.'

_SQL_TAIL_RE = re.compile(r'\[(?:SQL|parameters):.*', re.DOTALL)
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_LONG_NUMBER_RE = re.compile(r'\d{5,}')


def _without_client_data(details: str) -> str:
    """Текст сбоя для внешнего журнала: без SQL с параметрами, почты и длинных чисел (ID клиентов)."""
    text = _SQL_TAIL_RE.sub('', details or '')
    text = _EMAIL_RE.sub('[email]', text)
    text = _LONG_NUMBER_RE.sub('[id]', text)
    return text.strip()[:300]


async def alert_admins(key: str, title: str, details: str, hint: str | None = None) -> None:
    logger.error('degradation alert', alert_key=key, title=title, details=details)

    if not settings_store.get_bool('ALERT_ADMINS_ON_FAILURE'):
        return
    if _bot_ref is None or not settings.admin_ids:
        return
    if _throttled(key):
        return

    hub_events.emit(
        'support.error',
        title=title,
        text='\n'.join(part for part in (_without_client_data(details), hint or _DEFAULT_HINT) if part),
        severity='warn',
        silent=True,
    )

    message = (
        f'🛑 <b>Сбой ИИ-поддержки: {_escape(title)}</b>\n\n'
        f'<code>{_escape(details[:900])}</code>\n\n'
        f'{_escape(hint or _DEFAULT_HINT)}'
    )
    for admin_id in settings.admin_ids:
        try:
            await _bot_ref.send_message(admin_id, message, parse_mode='HTML')
        except Exception as error:
            logger.warning('Failed to deliver degradation alert', admin_id=admin_id, error=str(error))
