"""Middleware для автоматического логирования кликов по кнопкам."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import settings
from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)

# Известные builtin callback_data из меню
BUILTIN_CALLBACKS: set[str] = {
    # Основные кнопки меню
    'subscription_connect',
    'subscription_happ_download',
    'menu_subscription',
    'buy_traffic',
    'menu_balance',
    'menu_trial',
    'menu_buy',
    'simple_subscription_purchase',
    'return_to_saved_cart',
    'menu_promocode',
    'menu_referrals',
    'contests_menu',
    'menu_support',
    'menu_info',
    'menu_language',
    'admin_panel',
    'moderator_panel',
    # Навигация
    'back_to_menu',
    'menu_faq',
    'menu_info_promo_groups',
    'menu_privacy_policy',
    'menu_public_offer',
    'menu_rules',
    'menu_server_status',
    # Баланс
    'balance_history',
    'balance_topup',
    # Подписка
    'subscription_extend',
    'subscription_autopay',
    'subscription_settings',
    'open_subscription_link',
    'subscription_add_countries',
    'subscription_reset_traffic',
    'subscription_switch_traffic',
    'subscription_change_devices',
    'subscription_manage_devices',
    'subscription_upgrade',
    # Устройства
    'device_guide_ios',
    'device_guide_android',
    'device_guide_windows',
    'device_guide_mac',
    'device_guide_tv',
    'device_guide_appletv',
    # Happ
    'happ_download_ios',
    'happ_download_android',
    'happ_download_macos',
    'happ_download_windows',
    # Рефералы
    'referral_create_invite',
    'referral_show_qr',
    'referral_list',
    'referral_analytics',
    # Поддержка
    'create_ticket',
    'my_tickets',
    # Триал
    'trial_activate',
    # Покупка
    'clear_saved_cart',
    'subscription_confirm',
    'subscription_cancel',
}


class ButtonStatsMiddleware(BaseMiddleware):
    """Middleware для автоматического логирования кликов по кнопкам и команд бота."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Перехватывает CallbackQuery (клики) и Message-команды (/start и т.п.)."""

        # Пропускаем, если выключены и статистика конструктора меню,
        # и лог действий юзера (таймлайн активности в карточке).
        if not (settings.MENU_LAYOUT_ENABLED or settings.USER_ACTION_LOG_ENABLED):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            self._log_callback(event)
        elif isinstance(event, Message):
            self._log_command(event)

        # Продолжаем обработку
        return await handler(event, data)

    def _log_callback(self, event: CallbackQuery) -> None:
        """Логирует клик по inline-кнопке (асинхронно, не блокируя обработку)."""
        try:
            callback_data = event.data
            if not callback_data:
                return

            user_id = event.from_user.id if event.from_user else None
            button_type = self._determine_button_type(callback_data)

            # Получаем текст кнопки, если возможно
            button_text = None
            if event.message and hasattr(event.message, 'reply_markup'):
                button_text = self._extract_button_text(event.message.reply_markup, callback_data)

            asyncio.create_task(
                self._log_button_click_async(
                    button_id=callback_data,
                    user_id=user_id,
                    callback_data=callback_data,
                    button_type=button_type,
                    button_text=button_text,
                )
            )
        except Exception as e:
            # Не прерываем обработку при ошибке логирования
            logger.error('Ошибка логирования клика по кнопке', error=e, exc_info=True)

    def _log_command(self, event: Message) -> None:
        """Логирует команды бота (/start, /menu, ...).

        Обычные текстовые сообщения не пишутся вовсе (промокоды, переписка с
        поддержкой). Payload команды тоже не сохраняется: в диплинках /start
        бывают секретные токены (webauth_, GIFT_, coupon_) — фиксируется лишь
        факт его наличия.
        """
        try:
            text = event.text
            if not text or not text.startswith('/'):
                return

            parts = text.split(maxsplit=1)
            command = parts[0].split('@', 1)[0][:100]  # '/start@my_bot arg' -> '/start'
            if len(command) < 2:
                return
            has_payload = len(parts) > 1

            user_id = event.from_user.id if event.from_user else None

            asyncio.create_task(
                self._log_button_click_async(
                    button_id=command,
                    user_id=user_id,
                    callback_data=None,
                    button_type='command',
                    button_text=f'{command} …' if has_payload else command,
                )
            )
        except Exception as e:
            logger.error('Ошибка логирования команды бота', error=e, exc_info=True)

    def _determine_button_type(self, callback_data: str) -> str:
        """Определяет тип кнопки по callback_data.

        Примечание: URL и MiniApp кнопки не имеют callback_data,
        поэтому они не отслеживаются через этот middleware.
        Для их отслеживания нужен отдельный механизм на стороне клиента.
        """
        # Проверяем по известному списку builtin кнопок
        if callback_data in BUILTIN_CALLBACKS:
            return 'builtin'

        # Дополнительная проверка по префиксам для динамических callback_data
        builtin_prefixes = (
            'menu_',
            'admin_',
            'subscription_',
            'balance_',
            'referral_',
            'device_guide_',
            'happ_download_',
        )
        if callback_data.startswith(builtin_prefixes):
            return 'builtin'

        # Всё остальное - кастомные callback кнопки
        return 'callback'

    def _extract_button_text(self, reply_markup, callback_data: str) -> str:
        """Извлекает текст кнопки из клавиатуры."""
        try:
            if not reply_markup or not hasattr(reply_markup, 'inline_keyboard'):
                return None

            for row in reply_markup.inline_keyboard:
                for button in row:
                    if hasattr(button, 'callback_data') and button.callback_data == callback_data:
                        if hasattr(button, 'text'):
                            return button.text
        except Exception:
            pass
        return None

    async def _log_button_click_async(
        self,
        button_id: str,
        user_id: int = None,
        callback_data: str = None,
        button_type: str = None,
        button_text: str = None,
    ):
        """Асинхронно логирует клик по кнопке."""
        try:
            async with AsyncSessionLocal() as db:
                try:
                    from app.services.menu_layout_service import MenuLayoutService

                    await MenuLayoutService.log_button_click(
                        db,
                        button_id=button_id,
                        user_id=user_id,
                        callback_data=callback_data,
                        button_type=button_type,
                        button_text=button_text,
                    )
                except Exception as e:
                    logger.warning('Ошибка записи клика в БД', button_id=button_id, error=e)
        except Exception as e:
            logger.warning('Ошибка создания сессии БД для логирования клика', error=e)
