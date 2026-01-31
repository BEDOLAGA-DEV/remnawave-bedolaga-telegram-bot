"""
Обработчик входящих webhooks от RemnaWave.

Принимает события в реальном времени вместо cron-синхронизации.

Поддерживаемые события:
- user.traffic_limit_reached - лимит трафика исчерпан
- user.expired - подписка истекла
- user.status_changed - изменение статуса пользователя
- user.updated - обновление данных пользователя
- user.created - создание пользователя в панели
- user.deleted - удаление пользователя

Настройка:
1. В RemnaWave указать URL: https://your-bot.com/api/webhooks/remnawave
2. Указать REMNAWAVE_WEBHOOK_SECRET в .env
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.database.uow import UnitOfWork
from app.localization.texts import get_texts


logger = logging.getLogger(__name__)


def _get_bot() -> Bot:
    """Создать экземпляр бота для отправки уведомлений из webhook."""
    return Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

router = APIRouter(prefix='/webhooks/remnawave', tags=['RemnaWave Webhooks'])


# =============================================================================
# Типы событий
# =============================================================================


class RemnaWaveEventType(str, Enum):
    """Типы событий от RemnaWave."""

    USER_CREATED = 'user.created'
    USER_UPDATED = 'user.updated'
    USER_DELETED = 'user.deleted'
    USER_STATUS_CHANGED = 'user.status_changed'
    USER_EXPIRED = 'user.expired'
    USER_TRAFFIC_LIMIT = 'user.traffic_limit_reached'
    USER_TRAFFIC_RESET = 'user.traffic_reset'


# =============================================================================
# Схемы данных
# =============================================================================


class RemnaWaveUserPayload(BaseModel):
    """Данные пользователя в webhook payload."""

    uuid: str
    username: str
    telegram_id: int | None = None
    status: str  # ACTIVE, DISABLED, LIMITED, EXPIRED
    traffic_limit_bytes: int | None = None
    used_traffic_bytes: int | None = None
    expire_at: datetime | None = None
    # Дополнительные поля
    email: str | None = None
    tag: str | None = None
    hwid_device_limit: int | None = None


class RemnaWaveWebhookPayload(BaseModel):
    """Payload входящего webhook от RemnaWave."""

    event: str
    timestamp: datetime
    data: dict[str, Any]
    # Опционально - для версионирования API
    version: str | None = None


@dataclass
class WebhookResult:
    """Результат обработки webhook."""

    success: bool
    event: str
    message: str
    processed_at: datetime


# =============================================================================
# Верификация подписи
# =============================================================================


def verify_webhook_signature(
    payload: bytes,
    signature: str | None,
    secret: str,
) -> bool:
    """
    Проверить подпись webhook.

    RemnaWave подписывает payload с помощью HMAC-SHA256.
    """
    if not signature:
        logger.warning('RemnaWave webhook: отсутствует подпись')
        return False

    # Формат: sha256=<hex>
    if signature.startswith('sha256='):
        signature = signature[7:]

    expected = hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


# =============================================================================
# Обработчики событий
# =============================================================================


class RemnaWaveWebhookHandler:
    """Обработчик событий RemnaWave."""

    async def handle_event(
        self,
        event_type: str,
        payload: RemnaWaveWebhookPayload,
    ) -> WebhookResult:
        """Роутинг события к соответствующему обработчику."""

        handlers = {
            RemnaWaveEventType.USER_TRAFFIC_LIMIT.value: self._handle_traffic_limit,
            RemnaWaveEventType.USER_EXPIRED.value: self._handle_user_expired,
            RemnaWaveEventType.USER_STATUS_CHANGED.value: self._handle_status_changed,
            RemnaWaveEventType.USER_UPDATED.value: self._handle_user_updated,
            RemnaWaveEventType.USER_CREATED.value: self._handle_user_created,
            RemnaWaveEventType.USER_DELETED.value: self._handle_user_deleted,
            RemnaWaveEventType.USER_TRAFFIC_RESET.value: self._handle_traffic_reset,
        }

        handler = handlers.get(event_type)
        if not handler:
            logger.warning('RemnaWave: неизвестный тип события %s', event_type)
            return WebhookResult(
                success=False,
                event=event_type,
                message=f'Unknown event type: {event_type}',
                processed_at=datetime.utcnow(),
            )

        try:
            message = await handler(payload.data)
            return WebhookResult(
                success=True,
                event=event_type,
                message=message,
                processed_at=datetime.utcnow(),
            )
        except Exception as e:
            logger.exception('RemnaWave webhook error: %s', e)
            return WebhookResult(
                success=False,
                event=event_type,
                message=str(e),
                processed_at=datetime.utcnow(),
            )

    async def _handle_traffic_limit(self, data: dict[str, Any]) -> str:
        """
        Обработка события: лимит трафика исчерпан.

        Действия:
        1. Обновить статус подписки
        2. Отправить уведомление пользователю
        """
        telegram_id = data.get('telegram_id')
        uuid = data.get('uuid')

        if not telegram_id:
            return f'Skipped: no telegram_id for user {uuid}'

        user = None
        subscription = None

        async with UnitOfWork() as uow:
            user = await uow.users.get_by_telegram_id(telegram_id)
            if not user:
                return f'User not found: telegram_id={telegram_id}'

            subscription = await uow.subscriptions.get_by_user_id(user.id)
            if subscription:
                # Обновляем трафик
                used_bytes = data.get('used_traffic_bytes', 0)
                await uow.subscriptions.update_traffic(subscription, used_bytes)
                await uow.commit()

        # Отправляем уведомление пользователю
        if user and telegram_id:
            await self._send_traffic_limit_notification(telegram_id, user, subscription)

        logger.info(
            'RemnaWave: лимит трафика для пользователя %s (uuid=%s)',
            telegram_id,
            uuid,
        )

        return f'Traffic limit handled for user {telegram_id}'

    async def _handle_user_expired(self, data: dict[str, Any]) -> str:
        """
        Обработка события: подписка истекла.

        Действия:
        1. Обновить статус подписки на expired
        2. Отправить уведомление с предложением продлить
        """
        telegram_id = data.get('telegram_id')
        uuid = data.get('uuid')

        if not telegram_id:
            return f'Skipped: no telegram_id for user {uuid}'

        user = None

        async with UnitOfWork() as uow:
            user = await uow.users.get_by_telegram_id(telegram_id)
            if not user:
                return f'User not found: telegram_id={telegram_id}'

            subscription = await uow.subscriptions.get_by_user_id(user.id)
            if subscription:
                from app.database.models import SubscriptionStatus
                await uow.subscriptions.set_status(
                    subscription,
                    SubscriptionStatus.EXPIRED,
                )
                await uow.commit()

        # Отправляем уведомление пользователю
        if user and telegram_id:
            await self._send_expired_notification(telegram_id, user)

        logger.info(
            'RemnaWave: подписка истекла для пользователя %s (uuid=%s)',
            telegram_id,
            uuid,
        )

        return f'User expired handled for {telegram_id}'

    async def _handle_status_changed(self, data: dict[str, Any]) -> str:
        """Обработка изменения статуса пользователя."""
        telegram_id = data.get('telegram_id')
        new_status = data.get('status')
        uuid = data.get('uuid')

        logger.info(
            'RemnaWave: статус изменён на %s для пользователя %s (uuid=%s)',
            new_status,
            telegram_id,
            uuid,
        )

        # Маппинг статусов RemnaWave -> наши статусы
        # ACTIVE, DISABLED, LIMITED, EXPIRED

        return f'Status changed to {new_status} for {telegram_id}'

    async def _handle_user_updated(self, data: dict[str, Any]) -> str:
        """Обработка обновления данных пользователя."""
        telegram_id = data.get('telegram_id')
        uuid = data.get('uuid')

        async with UnitOfWork() as uow:
            user = await uow.users.get_by_telegram_id(telegram_id) if telegram_id else None
            if not user:
                return f'User not found for update: telegram_id={telegram_id}, uuid={uuid}'

            subscription = await uow.subscriptions.get_by_user_id(user.id)
            if subscription:
                # Обновляем данные из RemnaWave
                if 'used_traffic_bytes' in data:
                    await uow.subscriptions.update_traffic(
                        subscription,
                        data['used_traffic_bytes'],
                    )

                if 'expire_at' in data and data['expire_at']:
                    subscription.end_date = data['expire_at']

                if 'traffic_limit_bytes' in data:
                    limit_gb = data['traffic_limit_bytes'] / (1024 ** 3)
                    subscription.traffic_limit_gb = limit_gb

                await uow.commit()

        logger.debug('RemnaWave: user updated %s', telegram_id)
        return f'User updated: {telegram_id}'

    async def _handle_user_created(self, data: dict[str, Any]) -> str:
        """Обработка создания пользователя в панели."""
        telegram_id = data.get('telegram_id')
        uuid = data.get('uuid')

        logger.info(
            'RemnaWave: пользователь создан в панели telegram_id=%s, uuid=%s',
            telegram_id,
            uuid,
        )

        return f'User created in panel: uuid={uuid}'

    async def _handle_user_deleted(self, data: dict[str, Any]) -> str:
        """Обработка удаления пользователя из панели."""
        telegram_id = data.get('telegram_id')
        uuid = data.get('uuid')

        logger.warning(
            'RemnaWave: пользователь удалён из панели telegram_id=%s, uuid=%s',
            telegram_id,
            uuid,
        )

        return f'User deleted from panel: uuid={uuid}'

    async def _handle_traffic_reset(self, data: dict[str, Any]) -> str:
        """Обработка сброса трафика."""
        telegram_id = data.get('telegram_id')
        uuid = data.get('uuid')

        async with UnitOfWork() as uow:
            user = await uow.users.get_by_telegram_id(telegram_id) if telegram_id else None
            if user:
                subscription = await uow.subscriptions.get_by_user_id(user.id)
                if subscription:
                    await uow.subscriptions.update_traffic(subscription, 0)
                    await uow.commit()

        logger.info('RemnaWave: трафик сброшен для %s', telegram_id)
        return f'Traffic reset for {telegram_id}'

    async def _send_expired_notification(self, telegram_id: int, user) -> None:
        """Отправляет уведомление об истечении подписки."""
        try:
            bot = _get_bot()
            texts = get_texts(getattr(user, 'language', 'ru'))

            message = texts.t(
                'SUBSCRIPTION_EXPIRED',
                '\n❌ <b>Подписка истекла</b>\n\nВаша подписка истекла. Для восстановления доступа продлите подписку.\n',
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('RENEW_SUBSCRIPTION_BUTTON', '🔄 Продлить подписку'),
                            callback_data='menu_subscription',
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=texts.t('BUY_SUBSCRIPTION_BUTTON', '💎 Купить подписку'),
                            callback_data='menu_buy',
                        )
                    ],
                ]
            )

            await bot.send_message(
                chat_id=telegram_id,
                text=message,
                reply_markup=keyboard,
            )

            logger.info('RemnaWave: уведомление об истечении подписки отправлено пользователю %s', telegram_id)

        except Exception as e:
            logger.warning('RemnaWave: не удалось отправить уведомление об истечении: %s', e)

    async def _send_traffic_limit_notification(self, telegram_id: int, user, subscription) -> None:
        """Отправляет уведомление об исчерпании трафика."""
        try:
            bot = _get_bot()
            texts = get_texts(getattr(user, 'language', 'ru'))

            # Получаем информацию о лимите трафика
            traffic_limit_gb = getattr(subscription, 'traffic_limit_gb', 0) if subscription else 0

            message = texts.t(
                'TRAFFIC_LIMIT_REACHED_NOTIFICATION',
                '⚠️ <b>Лимит трафика исчерпан</b>\n\n'
                'Вы использовали весь доступный трафик ({limit} ГБ).\n\n'
                'Докупите трафик или дождитесь обновления подписки.',
            ).format(limit=traffic_limit_gb)

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('BUY_TRAFFIC_BUTTON', '📦 Докупить трафик'),
                            callback_data='buy_traffic',
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка'),
                            callback_data='menu_subscription',
                        )
                    ],
                ]
            )

            await bot.send_message(
                chat_id=telegram_id,
                text=message,
                reply_markup=keyboard,
            )

            logger.info('RemnaWave: уведомление о лимите трафика отправлено пользователю %s', telegram_id)

        except Exception as e:
            logger.warning('RemnaWave: не удалось отправить уведомление о лимите трафика: %s', e)


# Синглтон обработчика
webhook_handler = RemnaWaveWebhookHandler()


# =============================================================================
# FastAPI endpoint
# =============================================================================


@router.post('')
@router.post('/')
async def receive_webhook(
    request: Request,
    x_webhook_signature: str | None = Header(None, alias='X-Webhook-Signature'),
    x_remnawave_signature: str | None = Header(None, alias='X-RemnaWave-Signature'),
):
    """
    Endpoint для приёма webhooks от RemnaWave.

    Headers:
        X-Webhook-Signature или X-RemnaWave-Signature: sha256=<hex>

    Body:
        {
            "event": "user.traffic_limit_reached",
            "timestamp": "2024-01-15T10:30:00Z",
            "data": {
                "uuid": "...",
                "telegram_id": 123456789,
                "status": "LIMITED",
                ...
            }
        }
    """
    # Читаем raw body для верификации подписи
    body = await request.body()

    # Проверяем подпись
    secret = getattr(settings, 'REMNAWAVE_WEBHOOK_SECRET', None)
    signature = x_remnawave_signature or x_webhook_signature

    if secret:
        if not verify_webhook_signature(body, signature, secret):
            logger.warning('RemnaWave webhook: неверная подпись')
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail='Invalid webhook signature',
            )
    else:
        logger.debug('RemnaWave webhook: подпись не настроена (REMNAWAVE_WEBHOOK_SECRET)')

    # Парсим payload
    try:
        import json
        data = json.loads(body)
        payload = RemnaWaveWebhookPayload(**data)
    except Exception as e:
        logger.error('RemnaWave webhook: ошибка парсинга: %s', e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid payload: {e}',
        )

    # Обрабатываем событие
    result = await webhook_handler.handle_event(payload.event, payload)

    logger.info(
        'RemnaWave webhook: %s - %s - %s',
        payload.event,
        'OK' if result.success else 'FAILED',
        result.message,
    )

    return {
        'success': result.success,
        'event': result.event,
        'message': result.message,
        'processed_at': result.processed_at.isoformat(),
    }


@router.get('/health')
async def webhook_health():
    """Проверка доступности webhook endpoint."""
    return {
        'status': 'ok',
        'service': 'remnawave-webhooks',
        'events_supported': [e.value for e in RemnaWaveEventType],
    }
