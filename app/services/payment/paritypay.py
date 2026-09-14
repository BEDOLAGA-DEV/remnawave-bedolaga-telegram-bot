"""Mixin для интеграции с ParityPay (api.paritypay.net v2, СБП и карты)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.paritypay_service import amount_to_kopeks, paritypay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов счёта ParityPay -> internal
PARITYPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'NEW': ('pending', False),
    'PAID': ('success', True),
    'EXPIRED': ('expired', False),
    'ERROR': ('declined', False),
    'REFUNDED': ('refunded', False),
}

# Статусы, из которых счёт уже никуда не переходит. EXPIRED и ERROR сюда
# намеренно НЕ входят: QR СБП оплачивается дольше нашего таймаута, и пришедший
# следом PAID — настоящая оплата, подтверждённая подписью. Игнорировать её
# значило бы забрать деньги и не зачислить.
PARITYPAY_FINAL_STATUSES = frozenset({'refunded', 'amount_mismatch'})

# Статусы, при которых счёт ещё может быть оплачен (фоновая сверка по API).
PARITYPAY_PENDING_STATUSES = frozenset({'pending'})

# Sub-метод бота -> service ParityPay (в API строчными буквами)
PARITYPAY_METHOD_MAP: dict[str, str] = {
    'sbp': 'sbp',
    'card': 'card',
}


def resolve_paritypay_method(payment_method_type: str | None) -> str | None:
    """Определяет поле ``service`` для API ParityPay.

    Явный sub-метод выигрывает всегда. Для генерик-метода способ не навязываем —
    покупатель выберет его на платёжной странице; исключение только когда
    магазину включён ровно один способ, иначе ParityPay ответит 422.
    """
    explicit = PARITYPAY_METHOD_MAP.get((payment_method_type or '').lower())
    if explicit:
        return explicit

    sbp_only = settings.is_paritypay_sbp_enabled() and not settings.is_paritypay_card_enabled()
    card_only = settings.is_paritypay_card_enabled() and not settings.is_paritypay_sbp_enabled()
    if sbp_only:
        return 'sbp'
    if card_only:
        return 'card'
    return None


def _extract_amount_kopeks(payload: dict[str, Any]) -> int | None:
    """Сумма счёта в копейках, либо None если её не разобрать.

    Провайдер шлёт рубли: числом в ответе API (1500.5) и строкой в уведомлении
    ("1250.00"). Оба варианта разбираются через Decimal.
    """
    return amount_to_kopeks(payload.get('amount'))


class ParityPayPaymentMixin:
    """Mixin для работы с платежами ParityPay."""

    async def create_paritypay_payment(
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
        """Создаёт счёт ParityPay и возвращает данные для перехода на платёжную форму.

        ``payment_method_type`` — sub-метод бота ('card' / 'sbp'); не задан —
        способ выбирает плательщик на форме. Уведомление приходит на адрес из
        настроек кассы.
        """
        if not settings.is_paritypay_enabled():
            logger.error('ParityPay не настроен')
            return None

        min_amount = settings.PARITYPAY_MIN_AMOUNT_KOPEKS
        max_amount = settings.PARITYPAY_MAX_AMOUNT_KOPEKS

        if amount_kopeks < min_amount:
            logger.warning('ParityPay: сумма меньше минимальной', amount_kopeks=amount_kopeks, min_kopeks=min_amount)
            return None

        if amount_kopeks > max_amount:
            logger.warning('ParityPay: сумма больше максимальной', amount_kopeks=amount_kopeks, max_kopeks=max_amount)
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            tg_id = None

        order_id = f'pp{tg_id or "guest"}_{uuid.uuid4().hex[:8]}'
        amount_rubles = amount_kopeks / 100
        paritypay_service_name = resolve_paritypay_method(payment_method_type)

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
            'payment_method_type': payment_method_type,
        }

        lifetime = settings.PARITYPAY_INVOICE_LIFETIME_MINUTES

        try:
            # Сетевой сбой на создании не приводит к дублю: клиент сверится по
            # нашему order_id и переиспользует уже созданный счёт.
            api_result = await paritypay_service.create_invoice_reconciled(
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                comment=description[:255] if description else None,
                service=paritypay_service_name,
                expire_minutes=lifetime,
                success_url=return_url,
                fail_url=fail_url or return_url,
            )

            paritypay_payment_id = api_result.get('id')
            payment_url = api_result.get('link')
            # Срок считаем сами по отправленному expire: провайдер отдаёт строку
            # "Y-m-d H:i:s" без часового пояса, и угадывать его — верный способ
            # промахнуться на несколько часов.
            expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            paritypay_crud = import_module('app.database.crud.paritypay')
            local_payment = await paritypay_crud.create_paritypay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency='RUB',
                description=description,
                payment_url=payment_url,
                payment_method=api_result.get('service') or paritypay_service_name,
                paritypay_payment_id=str(paritypay_payment_id) if paritypay_payment_id else None,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'ParityPay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                service=paritypay_service_name,
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': 'RUB',
                'payment_url': payment_url,
                'payment_id': str(paritypay_payment_id) if paritypay_payment_id else None,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('ParityPay: ошибка создания платежа', error=e)
            return None

    async def process_paritypay_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Обрабатывает HTTP-уведомление ParityPay (подпись уже проверена в webserver).

        Тело счёта: id, order_id, shop_id, amount, credited, comment, service,
        custom_fields, expires, created, status; для счёта по подписке ещё
        subscription_id. Обработка идемпотентна по паре (id, status): повторная
        доставка того же события не зачисляет баланс второй раз.
        """
        try:
            # На тот же адрес приходят уведомления о подписках — у них нет
            # order_id, зато есть shop_subscription_id. Подписки мы не оформляем,
            # но ответить надо: иначе провайдер повторит доставку пять раз.
            if not payload.get('order_id') and payload.get('shop_subscription_id'):
                logger.info(
                    'ParityPay callback: уведомление о подписке, подписки не подключены',
                    subscription_id=payload.get('id'),
                    status=payload.get('status'),
                )
                return True

            our_order_id = payload.get('order_id')
            paritypay_payment_id = payload.get('id')
            paritypay_status = (payload.get('status') or '').strip().upper()

            if not our_order_id or not paritypay_status or not paritypay_payment_id:
                logger.warning('ParityPay callback: отсутствуют обязательные поля', payload=payload)
                return False

            if payload.get('subscription_id'):
                # Регулярное списание по подписке: order_id вида "{sub_id}_2"
                # нашему счёту не соответствует. Подтверждаем и логируем.
                logger.warning(
                    'ParityPay callback: списание по подписке, обработчик не подключён',
                    order_id=our_order_id,
                    subscription_id=payload.get('subscription_id'),
                )
                return True

            paritypay_crud = import_module('app.database.crud.paritypay')
            payment = await paritypay_crud.get_paritypay_payment_by_order_id(db, our_order_id)
            if not payment:
                # Чужой order_id повторами не появится — подтверждаем доставку.
                logger.warning('ParityPay callback: платеж не найден', order_id=our_order_id)
                return True

            event_key = f'{paritypay_payment_id}:{paritypay_status}'
            if paritypay_crud.is_paritypay_event_processed(payment, event_key):
                logger.info('ParityPay callback: событие уже обработано', order_id=our_order_id, event_key=event_key)
                return True

            locked = await paritypay_crud.get_paritypay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('ParityPay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Повторная проверка под блокировкой: параллельная доставка того же
            # события могла пройти между чтением и захватом строки.
            if paritypay_crud.is_paritypay_event_processed(payment, event_key):
                logger.info('ParityPay callback: событие уже обработано (под блокировкой)', event_key=event_key)
                return True

            if paritypay_status not in PARITYPAY_STATUS_MAP:
                # Набор статусов может расширяться — подтверждаем и логируем.
                logger.warning(
                    'ParityPay callback: неизвестный статус',
                    order_id=payment.order_id,
                    status=paritypay_status,
                )
                return True

            if payment.status in PARITYPAY_FINAL_STATUSES:
                logger.warning(
                    'ParityPay callback: платёж в окончательном статусе, событие игнорируется',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=paritypay_status,
                )
                paritypay_crud.remember_paritypay_event(payment, event_key)
                await db.commit()
                return True

            internal_status, is_paid = PARITYPAY_STATUS_MAP[paritypay_status]

            callback_payload = {
                'paritypay_payment_id': paritypay_payment_id,
                'status': paritypay_status,
                'amount': payload.get('amount'),
                'credited': payload.get('credited'),
                'service': payload.get('service'),
                'custom_fields': payload.get('custom_fields'),
            }

            if is_paid:
                return await self._apply_paritypay_success(
                    db,
                    payment=payment,
                    payload=payload,
                    event_key=event_key,
                    paritypay_payment_id=str(paritypay_payment_id),
                    callback_payload=callback_payload,
                )

            if internal_status == 'refunded' and payment.is_paid:
                # Деньги вернулись покупателю уже после зачисления баланса:
                # автоматически списывать нельзя, но возврат должен быть виден.
                logger.error(
                    'ParityPay: возврат по оплаченному платежу, требуется ручная сверка баланса',
                    order_id=payment.order_id,
                    user_id=payment.user_id,
                    amount_kopeks=payment.amount_kopeks,
                )

            paritypay_crud.remember_paritypay_event(payment, event_key)
            await paritypay_crud.update_paritypay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=None,
                paritypay_payment_id=str(paritypay_payment_id),
                callback_payload=callback_payload,
            )
            return True

        except Exception as e:
            logger.exception('ParityPay callback: ошибка обработки', error=e)
            return False

    async def _apply_paritypay_success(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        payload: dict[str, Any],
        event_key: str,
        paritypay_payment_id: str,
        callback_payload: dict[str, Any],
    ) -> bool:
        """Сверяет сумму и зачисляет оплату. Блокировка строки уже взята."""
        paritypay_crud = import_module('app.database.crud.paritypay')

        received_kopeks = _extract_amount_kopeks(payload)
        if received_kopeks is None:
            # Поле amount обязательно по спеке. «Не смогли проверить» не равно
            # «сошлось»: оставляем счёт под ретрай и фоновую сверку.
            logger.error(
                'ParityPay callback: PAID без разбираемой суммы, зачисление отменено',
                order_id=payment.order_id,
                received=payload.get('amount'),
            )
            return False

        if received_kopeks != payment.amount_kopeks:
            logger.error(
                'ParityPay amount mismatch',
                expected_kopeks=payment.amount_kopeks,
                received_kopeks=received_kopeks,
                order_id=payment.order_id,
            )
            paritypay_crud.remember_paritypay_event(payment, event_key)
            await paritypay_crud.update_paritypay_payment_status(
                db=db,
                payment=payment,
                status='amount_mismatch',
                is_paid=False,
                callback_payload=callback_payload,
            )
            return False

        if payment.is_paid:
            # Оплату уже зачислили (другим событием или фоновой сверкой).
            logger.info('ParityPay callback: платеж уже оплачен', order_id=payment.order_id)
            paritypay_crud.remember_paritypay_event(payment, event_key)
            await db.commit()
            return True

        payment.status = 'success'
        payment.is_paid = True
        payment.paid_at = datetime.now(UTC)
        payment.paritypay_payment_id = paritypay_payment_id or payment.paritypay_payment_id
        # credited — сумма за вычетом комиссии провайдера. Пользователю
        # зачисляем полную сумму счёта, credited храним для сверки с кассой.
        credited = amount_to_kopeks(payload.get('credited'))
        if credited is not None:
            payment.credited_kopeks = credited
        payment.callback_payload = callback_payload
        payment.updated_at = datetime.now(UTC)
        paritypay_crud.remember_paritypay_event(payment, event_key)
        await db.flush()

        return await self._finalize_paritypay_payment(db, payment, trigger='webhook')

    async def _finalize_paritypay_payment(
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
        paritypay_crud = import_module('app.database.crud.paritypay')

        if payment.transaction_id:
            logger.info(
                'ParityPay платеж уже связан с транзакцией',
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
            provider_name='paritypay',
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
            logger.error('Пользователь не найден для ParityPay', user_id=payment.user_id)
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
                PaymentMethod.PARITYPAY,
            )

        display_name = settings.get_paritypay_display_name()
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
                payment_method=PaymentMethod.PARITYPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await paritypay_crud.link_paritypay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('ParityPay платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.PARITYPAY,
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
            logger.error('Ошибка обработки реферального пополнения ParityPay', error=error)

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
                logger.error('Ошибка отправки админ уведомления ParityPay', error=error)

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
                logger.error('Ошибка отправки уведомления пользователю ParityPay', error=error)

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
            'Обработан ParityPay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_paritypay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API ParityPay и синхронизирует БД.

        Страховка на случай потерянного вебхука: используется ручной проверкой
        из админки и фоновой сверкой.
        """
        try:
            paritypay_crud = import_module('app.database.crud.paritypay')
            payment = await paritypay_crud.get_paritypay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('ParityPay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {'payment': payment, 'status': payment.status, 'is_paid': True}

            if payment.status in PARITYPAY_FINAL_STATUSES:
                return {'payment': payment, 'status': payment.status, 'is_paid': False}

            try:
                status_data = await paritypay_service.get_invoice(
                    invoice_id=payment.paritypay_payment_id,
                    order_id=None if payment.paritypay_payment_id else payment.order_id,
                )
            except Exception as e:
                logger.error('Error checking ParityPay payment status via API', error=e)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

            if not status_data:
                logger.warning('ParityPay API check: платёж не найден на стороне провайдера', order_id=payment.order_id)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            paritypay_status = (status_data.get('status') or '').strip().upper()
            internal_status, is_paid = PARITYPAY_STATUS_MAP.get(paritypay_status, ('pending', False))

            if not is_paid:
                if internal_status != payment.status:
                    payment = await paritypay_crud.update_paritypay_payment_status(
                        db=db,
                        payment=payment,
                        status=internal_status,
                    )
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            # Сверяем сумму так же строго, как в уведомлении.
            received_kopeks = _extract_amount_kopeks(status_data)
            if received_kopeks is None:
                logger.error('ParityPay API check: PAID без разбираемой суммы, зачисление отменено', order_id=order_id)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            if received_kopeks != payment.amount_kopeks:
                logger.error(
                    'ParityPay amount mismatch (API check)',
                    expected_kopeks=payment.amount_kopeks,
                    received_kopeks=received_kopeks,
                    order_id=payment.order_id,
                )
                await paritypay_crud.update_paritypay_payment_status(
                    db=db,
                    payment=payment,
                    status='amount_mismatch',
                    is_paid=False,
                    callback_payload={'check_source': 'api', 'paritypay_status_data': status_data},
                )
                return {'payment': payment, 'status': 'amount_mismatch', 'is_paid': False}

            locked = await paritypay_crud.get_paritypay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('ParityPay: не удалось заблокировать платёж', payment_id=payment.id)
                return None
            payment = locked

            if payment.is_paid:
                logger.info('ParityPay платеж уже обработан (api_check)', order_id=payment.order_id)
                return {'payment': payment, 'status': 'success', 'is_paid': True}

            logger.info('ParityPay payment confirmed via API', order_id=payment.order_id)

            # Обновляем поля без промежуточного commit — он снял бы FOR UPDATE lock
            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.callback_payload = {'check_source': 'api', 'paritypay_status_data': status_data}
            payment.updated_at = datetime.now(UTC)
            if status_data.get('id'):
                payment.paritypay_payment_id = str(status_data['id'])
            # Ключ события в том же формате, что у вебхука: пришедший следом
            # SUCCESS по этому же платежу не станет зачислять второй раз.
            if payment.paritypay_payment_id:
                paritypay_crud.remember_paritypay_event(payment, f'{payment.paritypay_payment_id}:{paritypay_status}')
            await db.flush()

            await self._finalize_paritypay_payment(db, payment, trigger='api_check')

            return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

        except Exception as e:
            logger.exception('ParityPay: ошибка проверки статуса', error=e)
            return None
