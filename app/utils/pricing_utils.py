from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


if TYPE_CHECKING:  # pragma: no cover
    from app.database.models import PromoGroup, User
    from app.services.pricing_engine import RenewalPricing

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
    """Вычисляет стоимость простой подписки с учетом всех доплат и скидок.

    Delegates to PricingEngine.calculate_classic_new_subscription_price()
    and converts the RenewalPricing result to the legacy breakdown dict
    expected by callers.
    """
    from app.services.pricing_engine import PricingEngine

    period_days = int(params.get('period_days', 30) or 30)

    traffic_limit_raw = params.get('traffic_limit_gb')
    try:
        traffic_limit_gb = int(traffic_limit_raw) if traffic_limit_raw is not None else 0
    except (TypeError, ValueError):  # pragma: no cover - defensive conversion
        traffic_limit_gb = 0
    # Treat None / non-positive as unlimited (0 GB → price = 0 in PricingEngine)
    traffic_limit_gb = max(traffic_limit_gb, 0)

    device_limit_raw = params.get('device_limit', settings.DEFAULT_DEVICE_LIMIT)
    try:
        device_limit = int(device_limit_raw)
    except (TypeError, ValueError):  # pragma: no cover - defensive conversion
        device_limit = settings.DEFAULT_DEVICE_LIMIT

    # --- Resolve squad UUIDs from explicit arg or params ---
    resolved_uuids: list[str] = []
    if resolved_squad_uuids:
        resolved_uuids.extend([uuid for uuid in resolved_squad_uuids if uuid])
    else:
        raw_squad = params.get('squad_uuid')
        if isinstance(raw_squad, (list, tuple, set)):
            resolved_uuids.extend([str(uuid) for uuid in raw_squad if uuid])
        elif raw_squad:
            resolved_uuids.append(str(raw_squad))

    # --- Resolve promo_group from params (backward compat) ---
    # Callers may pass promo_group or promo_group_id via params dict.
    # PricingEngine resolves promo_group from user internally, so we only
    # need this for the applied_promo_group_id field in the breakdown.
    promo_group: PromoGroup | None = params.get('promo_group')

    if promo_group is None:
        promo_group_id = params.get('promo_group_id')
        if promo_group_id:
            from app.database.crud.promo_group import get_promo_group_by_id

            promo_group = await get_promo_group_by_id(db, int(promo_group_id))

    if promo_group is None and user is not None:
        promo_group = user.get_primary_promo_group()

    # --- Delegate to PricingEngine ---
    engine = PricingEngine()
    pricing = await engine.calculate_classic_new_subscription_price(
        db,
        period_days,
        resolved_uuids,
        traffic_limit_gb,
        device_limit,
        user=user,
    )

    # --- Build legacy breakdown dict from RenewalPricing + ClassicBreakdown ---
    breakdown = _build_simple_subscription_breakdown(pricing, resolved_uuids, promo_group)

    return pricing.final_total, breakdown


def _build_simple_subscription_breakdown(
    pricing: 'RenewalPricing',
    resolved_uuids: list[str],
    promo_group: Optional['PromoGroup'],
) -> dict[str, Any]:
    """Convert PricingEngine's RenewalPricing to the legacy breakdown dict.

    Preserves all keys that callers depend on:
    base_price, base_discount, traffic_price, traffic_discount,
    devices_price, devices_discount, servers_price, servers_discount,
    servers_final, server_details, total_before_discount, total_discount,
    resolved_squad_uuids, applied_promo_group_id, *_discount_percent.
    """
    from app.services.pricing_engine import PricingEngine

    bd = pricing.breakdown
    months = bd.get('months_in_period', 1) or 1
    group_pct: dict[str, int] = bd.get('group_discount_pct', {})

    # Original (pre-discount) prices from ClassicBreakdown
    base_price_original: int = bd.get('base_price_original', 0)
    traffic_price_per_month: int = bd.get('traffic_price_per_month', 0)
    servers_price_per_month: int = bd.get('servers_price_per_month', 0)
    devices_price_per_month: int = bd.get('devices_price_per_month', 0)

    # Per-category discount percents
    period_discount_percent: int = group_pct.get('period', 0)
    traffic_discount_percent: int = group_pct.get('traffic', 0)
    servers_discount_percent: int = group_pct.get('servers', 0)
    devices_discount_percent: int = group_pct.get('devices', 0)

    # Total original prices (traffic/servers/devices are monthly × months)
    traffic_price_total = traffic_price_per_month * months
    servers_price_total = servers_price_per_month * months
    devices_price_total = devices_price_per_month * months

    # Discount values
    base_discount = base_price_original - pricing.base_price
    traffic_discount = traffic_price_total - pricing.traffic_price
    servers_discount = servers_price_total - pricing.servers_price
    devices_discount = devices_price_total - pricing.devices_price

    total_before_discount = base_price_original + traffic_price_total + servers_price_total + devices_price_total
    # Group discounts only (promo_offer_discount is separate and already
    # reflected in final_total but NOT in per-category values above).
    total_discount = base_discount + traffic_discount + servers_discount + devices_discount

    # Build server_details in legacy format from PricingEngine's server list
    server_details: list[dict[str, Any]] = []
    servers_final = 0
    for srv in bd.get('servers', []):
        original_price = srv.get('price', 0)
        status = srv.get('status', 'available')
        is_available = status == 'available'
        final_price = PricingEngine.apply_discount(original_price, servers_discount_percent) if is_available else 0
        discount_value = original_price - final_price if is_available else 0
        servers_final += final_price

        server_details.append(
            {
                'uuid': srv.get('uuid', ''),
                'name': None,  # PricingEngine._calculate_servers_price doesn't return display_name
                'available': is_available,
                'original_price': original_price if is_available else 0,
                'discount': discount_value,
                'final_price': final_price,
            }
        )

    return {
        'base_price': base_price_original,
        'base_discount': base_discount,
        'traffic_price': traffic_price_total,
        'traffic_discount': traffic_discount,
        'devices_price': devices_price_total,
        'devices_discount': devices_discount,
        'servers_price': servers_price_total,
        'servers_discount': servers_discount,
        'servers_final': servers_final,
        'server_details': server_details,
        'total_before_discount': total_before_discount,
        'total_discount': total_discount,
        'resolved_squad_uuids': resolved_uuids,
        'applied_promo_group_id': getattr(promo_group, 'id', None) if promo_group else None,
        'period_discount_percent': period_discount_percent,
        'traffic_discount_percent': traffic_discount_percent,
        'devices_discount_percent': devices_discount_percent,
        'servers_discount_percent': servers_discount_percent,
    }


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
