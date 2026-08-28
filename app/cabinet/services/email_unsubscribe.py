"""Отписка от маркетинговых писем (ссылка в футере + one-click по RFC 8058).

Отдельной таблицы нет: отписка выставляет те же флаги ``news_enabled`` /
``promo_offers_enabled`` в ``User.notification_settings``, которыми управляет
кабинет — иначе у пользователя было бы два несогласованных переключателя.

Токен — ``<user_id>.<category>.<hmac>``, где HMAC-SHA256 берётся от
``user_id:category:email`` на секрете кабинета. Адрес входит в подпись, но НЕ
в ссылку: он не утекает в логи прокси и в Referer, а смена почты автоматически
обесценивает старые ссылки. Срока жизни у токена нет — письмо могут открыть
через год, и отписка обязана сработать.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.constants import POSTGRES_INT4_MAX, POSTGRES_INT4_MIN


logger = structlog.get_logger(__name__)

# Путь публичного эндпоинта внутри API кабинета (см. app/cabinet/routes/unsubscribe.py).
DEFAULT_UNSUBSCRIBE_PATH = '/api/cabinet/public/unsubscribe'

# Категория рассылки → флаги в User.notification_settings.
# 'all' — то, что ставится в письмах: нажавший «отписаться» ждёт тишины, а не
# переключения одного из двух потоков.
CATEGORY_PREF_KEYS: dict[str, tuple[str, ...]] = {
    'news': ('news_enabled',),
    'promo': ('promo_offers_enabled',),
    'all': ('news_enabled', 'promo_offers_enabled'),
}


def _secret() -> str:
    """Секрет для подписи. Тот же, что у сессий кабинета."""
    return settings.get_cabinet_jwt_secret()


def _sign(user_id: int, category: str, email: str) -> str:
    payload = f'{user_id}:{category}:{email.strip().lower()}'.encode()
    digest = hmac.new(_secret().encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip('=')


def build_token(user_id: int, email: str, category: str = 'all') -> str:
    """Собирает токен отписки для конкретного адреса."""
    if category not in CATEGORY_PREF_KEYS:
        category = 'all'
    return f'{user_id}.{category}.{_sign(user_id, category, email)}'


def parse_token(token: str) -> tuple[int, str] | None:
    """Достаёт (user_id, category) БЕЗ проверки подписи.

    Подпись проверить нечем, пока не известен текущий email пользователя, —
    поэтому разбор и проверка разделены. Мусор отдаёт None, а не исключение:
    эндпоинт публичный, туда прилетает что угодно.
    """
    parts = (token or '').split('.')
    if len(parts) != 3:
        return None
    raw_id, category, signature = parts
    # isdigit() пропускает и не-ASCII цифры («١٢٣» → 123), и строку любой длины.
    # users.id — int4, и db.get с числом вне диапазона роняет asyncpg, а эндпоинт
    # публичный: подобранный токен вернул бы 500 вместо честного «не сработало».
    if not raw_id.isascii() or not raw_id.isdigit() or not signature or category not in CATEGORY_PREF_KEYS:
        return None
    user_id = int(raw_id)
    if user_id < POSTGRES_INT4_MIN or user_id > POSTGRES_INT4_MAX:
        return None
    return user_id, category


def verify_token(token: str, email: str) -> bool:
    """Проверяет подпись токена против текущего адреса пользователя."""
    parsed = parse_token(token)
    if not parsed or not email:
        return False
    user_id, category = parsed
    expected = _sign(user_id, category, email)
    return hmac.compare_digest(token.split('.')[2], expected)


def build_unsubscribe_url(user_id: int, email: str, category: str = 'all') -> str:
    """Публичная ссылка отписки. Пустая строка = отписки в этом письме не будет."""
    if not getattr(settings, 'EMAIL_UNSUBSCRIBE_ENABLED', True):
        return ''
    if not user_id or not email:
        return ''

    base = (getattr(settings, 'EMAIL_UNSUBSCRIBE_BASE_URL', '') or '').strip()
    if not base:
        cabinet_url = (getattr(settings, 'CABINET_URL', '') or '').strip()
        if not cabinet_url:
            return ''
        base = f'{cabinet_url.rstrip("/")}{DEFAULT_UNSUBSCRIBE_PATH}'

    return f'{base.rstrip("/")}?token={build_token(user_id, email, category)}'


def build_unsubscribe_mailto() -> str:
    """mailto-вариант для клиентов без HTTP one-click. Пусто, если не настроен.

    Значение попадает в заголовок List-Unsubscribe рядом с URL, поэтому проходит
    ту же проверку: перенос строки или угловая скобка внутри дописали бы в письмо
    произвольный заголовок. Кривой адрес выбрасываем целиком, а не чиним.
    """
    address = (getattr(settings, 'EMAIL_UNSUBSCRIBE_MAILTO', '') or '').strip()
    if not address:
        return ''
    if any(ch in address for ch in '\r\n<>,') or '@' not in address:
        logger.warning('Некорректный EMAIL_UNSUBSCRIBE_MAILTO — mailto в заголовок не добавлен')
        return ''
    return f'mailto:{address}?subject=unsubscribe'


async def apply_unsubscribe(db: AsyncSession, token: str) -> bool:
    """Выключает маркетинговые рассылки по токену.

    Идемпотентна: повторный запрос по той же ссылке — снова True. Так и должно
    быть, иначе антивирус-прескан почты, дёрнувший ссылку до пользователя,
    показал бы ему ошибку.
    """
    from app.database.models import User

    parsed = parse_token(token)
    if not parsed:
        return False
    user_id, category = parsed

    user = await db.get(User, user_id)
    if not user or not user.email or not verify_token(token, user.email):
        logger.warning('Отписка: токен не прошёл проверку', user_id=user_id)
        return False

    current = dict(getattr(user, 'notification_settings', None) or {})
    for key in CATEGORY_PREF_KEYS[category]:
        current[key] = False
    # Присваиваем новый dict целиком: мутация на месте не помечает JSONB грязным.
    user.notification_settings = current
    await db.commit()

    logger.info('Пользователь отписался от рассылок', user_id=user_id, category=category)
    return True
