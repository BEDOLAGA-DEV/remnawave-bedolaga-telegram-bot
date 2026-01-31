import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis
from aiogram import BaseMiddleware, Bot, types
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.config import settings
from app.database.crud.campaign import get_campaign_by_start_parameter
from app.database.crud.subscription import deactivate_subscription, reactivate_subscription
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal
from app.database.models import SubscriptionStatus, UserStatus
from app.keyboards.inline import get_channel_sub_keyboard
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.subscription_service import SubscriptionService
from app.utils.check_reg_process import is_registration_process


logger = logging.getLogger(__name__)

# Ключ для хранения pending_start_payload в Redis (резервный механизм)
REDIS_PAYLOAD_KEY_PREFIX = 'pending_start_payload:'
REDIS_PAYLOAD_TTL = 3600  # 1 час


async def save_pending_payload_to_redis(telegram_id: int, payload: str) -> bool:
    """Сохраняет pending_start_payload в Redis напрямую (резервный механизм)."""
    try:
        redis_client = aioredis.from_url(settings.REDIS_URL)
        key = f'{REDIS_PAYLOAD_KEY_PREFIX}{telegram_id}'
        await redis_client.set(key, payload, ex=REDIS_PAYLOAD_TTL)
        await redis_client.aclose()
        logger.info(
            "💾 [Redis fallback] Сохранен payload '%s' для пользователя %s",
            payload,
            telegram_id,
        )
        return True
    except Exception as e:
        logger.error(
            '❌ [Redis fallback] Ошибка сохранения payload для %s: %s',
            telegram_id,
            e,
        )
        return False


async def get_pending_payload_from_redis(telegram_id: int) -> str | None:
    """Получает pending_start_payload из Redis (резервный механизм)."""
    try:
        redis_client = aioredis.from_url(settings.REDIS_URL)
        key = f'{REDIS_PAYLOAD_KEY_PREFIX}{telegram_id}'
        payload = await redis_client.get(key)
        await redis_client.aclose()
        if payload:
            return payload.decode('utf-8') if isinstance(payload, bytes) else payload
        return None
    except Exception as e:
        logger.debug('❌ [Redis fallback] Ошибка получения payload для %s: %s', telegram_id, e)
        return None


async def delete_pending_payload_from_redis(telegram_id: int) -> None:
    """Удаляет pending_start_payload из Redis."""
    try:
        redis_client = aioredis.from_url(settings.REDIS_URL)
        key = f'{REDIS_PAYLOAD_KEY_PREFIX}{telegram_id}'
        await redis_client.delete(key)
        await redis_client.aclose()
    except Exception:
        pass


