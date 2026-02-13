"""Subscription management routes for cabinet."""

import base64
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.crud.server_squad import get_server_squad_by_uuid
from app.database.crud.subscription import (
    create_paid_subscription,
    create_trial_subscription,
    extend_subscription,
    get_subscription_by_user_id,
)
from app.database.crud.tariff import get_tariff_by_id, get_tariffs_for_user
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import ServerSquad, Subscription, Tariff, TransactionType, User
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service,
)
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_purchase_service import (
    MiniAppSubscriptionPurchaseService,
    PurchaseBalanceError,
    PurchaseValidationError,
)
from app.services.subscription_service import SubscriptionService
from app.services.system_settings_service import bot_configuration_service
from app.services.user_cart_service import user_cart_service
from app.utils.cache import RateLimitCache, cache, cache_key
from app.utils.pricing_utils import format_period_description
from app.utils.promo_offer import get_user_active_promo_discount_percent

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.subscription import (
    AutopayUpdateRequest,
    DevicePurchaseRequest,
    PurchasePreviewRequest,
    RenewalOptionResponse,
    RenewalRequest,
    ServerInfo,
    SubscriptionData,
    SubscriptionResponse,
    SubscriptionStatusResponse,
    TariffPurchaseRequest,
    TrafficPackageResponse,
    TrafficPurchaseRequest,
    TrialInfoResponse,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix='/subscription', tags=['Cabinet Subscription'])


def _get_addon_discount_percent(
    user: User,
    category: str,
    period_days: int | None = None,
) -> int:
    """Get addon discount percent for user from promo group.

    Mirrors logic from app/handlers/subscription/common.py:_get_addon_discount_percent_for_user
    """
    promo_group = (
        user.get_primary_promo_group()
        if hasattr(user, 'get_primary_promo_group')
        else getattr(user, 'promo_group', None)
    )
    if promo_group is None:
        return 0

    if not getattr(promo_group, 'apply_discounts_to_addons', True):
        return 0

    try:
        return user.get_promo_discount(category, period_days)
    except AttributeError:
        return 0


def _apply_addon_discount(
    user: User,
    category: str,
    amount: int,
    period_days: int | None = None,
) -> dict[str, int]:
    """Apply addon discount to amount.

    Returns dict with keys: discounted, discount, percent
    """
    percent = _get_addon_discount_percent(user, category, period_days)
    if percent <= 0 or amount <= 0:
        return {'discounted': amount, 'discount': 0, 'percent': 0}

    discount_value = int(amount * percent / 100)
    discounted_amount = amount - discount_value
    return {
        'discounted': discounted_amount,
        'discount': discount_value,
        'percent': percent,
    }


def _get_period_discount_percent(user: User, period_days: int | None = None) -> int:
    """Get period discount percent for tariff switch calculations."""
    promo_group = (
        user.get_primary_promo_group()
        if hasattr(user, 'get_primary_promo_group')
        else getattr(user, 'promo_group', None)
    )
    if promo_group is None:
        return 0

    try:
        return user.get_promo_discount('period', period_days)
    except AttributeError:
        return 0


def _subscription_to_response(
    subscription: Subscription,
    servers: list[ServerInfo] | None = None,
    tariff_name: str | None = None,
    traffic_purchases: list[dict[str, Any]] | None = None,
) -> SubscriptionData:
    """Convert Subscription model to response."""
    now = datetime.utcnow()

    # Use actual_status property for correct status (same as bot uses)
    actual_status = subscription.actual_status
    is_expired = actual_status == 'expired'
    is_active = actual_status in ('active', 'trial')

    # Calculate time remaining
    days_left = 0
    hours_left = 0
    minutes_left = 0
    time_left_display = ''

    if subscription.end_date and not is_expired:
        time_delta = subscription.end_date - now
        total_seconds = max(0, int(time_delta.total_seconds()))

        days_left = total_seconds // 86400  # 86400 seconds in a day
        remaining_seconds = total_seconds % 86400
        hours_left = remaining_seconds // 3600
        minutes_left = (remaining_seconds % 3600) // 60

        # Create human-readable display
        if days_left > 0:
            time_left_display = f'{days_left}d {hours_left}h'
        elif hours_left > 0:
            time_left_display = f'{hours_left}h {minutes_left}m'
        elif minutes_left > 0:
            time_left_display = f'{minutes_left}m'
        else:
            time_left_display = '0m'
    else:
        time_left_display = '0m'

    traffic_limit_gb = subscription.traffic_limit_gb or 0
    traffic_used_gb = subscription.traffic_used_gb or 0.0

    if traffic_limit_gb > 0:
        traffic_used_percent = min(100, (traffic_used_gb / traffic_limit_gb) * 100)
    else:
        traffic_used_percent = 0

    # Check if this is a daily tariff
    is_daily_paused = getattr(subscription, 'is_daily_paused', False) or False
    tariff_id = getattr(subscription, 'tariff_id', None)

    # Use subscription's is_daily_tariff property if available
    is_daily = False
    daily_price_kopeks = None

    if hasattr(subscription, 'is_daily_tariff'):
        is_daily = subscription.is_daily_tariff
    elif tariff_id and hasattr(subscription, 'tariff') and subscription.tariff:
        is_daily = getattr(subscription.tariff, 'is_daily', False)

    # Get daily_price_kopeks and tariff_name from tariff (separate from is_daily check)
    if tariff_id and hasattr(subscription, 'tariff') and subscription.tariff:
        daily_price_kopeks = getattr(subscription.tariff, 'daily_price_kopeks', None)
        if not tariff_name:  # Only set if not passed as parameter
            tariff_name = getattr(subscription.tariff, 'name', None)

    # Calculate next daily charge time (24 hours after last charge)
    next_daily_charge_at = None
    if is_daily and not is_daily_paused:
        last_charge = getattr(subscription, 'last_daily_charge_at', None)
        if last_charge:
            next_daily_charge_at = last_charge + timedelta(days=1)

    # Проверяем настройку скрытия ссылки (скрывается только текст, кнопки работают)
    hide_link = settings.should_hide_subscription_link()

    return SubscriptionResponse(
        id=subscription.id,
        status=actual_status,  # Use actual_status instead of raw status
        is_trial=subscription.is_trial or actual_status == 'trial',
        start_date=subscription.start_date,
        end_date=subscription.end_date,
        days_left=days_left,
        hours_left=hours_left,
        minutes_left=minutes_left,
        time_left_display=time_left_display,
        traffic_limit_gb=traffic_limit_gb,
        traffic_used_gb=round(traffic_used_gb, 2),
        traffic_used_percent=round(traffic_used_percent, 1),
        device_limit=subscription.device_limit or 1,
        connected_squads=subscription.connected_squads or [],
        servers=servers or [],
        autopay_enabled=subscription.autopay_enabled or False,
        autopay_days_before=subscription.autopay_days_before or 3,
        subscription_url=subscription.subscription_url,
        hide_subscription_link=hide_link,
        is_active=is_active,
        is_expired=is_expired,
        traffic_purchases=traffic_purchases or [],
        is_daily=is_daily,
        is_daily_paused=is_daily_paused,
        daily_price_kopeks=daily_price_kopeks,
        next_daily_charge_at=next_daily_charge_at,
        tariff_id=tariff_id,
        tariff_name=tariff_name,
    )


@router.get('', response_model=SubscriptionStatusResponse)
async def get_subscription(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get current user's subscription details."""
    # Reload user from current session to get fresh data
    # (user object is from different session in get_current_cabinet_user)
    from app.database.crud.user import get_user_by_id

    fresh_user = await get_user_by_id(db, user.id)

    if not fresh_user or not fresh_user.subscription:
        # Return 200 with has_subscription: false instead of 404
        return SubscriptionStatusResponse(has_subscription=False, subscription=None)

    # Load tariff for daily subscription check and tariff name
    tariff_name = None
    if fresh_user.subscription.tariff_id:
        tariff = await get_tariff_by_id(db, fresh_user.subscription.tariff_id)
        if tariff:
            fresh_user.subscription.tariff = tariff
            tariff_name = tariff.name

    # Fetch server names for connected squads
    servers: list[ServerInfo] = []
    connected_squads = fresh_user.subscription.connected_squads or []
    if connected_squads:
        result = await db.execute(select(ServerSquad).where(ServerSquad.squad_uuid.in_(connected_squads)))
        server_squads = result.scalars().all()
        servers = [
            ServerInfo(uuid=sq.squad_uuid, name=sq.display_name, country_code=sq.country_code) for sq in server_squads
        ]

    # Fetch traffic purchases (monthly packages)
    traffic_purchases_data = []
    from app.database.models import TrafficPurchase

    now = datetime.utcnow()
    purchases_query = (
        select(TrafficPurchase)
        .where(TrafficPurchase.subscription_id == fresh_user.subscription.id)
        .where(TrafficPurchase.expires_at > now)
        .order_by(TrafficPurchase.expires_at.asc())
    )
    purchases_result = await db.execute(purchases_query)
    purchases = purchases_result.scalars().all()

    for purchase in purchases:
        time_remaining = purchase.expires_at - now
        days_remaining = max(0, int(time_remaining.total_seconds() / 86400))
        total_duration_seconds = (purchase.expires_at - purchase.created_at).total_seconds()
        elapsed_seconds = (now - purchase.created_at).total_seconds()
        progress_percent = min(
            100.0, max(0.0, (elapsed_seconds / total_duration_seconds * 100) if total_duration_seconds > 0 else 0)
        )

        traffic_purchases_data.append(
            {
                'id': purchase.id,
                'traffic_gb': purchase.traffic_gb,
                'expires_at': purchase.expires_at,
                'created_at': purchase.created_at,
                'days_remaining': days_remaining,
                'progress_percent': round(progress_percent, 1),
            }
        )

    subscription_data = _subscription_to_response(fresh_user.subscription, servers, tariff_name, traffic_purchases_data)
    return SubscriptionStatusResponse(has_subscription=True, subscription=subscription_data)


@router.get('/renewal-options', response_model=list[RenewalOptionResponse])
async def get_renewal_options(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get available subscription renewal options with prices."""
    options = []

    # В режиме тарифов берём цены из тарифа пользователя
    tariff_prices = None
    tariff_periods = None
    extra_devices = 0
    tariff_device_price = 0
    if settings.is_tariffs_mode():
        subscription = await get_subscription_by_user_id(db, user.id)
        if subscription and subscription.tariff_id:
            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff and tariff.period_prices:
                tariff_prices = {int(k): v for k, v in tariff.period_prices.items()}
                tariff_periods = sorted(tariff_prices.keys())
                # Учитываем докупленные устройства сверх тарифа
                extra_devices = max(0, (subscription.device_limit or 0) - (tariff.device_limit or 0))
                if extra_devices > 0:
                    tariff_device_price = tariff.device_price_kopeks or settings.PRICE_PER_DEVICE

    # Используем периоды тарифа или стандартные
    if tariff_periods:
        periods = tariff_periods
    else:
        periods = settings.get_available_renewal_periods()

    for period in periods:
        # Получаем цену из тарифа или из PERIOD_PRICES
        if tariff_prices and period in tariff_prices:
            price_kopeks = tariff_prices[period]
        else:
            price_kopeks = PERIOD_PRICES.get(period, 0)

        if price_kopeks <= 0:
            continue

        # Добавляем стоимость докупленных устройств за период продления
        if extra_devices > 0 and tariff_device_price > 0:
            from app.utils.pricing_utils import calculate_months_from_days

            months = calculate_months_from_days(period)
            price_kopeks += extra_devices * tariff_device_price * months

        # Apply user's discount if any
        discount_percent = 0
        if hasattr(user, 'get_promo_discount'):
            discount_percent = user.get_promo_discount('period', period)

        if discount_percent > 0:
            original_price = price_kopeks
            price_kopeks = int(price_kopeks * (100 - discount_percent) / 100)
        else:
            original_price = None

        options.append(
            RenewalOptionResponse(
                period_days=period,
                price_kopeks=price_kopeks,
                price_rubles=price_kopeks / 100,
                discount_percent=discount_percent,
                original_price_kopeks=original_price,
            )
        )

    return options


@router.post('/renew')
async def renew_subscription(
    request: RenewalRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Renew subscription (pay from balance)."""
    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    # В режиме тарифов берём цену из тарифа пользователя
    price_kopeks = 0
    tariff = None
    if settings.is_tariffs_mode() and user.subscription.tariff_id:
        tariff = await get_tariff_by_id(db, user.subscription.tariff_id)
        if tariff and tariff.period_prices:
            price_kopeks = tariff.period_prices.get(str(request.period_days), 0)

    # Fallback на PERIOD_PRICES
    if price_kopeks <= 0:
        price_kopeks = PERIOD_PRICES.get(request.period_days, 0)

    if price_kopeks <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid renewal period',
        )

    # Добавляем стоимость докупленных устройств сверх тарифа
    if tariff:
        extra_devices = max(0, (user.subscription.device_limit or 0) - (tariff.device_limit or 0))
        if extra_devices > 0:
            from app.utils.pricing_utils import calculate_months_from_days

            device_price = tariff.device_price_kopeks or settings.PRICE_PER_DEVICE
            months = calculate_months_from_days(request.period_days)
            price_kopeks += extra_devices * device_price * months

    # Apply promo group discount
    original_price_kopeks = price_kopeks
    promo_group_discount_percent = 0
    if hasattr(user, 'get_promo_discount'):
        promo_group_discount_percent = user.get_promo_discount('period', request.period_days)

    if promo_group_discount_percent > 0:
        price_kopeks = int(price_kopeks * (100 - promo_group_discount_percent) / 100)

    # Apply promo offer discount (temporary discount from promo offers)
    promo_offer_discount_percent = get_user_active_promo_discount_percent(user)
    promo_offer_discount_value = 0
    if promo_offer_discount_percent > 0:
        promo_offer_discount_value = price_kopeks * promo_offer_discount_percent // 100
        price_kopeks = price_kopeks - promo_offer_discount_value

    # Combined discount percent for display
    discount_percent = promo_group_discount_percent
    if promo_offer_discount_percent > 0 and original_price_kopeks > 0:
        total_discount = original_price_kopeks - price_kopeks
        discount_percent = int(total_discount * 100 / original_price_kopeks)

    # Check balance
    if user.balance_kopeks < price_kopeks:
        missing = price_kopeks - user.balance_kopeks

        # Get tariff info for cart
        tariff_id = user.subscription.tariff_id
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
        cart_data = {
            'cart_mode': 'extend',
            'subscription_id': user.subscription.id,
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
            'source': 'cabinet',
        }

        # Add tariff parameters for tariffs mode
        if tariff_id:
            cart_data['traffic_limit_gb'] = tariff_traffic_limit_gb
            # Сохраняем актуальный device_limit подписки (включая докупленные устройства)
            cart_data['device_limit'] = user.subscription.device_limit
            cart_data['allowed_squads'] = tariff_allowed_squads

        try:
            await user_cart_service.save_user_cart(user.id, cart_data)
            logger.info(f'Cart saved for auto-renewal (cabinet) user {user.id}')
        except Exception as e:
            logger.error(f'Error saving cart for auto-renewal (cabinet): {e}')

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

    # Deduct balance and extend subscription
    user.balance_kopeks -= price_kopeks

    # Consume promo offer discount if it was used
    if promo_offer_discount_value > 0:
        user.promo_offer_discount_percent = 0
        user.promo_offer_discount_source = None
        user.promo_offer_discount_expires_at = None

    # Extend from end_date or now if expired
    now = datetime.utcnow()
    if user.subscription.end_date and user.subscription.end_date > now:
        user.subscription.end_date = user.subscription.end_date + timedelta(days=request.period_days)
    else:
        user.subscription.end_date = now + timedelta(days=request.period_days)
        user.subscription.start_date = now

    user.subscription.status = 'active'
    user.subscription.is_trial = False

    await db.commit()

    # Отправляем уведомление админам о продлении подписки
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
                    subscription=user.subscription,
                    transaction=None,
                    period_days=request.period_days,
                    was_trial_conversion=False,
                    amount_kopeks=price_kopeks,
                    purchase_type='renewal',
                )
            finally:
                await bot.session.close()
    except Exception as e:
        logger.error(f'Failed to send admin notification for subscription renewal: {e}')

    response = {
        'message': 'Subscription renewed successfully',
        'new_end_date': user.subscription.end_date.isoformat(),
        'amount_paid_kopeks': price_kopeks,
    }

    # Add discount info to response
    if promo_offer_discount_value > 0:
        response['promo_discount_percent'] = promo_offer_discount_percent
        response['promo_discount_amount_kopeks'] = promo_offer_discount_value
        response['original_price_kopeks'] = original_price_kopeks

    return response


