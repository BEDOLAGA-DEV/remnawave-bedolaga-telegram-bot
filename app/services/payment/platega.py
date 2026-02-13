"""Mixin для интеграции платежей Platega."""

from __future__ import annotations

import uuid
from datetime import datetime
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.platega_service import PlategaService
from app.services.subscription_auto_purchase_service import (
    auto_activate_subscription_after_topup,
    auto_purchase_saved_cart_after_topup,
)
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class PlategaPaymentMixin:
    """Логика создания и обработки платежей Platega."""

    _SUCCESS_STATUSES = {'CONFIRMED'}
    _FAILED_STATUSES = {'FAILED', 'CANCELED', 'EXPIRED'}
    _PENDING_STATUSES = {'PENDING', 'INPROGRESS'}

    async def create_platega_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        amount_kopeks: int,
        description: str,
        language: str,
        payment_method_code: int,
    ) -> dict[str, Any] | None:
        service: PlategaService | None = getattr(self, 'platega_service', None)
        if not service or not service.is_configured:
            logger.error('Platega сервис не инициализирован')
            return None

        if amount_kopeks < settings.PLATEGA_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма Platega меньше минимальной: %s < %s',
                amount_kopeks,
                settings.PLATEGA_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.PLATEGA_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма Platega больше максимальной: %s > %s',
                amount_kopeks,
                settings.PLATEGA_MAX_AMOUNT_KOPEKS,
            )
            return None

        correlation_id = uuid.uuid4().hex
        payload_token = f'platega:{correlation_id}'

        amount_value = amount_kopeks / 100

        try:
            response = await service.create_payment(
                payment_method=payment_method_code,
                amount=amount_value,
                currency=settings.PLATEGA_CURRENCY,
                description=description,
                return_url=settings.get_platega_return_url(),
                failed_url=settings.get_platega_failed_url(),
                payload=payload_token,
            )
        except Exception as error:  # pragma: no cover - network errors
            logger.exception('Ошибка Platega при создании платежа: %s', error)
            return None

        if not response:
            logger.error('Platega вернул пустой ответ при создании платежа')
            return None

        transaction_id = response.get('transactionId') or response.get('id')
        redirect_url = response.get('redirect')
        status = str(response.get('status') or 'PENDING').upper()
        expires_at = PlategaService.parse_expires_at(response.get('expiresIn'))

        metadata = {
            'raw_response': response,
            'language': language,
            'selected_method': payment_method_code,
        }

        payment_module = import_module('app.services.payment_service')

        payment = await payment_module.create_platega_payment(
            db,
            user_id=user_id,
            amount_kopeks=amount_kopeks,
            currency=settings.PLATEGA_CURRENCY,
            description=description,
            status=status,
            payment_method_code=payment_method_code,
            correlation_id=correlation_id,
            platega_transaction_id=transaction_id,
            redirect_url=redirect_url,
            return_url=settings.get_platega_return_url(),
            failed_url=settings.get_platega_failed_url(),
            payload=payload_token,
            metadata=metadata,
            expires_at=expires_at,
        )

        logger.info(
            'Создан Platega платеж %s для пользователя %s (метод %s, сумма %s₽)',
            transaction_id or payment.id,
            user_id,
            payment_method_code,
            amount_value,
        )

        return {
            'local_payment_id': payment.id,
            'transaction_id': transaction_id,
            'redirect_url': redirect_url,
            'status': status,
            'expires_at': expires_at,
            'correlation_id': correlation_id,
            'payload': payload_token,
        }

    async def process_platega_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        payment_module = import_module('app.services.payment_service')

        transaction_id = str(payload.get('id') or '').strip()
        payload_token = payload.get('payload')

        payment = None
        if transaction_id:
            payment = await payment_module.get_platega_payment_by_transaction_id(db, transaction_id)
        if not payment and payload_token:
            payment = await payment_module.get_platega_payment_by_correlation_id(
                db, str(payload_token).replace('platega:', '')
            )

        if not payment:
            logger.warning('Platega webhook: платеж не найден (id=%s)', transaction_id)
            return False

        status_raw = str(payload.get('status') or '').upper()
        if not status_raw:
            logger.warning('Platega webhook без статуса для платежа %s', payment.id)
            return False

        update_kwargs = {
            'status': status_raw,
            'callback_payload': payload,
        }

        if transaction_id:
            update_kwargs['platega_transaction_id'] = transaction_id

        if status_raw in self._SUCCESS_STATUSES:
            if payment.is_paid:
                logger.info('Platega платеж %s уже помечен как оплачен', payment.correlation_id)
                await payment_module.update_platega_payment(
                    db,
                    payment=payment,
                    **update_kwargs,
                    is_paid=True,
                )
                return True

            payment = await payment_module.update_platega_payment(
                db,
                payment=payment,
                **update_kwargs,
            )
            await self._finalize_platega_payment(db, payment, payload)
            return True

        if status_raw in self._FAILED_STATUSES:
            await payment_module.update_platega_payment(
                db,
                payment=payment,
                **update_kwargs,
                is_paid=False,
            )
            logger.info('Platega платеж %s перешёл в статус %s', payment.correlation_id, status_raw)
            return True

        await payment_module.update_platega_payment(
            db,
            payment=payment,
            **update_kwargs,
        )
        return True

    async def get_platega_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        payment_module = import_module('app.services.payment_service')
        payment = await payment_module.get_platega_payment_by_id(db, local_payment_id)
        if not payment:
            return None

        service: PlategaService | None = getattr(self, 'platega_service', None)
        remote_status: str | None = None
        remote_payload: dict[str, Any] | None = None

        if service and payment.platega_transaction_id:
            try:
                remote_payload = await service.get_transaction(payment.platega_transaction_id)
            except Exception as error:  # pragma: no cover - network errors
                logger.error(
                    'Ошибка Platega при получении транзакции %s: %s',
                    payment.platega_transaction_id,
                    error,
                )

        if remote_payload:
            remote_status = str(remote_payload.get('status') or '').upper()
            if remote_status and remote_status != payment.status:
                await payment_module.update_platega_payment(
                    db,
                    payment=payment,
                    status=remote_status,
                    metadata={
                        **(getattr(payment, 'metadata_json', {}) or {}),
                        'remote_status': remote_payload,
                    },
                )
                payment = await payment_module.get_platega_payment_by_id(db, local_payment_id)

            if remote_status in self._SUCCESS_STATUSES and not payment.is_paid:
                payment = await payment_module.update_platega_payment(
                    db,
                    payment=payment,
                    status=remote_status,
                    callback_payload=remote_payload,
                )
                await self._finalize_platega_payment(db, payment, remote_payload)

        return {
            'payment': payment,
            'status': payment.status,
            'is_paid': payment.is_paid,
            'remote': remote_payload,
        }

    async def _finalize_platega_payment(
        self,
        db: AsyncSession,
        payment: Any,
        payload: dict[str, Any] | None,
    ) -> Any:
        payment_module = import_module('app.services.payment_service')

        metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        if payload is not None:
            metadata['webhook'] = payload

        paid_at = None
        if isinstance(payload, dict):
            paid_at_raw = payload.get('paidAt') or payload.get('confirmedAt')
            if paid_at_raw:
                try:
                    paid_at = datetime.fromisoformat(str(paid_at_raw))
                except ValueError:
                    paid_at = None

        payment = await payment_module.update_platega_payment(
            db,
            payment=payment,
            status='CONFIRMED',
            is_paid=True,
            paid_at=paid_at,
            metadata=metadata,
            callback_payload=payload,
        )

        locked_payment = await payment_module.get_platega_payment_by_id_for_update(db, payment.id)
        if locked_payment:
            payment = locked_payment

        metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        balance_already_credited = bool(metadata.get('balance_credited'))

        invoice_message = metadata.get('invoice_message') or {}
        if getattr(self, 'bot', None):
            chat_id = invoice_message.get('chat_id')
            message_id = invoice_message.get('message_id')
            if chat_id and message_id:
                try:
                    await self.bot.delete_message(chat_id, message_id)
                except Exception as delete_error:  # pragma: no cover - depends on bot rights
                    logger.warning(
                        'Не удалось удалить Platega счёт %s: %s',
                        message_id,
                        delete_error,
                    )
                else:
                    metadata.pop('invoice_message', None)

        if payment.transaction_id:
            logger.info(
                'Platega платеж %s уже связан с транзакцией %s',
                payment.correlation_id,
                payment.transaction_id,
            )
            return payment

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь %s не найден для Platega', payment.user_id)
            return payment

        # Убеждаемся, что промогруппы загружены в асинхронном контексте,
        # чтобы избежать попыток ленивой загрузки без greenlet
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = (
            str(payload.get('id'))
            if isinstance(payload, dict) and payload.get('id')
            else payment.platega_transaction_id
        )

        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.PLATEGA,
            )

        platega_name = settings.get_platega_display_name()
        method_display = settings.get_platega_method_display_name(payment.payment_method_code)
        description = (
            f'Пополнение через {platega_name} ({method_display})'
            if method_display
            else f'Пополнение через {platega_name}'
        )

        transaction = existing_transaction
        created_transaction = False

        if not transaction:
            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=description,
                payment_method=PaymentMethod.PLATEGA,
                external_id=transaction_external_id or payment.correlation_id,
                is_completed=True,
            )
            created_transaction = True

        await payment_module.link_platega_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info(
                'Platega платеж %s уже зачислил баланс ранее',
                payment.correlation_id,
            )
            return payment

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(user)
        topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения Platega: %s', error)

        if was_first_topup and not user.has_made_first_topup:
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
                logger.error('Ошибка отправки админ уведомления Platega: %s', error)

        method_title = settings.get_platega_method_display_title(payment.payment_method_code)

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'🦊 Способ: {method_title}\n'
                        f'🆔 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Platega: %s', error)

        try:
            from aiogram import types

            from app.services.user_cart_service import user_cart_service

            has_saved_cart = await user_cart_service.has_user_cart(user.id)
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
            activation_notification_sent = False
            if not auto_purchase_success:
                try:
                    _, activation_notification_sent = await auto_activate_subscription_after_topup(
                        db, user, bot=getattr(self, 'bot', None), topup_amount=payment.amount_kopeks
                    )
                except Exception as auto_activate_error:
                    logger.error(
                        'Ошибка умной автоактивации для пользователя %s: %s',
                        user.id,
                        auto_activate_error,
                        exc_info=True,
                    )

            # Отправляем уведомление только если его ещё не отправили
            if has_saved_cart and getattr(self, 'bot', None) and not activation_notification_sent and user.telegram_id:
                from app.localization.texts import get_texts

                texts = get_texts(user.language)
                cart_message = texts.t(
                    'BALANCE_TOPUP_CART_REMINDER_DETAILED',
                    '🛒 У вас есть неоформленный заказ.\n\nВы можете продолжить оформление с теми же параметрами.',
                )

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
                    text=(
                        f'✅ Баланс пополнен на {settings.format_price(payment.amount_kopeks)}!\n\n'
                        f'⚠️ <b>Важно:</b> Пополнение баланса не активирует подписку автоматически. '
                        f'Обязательно активируйте подписку отдельно!\n\n'
                        f'🔄 При наличии сохранённой корзины подписки и включенной автопокупке, '
                        f'подписка будет приобретена автоматически после пополнения баланса.\n\n'
                        f'{cart_message}'
                    ),
                    reply_markup=keyboard,
                )
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя %s: %s',
                payment.user_id,
                error,
                exc_info=True,
            )

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.utcnow().isoformat(),
        }
        metadata['balance_credited'] = True

        await payment_module.update_platega_payment(
            db,
            payment=payment,
            metadata=metadata,
        )

        logger.info(
            '✅ Обработан Platega платеж %s для пользователя %s',
            payment.correlation_id,
            payment.user_id,
        )

        return payment
