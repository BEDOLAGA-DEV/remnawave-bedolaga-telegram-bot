"""Mixin для интеграции оплаты через SHKeeper."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, ShkeeperPayment, Transaction, TransactionType, User
from app.services.shkeeper_service import ShkeeperAPIError, ShkeeperService
from app.services.subscription_auto_purchase_service import (
    auto_purchase_saved_cart_after_topup,
)
from app.utils.currency_converter import currency_converter
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


PAID_STATUSES = {'paid', 'success', 'completed', 'confirmed'}


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    raw = str(value).strip().replace(' ', '').replace(',', '.')
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


class ShkeeperPaymentMixin:
    """Создание инвойсов и обработка callback событий SHKeeper."""

    @staticmethod
    def _extract_received_amount(payload: dict[str, Any]) -> Decimal | None:
        for key in (
            'paid_amount_crypto',
            'amount_crypto',
            'crypto_amount',
            'paid_amount',
            'amount_paid',
            'amount',
        ):
            amount = _parse_decimal(payload.get(key))
            if amount is not None:
                return amount
        return None

    @staticmethod
    def _extract_expected_amount(payment: Any) -> Decimal | None:
        expected_crypto = _parse_decimal(getattr(payment, 'amount_crypto', None))
        if expected_crypto is not None:
            return expected_crypto
        return _parse_decimal(getattr(payment, 'display_amount', None))

    def _is_sufficient_amount(self, payment: Any, payload: dict[str, Any]) -> bool:
        expected = self._extract_expected_amount(payment)
        if expected is None:
            logger.warning(
                'SHKeeper: не удалось проверить сумму, ожидаемая сумма отсутствует',
                payment_id=getattr(payment, 'id', None),
                order_id=getattr(payment, 'order_id', None),
            )
            return False

        received = self._extract_received_amount(payload)
        if received is None:
            logger.warning(
                'SHKeeper: не удалось проверить сумму, в callback отсутствует сумма',
                payment_id=getattr(payment, 'id', None),
                order_id=getattr(payment, 'order_id', None),
            )
            return False

        if received < expected:
            logger.warning(
                'SHKeeper: получена сумма меньше ожидаемой, платеж не финализирован',
                payment_id=getattr(payment, 'id', None),
                order_id=getattr(payment, 'order_id', None),
                expected=str(expected),
                received=str(received),
            )
            return False

        return True

    async def create_shkeeper_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
    ) -> dict[str, Any] | None:
        if not getattr(self, 'shkeeper_service', None):
            logger.error('SHKeeper сервис не инициализирован')
            return None

        if amount_kopeks < settings.SHKEEPER_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'SHKeeper: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                SHKEEPER_MIN_AMOUNT_KOPEKS=settings.SHKEEPER_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.SHKEEPER_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'SHKeeper: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                SHKEEPER_MAX_AMOUNT_KOPEKS=settings.SHKEEPER_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module('app.services.payment_service')
        order_id = f'shk_{user_id}_{uuid.uuid4().hex[:10]}'

        callback_url = None
        if settings.WEBHOOK_URL:
            callback_url = f'{settings.WEBHOOK_URL}{settings.SHKEEPER_WEBHOOK_PATH}'

        try:
            amount_rubles = amount_kopeks / 100
            amount_usd = await currency_converter.rub_to_usd(amount_rubles)
            response = await self.shkeeper_service.create_invoice(  # type: ignore[union-attr]
                amount_usd=amount_usd,
                order_id=order_id,
                description=description,
                callback_url=callback_url,
            )
        except ShkeeperAPIError as error:
            logger.error('Ошибка SHKeeper API при создании инвойса', error=error)
            return None
        except Exception as error:
            logger.exception('Непредвиденная ошибка создания SHKeeper инвойса', error=error)
            return None

        invoice_id = str(response.get('id') or response.get('invoice_id') or '')
        external_id = str(response.get('external_id') or order_id)
        payment_url = (
            response.get('url') or response.get('payment_url') or response.get('link') or response.get('checkout_url')
        )
        status = str(response.get('status') or 'new')

        if not payment_url:
            logger.error('SHKeeper не вернул ссылку на оплату', response=response)
            return None

        local_payment = await payment_module.create_shkeeper_payment(
            db=db,
            user_id=user_id,
            order_id=order_id,
            amount_kopeks=amount_kopeks,
            currency='RUB',
            description=description,
            payment_url=str(payment_url),
            shkeeper_invoice_id=invoice_id or None,
            external_id=external_id,
            amount_crypto=str(response.get('amount_crypto') or response.get('crypto_amount') or '') or None,
            crypto=str(response.get('cryptocurrency') or response.get('crypto') or settings.SHKEEPER_CRYPTO),
            display_amount=str(response.get('display_amount') or response.get('amount') or '') or None,
            expires_at=ShkeeperService.parse_datetime(response.get('expires_at') or response.get('expires')),
            metadata_json={'create_response': response},
        )

        if status != local_payment.status:
            await payment_module.update_shkeeper_payment_status(
                db,
                payment=local_payment,
                status=status,
            )

        logger.info('Создан SHKeeper платеж', order_id=order_id, user_id=user_id, amount_kopeks=amount_kopeks)
        return {
            'local_payment_id': local_payment.id,
            'order_id': order_id,
            'invoice_id': invoice_id,
            'external_id': external_id,
            'payment_url': str(payment_url),
            'status': status,
        }

    async def process_shkeeper_webhook(self, db: AsyncSession, payload: dict[str, Any]) -> bool:
        payment_module = import_module('app.services.payment_service')

        if not isinstance(payload, dict):
            logger.error('SHKeeper callback: payload не является словарем')
            return False

        external_id = str(payload.get('external_id') or payload.get('order_id') or '')
        invoice_id = str(payload.get('id') or payload.get('invoice_id') or '')
        status = str(payload.get('status') or payload.get('payment_status') or '').strip().lower()

        payment = None
        if external_id:
            payment = await payment_module.get_shkeeper_payment_by_external_id(db, external_id)
            if not payment:
                payment = await payment_module.get_shkeeper_payment_by_order_id(db, external_id)
        if not payment and invoice_id:
            payment = await payment_module.get_shkeeper_payment_by_invoice_id(db, invoice_id)

        if not payment:
            logger.warning('SHKeeper callback: платеж не найден', external_id=external_id, invoice_id=invoice_id)
            return False

        metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        metadata['last_callback'] = payload

        payment = await payment_module.update_shkeeper_payment_status(
            db,
            payment=payment,
            status=status or payment.status,
            shkeeper_invoice_id=invoice_id or None,
            external_id=external_id or payment.external_id,
            amount_crypto=str(payload.get('amount_crypto') or payload.get('crypto_amount') or '') or None,
            crypto=str(payload.get('cryptocurrency') or payload.get('crypto') or '') or None,
            display_amount=str(payload.get('display_amount') or payload.get('amount') or '') or None,
            metadata_json=metadata,
            callback_payload=payload,
        )

        if payment.is_paid:
            return True

        paid_flag = bool(payload.get('paid'))
        if paid_flag or status in PAID_STATUSES:
            if not self._is_sufficient_amount(payment, payload):
                return False
            await self._finalize_shkeeper_payment(db, payment, payload)

        return True

    async def get_shkeeper_payment_status(self, db: AsyncSession, local_payment_id: int) -> dict[str, Any] | None:
        payment_module = import_module('app.services.payment_service')
        payment = await payment_module.get_shkeeper_payment_by_id(db, local_payment_id)
        if not payment:
            return None

        if not getattr(self, 'shkeeper_service', None):
            return {'payment': payment}

        remote = None
        try:
            remote = await self.shkeeper_service.get_invoice_status(  # type: ignore[union-attr]
                invoice_id=payment.shkeeper_invoice_id,
                external_id=payment.external_id or payment.order_id,
            )
        except Exception as error:
            logger.warning('Ошибка запроса статуса SHKeeper', error=error, payment_id=payment.id)

        if remote:
            status = str(remote.get('status') or payment.status).strip().lower()
            paid_flag = bool(remote.get('paid'))
            payment = await payment_module.update_shkeeper_payment_status(
                db,
                payment=payment,
                status=status,
                shkeeper_invoice_id=str(remote.get('id') or remote.get('invoice_id') or '')
                or payment.shkeeper_invoice_id,
                amount_crypto=str(remote.get('amount_crypto') or remote.get('crypto_amount') or '')
                or payment.amount_crypto,
                display_amount=str(remote.get('display_amount') or remote.get('amount') or '')
                or payment.display_amount,
                metadata_json={**(payment.metadata_json or {}), 'last_status_response': remote},
            )
            if not payment.is_paid and (paid_flag or status in PAID_STATUSES):
                if not self._is_sufficient_amount(payment, remote):
                    return {'payment': payment, 'remote': remote}
                await self._finalize_shkeeper_payment(db, payment, remote)
                payment = await payment_module.get_shkeeper_payment_by_id(db, local_payment_id)

        return {'payment': payment, 'remote': remote}

    async def _finalize_shkeeper_payment(self, db: AsyncSession, payment: Any, payload: dict[str, Any]) -> bool:
        # Блокируем запись платежа, чтобы параллельные callback-и не зачислили баланс дважды.
        payment_result = await db.execute(
            select(ShkeeperPayment).where(ShkeeperPayment.id == payment.id).with_for_update()
        )
        locked_payment = payment_result.scalar_one_or_none()
        if not locked_payment:
            logger.error('SHKeeper платеж не найден при финализации', payment_id=payment.id)
            return False

        if locked_payment.transaction_id:
            logger.info(
                'SHKeeper платеж уже обработан',
                order_id=locked_payment.order_id,
                transaction_id=locked_payment.transaction_id,
            )
            return True

        user_result = await db.execute(select(User).where(User.id == locked_payment.user_id).with_for_update())
        user = user_result.scalar_one_or_none()
        if not user:
            logger.error('Пользователь не найден при финализации SHKeeper', user_id=locked_payment.user_id)
            return False

        transaction = Transaction(
            user_id=locked_payment.user_id,
            type=TransactionType.DEPOSIT.value,
            amount_kopeks=locked_payment.amount_kopeks,
            description=f'Пополнение через {settings.get_shkeeper_display_name()} ({locked_payment.order_id})',
            payment_method=PaymentMethod.SHKEEPER.value,
            external_id=str(payload.get('id') or payload.get('invoice_id') or locked_payment.order_id),
            is_completed=True,
            completed_at=datetime.now(UTC),
            created_at=getattr(locked_payment, 'created_at', None),
        )
        db.add(transaction)
        await db.flush()

        now = datetime.now(UTC)
        locked_payment.status = 'paid'
        locked_payment.is_paid = True
        locked_payment.paid_at = now
        locked_payment.transaction_id = transaction.id
        locked_payment.callback_payload = payload
        locked_payment.updated_at = now

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup
        user.balance_kopeks += locked_payment.amount_kopeks
        user.updated_at = now
        if was_first_topup and not user.has_made_first_topup:
            user.has_made_first_topup = True

        await db.commit()
        await db.refresh(transaction)
        await db.refresh(locked_payment)
        await db.refresh(user)

        try:
            from app.services.event_emitter import event_emitter

            await event_emitter.emit(
                'payment.completed',
                {
                    'transaction_id': transaction.id,
                    'user_id': user.id,
                    'type': TransactionType.DEPOSIT.value,
                    'amount_kopeks': locked_payment.amount_kopeks,
                    'amount_rubles': locked_payment.amount_kopeks / 100,
                    'payment_method': PaymentMethod.SHKEEPER.value,
                    'external_id': transaction.external_id,
                    'is_completed': True,
                    'description': transaction.description,
                },
                db=db,
            )
        except Exception as error:
            logger.warning('Не удалось отправить событие о транзакции SHKeeper', error=error)

        try:
            from app.services.promo_group_assignment import maybe_assign_promo_group_by_total_spent

            await maybe_assign_promo_group_by_total_spent(db, user.id)
        except Exception as error:
            logger.debug('Не удалось проверить автовыдачу промогруппы после SHKeeper', user_id=user.id, error=error)

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, locked_payment.amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения SHKeeper', error=error)

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user=user,
                    transaction=transaction,
                    old_balance=old_balance,
                    topup_status='🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение',
                    referrer_info=format_referrer_info(user),
                    subscription=getattr(user, 'subscription', None),
                    promo_group=user.get_primary_promo_group(),
                    db=db,
                )
            except Exception as error:
                logger.error('Ошибка отправки админ-уведомления SHKeeper', error=error)

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(locked_payment.amount_kopeks)}\n'
                        f'💳 Способ: {settings.get_shkeeper_display_name()}\n'
                        f'🆔 Транзакция: {transaction.id}'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки пользовательского уведомления SHKeeper', error=error)

        try:
            await auto_purchase_saved_cart_after_topup(db, user, bot=getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка автопокупки после пополнения SHKeeper', error=error)

        logger.info('SHKeeper платеж успешно финализирован', order_id=locked_payment.order_id, user_id=user.id)
        return True
