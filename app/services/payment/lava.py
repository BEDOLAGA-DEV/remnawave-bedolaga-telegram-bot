"""Mixin для интеграции с Lava Business (gate.lava.ru)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.lava_service import lava_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг sub-method -> includeService для фильтрации методов на странице оплаты Lava
LAVA_INCLUDE_SERVICE_MAP: dict[str | None, list[str] | None] = {
    None: None,
    'card': ['card'],
    'sbp': ['sbp'],
}


# Маппинг статусов Lava -> internal
LAVA_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'created': ('pending', False),
    'pending': ('pending', False),
    'processing': ('pending', False),  # на случай промежуточного статуса
    'success': ('success', True),
    'cancel': ('cancelled', False),
    'cancelled': ('cancelled', False),
    'expired': ('expired', False),
    'error': ('error', False),
    'failed': ('failed', False),
}


class LavaPaymentMixin:
    """Mixin для работы с платежами Lava Business."""

    async def create_lava_payment(
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
        """Создаёт инвойс Lava."""
        if not settings.is_lava_enabled():
            logger.error('Lava не настроен')
            return None

        if amount_kopeks < settings.LAVA_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Lava: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                LAVA_MIN_AMOUNT_KOPEKS=settings.LAVA_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.LAVA_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Lava: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                LAVA_MAX_AMOUNT_KOPEKS=settings.LAVA_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            user = None
            tg_id = 'guest'

        # 32 hex char (128 бит) суффикс — order_id уникален даже при публичном tg_id
        order_id = f'lava{tg_id}_{uuid.uuid4().hex}'
        amount_rubles = amount_kopeks / 100
        currency = settings.LAVA_CURRENCY

        method_key = (payment_method_type or '').lower() or None
        include_service = LAVA_INCLUDE_SERVICE_MAP.get(method_key)

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
            'payment_method_type': method_key,
            'email': email,
        }

        try:
            hook_url = self._build_lava_hook_url()
            if not hook_url:
                logger.warning(
                    'Lava: hook_url не сконфигурирован — '
                    'платёж создаётся, но автоматическое подтверждение через webhook невозможно. '
                    'Установите WEBHOOK_URL / WEB_API_BASE_URL / CABINET_URL.'
                )
            actual_return_url = return_url or settings.LAVA_RETURN_URL

            api_result = await lava_service.create_invoice(
                amount_rubles=amount_rubles,
                order_id=order_id,
                hook_url=hook_url,
                success_url=actual_return_url,
                fail_url=actual_return_url,
                expire_minutes=settings.LAVA_PAYMENT_LIFETIME_MINUTES,
                comment=(description or '')[:255] or None,
                custom_fields=str(user_id) if user_id is not None else None,
                include_service=include_service,
            )

            data = (api_result.get('data') or api_result) if isinstance(api_result, dict) else {}
            lava_invoice_id = data.get('id') or data.get('invoice_id')
            payment_url = data.get('url') or data.get('payment_url')
            expired_str = data.get('expired')

            if not payment_url:
                # Без URL у пользователя нет способа оплатить — это аномалия Lava API.
                # Row не сохраняем, чтобы не плодить «зависшие» pending-инвойсы без реквизитов.
                logger.error(
                    'Lava: ответ API без payment URL, инвойс не создан',
                    order_id=order_id,
                    lava_invoice_id=lava_invoice_id,
                    response_keys=list(data.keys()) if isinstance(data, dict) else None,
                )
                return None

            logger.info(
                'Lava: получен ответ API',
                order_id=order_id,
                lava_invoice_id=lava_invoice_id,
                payment_url=payment_url,
            )

            lifetime = settings.LAVA_PAYMENT_LIFETIME_MINUTES
            expires_at = self._parse_lava_expired(expired_str) or (datetime.now(UTC) + timedelta(minutes=lifetime))

            lava_crud = import_module('app.database.crud.lava')
            local_payment = await lava_crud.create_lava_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=method_key,
                lava_invoice_id=str(lava_invoice_id) if lava_invoice_id else None,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'Lava: создан платеж',
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
                'payment_id': str(lava_invoice_id) if lava_invoice_id else None,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('Lava: ошибка создания платежа', error=e)
            return None

    @staticmethod
    def _parse_lava_expired(value: Any) -> datetime | None:
        """Парсит поле ``expired`` из ответа Lava.

        Принимаются только TZ-aware строки (ISO с offset/Z) или unix timestamp.
        Naive-строки игнорируются — TZ Lava в спеке не задокументирована,
        а угадывание UTC может сместить срок жизни инвойса на несколько часов.
        Если парсинг не удался — caller использует fallback ``now + lifetime``.
        """
        if value is None or value == '':
            return None
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value), tz=UTC)
            except (ValueError, OSError):
                return None
        if isinstance(value, str):
            try:
                from dateutil.parser import isoparse  # type: ignore[import-not-found]

                parsed = isoparse(value)
                if parsed.tzinfo is None:
                    return None  # без TZ доверять не можем
                return parsed
            except Exception:
                return None
        return None

    @staticmethod
    def _build_lava_hook_url() -> str | None:
        """Собирает абсолютный URL вебхука для Lava."""
        webhook_path = settings.LAVA_WEBHOOK_PATH or '/lava-webhook'
        base = (
            getattr(settings, 'WEBHOOK_URL', None)
            or getattr(settings, 'WEB_API_BASE_URL', None)
            or getattr(settings, 'CABINET_URL', None)
        )
        if not base:
            return None
        suffix = webhook_path if webhook_path.startswith('/') else f'/{webhook_path}'
        return f'{base.rstrip("/")}{suffix}'

    async def process_lava_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Обрабатывает webhook от Lava (подпись уже проверена в webserver)."""
        try:
            lava_invoice_id = payload.get('invoice_id')
            our_order_id = payload.get('order_id')
            lava_status = (payload.get('status') or '').strip().lower()
            pay_service = payload.get('pay_service')

            if not our_order_id or not lava_status:
                logger.warning('Lava webhook: отсутствуют обязательные поля')
                return False

            lava_crud = import_module('app.database.crud.lava')
            payment = await lava_crud.get_lava_payment_by_order_id(db, our_order_id)
            if not payment:
                # Fallback по invoice_id, но строго проверяем совпадение order_id
                if lava_invoice_id:
                    payment = await lava_crud.get_lava_payment_by_invoice_id(db, str(lava_invoice_id))
                    if payment and payment.order_id != our_order_id:
                        logger.error(
                            'Lava webhook: order_id mismatch',
                            webhook_order_id=our_order_id,
                            record_order_id=payment.order_id,
                            invoice_id=lava_invoice_id,
                        )
                        return False
                if not payment:
                    logger.warning('Lava webhook: платеж не найден', order_id=our_order_id)
                    return False

            locked = await lava_crud.get_lava_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Lava: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            if payment.is_paid:
                logger.info('Lava webhook: платеж уже обработан', order_id=payment.order_id)
                return True

            # Терминальные неуспешные статусы — стики, защита от повторного успеха
            if payment.status in {'amount_mismatch', 'cancelled', 'cancel', 'error', 'expired', 'failed'}:
                # Если внезапно пришёл success после терминальной неудачи — это сигнал
                # подделки или ошибки на стороне Lava, эскалируем.
                if lava_status == 'success':
                    logger.error(
                        'Lava webhook: success на терминально-неуспешном платеже, игнорируется',
                        order_id=payment.order_id,
                        current_status=payment.status,
                    )
                else:
                    logger.warning(
                        'Lava webhook: платёж в терминальном неуспешном статусе, игнорируется',
                        order_id=payment.order_id,
                        current_status=payment.status,
                        incoming_status=lava_status,
                    )
                return True

            if lava_status not in LAVA_STATUS_MAP:
                logger.warning(
                    'Lava webhook: неизвестный статус, обрабатываем как pending',
                    order_id=payment.order_id,
                    incoming_status=lava_status,
                )
            internal_status, is_paid = LAVA_STATUS_MAP.get(lava_status, ('pending', False))

            callback_payload = {
                'lava_invoice_id': lava_invoice_id,
                'status': lava_status,
                'amount': payload.get('amount'),
                'credited': payload.get('credited'),
                'pay_service': pay_service,
                'pay_time': payload.get('pay_time'),
                'payer_details': payload.get('payer_details'),
                'custom_fields': payload.get('custom_fields'),
            }

            # Сверяем сумму ДО зачисления
            if is_paid:
                # Lava webhook содержит amount (сумма счёта в рублях, float).
                # Сверяем с тем, что мы отправляли на создание.
                received_amount = payload.get('amount')
                if received_amount is not None:
                    try:
                        received_kopeks = round(float(received_amount) * 100)
                    except (TypeError, ValueError):
                        received_kopeks = None
                    if received_kopeks is not None and abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'Lava amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await lava_crud.update_lava_payment_status(
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
                if lava_invoice_id and not payment.lava_invoice_id:
                    payment.lava_invoice_id = str(lava_invoice_id)
                # Сохраняем pay_service в metadata, не перезаписывая user-выбранный payment_method
                if pay_service:
                    metadata_now = dict(getattr(payment, 'metadata_json', {}) or {})
                    metadata_now['actual_pay_service'] = str(pay_service).lower()
                    payment.metadata_json = metadata_now
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_lava_payment(db, payment, trigger='webhook')

            payment = await lava_crud.update_lava_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                callback_payload=callback_payload,
            )
            return True

        except Exception as e:
            logger.exception('Lava webhook: ошибка обработки', error=e)
            return False

    async def _finalize_lava_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE-lock уже взят вызывающим.
        """
        payment_module = import_module('app.services.payment_service')
        lava_crud = import_module('app.database.crud.lava')

        if payment.transaction_id:
            logger.info(
                'Lava платеж уже связан с транзакцией',
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
            provider_name='lava',
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
            logger.error('Пользователь не найден для Lava', user_id=payment.user_id)
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
                PaymentMethod.LAVA,
            )

        display_name = settings.get_lava_display_name()
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
                payment_method=PaymentMethod.LAVA,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await lava_crud.link_lava_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Lava платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.LAVA,
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
            logger.error('Ошибка обработки реферального пополнения Lava', error=error)

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
                logger.error('Ошибка отправки админ уведомления Lava', error=error)

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
                logger.error('Ошибка отправки уведомления пользователю Lava', error=error)

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
            'Обработан Lava платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_lava_payment_status(
        self,
        db: AsyncSession,
        order_id: str | None = None,
        invoice_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Запрос статуса инвойса через API Lava."""
        try:
            return await lava_service.get_invoice_status(order_id=order_id, invoice_id=invoice_id)
        except Exception as e:
            logger.error(
                'Lava: ошибка проверки статуса',
                order_id=order_id,
                invoice_id=invoice_id,
                error=e,
            )
            return None
