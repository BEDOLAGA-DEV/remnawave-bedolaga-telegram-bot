"""Device management endpoints.

GET /subscription/devices
POST /subscription/devices (legacy)
DELETE /subscription/devices/{hwid}
DELETE /subscription/devices
POST /subscription/devices/purchase
GET /subscription/devices/reduction-info
POST /subscription/devices/reduce
GET /subscription/devices/price
POST /subscription/devices/save-cart
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import Subscription, TransactionType, User
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import DevicePurchaseRequest
from .helpers import _apply_addon_discount, resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


def _resolve_panel_uuid(subscription: Subscription | None, user: User) -> str | None:
    """Resolve RemnaWave panel UUID: per-subscription in multi-tariff, user-level otherwise."""
    if settings.is_multi_tariff_enabled() and subscription and subscription.remnawave_uuid:
        return subscription.remnawave_uuid
    return user.remnawave_uuid


@router.post('/devices')
async def purchase_devices_legacy(
    request: DevicePurchaseRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Purchase additional device slots (legacy endpoint).

    DEPRECATED: Use /devices/purchase instead for full tariff and discount support.
    Now uses tariff-aware pricing when subscription has a tariff_id.
    """
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Subscription purchases are restricted for this account',
        )

    # Resolve subscription (ownership validated), then lock the row for concurrent safety
    resolved = await resolve_subscription(db, user, subscription_id)
    if not resolved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    result = await db.execute(
        select(Subscription)
        .where(and_(Subscription.id == resolved.id, Subscription.user_id == user.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
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
    if tariff and tariff.device_price_kopeks is not None:
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

    base_total_price = device_price * request.devices

    # Lock user row to prevent TOCTOU on promo-offer state
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    # Apply discount from promo group
    discount_result = _apply_addon_discount(user, 'devices', base_total_price, 30)
    total_price = discount_result['discounted']
    devices_discount_percent = discount_result['percent']

    # Ensure minimum price after discount (except for 100% discount)
    if devices_discount_percent < 100 and total_price > 0:
        total_price = max(100, total_price)

    # Check max devices limit (under row lock — prevents concurrent purchases exceeding limit)
    current_devices = subscription.device_limit or 1
    new_devices = current_devices + request.devices

    if max_device_limit and new_devices > max_device_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Максимальное количество устройств: {max_device_limit}',
        )

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
            logger.info(
                'Cart saved for device purchase (cabinet /devices) user + devices',
                user_id=user.id,
                devices=request.devices,
            )
        except Exception as e:
            logger.error('Error saving cart for device purchase (cabinet /devices)', error=e)

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

    # Deduct balance and create transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import PaymentMethod

    # Build description with discount info
    if devices_discount_percent > 0:
        description = f'Покупка {request.devices} доп. устройств (скидка {devices_discount_percent}%)'
    else:
        description = f'Покупка {request.devices} доп. устройств'

    success = await subtract_user_balance(
        db=db,
        user=user,
        amount_kopeks=total_price,
        description=description,
        create_transaction=True,
        payment_method=PaymentMethod.BALANCE,
        transaction_type=TransactionType.SUBSCRIPTION_PAYMENT,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail='Insufficient funds',
        )

    # Re-lock subscription after subtract_user_balance committed (which released all locks).
    # Re-validate max device limit to prevent concurrent purchases exceeding the limit.
    relock_result = await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    subscription = relock_result.scalar_one()

    actual_current = subscription.device_limit or 1
    actual_new = actual_current + request.devices
    if max_device_limit and actual_new > max_device_limit:
        # Concurrent purchase already exceeded limit — refund balance
        user_refund = await db.execute(
            select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
        )
        refund_user = user_refund.scalar_one()
        refund_user.balance_kopeks += total_price
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Максимальное количество устройств: {max_device_limit}. Баланс возвращён.',
        )

    # Add devices (under lock)
    subscription.device_limit = actual_new
    await db.commit()
    await db.refresh(subscription)
    await db.refresh(user)

    # Sync with RemnaWave
    try:
        service = SubscriptionService()
        if _resolve_panel_uuid(subscription, user):
            await service.update_remnawave_user(db, subscription)
        else:
            await service.create_remnawave_user(db, subscription)
    except Exception as e:
        logger.error('Failed to sync devices with RemnaWave (legacy endpoint)', error=e)

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
                    new_value=actual_new,
                    price_paid=total_price,
                )
            finally:
                await bot.session.close()
    except Exception as e:
        logger.error('Failed to send admin notification for device purchase', error=e)

    response: dict[str, Any] = {
        'message': 'Devices added successfully',
        'devices_added': request.devices,
        'new_device_limit': actual_new,
        'amount_paid_kopeks': total_price,
    }

    if devices_discount_percent > 0:
        response['discount_percent'] = devices_discount_percent
        response['discount_kopeks'] = discount_result['discount']
        response['base_price_kopeks'] = base_total_price

    return response


