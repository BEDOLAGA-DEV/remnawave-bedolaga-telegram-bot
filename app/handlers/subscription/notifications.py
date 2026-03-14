from datetime import datetime

from aiogram import types
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subscription, User
from app.services.admin_notification_service import AdminNotificationService

from .common import logger


async def send_trial_notification(
    callback: types.CallbackQuery, db: AsyncSession, db_user: User, subscription: Subscription
):
    try:
        notification_service = AdminNotificationService(callback.bot)
        await notification_service.send_trial_activation_notification(db, db_user, subscription)
    except Exception as e:
        logger.error('Ошибка отправки уведомления о триале', error=e)


async def send_purchase_notification(
    callback: types.CallbackQuery,
    db: AsyncSession,
    db_user: User,
    subscription: Subscription,
    transaction_id: int,
    period_days: int,
    was_trial_conversion: bool = False,
    purchase_type: str | None = None,
):
    try:
        from app.database.crud.transaction import get_transaction_by_id

        transaction = await get_transaction_by_id(db, transaction_id)
        if transaction:
            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_purchase_notification(
                db,
                db_user,
                subscription,
                transaction,
                period_days,
                was_trial_conversion,
                purchase_type=purchase_type,
            )
    except Exception as e:
        logger.error('Ошибка отправки уведомления о покупке', error=e)


async def send_extension_notification(
    callback: types.CallbackQuery,
    db: AsyncSession,
    db_user: User,
    subscription: Subscription,
    transaction_id: int,
    extended_days: int,
    old_end_date: datetime,
):
    try:
        from app.database.crud.transaction import get_transaction_by_id

        transaction = await get_transaction_by_id(db, transaction_id)
        if transaction:
            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_extension_notification(
                db, db_user, subscription, transaction, extended_days, old_end_date
            )
    except Exception as e:
        logger.error('Ошибка отправки уведомления о продлении', error=e)
