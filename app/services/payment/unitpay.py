"""Mixin для интеграции с UnitPay (unitpay.ru)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.unitpay_service import unitpay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class UnitPayPaymentMixin:
    """Mixin для работы с платежами UnitPay."""

    async def create_unitpay_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        payment_type: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Создает платеж UnitPay.

        Args:
            db: Сессия БД
            user_id: ID пользователя
            amount_kopeks: Сумма в копейках
            description: Описание платежа
            email: Email пользователя
            language: Язык интерфейса
            payment_type: Тип оплаты (card, sbp)

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_unitpay_enabled():
            logger.error('UnitPay не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.UNITPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'UnitPay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                min=settings.UNITPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.UNITPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'UnitPay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                max=settings.UNITPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id
        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
        else:
            user = None
        tg_id = user.telegram_id if user else (user_id or 'guest')

        # Генерируем уникальный order_id (account для UnitPay)
        order_id = f'up{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.UNITPAY_CURRENCY

        # Срок действия платежа (1 час)
        expires_at = datetime.now(UTC) + timedelta(hours=1)

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        # Тип оплаты
        pt = payment_type or settings.UNITPAY_PAYMENT_TYPE

        try:
            result = await unitpay_service.create_payment(
                account=order_id,
                amount=amount_rubles,
                description=description,
                payment_type=pt,
                currency=currency,
            )

            payment_url = result.get('redirectUrl')
            unitpay_payment_id = result.get('paymentId')

            if not payment_url:
                logger.error('UnitPay API не вернул URL платежа')
                return None

            logger.info('UnitPay API: создан заказ', order_id=order_id, payment_url=payment_url)

            # Импортируем CRUD модуль
            unitpay_crud = import_module('app.database.crud.unitpay')

            # Сохраняем в БД
            local_payment = await unitpay_crud.create_unitpay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_type=pt,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            # Save id before potential rollback (avoids MissingGreenlet on detached object)
            local_payment_id = local_payment.id

            # Сохраняем unitpay_payment_id если получили
            if unitpay_payment_id:
                try:
                    local_payment.unitpay_payment_id = unitpay_payment_id
                    await db.commit()
                    await db.refresh(local_payment)
                except Exception:
                    await db.rollback()
                    logger.warning(
                        'UnitPay: duplicate unitpay_payment_id, ignoring', unitpay_payment_id=unitpay_payment_id
                    )

            logger.info(
                'UnitPay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment_id,
            }

        except Exception as e:
            logger.exception('UnitPay: ошибка создания платежа', e=e)
            return None

    async def process_unitpay_webhook(
        self,
        db: AsyncSession,
        *,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Обрабатывает webhook от UnitPay.

        Args:
            db: Сессия БД
            method: Метод (check, pay, error)
            params: Параметры запроса

        Returns:
            Ответ для UnitPay (result или error)
        """
        try:
            account = params.get('account', '')  # Наш order_id

            # Проверка подписи (до любой обработки)
            signature = params.get('signature', '')
            if not unitpay_service.verify_webhook_signature(method, params, signature):
                # Тестовый запрос от UnitPay (проверка webhook URL) — только если подпись не прошла
                logger.warning('UnitPay webhook: неверная подпись', method=method)
                return {'error': {'message': 'Invalid signature'}}

            unitpay_id = params.get('unitpayId')
            try:
                order_sum = float(params.get('orderSum', 0))
            except (ValueError, TypeError):
                return {'error': {'message': 'Invalid orderSum'}}
            order_currency = params.get('orderCurrency', 'RUB')

            # Импортируем CRUD модуль
            unitpay_crud = import_module('app.database.crud.unitpay')

            # Находим платеж
            payment = await unitpay_crud.get_unitpay_payment_by_order_id(db, account)
            if not payment:
                logger.warning('UnitPay webhook: платеж не найден', account=account)
                return {'error': {'message': f'Payment not found: {account}'}}

            if method == 'check':
                # Предварительная проверка — проверяем сумму и валюту
                expected_amount = payment.amount_kopeks / 100
                if abs(order_sum - expected_amount) > 0.01:
                    logger.warning(
                        'UnitPay check: несоответствие суммы',
                        expected=expected_amount,
                        received=order_sum,
                    )
                    return {'error': {'message': 'Amount mismatch'}}

                logger.info('UnitPay check OK', account=account)
                return {'result': {'message': 'OK'}}

            if method == 'pay':
                # Lock payment row to prevent concurrent processing
                locked = await unitpay_crud.get_unitpay_payment_by_id_for_update(db, payment.id)
                if not locked:
                    logger.error('UnitPay webhook: не удалось заблокировать платёж', payment_id=payment.id)
                    return {'error': {'message': 'Lock failed'}}
                payment = locked

                # Idempotency check
                if payment.is_paid:
                    logger.info('UnitPay webhook: платеж уже обработан', account=account)
                    return {'result': {'message': 'Already processed'}}

                # Проверка суммы
                expected_amount = payment.amount_kopeks / 100
                if abs(order_sum - expected_amount) > 0.01:
                    logger.warning(
                        'UnitPay pay: несоответствие суммы',
                        expected=expected_amount,
                        received=order_sum,
                    )
                    return {'error': {'message': 'Amount mismatch'}}

                # Inline field updates — NO intermediate commit
                callback_payload = {
                    'method': method,
                    'unitpayId': unitpay_id,
                    'account': account,
                    'orderSum': order_sum,
                    'orderCurrency': order_currency,
                    'paymentType': params.get('paymentType'),
                    'date': params.get('date'),
                }
                payment.status = 'success'
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.callback_payload = callback_payload
                if unitpay_id:
                    payment.unitpay_payment_id = int(unitpay_id)
                payment.updated_at = datetime.now(UTC)
                await db.flush()

                # Финализируем платеж
                success = await self._finalize_unitpay_payment(
                    db, payment, unitpay_id=str(unitpay_id), trigger='webhook'
                )

                if success:
                    return {'result': {'message': 'OK'}}
                return {'error': {'message': 'Finalization failed'}}

            if method == 'error':
                error_msg = params.get('errorMessage', 'Unknown error')
                logger.warning('UnitPay error webhook', account=account, error=error_msg)
                # Не меняем статус — после error может прийти pay
                return {'result': {'message': 'Error received'}}

            return {'error': {'message': f'Unknown method: {method}'}}

        except Exception as e:
            logger.exception('UnitPay webhook: ошибка обработки', e=e)
            return {'error': {'message': 'Internal error'}}

    async def _finalize_unitpay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        unitpay_id: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления."""
        payment_module = import_module('app.services.payment_service')

        # Idempotency check
        if payment.transaction_id:
            logger.info('UnitPay платеж уже привязан к транзакции', order_id=payment.order_id, trigger=trigger)
            return True

        # --- Guest purchase flow ---
        up_metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=up_metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=unitpay_id or payment.order_id,
            provider_name='unitpay',
        )
        if guest_result is not None:
            return True

        # Получаем пользователя
        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error(
                'Пользователь не найден для UnitPay платежа',
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
            description=f'Пополнение через UnitPay (#{unitpay_id or payment.order_id})',
            payment_method=PaymentMethod.UNITPAY,
            external_id=unitpay_id or payment.order_id,
            is_completed=True,
            created_at=getattr(payment, 'created_at', None),
            commit=False,
        )

        # Связываем платеж с транзакцией
        payment.transaction_id = transaction.id
        payment.updated_at = datetime.now(UTC)
        await db.flush()

        # Lock user row
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        # Начисляем баланс (атомарно с has_made_first_topup)
        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        if was_first_topup and not user.has_made_first_topup:
            user.has_made_first_topup = True

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)
        topup_status = 'Первое пополнение' if was_first_topup else 'Пополнение'

        await db.commit()

        # Emit deferred side-effects
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.UNITPAY,
            external_id=unitpay_id or payment.order_id,
        )

        # Реферальное пополнение
        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, payment.amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения UnitPay', error=error)

        await db.refresh(user)
        await db.refresh(payment)

        # Уведомление админам
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
            except Exception as error:
                logger.error('Ошибка отправки админ уведомления UnitPay', error=error)

        # Уведомление пользователю
        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                display_name = settings.UNITPAY_DISPLAY_NAME

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
                logger.error('Ошибка отправки уведомления пользователю UnitPay', error=error)

        # Автопокупка подписки
        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка при работе с сохраненной корзиной', user_id=user.id, error=error, exc_info=True)

        logger.info(
            '✅ Обработан UnitPay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def get_unitpay_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        """
        Проверяет статус платежа UnitPay по локальному ID через API.
        Если платёж оплачен — автоматически начисляет баланс.
        """
        logger.info('UnitPay: checking payment status', local_payment_id=local_payment_id)
        unitpay_crud = import_module('app.database.crud.unitpay')

        payment = await unitpay_crud.get_unitpay_payment_by_id(db, local_payment_id)
        if not payment:
            logger.warning('UnitPay payment not found', local_payment_id=local_payment_id)
            return None

        if payment.is_paid:
            return {
                'payment': payment,
                'status': 'success',
                'is_paid': True,
            }

        if not payment.unitpay_payment_id:
            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        try:
            result = await unitpay_service.get_payment_info(payment.unitpay_payment_id)
            up_status = result.get('status', '')

            if up_status == 'success':
                logger.info('UnitPay payment confirmed via API', order_id=payment.order_id)

                locked = await unitpay_crud.get_unitpay_payment_by_id_for_update(db, payment.id)
                if not locked:
                    logger.error('UnitPay status check: не удалось заблокировать', payment_id=payment.id)
                elif locked.is_paid:
                    logger.info('UnitPay платеж уже оплачен', order_id=locked.order_id)
                    payment = locked
                else:
                    payment = locked

                    payment.status = 'success'
                    payment.is_paid = True
                    payment.paid_at = datetime.now(UTC)
                    payment.callback_payload = {'check_source': 'api', 'api_result': result}
                    payment.updated_at = datetime.now(UTC)
                    await db.flush()

                    await self._finalize_unitpay_payment(
                        db,
                        payment,
                        unitpay_id=str(payment.unitpay_payment_id),
                        trigger='api_check',
                    )
        except Exception as e:
            logger.error('Error checking UnitPay payment status', e=e)

        return {
            'payment': payment,
            'status': payment.status or 'pending',
            'is_paid': payment.is_paid,
        }
