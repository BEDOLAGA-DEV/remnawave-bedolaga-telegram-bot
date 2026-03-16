"""Mixin for integrating WATA payment links into the payment service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.wata_service import WataAPIError, WataService
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


def _extract_transaction_id(payment: Any, remote_link: dict[str, Any] | None = None) -> str | None:
    """Try to find the remote WATA transaction identifier from stored payloads."""

    def _from_mapping(mapping: Any) -> str | None:
        if isinstance(mapping, str):
            try:
                import json

                mapping = json.loads(mapping)
            except Exception:  # pragma: no cover - defensive parsing
                return None
        if not isinstance(mapping, dict):
            return None
        for key in ('id', 'transaction_id', 'transactionId'):
            value = mapping.get(key)
            if not value:
                continue
            value_str = str(value)
            if '-' in value_str:
                return value_str
        return None

    candidate = None

    if hasattr(payment, 'callback_payload'):
        candidate = _from_mapping(payment.callback_payload)
        if candidate:
            return candidate

    metadata = getattr(payment, 'metadata_json', None)
    if isinstance(metadata, dict):
        if 'transaction' in metadata:
            candidate = _from_mapping(metadata.get('transaction'))
            if candidate:
                return candidate
        candidate = _from_mapping(metadata)
        if candidate:
            return candidate

    candidate = _from_mapping(remote_link)
    if candidate:
        return candidate

    return None


class WataPaymentMixin:
    """Encapsulates creation and status handling for WATA payment links."""

    async def create_wata_payment(
        self,
        db: AsyncSession,
        user_id: int | None,
        amount_kopeks: int,
        description: str,
        *,
        language: str | None = None,
        return_url: str | None = None,
        failed_url: str | None = None,
    ) -> dict[str, Any] | None:
        if not getattr(self, 'wata_service', None):
            logger.error('WATA service is not initialised')
            return None

        if amount_kopeks < settings.WATA_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма WATA меньше минимальной: <',
                amount_kopeks=amount_kopeks,
                WATA_MIN_AMOUNT_KOPEKS=settings.WATA_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.WATA_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма WATA больше максимальной: >',
                amount_kopeks=amount_kopeks,
                WATA_MAX_AMOUNT_KOPEKS=settings.WATA_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module('app.services.payment_service')

        # Добавляем идентификатор плательщика (telegram_id или email) в описание
        try:
            if user_id is not None:
                user = await payment_module.get_user_by_id(db, user_id)
            else:
                user = None
            if user:
                if user.telegram_id:
                    description = f'{description} | ID: {user.telegram_id}'
                elif user.email:
                    description = f'{description} | {user.email}'
        except Exception as error:
            logger.debug('Не удалось получить данные пользователя для описания WATA', error=error)

        order_id = f'wata_{user_id or "guest"}_{uuid.uuid4().hex[:12]}'

        try:
            response = await self.wata_service.create_payment_link(  # type: ignore[union-attr]
                amount_kopeks=amount_kopeks,
                currency='RUB',
                description=description,
                order_id=order_id,
                success_url=return_url,
                fail_url=failed_url,
            )
        except WataAPIError as error:
            logger.error('Ошибка создания WATA платежа', error=error)
            return None
        except Exception as error:  # pragma: no cover - safety net
            logger.exception('Непредвиденная ошибка при создании WATA платежа', error=error)
            return None

        payment_link_id = response.get('id')
        payment_url = response.get('url') or response.get('paymentUrl')
        status = response.get('status') or 'Opened'
        terminal_public_id = response.get('terminalPublicId')
        success_url = response.get('successRedirectUrl')
        fail_url = response.get('failRedirectUrl')

        if not payment_link_id:
            logger.error('WATA API не вернула идентификатор платежной ссылки', response=response)
            return None

        expiration_raw = response.get('expirationDateTime')
        expires_at = WataService._parse_datetime(expiration_raw)

        metadata = {
            'response': response,
            'language': language or settings.DEFAULT_LANGUAGE,
        }

        local_payment = await payment_module.create_wata_payment(
            db=db,
            user_id=user_id,
            payment_link_id=payment_link_id,
            amount_kopeks=amount_kopeks,
            currency='RUB',
            description=description,
            status=status,
            type_=response.get('type'),
            url=payment_url,
            order_id=order_id,
            metadata=metadata,
            expires_at=expires_at,
            terminal_public_id=terminal_public_id,
            success_redirect_url=success_url,
            fail_redirect_url=fail_url,
        )

        logger.info(
            'Создан WATA платеж на ₽ для пользователя',
            payment_link_id=payment_link_id,
            amount_kopeks=amount_kopeks / 100,
            user_id=user_id,
        )

        return {
            'local_payment_id': local_payment.id,
            'payment_link_id': payment_link_id,
            'payment_url': payment_url,
            'status': status,
            'order_id': order_id,
        }

    async def process_wata_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Handles asynchronous webhook notifications from WATA."""

        payment_module = import_module('app.services.payment_service')

        if not isinstance(payload, dict):
            logger.error('WATA webhook payload не является словарём', payload=payload)
            return False

        order_id_raw = payload.get('orderId')
        payment_link_raw = payload.get('paymentLinkId') or payload.get('id') or payload.get('transactionId')
        transaction_status_raw = payload.get('transactionStatus')

        order_id = str(order_id_raw) if order_id_raw else None
        payment_link_id = str(payment_link_raw) if payment_link_raw else None
        transaction_status = (transaction_status_raw or '').strip()

        if not order_id and not payment_link_id:
            logger.error('WATA webhook без orderId и paymentLinkId', payload=payload)
            return False

        if not transaction_status:
            logger.error('WATA webhook без статуса транзакции', payload=payload)
            return False

        payment = None
        if order_id:
            payment = await payment_module.get_wata_payment_by_order_id(db, order_id)
        if not payment and payment_link_id:
            payment = await payment_module.get_wata_payment_by_link_id(db, payment_link_id)

        if not payment:
            logger.error(
                'WATA платеж не найден (order_id payment_link_id=)', order_id=order_id, payment_link_id=payment_link_id
            )
            return False

        status_lower = transaction_status.lower()
        metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        metadata['last_webhook'] = payload
        terminal_public_id = (
            payload.get('terminalPublicId') or payload.get('terminal_public_id') or payload.get('terminalPublicID')
        )

        update_kwargs: dict[str, Any] = {
            'metadata': metadata,
            'callback_payload': payload,
            'terminal_public_id': terminal_public_id,
        }

        if transaction_status:
            update_kwargs['status'] = transaction_status
            update_kwargs['last_status'] = transaction_status

        if status_lower != 'paid' and not payment.is_paid:
            update_kwargs['is_paid'] = False

        payment = await payment_module.update_wata_payment_status(
            db,
            payment=payment,
            **update_kwargs,
        )

        if status_lower == 'paid':
            if payment.is_paid:
                logger.info('WATA платеж уже помечен как оплачен', payment_link_id=payment.payment_link_id)
                return True

            await self._finalize_wata_payment(db, payment, payload)
            return True

        if status_lower == 'declined':
            logger.info('WATA платеж отклонён', payment_link_id=payment.payment_link_id)

        return True

    async def get_wata_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        payment_module = import_module('app.services.payment_service')

        payment = await payment_module.get_wata_payment_by_id(db, local_payment_id)
        if not payment:
            return None

        remote_link: dict[str, Any] | None = None
        transaction_payload: dict[str, Any] | None = None
        transaction_id: str | None = None

        if getattr(self, 'wata_service', None) and payment.payment_link_id:
            try:
                remote_link = await self.wata_service.get_payment_link(payment.payment_link_id)  # type: ignore[union-attr]
            except WataAPIError as error:
                logger.error('Ошибка получения WATA ссылки', payment_link_id=payment.payment_link_id, error=error)
            except Exception as error:  # pragma: no cover - safety net
                logger.exception('Непредвиденная ошибка при запросе WATA ссылки', error=error)

        if remote_link:
            remote_status = remote_link.get('status') or payment.status
            if remote_status != payment.status:
                existing_metadata = dict(getattr(payment, 'metadata_json', {}) or {})
                existing_metadata['link'] = remote_link
                await payment_module.update_wata_payment_status(
                    db,
                    payment=payment,
                    status=remote_status,
                    last_status=remote_status,
                    url=remote_link.get('url') or remote_link.get('paymentUrl'),
                    metadata=existing_metadata,
                    terminal_public_id=remote_link.get('terminalPublicId'),
                )
                payment = await payment_module.get_wata_payment_by_id(db, local_payment_id)

            remote_status_normalized = (remote_status or '').lower()
            if remote_status_normalized in {'closed', 'paid'} and not payment.is_paid:
                transaction_id = _extract_transaction_id(payment, remote_link)
                if transaction_id:
                    try:
                        transaction_payload = await self.wata_service.get_transaction(  # type: ignore[union-attr]
                            transaction_id
                        )
                    except WataAPIError as error:
                        logger.error('Ошибка получения WATA транзакции', transaction_id=transaction_id, error=error)
                    except Exception as error:  # pragma: no cover - safety net
                        logger.exception(
                            'Непредвиденная ошибка при запросе WATA транзакции',
                            transaction_id=transaction_id,
                            error=error,
                        )
                if not transaction_payload:
                    try:
                        tx_response = await self.wata_service.search_transactions(  # type: ignore[union-attr]
                            order_id=payment.order_id,
                            payment_link_id=payment.payment_link_id,
                            status='Paid',
                            limit=5,
                        )
                        items = tx_response.get('items') or []
                        for item in items:
                            if (item or {}).get('status') == 'Paid':
                                transaction_payload = item
                                break
                    except WataAPIError as error:
                        logger.error(
                            'Ошибка поиска WATA транзакций для', payment_link_id=payment.payment_link_id, error=error
                        )
                    except Exception as error:  # pragma: no cover - safety net
                        logger.exception('Непредвиденная ошибка при поиске WATA транзакции', error=error)

        if not transaction_payload and not payment.is_paid and getattr(self, 'wata_service', None):
            fallback_transaction_id = transaction_id or _extract_transaction_id(payment)
            if fallback_transaction_id:
                try:
                    transaction_payload = await self.wata_service.get_transaction(  # type: ignore[union-attr]
                        fallback_transaction_id
                    )
                except WataAPIError as error:
                    logger.error(
                        'Ошибка повторного запроса WATA транзакции',
                        fallback_transaction_id=fallback_transaction_id,
                        error=error,
                    )
                except Exception as error:  # pragma: no cover - safety net
                    logger.exception(
                        'Непредвиденная ошибка при повторном запросе WATA транзакции',
                        fallback_transaction_id=fallback_transaction_id,
                        error=error,
                    )

        if transaction_payload and not payment.is_paid:
            normalized_status = None
            if isinstance(transaction_payload, dict):
                raw_status = transaction_payload.get('status') or transaction_payload.get('statusName')
                if raw_status:
                    normalized_status = str(raw_status).lower()
            if normalized_status == 'paid':
                payment = await self._finalize_wata_payment(db, payment, transaction_payload)
            else:
                logger.debug(
                    'WATA транзакция в статусе , повторная обработка не требуется',
                    transaction_id=transaction_id or getattr(payment, 'payment_link_id', ''),
                    normalized_status=normalized_status or 'unknown',
                )

        return {
            'payment': payment,
            'status': payment.status,
            'is_paid': payment.is_paid,
            'remote_link': remote_link,
            'transaction': transaction_payload,
        }

    async def _finalize_wata_payment(
        self,
        db: AsyncSession,
        payment: Any,
        transaction_payload: dict[str, Any],
    ) -> Any:
        payment_module = import_module('app.services.payment_service')

        if isinstance(transaction_payload, dict):
            paid_status = transaction_payload.get('status') or transaction_payload.get('statusName')
        else:
            paid_status = None
        if paid_status and str(paid_status).lower() not in {'paid', 'declined', 'pending'}:
            logger.debug(
                'Неизвестный статус WATA транзакции',
                getattr=getattr(payment, 'payment_link_id', ''),
                paid_status=paid_status,
            )

        paid_at = None
        if isinstance(transaction_payload, dict):
            paid_at = WataService._parse_datetime(transaction_payload.get('paymentTime'))
        if not paid_at and getattr(payment, 'paid_at', None):
            paid_at = payment.paid_at
        existing_metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        invoice_message = existing_metadata.get('invoice_message') or {}
        if getattr(self, 'bot', None) and invoice_message:
            chat_id = invoice_message.get('chat_id')
            message_id = invoice_message.get('message_id')
            if chat_id and message_id:
                try:
                    await self.bot.delete_message(chat_id, message_id)
                except Exception as delete_error:  # pragma: no cover - depends on rights
                    logger.warning('Не удалось удалить счёт WATA', message_id=message_id, delete_error=delete_error)
                else:
                    existing_metadata.pop('invoice_message', None)

        existing_metadata['transaction'] = transaction_payload

        await payment_module.update_wata_payment_status(
            db,
            payment=payment,
            status='Paid',
            is_paid=True,
            paid_at=paid_at,
            callback_payload=transaction_payload,
            metadata=existing_metadata,
        )

        wata_lock_crud = import_module('app.database.crud.wata')
        locked = await wata_lock_crud.get_wata_payment_by_id_for_update(db, payment.id)
        if not locked:
            logger.error('WATA: не удалось заблокировать платёж', payment_id=payment.id)
            return None
        payment = locked

        if payment.transaction_id:
            logger.info(
                'WATA платеж уже привязан к транзакции',
                payment_link_id=payment.payment_link_id,
                transaction_id=payment.transaction_id,
            )
            return payment

        # --- Guest purchase flow (landing page) ---
        wata_metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=wata_metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.payment_link_id,
            provider_name='wata',
        )
        if guest_result is not None:
            return payment

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден при обработке WATA', user_id=payment.user_id)
            return payment

        transaction_external_id = str(transaction_payload.get('id') or transaction_payload.get('transactionId') or '')
        description = f'Пополнение через WATA ({payment.payment_link_id})'

        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment.amount_kopeks,
            description=description,
            payment_method=PaymentMethod.WATA,
            external_id=transaction_external_id or payment.payment_link_id,
            is_completed=True,
            created_at=getattr(payment, 'created_at', None),
            commit=False,
        )

        await payment_module.link_wata_payment_to_transaction(db, payment, transaction.id)

        # Lock user row to prevent concurrent balance race conditions
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

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
            payment_method=PaymentMethod.WATA,
            external_id=transaction_external_id or payment.payment_link_id,
        )

        user = await payment_module.get_user_by_id(db, user.id)
        if not user:
            logger.error('Пользователь не найден после коммита WATA', user_id=payment.user_id)
            return payment

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)
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
            logger.error('Ошибка обработки реферального пополнения WATA', error=error)

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
                logger.error('Ошибка отправки админ уведомления WATA', error=error)

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        '🦊 Способ: WATA\n'
                        f'🆔 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю WATA', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.debug('Не удалось отправить напоминание о корзине после WATA', error=error)

        return payment
