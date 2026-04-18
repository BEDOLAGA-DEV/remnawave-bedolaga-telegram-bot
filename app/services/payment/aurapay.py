"""Mixin для интеграции с AuraPay (aurapay.tech)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.aurapay_service import aurapay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов AuraPay -> internal
AURAPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'PENDING': ('pending', False),
    'PAID': ('success', True),
    'EXPIRED': ('expired', False),
}


class AuraPayPaymentMixin:
    """Mixin для работы с платежами AuraPay."""

    async def create_aurapay_payment(
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
        Создает платеж AuraPay.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_aurapay_enabled():
            logger.error('AuraPay не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.AURAPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'AuraPay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                AURAPAY_MIN_AMOUNT_KOPEKS=settings.AURAPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.AURAPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'AuraPay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                AURAPAY_MAX_AMOUNT_KOPEKS=settings.AURAPAY_MAX_AMOUNT_KOPEKS,
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
        order_id = f'ap{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.AURAPAY_CURRENCY

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            # Формируем webhook URL
            webhook_url = None
            if settings.WEBHOOK_URL:
                webhook_url = f'{settings.WEBHOOK_URL.rstrip("/")}{settings.AURAPAY_WEBHOOK_PATH}'

            lifetime = settings.AURAPAY_PAYMENT_LIFETIME_MINUTES

            # Используем API для создания инвойса
            result = await aurapay_service.create_invoice(
                amount=amount_rubles,
                order_id=order_id,
                comment=description,
                service=payment_method_type,
                success_url=return_url or settings.AURAPAY_RETURN_URL,
                fail_url=return_url or settings.AURAPAY_RETURN_URL,
                callback_url=webhook_url,
                custom_fields=f'user_id={user_id}' if user_id else None,
                lifetime=lifetime,
            )

            payment_data = result.get('payment_data', {})
            payment_url = payment_data.get('url') if isinstance(payment_data, dict) else None
            aurapay_invoice_id = result.get('id')

            if not payment_url:
                logger.error('AuraPay API не вернул URL платежа', result=result)
                return None

            logger.info(
                'AuraPay API: создан инвойс',
                order_id=order_id,
                aurapay_invoice_id=aurapay_invoice_id,
                payment_url=payment_url,
            )

            # Срок действия из expires_at ответа или lifetime минут по умолчанию
            expires_at_str = result.get('expires_at')
            if expires_at_str:
                try:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)
            else:
                expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            # Сохраняем в БД
            aurapay_crud = import_module('app.database.crud.aurapay')
            local_payment = await aurapay_crud.create_aurapay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=payment_method_type,
                aurapay_invoice_id=aurapay_invoice_id,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'AuraPay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'aurapay_invoice_id': aurapay_invoice_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('AuraPay: ошибка создания платежа', error=e)
            return None

    async def process_aurapay_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает webhook от AuraPay.

        Подпись проверяется в webserver/payments.py до вызова этого метода.

        Args:
            db: Сессия БД
            payload: JSON тело webhook (signature проверена в webserver)

        Returns:
            True если платеж успешно обработан
        """
        try:
            aurapay_invoice_id = payload.get('id')
            order_id = payload.get('order_id')
            aurapay_status = payload.get('status')

            if not aurapay_invoice_id or not aurapay_status:
                logger.warning('AuraPay webhook: отсутствуют обязательные поля', payload=payload)
                return False

            # Определяем is_paid по статусу
            is_confirmed = aurapay_status == 'PAID'

            # Ищем платеж по order_id (наш) или aurapay_invoice_id
            aurapay_crud = import_module('app.database.crud.aurapay')
            payment = None
            if order_id:
                payment = await aurapay_crud.get_aurapay_payment_by_order_id(db, str(order_id))
            if not payment and aurapay_invoice_id:
                payment = await aurapay_crud.get_aurapay_payment_by_invoice_id(db, aurapay_invoice_id)

            if not payment:
                logger.warning(
                    'AuraPay webhook: платеж не найден',
                    order_id=order_id,
                    aurapay_invoice_id=aurapay_invoice_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await aurapay_crud.get_aurapay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('AuraPay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('AuraPay webhook: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = AURAPAY_STATUS_MAP.get(aurapay_status, ('pending', False))
            internal_status, is_paid = status_info

            # Если статус PAID, принудительно считаем оплаченным
            if is_confirmed:
                is_paid = True
                internal_status = 'success'

            callback_payload = {
                'aurapay_invoice_id': aurapay_invoice_id,
                'order_id': order_id,
                'status': aurapay_status,
                'amount': payload.get('amount'),
                'service': payload.get('service'),
                'payer_details': payload.get('payer_details'),
            }

            # Проверка суммы ДО обновления статуса
            # AuraPay отправляет amount как строку "1250.00"
            if is_paid:
                amount_value = payload.get('amount')
                if amount_value is not None:
                    received_kopeks = round(float(amount_value) * 100)
                    if abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'AuraPay amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await aurapay_crud.update_aurapay_payment_status(
                            db=db,
                            payment=payment,
                            status='amount_mismatch',
                            is_paid=False,
                            aurapay_invoice_id=aurapay_invoice_id,
                            callback_payload=callback_payload,
                        )
                        return False

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.aurapay_invoice_id = aurapay_invoice_id or payment.aurapay_invoice_id
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_aurapay_payment(
                    db, payment, aurapay_invoice_id=aurapay_invoice_id, trigger='webhook'
                )

            # Для не-success статусов можно безопасно коммитить
            payment = await aurapay_crud.update_aurapay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                aurapay_invoice_id=aurapay_invoice_id,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('AuraPay webhook: ошибка обработки', error=e)
            return False

    async def _finalize_aurapay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        aurapay_invoice_id: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock must be acquired by the caller before invoking this method.
        """
        payment_module = import_module('app.services.payment_service')
        aurapay_crud = import_module('app.database.crud.aurapay')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'AuraPay платеж уже связан с транзакцией',
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
            provider_payment_id=str(aurapay_invoice_id) if aurapay_invoice_id else payment.order_id,
            provider_name='aurapay',
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
            logger.error('Пользователь не найден для AuraPay', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = str(aurapay_invoice_id) if aurapay_invoice_id else payment.order_id

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.AURAPAY,
            )

        display_name = settings.get_aurapay_display_name()
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
                payment_method=PaymentMethod.AURAPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await aurapay_crud.link_aurapay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('AuraPay платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.AURAPAY,
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
            logger.error('Ошибка обработки реферального пополнения AuraPay', error=error)

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
                logger.error('Ошибка отправки админ уведомления AuraPay', error=error)

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
                logger.error('Ошибка отправки уведомления пользователю AuraPay', error=error)

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
            'Обработан AuraPay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_aurapay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API."""
        try:
            aurapay_crud = import_module('app.database.crud.aurapay')
            payment = await aurapay_crud.get_aurapay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('AuraPay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {
                    'payment': payment,
                    'status': 'success',
                    'is_paid': True,
                }

            # Проверяем через API по aurapay_invoice_id или order_id
            try:
                order_data = await aurapay_service.get_invoice_status(
                    order_id=payment.order_id,
                    invoice_id=payment.aurapay_invoice_id,
                )
                aurapay_status = order_data.get('status')

                if aurapay_status:
                    status_info = AURAPAY_STATUS_MAP.get(aurapay_status, ('pending', False))
                    internal_status, is_paid = status_info

                    if is_paid:
                        # Проверка суммы — AuraPay возвращает amount как число
                        api_amount = order_data.get('amount')
                        if api_amount is not None:
                            received_kopeks = round(float(api_amount) * 100)
                            if abs(received_kopeks - payment.amount_kopeks) > 1:
                                logger.error(
                                    'AuraPay amount mismatch (API check)',
                                    expected_kopeks=payment.amount_kopeks,
                                    received_kopeks=received_kopeks,
                                    order_id=payment.order_id,
                                )
                                await aurapay_crud.update_aurapay_payment_status(
                                    db=db,
                                    payment=payment,
                                    status='amount_mismatch',
                                    is_paid=False,
                                    aurapay_invoice_id=payment.aurapay_invoice_id,
                                    callback_payload={
                                        'check_source': 'api',
                                        'aurapay_order_data': order_data,
                                    },
                                )
                                return {
                                    'payment': payment,
                                    'status': 'amount_mismatch',
                                    'is_paid': False,
                                }

                        # Acquire FOR UPDATE lock before finalization
                        locked = await aurapay_crud.get_aurapay_payment_by_id_for_update(db, payment.id)
                        if not locked:
                            logger.error('AuraPay: не удалось заблокировать платёж', payment_id=payment.id)
                            return None
                        payment = locked

                        if payment.is_paid:
                            logger.info('AuraPay платеж уже обработан (api_check)', order_id=payment.order_id)
                            return {
                                'payment': payment,
                                'status': 'success',
                                'is_paid': True,
                            }

                        logger.info('AuraPay payment confirmed via API', order_id=payment.order_id)

                        # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
                        payment.status = 'success'
                        payment.is_paid = True
                        payment.paid_at = datetime.now(UTC)
                        payment.callback_payload = {
                            'check_source': 'api',
                            'aurapay_order_data': order_data,
                        }
                        payment.updated_at = datetime.now(UTC)
                        await db.flush()

                        await self._finalize_aurapay_payment(
                            db,
                            payment,
                            aurapay_invoice_id=payment.aurapay_invoice_id,
                            trigger='api_check',
                        )
                    elif internal_status != payment.status:
                        # Обновляем статус если изменился
                        payment = await aurapay_crud.update_aurapay_payment_status(
                            db=db,
                            payment=payment,
                            status=internal_status,
                        )

            except Exception as e:
                logger.error('Error checking AuraPay payment status via API', error=e)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        except Exception as e:
            logger.exception('AuraPay: ошибка проверки статуса', error=e)
            return None
