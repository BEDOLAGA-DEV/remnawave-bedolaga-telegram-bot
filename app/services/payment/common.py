"""Общие инструменты платёжного сервиса.

В этом модуле собраны методы, которые нужны всем платёжным каналам:
построение клавиатур, базовые уведомления и стандартная обработка
успешных платежей.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal, get_db
from app.database.models import Subscription
from app.localization.texts import get_texts
from app.services.subscription_checkout_service import (
    has_subscription_checkout_draft,
    should_offer_checkout_resume,
)
from app.services.user_cart_service import user_cart_service
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.utils.payment_logger import payment_logger as logger


class PaymentCommonMixin:
    """Mixin с базовой логикой, которую используют остальные платёжные блоки."""

    async def build_topup_success_keyboard(self, user: Any) -> InlineKeyboardMarkup:
        """Формирует клавиатуру по завершении платежа, подстраиваясь под пользователя."""
        # Загружаем нужные тексты с учётом выбранного языка пользователя.
        texts = get_texts(user.language if user else 'ru')

        # Определяем статус подписки, чтобы показать подходящую кнопку.
        has_active_subscription = False
        subscription = None
        if user:
            try:
                subscription = user.subscription
                has_active_subscription = bool(
                    subscription
                    and not getattr(subscription, 'is_trial', False)
                    and getattr(subscription, 'is_active', False)
                )
            except MissingGreenlet:
                # user вне сессии — загружаем подписку отдельным запросом
                try:
                    async with AsyncSessionLocal() as session:
                        result = await session.execute(
                            select(Subscription.status, Subscription.is_trial, Subscription.end_date)
                            .where(Subscription.user_id == user.id)
                            .order_by(Subscription.created_at.desc())
                            .limit(1)
                        )
                        row = result.one_or_none()
                        if row:
                            end_date = row.end_date
                            if end_date is not None and end_date.tzinfo is None:
                                end_date = end_date.replace(tzinfo=UTC)
                            is_active = row.status == 'active' and end_date is not None and end_date > datetime.now(UTC)
                            has_active_subscription = bool(is_active and not row.is_trial)
                except Exception as db_error:
                    logger.warning(
                        'Не удалось загрузить подписку пользователя из БД',
                        getattr=getattr(user, 'id', None),
                        db_error=db_error,
                    )
            except Exception as error:  # pragma: no cover - защитный код
                logger.error(
                    'Ошибка загрузки подписки пользователя при построении клавиатуры после пополнения',
                    getattr=getattr(user, 'id', None),
                    error=error,
                )

        # Создаем основную кнопку: если есть активная подписка - продлить, иначе купить
        first_button = build_miniapp_or_callback_button(
            text=(texts.MENU_EXTEND_SUBSCRIPTION if has_active_subscription else texts.MENU_BUY_SUBSCRIPTION),
            callback_data=('subscription_extend' if has_active_subscription else 'menu_buy'),
        )

        keyboard_rows: list[list[InlineKeyboardButton]] = [
            [first_button],
        ]

        # Если для пользователя есть незавершённый checkout, предлагаем вернуться к нему.
        if user:
            try:
                has_saved_cart = await user_cart_service.has_user_cart(user.id)
            except Exception as cart_error:
                logger.warning(
                    'Не удалось проверить наличие сохраненной корзины у пользователя',
                    user_id=user.id,
                    cart_error=cart_error,
                )
                has_saved_cart = False

            if has_saved_cart:
                keyboard_rows.append(
                    [
                        build_miniapp_or_callback_button(
                            text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                            callback_data='return_to_saved_cart',
                        )
                    ]
                )
            else:
                draft_exists = await has_subscription_checkout_draft(user.id)
                if should_offer_checkout_resume(user, draft_exists, subscription=subscription):
                    keyboard_rows.append(
                        [
                            build_miniapp_or_callback_button(
                                text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                                callback_data='subscription_resume_checkout',
                            )
                        ]
                    )

        # Стандартные кнопки быстрого доступа к балансу и главному меню.
        keyboard_rows.append(
            [
                build_miniapp_or_callback_button(
                    text=texts.t('MY_BALANCE_BUTTON', '💰 Мой баланс'),
                    callback_data='menu_balance',
                )
            ]
        )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('MAIN_MENU_BUTTON', '🏠 Главное меню'),
                    callback_data='back_to_menu',
                )
            ]
        )

        return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    async def _send_payment_success_notification(
        self,
        telegram_id: int | None,
        amount_kopeks: int,
        user: Any | None = None,
        *,
        db: AsyncSession | None = None,
        payment_method_title: str | None = None,
    ) -> None:
        """Отправляет пользователю уведомление об успешном платеже."""
        # Lazy import to avoid circular dependency
        from app.cabinet.routes.websocket import notify_user_balance_topup

        # Send WebSocket notification to cabinet frontend (works for both Telegram and email-only users)
        user_id = getattr(user, 'id', None) if user else None
        if user_id:
            try:
                # Get new balance from user
                new_balance = getattr(user, 'balance_kopeks', 0)
                await notify_user_balance_topup(
                    user_id=user_id,
                    amount_kopeks=amount_kopeks,
                    new_balance_kopeks=new_balance,
                    description=payment_method_title or '',
                )
            except Exception as ws_error:
                logger.warning(
                    'Не удалось отправить WS уведомление о пополнении баланса для user_id',
                    user_id=user_id,
                    ws_error=ws_error,
                )

        if not getattr(self, 'bot', None):
            # Если бот не передан (например, внутри фоновых задач), уведомление пропускаем.
            return

        # Skip email-only users (no telegram_id)
        if not telegram_id:
            return

        user_snapshot = await self._ensure_user_snapshot(
            telegram_id,
            user,
            db=db,
        )
        texts = get_texts(getattr(user_snapshot, 'language', 'ru') if user_snapshot else 'ru')

        try:
            payment_method = payment_method_title or texts.t(
                'PAYMENT_COMMON_DEFAULT_METHOD_YOOKASSA',
                'Банковская карта (YooKassa)',
            )

            # Стандартное сообщение с полной клавиатурой
            keyboard = await self.build_topup_success_keyboard(user_snapshot)
            message = texts.t(
                'PAYMENT_COMMON_TOPUP_SUCCESS_NOTIFICATION',
                '✅ <b>Платеж успешно завершен!</b>\n\n'
                '💰 Сумма: {amount}\n'
                '💳 Способ: {payment_method}\n\n'
                'Средства зачислены на ваш баланс!\n\n'
                '⚠️ <b>Важно:</b> Пополнение баланса не активирует подписку автоматически. '
                'Обязательно активируйте подписку отдельно!\n\n'
                '🔄 При наличии сохранённой корзины подписки и включенной автопокупке, '
                'подписка будет приобретена автоматически после пополнения баланса.',
            ).format(
                amount=settings.format_price(amount_kopeks),
                payment_method=payment_method,
            )

            await self.bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        except Exception as error:
            logger.error('Ошибка отправки уведомления пользователю', telegram_id=telegram_id, error=error)

    async def _ensure_user_snapshot(
        self,
        telegram_id: int | None,
        user: Any | None,
        *,
        db: AsyncSession | None = None,
    ) -> Any | None:
        """Гарантирует, что данные пользователя пригодны для построения клавиатуры."""

        def _build_snapshot(source: Any | None) -> SimpleNamespace | None:
            if source is None:
                return None

            subscription = getattr(source, 'subscription', None)
            subscription_snapshot = None

            if subscription is not None:
                subscription_snapshot = SimpleNamespace(
                    is_trial=getattr(subscription, 'is_trial', False),
                    is_active=getattr(subscription, 'is_active', False),
                    actual_status=getattr(subscription, 'actual_status', None),
                )

            return SimpleNamespace(
                id=getattr(source, 'id', None),
                telegram_id=getattr(source, 'telegram_id', None),
                language=getattr(source, 'language', 'ru'),
                subscription=subscription_snapshot,
            )

        try:
            snapshot = _build_snapshot(user)
        except MissingGreenlet:
            snapshot = None

        if snapshot is not None:
            return snapshot

        fetch_session = db

        if fetch_session is not None:
            try:
                fetched_user = await get_user_by_telegram_id(fetch_session, telegram_id)
                return _build_snapshot(fetched_user)
            except Exception as fetch_error:
                logger.warning(
                    'Не удалось обновить пользователя из переданной сессии',
                    telegram_id=telegram_id,
                    fetch_error=fetch_error,
                )

        try:
            async for db_session in get_db():
                fetched_user = await get_user_by_telegram_id(db_session, telegram_id)
                return _build_snapshot(fetched_user)
        except Exception as fetch_error:
            logger.warning(
                'Не удалось получить пользователя для уведомления', telegram_id=telegram_id, fetch_error=fetch_error
            )

        return None

    async def process_successful_payment(
        self,
        payment_id: str,
        amount_kopeks: int,
        user_id: int,
        payment_method: str,
    ) -> bool:
        """Общая точка учёта успешных платежей (используется провайдерами при необходимости)."""
        try:
            logger.info(
                'Обработан успешный платеж ₽, пользователь , метод',
                payment_id=payment_id,
                amount_kopeks=amount_kopeks / 100,
                user_id=user_id,
                payment_method=payment_method,
            )
            return True
        except Exception as error:
            logger.error('Ошибка обработки платежа', payment_id=payment_id, error=error)
            return False
