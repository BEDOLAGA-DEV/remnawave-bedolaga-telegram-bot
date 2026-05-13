"""Mixin для интеграции с Antilopay (lk.antilopay.com)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.antilopay_service import antilopay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов Antilopay -> internal
ANTILOPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'PENDING': ('pending', False),
    'SUCCESS': ('success', True),
    'FAIL': ('failed', False),
    'CANCEL': ('cancelled', False),
    'EXPIRED': ('expired', False),
    'CHARGEBACK': ('chargeback', False),
    'REVERSED': ('reversed', False),
}


class AntilopayPaymentMixin:
    """Mixin для работы с платежами Antilopay."""

    async def create_antilopay_payment(
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
        Создает платеж Antilopay.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_antilopay_enabled():
            logger.error('Antilopay не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.ANTILOPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Antilopay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                ANTILOPAY_MIN_AMOUNT_KOPEKS=settings.ANTILOPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.ANTILOPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Antilopay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                ANTILOPAY_MAX_AMOUNT_KOPEKS=settings.ANTILOPAY_MAX_AMOUNT_KOPEKS,
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
        order_id = f'alp{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.ANTILOPAY_CURRENCY

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            # Определяем prefer_methods по типу подметода
            prefer_methods: list[str] | None = None
            if payment_method_type == 'sbp':
                prefer_methods = ['SBP']
            elif payment_method_type == 'card':
                prefer_methods = ['CARD_RU']
            elif payment_method_type == 'sberpay':
                prefer_methods = ['SBER_PAY']

            # Формируем success/fail URL
            result_url = return_url or settings.ANTILOPAY_RETURN_URL

            # merchant_extra — строка до 255 символов для callback
            merchant_extra = order_id

            # Создаем платеж через API
            api_result = await antilopay_service.create_payment(
                amount_rubles=amount_rubles,
                order_id=order_id,
                product_name=settings.ANTILOPAY_PRODUCT_NAME,
                product_type=settings.ANTILOPAY_PRODUCT_TYPE,
                description=description,
                customer_email=email,
                prefer_methods=prefer_methods,
                success_url=result_url,
                fail_url=result_url,
                merchant_extra=merchant_extra,
            )

            payment_id = api_result.get('payment_id')
            payment_url = api_result.get('payment_url')

            logger.info(
                'Antilopay: получен ответ API',
                order_id=order_id,
                payment_id=payment_id,
                payment_url=payment_url,
            )

            lifetime = settings.ANTILOPAY_PAYMENT_LIFETIME_MINUTES
            expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            # Сохраняем в БД
            antilopay_crud = import_module('app.database.crud.antilopay')
            local_payment = await antilopay_crud.create_antilopay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=payment_method_type,
                antilopay_payment_id=payment_id,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'Antilopay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'payment_id': payment_id,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('Antilopay: ошибка создания платежа', error=e)
            return None

    async def process_antilopay_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает callback от Antilopay.

        Подпись проверяется в webserver/payments.py до вызова этого метода.

        Args:
            db: Сессия БД
            payload: JSON тело callback (signature проверена в webserver)

        Returns:
            True если платеж успешно обработан
        """
        try:
            callback_type = payload.get('type')
            if callback_type != 'payment':
                logger.info('Antilopay callback: неизвестный тип', callback_type=callback_type)
                return True  # Не наш тип — не ошибка

            antilopay_payment_id = payload.get('payment_id')
            antilopay_status = payload.get('status')
            our_order_id = payload.get('order_id')

            if not our_order_id or not antilopay_status:
                logger.warning('Antilopay callback: отсутствуют обязательные поля', payload=payload)
                return False

            # Определяем is_paid по статусу
            is_confirmed = antilopay_status == 'SUCCESS'

            # Ищем платеж по order_id
            antilopay_crud = import_module('app.database.crud.antilopay')
            payment = await antilopay_crud.get_antilopay_payment_by_order_id(db, our_order_id)

            if not payment:
                logger.warning(
                    'Antilopay callback: платеж не найден',
                    order_id=our_order_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await antilopay_crud.get_antilopay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Antilopay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('Antilopay callback: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = ANTILOPAY_STATUS_MAP.get(antilopay_status, ('pending', False))
            internal_status, is_paid = status_info

            # Если статус SUCCESS, принудительно считаем оплаченным
            if is_confirmed:
                is_paid = True
                internal_status = 'success'

            callback_payload = {
                'antilopay_payment_id': antilopay_payment_id,
                'status': antilopay_status,
                'amount': payload.get('amount'),
                'original_amount': payload.get('original_amount'),
                'fee': payload.get('fee'),
                'currency': payload.get('currency'),
                'pay_method': payload.get('pay_method'),
                'pay_data': payload.get('pay_data'),
                'customer': payload.get('customer'),
                'merchant_extra': payload.get('merchant_extra'),
            }

            # Проверка суммы ДО обновления статуса
            if is_paid:
                original_amount = payload.get('original_amount')
                if original_amount is not None:
                    # original_amount в РУБЛЯХ (float), конвертируем в копейки
                    received_kopeks = round(float(original_amount) * 100)
                    if abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'Antilopay amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await antilopay_crud.update_antilopay_payment_status(
                            db=db,
                            payment=payment,
                            status='amount_mismatch',
                            is_paid=False,
                            callback_payload=callback_payload,
                        )
                        return False

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.antilopay_payment_id = str(antilopay_payment_id) if antilopay_payment_id else None
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_antilopay_payment(db, payment, trigger='webhook')

            # Для не-success статусов можно безопасно коммитить
            payment = await antilopay_crud.update_antilopay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('Antilopay callback: ошибка обработки', error=e)
            return False

    async def _finalize_antilopay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock must be acquired by the caller before invoking this method.
        """
        payment_module = import_module('app.services.payment_service')
        antilopay_crud = import_module('app.database.crud.antilopay')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'Antilopay платеж уже связан с транзакцией',
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
            provider_payment_id=payment.order_id,
            provider_name='antilopay',
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
            logger.error('Пользователь не найден для Antilopay', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = payment.order_id

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.ANTILOPAY,
            )

        display_name = settings.get_antilopay_display_name()
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
                payment_method=PaymentMethod.ANTILOPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await antilopay_crud.link_antilopay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Antilopay платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.ANTILOPAY,
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
            logger.error('Ошибка обработки реферального пополнения Antilopay', error=error)

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
                logger.error('Ошибка отправки админ уведомления Antilopay', error=error)

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
                logger.error('Ошибка отправки уведомления пользователю Antilopay', error=error)

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
            'Обработан Antilopay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_antilopay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API Antilopay."""
        try:
            result = await antilopay_service.check_payment(order_id=order_id)
            return result
        except Exception as e:
            logger.error('Antilopay: ошибка проверки статуса', order_id=order_id, error=e)
            return None
