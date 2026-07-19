"""Mixin для интеграции с Overpay (pay.overpay.io)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.overpay_service import overpay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


OVERPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'charged': ('success', True),
    'authorized': ('authorized', False),
    'preflight': ('pending', False),
    'new': ('pending', False),
    'prepared': ('processing', False),
    'prepared_for_holder_metadata_collecting': ('processing', False),
    'processing': ('processing', False),
    'declined': ('declined', False),
    'rejected': ('rejected', False),
    'error': ('error', False),
    'reversed': ('reversed', False),
    'refunded': ('refunded', False),
    'chargeback': ('chargeback', False),
    'representment': ('chargeback', False),
    'credited': ('credited', False),
}

OVERPAY_OPTIONS = ('fps', 'card', 'int')


class OverpayPaymentMixin:
    """Mixin для работы с платежами Overpay."""

    async def create_overpay_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        return_url: str | None = None,
        option: str | None = None,
    ) -> dict[str, Any] | None:
        if not settings.is_overpay_enabled():
            logger.error('Overpay не настроен')
            return None

        if option is not None and option not in OVERPAY_OPTIONS:
            logger.warning('Overpay: неизвестная опция', option=option)
            return None

        if option == 'int' and not settings.is_overpay_int_enabled():
            logger.warning('Overpay: международные платежи отключены')
            return None

        if amount_kopeks < settings.OVERPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Overpay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                OVERPAY_MIN_AMOUNT_KOPEKS=settings.OVERPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.OVERPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Overpay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                OVERPAY_MAX_AMOUNT_KOPEKS=settings.OVERPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            user = None
            tg_id = 'guest'

        order_id = f'op{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100

        amount_eur = None
        if option == 'int':
            amount_eur = round(amount_rubles / settings.OVERPAY_RUB_PER_EUR, 2)
            if amount_eur < settings.OVERPAY_INT_MIN_EUR:
                logger.warning(
                    'Overpay: сумма меньше минимальной в EUR',
                    amount_eur=amount_eur,
                    OVERPAY_INT_MIN_EUR=settings.OVERPAY_INT_MIN_EUR,
                )
                return None
            amount_value = f'{amount_eur:.2f}'
            currency = 'EUR'
        else:
            amount_value = f'{amount_rubles:.2f}'
            currency = settings.OVERPAY_CURRENCY

        project_id = settings.get_overpay_terminal_id(option)

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
            'option': option,
            'currency': currency,
        }
        if option == 'int':
            metadata['amount_eur'] = amount_eur
            metadata['rub_per_eur'] = settings.OVERPAY_RUB_PER_EUR

        if option == 'fps':
            payment_methods = ['fps']
        elif option in ('card', 'int'):
            payment_methods = ['card']
        else:
            payment_methods_str = settings.OVERPAY_PAYMENT_METHODS
            payment_methods = (
                [m.strip() for m in payment_methods_str.split(',') if m.strip()] if payment_methods_str else None
            )

        effective_return_url = return_url or settings.OVERPAY_RETURN_URL
        use_direct_qr = option == 'fps' and settings.is_overpay_sbp_direct_qr_enabled() and bool(effective_return_url)

        try:
            payment_url = None
            overpay_payment_id = None

            if use_direct_qr:
                init_result = None
                try:
                    init_result = await overpay_service.create_payment_s2s(
                        amount=amount_value,
                        currency=currency,
                        project_id=project_id,
                        merchant_transaction_id=order_id,
                        client_email=email or None,
                        return_url=effective_return_url,
                    )
                except Exception as e:
                    logger.warning('Overpay: S2S init не сработал, fallback на форму', order_id=order_id, error=e)

                if init_result is not None:
                    init_id = init_result.get('id')
                    if not init_id:
                        logger.error('Overpay: S2S init без id', order_id=order_id, result=init_result)
                        return None
                    overpay_payment_id = str(init_id)
                    payment_url = await overpay_service.wait_for_redirect_link(overpay_payment_id)
                    if not payment_url:
                        logger.error('Overpay: прямой QR недоступен, платеж не создан', order_id=order_id)
                        return None
                    metadata['direct_qr'] = True

            if not payment_url:
                result = await overpay_service.create_payment(
                    amount=amount_value,
                    currency=currency,
                    lifetime_minutes=settings.OVERPAY_LIFETIME_MINUTES,
                    merchant_transaction_id=order_id,
                    description=description,
                    return_url=effective_return_url,
                    payment_methods=payment_methods,
                    project_id=project_id,
                )
                payment_url = result.get('resultUrl')
                overpay_payment_id = str(result.get('id', '')) if result.get('id') else None

            if not payment_url:
                logger.error('Overpay API не вернул URL платежа', order_id=order_id)
                return None

            logger.info(
                'Overpay API: создан платеж',
                order_id=order_id,
                overpay_payment_id=overpay_payment_id,
                payment_url=payment_url,
                option=option,
            )

            expires_at = datetime.now(UTC) + timedelta(minutes=settings.OVERPAY_LIFETIME_MINUTES)

            overpay_crud = import_module('app.database.crud.overpay')
            local_payment = await overpay_crud.create_overpay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=option,
                overpay_payment_id=overpay_payment_id,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'Overpay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'overpay_payment_id': overpay_payment_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'amount_eur': amount_eur,
                'currency': currency,
                'option': option,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('Overpay: ошибка создания платежа', error=e)
            return None

    async def process_overpay_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает webhook от Overpay.

        mTLS обеспечивает аутентификацию; дополнительно проверяем наличие платежа в БД.

        Args:
            db: Сессия БД
            payload: JSON тело webhook

        Returns:
            True если платеж успешно обработан
        """
        try:
            overpay_payment_id = str(payload.get('id', '')) if payload.get('id') else None
            merchant_transaction_id = payload.get('merchantTransactionId')
            overpay_status = payload.get('status')

            if not overpay_payment_id or not overpay_status:
                logger.warning('Overpay webhook: отсутствуют обязательные поля', payload=payload)
                return False

            # Ищем платеж по order_id (наш merchantTransactionId) или overpay_payment_id
            overpay_crud = import_module('app.database.crud.overpay')
            payment = None
            if merchant_transaction_id:
                payment = await overpay_crud.get_overpay_payment_by_order_id(db, merchant_transaction_id)
            if not payment and overpay_payment_id:
                payment = await overpay_crud.get_overpay_payment_by_overpay_id(db, overpay_payment_id)

            if not payment:
                logger.warning(
                    'Overpay webhook: платеж не найден',
                    merchant_transaction_id=merchant_transaction_id,
                    overpay_payment_id=overpay_payment_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await overpay_crud.get_overpay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Overpay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('Overpay webhook: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = OVERPAY_STATUS_MAP.get(overpay_status, ('pending', False))
            internal_status, is_paid = status_info

            callback_payload = {
                'overpay_payment_id': overpay_payment_id,
                'merchant_transaction_id': merchant_transaction_id,
                'status': overpay_status,
            }

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Defense in depth: the Overpay webhook is authenticated only by mTLS at the
                # reverse proxy, which the application itself cannot verify. Before crediting,
                # cross-check the authoritative status with Overpay over the mTLS API client so a
                # forged "paid" callback for a still-unpaid invoice is rejected. Fail OPEN (trust
                # the webhook) on API errors so a transient Overpay outage never blocks a
                # genuinely-paid callback — same posture as the YooKassa cross-check.
                try:
                    remote = await overpay_service.get_payment(payment.order_id)
                    remote_status = (remote or {}).get('status')
                    if remote_status is None:
                        orders = (remote or {}).get('orders') or []
                        if orders:
                            remote_status = orders[0].get('status')
                    if remote_status is not None:
                        _, remote_paid = OVERPAY_STATUS_MAP.get(remote_status, ('pending', False))
                        if not remote_paid:
                            logger.warning(
                                'Overpay webhook: API не подтвердил оплату — начисление отклонено',
                                order_id=payment.order_id,
                                webhook_status=overpay_status,
                                api_status=remote_status,
                            )
                            return False
                except Exception as cross_check_error:
                    logger.warning(
                        'Overpay webhook: не удалось перепроверить статус через API — доверяем вебхуку',
                        order_id=payment.order_id,
                        error=cross_check_error,
                    )

                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.overpay_payment_id = overpay_payment_id or payment.overpay_payment_id
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_overpay_payment(
                    db, payment, overpay_payment_id=overpay_payment_id, trigger='webhook'
                )

            # Для не-success статусов можно безопасно коммитить
            payment = await overpay_crud.update_overpay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                overpay_payment_id=overpay_payment_id,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('Overpay webhook: ошибка обработки', error=e)
            return False

    async def _finalize_overpay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        overpay_payment_id: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock must be acquired by the caller before invoking this method.
        """
        payment_module = import_module('app.services.payment_service')
        overpay_crud = import_module('app.database.crud.overpay')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'Overpay платеж уже связан с транзакцией',
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
            provider_payment_id=str(overpay_payment_id) if overpay_payment_id else payment.order_id,
            provider_name='overpay',
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
            logger.error('Пользователь не найден для Overpay', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = str(overpay_payment_id) if overpay_payment_id else payment.order_id

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.OVERPAY,
            )

        display_name = settings.get_overpay_display_name()
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
                payment_method=PaymentMethod.OVERPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await overpay_crud.link_overpay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Overpay платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.OVERPAY,
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
            logger.error('Ошибка обработки реферального пополнения Overpay', error=error)

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
                logger.error('Ошибка отправки админ уведомления Overpay', error=error)

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '\u2705 <b>Пополнение успешно!</b>\n\n'
                        f'\U0001f4b0 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'\U0001f4b3 Способ: {display_name}\n'
                        f'\U0001f194 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Overpay', error=error)

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
            'Обработан Overpay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_overpay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API."""
        try:
            overpay_crud = import_module('app.database.crud.overpay')
            payment = await overpay_crud.get_overpay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('Overpay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {
                    'payment': payment,
                    'status': 'success',
                    'is_paid': True,
                }

            # Проверяем через API по overpay_payment_id
            if payment.overpay_payment_id:
                try:
                    order_data = await overpay_service.get_payment(payment.overpay_payment_id)
                    overpay_status = order_data.get('status')
                    if not overpay_status:
                        orders = order_data.get('orders') or []
                        if orders:
                            overpay_status = orders[0].get('status')

                    if overpay_status:
                        status_info = OVERPAY_STATUS_MAP.get(overpay_status, ('pending', False))
                        internal_status, is_paid = status_info

                        if is_paid:
                            # Acquire FOR UPDATE lock before finalization
                            locked = await overpay_crud.get_overpay_payment_by_id_for_update(db, payment.id)
                            if not locked:
                                logger.error('Overpay: не удалось заблокировать платёж', payment_id=payment.id)
                                return None
                            payment = locked

                            if payment.is_paid:
                                logger.info('Overpay платеж уже обработан (api_check)', order_id=payment.order_id)
                                return {
                                    'payment': payment,
                                    'status': 'success',
                                    'is_paid': True,
                                }

                            logger.info('Overpay payment confirmed via API', order_id=payment.order_id)

                            # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
                            payment.status = 'success'
                            payment.is_paid = True
                            payment.paid_at = datetime.now(UTC)
                            payment.callback_payload = {
                                'check_source': 'api',
                                'overpay_order_data': order_data,
                            }
                            payment.updated_at = datetime.now(UTC)
                            await db.flush()

                            await self._finalize_overpay_payment(
                                db,
                                payment,
                                overpay_payment_id=payment.overpay_payment_id,
                                trigger='api_check',
                            )
                        elif internal_status != payment.status:
                            # Обновляем статус если изменился
                            payment = await overpay_crud.update_overpay_payment_status(
                                db=db,
                                payment=payment,
                                status=internal_status,
                            )

                except Exception as e:
                    logger.error('Error checking Overpay payment status via API', error=e)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        except Exception as e:
            logger.exception('Overpay: ошибка проверки статуса', error=e)
            return None