@router.get('/traffic-packages', response_model=list[TrafficPackageResponse])
async def get_traffic_packages(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get available traffic packages."""
    from app.database.crud.tariff import get_tariff_by_id
    from app.database.crud.user import get_user_by_id

    fresh_user = await get_user_by_id(db, user.id)
    if not fresh_user or not fresh_user.subscription:
        return []

    # Режим тарифов - берём пакеты из тарифа
    if settings.is_tariffs_mode() and fresh_user.subscription.tariff_id:
        tariff = await get_tariff_by_id(db, fresh_user.subscription.tariff_id)
        if not tariff:
            return []

        # Проверяем, разрешена ли докупка для этого тарифа
        if not getattr(tariff, 'traffic_topup_enabled', False):
            return []

        # Проверяем безлимит
        if tariff.traffic_limit_gb == 0:
            return []

        packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
        result = []

        for gb, price in packages.items():
            result.append(
                TrafficPackageResponse(
                    gb=gb,
                    price_kopeks=price,
                    price_rubles=price / 100,
                    is_unlimited=False,
                )
            )

        return sorted(result, key=lambda x: x.gb)

    # Classic режим - глобальные настройки
    if not settings.is_traffic_topup_enabled():
        return []

    # Проверяем настройку тарифа пользователя (allow_traffic_topup)
    if fresh_user.subscription.tariff_id:
        tariff = await get_tariff_by_id(db, fresh_user.subscription.tariff_id)
        if tariff and not tariff.allow_traffic_topup:
            return []

    packages = settings.get_traffic_packages()
    result = []

    for pkg in packages:
        if not pkg.get('enabled', True):
            continue

        result.append(
            TrafficPackageResponse(
                gb=pkg['gb'],
                price_kopeks=pkg['price'],
                price_rubles=pkg['price'] / 100,
                is_unlimited=pkg['gb'] == 0,
            )
        )

    return result


@router.post('/traffic')
async def purchase_traffic(
    request: TrafficPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Purchase additional traffic."""
    from app.database.crud.subscription import add_subscription_traffic
    from app.database.crud.tariff import get_tariff_by_id
    from app.utils.pricing_utils import calculate_prorated_price

    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    subscription = user.subscription
    tariff = None
    base_price_kopeks = 0
    is_tariff_mode = settings.is_tariffs_mode() and subscription.tariff_id

    # Режим тарифов
    if is_tariff_mode:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if not tariff:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Tariff not found',
            )

        # Проверяем, разрешена ли докупка
        if not getattr(tariff, 'traffic_topup_enabled', False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Traffic top-up is disabled for this tariff',
            )

        # Проверяем безлимит
        if tariff.traffic_limit_gb == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Cannot add traffic to unlimited subscription',
            )

        # Проверяем лимит докупки
        max_topup_limit = getattr(tariff, 'max_topup_traffic_gb', 0) or 0
        if max_topup_limit > 0:
            current_traffic = subscription.traffic_limit_gb or 0
            new_traffic = current_traffic + request.gb
            if new_traffic > max_topup_limit:
                available_gb = max(0, max_topup_limit - current_traffic)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Traffic limit exceeded. Max: {max_topup_limit} GB, available: {available_gb} GB',
                )

        # Получаем цену из тарифа
        packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
        if request.gb not in packages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Traffic package {request.gb}GB is not available',
            )
        base_price_kopeks = packages[request.gb]

    else:
        # Classic режим
        if not settings.is_traffic_topup_enabled():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Traffic top-up feature is disabled',
            )

        # Проверяем настройку тарифа (allow_traffic_topup)
        if subscription.tariff_id:
            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff and not tariff.allow_traffic_topup:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='Traffic top-up is not available for your tariff',
                )

        # Получаем цену из глобальных настроек
        packages = settings.get_traffic_packages()
        matching_pkg = next((pkg for pkg in packages if pkg['gb'] == request.gb and pkg.get('enabled', True)), None)
        if not matching_pkg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid traffic package',
            )
        base_price_kopeks = matching_pkg['price']

    # На тарифах пакеты трафика покупаются на 1 месяц (30 дней),
    # цена в тарифе уже месячная — не умножаем на оставшиеся месяцы подписки.
    # Пропорциональный расчёт применяем только в классическом режиме.
    if is_tariff_mode:
        prorated_price = base_price_kopeks
        months_charged = 1
    else:
        prorated_price, months_charged = calculate_prorated_price(
            base_price_kopeks,
            subscription.end_date,
        )

    # Apply discount from promo group using proper method
    period_hint_days = months_charged * 30 if months_charged > 0 else 30
    discount_result = _apply_addon_discount(user, 'traffic', prorated_price, period_hint_days)
    final_price = discount_result['discounted']
    traffic_discount_percent = discount_result['percent']
    discount_value = discount_result['discount']

    # Ensure minimum price after discount (except for 100% discount)
    if traffic_discount_percent < 100 and final_price > 0:
        final_price = max(100, final_price)

    # Проверяем баланс
    if user.balance_kopeks < final_price:
        missing = final_price - user.balance_kopeks

        # Save cart for auto-purchase after balance top-up
        cart_data = {
            'cart_mode': 'add_traffic',
            'subscription_id': subscription.id,
            'traffic_gb': request.gb,
            'price_kopeks': final_price,
            'base_price_kopeks': prorated_price,
            'discount_percent': traffic_discount_percent,
            'source': 'cabinet',
            'description': f'Докупка {request.gb} ГБ трафика',
        }

        try:
            await user_cart_service.save_user_cart(user.id, cart_data)
            logger.info(
                f'Cart saved for traffic purchase (cabinet) user {user.id}: '
                f'+{request.gb} GB, discount {traffic_discount_percent}%'
            )
        except Exception as e:
            logger.error(f'Error saving cart for traffic purchase (cabinet): {e}')

        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                'code': 'insufficient_funds',
                'message': f'Недостаточно средств. Не хватает {settings.format_price(missing)}',
                'missing_amount': missing,
                'cart_saved': True,
                'cart_mode': 'add_traffic',
            },
        )

    # Формируем описание
    if traffic_discount_percent > 0:
        traffic_description = f'Докупка {request.gb} ГБ трафика (скидка {traffic_discount_percent}%)'
    else:
        traffic_description = f'Докупка {request.gb} ГБ трафика'

    # Списываем баланс
    success = await subtract_user_balance(db, user, final_price, traffic_description)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to charge balance',
        )

    # Добавляем трафик
    await add_subscription_traffic(db, subscription, request.gb)

    # Обновляем purchased_traffic_gb
    current_purchased = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    subscription.purchased_traffic_gb = current_purchased + request.gb

    # Устанавливаем дату сброса трафика (только при первой докупке)
    # При повторной докупке дата НЕ продлевается
    if not subscription.traffic_reset_at:
        from datetime import timedelta

        subscription.traffic_reset_at = datetime.utcnow() + timedelta(days=30)
        logger.info(f'Set traffic_reset_at for subscription {subscription.id}: {subscription.traffic_reset_at}')

    await db.commit()

    # Синхронизируем с RemnaWave
    try:
        subscription_service = SubscriptionService()
        if getattr(user, 'remnawave_uuid', None):
            await subscription_service.update_remnawave_user(db, subscription)
        else:
            await subscription_service.create_remnawave_user(db, subscription)
    except Exception as e:
        logger.error(f'Failed to sync traffic with RemnaWave: {e}')

    # Создаём транзакцию
    await create_transaction(
        db=db,
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=final_price,
        description=traffic_description,
    )

    await db.refresh(user)
    await db.refresh(subscription)

    # Отправляем уведомление админам
    try:
        from aiogram import Bot

        from app.services.admin_notification_service import AdminNotificationService

        if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
            bot = Bot(token=settings.BOT_TOKEN)
            try:
                notification_service = AdminNotificationService(bot)
                old_traffic = subscription.traffic_limit_gb - request.gb
                await notification_service.send_subscription_update_notification(
                    db=db,
                    user=user,
                    subscription=subscription,
                    update_type='traffic',
                    old_value=old_traffic,
                    new_value=subscription.traffic_limit_gb,
                    price_paid=final_price,
                )
            finally:
                await bot.session.close()
    except Exception as e:
        logger.error(f'Failed to send admin notification for traffic purchase: {e}')

    response = {
        'success': True,
        'message': 'Traffic purchased successfully',
        'gb_added': request.gb,
        'new_traffic_limit_gb': subscription.traffic_limit_gb,
        'amount_paid_kopeks': final_price,
        'new_balance_kopeks': user.balance_kopeks,
    }

    if traffic_discount_percent > 0:
        response['discount_percent'] = traffic_discount_percent
        response['discount_kopeks'] = discount_value
        response['base_price_kopeks'] = prorated_price

    return response


