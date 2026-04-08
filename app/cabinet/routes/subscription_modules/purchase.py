"""Purchase-related endpoints.

GET /subscription/purchase-options
POST /subscription/purchase-preview
POST /subscription/purchase
POST /subscription/purchase-tariff
GET /subscription/trial
POST /subscription/trial
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.server_squad import get_server_squad_by_uuid
from app.database.crud.subscription import (
    create_paid_subscription,
    create_trial_subscription,
    decrement_subscription_server_counts,
    extend_subscription,
    get_subscription_by_user_id,
)
from app.database.crud.tariff import get_tariff_by_id, get_tariffs_for_user
from app.database.crud.transaction import create_transaction
from app.database.crud.user import add_user_balance, subtract_user_balance
from app.database.models import PaymentMethod, Subscription, Tariff, TransactionType, User
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service,
)
from app.services.pricing_engine import pricing_engine
from app.services.subscription_purchase_service import (
    MiniAppSubscriptionPurchaseService,
    PurchaseBalanceError,
    PurchaseValidationError,
)
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service
from app.utils.pricing_utils import format_period_description

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import (
    PurchasePreviewRequest,
    SubscriptionResponse,
    TariffPurchaseRequest,
    TrialInfoResponse,
)
from .helpers import _subscription_to_response


logger = structlog.get_logger(__name__)

router = APIRouter()


# ============ Full Purchase Flow (like MiniApp) ============

purchase_service = MiniAppSubscriptionPurchaseService()


async def _build_tariff_response(
    db: AsyncSession,
    tariff: Tariff,
    current_tariff_id: int | None = None,
    language: str = 'ru',
    user: User | None = None,
    subscription: Subscription | None = None,
) -> dict[str, Any]:
    """Build tariff model for API response with promo group discounts applied."""
    servers = []
    servers_count = 0

    if tariff.allowed_squads:
        servers_count = len(tariff.allowed_squads)
        for squad_uuid in tariff.allowed_squads[:5]:  # Limit for preview
            server = await get_server_squad_by_uuid(db, squad_uuid)
            if server:
                servers.append(
                    {
                        'uuid': squad_uuid,
                        'name': server.display_name or squad_uuid[:8],
                    }
                )

    # Get promo group for discount calculation
    # Use get_primary_promo_group() for correct promo group resolution
    promo_group = user.get_primary_promo_group() if user and hasattr(user, 'get_primary_promo_group') else None
    if promo_group is None and user:
        # Fallback to legacy promo_group attribute
        promo_group = getattr(user, 'promo_group', None)
    promo_group_name = promo_group.name if promo_group else None

    # Вычисляем доп. устройства для текущего тарифа (при продлении)
    extra_devices_count = 0
    extra_device_price_per_month = 0
    if subscription and subscription.tariff_id == tariff.id:
        extra_devices_count = max(0, (subscription.device_limit or 0) - (tariff.device_limit or 0))
        if extra_devices_count > 0:
            extra_device_price_per_month = (
                tariff.device_price_kopeks if tariff.device_price_kopeks is not None else settings.PRICE_PER_DEVICE
            )

    periods = []
    if tariff.period_prices:
        for period_str, price_kopeks in sorted(tariff.period_prices.items(), key=lambda x: int(x[0])):
            if int(price_kopeks) < 0:
                continue  # Skip disabled periods (negative price)
            period_days = int(period_str)
            months = max(1, period_days // 30)

            # Базовая цена тарифа
            base_tariff_price = int(price_kopeks)

            # Стоимость доп. устройств за этот период
            extra_devices_cost = extra_devices_count * extra_device_price_per_month * months

            # Apply per-category promo group discounts
            original_price = base_tariff_price + extra_devices_cost
            discount_amount = 0

            if promo_group:
                period_pct = promo_group.get_discount_percent('period', period_days)
                devices_pct = promo_group.get_discount_percent('devices', period_days)
                discounted_base = (
                    pricing_engine.apply_discount(base_tariff_price, period_pct)
                    if period_pct > 0
                    else base_tariff_price
                )
                discounted_devices = (
                    pricing_engine.apply_discount(extra_devices_cost, devices_pct)
                    if devices_pct > 0
                    else extra_devices_cost
                )
                final_price = discounted_base + discounted_devices
                discount_amount = original_price - final_price
                discount_percent = max(period_pct, devices_pct)
            else:
                discount_percent = 0
                final_price = original_price

            per_month = final_price // months if months > 0 else final_price
            original_per_month = original_price // months if months > 0 else original_price

            period_data: dict[str, Any] = {
                'days': period_days,
                'months': months,
                'label': format_period_description(period_days, language),
                'price_kopeks': final_price,
                'price_label': settings.format_price(final_price),
                'price_per_month_kopeks': per_month,
                'price_per_month_label': settings.format_price(per_month),
            }

            # Информация о доп. устройствах в цене
            if extra_devices_count > 0:
                period_data['extra_devices_count'] = extra_devices_count
                period_data['extra_devices_cost_kopeks'] = extra_devices_cost
                period_data['extra_devices_cost_label'] = settings.format_price(extra_devices_cost)
                period_data['base_tariff_price_kopeks'] = base_tariff_price
                period_data['base_tariff_price_label'] = settings.format_price(base_tariff_price)

            # Add discount info if discount is applied
            if discount_percent > 0:
                period_data['original_price_kopeks'] = original_price
                period_data['original_price_label'] = settings.format_price(original_price)
                period_data['original_per_month_kopeks'] = original_per_month
                period_data['original_per_month_label'] = settings.format_price(original_per_month)
                period_data['discount_percent'] = discount_percent
                period_data['discount_amount_kopeks'] = discount_amount
                period_data['discount_label'] = f'-{discount_percent}%'

            periods.append(period_data)

    traffic_label = '♾️ Безлимит' if tariff.traffic_limit_gb == 0 else f'{tariff.traffic_limit_gb} ГБ'

    # Apply discount to daily price if applicable (group + promo-offer)
    daily_price = getattr(tariff, 'daily_price_kopeks', 0)
    original_daily_price = daily_price
    daily_discount_percent = 0
    if daily_price > 0:
        from app.services.pricing_engine import PricingEngine
        from app.utils.promo_offer import get_user_active_promo_discount_percent

        daily_group_pct = promo_group.get_discount_percent('period', 1) if promo_group else 0
        daily_offer_pct = get_user_active_promo_discount_percent(user) if user else 0
        if daily_group_pct > 0 or daily_offer_pct > 0:
            daily_price, _, _ = PricingEngine.apply_stacked_discounts(daily_price, daily_group_pct, daily_offer_pct)
            # Комбинированный процент для отображения
            remaining = (100 - daily_group_pct) * (100 - daily_offer_pct)
            daily_discount_percent = 100 - remaining // 100

    # Apply discount to custom price_per_day if applicable
    price_per_day = tariff.price_per_day_kopeks
    original_price_per_day = price_per_day
    custom_days_discount_percent = 0
    if promo_group and price_per_day > 0:
        custom_days_discount_percent = promo_group.get_discount_percent('period', 30)  # Use 30-day rate as base
        if custom_days_discount_percent > 0:
            price_per_day = pricing_engine.apply_discount(price_per_day, custom_days_discount_percent)

    # Apply discount to device price if applicable
    device_price = tariff.device_price_kopeks if tariff.device_price_kopeks is not None else 0
    original_device_price = device_price
    device_discount_percent = 0
    if promo_group and device_price > 0:
        device_discount_percent = promo_group.get_discount_percent('devices', 30)
        if device_discount_percent > 0:
            device_price = pricing_engine.apply_discount(device_price, device_discount_percent)

    # Показываем реальное количество устройств (с докупленными) для текущего тарифа
    actual_device_limit = tariff.device_limit
    if subscription and subscription.tariff_id == tariff.id:
        actual_device_limit = max(tariff.device_limit or 0, subscription.device_limit or 0)

    response: dict[str, Any] = {
        'id': tariff.id,
        'name': tariff.name,
        'description': tariff.description,
        'tier_level': tariff.tier_level,
        'traffic_limit_gb': tariff.traffic_limit_gb,
        'traffic_limit_label': traffic_label,
        'is_unlimited_traffic': tariff.traffic_limit_gb == 0,
        'device_limit': actual_device_limit,
        'base_device_limit': tariff.device_limit,
        'extra_devices_count': extra_devices_count,
        'device_price_kopeks': device_price,
        'servers_count': servers_count,
        'servers': servers,
        'periods': periods,
        'is_current': current_tariff_id == tariff.id if current_tariff_id else False,
        'is_available': tariff.is_active,
        # Произвольное количество дней
        'custom_days_enabled': tariff.custom_days_enabled,
        'price_per_day_kopeks': price_per_day,
        'min_days': tariff.min_days,
        'max_days': tariff.max_days,
        # Произвольный трафик при покупке
        'custom_traffic_enabled': tariff.custom_traffic_enabled,
        'traffic_price_per_gb_kopeks': tariff.traffic_price_per_gb_kopeks,
        'min_traffic_gb': tariff.min_traffic_gb,
        'max_traffic_gb': tariff.max_traffic_gb,
        # Докупка трафика
        'traffic_topup_enabled': tariff.traffic_topup_enabled,
        'traffic_topup_packages': tariff.get_traffic_topup_packages()
        if hasattr(tariff, 'get_traffic_topup_packages')
        else {},
        'max_topup_traffic_gb': tariff.max_topup_traffic_gb,
        # Дневной тариф
        'is_daily': getattr(tariff, 'is_daily', False),
        'daily_price_kopeks': daily_price,
        # Сброс трафика
        'traffic_reset_mode': tariff.traffic_reset_mode or settings.DEFAULT_TRAFFIC_RESET_STRATEGY,
    }

    # Add promo group info if user has discounts
    if promo_group_name:
        response['promo_group_name'] = promo_group_name

    # Add original prices if discounts were applied
    if device_discount_percent > 0:
        response['original_device_price_kopeks'] = original_device_price
        response['device_discount_percent'] = device_discount_percent

    if daily_discount_percent > 0 and original_daily_price > 0:
        response['original_daily_price_kopeks'] = original_daily_price
        response['daily_discount_percent'] = daily_discount_percent

    if custom_days_discount_percent > 0 and original_price_per_day > 0:
        response['original_price_per_day_kopeks'] = original_price_per_day
        response['custom_days_discount_percent'] = custom_days_discount_percent

    return response


@router.get('/purchase-options')
async def get_purchase_options(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = None,
) -> dict[str, Any]:
    """Get all subscription purchase options (periods, servers, traffic, devices)."""
    try:
        settings.get_sales_mode()

        # Tariffs mode - return list of tariffs
        if settings.is_tariffs_mode():
            # Use get_primary_promo_group() for correct promo group resolution
            # (handles both legacy promo_group FK and new user_promo_groups M2M)
            promo_group = user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None
            if promo_group is None:
                # Fallback to legacy promo_group attribute
                promo_group = getattr(user, 'promo_group', None)
            promo_group_id = promo_group.id if promo_group else None
            tariffs = await get_tariffs_for_user(db, promo_group_id)

            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user.id)
                purchased_tariff_ids = {s.tariff_id for s in active_subs if s.tariff_id and not s.is_trial}

                if subscription_id:
                    from app.database.crud.subscription import get_subscription_by_id_for_user

                    subscription = await get_subscription_by_id_for_user(db, subscription_id, user.id)
                elif active_subs:
                    _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                    _pool = _non_daily or active_subs
                    subscription = max(_pool, key=lambda s: s.days_left)
                else:
                    subscription = None
            else:
                purchased_tariff_ids = set()
                subscription = await get_subscription_by_user_id(db, user.id)
            current_tariff_id = subscription.tariff_id if subscription else None
            language = getattr(user, 'language', 'ru') or 'ru'

            # Determine subscription status for frontend to decide purchase vs switch flow
            subscription_status = None
            subscription_is_expired = False
            if subscription:
                subscription_status = subscription.actual_status
                subscription_is_expired = subscription_status == 'expired'

            tariff_responses = []
            for tariff in tariffs:
                tariff_data = await _build_tariff_response(db, tariff, current_tariff_id, language, user, subscription)
                # In multi-tariff mode: mark purchased tariffs so frontend can filter them
                if settings.is_multi_tariff_enabled() and tariff.id in purchased_tariff_ids:
                    tariff_data['is_purchased'] = True
                else:
                    tariff_data['is_purchased'] = False
                tariff_responses.append(tariff_data)

            return {
                'sales_mode': 'tariffs',
                'tariffs': tariff_responses,
                'current_tariff_id': current_tariff_id,
                'balance_kopeks': user.balance_kopeks,
                'balance_label': settings.format_price(user.balance_kopeks),
                # Include subscription status info for frontend decision making
                'subscription_status': subscription_status,
                'subscription_is_expired': subscription_is_expired,
                'has_subscription': subscription is not None,
                # Multi-tariff: all tariffs purchased flag for frontend fallback
                'all_tariffs_purchased': len(purchased_tariff_ids) >= len(tariffs)
                if settings.is_multi_tariff_enabled()
                else False,
            }

        # Classic mode - return periods
        context = await purchase_service.build_options(db, user, subscription_id=subscription_id)
        payload = context.payload
        payload['sales_mode'] = 'classic'
        return payload

    except PurchaseValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error('Failed to build purchase options for user', user_id=user.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load purchase options',
        )


@router.post('/purchase-preview')
async def preview_purchase(
    request: PurchasePreviewRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Calculate and preview the total price for selected options (classic mode only)."""
    # This endpoint is for classic mode only, tariffs mode uses /purchase-tariff
    if settings.is_tariffs_mode():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This endpoint is not available in tariffs mode. Use /purchase-tariff instead.',
        )

    try:
        context = await purchase_service.build_options(db, user)

        # Convert request to dict for parsing
        selection_dict = {
            'period_id': request.selection.period_id,
            'period_days': request.selection.period_days,
            'traffic_value': request.selection.traffic_value,
            'servers': request.selection.servers,
            'devices': request.selection.devices,
        }

        selection = purchase_service.parse_selection(context, selection_dict)
        pricing = await purchase_service.calculate_pricing(db, context, selection)
        preview = purchase_service.build_preview_payload(context, pricing)

        return preview

    except PurchaseValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error('Failed to calculate purchase preview for user', user_id=user.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to calculate price',
        )


