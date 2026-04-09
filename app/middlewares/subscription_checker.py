from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.database.models import SubscriptionStatus


logger = structlog.get_logger(__name__)

# Буфер времени перед деактивацией (защита от race condition при продлении)
EXPIRATION_BUFFER_MINUTES = 5


class SubscriptionStatusMiddleware(BaseMiddleware):
    """
    Проверяет статус подписки пользователя.
    ВАЖНО: Использует db и db_user из data, которые уже загружены в AuthMiddleware.
    Не создаёт дополнительных сессий БД.

    Деактивирует подписку только если она истекла более чем на EXPIRATION_BUFFER_MINUTES минут.
    Это защищает от race conditions при продлении подписки.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Используем db и user из AuthMiddleware - не создаём новую сессию!
        db = data.get('db')
        user = data.get('db_user')

        if db and user and getattr(user, 'subscriptions', None):
            try:
                current_time = datetime.now(UTC)
                needs_commit = False

                # Check all subscriptions (multi-tariff aware)
                for subscription in user.subscriptions:
                    # Суточные подписки управляются DailySubscriptionService — не экспайрим их тут
                    tariff = getattr(subscription, 'tariff', None)
                    is_active_daily = tariff and getattr(tariff, 'is_daily', False) and not subscription.is_daily_paused

                    if (
                        subscription.status == SubscriptionStatus.ACTIVE.value
                        and subscription.end_date
                        and subscription.end_date <= current_time
                        and not is_active_daily
                    ):
                        time_since_expiry = current_time - subscription.end_date

                        if time_since_expiry > timedelta(minutes=EXPIRATION_BUFFER_MINUTES):
                            subscription.status = SubscriptionStatus.EXPIRED.value
                            subscription.updated_at = current_time
                            needs_commit = True

                            logger.warning(
                                '⏰ Middleware DEACTIVATION: подписка (user_id=) деактивирована. end_date=, просрочена на',
                                subscription_id=subscription.id,
                                user_id=user.id,
                                end_date=subscription.end_date,
                                time_since_expiry=time_since_expiry,
                            )
                        else:
                            logger.debug(
                                '⏰ Middleware: подписка пользователя истекла недавно ждём буфер мин',
                                user_id=user.id,
                                time_since_expiry=time_since_expiry,
                                EXPIRATION_BUFFER_MINUTES=EXPIRATION_BUFFER_MINUTES,
                            )

                if needs_commit:
                    await db.commit()

            except Exception as e:
                logger.error('Ошибка проверки статуса подписки', error=e)

        return await handler(event, data)