@router.post('/devices')
async def purchase_devices_legacy(
    request: DevicePurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Purchase additional device slots (legacy endpoint without tariff support).

    DEPRECATED: Use /devices/purchase instead for full tariff and discount support.
    """
    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    price_per_device = settings.PRICE_PER_DEVICE
    base_total_price = price_per_device * request.devices

    # Apply discount from promo group
    discount_result = _apply_addon_discount(user, 'devices', base_total_price, 30)
    total_price = discount_result['discounted']
    devices_discount_percent = discount_result['percent']

    # Ensure minimum price after discount (except for 100% discount)
    if devices_discount_percent < 100 and total_price > 0:
        total_price = max(100, total_price)

    # Check balance
    if user.balance_kopeks < total_price:
        missing = total_price - user.balance_kopeks

        # Сохраняем корзину для автопокупки после пополнения
        try:
            cart_data = {
                'cart_mode': 'add_devices',
                'devices_to_add': request.devices,
                'price_kopeks': total_price,
                'base_price_kopeks': base_total_price,
                'discount_percent': devices_discount_percent,
                'source': 'cabinet',
            }
            await user_cart_service.save_user_cart(user.id, cart_data)
            logger.info(f'Cart saved for device purchase (cabinet /devices) user {user.id}: +{request.devices} devices')
        except Exception as e:
            logger.error(f'Error saving cart for device purchase (cabinet /devices): {e}')

        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                'code': 'insufficient_funds',
                'error': 'Insufficient balance',
                'required_kopeks': total_price,
                'current_kopeks': user.balance_kopeks,
                'missing_kopeks': missing,
                'cart_saved': True,
            },
        )

    # Check max devices limit
    current_devices = user.subscription.device_limit or 1
    new_devices = current_devices + request.devices
    max_devices = settings.MAX_DEVICES_LIMIT

    if new_devices > max_devices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Maximum device limit is {max_devices}',
        )

    # Deduct balance and create transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import PaymentMethod

    # Build description with discount info
    if devices_discount_percent > 0:
        description = f'Покупка {request.devices} доп. устройств (скидка {devices_discount_percent}%)'
    else:
        description = f'Покупка {request.devices} доп. устройств'

    await subtract_user_balance(
        db=db,
        user=user,
        amount_kopeks=total_price,
        description=description,
        create_transaction=True,
        payment_method=PaymentMethod.BALANCE,
    )

    # Add devices
    user.subscription.device_limit = new_devices

    await db.commit()
    await db.refresh(user)

    # Отправляем уведомление админам
    try:
        from aiogram import Bot

        from app.services.admin_notification_service import AdminNotificationService

        if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
            bot = Bot(token=settings.BOT_TOKEN)
            try:
                notification_service = AdminNotificationService(bot)
                await notification_service.send_subscription_update_notification(
                    db=db,
                    user=user,
                    subscription=user.subscription,
                    update_type='devices',
                    old_value=current_devices,
                    new_value=new_devices,
                    price_paid=total_price,
                )
            finally:
                await bot.session.close()
    except Exception as e:
        logger.error(f'Failed to send admin notification for device purchase: {e}')

    response = {
        'message': 'Devices added successfully',
        'devices_added': request.devices,
        'new_device_limit': new_devices,
        'amount_paid_kopeks': total_price,
    }

    if devices_discount_percent > 0:
        response['discount_percent'] = devices_discount_percent
        response['discount_kopeks'] = discount_result['discount']
        response['base_price_kopeks'] = base_total_price

    return response


@router.patch('/autopay')
async def update_autopay(
    request: AutopayUpdateRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update autopay settings."""
    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    # Суточные подписки имеют свой механизм продления (DailySubscriptionService),
    # глобальный autopay для них запрещён
    if request.enabled:
        await db.refresh(user.subscription, ['tariff'])
        if user.subscription.tariff and getattr(user.subscription.tariff, 'is_daily', False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Autopay is not available for daily subscriptions',
            )

    user.subscription.autopay_enabled = request.enabled

    if request.days_before is not None:
        user.subscription.autopay_days_before = request.days_before

    await db.commit()

    return {
        'message': 'Autopay settings updated',
        'autopay_enabled': user.subscription.autopay_enabled,
        'autopay_days_before': user.subscription.autopay_days_before,
    }


@router.get('/trial', response_model=TrialInfoResponse)
async def get_trial_info(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get trial subscription info and availability."""
    await db.refresh(user, ['subscription'])

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
        logger.error(f'Error getting trial tariff for info: {e}')

    # Check if user already has an active subscription
    if user.subscription:
        now = datetime.utcnow()
        is_active = (
            user.subscription.status == 'active' and user.subscription.end_date and user.subscription.end_date > now
        )
        if is_active:
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

        # Check if user already used trial
        if user.subscription.is_trial or user.has_had_paid_subscription:
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
    await db.refresh(user, ['subscription'])

    # Проверяем, отключён ли триал для этого типа пользователя
    if settings.is_trial_disabled_for_user(getattr(user, 'auth_type', 'telegram')):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Trial is not available for your account type',
        )

    # Check if user already has an active subscription
    if user.subscription:
        now = datetime.utcnow()
        is_active = (
            user.subscription.status == 'active' and user.subscription.end_date and user.subscription.end_date > now
        )
        if is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='You already have an active subscription',
            )

        # Check if user already used trial
        if user.subscription.is_trial or user.has_had_paid_subscription:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Trial already used',
            )

    # Check if trial requires payment
    requires_payment = bool(settings.TRIAL_PAYMENT_ENABLED)
    if requires_payment:
        price_kopeks = settings.TRIAL_ACTIVATION_PRICE
        if user.balance_kopeks < price_kopeks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Insufficient balance. Need {price_kopeks / 100:.2f} RUB',
            )
        user.balance_kopeks -= price_kopeks
        logger.info(f'User {user.id} paid {price_kopeks} kopeks for trial activation')

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
            logger.info(f'Using trial tariff {trial_tariff.name} (ID: {trial_tariff.id}) with squads: {trial_squads}')
    except Exception as e:
        logger.error(f'Error getting trial tariff: {e}')

    # Create trial subscription
    subscription = await create_trial_subscription(
        db=db,
        user_id=user.id,
        duration_days=trial_duration,
        traffic_limit_gb=trial_traffic_limit,
        device_limit=trial_device_limit,
        connected_squads=trial_squads if trial_squads else None,
        tariff_id=tariff_id_for_trial,
    )

    logger.info(f'Trial subscription activated for user {user.id}')

    # Create RemnaWave user
    try:
        subscription_service = SubscriptionService()
        if subscription_service.is_configured:
            await subscription_service.create_remnawave_user(db, subscription)
            await db.refresh(subscription)
    except Exception as e:
        logger.error(f'Failed to create RemnaWave user for trial: {e}')

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
        logger.error(f'Failed to send trial activation notification: {e}')

    return _subscription_to_response(subscription)


# ============ Full Purchase Flow (like MiniApp) ============

purchase_service = MiniAppSubscriptionPurchaseService()


async def _build_tariff_response(
    db: AsyncSession,
    tariff: Tariff,
    current_tariff_id: int | None = None,
    language: str = 'ru',
    user: User | None = None,
    subscription: 'Subscription | None' = None,
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
            extra_device_price_per_month = tariff.device_price_kopeks or settings.PRICE_PER_DEVICE

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

            # Apply promo group discount for this period (на базовую цену тарифа)
            original_price = base_tariff_price + extra_devices_cost
            discount_percent = 0
            discount_amount = 0
            final_price = original_price

            if promo_group:
                discount_percent = promo_group.get_discount_percent('period', period_days)
                if discount_percent > 0:
                    discount_amount = original_price * discount_percent // 100
                    final_price = original_price - discount_amount

            per_month = final_price // months if months > 0 else final_price
            original_per_month = original_price // months if months > 0 else original_price

            period_data = {
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

    # Apply discount to daily price if applicable
    daily_price = getattr(tariff, 'daily_price_kopeks', 0)
    original_daily_price = daily_price
    daily_discount_percent = 0
    if promo_group and daily_price > 0:
        # For daily tariffs, use period discount with period_days=1
        daily_discount_percent = promo_group.get_discount_percent('period', 1)
        if daily_discount_percent > 0:
            discount_amount = daily_price * daily_discount_percent // 100
            daily_price = daily_price - discount_amount

    # Apply discount to custom price_per_day if applicable
    price_per_day = tariff.price_per_day_kopeks
    original_price_per_day = price_per_day
    custom_days_discount_percent = 0
    if promo_group and price_per_day > 0:
        custom_days_discount_percent = promo_group.get_discount_percent('period', 30)  # Use 30-day rate as base
        if custom_days_discount_percent > 0:
            discount_amount = price_per_day * custom_days_discount_percent // 100
            price_per_day = price_per_day - discount_amount

    # Apply discount to device price if applicable
    device_price = tariff.device_price_kopeks or 0
    original_device_price = device_price
    device_discount_percent = 0
    if promo_group and device_price > 0:
        device_discount_percent = promo_group.get_discount_percent('devices')
        if device_discount_percent > 0:
            discount_amount = device_price * device_discount_percent // 100
            device_price = device_price - discount_amount

    # Показываем реальное количество устройств (с докупленными) для текущего тарифа
    actual_device_limit = tariff.device_limit
    if subscription and subscription.tariff_id == tariff.id:
        actual_device_limit = max(tariff.device_limit or 0, subscription.device_limit or 0)

    response = {
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
            }

        # Classic mode - return periods
        context = await purchase_service.build_options(db, user)
        payload = context.payload
        payload['sales_mode'] = 'classic'
        return payload

    except PurchaseValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f'Failed to build purchase options for user {user.id}: {e}')
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
        logger.error(f'Failed to calculate purchase preview for user {user.id}: {e}')
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
                        'subscription': subscription,
                        'expires_at': end_date_str,  # for SUBSCRIPTION_ACTIVATED
                        'new_expires_at': end_date_str,  # for SUBSCRIPTION_RENEWED
                        'traffic_limit_gb': subscription.traffic_limit_gb,
                        'device_limit': subscription.device_limit,
                    },
                    bot=None,
                )
            except Exception as notif_error:
                logger.warning(f'Failed to send subscription notification to {user.email}: {notif_error}')

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
                        transaction=None,
                        period_days=selection.period_days,
                        was_trial_conversion=result.get('was_trial_conversion', False),
                        amount_kopeks=pricing.final_total,
                        purchase_type='renewal' if not is_new_subscription else None,
                    )
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f'Failed to send admin notification for subscription purchase: {e}')

        return {
            'success': True,
            'message': result['message'],
            'subscription': _subscription_to_response(subscription),
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
            logger.info(f'Cart saved for auto-purchase (cabinet /purchase) user {user.id}')
        except Exception as cart_error:
            logger.error(f'Error saving cart for auto-purchase (cabinet /purchase): {cart_error}')

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
        logger.error(f'Failed to submit purchase for user {user.id}: {e}')
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
        discount_percent = 0
        original_price = 0

        if is_daily_tariff:
            daily_price = getattr(tariff, 'daily_price_kopeks', 0)
            if daily_price <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='Daily tariff has invalid price',
                )
            original_price = daily_price
            # Apply promo group discount for daily tariff
            if promo_group:
                discount_percent = promo_group.get_discount_percent('period', 1)
                if discount_percent > 0:
                    discount_amount = daily_price * discount_percent // 100
                    daily_price = daily_price - discount_amount
            # For daily tariffs, charge first day and set period to 1 day
            price_kopeks = daily_price
            period_days = 1
        else:
            period_days = request.period_days
            # Get price for period (support custom days)
            price_kopeks = tariff.get_price_for_period(period_days)
            if price_kopeks is None:
                # Check for custom days
                if tariff.can_purchase_custom_days():
                    price_kopeks = tariff.get_price_for_custom_days(period_days)
                    if price_kopeks is None:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f'Period must be between {tariff.min_days} and {tariff.max_days} days',
                        )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail='Invalid period for this tariff',
                    )

            original_price = price_kopeks
            # Apply promo group discount for period
            if promo_group and price_kopeks > 0:
                discount_percent = promo_group.get_discount_percent('period', period_days)
                if discount_percent > 0:
                    discount_amount = price_kopeks * discount_percent // 100
                    price_kopeks = price_kopeks - discount_amount

        # Calculate traffic limit and price
        traffic_limit_gb = tariff.traffic_limit_gb
        traffic_price_kopeks = 0
        if request.traffic_gb is not None and tariff.can_purchase_custom_traffic():
            # Custom traffic requested
            traffic_price_kopeks = tariff.get_price_for_custom_traffic(request.traffic_gb)
            if traffic_price_kopeks is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Traffic must be between {tariff.min_traffic_gb} and {tariff.max_traffic_gb} GB',
                )
            # Apply traffic discount if promo group has it
            if promo_group and traffic_price_kopeks > 0:
                traffic_discount_percent = promo_group.get_discount_percent('traffic', period_days)
                if traffic_discount_percent > 0:
                    traffic_discount = traffic_price_kopeks * traffic_discount_percent // 100
                    traffic_price_kopeks = traffic_price_kopeks - traffic_discount
            traffic_limit_gb = request.traffic_gb
            price_kopeks += traffic_price_kopeks

        # Проверяем, есть ли докупленные устройства при продлении того же тарифа
        existing_subscription = await get_subscription_by_user_id(db, user.id)
        extra_devices = 0
        effective_device_limit = tariff.device_limit
        if existing_subscription and existing_subscription.tariff_id == tariff.id:
            extra_devices = max(0, (existing_subscription.device_limit or 0) - (tariff.device_limit or 0))
            if extra_devices > 0:
                effective_device_limit = existing_subscription.device_limit
                if not is_daily_tariff:
                    from app.utils.pricing_utils import calculate_months_from_days

                    device_price_per_month = tariff.device_price_kopeks or settings.PRICE_PER_DEVICE
                    months = calculate_months_from_days(period_days)
                    extra_devices_cost = extra_devices * device_price_per_month * months
                    # Применяем скидку промогруппы на устройства
                    if promo_group and extra_devices_cost > 0:
                        devices_discount_pct = promo_group.get_discount_percent('devices', period_days)
                        if devices_discount_pct > 0:
                            extra_devices_cost = extra_devices_cost - (extra_devices_cost * devices_discount_pct // 100)
                    price_kopeks += extra_devices_cost

        # Apply promo offer discount (temporary discount from promo offers)
        price_before_promo_offer = price_kopeks
        promo_offer_discount_percent = get_user_active_promo_discount_percent(user)
        promo_offer_discount_value = 0
        if promo_offer_discount_percent > 0:
            promo_offer_discount_value = price_kopeks * promo_offer_discount_percent // 100
            price_kopeks = price_kopeks - promo_offer_discount_value

        # Check balance
        if user.balance_kopeks < price_kopeks:
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
                    'source': 'cabinet',
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
                    'source': 'cabinet',
                }

            try:
                await user_cart_service.save_user_cart(user.id, cart_data)
                logger.info(f'Cart saved for auto-purchase (cabinet) user {user.id}, tariff {tariff.id}')
            except Exception as e:
                logger.error(f'Error saving cart for auto-purchase (cabinet): {e}')

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
        success = await subtract_user_balance(db, user, price_kopeks, description)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to charge balance',
            )

        # Consume promo offer discount if it was used
        if promo_offer_discount_value > 0:
            user.promo_offer_discount_percent = 0
            user.promo_offer_discount_source = None
            user.promo_offer_discount_expires_at = None

        # Create transaction
        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price_kopeks,
            description=description,
        )

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
            subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=period_days,
                traffic_limit_gb=traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=squads,
                tariff_id=tariff.id,
            )

        # For daily tariffs, set last_daily_charge_at
        if is_daily_tariff:
            subscription.last_daily_charge_at = datetime.utcnow()
            subscription.is_daily_paused = False
            await db.commit()
            await db.refresh(subscription)

        # Sync with RemnaWave
        # При покупке тарифа ВСЕГДА сбрасываем трафик в панели
        service = SubscriptionService()
        try:
            if getattr(user, 'remnawave_uuid', None):
                await service.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=True,
                    reset_reason='покупка тарифа (cabinet)',
                )
            else:
                await service.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=True,
                    reset_reason='покупка тарифа (cabinet)',
                )
        except Exception as remnawave_error:
            logger.error(f'Failed to sync subscription with RemnaWave: {remnawave_error}')

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
                logger.info(f'Tariff cart saved for auto-renewal (cabinet) user {user.id}')
            except Exception as e:
                logger.error(f'Error saving tariff cart (cabinet): {e}')

        await db.refresh(user)

        response = {
            'success': True,
            'message': f"Тариф '{tariff.name}' успешно активирован",
            'subscription': _subscription_to_response(subscription),
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
                    subscription.start_date and (datetime.utcnow() - subscription.start_date).total_seconds() < 60
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
                        'subscription': subscription,
                        'expires_at': end_date_str,  # for SUBSCRIPTION_ACTIVATED
                        'new_expires_at': end_date_str,  # for SUBSCRIPTION_RENEWED
                        'traffic_limit_gb': subscription.traffic_limit_gb,
                        'device_limit': subscription.device_limit,
                        'tariff_name': tariff.name,
                    },
                    bot=None,
                )
            except Exception as notif_error:
                logger.warning(f'Failed to send subscription notification to {user.email}: {notif_error}')

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
                        subscription.start_date and (datetime.utcnow() - subscription.start_date).total_seconds() < 60
                    )
                    await notification_service.send_subscription_purchase_notification(
                        db=db,
                        user=user,
                        subscription=subscription,
                        transaction=None,
                        period_days=period_days,
                        was_trial_conversion=False,
                        amount_kopeks=price_kopeks,
                        purchase_type='renewal' if not was_new_subscription else None,
                    )
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f'Failed to send admin notification for tariff purchase: {e}')

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Failed to purchase tariff for user {user.id}: {e}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to process tariff purchase',
        )


# ============ Device Purchase ============


@router.post('/devices/purchase')
async def purchase_devices(
    request: DevicePurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Purchase additional device slots for subscription."""
    try:
        await db.refresh(user, ['subscription'])
        subscription = user.subscription

        if not subscription:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='У вас нет активной подписки',
            )

        if subscription.status not in ['active', 'trial']:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Ваша подписка неактивна',
            )

        # Get tariff for device price (if exists)
        tariff = None
        if subscription.tariff_id:
            from app.database.crud.tariff import get_tariff_by_id

            tariff = await get_tariff_by_id(db, subscription.tariff_id)

        # Determine device price and max limit from tariff or settings
        if tariff and tariff.device_price_kopeks:
            device_price = tariff.device_price_kopeks
            max_device_limit = tariff.max_device_limit
        else:
            # Classic mode - use settings
            device_price = settings.PRICE_PER_DEVICE
            max_device_limit = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None

        if not device_price or device_price <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Докупка устройств недоступна',
            )

        # Check max device limit
        current_devices = subscription.device_limit or 1
        new_device_count = current_devices + request.devices
        if max_device_limit and new_device_count > max_device_limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Максимальное количество устройств: {max_device_limit}',
            )

        # Calculate prorated price based on remaining days
        from datetime import datetime

        now = datetime.now(UTC)
        end_date = subscription.end_date
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=UTC)

        days_left = max(1, (end_date - now).days)
        total_days = 30  # Base period for device price calculation

        # Calculate base price before discount
        base_price_per_month = device_price * request.devices
        base_price_prorated = int(base_price_per_month * days_left / total_days)
        base_price_prorated = max(100, base_price_prorated)  # Minimum 1 ruble

        # Apply discount from promo group
        period_hint_days = days_left
        discount_result = _apply_addon_discount(user, 'devices', base_price_prorated, period_hint_days)
        price_kopeks = discount_result['discounted']
        devices_discount_percent = discount_result['percent']
        discount_value = discount_result['discount']

        # Ensure minimum price after discount (except for 100% discount)
        if devices_discount_percent < 100:
            price_kopeks = max(100, price_kopeks)

        # Check balance
        if user.balance_kopeks < price_kopeks:
            missing = price_kopeks - user.balance_kopeks

            # Сохраняем корзину для автопокупки после пополнения
            try:
                cart_data = {
                    'cart_mode': 'add_devices',
                    'devices_to_add': request.devices,
                    'price_kopeks': price_kopeks,
                    'base_price_kopeks': base_price_prorated,
                    'discount_percent': devices_discount_percent,
                    'source': 'cabinet',
                }
                await user_cart_service.save_user_cart(user.id, cart_data)
                logger.info(
                    f'Cart saved for device purchase (cabinet) user {user.id}: '
                    f'+{request.devices} devices, discount {devices_discount_percent}%'
                )
            except Exception as e:
                logger.error(f'Error saving cart for device purchase (cabinet): {e}')

            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    'code': 'insufficient_funds',
                    'error': 'Insufficient balance',
                    'required_kopeks': price_kopeks,
                    'current_kopeks': user.balance_kopeks,
                    'missing_kopeks': missing,
                    'cart_saved': True,
                },
            )

        # Deduct balance and create transaction
        from app.database.crud.user import subtract_user_balance
        from app.database.models import PaymentMethod

        # Build description with discount info
        if devices_discount_percent > 0:
            description = f'Покупка {request.devices} доп. устройств (скидка {devices_discount_percent}%)'
        else:
            description = f'Покупка {request.devices} доп. устройств'

        await subtract_user_balance(
            db=db,
            user=user,
            amount_kopeks=price_kopeks,
            description=description,
            create_transaction=True,
            payment_method=PaymentMethod.BALANCE,
        )

        # Increase device limit
        subscription.device_limit += request.devices
        await db.commit()
        await db.refresh(subscription)

        # Sync with RemnaWave
        service = SubscriptionService()
        try:
            if getattr(user, 'remnawave_uuid', None):
                await service.update_remnawave_user(db, subscription)
            else:
                await service.create_remnawave_user(db, subscription)
        except Exception as e:
            logger.error(f'Failed to sync devices with RemnaWave: {e}')

        await db.refresh(user)

        if devices_discount_percent > 0:
            logger.info(
                f'User {user.id} purchased {request.devices} devices for {price_kopeks} kopeks '
                f'(discount {devices_discount_percent}%, saved {discount_value} kopeks)'
            )
        else:
            logger.info(f'User {user.id} purchased {request.devices} devices for {price_kopeks} kopeks')

        # Отправляем уведомление админам
        try:
            from aiogram import Bot

            from app.services.admin_notification_service import AdminNotificationService

            if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
                bot = Bot(token=settings.BOT_TOKEN)
                try:
                    notification_service = AdminNotificationService(bot)
                    await notification_service.send_subscription_update_notification(
                        db=db,
                        user=user,
                        subscription=subscription,
                        update_type='devices',
                        old_value=current_devices,
                        new_value=subscription.device_limit,
                        price_paid=price_kopeks,
                    )
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f'Failed to send admin notification for device purchase: {e}')

        response = {
            'success': True,
            'message': f'Добавлено {request.devices} устройств',
            'devices_added': request.devices,
            'new_device_limit': subscription.device_limit,
            'price_kopeks': price_kopeks,
            'price_label': settings.format_price(price_kopeks),
            'balance_kopeks': user.balance_kopeks,
            'balance_label': settings.format_price(user.balance_kopeks),
        }

        if devices_discount_percent > 0:
            response['discount_percent'] = devices_discount_percent
            response['discount_kopeks'] = discount_value
            response['base_price_kopeks'] = base_price_prorated

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Failed to purchase devices for user {user.id}: {e}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Не удалось обработать покупку устройств',
        )


