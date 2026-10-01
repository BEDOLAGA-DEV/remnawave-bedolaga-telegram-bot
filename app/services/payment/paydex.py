"""Mixin для интеграции с Paydex (merchant API v1, paydex.pro)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.paydex_service import paydex_service, rubles_to_kopeks
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Статус счёта Paydex -> (внутренний статус, зачислять ли баланс)
PAYDEX_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'created': ('pending', False),
    'pending': ('pending', False),
    'paid': ('success', True),
    'failed': ('declined', False),
    'expired': ('expired', False),
    'refunded': ('refunded', False),
    'partially_refunded': ('refunded', False),
}

# Sub-метод бота -> значение поля `method` при создании счёта в Paydex
PAYDEX_METHOD_MAP: dict[str, str] = {
    'sbp': 'sbp',
    'card': 'card',
    'crypto': 'crypto',
}


def resolve_paydex_method(payment_method_type: str | None) -> str | None:
    """Определяет значение `method` для API Paydex.

    Явный sub-метод выигрывает всегда. Если sub-методы не настроены (или кабинет
    не прислал payment_option), возвращаем ``None`` — Paydex покажет покупателю
    выбор способа на своей странице оплаты. Это отличие от провайдеров, где метод
    обязателен: подставлять карту «по умолчанию» нельзя, у проекта она может быть
    выключена, и счёт просто не оплатится.
    """
    explicit = PAYDEX_METHOD_MAP.get((payment_method_type or '').lower())
    if explicit:
        return explicit
    enabled = [
        code
        for code, is_on in (
            ('sbp', settings.is_paydex_sbp_enabled()),
            ('card', settings.is_paydex_card_enabled()),
            ('crypto', settings.is_paydex_crypto_enabled()),
        )
        if is_on
    ]
    # Включён ровно один способ — фиксируем его, чтобы не гонять покупателя через лишний экран.
    return enabled[0] if len(enabled) == 1 else None


class PaydexPaymentMixin:
    """Mixin для работы с платежами Paydex."""

    async def create_paydex_payment(
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
        fail_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Создаёт счёт в Paydex.

        ``payment_method_type`` — sub-метод бота ('sbp' / 'card' / 'crypto'); если
        не задан, способ выбирает покупатель на странице оплаты. ``return_url``
        уходит в successUrl (и в failUrl, если ``fail_url`` не передан). Вебхук
        приходит на URL, настроенный в проекте Paydex.
        """
        if not settings.is_paydex_enabled():
            logger.error('Paydex не настроен')
            return None

        if amount_kopeks < settings.PAYDEX_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Paydex: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                PAYDEX_MIN_AMOUNT_KOPEKS=settings.PAYDEX_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.PAYDEX_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Paydex: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                PAYDEX_MAX_AMOUNT_KOPEKS=settings.PAYDEX_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            tg_id = 'guest'

        order_id = f'pdx{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.PAYDEX_CURRENCY
        paydex_method = resolve_paydex_method(payment_method_type)
        customer_id = str(tg_id) if tg_id != 'guest' else f'guest-{order_id[-6:]}'

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
            'payment_method_type': payment_method_type,
        }

        try:
            api_result = await paydex_service.create_invoice(
                amount_kopeks=amount_kopeks,
                order_id=order_id,
                method=paydex_method,
                description=description[:500] if description else None,
                success_url=return_url,
                fail_url=fail_url or return_url,
                customer_id=customer_id,
                email=email,
                expire_minutes=settings.PAYDEX_PAYMENT_LIFETIME_MINUTES,
            )

            paydex_invoice_id = api_result.get('id')
            payment_url = api_result.get('url')
            # payableAmount — сколько заплатит покупатель: если комиссию в проекте
            # платит он, это больше запрошенной суммы.
            payable_amount = api_result.get('payableAmount')
            charged_kopeks = rubles_to_kopeks(payable_amount) if payable_amount is not None else None

            # Срок берём из ответа, а не считаем сами: у проекта может быть свой TTL.
            expires_at = None
            expires_raw = api_result.get('expiresAt')
            if expires_raw:
                try:
                    expires_at = datetime.fromisoformat(str(expires_raw).replace('Z', '+00:00'))
                except ValueError:
                    expires_at = None
            if expires_at is None:
                expires_at = datetime.now(UTC) + timedelta(
                    minutes=settings.PAYDEX_PAYMENT_LIFETIME_MINUTES
                )

            if api_result.get('isTest'):
                # Тестовый ключ (sk_test_) создаёт счёт-пустышку: такую «оплату» нельзя
                # зачислять на реальный баланс. Пишем явно, чтобы это не выяснилось
                # после первого бесплатного пополнения.
                logger.warning(
                    'Paydex: создан ТЕСТОВЫЙ счёт — баланс по нему начислен не будет',
                    order_id=order_id,
                )

            paydex_crud = import_module('app.database.crud.paydex')
            local_payment = await paydex_crud.create_paydex_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=paydex_method,
                paydex_invoice_id=str(paydex_invoice_id) if paydex_invoice_id else None,
                charged_amount_kopeks=charged_kopeks,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'Paydex: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                payment_method=paydex_method or 'any',
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'payment_id': str(paydex_invoice_id) if paydex_invoice_id else None,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('Paydex: ошибка создания платежа', error=e)
            return None

    async def process_paydex_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Обрабатывает вебхук Paydex (подпись уже проверена в webserver).

        Тело: ``{event, isTest, data: {invoice: {...}}}``; события —
        ``invoice.paid`` / ``invoice.failed`` / ``invoice.expired`` /
        ``invoice.refunded``. Суммы в счёте — строки рублей.
        """
        try:
            event = (payload.get('event') or '').strip()
            data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
            invoice = data.get('invoice') if isinstance(data.get('invoice'), dict) else {}

            our_order_id = invoice.get('orderId')
            paydex_invoice_id = invoice.get('id')
            paydex_status = (invoice.get('status') or '').strip().lower()
            is_test = bool(payload.get('isTest') or invoice.get('isTest'))

            if not our_order_id or not paydex_status:
                logger.warning('Paydex callback: отсутствуют обязательные поля', payload=payload)
                return False

            paydex_crud = import_module('app.database.crud.paydex')
            payment = await paydex_crud.get_paydex_payment_by_order_id(db, our_order_id)
            if not payment:
                logger.warning('Paydex callback: платеж не найден', order_id=our_order_id)
                return False

            locked = await paydex_crud.get_paydex_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Paydex: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            if payment.is_paid:
                logger.info('Paydex callback: платеж уже обработан', order_id=payment.order_id)
                return True

            # Терминальные неуспешные статусы стики — провайдер не должен «починить»
            # отклонённый или просроченный платёж повторным вебхуком.
            if payment.status in {'amount_mismatch', 'declined', 'expired', 'refunded', 'error'}:
                logger.warning(
                    'Paydex callback: платёж в терминальном неуспешном статусе, игнорируется',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=paydex_status,
                )
                return True

            internal_status, is_paid = PAYDEX_STATUS_MAP.get(paydex_status, ('pending', False))

            callback_payload = {
                'event': event,
                'paydex_invoice_id': paydex_invoice_id,
                'status': paydex_status,
                'amount': invoice.get('amount'),
                'payable_amount': invoice.get('payableAmount'),
                'net_amount': invoice.get('netAmount'),
                'fee_amount': invoice.get('feeAmount'),
                'method': invoice.get('method'),
                'paid_at': invoice.get('paidAt'),
                'is_test': is_test,
            }

            if is_paid and is_test:
                # Тестовый счёт можно «оплатить» эмулятором без денег — реальный баланс
                # по нему начислять нельзя. Фиксируем и выходим успешно, чтобы Paydex
                # не повторял доставку.
                logger.error(
                    'Paydex callback: ТЕСТОВЫЙ платёж, баланс не начисляем',
                    order_id=payment.order_id,
                )
                await paydex_crud.update_paydex_payment_status(
                    db=db,
                    payment=payment,
                    status='error',
                    is_paid=False,
                    callback_payload=callback_payload,
                )
                return True

            # Сверяем сумму ДО зачисления: `amount` счёта — это то, что мы запрашивали
            # (нетто для бота). Зачисляем только при подтверждённой сумме: «не смогли
            # проверить» не равно «всё сошлось».
            if is_paid:
                received_amount = invoice.get('amount')
                if received_amount is None:
                    # Поле есть в любом ответе API. Оставляем pending (статус не терминальный)
                    # и отвечаем не-2xx: Paydex повторит вебхук, плюс сработает фоновая сверка.
                    logger.error(
                        'Paydex callback: оплата без поля amount, зачисление отменено',
                        order_id=payment.order_id,
                    )
                    return False

                try:
                    received_kopeks = rubles_to_kopeks(received_amount)
                except Exception:  # noqa: BLE001 — любой мусор в сумме = не зачисляем
                    received_kopeks = None

                if received_kopeks is None or received_kopeks != payment.amount_kopeks:
                    logger.error(
                        'Paydex amount mismatch',
                        expected_kopeks=payment.amount_kopeks,
                        received_amount=received_amount,
                        received_kopeks=received_kopeks,
                        order_id=payment.order_id,
                    )
                    await paydex_crud.update_paydex_payment_status(
                        db=db,
                        payment=payment,
                        status='amount_mismatch',
                        is_paid=False,
                        callback_payload=callback_payload,
                    )
                    return False

            if is_paid:
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.paydex_invoice_id = (
                    str(paydex_invoice_id) if paydex_invoice_id else payment.paydex_invoice_id
                )
                payable_amount = invoice.get('payableAmount')
                if payable_amount is not None:
                    try:
                        payment.charged_amount_kopeks = rubles_to_kopeks(payable_amount)
                    except Exception:  # noqa: BLE001 — справочное поле, не критично
                        pass
                if invoice.get('method'):
                    payment.payment_method = str(invoice['method'])
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_paydex_payment(db, payment, trigger='webhook')

            payment = await paydex_crud.update_paydex_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                callback_payload=callback_payload,
            )
            return True

        except Exception as e:
            logger.exception('Paydex callback: ошибка обработки', error=e)
            return False

    async def _finalize_paydex_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock уже взят вызывающим.
        """
        payment_module = import_module('app.services.payment_service')
        paydex_crud = import_module('app.database.crud.paydex')

        if payment.transaction_id:
            logger.info(
                'Paydex платеж уже связан с транзакцией',
                order_id=payment.order_id,
                transaction_id=payment.transaction_id,
                trigger=trigger,
            )
            return True

        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.order_id,
            provider_name='paydex',
        )
        if guest_result is not None:
            return True

        if not payment.is_paid:
            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.updated_at = datetime.now(UTC)

        balance_already_credited = bool(metadata.get('balance_credited'))

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для Paydex', user_id=payment.user_id)
            return False

        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = payment.order_id

        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.PAYDEX,
            )

        display_name = settings.get_paydex_display_name()
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
                payment_method=PaymentMethod.PAYDEX,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await paydex_crud.link_paydex_payment_to_transaction(
            db, payment=payment, transaction_id=transaction.id
        )

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Paydex платеж уже зачислил баланс ранее', order_id=payment.order_id)
            return True

        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)

        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.PAYDEX,
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
            logger.error('Ошибка обработки реферального пополнения Paydex', error=error)

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
                logger.error('Ошибка отправки админ уведомления Paydex', error=error)

        if getattr(self, 'bot', None) and user.telegram_id and settings.is_notifications_enabled():
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
                logger.error('Ошибка отправки уведомления пользователю Paydex', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(
                user, payment.amount_kopeks, db, getattr(self, 'bot', None)
            )
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
            'Обработан Paydex платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_paydex_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус счёта через API Paydex и синхронизирует БД.

        Используется для ручной проверки из админки и фоновой сверки — если вебхук
        потерялся, оплаченный счёт всё равно будет зачислен.
        """
        try:
            paydex_crud = import_module('app.database.crud.paydex')
            payment = await paydex_crud.get_paydex_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('Paydex payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {'payment': payment, 'status': 'success', 'is_paid': True}

            if payment.status in {'amount_mismatch', 'declined', 'expired', 'refunded', 'error'}:
                return {'payment': payment, 'status': payment.status, 'is_paid': False}

            try:
                status_data = await paydex_service.check_invoice(
                    invoice_id=payment.paydex_invoice_id,
                    order_id=None if payment.paydex_invoice_id else payment.order_id,
                )
                paydex_status = (status_data.get('status') or '').strip().lower()

                if paydex_status:
                    internal_status, is_paid = PAYDEX_STATUS_MAP.get(paydex_status, ('pending', False))

                    if is_paid and status_data.get('isTest'):
                        logger.error(
                            'Paydex API check: ТЕСТОВЫЙ платёж, баланс не начисляем',
                            order_id=payment.order_id,
                        )
                        await paydex_crud.update_paydex_payment_status(
                            db=db, payment=payment, status='error', is_paid=False
                        )
                        return {'payment': payment, 'status': 'error', 'is_paid': False}

                    if is_paid:
                        api_amount = status_data.get('amount')
                        if api_amount is None:
                            logger.error(
                                'Paydex API check: оплата без поля amount, зачисление отменено',
                                order_id=payment.order_id,
                            )
                            return {
                                'payment': payment,
                                'status': payment.status or 'pending',
                                'is_paid': False,
                            }

                        try:
                            received_kopeks = rubles_to_kopeks(api_amount)
                        except Exception:  # noqa: BLE001
                            received_kopeks = None

                        if received_kopeks is None or received_kopeks != payment.amount_kopeks:
                            logger.error(
                                'Paydex amount mismatch (API check)',
                                expected_kopeks=payment.amount_kopeks,
                                received_amount=api_amount,
                                received_kopeks=received_kopeks,
                                order_id=payment.order_id,
                            )
                            await paydex_crud.update_paydex_payment_status(
                                db=db,
                                payment=payment,
                                status='amount_mismatch',
                                is_paid=False,
                            )
                            return {'payment': payment, 'status': 'amount_mismatch', 'is_paid': False}

                        locked = await paydex_crud.get_paydex_payment_by_id_for_update(db, payment.id)
                        if not locked:
                            logger.error(
                                'Paydex: не удалось заблокировать платёж при сверке',
                                payment_id=payment.id,
                            )
                            return {'payment': payment, 'status': payment.status, 'is_paid': False}
                        payment = locked
                        if payment.is_paid:
                            return {'payment': payment, 'status': 'success', 'is_paid': True}

                        payment.status = internal_status
                        payment.is_paid = True
                        payment.paid_at = datetime.now(UTC)
                        payment.updated_at = datetime.now(UTC)
                        await db.flush()
                        await self._finalize_paydex_payment(db, payment, trigger='api_check')
                        return {'payment': payment, 'status': 'success', 'is_paid': True}

                    if internal_status != payment.status:
                        payment = await paydex_crud.update_paydex_payment_status(
                            db=db, payment=payment, status=internal_status, is_paid=False
                        )

            except Exception as error:
                logger.error('Paydex: ошибка проверки статуса в API', order_id=order_id, error=error)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': bool(payment.is_paid),
            }

        except Exception as e:
            logger.exception('Paydex: ошибка проверки платежа', error=e)
            return None
