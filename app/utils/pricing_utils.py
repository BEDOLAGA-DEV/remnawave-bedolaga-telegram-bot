from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


if TYPE_CHECKING:  # pragma: no cover
    from app.database.models import PromoGroup, User

logger = structlog.get_logger(__name__)


def calculate_months_from_days(days: int) -> int:
    return max(1, round(days / 30))


def calculate_prorated_price(monthly_price: int, end_date: datetime, min_charge_days: int = 30) -> tuple[int, int]:
    """Calculate prorated price based on remaining days.

    Returns:
        tuple of (total_price_kopeks, days_charged)
    """
    now = datetime.now(UTC)
    days_remaining = max(1, (end_date - now).days)
    days_to_charge = max(min_charge_days, days_remaining)

    total_price = monthly_price * days_to_charge // 30
    if monthly_price > 0:
        total_price = max(100, total_price)  # Минимум 1 рубль

    logger.debug(
        'Расчет пропорциональной цены: ₽/мес × дн./30 = ₽',
        monthly_price=monthly_price / 100,
        days_to_charge=days_to_charge,
        total_price=total_price / 100,
    )

    return total_price, days_to_charge


def apply_percentage_discount(amount: int, percent: int) -> tuple[int, int]:
    """Apply percentage discount using PricingEngine's floor division.

    Returns (discounted_amount, discount_value).
    """
    from app.services.pricing_engine import PricingEngine

    if amount <= 0 or percent <= 0:
        return amount, 0

    discounted = PricingEngine.apply_discount(amount, percent)
    return discounted, amount - discounted


def resolve_discount_percent(
    user: Optional['User'],
    promo_group: Optional['PromoGroup'],
    category: str,
    *,
    period_days: int | None = None,
) -> int:
    """Определяет размер скидки для указанной категории."""

    if user is not None:
        try:
            return user.get_promo_discount(category, period_days)
        except AttributeError:  # pragma: no cover - defensive guard
            pass

    if promo_group is not None:
        return promo_group.get_discount_percent(category, period_days)

    return 0


