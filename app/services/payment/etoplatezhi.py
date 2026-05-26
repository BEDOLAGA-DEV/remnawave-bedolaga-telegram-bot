"""Mixin для интеграции с Etoplatezhi (paymentpage.etoplatezhi.ru)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.etoplatezhi_service import etoplatezhi_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов Etoplatezhi -> internal
ETOPLATEZHI_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'success': ('success', True),
    'decline': ('declined', False),
    'error': ('error', False),
    'processing': ('pending', False),
    'awaiting 3ds result': ('pending', False),
    'awaiting redirect result': ('pending', False),
    'awaiting clarification': ('pending', False),
    'awaiting customer action': ('pending', False),
    'cancelled': ('cancelled', False),
    'refunded': ('refunded', False),
    'partially refunded': ('partially_refunded', False),
    'reversed': ('reversed', False),
}


class EtoplatezhiPaymentMixin:
    """Mixin для работы с платежами Etoplatezhi."""

    async def create_etoplatezhi_payment(
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
        success_url: str | None = None,
        fail_url: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Создает платеж Etoplatezhi.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_etoplatezhi_enabled():
            logger.error('Etoplatezhi не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.ETOPLATEZHI_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Etoplatezhi: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                ETOPLATEZHI_MIN_AMOUNT_KOPEKS=settings.ETOPLATEZHI_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.ETOPLATEZHI_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Etoplatezhi: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                ETOPLATEZHI_MAX_AMOUNT_KOPEKS=settings.ETOPLATEZHI_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id
        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            # EtoPlatezhi anti-fraud declines transactions when customer_id
            # repeats across distinct guest payments. Generate a unique guest
            # tag per session so each cart-payment looks like a distinct buyer.
            user = None
            tg_id = f'guest{uuid.uuid4().hex[:10]}'

        # Генерируем уникальный order_id с telegram_id для удобного поиска
        order_id = f'etp{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.ETOPLATEZHI_CURRENCY

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
                webhook_url = f'{settings.WEBHOOK_URL.rstrip("/")}{settings.ETOPLATEZHI_WEBHOOK_PATH}'

            lifetime = settings.ETOPLATEZHI_PAYMENT_LIFETIME_MINUTES

            # Определяем force_payment_method по типу подметода.
            # Коды берутся из справочника ETO (ru_pm_codes.html). На нашем
            # проекте активны: card-partner, sberpay, yoomoney-wallet.
            force_method_map = {
                'sbp': 'sbp-qr',
                'card': 'card-partner',
                'sberpay': 'sberpay',
                'yoomoney': 'yoomoney-wallet',
            }
            force_method = force_method_map.get(payment_method_type or '')

            # Если включены рекуррентные платежи EtoPlatezhi — регистрируем карту
            # в этой же транзакции (stored_card_type=3). После успешного callback
            # сохраняем recurring.id в saved_payment_methods.
            register_recurring = bool(
                settings.ETOPLATEZHI_RECURRENT_ENABLED and settings.ETOPLATEZHI_RECURRENT_REQUIRED
            )

            # Строим URL для редиректа на платёжную страницу
            payment_url = etoplatezhi_service.build_payment_url(
                project_id=settings.ETOPLATEZHI_PROJECT_ID or 0,
                payment_id=order_id,
                payment_amount=amount_kopeks,
                payment_currency=currency,
                customer_id=str(tg_id),
                description=description,
                callback_url=webhook_url,
                success_url=success_url or return_url or settings.ETOPLATEZHI_RETURN_URL,
                fail_url=fail_url or return_url or settings.ETOPLATEZHI_RETURN_URL,
                force_payment_method=force_method,
                customer_email=email,
                language_code=language,
                register_recurring=register_recurring,
            )

            logger.info(
                'Etoplatezhi: сформирован URL платежа',
                order_id=order_id,
                payment_url=payment_url,
            )

            expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            # Сохраняем в БД
            etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
            local_payment = await etoplatezhi_crud.create_etoplatezhi_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=payment_method_type,
                etoplatezhi_payment_id=None,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'Etoplatezhi: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('Etoplatezhi: ошибка создания платежа', error=e)
            return None

    async def process_etoplatezhi_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает callback от Etoplatezhi.

        Подпись проверяется в webserver/payments.py до вызова этого метода.

        Args:
            db: Сессия БД
            payload: JSON тело callback (signature проверена в webserver)

        Returns:
            True если платеж успешно обработан
        """
        try:
            # Etoplatezhi callback structure:
            # {project_id, payment: {id, status, sum: {amount, currency}}, customer: {id}, signature}
            payment_data = payload.get('payment', {})
            etoplatezhi_payment_id = payment_data.get('id')
            etoplatezhi_status = payment_data.get('status')

            # payment.id в callback — это наш payment_id (order_id)
            our_payment_id = str(etoplatezhi_payment_id) if etoplatezhi_payment_id else None

            if not our_payment_id or not etoplatezhi_status:
                logger.warning('Etoplatezhi callback: отсутствуют обязательные поля', payload=payload)
                return False

            # Определяем is_paid по статусу
            is_confirmed = etoplatezhi_status == 'success'

            # Trial → paid auto-conversion: payment_id с префиксом trial_convert_
            # не имеет row в etoplatezhi_payments (charge инициируется через COF
            # endpoint, минуя Payment Page). Роутим в trial_conversion_service.
            from app.services.trial_conversion_service import (
                TRIAL_CONVERT_PAYMENT_PREFIX,
                convert_trial_to_paid_from_callback,
                parse_subscription_id,
            )

            if our_payment_id.startswith(TRIAL_CONVERT_PAYMENT_PREFIX):
                subscription_id = parse_subscription_id(our_payment_id)
                if not subscription_id:
                    logger.warning(
                        'Etoplatezhi trial_convert: невалидный payment_id',
                        payment_id=our_payment_id,
                    )
                    return False

                # Mirror the recurring/topup model: maintain an etoplatezhi_payments
                # row for every trial_convert webhook (decline/success), so admin
                # /admin/payments search via _search_etoplatezhi can surface them.
                # Trial-convert charges bypass Payment Page (COF endpoint), so we
                # create the row here in the webhook rather than at charge time.
                etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
                sum_data = payment_data.get('sum', {}) or {}
                amount_kopeks = int(sum_data.get('amount') or 0) or None

                from app.database.models import Subscription as _SubModel
                _sub_row = (
                    await db.execute(select(_SubModel).where(_SubModel.id == subscription_id))
                ).scalar_one_or_none()
                _user_id = _sub_row.user_id if _sub_row else None

                existing = await etoplatezhi_crud.get_etoplatezhi_payment_by_order_id(db, our_payment_id)
                if existing is None and amount_kopeks:
                    existing = await etoplatezhi_crud.create_etoplatezhi_payment(
                        db,
                        user_id=_user_id,
                        order_id=our_payment_id,
                        amount_kopeks=amount_kopeks,
                        description=f'Конверсия триала #{subscription_id}',
                        etoplatezhi_payment_id=str(etoplatezhi_payment_id) if etoplatezhi_payment_id else None,
                    )

                if not is_confirmed:
                    logger.info(
                        'Etoplatezhi trial_convert: статус не success, skip',
                        payment_id=our_payment_id,
                        status=etoplatezhi_status,
                    )
                    if existing:
                        await etoplatezhi_crud.update_etoplatezhi_payment_status(
                            db,
                            existing,
                            status=etoplatezhi_status or 'error',
                            etoplatezhi_payment_id=str(etoplatezhi_payment_id) if etoplatezhi_payment_id else None,
                        )
                    return True

                ok = await convert_trial_to_paid_from_callback(
                    db,
                    subscription_id=subscription_id,
                    amount_kopeks=amount_kopeks,
                    provider='etoplatezhi',
                    provider_payment_id=str(etoplatezhi_payment_id),
                )
                if ok and existing:
                    await etoplatezhi_crud.update_etoplatezhi_payment_status(
                        db,
                        existing,
                        status='success',
                        is_paid=True,
                        etoplatezhi_payment_id=str(etoplatezhi_payment_id) if etoplatezhi_payment_id else None,
                    )
                if ok:
                    await db.commit()
                return ok

            # Ищем платеж по order_id (наш payment_id = order_id)
            etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
            payment = await etoplatezhi_crud.get_etoplatezhi_payment_by_order_id(db, our_payment_id)

            if not payment:
                logger.warning(
                    'Etoplatezhi callback: платеж не найден',
                    payment_id=our_payment_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await etoplatezhi_crud.get_etoplatezhi_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Etoplatezhi: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('Etoplatezhi callback: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = ETOPLATEZHI_STATUS_MAP.get(etoplatezhi_status, ('pending', False))
            internal_status, is_paid = status_info

            # Если статус success, принудительно считаем оплаченным
            if is_confirmed:
                is_paid = True
                internal_status = 'success'

            # Извлекаем сумму из callback: payment.sum.amount (в минорных единицах)
            sum_data = payment_data.get('sum', {})

            callback_payload = {
                'etoplatezhi_payment_id': etoplatezhi_payment_id,
                'status': etoplatezhi_status,
                'sum': sum_data,
                'customer': payload.get('customer'),
                'project_id': payload.get('project_id'),
            }

            # Проверка суммы ДО обновления статуса
            if is_paid:
                amount_value = sum_data.get('amount')
                if amount_value is not None:
                    # amount в минорных единицах (копейках)
                    received_kopeks = int(amount_value)
                    if abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'Etoplatezhi amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await etoplatezhi_crud.update_etoplatezhi_payment_status(
                            db=db,
                            payment=payment,
                            status='amount_mismatch',
                            is_paid=False,
                            callback_payload=callback_payload,
                        )
                        return False

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.etoplatezhi_payment_id = str(etoplatezhi_payment_id) if etoplatezhi_payment_id else None
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()

                # Persist saved card for recurring charges if the platform
                # included a ``recurring`` block in the callback (i.e. the
                # initial payment was registered with stored_card_type=3).
                await self._save_etoplatezhi_recurring_card(db, payment, payload)

                return await self._finalize_etoplatezhi_payment(db, payment, trigger='webhook')

            # Для не-success статусов можно безопасно коммитить
            payment = await etoplatezhi_crud.update_etoplatezhi_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('Etoplatezhi callback: ошибка обработки', error=e)
            return False

    async def _save_etoplatezhi_recurring_card(
        self,
        db: AsyncSession,
        payment: Any,
        payload: dict[str, Any],
    ) -> None:
        """Если в callback'е есть ``recurring`` → создаём saved-card запись.

        Вызывается только после успешного платежа. Промахи логируются,
        но не валят основную обработку платежа.
        """
        if not settings.ETOPLATEZHI_RECURRENT_ENABLED:
            return

        recurring = payload.get('recurring') if isinstance(payload, dict) else None
        if not isinstance(recurring, dict):
            return
        recurring_id = recurring.get('id')
        if recurring_id in (None, ''):
            return

        user_id = getattr(payment, 'user_id', None)
        if not user_id:
            # Guest landing flow: user is created later in fulfill_purchase.
            # Stash the recurring + account block in payment.metadata_json so the
            # guest fulfillment step can pick it up and create the saved card
            # once user_id is resolved.
            try:
                existing_metadata = payment.metadata_json or {}
                if not isinstance(existing_metadata, dict):
                    existing_metadata = {}
                existing_metadata['recurring'] = recurring
                existing_metadata['account'] = payload.get('account') if isinstance(payload, dict) else None
                # Capture terminal.method_code too — guest fulfillment uses it to
                # pick the correct recurring endpoint (card-partner/sberpay/yoomoney).
                terminal_stash = payload.get('terminal') if isinstance(payload, dict) else None
                if isinstance(terminal_stash, dict) and terminal_stash.get('method_code'):
                    existing_metadata['method_code'] = terminal_stash.get('method_code')
                payment.metadata_json = existing_metadata
                # Explicit flush — AsyncSessionLocal uses autoflush=False, so
                # the subsequent SELECT in _maybe_save_etoplatezhi_card_from_guest_payment
                # would otherwise read the pre-stash row and silently skip card creation.
                await db.flush()
                logger.info(
                    'Etoplatezhi: recurring data сохранён в metadata для guest платежа — карта создастся в fulfill',
                    order_id=getattr(payment, 'order_id', None),
                    recurring_id=recurring_id,
                )
            except Exception as e:
                logger.warning(
                    'Etoplatezhi: не удалось сохранить recurring data в metadata',
                    order_id=getattr(payment, 'order_id', None),
                    error=e,
                )
            return

        account = payload.get('account') if isinstance(payload, dict) else None
        if not isinstance(account, dict):
            account = {}

        # EtoPlatezhi has distinct recurring endpoints per payment method —
        # capture terminal.method_code so the recurring provider can route
        # charges correctly (card-partner / sberpay / yoomoney-wallet).
        terminal = payload.get('terminal') if isinstance(payload, dict) else None
        method_code = None
        if isinstance(terminal, dict):
            method_code = terminal.get('method_code') or None

        number = account.get('number') or ''
        card_first6 = number[:6] if len(number) >= 6 else None
        card_last4 = number[-4:] if len(number) >= 4 else None
        card_type = (account.get('type') or '').lower() or None
        expiry_month = account.get('expiry_month')
        expiry_year = account.get('expiry_year')
        card_holder = account.get('card_holder')

        title = None
        if card_last4:
            type_label = (card_type or 'card').capitalize()
            title = f'{type_label} *{card_last4}'
        elif card_holder:
            title = str(card_holder)

        valid_thru_raw = recurring.get('valid_thru')
        valid_thru = None
        if isinstance(valid_thru_raw, str) and valid_thru_raw:
            try:
                valid_thru = datetime.fromisoformat(valid_thru_raw.replace('Z', '+00:00'))
            except ValueError:
                valid_thru = None

        from app.database.crud.saved_payment_method import create_saved_payment_method

        try:
            # commit=False keeps the FOR UPDATE lock on the payment row held by
            # the caller (process_etoplatezhi_callback) until _finalize_etoplatezhi_payment
            # issues its single commit. Otherwise a concurrent webhook delivery
            # could reprocess the same payment between save_card and finalize.
            saved = await create_saved_payment_method(
                db=db,
                user_id=user_id,
                provider='etoplatezhi',
                provider_token=str(recurring_id),
                method_type='bank_card',
                card_first6=card_first6,
                card_last4=card_last4,
                card_type=card_type,
                card_expiry_month=str(expiry_month) if expiry_month is not None else None,
                card_expiry_year=str(expiry_year) if expiry_year is not None else None,
                title=title,
                valid_thru=valid_thru,
                method_code=method_code,
                commit=False,
            )
            if saved:
                logger.info(
                    'Etoplatezhi: карта сохранена для рекуррентных платежей',
                    saved_method_id=saved.id,
                    user_id=user_id,
                    recurring_id=str(recurring_id),
                    card_last4=card_last4,
                )
        except Exception as e:  # pragma: no cover - safety net
            logger.warning(
                'Etoplatezhi: не удалось сохранить карту для рекуррентных платежей',
                user_id=user_id,
                recurring_id=str(recurring_id),
                error=e,
            )

    async def _finalize_etoplatezhi_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock must be acquired by the caller before invoking this method.
        """
        payment_module = import_module('app.services.payment_service')
        etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'Etoplatezhi платеж уже связан с транзакцией',
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
            provider_payment_id=payment.order_id,
            provider_name='etoplatezhi',
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
            logger.error('Пользователь не найден для Etoplatezhi', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = payment.order_id

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.ETOPLATEZHI,
            )

        display_name = settings.get_etoplatezhi_display_name()
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
                payment_method=PaymentMethod.ETOPLATEZHI,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await etoplatezhi_crud.link_etoplatezhi_payment_to_transaction(
            db, payment=payment, transaction_id=transaction.id
        )

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Etoplatezhi платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.ETOPLATEZHI,
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
            logger.error('Ошибка обработки реферального пополнения Etoplatezhi', error=error)

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
                logger.error('Ошибка отправки админ уведомления Etoplatezhi', error=error)

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
                logger.error('Ошибка отправки уведомления пользователю Etoplatezhi', error=error)

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
            'Обработан Etoplatezhi платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True
