"""
Сервис для отправки уведомлений от ban системы пользователям
"""

import html

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


def _get_notification_template(setting_name: str, language: str) -> str:
    """Localize only the built-in default; keep operator templates verbatim."""
    template = getattr(settings, setting_name)
    default = type(settings).model_fields[setting_name].default
    if template != default:
        return template
    return get_texts(language).t(f'BAN_DEFAULT_{setting_name.removeprefix("BAN_MSG_")}', default)


def _format_notification_template(template: str, fallback: str, **values: object) -> str:
    """Format an editable template and fall back safely when its placeholders are invalid."""
    try:
        return template.format(**values)
    except (IndexError, KeyError, ValueError):
        logger.exception('Некорректный шаблон ban-уведомления, использован резервный')
        return fallback.format(**values)


def get_delete_keyboard(language: str | None = None) -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой удаления уведомления"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_texts(language or settings.DEFAULT_LANGUAGE).t('BAN_NOTICE_016', '🗑 Удалить'),
                    callback_data='ban_notify:delete',
                )
            ]
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
        revoke: bool = False,
    ) -> tuple[bool, str, int | None]:
        """
        Отправить уведомление о блокировке пользователю

        Returns:
            (success, message, telegram_id)
        """
        if not self._bot:
            return False, 'Бот не инициализирован', None
        if not settings.is_notifications_enabled():
            return False, 'Уведомления пользователям отключены', None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, f'Пользователь не найден: {user_identifier}', None

        # Формируем информацию о ноде (заметно выделяем)
        node_info = (
            get_texts(language)
            .t('BAN_NOTICE_000', '🖥 <b>Нода:</b> <code>{v0}</code>')
            .format(v0=html.escape(node_name))
            if node_name
            else ''
        )

        # Формируем сообщение из настроек
        # Используем безопасное форматирование - если {node_info} отсутствует в шаблоне, не будет ошибки
        format_vars = {'ip_count': ip_count, 'limit': limit, 'ban_minutes': ban_minutes, 'node_info': node_info}
        template = (
            _get_notification_template('BAN_MSG_REVOKE', language)
            if revoke
            else _get_notification_template('BAN_MSG_PUNISHMENT', language)
        )
        message_text = _format_notification_template(
            template,
            get_texts(language).t(
                'BAN_NOTICE_001',
                '🚫 <b>АККАУНТ ЗАБЛОКИРОВАН</b>\n\n{node_info}\n📱 Устройств: <b>{ip_count}</b> из <b>{limit}</b>\n⏱ Ограничение: <b>{ban_minutes} мин</b>',
            ),
            **format_vars,
        )

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            action = (
                get_texts(language).t('BAN_NOTICE_010', 'Ключи доступа сброшены')
                if revoke
                else get_texts(language).t('BAN_NOTICE_011', 'Бан на {v0} минут').format(v0=ban_minutes)
            )
            reason = (
                get_texts(language)
                .t('BAN_NOTICE_002', 'IP лимит превышен: {v0}/{v1}. {v2}.')
                .format(v0=ip_count, v1=limit, v2=action)
            )
            if node_name:
                reason += get_texts(language).t('BAN_NOTICE_012', ' Нода: {v0}').format(v0=node_name)
            success = await notification_delivery_service.notify_ban(
                user=user,
                reason=reason,
            )
            if success:
                logger.info('Email уведомление о бане отправлено пользователю', user_id=user.id)
                return True, 'Email уведомление отправлено', None
            return False, 'Не удалось отправить email уведомление', None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(language),
            )
            logger.info(
                'Уведомление о бане отправлено пользователю',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, 'Уведомление отправлено', user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки уведомления пользователю',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, f'Ошибка Telegram API: {e!s}', user.telegram_id

    async def send_enabled_notification(
        self, db: AsyncSession, user_identifier: str, username: str
    ) -> tuple[bool, str, int | None]:
        """
        Отправить уведомление о разблокировке пользователю

        Returns:
            (success, message, telegram_id)
        """
        if not self._bot:
            return False, 'Бот не инициализирован', None
        if not settings.is_notifications_enabled():
            return False, 'Уведомления пользователям отключены', None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, f'Пользователь не найден: {user_identifier}', None

        # Формируем сообщение из настроек
        message_text = _get_notification_template('BAN_MSG_ENABLED', language)

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            success = await notification_delivery_service.notify_unban(user=user)
            if success:
                logger.info('Email уведомление о разбане отправлено пользователю', user_id=user.id)
                return True, 'Email уведомление отправлено', None
            return False, 'Не удалось отправить email уведомление', None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(language),
            )
            logger.info(
                'Уведомление о разбане отправлено пользователю',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, 'Уведомление отправлено', user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки уведомления пользователю',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, f'Ошибка Telegram API: {e!s}', user.telegram_id

    async def send_warning_notification(
        self, db: AsyncSession, user_identifier: str, username: str, warning_message: str
    ) -> tuple[bool, str, int | None]:
        """
        Отправить предупреждение пользователю

        Returns:
            (success, message, telegram_id)
        """
        if not self._bot:
            return False, 'Бот не инициализирован', None
        if not settings.is_notifications_enabled():
            return False, 'Уведомления пользователям отключены', None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, f'Пользователь не найден: {user_identifier}', None

        # Формируем сообщение из настроек
        safe_warning = html.escape(warning_message)
        message_text = _format_notification_template(
            _get_notification_template('BAN_MSG_WARNING', language),
            get_texts(language).t('BAN_NOTICE_003', '⚠️ <b>ПРЕДУПРЕЖДЕНИЕ</b>\n\n{warning_message}'),
            warning_message=safe_warning,
        )

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
                return True, 'Email предупреждение отправлено', None
            return False, 'Не удалось отправить email предупреждение', None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(language),
            )
            logger.info(
                'Предупреждение отправлено пользователю',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, 'Предупреждение отправлено', user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки предупреждения пользователю',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, f'Ошибка Telegram API: {e!s}', user.telegram_id

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
        if not self._bot:
            return False, 'Бот не инициализирован', None
        if not settings.is_notifications_enabled():
            return False, 'Уведомления пользователям отключены', None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, f'Пользователь не найден: {user_identifier}', None

        # Формируем сообщение из настроек (заметно выделяем)
        network_info = (
            get_texts(language).t('BAN_NOTICE_004', '├ 🌐 Сеть: <b>{v0}</b>\n').format(v0=html.escape(network_type))
            if network_type
            else ''
        )
        node_info = (
            get_texts(language)
            .t('BAN_NOTICE_000', '🖥 <b>Нода:</b> <code>{v0}</code>')
            .format(v0=html.escape(node_name))
            if node_name
            else ''
        )

        logger.info('WiFi notification', node_name=repr(node_name), node_info=repr(node_info))

        # Безопасное форматирование
        format_vars = {'ban_minutes': ban_minutes, 'network_info': network_info, 'node_info': node_info}
        message_text = _format_notification_template(
            _get_notification_template('BAN_MSG_WIFI', language),
            get_texts(language).t(
                'BAN_NOTICE_005',
                '🚫 <b>Блокировка за WiFi</b>\n\n{node_info}\n{network_info}⏱ Время блокировки: <b>{ban_minutes} мин</b>',
            ),
            **format_vars,
        )

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            reason = (
                get_texts(language)
                .t('BAN_NOTICE_006', 'Использование WiFi сети запрещено. Бан на {v0} минут.')
                .format(v0=ban_minutes)
            )
            if network_type:
                reason += get_texts(language).t('BAN_NOTICE_013', ' Сеть: {v0}').format(v0=network_type)
            if node_name:
                reason += get_texts(language).t('BAN_NOTICE_012', ' Нода: {v0}').format(v0=node_name)
            success = await notification_delivery_service.notify_ban(
                user=user,
                reason=reason,
            )
            if success:
                logger.info('Email WiFi уведомление отправлено пользователю', user_id=user.id)
                return True, 'Email уведомление отправлено', None
            return False, 'Не удалось отправить email уведомление', None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(language),
            )
            logger.info(
                'Уведомление о WiFi бане отправлено пользователю',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, 'Уведомление отправлено', user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки WiFi уведомления пользователю',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, f'Ошибка Telegram API: {e!s}', user.telegram_id

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
        if not self._bot:
            return False, 'Бот не инициализирован', None
        if not settings.is_notifications_enabled():
            return False, 'Уведомления пользователям отключены', None

        # Находим пользователя
        user = await self._find_user_by_identifier(db, user_identifier)
        language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, f'Пользователь не найден: {user_identifier}', None

        # Формируем сообщение из настроек (заметно выделяем)
        network_info = (
            get_texts(language).t('BAN_NOTICE_004', '├ 🌐 Сеть: <b>{v0}</b>\n').format(v0=html.escape(network_type))
            if network_type
            else ''
        )
        node_info = (
            get_texts(language)
            .t('BAN_NOTICE_000', '🖥 <b>Нода:</b> <code>{v0}</code>')
            .format(v0=html.escape(node_name))
            if node_name
            else ''
        )

        # Безопасное форматирование
        format_vars = {'ban_minutes': ban_minutes, 'network_info': network_info, 'node_info': node_info}
        message_text = _format_notification_template(
            _get_notification_template('BAN_MSG_MOBILE', language),
            get_texts(language).t(
                'BAN_NOTICE_007',
                '🚫 <b>Блокировка за мобильную сеть</b>\n\n{node_info}\n{network_info}⏱ Время блокировки: <b>{ban_minutes} мин</b>',
            ),
            **format_vars,
        )

        # Handle email-only users via notification delivery service
        if not user.telegram_id:
            reason = (
                get_texts(language)
                .t('BAN_NOTICE_008', 'Использование мобильной сети запрещено. Бан на {v0} минут.')
                .format(v0=ban_minutes)
            )
            if network_type:
                reason += get_texts(language).t('BAN_NOTICE_013', ' Сеть: {v0}').format(v0=network_type)
            if node_name:
                reason += get_texts(language).t('BAN_NOTICE_012', ' Нода: {v0}').format(v0=node_name)
            success = await notification_delivery_service.notify_ban(
                user=user,
                reason=reason,
            )
            if success:
                logger.info('Email Mobile уведомление отправлено пользователю', user_id=user.id)
                return True, 'Email уведомление отправлено', None
            return False, 'Не удалось отправить email уведомление', None

        # Отправляем сообщение с кнопкой удаления
        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(language),
            )
            logger.info(
                'Уведомление о Mobile бане отправлено пользователю',
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, 'Уведомление отправлено', user.telegram_id

        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки Mobile уведомления пользователю',
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, f'Ошибка Telegram API: {e!s}', user.telegram_id

    async def send_typed_ban_notification(
        self,
        db: AsyncSession,
        user_identifier: str,
        username: str,
        notification_type: str,
        ban_minutes: int,
        reason: str | None = None,
        node_name: str | None = None,
    ) -> tuple[bool, str, int | None]:
        """Send a BanHammer ban notification using a template for its cause."""
        if not self._bot:
            return False, 'Бот не инициализирован', None
        # Тот же глобальный рубильник, что и у остальных методов сервиса:
        # без него выключенные уведомления всё равно доставляли BanHammer-сообщения
        # в Telegram, хотя email-ветка ниже уже уходит через
        # notification_delivery_service, который рубильник уважает.
        if not settings.is_notifications_enabled():
            return False, 'Уведомления пользователям отключены', None

        template_names = {
            'torrent': 'BAN_MSG_TORRENT',
            'hwid_limit': 'BAN_MSG_HWID_LIMIT',
            'suspicious_destination': 'BAN_MSG_SUSPICIOUS_DESTINATION',
            'traffic_limit': 'BAN_MSG_TRAFFIC_LIMIT',
            'manual': 'BAN_MSG_MANUAL',
        }
        template_name = template_names.get(notification_type)
        if not template_name:
            logger.warning('Неизвестный типизированный тип бана', notification_type=notification_type)
            return False, f'Неизвестный тип бана: {notification_type}', None

        user = await self._find_user_by_identifier(db, user_identifier)
        language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
        if not user:
            logger.warning('Пользователь не найден в базе данных', user_identifier=user_identifier)
            return False, f'Пользователь не найден: {user_identifier}', None

        template = _get_notification_template(template_name, language)
        safe_reason = html.escape(reason or get_texts(language).t('BAN_NOTICE_014', 'Детали нарушения не указаны'))
        node_info = (
            get_texts(language)
            .t('BAN_NOTICE_000', '🖥 <b>Нода:</b> <code>{v0}</code>')
            .format(v0=html.escape(node_name))
            if node_name
            else ''
        )
        message_text = _format_notification_template(
            template,
            get_texts(language).t(
                'BAN_NOTICE_009',
                '🚫 <b>АККАУНТ ЗАБЛОКИРОВАН</b>\n\n{node_info}\n📝 <b>Детали:</b> {reason}\n⏱ <b>Время блокировки:</b> {ban_minutes} мин\n\n🔄 Доступ восстановится автоматически после окончания блокировки.',
            ),
            ban_minutes=ban_minutes,
            reason=safe_reason,
            node_info=node_info,
        )

        if not user.telegram_id:
            email_reason = reason or get_texts(language).t('BAN_NOTICE_015', 'Нарушение правил сервиса')
            if node_name:
                email_reason += get_texts(language).t('BAN_NOTICE_012', ' Нода: {v0}').format(v0=node_name)
            success = await notification_delivery_service.notify_ban(user=user, reason=email_reason)
            if success:
                return True, 'Email уведомление отправлено', None
            return False, 'Не удалось отправить email уведомление', None

        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=get_delete_keyboard(language),
            )
            logger.info(
                'Типизированное уведомление о бане отправлено пользователю',
                notification_type=notification_type,
                username=username,
                telegram_id=user.telegram_id,
            )
            return True, 'Уведомление отправлено', user.telegram_id
        except TelegramAPIError as e:
            logger.error(
                'Ошибка отправки типизированного уведомления о бане',
                notification_type=notification_type,
                username=username,
                telegram_id=user.telegram_id,
                error=e,
            )
            return False, f'Ошибка Telegram API: {e!s}', user.telegram_id


# Глобальный экземпляр сервиса
ban_notification_service = BanNotificationService()
