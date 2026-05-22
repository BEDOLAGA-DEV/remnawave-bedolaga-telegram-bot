"""Mixin для интеграции с RollyPay (rollypay.io)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.rollypay_service import rollypay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов RollyPay -> internal
ROLLYPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'created': ('pending', False),
    'processing': ('processing', False),
    'paid': ('success', True),
    'expired': ('expired', False),
    'canceled': ('canceled', False),
    'chargeback': ('chargeback', False),
}


class RollyPayPaymentMixin:
    """Mixin для работы с платежами RollyPay."""

    async def create_rollypay_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        payment_method_type: str | None = None,
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Создает платеж RollyPay.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_rollypay_enabled():
            logger.error('RollyPay не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.ROLLYPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'RollyPay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                ROLLYPAY_MIN_AMOUNT_KOPEKS=settings.ROLLYPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.ROLLYPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'RollyPay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                ROLLYPAY_MAX_AMOUNT_KOPEKS=settings.ROLLYPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id
        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            user = None
            tg_id = 'guest'

        # Генерируем уникальный order_id с telegram_id для удобного поиска
        order_id = f'rp{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        amount_value = f'{amount_rubles:.2f}'
        currency = settings.ROLLYPAY_CURRENCY

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            # Используем API для создания платежа
            result = await rollypay_service.create_payment(
                amount_value=amount_value,
                currency=currency,
                order_id=order_id,
                payment_method=payment_method_type,
                description=description,
                redirect_url=return_url or settings.ROLLYPAY_RETURN_URL,
                customer_id=str(tg_id),
                metadata=metadata,
            )

            payment_url = result.get('pay_url')
            rollypay_payment_id = result.get('payment_id')

            if not payment_url:
                logger.error('RollyPay API не вернул URL платежа', result=result)
                return None

            logger.info(
                'RollyPay API: создан платеж',
                order_id=order_id,
                rollypay_payment_id=rollypay_payment_id,
                payment_url=payment_url,
            )

            # Срок действия из expires_at ответа или 30 минут по умолчанию
            expires_at_str = result.get('expires_at')
            if expires_at_str:
                try:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    expires_at = datetime.now(UTC) + timedelta(minutes=30)
            else:
                expires_at = datetime.now(UTC) + timedelta(minutes=30)

            # Сохраняем в БД
            rollypay_crud = import_module('app.database.crud.rollypay')
            local_payment = await rollypay_crud.create_rollypay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=payment_method_type or 'sbp',
                rollypay_payment_id=rollypay_payment_id,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'RollyPay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'rollypay_payment_id': rollypay_payment_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('RollyPay: ошибка создания платежа', error=e)
            return None

    async def process_rollypay_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает webhook от RollyPay.

        Подпись проверяется в webserver/payments.py до вызова этого метода.

        Args:
            db: Сессия БД
            payload: JSON тело webhook (signature проверена в webserver)

        Returns:
            True если платеж успешно обработан
        """
        try:
            event_type = payload.get('event_type')
            rollypay_payment_id = payload.get('payment_id')
            order_id = payload.get('order_id')
            rollypay_status = payload.get('status')

            if not rollypay_payment_id or not rollypay_status:
                logger.warning('RollyPay webhook: отсутствуют обязательные поля', payload=payload)
                return False

            # Определяем is_paid по event
            is_confirmed = event_type == 'payment.paid'

            # Ищем платеж по order_id (наш) или rollypay_payment_id
            rollypay_crud = import_module('app.database.crud.rollypay')
            payment = None
            if order_id:
                payment = await rollypay_crud.get_rollypay_payment_by_order_id(db, order_id)
            if not payment and rollypay_payment_id:
                payment = await rollypay_crud.get_rollypay_payment_by_rollypay_id(db, rollypay_payment_id)

            if not payment:
                logger.warning(
                    'RollyPay webhook: платеж не найден',
                    order_id=order_id,
                    rollypay_payment_id=rollypay_payment_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await rollypay_crud.get_rollypay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('RollyPay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('RollyPay webhook: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = ROLLYPAY_STATUS_MAP.get(rollypay_status, ('pending', False))
            internal_status, is_paid = status_info

            # Если event = payment.paid, принудительно считаем оплаченным
            if is_confirmed:
                is_paid = True
                internal_status = 'success'

            callback_payload = {
                'rollypay_payment_id': rollypay_payment_id,
                'order_id': order_id,
                'status': rollypay_status,
                'event_type': event_type,
                'amount': payload.get('amount'),
                'currency': payload.get('currency'),
            }

            # Проверка суммы ДО обновления статуса
            if is_paid:
                amount_value = payload.get('amount')
                if amount_value is not None:
                    received_kopeks = round(float(amount_value) * 100)
                    if abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'RollyPay amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await rollypay_crud.update_rollypay_payment_status(
                            db=db,
                            payment=payment,
                            status='amount_mismatch',
                            is_paid=False,
                            rollypay_payment_id=rollypay_payment_id,
                            callback_payload=callback_payload,
                        )
                        return False

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.rollypay_payment_id = rollypay_payment_id or payment.rollypay_payment_id
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_rollypay_payment(
                    db, payment, rollypay_payment_id=rollypay_payment_id, trigger='webhook'
                )

            # Для не-success статусов можно безопасно коммитить
            payment = await rollypay_crud.update_rollypay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                rollypay_payment_id=rollypay_payment_id,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('RollyPay webhook: ошибка обработки', error=e)
            return False

    async def _finalize_rollypay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        rollypay_payment_id: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock must be acquired by the caller before invoking this method.
        """
        payment_module = import_module('app.services.payment_service')
        rollypay_crud = import_module('app.database.crud.rollypay')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'RollyPay платеж уже связан с транзакцией',
                order_id=payment.order_id,
                transaction_id=payment.transaction_id,
                trigger=trigger,
            )
            return True

        # Read fresh metadata AFTER lock to avoid stale data
        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        # --- Guest purchase flow ---
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=str(rollypay_payment_id) if rollypay_payment_id else payment.order_id,
            provider_name='rollypay',
        )
        if guest_result is not None:
            return True

        # Ensure paid fields are set (idempotent — caller may have already set them)
        if not payment.is_paid:
            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.updated_at = datetime.now(UTC)

        balance_already_credited = bool(metadata.get('balance_credited'))

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для RollyPay', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = str(rollypay_payment_id) if rollypay_payment_id else payment.order_id

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.ROLLYPAY,
            )

        display_name = settings.get_rollypay_display_name()
        description = f'Пополнение через {display_name}'

        transaction = existing_transaction
        created_transaction = False

        if not transaction:
            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=description,
                payment_method=PaymentMethod.ROLLYPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await rollypay_crud.link_rollypay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('RollyPay платеж уже зачислил баланс ранее', order_id=payment.order_id)
            return True

        # Lock user row to prevent concurrent balance race conditions
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)

        # Emit deferred side-effects after atomic commit
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.ROLLYPAY,
            external_id=transaction_external_id,
        )

        topup_status = '\U0001f195 Первое пополнение' if was_first_topup else '\U0001f504 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения RollyPay', error=error)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

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
                logger.error('Ошибка отправки админ уведомления RollyPay', error=error)

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '\u2705 <b>Пополнение успешно!</b>\n\n'
                        f'\U0001f4b0 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'\U0001f4b3 Способ: {display_name}\n'
                        f'\U0001f194 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю RollyPay', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя',
                user_id=payment.user_id,
                error=error,
                exc_info=True,
            )

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True
        payment.metadata_json = metadata
        await db.commit()

        logger.info(
            'Обработан RollyPay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_rollypay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API."""
        try:
            rollypay_crud = import_module('app.database.crud.rollypay')
            payment = await rollypay_crud.get_rollypay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('RollyPay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {
                    'payment': payment,
                    'status': 'success',
                    'is_paid': True,
                }

            # Проверяем через API по rollypay_payment_id
            if payment.rollypay_payment_id:
                try:
                    order_data = await rollypay_service.get_payment(payment.rollypay_payment_id)
                    rollypay_status = order_data.get('status')

                    if rollypay_status:
                        status_info = ROLLYPAY_STATUS_MAP.get(rollypay_status, ('pending', False))
                        internal_status, is_paid = status_info

                        if is_paid:
                            # Проверка суммы
                            api_amount = order_data.get('amount')
                            if api_amount is not None:
                                received_kopeks = round(float(api_amount) * 100)
                                if abs(received_kopeks - payment.amount_kopeks) > 1:
                                    logger.error(
                                        'RollyPay amount mismatch (API check)',
                                        expected_kopeks=payment.amount_kopeks,
                                        received_kopeks=received_kopeks,
                                        order_id=payment.order_id,
                                    )
                                    await rollypay_crud.update_rollypay_payment_status(
                                        db=db,
                                        payment=payment,
                                        status='amount_mismatch',
                                        is_paid=False,
                                        rollypay_payment_id=payment.rollypay_payment_id,
                                        callback_payload={
                                            'check_source': 'api',
                                            'rollypay_order_data': order_data,
                                        },
                                    )
                                    return {
                                        'payment': payment,
                                        'status': 'amount_mismatch',
                                        'is_paid': False,
                                    }

                            # Acquire FOR UPDATE lock before finalization
                            locked = await rollypay_crud.get_rollypay_payment_by_id_for_update(db, payment.id)
                            if not locked:
                                logger.error('RollyPay: не удалось заблокировать платёж', payment_id=payment.id)
                                return None
                            payment = locked

                            if payment.is_paid:
                                logger.info('RollyPay платеж уже обработан (api_check)', order_id=payment.order_id)
                                return {
                                    'payment': payment,
                                    'status': 'success',
                                    'is_paid': True,
                                }

                            logger.info('RollyPay payment confirmed via API', order_id=payment.order_id)

                            # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
                            payment.status = 'success'
                            payment.is_paid = True
                            payment.paid_at = datetime.now(UTC)
                            payment.callback_payload = {
                                'check_source': 'api',
                                'rollypay_order_data': order_data,
                            }
                            payment.updated_at = datetime.now(UTC)
                            await db.flush()

                            await self._finalize_rollypay_payment(
                                db,
                                payment,
                                rollypay_payment_id=payment.rollypay_payment_id,
                                trigger='api_check',
                            )
                        elif internal_status != payment.status:
                            # Обновляем статус если изменился
                            payment = await rollypay_crud.update_rollypay_payment_status(
                                db=db,
                                payment=payment,
                                status=internal_status,
                            )

                except Exception as e:
                    logger.error('Error checking RollyPay payment status via API', error=e)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        except Exception as e:
            logger.exception('RollyPay: ошибка проверки статуса', error=e)
            return None
