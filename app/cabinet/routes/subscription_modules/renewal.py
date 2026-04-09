"""Subscription renewal endpoints.

GET /subscription/renewal-options
POST /subscription/renew
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import PaymentMethod, SubscriptionStatus, User
from app.services.pricing_engine import pricing_engine
from app.services.subscription_renewal_service import (
    SubscriptionRenewalChargeError,
    SubscriptionRenewalService,
)
from app.services.user_cart_service import user_cart_service

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import (
    RenewalOptionResponse,
    RenewalRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get('/renewal-options', response_model=list[RenewalOptionResponse])
async def get_renewal_options(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Get available subscription renewal options with prices."""
    from .helpers import resolve_subscription

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        return []

    # Classic subscriptions cannot be renewed when tariff mode is enabled
    if settings.is_tariffs_mode() and not subscription.tariff_id:
        return []

    _non_renewable = {SubscriptionStatus.DISABLED.value, SubscriptionStatus.PENDING.value}
    _actual_status = getattr(subscription, 'actual_status', subscription.status)
    if _actual_status in _non_renewable:
        return []

    # Determine available periods
    if subscription.tariff_id and subscription.tariff and subscription.tariff.period_prices:
        periods = sorted(int(k) for k in subscription.tariff.period_prices.keys())
    else:
        periods = settings.get_available_renewal_periods()

    options = []

    for period in periods:
        pricing = await pricing_engine.calculate_renewal_price(db, subscription, period, user=user)

        if pricing.final_total <= 0 and pricing.original_total <= 0:
            continue

        original_price = pricing.original_total
        combined_discount = 0
        if original_price > 0 and original_price != pricing.final_total:
            combined_discount = int((original_price - pricing.final_total) * 100 / original_price)

        options.append(
            RenewalOptionResponse(
                period_days=period,
                price_kopeks=pricing.final_total,
                price_rubles=pricing.final_total / 100,
                discount_percent=combined_discount,
                original_price_kopeks=original_price if combined_discount > 0 else None,
            )
        )

    return options


@router.post('/renew')
async def renew_subscription(
    request: RenewalRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Renew subscription (pay from balance)."""
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Subscription renewal is restricted for this account',
        )

    # Support subscription_id from both query param and body (backward compat)
    from .helpers import resolve_subscription

    _sub_id = subscription_id or request.subscription_id
    subscription = await resolve_subscription(db, user, _sub_id)
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    # Classic subscriptions cannot be renewed when tariff mode is enabled
    if settings.is_tariffs_mode() and not subscription.tariff_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Classic subscriptions cannot be renewed. Please purchase a tariff.',
        )

    _non_renewable = {SubscriptionStatus.DISABLED.value, SubscriptionStatus.PENDING.value}
    _actual_status = getattr(subscription, 'actual_status', subscription.status)
    if _actual_status in _non_renewable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Cannot renew subscription with status: {_actual_status}',
        )

    if subscription.tariff_id and subscription.tariff and subscription.tariff.period_prices:
        available_periods = [int(p) for p in subscription.tariff.period_prices.keys()]
    else:
        available_periods = settings.get_available_renewal_periods()

    if request.period_days not in available_periods:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Selected renewal period is not available',
        )

    # Lock user row to prevent TOCTOU on promo-offer state
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    # Unified pricing via PricingEngine
    pricing = await pricing_engine.calculate_renewal_price(
        db,
        subscription,
        request.period_days,
        user=user,
    )
    price_kopeks = pricing.final_total
    promo_offer_discount_value = pricing.promo_offer_discount
    promo_offer_discount_percent = pricing.breakdown.get('offer_discount_pct', 0)

    if price_kopeks <= 0 and pricing.original_total <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid renewal period',
        )

    original_price_kopeks = pricing.original_total
    discount_percent = 0
    if original_price_kopeks > 0 and original_price_kopeks != price_kopeks:
        discount_percent = int((original_price_kopeks - price_kopeks) * 100 / original_price_kopeks)

    tariff = subscription.tariff if subscription.tariff_id else None

    # Check balance (skip for 100% discount)
    if price_kopeks > 0 and user.balance_kopeks < price_kopeks:
        missing = price_kopeks - user.balance_kopeks

        # Get tariff info for cart
        tariff_id = subscription.tariff_id
        tariff_name = None
        tariff_traffic_limit_gb = None
        tariff_allowed_squads = None

        if tariff_id:
            tariff = await get_tariff_by_id(db, tariff_id)
            if tariff:
                tariff_name = tariff.name
                tariff_traffic_limit_gb = tariff.traffic_limit_gb
                tariff_allowed_squads = tariff.allowed_squads or []

        # Save cart for auto-purchase after balance top-up
        cart_data: dict[str, Any] = {
            'cart_mode': 'extend',
            'subscription_id': subscription.id,
            'tariff_id': tariff_id,
            'period_days': request.period_days,
            'total_price': price_kopeks,
            'user_id': user.id,
            'saved_cart': True,
            'missing_amount': missing,
            'return_to_cart': True,
            'description': f'Продление подписки на {request.period_days} дней'
            + (f' ({tariff_name})' if tariff_name else ''),
            'discount_percent': discount_percent,
            'consume_promo_offer': promo_offer_discount_value > 0,
            'source': 'cabinet',
        }

        # Add subscription parameters for auto-purchase
        if tariff_id:
            cart_data['traffic_limit_gb'] = tariff_traffic_limit_gb
            # Сохраняем актуальный device_limit подписки (включая докупленные устройства)
            cart_data['device_limit'] = subscription.device_limit
            cart_data['allowed_squads'] = tariff_allowed_squads
        else:
            # Classic mode: сохраняем текущие параметры подписки для корректной автопокупки
            cart_data['device_limit'] = subscription.device_limit
            cart_data['traffic_limit_gb'] = subscription.traffic_limit_gb

        try:
            await user_cart_service.save_user_cart(user.id, cart_data)
            logger.info('Cart saved for auto-renewal (cabinet) user', user_id=user.id)
        except Exception as e:
            logger.error('Error saving cart for auto-renewal (cabinet)', error=e)

        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                'code': 'insufficient_funds',
                'message': f'Недостаточно средств. Не хватает {settings.format_price(missing)}',
                'missing_amount': missing,
                'cart_saved': True,
                'cart_mode': 'extend',
            },
        )

    # Centralized renewal: balance deduction, extension, RemnaWave sync, admin notification,
    # server price recording, and compensating refund on failure.
    renewal_description = f'Продление подписки на {request.period_days} дней' + (f' ({tariff.name})' if tariff else '')
    renewal_service = SubscriptionRenewalService()

    try:
        result = await renewal_service.finalize(
            db,
            user,
            subscription,
            pricing,
            description=renewal_description,
            payment_method=PaymentMethod.BALANCE,
        )
    except SubscriptionRenewalChargeError:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                'code': 'insufficient_funds',
                'message': 'Недостаточно средств (concurrent check)',
            },
        )

    response: dict[str, Any] = {
        'message': 'Subscription renewed successfully',
        'new_end_date': result.subscription.end_date.isoformat(),
        'amount_paid_kopeks': price_kopeks,
    }

    # Add discount info to response
    if promo_offer_discount_value > 0:
        response['promo_discount_percent'] = promo_offer_discount_percent
        response['promo_discount_amount_kopeks'] = promo_offer_discount_value
        response['original_price_kopeks'] = original_price_kopeks

    return response
