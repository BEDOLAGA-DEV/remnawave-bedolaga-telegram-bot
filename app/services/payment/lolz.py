"""Mixin для интеграции с LOLZ (lzt-market.com / prod-api.lzt.market)."""

from __future__ import annotations

import json as _json
import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.lolz_service import lolz_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов LOLZ -> internal
LOLZ_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'paid': ('success', True),
    'not_paid': ('pending', False),
}


class LolzPaymentMixin:
    """Mixin для работы с платежами LOLZ."""

    async def create_lolz_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        payment_method_type: str | None = None,
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Создает платеж LOLZ.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_lolz_enabled():
            logger.error('LOLZ не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.LOLZ_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'LOLZ: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                LOLZ_MIN_AMOUNT_KOPEKS=settings.LOLZ_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.LOLZ_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'LOLZ: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                LOLZ_MAX_AMOUNT_KOPEKS=settings.LOLZ_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id и required_telegram_id
        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            user = None
            tg_id = 'guest'

        # Генерируем уникальный order_id (он же payment_id для LOLZ) с telegram_id для удобного поиска
        order_id = f'lolz{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.LOLZ_CURRENCY

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            # Формируем webhook URL
            webhook_url = None
            if settings.WEBHOOK_URL:
                webhook_url = f'{settings.WEBHOOK_URL.rstrip("/")}{settings.LOLZ_WEBHOOK_PATH}'

            # Lifetime в секундах
            lifetime_seconds = settings.LOLZ_PAYMENT_LIFETIME_MINUTES * 60

            # required_telegram_id если включено и есть пользователь
            required_telegram_id: int | None = None
            if settings.LOLZ_REQUIRE_TELEGRAM_ID and user is not None and user.telegram_id:
                required_telegram_id = int(user.telegram_id)

            # additional_data как JSON-строка
            additional_data = _json.dumps(metadata, ensure_ascii=False)

            url_success = return_url or settings.LOLZ_RETURN_URL or ''

            # Используем API для создания инвойса
            invoice = await lolz_service.create_invoice(
                amount=amount_rubles,
                payment_id=order_id,
                comment=description,
                url_success=url_success,
                url_callback=webhook_url,
                currency=currency,
                lifetime=lifetime_seconds,
                additional_data=additional_data,
                required_telegram_id=required_telegram_id,
            )

            payment_url = invoice.get('url')
            lolz_invoice_id = invoice.get('invoice_id')
            lolz_payment_id = invoice.get('payment_id') or order_id

            if not payment_url:
                logger.error('LOLZ API не вернул URL платежа', invoice=invoice)
                return None

            logger.info(
                'LOLZ API: создан инвойс',
                order_id=order_id,
                lolz_invoice_id=lolz_invoice_id,
                payment_url=payment_url,
            )

            # Срок действия из expires_at ответа (unix epoch) или lifetime минут по умолчанию
            expires_at_value = invoice.get('expires_at')
            expires_at: datetime | None = None
            if expires_at_value:
                try:
                    expires_at = datetime.fromtimestamp(int(expires_at_value), tz=UTC)
                except (ValueError, TypeError, OSError):
                    expires_at = None
            if expires_at is None:
                expires_at = datetime.now(UTC) + timedelta(minutes=settings.LOLZ_PAYMENT_LIFETIME_MINUTES)

            # Сохраняем в БД
            lolz_crud = import_module('app.database.crud.lolz')
            local_payment = await lolz_crud.create_lolz_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency.upper(),
                description=description,
                payment_url=payment_url,
                payment_method=payment_method_type,
                lolz_invoice_id=int(lolz_invoice_id) if lolz_invoice_id is not None else None,
                lolz_payment_id=str(lolz_payment_id) if lolz_payment_id is not None else None,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'LOLZ: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'lolz_invoice_id': lolz_invoice_id,
                'lolz_payment_id': lolz_payment_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('LOLZ: ошибка создания платежа', error=e)
            return None

    async def process_lolz_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает webhook от LOLZ.

        Подпись (`x-secret-key`) проверяется в webserver/payments.py до вызова этого метода.

        Args:
            db: Сессия БД
            payload: JSON тело webhook (signature проверена в webserver)

        Returns:
            True если платеж успешно обработан
        """
        try:
            lolz_invoice_id_raw = payload.get('invoice_id')
            lolz_payment_id = payload.get('payment_id')
            lolz_status = payload.get('status')

            try:
                lolz_invoice_id = int(lolz_invoice_id_raw) if lolz_invoice_id_raw is not None else None
            except (ValueError, TypeError):
                lolz_invoice_id = None

            if not lolz_payment_id and lolz_invoice_id is None:
                logger.warning('LOLZ webhook: отсутствуют идентификаторы', payload=payload)
                return False

            if not lolz_status:
                logger.warning('LOLZ webhook: отсутствует status', payload=payload)
                return False

            # Определяем is_paid по статусу
            is_confirmed = lolz_status == 'paid'

            # Ищем платеж: сначала по нашему payment_id (LOLZ возвращает наш payment_id),
            # затем — fallback по lolz_invoice_id
            lolz_crud = import_module('app.database.crud.lolz')
            payment = None
            if lolz_payment_id:
                payment = await lolz_crud.get_lolz_payment_by_payment_id(db, str(lolz_payment_id))
            if not payment and lolz_invoice_id is not None:
                payment = await lolz_crud.get_lolz_payment_by_invoice_id(db, lolz_invoice_id)
            # Также пытаемся найти по order_id, если payment_id совпадает с нашим order_id
            if not payment and lolz_payment_id:
                payment = await lolz_crud.get_lolz_payment_by_order_id(db, str(lolz_payment_id))

            if not payment:
                logger.warning(
                    'LOLZ webhook: платеж не найден',
                    lolz_payment_id=lolz_payment_id,
                    lolz_invoice_id=lolz_invoice_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await lolz_crud.get_lolz_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('LOLZ: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('LOLZ webhook: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = LOLZ_STATUS_MAP.get(lolz_status, ('pending', False))
            internal_status, is_paid = status_info

            # Если статус paid, принудительно считаем оплаченным
            if is_confirmed:
                is_paid = True
                internal_status = 'success'

            callback_payload = {
                'lolz_invoice_id': lolz_invoice_id,
                'lolz_payment_id': lolz_payment_id,
                'status': lolz_status,
                'amount': payload.get('amount'),
                'paid_date': payload.get('paid_date'),
                'payer_user_id': payload.get('payer_user_id'),
                'is_test': payload.get('is_test'),
            }

            # Проверка суммы ДО обновления статуса
            # LOLZ отправляет amount в той же единице, что и при create (рубли)
            if is_paid:
                amount_value = payload.get('amount')
                if amount_value is not None:
                    received_kopeks = round(float(amount_value) * 100)
                    if abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'LOLZ amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await lolz_crud.update_lolz_payment_status(
                            db=db,
                            payment=payment,
                            status='amount_mismatch',
                            is_paid=False,
                            lolz_invoice_id=lolz_invoice_id,
                            lolz_payment_id=str(lolz_payment_id) if lolz_payment_id else None,
                            callback_payload=callback_payload,
                        )
                        return False

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                if lolz_invoice_id is not None:
                    payment.lolz_invoice_id = lolz_invoice_id
                if lolz_payment_id:
                    payment.lolz_payment_id = str(lolz_payment_id)
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_lolz_payment(
                    db, payment, lolz_invoice_id=lolz_invoice_id, trigger='webhook'
                )

            # Для не-success статусов можно безопасно коммитить
            payment = await lolz_crud.update_lolz_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                lolz_invoice_id=lolz_invoice_id,
                lolz_payment_id=str(lolz_payment_id) if lolz_payment_id else None,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('LOLZ webhook: ошибка обработки', error=e)
            return False

    async def _finalize_lolz_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        lolz_invoice_id: int | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock must be acquired by the caller before invoking this method.
        """
        payment_module = import_module('app.services.payment_service')
        lolz_crud = import_module('app.database.crud.lolz')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'LOLZ платеж уже связан с транзакцией',
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
            provider_payment_id=str(lolz_invoice_id) if lolz_invoice_id is not None else payment.order_id,
            provider_name='lolz',
        )
        if guest_result is not None:
            return True

        # Ensure paid fields are set (idempotent — caller may have already set them)
        if not payment.is_paid:
            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.updated_at = datetime.now(UTC)

        balance_already_credited = bool(metadata.get('balance_credited'))

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для LOLZ', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = (
            str(lolz_invoice_id) if lolz_invoice_id is not None else payment.order_id
        )

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.LOLZ,
            )

        display_name = settings.get_lolz_display_name()
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
                payment_method=PaymentMethod.LOLZ,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await lolz_crud.link_lolz_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('LOLZ платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.LOLZ,
            external_id=transaction_external_id,
        )

        topup_status = '\U0001f195 Первое пополнение' if was_first_topup else '\U0001f504 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения LOLZ', error=error)

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
                logger.error('Ошибка отправки админ уведомления LOLZ', error=error)

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'\U0001f4b0 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'\U0001f4b3 Способ: {display_name}\n'
                        f'\U0001f194 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю LOLZ', error=error)

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
            'Обработан LOLZ платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_lolz_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API."""
        try:
            lolz_crud = import_module('app.database.crud.lolz')
            payment = await lolz_crud.get_lolz_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('LOLZ payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {
                    'payment': payment,
                    'status': 'success',
                    'is_paid': True,
                }

            if payment.lolz_invoice_id is None:
                # Без invoice_id невозможно опросить API
                return {
                    'payment': payment,
                    'status': payment.status or 'pending',
                    'is_paid': payment.is_paid,
                }

            try:
                invoice_data = await lolz_service.get_invoice_status(
                    invoice_id=int(payment.lolz_invoice_id),
                )
                lolz_status = invoice_data.get('status')

                if lolz_status:
                    status_info = LOLZ_STATUS_MAP.get(lolz_status, ('pending', False))
                    internal_status, is_paid = status_info

                    if is_paid:
                        # Проверка суммы — LOLZ возвращает amount в рублях
                        api_amount = invoice_data.get('amount')
                        if api_amount is not None:
                            received_kopeks = round(float(api_amount) * 100)
                            if abs(received_kopeks - payment.amount_kopeks) > 1:
                                logger.error(
                                    'LOLZ amount mismatch (API check)',
                                    expected_kopeks=payment.amount_kopeks,
                                    received_kopeks=received_kopeks,
                                    order_id=payment.order_id,
                                )
                                await lolz_crud.update_lolz_payment_status(
                                    db=db,
                                    payment=payment,
                                    status='amount_mismatch',
                                    is_paid=False,
                                    lolz_invoice_id=payment.lolz_invoice_id,
                                    callback_payload={
                                        'check_source': 'api',
                                        'lolz_invoice_data': invoice_data,
                                    },
                                )
                                return {
                                    'payment': payment,
                                    'status': 'amount_mismatch',
                                    'is_paid': False,
                                }

                        # Acquire FOR UPDATE lock before finalization
                        locked = await lolz_crud.get_lolz_payment_by_id_for_update(db, payment.id)
                        if not locked:
                            logger.error('LOLZ: не удалось заблокировать платёж', payment_id=payment.id)
                            return None
                        payment = locked

                        if payment.is_paid:
                            logger.info('LOLZ платеж уже обработан (api_check)', order_id=payment.order_id)
                            return {
                                'payment': payment,
                                'status': 'success',
                                'is_paid': True,
                            }

                        logger.info('LOLZ payment confirmed via API', order_id=payment.order_id)

                        # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
                        payment.status = 'success'
                        payment.is_paid = True
                        payment.paid_at = datetime.now(UTC)
                        payment.callback_payload = {
                            'check_source': 'api',
                            'lolz_invoice_data': invoice_data,
                        }
                        payment.updated_at = datetime.now(UTC)
                        await db.flush()

                        await self._finalize_lolz_payment(
                            db,
                            payment,
                            lolz_invoice_id=payment.lolz_invoice_id,
                            trigger='api_check',
                        )
                    elif internal_status != payment.status:
                        # Обновляем статус если изменился
                        payment = await lolz_crud.update_lolz_payment_status(
                            db=db,
                            payment=payment,
                            status=internal_status,
                        )

            except Exception as e:
                logger.error('Error checking LOLZ payment status via API', error=e)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        except Exception as e:
            logger.exception('LOLZ: ошибка проверки статуса', error=e)
            return None
