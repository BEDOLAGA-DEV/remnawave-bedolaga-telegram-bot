"""Функции работы с YooKassa вынесены в dedicated mixin.

Такое разделение облегчает поддержку и делает очевидным, какая часть
отвечает за конкретного провайдера.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


if TYPE_CHECKING:
    from app.database.models import Transaction, User, YooKassaPayment

_INT32_MAX = 2_147_483_647


class YooKassaPaymentMixin:
    """Mixin с операциями по созданию и подтверждению платежей YooKassa."""

    @staticmethod
    def _format_amount_value(value: Any) -> str:
        """Форматирует сумму для хранения в webhook-объекте."""

        try:
            quantized = Decimal(str(value)).quantize(Decimal('0.00'))
            return format(quantized, 'f')
        except (InvalidOperation, ValueError, TypeError):
            return str(value)

    @classmethod
    def _merge_remote_yookassa_payload(
        cls,
        event_object: dict[str, Any],
        remote_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Объединяет локальные данные вебхука с ответом API YooKassa."""

        merged: dict[str, Any] = dict(event_object)

        status = remote_data.get('status')
        if status:
            merged['status'] = status

        if 'paid' in remote_data:
            merged['paid'] = bool(remote_data.get('paid'))

        if 'refundable' in remote_data:
            merged['refundable'] = bool(remote_data.get('refundable'))

        payment_method_type = remote_data.get('payment_method_type')
        if payment_method_type:
            payment_method = dict(merged.get('payment_method') or {})
            payment_method['type'] = payment_method_type
            merged['payment_method'] = payment_method

        amount_value = remote_data.get('amount_value')
        amount_currency = remote_data.get('amount_currency')
        if amount_value is not None or amount_currency:
            merged_amount = dict(merged.get('amount') or {})
            if amount_value is not None:
                merged_amount['value'] = cls._format_amount_value(amount_value)
            if amount_currency:
                merged_amount['currency'] = str(amount_currency).upper()
            merged['amount'] = merged_amount

        for datetime_field in ('captured_at', 'created_at'):
            value = remote_data.get(datetime_field)
            if value:
                merged[datetime_field] = value

        metadata = remote_data.get('metadata')
        if metadata:
            try:
                merged['metadata'] = dict(metadata)  # type: ignore[arg-type]
            except TypeError:
                merged['metadata'] = metadata

        return merged

    async def create_yookassa_payment(
        self,
        db: AsyncSession,
        user_id: int | None,
        amount_kopeks: int,
        description: str,
        receipt_email: str | None = None,
        receipt_phone: str | None = None,
        metadata: dict[str, Any] | None = None,
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Создаёт обычный платёж в YooKassa и сохраняет локальную запись."""
        if not getattr(self, 'yookassa_service', None):
            logger.error('YooKassa сервис не инициализирован')
            return None

        payment_module = import_module('app.services.payment_service')

        try:
            amount_rubles = amount_kopeks / 100

            payment_metadata = metadata.copy() if metadata else {}

            # Всегда добавляем telegram_id в метаданные для возможности возврата платежа
            if user_id is not None and 'user_telegram_id' not in payment_metadata:
                try:
                    from app.database.crud.user import get_user_by_id

                    user = await get_user_by_id(db, user_id)
                    if user and user.telegram_id:
                        payment_metadata['user_telegram_id'] = str(user.telegram_id)
                        payment_metadata['user_username'] = user.username or ''
                except Exception as e:
                    logger.warning('Не удалось получить telegram_id для user_id', user_id=user_id, error=e)

            # Preserve existing type from metadata if passed (e.g., "trial")
            existing_type = payment_metadata.get('type')
            payment_metadata.update(
                {
                    'user_id': str(user_id) if user_id is not None else '',
                    'amount_kopeks': str(amount_kopeks),
                    'type': existing_type or 'balance_topup',
                }
            )

            yookassa_response = await self.yookassa_service.create_payment(
                amount=amount_rubles,
                currency='RUB',
                description=description,
                metadata=payment_metadata,
                receipt_email=receipt_email,
                receipt_phone=receipt_phone,
                return_url=return_url,
            )

            if not yookassa_response or yookassa_response.get('error'):
                logger.error('Ошибка создания платежа YooKassa', yookassa_response=yookassa_response)
                return None

            yookassa_created_at: datetime | None = None
            if yookassa_response.get('created_at'):
                try:
                    dt_with_tz = datetime.fromisoformat(yookassa_response['created_at'].replace('Z', '+00:00'))
                    yookassa_created_at = dt_with_tz
                except Exception as error:
                    logger.warning('Не удалось распарсить created_at', error=error)
                    yookassa_created_at = None

            local_payment = await payment_module.create_yookassa_payment(
                db=db,
                user_id=user_id,
                yookassa_payment_id=yookassa_response['id'],
                amount_kopeks=amount_kopeks,
                currency='RUB',
                description=description,
                status=yookassa_response['status'],
                confirmation_url=yookassa_response.get('confirmation_url'),
                metadata_json=payment_metadata,
                payment_method_type=None,
                yookassa_created_at=yookassa_created_at,
                test_mode=yookassa_response.get('test_mode', False),
            )

            logger.info(
                'Создан платеж YooKassa на ₽ для пользователя',
                yookassa_response=yookassa_response['id'],
                amount_rubles=amount_rubles,
                user_id=user_id,
            )

            return {
                'local_payment_id': local_payment.id,
                'yookassa_payment_id': yookassa_response['id'],
                'confirmation_url': yookassa_response.get('confirmation_url'),
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'status': yookassa_response['status'],
                'created_at': local_payment.created_at,
            }

        except Exception as error:
            logger.error('Ошибка создания платежа YooKassa', error=error)
            return None

    async def create_yookassa_sbp_payment(
        self,
        db: AsyncSession,
        user_id: int | None,
        amount_kopeks: int,
        description: str,
        receipt_email: str | None = None,
        receipt_phone: str | None = None,
        metadata: dict[str, Any] | None = None,
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Создаёт платёж по СБП через YooKassa."""
        if not getattr(self, 'yookassa_service', None):
            logger.error('YooKassa сервис не инициализирован')
            return None

        payment_module = import_module('app.services.payment_service')

        try:
            amount_rubles = amount_kopeks / 100

            payment_metadata = metadata.copy() if metadata else {}

            # Всегда добавляем telegram_id в метаданные для возможности возврата платежа
            if user_id is not None and 'user_telegram_id' not in payment_metadata:
                try:
                    from app.database.crud.user import get_user_by_id

                    user = await get_user_by_id(db, user_id)
                    if user and user.telegram_id:
                        payment_metadata['user_telegram_id'] = str(user.telegram_id)
                        payment_metadata['user_username'] = user.username or ''
                except Exception as e:
                    logger.warning('Не удалось получить telegram_id для user_id', user_id=user_id, error=e)

            # Preserve existing type from metadata if passed (e.g., "trial")
            existing_type = payment_metadata.get('type')
            payment_metadata.update(
                {
                    'user_id': str(user_id) if user_id is not None else '',
                    'amount_kopeks': str(amount_kopeks),
                    'type': existing_type or 'balance_topup_sbp',
                }
            )

            yookassa_response = await self.yookassa_service.create_sbp_payment(
                amount=amount_rubles,
                currency='RUB',
                description=description,
                metadata=payment_metadata,
                receipt_email=receipt_email,
                receipt_phone=receipt_phone,
                return_url=return_url,
            )

            if not yookassa_response or yookassa_response.get('error'):
                logger.error('Ошибка создания платежа YooKassa СБП', yookassa_response=yookassa_response)
                return None

            local_payment = await payment_module.create_yookassa_payment(
                db=db,
                user_id=user_id,
                yookassa_payment_id=yookassa_response['id'],
                amount_kopeks=amount_kopeks,
                currency='RUB',
                description=description,
                status=yookassa_response['status'],
                confirmation_url=yookassa_response.get('confirmation_url'),  # Используем confirmation URL
                metadata_json=payment_metadata,
                payment_method_type='sbp',
                yookassa_created_at=None,
                test_mode=yookassa_response.get('test_mode', False),
            )

            logger.info(
                'Создан платеж YooKassa СБП на ₽ для пользователя',
                yookassa_response=yookassa_response['id'],
                amount_rubles=amount_rubles,
                user_id=user_id,
            )

            confirmation_token = (yookassa_response.get('confirmation', {}) or {}).get('confirmation_token')

            return {
                'local_payment_id': local_payment.id,
                'yookassa_payment_id': yookassa_response['id'],
                'confirmation_url': yookassa_response.get('confirmation_url'),  # URL для подтверждения
                'qr_confirmation_data': yookassa_response.get('qr_confirmation_data'),  # Данные для QR-кода
                'confirmation_token': confirmation_token,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'status': yookassa_response['status'],
                'created_at': local_payment.created_at,
            }

        except Exception as error:
            logger.error('Ошибка создания платежа YooKassa СБП', error=error)
            return None

    async def get_yookassa_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        """Запрашивает статус платежа в YooKassa и синхронизирует локальные данные."""

        payment_module = import_module('app.services.payment_service')

        payment = await payment_module.get_yookassa_payment_by_local_id(db, local_payment_id)
        if not payment:
            return None

        remote_data: dict[str, Any] | None = None

        if getattr(self, 'yookassa_service', None):
            try:
                remote_data = await self.yookassa_service.get_payment_info(  # type: ignore[union-attr]
                    payment.yookassa_payment_id
                )
            except Exception as error:  # pragma: no cover - defensive logging
                logger.error(
                    'Ошибка получения статуса YooKassa', yookassa_payment_id=payment.yookassa_payment_id, error=error
                )

        if remote_data:
            status = remote_data.get('status') or payment.status
            paid = bool(remote_data.get('paid', getattr(payment, 'is_paid', False)))
            captured_raw = remote_data.get('captured_at')
            captured_at = None
            if captured_raw:
                try:
                    captured_at = datetime.fromisoformat(str(captured_raw).replace('Z', '+00:00'))
                except Exception as parse_error:  # pragma: no cover - diagnostic log
                    logger.debug(
                        'Не удалось распарсить captured_at', captured_raw=captured_raw, parse_error=parse_error
                    )
                    captured_at = None

            payment_method_type = remote_data.get('payment_method_type')

            updated_payment = await payment_module.update_yookassa_payment_status(
                db,
                payment.yookassa_payment_id,
                status=status,
                is_paid=paid,
                is_captured=paid and status == 'succeeded',
                captured_at=captured_at,
                payment_method_type=payment_method_type,
            )

            if updated_payment:
                payment = updated_payment

        transaction_id = getattr(payment, 'transaction_id', None)

        if payment.status == 'succeeded' and getattr(payment, 'is_paid', False):
            if not transaction_id:
                try:
                    await db.refresh(payment)
                    transaction_id = getattr(payment, 'transaction_id', None)
                except Exception as refresh_error:  # pragma: no cover - defensive logging
                    logger.warning(
                        'Не удалось обновить состояние платежа YooKassa перед повторной обработкой',
                        yookassa_payment_id=payment.yookassa_payment_id,
                        refresh_error=refresh_error,
                        exc_info=True,
                    )

            if transaction_id:
                logger.info(
                    'Пропускаем повторную обработку платежа YooKassa : уже связан с транзакцией',
                    yookassa_payment_id=payment.yookassa_payment_id,
                    transaction_id=transaction_id,
                )
            else:
                try:
                    await self._process_successful_yookassa_payment(db, payment)
                except Exception as process_error:  # pragma: no cover - defensive logging
                    logger.error(
                        'Ошибка обработки успешного платежа YooKassa',
                        yookassa_payment_id=payment.yookassa_payment_id,
                        process_error=process_error,
                        exc_info=True,
                    )

        return {
            'payment': payment,
            'status': payment.status,
            'is_paid': getattr(payment, 'is_paid', False),
            'remote_data': remote_data,
        }

    async def _process_successful_yookassa_payment(
        self,
        db: AsyncSession,
        payment: YooKassaPayment,
        event_object: dict[str, Any] | None = None,
    ) -> bool:
        """Переносит успешный платёж YooKassa в транзакции и начисляет баланс пользователю."""
        try:
            from sqlalchemy import select

            from app.database.models import YooKassaPayment as YKPayment

            # Lock the payment row to prevent concurrent double-processing
            locked_result = await db.execute(select(YKPayment).where(YKPayment.id == payment.id).with_for_update())
            payment = locked_result.scalar_one()

            # Fast-path: already processed
            if getattr(payment, 'transaction_id', None):
                logger.info(
                    'Платеж YooKassa уже обработан (transaction_id=), пропускаем.',
                    yookassa_payment_id=payment.yookassa_payment_id,
                    transaction_id=payment.transaction_id,
                )
                return True

            # Reject test-mode payments in production
            if getattr(payment, 'test_mode', False) and not getattr(settings, 'YOOKASSA_TEST_MODE', False):
                logger.warning(
                    'YooKassa: rejecting test_mode payment in production',
                    yookassa_payment_id=payment.yookassa_payment_id,
                )
                return False

            payment_module = import_module('app.services.payment_service')

            # Проверяем, не обрабатывается ли уже этот платеж (защита от дублирования)
            get_transaction_by_external_id = getattr(payment_module, 'get_transaction_by_external_id', None)
            existing_transaction = None
            if get_transaction_by_external_id:
                try:
                    existing_transaction = await get_transaction_by_external_id(  # type: ignore[attr-defined]
                        db,
                        payment.yookassa_payment_id,
                        PaymentMethod.YOOKASSA,
                    )
                except AttributeError:
                    logger.debug('get_transaction_by_external_id недоступен, пропускаем проверку дубликатов')

            if existing_transaction:
                # Если транзакция уже существует, просто завершаем обработку
                logger.info(
                    'Платеж YooKassa уже был обработан транзакцией . Пропускаем повторную обработку.',
                    yookassa_payment_id=payment.yookassa_payment_id,
                    existing_transaction_id=existing_transaction.id,
                )

                # Убедимся, что платеж связан с транзакцией
                if not getattr(payment, 'transaction_id', None):
                    try:
                        linked_payment = await payment_module.link_yookassa_payment_to_transaction(  # type: ignore[attr-defined]
                            db,
                            payment.yookassa_payment_id,
                            existing_transaction.id,
                        )
                        if linked_payment:
                            payment.transaction_id = getattr(
                                linked_payment,
                                'transaction_id',
                                existing_transaction.id,
                            )
                            if hasattr(linked_payment, 'transaction'):
                                payment.transaction = linked_payment.transaction
                    except Exception as link_error:  # pragma: no cover - защитный лог
                        logger.warning(
                            'Не удалось привязать платеж YooKassa к существующей транзакции',
                            yookassa_payment_id=payment.yookassa_payment_id,
                            existing_transaction_id=existing_transaction.id,
                            link_error=link_error,
                            exc_info=True,
                        )

                await db.commit()
                return True

            payment_metadata: dict[str, Any] = {}
            try:
                if hasattr(payment, 'metadata_json') and payment.metadata_json:
                    import json

                    if isinstance(payment.metadata_json, str):
                        payment_metadata = json.loads(payment.metadata_json)
                    elif isinstance(payment.metadata_json, dict):
                        payment_metadata = payment.metadata_json
                    logger.info('Метаданные платежа', payment_metadata=payment_metadata)
            except Exception as parse_error:
                logger.error('Ошибка парсинга метаданных платежа', parse_error=parse_error)

            invoice_message = payment_metadata.get('invoice_message') or {}
            if getattr(self, 'bot', None):
                chat_id = invoice_message.get('chat_id')
                message_id = invoice_message.get('message_id')
                if chat_id and message_id:
                    try:
                        await self.bot.delete_message(chat_id, message_id)
                    except Exception as delete_error:  # pragma: no cover - depends on bot rights
                        logger.warning(
                            'Не удалось удалить сообщение YooKassa', message_id=message_id, delete_error=delete_error
                        )
                    else:
                        payment_metadata.pop('invoice_message', None)

            processing_completed = bool(payment_metadata.get('processing_completed'))

            transaction = None

            existing_transaction_id = getattr(payment, 'transaction_id', None)
            if existing_transaction_id:
                try:
                    from app.database.crud.transaction import get_transaction_by_id

                    transaction = await get_transaction_by_id(db, existing_transaction_id)
                except Exception as fetch_error:  # pragma: no cover - диагностический лог
                    logger.warning(
                        'Не удалось получить транзакцию для платежа YooKassa',
                        existing_transaction_id=existing_transaction_id,
                        yookassa_payment_id=payment.yookassa_payment_id,
                        fetch_error=fetch_error,
                        exc_info=True,
                    )

                if transaction and processing_completed:
                    logger.info(
                        'Пропускаем повторную обработку платежа YooKassa : транзакция уже завершила начисление.',
                        yookassa_payment_id=payment.yookassa_payment_id,
                        existing_transaction_id=existing_transaction_id,
                    )
                    return True

                if transaction:
                    logger.info(
                        'Транзакция для платежа YooKassa найдена, но обработка ранее не была завершена — повторяем критические шаги.',
                        existing_transaction_id=existing_transaction_id,
                        yookassa_payment_id=payment.yookassa_payment_id,
                    )

            if transaction is None:
                get_transaction_by_external_id = getattr(payment_module, 'get_transaction_by_external_id', None)
                existing_transaction = None

                if get_transaction_by_external_id:
                    try:
                        existing_transaction = await get_transaction_by_external_id(  # type: ignore[attr-defined]
                            db,
                            payment.yookassa_payment_id,
                            PaymentMethod.YOOKASSA,
                        )
                    except AttributeError:
                        logger.debug('get_transaction_by_external_id недоступен, пропускаем проверку дубликатов')

                if existing_transaction:
                    # Если транзакция уже существует, пропускаем обработку
                    logger.info(
                        'Платеж YooKassa уже был обработан транзакцией . Пропускаем повторную обработку.',
                        yookassa_payment_id=payment.yookassa_payment_id,
                        existing_transaction_id=existing_transaction.id,
                    )

                    # Убедимся, что платеж связан с транзакцией
                    if not getattr(payment, 'transaction_id', None):
                        try:
                            linked_payment = await payment_module.link_yookassa_payment_to_transaction(  # type: ignore[attr-defined]
                                db,
                                payment.yookassa_payment_id,
                                existing_transaction.id,
                            )
                            if linked_payment:
                                payment.transaction_id = getattr(
                                    linked_payment,
                                    'transaction_id',
                                    existing_transaction.id,
                                )
                                if hasattr(linked_payment, 'transaction'):
                                    payment.transaction = linked_payment.transaction
                        except Exception as link_error:  # pragma: no cover - защитный лог
                            logger.warning(
                                'Не удалось привязать платеж YooKassa к существующей транзакции',
                                yookassa_payment_id=payment.yookassa_payment_id,
                                existing_transaction_id=existing_transaction.id,
                                link_error=link_error,
                                exc_info=True,
                            )

                    await db.commit()
                    return True

            # --- Guest purchase flow (landing page) ---------------------------
            webhook_amount_kopeks = payment.amount_kopeks

            from app.services.payment.common import try_fulfill_guest_purchase

            guest_result = await try_fulfill_guest_purchase(
                db,
                metadata=payment_metadata,
                payment_amount_kopeks=webhook_amount_kopeks,
                provider_payment_id=payment.yookassa_payment_id,
                provider_name='yookassa',
            )
            if guest_result is not None:
                return True

            # --- Standard user payment flow ------------------------------------
            payment_description = getattr(payment, 'description', 'YooKassa платеж')

            payment_purpose = payment_metadata.get('payment_purpose', '')
            payment_type = payment_metadata.get('type', '')
            is_simple_subscription = payment_purpose == 'simple_subscription_purchase'
            is_trial_payment = payment_type == 'trial'
            is_recurrent_topup = payment_metadata.get('purpose') == 'recurrent_topup'

            transaction_type = (
                TransactionType.SUBSCRIPTION_PAYMENT
                if is_simple_subscription or is_trial_payment
                else TransactionType.DEPOSIT
            )
            transaction_description = (
                f'Оплата подписки через YooKassa: {payment_description}'
                if is_simple_subscription
                else f'Оплата пробной подписки через YooKassa: {payment_description}'
                if is_trial_payment
                else f'Пополнение через YooKassa: {payment_description}'
            )

            if transaction is None:
                transaction = await payment_module.create_transaction(
                    db=db,
                    user_id=payment.user_id,
                    type=transaction_type,
                    amount_kopeks=payment.amount_kopeks,
                    description=transaction_description,
                    payment_method=PaymentMethod.YOOKASSA,
                    external_id=payment.yookassa_payment_id,
                    is_completed=True,
                    created_at=getattr(payment, 'created_at', None),
                    commit=False,
                )

            if not getattr(payment, 'transaction_id', None):
                linked_payment = await payment_module.link_yookassa_payment_to_transaction(
                    db,
                    payment.yookassa_payment_id,
                    transaction.id,
                )

                if linked_payment:
                    payment.transaction_id = getattr(linked_payment, 'transaction_id', transaction.id)
                    if hasattr(linked_payment, 'transaction'):
                        payment.transaction = linked_payment.transaction

            critical_flow_completed = False
            processing_marked = False

            user = await payment_module.get_user_by_id(db, payment.user_id)
            if user:
                if is_trial_payment:
                    # Обработка платного триала
                    logger.info(
                        'YooKassa платеж обработан как оплата триала. Баланс пользователя не изменяется.',
                        yookassa_payment_id=payment.yookassa_payment_id,
                        user_id=user.id,
                    )
                    try:
                        subscription_id = payment_metadata.get('subscription_id')
                        if subscription_id:
                            from app.database.crud.subscription import activate_pending_trial_subscription
                            from app.services.admin_notification_service import AdminNotificationService
                            from app.services.subscription_service import SubscriptionService

                            subscription = await activate_pending_trial_subscription(
                                db=db,
                                subscription_id=int(subscription_id),
                                user_id=user.id,
                            )

                            if subscription:
                                logger.info(
                                    'Триальная подписка активирована для пользователя',
                                    subscription_id=subscription_id,
                                    user_id=user.id,
                                )

                                # Создаем пользователя в RemnaWave
                                subscription_service = SubscriptionService()
                                try:
                                    await subscription_service.create_remnawave_user(db, subscription)
                                except Exception as rw_error:
                                    logger.error('Ошибка создания RemnaWave для триала', rw_error=rw_error)

                                # Уведомление админам
                                if getattr(self, 'bot', None):
                                    try:
                                        admin_notification_service = AdminNotificationService(self.bot)
                                        await admin_notification_service.send_trial_activation_notification(
                                            user=user,
                                            subscription=subscription,
                                            paid_amount=payment.amount_kopeks,
                                            payment_method='YooKassa',
                                        )
                                    except Exception as admin_error:
                                        logger.warning('Ошибка уведомления админов о триале', admin_error=admin_error)

                                # Уведомление пользователю (только для Telegram-пользователей)
                                if getattr(self, 'bot', None) and user.telegram_id:
                                    try:
                                        await self.bot.send_message(
                                            chat_id=user.telegram_id,
                                            text=(
                                                f'🎉 <b>Пробная подписка активирована!</b>\n\n'
                                                f'💳 Оплачено: {settings.format_price(payment.amount_kopeks)}\n'
                                                f'📅 Период: {settings.TRIAL_DURATION_DAYS} дней\n'
                                                f'📱 Устройств: {subscription.device_limit}\n\n'
                                                f'Используйте меню для подключения к VPN.'
                                            ),
                                            parse_mode='HTML',
                                        )
                                    except Exception as notify_error:
                                        logger.warning(
                                            'Ошибка уведомления пользователя о триале', notify_error=notify_error
                                        )
                                elif not user.telegram_id:
                                    logger.info(
                                        'Пропуск Telegram-уведомления о триале для email-пользователя', user_id=user.id
                                    )
                            else:
                                logger.error(
                                    'Не удалось активировать триал для',
                                    subscription_id=subscription_id,
                                    user_id=user.id,
                                )
                        else:
                            logger.error('Отсутствует subscription_id в metadata триального платежа YooKassa')
                    except Exception as trial_error:
                        logger.error(
                            'Ошибка обработки триального платежа YooKassa', trial_error=trial_error, exc_info=True
                        )

                elif is_simple_subscription:
                    logger.info(
                        'YooKassa платеж обработан как покупка подписки. Баланс пользователя не изменяется.',
                        yookassa_payment_id=payment.yookassa_payment_id,
                        user_id=user.id,
                    )

                    # Начисляем реферальную комиссию за прямую покупку подписки
                    try:
                        from app.services.referral_service import process_referral_topup

                        await process_referral_topup(
                            db,
                            user.id,
                            payment.amount_kopeks,
                            getattr(self, 'bot', None),
                        )
                    except Exception as ref_error:
                        logger.error(
                            'Ошибка реферального начисления при покупке подписки YooKassa', ref_error=ref_error
                        )
                else:
                    # Lock user row to prevent concurrent balance race conditions
                    from app.database.crud.user import lock_user_for_update

                    user = await lock_user_for_update(db, user)

                    old_balance = user.balance_kopeks
                    was_first_topup = not user.has_made_first_topup

                    user.balance_kopeks += payment.amount_kopeks
                    user.updated_at = datetime.now(UTC)

                    # Обновляем пользователя с нужными связями, чтобы избежать проблем с ленивой загрузкой
                    from sqlalchemy.orm import selectinload

                    from app.database.models import Subscription as SubscriptionModel, User

                    # Загружаем пользователя с подпиской и промо-группой
                    full_user_result = await db.execute(
                        select(User)
                        .options(selectinload(User.subscriptions).selectinload(SubscriptionModel.tariff))
                        .options(selectinload(User.user_promo_groups))
                        .where(User.id == user.id)
                    )
                    full_user = full_user_result.scalar_one_or_none()

                    # Используем обновленные данные или исходные, если не удалось обновить
                    full_subs = getattr(full_user, 'subscriptions', []) if full_user else []
                    fallback_subs = getattr(user, 'subscriptions', [])
                    all_subs = full_subs or fallback_subs
                    _active = [s for s in all_subs if s.status in ('active', 'trial')]
                    if _active:
                        _non_daily = [s for s in _active if not getattr(s, 'is_daily_tariff', False)]
                        _pool = _non_daily or _active
                        subscription = max(_pool, key=lambda s: s.days_left)
                    else:
                        subscription = all_subs[0] if all_subs else None

                    # Validate subscription_id from metadata matches the resolved subscription
                    if is_recurrent_topup and subscription is not None:
                        _meta_sub_id = payment_metadata.get('subscription_id')
                        if _meta_sub_id and str(subscription.id) != _meta_sub_id:
                            # Heuristic picked the wrong sub — resolve correct one from metadata
                            try:
                                _correct_sub_id = int(_meta_sub_id)
                                _correct_sub = next(
                                    (s for s in all_subs if s.id == _correct_sub_id),
                                    None,
                                )
                                if _correct_sub:
                                    logger.info(
                                        'Recurrent payment: resolved correct subscription from metadata',
                                        meta_sub_id=_meta_sub_id,
                                        heuristic_sub_id=subscription.id,
                                        user_id=user.id,
                                    )
                                    subscription = _correct_sub
                                else:
                                    logger.warning(
                                        'Recurrent payment subscription_id mismatch, metadata sub not found in user subs',
                                        expected_sub_id=_meta_sub_id,
                                        actual_sub_id=subscription.id,
                                        user_id=user.id,
                                    )
                            except (ValueError, TypeError):
                                logger.warning(
                                    'Recurrent payment: invalid subscription_id in metadata',
                                    meta_sub_id=_meta_sub_id,
                                    user_id=user.id,
                                )

                    promo_group = (
                        full_user.get_primary_promo_group()
                        if full_user
                        else (user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None)
                    )

                    # Используем full_user для форматирования реферальной информации, чтобы избежать проблем с ленивой загрузкой
                    user_for_referrer = full_user or user
                    referrer_info = format_referrer_info(user_for_referrer)
                    topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'

                    payment_metadata = await self._mark_yookassa_payment_processing_completed(
                        db,
                        payment,
                        payment_metadata,
                        commit=False,
                    )
                    processing_marked = True

                    await db.commit()

                    # Emit deferred side-effects after atomic commit
                    try:
                        from app.database.crud.transaction import emit_transaction_side_effects

                        await emit_transaction_side_effects(
                            db,
                            transaction,
                            amount_kopeks=payment.amount_kopeks,
                            user_id=payment.user_id,
                            type=transaction_type,
                            payment_method=PaymentMethod.YOOKASSA,
                            external_id=payment.yookassa_payment_id,
                        )
                    except Exception as error:
                        logger.warning('Failed to emit YooKassa transaction side effects', error=error)

                    try:
                        from app.services.referral_service import process_referral_topup

                        await process_referral_topup(
                            db,
                            user.id,
                            payment.amount_kopeks,
                            getattr(self, 'bot', None),
                        )
                    except Exception as error:
                        logger.error('Ошибка обработки реферального пополнения YooKassa', error=error)

                    if was_first_topup and not getattr(user, 'has_made_first_topup', False) and not user.referred_by_id:
                        user.has_made_first_topup = True
                        await db.commit()

                    await db.refresh(user)

                    # Отправляем уведомления админам
                    if getattr(self, 'bot', None):
                        try:
                            from app.services.admin_notification_service import (
                                AdminNotificationService,
                            )

                            notification_service = AdminNotificationService(self.bot)
                            # Перезагрузка user при lazy-loading ошибке
                            # происходит внутри send_balance_topup_notification
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
                            logger.info('Уведомление админам о пополнении отправлено успешно')
                        except Exception as error:
                            logger.error(
                                'Ошибка отправки уведомления админам о YooKassa пополнении', error=error, exc_info=True
                            )

                    # Для рекуррентных автоплатежей уведомления отправляет recurrent_payment_service
                    if not is_recurrent_topup:
                        # Отправляем уведомление пользователю (только Telegram-пользователям)
                        if getattr(self, 'bot', None) and user.telegram_id:
                            try:
                                # Передаем только простые данные, чтобы избежать проблем с ленивой загрузкой
                                await self._send_payment_success_notification(
                                    user.telegram_id,
                                    payment.amount_kopeks,
                                    user=None,  # Передаем None, чтобы _ensure_user_snapshot загрузил данные сам
                                    db=db,
                                    payment_method_title='Банковская карта (YooKassa)',
                                )
                                logger.info('Уведомление пользователю о платеже отправлено успешно')
                            except Exception as error:
                                logger.error('Ошибка отправки уведомления о платеже', error=error, exc_info=True)

                        # Проверяем наличие сохраненной корзины для возврата к оформлению подписки
                        # ВАЖНО: этот код должен выполняться даже при ошибках в уведомлениях
                        try:
                            from app.services.payment.common import send_cart_notification_after_topup

                            await send_cart_notification_after_topup(
                                user, payment.amount_kopeks, db, getattr(self, 'bot', None)
                            )
                        except Exception as e:
                            logger.error(
                                'Ошибка при работе с сохраненной корзиной для пользователя',
                                user_id=user.id,
                                error=e,
                                exc_info=True,
                            )

                if is_simple_subscription:
                    logger.info('Обнаружен платеж простой покупки подписки для пользователя', user_id=user.id)
                    try:
                        # Активируем подписку
                        from app.services.subscription_service import SubscriptionService

                        subscription_service = SubscriptionService()

                        # Получаем параметры подписки из метаданных
                        subscription_period = int(payment_metadata.get('subscription_period', 30))
                        order_id = payment_metadata.get('order_id')

                        logger.info(
                            'Активация подписки: период= дней, заказ',
                            subscription_period=subscription_period,
                            order_id=order_id,
                        )

                        # Активируем pending подписку пользователя
                        from app.database.crud.subscription import activate_pending_subscription

                        order_subscription_id = int(order_id) if order_id is not None else None
                        subscription = await activate_pending_subscription(
                            db=db,
                            user_id=user.id,
                            period_days=subscription_period,
                            subscription_id=order_subscription_id,
                        )

                        if subscription:
                            logger.info('Подписка успешно активирована для пользователя', user_id=user.id)

                            # Consume promo-offer discount (invoice was created with discounted price)
                            try:
                                from app.utils.promo_offer import consume_user_promo_offer

                                await consume_user_promo_offer(db, user.id)
                            except Exception as promo_error:
                                logger.warning(
                                    'Ошибка потребления промо-оффера при YooKassa оплате',
                                    user_id=user.id,
                                    error=promo_error,
                                )

                            # Обновляем данные подписки в RemnaWave, чтобы получить актуальные ссылки
                            try:
                                remnawave_user = await subscription_service.create_remnawave_user(db, subscription)
                                if remnawave_user:
                                    await db.refresh(subscription)
                            except Exception as sync_error:
                                logger.error(
                                    'Ошибка синхронизации подписки с RemnaWave для пользователя',
                                    user_id=user.id,
                                    sync_error=sync_error,
                                    exc_info=True,
                                )

                            # Отправляем уведомление пользователю об активации подписки (только Telegram)
                            if getattr(self, 'bot', None) and user.telegram_id:
                                from aiogram import types

                                tariff_line = ''
                                if settings.is_multi_tariff_enabled() and getattr(subscription, 'tariff_id', None):
                                    try:
                                        from app.database.crud.tariff import get_tariff_by_id

                                        _t = await get_tariff_by_id(db, subscription.tariff_id)
                                        if _t:
                                            tariff_line = f'\n📦 Тариф: «{_t.name}»'
                                    except Exception:
                                        pass
                                success_message = (
                                    f'✅ <b>Подписка успешно активирована!</b>\n\n'
                                    f'📅 Период: {subscription_period} дней\n'
                                    f'📱 Устройства: 1\n'
                                    f'📊 Трафик: Безлимит\n'
                                    f'💳 Оплата: {settings.format_price(payment.amount_kopeks)} (YooKassa)'
                                    f'{tariff_line}\n\n'
                                    f"🔗 Для подключения перейдите в раздел 'Моя подписка'"
                                )

                                keyboard = types.InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            types.InlineKeyboardButton(
                                                text='📱 Моя подписка', callback_data='menu_subscription'
                                            )
                                        ],
                                        [
                                            types.InlineKeyboardButton(
                                                text='🏠 Главное меню', callback_data='back_to_menu'
                                            )
                                        ],
                                    ]
                                )

                                await self.bot.send_message(
                                    chat_id=user.telegram_id,
                                    text=success_message,
                                    reply_markup=keyboard,
                                    parse_mode='HTML',
                                )
                            elif not user.telegram_id:
                                logger.info(
                                    'Пропуск Telegram-уведомления о подписке для email-пользователя', user_id=user.id
                                )

                            if getattr(self, 'bot', None):
                                try:
                                    from app.services.admin_notification_service import (
                                        AdminNotificationService,
                                    )

                                    notification_service = AdminNotificationService(self.bot)

                                    # Обновляем пользователя с нужными связями, чтобы избежать проблем с ленивой загрузкой
                                    from sqlalchemy import select
                                    from sqlalchemy.orm import selectinload

                                    from app.database.models import Subscription as SubscriptionModel, User

                                    # Загружаем пользователя с подпиской и промо-группой
                                    full_user_result = await db.execute(
                                        select(User)
                                        .options(
                                            selectinload(User.subscriptions).selectinload(SubscriptionModel.tariff)
                                        )
                                        .options(selectinload(User.user_promo_groups))
                                        .where(User.id == user.id)
                                    )
                                    full_user = full_user_result.scalar_one_or_none()

                                    await notification_service.send_subscription_purchase_notification(
                                        db,
                                        full_user or user,
                                        subscription,
                                        transaction,
                                        subscription_period,
                                        was_trial_conversion=False,
                                        purchase_type='renewal'
                                        if (full_user or user).has_had_paid_subscription
                                        else 'first_purchase',
                                    )
                                except Exception as admin_error:
                                    logger.error(
                                        'Ошибка отправки уведомления админам о покупке подписки через YooKassa',
                                        admin_error=admin_error,
                                        exc_info=True,
                                    )
                        else:
                            logger.error('Ошибка активации подписки для пользователя', user_id=user.id)
                    except Exception as e:
                        logger.error(
                            'Ошибка активации подписки для пользователя', user_id=user.id, error=e, exc_info=True
                        )

                    if not processing_marked:
                        payment_metadata = await self._mark_yookassa_payment_processing_completed(
                            db,
                            payment,
                            payment_metadata,
                            commit=True,
                        )
                        processing_marked = True

                critical_flow_completed = True
            else:
                logger.warning(
                    'Пользователь для платежа YooKassa не найден — начисление баланса невозможно',
                    user_id=payment.user_id,
                    yookassa_payment_id=payment.yookassa_payment_id,
                )

            if critical_flow_completed and not processing_marked:
                payment_metadata = await self._mark_yookassa_payment_processing_completed(
                    db,
                    payment,
                    payment_metadata,
                    commit=True,
                )

            if is_simple_subscription:
                logger.info(
                    'Успешно обработан платеж YooKassa как покупка подписки: пользователь , сумма ₽',
                    yookassa_payment_id=payment.yookassa_payment_id,
                    user_id=payment.user_id,
                    amount_rubles=payment.amount_kopeks / 100,
                )
            else:
                logger.info(
                    'Успешно обработан платеж YooKassa : пользователь пополнил баланс на ₽',
                    yookassa_payment_id=payment.yookassa_payment_id,
                    user_id=payment.user_id,
                    amount_rubles=payment.amount_kopeks / 100,
                )

            # Сохраняем привязанный метод оплаты для рекуррентных платежей
            if settings.YOOKASSA_RECURRENT_ENABLED and event_object:
                await self._save_payment_method_if_available(db, payment, event_object)

            # Создаем чек через NaloGO (если NALOGO_ENABLED=true)
            if hasattr(self, 'nalogo_service') and self.nalogo_service:
                await self._create_nalogo_receipt(
                    db=db,
                    payment=payment,
                    transaction=transaction,
                    telegram_user_id=user.telegram_id if user else None,
                )

            return True

        except Exception as error:
            logger.error(
                'Ошибка обработки успешного платежа YooKassa',
                yookassa_payment_id=payment.yookassa_payment_id,
                error=error,
            )
            return False

    async def _mark_yookassa_payment_processing_completed(
        self,
        db: AsyncSession,
        payment: YooKassaPayment,
        payment_metadata: dict[str, Any],
        *,
        commit: bool = False,
    ) -> dict[str, Any]:
        """Отмечает платёж как полностью обработанный, чтобы избежать повторного начисления."""

        if payment_metadata.get('processing_completed'):
            return payment_metadata

        updated_metadata = dict(payment_metadata)
        updated_metadata['processing_completed'] = True

        try:
            from sqlalchemy import update

            from app.database.models import YooKassaPayment as YooKassaPaymentModel

            await db.execute(
                update(YooKassaPaymentModel)
                .where(YooKassaPaymentModel.id == payment.id)
                .values(metadata_json=updated_metadata, updated_at=datetime.now(UTC))
            )
            if commit:
                await db.commit()
            else:
                await db.flush()
            payment.metadata_json = updated_metadata
        except Exception as mark_error:  # pragma: no cover - защитный лог
            logger.warning(
                'Не удалось отметить платеж YooKassa как завершенный',
                yookassa_payment_id=payment.yookassa_payment_id,
                mark_error=mark_error,
                exc_info=True,
            )

        return updated_metadata

    async def _save_payment_method_if_available(
        self,
        db: AsyncSession,
        payment: YooKassaPayment,
        event_object: dict[str, Any],
    ) -> None:
        """Сохраняет привязанный метод оплаты из ответа YooKassa, если карта была сохранена."""
        try:
            pm = event_object.get('payment_method') or {}
            pm_id = pm.get('id')
            pm_saved = pm.get('saved', False)

            if not pm_id or not pm_saved:
                return

            from app.database.crud.saved_payment_method import (
                create_saved_payment_method,
                get_payment_method_by_yookassa_id,
            )

            # Проверяем, не сохранён ли уже (включая деактивированные —
            # если пользователь удалил карту, не реактивируем её)
            existing = await get_payment_method_by_yookassa_id(db, pm_id, include_inactive=True)
            if existing:
                logger.debug(
                    'Метод оплаты уже сохранён',
                    yookassa_payment_method_id=pm_id,
                    user_id=payment.user_id,
                    is_active=existing.is_active,
                )
                return

            # Извлекаем данные карты
            card = pm.get('card') or {}
            card_first6 = card.get('first6')
            card_last4 = card.get('last4')
            card_type = card.get('card_type')
            raw_month = card.get('expiry_month')
            raw_year = card.get('expiry_year')
            expiry_month = str(raw_month).zfill(2) if raw_month is not None else None
            expiry_year = str(raw_year) if raw_year is not None else None
            method_type = pm.get('type', 'bank_card')

            # Формируем title — только реквизиты без названия метода
            # (локализованное название подставляется в UI через _get_payment_method_display_name)
            title = None
            if card_last4:
                type_label = card_type or 'Card'
                title = f'{type_label} *{card_last4}'
            elif method_type != 'bank_card':
                # Для не-карточных методов: yoo_money (account_number), sbp/sberbank (phone) и т.д.
                account = pm.get('account_number') or pm.get('phone')
                if account:
                    masked = account[-4:] if len(account) >= 4 else account
                    title = f'*{masked}'

            saved = await create_saved_payment_method(
                db=db,
                user_id=payment.user_id,
                yookassa_payment_method_id=pm_id,
                method_type=method_type,
                card_first6=card_first6,
                card_last4=card_last4,
                card_type=card_type,
                card_expiry_month=expiry_month,
                card_expiry_year=expiry_year,
                title=title,
            )

            if saved:
                logger.info(
                    'Метод оплаты сохранён для рекуррентных платежей',
                    saved_method_id=saved.id,
                    user_id=payment.user_id,
                    card_last4=card_last4,
                    method_type=method_type,
                )

        except Exception as save_error:
            logger.error(
                'Ошибка сохранения метода оплаты',
                yookassa_payment_id=payment.yookassa_payment_id,
                save_error=save_error,
                exc_info=True,
            )

    async def _create_nalogo_receipt(
        self,
        db: AsyncSession,
        payment: YooKassaPayment,
        transaction: Transaction | None = None,
        telegram_user_id: int | None = None,
    ) -> None:
        """Создание чека через NaloGO для успешного платежа."""
        if not hasattr(self, 'nalogo_service') or not self.nalogo_service:
            logger.debug('NaloGO сервис не инициализирован, чек не создан')
            return

        # Защита от дублей: если у транзакции уже есть чек — не создаём новый
        if transaction and getattr(transaction, 'receipt_uuid', None):
            logger.info(
                'Чек для платежа уже создан: пропускаем повторное создание',
                yookassa_payment_id=payment.yookassa_payment_id,
                receipt_uuid=transaction.receipt_uuid,
            )
            return

        try:
            amount_rubles = payment.amount_kopeks / 100
            # Формируем описание из настроек (включает сумму и ID пользователя)
            receipt_name = settings.get_balance_payment_description(
                payment.amount_kopeks, telegram_user_id=telegram_user_id
            )

            receipt_uuid = await self.nalogo_service.create_receipt(
                name=receipt_name,
                amount=amount_rubles,
                quantity=1,
                payment_id=payment.yookassa_payment_id,
                telegram_user_id=telegram_user_id,
                amount_kopeks=payment.amount_kopeks,
            )

            if receipt_uuid:
                logger.info(
                    'Чек NaloGO создан для платежа',
                    yookassa_payment_id=payment.yookassa_payment_id,
                    receipt_uuid=receipt_uuid,
                )

                # Сохраняем receipt_uuid в транзакцию
                if transaction:
                    try:
                        transaction.receipt_uuid = receipt_uuid
                        transaction.receipt_created_at = datetime.now(UTC)
                        await db.commit()
                        logger.debug(
                            'Чек привязан к транзакции', receipt_uuid=receipt_uuid, transaction_id=transaction.id
                        )
                    except Exception as save_error:
                        logger.warning('Не удалось сохранить receipt_uuid в транзакцию', save_error=save_error)
            # При временной недоступности чек добавляется в очередь автоматически

        except Exception as error:
            logger.error(
                'Ошибка создания чека NaloGO для платежа',
                yookassa_payment_id=payment.yookassa_payment_id,
                error=error,
                exc_info=True,
            )

    async def process_yookassa_webhook(
        self,
        db: AsyncSession,
        event: dict[str, Any],
    ) -> bool:
        """Обрабатывает входящий webhook YooKassa и синхронизирует состояние платежа."""
        event_object = event.get('object', {})
        yookassa_payment_id = event_object.get('id')

        if not yookassa_payment_id:
            logger.warning('Webhook без payment id', event=event)
            return False

        remote_data: dict[str, Any] | None = None
        if getattr(self, 'yookassa_service', None):
            try:
                remote_data = await self.yookassa_service.get_payment_info(  # type: ignore[union-attr]
                    yookassa_payment_id
                )
            except Exception as error:  # pragma: no cover - диагностический лог
                logger.warning(
                    'Не удалось запросить актуальный статус платежа YooKassa',
                    yookassa_payment_id=yookassa_payment_id,
                    error=error,
                    exc_info=True,
                )

        if remote_data:
            previous_status = event_object.get('status')
            event_object = self._merge_remote_yookassa_payload(event_object, remote_data)
            if previous_status and event_object.get('status') != previous_status:
                logger.info(
                    'Статус платежа YooKassa скорректирован по данным API: →',
                    yookassa_payment_id=yookassa_payment_id,
                    previous_status=previous_status,
                    event_object=event_object.get('status'),
                )
            event['object'] = event_object

        payment_module = import_module('app.services.payment_service')

        payment = await payment_module.get_yookassa_payment_by_id(db, yookassa_payment_id)
        if not payment:
            logger.warning('Локальный платеж для YooKassa id не найден', yookassa_payment_id=yookassa_payment_id)
            payment = await self._restore_missing_yookassa_payment(db, event_object)

            if not payment:
                logger.error(
                    'Не удалось восстановить локальную запись платежа YooKassa', yookassa_payment_id=yookassa_payment_id
                )
                return False

        payment.status = event_object.get('status', payment.status)
        payment.confirmation_url = self._extract_confirmation_url(event_object)

        payment.payment_method_type = (event_object.get('payment_method') or {}).get(
            'type'
        ) or payment.payment_method_type
        payment.refundable = event_object.get('refundable', getattr(payment, 'refundable', False))

        current_paid = bool(getattr(payment, 'is_paid', getattr(payment, 'paid', False)))
        payment.is_paid = bool(event_object.get('paid', current_paid))

        captured_at_raw = event_object.get('captured_at')
        if captured_at_raw:
            try:
                payment.captured_at = datetime.fromisoformat(captured_at_raw.replace('Z', '+00:00'))
            except Exception as error:
                logger.debug('Не удалось распарсить captured_at', captured_at_raw=captured_at_raw, error=error)

        await db.commit()
        await db.refresh(payment)

        if payment.status == 'succeeded' and payment.is_paid:
            return await self._process_successful_yookassa_payment(db, payment, event_object=event_object)

        logger.info(
            'Webhook YooKassa обновил платеж до статуса',
            yookassa_payment_id=yookassa_payment_id,
            payment_status=payment.status,
        )
        return True

    async def _restore_missing_yookassa_payment(
        self,
        db: AsyncSession,
        event_object: dict[str, Any],
    ) -> YooKassaPayment | None:
        """Создает локальную запись платежа на основе данных webhook, если она отсутствует."""

        yookassa_payment_id = event_object.get('id')
        if not yookassa_payment_id:
            return None

        metadata = self._normalise_yookassa_metadata(event_object.get('metadata'))
        user_id_raw = metadata.get('user_id')
        if user_id_raw is None:
            user_id_raw = metadata.get('userId')

        if user_id_raw is None:
            logger.error(
                'Webhook YooKassa не содержит user_id в metadata. Невозможно восстановить платеж.',
                yookassa_payment_id=yookassa_payment_id,
            )
            return None

        try:
            user_id = int(user_id_raw)
        except (TypeError, ValueError):
            logger.error(
                'Webhook YooKassa содержит некорректный user_id',
                yookassa_payment_id=yookassa_payment_id,
                user_id_raw=user_id_raw,
            )
            return None

        if user_id <= 0:
            logger.error(
                'Webhook YooKassa содержит неположительный user_id',
                yookassa_payment_id=yookassa_payment_id,
                user_id=user_id,
            )
            return None

        # Verify user exists before creating FK-linked record.
        # Legacy payments may have telegram_id stored in metadata['user_id']
        # instead of the internal User.id. Detect by checking int32 range.
        user: User | None = None

        try:
            from app.database.crud.user import get_user_by_id, get_user_by_telegram_id

            if user_id <= _INT32_MAX:
                user = await get_user_by_id(db, user_id)
                # Cross-validate: if metadata also has telegram_id, verify it matches
                if user:
                    meta_tg = metadata.get('user_telegram_id') or metadata.get('userTelegramId')
                    if meta_tg is not None:
                        try:
                            expected_tg = int(meta_tg)
                        except (TypeError, ValueError):
                            expected_tg = None
                        if expected_tg and user.telegram_id != expected_tg:
                            logger.warning(
                                'Webhook YooKassa: user_id совпал, но telegram_id не совпадает — '
                                'вероятно legacy metadata, ищем по telegram_id',
                                yookassa_payment_id=yookassa_payment_id,
                                user_id=user_id,
                                user_telegram_id=user.telegram_id,
                                expected_telegram_id=expected_tg,
                            )
                            user = await get_user_by_telegram_id(db, expected_tg)
            else:
                # user_id exceeds int32 — это telegram_id из legacy-платежа
                logger.warning(
                    'Webhook YooKassa: metadata[user_id] превышает int32, ищем как telegram_id',
                    yookassa_payment_id=yookassa_payment_id,
                    suspected_telegram_id=user_id,
                )
                user = await get_user_by_telegram_id(db, user_id)

            # Fallback: try user_telegram_id from metadata if primary lookup failed
            if not user:
                tg_id_raw = metadata.get('user_telegram_id')
                if tg_id_raw is None:
                    tg_id_raw = metadata.get('userTelegramId')
                if tg_id_raw is not None:
                    try:
                        tg_id = int(tg_id_raw)
                    except (TypeError, ValueError):
                        tg_id = None
                    if tg_id and tg_id > 0:
                        user = await get_user_by_telegram_id(db, tg_id)

            if not user:
                logger.warning(
                    'Webhook YooKassa: пользователь не найден, пропускаем восстановление платежа',
                    yookassa_payment_id=yookassa_payment_id,
                    user_id=user_id,
                    user_telegram_id=metadata.get('user_telegram_id'),
                )
                return None

            # Use the resolved internal ID for the FK column
            user_id = user.id

        except Exception as e:
            logger.warning(
                'Webhook YooKassa: не удалось проверить user_id',
                yookassa_payment_id=yookassa_payment_id,
                user_id=user_id,
                e=e,
            )
            return None

        amount_info = event_object.get('amount') or {}
        amount_value = amount_info.get('value')
        currency = (amount_info.get('currency') or 'RUB').upper()

        if amount_value is None:
            logger.error('Webhook YooKassa не содержит сумму платежа', yookassa_payment_id=yookassa_payment_id)
            return None

        try:
            amount_kopeks = int((Decimal(str(amount_value)) * 100).quantize(Decimal(1)))
        except (InvalidOperation, ValueError) as error:
            logger.error(
                'Некорректная сумма в webhook YooKassa',
                yookassa_payment_id=yookassa_payment_id,
                amount_value=amount_value,
                error=error,
            )
            return None

        description = event_object.get('description') or metadata.get('description') or 'YooKassa платеж'
        payment_method_type = (event_object.get('payment_method') or {}).get('type')

        yookassa_created_at = None
        created_at_raw = event_object.get('created_at')
        if created_at_raw:
            try:
                yookassa_created_at = datetime.fromisoformat(created_at_raw.replace('Z', '+00:00'))
            except Exception as error:  # pragma: no cover - диагностический лог
                logger.debug(
                    'Не удалось распарсить created_at= для YooKassa',
                    created_at_raw=created_at_raw,
                    yookassa_payment_id=yookassa_payment_id,
                    error=error,
                )

        payment_module = import_module('app.services.payment_service')

        local_payment = await payment_module.create_yookassa_payment(
            db=db,
            user_id=user_id,
            yookassa_payment_id=yookassa_payment_id,
            amount_kopeks=amount_kopeks,
            currency=currency,
            description=description,
            status=event_object.get('status', 'pending'),
            confirmation_url=self._extract_confirmation_url(event_object),
            metadata_json=metadata,
            payment_method_type=payment_method_type,
            yookassa_created_at=yookassa_created_at,
            test_mode=bool(event_object.get('test') or event_object.get('test_mode')),
        )

        if not local_payment:
            return None

        await payment_module.update_yookassa_payment_status(
            db=db,
            yookassa_payment_id=yookassa_payment_id,
            status=event_object.get('status', local_payment.status),
            is_paid=bool(event_object.get('paid')),
            is_captured=event_object.get('status') == 'succeeded',
            captured_at=self._parse_datetime(event_object.get('captured_at')),
            payment_method_type=payment_method_type,
        )

        return await payment_module.get_yookassa_payment_by_id(db, yookassa_payment_id)

    @staticmethod
    def _normalise_yookassa_metadata(metadata: Any) -> dict[str, Any]:
        if isinstance(metadata, dict):
            return metadata

        if isinstance(metadata, list):
            normalised: dict[str, Any] = {}
            for item in metadata:
                key = item.get('key') if isinstance(item, dict) else None
                if key:
                    normalised[key] = item.get('value')
            return normalised

        if isinstance(metadata, str):
            try:
                import json

                parsed = json.loads(metadata)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                logger.debug('Не удалось распарсить metadata webhook YooKassa', metadata=metadata)

        return {}

    @staticmethod
    def _extract_confirmation_url(event_object: dict[str, Any]) -> str | None:
        if 'confirmation_url' in event_object:
            return event_object.get('confirmation_url')

        confirmation = event_object.get('confirmation')
        if isinstance(confirmation, dict):
            return confirmation.get('confirmation_url') or confirmation.get('return_url')

        return None

    @staticmethod
    def _parse_datetime(raw_value: str | None) -> datetime | None:
        if not raw_value:
            return None

        try:
            return datetime.fromisoformat(raw_value.replace('Z', '+00:00'))
        except Exception:
            return None
