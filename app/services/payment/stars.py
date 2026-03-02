"""Логика Telegram Stars вынесена в отдельный mixin.

Методы здесь отвечают только за работу с звёздами, что позволяет держать
основной сервис компактным и облегчает тестирование конкретных сценариев.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from aiogram.types import LabeledPrice
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.transaction import create_transaction
from app.database.crud.user import get_user_by_id
from app.database.models import PaymentMethod, TransactionType
from app.external.telegram_stars import TelegramStarsService
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


@dataclass(slots=True)
class _SimpleSubscriptionPayload:
    """Данные для простой подписки, извлечённые из payload звёздного платежа."""

    subscription_id: int | None
    period_days: int | None


class TelegramStarsMixin:
    """Mixin с операциями создания и обработки платежей через Telegram Stars."""

    async def create_stars_invoice(
        self,
        amount_kopeks: int,
        description: str,
        payload: str | None = None,
        *,
        stars_amount: int | None = None,
    ) -> str:
        """Создаёт invoice в Telegram Stars, автоматически рассчитывая количество звёзд."""
        if not self.bot or not getattr(self, 'stars_service', None):
            raise ValueError('Bot instance required for Stars payments')

        try:
            amount_rubles = Decimal(amount_kopeks) / Decimal(100)

            # Если количество звёзд не задано, вычисляем его из курса.
            if stars_amount is None:
                stars_amount = settings.rubles_to_stars(float(amount_rubles))

            if stars_amount <= 0:
                raise ValueError('Stars amount must be positive')

            invoice_link = await self.bot.create_invoice_link(
                title='Пополнение баланса VPN',
                description=f'{description} (≈{stars_amount} ⭐)',
                payload=payload or f'balance_topup_{amount_kopeks}',
                provider_token='',
                currency='XTR',
                prices=[LabeledPrice(label='Пополнение', amount=stars_amount)],
            )

            logger.info(
                'Создан Stars invoice на звезд (~)',
                stars_amount=stars_amount,
                format_price=settings.format_price(amount_kopeks),
            )
            return invoice_link

        except Exception as error:
            logger.error('Ошибка создания Stars invoice', error=error)
            raise

    async def process_stars_payment(
        self,
        db: AsyncSession,
        user_id: int,
        stars_amount: int,
        payload: str,
        telegram_payment_charge_id: str,
    ) -> bool:
        """Финализирует платеж, пришедший из Telegram Stars, и обновляет баланс пользователя."""
        try:
            rubles_amount = TelegramStarsService.calculate_rubles_from_stars(stars_amount)
            amount_kopeks = int((rubles_amount * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))

            simple_payload = self._parse_simple_subscription_payload(
                payload,
                user_id,
            )

            transaction_description = (
                f'Оплата подписки через Telegram Stars ({stars_amount} ⭐)'
                if simple_payload
                else f'Пополнение через Telegram Stars ({stars_amount} ⭐)'
            )
            transaction_type = TransactionType.SUBSCRIPTION_PAYMENT if simple_payload else TransactionType.DEPOSIT

            transaction = await create_transaction(
                db=db,
                user_id=user_id,
                type=transaction_type,
                amount_kopeks=amount_kopeks,
                description=transaction_description,
                payment_method=PaymentMethod.TELEGRAM_STARS,
                external_id=telegram_payment_charge_id,
                is_completed=True,
            )

            user = await get_user_by_id(db, user_id)
            if not user:
                logger.error('Пользователь с ID не найден при обработке Stars платежа', user_id=user_id)
                return False

            if simple_payload:
                return await self._finalize_simple_subscription_stars_payment(
                    db=db,
                    user=user,
                    transaction=transaction,
                    amount_kopeks=amount_kopeks,
                    stars_amount=stars_amount,
                    payload_data=simple_payload,
                    telegram_payment_charge_id=telegram_payment_charge_id,
                )

            return await self._finalize_stars_balance_topup(
                db=db,
                user=user,
                transaction=transaction,
                amount_kopeks=amount_kopeks,
                stars_amount=stars_amount,
                telegram_payment_charge_id=telegram_payment_charge_id,
            )

        except Exception as error:
            logger.error('Ошибка обработки Stars платежа', error=error, exc_info=True)
            return False

    @staticmethod
    def _parse_simple_subscription_payload(
        payload: str,
        expected_user_id: int,
    ) -> _SimpleSubscriptionPayload | None:
        """Пытается извлечь параметры простой подписки из payload звёздного платежа."""

        prefix = 'simple_sub_'
        if not payload or not payload.startswith(prefix):
            return None

        tail = payload[len(prefix) :]
        parts = tail.split('_', 2)
        if len(parts) < 3:
            logger.warning('Payload Stars simple subscription имеет некорректный формат', payload=payload)
            return None

        user_part, subscription_part, period_part = parts

        try:
            payload_user_id = int(user_part)
        except ValueError:
            logger.warning('Не удалось разобрать user_id в payload Stars simple subscription', payload=payload)
            return None

        if payload_user_id != expected_user_id:
            logger.warning(
                'Получен payload Stars simple subscription с чужим user_id: (ожидался)',
                payload_user_id=payload_user_id,
                expected_user_id=expected_user_id,
            )
            return None

        try:
            subscription_id = int(subscription_part)
        except ValueError:
            logger.warning('Не удалось разобрать subscription_id в payload Stars simple subscription', payload=payload)
            return None

        period_days: int | None = None
        try:
            period_days = int(period_part)
        except ValueError:
            logger.warning('Не удалось разобрать период в payload Stars simple subscription', payload=payload)

        return _SimpleSubscriptionPayload(
            subscription_id=subscription_id,
            period_days=period_days,
        )

    async def _finalize_simple_subscription_stars_payment(
        self,
        db: AsyncSession,
        user,
        transaction,
        amount_kopeks: int,
        stars_amount: int,
        payload_data: _SimpleSubscriptionPayload,
        telegram_payment_charge_id: str,
    ) -> bool:
        """Активация простой подписки, оплаченной через Telegram Stars."""

        period_days = payload_data.period_days or settings.SIMPLE_SUBSCRIPTION_PERIOD_DAYS
        pending_subscription = None

        if payload_data.subscription_id is not None:
            try:
                from sqlalchemy import select

                from app.database.models import Subscription

                result = await db.execute(
                    select(Subscription).where(
                        Subscription.id == payload_data.subscription_id,
                        Subscription.user_id == user.id,
                    )
                )
                pending_subscription = result.scalar_one_or_none()
            except Exception as lookup_error:  # pragma: no cover - диагностический лог
                logger.error(
                    'Ошибка поиска pending подписки для пользователя',
                    subscription_id=payload_data.subscription_id,
                    user_id=user.id,
                    lookup_error=lookup_error,
                    exc_info=True,
                )
                pending_subscription = None

            if not pending_subscription:
                logger.error(
                    'Не найдена pending подписка для пользователя',
                    subscription_id=payload_data.subscription_id,
                    user_id=user.id,
                )
                return False

            if payload_data.period_days is None:
                start_point = pending_subscription.start_date or datetime.now(UTC)
                end_point = pending_subscription.end_date or start_point
                computed_days = max(1, (end_point - start_point).days or 0)
                period_days = max(period_days, computed_days)

        try:
            from app.database.crud.subscription import activate_pending_subscription

            subscription = await activate_pending_subscription(
                db=db,
                user_id=user.id,
                period_days=period_days,
            )
        except Exception as error:
            logger.error(
                'Ошибка активации pending подписки для пользователя', user_id=user.id, error=error, exc_info=True
            )
            return False

        if not subscription:
            logger.error('Не удалось активировать pending подписку пользователя', user_id=user.id)
            return False

        try:
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            remnawave_user = await subscription_service.create_remnawave_user(
                db,
                subscription,
            )
            if remnawave_user:
                await db.refresh(subscription)
        except Exception as sync_error:  # pragma: no cover - диагностический лог
            logger.error(
                'Ошибка синхронизации подписки с RemnaWave для пользователя',
                user_id=user.id,
                sync_error=sync_error,
                exc_info=True,
            )

        period_display = period_days
        if not period_display and getattr(subscription, 'start_date', None) and getattr(subscription, 'end_date', None):
            period_display = max(1, (subscription.end_date - subscription.start_date).days or 0)
        if not period_display:
            period_display = settings.SIMPLE_SUBSCRIPTION_PERIOD_DAYS

        # Отправляем уведомление только Telegram-пользователям (Stars требует Telegram)
        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                from aiogram import types

                from app.localization.texts import get_texts

                get_texts(user.language)
                traffic_limit = getattr(subscription, 'traffic_limit_gb', 0) or 0
                traffic_label = 'Безлимит' if traffic_limit == 0 else f'{int(traffic_limit)} ГБ'

                success_message = (
                    '✅ <b>Подписка успешно активирована!</b>\n\n'
                    f'📅 Период: {period_display} дней\n'
                    f'📱 Устройства: {getattr(subscription, "device_limit", 1)}\n'
                    f'📊 Трафик: {traffic_label}\n'
                    f'⭐ Оплата: {stars_amount} ⭐ ({settings.format_price(amount_kopeks)})\n\n'
                    "🔗 Для подключения перейдите в раздел 'Моя подписка'"
                )

                keyboard = types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text='📱 Моя подписка',
                                callback_data='menu_subscription',
                            )
                        ],
                        [
                            types.InlineKeyboardButton(
                                text='🏠 Главное меню',
                                callback_data='back_to_menu',
                            )
                        ],
                    ]
                )

                await self.bot.send_message(
                    chat_id=user.telegram_id,
                    text=success_message,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
                logger.info(
                    '✅ Пользователь получил уведомление об оплате подписки через Stars', telegram_id=user.telegram_id
                )
            except Exception as error:  # pragma: no cover - диагностический лог
                logger.error('Ошибка отправки уведомления о подписке через Stars', error=error, exc_info=True)

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_subscription_purchase_notification(
                    db,
                    user,
                    subscription,
                    transaction,
                    period_display,
                    was_trial_conversion=False,
                )
            except Exception as admin_error:  # pragma: no cover - диагностический лог
                logger.error(
                    'Ошибка уведомления администраторов о подписке через Stars', admin_error=admin_error, exc_info=True
                )

        # Начисляем реферальную комиссию за прямую покупку подписки
        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as ref_error:
            logger.error('Ошибка реферального начисления при покупке подписки через Stars', ref_error=ref_error)

        logger.info(
            '✅ Обработан Stars платеж как покупка подписки: пользователь , звезд →',
            user_id=user.id,
            stars_amount=stars_amount,
            format_price=settings.format_price(amount_kopeks),
        )
        return True

    async def _finalize_stars_balance_topup(
        self,
        db: AsyncSession,
        user,
        transaction,
        amount_kopeks: int,
        stars_amount: int,
        telegram_payment_charge_id: str,
    ) -> bool:
        """Начисляет баланс пользователю после оплаты Stars и запускает автопокупку."""

        # Запоминаем старые значения, чтобы корректно построить уведомления.
        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        # Обновляем баланс в БД.
        user.balance_kopeks += amount_kopeks
        user.updated_at = datetime.now(UTC)

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)
        topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'

        await db.commit()

        description_for_referral = f'Пополнение Stars: {settings.format_price(amount_kopeks)} ({stars_amount} ⭐)'
        logger.info(
            "🔍 Проверка реферальной логики для описания: ''", description_for_referral=description_for_referral
        )

        lower_description = description_for_referral.lower()
        contains_allowed_keywords = any(
            word in lower_description for word in ['пополнение', 'stars', 'yookassa', 'topup']
        )
        contains_forbidden_keywords = any(word in lower_description for word in ['комиссия', 'бонус'])
        allow_referral = contains_allowed_keywords and not contains_forbidden_keywords

        if allow_referral:
            logger.info('🔞 Вызов process_referral_topup для пользователя', user_id=user.id)
            try:
                from app.services.referral_service import process_referral_topup

                await process_referral_topup(
                    db,
                    user.id,
                    amount_kopeks,
                    getattr(self, 'bot', None),
                )
            except Exception as error:  # pragma: no cover - диагностический лог
                logger.error('Ошибка обработки реферального пополнения', error=error)
        else:
            logger.info(
                "❌ Описание '' не подходит для реферальной логики", description_for_referral=description_for_referral
            )

        if was_first_topup and not user.has_made_first_topup:
            user.has_made_first_topup = True
            await db.commit()

        await db.refresh(user)

        logger.info(
            '💰 Баланс пользователя изменен: → (Δ +)',
            telegram_id=user.telegram_id,
            old_balance=old_balance,
            balance_kopeks=user.balance_kopeks,
            amount_kopeks=amount_kopeks,
        )

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:  # pragma: no cover - диагностический лог
                logger.error('Ошибка отправки уведомления о пополнении Stars', error=error, exc_info=True)

        # Проверяем наличие сохраненной корзины для возврата к оформлению подписки
        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:  # pragma: no cover - диагностический лог
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя', user_id=user.id, error=error, exc_info=True
            )

        logger.info(
            '✅ Обработан Stars платеж: пользователь , звезд →',
            user_id=user.id,
            stars_amount=stars_amount,
            format_price=settings.format_price(amount_kopeks),
        )
        return True
