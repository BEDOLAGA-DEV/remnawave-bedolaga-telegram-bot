"""Mixin для интеграции с SeverPay (severpay.io)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.severpay_service import severpay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов SeverPay -> internal
SEVERPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'new': ('pending', False),
    'process': ('processing', False),
    'success': ('success', True),
    'decline': ('declined', False),
    'fail': ('failed', False),
}


class SeverPayPaymentMixin:
    """Mixin для работы с платежами SeverPay."""

    async def create_severpay_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Создает платеж SeverPay.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_severpay_enabled():
            logger.error('SeverPay не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.SEVERPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'SeverPay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                SEVERPAY_MIN_AMOUNT_KOPEKS=settings.SEVERPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.SEVERPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'SeverPay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                SEVERPAY_MAX_AMOUNT_KOPEKS=settings.SEVERPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id
        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            tg_id = 'guest'

        # Генерируем уникальный order_id с telegram_id для удобного поиска
        order_id = f'sp{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.SEVERPAY_CURRENCY

        # Срок действия платежа
        lifetime = settings.SEVERPAY_LIFETIME
        expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            # Используем API для создания платежа
            result = await severpay_service.create_payment(
                order_id=order_id,
                amount=amount_rubles,
                currency=currency,
                client_email=email or '',
                client_id=str(tg_id),
                url_return=return_url or settings.SEVERPAY_RETURN_URL,
                lifetime=lifetime,
            )

            payment_url = result.get('url')
            severpay_id = str(result.get('id', '')) if result.get('id') else None
            severpay_uid = result.get('uid')

            if not payment_url:
                logger.error('SeverPay API не вернул URL платежа', result=result)
                return None

            logger.info(
                'SeverPay API: создан платеж',
                order_id=order_id,
                severpay_id=severpay_id,
                payment_url=payment_url,
            )

            # Вычисляем expires_at из ответа API если доступен
            expire_at_raw = result.get('expire_at')
            if expire_at_raw:
                try:
                    expires_at = datetime.fromtimestamp(int(expire_at_raw), tz=UTC)
                except (ValueError, TypeError):
                    pass

            # Сохраняем в БД
            severpay_crud = import_module('app.database.crud.severpay')
            local_payment = await severpay_crud.create_severpay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                severpay_id=severpay_id,
                severpay_uid=severpay_uid,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'SeverPay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'severpay_id': severpay_id,
                'severpay_uid': severpay_uid,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('SeverPay: ошибка создания платежа', error=e)
            return None

    async def process_severpay_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает webhook от SeverPay.

        Подпись проверяется в webserver/payments.py до вызова этого метода.

        Args:
            db: Сессия БД
            payload: JSON тело webhook (sign проверен в webserver, тело передаётся как есть)

        Returns:
            True если платеж успешно обработан
        """
        try:
            webhook_type = payload.get('type')
            if webhook_type != 'payin':
                logger.info('SeverPay webhook: пропускаем тип', webhook_type=webhook_type)
                return True

            data = payload.get('data', {})
            severpay_id = str(data.get('id', '')) if data.get('id') else None
            order_id = data.get('order_id')
            severpay_status = data.get('status')
            amount = data.get('amount')

            if not severpay_id or not severpay_status:
                logger.warning('SeverPay webhook: отсутствуют обязательные поля', payload=payload)
                return False

            # Ищем платеж по order_id (наш) или severpay_id
            severpay_crud = import_module('app.database.crud.severpay')
            payment = None
            if order_id:
                payment = await severpay_crud.get_severpay_payment_by_order_id(db, order_id)
            if not payment and severpay_id:
                payment = await severpay_crud.get_severpay_payment_by_severpay_id(db, severpay_id)

            if not payment:
                logger.warning(
                    'SeverPay webhook: платеж не найден',
                    order_id=order_id,
                    severpay_id=severpay_id,
                )
                return False

            # Проверка дублирования
            if payment.is_paid:
                logger.info('SeverPay webhook: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = SEVERPAY_STATUS_MAP.get(severpay_status, ('pending', False))
            internal_status, is_paid = status_info

            callback_payload = {
                'severpay_id': severpay_id,
                'order_id': order_id,
                'status': severpay_status,
                'amount': amount,
                'currency': data.get('currency'),
            }

            # Проверка суммы ДО обновления статуса
            if is_paid and amount is not None:
                received_kopeks = round(float(amount) * 100)
                if abs(received_kopeks - payment.amount_kopeks) > 1:
                    logger.error(
                        'SeverPay amount mismatch',
                        expected_kopeks=payment.amount_kopeks,
                        received_kopeks=received_kopeks,
                        order_id=payment.order_id,
                    )
                    await severpay_crud.update_severpay_payment_status(
                        db=db,
                        payment=payment,
                        status='amount_mismatch',
                        is_paid=False,
                        severpay_id=severpay_id,
                        callback_payload=callback_payload,
                    )
                    return False

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.severpay_id = severpay_id or payment.severpay_id
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                return await self._finalize_severpay_payment(db, payment, severpay_id=severpay_id, trigger='webhook')

            # Для не-success статусов можно безопасно коммитить
            payment = await severpay_crud.update_severpay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                severpay_id=severpay_id,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('SeverPay webhook: ошибка обработки', error=e)
            return False

    async def _finalize_severpay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        severpay_id: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        Использует FOR UPDATE lock для защиты от race condition.
        """
        payment_module = import_module('app.services.payment_service')
        severpay_crud = import_module('app.database.crud.severpay')

        # Lock FIRST, then read fresh state
        locked = await severpay_crud.get_severpay_payment_by_id_for_update(db, payment.id)
        if not locked:
            logger.error('SeverPay: не удалось заблокировать платёж', payment_id=payment.id)
            return False
        payment = locked

        if payment.transaction_id:
            logger.info(
                'SeverPay платеж уже связан с транзакцией',
                order_id=payment.order_id,
                transaction_id=payment.transaction_id,
                trigger=trigger,
            )
            return True

        # Read fresh metadata AFTER lock to avoid stale data
        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        # --- Guest purchase flow ---
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=str(severpay_id) if severpay_id else payment.order_id,
            provider_name='severpay',
        )
        if guest_result is not None:
            return True

        # Inline field assignments to keep FOR UPDATE lock
        payment.status = 'success'
        payment.is_paid = True
        payment.paid_at = datetime.now(UTC)
        payment.updated_at = datetime.now(UTC)

        balance_already_credited = bool(metadata.get('balance_credited'))

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для SeverPay', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = str(severpay_id) if severpay_id else payment.order_id

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.SEVERPAY,
            )

        display_name = settings.get_severpay_display_name()
        description = f'Пополнение через {display_name}'

        transaction = existing_transaction
        created_transaction = False

        if not transaction:
            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=description,
                payment_method=PaymentMethod.SEVERPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await severpay_crud.link_severpay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('SeverPay платеж уже зачислил баланс ранее', order_id=payment.order_id)
            return True

        # Lock user row to prevent concurrent balance race conditions
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)

        # Emit deferred side-effects after atomic commit
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.SEVERPAY,
            external_id=transaction_external_id,
        )

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
            logger.error('Ошибка обработки реферального пополнения SeverPay', error=error)

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
                logger.error('Ошибка отправки админ уведомления SeverPay', error=error)

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'💳 Способ: {display_name}\n'
                        f'🆔 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю SeverPay', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя',
                user_id=payment.user_id,
                error=error,
                exc_info=True,
            )

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True
        payment.metadata_json = metadata
        await db.commit()

        logger.info(
            'Обработан SeverPay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_severpay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API."""
        try:
            severpay_crud = import_module('app.database.crud.severpay')
            payment = await severpay_crud.get_severpay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('SeverPay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {
                    'payment': payment,
                    'status': 'success',
                    'is_paid': True,
                }

            # Проверяем через API по severpay_id
            if payment.severpay_id:
                try:
                    order_data = await severpay_service.get_payment(payment.severpay_id)
                    severpay_status = order_data.get('status')

                    if severpay_status:
                        status_info = SEVERPAY_STATUS_MAP.get(severpay_status, ('pending', False))
                        internal_status, is_paid = status_info

                        if is_paid:
                            # Проверка суммы
                            api_amount = order_data.get('amount')
                            if api_amount is not None:
                                received_kopeks = round(float(api_amount) * 100)
                                if abs(received_kopeks - payment.amount_kopeks) > 1:
                                    logger.error(
                                        'SeverPay amount mismatch (API check)',
                                        expected_kopeks=payment.amount_kopeks,
                                        received_kopeks=received_kopeks,
                                        order_id=payment.order_id,
                                    )
                                    await severpay_crud.update_severpay_payment_status(
                                        db=db,
                                        payment=payment,
                                        status='amount_mismatch',
                                        is_paid=False,
                                        severpay_id=payment.severpay_id,
                                        callback_payload={
                                            'check_source': 'api',
                                            'severpay_order_data': order_data,
                                        },
                                    )
                                    return {
                                        'payment': payment,
                                        'status': 'amount_mismatch',
                                        'is_paid': False,
                                    }

                            logger.info('SeverPay payment confirmed via API', order_id=payment.order_id)

                            callback_payload = {
                                'check_source': 'api',
                                'severpay_order_data': order_data,
                            }

                            payment = await severpay_crud.update_severpay_payment_status(
                                db=db,
                                payment=payment,
                                status='success',
                                is_paid=True,
                                severpay_id=payment.severpay_id,
                                callback_payload=callback_payload,
                            )

                            await self._finalize_severpay_payment(
                                db,
                                payment,
                                severpay_id=payment.severpay_id,
                                trigger='api_check',
                            )
                        elif internal_status != payment.status:
                            # Обновляем статус если изменился
                            payment = await severpay_crud.update_severpay_payment_status(
                                db=db,
                                payment=payment,
                                status=internal_status,
                            )

                except Exception as e:
                    logger.error('Error checking SeverPay payment status via API', error=e)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        except Exception as e:
            logger.exception('SeverPay: ошибка проверки статуса', error=e)
            return None