@router.post('/traffic/save-cart')
async def save_traffic_cart(
    request: TrafficPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, bool]:
    """Save cart for traffic purchase (for insufficient balance flow)."""
    from app.utils.pricing_utils import calculate_prorated_price

    await db.refresh(user, ['subscription'])
    subscription = user.subscription

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='У вас нет активной подписки',
        )

    if subscription.status not in ['active', 'trial']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Ваша подписка неактивна',
        )

    if subscription.is_trial:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Докупка трафика недоступна на пробном периоде',
        )

    if subscription.traffic_limit_gb == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='У вас уже безлимитный трафик',
        )

    # Get traffic price from tariff or settings
    tariff = None
    base_price_kopeks = 0
    is_tariff_mode = settings.is_tariffs_mode() and subscription.tariff_id

    if is_tariff_mode:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if not tariff:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Тариф не найден',
            )

        if not getattr(tariff, 'traffic_topup_enabled', False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Докупка трафика недоступна на вашем тарифе',
            )

        packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
        if request.gb not in packages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Пакет трафика {request.gb} ГБ недоступен',
            )
        base_price_kopeks = packages[request.gb]
    else:
        if not settings.is_traffic_topup_enabled():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Докупка трафика отключена',
            )

        packages = settings.get_traffic_packages()
        matching_pkg = next((pkg for pkg in packages if pkg['gb'] == request.gb and pkg.get('enabled', True)), None)
        if not matching_pkg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Недоступный пакет трафика',
            )
        base_price_kopeks = matching_pkg['price']

    # Apply promo group discount
    traffic_discount_percent = 0
    promo_group = (
        user.get_primary_promo_group()
        if hasattr(user, 'get_primary_promo_group')
        else getattr(user, 'promo_group', None)
    )
    if promo_group:
        apply_to_addons = getattr(promo_group, 'apply_discounts_to_addons', True)
        if apply_to_addons:
            traffic_discount_percent = max(0, min(100, int(getattr(promo_group, 'traffic_discount_percent', 0) or 0)))

    if traffic_discount_percent > 0:
        base_price_kopeks = int(base_price_kopeks * (100 - traffic_discount_percent) / 100)

    # Calculate prorated price
    final_price, _ = calculate_prorated_price(
        base_price_kopeks,
        subscription.end_date,
    )

    # Save cart for auto-purchase after balance top-up
    cart_data = {
        'cart_mode': 'add_traffic',
        'subscription_id': subscription.id,
        'traffic_gb': request.gb,
        'price_kopeks': final_price,
        'base_price_kopeks': base_price_kopeks,
        'discount_percent': traffic_discount_percent,
        'source': 'cabinet',
        'description': f'Докупка {request.gb} ГБ трафика',
    }
    await user_cart_service.save_user_cart(user.id, cart_data)
    logger.info(f'Cart saved for traffic purchase (cabinet save-cart) user {user.id}: +{request.gb} GB')

    return {'success': True, 'cart_saved': True}


@router.post('/devices/save-cart')
async def save_devices_cart(
    request: DevicePurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, bool]:
    """Save cart for device purchase (for insufficient balance flow)."""
    await db.refresh(user, ['subscription'])
    subscription = user.subscription

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='У вас нет активной подписки',
        )

    if subscription.status not in ['active', 'trial']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Ваша подписка неактивна',
        )

    # Get tariff for device price (if exists)
    tariff = None
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Determine device price and max limit from tariff or settings
    if tariff and tariff.device_price_kopeks:
        device_price = tariff.device_price_kopeks
        max_device_limit = tariff.max_device_limit
    else:
        device_price = settings.PRICE_PER_DEVICE
        max_device_limit = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None

    if not device_price or device_price <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Докупка устройств недоступна',
        )

    # Check max device limit
    current_devices = subscription.device_limit or 1
    new_device_count = current_devices + request.devices
    if max_device_limit and new_device_count > max_device_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Максимальное количество устройств: {max_device_limit}',
        )

    # Calculate prorated price based on remaining days
    now = datetime.now(UTC)
    end_date = subscription.end_date
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)

    days_left = max(1, (end_date - now).days)
    total_days = 30

    price_kopeks = int(device_price * request.devices * days_left / total_days)
    price_kopeks = max(100, price_kopeks)  # Minimum 1 ruble

    # Save cart for auto-purchase after balance top-up
    cart_data = {
        'cart_mode': 'add_devices',
        'devices_to_add': request.devices,
        'price_kopeks': price_kopeks,
        'source': 'cabinet',
    }
    await user_cart_service.save_user_cart(user.id, cart_data)
    logger.info(f'Cart saved for device purchase (cabinet save-cart) user {user.id}: +{request.devices} devices')

    return {'success': True, 'cart_saved': True}


