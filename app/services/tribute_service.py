import json
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.database.crud.transaction import create_transaction, get_transaction_by_external_id
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import get_db
from app.database.models import PaymentMethod, TransactionType
from app.external.tribute import TributeService as TributeAPI
from app.services.payment_service import PaymentService
from app.utils.user_utils import format_referrer_info


logger = structlog.get_logger(__name__)


class TributeService:
    _invoice_messages: dict[int, dict[str, int]] = {}

    def __init__(self, bot: Bot):
        self.bot = bot
        self.tribute_api = TributeAPI()

    @classmethod
    def remember_invoice_message(cls, user_id: int, chat_id: int, message_id: int) -> None:
        cls._invoice_messages[user_id] = {'chat_id': chat_id, 'message_id': message_id}

    async def _cleanup_invoice_message(self, user_id: int) -> None:
        invoice_message = self._invoice_messages.pop(user_id, None)
        if not invoice_message or not getattr(self, 'bot', None):
            return

        chat_id = invoice_message.get('chat_id')
        message_id = invoice_message.get('message_id')
        if not chat_id or not message_id:
            return

        try:
            await self.bot.delete_message(chat_id, message_id)
        except Exception as error:  # pragma: no cover - depends on bot rights
            logger.warning('Не удалось удалить Tribute счёт', message_id=message_id, error=error)

    async def create_payment_link(
        self, user_id: int, amount_kopeks: int, description: str = 'Пополнение баланса'
    ) -> str | None:
        if not settings.TRIBUTE_ENABLED:
            logger.warning('Tribute платежи отключены')
            return None

        try:
            payment_url = await self.tribute_api.create_payment_link(
                user_id=user_id, amount_kopeks=amount_kopeks, description=description
            )

            if not payment_url:
                return None

            return payment_url

        except Exception as e:
            logger.error('Ошибка создания Tribute платежа', error=e)
            return None

    async def process_webhook(self, payload: str) -> dict[str, Any]:
        try:
            webhook_data = json.loads(payload)
        except json.JSONDecodeError:
            logger.error('Некорректный JSON в Tribute webhook')
            return {'status': 'error', 'reason': 'invalid_json'}

        logger.info('Получен Tribute webhook', dumps=json.dumps(webhook_data, ensure_ascii=False))

        processed_data = await self.tribute_api.process_webhook(webhook_data)
        if not processed_data:
            return {'status': 'ignored', 'reason': 'invalid_data'}

        event_type = processed_data.get('event_type', 'payment')
        status = processed_data.get('status')

        if event_type == 'payment' and status == 'paid':
            await self._handle_successful_payment(processed_data)
        elif event_type == 'payment' and status == 'failed':
            await self._handle_failed_payment(processed_data)
        elif event_type == 'refund':
            await self._handle_refund(processed_data)

        return {'status': 'ok', 'event': event_type}

    async def _handle_successful_payment(self, payment_data: dict[str, Any]):
        try:
            user_telegram_id = payment_data['user_id']
            amount_kopeks = payment_data['amount_kopeks']
            payment_id = payment_data['payment_id']

            logger.info(
                'Обрабатываем успешный Tribute платеж: user_telegram_id=, amount=, payment_id',
                user_telegram_id=user_telegram_id,
                amount_kopeks=amount_kopeks,
                payment_id=payment_id,
            )

            async for session in get_db():
                user = await get_user_by_telegram_id(session, user_telegram_id)
                if not user:
                    logger.error('Пользователь не найден', user_telegram_id=user_telegram_id)
                    return

                logger.info(
                    'Найден пользователь текущий баланс: коп',
                    telegram_id=user.telegram_id,
                    balance_kopeks=user.balance_kopeks,
                )

                from app.database.crud.transaction import check_tribute_payment_duplicate

                duplicate_transaction = await check_tribute_payment_duplicate(
                    session, payment_id, amount_kopeks, user_telegram_id
                )

                if duplicate_transaction:
                    logger.warning('Найден дубликат платежа в течение 24ч:')
                    logger.warning('Transaction ID', duplicate_transaction_id=duplicate_transaction.id)
                    logger.warning('Amount: коп', amount_kopeks=duplicate_transaction.amount_kopeks)
                    logger.warning('Created', created_at=duplicate_transaction.created_at)
                    logger.warning('External ID', external_id=duplicate_transaction.external_id)
                    logger.warning('Платеж игнорирован - это дубликат свежего платежа')
                    return

                from app.database.crud.transaction import create_unique_tribute_transaction

                transaction = await create_unique_tribute_transaction(
                    db=session,
                    user_id=user.id,
                    payment_id=payment_id,
                    amount_kopeks=amount_kopeks,
                    description=f'Пополнение через Tribute: {amount_kopeks / 100}₽ (ID: {payment_id})',
                )

                old_balance = user.balance_kopeks
                was_first_topup = not user.has_made_first_topup

                user.balance_kopeks += amount_kopeks
                user.updated_at = datetime.now(UTC)

                promo_group = user.get_primary_promo_group()
                subscription = getattr(user, 'subscription', None)
                referrer_info = format_referrer_info(user)
                topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'

                await session.commit()

                try:
                    from app.services.referral_service import process_referral_topup

                    await process_referral_topup(session, user.id, amount_kopeks, self.bot)
                except Exception as e:
                    logger.error('Ошибка обработки реферального пополнения Tribute', error=e)

                if was_first_topup and not user.has_made_first_topup:
                    user.has_made_first_topup = True
                    await session.commit()

                await session.refresh(user)

                logger.info(
                    '✅ Баланс пользователя обновлен: коп (+)',
                    user_telegram_id=user_telegram_id,
                    old_balance=old_balance,
                    balance_kopeks=user.balance_kopeks,
                    amount_kopeks=amount_kopeks,
                )
                logger.info('✅ Создана транзакция ID', transaction_id=transaction.id)

                if was_first_topup:
                    logger.info('Отмечен первый топап для пользователя', user_telegram_id=user_telegram_id)

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
                        db=session,
                    )
                except Exception as e:
                    logger.error('Ошибка отправки уведомления о Tribute пополнении', error=e)

                await self._cleanup_invoice_message(user_telegram_id)
                await self._send_success_notification(user_telegram_id, amount_kopeks)

                logger.info(
                    '🎉 Успешно обработан Tribute платеж: ₽ для пользователя',
                    amount_kopeks=amount_kopeks / 100,
                    user_telegram_id=user_telegram_id,
                )
                break

        except Exception as e:
            logger.error('Ошибка обработки успешного Tribute платежа', error=e, exc_info=True)

    async def _handle_failed_payment(self, payment_data: dict[str, Any]):
        try:
            user_id = payment_data['user_id']
            payment_id = payment_data['payment_id']

            async for session in get_db():
                transaction = await get_transaction_by_external_id(
                    session, f'donation_{payment_id}', PaymentMethod.TRIBUTE
                )

                if transaction:
                    transaction.description = f'{transaction.description} (платеж отклонен)'
                    await session.commit()

                await self._send_failure_notification(user_id)

                logger.info('Обработан неудачный Tribute платеж для пользователя', user_id=user_id)
                break

        except Exception as e:
            logger.error('Ошибка обработки неудачного Tribute платежа', error=e)

    async def _handle_refund(self, refund_data: dict[str, Any]):
        try:
            user_id = refund_data['user_id']
            amount_kopeks = refund_data['amount_kopeks']
            payment_id = refund_data['payment_id']

            async for session in get_db():
                await create_transaction(
                    db=session,
                    user_id=user_id,
                    type=TransactionType.REFUND,
                    amount_kopeks=-amount_kopeks,
                    description=f'Возврат Tribute платежа {payment_id}',
                    payment_method=PaymentMethod.TRIBUTE,
                    external_id=f'refund_{payment_id}',
                    is_completed=True,
                )

                user = await get_user_by_telegram_id(session, user_id)
                if user and user.balance_kopeks >= amount_kopeks:
                    user.balance_kopeks -= amount_kopeks
                    await session.commit()

                await self._send_refund_notification(user_id, amount_kopeks)

                logger.info(
                    'Обработан возврат Tribute: ₽ для пользователя', amount_kopeks=amount_kopeks / 100, user_id=user_id
                )
                break

        except Exception as e:
            logger.error('Ошибка обработки возврата Tribute', error=e)

    async def _send_success_notification(self, user_id: int, amount_kopeks: int):
        # Skip if no telegram_id (email-only user)
        if not user_id:
            logger.debug('Пропуск уведомления Tribute для пользователя без telegram_id')
            return

        try:
            amount_rubles = amount_kopeks / 100

            async for session in get_db():
                user = await get_user_by_telegram_id(session, user_id)
                break

            # Сначала отправляем стандартное уведомление
            payment_service = PaymentService(self.bot)
            keyboard = await payment_service.build_topup_success_keyboard(user)

            text = (
                f'✅ **Платеж успешно получен!**\n\n'
                f'💰 Сумма: {int(amount_rubles)} ₽\n'
                f'💳 Способ оплаты: Tribute\n'
                f'🎉 Средства зачислены на баланс!\n\n'
                f'Спасибо за оплату! 🙏'
            )

            await self.bot.send_message(user_id, text, reply_markup=keyboard, parse_mode='Markdown')

            # Проверяем наличие сохраненной корзины для возврата к оформлению подписки
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, amount_kopeks, session, self.bot)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об успешном платеже', error=e)

    async def _send_failure_notification(self, user_id: int):
        # Skip if no telegram_id (email-only user)
        if not user_id:
            logger.debug('Пропуск уведомления об ошибке Tribute для пользователя без telegram_id')
            return

        try:
            text = (
                '⌘ **Платеж не прошел**\n\n'
                'К сожалению, ваш платеж через Tribute был отклонен.\n\n'
                'Возможные причины:\n'
                '• Недостаточно средств на карте\n'
                '• Технические проблемы банка\n'
                '• Превышен лимит операций\n\n'
                'Попробуйте еще раз или обратитесь в поддержку.'
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text='🔄 Попробовать снова', callback_data='menu_balance')],
                    [InlineKeyboardButton(text='💬 Поддержка', callback_data='menu_support')],
                ]
            )

            await self.bot.send_message(user_id, text, reply_markup=keyboard, parse_mode='Markdown')

        except Exception as e:
            logger.error('Ошибка отправки уведомления о неудачном платеже', error=e)

    async def _send_refund_notification(self, user_id: int, amount_kopeks: int):
        # Skip if no telegram_id (email-only user)
        if not user_id:
            logger.debug('Пропуск уведомления о возврате Tribute для пользователя без telegram_id')
            return

        try:
            amount_rubles = amount_kopeks / 100

            text = (
                f'🔄 **Возврат средств**\n\n'
                f'💰 Сумма возврата: {int(amount_rubles)} ₽\n'
                f'💳 Способ: Tribute\n\n'
                f'Средства будут возвращены на вашу карту в течение 3-5 рабочих дней.\n\n'
                f'Если у вас есть вопросы, обратитесь в поддержку.'
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text='💬 Поддержка', callback_data='menu_support')],
                    [InlineKeyboardButton(text='🏠 Главное меню', callback_data='back_to_menu')],
                ]
            )

            await self.bot.send_message(user_id, text, reply_markup=keyboard, parse_mode='Markdown')

        except Exception as e:
            logger.error('Ошибка отправки уведомления о возврате', error=e)

    async def force_process_payment(
        self,
        payment_id: str,
        user_id: int,
        amount_kopeks: int,
        description: str = 'Принудительная обработка Tribute платежа',
    ) -> bool:
        try:
            logger.info(
                '🔧 ПРИНУДИТЕЛЬНАЯ ОБРАБОТКА: payment_id=, user_id=, amount',
                payment_id=payment_id,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
            )

            async for session in get_db():
                user = await get_user_by_telegram_id(session, user_id)
                if not user:
                    logger.error('⌘ Пользователь не найден', user_id=user_id)
                    return False

                external_id = f'force_donation_{payment_id}_{int(datetime.now(UTC).timestamp())}'

                await create_transaction(
                    db=session,
                    user_id=user.id,
                    type=TransactionType.DEPOSIT,
                    amount_kopeks=amount_kopeks,
                    description=description,
                    payment_method=PaymentMethod.TRIBUTE,
                    external_id=external_id,
                    is_completed=True,
                )

                old_balance = user.balance_kopeks
                user.balance_kopeks += amount_kopeks
                user.updated_at = datetime.now(UTC)

                await session.commit()

                logger.info(
                    '💰 ПРИНУДИТЕЛЬНО обновлен баланс: коп', old_balance=old_balance, balance_kopeks=user.balance_kopeks
                )

                await self._send_success_notification(user_id, amount_kopeks)

                logger.info('✅ Принудительно обработан платеж', payment_id=payment_id)
                return True

        except Exception as e:
            logger.error('Ошибка принудительной обработки', error=e, exc_info=True)
            return False

    async def get_payment_status(self, payment_id: str) -> dict[str, Any] | None:
        return await self.tribute_api.get_payment_status(payment_id)

    async def create_refund(
        self, payment_id: str, amount_kopeks: int | None = None, reason: str = 'Возврат по запросу'
    ) -> dict[str, Any] | None:
        return await self.tribute_api.refund_payment(payment_id, amount_kopeks, reason)