@router.post('/devices/purchase')
async def purchase_devices(
    request: DevicePurchaseRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Purchase additional device slots for subscription."""
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Subscription purchases are restricted for this account',
        )

    try:
        # Resolve subscription (ownership validated), then lock the row for concurrent safety
        resolved = await resolve_subscription(db, user, subscription_id)
        if not resolved:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='У вас нет активной подписки')

        result = await db.execute(
            select(Subscription)
            .where(and_(Subscription.id == resolved.id, Subscription.user_id == user.id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        subscription = result.scalar_one_or_none()

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
        if tariff and tariff.device_price_kopeks is not None:
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

        # Check max device limit (under row lock — prevents concurrent purchases exceeding limit)
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
        total_days = 30  # Base period for device price calculation

        # Calculate base price before discount
        base_price_per_month = device_price * request.devices
        base_price_prorated = int(base_price_per_month * days_left / total_days)
        base_price_prorated = max(100, base_price_prorated)  # Minimum 1 ruble

        # Lock user BEFORE discount computation to prevent TOCTOU on promo group
        from app.database.crud.user import lock_user_for_pricing

        user = await lock_user_for_pricing(db, user.id)

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
                    'Cart saved for device purchase (cabinet) user + devices, discount',
                    user_id=user.id,
                    devices=request.devices,
                    devices_discount_percent=devices_discount_percent,
                )
            except Exception as e:
                logger.error('Error saving cart for device purchase (cabinet)', error=e)

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

        success = await subtract_user_balance(
            db=db,
            user=user,
            amount_kopeks=price_kopeks,
            description=description,
            create_transaction=True,
            payment_method=PaymentMethod.BALANCE,
            transaction_type=TransactionType.SUBSCRIPTION_PAYMENT,
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail='Insufficient funds',
            )

        # Re-lock subscription after subtract_user_balance committed (which released all locks).
        # Re-validate max device limit to prevent concurrent purchases exceeding the limit.
        relock_result = await db.execute(
            select(Subscription)
            .where(Subscription.id == subscription.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        subscription = relock_result.scalar_one()

        actual_current = subscription.device_limit or 1
        actual_new = actual_current + request.devices
        if max_device_limit and actual_new > max_device_limit:
            # Concurrent purchase already exceeded limit — refund balance
            user_refund = await db.execute(
                select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
            )
            refund_user = user_refund.scalar_one()
            refund_user.balance_kopeks += price_kopeks
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'Максимальное количество устройств: {max_device_limit}. Баланс возвращён.',
            )

        # Increase device limit (under lock)
        subscription.device_limit = actual_new
        await db.commit()
        await db.refresh(subscription)

        # Sync with RemnaWave
        service = SubscriptionService()
        try:
            if _resolve_panel_uuid(subscription, user):
                await service.update_remnawave_user(db, subscription)
            else:
                await service.create_remnawave_user(db, subscription)
        except Exception as e:
            logger.error('Failed to sync devices with RemnaWave', error=e)

        await db.refresh(user)

        if devices_discount_percent > 0:
            logger.info(
                'User purchased devices for kopeks (discount saved kopeks)',
                user_id=user.id,
                devices=request.devices,
                price_kopeks=price_kopeks,
                devices_discount_percent=devices_discount_percent,
                discount_value=discount_value,
            )
        else:
            logger.info(
                'User purchased devices for kopeks', user_id=user.id, devices=request.devices, price_kopeks=price_kopeks
            )

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
            logger.error('Failed to send admin notification for device purchase', error=e)

        response: dict[str, Any] = {
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
        logger.error('Failed to purchase devices for user', user_id=user.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Не удалось обработать покупку устройств',
        )


@router.post('/devices/save-cart')
async def save_devices_cart(
    request: DevicePurchaseRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, bool]:
    """Save cart for device purchase (for insufficient balance flow)."""
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

    # Get tariff for device price (if exists)
    tariff = None
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    # Determine device price and max limit from tariff or settings
    if tariff and tariff.device_price_kopeks is not None:
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

    base_total_price = int(device_price * request.devices * days_left / total_days)
    base_total_price = max(100, base_total_price)  # Minimum 1 ruble

    # Apply discount from promo group
    period_hint_days = days_left
    discount_result = _apply_addon_discount(user, 'devices', base_total_price, period_hint_days)
    price_kopeks = discount_result['discounted']
    devices_discount_percent = discount_result['percent']

    # Ensure minimum price after discount (except for 100% discount)
    if devices_discount_percent < 100 and price_kopeks > 0:
        price_kopeks = max(100, price_kopeks)

    # Save cart for auto-purchase after balance top-up
    cart_data = {
        'cart_mode': 'add_devices',
        'devices_to_add': request.devices,
        'price_kopeks': price_kopeks,
        'base_price_kopeks': base_total_price,
        'discount_percent': devices_discount_percent,
        'source': 'cabinet',
    }
    await user_cart_service.save_user_cart(user.id, cart_data)
    logger.info(
        'Cart saved for device purchase (cabinet save-cart) user + devices', user_id=user.id, devices=request.devices
    )

    return {'success': True, 'cart_saved': True}


@router.get('/devices/price')
async def get_device_price(
    devices: int = 1,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get price for additional devices."""
    subscription = await resolve_subscription(db, user, subscription_id)

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
    if tariff and tariff.device_price_kopeks is not None:
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
    now = datetime.now(UTC)
    end_date = subscription.end_date
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)

    days_left = max(1, (end_date - now).days)
    total_days = 30

    # Calculate base price before discount (total first, then floor)
    base_total_price = int(device_price * devices * days_left / total_days)
    base_total_price = max(100, base_total_price)

    # Apply discount from promo group
    period_hint_days = days_left
    discount_result = _apply_addon_discount(user, 'devices', base_total_price, period_hint_days)
    total_price_kopeks = discount_result['discounted']
    devices_discount_percent = discount_result['percent']
    discount_value = discount_result['discount']

    # Ensure minimum price after discount (except for 100% discount)
    if devices_discount_percent < 100 and total_price_kopeks > 0:
        total_price_kopeks = max(100, total_price_kopeks)
    price_per_device_kopeks = total_price_kopeks // devices if devices > 0 else 0

    response: dict[str, Any] = {
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


# ============ Device Management (list/delete) ============


@router.get('/devices')
async def get_devices(
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get list of connected devices."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    _puuid = _resolve_panel_uuid(subscription, user)
    if not _puuid:
        return {
            'devices': [],
            'total': 0,
            'device_limit': subscription.device_limit or 0,
        }

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            response = await api.get_user_devices_all(_puuid)

            devices_list = response.get('devices', [])
            formatted_devices = []
            for device in devices_list:
                hwid = device.get('hwid') or device.get('deviceId') or device.get('id')
                platform = device.get('platform') or device.get('platformType') or 'Unknown'
                model = device.get('deviceModel') or device.get('model') or device.get('name') or 'Unknown'
                user_agent = device.get('userAgent') or ''
                created_at = device.get('updatedAt') or device.get('lastSeen') or device.get('createdAt')

                formatted_devices.append(
                    {
                        'hwid': hwid,
                        'platform': platform,
                        'device_model': model,
                        'user_agent': user_agent,
                        'created_at': created_at,
                    }
                )

            return {
                'devices': formatted_devices,
                'total': response.get('total', len(formatted_devices)),
                'device_limit': subscription.device_limit or 0,
            }

    except Exception as e:
        logger.error('Error fetching devices', error=e)
        return {
            'devices': [],
            'total': 0,
            'device_limit': subscription.device_limit or 0,
        }


@router.delete('/devices/{hwid}')
async def delete_device(
    hwid: str,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Delete a specific device by HWID."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    _puuid = _resolve_panel_uuid(subscription, user)
    if not _puuid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='User UUID not found',
        )

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            delete_data = {'userUuid': _puuid, 'hwid': hwid}
            await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)

            return {
                'success': True,
                'message': 'Device deleted successfully',
                'deleted_hwid': hwid,
            }

    except Exception as e:
        logger.error('Error deleting device', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete device',
        )


@router.delete('/devices')
async def delete_all_devices(
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Delete all connected devices."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    _puuid = _resolve_panel_uuid(subscription, user)
    if not _puuid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='User UUID not found',
        )

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            # Get all devices first
            response = await api.get_user_devices_all(_puuid)

            if not response:
                return {
                    'success': True,
                    'message': 'No devices to delete',
                    'deleted_count': 0,
                }

            devices_list = response.get('devices', [])
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
                        delete_data = {'userUuid': _puuid, 'hwid': device_hwid}
                        await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
                        deleted_count += 1
                    except Exception as device_error:
                        logger.error('Error deleting device', device_hwid=device_hwid, device_error=device_error)

            return {
                'success': True,
                'message': f'Deleted {deleted_count} devices',
                'deleted_count': deleted_count,
            }

    except Exception as e:
        logger.error('Error deleting all devices', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete devices',
        )


# ============ Device Reduction ============


@router.get('/devices/reduction-info')
async def get_device_reduction_info(
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get info about device limit reduction availability."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        return {
            'available': False,
            'reason': 'No subscription found',
            'current_device_limit': 0,
            'min_device_limit': 1,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

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

    # Minimum device limit for decrease is always 1 (tariff's device_limit is the
    # number of devices included at purchase, not the floor for decrease)
    min_device_limit = 1

    current_device_limit = subscription.device_limit or 1

    # Can't reduce below minimum
    if current_device_limit <= min_device_limit:
        return {
            'available': False,
            'reason': 'Already at minimum device limit',
            'current_device_limit': current_device_limit,
            'min_device_limit': min_device_limit,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    # Get connected devices count
    connected_devices_count = 0
    _puuid = _resolve_panel_uuid(subscription, user)
    if _puuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                response = await api.get_user_devices_all(_puuid)
                if response:
                    connected_devices_count = response.get('total', 0)
        except Exception as e:
            logger.error('Error getting connected devices count', error=e)

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
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
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

    # Resolve subscription (ownership validated), then lock the row for concurrent safety
    resolved = await resolve_subscription(db, user, subscription_id)
    if not resolved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    result = await db.execute(
        select(Subscription)
        .where(and_(Subscription.id == resolved.id, Subscription.user_id == user.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if subscription.is_trial:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Device reduction is not available for trial subscriptions',
        )

    # Minimum device limit for decrease is always 1 (tariff's device_limit is the
    # number of devices included at purchase, not the floor for decrease)
    min_device_limit = 1

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
    _puuid = _resolve_panel_uuid(subscription, user)
    if _puuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                response = await api.get_user_devices_all(_puuid)
                if response:
                    devices_list = response.get('devices', [])
                    connected_devices_count = len(devices_list)

                    # If connected devices exceed new limit, remove excess (last connected)
                    if connected_devices_count > new_device_limit:
                        devices_to_remove = connected_devices_count - new_device_limit
                        logger.info(
                            'Removing excess devices for user had new limit',
                            devices_to_remove=devices_to_remove,
                            user_id=user.id,
                            connected_devices_count=connected_devices_count,
                            new_device_limit=new_device_limit,
                        )

                        # Sort by date (oldest first) and remove the last ones
                        sorted_devices = sorted(
                            devices_list,
                            key=lambda d: d.get('updatedAt') or d.get('createdAt') or '\xff',
                        )
                        devices_to_delete = sorted_devices[-devices_to_remove:]

                        for device in devices_to_delete:
                            device_hwid = device.get('hwid')
                            if device_hwid:
                                try:
                                    delete_data = {'userUuid': _puuid, 'hwid': device_hwid}
                                    await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
                                    devices_removed_count += 1
                                    logger.info('Removed device for user', device_hwid=device_hwid, user_id=user.id)
                                except Exception as del_error:
                                    logger.error('Error removing device', device_hwid=device_hwid, del_error=del_error)
        except Exception as e:
            logger.error('Error checking/removing devices', error=e)

    old_device_limit = current_device_limit
    user_id = user.id  # save before potential rollback (expires ORM objects)

    # Update subscription in memory (will be committed by update_remnawave_user on success)
    subscription.device_limit = new_device_limit
    subscription.updated_at = datetime.now(UTC)

    # Update RemnaWave — commits on success, returns None on failure
    subscription_service = SubscriptionService()
    result = await subscription_service.update_remnawave_user(db, subscription)

    if result is None:
        # RemnaWave update failed — rollback local changes
        await db.rollback()
        logger.error(
            'Failed to update RemnaWave after device limit reduction',
            user_id=user_id,
            old_device_limit=old_device_limit,
            new_device_limit=new_device_limit,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Не удалось обновить VPN-панель. Попробуйте позже.',
        )

    logger.info(
        'User reduced device limit',
        user_id=user_id,
        old_device_limit=old_device_limit,
        new_device_limit=new_device_limit,
        devices_removed=devices_removed_count if devices_removed_count > 0 else None,
    )

    return {
        'success': True,
        'message': 'Device limit reduced successfully'
        + (f' ({devices_removed_count} devices removed)' if devices_removed_count > 0 else ''),
        'old_device_limit': old_device_limit,
        'new_device_limit': new_device_limit,
        'devices_removed': devices_removed_count,
    }
