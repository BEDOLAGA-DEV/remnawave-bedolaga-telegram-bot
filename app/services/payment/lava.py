"""Mixin для интеграции с Lava Business (gate.lava.ru)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, Subscription, TransactionType
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


def _lava_amount_to_kopeks(raw: Any) -> int:
    """Сумма Lava (рубли, число/строка) в копейки; 0 при отсутствии/мусоре."""
    if raw is None:
        return 0
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return 0


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
            # В ответе Lava Business поле называется ``expire`` (без d); старое ``expired`` остаётся
            # как fallback для совместимости со снапшотами от старого gate.lava.ru.
            expired_str = data.get('expire') or data.get('expired')

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

    # ==================== Рекуррентные подписки Lava ====================

    @staticmethod
    def _lava_consumer_email(user: Any) -> str:
        """Email плательщика: у Lava поле обязательное.

        Для Telegram-пользователей без почты синтезируем адрес по тому же
        шаблону, что KassaAI/SeverPay (``{telegram_id}@telegram.org``).
        """
        email = (getattr(user, 'email', None) or '').strip()
        if email:
            return email
        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id:
            return f'{telegram_id}@telegram.org'
        return f'user{getattr(user, "id", 0)}@telegram.org'

    @staticmethod
    def _lava_consumer_id(user: Any) -> str:
        """Стабильный идентификатор плательщика: один на пользователя бота."""
        return f'bot-user-{getattr(user, "id", 0)}'

    async def _resolve_lava_product(self, product_id: str) -> dict[str, Any] | None:
        """Продукт Lava по id из ``recurrent/product/list``.

        Нужен ради ``periodDays``/``price``: они заданы в кабинете Lava, а не у
        нас. Недоступность списка не фатальна — сработают дефолты вызывающего.
        """
        try:
            products = await lava_service.list_recurrent_products()
        except Exception as error:  # pragma: no cover - network errors
            logger.warning('Lava: не удалось получить список продуктов', error=str(error))
            return None
        for product in products:
            if str(product.get('id') or '') == str(product_id):
                return product
        return None

    async def _notify_lava_recurring(
        self,
        db: AsyncSession,
        record: Any,
        kind: str,
    ) -> None:
        """Best-effort уведомление пользователя о событии автопродления Lava.

        Никогда не бросает наружу — вебхук обязан завершиться 200 независимо
        от доставки сообщения (зеркало ``_notify_sbp_recurring`` у Platega).
        """
        if not settings.is_notifications_enabled():
            return

        try:
            from app.cabinet.routes.websocket import cabinet_ws_manager

            await cabinet_ws_manager.send_to_user(
                record.user_id,
                {
                    'type': f'lava_recurring.{kind}',
                    'status': record.status,
                    'amount_kopeks': record.amount_kopeks,
                    'amount_rubles': record.amount_kopeks / 100,
                    'next_charge_at': record.next_charge_at.isoformat() if record.next_charge_at else None,
                    'subscription_id': record.subscription_id,
                },
            )
        except Exception as ws_error:  # pragma: no cover — best-effort
            logger.warning('Не удалось отправить WS-событие автопродления Lava', error=str(ws_error), kind=kind)

        bot = getattr(self, 'bot', None)
        if not bot:
            return
        try:
            from app.database.models import User
            from app.localization.texts import get_texts

            user = await db.get(User, record.user_id)
            if not user or not user.telegram_id:
                return

            texts = get_texts(user.language)
            messages = {
                'confirmed': texts.t('LAVA_RECURRING_NOTIFY_CONFIRMED', '✅ Подписка продлена автосписанием Lava.'),
                'failed': texts.t('LAVA_RECURRING_NOTIFY_FAILED', '⚠️ Не удалось списать оплату по автопродлению Lava.'),
                'cancelled': texts.t('LAVA_RECURRING_NOTIFY_CANCELLED', 'ℹ️ Автопродление Lava отменено.'),
            }
            text = messages.get(kind)
            if text:
                await bot.send_message(chat_id=user.telegram_id, text=text)
        except Exception as error:  # pragma: no cover - best-effort notify
            logger.warning('Не удалось отправить уведомление об автопродлении Lava', error=str(error), kind=kind)

    async def create_lava_recurrent_subscription(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        subscription: Any,
        tariff: Any,
    ) -> dict[str, Any]:
        """Оформляет рекуррентную подписку Lava для подписки бота.

        В отличие от Platega сумма и периодичность НЕ выводятся из тарифа: они
        заданы продуктом в кабинете Lava, на который ссылается
        ``tariff.lava_product_id``. Первое списание идёт по ссылке
        ``data.url`` — до его оплаты подписка остаётся ``PENDING``.

        Как и у Platega, включение выключает ``subscription.autopay_enabled``:
        рекуррент провайдера и баланс-автосписание — взаимоисключающие движки
        продления одной подписки.
        """
        from app.database.crud import lava_subscription as sub_crud
        from app.services.lava_recurrent import build_recurrent_order_id, resolve_product_charge_days

        existing = await sub_crud.get_active_lava_subscription_by_subscription(db, subscription.id)
        if existing:
            # Идемпотентный повтор обязан ТОЖЕ восстановить взаимоисключение.
            if getattr(subscription, 'autopay_enabled', False):
                subscription.autopay_enabled = False
                await db.commit()
            return {
                'local_id': existing.id,
                'lava_subscription_id': existing.lava_subscription_id,
                'redirect_url': existing.redirect_url,
                'status': existing.status,
            }

        product_id = (getattr(tariff, 'lava_product_id', None) or '').strip()
        if not product_id:
            raise ValueError('Для тарифа не задан продукт Lava — автопродление недоступно')

        payment_module = import_module('app.services.payment_service')
        user = await payment_module.get_user_by_id(db, user_id)
        if not user:
            raise ValueError('Пользователь не найден')

        # Продукт обязателен: из него берутся шаг продления и цена. Догадка по
        # умолчанию (30 дней) при недоступном product/list навсегда исказила бы
        # каденс — годовой продукт продлевал бы подписку на месяц за списание.
        product = await self._resolve_lava_product(product_id)
        if product is None:
            raise ValueError('Продукт Lava недоступен — попробуйте позже')
        charge_days = resolve_product_charge_days(product)

        # ВСЯ валидация — ДО обращения к Lava: subscribe создаёт живую привязку
        # на их стороне, и любой raise после него оставил бы подписку, которая
        # списывает вечно и которую нечем отменить (локальной записи нет).
        amount_kopeks = _lava_amount_to_kopeks(product.get('price'))
        if amount_kopeks <= 0:
            raise ValueError('Продукт Lava не имеет цены — автопродление недоступно')

        # Сумму диктует продукт в кабинете Lava — поменять её под конкретную
        # подписку нельзя. Если продукт дешевле реальной цены продления (у
        # подписки докуплены устройства/трафик), привязка недобирала бы разницу
        # при каждом списании — отказываем с конкретными числами, чтобы
        # оператор завёл продукт с нужной ценой.
        from app.services.recurrent_amount import resolve_true_renewal_amount

        true_amount = await resolve_true_renewal_amount(db, subscription, charge_days)
        if true_amount and true_amount - amount_kopeks > 1:
            raise ValueError(
                f'Продукт Lava стоит {amount_kopeks / 100:.2f} ₽, а продление подписки — '
                f'{true_amount / 100:.2f} ₽ за {charge_days} дн. Заведите продукт с актуальной ценой.'
            )

        consumer_id = self._lava_consumer_id(user)
        try:
            await lava_service.create_recurrent_consumer(
                consumer_id=consumer_id,
                email=self._lava_consumer_email(user),
            )
        except Exception as error:
            # Плательщик мог быть создан ранее — повторное создание отдаёт
            # ошибку валидации. Подписку всё равно пробуем оформить: если
            # consumer действительно отсутствует, subscribe отвергнет запрос.
            logger.warning('Lava: consumer/create не подтверждён', consumer_id=consumer_id, error=str(error))

        order_id = build_recurrent_order_id(subscription.id, uuid.uuid4().hex)
        response = await lava_service.subscribe_recurrent(
            product_id=product_id,
            consumer_id=consumer_id,
            order_id=order_id,
        )
        data = (response or {}).get('data') or {}
        lava_id = data.get('subscriptionId')
        redirect_url = data.get('url')

        # Фактическая сумма первого счёта, если Lava её вернула, иначе цена продукта.
        response_amount = _lava_amount_to_kopeks(data.get('amount'))
        if response_amount > 0:
            amount_kopeks = response_amount

        if not lava_id:
            # Без subscriptionId молча отключаются обе страховки (отмена
            # осиротевшей привязки и свип недошедших отмен) — зеркало Platega,
            # которая в этом месте жёстко падает. Отменяем по orderId и падаем.
            logger.error('Lava: subscribe без subscriptionId — отменяем привязку', order_id=order_id)
            try:
                await lava_service.unsubscribe_recurrent(order_id=order_id)
            except Exception as cancel_error:  # pragma: no cover - network errors
                logger.error(
                    'Lava: не удалось отменить привязку без subscriptionId',
                    order_id=order_id,
                    error=str(cancel_error),
                )
            raise RuntimeError('Lava не вернула subscriptionId при создании подписки')

        try:
            record = await sub_crud.create_lava_subscription(
                db,
                user_id=user_id,
                subscription_id=subscription.id,
                tariff_id=getattr(tariff, 'id', None),
                lava_product_id=product_id,
                lava_consumer_id=consumer_id,
                order_id=order_id,
                charge_days=charge_days,
                amount_kopeks=amount_kopeks,
                redirect_url=redirect_url,
                lava_subscription_id=lava_id,
            )
        except IntegrityError:
            # Конкурентный enable выиграл гонку и занял partial unique
            # uq_lava_subscriptions_alive. Наша remote-подписка уже создана —
            # отменяем её, иначе Lava продолжит списывать по осиротевшей записи.
            await db.rollback()
            if lava_id:
                try:
                    await lava_service.unsubscribe_recurrent(subscription_id=lava_id)
                except Exception as cancel_error:  # pragma: no cover - network errors
                    logger.error(
                        'Lava: не удалось отменить осиротевшую подписку после гонки',
                        lava_subscription_id=lava_id,
                        error=str(cancel_error),
                    )
            winner = await sub_crud.get_active_lava_subscription_by_subscription(db, subscription.id)
            if not winner:
                raise
            logger.warning(
                'Lava: конкурентный enable — возвращаем существующую привязку',
                subscription_id=subscription.id,
                local_id=winner.id,
            )
            return {
                'local_id': winner.id,
                'lava_subscription_id': winner.lava_subscription_id,
                'redirect_url': winner.redirect_url,
                'status': winner.status,
            }

        # Взаимоисключение движков продления — ПОСЛЕ успешного создания записи:
        # раньше отмена Platega шла до subscribe, и любой сбой оформления Lava
        # оставлял пользователя вообще без автопродления (старую привязку уже
        # погасили, новую не создали).
        subscription.autopay_enabled = False
        await db.commit()

        from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

        await cancel_platega_recurring_for_subscription_safe(db, subscription.id)

        return {
            'local_id': record.id,
            'lava_subscription_id': lava_id,
            'redirect_url': redirect_url,
            'status': record.status,
        }

    async def cancel_lava_recurrent_subscription(
        self,
        db: AsyncSession,
        *,
        local_id: int,
        commit: bool = True,
    ) -> bool:
        """Отменяет одну Lava-подписку по локальному id записи.

        Идемпотентна; удалённая отмена — best-effort: сетевой сбой логируется,
        но не мешает пометить запись ``CANCELLED`` локально (иначе временная
        недоступность Lava блокировала бы отмену навсегда). Недоведённую до
        провайдера отмену добьёт reconciler по свипу CANCELLED-записей.
        """
        from app.database.crud import lava_subscription as sub_crud

        record = await sub_crud.get_lava_subscription_by_id(db, local_id)
        if not record:
            return False

        if record.status == 'CANCELLED':
            return True

        if record.lava_subscription_id or record.order_id:
            try:
                await lava_service.unsubscribe_recurrent(
                    subscription_id=record.lava_subscription_id,
                    order_id=None if record.lava_subscription_id else record.order_id,
                )
            except Exception as error:  # pragma: no cover - network errors
                logger.warning(
                    'Не удалось отменить Lava-подписку на стороне провайдера',
                    lava_subscription_id=record.lava_subscription_id,
                    error=str(error),
                )

        record.status = 'CANCELLED'
        if commit:
            await db.commit()
        else:
            # commit=False — вызывающий держит собственную транзакцию: CANCELLED
            # войдёт в неё и закоммитится/откатится вместе с остальным.
            await db.flush()
        return True

    async def cancel_lava_recurring_for_subscription(
        self,
        db: AsyncSession,
        subscription_id: int,
        *,
        commit: bool = True,
    ) -> None:
        """Best-effort отмена активной Lava-подписки по ``subscription_id``.

        Вызывается из путей удаления/замены подписки — никогда не бросает
        исключение и не блокирует удаление сбоем Lava.
        """
        from app.database.crud import lava_subscription as sub_crud

        try:
            record = await sub_crud.get_active_lava_subscription_by_subscription(db, subscription_id)
            if not record:
                return
            await self.cancel_lava_recurrent_subscription(db, local_id=record.id, commit=commit)
        except Exception as error:  # pragma: no cover - best-effort cleanup
            logger.warning(
                'Не удалось отменить Lava-автопродление по подписке',
                subscription_id=subscription_id,
                error=str(error),
            )

    async def process_lava_subscription_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Обрабатывает вебхук Lava по списанию рекуррентной подписки.

        Списания приходят обычными инвойс-вебхуками на тот же ``/lava-webhook``;
        отличает их префикс нашего ``orderId`` (см. ``is_recurrent_order_id``).
        Баланс пользователя НЕ трогается: продление — прямой вызов
        ``Subscription.extend_subscription``, а не начисление на баланс.

        Идемпотентность — по ``invoice_id`` списания: быстрая проверка через
        ``last_charge_external_id`` плюс полноисторийная сверка с
        ``transactions.external_id`` (поздний ределивери старого списания).
        """
        from app.database.crud import lava_subscription as sub_crud
        from app.services import lava_recurrent as lr

        order_id = payload.get('order_id')
        charge_id = payload.get('invoice_id')
        lava_status = (payload.get('status') or '').strip().lower()

        if not order_id:
            logger.warning('Lava subscription callback без order_id', status=lava_status)
            return False

        found = await sub_crud.get_lava_subscription_by_order_id(db, str(order_id))
        if not found:
            logger.warning('Lava subscription callback: подписка не найдена', order_id=order_id)
            return False

        # Блокируем строку перед изменением статуса/счётчиков — конкурентные
        # ретраи вебхука не должны продлевать/учитывать дважды.
        record = await sub_crud.get_lava_subscription_by_id_for_update(db, found.id)
        if not record:
            logger.warning('Lava subscription callback: запись исчезла после блокировки', order_id=order_id)
            return False

        if lava_status in lr.CHARGE_SUCCESS:
            if not charge_id:
                # Без invoice_id идемпотентность не сработает, и каждый повтор
                # продлевал бы подписку заново — не продлеваем.
                logger.warning('Lava subscription callback: success без invoice_id', order_id=order_id)
                return False

            charge_id = str(charge_id)
            if record.last_charge_external_id == charge_id:
                logger.info(
                    'Lava subscription callback: списание уже обработано',
                    order_id=order_id,
                    charge_id=charge_id,
                )
                return True

            # last_charge_external_id хранит только ПОСЛЕДНИЙ id, поэтому
            # поздний ределивери старого списания прошёл бы мимо проверки выше.
            from sqlalchemy import select as sa_select

            from app.database.models import Transaction

            duplicate_tx = (
                await db.execute(
                    sa_select(Transaction.id).where(
                        Transaction.external_id == charge_id,
                        Transaction.payment_method == PaymentMethod.LAVA.value,
                    )
                )
            ).scalar_one_or_none()
            if duplicate_tx is not None:
                logger.info(
                    'Lava subscription callback: поздний ределивери старого списания',
                    order_id=order_id,
                    charge_id=charge_id,
                )
                return True

            # Фактически списанная сумма важнее снапшота времени оформления:
            # продукт в кабинете Lava могли передобрить, и в транзакции должна
            # оказаться настоящая сумма, а не цена годичной давности.
            charged_kopeks = _lava_amount_to_kopeks(payload.get('amount'))
            if charged_kopeks > 0 and abs(charged_kopeks - record.amount_kopeks) > 1:
                logger.warning(
                    'Lava: сумма списания отличается от сохранённой — обновляем запись',
                    order_id=order_id,
                    stored_kopeks=record.amount_kopeks,
                    charged_kopeks=charged_kopeks,
                )
                record.amount_kopeks = charged_kopeks

            from app.database.crud.transaction import create_transaction

            subscription = await db.get(Subscription, record.subscription_id)
            if subscription is None:
                # Продлевать нечего (подписку удалили), а деньги уже списаны и
                # ретраев не будет — вебхук всегда отвечает 200. Единственное
                # полезное действие: немедленно остановить дальнейшие списания.
                logger.error(
                    'Lava subscription callback: Subscription не найдена — останавливаем автосписания',
                    subscription_id=record.subscription_id,
                    order_id=order_id,
                )
                try:
                    await lava_service.unsubscribe_recurrent(
                        subscription_id=record.lava_subscription_id,
                        order_id=None if record.lava_subscription_id else record.order_id,
                    )
                    record.status = 'CANCELLED'
                    await db.commit()
                except Exception as cancel_error:  # pragma: no cover - network errors
                    logger.error(
                        'Lava: не удалось остановить списания по удалённой подписке',
                        order_id=order_id,
                        error=str(cancel_error),
                    )
                return False

            # Лок строки подписки: продление — read-modify-write ``end_date``,
            # и конкурентное продление (ручное/другой коллбек) без него теряло
            # бы одно из двух.
            from app.database.crud.subscription import _lock_subscription_row

            await _lock_subscription_row(db, subscription)

            subscription.extend_subscription(record.charge_days)

            # Списание по локально ОТМЕНЁННОЙ записи = удалённая отмена не
            # прошла. Деньги взяты — продлеваем честно, но запись НЕ воскрешаем
            # в ACTIVE и повторяем удалённую отмену.
            was_cancelled = record.status == 'CANCELLED'

            charged_at = datetime.now(UTC)
            record.last_charge_external_id = charge_id
            if not was_cancelled:
                record.status = 'ACTIVE'
            record.last_charge_at = charged_at
            record.charges_success += 1
            # Вебхук Lava не несёт дату следующего списания (в отличие от
            # NextChargeAt у Platega) — считаем по каденсу продукта, иначе
            # колонка и строка «следующее списание» в кабинете мертвы.
            if not was_cancelled:
                record.next_charge_at = charged_at + timedelta(days=record.charge_days)

            tx = await create_transaction(
                db,
                user_id=record.user_id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=record.amount_kopeks,
                description='Автопродление Lava',
                payment_method=PaymentMethod.LAVA,
                external_id=charge_id,
                commit=False,
            )

            await db.commit()

            from app.database.crud.transaction import emit_transaction_side_effects

            await emit_transaction_side_effects(
                db,
                tx,
                amount_kopeks=record.amount_kopeks,
                user_id=record.user_id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                payment_method=PaymentMethod.LAVA,
                external_id=charge_id,
                description='Автопродление Lava',
            )

            await self._notify_lava_recurring(db, record, 'confirmed')

            if was_cancelled:
                logger.error(
                    'Lava: списание по локально отменённой подписке — повторяем удалённую отмену',
                    order_id=order_id,
                    lava_subscription_id=record.lava_subscription_id,
                )
                try:
                    await lava_service.unsubscribe_recurrent(
                        subscription_id=record.lava_subscription_id,
                        order_id=None if record.lava_subscription_id else record.order_id,
                    )
                except Exception as cancel_error:  # pragma: no cover - network errors
                    logger.warning(
                        'Lava: повторная удалённая отмена не удалась',
                        order_id=order_id,
                        error=str(cancel_error),
                    )

            # Синк панели RemnaWave — намеренно ПОСЛЕДНИЙ шаг: продление уже
            # закоммичено, а при сбое update_remnawave_user делает внутренний
            # rollback, экспайрящий атрибуты сессии (чтения record.* выше
            # упали бы MissingGreenlet).
            subscription_id_for_log = subscription.id
            try:
                from app.services.subscription_service import SubscriptionService

                await SubscriptionService().update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                    reset_reason='Автопродление Lava',
                )
            except Exception as sync_error:  # best-effort: продление уже в БД
                logger.warning(
                    'Синк панели после автопродления Lava не удался',
                    error=str(sync_error),
                    subscription_id=subscription_id_for_log,
                )
            return True

        if lava_status in lr.CHARGE_FAILED:
            # CANCELLED не воскрешаем: истёкший счёт по уже отменённой привязке —
            # рутина (включил → не оплатил → отменил → счёт протух). Перевод в
            # PAST_DUE стирал бы отмену в UI, блокировал повторное включение
            # (partial unique по «живым» статусам), выбрасывал запись из свипа
            # недошедших отмен и — главное — обнулял бы was_cancelled, отключая
            # повторную удалённую отмену при следующем успешном списании.
            record.charges_failed += 1
            if record.status != 'CANCELLED':
                record.status = 'PAST_DUE'
            await db.commit()
            await self._notify_lava_recurring(db, record, 'failed')
            return True

        logger.info(
            'Lava subscription callback: промежуточный статус, изменений нет',
            order_id=order_id,
            status=lava_status,
        )
        return True