@router.post('/purchase')
async def submit_purchase(
    request: PurchasePreviewRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Submit subscription purchase (deduct from balance, classic mode only)."""
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Subscription purchases are restricted for this account',
        )

    # This endpoint is for classic mode only, tariffs mode uses /purchase-tariff
    if settings.is_tariffs_mode():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This endpoint is not available in tariffs mode. Use /purchase-tariff instead.',
        )

    try:
        from app.database.crud.user import lock_user_for_pricing

        user = await lock_user_for_pricing(db, user.id)
        context = await purchase_service.build_options(db, user)

        # Convert request to dict for parsing
        selection_dict = {
            'period_id': request.selection.period_id,
            'period_days': request.selection.period_days,
            'traffic_value': request.selection.traffic_value,
            'servers': request.selection.servers,
            'devices': request.selection.devices,
        }

        selection = purchase_service.parse_selection(context, selection_dict)
        pricing = await purchase_service.calculate_pricing(db, context, selection)
        result = await purchase_service.submit_purchase(db, context, pricing)

        subscription = result['subscription']

        # Send email notification for email-only users
        if not user.telegram_id and user.email and user.email_verified:
            try:
                is_new_subscription = result.get('was_trial_conversion') or not context.subscription
                notification_type = (
                    NotificationType.SUBSCRIPTION_ACTIVATED
                    if is_new_subscription
                    else NotificationType.SUBSCRIPTION_RENEWED
                )
                end_date_str = subscription.end_date.strftime('%d.%m.%Y') if subscription.end_date else ''
                await notification_delivery_service.send_notification(
                    user=user,
                    notification_type=notification_type,
                    context={
                        'expires_at': end_date_str,  # for SUBSCRIPTION_ACTIVATED
                        'new_expires_at': end_date_str,  # for SUBSCRIPTION_RENEWED
                        'traffic_limit_gb': subscription.traffic_limit_gb,
                        'device_limit': subscription.device_limit,
                        'tariff_name': '',  # classic mode has no tariff
                    },
                    bot=None,
                )
            except Exception as notif_error:
                logger.warning('Failed to send subscription notification to', email=user.email, notif_error=notif_error)

        # Отправляем уведомление админам о покупке подписки
        try:
            from aiogram import Bot

            from app.services.admin_notification_service import AdminNotificationService

            if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
                bot = Bot(token=settings.BOT_TOKEN)
                try:
                    notification_service = AdminNotificationService(bot)
                    is_new_subscription = result.get('was_trial_conversion') or not context.subscription
                    await notification_service.send_subscription_purchase_notification(
                        db=db,
                        user=user,
                        subscription=subscription,
                        transaction=result.get('transaction'),
                        period_days=selection.period.days,
                        was_trial_conversion=result.get('was_trial_conversion', False),
                        amount_kopeks=pricing.final_total,
                        purchase_type='renewal' if not is_new_subscription else 'first_purchase',
                    )
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error('Failed to send admin notification for subscription purchase', error=e)

        # Refresh expired objects after db.commit() in _record_subscription_event
        await db.refresh(subscription)

        return {
            'success': True,
            'message': result['message'],
            'subscription': _subscription_to_response(subscription, user=user),
            'was_trial_conversion': result.get('was_trial_conversion', False),
        }

    except PurchaseValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except PurchaseBalanceError as e:
        # Save cart for auto-purchase after balance top-up
        try:
            total_price = pricing.final_total if 'pricing' in locals() else 0
            cart_data = {
                'cart_mode': 'subscription_purchase',
                'period_id': request.selection.period_id,
                'period_days': request.selection.period_days,
                'traffic_gb': request.selection.traffic_value,  # _prepare_auto_purchase expects traffic_gb
                'countries': request.selection.servers,  # _prepare_auto_purchase expects countries
                'devices': request.selection.devices,
                'total_price': total_price,
                'user_id': user.id,
                'saved_cart': True,
                'return_to_cart': True,
                'source': 'cabinet',
            }
            await user_cart_service.save_user_cart(user.id, cart_data)
            logger.info('Cart saved for auto-purchase (cabinet /purchase) user', user_id=user.id)
        except Exception as cart_error:
            logger.error('Error saving cart for auto-purchase (cabinet /purchase)', cart_error=cart_error)

        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                'code': 'insufficient_funds',
                'message': str(e),
                'cart_saved': True,
                'cart_mode': 'subscription_purchase',
            },
        )
    except Exception as e:
        logger.error('Failed to submit purchase for user', user_id=user.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to process purchase',
        )


# ============ Tariff Purchase (for tariffs mode) ============


@router.post('/purchase-tariff')
async def purchase_tariff(
    request: TariffPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Purchase a tariff (for tariffs mode)."""
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Subscription purchases are restricted for this account',
        )

    try:
        # Check tariffs mode
        if not settings.is_tariffs_mode():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Tariffs mode is not enabled',
            )

        # Get tariff
        tariff = await get_tariff_by_id(db, request.tariff_id)
        if not tariff or not tariff.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Tariff not found or inactive',
            )

        # Lock user BEFORE price computation to prevent TOCTOU on promo offer
        from app.database.crud.user import lock_user_for_pricing

        user = await lock_user_for_pricing(db, user.id)

        # Check tariff availability for user's promo group and get promo group for discounts
        promo_group = user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None
        promo_group_id = promo_group.id if promo_group else None
        if not tariff.is_available_for_promo_group(promo_group_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='This tariff is not available for your promo group',
            )

        # Handle daily tariffs specially
        is_daily_tariff = getattr(tariff, 'is_daily', False)
        if is_daily_tariff:
            period_days = 1
        else:
            period_days = request.period_days

        # Validate period_days against tariff's configured periods (prevent arbitrary periods)
        if not is_daily_tariff:
            if tariff.period_prices:
                available_periods = [int(p) for p in tariff.period_prices.keys()]
            else:
                available_periods = []

            custom_days_allowed = (
                hasattr(tariff, 'can_purchase_custom_days')
                and tariff.can_purchase_custom_days()
                and hasattr(tariff, 'get_price_for_custom_days')
                and tariff.get_price_for_custom_days(period_days) is not None
            )

            if period_days not in available_periods and not custom_days_allowed:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='Selected period is not available for this tariff',
                )

        # Determine traffic limit (custom traffic support)
        traffic_limit_gb = tariff.traffic_limit_gb
        custom_traffic_gb = None
        if request.traffic_gb is not None and tariff.can_purchase_custom_traffic():
            custom_traffic_gb = request.traffic_gb
            traffic_limit_gb = request.traffic_gb

        # Determine device_limit for renewal pricing
        if settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_subscription_by_user_and_tariff

            existing_subscription = await get_subscription_by_user_and_tariff(db, user.id, tariff.id)
        else:
            existing_subscription = await get_subscription_by_user_id(db, user.id)
        device_limit = None
        effective_device_limit = tariff.device_limit
        if existing_subscription and existing_subscription.tariff_id == tariff.id:
            device_limit = existing_subscription.device_limit
            if (existing_subscription.device_limit or 0) > (tariff.device_limit or 0):
                effective_device_limit = existing_subscription.device_limit

        # Calculate price via PricingEngine (single source of truth)
        result = await pricing_engine.calculate_tariff_purchase_price(
            tariff,
            period_days,
            device_limit=device_limit,
            custom_traffic_gb=custom_traffic_gb,
            user=user,
        )
        price_kopeks = result.final_total
        original_price = result.original_total
        bd = result.breakdown
        group_pcts = bd.get('group_discount_pct', {})
        discount_percent = group_pcts.get('period', 0)
        promo_offer_discount_percent = bd.get('offer_discount_pct', 0)
        promo_offer_discount_value = result.promo_offer_discount
        price_before_promo_offer = price_kopeks + promo_offer_discount_value

        # Safety guard: reject zero-price purchases for non-daily tariffs (defense in depth)
        if price_kopeks <= 0 and result.base_price <= 0 and not is_daily_tariff:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid tariff period or pricing configuration',
            )

        # Check balance
        if price_kopeks > 0 and user.balance_kopeks < price_kopeks:
            missing = price_kopeks - user.balance_kopeks

            # Save cart for auto-purchase after balance top-up
            if is_daily_tariff:
                cart_data = {
                    'cart_mode': 'daily_tariff_purchase',
                    'tariff_id': tariff.id,
                    'is_daily': True,
                    'daily_price_kopeks': price_kopeks,
                    'total_price': price_kopeks,
                    'user_id': user.id,
                    'saved_cart': True,
                    'missing_amount': missing,
                    'return_to_cart': True,
                    'description': f'Покупка суточного тарифа {tariff.name}',
                    'traffic_limit_gb': tariff.traffic_limit_gb,
                    'device_limit': effective_device_limit,
                    'allowed_squads': tariff.allowed_squads or [],
                    'consume_promo_offer': promo_offer_discount_value > 0,
                    'source': 'cabinet',
                    'subscription_id': existing_subscription.id if existing_subscription else None,
                }
            else:
                cart_data = {
                    'cart_mode': 'tariff_purchase',
                    'tariff_id': tariff.id,
                    'period_days': period_days,
                    'total_price': price_kopeks,
                    'user_id': user.id,
                    'saved_cart': True,
                    'missing_amount': missing,
                    'return_to_cart': True,
                    'description': f'Покупка тарифа {tariff.name} на {period_days} дней',
                    'traffic_limit_gb': traffic_limit_gb,
                    'device_limit': effective_device_limit,
                    'allowed_squads': tariff.allowed_squads or [],
                    'discount_percent': discount_percent,
                    'consume_promo_offer': promo_offer_discount_value > 0,
                    'source': 'cabinet',
                    'subscription_id': existing_subscription.id if existing_subscription else None,
                }

            try:
                await user_cart_service.save_user_cart(user.id, cart_data)
                logger.info('Cart saved for auto-purchase (cabinet) user tariff', user_id=user.id, tariff_id=tariff.id)
            except Exception as e:
                logger.error('Error saving cart for auto-purchase (cabinet)', error=e)

            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    'code': 'insufficient_funds',
                    'message': f'Недостаточно средств. Не хватает {settings.format_price(missing)}',
                    'missing_amount': missing,
                    'cart_saved': True,
                    'cart_mode': cart_data['cart_mode'],
                },
            )

        subscription = existing_subscription

        # Get server squads from tariff
        squads = tariff.allowed_squads or []

        # If allowed_squads is empty, it means "all servers"
        if not squads:
            from app.database.crud.server_squad import get_all_server_squads

            all_servers, _ = await get_all_server_squads(db, available_only=True)
            squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

        # Charge balance
        if is_daily_tariff:
            description = f"Активация суточного тарифа '{tariff.name}'"
        else:
            description = f"Покупка тарифа '{tariff.name}' на {period_days} дней"
        if discount_percent > 0:
            description += f' (скидка {discount_percent}%)'
        if promo_offer_discount_value > 0:
            description += f' (промо -{promo_offer_discount_percent}%)'
        success = await subtract_user_balance(
            db,
            user,
            price_kopeks,
            description,
            consume_promo_offer=promo_offer_discount_value > 0,
            mark_as_paid_subscription=True,
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to charge balance',
            )

        # Create transaction
        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price_kopeks,
            description=description,
            payment_method=PaymentMethod.BALANCE,
        )

        # --- Trial cleanup: find and kill all trials BEFORE creating/extending ---
        from app.database.crud.subscription import deactivate_user_trial_subscriptions

        # Collect remaining trial seconds for TRIAL_ADD_REMAINING_DAYS_TO_PAID
        _bonus_seconds = 0
        _now_trial = datetime.now(UTC)
        killed_trials = await deactivate_user_trial_subscriptions(
            db,
            user.id,
            exclude_subscription_id=subscription.id if subscription else None,
        )
        if settings.TRIAL_ADD_REMAINING_DAYS_TO_PAID:
            for _kt in killed_trials:
                if _kt.end_date and _kt.end_date > _now_trial:
                    _bonus_seconds += max(0, (_kt.end_date - _now_trial).total_seconds())

        # If existing subscription IS the trial being extended — it's already deactivated
        # as trial by deactivate_user_trial_subscriptions (is_trial=False, status=DISABLED).
        # We need to re-activate it for extend to work correctly.
        if subscription and subscription.id in {kt.id for kt in killed_trials}:
            subscription.status = 'active'
            subscription.is_trial = False
            await db.flush()

        if subscription:
            # Extend/change tariff — сохраняем докупленные устройства при продлении того же тарифа
            subscription = await extend_subscription(
                db=db,
                subscription=subscription,
                days=period_days,
                tariff_id=tariff.id,
                traffic_limit_gb=traffic_limit_gb,
                device_limit=effective_device_limit,
                connected_squads=squads,
            )
        else:
            # Create new subscription
            try:
                subscription = await create_paid_subscription(
                    db=db,
                    user_id=user.id,
                    duration_days=period_days,
                    traffic_limit_gb=traffic_limit_gb,
                    device_limit=tariff.device_limit,
                    connected_squads=squads,
                    tariff_id=tariff.id,
                )
            except IntegrityError:
                # Partial unique index violation: user already has active subscription for this tariff
                logger.warning(
                    'Cabinet purchase: tariff already active (IntegrityError), refunding',
                    tariff_id=tariff.id,
                    user_id=user.id,
                )
                await db.rollback()
                await add_user_balance(
                    db,
                    user,
                    price_kopeks,
                    f"Возврат: тариф '{tariff.name}' уже активен",
                    create_transaction=True,
                    transaction_type=TransactionType.REFUND,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail='You already have an active subscription for this tariff',
                )

        # Add remaining trial time to paid subscription
        if _bonus_seconds > 0 and subscription:
            subscription.end_date = subscription.end_date + timedelta(seconds=_bonus_seconds)
            await db.commit()
            await db.refresh(subscription)
            logger.info(
                'Added remaining trial time to paid subscription',
                bonus_seconds=int(_bonus_seconds),
                subscription_id=subscription.id,
            )

        # For daily tariffs, set last_daily_charge_at
        if is_daily_tariff:
            subscription.last_daily_charge_at = datetime.now(UTC)
            subscription.is_daily_paused = False
            await db.commit()
            await db.refresh(subscription)

        # --- Disable killed trials on RemnaWave panel ---
        service = SubscriptionService()
        for trial_sub in killed_trials:
            if trial_sub.id == (subscription.id if subscription else None):
                continue  # This trial became the paid subscription, don't disable
            try:
                _trial_uuid = trial_sub.remnawave_uuid or (
                    getattr(user, 'remnawave_uuid', None) if not settings.is_multi_tariff_enabled() else None
                )
                if _trial_uuid:
                    await service.disable_remnawave_user(_trial_uuid)
                await decrement_subscription_server_counts(db, trial_sub)
            except Exception as trial_err:
                logger.warning('Failed to disable trial on RemnaWave', error=trial_err, trial_id=trial_sub.id)
        try:
            if subscription.remnawave_uuid:
                # Existing subscription with Remnawave user — update it
                await service.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=True,
                    reset_reason='покупка тарифа (cabinet)',
                    sync_squads=True,
                )
            else:
                # New subscription — create new Remnawave user
                await service.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=True,
                    reset_reason='покупка тарифа (cabinet)',
                )
        except Exception as remnawave_error:
            logger.error('Failed to sync subscription with RemnaWave', remnawave_error=remnawave_error)

        # Save cart for auto-renewal (not for daily tariffs - they have their own charging)
        if not is_daily_tariff:
            try:
                cart_data = {
                    'cart_mode': 'extend',
                    'subscription_id': subscription.id,
                    'period_days': period_days,
                    'total_price': price_kopeks,
                    'tariff_id': tariff.id,
                    'description': f'Продление тарифа {tariff.name} на {period_days} дней',
                }
                await user_cart_service.save_user_cart(user.id, cart_data)
                logger.info('Tariff cart saved for auto-renewal (cabinet) user', user_id=user.id)
            except Exception as e:
                logger.error('Error saving tariff cart (cabinet)', error=e)

        await db.refresh(user)
        await db.refresh(subscription)

        response: dict[str, Any] = {
            'success': True,
            'message': f"Тариф '{tariff.name}' успешно активирован",
            'subscription': _subscription_to_response(subscription, user=user),
            'tariff_id': tariff.id,
            'tariff_name': tariff.name,
            'charged_amount': price_kopeks,
            'charged_label': settings.format_price(price_kopeks),
            'balance_kopeks': user.balance_kopeks,
            'balance_label': settings.format_price(user.balance_kopeks),
        }

        # Add discount info if discount was applied
        if discount_percent > 0:
            response['discount_percent'] = discount_percent
            response['original_price_kopeks'] = original_price
            response['original_price_label'] = settings.format_price(original_price)
            response['discount_amount_kopeks'] = original_price - price_before_promo_offer
            response['discount_label'] = settings.format_price(original_price - price_before_promo_offer)
            if promo_group:
                response['promo_group_name'] = promo_group.name

        # Add promo offer discount info if it was applied
        if promo_offer_discount_value > 0:
            response['promo_offer_discount_percent'] = promo_offer_discount_percent
            response['promo_offer_discount_amount_kopeks'] = promo_offer_discount_value
            response['promo_offer_discount_label'] = settings.format_price(promo_offer_discount_value)
            response['price_before_promo_offer_kopeks'] = price_before_promo_offer

        # Send email notification for email-only users
        if not user.telegram_id and user.email and user.email_verified:
            try:
                # Determine if this is a new subscription or extension
                was_new_subscription = (
                    subscription.start_date and (datetime.now(UTC) - subscription.start_date).total_seconds() < 60
                )
                notification_type = (
                    NotificationType.SUBSCRIPTION_ACTIVATED
                    if was_new_subscription
                    else NotificationType.SUBSCRIPTION_RENEWED
                )
                end_date_str = subscription.end_date.strftime('%d.%m.%Y') if subscription.end_date else ''
                await notification_delivery_service.send_notification(
                    user=user,
                    notification_type=notification_type,
                    context={
                        'expires_at': end_date_str,  # for SUBSCRIPTION_ACTIVATED
                        'new_expires_at': end_date_str,  # for SUBSCRIPTION_RENEWED
                        'traffic_limit_gb': subscription.traffic_limit_gb,
                        'device_limit': subscription.device_limit,
                        'tariff_name': tariff.name,
                    },
                    bot=None,
                )
            except Exception as notif_error:
                logger.warning('Failed to send subscription notification to', email=user.email, notif_error=notif_error)

        # Отправляем уведомление админам о покупке/продлении тарифа
        try:
            from aiogram import Bot

            from app.services.admin_notification_service import AdminNotificationService

            if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
                bot = Bot(token=settings.BOT_TOKEN)
                try:
                    notification_service = AdminNotificationService(bot)
                    # Определяем тип покупки: новая подписка или продление
                    was_new_subscription = (
                        subscription.start_date and (datetime.now(UTC) - subscription.start_date).total_seconds() < 60
                    )
                    await notification_service.send_subscription_purchase_notification(
                        db=db,
                        user=user,
                        subscription=subscription,
                        transaction=transaction,
                        period_days=period_days,
                        was_trial_conversion=False,
                        amount_kopeks=price_kopeks,
                        purchase_type='renewal' if not was_new_subscription else 'first_purchase',
                    )
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error('Failed to send admin notification for tariff purchase', error=e)

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to purchase tariff for user', user_id=user.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to process tariff purchase',
        )


