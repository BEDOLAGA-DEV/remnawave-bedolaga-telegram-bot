"""Функции работы с YooKassa вынесены в dedicated mixin.

Такое разделение облегчает поддержку и делает очевидным, какая часть
отвечает за конкретного провайдера.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.subscription_auto_purchase_service import (
    auto_activate_subscription_after_topup,
    auto_purchase_saved_cart_after_topup,
)
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


if TYPE_CHECKING:
    from app.database.models import Transaction, YooKassaPayment


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
        user_id: int,
        amount_kopeks: int,
        description: str,
        receipt_email: str | None = None,
        receipt_phone: str | None = None,
        metadata: dict[str, Any] | None = None,
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
            if 'user_telegram_id' not in payment_metadata:
                try:
                    from app.database.crud.user import get_user_by_id

                    user = await get_user_by_id(db, user_id)
                    if user and user.telegram_id:
                        payment_metadata['user_telegram_id'] = str(user.telegram_id)
                        payment_metadata['user_username'] = user.username or ''
                except Exception as e:
                    logger.warning(f'Не удалось получить telegram_id для user_id={user_id}: {e}')

            # Preserve existing type from metadata if passed (e.g., "trial")
            existing_type = payment_metadata.get('type')
            payment_metadata.update(
                {
                    'user_id': str(user_id),
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
            )

            if not yookassa_response or yookassa_response.get('error'):
                logger.error('Ошибка создания платежа YooKassa: %s', yookassa_response)
                return None

            yookassa_created_at: datetime | None = None
            if yookassa_response.get('created_at'):
                try:
                    dt_with_tz = datetime.fromisoformat(yookassa_response['created_at'].replace('Z', '+00:00'))
                    yookassa_created_at = dt_with_tz.replace(tzinfo=None)
                except Exception as error:
                    logger.warning('Не удалось распарсить created_at: %s', error)
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
                'Создан платеж YooKassa %s на %s₽ для пользователя %s',
                yookassa_response['id'],
                amount_rubles,
                user_id,
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
            logger.error('Ошибка создания платежа YooKassa: %s', error)
            return None

    async def create_yookassa_sbp_payment(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int,
        description: str,
        receipt_email: str | None = None,
        receipt_phone: str | None = None,
        metadata: dict[str, Any] | None = None,
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
            if 'user_telegram_id' not in payment_metadata:
                try:
                    from app.database.crud.user import get_user_by_id

                    user = await get_user_by_id(db, user_id)
                    if user and user.telegram_id:
                        payment_metadata['user_telegram_id'] = str(user.telegram_id)
                        payment_metadata['user_username'] = user.username or ''
                except Exception as e:
                    logger.warning(f'Не удалось получить telegram_id для user_id={user_id}: {e}')

            # Preserve existing type from metadata if passed (e.g., "trial")
            existing_type = payment_metadata.get('type')
            payment_metadata.update(
                {
                    'user_id': str(user_id),
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
            )

            if not yookassa_response or yookassa_response.get('error'):
                logger.error(
                    'Ошибка создания платежа YooKassa СБП: %s',
                    yookassa_response,
                )
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
                payment_method_type='bank_card',
                yookassa_created_at=None,
                test_mode=yookassa_response.get('test_mode', False),
            )

            logger.info(
                'Создан платеж YooKassa СБП %s на %s₽ для пользователя %s',
                yookassa_response['id'],
                amount_rubles,
                user_id,
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
            logger.error('Ошибка создания платежа YooKassa СБП: %s', error)
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
                    'Ошибка получения статуса YooKassa %s: %s',
                    payment.yookassa_payment_id,
                    error,
                )

        if remote_data:
            status = remote_data.get('status') or payment.status
            paid = bool(remote_data.get('paid', getattr(payment, 'is_paid', False)))
            captured_raw = remote_data.get('captured_at')
            captured_at = None
            if captured_raw:
                try:
                    captured_at = datetime.fromisoformat(str(captured_raw).replace('Z', '+00:00')).replace(tzinfo=None)
                except Exception as parse_error:  # pragma: no cover - diagnostic log
                    logger.debug(
                        'Не удалось распарсить captured_at %s: %s',
                        captured_raw,
                        parse_error,
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
                        'Не удалось обновить состояние платежа YooKassa %s перед повторной обработкой: %s',
                        payment.yookassa_payment_id,
                        refresh_error,
                        exc_info=True,
                    )

            if transaction_id:
                logger.info(
                    'Пропускаем повторную обработку платежа YooKassa %s: уже связан с транзакцией %s',
                    payment.yookassa_payment_id,
                    transaction_id,
                )
            else:
                try:
                    await self._process_successful_yookassa_payment(db, payment)
                except Exception as process_error:  # pragma: no cover - defensive logging
                    logger.error(
                        'Ошибка обработки успешного платежа YooKassa %s: %s',
                        payment.yookassa_payment_id,
                        process_error,
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
    ) -> bool:
        """Переносит успешный платёж YooKassa в транзакции и начисляет баланс пользователю."""
        try:
            from sqlalchemy import select

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
                    'Платеж YooKassa %s уже был обработан транзакцией %s. Пропускаем повторную обработку.',
                    payment.yookassa_payment_id,
                    existing_transaction.id,
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
                            'Не удалось привязать платеж YooKassa %s к существующей транзакции %s: %s',
                            payment.yookassa_payment_id,
                            existing_transaction.id,
                            link_error,
                            exc_info=True,
                        )

                return True

            payment_metadata: dict[str, Any] = {}
            try:
                if hasattr(payment, 'metadata_json') and payment.metadata_json:
                    import json

                    if isinstance(payment.metadata_json, str):
                        payment_metadata = json.loads(payment.metadata_json)
                    elif isinstance(payment.metadata_json, dict):
                        payment_metadata = payment.metadata_json
                    logger.info(f'Метаданные платежа: {payment_metadata}')
            except Exception as parse_error:
                logger.error(f'Ошибка парсинга метаданных платежа: {parse_error}')

            invoice_message = payment_metadata.get('invoice_message') or {}
            if getattr(self, 'bot', None):
                chat_id = invoice_message.get('chat_id')
                message_id = invoice_message.get('message_id')
                if chat_id and message_id:
                    try:
                        await self.bot.delete_message(chat_id, message_id)
                    except Exception as delete_error:  # pragma: no cover - depends on bot rights
                        logger.warning(
                            'Не удалось удалить сообщение YooKassa %s: %s',
                            message_id,
                            delete_error,
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
                        'Не удалось получить транзакцию %s для платежа YooKassa %s: %s',
                        existing_transaction_id,
                        payment.yookassa_payment_id,
                        fetch_error,
                        exc_info=True,
                    )

                if transaction and processing_completed:
                    logger.info(
                        'Пропускаем повторную обработку платежа YooKassa %s: транзакция %s уже завершила начисление.',
                        payment.yookassa_payment_id,
                        existing_transaction_id,
                    )
                    return True

                if transaction:
                    logger.info(
                        'Транзакция %s для платежа YooKassa %s найдена, но обработка ранее не была завершена — повторяем критические шаги.',
                        existing_transaction_id,
                        payment.yookassa_payment_id,
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
                        'Платеж YooKassa %s уже был обработан транзакцией %s. Пропускаем повторную обработку.',
                        payment.yookassa_payment_id,
                        existing_transaction.id,
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
                                'Не удалось привязать платеж YooKassa %s к существующей транзакции %s: %s',
                                payment.yookassa_payment_id,
                                existing_transaction.id,
                                link_error,
                                exc_info=True,
                            )

                    return True

            payment_description = getattr(payment, 'description', 'YooKassa платеж')

            payment_purpose = payment_metadata.get('payment_purpose', '')
            payment_type = payment_metadata.get('type', '')
            is_simple_subscription = payment_purpose == 'simple_subscription_purchase'
            is_trial_payment = payment_type == 'trial'

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
                        'YooKassa платеж %s обработан как оплата триала. Баланс пользователя %s не изменяется.',
                        payment.yookassa_payment_id,
                        user.id,
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
                                    f'Триальная подписка {subscription_id} активирована для пользователя {user.id}'
                                )

                                # Создаем пользователя в RemnaWave
                                subscription_service = SubscriptionService()
                                try:
                                    await subscription_service.create_remnawave_user(db, subscription)
                                except Exception as rw_error:
                                    logger.error(f'Ошибка создания RemnaWave для триала: {rw_error}')

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
                                        logger.warning(f'Ошибка уведомления админов о триале: {admin_error}')

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
                                        logger.warning(f'Ошибка уведомления пользователя о триале: {notify_error}')
                                elif not user.telegram_id:
                                    logger.info(
                                        f'Пропуск Telegram-уведомления о триале для email-пользователя {user.id}'
                                    )
                            else:
                                logger.error(f'Не удалось активировать триал {subscription_id} для {user.id}')
                        else:
                            logger.error('Отсутствует subscription_id в metadata триального платежа YooKassa')
                    except Exception as trial_error:
                        logger.error(f'Ошибка обработки триального платежа YooKassa: {trial_error}', exc_info=True)

                elif is_simple_subscription:
                    logger.info(
                        'YooKassa платеж %s обработан как покупка подписки. Баланс пользователя %s не изменяется.',
                        payment.yookassa_payment_id,
                        user.id,
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
                            'Ошибка реферального начисления при покупке подписки YooKassa: %s',
                            ref_error,
                        )
                else:
                    old_balance = getattr(user, 'balance_kopeks', 0)
                    was_first_topup = not getattr(user, 'has_made_first_topup', False)

                    user.balance_kopeks += payment.amount_kopeks
                    user.updated_at = datetime.utcnow()

                    # Обновляем пользователя с нужными связями, чтобы избежать проблем с ленивой загрузкой
                    from sqlalchemy.orm import selectinload

                    from app.database.models import Subscription as SubscriptionModel, User

                    # Загружаем пользователя с подпиской и промо-группой
                    full_user_result = await db.execute(
                        select(User)
                        .options(selectinload(User.subscription))
                        .options(selectinload(User.user_promo_groups))
                        .where(User.id == user.id)
                    )
                    full_user = full_user_result.scalar_one_or_none()

                    # Используем обновленные данные или исходные, если не удалось обновить
                    subscription = full_user.subscription if full_user else getattr(user, 'subscription', None)
                    promo_group = (
                        full_user.get_primary_promo_group()
                        if full_user
                        else (user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None)
                    )

                    # Используем full_user для форматирования реферальной информации, чтобы избежать проблем с ленивой загрузкой
                    user_for_referrer = full_user if full_user else user
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

                    try:
                        from app.services.referral_service import process_referral_topup

                        await process_referral_topup(
                            db,
                            user.id,
                            payment.amount_kopeks,
                            getattr(self, 'bot', None),
                        )
                    except Exception as error:
                        logger.error(
                            'Ошибка обработки реферального пополнения YooKassa: %s',
                            error,
                        )

                    if was_first_topup and not getattr(user, 'has_made_first_topup', False):
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
                                'Ошибка отправки уведомления админам о YooKassa пополнении: %s',
                                error,
                                exc_info=True,  # Добавляем полный стек вызовов для отладки
                            )

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
                            logger.error(
                                'Ошибка отправки уведомления о платеже: %s',
                                error,
                                exc_info=True,  # Добавляем полный стек вызовов для отладки
                            )

                    # Проверяем наличие сохраненной корзины для возврата к оформлению подписки
                    # ВАЖНО: этот код должен выполняться даже при ошибках в уведомлениях
                    logger.info(f'Проверяем наличие сохраненной корзины для пользователя {user.id}')
                    from app.services.user_cart_service import user_cart_service

                    try:
                        has_saved_cart = await user_cart_service.has_user_cart(user.id)
                        logger.info(
                            'Результат проверки корзины для пользователя %s: %s',
                            user.id,
                            has_saved_cart,
                        )

                        auto_purchase_success = False
                        if has_saved_cart:
                            try:
                                auto_purchase_success = await auto_purchase_saved_cart_after_topup(
                                    db,
                                    user,
                                    bot=getattr(self, 'bot', None),
                                )
                            except Exception as auto_error:
                                logger.error(
                                    'Ошибка автоматической покупки подписки для пользователя %s: %s',
                                    user.id,
                                    auto_error,
                                    exc_info=True,
                                )

                            if auto_purchase_success:
                                has_saved_cart = False

                        # Умная автоактивация если автопокупка не сработала
                        if not auto_purchase_success:
                            try:
                                await auto_activate_subscription_after_topup(
                                    db,
                                    user,
                                    bot=getattr(self, 'bot', None),
                                )
                            except Exception as auto_activate_error:
                                logger.error(
                                    'Ошибка умной автоактивации для пользователя %s: %s',
                                    user.id,
                                    auto_activate_error,
                                    exc_info=True,
                                )

                        # Если включен яркий промпт активации, пропускаем старое уведомление
                        # т.к. оно будет отправлено через _send_payment_success_notification
                        if not settings.SHOW_ACTIVATION_PROMPT_AFTER_TOPUP:
                            if has_saved_cart and getattr(self, 'bot', None) and user.telegram_id:
                                # Если у пользователя есть сохраненная корзина,
                                # отправляем ему уведомление с кнопкой вернуться к оформлению
                                from aiogram import types

                                from app.localization.texts import get_texts

                                texts = get_texts(user.language)
                                cart_message = texts.BALANCE_TOPUP_CART_REMINDER_DETAILED.format(
                                    total_amount=settings.format_price(payment.amount_kopeks)
                                )

                                # Создаем клавиатуру с кнопками
                                keyboard = types.InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            types.InlineKeyboardButton(
                                                text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                                                callback_data='return_to_saved_cart',
                                            )
                                        ],
                                        [
                                            types.InlineKeyboardButton(
                                                text='💰 Мой баланс',
                                                callback_data='menu_balance',
                                            )
                                        ],
                                        [
                                            types.InlineKeyboardButton(
                                                text='🏠 Главное меню',
                                                callback_data='back_to_menu',
                                            )
                                        ],
                                    ]
                                )

                                await self.bot.send_message(
                                    chat_id=user.telegram_id,
                                    text=f'✅ Баланс пополнен на {settings.format_price(payment.amount_kopeks)}!\n\n'
                                    f'⚠️ <b>Важно:</b> Пополнение баланса не активирует подписку автоматически. '
                                    f'Обязательно активируйте подписку отдельно!\n\n'
                                    f'🔄 При наличии сохранённой корзины подписки и включенной автопокупке, '
                                    f'подписка будет приобретена автоматически после пополнения баланса.\n\n{cart_message}',
                                    reply_markup=keyboard,
                                )
                                logger.info(
                                    f'Отправлено уведомление с кнопкой возврата к оформлению подписки пользователю {user.id}'
                                )
                            else:
                                logger.info(
                                    'У пользователя %s нет сохраненной корзины, бот недоступен или покупка уже выполнена',
                                    user.id,
                                )
                    except Exception as e:
                        logger.error(
                            f'Критическая ошибка при работе с сохраненной корзиной для пользователя {user.id}: {e}',
                            exc_info=True,
                        )

                if is_simple_subscription:
                    logger.info(f'Обнаружен платеж простой покупки подписки для пользователя {user.id}')
                    try:
                        # Активируем подписку
                        from app.services.subscription_service import SubscriptionService

                        subscription_service = SubscriptionService()

                        # Получаем параметры подписки из метаданных
                        subscription_period = int(payment_metadata.get('subscription_period', 30))
                        order_id = payment_metadata.get('order_id')

                        logger.info(f'Активация подписки: период={subscription_period} дней, заказ={order_id}')

                        # Активируем pending подписку пользователя
                        from app.database.crud.subscription import activate_pending_subscription

                        subscription = await activate_pending_subscription(
                            db=db, user_id=user.id, period_days=subscription_period
                        )

                        if subscription:
                            logger.info(f'Подписка успешно активирована для пользователя {user.id}')

                            # Обновляем данные подписки в RemnaWave, чтобы получить актуальные ссылки
                            try:
                                remnawave_user = await subscription_service.create_remnawave_user(db, subscription)
                                if remnawave_user:
                                    await db.refresh(subscription)
                            except Exception as sync_error:
                                logger.error(
                                    'Ошибка синхронизации подписки с RemnaWave для пользователя %s: %s',
                                    user.id,
                                    sync_error,
                                    exc_info=True,
                                )

                            # Отправляем уведомление пользователю об активации подписки (только Telegram)
                            if getattr(self, 'bot', None) and user.telegram_id:
                                from aiogram import types

                                from app.localization.texts import get_texts

                                texts = get_texts(user.language)

                                success_message = (
                                    f'✅ <b>Подписка успешно активирована!</b>\n\n'
                                    f'📅 Период: {subscription_period} дней\n'
                                    f'📱 Устройства: 1\n'
                                    f'📊 Трафик: Безлимит\n'
                                    f'💳 Оплата: {settings.format_price(payment.amount_kopeks)} (YooKassa)\n\n'
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
                                logger.info(f'Пропуск Telegram-уведомления о подписке для email-пользователя {user.id}')

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
                                        .options(selectinload(User.subscription))
                                        .options(selectinload(User.user_promo_groups))
                                        .where(User.id == user.id)
                                    )
                                    full_user = full_user_result.scalar_one_or_none()

                                    # Загружаем подписку отдельно, если нужно
                                    subscription_result = await db.execute(
                                        select(SubscriptionModel).where(SubscriptionModel.user_id == user.id)
                                    )
                                    subscription_db = subscription_result.scalar_one_or_none()

                                    await notification_service.send_subscription_purchase_notification(
                                        db,
                                        full_user or user,
                                        subscription_db or subscription,
                                        transaction,
                                        subscription_period,
                                        was_trial_conversion=False,
                                    )
                                except Exception as admin_error:
                                    logger.error(
                                        'Ошибка отправки уведомления админам о покупке подписки через YooKassa: %s',
                                        admin_error,
                                        exc_info=True,
                                    )
                        else:
                            logger.error(f'Ошибка активации подписки для пользователя {user.id}')
                    except Exception as e:
                        logger.error(f'Ошибка активации подписки для пользователя {user.id}: {e}', exc_info=True)

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
                    'Пользователь %s для платежа YooKassa %s не найден — начисление баланса невозможно',
                    payment.user_id,
                    payment.yookassa_payment_id,
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
                    'Успешно обработан платеж YooKassa %s как покупка подписки: пользователь %s, сумма %s₽',
                    payment.yookassa_payment_id,
                    payment.user_id,
                    payment.amount_kopeks / 100,
                )
            else:
                logger.info(
                    'Успешно обработан платеж YooKassa %s: пользователь %s пополнил баланс на %s₽',
                    payment.yookassa_payment_id,
                    payment.user_id,
                    payment.amount_kopeks / 100,
                )

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
                'Ошибка обработки успешного платежа YooKassa %s: %s',
                payment.yookassa_payment_id,
                error,
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
                .values(metadata_json=updated_metadata, updated_at=datetime.utcnow())
            )
            if commit:
                await db.commit()
            else:
                await db.flush()
            payment.metadata_json = updated_metadata
        except Exception as mark_error:  # pragma: no cover - защитный лог
            logger.warning(
                'Не удалось отметить платеж YooKassa %s как завершенный: %s',
                payment.yookassa_payment_id,
                mark_error,
                exc_info=True,
            )

        return updated_metadata

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
                f'Чек для платежа {payment.yookassa_payment_id} уже создан: {transaction.receipt_uuid}, '
                'пропускаем повторное создание'
            )
            return

        try:
            amount_rubles = payment.amount_kopeks / 100
            # Формируем описание из настроек (включает сумму и ID пользователя)
            receipt_name = settings.get_balance_payment_description(payment.amount_kopeks, telegram_user_id)

            receipt_uuid = await self.nalogo_service.create_receipt(
                name=receipt_name,
                amount=amount_rubles,
                quantity=1,
                payment_id=payment.yookassa_payment_id,
                telegram_user_id=telegram_user_id,
                amount_kopeks=payment.amount_kopeks,
            )

            if receipt_uuid:
                logger.info(f'Чек NaloGO создан для платежа {payment.yookassa_payment_id}: {receipt_uuid}')

                # Сохраняем receipt_uuid в транзакцию
                if transaction:
                    try:
                        transaction.receipt_uuid = receipt_uuid
                        transaction.receipt_created_at = datetime.utcnow()
                        await db.commit()
                        logger.debug(f'Чек {receipt_uuid} привязан к транзакции {transaction.id}')
                    except Exception as save_error:
                        logger.warning(f'Не удалось сохранить receipt_uuid в транзакцию: {save_error}')
            # При временной недоступности чек добавляется в очередь автоматически

        except Exception as error:
            logger.error(
                'Ошибка создания чека NaloGO для платежа %s: %s',
                payment.yookassa_payment_id,
                error,
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
            logger.warning('Webhook без payment id: %s', event)
            return False

        remote_data: dict[str, Any] | None = None
        if getattr(self, 'yookassa_service', None):
            try:
                remote_data = await self.yookassa_service.get_payment_info(  # type: ignore[union-attr]
                    yookassa_payment_id
                )
            except Exception as error:  # pragma: no cover - диагностический лог
                logger.warning(
                    'Не удалось запросить актуальный статус платежа YooKassa %s: %s',
                    yookassa_payment_id,
                    error,
                    exc_info=True,
                )

        if remote_data:
            previous_status = event_object.get('status')
            event_object = self._merge_remote_yookassa_payload(event_object, remote_data)
            if previous_status and event_object.get('status') != previous_status:
                logger.info(
                    'Статус платежа YooKassa %s скорректирован по данным API: %s → %s',
                    yookassa_payment_id,
                    previous_status,
                    event_object.get('status'),
                )
            event['object'] = event_object

        payment_module = import_module('app.services.payment_service')

        payment = await payment_module.get_yookassa_payment_by_id(db, yookassa_payment_id)
        if not payment:
            logger.warning('Локальный платеж для YooKassa id %s не найден', yookassa_payment_id)
            payment = await self._restore_missing_yookassa_payment(db, event_object)

            if not payment:
                logger.error(
                    'Не удалось восстановить локальную запись платежа YooKassa %s',
                    yookassa_payment_id,
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
                payment.captured_at = datetime.fromisoformat(captured_at_raw.replace('Z', '+00:00')).replace(
                    tzinfo=None
                )
            except Exception as error:
                logger.debug(
                    'Не удалось распарсить captured_at=%s: %s',
                    captured_at_raw,
                    error,
                )

        await db.commit()
        await db.refresh(payment)

        if payment.status == 'succeeded' and payment.is_paid:
            return await self._process_successful_yookassa_payment(db, payment)

        logger.info(
            'Webhook YooKassa обновил платеж %s до статуса %s',
            yookassa_payment_id,
            payment.status,
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
        user_id_raw = metadata.get('user_id') or metadata.get('userId')

        if user_id_raw is None:
            logger.error(
                'Webhook YooKassa %s не содержит user_id в metadata. Невозможно восстановить платеж.',
                yookassa_payment_id,
            )
            return None

        try:
            user_id = int(user_id_raw)
        except (TypeError, ValueError):
            logger.error(
                'Webhook YooKassa %s содержит некорректный user_id=%s',
                yookassa_payment_id,
                user_id_raw,
            )
            return None

        # Verify user exists before creating FK-linked record
        try:
            from app.database.crud.user import get_user_by_id

            user = await get_user_by_id(db, user_id)
            if not user:
                logger.warning(
                    'Webhook YooKassa %s: user_id=%s не найден в БД, пропускаем восстановление платежа',
                    yookassa_payment_id,
                    user_id,
                )
                return None
        except Exception as e:
            logger.warning(
                'Webhook YooKassa %s: не удалось проверить user_id=%s: %s',
                yookassa_payment_id,
                user_id,
                e,
            )
            return None

        amount_info = event_object.get('amount') or {}
        amount_value = amount_info.get('value')
        currency = (amount_info.get('currency') or 'RUB').upper()

        if amount_value is None:
            logger.error(
                'Webhook YooKassa %s не содержит сумму платежа',
                yookassa_payment_id,
            )
            return None

        try:
            amount_kopeks = int((Decimal(str(amount_value)) * 100).quantize(Decimal(1)))
        except (InvalidOperation, ValueError) as error:
            logger.error(
                'Некорректная сумма в webhook YooKassa %s: %s (%s)',
                yookassa_payment_id,
                amount_value,
                error,
            )
            return None

        description = event_object.get('description') or metadata.get('description') or 'YooKassa платеж'
        payment_method_type = (event_object.get('payment_method') or {}).get('type')

        yookassa_created_at = None
        created_at_raw = event_object.get('created_at')
        if created_at_raw:
            try:
                yookassa_created_at = datetime.fromisoformat(created_at_raw.replace('Z', '+00:00')).replace(tzinfo=None)
            except Exception as error:  # pragma: no cover - диагностический лог
                logger.debug(
                    'Не удалось распарсить created_at=%s для YooKassa %s: %s',
                    created_at_raw,
                    yookassa_payment_id,
                    error,
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
                logger.debug('Не удалось распарсить metadata webhook YooKassa: %s', metadata)

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
            return datetime.fromisoformat(raw_value.replace('Z', '+00:00')).replace(tzinfo=None)
        except Exception:
            return None