class _LavaRecurrentAgent(LavaPaymentMixin):
    """Минимальный носитель ``LavaPaymentMixin`` для модульных точек входа
    рекуррентных подписок Lava — в отличие от полного ``PaymentService`` с
    десятками провайдеров, которые здесь не нужны."""


async def enable_lava_recurring(
    db: AsyncSession,
    *,
    user_id: int,
    subscription: Any,
    tariff: Any,
) -> dict[str, Any]:
    """Включить автопродление Lava. Возвращает {local_id, lava_subscription_id, redirect_url, status}.

    НЕ best-effort: пробрасывает ValueError (у тарифа не задан продукт Lava /
    продукт без цены), чтобы UI показал причину. Гейт на фичу.
    """
    if not settings.is_lava_recurrent_enabled():
        raise RuntimeError('Lava recurrent is disabled')
    return await _LavaRecurrentAgent().create_lava_recurrent_subscription(
        db, user_id=user_id, subscription=subscription, tariff=tariff
    )


async def purchase_tariff_with_lava_recurring(
    db: AsyncSession,
    *,
    user: Any,
    tariff: Any,
) -> dict[str, Any]:
    """Оформление подписки на тариф оплатой через автопродление Lava.

    Первое списание Lava = оплата первого счёта по ссылке из subscribe, поэтому
    «купить с автооплатой» — это привязка + первый чардж, который продлит и
    оживит подписку по каденсу продукта.

    Зеркало ``purchase_tariff_with_sbp_recurring`` (Platega), включая отказы:
    - подписки на этот тариф нет → создаётся EXPIRED-заготовка без доступа и
      без панели; первый успешный чардж продлит её и создаст panel-юзера;
    - триал (живой или истёкший) → отказ: конверсию триала делает только
      покупка с баланса, а активация второй строки того же тарифа упёрлась бы
      в partial unique прямо в чардж-коллбеке (деньги взяты, продления нет);
    - DISABLED/PENDING → отказ: чардж не оживит такую подписку;
    - в single-режиме подписка другого тарифа → отказ: чарджи продлевали бы
      подписку ЧУЖОГО тарифа по цене выбранного.
    """
    if not settings.is_lava_recurrent_enabled():
        raise RuntimeError('Lava recurrent is disabled')

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
            raise ValueError('Оформление через Lava недоступно при подписке другого тарифа — оплатите с баланса')

    if subscription is not None:
        if getattr(subscription, 'is_trial', False):
            raise ValueError('Оформление через Lava недоступно для триальной подписки — оплатите с баланса')
        if subscription.status in ('disabled', 'pending'):
            raise ValueError('Оформление через Lava недоступно для этой подписки — оплатите с баланса')

    if subscription is None:
        # Заготовка провайдер-агностична: EXPIRED, end_date=now, без панели —
        # первый чардж её оживит (extend_subscription поднимает EXPIRED).
        subscription = await create_sbp_pending_subscription(db, user.id, tariff)

    result = await enable_lava_recurring(db, user_id=user.id, subscription=subscription, tariff=tariff)
    return {**result, 'subscription_id': subscription.id}


