"""Mixin для интеграции с cisPay (H2H merchant API, api.cispay.app)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.cispay_service import cispay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов cisPay -> internal
CISPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'PENDING': ('pending', False),
    'PAID': ('success', True),
    'FAILED': ('declined', False),
    'EXPIRED': ('expired', False),
    'REFUNDED': ('refunded', False),
}

# Sub-метод бота -> payment_method cisPay
CISPAY_METHOD_MAP: dict[str, str] = {
    'sbp': 'SBP',
    'card': 'CARD',
}


def resolve_cispay_method(payment_method_type: str | None) -> str:
    """Определяет payment_method для API cisPay.

    Явный sub-метод выигрывает всегда. Для генерик-метода (сабметоды не настроены
    или кабинет не прислал payment_option) берём единственный включённый сабметод:
    иначе SBP-only магазин получал бы CARD, который провайдер отклоняет.
    """
    explicit = CISPAY_METHOD_MAP.get((payment_method_type or '').lower())
    if explicit:
        return explicit
    if settings.is_cispay_sbp_enabled() and not settings.is_cispay_card_enabled():
        return 'SBP'
    return 'CARD'


class CisPayPaymentMixin:
    """Mixin для работы с платежами cisPay."""

    async def create_cispay_payment(
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
        """Создаёт платёж cisPay.

        ``payment_method_type`` — sub-метод бота ('card' / 'sbp'), по умолчанию CARD.
        ``return_url`` пробрасывается в redirect_success_url (и в redirect_fail_url,
        если ``fail_url`` не задан) — cisPay вернёт покупателя туда после оплаты
        на хостинговой странице. Вебхук приходит на URL, настроенный в личном
        кабинете магазина.
        """
        if not settings.is_cispay_enabled():
            logger.error('cisPay не настроен')
            return None

        if amount_kopeks < settings.CISPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'cisPay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                CISPAY_MIN_AMOUNT_KOPEKS=settings.CISPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.CISPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'cisPay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                CISPAY_MAX_AMOUNT_KOPEKS=settings.CISPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            tg_id = 'guest'

        order_id = f'cis{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.CISPAY_CURRENCY
        cispay_method = resolve_cispay_method(payment_method_type)
        # customer_id обязателен для SBP — провайдер отклоняет платежи без него
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
            api_result = await cispay_service.create_payment(
                amount_kopeks=amount_kopeks,
                order_id=order_id,
                payment_method=cispay_method,
                customer_id=customer_id,
                description=description[:512] if description else None,
                redirect_success_url=return_url,
                redirect_fail_url=fail_url or return_url,
            )

            cispay_payment_id = api_result.get('id')
            payment_url = api_result.get('payment_url')
            charged_amount = api_result.get('charged_amount')

            # Счёт cisPay живёт 30 минут, после чего EXPIRED на стороне провайдера
            lifetime = settings.CISPAY_PAYMENT_LIFETIME_MINUTES
            expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            cispay_crud = import_module('app.database.crud.cispay')
            local_payment = await cispay_crud.create_cispay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=cispay_method,
                cispay_payment_id=str(cispay_payment_id) if cispay_payment_id else None,
                charged_amount_kopeks=int(charged_amount) if charged_amount is not None else None,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'cisPay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                payment_method=cispay_method,
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'payment_id': str(cispay_payment_id) if cispay_payment_id else None,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('cisPay: ошибка создания платежа', error=e)
            return None

    async def process_cispay_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Обрабатывает вебхук от cisPay (подпись уже проверена в webserver).

        Тело: id, store_id, order_id, payment_method, status, amount (копейки),
        currency, charged_amount, merchant_revenue, paid_at, timestamp.
        """
        try:
            our_order_id = payload.get('order_id')
            cispay_payment_id = payload.get('id')
            cispay_status = (payload.get('status') or '').strip().upper()

            if not our_order_id or not cispay_status:
                logger.warning('cisPay callback: отсутствуют обязательные поля', payload=payload)
                return False

            cispay_crud = import_module('app.database.crud.cispay')
            payment = await cispay_crud.get_cispay_payment_by_order_id(db, our_order_id)
            if not payment:
                logger.warning('cisPay callback: платеж не найден', order_id=our_order_id)
                return False

            locked = await cispay_crud.get_cispay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('cisPay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            if payment.is_paid:
                logger.info('cisPay callback: платеж уже обработан', order_id=payment.order_id)
                return True

            # Терминальные неуспешные статусы стики — провайдер не должен иметь возможность
            # «починить» отклонённый/просроченный платёж повторным вебхуком.
            if payment.status in {'amount_mismatch', 'declined', 'expired', 'refunded', 'error'}:
                logger.warning(
                    'cisPay callback: платёж в терминальном неуспешном статусе, игнорируется',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=cispay_status,
                )
                return True

            internal_status, is_paid = CISPAY_STATUS_MAP.get(cispay_status, ('pending', False))

            callback_payload = {
                'cispay_payment_id': cispay_payment_id,
                'status': cispay_status,
                'amount': payload.get('amount'),
                'charged_amount': payload.get('charged_amount'),
                'merchant_revenue': payload.get('merchant_revenue'),
                'payment_method': payload.get('payment_method'),
                'paid_at': payload.get('paid_at'),
                'timestamp': payload.get('timestamp'),
            }

            # Сверяем сумму ДО обновления статуса: amount в вебхуке — копейки (нетто, как в запросе).
            # Зачисляем только при подтверждённой сумме — «не смогли проверить» не равно «всё сошлось».
            if is_paid:
                received_amount = payload.get('amount')
                if received_amount is None:
                    # Поле обязательно по спеке. Оставляем платёж в pending (статус не терминальный)
                    # и отвечаем не-2xx: cisPay повторит вебхук, а ручная/фоновая сверка через
                    # GET /payments/status тоже сможет его закрыть.
                    logger.error(
                        'cisPay callback: PAID без поля amount, зачисление отменено',
                        order_id=payment.order_id,
                    )
                    return False

                try:
                    received_kopeks = int(received_amount)
                except (TypeError, ValueError):
                    received_kopeks = None

                if received_kopeks is None or received_kopeks != payment.amount_kopeks:
                    logger.error(
                        'cisPay amount mismatch',
                        expected_kopeks=payment.amount_kopeks,
                        received_amount=received_amount,
                        received_kopeks=received_kopeks,
                        order_id=payment.order_id,
                    )
                    await cispay_crud.update_cispay_payment_status(
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
                payment.cispay_payment_id = str(cispay_payment_id) if cispay_payment_id else payment.cispay_payment_id
                charged_amount = payload.get('charged_amount')
                if charged_amount is not None:
                    try:
                        payment.charged_amount_kopeks = int(charged_amount)
                    except (TypeError, ValueError):
                        pass
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_cispay_payment(db, payment, trigger='webhook')

            payment = await cispay_crud.update_cispay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                callback_payload=callback_payload,
            )
            return True

        except Exception as e:
            logger.exception('cisPay callback: ошибка обработки', error=e)
            return False

    async def _finalize_cispay_payment(
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
        cispay_crud = import_module('app.database.crud.cispay')

        if payment.transaction_id:
            logger.info(
                'cisPay платеж уже связан с транзакцией',
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
            provider_name='cispay',
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
            logger.error('Пользователь не найден для cisPay', user_id=payment.user_id)
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
                PaymentMethod.CISPAY,
            )

        display_name = settings.get_cispay_display_name()
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
                payment_method=PaymentMethod.CISPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await cispay_crud.link_cispay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('cisPay платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.CISPAY,
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
            logger.error('Ошибка обработки реферального пополнения cisPay', error=error)

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
                logger.error('Ошибка отправки админ уведомления cisPay', error=error)

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
                logger.error('Ошибка отправки уведомления пользователю cisPay', error=error)

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
            'Обработан cisPay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_cispay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API cisPay и синхронизирует БД.

        Используется для ручной проверки из админки и фоновой сверки —
        если вебхук потерялся, оплаченный платёж всё равно будет зачислен.
        """
        try:
            cispay_crud = import_module('app.database.crud.cispay')
            payment = await cispay_crud.get_cispay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('cisPay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {
                    'payment': payment,
                    'status': 'success',
                    'is_paid': True,
                }

            if payment.status in {'amount_mismatch', 'declined', 'expired', 'refunded', 'error'}:
                return {
                    'payment': payment,
                    'status': payment.status,
                    'is_paid': False,
                }

            try:
                status_data = await cispay_service.check_payment(
                    payment_id=payment.cispay_payment_id,
                    order_id=None if payment.cispay_payment_id else payment.order_id,
                )
                cispay_status = (status_data.get('status') or '').strip().upper()

                if cispay_status:
                    internal_status, is_paid = CISPAY_STATUS_MAP.get(cispay_status, ('pending', False))

                    if is_paid:
                        # Сверяем сумму — API возвращает amount в копейках (нетто).
                        # Как и в вебхуке, без подтверждённой суммы не зачисляем.
                        api_amount = status_data.get('amount')
                        if api_amount is None:
                            # amount обязателен в PaymentStatusResponse — его отсутствие
                            # означает сломанный ответ. Оставляем pending до следующей сверки.
                            logger.error(
                                'cisPay API check: PAID без поля amount, зачисление отменено',
                                order_id=payment.order_id,
                            )
                            return {
                                'payment': payment,
                                'status': payment.status or 'pending',
                                'is_paid': False,
                            }

                        try:
                            received_kopeks = int(api_amount)
                        except (TypeError, ValueError):
                            received_kopeks = None

                        if received_kopeks is None or received_kopeks != payment.amount_kopeks:
                            logger.error(
                                'cisPay amount mismatch (API check)',
                                expected_kopeks=payment.amount_kopeks,
                                received_amount=api_amount,
                                received_kopeks=received_kopeks,
                                order_id=payment.order_id,
                            )
                            await cispay_crud.update_cispay_payment_status(
                                db=db,
                                payment=payment,
                                status='amount_mismatch',
                                is_paid=False,
                                callback_payload={
                                    'check_source': 'api',
                                    'cispay_status_data': status_data,
                                },
                            )
                            return {
                                'payment': payment,
                                'status': 'amount_mismatch',
                                'is_paid': False,
                            }

                        locked = await cispay_crud.get_cispay_payment_by_id_for_update(db, payment.id)
                        if not locked:
                            logger.error('cisPay: не удалось заблокировать платёж', payment_id=payment.id)
                            return None
                        payment = locked

                        if payment.is_paid:
                            logger.info('cisPay платеж уже обработан (api_check)', order_id=payment.order_id)
                            return {
                                'payment': payment,
                                'status': 'success',
                                'is_paid': True,
                            }

                        logger.info('cisPay payment confirmed via API', order_id=payment.order_id)

                        # Обновляем поля без промежуточного commit — он снял бы FOR UPDATE lock
                        payment.status = 'success'
                        payment.is_paid = True
                        payment.paid_at = datetime.now(UTC)
                        payment.callback_payload = {
                            'check_source': 'api',
                            'cispay_status_data': status_data,
                        }
                        payment.updated_at = datetime.now(UTC)
                        await db.flush()

                        await self._finalize_cispay_payment(db, payment, trigger='api_check')
                    elif internal_status != payment.status:
                        payment = await cispay_crud.update_cispay_payment_status(
                            db=db,
                            payment=payment,
                            status=internal_status,
                        )

            except Exception as e:
                logger.error('Error checking cisPay payment status via API', error=e)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        except Exception as e:
            logger.exception('cisPay: ошибка проверки статуса', error=e)
            return None
