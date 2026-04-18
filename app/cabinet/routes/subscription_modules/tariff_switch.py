"""Tariff switching endpoints.

POST /subscription/tariff/switch/preview
POST /subscription/tariff/switch
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import PaymentMethod, Subscription, TransactionType, User
from app.services.pricing_engine import pricing_engine
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import TariffPurchaseRequest
from .helpers import _subscription_to_response, resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post('/tariff/switch/preview')
async def preview_tariff_switch(
    request: TariffPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Preview tariff switch - shows cost calculation."""
    if not settings.is_tariffs_mode():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Tariffs mode is not enabled',
        )

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription or not subscription.tariff_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No active subscription with tariff',
        )

    # Use actual_status for correct status check (handles time-based expiration)
    actual_status = subscription.actual_status
    if actual_status == 'expired':
        # For expired subscriptions, user should purchase a new tariff, not switch
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                'code': 'subscription_expired',
                'message': 'Subscription is expired. Please purchase a new tariff instead of switching.',
                'use_purchase_flow': True,
            },
        )
    if actual_status not in ('active', 'trial'):
        # For disabled/pending subscriptions, block switching with generic error
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                'code': 'subscription_not_active',
                'message': f'Subscription is not active (status: {actual_status}). Cannot switch tariff.',
            },
        )

    current_tariff = await get_tariff_by_id(db, subscription.tariff_id)
    new_tariff = await get_tariff_by_id(db, request.tariff_id)

    if not new_tariff or not new_tariff.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found or inactive',
        )

    if subscription.tariff_id == request.tariff_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Already on this tariff',
        )

    # Check tariff availability for user's promo group
    # Use get_primary_promo_group() for correct promo group resolution
    promo_group = user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None
    if promo_group is None:
        promo_group = getattr(user, 'promo_group', None)
    promo_group_id = promo_group.id if promo_group else None
    if not new_tariff.is_available_for_promo_group(promo_group_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Tariff not available for your promo group',
        )

    # Calculate remaining days
    remaining_days = 0
    if subscription.end_date and subscription.end_date > datetime.now(UTC):
        delta = subscription.end_date - datetime.now(UTC)
        remaining_days = max(0, delta.days)

    # Calculate switch cost (PricingEngine handles all cases: periodic<->periodic, daily->periodic, periodic->daily)
    switch_result = pricing_engine.calculate_tariff_switch_cost(
        current_tariff,
        new_tariff,
        remaining_days,
        user=user,
    )
    upgrade_cost = switch_result.upgrade_cost
    is_upgrade = switch_result.is_upgrade
    base_upgrade_cost = switch_result.raw_cost
    discount_value = switch_result.discount_value
    period_discount_percent = switch_result.effective_discount_pct

    balance = user.balance_kopeks or 0
    has_enough = balance >= upgrade_cost
    missing = max(0, upgrade_cost - balance) if not has_enough else 0

    response: dict[str, Any] = {
        'can_switch': has_enough,
        'current_tariff_id': current_tariff.id if current_tariff else None,
        'current_tariff_name': current_tariff.name if current_tariff else None,
        'new_tariff_id': new_tariff.id,
        'new_tariff_name': new_tariff.name,
        'remaining_days': remaining_days,
        'upgrade_cost_kopeks': upgrade_cost,
        'upgrade_cost_label': settings.format_price(upgrade_cost) if upgrade_cost > 0 else 'Бесплатно',
        'balance_kopeks': balance,
        'balance_label': settings.format_price(balance),
        'has_enough_balance': has_enough,
        'missing_amount_kopeks': missing,
        'missing_amount_label': settings.format_price(missing) if missing > 0 else '',
        'is_upgrade': is_upgrade,
    }

    # Add discount info if applicable
    if period_discount_percent > 0 and discount_value > 0:
        response['discount_percent'] = period_discount_percent
        response['discount_kopeks'] = discount_value
        response['base_upgrade_cost_kopeks'] = base_upgrade_cost

    return response


