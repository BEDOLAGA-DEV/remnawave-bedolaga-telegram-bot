"""Mixin для интеграции с Cashera (api.cashera.cash, server-to-server)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services import cashera_recurring_cancel as cashera_cancel
from app.services.cashera_service import cashera_service, normalize_payment_url
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Логгеры платёжных модулей (app.payments) не доходят до админ-чата — возвраты и
# чарджбэки, требующие внимания человека, сообщаем отдельным логгером вне этих фильтров.
alert_logger = structlog.get_logger('app.cashera_alert')

# Статус Cashera -> (внутренний статус, оплачен ли)
CASHERA_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'pending': ('pending', False),
    'paid': ('success', True),
    'failed': ('failed', False),
    'expired': ('expired', False),
    'refunded': ('refunded', False),
    'chargeback': ('chargeback', False),
}

# H2H (свой экран оплаты) у Cashera есть только у этих методов; mastercard и
# cryptobot работают исключительно через страницу провайдера.
CASHERA_H2H_METHODS = frozenset({'sbp', 'card', 'crypto'})
_H2H_ATTEMPTS = 3
_H2H_DELAY_SECONDS = 1.0

# Финальные неуспехи: повторный вебхук не должен «чинить» такой платёж.
CASHERA_TERMINAL_FAILURES = frozenset({'failed', 'expired', 'refunded', 'chargeback', 'amount_mismatch', 'error'})


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class CasheraPaymentMixin:
    """Mixin для работы с платежами Cashera."""

    async def create_cashera_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        language: str = 'ru',
        payment_method_code: str | None = None,
        return_url: str | None = None,
        fail_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Создаёт платёж Cashera и сохраняет его до ответа покупателю.

        ``payment_method_code`` — код метода Cashera (sbp, card, …) из активных.
        Без него создаётся общая платёжная форма, где метод выбирает покупатель.
        """
        if not settings.is_cashera_enabled():
            logger.error('Cashera не настроена')
            return None

        if amount_kopeks < settings.CASHERA_MIN_AMOUNT_KOPEKS or amount_kopeks > settings.CASHERA_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Cashera: сумма вне допустимого диапазона',
                amount_kopeks=amount_kopeks,
                min_kopeks=settings.CASHERA_MIN_AMOUNT_KOPEKS,
                max_kopeks=settings.CASHERA_MAX_AMOUNT_KOPEKS,
            )
            return None

        if payment_method_code is not None and payment_method_code not in settings.get_cashera_active_methods():
            logger.warning('Cashera: метод не включён', payment_method=payment_method_code)
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user and user.telegram_id else f'u{user_id}'
        else:
            tg_id = 'guest'

        # external_id Cashera: буквы, цифры и . _ - (до 255). Он же ключ идемпотентности.
        order_id = f'cas{tg_id}_{uuid.uuid4().hex[:10]}'

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
            'payment_method': payment_method_code,
        }

        try:
            api_result = await cashera_service.create_transaction(
                amount_kopeks=amount_kopeks,
                external_id=order_id,
                description=description,
                payment_method=payment_method_code,
                callback_url=settings.get_cashera_callback_url(),
                success_url=return_url or settings.get_cashera_return_url(),
                fail_url=fail_url or settings.get_cashera_failed_url(),
            )
        except Exception as error:
            logger.exception('Cashera: ошибка создания платежа', error=error)
            return None

        cashera_uuid = str(api_result.get('uuid'))
        payment_url = normalize_payment_url(api_result.get('payment_url'))
        expires_at = _parse_datetime(api_result.get('expires_at'))

        cashera_crud = import_module('app.database.crud.cashera')
        # Сохраняем даже без payment_url: транзакция на стороне Cashera создана, и
        # пришедший вебхук должен найти платёж, иначе деньги пришлось бы сверять руками.
        local_payment = await cashera_crud.create_cashera_payment(
            db=db,
            user_id=user_id,
            order_id=order_id,
            amount_kopeks=amount_kopeks,
            currency='RUB',
            description=description,
            payment_url=payment_url,
            payment_method=api_result.get('payment_method') or payment_method_code,
            cashera_uuid=cashera_uuid,
            cashera_status=(api_result.get('status') or 'pending').lower(),
            expires_at=expires_at,
            metadata_json=metadata,
        )

        if not payment_url:
            logger.warning('Cashera: ответ без payment_url', order_id=order_id, cashera_uuid=cashera_uuid)

        logger.info(
            'Cashera: создан платеж',
            order_id=order_id,
            user_id=user_id,
            amount_kopeks=amount_kopeks,
            payment_method=payment_method_code,
        )

        return {
            'order_id': order_id,
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'currency': 'RUB',
            'payment_url': payment_url,
            'payment_id': cashera_uuid,
            'expires_at': expires_at.isoformat() if expires_at else None,
            'local_payment_id': local_payment.id,
        }

    async def process_cashera_webhook(self, db: AsyncSession, payload: dict[str, Any]) -> bool:
        """Обрабатывает вебхук Cashera (подлинность уже проверена в webserver).

        True — принять (ответ 2xx). False — ответить 5xx, чтобы Cashera повторила:
        только там, где повтор может помочь. 4xx Cashera не повторяет вовсе.
        """
        event = payload.get('event')

        # Автопродление: состояние подписки и списания по ней. Обрабатываются и при
        # выключенном флаге — живые привязки у Cashera продолжают списывать.
        if event == 'subscription.status_updated' and isinstance(payload.get('subscription'), dict):
            try:
                return await self.process_cashera_subscription_status(db, payload['subscription'])
            except Exception as error:
                logger.exception('Cashera: ошибка обработки события подписки', error=error)
                return False
        from app.services.cashera_recurrent import is_recurring_charge

        if event == 'transaction.status_updated' and is_recurring_charge(payload):
            try:
                return await self.process_cashera_recurring_charge(db, payload)
            except Exception as error:
                logger.exception('Cashera: ошибка обработки списания по подписке', error=error)
                return False

        if event != 'transaction.status_updated':
            # webhook.test, выплаты, подписки и будущие события — просто подтверждаем.
            logger.info('Cashera webhook: событие не про платёж, подтверждаем', cashera_event=event)
            return True

        transaction = payload.get('transaction')
        if not isinstance(transaction, dict):
            logger.warning('Cashera webhook: нет объекта transaction')
            return True

        order_id = transaction.get('external_id')
        if not order_id:
            logger.warning('Cashera webhook: нет external_id', cashera_uuid=transaction.get('uuid'))
            return True

        try:
            cashera_crud = import_module('app.database.crud.cashera')
            payment = await cashera_crud.get_cashera_payment_by_order_id(db, str(order_id))
            if not payment:
                # Не наш платёж (например, другой интеграции того же мерчанта) — повтор не поможет.
                logger.warning('Cashera webhook: платеж не найден', order_id=order_id)
                return True

            locked = await cashera_crud.get_cashera_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Cashera: не удалось заблокировать платёж', payment_id=payment.id)
                return False

            return await self._apply_cashera_transaction(db, locked, transaction, source='webhook')
        except Exception as error:
            logger.exception('Cashera webhook: ошибка обработки', error=error)
            return False

    async def _apply_cashera_transaction(
        self,
        db: AsyncSession,
        payment: Any,
        transaction: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        """Применяет состояние транзакции Cashera к платежу (FOR UPDATE уже взят).

        Общая логика вебхука и сверки через API. Возвращает False только когда
        имеет смысл повторить (подтверждённой суммы в paid нет).
        """
        cashera_crud = import_module('app.database.crud.cashera')
        incoming_status = str(transaction.get('status') or '').strip().lower()
        callback_payload = {
            'source': source,
            'uuid': transaction.get('uuid'),
            'status': incoming_status,
            'amount': transaction.get('amount'),
            'gross_amount': transaction.get('gross_amount'),
            'net_amount': transaction.get('net_amount'),
            'currency': transaction.get('currency'),
            'payment_method': transaction.get('payment_method'),
            'paid_at': transaction.get('paid_at'),
        }

        if payment.is_paid:
            if incoming_status in {'refunded', 'chargeback'} and payment.cashera_status != incoming_status:
                await self._reverse_cashera_payment(db, payment, incoming_status, callback_payload)
            else:
                logger.info('Cashera: платеж уже обработан', order_id=payment.order_id, source=source)
            return True

        if payment.status in CASHERA_TERMINAL_FAILURES:
            logger.warning(
                'Cashera: платёж в финальном неуспешном статусе, событие игнорируется',
                order_id=payment.order_id,
                current_status=payment.status,
                incoming_status=incoming_status,
            )
            return True

        if incoming_status and incoming_status == payment.cashera_status and incoming_status != 'paid':
            # Идемпотентность uuid + status: этот статус уже обработан.
            return True

        if incoming_status not in CASHERA_STATUS_MAP:
            logger.warning('Cashera: неизвестный статус', order_id=payment.order_id, cashera_status=incoming_status)
            return True

        internal_status, is_paid = CASHERA_STATUS_MAP[incoming_status]
        transaction_uuid = transaction.get('uuid')

        if is_paid:
            received_amount = transaction.get('amount')
            if received_amount is None:
                # Без подтверждённой суммы не зачисляем; статус не финальный — повтор или
                # сверка через API ещё могут закрыть платёж.
                logger.error('Cashera: paid без поля amount, зачисление отменено', order_id=payment.order_id)
                return False
            try:
                received_kopeks = int(received_amount)
            except (TypeError, ValueError):
                received_kopeks = None
            currency = str(transaction.get('currency') or '').upper()

            if received_kopeks != payment.amount_kopeks or currency != (payment.currency or 'RUB').upper():
                logger.error(
                    'Cashera amount mismatch',
                    order_id=payment.order_id,
                    expected_kopeks=payment.amount_kopeks,
                    received_amount=received_amount,
                    expected_currency=payment.currency,
                    received_currency=currency,
                )
                await cashera_crud.update_cashera_payment_status(
                    db=db,
                    payment=payment,
                    status='amount_mismatch',
                    is_paid=False,
                    cashera_status=incoming_status,
                    callback_payload=callback_payload,
                )
                # Повтор не исправит расхождение — подтверждаем, платёж ждёт разбора.
                return True

            payment.status = internal_status
            payment.is_paid = True
            payment.cashera_status = incoming_status
            payment.paid_at = _parse_datetime(transaction.get('paid_at')) or datetime.now(UTC)
            if transaction_uuid:
                payment.cashera_uuid = str(transaction_uuid)
            if transaction.get('payment_method'):
                payment.payment_method = transaction.get('payment_method')
            payment.callback_payload = callback_payload
            payment.updated_at = datetime.now(UTC)
            # Без промежуточного commit — он снял бы FOR UPDATE lock до зачисления.
            await db.flush()
            return await self._finalize_cashera_payment(db, payment, trigger=source)

        await cashera_crud.update_cashera_payment_status(
            db=db,
            payment=payment,
            status=internal_status,
            is_paid=False,
            cashera_status=incoming_status,
            cashera_uuid=str(transaction_uuid) if transaction_uuid else None,
            callback_payload=callback_payload,
        )
        return True

    async def _reverse_cashera_payment(
        self,
        db: AsyncSession,
        payment: Any,
        incoming_status: str,
        callback_payload: dict[str, Any],
    ) -> None:
        """Возврат или чарджбэк по уже зачисленному платежу: списываем зачисленное.

        Отрицательного баланса в боте нет, поэтому списываем не больше, чем есть:
        недостающее фиксируется в платеже и уходит тревогой — решать админу.
        Идемпотентно: повторное событие (и смена refunded → chargeback) второй раз
        не списывает. FOR UPDATE по платежу уже взят вызывающим.
        """
        payment_module = import_module('app.services.payment_service')
        metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        payment.cashera_status = incoming_status
        payment.callback_payload = callback_payload
        payment.updated_at = datetime.now(UTC)

        if metadata.get('reversal'):
            payment.metadata_json = metadata
            await db.commit()
            return

        kind = 'Чарджбэк' if incoming_status == 'chargeback' else 'Возврат'
        reversal: dict[str, Any] = {
            'kind': incoming_status,
            'amount_kopeks': payment.amount_kopeks,
            'at': datetime.now(UTC).isoformat(),
        }

        credited = bool(metadata.get('balance_credited')) and payment.user_id is not None
        user = await payment_module.get_user_by_id(db, payment.user_id) if credited else None
        if user is None:
            # Гостевая покупка или баланс не зачислялся — списывать нечего, только сообщаем.
            reversal['debited_kopeks'] = 0
            reversal['shortfall_kopeks'] = payment.amount_kopeks
            metadata['reversal'] = reversal
            payment.metadata_json = metadata
            await db.commit()
            alert_logger.error(
                f'Cashera: {kind.lower()} по платежу без зачисления на баланс — разобрать вручную',
                order_id=payment.order_id,
                user_id=payment.user_id,
                amount_kopeks=payment.amount_kopeks,
            )
            return

        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)
        debit = min(max(user.balance_kopeks, 0), payment.amount_kopeks)
        shortfall = payment.amount_kopeks - debit
        old_balance = user.balance_kopeks

        if debit > 0:
            user.balance_kopeks -= debit
            user.updated_at = datetime.now(UTC)
            await payment_module.create_transaction(
                db,
                user_id=user.id,
                type=TransactionType.WITHDRAWAL,
                amount_kopeks=debit,
                description=f'{kind} платежа {settings.get_cashera_display_name()}',
                payment_method=PaymentMethod.CASHERA,
                external_id=f'{payment.order_id}:reversal',
                is_completed=True,
                commit=False,
            )

        reversal['debited_kopeks'] = debit
        reversal['shortfall_kopeks'] = shortfall
        reversal['balance_before'] = old_balance
        metadata['reversal'] = reversal
        payment.metadata_json = metadata
        await db.commit()

        alert_logger.error(
            f'Cashera: {kind.lower()} по зачисленному платежу — баланс списан',
            order_id=payment.order_id,
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_kopeks=payment.amount_kopeks,
            debited_kopeks=debit,
            shortfall_kopeks=shortfall,
        )

    async def _finalize_cashera_payment(self, db: AsyncSession, payment: Any, *, trigger: str) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock уже взят вызывающим.
        """
        payment_module = import_module('app.services.payment_service')
        cashera_crud = import_module('app.database.crud.cashera')

        if payment.transaction_id:
            logger.info(
                'Cashera платеж уже связан с транзакцией',
                order_id=payment.order_id,
                transaction_id=payment.transaction_id,
                trigger=trigger,
            )
            await db.commit()
            return True

        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.order_id,
            provider_name='cashera',
        )
        if guest_result is not None:
            return True

        balance_already_credited = bool(metadata.get('balance_credited'))

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для Cashera', user_id=payment.user_id)
            return False

        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = payment.order_id
        existing_transaction = await payment_module.get_transaction_by_external_id(
            db,
            transaction_external_id,
            PaymentMethod.CASHERA,
        )

        display_name = settings.get_cashera_display_name()
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
                payment_method=PaymentMethod.CASHERA,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await cashera_crud.link_cashera_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        if not (created_transaction or not balance_already_credited):
            logger.info('Cashera платеж уже зачислил баланс ранее', order_id=payment.order_id)
            await db.commit()
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
            payment_method=PaymentMethod.CASHERA,
            external_id=transaction_external_id,
        )

        topup_status = '\U0001f195 Первое пополнение' if was_first_topup else '\U0001f504 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, payment.amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения Cashera', error=error)

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
                logger.error('Ошибка отправки админ уведомления Cashera', error=error)

        if getattr(self, 'bot', None) and user.telegram_id and settings.is_notifications_enabled():
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'\U0001f4b0 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'\U0001f4b3 Способ: {settings.get_cashera_display_name_html()}\n'
                        f'\U0001f194 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Cashera', error=error)

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

        logger.info('Обработан Cashera платеж', order_id=payment.order_id, user_id=payment.user_id, trigger=trigger)
        return True

    async def check_cashera_payment_status(self, db: AsyncSession, order_id: str) -> dict[str, Any] | None:
        """Сверяет платёж с Cashera через API и синхронизирует БД.

        Резерв на случай, если вебхук не дошёл: ручная проверка из админки,
        кнопка «Проверить статус» и фоновая сверка.
        """
        cashera_crud = import_module('app.database.crud.cashera')
        payment = await cashera_crud.get_cashera_payment_by_order_id(db, order_id)
        if not payment:
            logger.warning('Cashera payment not found', order_id=order_id)
            return None

        if payment.is_paid or payment.status in CASHERA_TERMINAL_FAILURES:
            return {'payment': payment, 'status': payment.status, 'is_paid': bool(payment.is_paid)}

        try:
            if payment.cashera_uuid:
                remote = await cashera_service.get_transaction(payment.cashera_uuid)
            else:
                remote = await cashera_service.get_transaction_by_external_id(payment.order_id)
        except Exception as error:
            logger.error('Cashera: не удалось получить статус через API', order_id=order_id, error=str(error))
            return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': bool(payment.is_paid)}

        locked = await cashera_crud.get_cashera_payment_by_id_for_update(db, payment.id)
        if not locked:
            logger.error('Cashera: не удалось заблокировать платёж', payment_id=payment.id)
            return None

        await self._apply_cashera_transaction(db, locked, remote, source='api_check')
        await db.refresh(locked)
        return {'payment': locked, 'status': locked.status or 'pending', 'is_paid': bool(locked.is_paid)}

    async def get_cashera_h2h(self, cashera_uuid: str | None, payment_method: str | None) -> dict[str, Any] | None:
        """Реквизиты для своего экрана оплаты или None — тогда показываем ссылку.

        Реквизиты появляются не сразу: пока их нет, Cashera отвечает 422 — делаем
        несколько коротких повторов. Любая другая неудача — молча None: у покупателя
        всегда остаётся обычная ссылка на оплату.
        """
        if not settings.CASHERA_H2H_ENABLED or not cashera_uuid or payment_method not in CASHERA_H2H_METHODS:
            return None

        import asyncio

        from app.services.cashera_service import CasheraAPIError

        for attempt in range(1, _H2H_ATTEMPTS + 1):
            try:
                data = await cashera_service.get_h2h(cashera_uuid)
            except CasheraAPIError as error:
                if error.status_code == 422 and attempt < _H2H_ATTEMPTS:
                    await asyncio.sleep(_H2H_DELAY_SECONDS)
                    continue
                logger.info('Cashera H2H: реквизиты недоступны, остаётся ссылка', status=error.status_code)
                return None
            except Exception as error:
                logger.warning('Cashera H2H: ошибка получения реквизитов', error=str(error))
                return None
            qr = str(data.get('qr') or '').strip()
            return {'qr': qr, 'amount': data.get('amount')} if qr else None
        return None

    async def get_cashera_payment_status(self, db: AsyncSession, local_payment_id: int) -> dict[str, Any] | None:
        """Статус по локальному id — для кнопки «Проверить статус» в боте."""
        cashera_crud = import_module('app.database.crud.cashera')
        payment = await cashera_crud.get_cashera_payment_by_id(db, local_payment_id)
        if not payment:
            return None
        return await self.check_cashera_payment_status(db, payment.order_id)

    # ==================== Автопродление: подписки Cashera ====================

    async def _notify_cashera_recurring(self, db: AsyncSession, record: Any, kind: str) -> None:
        """Best-effort уведомление о событии автопродления (см. cashera_recurring_cancel)."""
        await cashera_cancel.notify_cashera_recurring(db, record, kind, bot=getattr(self, 'bot', None))

    async def create_cashera_recurrent_subscription(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        subscription: Any,
        tariff: Any,
    ) -> dict[str, Any]:
        """Оформляет подписку Cashera для подписки бота.

        Каденс — та же иерархия, что у Platega и balance-autopay: выбор
        пользователя → глобальный дефолт → самый короткий период тарифа → 30 дней.
        Сумма — полная цена продления без промо (user=None), округлённая вверх до
        рублей (Cashera принимает только целые рубли). Первое подтверждение по
        ``redirect_url`` — до него запись PENDING.
        """
        from app.database.crud import cashera_subscription as sub_crud
        from app.services import cashera_recurrent as cr
        from app.services.autopay_period import resolve_autopay_period_candidate

        existing = await sub_crud.get_active_cashera_subscription_by_subscription(db, subscription.id)
        if existing:
            # Идемпотентный повтор тоже восстанавливает взаимоисключение движков продления.
            if getattr(subscription, 'autopay_enabled', False):
                subscription.autopay_enabled = False
                await db.commit()
            return {
                'local_id': existing.id,
                'cashera_subscription_uuid': existing.cashera_subscription_uuid,
                'redirect_url': existing.redirect_url,
                'status': existing.status,
            }

        period_days = (
            resolve_autopay_period_candidate(getattr(subscription, 'autopay_period_days', None), tariff)
            or resolve_autopay_period_candidate(getattr(settings, 'DEFAULT_AUTOPAY_PERIOD_DAYS', 0), tariff)
            or (tariff.get_shortest_period() if tariff else None)
            or 30
        )
        interval, charge_days = cr.resolve_cashera_interval(period_days, bool(getattr(tariff, 'is_daily', False)))

        amount_kopeks = 0
        try:
            from app.services.pricing_engine import pricing_engine

            pricing_result = await pricing_engine.calculate_tariff_purchase_price(
                tariff,
                charge_days,
                device_limit=getattr(subscription, 'device_limit', None),
            )
            amount_kopeks = int(pricing_result.final_total or 0)
        except Exception as pricing_error:  # pragma: no cover - defensive
            logger.warning('Cashera: не удалось посчитать цену с доп. устройствами', error=str(pricing_error))
        if amount_kopeks <= 0 and tariff is not None:
            amount_kopeks = int(tariff.get_purchasable_price_for_period(charge_days) or 0)
        amount_kopeks = cr.round_up_to_rubles(amount_kopeks)
        # Вся валидация — ДО обращения к Cashera: подписка там создаётся сразу, и
        # raise после неё оставил бы привязку, которую нечем отменить.
        if amount_kopeks < 100:
            raise ValueError(f'Тариф не имеет цены за период {charge_days} дн. — автопродление Cashera недоступно')

        external_id = cr.build_subscription_external_id(subscription.id, uuid.uuid4().hex[:12])
        response = await cashera_service.create_subscription(
            amount_kopeks=amount_kopeks,
            external_id=external_id,
            interval=interval,
            description=getattr(tariff, 'name', None) or 'Подписка',
            callback_url=settings.get_cashera_callback_url(),
        )
        cashera_uuid = str(response.get('uuid'))
        redirect_url = normalize_payment_url(response.get('payment_url'))
        remote_status = cr.normalize_remote_status(response.get('status'))

        try:
            record = await sub_crud.create_cashera_subscription(
                db,
                user_id=user_id,
                subscription_id=subscription.id,
                tariff_id=getattr(tariff, 'id', None),
                external_id=external_id,
                interval=interval,
                charge_days=charge_days,
                amount_kopeks=amount_kopeks,
                redirect_url=redirect_url,
                cashera_subscription_uuid=cashera_uuid,
                remote_status=remote_status,
            )
        except IntegrityError:
            # Конкурентный enable занял partial unique — наша удалённая подписка
            # осталась бы сиротой. Отменяем её и возвращаем победителя.
            await db.rollback()
            try:
                await cashera_service.cancel_subscription(cashera_uuid)
            except Exception as cancel_error:  # pragma: no cover - network errors
                logger.error(
                    'Cashera: не удалось отменить осиротевшую подписку после гонки',
                    cashera_uuid=cashera_uuid,
                    error=str(cancel_error),
                )
            winner = await sub_crud.get_active_cashera_subscription_by_subscription(db, subscription.id)
            if not winner:
                raise
            return {
                'local_id': winner.id,
                'cashera_subscription_uuid': winner.cashera_subscription_uuid,
                'redirect_url': winner.redirect_url,
                'status': winner.status,
            }

        # Взаимоисключение движков продления — после успешного создания записи:
        # сбой оформления не должен оставлять человека вообще без автопродления.
        subscription.autopay_enabled = False
        await db.commit()

        from app.services.payment.lava import cancel_lava_recurring_for_subscription_safe
        from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

        await cancel_platega_recurring_for_subscription_safe(db, subscription.id)
        await cancel_lava_recurring_for_subscription_safe(db, subscription.id)

        return {
            'local_id': record.id,
            'cashera_subscription_uuid': cashera_uuid,
            'redirect_url': redirect_url,
            'status': record.status,
        }

    async def cancel_cashera_recurrent_subscription(
        self,
        db: AsyncSession,
        *,
        local_id: int,
        commit: bool = True,
    ) -> bool:
        """Отменяет одну подписку Cashera по локальному id (см. cashera_recurring_cancel)."""
        return await cashera_cancel.cancel_cashera_recurrent_subscription(db, local_id=local_id, commit=commit)

    async def cancel_cashera_recurring_for_subscription(
        self,
        db: AsyncSession,
        subscription_id: int,
        *,
        commit: bool = True,
    ) -> None:
        """Best-effort отмена живой подписки Cashera по subscription_id; не бросает."""
        await cashera_cancel.cancel_cashera_recurring_for_subscription(db, subscription_id, commit=commit)

    async def _find_cashera_subscription_record(self, db: AsyncSession, ref: dict[str, Any]) -> Any | None:
        from app.database.crud import cashera_subscription as sub_crud

        record = None
        if ref.get('uuid'):
            record = await sub_crud.get_cashera_subscription_by_uuid(db, str(ref['uuid']))
        if record is None and ref.get('external_id'):
            record = await sub_crud.get_cashera_subscription_by_external_id(db, str(ref['external_id']))
        if record is None:
            return None
        return await sub_crud.get_cashera_subscription_by_id_for_update(db, record.id)

    async def process_cashera_subscription_status(self, db: AsyncSession, subscription_payload: dict[str, Any]) -> bool:
        """Событие subscription.status_updated: состояние привязки, не оплата.

        active подтверждает согласие клиента, но не конкретное списание — продление
        идёт только по транзакции paid. Локально отменённую запись не воскрешаем:
        если Cashera считает её активной, повторяем удалённую отмену.
        """
        from app.services import cashera_recurrent as cr

        record = await self._find_cashera_subscription_record(db, subscription_payload)
        if record is None:
            logger.warning('Cashera subscription event: подписка не найдена', ref=subscription_payload.get('uuid'))
            return True

        remote_status = cr.normalize_remote_status(subscription_payload.get('status'))
        new_status = cr.local_status_for(remote_status)
        record.remote_status = remote_status
        if subscription_payload.get('uuid') and not record.cashera_subscription_uuid:
            record.cashera_subscription_uuid = str(subscription_payload['uuid'])
        next_charge = _parse_datetime(subscription_payload.get('next_charge_at'))
        if next_charge:
            record.next_charge_at = next_charge

        if record.status == 'CANCELLED':
            await db.commit()
            if remote_status in ('active', 'past_due', 'pending_agreement'):
                logger.error(
                    'Cashera: подписка отменена у нас, но жива у провайдера — повторяем отмену',
                    cashera_uuid=record.cashera_subscription_uuid,
                )
                try:
                    await cashera_service.cancel_subscription(record.cashera_subscription_uuid)
                except Exception as error:  # pragma: no cover - network errors
                    logger.warning('Cashera: повторная удалённая отмена не удалась', error=str(error))
            return True

        previous = record.status
        if new_status and new_status != previous:
            record.status = new_status
        await db.commit()

        if record.status != previous:
            kind = {'ACTIVE': 'activated', 'CANCELLED': 'cancelled', 'FAILED': 'cancelled'}.get(record.status)
            # Возврат из PAST_DUE в ACTIVE — не новая привязка, о нём скажет само списание.
            if kind and not (kind == 'activated' and previous == 'PAST_DUE'):
                await self._notify_cashera_recurring(db, record, kind)
        return True

    async def process_cashera_recurring_charge(self, db: AsyncSession, payload: dict[str, Any]) -> bool:
        """Списание по подписке: transaction.status_updated с объектом subscription.

        Баланс не трогается — подписка продлевается напрямую, как у Platega/Lava.
        """
        transaction = payload.get('transaction') if isinstance(payload.get('transaction'), dict) else {}
        ref = payload.get('subscription') if isinstance(payload.get('subscription'), dict) else {}
        record = await self._find_cashera_subscription_record(db, ref)
        if record is None:
            logger.warning(
                'Cashera: списание по неизвестной подписке',
                subscription_uuid=ref.get('uuid'),
                charge_uuid=transaction.get('uuid'),
            )
            return True
        return await self._apply_cashera_charge(db, record, transaction, source='webhook')

    async def _apply_cashera_charge(
        self,
        db: AsyncSession,
        record: Any,
        transaction: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        """Применяет одно списание к подписке (FOR UPDATE по записи уже взят)."""
        from sqlalchemy import select as sa_select

        from app.database.models import Subscription, Transaction
        from app.services import cashera_recurrent as cr

        status = str(transaction.get('status') or '').strip().lower()
        charge_id = transaction.get('uuid')

        if status in cr.CHARGE_SUCCESS:
            if not charge_id:
                # Без uuid идемпотентность не сработает — не продлеваем.
                logger.warning('Cashera: списание paid без uuid', subscription_record=record.id)
                return True
            charge_id = str(charge_id)
            if record.last_charge_external_id == charge_id:
                return True
            duplicate = (
                await db.execute(
                    sa_select(Transaction.id).where(
                        Transaction.external_id == charge_id,
                        Transaction.payment_method == PaymentMethod.CASHERA.value,
                    )
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                return True

            if str(transaction.get('currency') or 'RUB').upper() != 'RUB':
                logger.error('Cashera: списание не в RUB — не продлеваем', charge_id=charge_id)
                return True
            try:
                charged_kopeks = int(transaction.get('amount'))
            except (TypeError, ValueError):
                charged_kopeks = 0
            if charged_kopeks > 0 and charged_kopeks != record.amount_kopeks:
                logger.warning(
                    'Cashera: сумма списания отличается от сохранённой — фиксируем фактическую',
                    stored_kopeks=record.amount_kopeks,
                    charged_kopeks=charged_kopeks,
                )
                record.amount_kopeks = charged_kopeks

            subscription = await db.get(Subscription, record.subscription_id)
            if subscription is None:
                # Продлевать нечего, а деньги взяты — единственное полезное: остановить списания.
                logger.error('Cashera: списание по удалённой подписке — останавливаем', charge_id=charge_id)
                try:
                    await cashera_service.cancel_subscription(record.cashera_subscription_uuid)
                except Exception as cancel_error:  # pragma: no cover - network errors
                    logger.error('Cashera: не удалось остановить списания', error=str(cancel_error))
                record.status = 'CANCELLED'
                await db.commit()
                return True

            from app.database.crud.subscription import _lock_subscription_row, reconcile_tariff_traffic_limit
            from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
            from app.services.grace_access_echo import undo_grace_overlay_echo

            await _lock_subscription_row(db, subscription)
            await undo_grace_overlay_echo(db, subscription)
            subscription.extend_subscription(record.charge_days)
            await reconcile_tariff_traffic_limit(db, subscription)

            # Списание по локально отменённой записи: деньги взяты — продлеваем, но
            # запись не воскрешаем и повторяем удалённую отмену.
            was_cancelled = record.status == 'CANCELLED'
            charged_at = _parse_datetime(transaction.get('paid_at')) or datetime.now(UTC)
            record.last_charge_external_id = charge_id
            record.last_charge_at = charged_at
            record.charges_success += 1
            if not was_cancelled:
                record.status = 'ACTIVE'
                record.next_charge_at = charged_at + timedelta(days=record.charge_days)

            tx = await create_transaction(
                db,
                user_id=record.user_id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=record.amount_kopeks,
                description=f'Автопродление {settings.get_cashera_display_name()}',
                payment_method=PaymentMethod.CASHERA,
                external_id=charge_id,
                commit=False,
            )
            await db.commit()

            await emit_transaction_side_effects(
                db,
                tx,
                amount_kopeks=record.amount_kopeks,
                user_id=record.user_id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                payment_method=PaymentMethod.CASHERA,
                external_id=charge_id,
                description=f'Автопродление {settings.get_cashera_display_name()}',
            )
            await self._notify_cashera_recurring(db, record, 'confirmed')

            if was_cancelled and record.cashera_subscription_uuid:
                try:
                    await cashera_service.cancel_subscription(record.cashera_subscription_uuid)
                except Exception as cancel_error:  # pragma: no cover - network errors
                    logger.warning('Cashera: повторная удалённая отмена не удалась', error=str(cancel_error))

            # Синк панели — последним: при сбое update_remnawave_user делает rollback,
            # экспайрящий сессию, а продление уже закоммичено.
            subscription_id_for_log = subscription.id
            try:
                from app.services.subscription_service import SubscriptionService

                await SubscriptionService().update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                    reset_reason=f'Автопродление {settings.get_cashera_display_name()}',
                )
            except Exception as sync_error:  # best-effort: продление уже в БД
                logger.warning(
                    'Cashera: синк панели после автопродления не удался',
                    error=str(sync_error),
                    subscription_id=subscription_id_for_log,
                    source=source,
                )
            return True

        if status in cr.CHARGE_FAILED:
            # CANCELLED не трогаем: иначе стирается отмена и выключается повторная
            # удалённая отмена при следующем успешном списании.
            record.charges_failed += 1
            if record.status != 'CANCELLED':
                record.status = 'PAST_DUE'
            await db.commit()
            await self._notify_cashera_recurring(db, record, 'failed')
            return True

        return True

    async def replay_missed_cashera_charges(self, db: AsyncSession, record_id: int) -> int:
        """Доначисляет оплаченные списания, чей вебхук не дошёл (по истории /charges).

        Идемпотентно по uuid списания. Возвращает число применённых списаний.
        """
        from sqlalchemy import select as sa_select

        from app.database.crud import cashera_subscription as sub_crud
        from app.database.models import Transaction

        record = await sub_crud.get_cashera_subscription_by_id(db, record_id)
        if record is None or not record.cashera_subscription_uuid:
            return 0
        try:
            charges = await cashera_service.list_subscription_charges(record.cashera_subscription_uuid)
        except Exception as error:
            logger.warning('Cashera: не удалось получить историю списаний', error=str(error), record_id=record_id)
            return 0

        paid = [c for c in charges if str(c.get('status') or '').lower() == 'paid' and c.get('uuid')]
        if not paid:
            return 0
        known = set(
            (
                await db.execute(
                    sa_select(Transaction.external_id).where(
                        Transaction.payment_method == PaymentMethod.CASHERA.value,
                        Transaction.external_id.in_([str(c['uuid']) for c in paid]),
                    )
                )
            ).scalars()
        )
        applied = 0
        for charge in sorted(paid, key=lambda c: str(c.get('paid_at') or c.get('created_at') or '')):
            if str(charge['uuid']) in known:
                continue
            locked = await sub_crud.get_cashera_subscription_by_id_for_update(db, record_id)
            if locked is None:
                break
            await self._apply_cashera_charge(db, locked, charge, source='replay')
            applied += 1
        if applied:
            logger.warning('Cashera: доначислены пропущенные списания', record_id=record_id, applied=applied)
        return applied


class _CasheraRecurrentAgent(CasheraPaymentMixin):
    """Минимальный носитель миксина для модульных точек входа автопродления."""

    def __init__(self, bot: Any = None) -> None:
        self.bot = bot


async def enable_cashera_recurring(
    db: AsyncSession,
    *,
    user_id: int,
    subscription: Any,
    tariff: Any,
) -> dict[str, Any]:
    """Включить автопродление Cashera: {local_id, cashera_subscription_uuid, redirect_url, status}.

    Пробрасывает ValueError (нет цены и т. п.), чтобы UI показал причину. Гейт на фичу.
    """
    if not settings.is_cashera_recurrent_enabled():
        raise RuntimeError('Cashera recurrent is disabled')
    if getattr(subscription, 'is_trial', False):
        raise ValueError('Автопродление Cashera недоступно для пробной подписки')
    return await _CasheraRecurrentAgent().create_cashera_recurrent_subscription(
        db, user_id=user_id, subscription=subscription, tariff=tariff
    )


async def purchase_tariff_with_cashera_recurring(db: AsyncSession, *, user: Any, tariff: Any) -> dict[str, Any]:
    """Покупка тарифа оплатой через автопродление Cashera.

    Зеркало Platega/Lava: нет подписки на тариф → EXPIRED-заготовка без доступа,
    первое списание её продлит и создаст panel-юзера. Отказы (ValueError): триал,
    DISABLED/PENDING, в single-режиме — подписка другого тарифа.
    """
    if not settings.is_cashera_recurrent_enabled():
        raise RuntimeError('Cashera recurrent is disabled')

    from app.database.crud.subscription import (
        create_sbp_pending_subscription,
        get_subscription_by_user_and_tariff,
        get_subscription_by_user_id,
    )

    if settings.is_multi_tariff_enabled():
        subscription = await get_subscription_by_user_and_tariff(db, user.id, tariff.id, include_inactive=True)
    else:
        subscription = await get_subscription_by_user_id(db, user.id)
        if subscription is not None and subscription.tariff_id != tariff.id:
            raise ValueError('Оформление через Cashera недоступно при подписке другого тарифа — оплатите с баланса')

    if subscription is not None:
        if getattr(subscription, 'is_trial', False):
            raise ValueError('Оформление через Cashera недоступно для триальной подписки — оплатите с баланса')
        if subscription.status in ('disabled', 'pending'):
            raise ValueError('Оформление через Cashera недоступно для этой подписки — оплатите с баланса')

    if subscription is None:
        subscription = await create_sbp_pending_subscription(db, user.id, tariff)

    result = await enable_cashera_recurring(db, user_id=user.id, subscription=subscription, tariff=tariff)
    return {**result, 'subscription_id': subscription.id}
