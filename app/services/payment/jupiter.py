"""Mixin для интеграции с Jupiter (FPGate P2P v2.1, app.juppiter.tech)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.jupiter_service import jupiter_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов Jupiter -> internal
JUPITER_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'success': ('success', True),
    'processing': ('pending', False),
    'cancelled': ('cancelled', False),
    'declined': ('declined', False),
    'error': ('error', False),
}


class JupiterPaymentMixin:
    """Mixin для работы с платежами Jupiter."""

    async def create_jupiter_payment(
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
        """Создаёт платёж Jupiter.

        Параметр ``return_url`` принимается для совместимости сигнатуры с другими провайдерами
        (cabinet/routes/balance.py одинаково вызывает все ``create_*_payment``), но фактически
        не используется: спецификация Jupiter v2.1 имеет поле ``redirect`` зарезервированное
        для будущего использования и не поддерживает return-URL семантику. Пользователь
        видит QR-код СБП и подтверждает оплату в банковском приложении.
        """
        if not settings.is_jupiter_enabled():
            logger.error('Jupiter не настроен')
            return None

        if amount_kopeks < settings.JUPITER_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Jupiter: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                JUPITER_MIN_AMOUNT_KOPEKS=settings.JUPITER_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.JUPITER_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Jupiter: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                JUPITER_MAX_AMOUNT_KOPEKS=settings.JUPITER_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            user = None
            tg_id = 'guest'

        order_id = f'jup{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.JUPITER_CURRENCY

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
            'payment_method_type': payment_method_type,
        }

        try:
            callback_url = self._build_jupiter_callback_url()
            customer_id = str(tg_id) if tg_id != 'guest' else f'guest-{order_id[-6:]}'
            customer_name = (
                getattr(user, 'first_name', None) or getattr(user, 'username', None) or settings.JUPITER_FALLBACK_NAME
            )

            api_result = await jupiter_service.create_payment(
                amount_rubles=amount_rubles,
                order_id=order_id,
                customer_id=customer_id,
                customer_email=email,
                customer_name=customer_name,
                callback_url=callback_url,
                description=description[:255] if description else None,
            )

            transaction_id = api_result.get('transaction_id')
            details = api_result.get('details') or {}
            payment_url = details.get('qrcode_url') or details.get('url')

            logger.info(
                'Jupiter: получен ответ API',
                order_id=order_id,
                transaction_id=transaction_id,
                payment_url=payment_url,
            )

            lifetime = settings.JUPITER_PAYMENT_LIFETIME_MINUTES
            expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            jupiter_crud = import_module('app.database.crud.jupiter')
            local_payment = await jupiter_crud.create_jupiter_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=payment_method_type,
                jupiter_transaction_id=str(transaction_id) if transaction_id else None,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'Jupiter: создан платеж',
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
                'payment_id': str(transaction_id) if transaction_id else None,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('Jupiter: ошибка создания платежа', error=e)
            return None

    @staticmethod
    def _build_jupiter_callback_url() -> str | None:
        """Собирает абсолютный callback URL для Jupiter."""
        webhook_path = settings.JUPITER_WEBHOOK_PATH or '/jupiter-webhook'
        base = (
            getattr(settings, 'WEBHOOK_URL', None)
            or getattr(settings, 'WEB_API_BASE_URL', None)
            or getattr(settings, 'CABINET_URL', None)
        )
        if not base:
            return None
        return f'{base.rstrip("/")}{webhook_path if webhook_path.startswith("/") else "/" + webhook_path}'

    async def process_jupiter_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Обрабатывает callback от Jupiter (подпись уже проверена в webserver)."""
        try:
            our_order_id = payload.get('order_id')
            jupiter_transaction_id = payload.get('transaction_id')
            status_obj = payload.get('status') or {}
            jupiter_status = (status_obj.get('type') or '').strip().lower()

            if not our_order_id or not jupiter_status:
                logger.warning('Jupiter callback: отсутствуют обязательные поля', payload=payload)
                return False

            jupiter_crud = import_module('app.database.crud.jupiter')
            payment = await jupiter_crud.get_jupiter_payment_by_order_id(db, our_order_id)
            if not payment:
                logger.warning('Jupiter callback: платеж не найден', order_id=our_order_id)
                return False

            locked = await jupiter_crud.get_jupiter_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Jupiter: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            if payment.is_paid:
                logger.info('Jupiter callback: платеж уже обработан', order_id=payment.order_id)
                return True

            # Терминальные неуспешные статусы стики — провайдер не должен иметь возможность
            # «починить» отклонённый/несовпавший платёж повторным callback'ом.
            if payment.status in {'amount_mismatch', 'cancelled', 'declined', 'error', 'expired'}:
                logger.warning(
                    'Jupiter callback: платёж в терминальном неуспешном статусе, игнорируется',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=jupiter_status,
                )
                return True

            internal_status, is_paid = JUPITER_STATUS_MAP.get(jupiter_status, ('pending', False))

            callback_payload = {
                'jupiter_transaction_id': jupiter_transaction_id,
                'status_type': jupiter_status,
                'amount': payload.get('amount'),
                'recalculated': payload.get('recalculated'),
                'timestamp': payload.get('timestamp'),
            }

            # Сверяем сумму ДО обновления статуса
            if is_paid:
                amount_obj = payload.get('amount') or {}
                received_value = amount_obj.get('value')
                if received_value is not None:
                    try:
                        received_kopeks = round(float(received_value) * 100)
                    except (TypeError, ValueError):
                        received_kopeks = None
                    if received_kopeks is not None and abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'Jupiter amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await jupiter_crud.update_jupiter_payment_status(
                            db=db,
                            payment=payment,
                            status='amount_mismatch',
                            is_paid=False,
                            callback_payload=callback_payload,
                        )
                        return False

            if is_paid:
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.jupiter_transaction_id = (
                    str(jupiter_transaction_id) if jupiter_transaction_id else payment.jupiter_transaction_id
                )
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_jupiter_payment(db, payment, trigger='webhook')

            payment = await jupiter_crud.update_jupiter_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                callback_payload=callback_payload,
            )
            return True

        except Exception as e:
            logger.exception('Jupiter callback: ошибка обработки', error=e)
            return False

    async def _finalize_jupiter_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock уже взят вызывающим.
        """
        payment_module = import_module('app.services.payment_service')
        jupiter_crud = import_module('app.database.crud.jupiter')

        if payment.transaction_id:
            logger.info(
                'Jupiter платеж уже связан с транзакцией',
                order_id=payment.order_id,
                transaction_id=payment.transaction_id,
                trigger=trigger,
            )
            return True

        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.order_id,
            provider_name='jupiter',
        )
        if guest_result is not None:
            return True

        if not payment.is_paid:
            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.updated_at = datetime.now(UTC)

        balance_already_credited = bool(metadata.get('balance_credited'))

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для Jupiter', user_id=payment.user_id)
            return False

        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = payment.order_id

        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.JUPITER,
            )

        display_name = settings.get_jupiter_display_name()
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
                payment_method=PaymentMethod.JUPITER,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await jupiter_crud.link_jupiter_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Jupiter платеж уже зачислил баланс ранее', order_id=payment.order_id)
            return True

        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)

        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.JUPITER,
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
            logger.error('Ошибка обработки реферального пополнения Jupiter', error=error)

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
                logger.error('Ошибка отправки админ уведомления Jupiter', error=error)

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'\U0001f4b0 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'\U0001f4b3 Способ: {display_name}\n'
                        f'\U0001f194 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Jupiter', error=error)

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
            'Обработан Jupiter платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_jupiter_payment_status(
        self,
        db: AsyncSession,
        transaction_id: str,
    ) -> dict[str, Any] | None:
        """Запрос статуса платежа через API Jupiter."""
        try:
            return await jupiter_service.check_payment(transaction_id=transaction_id)
        except Exception as e:
            logger.error('Jupiter: ошибка проверки статуса', transaction_id=transaction_id, error=e)
            return None
