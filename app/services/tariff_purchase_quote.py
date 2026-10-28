"""Side-effect-free tariff purchase calculation for bot and cabinet checkout."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.services.pricing_engine import RenewalPricing, pricing_engine
from app.services.tariff_purchase_devices import PurchaseDeviceSelection, resolve_purchase_device_selection
from app.utils.cache import cache


MAX_PRICE_KOPEKS = 2_147_483_647
QUOTE_TTL_SECONDS = 300


class PurchaseQuoteError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def save_purchase_quote(
    user_id: int, channel: str, terms: dict[str, Any], *, now: datetime | None = None
) -> tuple[UUID, datetime]:
    """Cache consent terms, never a substitute for durable checkout identity."""
    quote_id = uuid4()
    expires_at = (now or datetime.now(UTC)) + timedelta(seconds=QUOTE_TTL_SECONDS)
    saved = await cache.set(
        f'tariff_purchase_quote:{user_id}:{channel}:{quote_id}',
        {'version': 1, 'expires_at': expires_at.isoformat(), 'terms': terms},
        expire=QUOTE_TTL_SECONDS,
    )
    if not saved:
        raise PurchaseQuoteError('quote_unavailable')
    return quote_id, expires_at


async def load_purchase_quote(user_id: int, channel: str, quote_id: UUID) -> dict[str, Any]:
    value = await cache.get(f'tariff_purchase_quote:{user_id}:{channel}:{quote_id}')
    try:
        if not isinstance(value, dict) or value.get('version') != 1:
            raise ValueError
        if datetime.fromisoformat(value['expires_at']) <= datetime.now(UTC):
            raise ValueError
        if not isinstance(value['terms'], dict):
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise PurchaseQuoteError('quote_expired') from error
    return value['terms']


@dataclass(frozen=True, slots=True)
class PurchaseQuote:
    devices: PurchaseDeviceSelection
    pricing: RenewalPricing
    purchase_kind: str
    traffic_limit_gb: int
    custom_traffic_gb: int | None
    next_daily_charge_kopeks: int | None = None


def purchase_parameters(request) -> dict[str, Any]:
    """Exclude transport IDs from the immutable identity of the order."""
    return {
        key: getattr(request, key, None)
        for key in ('tariff_id', 'period_days', 'traffic_gb', 'subscription_id', 'selected_device_limit')
    }


def purchase_fingerprint(parameters: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(parameters, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def purchase_quote_terms(request, target, quote: PurchaseQuote) -> dict[str, Any]:
    return {
        'request': purchase_parameters(request),
        'subscription_id': target.id if target else None,
        'purchase_kind': quote.purchase_kind,
        'device_selection': asdict(quote.devices),
        'traffic_limit_gb': quote.traffic_limit_gb,
        'pricing': asdict(quote.pricing),
        'next_daily_charge_kopeks': quote.next_daily_charge_kopeks,
    }


def validate_purchase_duration(period_days: int, subscription, *, now: datetime | None = None) -> None:
    """Reject unrepresentable expiry before any debit, including legacy requests."""
    if type(period_days) is not int or period_days < 1:
        raise PurchaseQuoteError('invalid_period')
    moment = now or datetime.now(UTC)
    end_date = getattr(subscription, 'end_date', None)
    if end_date is not None:
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=UTC)
        moment = max(moment, end_date)
    try:
        moment + timedelta(days=period_days)
    except (OverflowError, ValueError) as error:
        raise PurchaseQuoteError('period_out_of_range') from error


async def calculate_purchase_quote(
    tariff,
    user,
    subscription,
    *,
    period_days: int,
    selected_device_limit: int | None = None,
    traffic_gb: int | None = None,
    now: datetime | None = None,
) -> PurchaseQuote:
    """Calculate validated terms; callers enforce availability and target ownership.

    No database, wallet or panel writes are performed. Confirmation must resolve
    the target again under the wallet lock and compare the agreed terms.
    """
    if type(period_days) is not int or period_days < 1:
        raise PurchaseQuoteError('invalid_period')
    if tariff.is_daily and period_days != 1:
        raise PurchaseQuoteError('invalid_period')
    if not tariff.has_configured_price_for_period(period_days):
        raise PurchaseQuoteError('invalid_period')
    validate_purchase_duration(period_days, subscription, now=now)

    devices = resolve_purchase_device_selection(tariff, subscription, selected_device_limit)
    custom_traffic = None
    traffic_limit = tariff.traffic_limit_gb
    if traffic_gb is not None:
        if (
            type(traffic_gb) is not int
            or not tariff.can_purchase_custom_traffic()
            or not tariff.min_traffic_gb <= traffic_gb <= tariff.max_traffic_gb
            or tariff.get_price_for_custom_traffic(traffic_gb) is None
        ):
            raise PurchaseQuoteError('invalid_traffic')
        custom_traffic = traffic_limit = traffic_gb

    pricing = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period_days,
        device_limit=devices.effective_limit,
        custom_traffic_gb=custom_traffic,
        user=user,
        daily_devices_billing_version=1
        if tariff.is_daily and devices.selectable
        else getattr(subscription, 'daily_devices_billing_version', 0),
    )
    for amount in (
        pricing.base_price,
        pricing.devices_price,
        pricing.traffic_price,
        pricing.promo_group_discount,
        pricing.promo_offer_discount,
        pricing.original_total,
        pricing.final_total,
    ):
        if type(amount) is not int or not 0 <= amount <= MAX_PRICE_KOPEKS:
            raise PurchaseQuoteError('price_out_of_range')

    if subscription is None:
        kind = 'new'
    elif subscription.is_trial:
        kind = 'trial_conversion'
    elif subscription.tariff_id is None:
        kind = 'classic_conversion'
    else:
        kind = 'paid_renewal'
    next_daily = None
    if tariff.is_daily:
        from types import SimpleNamespace

        from app.services.pricing_engine import PricingEngine

        next_daily = PricingEngine.daily_subscription_price(
            SimpleNamespace(
                device_limit=devices.effective_limit,
                daily_devices_billing_version=1
                if devices.selectable
                else getattr(subscription, 'daily_devices_billing_version', 0),
            ),
            tariff,
            user,
        )
        if not 0 <= next_daily <= MAX_PRICE_KOPEKS:
            raise PurchaseQuoteError('price_out_of_range')
    return PurchaseQuote(devices, pricing, kind, traffic_limit, custom_traffic, next_daily)
