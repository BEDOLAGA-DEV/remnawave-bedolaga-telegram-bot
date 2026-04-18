"""Traffic management endpoints.

GET /subscription/traffic-packages
POST /subscription/traffic
PUT /subscription/traffic
POST /subscription/refresh-traffic
POST /subscription/traffic/save-cart
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import TransactionType, User
from app.services.pricing_engine import pricing_engine
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service
from app.utils.cache import RateLimitCache, cache, cache_key

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import (
    TrafficPackageResponse,
    TrafficPurchaseRequest,
)
from .helpers import _apply_addon_discount, resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get('/traffic-packages', response_model=list[TrafficPackageResponse])
async def get_traffic_packages(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
):
    """Get available traffic packages."""
    from app.database.crud.tariff import get_tariff_by_id

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        return []

    # Режим тарифов - берём пакеты из тарифа
    if settings.is_tariffs_mode() and subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
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
            if price <= 0:
                continue
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
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff and not tariff.allow_traffic_topup:
            return []

    packages = settings.get_traffic_topup_packages()
    result = []

    for pkg in packages:
        if not pkg.get('enabled', True):
            continue
        if pkg['price'] <= 0:
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
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
):
    """Purchase additional traffic."""
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Subscription purchases are restricted for this account',
        )

    from app.database.crud.subscription import add_subscription_traffic
    from app.database.crud.tariff import get_tariff_by_id
    from app.utils.pricing_utils import calculate_prorated_price

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )
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
        if base_price_kopeks <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Traffic package {request.gb}GB has no price configured',
            )

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
        packages = settings.get_traffic_topup_packages()
        matching_pkg = next((pkg for pkg in packages if pkg['gb'] == request.gb and pkg.get('enabled', True)), None)
        if not matching_pkg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid traffic package',
            )
        base_price_kopeks = matching_pkg['price']
        if base_price_kopeks <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Traffic package has no price configured',
            )

    # На тарифах пакеты трафика покупаются на 1 месяц (30 дней),
    # цена в тарифе уже месячная — не умножаем на оставшиеся месяцы подписки.
    # Пропорциональный расчёт применяем только в классическом режиме.
    if is_tariff_mode:
        prorated_price = base_price_kopeks
        days_charged = 30
    else:
        prorated_price, days_charged = calculate_prorated_price(
            base_price_kopeks,
            subscription.end_date,
        )

    # Lock user row to prevent TOCTOU on promo-offer state
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    # Apply discount from promo group using proper method
    period_hint_days = days_charged if days_charged > 0 else 30
    discount_result = _apply_addon_discount(user, 'traffic', prorated_price, period_hint_days)
    final_price = discount_result['discounted']
    traffic_discount_percent = discount_result['percent']
    discount_value = discount_result['discount']

    # Ensure minimum price after discount (except for 100% discount)
    if traffic_discount_percent < 100 and final_price > 0:
        final_price = max(100, final_price)

    # Проверяем баланс
    if final_price > 0 and user.balance_kopeks < final_price:
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
                'Cart saved for traffic purchase (cabinet) user + discount',
                user_id=user.id,
                gb=request.gb,
                traffic_discount_percent=traffic_discount_percent,
            )
        except Exception as e:
            logger.error('Error saving cart for traffic purchase (cabinet)', error=e)

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

    # Добавляем трафик (add_subscription_traffic обновляет purchased_traffic_gb, traffic_reset_at и коммитит)
    await add_subscription_traffic(db, subscription, request.gb)

    # Реактивируем подписку если она была DISABLED/EXPIRED (например, после LIMITED/EXPIRED в RemnaWave)
    from app.database.crud.subscription import reactivate_subscription

    await reactivate_subscription(db, subscription)

    # Синхронизируем с RemnaWave
    try:
        subscription_service = SubscriptionService()
        if settings.is_multi_tariff_enabled():
            _should_create = not subscription.remnawave_uuid
        else:
            _should_create = not getattr(user, 'remnawave_uuid', None)

        if _should_create:
            await subscription_service.create_remnawave_user(db, subscription)
        else:
            await subscription_service.update_remnawave_user(db, subscription)
            if subscription.status == 'active':
                _enable_uuid = (
                    subscription.remnawave_uuid
                    if settings.is_multi_tariff_enabled()
                    else getattr(user, 'remnawave_uuid', None)
                )
                if _enable_uuid:
                    await subscription_service.enable_remnawave_user(_enable_uuid)
    except Exception as e:
        logger.error('Failed to sync traffic with RemnaWave', error=e)
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=user.id,
            action='create' if _should_create else 'update',
        )

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
        logger.error('Failed to send admin notification for traffic purchase', error=e)

    response: dict[str, Any] = {
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


@router.post('/traffic/save-cart')
async def save_traffic_cart(
    request: TrafficPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, bool]:
    """Save cart for traffic purchase (for insufficient balance flow)."""

    subscription = await resolve_subscription(db, user, subscription_id)

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

        packages = settings.get_traffic_topup_packages()
        matching_pkg = next((pkg for pkg in packages if pkg['gb'] == request.gb and pkg.get('enabled', True)), None)
        if not matching_pkg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Недоступный пакет трафика',
            )
        base_price_kopeks = matching_pkg['price']

    # Calculate prorated price (days-based), then apply discount
    from app.utils.pricing_utils import calculate_prorated_price as _calc_prorated

    now = datetime.now(UTC)
    days_left = max(1, (subscription.end_date - now).days)
    prorated_price, _ = _calc_prorated(
        base_price_kopeks,
        subscription.end_date,
    )
    discount_result = _apply_addon_discount(user, 'traffic', prorated_price, days_left)
    final_price = discount_result['discounted']
    traffic_discount_percent = discount_result['percent']

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
    logger.info('Cart saved for traffic purchase (cabinet save-cart) user +', user_id=user.id, gb=request.gb)

    return {'success': True, 'cart_saved': True}


# ============ Traffic Switch (Change Traffic Package) ============


@router.put('/traffic')
async def switch_traffic_package(
    request: TrafficPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Switch to a different traffic package (change limit)."""
    from app.utils.pricing_utils import calculate_prorated_price

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if subscription.is_trial:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Traffic management is only available for paid subscriptions',
        )

    current_traffic = subscription.traffic_limit_gb or 0
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

        # Lock user row to prevent TOCTOU on promo-offer state
        from app.database.crud.user import lock_user_for_pricing

        user = await lock_user_for_pricing(db, user.id)

        # Apply promo discount via PricingEngine
        price_diff, _discount_val, traffic_discount_percent = pricing_engine.calculate_traffic_discount(
            price_diff,
            user,
        )

        # Prorated calculation
        final_price, days_charged = calculate_prorated_price(price_diff, subscription.end_date)

        if final_price > 0 and user.balance_kopeks < final_price:
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

    # Update subscription — delete TrafficPurchase records before resetting purchased_traffic_gb
    from sqlalchemy import delete as sql_delete

    from app.database.models import TrafficPurchase

    await db.execute(sql_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
    subscription.traffic_limit_gb = new_traffic
    subscription.purchased_traffic_gb = 0  # Reset purchased traffic on switch
    subscription.traffic_reset_at = None  # Reset traffic reset date
    subscription.updated_at = datetime.now(UTC)
    await db.commit()

    # Sync with RemnaWave
    try:
        subscription_service = SubscriptionService()
        if settings.is_multi_tariff_enabled():
            _should_create = not subscription.remnawave_uuid
        else:
            _should_create = not getattr(user, 'remnawave_uuid', None)

        if _should_create:
            await subscription_service.create_remnawave_user(db, subscription)
        else:
            await subscription_service.update_remnawave_user(db, subscription)
    except Exception as e:
        logger.error('Failed to sync traffic switch with RemnaWave', error=e)
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        if hasattr(subscription, 'id') and hasattr(subscription, 'user_id'):
            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=subscription.user_id,
                action='create' if _should_create else 'update',
            )

    await db.refresh(user)
    await db.refresh(subscription)

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
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
):
    """
    Refresh traffic usage from RemnaWave panel.
    Rate limited to 1 request per 60 seconds.
    """
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No active subscription',
        )

    # Use per-subscription key when subscription_id is available so that refreshing
    # Sub B is not blocked by a previous refresh of Sub A (multi-tariff mode).
    cache_suffix = f'{user.id}_{subscription_id}' if subscription_id is not None else str(user.id)

    # Check rate limit
    is_limited = await RateLimitCache.is_rate_limited(
        cache_suffix,
        'traffic_refresh',
        TRAFFIC_REFRESH_RATE_LIMIT,
        TRAFFIC_REFRESH_RATE_WINDOW,
    )

    if is_limited:
        # Check if we have cached data
        traffic_cache_key = cache_key('traffic', cache_suffix)
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

        # Resolve panel UUID for traffic lookup
        _traffic_uuid = (
            subscription.remnawave_uuid
            if settings.is_multi_tariff_enabled() and subscription.remnawave_uuid
            else user.remnawave_uuid
        )
        if user.telegram_id and not settings.is_multi_tariff_enabled():
            traffic_stats = await remnawave_service.get_user_traffic_stats(user.telegram_id)
        elif _traffic_uuid:
            traffic_stats = await remnawave_service.get_user_traffic_stats_by_uuid(_traffic_uuid)
        else:
            traffic_stats = None

        if not traffic_stats:
            # Return current database values if RemnaWave unavailable
            traffic_data = {
                'traffic_used_bytes': int((subscription.traffic_used_gb or 0) * (1024**3)),
                'traffic_used_gb': round(subscription.traffic_used_gb or 0, 2),
                'traffic_limit_bytes': int((subscription.traffic_limit_gb or 0) * (1024**3)),
                'traffic_limit_gb': subscription.traffic_limit_gb or 0,
                'traffic_used_percent': round(
                    ((subscription.traffic_used_gb or 0) / (subscription.traffic_limit_gb or 1)) * 100
                    if subscription.traffic_limit_gb
                    else 0,
                    1,
                ),
                'is_unlimited': (subscription.traffic_limit_gb or 0) == 0,
            }
            return {
                'success': True,
                'cached': False,
                'source': 'database',
                **traffic_data,
            }

        # Update subscription with fresh data
        used_gb = traffic_stats.get('used_traffic_gb', 0)
        if abs((subscription.traffic_used_gb or 0) - used_gb) > 0.01:
            subscription.traffic_used_gb = used_gb
            subscription.updated_at = datetime.now(UTC)
            await db.commit()

        # Calculate percentage
        limit_gb = subscription.traffic_limit_gb or 0
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
        traffic_cache_key = cache_key('traffic', cache_suffix)
        await cache.set(traffic_cache_key, traffic_data, TRAFFIC_CACHE_TTL)

        return {
            'success': True,
            'cached': False,
            'source': 'remnawave',
            **traffic_data,
        }

    except Exception as e:
        logger.error('Error refreshing traffic for user', user_id=user.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to refresh traffic data',
        )
