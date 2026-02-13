"""Mixin with Heleket payment flow implementation."""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.subscription_auto_purchase_service import (
    auto_activate_subscription_after_topup,
    auto_purchase_saved_cart_after_topup,
)
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class HeleketPaymentMixin:
    """Provides helpers to create and process Heleket payments."""

    async def create_heleket_payment(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int,
        description: str,
        *,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        if not getattr(self, 'heleket_service', None):
            logger.error('Heleket сервис не инициализирован')
            return None

        if amount_kopeks <= 0:
            logger.error('Сумма Heleket должна быть положительной: %s', amount_kopeks)
            return None

        amount_rubles = amount_kopeks / 100
        amount_str = f'{amount_rubles:.2f}'

        order_id = f'heleket_{user_id}_{int(time.time())}_{secrets.token_hex(3)}'

        markup_percent = settings.get_heleket_markup_percent()
        discount_percent: int | None = None
        if markup_percent:
            try:
                rounded = int(round(markup_percent))
                if rounded != 0:
                    discount_percent = -rounded
            except (TypeError, ValueError):
                logger.warning('Некорректная наценка Heleket: %s', markup_percent)

        payload: dict[str, Any] = {
            'amount': amount_str,
            'currency': 'RUB',
            'order_id': order_id,
            'lifetime': settings.get_heleket_lifetime(),
        }

        to_currency = (settings.HELEKET_DEFAULT_CURRENCY or '').strip()
        if to_currency:
            payload['to_currency'] = to_currency

        network = (settings.HELEKET_DEFAULT_NETWORK or '').strip()
        if network:
            payload['network'] = network

        callback_url = settings.get_heleket_callback_url()
        if callback_url:
            payload['url_callback'] = callback_url

        if settings.HELEKET_RETURN_URL:
            payload['url_return'] = settings.HELEKET_RETURN_URL
        if settings.HELEKET_SUCCESS_URL:
            payload['url_success'] = settings.HELEKET_SUCCESS_URL

        if discount_percent is not None:
            payload['discount_percent'] = discount_percent

        metadata: dict[str, Any] = {
            'language': language or settings.DEFAULT_LANGUAGE,
            'created_at': datetime.utcnow().isoformat(),
        }

        try:
            response = await self.heleket_service.create_payment(payload)  # type: ignore[union-attr]
        except Exception as error:  # pragma: no cover - safety net
            logger.exception('Ошибка создания Heleket платежа: %s', error)
            return None

        if not response:
            logger.error('Heleket API вернул пустой ответ при создании платежа')
            return None

        payment_result = response.get('result') if isinstance(response, dict) else None
        if not payment_result:
            logger.error('Некорректный ответ Heleket API: %s', response)
            return None

        uuid = str(payment_result.get('uuid'))
        response_order_id = payment_result.get('order_id')
        if response_order_id:
            order_id = str(response_order_id)

        url = payment_result.get('url')
        status = payment_result.get('status') or payment_result.get('payment_status') or 'check'
        payer_amount = payment_result.get('payer_amount')
        payer_currency = payment_result.get('payer_currency')
        exchange_rate = payment_result.get('payer_amount_exchange_rate')

        try:
            exchange_rate_value = float(exchange_rate) if exchange_rate is not None else None
        except (TypeError, ValueError):
            exchange_rate_value = None

        if exchange_rate_value is None and payer_amount:
            try:
                exchange_rate_value = float(payer_amount) / amount_rubles if amount_rubles else None
            except (TypeError, ValueError, ZeroDivisionError):
                exchange_rate_value = None

        expires_at_raw = payment_result.get('expired_at')
        expires_at: datetime | None = None
        if expires_at_raw:
            try:
                expires_at = datetime.fromtimestamp(int(expires_at_raw))
            except (TypeError, ValueError, OSError):
                expires_at = None

        heleket_crud = import_module('app.database.crud.heleket')

        local_payment = await heleket_crud.create_heleket_payment(
            db=db,
            user_id=user_id,
            uuid=uuid,
            order_id=order_id,
            amount=amount_str,
            currency='RUB',
            status=status,
            payer_amount=payer_amount,
            payer_currency=payer_currency,
            exchange_rate=exchange_rate_value,
            discount_percent=discount_percent,
            payment_url=url,
            expires_at=expires_at,
            metadata={'raw_response': payment_result, **metadata},
        )

        logger.info(
            'Создан Heleket платеж %s на %s₽ для пользователя %s',
            uuid,
            amount_str,
            user_id,
        )

        return {
            'local_payment_id': local_payment.id,
            'uuid': uuid,
            'order_id': order_id,
            'amount': amount_str,
            'amount_kopeks': amount_kopeks,
            'payment_url': url,
            'status': status,
            'payer_amount': payer_amount,
            'payer_currency': payer_currency,
            'exchange_rate': exchange_rate_value,
            'discount_percent': discount_percent,
        }

    async def _process_heleket_payload(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
        *,
        metadata_key: str,
    ) -> HeleketPayment | None:
        if not isinstance(payload, dict):
            logger.error('Heleket webhook payload не является словарём: %s', payload)
            return None

        heleket_crud = import_module('app.database.crud.heleket')
        payment_module = import_module('app.services.payment_service')

        uuid = str(payload.get('uuid') or '').strip()
        order_id = str(payload.get('order_id') or '').strip()
        status = payload.get('status') or payload.get('payment_status')

        if not uuid and not order_id:
            logger.error('Heleket webhook без uuid/order_id: %s', payload)
            return None

        payment = None
        if uuid:
            payment = await heleket_crud.get_heleket_payment_by_uuid(db, uuid)
        if payment is None and order_id:
            payment = await heleket_crud.get_heleket_payment_by_order_id(db, order_id)

        if not payment:
            logger.error(
                'Heleket платеж не найден (uuid=%s order_id=%s)',
                uuid,
                order_id,
            )
            return None

        payer_amount = payload.get('payer_amount') or payload.get('payment_amount')
        payer_currency = payload.get('payer_currency') or payload.get('currency')
        discount_percent = payload.get('discount_percent')
        exchange_rate_raw = payload.get('payer_amount_exchange_rate')
        payment_url = payload.get('url')

        exchange_rate: float | None = None
        if exchange_rate_raw is not None:
            try:
                exchange_rate = float(exchange_rate_raw)
            except (TypeError, ValueError):
                exchange_rate = None

        if exchange_rate is None and payer_amount:
            try:
                exchange_rate = float(payer_amount) / payment.amount_float if payment.amount_float else None
            except (TypeError, ValueError, ZeroDivisionError):
                exchange_rate = None

        paid_at: datetime | None = None
        paid_at_raw = payload.get('paid_at') or payload.get('updated_at')
        if paid_at_raw:
            try:
                if isinstance(paid_at_raw, (int, float)):
                    paid_at = datetime.utcfromtimestamp(float(paid_at_raw))
                else:
                    paid_at = datetime.fromisoformat(str(paid_at_raw).replace('Z', '+00:00'))
            except (ValueError, TypeError):
                paid_at = None

        if paid_at and paid_at.tzinfo is not None:
            paid_at = paid_at.astimezone(UTC).replace(tzinfo=None)

        updated_payment = await heleket_crud.update_heleket_payment(
            db,
            payment.uuid,
            status=status,
            payer_amount=str(payer_amount) if payer_amount is not None else None,
            payer_currency=str(payer_currency) if payer_currency is not None else None,
            exchange_rate=exchange_rate,
            discount_percent=int(discount_percent) if isinstance(discount_percent, (int, float)) else None,
            paid_at=paid_at,
            payment_url=payment_url,
            metadata={metadata_key: payload},
        )

        if updated_payment is None:
            return None

        metadata = dict(getattr(updated_payment, 'metadata_json', {}) or {})
        invoice_message = metadata.get('invoice_message') or {}
        invoice_message_removed = False

        if getattr(self, 'bot', None) and invoice_message:
            chat_id = invoice_message.get('chat_id')
            message_id = invoice_message.get('message_id')
            if chat_id and message_id:
                try:
                    await self.bot.delete_message(chat_id, message_id)
                except Exception as delete_error:  # pragma: no cover - depends on rights
                    logger.warning(
                        'Не удалось удалить счёт Heleket %s: %s',
                        message_id,
                        delete_error,
                    )
                else:
                    metadata.pop('invoice_message', None)
                    invoice_message_removed = True

        if invoice_message_removed:
            try:
                from app.database.crud import heleket as heleket_crud

                await heleket_crud.update_heleket_payment(
                    db,
                    updated_payment.uuid,
                    metadata=metadata,
                )
                updated_payment.metadata_json = metadata
            except Exception as error:  # pragma: no cover - diagnostics
                logger.warning(
                    'Не удалось обновить метаданные Heleket после удаления счёта: %s',
                    error,
                )

        if updated_payment.transaction_id:
            logger.info(
                'Heleket платеж %s уже связан с транзакцией %s',
                updated_payment.uuid,
                updated_payment.transaction_id,
            )
            return updated_payment

        status_normalized = (status or '').lower()
        if status_normalized not in {'paid', 'paid_over'}:
            logger.info('Heleket платеж %s в статусе %s, зачисление не требуется', updated_payment.uuid, status)
            return updated_payment

        amount_kopeks = updated_payment.amount_kopeks
        if amount_kopeks <= 0:
            logger.error('Heleket платеж %s имеет некорректную сумму: %s', updated_payment.uuid, updated_payment.amount)
            return None

        transaction = await payment_module.create_transaction(
            db,
            user_id=updated_payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=amount_kopeks,
            description=(
                'Пополнение через Heleket'
                if not updated_payment.payer_currency
                else (f'Пополнение через Heleket ({updated_payment.payer_amount} {updated_payment.payer_currency})')
            ),
            payment_method=PaymentMethod.HELEKET,
            external_id=updated_payment.uuid,
            is_completed=True,
        )

        linked_payment = await heleket_crud.link_heleket_payment_to_transaction(
            db,
            updated_payment.uuid,
            transaction.id,
        )
        if linked_payment:
            updated_payment = linked_payment

        get_user_by_id = payment_module.get_user_by_id
        user = await get_user_by_id(db, updated_payment.user_id)
        if not user:
            logger.error('Пользователь %s не найден для Heleket платежа', updated_payment.user_id)
            return None

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += amount_kopeks
        user.updated_at = datetime.utcnow()

        await db.commit()
        await db.refresh(user)

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:  # pragma: no cover - defensive
            logger.error('Ошибка реферального начисления Heleket: %s', error)

        if was_first_topup and not user.has_made_first_topup:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

        # Перезагружаем пользователя с зависимостями после коммита,
        # чтобы избежать lazy load в async-контексте (MissingGreenlet)
        user = await get_user_by_id(db, user.id) or user

        if getattr(self, 'bot', None):
            topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'
            referrer_info = format_referrer_info(user)
            subscription = getattr(user, 'subscription', None)
            promo_group = user.get_primary_promo_group()

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
            except Exception as error:  # pragma: no cover
                logger.error('Ошибка отправки админ-уведомления Heleket: %s', error)

            # Отправляем уведомление только Telegram-пользователям
            if user.telegram_id:
                try:
                    keyboard = await self.build_topup_success_keyboard(user)

                    exchange_rate_value = updated_payment.exchange_rate or 0
                    rate_text = (
                        f'💱 Курс: 1 RUB = {1 / exchange_rate_value:.4f} {updated_payment.payer_currency}'
                        if exchange_rate_value and updated_payment.payer_currency
                        else None
                    )

                    message_lines = [
                        '✅ <b>Пополнение успешно!</b>',
                        f'💰 Сумма: {settings.format_price(amount_kopeks)}',
                        '💳 Способ: Heleket',
                    ]
                    if updated_payment.payer_amount and updated_payment.payer_currency:
                        message_lines.append(
                            f'🪙 Оплата: {updated_payment.payer_amount} {updated_payment.payer_currency}'
                        )
                    if rate_text:
                        message_lines.append(rate_text)

                    await self.bot.send_message(
                        chat_id=user.telegram_id,
                        text='\n'.join(message_lines),
                        parse_mode='HTML',
                        reply_markup=keyboard,
                    )
                except Exception as error:  # pragma: no cover
                    logger.error('Ошибка отправки уведомления пользователю Heleket: %s', error)
            else:
                logger.info(f'Пропуск Telegram-уведомления Heleket для email-пользователя {user.id}')

        # Автопокупка из сохранённой корзины и умная автоактивация
        try:
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
            if not auto_purchase_success:
                try:
                    await auto_activate_subscription_after_topup(
                        db,
                        user,
                        bot=getattr(self, 'bot', None),
                        topup_amount=amount_kopeks,
                    )
                except Exception as auto_activate_error:
                    logger.error(
                        'Ошибка умной автоактивации для пользователя %s: %s',
                        user.id,
                        auto_activate_error,
                        exc_info=True,
                    )
        except Exception as error:
            logger.error(
                'Ошибка при работе с автоактивацией для пользователя %s: %s',
                user.id,
                error,
                exc_info=True,
            )

        return updated_payment

    async def process_heleket_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        result = await self._process_heleket_payload(
            db,
            payload,
            metadata_key='last_webhook',
        )

        return result is not None

    async def sync_heleket_payment_status(
        self,
        db: AsyncSession,
        *,
        local_payment_id: int,
    ) -> HeleketPayment | None:
        if not getattr(self, 'heleket_service', None):
            logger.error('Heleket сервис не инициализирован')
            return None

        heleket_crud = import_module('app.database.crud.heleket')

        payment = await heleket_crud.get_heleket_payment_by_id(db, local_payment_id)
        if not payment:
            logger.error('Heleket платеж с id=%s не найден', local_payment_id)
            return None

        payload: dict[str, Any] | None = None
        try:
            response = await self.heleket_service.get_payment_info(  # type: ignore[union-attr]
                uuid=payment.uuid,
                order_id=payment.order_id,
            )
        except Exception as error:  # pragma: no cover - defensive
            logger.exception(
                'Ошибка получения статуса Heleket платежа %s: %s',
                payment.uuid,
                error,
            )
        else:
            if response:
                result = response.get('result') if isinstance(response, dict) else None
                if isinstance(result, dict):
                    payload = dict(result)
                else:
                    logger.error(
                        'Некорректный ответ Heleket API при проверке платежа %s: %s',
                        payment.uuid,
                        response,
                    )

        if payload is None:
            fallback = await self._lookup_heleket_payment_history(payment)
            if not fallback:
                logger.warning(
                    'Heleket API не вернул информацию по платежу %s',
                    payment.uuid,
                )
                return payment
            payload = dict(fallback)

        payload.setdefault('uuid', payment.uuid)
        payload.setdefault('order_id', payment.order_id)

        updated_payment = await self._process_heleket_payload(
            db,
            payload,
            metadata_key='last_status_check',
        )

        return updated_payment or payment

    async def _lookup_heleket_payment_history(
        self,
        payment: HeleketPayment,
    ) -> dict[str, Any] | None:
        service = getattr(self, 'heleket_service', None)
        if not service:
            return None

        created_at = getattr(payment, 'created_at', None)
        date_from_str: str | None = None
        date_to_str: str | None = None
        if isinstance(created_at, datetime):
            start = created_at - timedelta(days=2)
            end = created_at + timedelta(days=2)
            date_from_str = start.strftime('%Y-%m-%d %H:%M:%S')
            date_to_str = end.strftime('%Y-%m-%d %H:%M:%S')

        cursor: str | None = None
        for _ in range(10):
            response = await service.list_payments(
                date_from=date_from_str,
                date_to=date_to_str,
                cursor=cursor,
            )
            if not response or not isinstance(response, dict):
                return None

            result = response.get('result')
            if not isinstance(result, dict):
                return None

            items = result.get('items')
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    uuid = str(item.get('uuid') or '').strip()
                    order_id = str(item.get('order_id') or '').strip()
                    if uuid and uuid == str(payment.uuid):
                        return item
                    if order_id and order_id == str(payment.order_id):
                        return item

            paginate = result.get('paginate')
            cursor = None
            if isinstance(paginate, dict):
                next_cursor = paginate.get('nextCursor')
                if isinstance(next_cursor, str) and next_cursor:
                    cursor = next_cursor

            if not cursor:
                break

        return None