async def cancel_lava_recurring_for_subscription_safe(
    db: AsyncSession,
    subscription_id: int,
    *,
    commit: bool = True,
) -> None:
    """Точка входа для путей удаления/отзыва подписки: отменяет активное
    автопродление Lava, привязанное к ``subscription_id``.

    Best-effort и никогда не бросает исключение — удаление подписки не должно
    блокироваться недоступностью Lava.

    НЕ гейтится флагом рекуррента НАМЕРЕННО: отмена — операция безопасности, а
    не фича. Выключение LAVA_RECURRENT_ENABLED при живых привязках не
    останавливает списания на стороне Lava; если бы отмена гейтилась, юзер не
    смог бы остановить их ни с одной поверхности.
    """
    try:
        await _LavaRecurrentAgent().cancel_lava_recurring_for_subscription(db, subscription_id, commit=commit)
    except Exception as error:  # pragma: no cover - defensive; mixin already best-effort
        logger.warning(
            'Не удалось отменить автопродление Lava при удалении подписки',
            error=str(error),
            subscription_id=subscription_id,
        )


async def get_lava_recurring_status(db: AsyncSession, subscription_id: int) -> dict[str, Any] | None:
    """Состояние активной привязки Lava для UI (бот/кабинет) либо None."""
    from app.database.crud import lava_subscription as sub_crud

    record = await sub_crud.get_active_lava_subscription_by_subscription(db, subscription_id)
    if not record:
        return None
    return {
        'local_id': record.id,
        'lava_subscription_id': record.lava_subscription_id,
        'status': record.status,
        'amount_kopeks': record.amount_kopeks,
        'charge_days': record.charge_days,
        'redirect_url': record.redirect_url,
        'next_charge_at': record.next_charge_at,
        'last_charge_at': record.last_charge_at,
        'charges_success': record.charges_success,
        'charges_failed': record.charges_failed,
    }