@router.get('/devices/price')
async def get_device_price(
    devices: int = 1,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get price for additional devices."""
    await db.refresh(user, ['subscription'])
    subscription = user.subscription

    if not subscription or subscription.status not in ['active', 'trial']:
        return {
            'available': False,
            'reason': 'Нет активной подписки',
        }

    tariff = None
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Determine device price and max limit from tariff or settings
    if tariff and tariff.device_price_kopeks:
        device_price = tariff.device_price_kopeks
        max_device_limit = tariff.max_device_limit
    else:
        # Classic mode - use settings
        device_price = settings.PRICE_PER_DEVICE
        max_device_limit = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None

    if not device_price or device_price <= 0:
        return {
            'available': False,
            'reason': 'Докупка устройств недоступна',
        }

    # Check max device limit
    current_devices = subscription.device_limit or 1
    can_add = max_device_limit - current_devices if max_device_limit else None

    if max_device_limit and current_devices >= max_device_limit:
        return {
            'available': False,
            'reason': f'Достигнут максимум устройств ({max_device_limit})',
            'current_device_limit': current_devices,
            'max_device_limit': max_device_limit,
        }

    if max_device_limit and current_devices + devices > max_device_limit:
        return {
            'available': False,
            'reason': f'Можно добавить максимум {can_add} устройств',
            'current_device_limit': current_devices,
            'max_device_limit': max_device_limit,
            'can_add': can_add,
        }

    # Calculate prorated price
    from datetime import datetime

    now = datetime.now(UTC)
    end_date = subscription.end_date
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)

    days_left = max(1, (end_date - now).days)
    total_days = 30

    # Calculate base price before discount
    base_price_per_device = int(device_price * days_left / total_days)
    base_price_per_device = max(100, base_price_per_device)
    base_total_price = base_price_per_device * devices

    # Apply discount from promo group
    period_hint_days = days_left
    discount_result = _apply_addon_discount(user, 'devices', base_total_price, period_hint_days)
    total_price_kopeks = discount_result['discounted']
    devices_discount_percent = discount_result['percent']
    discount_value = discount_result['discount']

    # Calculate per-device price after discount
    if devices_discount_percent < 100 and total_price_kopeks > 0:
        total_price_kopeks = max(100, total_price_kopeks)
    price_per_device_kopeks = total_price_kopeks // devices if devices > 0 else 0

    response = {
        'available': True,
        'devices': devices,
        'price_per_device_kopeks': price_per_device_kopeks,
        'price_per_device_label': settings.format_price(price_per_device_kopeks),
        'total_price_kopeks': total_price_kopeks,
        'total_price_label': settings.format_price(total_price_kopeks),
        'current_device_limit': current_devices,
        'max_device_limit': max_device_limit,
        'can_add': can_add,
        'days_left': days_left,
        'base_device_price_kopeks': device_price,
    }

    # Add discount info if applicable
    if devices_discount_percent > 0:
        response['discount_percent'] = devices_discount_percent
        response['discount_kopeks'] = discount_value
        response['base_total_price_kopeks'] = base_total_price

    return response


# ============ App Config for Connection ============


def _load_app_config_from_file() -> dict[str, Any]:
    """Load app-config.json file."""
    try:
        config_path = settings.get_app_config_path()
        with open(config_path, encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.error(f'Failed to load app-config.json: {e}')
    return {}


def _get_remnawave_config_uuid() -> str | None:
    """Get RemnaWave config UUID from system settings or env."""
    try:
        return bot_configuration_service.get_current_value('CABINET_REMNA_SUB_CONFIG')
    except Exception:
        return settings.CABINET_REMNA_SUB_CONFIG


def _is_subscription_link_template(url: str) -> bool:
    """Check if URL is a RemnaWave subscription link template."""
    if not url:
        return False
    # RemnaWave uses templates like {{HAPP_CRYPT4_LINK}}, {{V2RAY_LINK}}, etc.
    if url.startswith('{{') and url.endswith('}}'):
        return True
    # Also check for button type "subscriptionLink" indicator
    return False


def _convert_remnawave_block_to_step(block: dict[str, Any], url_scheme: str = '') -> dict[str, Any]:
    """Convert RemnaWave block format to cabinet step format."""
    step = {
        'description': block.get('description', {}),
    }
    if block.get('title'):
        step['title'] = block['title']
    if block.get('buttons'):
        buttons = []
        for btn in block['buttons']:
            btn_url = btn.get('url', '') or btn.get('link', '')
            btn_type = btn.get('type', '')

            # Replace subscription link templates with {{deepLink}} placeholder
            # RemnaWave uses templates like {{HAPP_CRYPT4_LINK}} or type="subscriptionLink"
            if (
                _is_subscription_link_template(btn_url)
                or btn_type == 'subscriptionLink'
                or (
                    url_scheme
                    and btn_url
                    and (
                        btn_url.startswith(url_scheme)
                        or btn_url.endswith('://')
                        or btn_url.endswith('://add/')
                        or ('://' in btn_url and not btn_url.startswith('http'))
                    )
                )
            ):
                btn_url = '{{deepLink}}'

            buttons.append(
                {
                    'buttonLink': btn_url,
                    'buttonText': btn.get('text', {}),
                }
            )
        step['buttons'] = buttons
    return step


def _extract_scheme_from_buttons(buttons: list[dict[str, Any]]) -> tuple[str, bool]:
    """Extract URL scheme from buttons list.

    Returns:
        Tuple of (scheme, uses_crypto_link).
        uses_crypto_link=True when the template is {{HAPP_CRYPT4_LINK}},
        meaning subscription_crypto_link should be used as payload.
    """
    for btn in buttons:
        if not isinstance(btn, dict):
            continue
        link = btn.get('link', '') or btn.get('url', '') or btn.get('buttonLink', '')
        if not link:
            continue
        link_upper = link.upper()

        # Check for {{HAPP_CRYPT4_LINK}} -- uses crypto link as payload
        if '{{HAPP_CRYPT4_LINK}}' in link_upper or 'HAPP_CRYPT4_LINK' in link_upper:
            scheme = re.sub(r'\{\{HAPP_CRYPT4_LINK\}\}', '', link, flags=re.IGNORECASE)
            if scheme and '://' in scheme:
                return scheme, True

        # Check for {{SUBSCRIPTION_LINK}} -- uses plain subscription_url as payload
        if '{{SUBSCRIPTION_LINK}}' in link_upper or 'SUBSCRIPTION_LINK' in link_upper:
            scheme = re.sub(r'\{\{SUBSCRIPTION_LINK\}\}', '', link, flags=re.IGNORECASE)
            if scheme and '://' in scheme:
                return scheme, False

        # Also check for type="subscriptionLink" buttons with custom schemes
        btn_type = btn.get('type', '')
        if btn_type == 'subscriptionLink' and '://' in link and not link.startswith('http'):
            scheme = link.split('{{')[0] if '{{' in link else link
            if scheme and '://' in scheme:
                return scheme, False
    return '', False


def _get_url_scheme_for_app(app: dict[str, Any]) -> tuple[str, bool]:
    """Get URL scheme for app - from config, buttons, or fallback by name.

    Returns:
        Tuple of (scheme, uses_crypto_link).
        uses_crypto_link=True means the app template uses {{HAPP_CRYPT4_LINK}},
        so subscription_crypto_link should be used as the deep link payload.
    """
    # 1. Check urlScheme field (cabinet format stores usesCryptoLink alongside)
    scheme = str(app.get('urlScheme', '')).strip()
    if scheme:
        uses_crypto = bool(app.get('usesCryptoLink', False))
        return scheme, uses_crypto

    # 2. Extract from buttons in blocks (RemnaWave format)
    blocks = app.get('blocks', [])
    for block in blocks:
        if not isinstance(block, dict):
            continue
        buttons = block.get('buttons', [])
        scheme, uses_crypto = _extract_scheme_from_buttons(buttons)
        if scheme:
            return scheme, uses_crypto

    # 3. Check buttons directly in app (alternative structure)
    direct_buttons = app.get('buttons', [])
    if direct_buttons:
        scheme, uses_crypto = _extract_scheme_from_buttons(direct_buttons)
        if scheme:
            return scheme, uses_crypto

    # 4. Check in step structures (cabinet format)
    for step_key in ['installationStep', 'addSubscriptionStep', 'connectAndUseStep']:
        step = app.get(step_key, {})
        if isinstance(step, dict):
            step_buttons = step.get('buttons', [])
            scheme, uses_crypto = _extract_scheme_from_buttons(step_buttons)
            if scheme:
                return scheme, uses_crypto

    # No scheme found
    logger.debug(
        f"_get_url_scheme_for_app: No scheme found for app '{app.get('name')}', "
        f'has blocks: {bool(app.get("blocks"))}, '
        f'has buttons: {bool(app.get("buttons"))}, '
        f'has urlScheme: {bool(app.get("urlScheme"))}'
    )
    return '', False


def _find_subscription_block(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find block that contains subscriptionLink button."""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        buttons = block.get('buttons', [])
        for btn in buttons:
            if not isinstance(btn, dict):
                continue
            # Check for subscriptionLink type, {{SUBSCRIPTION_LINK}}, or {{HAPP_CRYPT4_LINK}} in link
            btn_type = btn.get('type', '')
            link = btn.get('link', '') or btn.get('url', '')
            link_upper = link.upper() if link else ''
            if btn_type == 'subscriptionLink' or 'SUBSCRIPTION_LINK' in link_upper or 'HAPP_CRYPT4_LINK' in link_upper:
                return block
    return None


def _find_connect_block(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find block that is about connection/usage (usually last or has specific keywords)."""
    # Look for block with "connect" or "use" in title
    for block in blocks:
        if not isinstance(block, dict):
            continue
        title = block.get('title', {})
        title_en = title.get('en', '') if isinstance(title, dict) else ''
        title_lower = title_en.lower()
        if 'connect' in title_lower or 'use' in title_lower:
            return block
    # Fallback to last block if no match
    return blocks[-1] if blocks else None


def _convert_remnawave_app_to_cabinet(app: dict[str, Any]) -> dict[str, Any]:
    """Convert RemnaWave app format to cabinet app format."""
    blocks = app.get('blocks', [])
    url_scheme, uses_crypto = _get_url_scheme_for_app(app)

    # Debug log for conversion (не логируем отсутствие urlScheme - для Happ это нормально)
    app_name = app.get('name', 'unknown')
    if url_scheme:
        logger.debug(f"_convert_remnawave_app_to_cabinet: app '{app_name}' -> urlScheme='{url_scheme}'")

    # Smart block mapping: find blocks by their content, not just position
    # 1. First block is usually installation
    installation_block = blocks[0] if len(blocks) > 0 else None
    # 2. Find subscription block (with subscriptionLink button)
    subscription_block = _find_subscription_block(blocks)
    # 3. Find connect/use block (usually last or has "connect" in title)
    connect_block = _find_connect_block(blocks)

    # Convert blocks to steps
    installation_step = (
        _convert_remnawave_block_to_step(installation_block, url_scheme) if installation_block else {'description': {}}
    )
    subscription_step = (
        _convert_remnawave_block_to_step(subscription_block, url_scheme) if subscription_block else {'description': {}}
    )
    connect_step = _convert_remnawave_block_to_step(connect_block, url_scheme) if connect_block else {'description': {}}

    # Ensure subscription step has a deepLink button if urlScheme exists
    if url_scheme:
        has_deeplink_button = False
        if 'buttons' in subscription_step:
            for btn in subscription_step['buttons']:
                if btn.get('buttonLink') == '{{deepLink}}':
                    has_deeplink_button = True
                    break

        if not has_deeplink_button:
            # Add deepLink button at the beginning
            deeplink_button = {
                'buttonLink': '{{deepLink}}',
                'buttonText': {
                    'en': 'Open app',
                    'ru': 'Открыть приложение',
                    'zh': '打开应用',
                    'fa': 'باز کردن برنامه',
                },
            }
            if 'buttons' not in subscription_step:
                subscription_step['buttons'] = []
            subscription_step['buttons'].insert(0, deeplink_button)

    return {
        'id': app.get('name', '').lower().replace(' ', '-'),
        'name': app.get('name', ''),
        'isFeatured': app.get('featured', False),
        'urlScheme': url_scheme,
        'usesCryptoLink': uses_crypto,
        'isNeedBase64Encoding': app.get('isNeedBase64Encoding', False),
        'installationStep': installation_step,
        'addSubscriptionStep': subscription_step,
        'connectAndUseStep': connect_step,
    }


def _convert_remnawave_config_to_cabinet(config: dict[str, Any]) -> dict[str, Any]:
    """Convert RemnaWave config format to cabinet format."""
    platforms = {}
    remnawave_platforms = config.get('platforms', {})

    for platform_key, platform_data in remnawave_platforms.items():
        if not isinstance(platform_data, dict):
            continue
        apps = platform_data.get('apps', [])
        if not isinstance(apps, list):
            continue

        cabinet_apps = []
        for app in apps:
            if isinstance(app, dict):
                cabinet_apps.append(_convert_remnawave_app_to_cabinet(app))

        if cabinet_apps:
            platforms[platform_key] = cabinet_apps

    # Convert branding
    branding = {}
    if config.get('brandingSettings'):
        branding = {
            'name': config['brandingSettings'].get('name', ''),
            'logoUrl': config['brandingSettings'].get('logoUrl', ''),
            'supportUrl': config['brandingSettings'].get('supportUrl', ''),
        }

    return {
        'config': {
            'additionalLocales': ['zh', 'fa'],
            'branding': branding,
        },
        'platforms': platforms,
    }


async def _load_app_config_async() -> dict[str, Any]:
    """Load app config from RemnaWave (if configured) or local file.

    When config comes from RemnaWave, returns the original format with
    ``_isRemnawave`` flag so the caller can serve it as-is (enriched with
    deep links) instead of converting to the legacy step-based format.
    """
    remnawave_uuid = _get_remnawave_config_uuid()

    if remnawave_uuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                config = await api.get_subscription_page_config(remnawave_uuid)
                if config and config.config:
                    logger.debug(f'Loaded app config from RemnaWave: {remnawave_uuid}')
                    raw = dict(config.config)
                    raw['_isRemnawave'] = True
                    return raw
        except Exception as e:
            logger.warning(f'Failed to load RemnaWave config, falling back to file: {e}')

    # Fallback to local file
    return _load_app_config_from_file()


def _load_app_config() -> dict[str, Any]:
    """Load app-config.json file (sync version for compatibility)."""
    return _load_app_config_from_file()


def _create_deep_link(
    app: dict[str, Any], subscription_url: str, subscription_crypto_link: str | None = None
) -> str | None:
    """Create deep link for app with subscription URL.

    Uses urlScheme from RemnaWave config (e.g. "happ://add/", "v2rayng://install-config?url=")
    combined with the appropriate payload URL.

    Two Happ schemes exist in RemnaWave:
      - happ://add/{{SUBSCRIPTION_LINK}}       -> uses plain subscription_url
      - happ://crypt4/{{HAPP_CRYPT4_LINK}}     -> uses subscription_crypto_link
    """
    if not isinstance(app, dict):
        return None

    if not subscription_url and not subscription_crypto_link:
        return None

    scheme, uses_crypto = _get_url_scheme_for_app(app)
    if not scheme:
        logger.debug(f"_create_deep_link: no urlScheme for app '{app.get('name', 'unknown')}'")
        return None

    # Pick the correct payload based on which template the app uses
    if uses_crypto:
        if not subscription_crypto_link:
            logger.debug(
                f"_create_deep_link: app '{app.get('name', 'unknown')}' requires crypto link but none available"
            )
            return None
        payload = subscription_crypto_link
    else:
        if not subscription_url:
            logger.debug(
                f"_create_deep_link: app '{app.get('name', 'unknown')}' requires subscription_url but none available"
            )
            return None
        payload = subscription_url

    if app.get('isNeedBase64Encoding'):
        try:
            payload = base64.b64encode(payload.encode('utf-8')).decode('utf-8')
        except Exception as e:
            logger.warning(f'Failed to encode payload to base64: {e}')

    return f'{scheme}{payload}'


# ============ Countries Management ============


@router.get('/countries')
async def get_available_countries(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get available countries/servers for the user."""
    from app.database.crud.server_squad import get_available_server_squads
    from app.utils.pricing_utils import apply_percentage_discount, calculate_prorated_price

    await db.refresh(user, ['subscription'])

    promo_group_id = user.promo_group_id
    # Exclude trial-only servers from available servers for purchase
    available_servers = await get_available_server_squads(db, promo_group_id=promo_group_id, exclude_trial_only=True)

    connected_squads = []
    days_left = 0
    if user.subscription:
        connected_squads = user.subscription.connected_squads or []
        # Calculate days left for prorated pricing
        if user.subscription.end_date:
            from datetime import datetime

            delta = user.subscription.end_date - datetime.utcnow()
            days_left = max(0, delta.days)

    # Get discount from promo group
    servers_discount_percent = 0
    promo_group = user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None
    if promo_group:
        servers_discount_percent = promo_group.get_discount_percent('servers', None)

    countries = []
    for server in available_servers:
        base_price = server.price_kopeks

        # Apply discount
        if servers_discount_percent > 0:
            discounted_price, _ = apply_percentage_discount(base_price, servers_discount_percent)
        else:
            discounted_price = base_price

        # Calculate prorated price if subscription exists
        prorated_price = discounted_price
        if user.subscription and user.subscription.end_date:
            prorated_price, _ = calculate_prorated_price(
                discounted_price,
                user.subscription.end_date,
            )

        countries.append(
            {
                'uuid': server.squad_uuid,
                'name': server.display_name,
                'country_code': server.country_code,
                'base_price_kopeks': base_price,
                'price_kopeks': prorated_price,  # Prorated price with discount
                'price_per_month_kopeks': discounted_price,  # Monthly price with discount
                'price_rubles': prorated_price / 100,
                'is_available': server.is_available and not server.is_full,
                'is_connected': server.squad_uuid in connected_squads,
                'has_discount': servers_discount_percent > 0,
                'discount_percent': servers_discount_percent,
            }
        )

    return {
        'countries': countries,
        'connected_count': len(connected_squads),
        'has_subscription': user.subscription is not None,
        'days_left': days_left,
        'discount_percent': servers_discount_percent,
    }


@router.post('/countries')
async def update_countries(
    request: dict[str, Any],
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Update subscription countries/servers."""
    from app.database.crud.server_squad import add_user_to_servers, get_available_server_squads, get_server_ids_by_uuids
    from app.database.crud.subscription import add_subscription_servers
    from app.database.crud.transaction import create_transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import TransactionType
    from app.utils.pricing_utils import apply_percentage_discount, calculate_prorated_price

    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if user.subscription.is_trial:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Country management is not available for trial subscriptions',
        )

    selected_countries = request.get('countries', [])
    if not selected_countries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='At least one country must be selected',
        )

    current_countries = user.subscription.connected_squads or []
    promo_group_id = user.promo_group_id

    # Exclude trial-only servers from available servers for purchase
    available_servers = await get_available_server_squads(db, promo_group_id=promo_group_id, exclude_trial_only=True)
    allowed_country_ids = {server.squad_uuid for server in available_servers}

    # Validate selected countries
    for country_uuid in selected_countries:
        if country_uuid not in allowed_country_ids and country_uuid not in current_countries:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Country {country_uuid} is not available',
            )

    added = [c for c in selected_countries if c not in current_countries]
    removed = [c for c in current_countries if c not in selected_countries]

    if not added and not removed:
        return {
            'message': 'No changes detected',
            'connected_squads': current_countries,
        }

    # Calculate cost for added servers
    total_cost = 0
    added_names = []
    removed_names = []

    servers_discount_percent = 0
    promo_group = user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None
    if promo_group:
        servers_discount_percent = promo_group.get_discount_percent('servers', None)

    added_server_prices = []

    for server in available_servers:
        if server.squad_uuid in added:
            server_price_per_month = server.price_kopeks
            if servers_discount_percent > 0:
                discounted_per_month, _ = apply_percentage_discount(
                    server_price_per_month,
                    servers_discount_percent,
                )
            else:
                discounted_per_month = server_price_per_month

            charged_price, charged_months = calculate_prorated_price(
                discounted_per_month,
                user.subscription.end_date,
            )

            total_cost += charged_price
            added_names.append(server.display_name)
            added_server_prices.append(charged_price)

        if server.squad_uuid in removed:
            removed_names.append(server.display_name)

    # Check balance
    if total_cost > 0 and user.balance_kopeks < total_cost:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f'Insufficient balance. Need {total_cost / 100:.2f} RUB, have {user.balance_kopeks / 100:.2f} RUB',
        )

    # Deduct balance and update subscription
    if added and total_cost > 0:
        success = await subtract_user_balance(db, user, total_cost, f'Adding countries: {", ".join(added_names)}')
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to charge balance',
            )

        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=total_cost,
            description=f'Adding countries to subscription: {", ".join(added_names)}',
        )

    # Add servers to subscription
    if added:
        added_server_ids = await get_server_ids_by_uuids(db, added)
        if added_server_ids:
            await add_subscription_servers(db, user.subscription, added_server_ids, added_server_prices)
            await add_user_to_servers(db, added_server_ids)

    # Update connected squads
    user.subscription.connected_squads = selected_countries
    user.subscription.updated_at = datetime.utcnow()
    await db.commit()

    # Sync with RemnaWave
    try:
        subscription_service = SubscriptionService()
        if getattr(user, 'remnawave_uuid', None):
            await subscription_service.update_remnawave_user(db, user.subscription)
        else:
            await subscription_service.create_remnawave_user(db, user.subscription)
    except Exception as e:
        logger.error(f'Failed to sync countries with RemnaWave: {e}')

    await db.refresh(user.subscription)

    return {
        'message': 'Countries updated successfully',
        'added': added_names,
        'removed': removed_names,
        'amount_paid_kopeks': total_cost,
        'connected_squads': user.subscription.connected_squads,
    }


# ============ Connection Link ============


@router.get('/connection-link')
async def get_connection_link(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get subscription connection link and instructions."""
    from app.utils.subscription_utils import (
        convert_subscription_link_to_happ_scheme,
        get_display_subscription_link,
        get_happ_cryptolink_redirect_link,
    )

    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    subscription_url = user.subscription.subscription_url
    if not subscription_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Subscription link not yet generated',
        )

    display_link = get_display_subscription_link(user.subscription)
    happ_redirect = get_happ_cryptolink_redirect_link(subscription_url) if settings.is_happ_cryptolink_mode() else None
    happ_scheme_link = (
        convert_subscription_link_to_happ_scheme(subscription_url) if settings.is_happ_cryptolink_mode() else None
    )

    connect_mode = settings.CONNECT_BUTTON_MODE
    hide_subscription_link = settings.should_hide_subscription_link()

    return {
        'subscription_url': subscription_url if not hide_subscription_link else None,
        'display_link': display_link if not hide_subscription_link else None,
        'happ_redirect_link': happ_redirect,
        'happ_scheme_link': happ_scheme_link,
        'connect_mode': connect_mode,
        'hide_link': hide_subscription_link,
        'instructions': {
            'steps': [
                'Copy the subscription link',
                'Open your VPN application',
                "Find 'Add subscription' or 'Import' option",
                'Paste the copied link',
            ]
        },
    }


# ============ hApp Downloads ============


@router.get('/happ-downloads')
async def get_happ_downloads(
    user: User = Depends(get_current_cabinet_user),
) -> dict[str, Any]:
    """Get hApp download links for different platforms."""
    platforms = {
        'ios': {
            'name': 'iOS (iPhone/iPad)',
            'icon': '🍎',
            'link': settings.get_happ_download_link('ios'),
        },
        'android': {
            'name': 'Android',
            'icon': '🤖',
            'link': settings.get_happ_download_link('android'),
        },
        'macos': {
            'name': 'macOS',
            'icon': '🖥️',
            'link': settings.get_happ_download_link('macos'),
        },
        'windows': {
            'name': 'Windows',
            'icon': '💻',
            'link': settings.get_happ_download_link('windows'),
        },
    }

    # Filter out platforms without links
    available_platforms = {k: v for k, v in platforms.items() if v['link']}

    return {
        'platforms': available_platforms,
        'happ_enabled': bool(available_platforms),
    }


def _resolve_button_url(
    url: str,
    subscription_url: str | None,
    subscription_crypto_link: str | None,
) -> str:
    """Resolve template variables in button URLs.

    Matches remnawave/subscription-page frontend TemplateEngine:
    - {{SUBSCRIPTION_LINK}} -> plain subscription URL
    - {{HAPP_CRYPT3_LINK}} -> crypto link
    - {{HAPP_CRYPT4_LINK}} -> crypto link
    """
    if not url:
        return url
    result = url
    if subscription_url:
        result = result.replace('{{SUBSCRIPTION_LINK}}', subscription_url)
    if subscription_crypto_link:
        result = result.replace('{{HAPP_CRYPT3_LINK}}', subscription_crypto_link)
        result = result.replace('{{HAPP_CRYPT4_LINK}}', subscription_crypto_link)
    return result


@router.get('/app-config')
async def get_app_config(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get app configuration for connection with deep links."""
    await db.refresh(user, ['subscription'])

    subscription_url = None
    subscription_crypto_link = None
    if user.subscription:
        subscription_url = user.subscription.subscription_url
        subscription_crypto_link = user.subscription.subscription_crypto_link

    # Load config from RemnaWave (if configured) or local file
    config = await _load_app_config_async()

    is_remnawave = config.pop('_isRemnawave', False)
    hide_link = settings.should_hide_subscription_link()

    # Строим platformNames из displayName каждой платформы RemnaWave
    platform_names: dict[str, Any] = {}
    for pk, pd in config.get('platforms', {}).items():
        if isinstance(pd, dict) and 'displayName' in pd:
            platform_names[pk] = pd['displayName']
    # Фоллбэк для платформ без displayName (en достаточно)
    fallback_names = {
        'ios': {'en': 'iPhone/iPad'},
        'android': {'en': 'Android'},
        'macos': {'en': 'macOS'},
        'windows': {'en': 'Windows'},
        'linux': {'en': 'Linux'},
        'androidTV': {'en': 'Android TV'},
        'appleTV': {'en': 'Apple TV'},
    }
    for k, v in fallback_names.items():
        if k not in platform_names:
            platform_names[k] = v

    if is_remnawave:
        # ── RemnaWave original format ──
        # Serve original blocks/svgLibrary enriched with deep links and resolved URLs.
        platforms: dict[str, Any] = {}
        for platform_key, platform_data in config.get('platforms', {}).items():
            if not isinstance(platform_data, dict):
                continue
            apps = platform_data.get('apps', [])
            if not isinstance(apps, list):
                continue

            enriched_apps = []
            for app in apps:
                if not isinstance(app, dict):
                    continue

                # Generate deep link
                deep_link = None
                if subscription_url or subscription_crypto_link:
                    deep_link = _create_deep_link(app, subscription_url, subscription_crypto_link)
                app['deepLink'] = deep_link

                # Resolve templates only for subscriptionLink and copyButton (not external)
                for block in app.get('blocks', []):
                    if not isinstance(block, dict):
                        continue
                    for btn in block.get('buttons', []):
                        if not isinstance(btn, dict):
                            continue
                        btn_type = btn.get('type', '')
                        if btn_type in ('subscriptionLink', 'copyButton'):
                            url = btn.get('url', '') or btn.get('link', '')
                            if url and '{{' in url:
                                btn['resolvedUrl'] = _resolve_button_url(
                                    url,
                                    subscription_url,
                                    subscription_crypto_link,
                                )

                enriched_apps.append(app)

            if enriched_apps:
                # Сохраняем platform-level поля (svgIconKey, displayName и т.д.)
                platform_output = {k: v for k, v in platform_data.items() if k != 'apps'}
                platform_output['apps'] = enriched_apps
                platforms[platform_key] = platform_output

        return {
            'isRemnawave': True,
            'platforms': platforms,
            'svgLibrary': config.get('svgLibrary', {}),
            'baseTranslations': config.get('baseTranslations'),
            'baseSettings': config.get('baseSettings'),
            'uiConfig': config.get('uiConfig', {}),
            'platformNames': platform_names,
            'hasSubscription': bool(subscription_url or subscription_crypto_link),
            'subscriptionUrl': subscription_url,
            'subscriptionCryptoLink': subscription_crypto_link,
            'hideLink': hide_link,
            'branding': config.get('brandingSettings', {}),
        }

    # ── Legacy file-based format ──
    platforms_raw = config.get('platforms', {})
    if not isinstance(platforms_raw, dict):
        platforms_raw = {}

    platforms_legacy: dict[str, Any] = {}
    for platform_key, apps in platforms_raw.items():
        if not isinstance(apps, list):
            continue

        platform_apps = []
        for app in apps:
            if not isinstance(app, dict):
                continue

            app_data = {
                'id': app.get('id'),
                'name': app.get('name'),
                'isFeatured': app.get('isFeatured', False),
                'installationStep': app.get('installationStep'),
                'addSubscriptionStep': app.get('addSubscriptionStep'),
                'connectAndUseStep': app.get('connectAndUseStep'),
                'additionalBeforeAddSubscriptionStep': app.get('additionalBeforeAddSubscriptionStep'),
                'additionalAfterAddSubscriptionStep': app.get('additionalAfterAddSubscriptionStep'),
            }

            # Add deep link if subscription exists
            if subscription_url or subscription_crypto_link:
                app_data['deepLink'] = _create_deep_link(app, subscription_url, subscription_crypto_link)

            platform_apps.append(app_data)

        if platform_apps:
            platforms_legacy[platform_key] = platform_apps

    return {
        'platforms': platforms_legacy,
        'platformNames': platform_names,
        'hasSubscription': bool(subscription_url or subscription_crypto_link),
        'subscriptionUrl': subscription_url if not hide_link else None,
        'subscriptionCryptoLink': subscription_crypto_link if not hide_link else None,
        'hideLink': hide_link,
        'branding': config.get('config', {}).get('branding', {}),
    }


# ============ Device Management ============


@router.get('/devices')
async def get_devices(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get list of connected devices."""
    from app.services.remnawave_service import RemnaWaveService

    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if not user.remnawave_uuid:
        return {
            'devices': [],
            'total': 0,
            'device_limit': user.subscription.device_limit or 1,
        }

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            response = await api.get_user_devices(user.remnawave_uuid)

            devices_list = response.get('devices', [])
            formatted_devices = []
            for device in devices_list:
                hwid = device.get('hwid') or device.get('deviceId') or device.get('id')
                platform = device.get('platform') or device.get('platformType') or 'Unknown'
                model = device.get('deviceModel') or device.get('model') or device.get('name') or 'Unknown'
                created_at = device.get('updatedAt') or device.get('lastSeen') or device.get('createdAt')

                formatted_devices.append(
                    {
                        'hwid': hwid,
                        'platform': platform,
                        'device_model': model,
                        'created_at': created_at,
                    }
                )

            return {
                'devices': formatted_devices,
                'total': response.get('total', len(formatted_devices)),
                'device_limit': user.subscription.device_limit or 1,
            }

    except Exception as e:
        logger.error(f'Error fetching devices: {e}')
        return {
            'devices': [],
            'total': 0,
            'device_limit': user.subscription.device_limit or 1,
        }


@router.delete('/devices/{hwid}')
async def delete_device(
    hwid: str,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Delete a specific device by HWID."""
    from app.services.remnawave_service import RemnaWaveService

    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if not user.remnawave_uuid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='User UUID not found',
        )

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            delete_data = {'userUuid': user.remnawave_uuid, 'hwid': hwid}
            await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)

            return {
                'success': True,
                'message': 'Device deleted successfully',
                'deleted_hwid': hwid,
            }

    except Exception as e:
        logger.error(f'Error deleting device: {e}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete device',
        )


@router.delete('/devices')
async def delete_all_devices(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Delete all connected devices."""
    from app.services.remnawave_service import RemnaWaveService

    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if not user.remnawave_uuid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='User UUID not found',
        )

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            # Get all devices first
            response = await api._make_request('GET', f'/api/hwid/devices/{user.remnawave_uuid}')

            if not response or 'response' not in response:
                return {
                    'success': True,
                    'message': 'No devices to delete',
                    'deleted_count': 0,
                }

            devices_list = response['response'].get('devices', [])
            if not devices_list:
                return {
                    'success': True,
                    'message': 'No devices to delete',
                    'deleted_count': 0,
                }

            deleted_count = 0
            for device in devices_list:
                device_hwid = device.get('hwid')
                if device_hwid:
                    try:
                        delete_data = {'userUuid': user.remnawave_uuid, 'hwid': device_hwid}
                        await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
                        deleted_count += 1
                    except Exception as device_error:
                        logger.error(f'Error deleting device {device_hwid}: {device_error}')

            return {
                'success': True,
                'message': f'Deleted {deleted_count} devices',
                'deleted_count': deleted_count,
            }

    except Exception as e:
        logger.error(f'Error deleting all devices: {e}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete devices',
        )


# ============ Device Reduction ============


@router.get('/devices/reduction-info')
async def get_device_reduction_info(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get info about device limit reduction availability."""
    from app.services.remnawave_service import RemnaWaveService

    await db.refresh(user, ['subscription'])

    if not user.subscription:
        return {
            'available': False,
            'reason': 'No subscription found',
            'current_device_limit': 0,
            'min_device_limit': 1,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    subscription = user.subscription

    # Check if it's a trial subscription
    if subscription.is_trial:
        return {
            'available': False,
            'reason': 'Device reduction is not available for trial subscriptions',
            'current_device_limit': subscription.device_limit or 1,
            'min_device_limit': 1,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    # Get tariff info for min device limit
    tariff = None
    min_device_limit = 1
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff:
            min_device_limit = getattr(tariff, 'device_limit', 1) or 1

    current_device_limit = subscription.device_limit or 1

    # Can't reduce below minimum
    if current_device_limit <= min_device_limit:
        return {
            'available': False,
            'reason': 'Already at minimum device limit for your tariff',
            'current_device_limit': current_device_limit,
            'min_device_limit': min_device_limit,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    # Get connected devices count
    connected_devices_count = 0
    if user.remnawave_uuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                response = await api._make_request('GET', f'/api/hwid/devices/{user.remnawave_uuid}')
                if response and 'response' in response:
                    connected_devices_count = response['response'].get('total', 0)
        except Exception as e:
            logger.error(f'Error getting connected devices count: {e}')

    can_reduce = current_device_limit - min_device_limit

    return {
        'available': True,
        'current_device_limit': current_device_limit,
        'min_device_limit': min_device_limit,
        'can_reduce': can_reduce,
        'connected_devices_count': connected_devices_count,
    }


@router.post('/devices/reduce')
async def reduce_devices(
    request: dict[str, int],
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Reduce device limit (no refund)."""
    from app.services.remnawave_service import RemnaWaveService

    new_device_limit = request.get('new_device_limit')
    if not new_device_limit or new_device_limit < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid new_device_limit',
        )

    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    subscription = user.subscription

    if subscription.is_trial:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Device reduction is not available for trial subscriptions',
        )

    # Get tariff info for min device limit
    tariff = None
    min_device_limit = 1
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff:
            min_device_limit = getattr(tariff, 'device_limit', 1) or 1

    current_device_limit = subscription.device_limit or 1

    # Validate new limit
    if new_device_limit >= current_device_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='New device limit must be less than current limit',
        )

    if new_device_limit < min_device_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Cannot reduce below minimum device limit ({min_device_limit}) for your tariff',
        )

    # Get connected devices and remove excess (last connected ones)
    connected_devices_count = 0
    devices_removed_count = 0
    if user.remnawave_uuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                response = await api._make_request('GET', f'/api/hwid/devices/{user.remnawave_uuid}')
                if response and 'response' in response:
                    devices_list = response['response'].get('devices', [])
                    connected_devices_count = len(devices_list)

                    # If connected devices exceed new limit, remove excess (last connected)
                    if connected_devices_count > new_device_limit:
                        devices_to_remove = connected_devices_count - new_device_limit
                        logger.info(
                            f'Removing {devices_to_remove} excess devices for user {user.id}: '
                            f'had {connected_devices_count}, new limit {new_device_limit}'
                        )

                        # Sort by date (oldest first) and remove the last ones
                        sorted_devices = sorted(
                            devices_list,
                            key=lambda d: d.get('updatedAt') or d.get('createdAt') or '',
                        )
                        devices_to_delete = sorted_devices[-devices_to_remove:]

                        for device in devices_to_delete:
                            device_hwid = device.get('hwid')
                            if device_hwid:
                                try:
                                    delete_data = {'userUuid': user.remnawave_uuid, 'hwid': device_hwid}
                                    await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
                                    devices_removed_count += 1
                                    logger.info(f'Removed device {device_hwid} for user {user.id}')
                                except Exception as del_error:
                                    logger.error(f'Error removing device {device_hwid}: {del_error}')
        except Exception as e:
            logger.error(f'Error checking/removing devices: {e}')

    old_device_limit = current_device_limit

    # Update subscription
    subscription.device_limit = new_device_limit
    subscription.updated_at = datetime.utcnow()
    await db.commit()

    # Update RemnaWave
    try:
        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)
    except Exception as e:
        logger.error(f'Error updating RemnaWave user: {e}')

    logger.info(
        f'User {user.id} reduced device limit from {old_device_limit} to {new_device_limit}'
        + (f' (removed {devices_removed_count} devices)' if devices_removed_count > 0 else '')
    )

    return {
        'success': True,
        'message': 'Device limit reduced successfully'
        + (f' ({devices_removed_count} devices removed)' if devices_removed_count > 0 else ''),
        'old_device_limit': old_device_limit,
        'new_device_limit': new_device_limit,
        'devices_removed': devices_removed_count,
    }


