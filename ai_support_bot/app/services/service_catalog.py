import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import text

from ai_support_bot.app.db.database import get_main_session
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.alerting import alert_admins


logger = structlog.get_logger(__name__)

_MAX_TARIFFS = 8
_MAX_PROMOCODES = 8
_MAX_PROMO_GROUPS = 6
_MAX_OFFER_TEMPLATES = 5


class _CatalogCache:
    def __init__(self) -> None:
        self._value: str = ''
        self._created: float = 0.0
        self._lock = asyncio.Lock()

    def get(self, ttl: int) -> str | None:
        if not self._created:
            return None
        if ttl > 0 and (time.time() - self._created) > ttl:
            return None
        return self._value

    def set(self, value: str) -> None:
        self._value = value
        self._created = time.time()

    def clear(self) -> None:
        self._value = ''
        self._created = 0.0

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock


_cache = _CatalogCache()


def _money(kopeks: Any) -> str:
    try:
        value = int(kopeks or 0) / 100
    except (TypeError, ValueError):
        return '0 ₽'
    if value == int(value):
        return f'{int(value)} ₽'
    return f'{value:.2f} ₽'


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
    return None


def _fmt_date(value: Any) -> str:
    moment = _as_datetime(value)
    if moment is not None:
        return moment.strftime('%d.%m.%Y')
    return str(value) if value else ''