@router.post('/tariff/switch')
async def switch_tariff(
    request: TariffPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Switch to a different tariff without changing end date."""
    if not settings.is_tariffs_mode():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Tariffs mode is not enabled',
        )

    resolved = await resolve_subscription(db, user, subscription_id)

    if not resolved or not resolved.tariff_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No active subscription with tariff',
        )

    # Lock subscription row to prevent concurrent tariff switches
    locked_result = await db.execute(
        select(Subscription)
        .where(Subscription.id == resolved.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    subscription = locked_result.scalar_one()

    # Use actual_status for correct status check (handles time-based expiration)
    actual_status = subscription.actual_status
    if actual_status == 'expired':
        # For expired subscriptions, user should purchase a new tariff, not switch
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                'code': 'subscription_expired',
                'message': 'Subscription is expired. Please purchase a new tariff instead of switching.',
                'use_purchase_flow': True,
            },
        )
    if actual_status not in ('active', 'trial'):
        # For disabled/pending subscriptions, block switching with generic error
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                'code': 'subscription_not_active',
                'message': f'Subscription is not active (status: {actual_status}). Cannot switch tariff.',
            },
        )

    current_tariff = await get_tariff_by_id(db, subscription.tariff_id)
    new_tariff = await get_tariff_by_id(db, request.tariff_id)

    if not new_tariff or not new_tariff.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found or inactive',
        )

    if subscription.tariff_id == request.tariff_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Already on this tariff',
        )

    # Check tariff availability
    # Use get_primary_promo_group() for correct promo group resolution
    promo_group = user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None
    if promo_group is None:
        promo_group = getattr(user, 'promo_group', None)
    promo_group_id = promo_group.id if promo_group else None
    if not new_tariff.is_available_for_promo_group(promo_group_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Tariff not available',
        )

    # Lock user BEFORE price computation to prevent TOCTOU on promo offer
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    # Calculate remaining days
    remaining_days = 0
    if subscription.end_date and subscription.end_date > datetime.now(UTC):
        delta = subscription.end_date - datetime.now(UTC)
        remaining_days = max(0, delta.days)

    # Calculate cost (PricingEngine handles all cases: periodic<->periodic, daily->periodic, periodic->daily)
    switch_result = pricing_engine.calculate_tariff_switch_cost(
        current_tariff,
        new_tariff,
        remaining_days,
        user=user,
    )
    upgrade_cost = switch_result.upgrade_cost
    base_upgrade_cost = switch_result.raw_cost
    discount_value = switch_result.discount_value
    period_discount_percent = switch_result.effective_discount_pct
    new_period_days = switch_result.new_period_days

    # Validate daily price for switching TO daily
    new_is_daily = getattr(new_tariff, 'is_daily', False)
    current_is_daily = getattr(current_tariff, 'is_daily', False) if current_tariff else False
    switching_to_daily = not current_is_daily and new_is_daily
    switching_from_daily = current_is_daily and not new_is_daily

    if switching_to_daily and (getattr(new_tariff, 'daily_price_kopeks', 0) or 0) <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Daily tariff has invalid price',
        )

    # Charge if upgrade
    switch_transaction = None
    if upgrade_cost > 0:
        if user.balance_kopeks < upgrade_cost:
            missing = upgrade_cost - user.balance_kopeks
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    'code': 'insufficient_funds',
                    'message': f'Insufficient funds. Missing {settings.format_price(missing)}',
                    'missing_amount': missing,
                },
            )

        if switching_to_daily:
            description = f"Переход на суточный тариф '{new_tariff.name}'"
        elif switching_from_daily:
            description = f"Переход с суточного на тариф '{new_tariff.name}' ({new_period_days} дней)"
        else:
            description = f"Переход на тариф '{new_tariff.name}' (доплата за {remaining_days} дней)"

        # Add discount info to description if applicable
        if period_discount_percent > 0 and discount_value > 0:
            description += f' (скидка {period_discount_percent}%)'

        success = await subtract_user_balance(
            db,
            user,
            upgrade_cost,
            description,
            consume_promo_offer=switch_result.offer_discount_pct > 0,
            mark_as_paid_subscription=True,
            commit=False,
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to charge balance',
            )

        # Create transaction (commit=False to keep FOR UPDATE lock held)
        switch_transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=upgrade_cost,
            description=description,
            payment_method=PaymentMethod.BALANCE,
            commit=False,
        )
    else:
        # Free switch (downgrade) — record in history
        description = f"Переход на тариф '{new_tariff.name}'"
        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=0,
            description=description,
            commit=False,
        )

    # Update subscription
    old_tariff_name = current_tariff.name if current_tariff else 'Unknown'

    # Reset device limit to new tariff base (extra purchased devices are not carried over)
    from app.database.crud.subscription import calc_device_limit_on_tariff_switch

    # Re-load subscription to avoid MissingGreenlet from expired lazy relationship
    # (subtract_user_balance re-selects User with populate_existing=True which expires relationships)
    await db.refresh(subscription)

    subscription.tariff_id = new_tariff.id
    subscription.traffic_limit_gb = new_tariff.traffic_limit_gb
    subscription.device_limit = calc_device_limit_on_tariff_switch(
        current_device_limit=subscription.device_limit,
        old_tariff_device_limit=current_tariff.device_limit if current_tariff else None,
        new_tariff_device_limit=new_tariff.device_limit,
        max_device_limit=new_tariff.max_device_limit,
    )
    subscription.connected_squads = new_tariff.allowed_squads or []

    # Reset purchased traffic and delete TrafficPurchase records on tariff switch
    from sqlalchemy import delete as sql_delete

    from app.database.models import TrafficPurchase

    await db.execute(sql_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
    subscription.purchased_traffic_gb = 0
    subscription.traffic_reset_at = None

    if settings.RESET_TRAFFIC_ON_TARIFF_SWITCH:
        subscription.traffic_used_gb = 0.0

    if switching_to_daily:
        # Switching TO daily - reset end_date to 1 day, set last_daily_charge_at
        subscription.end_date = datetime.now(UTC) + timedelta(days=1)
        subscription.last_daily_charge_at = datetime.now(UTC)
        subscription.is_daily_paused = False
    elif switching_from_daily:
        subscription.end_date = datetime.now(UTC) + timedelta(days=new_period_days)
        subscription.is_daily_paused = False

    subscription.updated_at = datetime.now(UTC)
    await db.commit()

    # Emit deferred side-effects after atomic commit
    if upgrade_cost > 0 and switch_transaction:
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            switch_transaction,
            amount_kopeks=upgrade_cost,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            payment_method=PaymentMethod.BALANCE,
        )

    # Sync with RemnaWave (optionally reset traffic based on admin setting)
    should_reset_traffic = settings.RESET_TRAFFIC_ON_TARIFF_SWITCH
    # Refresh subscription after commit (all objects are expired)
    await db.refresh(subscription)

    try:
        subscription_service = SubscriptionService()
        _has_panel = (
            getattr(subscription, 'remnawave_uuid', None)
            if settings.is_multi_tariff_enabled()
            else getattr(user, 'remnawave_uuid', None)
        )
        if _has_panel:
            await subscription_service.update_remnawave_user(
                db,
                subscription,
                reset_traffic=should_reset_traffic,
                reset_reason='смена тарифа',
                sync_squads=True,
            )
        else:
            await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=should_reset_traffic,
                reset_reason='смена тарифа',
            )
    except Exception as e:
        logger.error('Failed to sync tariff switch with RemnaWave', error=e)
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=user.id,
            action='update' if _has_panel else 'create',
        )

    # Reset all devices on tariff switch
    devices_reset = False
    _switch_uuid = (
        subscription.remnawave_uuid
        if settings.is_multi_tariff_enabled() and subscription.remnawave_uuid
        else user.remnawave_uuid
    )
    if _switch_uuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                await api.reset_user_devices(_switch_uuid)
                devices_reset = True
                logger.info('Reset all devices for user on tariff switch', user_id=user.id)
        except Exception as e:
            logger.error('Failed to reset devices on tariff switch', error=e)

    await db.refresh(user)
    await db.refresh(subscription)

    # Отправляем уведомление админам о смене тарифа
    try:
        from aiogram import Bot

        from app.services.admin_notification_service import AdminNotificationService

        if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
            bot = Bot(token=settings.BOT_TOKEN)
            try:
                notification_service = AdminNotificationService(bot)
                await notification_service.send_subscription_purchase_notification(
                    db=db,
                    user=user,
                    subscription=subscription,
                    transaction=switch_transaction if upgrade_cost > 0 else None,
                    period_days=remaining_days if remaining_days > 0 else new_period_days,
                    was_trial_conversion=False,
                    amount_kopeks=upgrade_cost,
                    purchase_type='tariff_switch',
                )
            finally:
                await bot.session.close()
    except Exception as e:
        logger.error('Failed to send admin notification for tariff switch', error=e)

    # Refresh expired objects after db.commit() in _record_subscription_event
    await db.refresh(subscription)
    await db.refresh(user)

    response: dict[str, Any] = {
        'success': True,
        'message': f"Switched from '{old_tariff_name}' to '{new_tariff.name}'"
        + (' (devices reset)' if devices_reset else ''),
        'subscription': _subscription_to_response(subscription, user=user),
        'old_tariff_name': old_tariff_name,
        'new_tariff_id': new_tariff.id,
        'new_tariff_name': new_tariff.name,
        'charged_kopeks': upgrade_cost,
        'balance_kopeks': user.balance_kopeks,
        'balance_label': settings.format_price(user.balance_kopeks),
    }

    # Add discount info if applicable
    if period_discount_percent > 0 and discount_value > 0:
        response['discount_percent'] = period_discount_percent
        response['discount_kopeks'] = discount_value
        response['base_charged_kopeks'] = base_upgrade_cost

    return response
