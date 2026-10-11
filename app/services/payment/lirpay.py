"""Mixin для интеграции с LirPay (Integration API v2, lirpay.org)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.lirpay_service import amount_to_kopeks, lirpay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Публичный статус payment link LirPay -> (внутренний статус, зачислять ли баланс)
LIRPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'active': ('pending', False),
    'in_progress': ('pending', False),
    'paid': ('success', True),
    'expired': ('expired', False),
}


class LirPayPaymentMixin:
    """Mixin для работы с платежами LirPay."""

    async def create_lirpay_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        return_url: str | None = None,
        fail_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Создаёт payment link LirPay и возвращает данные для перехода на оплату.

        Способ оплаты (СБП/крипта/баланс LolzTeam) покупатель выбирает на странице LirPay
        (method_mode=multi). Результат приходит вебхуком на URL, настроенный
        в кабинете LirPay (PUT /webhook), поэтому ``return_url`` только
        возвращает покупателя в бот после оплаты.
        """
        if not settings.is_lirpay_enabled():
            logger.error('LirPay не настроен')
            return None

        if amount_kopeks < settings.LIRPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'LirPay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                LIRPAY_MIN_AMOUNT_KOPEKS=settings.LIRPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.LIRPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'LirPay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                LIRPAY_MAX_AMOUNT_KOPEKS=settings.LIRPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        project_id = (settings.LIRPAY_PROJECT_ID or '').strip()
        if not project_id:
            logger.error('LirPay: не задан LIRPAY_PROJECT_ID (UUID одобренного проекта)')
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            tg_id = 'guest'

        order_id = f'lp{tg_id}_{uuid.uuid4().hex[:8]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.LIRPAY_CURRENCY
        customer_id = str(tg_id) if tg_id != 'guest' else f'guest-{order_id[-8:]}'

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
            'customer_id': customer_id,
        }

        try:
            api_result = await lirpay_service.create_payment_link(
                amount_kopeks=amount_kopeks,
                project_id=project_id,
                display_name=description[:255] if description else 'Пополнение баланса',
                currency=currency,
                customer_id=customer_id,
                # Idempotency-Key = наш order_id: повтор запроса после сетевого
                # сбоя вернёт ту же ссылку, а не создаст второй счёт.
                idempotency_key=order_id,
            )

            lirpay_payment_id = api_result.get('public_id')
            payment_url = api_result.get('payment_link')

            # LirPay сам задаёт срок жизни ссылки — не храним свой.
            expires_at = None

            if api_result.get('test_mode') or api_result.get('status') == 'succeeded':
                # TEST-ключ «оплачивает» счёт сразу без денег. Реальный баланс
                # по такой ссылке начислять нельзя — см. process_lirpay_callback.
                logger.warning(
                    'LirPay: создан ТЕСТОВЫЙ счёт — баланс по нему начислен не будет',
                    order_id=order_id,
                )

            lirpay_crud = import_module('app.database.crud.lirpay')
            local_payment = await lirpay_crud.create_lirpay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=None,
                lirpay_payment_id=str(lirpay_payment_id) if lirpay_payment_id else None,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'LirPay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                payment_method='multi',
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'payment_id': str(lirpay_payment_id) if lirpay_payment_id else None,
                'expires_at': expires_at.isoformat() if expires_at else None,
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('LirPay: ошибка создания платежа', error=e)
            return None

    async def process_lirpay_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Обрабатывает вебхук LirPay (подпись уже проверена в webserver).

        События: ``payment.succeeded`` / ``payment.failed`` /
        ``payment.expired`` / ``payment.refund_required`` /
        ``payment.refunded`` / ``payment.chargeback``. Точная схема тела в
        доках не зафиксирована, поэтому идентификатор и сумма ищутся и в
        корне, и во вложенных объектах ``payment`` / ``data`` /
        ``payment_link``. События не по нашим счетам подтверждаются сразу,
        чтобы LirPay не повторял доставку в пустоту.
        """
        try:
            event = str(payload.get('type') or payload.get('event') or '')
            data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
            source = payload.get('payment') if isinstance(payload.get('payment'), dict) else data
            if not source:
                source = payload.get('payment_link') if isinstance(payload.get('payment_link'), dict) else {}
            # События балансов, выпусков и конверсий к счетам отношения не имеют.
            if event and not event.startswith('payment.'):
                logger.info('LirPay callback: событие не по платежу, пропускаем', lirpay_event=event)
                return True
            # Внутренний служебный ключ, проставляется вебхук-маршрутом
            # (значение заголовка X-Lirpay-Mode) — читаем и убираем, чтобы он
            # не попал в бизнес-поля и сохранённый callback_payload.
            mode_from_header = str(payload.pop('_lirpay_mode', '') or '').lower()

            def _field(*names: str) -> Any:
                for name in names:
                    for scope_dict in (source, payload):
                        if isinstance(scope_dict, dict) and scope_dict.get(name) is not None:
                            return scope_dict[name]
                return None

            # customer_id — наша ссылка на плательщика (tg_id или guest-*),
            # public_id/id — идентификатор ссылки. Наш order_id в теле не
            # приходит, поэтому матчим по customer_id + сумме, а при
            # расхождении — по public_id, сохранённому при создании.
            our_order_id = _field('order_id', 'orderId', 'external_id')
            lirpay_payment_id = _field('public_id', 'payment_link_id', 'id')
            customer_id = _field('customer_id', 'customerId')
            amount_raw = _field('amount', 'expected_amount', 'paid_amount')

            if not lirpay_payment_id and not our_order_id and not customer_id:
                logger.warning('LirPay callback: нет идентификаторов платежа', payload=payload)
                return False

            lirpay_crud = import_module('app.database.crud.lirpay')
            payment = None
            if our_order_id:
                payment = await lirpay_crud.get_lirpay_payment_by_order_id(db, str(our_order_id))
            if payment is None and lirpay_payment_id:
                payment = await lirpay_crud.get_lirpay_payment_by_invoice_id(db, str(lirpay_payment_id))

            fallback_ambiguous = False
            if payment is None and customer_id:
                # Ищем единственный свежий неоплаченный платёж этого покупателя
                # на такую сумму: наш order_id в теле вебхука не приходит, а
                # customer_id мы передаём при создании ссылки. Окно 24ч — не
                # весь исторический хвост неоплаченных.
                from datetime import UTC, datetime, timedelta

                from sqlalchemy import select

                from app.database.models import LirPayPayment

                received_kopeks = amount_to_kopeks(amount_raw)
                if received_kopeks is not None:
                    try:
                        cutoff = datetime.now(UTC) - timedelta(hours=24)
                        result = await db.execute(
                            select(LirPayPayment)
                            .where(LirPayPayment.is_paid == False)
                            .where(LirPayPayment.created_at >= cutoff)
                        )
                        candidates = [
                            candidate
                            for candidate in result.scalars().all()
                            if candidate.amount_kopeks == received_kopeks
                            and str((candidate.metadata_json or {}).get('customer_id')) == str(customer_id)
                        ]
                        if len(candidates) == 1:
                            payment = candidates[0]
                        elif len(candidates) > 1:
                            # Несколько ссылок на одну сумму: деньги прошли, но
                            # привязать однозначно нельзя — не подтверждаем
                            # доставку, пусть провайдер повторит, а сверка
                            # найдёт счёт по public_id.
                            fallback_ambiguous = True
                            logger.error(
                                'LirPay callback: неоднозначный матч по customer_id+сумме',
                                customer_id=customer_id,
                                amount_kopeks=received_kopeks,
                                candidates=[candidate.order_id for candidate in candidates],
                            )
                    except Exception as search_error:
                        # Фолбэк не должен ломать доставку: не нашли — работаем
                        # как при «платёж не найден» ниже.
                        logger.warning(
                            'LirPay callback: fallback-поиск по customer_id не удался',
                            customer_id=customer_id,
                            error=str(search_error),
                        )

            if payment is None:
                if fallback_ambiguous:
                    # Деньги прошли, но однозначно привязать их не вышло —
                    # просим повторную доставку, а не молча подтверждаем.
                    return False
                # Чужой счёт повторами не появится — подтверждаем доставку.
                logger.warning(
                    'LirPay callback: платеж не найден',
                    order_id=our_order_id,
                    public_id=lirpay_payment_id,
                    customer_id=customer_id,
                )
                return True

            # Блокируем строку ДО применения события: вебхук и фоновая сверка
            # могут прийти одновременно, и без FOR UPDATE двойное зачисление
            # останавливал бы только UNIQUE(external_id, payment_method).
            locked = await lirpay_crud.get_lirpay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('LirPay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Первая проверка под блокировкой: оплаченный платёж поздние
            # expired/failed/test-события не перезаписывают; возврат идёт
            # своей веткой ниже.
            if payment.is_paid and event != 'payment.succeeded':
                if event not in ('payment.refund_required', 'payment.refunded', 'payment.chargeback'):
                    logger.info(
                        'LirPay callback: платеж уже оплачен, событие не трогает статус',
                        order_id=payment.order_id,
                        lirpay_event=event,
                    )
                    return True

            # Режим ключа: live-события приходят только live-ключу, но
            # тестовое окружение «оплачивает» эмулятором без денег — реальный
            # баланс по таким событиям начислять нельзя.
            mode = str(_field('mode') or mode_from_header or '').lower()
            is_test = bool(payload.get('test_mode') or source.get('test_mode') or mode == 'test')
            if is_test:
                logger.error(
                    'LirPay callback: ТЕСТОВЫЙ платёж, баланс не начисляем',
                    order_id=payment.order_id,
                )
                await lirpay_crud.update_lirpay_payment_status(
                    db=db,
                    payment=payment,
                    status='error',
                    is_paid=False,
                    callback_payload=payload,
                )
                return True

            if event in ('payment.refund_required', 'payment.refunded', 'payment.chargeback'):
                # Деньги вернулись покупателю уже после зачисления: списывать
                # автоматически нельзя, но возврат должен быть виден.
                logger.error(
                    'LirPay: возврат/чарджбэк по платежу, требуется ручная сверка баланса',
                    order_id=payment.order_id,
                    user_id=payment.user_id,
                    amount_kopeks=payment.amount_kopeks,
                    lirpay_event=event,
                )
                await lirpay_crud.update_lirpay_payment_status(
                    db=db,
                    payment=payment,
                    status='refunded',
                    is_paid=None,
                    callback_payload=payload,
                )
                return True

            if event == 'payment.failed' or 'failed' in str(_field('status') or '').lower():
                await lirpay_crud.update_lirpay_payment_status(
                    db=db,
                    payment=payment,
                    status='declined',
                    is_paid=False,
                    callback_payload=payload,
                )
                return True

            if event == 'payment.expired' or str(_field('status') or '').lower() == 'expired':
                await lirpay_crud.update_lirpay_payment_status(
                    db=db,
                    payment=payment,
                    status='expired',
                    is_paid=False,
                    callback_payload=payload,
                )
                return True

            if event == 'payment.succeeded' or str(_field('status') or '').lower() == 'paid':
                return await self._apply_lirpay_success(
                    db,
                    payment=payment,
                    payload=payload,
                    amount_raw=amount_raw,
                    lirpay_payment_id=str(lirpay_payment_id) if lirpay_payment_id else None,
                    currency_raw=_field('currency', 'currency_code'),
                )

            # Неизвестное событие/статус — подтверждаем и логируем.
            logger.warning(
                'LirPay callback: неизвестное событие',
                order_id=payment.order_id,
                lirpay_event=event,
                status=_field('status'),
            )
            return True

        except Exception as e:
            logger.exception('LirPay callback: ошибка обработки', error=e)
            return False

    async def _apply_lirpay_success(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        payload: dict[str, Any],
        amount_raw: Any,
        lirpay_payment_id: str | None,
        currency_raw: Any = None,
    ) -> bool:
        """Сверяет сумму и валюту, зачисляет оплату. Блокировка строки уже взята."""
        lirpay_crud = import_module('app.database.crud.lirpay')

        # Учёт бота рублёвый: сумма в чужой валюте не интерпретируется в
        # копейки — сверяем валюту раньше суммы.
        received_currency = str(currency_raw or '').strip().upper()
        expected_currency = (payment.currency or 'RUB').strip().upper()
        if received_currency and received_currency != expected_currency:
            logger.error(
                'LirPay currency mismatch',
                expected=expected_currency,
                received=received_currency,
                order_id=payment.order_id,
            )
            await lirpay_crud.update_lirpay_payment_status(
                db=db,
                payment=payment,
                status='amount_mismatch',
                is_paid=False,
                callback_payload=payload,
            )
            return False

        received_kopeks = amount_to_kopeks(amount_raw)
        if received_kopeks is None:
            # «Не смогли проверить» не равно «сошлось»: оставляем счёт
            # под ретрай и фоновую сверку, отвечаем не-2xx.
            logger.error(
                'LirPay callback: оплата без разбираемой суммы, зачисление отменено',
                order_id=payment.order_id,
                received=amount_raw,
            )
            return False

        if received_kopeks != payment.amount_kopeks:
            logger.error(
                'LirPay amount mismatch',
                expected_kopeks=payment.amount_kopeks,
                received_kopeks=received_kopeks,
                order_id=payment.order_id,
            )
            await lirpay_crud.update_lirpay_payment_status(
                db=db,
                payment=payment,
                status='amount_mismatch',
                is_paid=False,
                callback_payload=payload,
            )
            return False

        if payment.is_paid:
            logger.info('LirPay callback: платеж уже оплачен', order_id=payment.order_id)
            return True

        payment.status = 'success'
        payment.is_paid = True
        payment.paid_at = datetime.now(UTC)
        if lirpay_payment_id:
            payment.lirpay_payment_id = lirpay_payment_id
        payment.callback_payload = payload
        payment.updated_at = datetime.now(UTC)
        await db.flush()

        return await self._finalize_lirpay_payment(db, payment, trigger='webhook')

    async def _finalize_lirpay_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления."""
        payment_module = import_module('app.services.payment_service')
        lirpay_crud = import_module('app.database.crud.lirpay')

        if payment.transaction_id:
            logger.info(
                'LirPay платеж уже связан с транзакцией',
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
            provider_name='lirpay',
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
            logger.error('Пользователь не найден для LirPay', user_id=payment.user_id)
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
                PaymentMethod.LIRPAY,
            )

        display_name = settings.get_lirpay_display_name()
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
                payment_method=PaymentMethod.LIRPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await lirpay_crud.link_lirpay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('LirPay платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.LIRPAY,
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
            logger.error('Ошибка обработки реферального пополнения LirPay', error=error)

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
                logger.error('Ошибка отправки админ уведомления LirPay', error=error)

        if getattr(self, 'bot', None) and user.telegram_id and settings.is_notifications_enabled():
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
                logger.error('Ошибка отправки уведомления пользователю LirPay', error=error)

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
            'Обработан LirPay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_lirpay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус ссылки через API LirPay и синхронизирует БД.

        Страховка на случай потерянного вебхука: ручная проверка из админки
        и фоновая сверка доначисляют оплаченные счета.
        """
        try:
            lirpay_crud = import_module('app.database.crud.lirpay')
            payment = await lirpay_crud.get_lirpay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('LirPay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {'payment': payment, 'status': 'success', 'is_paid': True}

            if payment.status in LIRPAY_FINAL_STATUSES:
                return {'payment': payment, 'status': payment.status, 'is_paid': False}

            try:
                status_data = await lirpay_service.get_payment_link(payment.lirpay_payment_id)
                if not status_data:
                    logger.warning(
                        'LirPay API check: ссылка не найдена на стороне провайдера',
                        order_id=payment.order_id,
                    )
                    return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

                lirpay_status = (status_data.get('status') or '').strip().lower()
                internal_status, is_paid = LIRPAY_STATUS_MAP.get(lirpay_status, ('pending', False))

                if is_paid and lirpay_service.is_test_key():
                    # Тестовый ключ «оплачивает» эмулятором без денег — реальный
                    # баланс по таким счетам начислять нельзя. Вебхук-путь ловит
                    # это по X-Lirpay-Mode/полю test, но ответ GET /payment-links
                    # режима не несёт — сверяемся с префиксом нашего ключа.
                    logger.error(
                        'LirPay API check: ТЕСТОВЫЙ платёж (ключ lpk_test_), баланс не начисляем',
                        order_id=payment.order_id,
                    )
                    await lirpay_crud.update_lirpay_payment_status(
                        db=db,
                        payment=payment,
                        status='error',
                        is_paid=False,
                        callback_payload={'check_source': 'api', 'lirpay_status_data': status_data},
                    )
                    return {'payment': payment, 'status': 'error', 'is_paid': False}

                if not is_paid:
                    if internal_status != payment.status:
                        payment = await lirpay_crud.update_lirpay_payment_status(
                            db=db,
                            payment=payment,
                            status=internal_status,
                            is_paid=False,
                        )
                    return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

                # Сверяем сумму и валюту так же строго, как в вебхуке.
                received_currency = str(status_data.get('currency') or '').strip().upper()
                expected_currency = (payment.currency or 'RUB').strip().upper()
                if received_currency and received_currency != expected_currency:
                    logger.error(
                        'LirPay currency mismatch (API check)',
                        expected=expected_currency,
                        received=received_currency,
                        order_id=payment.order_id,
                    )
                    await lirpay_crud.update_lirpay_payment_status(
                        db=db,
                        payment=payment,
                        status='amount_mismatch',
                        is_paid=False,
                        callback_payload={'check_source': 'api', 'lirpay_status_data': status_data},
                    )
                    return {'payment': payment, 'status': 'amount_mismatch', 'is_paid': False}

                amount_raw = status_data.get('amount', status_data.get('expected_amount'))
                received_kopeks = amount_to_kopeks(amount_raw)
                if received_kopeks is None:
                    logger.error(
                        'LirPay API check: paid без разбираемой суммы, зачисление отменено',
                        order_id=payment.order_id,
                    )
                    return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

                if received_kopeks != payment.amount_kopeks:
                    logger.error(
                        'LirPay amount mismatch (API check)',
                        expected_kopeks=payment.amount_kopeks,
                        received_kopeks=received_kopeks,
                        order_id=payment.order_id,
                    )
                    await lirpay_crud.update_lirpay_payment_status(
                        db=db,
                        payment=payment,
                        status='amount_mismatch',
                        is_paid=False,
                        callback_payload={'check_source': 'api', 'lirpay_status_data': status_data},
                    )
                    return {'payment': payment, 'status': 'amount_mismatch', 'is_paid': False}

                locked = await lirpay_crud.get_lirpay_payment_by_id_for_update(db, payment.id)
                if not locked:
                    logger.error('LirPay: не удалось заблокировать платёж', payment_id=payment.id)
                    return None
                payment = locked

                if payment.is_paid:
                    logger.info('LirPay платеж уже обработан (api_check)', order_id=payment.order_id)
                    return {'payment': payment, 'status': 'success', 'is_paid': True}

                logger.info('LirPay payment confirmed via API', order_id=payment.order_id)

                payment.status = 'success'
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.callback_payload = {'check_source': 'api', 'lirpay_status_data': status_data}
                payment.updated_at = datetime.now(UTC)
                await db.flush()

                await self._finalize_lirpay_payment(db, payment, trigger='api_check')

                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

            except Exception as e:
                logger.error('Error checking LirPay payment status via API', error=e)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

        except Exception as e:
            logger.exception('LirPay: ошибка проверки статуса', error=e)
            return None


# Терминальные неуспешные статусы: «expired» сюда НЕ входит — фоновая сверка
# может найти уже оплаченный счёт позже локального таймаута, и деньги
# нельзя забрать без зачисления.
LIRPAY_FINAL_STATUSES = frozenset({'amount_mismatch', 'declined', 'refunded', 'error'})

# Статусы, при которых счёт ещё может быть оплачен (фоновая сверка по API).
LIRPAY_PENDING_STATUSES = frozenset({'pending'})