def _days_left(value: Any) -> int | None:
    moment = _as_datetime(value)
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int((moment - datetime.now(timezone.utc)).total_seconds() // 86400)


def _period_summary(period_prices: Any) -> str:
    prices = _as_dict(period_prices)
    if not prices:
        return ''
    pairs: list[tuple[int, int]] = []
    for raw_days, raw_price in prices.items():
        try:
            days = int(raw_days)
            price = int(raw_price)
        except (TypeError, ValueError):
            continue
        if days > 0 and price >= 0:
            pairs.append((days, price))
    if not pairs:
        return ''
    pairs.sort()
    return ', '.join(f'{days} дн. — {_money(price)}' for days, price in pairs[:5])


def _traffic_summary(limit_gb: Any) -> str:
    try:
        limit = int(limit_gb or 0)
    except (TypeError, ValueError):
        limit = 0
    return 'безлимит' if limit <= 0 else f'{limit} ГБ'


async def _load_tariffs(session) -> list[str]:
    result = await session.execute(
        text(
            'SELECT name, traffic_limit_gb, device_limit, period_prices, is_daily, daily_price_kopeks, '
            'custom_days_enabled, price_per_day_kopeks, traffic_topup_enabled '
            'FROM tariffs WHERE is_active = true ORDER BY display_order ASC, id ASC LIMIT :limit'
        ),
        {'limit': _MAX_TARIFFS},
    )
    rows = result.mappings().all()
    if not rows:
        return []

    lines = ['Активные тарифы:']
    for row in rows:
        parts = [f'трафик {_traffic_summary(row.get("traffic_limit_gb"))}']
        device_limit = row.get('device_limit')
        if device_limit:
            parts.append(f'устройств {device_limit}')
        periods = _period_summary(row.get('period_prices'))
        if periods:
            parts.append(f'периоды: {periods}')
        if row.get('is_daily') and row.get('daily_price_kopeks'):
            parts.append(f'суточный: {_money(row.get("daily_price_kopeks"))}/день')
        if row.get('custom_days_enabled') and row.get('price_per_day_kopeks'):
            parts.append(f'произвольный срок: {_money(row.get("price_per_day_kopeks"))}/день')
        if row.get('traffic_topup_enabled'):
            parts.append('можно докупать трафик')
        lines.append(f'  • {row.get("name")}: ' + '; '.join(parts))
    return lines


async def _load_promocodes(session) -> list[str]:
    result = await session.execute(
        text(
            'SELECT code, type, balance_bonus_kopeks, subscription_days, max_uses, current_uses, '
            'valid_until, first_purchase_only '
            'FROM promocodes WHERE is_active = true '
            'AND (valid_from IS NULL OR valid_from <= :now) '
            'AND (valid_until IS NULL OR valid_until >= :now) '
            'AND (max_uses IS NULL OR max_uses <= 0 OR current_uses < max_uses) '
            'ORDER BY valid_until ASC NULLS LAST, id DESC LIMIT :limit'
        ),
        {'now': datetime.now(timezone.utc), 'limit': _MAX_PROMOCODES},
    )
    rows = result.mappings().all()
    if not rows:
        return ['Активных промокодов сейчас нет.']

    lines = ['Активные промокоды:']
    for row in rows:
        parts: list[str] = []
        bonus = row.get('balance_bonus_kopeks') or 0
        if bonus:
            parts.append(f'бонус на баланс {_money(bonus)}')
        days = row.get('subscription_days') or 0
        if days:
            parts.append(f'+{days} дн. подписки')
        max_uses = row.get('max_uses') or 0
        if max_uses > 0:
            left = max(max_uses - int(row.get('current_uses') or 0), 0)
            parts.append(f'осталось активаций: {left}')
        valid_until = row.get('valid_until')
        if valid_until:
            parts.append(f'до {_fmt_date(valid_until)}')
        if row.get('first_purchase_only'):
            parts.append('только для первой покупки')
        if not parts:
            parts.append(f'тип: {row.get("type")}')
        lines.append(f'  • {row.get("code")}: ' + '; '.join(parts))
    return lines


async def _load_promo_groups(session) -> list[str]:
    result = await session.execute(
        text(
            'SELECT name, server_discount_percent, traffic_discount_percent, device_discount_percent, '
            'period_discounts, auto_assign_total_spent_kopeks, is_default '
            'FROM promo_groups ORDER BY priority DESC, id ASC LIMIT :limit'
        ),
        {'limit': _MAX_PROMO_GROUPS},
    )
    rows = result.mappings().all()
    if not rows:
        return []

    lines = ['Скидочные промогруппы (накопительные скидки за траты):']
    for row in rows:
        parts: list[str] = []
        for label, key in (
            ('серверы', 'server_discount_percent'),
            ('трафик', 'traffic_discount_percent'),
            ('устройства', 'device_discount_percent'),
        ):
            percent = int(row.get(key) or 0)
            if percent:
                parts.append(f'{label} −{percent}%')
        period_discounts = _as_dict(row.get('period_discounts'))
        if period_discounts:
            rendered: list[str] = []
            for raw_days, raw_percent in list(period_discounts.items())[:4]:
                try:
                    rendered.append(f'{int(raw_days)} дн. −{int(raw_percent)}%')
                except (TypeError, ValueError):
                    continue
            if rendered:
                parts.append('периоды: ' + ', '.join(rendered))
        threshold = row.get('auto_assign_total_spent_kopeks')
        if threshold:
            parts.append(f'автоназначение от {_money(threshold)} трат')
        if row.get('is_default'):
            parts.append('группа по умолчанию')
        if not parts:
            parts.append('без скидок')
        lines.append(f'  • {row.get("name")}: ' + '; '.join(parts))
    return lines


async def _load_offer_templates(session) -> list[str]:
    result = await session.execute(
        text(
            'SELECT name, offer_type, discount_percent, bonus_amount_kopeks, valid_hours '
            'FROM promo_offer_templates WHERE is_active = true ORDER BY id ASC LIMIT :limit'
        ),
        {'limit': _MAX_OFFER_TEMPLATES},
    )
    rows = result.mappings().all()
    if not rows:
        return []

    lines = ['Действующие промо-предложения (высылаются точечно, не всем):']
    for row in rows:
        parts: list[str] = []
        percent = int(row.get('discount_percent') or 0)
        if percent:
            parts.append(f'скидка {percent}%')
        bonus = row.get('bonus_amount_kopeks') or 0
        if bonus:
            parts.append(f'бонус {_money(bonus)}')
        hours = row.get('valid_hours') or 0
        if hours:
            parts.append(f'срок действия {hours} ч.')
        if not parts:
            parts.append(f'тип: {row.get("offer_type")}')
        lines.append(f'  • {row.get("name")}: ' + '; '.join(parts))
    return lines


async def _collect() -> str:
    session = await get_main_session()
    if session is None:
        return ''

    blocks: list[list[str]] = []
    try:
        for loader in (_load_tariffs, _load_promocodes, _load_promo_groups, _load_offer_templates):
            try:
                blocks.append(await loader(session))
            except Exception as error:
                logger.warning('Service catalog section failed', section=loader.__name__, error=str(error))
    except Exception as error:
        await alert_admins(
            'main_db_service_catalog',
            'не читается каталог тарифов и промокодов из основной БД',
            f'{type(error).__name__}: {error}',
        )
        return ''
    finally:
        await session.close()

    lines = [line for block in blocks for line in block]
    return '\n'.join(lines)


async def build_service_catalog() -> str:
    if not settings_store.get_bool('SERVICE_CATALOG_ENABLED'):
        return ''

    ttl = settings_store.get_int('SERVICE_CATALOG_TTL') or 0
    cached = _cache.get(ttl)
    if cached is not None:
        return cached

    async with _cache.lock:
        cached = _cache.get(ttl)
        if cached is not None:
            return cached
        try:
            body = await _collect()
        except Exception as error:
            logger.warning('Service catalog build failed', error=str(error))
            return ''
        _cache.set(body)
        return body


async def build_user_offers(telegram_id: int) -> str:
    if not settings_store.get_bool('SERVICE_CATALOG_ENABLED'):
        return ''

    session = await get_main_session()
    if session is None:
        return ''

    try:
        result = await session.execute(
            text(
                'SELECT o.notification_type, o.discount_percent, o.bonus_amount_kopeks, o.effect_type, '
                'o.expires_at FROM discount_offers o JOIN users u ON u.id = o.user_id '
                'WHERE u.telegram_id = :tid AND o.is_active = true AND o.claimed_at IS NULL '
                'AND o.expires_at >= :now ORDER BY o.expires_at ASC LIMIT 5'
            ),
            {'tid': telegram_id, 'now': datetime.now(timezone.utc)},
        )
        rows = result.mappings().all()
    except Exception as error:
        logger.warning('User discount offers unavailable', error=str(error))
        return ''
    finally:
        await session.close()

    if not rows:
        return ''

    lines = ['Персональные активные предложения этого пользователя:']
    for row in rows:
        parts: list[str] = []
        percent = int(row.get('discount_percent') or 0)
        if percent:
            parts.append(f'скидка {percent}%')
        bonus = row.get('bonus_amount_kopeks') or 0
        if bonus:
            parts.append(f'бонус {_money(bonus)}')
        left = _days_left(row.get('expires_at'))
        if left is not None:
            parts.append(f'действует до {_fmt_date(row.get("expires_at"))} (осталось {max(left, 0)} дн.)')
        if not parts:
            parts.append(f'тип: {row.get("effect_type")}')
        lines.append(f'  • {row.get("notification_type")}: ' + '; '.join(parts))
    return '\n'.join(lines)


def invalidate_cache() -> None:
    _cache.clear()