# ============ Tariff Switch ============


@router.post('/tariff/switch/preview')
async def preview_tariff_switch(
    request: TariffPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Preview tariff switch - shows cost calculation."""
    if not settings.is_tariffs_mode():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Tariffs mode is not enabled',
        )

    await db.refresh(user, ['subscription'])

    if not user.subscription or not user.subscription.tariff_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No active subscription with tariff',
        )

    # Use actual_status for correct status check (handles time-based expiration)
    actual_status = user.subscription.actual_status
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

    current_tariff = await get_tariff_by_id(db, user.subscription.tariff_id)
    new_tariff = await get_tariff_by_id(db, request.tariff_id)

    if not new_tariff or not new_tariff.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found or inactive',
        )

    if user.subscription.tariff_id == request.tariff_id:
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
    if user.subscription.end_date and user.subscription.end_date > datetime.utcnow():
        delta = user.subscription.end_date - datetime.utcnow()
        remaining_days = max(0, delta.days)

    # Calculate switch cost
    current_is_daily = getattr(current_tariff, 'is_daily', False) if current_tariff else False
    new_is_daily = getattr(new_tariff, 'is_daily', False)
    switching_to_daily = not current_is_daily and new_is_daily
    switching_from_daily = current_is_daily and not new_is_daily

    def get_monthly_price(tariff) -> int:
        """Get 30-day price from tariff, or calculate from closest period."""
        if not tariff or not tariff.period_prices:
            return 0
        # Try to get 30-day price directly
        if '30' in tariff.period_prices:
            return tariff.period_prices['30']
        # Find closest period and calculate monthly equivalent
        min_period = None
        min_price = 0
        for period_str, price in tariff.period_prices.items():
            period_days = int(period_str)
            if min_period is None or period_days < min_period:
                min_period = period_days
                min_price = price
        if min_period and min_period > 0:
            return int(min_price * 30 / min_period)
        return 0

    # Get period discount percent for cost calculation
    period_discount_percent = _get_period_discount_percent(user, remaining_days if remaining_days > 0 else 30)
    base_upgrade_cost = 0
    discount_value = 0

    if switching_to_daily:
        # Switching TO daily - pay first day price
        daily_price = getattr(new_tariff, 'daily_price_kopeks', 0)
        base_upgrade_cost = daily_price
        # Apply discount to daily price
        if period_discount_percent > 0 and base_upgrade_cost > 0:
            discount_value = int(base_upgrade_cost * period_discount_percent / 100)
            upgrade_cost = base_upgrade_cost - discount_value
        else:
            upgrade_cost = base_upgrade_cost
        is_upgrade = upgrade_cost > 0
    elif switching_from_daily:
        # Switching FROM daily TO periodic - full payment for new tariff
        min_period_price = 0
        if new_tariff.period_prices:
            min_period_price = min(new_tariff.period_prices.values())
        base_upgrade_cost = min_period_price
        # Apply discount
        if period_discount_percent > 0 and base_upgrade_cost > 0:
            discount_value = int(base_upgrade_cost * period_discount_percent / 100)
            upgrade_cost = base_upgrade_cost - discount_value
        else:
            upgrade_cost = base_upgrade_cost
        is_upgrade = upgrade_cost > 0
    else:
        # Calculate proportional cost difference using monthly prices
        current_monthly = get_monthly_price(current_tariff)
        new_monthly = get_monthly_price(new_tariff)

        price_diff = new_monthly - current_monthly

        if price_diff > 0:
            # Upgrade - pay proportional difference
            base_upgrade_cost = int(price_diff * remaining_days / 30)
            # Apply discount to upgrade cost
            if period_discount_percent > 0 and base_upgrade_cost > 0:
                discount_value = int(base_upgrade_cost * period_discount_percent / 100)
                upgrade_cost = base_upgrade_cost - discount_value
            else:
                upgrade_cost = base_upgrade_cost
            is_upgrade = True
        else:
            # Downgrade or same - free
            upgrade_cost = 0
            base_upgrade_cost = 0
            is_upgrade = False

    balance = user.balance_kopeks or 0
    has_enough = balance >= upgrade_cost
    missing = max(0, upgrade_cost - balance) if not has_enough else 0

    response = {
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
) -> dict[str, Any]:
    """Switch to a different tariff without changing end date."""
    from datetime import timedelta

    if not settings.is_tariffs_mode():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Tariffs mode is not enabled',
        )

    await db.refresh(user, ['subscription'])

    if not user.subscription or not user.subscription.tariff_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No active subscription with tariff',
        )

    # Use actual_status for correct status check (handles time-based expiration)
    actual_status = user.subscription.actual_status
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

    current_tariff = await get_tariff_by_id(db, user.subscription.tariff_id)
    new_tariff = await get_tariff_by_id(db, request.tariff_id)

    if not new_tariff or not new_tariff.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found or inactive',
        )

    if user.subscription.tariff_id == request.tariff_id:
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

    # Calculate remaining days
    remaining_days = 0
    if user.subscription.end_date and user.subscription.end_date > datetime.utcnow():
        delta = user.subscription.end_date - datetime.utcnow()
        remaining_days = max(0, delta.days)

    # Calculate cost
    current_is_daily = getattr(current_tariff, 'is_daily', False) if current_tariff else False
    new_is_daily = getattr(new_tariff, 'is_daily', False)
    switching_from_daily = current_is_daily and not new_is_daily
    switching_to_daily = not current_is_daily and new_is_daily

    # Get period discount percent for cost calculation
    period_discount_percent = _get_period_discount_percent(user, remaining_days if remaining_days > 0 else 30)
    base_upgrade_cost = 0
    discount_value = 0

    if switching_to_daily:
        # Switching TO daily tariff - charge first day price
        daily_price = getattr(new_tariff, 'daily_price_kopeks', 0)
        if daily_price <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Daily tariff has invalid price',
            )
        base_upgrade_cost = daily_price
        # Apply discount
        if period_discount_percent > 0 and base_upgrade_cost > 0:
            discount_value = int(base_upgrade_cost * period_discount_percent / 100)
            upgrade_cost = base_upgrade_cost - discount_value
        else:
            upgrade_cost = base_upgrade_cost
        new_period_days = 1  # Daily tariff starts with 1 day
    elif switching_from_daily:
        # Switch FROM daily to regular tariff - pay for minimum period
        min_period_days = 30
        min_period_price = 0
        if new_tariff.period_prices:
            min_period_days = min(int(k) for k in new_tariff.period_prices.keys())
            min_period_price = new_tariff.period_prices.get(str(min_period_days), 0)
        base_upgrade_cost = min_period_price
        # Apply discount
        if period_discount_percent > 0 and base_upgrade_cost > 0:
            discount_value = int(base_upgrade_cost * period_discount_percent / 100)
            upgrade_cost = base_upgrade_cost - discount_value
        else:
            upgrade_cost = base_upgrade_cost
        new_period_days = min_period_days
    else:
        # Regular tariff switch - calculate proportional cost difference using monthly prices
        def get_monthly_price(tariff) -> int:
            if not tariff or not tariff.period_prices:
                return 0
            if '30' in tariff.period_prices:
                return tariff.period_prices['30']
            min_period = None
            min_price = 0
            for period_str, price in tariff.period_prices.items():
                period_days = int(period_str)
                if min_period is None or period_days < min_period:
                    min_period = period_days
                    min_price = price
            if min_period and min_period > 0:
                return int(min_price * 30 / min_period)
            return 0

        current_monthly = get_monthly_price(current_tariff)
        new_monthly = get_monthly_price(new_tariff)
        price_diff = new_monthly - current_monthly

        if price_diff > 0:
            base_upgrade_cost = int(price_diff * remaining_days / 30)
            # Apply discount
            if period_discount_percent > 0 and base_upgrade_cost > 0:
                discount_value = int(base_upgrade_cost * period_discount_percent / 100)
                upgrade_cost = base_upgrade_cost - discount_value
            else:
                upgrade_cost = base_upgrade_cost
        else:
            upgrade_cost = 0
            base_upgrade_cost = 0
        new_period_days = 0

    # Charge if upgrade
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

        success = await subtract_user_balance(db, user, upgrade_cost, description)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to charge balance',
            )

        # Create transaction
        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=upgrade_cost,
            description=description,
        )

    # Update subscription
    old_tariff_name = current_tariff.name if current_tariff else 'Unknown'
    user.subscription.tariff_id = new_tariff.id
    user.subscription.traffic_limit_gb = new_tariff.traffic_limit_gb
    user.subscription.device_limit = new_tariff.device_limit
    user.subscription.connected_squads = new_tariff.allowed_squads or []

    # Reset purchased traffic and delete TrafficPurchase records on tariff switch
    from sqlalchemy import delete as sql_delete

    from app.database.models import TrafficPurchase

    await db.execute(sql_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == user.subscription.id))
    user.subscription.purchased_traffic_gb = 0
    user.subscription.traffic_reset_at = None

    if switching_to_daily:
        # Switching TO daily - reset end_date to 1 day, set last_daily_charge_at
        user.subscription.end_date = datetime.utcnow() + timedelta(days=1)
        user.subscription.last_daily_charge_at = datetime.utcnow()
        user.subscription.is_daily_paused = False
    elif switching_from_daily:
        user.subscription.end_date = datetime.utcnow() + timedelta(days=new_period_days)
        user.subscription.is_daily_paused = False

    user.subscription.updated_at = datetime.utcnow()
    await db.commit()

    # Sync with RemnaWave
    try:
        subscription_service = SubscriptionService()
        if getattr(user, 'remnawave_uuid', None):
            await subscription_service.update_remnawave_user(db, user.subscription)
        else:
            await subscription_service.create_remnawave_user(db, user.subscription)
    except Exception as e:
        logger.error(f'Failed to sync tariff switch with RemnaWave: {e}')

    # Reset all devices on tariff switch
    devices_reset = False
    if user.remnawave_uuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                await api.reset_user_devices(user.remnawave_uuid)
                devices_reset = True
                logger.info(f'Reset all devices for user {user.id} on tariff switch')
        except Exception as e:
            logger.error(f'Failed to reset devices on tariff switch: {e}')

    await db.refresh(user)
    await db.refresh(user.subscription)

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
                    subscription=user.subscription,
                    transaction=None,
                    period_days=remaining_days if remaining_days > 0 else new_period_days,
                    was_trial_conversion=False,
                    amount_kopeks=upgrade_cost,
                    purchase_type='tariff_switch',
                )
            finally:
                await bot.session.close()
    except Exception as e:
        logger.error(f'Failed to send admin notification for tariff switch: {e}')

    response = {
        'success': True,
        'message': f"Switched from '{old_tariff_name}' to '{new_tariff.name}'"
        + (' (devices reset)' if devices_reset else ''),
        'subscription': _subscription_to_response(user.subscription),
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


# ============ Daily Subscription Pause ============


@router.post('/pause')
async def toggle_subscription_pause(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Toggle pause/resume for daily subscription."""
    from datetime import timedelta

    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    tariff_id = getattr(user.subscription, 'tariff_id', None)
    if not tariff_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Subscription has no tariff',
        )

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not getattr(tariff, 'is_daily', False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Pause is only available for daily tariffs',
        )

    # Toggle pause state
    is_currently_paused = getattr(user.subscription, 'is_daily_paused', False)
    new_paused_state = not is_currently_paused
    user.subscription.is_daily_paused = new_paused_state

    # Сохраняем статус ДО изменения для проверки RemnaWave
    from app.database.models import SubscriptionStatus

    was_disabled = user.subscription.status == SubscriptionStatus.DISABLED.value

    # If resuming, check balance
    if not new_paused_state:
        daily_price = getattr(tariff, 'daily_price_kopeks', 0)
        if daily_price > 0 and user.balance_kopeks < daily_price:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    'code': 'insufficient_balance',
                    'message': 'Insufficient balance to resume daily subscription',
                    'required': daily_price,
                    'balance': user.balance_kopeks,
                },
            )

        # Restore ACTIVE status if was DISABLED
        if was_disabled:
            user.subscription.status = SubscriptionStatus.ACTIVE.value
            user.subscription.last_daily_charge_at = datetime.utcnow()
            user.subscription.end_date = datetime.utcnow() + timedelta(days=1)

    await db.commit()
    await db.refresh(user.subscription)
    await db.refresh(user)

    # Sync with RemnaWave only when resuming from DISABLED state
    # При паузе НЕ отключаем - пользователь может пользоваться до конца оплаченного периода
    # При возобновлении включаем только если подписка была отключена (DISABLED)
    if not new_paused_state and user.remnawave_uuid and was_disabled:
        try:
            subscription_service = SubscriptionService()
            await subscription_service.enable_remnawave_user(user.remnawave_uuid)
        except Exception as e:
            logger.error(f'Error enabling RemnaWave user on resume: {e}')

    if new_paused_state:
        message = 'Daily subscription paused'
    else:
        message = 'Daily subscription resumed'

    return {
        'success': True,
        'message': message,
        'is_paused': new_paused_state,
        'balance_kopeks': user.balance_kopeks,
        'balance_label': settings.format_price(user.balance_kopeks),
    }