async def compute_simple_subscription_price(
    db: AsyncSession,
    params: dict[str, Any],
    *,
    user: Optional['User'] = None,
    resolved_squad_uuids: Sequence[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Вычисляет стоимость простой подписки с учетом всех доплат и скидок."""

    period_days = int(params.get('period_days', 30) or 30)
    attr_name = f'PRICE_{period_days}_DAYS'
    base_price_original = getattr(settings, attr_name, settings.BASE_SUBSCRIPTION_PRICE)

    traffic_limit_raw = params.get('traffic_limit_gb')
    try:
        traffic_limit = int(traffic_limit_raw) if traffic_limit_raw is not None else None
    except (TypeError, ValueError):  # pragma: no cover - defensive conversion
        traffic_limit = None

    if traffic_limit is None or traffic_limit <= 0:
        # Default simple subscriptions already include unlimited traffic.
        traffic_price_original = 0
    else:
        traffic_price_original = settings.get_traffic_price(traffic_limit)

    device_limit_raw = params.get('device_limit', settings.DEFAULT_DEVICE_LIMIT)
    try:
        device_limit = int(device_limit_raw)
    except (TypeError, ValueError):  # pragma: no cover - defensive conversion
        device_limit = settings.DEFAULT_DEVICE_LIMIT
    additional_devices = max(0, device_limit - settings.DEFAULT_DEVICE_LIMIT)
    devices_price_original = additional_devices * settings.PRICE_PER_DEVICE

    promo_group: PromoGroup | None = params.get('promo_group')

    if promo_group is None:
        promo_group_id = params.get('promo_group_id')
        if promo_group_id:
            from app.database.crud.promo_group import get_promo_group_by_id

            promo_group = await get_promo_group_by_id(db, int(promo_group_id))

    if promo_group is None and user is not None:
        promo_group = user.get_primary_promo_group()

    period_discount_percent = resolve_discount_percent(
        user,
        promo_group,
        'period',
        period_days=period_days,
    )
    base_discount = base_price_original * period_discount_percent // 100

    traffic_discount_percent = resolve_discount_percent(
        user,
        promo_group,
        'traffic',
        period_days=period_days,
    )
    traffic_discount = traffic_price_original * traffic_discount_percent // 100

    devices_discount_percent = resolve_discount_percent(
        user,
        promo_group,
        'devices',
        period_days=period_days,
    )
    devices_discount = devices_price_original * devices_discount_percent // 100

    servers_discount_percent = resolve_discount_percent(
        user,
        promo_group,
        'servers',
        period_days=period_days,
    )

    resolved_uuids: list[str] = []
    if resolved_squad_uuids:
        resolved_uuids.extend([uuid for uuid in resolved_squad_uuids if uuid])
    else:
        raw_squad = params.get('squad_uuid')
        if isinstance(raw_squad, (list, tuple, set)):
            resolved_uuids.extend([str(uuid) for uuid in raw_squad if uuid])
        elif raw_squad:
            resolved_uuids.append(str(raw_squad))

    from app.database.crud.server_squad import get_server_squads_by_uuids

    server_breakdown: list[dict[str, Any]] = []
    servers_price_original = 0
    servers_discount_total = 0

    if resolved_uuids:
        servers = await get_server_squads_by_uuids(db, resolved_uuids)
        server_map = {s.squad_uuid: s for s in servers}
    else:
        server_map = {}

    for squad_uuid in resolved_uuids:
        server = server_map.get(squad_uuid)
        if not server:
            logger.warning('SIMPLE_SUBSCRIPTION_PRICE_SERVER_NOT_FOUND | squad', squad_uuid=squad_uuid)
            server_breakdown.append(
                {
                    'uuid': squad_uuid,
                    'name': None,
                    'available': False,
                    'original_price': 0,
                    'discount': 0,
                    'final_price': 0,
                }
            )
            continue

        if not server.is_available or server.is_full:
            logger.warning(
                'SIMPLE_SUBSCRIPTION_PRICE_SERVER_UNAVAILABLE | squad= | available= | full',
                squad_uuid=squad_uuid,
                is_available=server.is_available,
                is_full=server.is_full,
            )
            server_breakdown.append(
                {
                    'uuid': squad_uuid,
                    'name': server.display_name,
                    'available': False,
                    'original_price': 0,
                    'discount': 0,
                    'final_price': 0,
                }
            )
            continue

        original_price = server.price_kopeks
        discount_value = original_price * servers_discount_percent // 100
        final_price = original_price - discount_value

        servers_price_original += original_price
        servers_discount_total += discount_value

        server_breakdown.append(
            {
                'uuid': squad_uuid,
                'name': server.display_name,
                'available': True,
                'original_price': original_price,
                'discount': discount_value,
                'final_price': final_price,
            }
        )

    total_before_discount = (
        base_price_original + traffic_price_original + devices_price_original + servers_price_original
    )

    total_discount = base_discount + traffic_discount + devices_discount + servers_discount_total

    total_price = max(0, total_before_discount - total_discount)

    breakdown = {
        'base_price': base_price_original,
        'base_discount': base_discount,
        'traffic_price': traffic_price_original,
        'traffic_discount': traffic_discount,
        'devices_price': devices_price_original,
        'devices_discount': devices_discount,
        'servers_price': servers_price_original,
        'servers_discount': servers_discount_total,
        'servers_final': sum(item['final_price'] for item in server_breakdown),
        'server_details': server_breakdown,
        'total_before_discount': total_before_discount,
        'total_discount': total_discount,
        'resolved_squad_uuids': resolved_uuids,
        'applied_promo_group_id': getattr(promo_group, 'id', None) if promo_group else None,
        'period_discount_percent': period_discount_percent,
        'traffic_discount_percent': traffic_discount_percent,
        'devices_discount_percent': devices_discount_percent,
        'servers_discount_percent': servers_discount_percent,
    }

    return total_price, breakdown


def _pluralize_days_ru(n: int) -> str:
    """Склонение слова 'день' по числу: 1 день, 2 дня, 5 дней."""
    mod100 = n % 100
    mod10 = n % 10
    if 11 <= mod100 <= 19:
        return 'дней'
    if mod10 == 1:
        return 'день'
    if 2 <= mod10 <= 4:
        return 'дня'
    return 'дней'


def format_period_description(days: int, language: str = 'ru') -> str:
    language_code = (language or 'ru').split('-')[0].lower()
    if language_code in {'ru', 'fa'}:
        if days == 30:
            return '1 месяц'
        if days == 60:
            return '2 месяца'
        if days == 90:
            return '3 месяца'
        if days == 180:
            return '6 месяцев'
        if days == 360:
            return '12 месяцев'
        return f'{days} {_pluralize_days_ru(days)}'

    if days == 30:
        return '1 month'
    if days == 60:
        return '2 months'
    if days == 90:
        return '3 months'
    if days == 180:
        return '6 months'
    if days == 360:
        return '12 months'
    day_word = 'day' if days == 1 else 'days'
    return f'{days} {day_word}'


def validate_pricing_calculation(base_price: int, monthly_additions: int, months: int, total_calculated: int) -> bool:
    expected_total = base_price + (monthly_additions * months)
    is_valid = expected_total == total_calculated

    if not is_valid:
        logger.warning(
            'Несоответствие в расчете цены: ожидалось ₽, получено ₽',
            expected_total=expected_total / 100,
            total_calculated=total_calculated / 100,
        )
        logger.warning(
            'Детали: базовая цена ₽ + месячные дополнения ₽ × мес',
            base_price=base_price / 100,
            monthly_additions=monthly_additions / 100,
            months=months,
        )

    return is_valid