async def cancel_lava_recurring_by_local_id(db: AsyncSession, local_id: int) -> bool:
    """Отмена привязки по локальному id (кабинет/бот). Идемпотентна."""
    return await _LavaRecurrentAgent().cancel_lava_recurrent_subscription(db, local_id=local_id)


async def shift_lava_next_charge_after_manual_extension(
    db: AsyncSession,
    subscription_id: int,
    days: int,
) -> None:
    """Сдвигает дату следующего списания Lava после РУЧНОГО продления подписки.

    Пользователь с активной привязкой Lava может продлить подписку и другим
    способом (оплата с баланса, промокод, бонусные дни от админа). Без сдвига
    Lava спишет по своему прежнему расписанию — то есть возьмёт деньги за
    период, который пользователь уже оплатил иначе. ``offset-next-pay-time``
    двигает ближайшее списание ровно на добавленное количество дней.

    Вызывается из CRUD ``extend_subscription``; наше собственное рекуррентное
    списание идёт мимо (оно продлевает через метод модели
    ``Subscription.extend_subscription``), поэтому расписание не «убегает»
    от самого себя.

    Best-effort: любая ошибка только логируется — продление уже закоммичено.
    """
    if days <= 0:
        return

    from app.database.crud import lava_subscription as sub_crud

    try:
        record = await sub_crud.get_active_lava_subscription_by_subscription(db, subscription_id)
        if not record or record.status not in ('ACTIVE', 'PAST_DUE'):
            # PENDING-привязка ещё не оплачена — двигать нечего.
            return
        if not (record.lava_subscription_id or record.order_id):
            return

        await lava_service.offset_recurrent_next_pay_time(
            days=int(days),
            subscription_id=record.lava_subscription_id,
            order_id=None if record.lava_subscription_id else record.order_id,
        )
        logger.info(
            'Lava: следующее списание сдвинуто после ручного продления',
            subscription_id=subscription_id,
            days=days,
            lava_subscription_id=record.lava_subscription_id,
        )
    except Exception as error:  # pragma: no cover - best-effort
        logger.warning(
            'Lava: не удалось сдвинуть дату следующего списания',
            subscription_id=subscription_id,
            days=days,
            error=str(error),
        )
