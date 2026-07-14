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
        payment_type: str | None = None,
        enable_subscription: bool = False,
        email: str | None = None,
    ) -> dict[str, Any] | None:
        if not settings.is_unitpay_enabled():
            logger.error('UnitPay не настроен')
            return None

        if amount_kopeks < settings.UNITPAY_MIN_AMOUNT_KOPEKS:
            logger.warning('UnitPay: сумма меньше минимальной', amount_kopeks=amount_kopeks)
            return None
        if amount_kopeks > settings.UNITPAY_MAX_AMOUNT_KOPEKS:
            logger.warning('UnitPay: сумма больше максимальной', amount_kopeks=amount_kopeks)
            return None

        payment_module = import_module('app.services.payment_service')
        user = await payment_module.get_user_by_id(db, user_id) if user_id else None
        tg_id = user.telegram_id if user else (user_id or 'guest')
        customer_email = email or (getattr(user, 'email', None) if user else None)

        order_id = f'up{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        effective_type = payment_type or settings.UNITPAY_PAYMENT_TYPE or 'sbp'
        expires_at = datetime.now(UTC) + timedelta(hours=1)

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'type': 'balance_topup',
            'enable_subscription': enable_subscription,
        }

        try:
            result = await unitpay_service.init_payment(
                order_id=order_id,
                amount_rubles=amount_rubles,
                desc=description,
                account=str(tg_id),
                payment_type=effective_type,
                currency=settings.UNITPAY_CURRENCY,
                result_url=settings.get_unitpay_result_url(),
                back_url=settings.get_unitpay_back_url(),
                hide_other_methods=settings.UNITPAY_HIDE_OTHER_METHODS,
                subscription=enable_subscription,
                customer_email=customer_email,
            )

            payment_url = (result.get('response') or {}).get('redirectUrl')
            unitpay_id = str((result.get('response') or {}).get('paymentId', '')) or None
            if not payment_url:
                logger.error('UnitPay API не вернул redirectUrl', result=result)
                return None

            unitpay_crud = import_module('app.database.crud.unitpay')
            local_payment = await unitpay_crud.create_unitpay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=settings.UNITPAY_CURRENCY,
                description=description,
                payment_url=payment_url,
                payment_type=effective_type,
                unitpay_id=unitpay_id,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info('UnitPay: создан платеж', order_id=order_id, user_id=user_id, amount_rubles=amount_rubles)
            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': settings.UNITPAY_CURRENCY,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
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
    ) -> str:
        """
        Обрабатывает GET-webhook от UnitPay.
        Возвращает JSON-строку ответа.
        """
        import json

        def _ok() -> str:
            return json.dumps({'result': {'message': 'ok'}})

        def _err(msg: str) -> str:
            return json.dumps({'error': {'message': msg}})

        try:
            sign = params.get('signature', '')
            if not unitpay_service.verify_webhook_signature(method, params, sign):
                logger.warning('UnitPay webhook: неверная подпись', method=method)
                return _err('bad signature')

            if method == 'check':
                return _ok()

            if method not in ('pay', 'preauth'):
                return _ok()

            account_field = params.get('account', '')
            unitpay_id = str(params.get('paymentId', ''))
            subscription_id = str(params.get('subscriptionId', '')) or None
            amount_str = params.get('orderSum', params.get('sum', '0'))
            try:
                amount = float(amount_str)
            except (ValueError, TypeError):
                amount = 0.0

            if not unitpay_id:
                return _err('missing paymentId')

            unitpay_crud = import_module('app.database.crud.unitpay')
            payment = await unitpay_crud.get_unitpay_payment_by_unitpay_id(db, unitpay_id)
            if not payment and account_field:
                payment = await unitpay_crud.get_unitpay_payment_by_order_id(db, account_field)
            if not payment:
                logger.warning('UnitPay webhook: платеж не найден', unitpay_id=unitpay_id, account=account_field)
                return _err('payment not found')

            locked = await unitpay_crud.get_unitpay_payment_by_id_for_update(db, payment.id)
            if not locked:
                return _err('lock failed')
            payment = locked

            if payment.is_paid:
                return _ok()

            expected = payment.amount_kopeks / 100
            if abs(amount - expected) > 0.01:
                logger.warning(
                    'UnitPay webhook: несоответствие суммы',
                    expected=expected,
                    got=amount,
                    order_id=order_id,
                )
                return _err('amount mismatch')

            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.unitpay_id = unitpay_id or payment.unitpay_id
            if subscription_id:
                payment.subscription_id = subscription_id
            payment.callback_payload = dict(params)
            payment.updated_at = datetime.now(UTC)
            await db.flush()

            await self._finalize_unitpay_payment(db, payment, unitpay_id=unitpay_id, trigger='webhook')
            return _ok()

        except Exception as e:
            logger.exception('UnitPay webhook: ошибка обработки', e=e)
            return '{"error":{"message":"internal error"}}'

    async def _finalize_unitpay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        unitpay_id: str | None,
        trigger: str,
    ) -> bool:
        payment_module = import_module('app.services.payment_service')

        if payment.transaction_id:
            return True

        metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=unitpay_id or payment.order_id,
            provider_name='unitpay',
        )
        if guest_result is not None:
            return True

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для UnitPay платежа', user_id=payment.user_id, order_id=payment.order_id)
            return False

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

        payment.transaction_id = transaction.id
        payment.updated_at = datetime.now(UTC)
        await db.flush()

        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)
        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)
        topup_status = 'Первое пополнение' if was_first_topup else 'Пополнение'

        await db.commit()

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

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, payment.amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка реферального пополнения UnitPay', error=error)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()

        await db.refresh(user)
        await db.refresh(payment)

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

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                display_name = settings.get_unitpay_display_name()
                keyboard = await self.build_topup_success_keyboard(user)
                message = (
                    '✅ <b>Пополнение успешно!</b>\n\n'
                    f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                    f'💳 Способ: {display_name}\n'
                    f'🆔 Транзакция: {transaction.id}\n\n'
                    'Баланс пополнен автоматически!'
                )
                await self.bot.send_message(user.telegram_id, message, parse_mode='HTML', reply_markup=keyboard)
            except Exception as error:
                logger.error('Ошибка уведомления пользователю UnitPay', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка корзины после пополнения UnitPay', user_id=user.id, error=error)

        if payment.subscription_id and payment.user_id:
            try:
                from app.database.crud.saved_payment_method import create_saved_payment_method

                await create_saved_payment_method(
                    db,
                    user_id=payment.user_id,
                    provider_token=payment.subscription_id,
                    provider='unitpay',
                    method_type='bank_card',
                    commit=True,
                )
                logger.info(
                    'UnitPay: сохранена карта для рекуррента',
                    user_id=payment.user_id,
                    subscription_id=payment.subscription_id,
                )
            except Exception as error:
                logger.error('UnitPay: ошибка сохранения карты для рекуррента', user_id=payment.user_id, error=error)

        logger.info('✅ Обработан UnitPay платеж', order_id=payment.order_id, user_id=payment.user_id, trigger=trigger)
        return True
