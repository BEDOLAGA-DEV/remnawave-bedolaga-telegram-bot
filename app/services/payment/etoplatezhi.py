"""Mixin для интеграции с Etoplatezhi (paymentpage.etoplatezhi.ru)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.etoplatezhi_service import etoplatezhi_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов Etoplatezhi -> internal
ETOPLATEZHI_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'success': ('success', True),
    'decline': ('declined', False),
    'error': ('error', False),
    'processing': ('pending', False),
    'awaiting 3ds result': ('pending', False),
    'awaiting redirect result': ('pending', False),
    'awaiting clarification': ('pending', False),
    'awaiting customer action': ('pending', False),
    'cancelled': ('cancelled', False),
    'refunded': ('refunded', False),
    'partially refunded': ('partially_refunded', False),
    'reversed': ('reversed', False),
}


class EtoplatezhiPaymentMixin:
    """Mixin для работы с платежами Etoplatezhi."""

    async def create_etoplatezhi_payment(
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
        Создает платеж Etoplatezhi.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_etoplatezhi_enabled():
            logger.error('Etoplatezhi не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.ETOPLATEZHI_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Etoplatezhi: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                ETOPLATEZHI_MIN_AMOUNT_KOPEKS=settings.ETOPLATEZHI_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.ETOPLATEZHI_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Etoplatezhi: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                ETOPLATEZHI_MAX_AMOUNT_KOPEKS=settings.ETOPLATEZHI_MAX_AMOUNT_KOPEKS,
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
        order_id = f'etp{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.ETOPLATEZHI_CURRENCY

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
                webhook_url = f'{settings.WEBHOOK_URL.rstrip("/")}{settings.ETOPLATEZHI_WEBHOOK_PATH}'

            lifetime = settings.ETOPLATEZHI_PAYMENT_LIFETIME_MINUTES

            # Определяем force_payment_method по типу подметода
            force_method = None
            if payment_method_type == 'sbp':
                force_method = 'sbp'
            elif payment_method_type == 'card':
                force_method = 'card'

            # Строим URL для редиректа на платёжную страницу
            payment_url = etoplatezhi_service.build_payment_url(
                project_id=settings.ETOPLATEZHI_PROJECT_ID or 0,
                payment_id=order_id,
                payment_amount=amount_kopeks,
                payment_currency=currency,
                customer_id=str(tg_id),
                description=description,
                callback_url=webhook_url,
                success_url=return_url or settings.ETOPLATEZHI_RETURN_URL,
                fail_url=return_url or settings.ETOPLATEZHI_RETURN_URL,
                force_payment_method=force_method,
                customer_email=email,
                language_code=language,
            )

            logger.info(
                'Etoplatezhi: сформирован URL платежа',
                order_id=order_id,
                payment_url=payment_url,
            )

            expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            # Сохраняем в БД
            etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
            local_payment = await etoplatezhi_crud.create_etoplatezhi_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=payment_method_type,
                etoplatezhi_payment_id=None,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'Etoplatezhi: создан платеж',
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
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('Etoplatezhi: ошибка создания платежа', error=e)
            return None

    async def process_etoplatezhi_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает callback от Etoplatezhi.

        Подпись проверяется в webserver/payments.py до вызова этого метода.

        Args:
            db: Сессия БД
            payload: JSON тело callback (signature проверена в webserver)

        Returns:
            True если платеж успешно обработан
        """
        try:
            # Etoplatezhi callback structure:
            # {project_id, payment: {id, status, sum: {amount, currency}}, customer: {id}, signature}
            payment_data = payload.get('payment', {})
            etoplatezhi_payment_id = payment_data.get('id')
            etoplatezhi_status = payment_data.get('status')

            # payment.id в callback — это наш payment_id (order_id)
            our_payment_id = str(etoplatezhi_payment_id) if etoplatezhi_payment_id else None

            if not our_payment_id or not etoplatezhi_status:
                logger.warning('Etoplatezhi callback: отсутствуют обязательные поля', payload=payload)
                return False

            # Определяем is_paid по статусу
            is_confirmed = etoplatezhi_status == 'success'

            # Ищем платеж по order_id (наш payment_id = order_id)
            etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
            payment = await etoplatezhi_crud.get_etoplatezhi_payment_by_order_id(db, our_payment_id)

            if not payment:
                logger.warning(
                    'Etoplatezhi callback: платеж не найден',
                    payment_id=our_payment_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await etoplatezhi_crud.get_etoplatezhi_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Etoplatezhi: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('Etoplatezhi callback: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = ETOPLATEZHI_STATUS_MAP.get(etoplatezhi_status, ('pending', False))
            internal_status, is_paid = status_info

            # Если статус success, принудительно считаем оплаченным
            if is_confirmed:
                is_paid = True
                internal_status = 'success'

            # Извлекаем сумму из callback: payment.sum.amount (в минорных единицах)
            sum_data = payment_data.get('sum', {})

            callback_payload = {
                'etoplatezhi_payment_id': etoplatezhi_payment_id,
                'status': etoplatezhi_status,
                'sum': sum_data,
                'customer': payload.get('customer'),
                'project_id': payload.get('project_id'),
            }

            # Проверка суммы ДО обновления статуса
            if is_paid:
                amount_value = sum_data.get('amount')
                if amount_value is not None:
                    # amount в минорных единицах (копейках)
                    received_kopeks = int(amount_value)
                    if abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'Etoplatezhi amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await etoplatezhi_crud.update_etoplatezhi_payment_status(
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
                payment.etoplatezhi_payment_id = str(etoplatezhi_payment_id) if etoplatezhi_payment_id else None
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_etoplatezhi_payment(db, payment, trigger='webhook')

            # Для не-success статусов можно безопасно коммитить
            payment = await etoplatezhi_crud.update_etoplatezhi_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('Etoplatezhi callback: ошибка обработки', error=e)
            return False

    async def _finalize_etoplatezhi_payment(
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
        etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'Etoplatezhi платеж уже связан с транзакцией',
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
            provider_name='etoplatezhi',
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
            logger.error('Пользователь не найден для Etoplatezhi', user_id=payment.user_id)
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
                PaymentMethod.ETOPLATEZHI,
            )

        display_name = settings.get_etoplatezhi_display_name()
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
                payment_method=PaymentMethod.ETOPLATEZHI,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await etoplatezhi_crud.link_etoplatezhi_payment_to_transaction(
            db, payment=payment, transaction_id=transaction.id
        )

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Etoplatezhi платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.ETOPLATEZHI,
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
            logger.error('Ошибка обработки реферального пополнения Etoplatezhi', error=error)

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
                logger.error('Ошибка отправки админ уведомления Etoplatezhi', error=error)

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
                logger.error('Ошибка отправки уведомления пользователю Etoplatezhi', error=error)

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
            'Обработан Etoplatezhi платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True
