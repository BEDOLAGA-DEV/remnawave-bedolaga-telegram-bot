"""
Сервис для отправки уведомлений от ban системы пользователям
"""

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service,
)
from app.services.remnawave_service import remnawave_service


logger = structlog.get_logger(__name__)


def _get_user_texts(user: User | None = None):
    language = getattr(user, 'language', None)
    return get_texts(language or 'ru')


def get_delete_keyboard(texts=None) -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой удаления уведомления"""
    texts = texts or _get_user_texts()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('DELETE_MESSAGE', '🗑 Удалить'), callback_data='ban_notify:delete')]
        ]
    )


class BanNotificationService:
    """Сервис для отправки уведомлений о банах пользователям"""

    def __init__(self):
        self._bot: Bot | None = None

    def set_bot(self, bot: Bot):
        """Установить инстанс бота для отправки сообщений"""
        self._bot = bot

    async def _find_user_by_identifier(self, db: AsyncSession, user_identifier: str) -> User | None:
        """
        Найти пользователя по email или user_id из Remnawave Panel

        Args:
            db: Сессия БД
            user_identifier: Email или user_id пользователя

        Returns:
            User или None если не найден
        """
        # Сначала пытаемся получить telegram_id через remnawave_service
        try:
            telegram_id = await remnawave_service.get_telegram_id_by_email(user_identifier)
            if telegram_id:
                # Ищем пользователя по telegram_id
                result = await db.execute(select(User).where(User.telegram_id == telegram_id))
                user = result.scalar_one_or_none()
                if user:
                    return user
        except Exception as e:
            logger.warning('Не удалось получить telegram_id через remnawave', error=e)

        # Если не нашли через remnawave, пытаемся искать по email в подписках
        # (это может быть полезно если у пользователя есть подписка с таким email)
        try:
            # Импортируем здесь чтобы избежать циклических импортов
            from app.database.models import Subscription

            result = await db.execute(
                select(User).join(Subscription).where(Subscription.email == user_identifier).limit(1)
            )
            user = result.scalar_one_or_none()
            if user:
                return user
        except Exception as e:
            logger.warning('Ошибка поиска пользователя по email в подписках', error=e)

        return None

    async def send_punishment_notification(
        self,
        db: AsyncSession,
        user_identifier: str,
        username: str,
        ip_count: int,
        limit: int,
        ban_minutes: int,
        node_name: str | None = None,
    ) -> tuple[bool, str, int | None]:
        """
        Отправить уведомление о блокировке пользователю

        Returns:
            (success, message, telegram_id)
        """
        default_texts = _get_user_texts()
        if not self._bot:
            return False, default_texts.t('BAN_NOTIFICATION_BOT_NOT_INITIALIZED', 'Бот не инициализирован'), None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return (
                False,
                default_texts.t(
                    'BAN_NOTIFICATION_USER_NOT_FOUND_WITH_IDENTIFIER',
                    'Пользователь не найден: {user_identifier}',
                ).format(user_identifier=user_identifier),
                None,
            )
        texts = _get_user_texts(user)

        # Формируем информацию о ноде (заметно выделяем)
        node_info = (
            texts.t(
                'BAN_NOTIFICATION_NODE_INFO_LINE',
                '🖥 <b>Нода:</b> <code>{node_name}</code>',
            ).format(node_name=node_name)
            if node_name
            else ''
        )

        # Формируем сообщение из настроек
        # Используем безопасное форматирование - если {node_info} отсутствует в шаблоне, не будет ошибки
        format_vars = {'ip_count': ip_count, 'limit': limit, 'ban_minutes': ban_minutes, 'node_info': node_info}
        try:
            message_text = settings.BAN_MSG_PUNISHMENT.format(**format_vars)
        except KeyError:
            # Старый шаблон без {node_info} - форматируем без него
            message_text = settings.BAN_MSG_PUNISHMENT.format(ip_count=ip_count, limit=limit, ban_minutes=ban_minutes)
            # Добавляем информацию о ноде в конец, если она есть
            if node_info:
                message_text = message_text.rstrip() + f'\n\n{node_info.rstrip()}'

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            reason = texts.t(
                'BAN_NOTIFICATION_REASON_IP_LIMIT',
                'IP лимит превышен: {ip_count}/{limit}. Бан на {ban_minutes} минут.',
            ).format(ip_count=ip_count, limit=limit, ban_minutes=ban_minutes)
            if node_name:
                reason += texts.t('BAN_NOTIFICATION_REASON_NODE_SUFFIX', ' Нода: {node_name}').format(
                    node_name=node_name
                )
            success = await notification_delivery_service.notify_ban(
                user=user,
                reason=reason,
            )
            if success:
                logger.info('Email уведомление о бане отправлено пользователю', user_id=user.id)
                return True, texts.t('BAN_NOTIFICATION_EMAIL_SENT', 'Email уведомление отправлено'), None
            return False, texts.t('BAN_NOTIFICATION_EMAIL_SEND_FAILED', 'Не удалось отправить email уведомление'), None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(texts),
            )
            logger.info(
                'Уведомление о бане отправлено пользователю (telegram_id: )',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t('BAN_NOTIFICATION_SENT', 'Уведомление отправлено'), user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки уведомления пользователю (telegram_id: )',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return (
                False,
                texts.t('BAN_NOTIFICATION_TELEGRAM_API_ERROR', 'Ошибка Telegram API: {error}').format(error=f'{e!s}'),
                user.telegram_id,
            )

    async def send_enabled_notification(
        self, db: AsyncSession, user_identifier: str, username: str
    ) -> tuple[bool, str, int | None]:
        """
        Отправить уведомление о разблокировке пользователю

        Returns:
            (success, message, telegram_id)
        """
        default_texts = _get_user_texts()
        if not self._bot:
            return False, default_texts.t('BAN_NOTIFICATION_BOT_NOT_INITIALIZED', 'Бот не инициализирован'), None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return (
                False,
                default_texts.t(
                    'BAN_NOTIFICATION_USER_NOT_FOUND_WITH_IDENTIFIER',
                    'Пользователь не найден: {user_identifier}',
                ).format(user_identifier=user_identifier),
                None,
            )
        texts = _get_user_texts(user)

        # Формируем сообщение из настроек
        message_text = settings.BAN_MSG_ENABLED

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            success = await notification_delivery_service.notify_unban(user=user)
            if success:
                logger.info('Email уведомление о разбане отправлено пользователю', user_id=user.id)
                return True, texts.t('BAN_NOTIFICATION_EMAIL_SENT', 'Email уведомление отправлено'), None
            return False, texts.t('BAN_NOTIFICATION_EMAIL_SEND_FAILED', 'Не удалось отправить email уведомление'), None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(texts),
            )
            logger.info(
                'Уведомление о разбане отправлено пользователю (telegram_id: )',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t('BAN_NOTIFICATION_SENT', 'Уведомление отправлено'), user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки уведомления пользователю (telegram_id: )',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return (
                False,
                texts.t('BAN_NOTIFICATION_TELEGRAM_API_ERROR', 'Ошибка Telegram API: {error}').format(error=f'{e!s}'),
                user.telegram_id,
            )

    async def send_warning_notification(
        self, db: AsyncSession, user_identifier: str, username: str, warning_message: str
    ) -> tuple[bool, str, int | None]:
        """
        Отправить предупреждение пользователю

        Returns:
            (success, message, telegram_id)
        """
        default_texts = _get_user_texts()
        if not self._bot:
            return False, default_texts.t('BAN_NOTIFICATION_BOT_NOT_INITIALIZED', 'Бот не инициализирован'), None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return (
                False,
                default_texts.t(
                    'BAN_NOTIFICATION_USER_NOT_FOUND_WITH_IDENTIFIER',
                    'Пользователь не найден: {user_identifier}',
                ).format(user_identifier=user_identifier),
                None,
            )
        texts = _get_user_texts(user)

        # Формируем сообщение из настроек
        message_text = settings.BAN_MSG_WARNING.format(warning_message=warning_message)

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            context = {'message': warning_message}
            success = await notification_delivery_service.send_notification(
                user=user,
                notification_type=NotificationType.WARNING_NOTIFICATION,
                context=context,
            )
            if success:
                logger.info('Email предупреждение отправлено пользователю', user_id=user.id)
                return True, texts.t('BAN_NOTIFICATION_WARNING_EMAIL_SENT', 'Email предупреждение отправлено'), None
            return (
                False,
                texts.t('BAN_NOTIFICATION_WARNING_EMAIL_SEND_FAILED', 'Не удалось отправить email предупреждение'),
                None,
            )

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(texts),
            )
            logger.info(
                'Предупреждение отправлено пользователю (telegram_id: )',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t('BAN_NOTIFICATION_WARNING_SENT', 'Предупреждение отправлено'), user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки предупреждения пользователю (telegram_id: )',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return (
                False,
                texts.t('BAN_NOTIFICATION_TELEGRAM_API_ERROR', 'Ошибка Telegram API: {error}').format(error=f'{e!s}'),
                user.telegram_id,
            )

    async def send_network_wifi_notification(
        self,
        db: AsyncSession,
        user_identifier: str,
        username: str,
        ban_minutes: int,
        network_type: str | None = None,
        node_name: str | None = None,
    ) -> tuple[bool, str, int | None]:
        """
        Отправить уведомление о блокировке за использование WiFi сети

        Returns:
            (success, message, telegram_id)
        """
        default_texts = _get_user_texts()
        if not self._bot:
            return False, default_texts.t('BAN_NOTIFICATION_BOT_NOT_INITIALIZED', 'Бот не инициализирован'), None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return (
                False,
                default_texts.t(
                    'BAN_NOTIFICATION_USER_NOT_FOUND_WITH_IDENTIFIER',
                    'Пользователь не найден: {user_identifier}',
                ).format(user_identifier=user_identifier),
                None,
            )
        texts = _get_user_texts(user)

        # Формируем сообщение из настроек (заметно выделяем)
        network_info = (
            texts.t(
                'BAN_NOTIFICATION_NETWORK_INFO_LINE',
                '├ 🌐 Сеть: <b>{network_type}</b>\n',
            ).format(network_type=network_type)
            if network_type
            else ''
        )
        node_info = (
            texts.t(
                'BAN_NOTIFICATION_NODE_INFO_LINE',
                '🖥 <b>Нода:</b> <code>{node_name}</code>',
            ).format(node_name=node_name)
            if node_name
            else ''
        )

        logger.info('WiFi notification: node_name=, node_info', node_name=repr(node_name), node_info=repr(node_info))

        # Безопасное форматирование
        format_vars = {'ban_minutes': ban_minutes, 'network_info': network_info, 'node_info': node_info}
        try:
            message_text = settings.BAN_MSG_WIFI.format(**format_vars)
        except KeyError:
            logger.warning('BAN_MSG_WIFI template missing placeholders, adding node_info to end')
            message_text = settings.BAN_MSG_WIFI.format(ban_minutes=ban_minutes)
            extra_info = (network_info + node_info).strip()
            if extra_info:
                message_text = message_text.rstrip() + f'\n\n{extra_info}'

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            reason = texts.t(
                'BAN_NOTIFICATION_REASON_WIFI_BAN',
                'Использование WiFi сети запрещено. Бан на {ban_minutes} минут.',
            ).format(ban_minutes=ban_minutes)
            if network_type:
                reason += texts.t('BAN_NOTIFICATION_REASON_NETWORK_SUFFIX', ' Сеть: {network_type}').format(
                    network_type=network_type
                )
            if node_name:
                reason += texts.t('BAN_NOTIFICATION_REASON_NODE_SUFFIX', ' Нода: {node_name}').format(
                    node_name=node_name
                )
            success = await notification_delivery_service.notify_ban(
                user=user,
                reason=reason,
            )
            if success:
                logger.info('Email WiFi уведомление отправлено пользователю', user_id=user.id)
                return True, texts.t('BAN_NOTIFICATION_EMAIL_SENT', 'Email уведомление отправлено'), None
            return False, texts.t('BAN_NOTIFICATION_EMAIL_SEND_FAILED', 'Не удалось отправить email уведомление'), None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(texts),
            )
            logger.info(
                'Уведомление о WiFi бане отправлено пользователю (telegram_id: )',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t('BAN_NOTIFICATION_SENT', 'Уведомление отправлено'), user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки WiFi уведомления пользователю (telegram_id: )',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return (
                False,
                texts.t('BAN_NOTIFICATION_TELEGRAM_API_ERROR', 'Ошибка Telegram API: {error}').format(error=f'{e!s}'),
                user.telegram_id,
            )

    async def send_network_mobile_notification(
        self,
        db: AsyncSession,
        user_identifier: str,
        username: str,
        ban_minutes: int,
        network_type: str | None = None,
        node_name: str | None = None,
    ) -> tuple[bool, str, int | None]:
        """
        Отправить уведомление о блокировке за использование мобильной сети

        Returns:
            (success, message, telegram_id)
        """
        default_texts = _get_user_texts()
        if not self._bot:
            return False, default_texts.t('BAN_NOTIFICATION_BOT_NOT_INITIALIZED', 'Бот не инициализирован'), None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return (
                False,
                default_texts.t(
                    'BAN_NOTIFICATION_USER_NOT_FOUND_WITH_IDENTIFIER',
                    'Пользователь не найден: {user_identifier}',
                ).format(user_identifier=user_identifier),
                None,
            )
        texts = _get_user_texts(user)

        # Формируем сообщение из настроек (заметно выделяем)
        network_info = (
            texts.t(
                'BAN_NOTIFICATION_NETWORK_INFO_LINE',
                '├ 🌐 Сеть: <b>{network_type}</b>\n',
            ).format(network_type=network_type)
            if network_type
            else ''
        )
        node_info = (
            texts.t(
                'BAN_NOTIFICATION_NODE_INFO_LINE',
                '🖥 <b>Нода:</b> <code>{node_name}</code>',
            ).format(node_name=node_name)
            if node_name
            else ''
        )

        # Безопасное форматирование
        format_vars = {'ban_minutes': ban_minutes, 'network_info': network_info, 'node_info': node_info}
        try:
            message_text = settings.BAN_MSG_MOBILE.format(**format_vars)
        except KeyError:
            message_text = settings.BAN_MSG_MOBILE.format(ban_minutes=ban_minutes)
            extra_info = (network_info + node_info).strip()
            if extra_info:
                message_text = message_text.rstrip() + f'\n\n{extra_info}'

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            reason = texts.t(
                'BAN_NOTIFICATION_REASON_MOBILE_BAN',
                'Использование мобильной сети запрещено. Бан на {ban_minutes} минут.',
            ).format(ban_minutes=ban_minutes)
            if network_type:
                reason += texts.t('BAN_NOTIFICATION_REASON_NETWORK_SUFFIX', ' Сеть: {network_type}').format(
                    network_type=network_type
                )
            if node_name:
                reason += texts.t('BAN_NOTIFICATION_REASON_NODE_SUFFIX', ' Нода: {node_name}').format(
                    node_name=node_name
                )
            success = await notification_delivery_service.notify_ban(
                user=user,
                reason=reason,
            )
            if success:
                logger.info('Email Mobile уведомление отправлено пользователю', user_id=user.id)
                return True, texts.t('BAN_NOTIFICATION_EMAIL_SENT', 'Email уведомление отправлено'), None
            return False, texts.t('BAN_NOTIFICATION_EMAIL_SEND_FAILED', 'Не удалось отправить email уведомление'), None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(texts),
            )
            logger.info(
                'Уведомление о Mobile бане отправлено пользователю (telegram_id: )',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, texts.t('BAN_NOTIFICATION_SENT', 'Уведомление отправлено'), user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки Mobile уведомления пользователю (telegram_id: )',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return (
                False,
                texts.t('BAN_NOTIFICATION_TELEGRAM_API_ERROR', 'Ошибка Telegram API: {error}').format(error=f'{e!s}'),
                user.telegram_id,
            )


# Глобальный экземпляр сервиса
ban_notification_service = BanNotificationService()
