"""Mixin для интеграции с Antilopay (lk.antilopay.com)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.antilopay_service import antilopay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов Antilopay -> internal
ANTILOPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'PENDING': ('pending', False),
    'SUCCESS': ('success', True),
    'FAIL': ('failed', False),
    'CANCEL': ('cancelled', False),
    'EXPIRED': ('expired', False),
    'CHARGEBACK': ('chargeback', False),
    'REVERSED': ('reversed', False),
}


def _build_antilopay_recurrent_payload() -> dict[str, Any] | None:
    if not settings.ANTILOPAY_RECURRENT_ENABLED:
        return None
    recurrent_type = (settings.ANTILOPAY_RECURRENT_TYPE or 'MONTH').upper()
    if recurrent_type not in {'WEEK', 'MONTH'}:
        recurrent_type = 'MONTH'
    payment_count = max(1, int(settings.ANTILOPAY_RECURRENT_PAYMENT_COUNT or 24))
    return {'type': recurrent_type, 'payment_count': payment_count}


def _supports_antilopay_recurrent(payment_method_type: str | None) -> bool:
    """Проверяет, можно ли передать recurrent для данного sub-метода Antilopay."""
    if not payment_method_type:
        return False
    return payment_method_type.strip().lower() in settings.get_antilopay_recurrent_methods()


class AntilopayPaymentMixin:
    """Mixin для работы с платежами Antilopay."""

    async def create_antilopay_payment(
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
        enable_recurrent: bool | None = None,
        subscription_id: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Создает платеж Antilopay.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_antilopay_enabled():
            logger.error('Antilopay не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.ANTILOPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Antilopay: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                ANTILOPAY_MIN_AMOUNT_KOPEKS=settings.ANTILOPAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.ANTILOPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Antilopay: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                ANTILOPAY_MAX_AMOUNT_KOPEKS=settings.ANTILOPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id
        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            user = None
            tg_id = 'guest'

        # Генерируем уникальный order_id с telegram_id для удобного поиска
        order_id = f'alp{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.ANTILOPAY_CURRENCY

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }
        if subscription_id is not None:
            metadata['subscription_id'] = subscription_id

        try:
            # Определяем prefer_methods по типу подметода
            prefer_methods: list[str] | None = None
            if payment_method_type == 'sbp':
                prefer_methods = ['SBP']
            elif payment_method_type == 'card':
                prefer_methods = ['CARD_RU']
            elif payment_method_type == 'sberpay':
                prefer_methods = ['SBER_PAY']

            recurrent_payload = None
            use_recurrent = (
                enable_recurrent
                if enable_recurrent is not None
                else _supports_antilopay_recurrent(payment_method_type)
            )
            if use_recurrent:
                recurrent_payload = _build_antilopay_recurrent_payload()
                if recurrent_payload:
                    metadata['recurrent_enabled'] = True
                    metadata['recurrent_type'] = recurrent_payload['type']
                    metadata['recurrent_payment_count'] = recurrent_payload['payment_count']
                    recurrent_payload.setdefault('category', 'SUBSCRIPTION')
                    recurrent_payload.setdefault('delay', 0)
                    recurrent_payload.setdefault('delay_type', 'DAY')

            result_url = return_url or settings.ANTILOPAY_RETURN_URL

            merchant_extra = order_id

            api_result = await antilopay_service.create_payment(
                amount_rubles=amount_rubles,
                order_id=order_id,
                product_name=settings.ANTILOPAY_PRODUCT_NAME,
                product_type=settings.ANTILOPAY_PRODUCT_TYPE,
                description=description,
                customer_email=email,
                prefer_methods=prefer_methods,
                success_url=result_url,
                fail_url=result_url,
                merchant_extra=merchant_extra,
                recurrent=recurrent_payload,
            )

            payment_id = api_result.get('payment_id')
            payment_url = api_result.get('payment_url')

            logger.info(
                'Antilopay: получен ответ API',
                order_id=order_id,
                payment_id=payment_id,
                payment_url=payment_url,
            )

            lifetime = settings.ANTILOPAY_PAYMENT_LIFETIME_MINUTES
            expires_at = datetime.now(UTC) + timedelta(minutes=lifetime)

            # Сохраняем в БД
            antilopay_crud = import_module('app.database.crud.antilopay')
            local_payment = await antilopay_crud.create_antilopay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=payment_method_type,
                antilopay_payment_id=payment_id,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'Antilopay: создан платеж',
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
                'payment_id': payment_id,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('Antilopay: ошибка создания платежа', error=e)
            return None

    async def process_antilopay_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает callback от Antilopay.

        Подпись проверяется в webserver/payments.py до вызова этого метода.

        Args:
            db: Сессия БД
            payload: JSON тело callback (signature проверена в webserver)

        Returns:
            True если платеж успешно обработан
        """
        try:
            callback_type = payload.get('type')
            if callback_type != 'payment':
                logger.info('Antilopay callback: неизвестный тип', callback_type=callback_type)
                return True  # Не наш тип — не ошибка

            antilopay_payment_id = payload.get('payment_id')
            antilopay_status = payload.get('status')
            our_order_id = payload.get('order_id')

            if not our_order_id or not antilopay_status:
                logger.warning('Antilopay callback: отсутствуют обязательные поля', payload=payload)
                return False

            # Определяем is_paid по статусу
            is_confirmed = antilopay_status == 'SUCCESS'

            # Ищем платеж по order_id
            antilopay_crud = import_module('app.database.crud.antilopay')
            payment = await antilopay_crud.get_antilopay_payment_by_order_id(db, our_order_id)

            if not payment and '_R' in our_order_id:
                import re

                base_order_id = re.sub(r'_R\d+$', '', our_order_id)
                if base_order_id != our_order_id:
                    base_payment = await antilopay_crud.get_antilopay_payment_by_order_id(db, base_order_id)
                    if base_payment:
                        raw_amount = payload.get('original_amount') or payload.get('amount')
                        if raw_amount is not None:
                            try:
                                amount_kopeks = round(float(raw_amount) * 100)
                            except (ValueError, TypeError):
                                amount_kopeks = base_payment.amount_kopeks
                        else:
                            amount_kopeks = base_payment.amount_kopeks

                        metadata = dict(getattr(base_payment, 'metadata_json', {}) or {})
                        metadata['is_recurrent_charge'] = True
                        metadata['parent_order_id'] = base_order_id

                        try:
                            payment = await antilopay_crud.create_antilopay_payment(
                                db=db,
                                user_id=base_payment.user_id,
                                order_id=our_order_id,
                                amount_kopeks=amount_kopeks,
                                currency=base_payment.currency,
                                description=f'Рекуррентное списание ({our_order_id})',
                                payment_method=base_payment.payment_method,
                                antilopay_payment_id=str(antilopay_payment_id) if antilopay_payment_id else None,
                                metadata_json=metadata,
                            )
                            logger.info(
                                'Antilopay: создана авто-запись для рекуррентного платежа',
                                order_id=our_order_id,
                                base_order_id=base_order_id,
                                user_id=base_payment.user_id,
                            )
                        except Exception as create_exc:
                            logger.warning(
                                'Antilopay: ошибка авто-создания записи для рекуррента, получаем существующую',
                                order_id=our_order_id,
                                error=str(create_exc),
                            )
                            try:
                                await db.rollback()
                            except Exception:
                                pass
                            payment = await antilopay_crud.get_antilopay_payment_by_order_id(db, our_order_id)

            if not payment:
                logger.warning(
                    'Antilopay callback: платеж не найден',
                    order_id=our_order_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await antilopay_crud.get_antilopay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('Antilopay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('Antilopay callback: платеж уже обработан', order_id=payment.order_id)
                return True

            # Маппинг статуса
            status_info = ANTILOPAY_STATUS_MAP.get(antilopay_status, ('pending', False))
            internal_status, is_paid = status_info

            # Если статус SUCCESS, принудительно считаем оплаченным
            if is_confirmed:
                is_paid = True
                internal_status = 'success'

            callback_payload = {
                'antilopay_payment_id': antilopay_payment_id,
                'status': antilopay_status,
                'amount': payload.get('amount'),
                'original_amount': payload.get('original_amount'),
                'fee': payload.get('fee'),
                'currency': payload.get('currency'),
                'pay_method': payload.get('pay_method'),
                'pay_data': payload.get('pay_data'),
                'customer': payload.get('customer'),
                'merchant_extra': payload.get('merchant_extra'),
                'recurrent_id': payload.get('recurrent_id'),
            }

            callback_recurrent_id = payload.get('recurrent_id')
            if callback_recurrent_id:
                payment.recurrent_id = str(callback_recurrent_id)

            # Проверка суммы ДО обновления статуса
            if is_paid:
                original_amount = payload.get('original_amount')
                if original_amount is not None:
                    # original_amount в РУБЛЯХ (float), конвертируем в копейки
                    received_kopeks = round(float(original_amount) * 100)
                    if abs(received_kopeks - payment.amount_kopeks) > 1:
                        logger.error(
                            'Antilopay amount mismatch',
                            expected_kopeks=payment.amount_kopeks,
                            received_kopeks=received_kopeks,
                            order_id=payment.order_id,
                        )
                        await antilopay_crud.update_antilopay_payment_status(
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
                payment.antilopay_payment_id = str(antilopay_payment_id) if antilopay_payment_id else None
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                finalized = await self._finalize_antilopay_payment(db, payment, trigger='webhook')
                if finalized and callback_recurrent_id and payment.user_id:
                    await self._register_antilopay_recurrent_from_callback(
                        db,
                        payment=payment,
                        payload=payload,
                        recurrent_id=str(callback_recurrent_id),
                    )
                return finalized

            # Для не-success статусов можно безопасно коммитить
            payment = await antilopay_crud.update_antilopay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('Antilopay callback: ошибка обработки', error=e)
            return False

    async def _finalize_antilopay_payment(
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
        antilopay_crud = import_module('app.database.crud.antilopay')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'Antilopay платеж уже связан с транзакцией',
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
            provider_name='antilopay',
        )
        if guest_result is not None:
            purchase_token = metadata.get('purchase_token')
            if purchase_token:
                from sqlalchemy import select
                from app.database.models import GuestPurchase
                gp_res = await db.execute(
                    select(GuestPurchase.user_id).where(GuestPurchase.token == purchase_token)
                )
                resolved_user_id = gp_res.scalar_one_or_none()
                if resolved_user_id:
                    payment.user_id = resolved_user_id
                    await db.commit()
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
            logger.error('Пользователь не найден для Antilopay', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = payment.order_id

        is_recurrent_charge = bool(metadata.get('is_recurrent_charge') or '_R' in payment.order_id)
        autopay_is_disabled = subscription and not getattr(subscription, 'autopay_enabled', True)

        if is_recurrent_charge and not autopay_is_disabled:
            return await self._finalize_antilopay_recurrent_as_subscription(
                db,
                payment=payment,
                user=user,
                subscription=subscription,
                metadata=metadata,
                transaction_external_id=transaction_external_id,
                trigger=trigger,
            )


        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.ANTILOPAY,
            )

        display_name = settings.get_antilopay_display_name()
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
                payment_method=PaymentMethod.ANTILOPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await antilopay_crud.link_antilopay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Antilopay платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.ANTILOPAY,
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
            logger.error('Ошибка обработки реферального пополнения Antilopay', error=error)

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
                logger.error('Ошибка отправки админ уведомления Antilopay', error=error)

        if getattr(self, 'bot', None) and user.telegram_id and settings.is_notifications_enabled():
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
                logger.error('Ошибка отправки уведомления пользователю Antilopay', error=error)

        is_recurrent_charge = bool(metadata.get('is_recurrent_charge') or '_R' in payment.order_id)
        autopay_is_disabled = subscription and not getattr(subscription, 'autopay_enabled', True)

        if is_recurrent_charge and autopay_is_disabled:
            logger.info(
                'Antilopay: автосписание при выключенном автопродлении. Баланс пополнен, автопродление пропущено, отменяем рекуррент.',
                user_id=payment.user_id,
                order_id=payment.order_id,
            )
            try:
                await self.cancel_user_antilopay_recurrents(db, payment.user_id)
            except Exception as error:
                logger.error('Antilopay: ошибка при отмене рекуррентов', user_id=payment.user_id, error=error)
        else:
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
            'Обработан Antilopay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def _finalize_antilopay_recurrent_as_subscription(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        user: Any,
        subscription: Any,
        metadata: dict,
        transaction_external_id: str,
        trigger: str,
    ) -> bool:
        """Обработка рекуррентного платежа Antilopay при включённом автопродлении.

        Создаёт транзакцию SUBSCRIPTION_PAYMENT и продлевает подписку напрямую,
        БЕЗ зачисления на баланс — чтобы не было двойного счёта в статистике.
        (DEPOSIT + SUBSCRIPTION_PAYMENT = двойной доход в отчётах)
        """
        from importlib import import_module

        payment_module = import_module('app.services.payment_service')
        antilopay_crud_mod = import_module('app.database.crud.antilopay')

        if transaction_external_id:
            existing = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.ANTILOPAY,
            )
            if existing:
                logger.info(
                    'Antilopay рекуррент: транзакция уже существует, пропускаем',
                    order_id=payment.order_id,
                    transaction_id=existing.id,
                )
                await antilopay_crud_mod.link_antilopay_payment_to_transaction(
                    db, payment=payment, transaction_id=existing.id
                )
                return True

        if not subscription:
            logger.warning(
                'Antilopay рекуррент: нет подписки, переходим в обычный режим (DEPOSIT)',
                user_id=payment.user_id,
                order_id=payment.order_id,
            )
            from app.database.crud.user import lock_user_for_update

            display_name = settings.get_antilopay_display_name()
            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=f'Пополнение через {display_name}',
                payment_method=PaymentMethod.ANTILOPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            await antilopay_crud_mod.link_antilopay_payment_to_transaction(
                db, payment=payment, transaction_id=transaction.id
            )
            user = await lock_user_for_update(db, user)
            user.balance_kopeks += payment.amount_kopeks
            user.updated_at = datetime.now(UTC)
            await db.commit()
            metadata['balance_credited'] = True
            payment.metadata_json = metadata
            await db.commit()
            return True

        tariff = getattr(subscription, 'tariff', None)
        if tariff:
            period_days = tariff.get_shortest_period() or 30
        else:
            period_days = 30

        display_name = settings.get_antilopay_display_name()
        description = f'Автоплатёж через {display_name} на {period_days} дней'

        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=payment.amount_kopeks,
            description=description,
            payment_method=PaymentMethod.ANTILOPAY,
            external_id=transaction_external_id,
            is_completed=True,
            created_at=getattr(payment, 'created_at', None),
            commit=False,
        )

        await antilopay_crud_mod.link_antilopay_payment_to_transaction(
            db, payment=payment, transaction_id=transaction.id
        )

        from app.database.crud.subscription import extend_subscription
        from app.services.subscription_service import SubscriptionService

        old_end_date = subscription.end_date
        subscription_service = SubscriptionService()
        try:
            updated_subscription = await extend_subscription(db, subscription, period_days)
        except Exception as error:
            logger.error(
                'Antilopay рекуррент: не удалось продлить подписку',
                user_id=payment.user_id,
                order_id=payment.order_id,
                error=error,
                exc_info=True,
            )
            await db.rollback()
            return False

        await db.commit()

        try:
            await subscription_service.update_remnawave_user(
                db,
                updated_subscription,
                reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                reset_reason='Antilopay рекуррентный автоплатёж',
            )
        except Exception as error:
            logger.error(
                'Antilopay рекуррент: не удалось обновить RemnaWave',
                user_id=payment.user_id,
                error=error,
            )
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=updated_subscription.id,
                user_id=updated_subscription.user_id,
                action='update',
            )

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, payment.amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Antilopay рекуррент: ошибка реферального начисления', error=error)

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_subscription_extension_notification(
                    db,
                    user,
                    updated_subscription,
                    transaction,
                    period_days,
                    old_end_date,
                    new_end_date=updated_subscription.end_date,
                    balance_after=user.balance_kopeks,
                )
            except Exception as error:
                logger.error('Antilopay рекуррент: ошибка уведомления админов', error=error)

        if getattr(self, 'bot', None) and getattr(user, 'telegram_id', None):
            try:
                from app.utils.pricing_utils import format_period_description
                from app.utils.timezone import format_local_datetime

                period_label = format_period_description(period_days, getattr(user, 'language', 'ru'))
                new_end_date = updated_subscription.end_date
                end_date_label = format_local_datetime(new_end_date, '%d.%m.%Y %H:%M')

                msg = (
                    f'✅ <b>Автоплатёж выполнен!</b>\n\n'
                    f'💳 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                    f'📅 Подписка продлена на {period_label}\n'
                    f'🗓 Действует до: {end_date_label}'
                )
                if settings.is_multi_tariff_enabled() and tariff:
                    msg += f'\n📦 Тариф: «{tariff.name}»'

                await self.bot.send_message(
                    user.telegram_id,
                    msg,
                    parse_mode='HTML',
                )
            except Exception as error:
                logger.error('Antilopay рекуррент: ошибка уведомления пользователя', error=error)

        try:
            from app.cabinet.routes.websocket import notify_user_subscription_renewed
            from app.utils.timezone import format_email_datetime

            await notify_user_subscription_renewed(
                user_id=user.id,
                subscription_id=subscription.id,
                new_expires_at=format_email_datetime(updated_subscription.end_date),
                amount_kopeks=payment.amount_kopeks,
            )
        except Exception as ws_error:
            logger.warning('Antilopay рекуррент: WS уведомление не отправлено', ws_error=ws_error)

        metadata['recurrent_processed'] = True
        payment.metadata_json = metadata
        await db.commit()

        logger.info(
            'Antilopay рекуррент: подписка продлена напрямую (SUBSCRIPTION_PAYMENT)',
            order_id=payment.order_id,
            user_id=payment.user_id,
            period_days=period_days,
            trigger=trigger,
        )

        return True

    async def _register_antilopay_recurrent_from_callback(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        payload: dict[str, Any],
        recurrent_id: str,
    ) -> None:
        """Сохраняет recurrent_id после успешной оплаты с рекуррентом."""
        if not settings.ANTILOPAY_RECURRENT_ENABLED or not payment.user_id:
            return

        try:
            from app.database.crud.antilopay_recurrent import upsert_antilopay_recurrent
            from app.database.crud.subscription import get_subscription_by_user_id

            subscription = await get_subscription_by_user_id(db, payment.user_id)
            if subscription and not getattr(subscription, 'autopay_enabled', True):
                logger.info(
                    'Antilopay: не сохраняем рекуррент, т.к. автопродление отключено пользователем',
                    user_id=payment.user_id,
                )
                try:
                    await antilopay_service.cancel_recurrent_payment(recurrent_id=recurrent_id)
                except Exception as cancel_err:
                    logger.warning('Antilopay: не удалось отменить новый рекуррент', error=cancel_err)
                return

            metadata = dict(getattr(payment, 'metadata_json', {}) or {})
            subscription_id = metadata.get('subscription_id')
            if subscription_id is not None:
                try:
                    subscription_id = int(subscription_id)
                except (TypeError, ValueError):
                    subscription_id = None

            await upsert_antilopay_recurrent(
                db=db,
                user_id=payment.user_id,
                recurrent_id=recurrent_id,
                initial_payment_id=str(payload.get('payment_id') or payment.antilopay_payment_id or ''),
                recurrent_type=str(metadata.get('recurrent_type') or settings.ANTILOPAY_RECURRENT_TYPE or 'MONTH'),
                payment_count=metadata.get('recurrent_payment_count'),
                status='ACTIVE',
                pay_method=payload.get('pay_method'),
                pay_data=payload.get('pay_data'),
                subscription_id=subscription_id,
            )
        except Exception as error:
            logger.error(
                'Antilopay: ошибка сохранения рекуррента',
                recurrent_id=recurrent_id,
                user_id=payment.user_id,
                error=error,
                exc_info=True,
            )

    async def cancel_user_antilopay_recurrents(
        self,
        db: AsyncSession,
        user_id: int,
    ) -> int:
        """Отменяет активные рекурренты Antilopay пользователя через API и в БД."""
        if not settings.ANTILOPAY_RECURRENT_ENABLED or not settings.is_antilopay_enabled():
            return 0

        from app.database.crud.antilopay_recurrent import (
            deactivate_antilopay_recurrent,
            get_active_antilopay_recurrents_by_user,
        )

        recurrents = await get_active_antilopay_recurrents_by_user(db, user_id)
        cancelled = 0
        for recurrent in recurrents:
            try:
                if recurrent.recurrent_id:
                    await antilopay_service.cancel_recurrent_payment(recurrent_id=recurrent.recurrent_id)
                elif recurrent.initial_payment_id:
                    await antilopay_service.cancel_recurrent_payment(transaction_id=recurrent.initial_payment_id)
            except Exception as error:
                if recurrent.recurrent_id and recurrent.initial_payment_id:
                    try:
                        await antilopay_service.cancel_recurrent_payment(transaction_id=recurrent.initial_payment_id)
                    except Exception as fallback_error:
                        logger.warning('Antilopay: fallback cancellation error', error=fallback_error)
                logger.warning(
                    'Antilopay: не удалось отменить рекуррент через API',
                    recurrent_id=recurrent.recurrent_id,
                    user_id=user_id,
                    error=error,
                )
            await deactivate_antilopay_recurrent(db, recurrent)
            cancelled += 1
        return cancelled


    async def check_antilopay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API Antilopay."""
        try:
            result = await antilopay_service.check_payment(order_id=order_id)
            return result
        except Exception as e:
            logger.error('Antilopay: ошибка проверки статуса', order_id=order_id, error=e)
            return None