# ============ Trial ============


@router.get('/trial', response_model=TrialInfoResponse)
async def get_trial_info(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get trial subscription info and availability."""
    await db.refresh(user, ['subscriptions'])

    # Проверяем, отключён ли триал для этого типа пользователя
    if settings.is_trial_disabled_for_user(getattr(user, 'auth_type', 'telegram')):
        return TrialInfoResponse(
            is_available=False,
            duration_days=settings.TRIAL_DURATION_DAYS,
            traffic_limit_gb=settings.TRIAL_TRAFFIC_LIMIT_GB,
            device_limit=settings.TRIAL_DEVICE_LIMIT,
            requires_payment=bool(settings.TRIAL_PAYMENT_ENABLED),
            price_kopeks=0,
            price_rubles=0,
            reason_unavailable='Trial is not available for your account type',
        )

    duration_days = settings.TRIAL_DURATION_DAYS
    traffic_limit_gb = settings.TRIAL_TRAFFIC_LIMIT_GB
    device_limit = settings.TRIAL_DEVICE_LIMIT
    requires_payment = bool(settings.TRIAL_PAYMENT_ENABLED)
    price_kopeks = settings.TRIAL_ACTIVATION_PRICE if requires_payment else 0

    # Get trial parameters from tariff if configured (same logic as activate_trial)
    try:
        from app.database.crud.tariff import get_tariff_by_id, get_trial_tariff

        trial_tariff = await get_trial_tariff(db)

        if not trial_tariff:
            trial_tariff_id = settings.get_trial_tariff_id()
            if trial_tariff_id > 0:
                trial_tariff = await get_tariff_by_id(db, trial_tariff_id)
                if trial_tariff and not trial_tariff.is_active:
                    trial_tariff = None

        if trial_tariff:
            traffic_limit_gb = trial_tariff.traffic_limit_gb
            device_limit = trial_tariff.device_limit
            tariff_trial_days = getattr(trial_tariff, 'trial_duration_days', None)
            if tariff_trial_days:
                duration_days = tariff_trial_days
    except Exception as e:
        logger.error('Error getting trial tariff for info', error=e)

    # Check if user already has an active subscription
    subs = getattr(user, 'subscriptions', None) or []
    has_active = any(s.status == 'active' and s.end_date and s.end_date > datetime.now(UTC) for s in subs)
    has_used_trial = any(s.is_trial for s in subs) or user.has_had_paid_subscription

    if has_active:
        return TrialInfoResponse(
            is_available=False,
            duration_days=duration_days,
            traffic_limit_gb=traffic_limit_gb,
            device_limit=device_limit,
            requires_payment=requires_payment,
            price_kopeks=price_kopeks,
            price_rubles=price_kopeks / 100,
            reason_unavailable='You already have an active subscription',
        )

    if has_used_trial:
        return TrialInfoResponse(
            is_available=False,
            duration_days=duration_days,
            traffic_limit_gb=traffic_limit_gb,
            device_limit=device_limit,
            requires_payment=requires_payment,
            price_kopeks=price_kopeks,
            price_rubles=price_kopeks / 100,
            reason_unavailable='Trial already used',
        )

    return TrialInfoResponse(
        is_available=True,
        duration_days=duration_days,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=device_limit,
        requires_payment=requires_payment,
        price_kopeks=price_kopeks,
        price_rubles=price_kopeks / 100,
    )


@router.post('/trial', response_model=SubscriptionResponse)
async def activate_trial(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Activate trial subscription."""
    await db.refresh(user, ['subscriptions'])

    # Проверяем, отключён ли триал для этого типа пользователя
    if settings.is_trial_disabled_for_user(getattr(user, 'auth_type', 'telegram')):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Trial is not available for your account type',
        )

    # Check if user already has an active subscription
    subs = getattr(user, 'subscriptions', None) or []
    has_active = any(s.status == 'active' and s.end_date and s.end_date > datetime.now(UTC) for s in subs)
    if has_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='You already have an active subscription',
        )

    # Check if user already used trial
    if any(s.is_trial for s in subs) or user.has_had_paid_subscription:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Trial already used',
        )

    # Check if trial requires payment
    requires_payment = bool(settings.TRIAL_PAYMENT_ENABLED)
    if requires_payment:
        from app.database.crud.user import subtract_user_balance

        price_kopeks = settings.TRIAL_ACTIVATION_PRICE
        if price_kopeks > 0 and user.balance_kopeks < price_kopeks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Insufficient balance. Need {price_kopeks / 100:.2f} RUB',
            )
        trial_description = 'Активация триальной подписки'
        success = await subtract_user_balance(
            db,
            user,
            price_kopeks,
            trial_description,
            mark_as_paid_subscription=True,
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail='Failed to charge trial activation fee',
            )

        # Создаём транзакцию для учёта списания за триал
        await create_transaction(
            db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price_kopeks,
            description=trial_description,
            payment_method=PaymentMethod.BALANCE,
        )

        logger.info('User paid kopeks for trial activation', user_id=user.id, price_kopeks=price_kopeks)

    # Get trial parameters from tariff if configured (same logic as bot handler)
    trial_duration = settings.TRIAL_DURATION_DAYS
    trial_traffic_limit = settings.TRIAL_TRAFFIC_LIMIT_GB
    trial_device_limit = settings.TRIAL_DEVICE_LIMIT
    trial_squads = []
    tariff_id_for_trial = None

    # First check for tariff with is_trial_available flag in DB (set via admin panel)
    # Then fallback to TRIAL_TARIFF_ID from settings
    trial_tariff = None
    try:
        from app.database.crud.tariff import get_tariff_by_id, get_trial_tariff

        trial_tariff = await get_trial_tariff(db)

        if not trial_tariff:
            trial_tariff_id = settings.get_trial_tariff_id()
            if trial_tariff_id > 0:
                trial_tariff = await get_tariff_by_id(db, trial_tariff_id)
                if trial_tariff and not trial_tariff.is_active:
                    trial_tariff = None

        if trial_tariff:
            trial_traffic_limit = trial_tariff.traffic_limit_gb
            trial_device_limit = trial_tariff.device_limit
            trial_squads = trial_tariff.allowed_squads or []
            tariff_id_for_trial = trial_tariff.id
            tariff_trial_days = getattr(trial_tariff, 'trial_duration_days', None)
            if tariff_trial_days:
                trial_duration = tariff_trial_days
            logger.info(
                'Using trial tariff (ID: ) with squads',
                trial_tariff_name=trial_tariff.name,
                trial_tariff_id=trial_tariff.id,
                trial_squads=trial_squads,
            )
    except Exception as e:
        logger.error('Error getting trial tariff', error=e)

    # Create trial subscription
    subscription = await create_trial_subscription(
        db=db,
        user_id=user.id,
        duration_days=trial_duration,
        traffic_limit_gb=trial_traffic_limit,
        device_limit=trial_device_limit,
        connected_squads=trial_squads or None,
        tariff_id=tariff_id_for_trial,
    )

    logger.info('Trial subscription activated for user', user_id=user.id)

    # Create RemnaWave user
    try:
        subscription_service = SubscriptionService()
        if subscription_service.is_configured:
            await subscription_service.create_remnawave_user(db, subscription)
            await db.refresh(subscription)
    except Exception as e:
        logger.error('Failed to create RemnaWave user for trial', error=e)

    # Send admin notification about trial activation
    try:
        from aiogram import Bot

        from app.services.admin_notification_service import AdminNotificationService

        if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
            bot = Bot(token=settings.BOT_TOKEN)
            try:
                notification_service = AdminNotificationService(bot)
                charged_amount = settings.TRIAL_ACTIVATION_PRICE if requires_payment else None
                await notification_service.send_trial_activation_notification(
                    db, user, subscription, charged_amount_kopeks=charged_amount
                )
            finally:
                await bot.session.close()
    except Exception as e:
        logger.error('Failed to send trial activation notification', error=e)

    return _subscription_to_response(subscription, user=user)