# ============ Traffic Switch (Change Traffic Package) ============


@router.put('/traffic')
async def switch_traffic_package(
    request: TrafficPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Switch to a different traffic package (change limit)."""
    from app.utils.pricing_utils import calculate_prorated_price

    await db.refresh(user, ['subscription'])

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if user.subscription.is_trial:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Traffic management is only available for paid subscriptions',
        )

    current_traffic = user.subscription.traffic_limit_gb or 0
    new_traffic = request.gb

    if current_traffic == new_traffic:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Already on this traffic package',
        )

    # Get available packages
    packages = settings.get_traffic_packages()
    current_pkg = next((p for p in packages if p['gb'] == current_traffic and p.get('enabled', True)), None)
    new_pkg = next((p for p in packages if p['gb'] == new_traffic and p.get('enabled', True)), None)

    if not new_pkg:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid traffic package',
        )

    # Calculate price difference (only charge for upgrade)
    current_price = current_pkg['price'] if current_pkg else 0
    new_price = new_pkg['price']

    if new_price > current_price:
        # Upgrade - charge difference
        price_diff = new_price - current_price

        # Apply promo discount
        traffic_discount_percent = 0
        promo_group = (
            user.get_primary_promo_group()
            if hasattr(user, 'get_primary_promo_group')
            else getattr(user, 'promo_group', None)
        )
        if promo_group:
            apply_to_addons = getattr(promo_group, 'apply_discounts_to_addons', True)
            if apply_to_addons:
                traffic_discount_percent = max(
                    0, min(100, int(getattr(promo_group, 'traffic_discount_percent', 0) or 0))
                )

        if traffic_discount_percent > 0:
            price_diff = int(price_diff * (100 - traffic_discount_percent) / 100)

        # Prorated calculation
        final_price, months_charged = calculate_prorated_price(price_diff, user.subscription.end_date)

        if user.balance_kopeks < final_price:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f'Insufficient balance. Need {final_price / 100:.2f} RUB',
            )

        # Charge balance
        description = f'Traffic upgrade from {current_traffic}GB to {new_traffic}GB'
        success = await subtract_user_balance(db, user, final_price, description)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to charge balance',
            )

        # Create transaction
        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=final_price,
            description=description,
        )

        charged = final_price
    else:
        # Downgrade - no charge, no refund
        charged = 0

    # Update subscription
    user.subscription.traffic_limit_gb = new_traffic
    user.subscription.purchased_traffic_gb = 0  # Reset purchased traffic on switch
    user.subscription.traffic_reset_at = None  # Reset traffic reset date
    user.subscription.updated_at = datetime.utcnow()
    await db.commit()

    # Sync with RemnaWave
    try:
        subscription_service = SubscriptionService()
        if getattr(user, 'remnawave_uuid', None):
            await subscription_service.update_remnawave_user(db, user.subscription)
        else:
            await subscription_service.create_remnawave_user(db, user.subscription)
    except Exception as e:
        logger.error(f'Failed to sync traffic switch with RemnaWave: {e}')

    await db.refresh(user)
    await db.refresh(user.subscription)

    return {
        'success': True,
        'message': f'Traffic changed from {current_traffic}GB to {new_traffic}GB',
        'old_traffic_gb': current_traffic,
        'new_traffic_gb': new_traffic,
        'charged_kopeks': charged,
        'balance_kopeks': user.balance_kopeks,
        'balance_label': settings.format_price(user.balance_kopeks),
    }


# ============ Traffic Refresh ============

# Rate limit: 1 request per 60 seconds per user
TRAFFIC_REFRESH_RATE_LIMIT = 1
TRAFFIC_REFRESH_RATE_WINDOW = 60  # seconds
TRAFFIC_CACHE_TTL = 60  # Cache traffic data for 60 seconds


@router.post('/refresh-traffic')
async def refresh_traffic(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Refresh traffic usage from RemnaWave panel.
    Rate limited to 1 request per 60 seconds.
    """
    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No active subscription',
        )

    # Используем user.id для rate limit и кеша (работает и для email-пользователей)
    user_cache_id = user.id

    # Check rate limit
    is_limited = await RateLimitCache.is_rate_limited(
        user_cache_id,
        'traffic_refresh',
        TRAFFIC_REFRESH_RATE_LIMIT,
        TRAFFIC_REFRESH_RATE_WINDOW,
    )

    if is_limited:
        # Check if we have cached data
        traffic_cache_key = cache_key('traffic', user_cache_id)
        cached_data = await cache.get(traffic_cache_key)

        if cached_data:
            return {
                'success': True,
                'cached': True,
                'rate_limited': True,
                'retry_after_seconds': TRAFFIC_REFRESH_RATE_WINDOW,
                **cached_data,
            }

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f'Rate limited. Try again in {TRAFFIC_REFRESH_RATE_WINDOW} seconds.',
            headers={'Retry-After': str(TRAFFIC_REFRESH_RATE_WINDOW)},
        )

    # Fetch traffic from RemnaWave
    try:
        remnawave_service = RemnaWaveService()

        # Для email-пользователей (без telegram_id) используем UUID
        if user.telegram_id:
            traffic_stats = await remnawave_service.get_user_traffic_stats(user.telegram_id)
        elif user.remnawave_uuid:
            traffic_stats = await remnawave_service.get_user_traffic_stats_by_uuid(user.remnawave_uuid)
        else:
            traffic_stats = None

        if not traffic_stats:
            # Return current database values if RemnaWave unavailable
            traffic_data = {
                'traffic_used_bytes': int((user.subscription.traffic_used_gb or 0) * (1024**3)),
                'traffic_used_gb': round(user.subscription.traffic_used_gb or 0, 2),
                'traffic_limit_bytes': int((user.subscription.traffic_limit_gb or 0) * (1024**3)),
                'traffic_limit_gb': user.subscription.traffic_limit_gb or 0,
                'traffic_used_percent': round(
                    ((user.subscription.traffic_used_gb or 0) / (user.subscription.traffic_limit_gb or 1)) * 100
                    if user.subscription.traffic_limit_gb
                    else 0,
                    1,
                ),
                'is_unlimited': (user.subscription.traffic_limit_gb or 0) == 0,
            }
            return {
                'success': True,
                'cached': False,
                'source': 'database',
                **traffic_data,
            }

        # Update subscription with fresh data
        used_gb = traffic_stats.get('used_traffic_gb', 0)
        if abs((user.subscription.traffic_used_gb or 0) - used_gb) > 0.01:
            user.subscription.traffic_used_gb = used_gb
            user.subscription.updated_at = datetime.utcnow()
            await db.commit()

        # Calculate percentage
        limit_gb = user.subscription.traffic_limit_gb or 0
        if limit_gb > 0:
            percent = min(100, (used_gb / limit_gb) * 100)
        else:
            percent = 0

        traffic_data = {
            'traffic_used_bytes': traffic_stats.get('used_traffic_bytes', 0),
            'traffic_used_gb': round(used_gb, 2),
            'traffic_limit_bytes': traffic_stats.get('traffic_limit_bytes', 0),
            'traffic_limit_gb': limit_gb,
            'traffic_used_percent': round(percent, 1),
            'is_unlimited': limit_gb == 0,
            'lifetime_used_bytes': traffic_stats.get('lifetime_used_traffic_bytes', 0),
            'lifetime_used_gb': round(traffic_stats.get('lifetime_used_traffic_gb', 0), 2),
        }

        # Cache the result
        traffic_cache_key = cache_key('traffic', user_cache_id)
        await cache.set(traffic_cache_key, traffic_data, TRAFFIC_CACHE_TTL)

        return {
            'success': True,
            'cached': False,
            'source': 'remnawave',
            **traffic_data,
        }

    except Exception as e:
        logger.error(f'Error refreshing traffic for user {user.id}: {e}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to refresh traffic data',
        )
