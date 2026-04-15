"""Mixin для интеграции с Robokassa."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class RobokassaPaymentMixin:
    """Создание платежей, обработка ResultURL-callback и проверка статусов Robokassa."""

    async def create_robokassa_payment(
        self,
        db: AsyncSession,
        user_id: int | None,
        amount_kopeks: int,
        description: str,
        inc_curr_label: str | None = None,
    ) -> dict[str, Any] | None:
        display_name = settings.get_robokassa_display_name()
        service = getattr(self, 'robokassa_service', None)
        if not service or not service.is_configured:
            logger.error('Robokassa service не инициализирован', display_name=display_name)
            return None

        if amount_kopeks < settings.ROBOKASSA_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Robokassa: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                min_kopeks=settings.ROBOKASSA_MIN_AMOUNT_KOPEKS,
            )
            return None
        if amount_kopeks > settings.ROBOKASSA_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Robokassa: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                max_kopeks=settings.ROBOKASSA_MAX_AMOUNT_KOPEKS,
            )
            return None

        robokassa_crud = import_module('app.database.crud.robokassa')

        try:
            # Генерируем уникальный 32-битный InvId: [epoch sec << 10] + random 10 bits
            base = (int(datetime.now(UTC).timestamp()) % 2_000_000) * 1024
            inv_id = base + secrets.randbelow(1024)
            # Защита от маловероятного совпадения
            existing = await robokassa_crud.get_robokassa_payment_by_inv_id(db, inv_id)
            while existing is not None:
                inv_id = base + secrets.randbelow(1024)
                existing = await robokassa_crud.get_robokassa_payment_by_inv_id(db, inv_id)

            payment_url = service.build_payment_url(
                amount_kopeks=amount_kopeks,
                inv_id=inv_id,
                description=description,
                inc_curr_label=inc_curr_label or settings.ROBOKASSA_INC_CURR_LABEL,
            )
            if not payment_url:
                logger.error('Robokassa: не удалось сформировать URL оплаты')
                return None

            metadata = {
                'user_id': user_id,
                'amount_kopeks': amount_kopeks,
                'description': description,
                'inc_curr_label': inc_curr_label or settings.ROBOKASSA_INC_CURR_LABEL,
            }

            local_payment = await robokassa_crud.create_robokassa_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                inv_id=inv_id,
                description=description,
                payment_url=payment_url,
                currency='RUB',
                status='created',
                inc_curr_label=inc_curr_label or settings.ROBOKASSA_INC_CURR_LABEL,
                metadata=metadata,
            )

            logger.info(
                'Создан платеж Robokassa',
                inv_id=inv_id,
                amount_kopeks=amount_kopeks,
                user_id=user_id,
            )

            return {
                'local_payment_id': local_payment.id,
                'inv_id': inv_id,
                'payment_url': payment_url,
                'amount_kopeks': amount_kopeks,
                'status': 'created',
            }
        except Exception as error:
            logger.error('Robokassa: ошибка создания платежа', display_name=display_name, error=error, exc_info=True)
            return None

    async def process_robokassa_callback(
        self,
        db: AsyncSession,
        callback_data: dict[str, Any],
    ) -> bool:
        """Обрабатывает ResultURL-callback от Robokassa."""
        display_name = settings.get_robokassa_display_name()
        display_name_html = settings.get_robokassa_display_name_html()
        service = getattr(self, 'robokassa_service', None)
        if not service:
            logger.error('Robokassa service не инициализирован (callback)')
            return False

        try:
            out_sum_raw = callback_data.get('OutSum') or callback_data.get('out_sum')
            inv_id_raw = callback_data.get('InvId') or callback_data.get('inv_id')
            signature = callback_data.get('SignatureValue') or callback_data.get('signature_value') or ''

            if out_sum_raw is None or inv_id_raw is None:
                logger.error('Robokassa callback: отсутствуют OutSum/InvId')
                return False

            try:
                inv_id = int(str(inv_id_raw))
            except (TypeError, ValueError):
                logger.error('Robokassa callback: некорректный InvId', inv_id_raw=inv_id_raw)
                return False

            amount_str = str(out_sum_raw).replace(',', '.')

            user_params = {
                str(key): str(value)
                for key, value in callback_data.items()
                if isinstance(key, str) and key.lower().startswith('shp_')
            }

            if not service.verify_result_signature(
                amount_str=amount_str,
                inv_id=inv_id,
                signature=str(signature),
                user_params=user_params,
            ):
                logger.warning('Robokassa callback: неверная подпись', inv_id=inv_id)
                return False

            robokassa_crud = import_module('app.database.crud.robokassa')
            payment = await robokassa_crud.get_robokassa_payment_by_inv_id(db, inv_id)
            if not payment:
                logger.error('Robokassa callback: платеж не найден', inv_id=inv_id)
                return False

            locked = await robokassa_crud.get_robokassa_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Robokassa callback: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка суммы
            expected_amount_str = service._format_amount(payment.amount_kopeks)
            if expected_amount_str != service._format_amount(int(round(float(amount_str) * 100))):
                logger.warning(
                    'Robokassa callback: несоответствие суммы',
                    expected=expected_amount_str,
                    got=amount_str,
                )
                return False

            metadata = dict(getattr(payment, 'metadata_json', {}) or {})
            invoice_message = metadata.get('invoice_message') or {}
            invoice_message_removed = False

            if getattr(self, 'bot', None) and invoice_message:
                chat_id = invoice_message.get('chat_id')
                message_id = invoice_message.get('message_id')
                if chat_id and message_id:
                    try:
                        await self.bot.delete_message(chat_id, message_id)
                    except Exception as delete_error:
                        logger.warning('Не удалось удалить счёт Robokassa', delete_error=delete_error)
                    else:
                        metadata.pop('invoice_message', None)
                        invoice_message_removed = True

            if payment.is_paid:
                if invoice_message_removed:
                    try:
                        payment.metadata_json = metadata
                        payment.updated_at = datetime.now(UTC)
                        await db.commit()
                    except Exception as error:
                        logger.warning('Robokassa: не удалось обновить метаданные', error=error)
                logger.info('Robokassa: платеж уже обработан', inv_id=inv_id)
                return True

            payment_module = import_module('app.services.payment_service')

            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.callback_payload = callback_data
            payment.metadata_json = metadata
            payment.updated_at = datetime.now(UTC)
            await db.flush()

            if payment.transaction_id:
                logger.info('Robokassa: транзакция уже создана', inv_id=inv_id)
                return True

            # Guest purchase flow
            payment_meta = dict(getattr(payment, 'metadata_json', {}) or {})
            from app.services.payment.common import try_fulfill_guest_purchase

            guest_result = await try_fulfill_guest_purchase(
                db,
                metadata=payment_meta,
                payment_amount_kopeks=payment.amount_kopeks,
                provider_payment_id=str(payment.inv_id),
                provider_name='robokassa',
            )
            if guest_result is not None:
                return True

            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=f'Пополнение через {display_name} (#{payment.inv_id})',
                payment_method=PaymentMethod.ROBOKASSA,
                external_id=str(payment.inv_id),
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )

            await payment_module.link_robokassa_payment_to_transaction(
                db=db,
                payment=payment,
                transaction_id=transaction.id,
            )

            user = await payment_module.get_user_by_id(db, payment.user_id)
            if not user:
                logger.error('Robokassa: пользователь не найден', user_id=payment.user_id)
                return False

            from app.database.crud.user import lock_user_for_update

            user = await lock_user_for_update(db, user)
            old_balance = user.balance_kopeks
            was_first_topup = not user.has_made_first_topup
            user.balance_kopeks += payment.amount_kopeks
            user.updated_at = datetime.now(UTC)

            await db.commit()

            from app.database.crud.transaction import emit_transaction_side_effects

            await emit_transaction_side_effects(
                db,
                transaction,
                amount_kopeks=payment.amount_kopeks,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                payment_method=PaymentMethod.ROBOKASSA,
                external_id=str(payment.inv_id),
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
                logger.error('Robokassa: ошибка реферального пополнения', error=error)

            if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
                user.has_made_first_topup = True
                await db.commit()

            user = await payment_module.get_user_by_id(db, user.id)
            if not user:
                return False

            promo_group = user.get_primary_promo_group()
            subscription = getattr(user, 'subscription', None)
            referrer_info = format_referrer_info(user)
            topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'

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
                    logger.error('Robokassa: ошибка уведомления админа', error=error)

            if getattr(self, 'bot', None) and user.telegram_id:
                try:
                    keyboard = await self.build_topup_success_keyboard(user)
                    await self.bot.send_message(
                        user.telegram_id,
                        (
                            '✅ <b>Пополнение успешно!</b>\n\n'
                            f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                            f'💳 Способ: {display_name_html}\n'
                            f'🆔 Транзакция: {transaction.id}\n\n'
                            'Баланс пополнен автоматически!'
                        ),
                        parse_mode='HTML',
                        reply_markup=keyboard,
                    )
                except Exception as error:
                    logger.error('Robokassa: ошибка уведомления пользователя', error=error)

            try:
                from app.services.payment.common import send_cart_notification_after_topup

                await send_cart_notification_after_topup(
                    user, payment.amount_kopeks, db, getattr(self, 'bot', None)
                )
            except Exception as error:
                logger.error('Robokassa: ошибка уведомления о корзине', user_id=user.id, error=error)

            logger.info('Robokassa: платеж обработан', inv_id=inv_id, user_id=payment.user_id)
            return True

        except Exception as error:
            logger.error('Robokassa callback: ошибка обработки', error=error, exc_info=True)
            return False

    async def get_robokassa_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        try:
            robokassa_crud = import_module('app.database.crud.robokassa')
            payment = await robokassa_crud.get_robokassa_payment_by_local_id(db, local_payment_id)
            if not payment:
                return None
            return {
                'payment': payment,
                'status': payment.status,
                'is_paid': payment.is_paid,
            }
        except Exception as error:
            logger.error('Robokassa: ошибка получения статуса', error=error, exc_info=True)
            return None
