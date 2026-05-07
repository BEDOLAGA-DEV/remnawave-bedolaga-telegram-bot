"""WL traffic endpoints for cabinet."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import reactivate_subscription
from app.database.crud.transaction import create_transaction
from app.database.crud.user import lock_user_for_pricing, subtract_user_balance
from app.database.models import TransactionType, User
from app.services.user_cart_service import user_cart_service
from app.utils.pricing_utils import calculate_prorated_price

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import TrafficPackageResponse, TrafficPurchaseRequest
from ._traffic_core import (
    apply_purchase_db,
    delete_purchases_for_switch,
    resolve_package_price,
    resolve_traffic_packages,
    sync_remnawave_after_purchase,
)
from .helpers import _apply_addon_discount, resolve_subscription


logger = structlog.get_logger(__name__)
router = APIRouter()


@router.get('/wl-traffic-packages', response_model=list[TrafficPackageResponse])
async def get_wl_traffic_packages(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> list[TrafficPackageResponse]:
    """Available WL top-up packages for the resolved subscription."""
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        return []

    packages = await resolve_traffic_packages(db, subscription, kind='wl')
    return [
        TrafficPackageResponse(
            gb=p['gb'],
            price_kopeks=p['price'],
            price_rubles=p['price'] / 100,
            is_unlimited=p['is_unlimited'],
        )
        for p in packages
    ]


@router.post('/wl-traffic')
async def purchase_wl_traffic(
    request: TrafficPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict:
    """Purchase additional WL traffic GB."""
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(status_code=403, detail='Subscription purchases are restricted for this account')

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail='No subscription found')
    if subscription.is_trial:
        raise HTTPException(status_code=400, detail='Эта функция доступна только для платных подписок')
    if (subscription.wl_traffic_limit_gb or 0) == 0:
        raise HTTPException(status_code=400, detail='У вас уже безлимитный трафик')
    if not getattr(settings, 'WL_TRAFFIC_TOPUP_ENABLED', True):
        raise HTTPException(status_code=400, detail='Функция докупки WL-трафика отключена')

    base_price = await resolve_package_price(db, subscription, gb=request.gb, kind='wl')
    if base_price <= 0:
        raise HTTPException(status_code=400, detail=f'Пакет {request.gb} ГБ недоступен')

    is_tariff_mode = settings.is_tariffs_mode() and subscription.tariff_id
    if is_tariff_mode:
        prorated_price, days_charged = base_price, 30
    else:
        prorated_price, days_charged = calculate_prorated_price(base_price, subscription.end_date)

    user = await lock_user_for_pricing(db, user.id)
    period_hint_days = days_charged if days_charged > 0 else 30
    discount = _apply_addon_discount(user, 'traffic', prorated_price, period_hint_days)
    final_price = discount['discounted']

    if discount['percent'] < 100 and final_price > 0:
        final_price = max(100, final_price)

    if final_price > 0 and user.balance_kopeks < final_price:
        missing = final_price - user.balance_kopeks
        try:
            await user_cart_service.save_user_cart(
                user.id,
                {
                    'cart_mode': 'add_wl_traffic',
                    'subscription_id': subscription.id,
                    'traffic_gb': request.gb,
                    'price_kopeks': final_price,
                    'base_price_kopeks': prorated_price,
                    'discount_percent': discount['percent'],
                    'source': 'cabinet',
                    'description': f'Докупка {request.gb} ГБ WL-трафика',
                },
            )
        except Exception as e:
            logger.warning('Failed to save WL cart', error=str(e))
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                'code': 'insufficient_funds',
                'message': f'Недостаточно средств. Не хватает {settings.format_price(missing)}',
                'missing_amount': missing,
                'cart_saved': True,
                'cart_mode': 'add_wl_traffic',
            },
        )

    description = f'Докупка {request.gb} ГБ WL-трафика'
    if discount['percent'] > 0:
        description += f' (скидка {discount["percent"]}%)'

    success = await subtract_user_balance(db, user, final_price, description)
    if not success:
        raise HTTPException(status_code=500, detail='Failed to charge balance')

    await apply_purchase_db(db, subscription, gb=request.gb, kind='wl')
    await reactivate_subscription(db, subscription)
    await sync_remnawave_after_purchase(db, subscription, user)
    await create_transaction(
        db=db,
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=final_price,
        description=description,
    )
    await db.refresh(user)
    await db.refresh(subscription)

    response = {
        'success': True,
        'gb_added': request.gb,
        'new_wl_traffic_limit_gb': subscription.wl_traffic_limit_gb,
        'amount_paid_kopeks': final_price,
        'new_balance_kopeks': user.balance_kopeks,
    }
    if discount['percent'] > 0:
        response['discount_percent'] = discount['percent']
        response['discount_kopeks'] = discount['discount']
        response['base_price_kopeks'] = prorated_price
    return response


@router.put('/wl-traffic')
async def switch_wl_traffic(
    request: TrafficPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict:
    """Switch the WL traffic package (upgrade or downgrade)."""
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail='No subscription found')
    if subscription.is_trial:
        raise HTTPException(status_code=400, detail='Эта функция доступна только для платных подписок')

    current = subscription.wl_traffic_limit_gb or 0
    new_gb = request.gb
    if current == new_gb:
        raise HTTPException(status_code=400, detail='Already on this WL traffic package')

    purchased = subscription.wl_purchased_traffic_gb or 0
    base = max(0, current - purchased)
    old_price = settings.get_wl_traffic_price(base)
    new_price = settings.get_wl_traffic_price(new_gb)
    if new_price <= 0 and new_gb != 0:
        raise HTTPException(status_code=400, detail='Invalid WL traffic package')

    user = await lock_user_for_pricing(db, user.id)

    charged = 0
    if new_price > old_price:
        diff_per_month = new_price - old_price
        discount = _apply_addon_discount(user, 'traffic', diff_per_month, 30)
        per_month_after_discount = discount['discounted']
        prorated_price, _days = calculate_prorated_price(per_month_after_discount, subscription.end_date)
        if prorated_price > 0 and user.balance_kopeks < prorated_price:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f'Insufficient balance. Need {settings.format_price(prorated_price)}',
            )
        if prorated_price > 0:
            description = f'WL traffic upgrade {current}GB → {new_gb}GB'
            success = await subtract_user_balance(db, user, prorated_price, description)
            if not success:
                raise HTTPException(status_code=500, detail='Failed to charge balance')
            await create_transaction(
                db=db,
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=prorated_price,
                description=description,
            )
            charged = prorated_price

    await delete_purchases_for_switch(db, subscription, kind='wl')
    subscription.wl_traffic_limit_gb = new_gb
    subscription.wl_purchased_traffic_gb = 0
    subscription.wl_traffic_reset_at = None
    subscription.updated_at = datetime.now(UTC)
    await db.commit()

    await sync_remnawave_after_purchase(db, subscription, user)
    await db.refresh(user)
    await db.refresh(subscription)

    return {
        'success': True,
        'old_wl_traffic_gb': current,
        'new_wl_traffic_gb': new_gb,
        'charged_kopeks': charged,
        'balance_kopeks': user.balance_kopeks,
        'balance_label': settings.format_price(user.balance_kopeks),
    }
