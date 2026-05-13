"""Mixin для интеграции с Overpay (pay.overpay.io)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.overpay_service import overpay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов Overpay -> internal
OVERPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'charged': ('success', True),
    'authorized': ('authorized', False),
    'preflight': ('pending', False),
    'new': ('pending', False),
    'processing': ('processing', False),
    'prepared': ('processing', False),
    'rejected': ('rejected', False),
    'declined': ('declined', False),
    'reversed': ('reversed', False),
    'refunded': ('refunded', False),
    'chargeback': ('chargeback', False),
    'error': ('error', False),
}


class OverpayPaymentMixin:
    """Mixin для работы с платежами Overpay."""

    async def create_overpay_payment(
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
        Создает платеж Overpay.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_overpay_enabled():
            logger.error('Overpay не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.OVERPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Overpay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                OVERPAY_MIN_AMOUNT_KOPEKS=settings.OVERPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.OVERPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Overpay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                OVERPAY_MAX_AMOUNT_KOPEKS=settings.OVERPAY_MAX_AMOUNT_KOPEKS,
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
        order_id = f'op{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        amount_value = f'{amount_rubles:.2f}'
        currency = settings.OVERPAY_CURRENCY

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        # Методы оплаты из настроек
        payment_methods_str = settings.OVERPAY_PAYMENT_METHODS
        payment_methods = (
            [m.strip() for m in payment_methods_str.split(',') if m.strip()] if payment_methods_str else None
        )

        try:
            # Используем API для создания платежа
            result = await overpay_service.create_payment(
                amount=amount_value,
                currency=currency,
                lifetime_minutes=settings.OVERPAY_LIFETIME_MINUTES,
                merchant_transaction_id=order_id,
                description=description,
                return_url=return_url or settings.OVERPAY_RETURN_URL,
                payment_methods=payment_methods,
            )

            payment_url = result.get('resultUrl')
            overpay_payment_id = str(result.get('id', '')) if result.get('id') else None

            if not payment_url:
                logger.error('Overpay API не вернул URL платежа', result=result)
                return None

            logger.info(
                'Overpay API: создан платеж',
                order_id=order_id,
                overpay_payment_id=overpay_payment_id,
                payment_url=payment_url,
            )

            # Срок действия
            expires_at = datetime.now(UTC) + timedelta(minutes=settings.OVERPAY_LIFETIME_MINUTES)

            # Сохраняем в БД
            overpay_crud = import_module('app.database.crud.overpay')
            local_payment = await overpay_crud.create_overpay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                overpay_payment_id=overpay_payment_id,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'Overpay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'overpay_payment_id': overpay_payment_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('Overpay: ошибка создания платежа', error=e)
            return None

    async def process_overpay_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает webhook от Overpay.

        mTLS обеспечивает аутентификацию; дополнительно проверяем наличие платежа в БД.

        Args:
            db: Сессия БД
            payload: JSON тело webhook

        Returns:
            True если платеж успешно обработан
        """
        try:
            overpay_payment_id = str(payload.get('id', '')) if payload.get('id') else None
            merchant_transaction_id = payload.get('merchantTransactionId')
            overpay_status = payload.get('status')

            if not overpay_payment_id or not overpay_status:
                logger.warning('Overpay webhook: отсутствуют обязательные поля', payload=payload)
                return False

            # Ищем платеж по order_id (наш merchantTransactionId) или overpay_payment_id
            overpay_crud = import_module('app.database.crud.overpay')
            payment = None
            if merchant_transaction_id:
                payment = await overpay_crud.get_overpay_payment_by_order_id(db, merchant_transaction_id)
            if not payment and overpay_payment_id:
                payment = await overpay_crud.get_overpay_payment_by_overpay_id(db, overpay_payment_id)

            if not payment:
                logger.warning(
                    'Overpay webhook: платеж не найден',
                    merchant_transaction_id=merchant_transaction_id,
                    overpay_payment_id=overpay_payment_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await overpay_crud.get_overpay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Overpay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('Overpay webhook: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = OVERPAY_STATUS_MAP.get(overpay_status, ('pending', False))
            internal_status, is_paid = status_info

            callback_payload = {
                'overpay_payment_id': overpay_payment_id,
                'merchant_transaction_id': merchant_transaction_id,
                'status': overpay_status,
            }

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.overpay_payment_id = overpay_payment_id or payment.overpay_payment_id
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_overpay_payment(
                    db, payment, overpay_payment_id=overpay_payment_id, trigger='webhook'
                )

            # Для не-success статусов можно безопасно коммитить
            payment = await overpay_crud.update_overpay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                overpay_payment_id=overpay_payment_id,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('Overpay webhook: ошибка обработки', error=e)
            return False

    async def _finalize_overpay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        overpay_payment_id: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock must be acquired by the caller before invoking this method.
        """
        payment_module = import_module('app.services.payment_service')
        overpay_crud = import_module('app.database.crud.overpay')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'Overpay платеж уже связан с транзакцией',
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
            provider_payment_id=str(overpay_payment_id) if overpay_payment_id else payment.order_id,
            provider_name='overpay',
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
            logger.error('Пользователь не найден для Overpay', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = str(overpay_payment_id) if overpay_payment_id else payment.order_id

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.OVERPAY,
            )

        display_name = settings.get_overpay_display_name()
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
                payment_method=PaymentMethod.OVERPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await overpay_crud.link_overpay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Overpay платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.OVERPAY,
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
            logger.error('Ошибка обработки реферального пополнения Overpay', error=error)

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
                logger.error('Ошибка отправки админ уведомления Overpay', error=error)

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
                logger.error('Ошибка отправки уведомления пользователю Overpay', error=error)

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
            'Обработан Overpay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_overpay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API."""
        try:
            overpay_crud = import_module('app.database.crud.overpay')
            payment = await overpay_crud.get_overpay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('Overpay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {
                    'payment': payment,
                    'status': 'success',
                    'is_paid': True,
                }

            # Проверяем через API по overpay_payment_id
            if payment.overpay_payment_id:
                try:
                    order_data = await overpay_service.get_payment(payment.overpay_payment_id)
                    overpay_status = order_data.get('status')

                    if overpay_status:
                        status_info = OVERPAY_STATUS_MAP.get(overpay_status, ('pending', False))
                        internal_status, is_paid = status_info

                        if is_paid:
                            # Acquire FOR UPDATE lock before finalization
                            locked = await overpay_crud.get_overpay_payment_by_id_for_update(db, payment.id)
                            if not locked:
                                logger.error('Overpay: не удалось заблокировать платёж', payment_id=payment.id)
                                return None
                            payment = locked

                            if payment.is_paid:
                                logger.info('Overpay платеж уже обработан (api_check)', order_id=payment.order_id)
                                return {
                                    'payment': payment,
                                    'status': 'success',
                                    'is_paid': True,
                                }

                            logger.info('Overpay payment confirmed via API', order_id=payment.order_id)

                            # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
                            payment.status = 'success'
                            payment.is_paid = True
                            payment.paid_at = datetime.now(UTC)
                            payment.callback_payload = {
                                'check_source': 'api',
                                'overpay_order_data': order_data,
                            }
                            payment.updated_at = datetime.now(UTC)
                            await db.flush()

                            await self._finalize_overpay_payment(
                                db,
                                payment,
                                overpay_payment_id=payment.overpay_payment_id,
                                trigger='api_check',
                            )
                        elif internal_status != payment.status:
                            # Обновляем статус если изменился
                            payment = await overpay_crud.update_overpay_payment_status(
                                db=db,
                                payment=payment,
                                status=internal_status,
                            )

                except Exception as e:
                    logger.error('Error checking Overpay payment status via API', error=e)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        except Exception as e:
            logger.exception('Overpay: ошибка проверки статуса', error=e)
            return None