class ChannelCheckerMiddleware(BaseMiddleware):
    """
    Middleware для проверки подписки на канал.
    ОПТИМИЗИРОВАНО: создаёт максимум одну сессию БД на запрос.
    """

    def __init__(self):
        self.BAD_MEMBER_STATUS = (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED)
        self.GOOD_MEMBER_STATUS = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        logger.info('🔧 ChannelCheckerMiddleware инициализирован')

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_id = None
        if isinstance(event, (Message, CallbackQuery)):
            telegram_id = event.from_user.id
        elif isinstance(event, Update):
            if event.message:
                telegram_id = event.message.from_user.id
            elif event.callback_query:
                telegram_id = event.callback_query.from_user.id

        if telegram_id is None:
            logger.debug('❌ telegram_id не найден, пропускаем')
            return await handler(event, data)

        # Админам разрешаем пропускать проверку подписки
        if settings.is_admin(telegram_id):
            logger.debug(
                '✅ Пользователь %s является администратором — пропускаем проверку подписки',
                telegram_id,
            )
            return await handler(event, data)

        state: FSMContext = data.get('state')
        current_state = None

        if state:
            current_state = await state.get_state()

        is_reg_process = is_registration_process(event, current_state)

        if is_reg_process:
            logger.debug('✅ Событие разрешено (процесс регистрации), пропускаем проверку')
            return await handler(event, data)

        bot: Bot = data['bot']

        channel_id = settings.CHANNEL_SUB_ID

        if not channel_id:
            logger.warning('⚠️ CHANNEL_SUB_ID не установлен, пропускаем проверку')
            return await handler(event, data)

        is_required = settings.CHANNEL_IS_REQUIRED_SUB

        if not is_required:
            logger.debug('⚠️ Обязательная подписка отключена, пропускаем проверку')
            return await handler(event, data)

        channel_link = self._normalize_channel_link(settings.CHANNEL_LINK, channel_id)

        if not channel_link:
            logger.warning('⚠️ CHANNEL_LINK не задан или невалиден, кнопка подписки будет скрыта')

        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)

            if member.status in self.GOOD_MEMBER_STATUS:
                # Реактивируем подписку если была отключена из-за отписки от канала
                if telegram_id and (settings.CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE or settings.CHANNEL_REQUIRED_FOR_ALL):
                    await self._reactivate_subscription_on_subscribe(telegram_id, bot)
                return await handler(event, data)
            if member.status in self.BAD_MEMBER_STATUS:
                logger.info(f'❌ Пользователь {telegram_id} не подписан на канал (статус: {member.status})')

                if telegram_id and (settings.CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE or settings.CHANNEL_REQUIRED_FOR_ALL):
                    await self._deactivate_subscription_on_unsubscribe(telegram_id, bot, channel_link)

                await self._capture_start_payload(state, event, bot)

                if isinstance(event, CallbackQuery) and event.data == 'sub_channel_check':
                    await event.answer(
                        '❌ Вы еще не подписались на канал! Подпишитесь и попробуйте снова.', show_alert=True
                    )
                    return None

                return await self._deny_message(event, bot, channel_link, channel_id)
            logger.warning(f'⚠️ Неожиданный статус пользователя {telegram_id}: {member.status}')
            await self._capture_start_payload(state, event, bot)
            return await self._deny_message(event, bot, channel_link, channel_id)

        except TelegramForbiddenError as e:
            logger.error(f'❌ Бот заблокирован в канале {channel_id}: {e}')
            await self._capture_start_payload(state, event, bot)
            return await self._deny_message(event, bot, channel_link, channel_id)
        except TelegramBadRequest as e:
            if 'chat not found' in str(e).lower():
                logger.error(f'❌ Канал {channel_id} не найден: {e}')
            elif 'user not found' in str(e).lower():
                logger.error(f'❌ Пользователь {telegram_id} не найден: {e}')
            else:
                logger.error(f'❌ Ошибка запроса к каналу {channel_id}: {e}')
            await self._capture_start_payload(state, event, bot)
            return await self._deny_message(event, bot, channel_link, channel_id)
        except Exception as e:
            logger.error(f'❌ Неожиданная ошибка при проверке подписки: {e}')
            return await handler(event, data)

    @staticmethod
    def _normalize_channel_link(channel_link: str | None, channel_id: str | None) -> str | None:
        link = (channel_link or '').strip()

        if link.startswith('@'):  # raw username
            return f'https://t.me/{link.lstrip("@")}'

        if link and not link.lower().startswith(('http://', 'https://', 'tg://')):
            return f'https://{link}'

        if link:
            return link

        if channel_id and str(channel_id).startswith('@'):
            return f'https://t.me/{str(channel_id).lstrip("@")}'

        return None

    async def _capture_start_payload(
        self,
        state: FSMContext | None,
        event: TelegramObject,
        bot: Bot | None = None,
    ) -> None:
        telegram_id = None
        if isinstance(event, (Message, CallbackQuery)):
            telegram_id = event.from_user.id if event.from_user else None

        message: Message | None = None
        if isinstance(event, Message):
            message = event
        elif isinstance(event, (CallbackQuery, Update)):
            message = event.message

        if not message or not message.text:
            return

        text = message.text.strip()
        if not text.startswith('/start'):
            return

        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1]:
            return

        payload = parts[1]

        # Сохраняем в FSM state
        if state:
            state_data = await state.get_data() or {}
            if state_data.get('pending_start_payload') != payload:
                state_data['pending_start_payload'] = payload
                await state.set_data(state_data)
                logger.info(
                    "💾 Сохранен start payload '%s' для пользователя %s (FSM)",
                    payload,
                    telegram_id,
                )
        else:
            logger.warning(
                '⚠️ _capture_start_payload: state=None для пользователя %s',
                telegram_id,
            )

        # Также сохраняем в Redis как резерв (на случай потери FSM state)
        if telegram_id:
            await save_pending_payload_to_redis(telegram_id, payload)

        if bot and message.from_user:
            await self._try_send_campaign_visit_notification(
                bot,
                message.from_user,
                state,
                payload,
            )

    async def _try_send_campaign_visit_notification(
        self,
        bot: Bot,
        telegram_user: types.User,
        state: FSMContext,
        payload: str,
    ) -> None:
        try:
            state_data = await state.get_data() or {}
        except Exception as error:
            logger.error(
                '❌ Не удалось получить данные состояния для уведомления по кампании %s: %s',
                payload,
                error,
            )
            return

        if state_data.get('campaign_notification_sent'):
            return

        async with AsyncSessionLocal() as db:
            try:
                campaign = await get_campaign_by_start_parameter(
                    db,
                    payload,
                    only_active=True,
                )
                if not campaign:
                    return

                user = await get_user_by_telegram_id(db, telegram_user.id)

                notification_service = AdminNotificationService(bot)
                sent = await notification_service.send_campaign_link_visit_notification(
                    db,
                    telegram_user,
                    campaign,
                    user,
                )
                if sent:
                    await state.update_data(campaign_notification_sent=True)
                await db.commit()
            except Exception as error:
                logger.error(
                    '❌ Ошибка отправки уведомления о переходе по кампании %s: %s',
                    payload,
                    error,
                )
                await db.rollback()

    async def _deactivate_subscription_on_unsubscribe(
        self, telegram_id: int, bot: Bot, channel_link: str | None
    ) -> None:
        """Деактивация подписки при отписке от канала."""
        if not settings.CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE and not settings.CHANNEL_REQUIRED_FOR_ALL:
            return

        async with AsyncSessionLocal() as db:
            try:
                user = await get_user_by_telegram_id(db, telegram_id)
                if not user or not user.subscription:
                    return

                subscription = user.subscription

                if subscription.status != SubscriptionStatus.ACTIVE.value:
                    return

                if settings.CHANNEL_REQUIRED_FOR_ALL:
                    pass
                elif not subscription.is_trial:
                    return

                await deactivate_subscription(db, subscription)
                sub_type = 'Триальная' if subscription.is_trial else 'Платная'
                logger.info(
                    '🚫 %s подписка пользователя %s отключена после отписки от канала',
                    sub_type,
                    telegram_id,
                )

                if user.remnawave_uuid:
                    service = SubscriptionService()
                    try:
                        await service.disable_remnawave_user(user.remnawave_uuid)
                    except Exception as api_error:
                        logger.error(
                            '❌ Не удалось отключить пользователя RemnaWave %s: %s',
                            user.remnawave_uuid,
                            api_error,
                        )

                # Уведомляем пользователя о деактивации
                try:
                    texts = get_texts(user.language if user.language else DEFAULT_LANGUAGE)
                    notification_text = texts.t(
                        'SUBSCRIPTION_DEACTIVATED_CHANNEL_UNSUBSCRIBE',
                        '🚫 Ваша подписка приостановлена, так как вы отписались от канала.\n\n'
                        'Подпишитесь на канал снова, чтобы восстановить доступ к VPN.',
                    )
                    channel_kb = get_channel_sub_keyboard(channel_link, language=user.language)
                    await bot.send_message(telegram_id, notification_text, reply_markup=channel_kb)
                except Exception as notify_error:
                    logger.error(
                        '❌ Не удалось отправить уведомление о деактивации пользователю %s: %s',
                        telegram_id,
                        notify_error,
                    )
                await db.commit()
            except Exception as db_error:
                logger.error(
                    '❌ Ошибка деактивации подписки пользователя %s после отписки: %s',
                    telegram_id,
                    db_error,
                )
                await db.rollback()

    async def _reactivate_subscription_on_subscribe(self, telegram_id: int, bot: Bot) -> None:
        """Реактивация подписки после повторной подписки на канал."""
        if not settings.CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE and not settings.CHANNEL_REQUIRED_FOR_ALL:
            return

        async with AsyncSessionLocal() as db:
            try:
                user = await get_user_by_telegram_id(db, telegram_id)
                if not user or not user.subscription:
                    return

                # НЕ реактивируем подписку заблокированным пользователям
                if user.status == UserStatus.BLOCKED.value:
                    logger.info(
                        '🚫 Пропуск реактивации для заблокированного пользователя %s',
                        telegram_id,
                    )
                    return

                subscription = user.subscription

                # Реактивируем только DISABLED подписки
                if subscription.status != SubscriptionStatus.DISABLED.value:
                    return

                # Проверяем что подписка ещё не истекла
                if subscription.end_date and subscription.end_date <= datetime.utcnow():
                    return

                # Реактивируем в БД
                await reactivate_subscription(db, subscription)
                sub_type = 'Триальная' if subscription.is_trial else 'Платная'
                logger.info(
                    '✅ %s подписка пользователя %s реактивирована после подписки на канал',
                    sub_type,
                    telegram_id,
                )

                # Включаем в RemnaWave
                if user.remnawave_uuid:
                    service = SubscriptionService()
                    try:
                        await service.enable_remnawave_user(user.remnawave_uuid)
                    except Exception as api_error:
                        logger.error(
                            '❌ Не удалось включить пользователя RemnaWave %s: %s',
                            user.remnawave_uuid,
                            api_error,
                        )

                # Уведомляем пользователя о реактивации
                try:
                    texts = get_texts(user.language if user.language else DEFAULT_LANGUAGE)
                    notification_text = texts.t(
                        'SUBSCRIPTION_REACTIVATED_CHANNEL_SUBSCRIBE',
                        '✅ Ваша подписка восстановлена!\n\nСпасибо, что подписались на канал. VPN снова работает.',
                    )
                    await bot.send_message(telegram_id, notification_text)
                except Exception as notify_error:
                    logger.warning(
                        'Не удалось отправить уведомление о реактивации пользователю %s: %s',
                        telegram_id,
                        notify_error,
                    )
                await db.commit()
            except Exception as db_error:
                logger.error(
                    '❌ Ошибка реактивации подписки пользователя %s: %s',
                    telegram_id,
                    db_error,
                )
                await db.rollback()

    @staticmethod
    async def _deny_message(
        event: TelegramObject,
        bot: Bot,
        channel_link: str | None,
        channel_id: str | None,
    ):
        logger.debug('🚫 Отправляем сообщение о необходимости подписки')

        user = None
        if isinstance(event, (Message, CallbackQuery)):
            user = getattr(event, 'from_user', None)
        elif isinstance(event, Update):
            if event.message and event.message.from_user:
                user = event.message.from_user
            elif event.callback_query and event.callback_query.from_user:
                user = event.callback_query.from_user

        language = DEFAULT_LANGUAGE
        if user and user.language_code:
            language = user.language_code.split('-')[0]

        texts = get_texts(language)
        channel_sub_kb = get_channel_sub_keyboard(channel_link, language=language)
        text = texts.t(
            'CHANNEL_REQUIRED_TEXT',
            '🔒 Для использования бота подпишитесь на новостной канал, чтобы получать уведомления о новых возможностях и обновлениях бота. Спасибо!',
        )

        if not channel_link and channel_id:
            channel_hint = None

            if str(channel_id).startswith('@'):  # username-based channel id
                channel_hint = f'@{str(channel_id).lstrip("@")}'

            if channel_hint:
                text = f'{text}\n\n{channel_hint}'

        try:
            if isinstance(event, Message):
                return await event.answer(text, reply_markup=channel_sub_kb)
            if isinstance(event, CallbackQuery):
                try:
                    return await event.message.edit_text(text, reply_markup=channel_sub_kb)
                except TelegramBadRequest as e:
                    if 'message is not modified' in str(e).lower():
                        logger.debug('ℹ️ Сообщение уже содержит текст проверки подписки, пропускаем редактирование')
                        return await event.answer(text, show_alert=True)
                    raise
            elif isinstance(event, Update) and event.message:
                return await bot.send_message(event.message.chat.id, text, reply_markup=channel_sub_kb)
        except Exception as e:
            logger.error(f'❌ Ошибка при отправке сообщения о подписке: {e}')
