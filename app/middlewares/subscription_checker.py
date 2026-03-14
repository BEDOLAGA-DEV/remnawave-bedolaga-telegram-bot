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

        if db and user and user.subscription:
            try:
                current_time = datetime.now(UTC)
                subscription = user.subscription

                # Суточные подписки управляются DailySubscriptionService — не экспайрим их тут
                tariff = getattr(subscription, 'tariff', None)
                is_active_daily = tariff and getattr(tariff, 'is_daily', False) and not subscription.is_daily_paused

                if (
                    subscription.status == SubscriptionStatus.ACTIVE.value
                    and subscription.end_date
                    and subscription.end_date <= current_time
                    and not is_active_daily
                ):
                    # Вычисляем насколько давно истекла подписка
                    time_since_expiry = current_time - subscription.end_date

                    # Деактивируем только если прошло больше буфера (защита от race condition)
                    if time_since_expiry > timedelta(minutes=EXPIRATION_BUFFER_MINUTES):
                        subscription.status = SubscriptionStatus.EXPIRED.value
                        subscription.updated_at = current_time
                        await db.commit()

                        logger.warning(
                            '⏰ Middleware DEACTIVATION: подписка (user_id=) деактивирована. end_date=, просрочена на',
                            subscription_id=subscription.id,
                            user_id=user.id,
                            end_date=subscription.end_date,
                            time_since_expiry=time_since_expiry,
                        )
                    else:
                        # Подписка только что истекла - не деактивируем сразу (может быть продление)
                        logger.debug(
                            '⏰ Middleware: подписка пользователя истекла недавно ждём буфер мин',
                            user_id=user.id,
                            time_since_expiry=time_since_expiry,
                            EXPIRATION_BUFFER_MINUTES=EXPIRATION_BUFFER_MINUTES,
                        )

            except Exception as e:
                logger.error('Ошибка проверки статуса подписки', error=e)

        return await handler(event, data)
