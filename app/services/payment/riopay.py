"""Mixin для интеграции с RioPay (api.riopay.online)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.riopay_service import riopay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов RioPay → internal
RIOPAY_STATUS_MAP = {
    'COMPLETED': ('success', True),
    'CANCELED': ('canceled', False),
    'FAILED': ('failed', False),
    'EXPIRED': ('expired', False),
    'CREATED': ('pending', False),
    'PENDING': ('pending', False),
}


class RioPayPaymentMixin:
    """Mixin для работы с платежами RioPay."""

    async def create_riopay_payment(
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
        Создает платеж RioPay.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_riopay_enabled():
            logger.error('RioPay не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.RIOPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'RioPay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                RIOPAY_MIN_AMOUNT_KOPEKS=settings.RIOPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.RIOPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'RioPay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                RIOPAY_MAX_AMOUNT_KOPEKS=settings.RIOPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id
        payment_module = import_module('app.services.payment_service')
        user = await payment_module.get_user_by_id(db, user_id)
        tg_id = user.telegram_id if user else user_id

        # Генерируем уникальный order_id с telegram_id для удобного поиска
        order_id = f'rp{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.RIOPAY_CURRENCY

        # Срок действия платежа (1 час по умолчанию)
        expires_at = datetime.now(UTC) + timedelta(hours=1)

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
            result = await riopay_service.create_order(
                amount=amount_rubles,
                currency=currency,
                external_id=order_id,
                purpose=description,
                success_url=settings.RIOPAY_SUCCESS_URL,
                fail_url=settings.RIOPAY_FAIL_URL,
            )

            payment_url = result.get('paymentLink')
            riopay_order_id = result.get('id')

            if not payment_url:
                logger.error('RioPay API не вернул URL платежа', result=result)
                return None

            logger.info('RioPay API: создан заказ', order_id=order_id, riopay_order_id=riopay_order_id, payment_url=payment_url)

            # Импортируем CRUD модуль
            riopay_crud = import_module('app.database.crud.riopay')

            # Сохраняем в БД
            local_payment = await riopay_crud.create_riopay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                riopay_order_id=riopay_order_id,
                payment_method=result.get('paymentType'),
                expires_at=expires_at,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )

            logger.info(
                'RioPay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'riopay_order_id': riopay_order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('RioPay: ошибка создания платежа', e=e)
            return None

    async def process_riopay_webhook(
        self,
        db: AsyncSession,
        *,
        payload: dict[str, Any],
        raw_body: bytes,
        signature: str | None = None,
    ) -> bool:
        """
        Обрабатывает webhook от RioPay.

        Args:
            db: Сессия БД
            payload: JSON тело webhook
            raw_body: Сырое тело для проверки подписи
            signature: Подпись из заголовка

        Returns:
            True если платеж успешно обработан
        """
        try:
            # Проверка подписи (обязательна)
            if not signature:
                logger.warning('RioPay webhook: отсутствует подпись')
                return False

            if not riopay_service.verify_webhook_signature(raw_body, signature):
                logger.warning('RioPay webhook: неверная подпись')
                return False

            # Извлекаем данные из payload
            riopay_order_id = payload.get('id')
            external_id = payload.get('externalId')
            riopay_status = payload.get('status')
            amount = payload.get('amount')

            if not riopay_order_id or not riopay_status:
                logger.warning('RioPay webhook: отсутствуют обязательные поля', payload=payload)
                return False

            # Импортируем CRUD модуль
            riopay_crud = import_module('app.database.crud.riopay')

            # Ищем платеж по external_id (наш order_id) или riopay_order_id
            payment = None
            if external_id:
                payment = await riopay_crud.get_riopay_payment_by_order_id(db, external_id)
            if not payment and riopay_order_id:
                payment = await riopay_crud.get_riopay_payment_by_riopay_order_id(db, riopay_order_id)

            if not payment:
                logger.warning(
                    'RioPay webhook: платеж не найден',
                    external_id=external_id,
                    riopay_order_id=riopay_order_id,
                )
                return False

            # Проверка дублирования
            if payment.is_paid:
                logger.info('RioPay webhook: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = RIOPAY_STATUS_MAP.get(riopay_status, ('pending', False))
            internal_status, is_paid = status_info

            # Обновляем статус платежа
            callback_payload = {
                'riopay_order_id': riopay_order_id,
                'external_id': external_id,
                'status': riopay_status,
                'amount': amount,
                'payment_type': payload.get('paymentType'),
                'included_fee': payload.get('includedFee'),
                'raw_payload': payload,
            }

            payment = await riopay_crud.update_riopay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=is_paid,
                riopay_order_id=riopay_order_id,
                payment_method=payload.get('paymentType'),
                callback_payload=callback_payload,
            )

            # Финализируем платеж если оплачен
            if is_paid:
                return await self._finalize_riopay_payment(
                    db, payment, riopay_order_id=riopay_order_id, trigger='webhook'
                )

            return True

        except Exception as e:
            logger.exception('RioPay webhook: ошибка обработки', e=e)
            return False

    async def _finalize_riopay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        riopay_order_id: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления."""
        payment_module = import_module('app.services.payment_service')

        if payment.transaction_id:
            logger.info(
                'RioPay платеж уже привязан к транзакции (trigger=)', order_id=payment.order_id, trigger=trigger
            )
            return True

        # Получаем пользователя
        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error(
                'Пользователь не найден для RioPay платежа (trigger=)',
                user_id=payment.user_id,
                order_id=payment.order_id,
                trigger=trigger,
            )
            return False

        # Создаем транзакцию
        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment.amount_kopeks,
            description=f'Пополнение через RioPay (#{riopay_order_id or payment.order_id})',
            payment_method=PaymentMethod.RIOPAY,
            external_id=str(riopay_order_id) if riopay_order_id else payment.order_id,
            is_completed=True,
            created_at=getattr(payment, 'created_at', None),
        )

        # Связываем платеж с транзакцией
        riopay_crud = import_module('app.database.crud.riopay')
        await riopay_crud.update_riopay_payment_status(
            db=db,
            payment=payment,
            status=payment.status,
            transaction_id=transaction.id,
        )

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        # Начисляем баланс
        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)

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
            logger.error('Ошибка обработки реферального пополнения RioPay', error=error)

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
                logger.error('Ошибка отправки админ уведомления RioPay', error=error)

        # Отправка уведомления пользователю (только Telegram-пользователям)
        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                display_name = settings.get_riopay_display_name()

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
                logger.error('Ошибка отправки уведомления пользователю RioPay', error=error)

        # Автопокупка подписки и уведомление о корзине
        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя', user_id=user.id, error=error, exc_info=True
            )

        logger.info(
            '✅ Обработан RioPay платеж для пользователя (trigger=)',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_riopay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """
        Проверяет статус платежа через API.
        """
        try:
            riopay_crud = import_module('app.database.crud.riopay')

            payment = await riopay_crud.get_riopay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('RioPay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {
                    'payment': payment,
                    'status': 'success',
                    'is_paid': True,
                }

            # Проверяем через API по riopay_order_id (UUID)
            if payment.riopay_order_id:
                try:
                    order_data = await riopay_service.get_order(payment.riopay_order_id)
                    riopay_status = order_data.get('status')

                    if riopay_status:
                        status_info = RIOPAY_STATUS_MAP.get(riopay_status, ('pending', False))
                        internal_status, is_paid = status_info

                        if is_paid:
                            logger.info('RioPay payment confirmed via API', order_id=payment.order_id)

                            callback_payload = {
                                'check_source': 'api',
                                'riopay_order_data': order_data,
                            }

                            payment = await riopay_crud.update_riopay_payment_status(
                                db=db,
                                payment=payment,
                                status='success',
                                is_paid=True,
                                riopay_order_id=payment.riopay_order_id,
                                payment_method=order_data.get('paymentType'),
                                callback_payload=callback_payload,
                            )

                            await self._finalize_riopay_payment(
                                db,
                                payment,
                                riopay_order_id=payment.riopay_order_id,
                                trigger='api_check',
                            )
                        elif internal_status != payment.status:
                            # Обновляем статус если изменился
                            payment = await riopay_crud.update_riopay_payment_status(
                                db=db,
                                payment=payment,
                                status=internal_status,
                            )

                except Exception as e:
                    logger.error('Error checking RioPay payment status via API', e=e)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        except Exception as e:
            logger.exception('RioPay: ошибка проверки статуса', e=e)
            return None
