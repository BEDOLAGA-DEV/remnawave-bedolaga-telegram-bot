"""Mixin для интеграции с KassaAI (api.fk.life)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.kassa_ai_service import kassa_ai_service
from app.services.subscription_auto_purchase_service import (
    auto_activate_subscription_after_topup,
    auto_purchase_saved_cart_after_topup,
)
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class KassaAiPaymentMixin:
    """Mixin для работы с платежами KassaAI."""

    async def create_kassa_ai_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
    ) -> dict[str, Any] | None:
        """
        Создает платеж KassaAI.

        Args:
            db: Сессия БД
            user_id: ID пользователя
            amount_kopeks: Сумма в копейках
            description: Описание платежа
            email: Email пользователя
            language: Язык интерфейса

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_kassa_ai_enabled():
            logger.error('KassaAI не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.KASSA_AI_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'KassaAI: сумма %s меньше минимальной %s',
                amount_kopeks,
                settings.KASSA_AI_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.KASSA_AI_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'KassaAI: сумма %s больше максимальной %s',
                amount_kopeks,
                settings.KASSA_AI_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id
        payment_module = import_module('app.services.payment_service')
        user = await payment_module.get_user_by_id(db, user_id)
        tg_id = user.telegram_id if user else user_id

        # Генерируем уникальный order_id с telegram_id для удобного поиска
        order_id = f'k{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.KASSA_AI_CURRENCY

        # Срок действия платежа (1 час по умолчанию)
        expires_at = datetime.utcnow() + timedelta(hours=1)

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            # Используем API для создания заказа
            result = await kassa_ai_service.create_order(
                order_id=order_id,
                amount=amount_rubles,
                currency=currency,
                email=email,
                payment_system_id=settings.KASSA_AI_PAYMENT_SYSTEM_ID,
            )

            payment_url = result.get('location')
            if not payment_url:
                logger.error('KassaAI API не вернул URL платежа')
                return None

            logger.info(
                'KassaAI API: создан заказ order_id=%s, url=%s',
                order_id,
                payment_url,
            )

            # Импортируем CRUD модуль
            kassa_ai_crud = import_module('app.database.crud.kassa_ai')

            # Сохраняем в БД
            local_payment = await kassa_ai_crud.create_kassa_ai_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_system_id=settings.KASSA_AI_PAYMENT_SYSTEM_ID,
                expires_at=expires_at,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )

            logger.info(
                'KassaAI: создан платеж order_id=%s, user_id=%s, amount=%s %s',
                order_id,
                user_id,
                amount_rubles,
                currency,
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('KassaAI: ошибка создания платежа: %s', e)
            return None

    async def process_kassa_ai_webhook(
        self,
        db: AsyncSession,
        *,
        merchant_id: int,
        amount: float,
        order_id: str,
        sign: str,
        intid: str,
        cur_id: int | None = None,
    ) -> bool:
        """
        Обрабатывает webhook от KassaAI.

        Args:
            db: Сессия БД
            merchant_id: ID магазина (MERCHANT_ID)
            amount: Сумма платежа (AMOUNT)
            order_id: Номер заказа (MERCHANT_ORDER_ID)
            sign: Подпись (SIGN)
            intid: ID транзакции KassaAI
            cur_id: ID валюты/платежной системы (CUR_ID)

        Returns:
            True если платеж успешно обработан
        """
        try:
            # Проверка подписи
            if not kassa_ai_service.verify_webhook_signature(merchant_id, amount, order_id, sign):
                logger.warning('KassaAI webhook: неверная подпись для order_id=%s', order_id)
                return False

            # Импортируем CRUD модуль
            kassa_ai_crud = import_module('app.database.crud.kassa_ai')

            # Получаем платеж из БД
            payment = await kassa_ai_crud.get_kassa_ai_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('KassaAI webhook: платеж не найден order_id=%s', order_id)
                return False

            # Проверка дублирования
            if payment.is_paid:
                logger.info('KassaAI webhook: платеж уже обработан order_id=%s', order_id)
                return True

            # Проверка суммы
            expected_amount = payment.amount_kopeks / 100
            if abs(amount - expected_amount) > 0.01:
                logger.warning(
                    'KassaAI webhook: несоответствие суммы ожидалось=%s, получено=%s',
                    expected_amount,
                    amount,
                )
                return False

            # Обновляем статус платежа
            callback_payload = {
                'merchant_id': merchant_id,
                'amount': amount,
                'order_id': order_id,
                'intid': intid,
                'cur_id': cur_id,
            }

            payment = await kassa_ai_crud.update_kassa_ai_payment_status(
                db=db,
                payment=payment,
                status='success',
                is_paid=True,
                kassa_ai_order_id=intid,
                payment_system_id=cur_id,
                callback_payload=callback_payload,
            )

            # Финализируем платеж (начисляем баланс, создаем транзакцию)
            return await self._finalize_kassa_ai_payment(db, payment, intid=intid, trigger='webhook')

        except Exception as e:
            logger.exception('KassaAI webhook: ошибка обработки: %s', e)
            return False

    async def _finalize_kassa_ai_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        intid: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления."""
        payment_module = import_module('app.services.payment_service')

        if payment.transaction_id:
            logger.info(
                'KassaAI платеж %s уже привязан к транзакции (trigger=%s)',
                payment.order_id,
                trigger,
            )
            return True

        # Получаем пользователя
        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error(
                'Пользователь %s не найден для KassaAI платежа %s (trigger=%s)',
                payment.user_id,
                payment.order_id,
                trigger,
            )
            return False

        # Создаем транзакцию
        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment.amount_kopeks,
            description=f'Пополнение через KassaAI (#{intid or payment.order_id})',
            payment_method=PaymentMethod.KASSA_AI,
            external_id=str(intid) if intid else payment.order_id,
            is_completed=True,
        )

        # Связываем платеж с транзакцией
        kassa_ai_crud = import_module('app.database.crud.kassa_ai')
        await kassa_ai_crud.update_kassa_ai_payment_status(
            db=db,
            payment=payment,
            status=payment.status,
            transaction_id=transaction.id,
        )

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        # Начисляем баланс
        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.utcnow()

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)
        topup_status = 'Первое пополнение' if was_first_topup else 'Пополнение'

        await db.commit()

        # Обработка реферального пополнения
        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, payment.amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения KassaAI: %s', error)

        if was_first_topup and not user.has_made_first_topup:
            user.has_made_first_topup = True
            await db.commit()

        await db.refresh(user)
        await db.refresh(payment)

        # Отправка уведомления админам
        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import (
                    AdminNotificationService,
                )

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
            except Exception as error:
                logger.error('Ошибка отправки админ уведомления KassaAI: %s', error)

        # Отправка уведомления пользователю (только Telegram-пользователям)
        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                display_name = settings.get_kassa_ai_display_name()

                if settings.SHOW_ACTIVATION_PROMPT_AFTER_TOPUP:
                    # Яркое сообщение для тупых
                    from aiogram import types

                    message = (
                        '✅ <b>Платеж успешно завершен!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'💳 Способ: {display_name}\n\n'
                        '💎 Средства зачислены на ваш баланс!\n\n'
                        '‼️ <b>ВНИМАНИЕ! ОБЯЗАТЕЛЬНО АКТИВИРУЙТЕ ПОДПИСКУ!</b> ‼️\n\n'
                        '⚠️ Пополнение баланса <b>НЕ АКТИВИРУЕТ</b> подписку автоматически!\n\n'
                        '👇 <b>НАЖМИТЕ КНОПКУ НИЖЕ ДЛЯ АКТИВАЦИИ</b> 👇'
                    )
                    keyboard = types.InlineKeyboardMarkup(
                        inline_keyboard=[
                            [types.InlineKeyboardButton(text='🔥 АКТИВИРОВАТЬ ПОДПИСКУ', callback_data='menu_buy')],
                        ]
                    )
                else:
                    # Стандартное сообщение (как было раньше)
                    keyboard = await self.build_topup_success_keyboard(user)
                    message = (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'💳 Способ: {display_name}\n'
                        f'🆔 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    )

                await self.bot.send_message(
                    user.telegram_id,
                    message,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю KassaAI: %s', error)

        # Автопокупка подписки
        try:
            from aiogram import types

            from app.services.user_cart_service import user_cart_service

            has_saved_cart = await user_cart_service.has_user_cart(user.id)
            auto_purchase_success = False

            if has_saved_cart:
                try:
                    auto_purchase_success = await auto_purchase_saved_cart_after_topup(
                        db,
                        user,
                        bot=getattr(self, 'bot', None),
                    )
                except Exception as auto_error:
                    logger.error(
                        'Ошибка автоматической покупки подписки для пользователя %s: %s',
                        user.id,
                        auto_error,
                        exc_info=True,
                    )

                if auto_purchase_success:
                    has_saved_cart = False

            # Умная автоактивация если автопокупка не сработала
            activation_notification_sent = False
            if not auto_purchase_success:
                try:
                    _, activation_notification_sent = await auto_activate_subscription_after_topup(
                        db, user, bot=getattr(self, 'bot', None), topup_amount=payment.amount_kopeks
                    )
                except Exception as auto_activate_error:
                    logger.error(
                        'Ошибка умной автоактивации для пользователя %s: %s',
                        user.id,
                        auto_activate_error,
                        exc_info=True,
                    )

            # Отправляем уведомление только если его ещё не отправили
            if has_saved_cart and getattr(self, 'bot', None) and not activation_notification_sent and user.telegram_id:
                from app.localization.texts import get_texts

                texts = get_texts(user.language)
                cart_message = texts.t(
                    'BALANCE_TOPUP_CART_REMINDER',
                    'У вас есть незавершенное оформление подписки. Вернуться?',
                )

                keyboard = types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t(
                                    'BALANCE_TOPUP_CART_BUTTON',
                                    '🛒 Продолжить оформление',
                                ),
                                callback_data='return_to_saved_cart',
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
                    text=(f'✅ Баланс пополнен на {settings.format_price(payment.amount_kopeks)}!\n\n{cart_message}'),
                    reply_markup=keyboard,
                )
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя %s: %s',
                user.id,
                error,
                exc_info=True,
            )

        logger.info(
            '✅ Обработан KassaAI платеж %s для пользователя %s (trigger=%s)',
            payment.order_id,
            payment.user_id,
            trigger,
        )

        return True

    async def check_kassa_ai_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """
        Проверяет статус платежа через API.

        Args:
            db: Сессия БД
            order_id: Номер заказа

        Returns:
            Данные о статусе платежа
        """
        try:
            status_data = await kassa_ai_service.get_order_status(order_id)
            return status_data
        except Exception as e:
            logger.exception('KassaAI: ошибка проверки статуса: %s', e)
            return None

    async def get_kassa_ai_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        """
        Проверяет статус платежа KassaAI по локальному ID через API.
        Если платёж оплачен — автоматически начисляет баланс.
        """
        logger.info('KassaAI: checking payment status for id=%s', local_payment_id)
        kassa_ai_crud = import_module('app.database.crud.kassa_ai')

        payment = await kassa_ai_crud.get_kassa_ai_payment_by_id(db, local_payment_id)
        if not payment:
            logger.warning('KassaAI payment not found: id=%s', local_payment_id)
            return None

        if payment.is_paid:
            return {
                'payment': payment,
                'status': 'success',
                'is_paid': True,
            }

        if not settings.KASSA_AI_API_KEY:
            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        try:
            # Запрашиваем статус заказа в KassaAI (api.fk.life)
            response = await kassa_ai_service.get_order_status(payment.order_id)

            # KassaAI возвращает список заказов (как Freekassa)
            orders = response.get('orders', [])
            target_order = None

            # Ищем наш заказ в списке
            for order in orders:
                order_key = str(order.get('merchant_order_id') or order.get('paymentId'))
                if order_key == str(payment.order_id):
                    target_order = order
                    break

            if target_order:
                # Статус 1 = Оплачен (как в Freekassa)
                kai_status = int(target_order.get('status', 0))

                if kai_status == 1:
                    logger.info('KassaAI payment %s confirmed via API', payment.order_id)

                    callback_payload = {
                        'check_source': 'api',
                        'kai_order_data': target_order,
                    }

                    # ID заказа на стороне KassaAI
                    kai_intid = str(target_order.get('fk_order_id') or target_order.get('id'))

                    # Обновляем статус
                    payment = await kassa_ai_crud.update_kassa_ai_payment_status(
                        db=db,
                        payment=payment,
                        status='success',
                        is_paid=True,
                        kassa_ai_order_id=kai_intid,
                        payment_system_id=int(target_order.get('curID')) if target_order.get('curID') else None,
                        callback_payload=callback_payload,
                    )

                    # Финализируем (начисляем баланс)
                    await self._finalize_kassa_ai_payment(
                        db,
                        payment,
                        intid=kai_intid,
                        trigger='api_check',
                    )
        except Exception as e:
            logger.error('Error checking KassaAI payment status: %s', e)

        return {
            'payment': payment,
            'status': payment.status or 'pending',
            'is_paid': payment.is_paid,
        }
