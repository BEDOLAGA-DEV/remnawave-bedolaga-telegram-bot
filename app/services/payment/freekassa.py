"""Mixin для интеграции с Freekassa."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.freekassa_service import freekassa_service
from app.services.subscription_auto_purchase_service import (
    auto_activate_subscription_after_topup,
    auto_purchase_saved_cart_after_topup,
)
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class FreekassaPaymentMixin:
    """Mixin для работы с платежами Freekassa."""

    async def create_freekassa_payment(
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
        Создает платеж Freekassa.

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
        if not settings.is_freekassa_enabled():
            logger.error('Freekassa не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.FREEKASSA_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Freekassa: сумма %s меньше минимальной %s',
                amount_kopeks,
                settings.FREEKASSA_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.FREEKASSA_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Freekassa: сумма %s больше максимальной %s',
                amount_kopeks,
                settings.FREEKASSA_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Генерируем уникальный order_id
        order_id = f'fk_{user_id}_{uuid.uuid4().hex[:12]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.FREEKASSA_CURRENCY

        # Срок действия платежа
        expires_at = datetime.utcnow() + timedelta(seconds=settings.FREEKASSA_PAYMENT_TIMEOUT_SECONDS)

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            # Выбираем способ создания платежа: API или форма
            if settings.FREEKASSA_USE_API:
                # Используем API для создания заказа (нужно для NSPK СБП)
                payment_url = await freekassa_service.create_order_and_get_url(
                    order_id=order_id,
                    amount=amount_rubles,
                    currency=currency,
                    email=email,
                    payment_system_id=settings.FREEKASSA_PAYMENT_SYSTEM_ID,
                )
                logger.info(
                    'Freekassa API: создан заказ order_id=%s, url=%s',
                    order_id,
                    payment_url,
                )
            else:
                # Генерируем URL для формы оплаты (стандартный способ)
                payment_url = freekassa_service.build_payment_url(
                    order_id=order_id,
                    amount=amount_rubles,
                    currency=currency,
                    email=email,
                    lang=language,
                )

            # Импортируем CRUD модуль
            freekassa_crud = import_module('app.database.crud.freekassa')

            # Сохраняем в БД
            local_payment = await freekassa_crud.create_freekassa_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                expires_at=expires_at,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )

            logger.info(
                'Freekassa: создан платеж order_id=%s, user_id=%s, amount=%s %s, use_api=%s',
                order_id,
                user_id,
                amount_rubles,
                currency,
                settings.FREEKASSA_USE_API,
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
            logger.exception('Freekassa: ошибка создания платежа: %s', e)
            return None

    async def process_freekassa_webhook(
        self,
        db: AsyncSession,
        *,
        merchant_id: int,
        amount: float,
        order_id: str,
        sign: str,
        intid: str,
        cur_id: int | None = None,
        client_ip: str,
    ) -> bool:
        """
        Обрабатывает webhook от Freekassa.

        Args:
            db: Сессия БД
            merchant_id: ID магазина (MERCHANT_ID)
            amount: Сумма платежа (AMOUNT)
            order_id: Номер заказа (MERCHANT_ORDER_ID)
            sign: Подпись (SIGN)
            intid: ID транзакции Freekassa
            cur_id: ID валюты/платежной системы (CUR_ID)
            client_ip: IP клиента

        Returns:
            True если платеж успешно обработан
        """
        try:
            # Проверка IP
            if not freekassa_service.verify_webhook_ip(client_ip):
                logger.warning('Freekassa webhook: недоверенный IP %s', client_ip)
                return False

            # Проверка подписи
            if not freekassa_service.verify_webhook_signature(merchant_id, amount, order_id, sign):
                logger.warning('Freekassa webhook: неверная подпись для order_id=%s', order_id)
                return False

            # Импортируем CRUD модуль
            freekassa_crud = import_module('app.database.crud.freekassa')

            # Получаем платеж из БД
            payment = await freekassa_crud.get_freekassa_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('Freekassa webhook: платеж не найден order_id=%s', order_id)
                return False

            # Проверка дублирования
            if payment.is_paid:
                logger.info('Freekassa webhook: платеж уже обработан order_id=%s', order_id)
                return True

            # Проверка суммы
            expected_amount = payment.amount_kopeks / 100
            if abs(amount - expected_amount) > 0.01:
                logger.warning(
                    'Freekassa webhook: несоответствие суммы ожидалось=%s, получено=%s',
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

            payment = await freekassa_crud.update_freekassa_payment_status(
                db=db,
                payment=payment,
                status='success',
                is_paid=True,
                freekassa_order_id=intid,
                payment_system_id=cur_id,
                callback_payload=callback_payload,
            )

            # Финализируем платеж (начисляем баланс, создаем транзакцию)
            return await self._finalize_freekassa_payment(db, payment, intid=intid, trigger='webhook')

        except Exception as e:
            logger.exception('Freekassa webhook: ошибка обработки: %s', e)
            return False

    async def _finalize_freekassa_payment(
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
                'Freekassa платеж %s уже привязан к транзакции (trigger=%s)',
                payment.order_id,
                trigger,
            )
            return True

        # Получаем пользователя
        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error(
                'Пользователь %s не найден для Freekassa платежа %s (trigger=%s)',
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
            description=f'Пополнение через Freekassa (#{intid or payment.order_id})',
            payment_method=PaymentMethod.FREEKASSA,
            external_id=str(intid) if intid else payment.order_id,
            is_completed=True,
        )

        # Связываем платеж с транзакцией
        freekassa_crud = import_module('app.database.crud.freekassa')
        await freekassa_crud.update_freekassa_payment_status(
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
            logger.error('Ошибка обработки реферального пополнения Freekassa: %s', error)

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
                logger.error('Ошибка отправки админ уведомления Freekassa: %s', error)

        # Отправка уведомления пользователю
        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                display_name = settings.get_freekassa_display_name()
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'💳 Способ: {display_name}\n'
                        f'🆔 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Freekassa: %s', error)

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
            '✅ Обработан Freekassa платеж %s для пользователя %s (trigger=%s)',
            payment.order_id,
            payment.user_id,
            trigger,
        )

        return True

    async def check_freekassa_payment_status(
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
            status_data = await freekassa_service.get_order_status(order_id)
            return status_data
        except Exception as e:
            logger.exception('Freekassa: ошибка проверки статуса: %s', e)
            return None

    async def get_freekassa_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        """
        Проверяет статус платежа Freekassa по локальному ID через API.
        """
        freekassa_crud = import_module('app.database.crud.freekassa')

        payment = await freekassa_crud.get_freekassa_payment_by_id(db, local_payment_id)
        if not payment:
            logger.warning('Freekassa payment not found: id=%s', local_payment_id)
            return None

        if payment.is_paid:
            return {
                'payment': payment,
                'status': 'success',
                'is_paid': True,
            }

        if not settings.FREEKASSA_API_KEY:
            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        try:
            # Запрашиваем статус заказа в Freekassa
            response = await freekassa_service.get_order_status(payment.order_id)

            # Freekassa возвращает список заказов
            orders = response.get('orders', [])
            target_order = None

            # Ищем наш заказ в списке
            for order in orders:
                # В ответе API поле называется merchant_order_id, а не paymentId
                # Поддерживаем оба варианта на всякий случай
                order_key = str(order.get('merchant_order_id') or order.get('paymentId'))
                if order_key == str(payment.order_id):
                    target_order = order
                    break

            if target_order:
                # Статус 1 = Оплачен
                fk_status = int(target_order.get('status', 0))

                if fk_status == 1:
                    logger.info('Freekassa payment %s confirmed via API', payment.order_id)

                    callback_payload = {
                        'check_source': 'api',
                        'fk_order_data': target_order,
                    }

                    # ID заказа на стороне FK (fk_order_id или id)
                    fk_intid = str(target_order.get('fk_order_id') or target_order.get('id'))

                    # Обновляем статус
                    payment = await freekassa_crud.update_freekassa_payment_status(
                        db=db,
                        payment=payment,
                        status='success',
                        is_paid=True,
                        freekassa_order_id=fk_intid,
                        payment_system_id=int(target_order.get('curID')) if target_order.get('curID') else None,
                        callback_payload=callback_payload,
                    )

                    # Финализируем
                    await self._finalize_freekassa_payment(
                        db,
                        payment,
                        intid=fk_intid,
                        trigger='api_check',
                    )
        except Exception as e:
            logger.error('Error checking Freekassa payment status: %s', e)

        return {
            'payment': payment,
            'status': payment.status or 'pending',
            'is_paid': payment.is_paid,
        }
