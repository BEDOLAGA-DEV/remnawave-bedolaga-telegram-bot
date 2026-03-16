"""Mixin, инкапсулирующий работу с MulenPay."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class MulenPayPaymentMixin:
    """Mixin с созданием платежей, обработкой callback и проверкой статусов MulenPay."""

    async def create_mulenpay_payment(
        self,
        db: AsyncSession,
        user_id: int | None,
        amount_kopeks: int,
        description: str,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        """Создаёт локальный платеж и инициализирует сессию в MulenPay."""
        display_name = settings.get_mulenpay_display_name()
        settings.get_mulenpay_display_name_html()
        if not getattr(self, 'mulenpay_service', None):
            logger.error('сервис не инициализирован', display_name=display_name)
            return None

        if amount_kopeks < settings.MULENPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма меньше минимальной: <',
                display_name=display_name,
                amount_kopeks=amount_kopeks,
                MULENPAY_MIN_AMOUNT_KOPEKS=settings.MULENPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.MULENPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма больше максимальной: >',
                display_name=display_name,
                amount_kopeks=amount_kopeks,
                MULENPAY_MAX_AMOUNT_KOPEKS=settings.MULENPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module('app.services.payment_service')
        try:
            payment_uuid = f'mulen_{user_id or "guest"}_{uuid.uuid4().hex}'
            amount_rubles = amount_kopeks / 100

            items = [
                {
                    'description': description[:128],
                    'quantity': 1,
                    'price': round(amount_rubles, 2),
                    'vat_code': settings.MULENPAY_VAT_CODE,
                    'payment_subject': settings.MULENPAY_PAYMENT_SUBJECT,
                    'payment_mode': settings.MULENPAY_PAYMENT_MODE,
                }
            ]

            response = await self.mulenpay_service.create_payment(
                amount_kopeks=amount_kopeks,
                description=description,
                uuid=payment_uuid,
                items=items,
                language=language or settings.MULENPAY_LANGUAGE,
                website_url=settings.MULENPAY_WEBSITE_URL or settings.WEBHOOK_URL,
            )

            if not response:
                logger.error('Ошибка создания платежа', display_name=display_name)
                return None

            mulen_payment_id = response.get('id')
            payment_url = response.get('paymentUrl')

            metadata = {
                'user_id': user_id,
                'amount_kopeks': amount_kopeks,
                'description': description,
            }

            local_payment = await payment_module.create_mulenpay_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                uuid=payment_uuid,
                description=description,
                payment_url=payment_url,
                mulen_payment_id=mulen_payment_id,
                currency='RUB',
                status='created',
                metadata=metadata,
            )

            logger.info(
                'Создан платеж на ₽ для пользователя',
                display_name=display_name,
                mulen_payment_id=mulen_payment_id,
                amount_rubles=amount_rubles,
                user_id=user_id,
            )

            return {
                'local_payment_id': local_payment.id,
                'mulen_payment_id': mulen_payment_id,
                'payment_url': payment_url,
                'amount_kopeks': amount_kopeks,
                'uuid': payment_uuid,
                'status': 'created',
            }

        except Exception as error:
            logger.error('Ошибка создания платежа', display_name=display_name, error=error)
            return None

    async def process_mulenpay_callback(
        self,
        db: AsyncSession,
        callback_data: dict[str, Any],
    ) -> bool:
        """Обрабатывает callback от MulenPay, обновляет статус и начисляет баланс."""
        display_name = settings.get_mulenpay_display_name()
        display_name_html = settings.get_mulenpay_display_name_html()
        try:
            payment_module = import_module('app.services.payment_service')
            uuid_value = callback_data.get('uuid')
            payment_status_raw = (
                callback_data.get('payment_status') or callback_data.get('status') or callback_data.get('paymentStatus')
            )
            payment_status = (payment_status_raw or '').lower()
            mulen_payment_id_raw = callback_data.get('id')
            mulen_payment_id_int: int | None = None
            if mulen_payment_id_raw is not None:
                try:
                    mulen_payment_id_int = int(mulen_payment_id_raw)
                except (TypeError, ValueError):
                    mulen_payment_id_int = None
            amount_value = callback_data.get('amount')
            logger.debug(
                'callback: uuid status amount',
                display_name=display_name,
                uuid_value=uuid_value,
                payment_status=payment_status,
                amount_value=amount_value,
            )

            if not uuid_value and mulen_payment_id_raw is None:
                logger.error('callback без uuid и id', display_name=display_name)
                return False

            payment = None
            if uuid_value:
                payment = await payment_module.get_mulenpay_payment_by_uuid(db, uuid_value)

            if not payment and mulen_payment_id_int is not None:
                payment = await payment_module.get_mulenpay_payment_by_mulen_id(db, mulen_payment_id_int)

            if not payment:
                logger.error(
                    'платеж не найден (uuid id=)',
                    display_name=display_name,
                    uuid_value=uuid_value,
                    mulen_payment_id_raw=mulen_payment_id_raw,
                )
                return False

            metadata = dict(getattr(payment, 'metadata_json', {}) or {})
            invoice_message = metadata.get('invoice_message') or {}

            invoice_message_removed = False

            if getattr(self, 'bot', None):
                chat_id = invoice_message.get('chat_id')
                message_id = invoice_message.get('message_id')
                if chat_id and message_id:
                    try:
                        await self.bot.delete_message(chat_id, message_id)
                    except Exception as delete_error:  # pragma: no cover - depends on bot rights
                        logger.warning(
                            'Не удалось удалить счёт',
                            display_name=display_name,
                            message_id=message_id,
                            delete_error=delete_error,
                        )
                    else:
                        metadata.pop('invoice_message', None)
                        invoice_message_removed = True

            if payment.is_paid:
                if invoice_message_removed:
                    try:
                        await payment_module.update_mulenpay_payment_metadata(
                            db,
                            payment=payment,
                            metadata=metadata,
                        )
                    except Exception as error:  # pragma: no cover - diagnostics
                        logger.warning(
                            'Не удалось обновить метаданные после удаления счёта',
                            display_name=display_name,
                            error=error,
                        )

                logger.info(
                    'платеж уже обработан, игнорируем повторный callback', display_name=display_name, uuid=payment.uuid
                )
                return True

            if payment_status == 'success':
                await payment_module.update_mulenpay_payment_status(
                    db,
                    payment=payment,
                    status='success',
                    callback_payload=callback_data,
                    mulen_payment_id=mulen_payment_id_int,
                    metadata=metadata,
                )

                mulenpay_lock_crud = import_module('app.database.crud.mulenpay')
                locked = await mulenpay_lock_crud.get_mulenpay_payment_by_id_for_update(db, payment.id)
                if not locked:
                    logger.error('MulenPay: не удалось заблокировать платёж', payment_id=payment.id)
                    return False
                payment = locked

                if payment.transaction_id:
                    logger.info('Для платежа уже создана транзакция', display_name=display_name, uuid=payment.uuid)
                    return True

                # --- Guest purchase flow (landing page) ---
                payment_meta = dict(getattr(payment, 'metadata_json', {}) or {})
                from app.services.payment.common import try_fulfill_guest_purchase

                guest_result = await try_fulfill_guest_purchase(
                    db,
                    metadata=payment_meta,
                    payment_amount_kopeks=payment.amount_kopeks,
                    provider_payment_id=payment.uuid,
                    provider_name='mulenpay',
                )
                if guest_result is not None:
                    return True

                payment_description = getattr(
                    payment,
                    'description',
                    f'платеж {payment.uuid}',
                )

                transaction = await payment_module.create_transaction(
                    db,
                    user_id=payment.user_id,
                    type=TransactionType.DEPOSIT,
                    amount_kopeks=payment.amount_kopeks,
                    description=f'Пополнение через {display_name}: {payment_description}',
                    payment_method=PaymentMethod.MULENPAY,
                    external_id=payment.uuid,
                    is_completed=True,
                    created_at=getattr(payment, 'created_at', None),
                    commit=False,
                )

                await payment_module.link_mulenpay_payment_to_transaction(
                    db=db,
                    payment=payment,
                    transaction_id=transaction.id,
                )

                user = await payment_module.get_user_by_id(db, payment.user_id)
                if not user:
                    logger.error(
                        'Пользователь не найден при обработке', user_id=payment.user_id, display_name=display_name
                    )
                    return False

                # Lock user row to prevent concurrent balance race conditions
                from app.database.crud.user import lock_user_for_update

                user = await lock_user_for_update(db, user)

                old_balance = user.balance_kopeks
                was_first_topup = not user.has_made_first_topup

                # Начисляем баланс напрямую (без add_user_balance, который делает db.commit())
                user.balance_kopeks += payment.amount_kopeks
                user.updated_at = datetime.now(UTC)

                await db.commit()

                # Emit deferred side-effects after atomic commit
                from app.database.crud.transaction import emit_transaction_side_effects

                await emit_transaction_side_effects(
                    db,
                    transaction,
                    amount_kopeks=payment.amount_kopeks,
                    user_id=payment.user_id,
                    type=TransactionType.DEPOSIT,
                    payment_method=PaymentMethod.MULENPAY,
                    external_id=payment.uuid,
                )

                try:
                    from app.services.referral_service import process_referral_topup

                    await process_referral_topup(
                        db,
                        user.id,
                        payment.amount_kopeks,
                        getattr(self, 'bot', None),
                    )
                except Exception as error:
                    logger.error('Ошибка обработки реферального пополнения', display_name=display_name, error=error)

                if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
                    user.has_made_first_topup = True
                    await db.commit()

                # После коммита отношения пользователя могли быть сброшены, поэтому
                # повторно загружаем пользователя с предзагрузкой зависимостей
                user = await payment_module.get_user_by_id(db, user.id)
                if not user:
                    logger.error(
                        'Пользователь не найден при повторной загрузке после',
                        user_id=payment.user_id,
                        display_name=display_name,
                    )
                    return False

                promo_group = user.get_primary_promo_group()
                subscription = getattr(user, 'subscription', None)
                referrer_info = format_referrer_info(user)
                topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'

                if getattr(self, 'bot', None):
                    try:
                        from app.services.admin_notification_service import (
                            AdminNotificationService,
                        )

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
                        logger.error('Ошибка отправки уведомления о пополнении', display_name=display_name, error=error)

                if getattr(self, 'bot', None) and user.telegram_id:
                    try:
                        keyboard = await self.build_topup_success_keyboard(user)
                        await self.bot.send_message(
                            user.telegram_id,
                            (
                                '✅ <b>Пополнение успешно!</b>\n\n'
                                f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                                f'🦊 Способ: {display_name_html}\n'
                                f'🆔 Транзакция: {transaction.id}\n\n'
                                'Баланс пополнен автоматически!'
                            ),
                            parse_mode='HTML',
                            reply_markup=keyboard,
                        )
                    except Exception as error:
                        logger.error('Ошибка отправки уведомления пользователю', display_name=display_name, error=error)

                # Проверяем наличие сохраненной корзины для возврата к оформлению подписки
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

                logger.info(
                    '✅ Обработан платеж для пользователя',
                    display_name=display_name,
                    uuid=payment.uuid,
                    user_id=payment.user_id,
                )
                return True

            if payment_status == 'cancel':
                await payment_module.update_mulenpay_payment_status(
                    db,
                    payment=payment,
                    status='canceled',
                    callback_payload=callback_data,
                    mulen_payment_id=mulen_payment_id_int,
                )
                logger.info('платеж отменен', display_name=display_name, uuid=payment.uuid)
                return True

            await payment_module.update_mulenpay_payment_status(
                db,
                payment=payment,
                status=payment_status or 'unknown',
                callback_payload=callback_data,
                mulen_payment_id=mulen_payment_id_int,
            )
            logger.info(
                'Получен callback со статусом для платежа',
                display_name=display_name,
                payment_status=payment_status,
                uuid=payment.uuid,
            )
            return True

        except Exception as error:
            logger.error('Ошибка обработки callback', display_name=display_name, error=error, exc_info=True)
            return False

    def _map_mulenpay_status(self, status_code: int | None) -> str:
        """Приводит числовой статус MulenPay к строковому значению."""
        mapping = {
            0: 'created',
            1: 'processing',
            2: 'canceled',
            3: 'success',
            4: 'error',
            5: 'hold',
            6: 'hold',
        }
        return mapping.get(status_code, 'unknown')

    async def get_mulenpay_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        """Возвращает текущее состояние платежа и при необходимости синхронизирует его."""
        display_name = settings.get_mulenpay_display_name()
        try:
            payment_module = import_module('app.services.payment_service')

            payment = await payment_module.get_mulenpay_payment_by_local_id(db, local_payment_id)
            if not payment:
                return None

            remote_status_code = None
            remote_data = None

            if getattr(self, 'mulenpay_service', None) and payment.mulen_payment_id is not None:
                response = await self.mulenpay_service.get_payment(payment.mulen_payment_id)
                if response:
                    if isinstance(response, dict) and response.get('success'):
                        remote_data = response.get('payment')
                    elif isinstance(response, dict) and 'status' in response and 'id' in response:
                        remote_data = response
                if not remote_data and getattr(self, 'mulenpay_service', None):
                    list_response = await self.mulenpay_service.list_payments(
                        limit=100,
                        uuid=payment.uuid,
                    )
                    items = []
                    if isinstance(list_response, dict):
                        items = list_response.get('items') or []
                    if items:
                        for candidate in items:
                            if not isinstance(candidate, dict):
                                continue
                            candidate_id = candidate.get('id')
                            candidate_uuid = candidate.get('uuid')
                            if (candidate_id is not None and candidate_id == payment.mulen_payment_id) or (
                                candidate_uuid and candidate_uuid == payment.uuid
                            ):
                                remote_data = candidate
                                break

                if isinstance(remote_data, dict):
                    remote_status_code = remote_data.get('status')
                    mapped_status = self._map_mulenpay_status(remote_status_code)

                    if mapped_status == 'success' and not payment.is_paid:
                        await self.process_mulenpay_callback(
                            db,
                            {
                                'uuid': payment.uuid,
                                'payment_status': 'success',
                                'id': remote_data.get('id'),
                                'amount': remote_data.get('amount'),
                            },
                        )
                        payment = await payment_module.get_mulenpay_payment_by_local_id(db, local_payment_id)
                    elif mapped_status and mapped_status != payment.status:
                        await payment_module.update_mulenpay_payment_status(
                            db,
                            payment=payment,
                            status=mapped_status,
                            mulen_payment_id=remote_data.get('id'),
                        )
                        payment = await payment_module.get_mulenpay_payment_by_local_id(db, local_payment_id)

            return {
                'payment': payment,
                'status': payment.status,
                'is_paid': payment.is_paid,
                'remote_status_code': remote_status_code,
                'remote_data': remote_data,
            }

        except Exception as error:
            logger.error('Ошибка получения статуса', display_name=display_name, error=error, exc_info=True)
            return None
