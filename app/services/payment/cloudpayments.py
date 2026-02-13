"""Mixin for integrating CloudPayments into the payment service."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.cloudpayments_service import CloudPaymentsAPIError
from app.services.subscription_auto_purchase_service import (
    auto_activate_subscription_after_topup,
    auto_purchase_saved_cart_after_topup,
)
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class CloudPaymentsPaymentMixin:
    """Encapsulates creation and webhook handling for CloudPayments."""

    async def create_cloudpayments_payment(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int,
        description: str,
        *,
        telegram_id: int | None = None,
        language: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Create a CloudPayments payment and return payment link info.

        Args:
            db: Database session
            user_id: Internal user ID
            amount_kopeks: Payment amount in kopeks
            description: Payment description
            telegram_id: User's Telegram ID
            language: User's language
            email: User's email (optional)

        Returns:
            Dict with payment_url and invoice_id, or None on error
        """
        if not getattr(self, 'cloudpayments_service', None):
            logger.error('CloudPayments service is not initialised')
            return None

        if amount_kopeks < settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма CloudPayments меньше минимальной: %s < %s',
                amount_kopeks,
                settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма CloudPayments больше максимальной: %s > %s',
                amount_kopeks,
                settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module('app.services.payment_service')

        # Generate unique invoice ID (use user_id for uniqueness, works for email-only users too)
        invoice_id = self.cloudpayments_service.generate_invoice_id(user_id)

        try:
            # Create payment order via CloudPayments API
            payment_url = await self.cloudpayments_service.generate_payment_link(
                telegram_id=telegram_id,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                invoice_id=invoice_id,
                description=description,
                email=email,
            )
        except CloudPaymentsAPIError as error:
            logger.error('Ошибка создания CloudPayments платежа: %s', error)
            return None
        except Exception as error:
            logger.exception('Непредвиденная ошибка при создании CloudPayments платежа: %s', error)
            return None

        metadata = {
            'language': language or settings.DEFAULT_LANGUAGE,
            'telegram_id': telegram_id,
        }

        # Create local payment record
        local_payment = await payment_module.create_cloudpayments_payment(
            db=db,
            user_id=user_id,
            invoice_id=invoice_id,
            amount_kopeks=amount_kopeks,
            description=description,
            payment_url=payment_url,
            metadata=metadata,
            test_mode=settings.CLOUDPAYMENTS_TEST_MODE,
        )

        if not local_payment:
            logger.error('Не удалось создать локальную запись CloudPayments платежа')
            return None

        logger.info(
            'Создан CloudPayments платёж: invoice=%s, amount=%s₽, user=%s',
            invoice_id,
            amount_kopeks / 100,
            user_id,
        )

        return {
            'payment_url': payment_url,
            'invoice_id': invoice_id,
            'payment_id': local_payment.id,
        }

    async def process_cloudpayments_pay_webhook(
        self,
        db: AsyncSession,
        webhook_data: dict[str, Any],
    ) -> bool:
        """
        Process CloudPayments Pay webhook (successful payment).

        Args:
            db: Database session
            webhook_data: Parsed webhook data

        Returns:
            True if payment was processed successfully
        """
        invoice_id = webhook_data.get('invoice_id')
        transaction_id_cp = webhook_data.get('transaction_id')
        amount = webhook_data.get('amount', 0)
        amount_kopeks = int(amount * 100)
        account_id = webhook_data.get('account_id', '')
        token = webhook_data.get('token')
        test_mode = webhook_data.get('test_mode', False)

        if not invoice_id:
            logger.error('CloudPayments webhook без invoice_id')
            return False

        payment_module = import_module('app.services.payment_service')

        # Find existing payment record
        payment = await payment_module.get_cloudpayments_payment_by_invoice_id(db, invoice_id)

        if not payment:
            logger.warning(
                'CloudPayments платёж не найден: invoice=%s, создаём новый',
                invoice_id,
            )
            # Try to extract user_id from account_id (we now use user_id as AccountId)
            try:
                user_id = int(account_id) if account_id else None
            except ValueError:
                user_id = None

            if not user_id:
                logger.error('Не удалось определить user_id из account_id: %s', account_id)
                return False

            # Get user by ID
            from app.database.crud.user import get_user_by_id

            user = await get_user_by_id(db, user_id)
            if not user:
                logger.error('Пользователь не найден: id=%s', user_id)
                return False

            # Create payment record
            payment = await payment_module.create_cloudpayments_payment(
                db=db,
                user_id=user.id,
                invoice_id=invoice_id,
                amount_kopeks=amount_kopeks,
                description=settings.CLOUDPAYMENTS_DESCRIPTION,
                test_mode=test_mode,
            )

            if not payment:
                logger.error('Не удалось создать запись платежа')
                return False

        # Check if already processed
        if payment.is_paid:
            logger.info('CloudPayments платёж уже обработан: invoice=%s', invoice_id)
            return True

        # Update payment record
        payment.transaction_id_cp = transaction_id_cp
        payment.status = 'completed'
        payment.is_paid = True
        payment.paid_at = datetime.utcnow()
        payment.token = token
        payment.card_first_six = webhook_data.get('card_first_six')
        payment.card_last_four = webhook_data.get('card_last_four')
        payment.card_type = webhook_data.get('card_type')
        payment.card_exp_date = webhook_data.get('card_exp_date')
        payment.email = webhook_data.get('email')
        payment.test_mode = test_mode
        payment.callback_payload = webhook_data

        await db.flush()

        # Get user
        from app.database.crud.user import add_user_balance, get_user_by_id

        user = await get_user_by_id(db, payment.user_id)

        if not user:
            logger.error('Пользователь не найден: id=%s', payment.user_id)
            return False

        # Add balance (без автоматической транзакции - создадим ниже с external_id)
        await add_user_balance(db, user, amount_kopeks, create_transaction=False)

        # Create transaction record
        from app.database.crud.transaction import create_transaction

        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=amount_kopeks,
            description=payment.description or settings.CLOUDPAYMENTS_DESCRIPTION,
            payment_method=PaymentMethod.CLOUDPAYMENTS,
            external_id=str(transaction_id_cp) if transaction_id_cp else invoice_id,
            is_completed=True,
        )

        payment.transaction_id = transaction.id
        await db.commit()

        user_id_display = user.telegram_id or user.email or f'#{user.id}'
        logger.info(
            'CloudPayments платёж успешно обработан: invoice=%s, amount=%s₽, user=%s',
            invoice_id,
            amount_kopeks / 100,
            user_id_display,
        )

        # Send notification to user
        try:
            await self._send_cloudpayments_success_notification(
                user=user,
                amount_kopeks=amount_kopeks,
                transaction=transaction,
            )
        except Exception as error:
            logger.exception('Ошибка отправки уведомления CloudPayments: %s', error)

        # Auto-purchase if enabled
        auto_purchase_success = False
        try:
            auto_purchase_success = await auto_purchase_saved_cart_after_topup(db, user, bot=getattr(self, 'bot', None))
        except Exception as error:
            logger.exception('Ошибка автопокупки после CloudPayments: %s', error)

        # Умная автоактивация если автопокупка не сработала
        if not auto_purchase_success:
            try:
                # Игнорируем notification_sent т.к. здесь нет дополнительных уведомлений
                await auto_activate_subscription_after_topup(
                    db, user, bot=getattr(self, 'bot', None), topup_amount=amount_kopeks
                )
            except Exception as error:
                logger.exception('Ошибка умной автоактивации после CloudPayments: %s', error)

        return True

    async def process_cloudpayments_fail_webhook(
        self,
        db: AsyncSession,
        webhook_data: dict[str, Any],
    ) -> bool:
        """
        Process CloudPayments Fail webhook (failed payment).

        Args:
            db: Database session
            webhook_data: Parsed webhook data

        Returns:
            True if processed successfully
        """
        invoice_id = webhook_data.get('invoice_id')
        reason = webhook_data.get('reason', 'Unknown')
        reason_code = webhook_data.get('reason_code')
        card_holder_message = webhook_data.get('card_holder_message', reason)
        account_id = webhook_data.get('account_id', '')

        if not invoice_id:
            logger.warning('CloudPayments fail webhook без invoice_id')
            return True

        payment_module = import_module('app.services.payment_service')

        # Find payment record
        payment = await payment_module.get_cloudpayments_payment_by_invoice_id(db, invoice_id)

        if payment:
            payment.status = 'failed'
            payment.callback_payload = webhook_data
            await db.commit()

        logger.info(
            'CloudPayments платёж неуспешен: invoice=%s, reason=%s (code=%s)',
            invoice_id,
            reason,
            reason_code,
        )

        # Notify user about failed payment (account_id now contains user_id, not telegram_id)
        try:
            user_id = int(account_id) if account_id else None
            if user_id:
                from app.database.crud.user import get_user_by_id

                # Need a new session for this query since we're outside the main flow
                from app.database.session import async_session_factory

                async with async_session_factory() as session:
                    user = await get_user_by_id(session, user_id)
                    if user and user.telegram_id:
                        await self._send_cloudpayments_fail_notification(
                            telegram_id=user.telegram_id,
                            message=card_holder_message,
                        )
        except Exception as error:
            logger.exception('Ошибка отправки уведомления о неуспешном платеже: %s', error)

        return True

    async def _send_cloudpayments_success_notification(
        self,
        user: Any,
        amount_kopeks: int,
        transaction: Any,
    ) -> None:
        """Send success notification to user via Telegram."""
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        from app.config import settings
        from app.localization.texts import get_texts

        bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

        # Skip email-only users (no telegram_id)
        if not user.telegram_id:
            logger.debug('Skipping CloudPayments notification for email-only user %s', user.id)
            return

        texts = get_texts(user.language)
        keyboard = await self.build_topup_success_keyboard(user)

        referrer_info = format_referrer_info(user)

        amount_rub = amount_kopeks / 100
        new_balance = user.balance_kopeks / 100

        message = texts.t(
            'PAYMENT_SUCCESS_CLOUDPAYMENTS',
            '✅ <b>Оплата получена!</b>\n\n'
            '💰 Сумма: {amount}₽\n'
            '💳 Способ: CloudPayments\n'
            '💵 Баланс: {balance}₽\n\n'
            'Спасибо за пополнение!',
        ).format(
            amount=f'{amount_rub:.2f}',
            balance=f'{new_balance:.2f}',
        )

        if referrer_info:
            message += f'\n\n{referrer_info}'

        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        except Exception as error:
            logger.warning('Не удалось отправить уведомление пользователю %s: %s', user.telegram_id, error)

    async def _send_cloudpayments_fail_notification(
        self,
        telegram_id: int,
        message: str,
    ) -> None:
        """Send failure notification to user via Telegram."""
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        from app.config import settings

        bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

        text = f'❌ <b>Оплата не прошла</b>\n\n{message}'

        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode='HTML',
            )
        except Exception as error:
            logger.warning('Не удалось отправить уведомление пользователю %s: %s', telegram_id, error)

    async def get_cloudpayments_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        """
        Check CloudPayments payment status via API.

        Args:
            db: Database session
            local_payment_id: Internal payment ID

        Returns:
            Dict with payment info or None if not found
        """
        payment_module = import_module('app.services.payment_service')

        # Get local payment record
        payment = await payment_module.get_cloudpayments_payment_by_id(db, local_payment_id)
        if not payment:
            logger.warning('CloudPayments payment not found: id=%s', local_payment_id)
            return None

        # If already paid, return current state
        if payment.is_paid:
            return {'payment': payment, 'status': 'completed'}

        # Check with CloudPayments API
        if not getattr(self, 'cloudpayments_service', None):
            logger.warning('CloudPayments service not initialized')
            return {'payment': payment, 'status': payment.status}

        try:
            # Try to find payment by invoice_id
            api_response = await self.cloudpayments_service.find_payment(payment.invoice_id)

            if not api_response.get('Success'):
                logger.debug(
                    'CloudPayments API: payment not found or error for invoice=%s',
                    payment.invoice_id,
                )
                return {'payment': payment, 'status': payment.status}

            model = api_response.get('Model', {})
            api_status = model.get('Status', '')
            transaction_id_cp = model.get('TransactionId')

            # Update local record if status changed
            if api_status == 'Completed' and not payment.is_paid:
                # Payment completed - process it
                webhook_data = {
                    'invoice_id': payment.invoice_id,
                    'transaction_id': transaction_id_cp,
                    'amount': model.get('Amount', 0),
                    'account_id': model.get('AccountId', ''),
                    'token': model.get('Token'),
                    'card_first_six': model.get('CardFirstSix'),
                    'card_last_four': model.get('CardLastFour'),
                    'card_type': model.get('CardType'),
                    'card_exp_date': model.get('CardExpDate'),
                    'email': model.get('Email'),
                    'test_mode': model.get('TestMode', False),
                    'status': api_status,
                }
                await self.process_cloudpayments_pay_webhook(db, webhook_data)
                await db.refresh(payment)

            elif api_status in ('Declined', 'Cancelled') and payment.status not in ('failed', 'cancelled'):
                payment.status = 'failed'
                await db.flush()
                await db.refresh(payment)

            return {'payment': payment, 'status': payment.status}

        except Exception as error:
            logger.error(
                'Error checking CloudPayments payment status: id=%s, error=%s',
                local_payment_id,
                error,
            )
            return {'payment': payment, 'status': payment.status}
