"""Mixin для интеграции с PayPear (paypear.ru)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.paypear_service import paypear_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов PayPear -> internal
PAYPEAR_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'NEW': ('pending', False),
    'PROCESS': ('processing', False),
    'CONFIRMED': ('success', True),
    'CANCELED': ('canceled', False),
    'REFUNDED': ('refunded', False),
    'EXPIRED': ('expired', False),
}


class PayPearPaymentMixin:
    """Mixin для работы с платежами PayPear."""

    async def create_paypear_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Создает платеж PayPear.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_paypear_enabled():
            logger.error('PayPear не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.PAYPEAR_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'PayPear: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                PAYPEAR_MIN_AMOUNT_KOPEKS=settings.PAYPEAR_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.PAYPEAR_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'PayPear: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                PAYPEAR_MAX_AMOUNT_KOPEKS=settings.PAYPEAR_MAX_AMOUNT_KOPEKS,
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
        order_id = f'pp{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.PAYPEAR_CURRENCY

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            payment_method_type = settings.PAYPEAR_PAYMENT_METHOD

            # Используем API для создания платежа
            result = await paypear_service.create_payment(
                order_id=order_id,
                amount_rubles=amount_rubles,
                currency=currency,
                payment_method_type=payment_method_type,
                description=description,
                return_url=return_url or settings.PAYPEAR_RETURN_URL,
                metadata=metadata,
            )

            confirmation = result.get('confirmation', {})
            payment_url = confirmation.get('url') if isinstance(confirmation, dict) else None
            paypear_id = result.get('id')

            if not payment_url:
                logger.error('PayPear API не вернул URL платежа', result=result)
                return None

            logger.info(
                'PayPear API: создан платеж',
                order_id=order_id,
                paypear_id=paypear_id,
                payment_url=payment_url,
            )

            # Срок действия — 30 минут по умолчанию
            expires_at = datetime.now(UTC) + timedelta(minutes=30)

            # Сохраняем в БД
            paypear_crud = import_module('app.database.crud.paypear')
            local_payment = await paypear_crud.create_paypear_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=payment_method_type,
                paypear_id=paypear_id,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'PayPear: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'paypear_id': paypear_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('PayPear: ошибка создания платежа', error=e)
            return None

    async def process_paypear_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает webhook от PayPear.

        Подпись проверяется в webserver/payments.py до вызова этого метода.

        Args:
            db: Сессия БД
            payload: JSON тело webhook (signature проверена в webserver)

        Returns:
            True если платеж успешно обработан
        """
        try:
            event = payload.get('event')
            obj = payload.get('object', {})
            paypear_id = obj.get('id')
            order_id = obj.get('order_id')
            paypear_status = obj.get('status')

            if not paypear_id or not paypear_status:
                logger.warning('PayPear webhook: отсутствуют обязательные поля', payload=payload)
                return False

            # Определяем is_paid по event
            is_confirmed = event == 'payment.confirmed'

            # Ищем платеж по order_id (наш) или paypear_id
            paypear_crud = import_module('app.database.crud.paypear')
            payment = None
            if order_id:
                payment = await paypear_crud.get_paypear_payment_by_order_id(db, order_id)
            if not payment and paypear_id:
                payment = await paypear_crud.get_paypear_payment_by_paypear_id(db, paypear_id)

            if not payment:
                logger.warning(
                    'PayPear webhook: платеж не найден',
                    order_id=order_id,
                    paypear_id=paypear_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await paypear_crud.get_paypear_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('PayPear: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('PayPear webhook: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = PAYPEAR_STATUS_MAP.get(paypear_status, ('pending', False))
            internal_status, is_paid = status_info

            # Если event = payment.confirmed, принудительно считаем оплаченным
            if is_confirmed:
                is_paid = True
                internal_status = 'success'

            callback_payload = {
                'paypear_id': paypear_id,
                'order_id': order_id,
                'status': paypear_status,
                'event': event,
                'amount': obj.get('amount'),
            }

            # Проверка суммы ДО обновления статуса
            if is_paid:
                amount_info = obj.get('amount', {})
                if isinstance(amount_info, dict):
                    amount_value = amount_info.get('value')
                else:
                    amount_value = amount_info

                if amount_value is not None:
                    received_kopeks = round(float(amount_value) * 100)
                    if abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'PayPear amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await paypear_crud.update_paypear_payment_status(
                            db=db,
                            payment=payment,
                            status='amount_mismatch',
                            is_paid=False,
                            paypear_id=paypear_id,
                            callback_payload=callback_payload,
                        )
                        return False

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.paypear_id = paypear_id or payment.paypear_id
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_paypear_payment(db, payment, paypear_id=paypear_id, trigger='webhook')

            # Для не-success статусов можно безопасно коммитить
            payment = await paypear_crud.update_paypear_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                paypear_id=paypear_id,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('PayPear webhook: ошибка обработки', error=e)
            return False

    async def _finalize_paypear_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        paypear_id: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock must be acquired by the caller before invoking this method.
        """
        payment_module = import_module('app.services.payment_service')
        paypear_crud = import_module('app.database.crud.paypear')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'PayPear платеж уже связан с транзакцией',
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
            provider_payment_id=str(paypear_id) if paypear_id else payment.order_id,
            provider_name='paypear',
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
            logger.error('Пользователь не найден для PayPear', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = str(paypear_id) if paypear_id else payment.order_id

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.PAYPEAR,
            )

        display_name = settings.get_paypear_display_name()
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
                payment_method=PaymentMethod.PAYPEAR,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await paypear_crud.link_paypear_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('PayPear платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.PAYPEAR,
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
            logger.error('Ошибка обработки реферального пополнения PayPear', error=error)

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
                logger.error('Ошибка отправки админ уведомления PayPear', error=error)

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
                logger.error('Ошибка отправки уведомления пользователю PayPear', error=error)

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
            'Обработан PayPear платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_paypear_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API."""
        try:
            paypear_crud = import_module('app.database.crud.paypear')
            payment = await paypear_crud.get_paypear_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('PayPear payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {
                    'payment': payment,
                    'status': 'success',
                    'is_paid': True,
                }

            # Проверяем через API по paypear_id
            if payment.paypear_id:
                try:
                    order_data = await paypear_service.get_payment(payment.paypear_id)
                    paypear_status = order_data.get('status')

                    if paypear_status:
                        status_info = PAYPEAR_STATUS_MAP.get(paypear_status, ('pending', False))
                        internal_status, is_paid = status_info

                        if is_paid:
                            # Проверка суммы
                            amount_info = order_data.get('amount', {})
                            api_amount = amount_info.get('value') if isinstance(amount_info, dict) else amount_info
                            if api_amount is not None:
                                received_kopeks = round(float(api_amount) * 100)
                                if abs(received_kopeks - payment.amount_kopeks) > 1:
                                    logger.error(
                                        'PayPear amount mismatch (API check)',
                                        expected_kopeks=payment.amount_kopeks,
                                        received_kopeks=received_kopeks,
                                        order_id=payment.order_id,
                                    )
                                    await paypear_crud.update_paypear_payment_status(
                                        db=db,
                                        payment=payment,
                                        status='amount_mismatch',
                                        is_paid=False,
                                        paypear_id=payment.paypear_id,
                                        callback_payload={
                                            'check_source': 'api',
                                            'paypear_order_data': order_data,
                                        },
                                    )
                                    return {
                                        'payment': payment,
                                        'status': 'amount_mismatch',
                                        'is_paid': False,
                                    }

                            # Acquire FOR UPDATE lock before finalization
                            locked = await paypear_crud.get_paypear_payment_by_id_for_update(db, payment.id)
                            if not locked:
                                logger.error('PayPear: не удалось заблокировать платёж', payment_id=payment.id)
                                return None
                            payment = locked

                            if payment.is_paid:
                                logger.info('PayPear платеж уже обработан (api_check)', order_id=payment.order_id)
                                return {
                                    'payment': payment,
                                    'status': 'success',
                                    'is_paid': True,
                                }

                            logger.info('PayPear payment confirmed via API', order_id=payment.order_id)

                            # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
                            payment.status = 'success'
                            payment.is_paid = True
                            payment.paid_at = datetime.now(UTC)
                            payment.callback_payload = {
                                'check_source': 'api',
                                'paypear_order_data': order_data,
                            }
                            payment.updated_at = datetime.now(UTC)
                            await db.flush()

                            await self._finalize_paypear_payment(
                                db,
                                payment,
                                paypear_id=payment.paypear_id,
                                trigger='api_check',
                            )
                        elif internal_status != payment.status:
                            # Обновляем статус если изменился
                            payment = await paypear_crud.update_paypear_payment_status(
                                db=db,
                                payment=payment,
                                status=internal_status,
                            )

                except Exception as e:
                    logger.error('Error checking PayPear payment status via API', error=e)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        except Exception as e:
            logger.exception('PayPear: ошибка проверки статуса', error=e)
            return None
