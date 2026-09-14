"""Mixin для интеграции с TabPay (tabpay.org, СБП и карты)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.tabpay_service import tabpay_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов TabPay -> internal
TABPAY_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'CREATED': ('pending', False),
    'PENDING': ('processing', False),
    'SUCCESS': ('success', True),
    'FAILED': ('declined', False),
    'EXPIRED': ('expired', False),
    'REFUNDED': ('refunded', False),
    'CANCELED': ('canceled', False),
}

# Статусы, из которых платёж уже никуда не переходит. FAILED и EXPIRED сюда
# намеренно НЕ входят: QR СБП живёт дольше таймаута TabPay, поэтому поздняя
# оплата штатно переводит такой платёж в SUCCESS отдельным вебхуком.
TABPAY_FINAL_STATUSES = frozenset({'refunded', 'canceled', 'amount_mismatch'})

# Статусы, при которых платёж ещё может быть оплачен (фоновая сверка по API).
TABPAY_PENDING_STATUSES = frozenset({'pending', 'processing', 'created'})

# Sub-метод бота -> method TabPay
TABPAY_METHOD_MAP: dict[str, str] = {
    'sbp': 'SBP',
    'card': 'CARD',
}


def resolve_tabpay_method(payment_method_type: str | None) -> str | None:
    """Определяет поле ``method`` для API TabPay.

    Явный sub-метод выигрывает всегда. Для генерик-метода способ не навязываем —
    покупатель выберет его на платёжной странице; исключение только когда
    магазину включён ровно один способ, иначе TabPay ответит 409.
    """
    explicit = TABPAY_METHOD_MAP.get((payment_method_type or '').lower())
    if explicit:
        return explicit

    sbp_only = settings.is_tabpay_sbp_enabled() and not settings.is_tabpay_card_enabled()
    card_only = settings.is_tabpay_card_enabled() and not settings.is_tabpay_sbp_enabled()
    if sbp_only:
        return 'SBP'
    if card_only:
        return 'CARD'
    return None


# Значения, которые считаем явным «да» в поле test/isTest. По спеке приходит
# булево, строки разобраны на случай нестрогой сериализации.
TABPAY_TEST_FLAG_TRUE = frozenset({'true', '1', 'yes', 'y'})


def _is_test_flag(value: Any) -> bool:
    """Толкует поле ``test`` вебхука (и ``isTest`` объекта платежа).

    Проверяется ЗНАЧЕНИЕ, а не наличие поля: у боевых платежей оно приходит
    равным false.

    Тестовым платёж считается ТОЛЬКО при явно распознанном «да». Всё
    остальное — включая нераспознанное значение — это боевой платёж, потому
    что ошибка в эту сторону стоит дороже: помеченный тестовым платёж уже не
    зачислится никогда (защита стоит в единственной точке зачисления), то есть
    покупатель заплатит, а баланс не пополнится. Лишний раз зачислить
    песочницу, наоборот, можно только если оператор сам подставил ключ
    тестового магазина.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in TABPAY_TEST_FLAG_TRUE
    if isinstance(value, int):
        return value == 1
    return False


def _extract_amount_kopecks(payload: dict[str, Any]) -> int | None:
    """Сумма из вебхука/объекта платежа в копейках, либо None если её не разобрать."""
    raw = payload.get('amountKopecks')
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class TabPayPaymentMixin:
    """Mixin для работы с платежами TabPay."""

    async def create_tabpay_payment(
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
        """Создаёт платёж TabPay и возвращает данные для перехода на payUrl.

        ``payment_method_type`` — sub-метод бота ('card' / 'sbp'); не задан —
        способ выбирает покупатель на платёжной странице. Вебхук приходит на
        URL, настроенный в кабинете магазина.
        """
        if not settings.is_tabpay_enabled():
            logger.error('TabPay не настроен')
            return None

        min_amount = max(settings.TABPAY_MIN_AMOUNT_KOPEKS, tabpay_service.API_MIN_AMOUNT_KOPEKS)
        max_amount = min(settings.TABPAY_MAX_AMOUNT_KOPEKS, tabpay_service.API_MAX_AMOUNT_KOPEKS)

        if amount_kopeks < min_amount:
            logger.warning('TabPay: сумма меньше минимальной', amount_kopeks=amount_kopeks, min_kopeks=min_amount)
            return None

        if amount_kopeks > max_amount:
            logger.warning('TabPay: сумма больше максимальной', amount_kopeks=amount_kopeks, max_kopeks=max_amount)
            return None

        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            tg_id = None

        order_id = f'tp{tg_id or "guest"}_{uuid.uuid4().hex[:8]}'
        amount_rubles = amount_kopeks / 100
        tabpay_method = resolve_tabpay_method(payment_method_type)

        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
            'payment_method_type': payment_method_type,
        }

        try:
            # Сетевой сбой на создании не приводит к дублю: клиент сверится по
            # нашему orderId и переиспользует уже созданный платёж.
            api_result = await tabpay_service.create_payment_reconciled(
                order_id=order_id,
                amount_kopecks=amount_kopeks,
                description=description[:255] if description else None,
                email=email,
                telegram_id=tg_id,
                metadata=metadata,
                success_url=return_url,
                fail_url=fail_url or return_url,
                method=tabpay_method,
            )

            tabpay_payment_id = api_result.get('id')
            payment_url = api_result.get('payUrl')
            commission = api_result.get('commissionKopecks')

            tabpay_crud = import_module('app.database.crud.tabpay')
            local_payment = await tabpay_crud.create_tabpay_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency='RUB',
                description=description,
                payment_url=payment_url,
                payment_method=api_result.get('method') or tabpay_method,
                tabpay_payment_id=str(tabpay_payment_id) if tabpay_payment_id else None,
                commission_kopeks=int(commission) if commission is not None else None,
                is_test=_is_test_flag(api_result.get('isTest')),
                metadata_json=metadata,
            )

            logger.info(
                'TabPay: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                payment_method=tabpay_method,
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': 'RUB',
                'payment_url': payment_url,
                'payment_id': str(tabpay_payment_id) if tabpay_payment_id else None,
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('TabPay: ошибка создания платежа', error=e)
            return None

    async def process_tabpay_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Обрабатывает вебхук TabPay (подпись уже проверена в webserver).

        Тело: id, orderId, status, amountKopecks, telegramId, metadata, test.
        Обработка идемпотентна по паре (id, status): повторная доставка того же
        события не зачисляет баланс второй раз.
        """
        try:
            our_order_id = payload.get('orderId')
            tabpay_payment_id = payload.get('id')
            tabpay_status = (payload.get('status') or '').strip().upper()

            if not our_order_id or not tabpay_status or not tabpay_payment_id:
                logger.warning('TabPay callback: отсутствуют обязательные поля', payload=payload)
                return False

            tabpay_crud = import_module('app.database.crud.tabpay')
            payment = await tabpay_crud.get_tabpay_payment_by_order_id(db, our_order_id)
            if not payment:
                # Чужой orderId повторами не появится — подтверждаем доставку.
                # Сюда же попадает кнопка «Отправить тестовый вебхук» из кабинета:
                # её id вида test-... нашему платежу не соответствует.
                logger.warning('TabPay callback: платеж не найден', order_id=our_order_id)
                return True

            event_key = f'{tabpay_payment_id}:{tabpay_status}'
            if tabpay_crud.is_tabpay_event_processed(payment, event_key):
                logger.info('TabPay callback: событие уже обработано', order_id=our_order_id, event_key=event_key)
                return True

            locked = await tabpay_crud.get_tabpay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('TabPay: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Повторная проверка под блокировкой: параллельная доставка того же
            # события могла пройти между чтением и захватом строки.
            if tabpay_crud.is_tabpay_event_processed(payment, event_key):
                logger.info('TabPay callback: событие уже обработано (под блокировкой)', event_key=event_key)
                return True

            if tabpay_status not in TABPAY_STATUS_MAP:
                # Набор статусов может расширяться — подтверждаем и логируем.
                logger.warning(
                    'TabPay callback: неизвестный статус',
                    order_id=payment.order_id,
                    status=tabpay_status,
                )
                return True

            if payment.status in TABPAY_FINAL_STATUSES:
                logger.warning(
                    'TabPay callback: платёж в окончательном статусе, событие игнорируется',
                    order_id=payment.order_id,
                    current_status=payment.status,
                    incoming_status=tabpay_status,
                )
                tabpay_crud.remember_tabpay_event(payment, event_key)
                await db.commit()
                return True

            internal_status, is_paid = TABPAY_STATUS_MAP[tabpay_status]

            callback_payload = {
                'tabpay_payment_id': tabpay_payment_id,
                'status': tabpay_status,
                'amountKopecks': payload.get('amountKopecks'),
                'telegramId': payload.get('telegramId'),
                'metadata': payload.get('metadata'),
                'test': payload.get('test'),
            }

            # Платёж магазина-песочницы: подписан как боевой, но деньги не
            # двигались. Исход фиксируем, баланс не трогаем.
            if _is_test_flag(payload.get('test')) or getattr(payment, 'is_test', False):
                logger.info(
                    'TabPay callback: тестовый платёж, баланс не зачисляется',
                    order_id=payment.order_id,
                    payment_id=tabpay_payment_id,
                    status=tabpay_status,
                )
                payment.is_test = True
                tabpay_crud.remember_tabpay_event(payment, event_key)
                await tabpay_crud.update_tabpay_payment_status(
                    db=db,
                    payment=payment,
                    status=internal_status,
                    is_paid=None,
                    tabpay_payment_id=str(tabpay_payment_id),
                    callback_payload=callback_payload,
                )
                return True

            if is_paid:
                return await self._apply_tabpay_success(
                    db,
                    payment=payment,
                    payload=payload,
                    event_key=event_key,
                    tabpay_payment_id=str(tabpay_payment_id),
                    callback_payload=callback_payload,
                )

            if internal_status == 'refunded' and payment.is_paid:
                # Деньги вернулись покупателю уже после зачисления баланса:
                # автоматически списывать нельзя, но возврат должен быть виден.
                logger.error(
                    'TabPay: возврат по оплаченному платежу, требуется ручная сверка баланса',
                    order_id=payment.order_id,
                    user_id=payment.user_id,
                    amount_kopeks=payment.amount_kopeks,
                )

            tabpay_crud.remember_tabpay_event(payment, event_key)
            await tabpay_crud.update_tabpay_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=None,
                tabpay_payment_id=str(tabpay_payment_id),
                callback_payload=callback_payload,
            )
            return True

        except Exception as e:
            logger.exception('TabPay callback: ошибка обработки', error=e)
            return False

    async def _apply_tabpay_success(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        payload: dict[str, Any],
        event_key: str,
        tabpay_payment_id: str,
        callback_payload: dict[str, Any],
    ) -> bool:
        """Сверяет сумму и зачисляет оплату. Блокировка строки уже взята."""
        tabpay_crud = import_module('app.database.crud.tabpay')

        received_kopeks = _extract_amount_kopecks(payload)
        if received_kopeks is None:
            # amountKopecks обязателен по спеке. «Не смогли проверить» не равно
            # «сошлось»: оставляем платёж под ретрай и фоновую сверку.
            logger.error(
                'TabPay callback: SUCCESS без разбираемого amountKopecks, зачисление отменено',
                order_id=payment.order_id,
                received=payload.get('amountKopecks'),
            )
            return False

        if received_kopeks != payment.amount_kopeks:
            logger.error(
                'TabPay amount mismatch',
                expected_kopeks=payment.amount_kopeks,
                received_kopeks=received_kopeks,
                order_id=payment.order_id,
            )
            tabpay_crud.remember_tabpay_event(payment, event_key)
            await tabpay_crud.update_tabpay_payment_status(
                db=db,
                payment=payment,
                status='amount_mismatch',
                is_paid=False,
                callback_payload=callback_payload,
            )
            return False

        if payment.is_paid:
            # Оплату уже зачислили (другим событием или фоновой сверкой).
            logger.info('TabPay callback: платеж уже оплачен', order_id=payment.order_id)
            tabpay_crud.remember_tabpay_event(payment, event_key)
            await db.commit()
            return True

        payment.status = 'success'
        payment.is_paid = True
        payment.paid_at = datetime.now(UTC)
        payment.tabpay_payment_id = tabpay_payment_id or payment.tabpay_payment_id
        payment.callback_payload = callback_payload
        payment.updated_at = datetime.now(UTC)
        tabpay_crud.remember_tabpay_event(payment, event_key)
        await db.flush()

        return await self._finalize_tabpay_payment(db, payment, trigger='webhook')

    async def _finalize_tabpay_payment(
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
        tabpay_crud = import_module('app.database.crud.tabpay')

        # Единственная точка, через которую идёт зачисление, — здесь же и последний
        # рубеж против тестовых платежей: у песочницы деньги не двигались, а прийти
        # сюда можно и вебхуком, и сверкой по API.
        if getattr(payment, 'is_test', False):
            logger.warning(
                'TabPay: тестовый платёж, баланс не зачисляется',
                order_id=payment.order_id,
                trigger=trigger,
            )
            return True

        if payment.transaction_id:
            logger.info(
                'TabPay платеж уже связан с транзакцией',
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
            provider_name='tabpay',
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
            logger.error('Пользователь не найден для TabPay', user_id=payment.user_id)
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
                PaymentMethod.TABPAY,
            )

        display_name = settings.get_tabpay_display_name()
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
                payment_method=PaymentMethod.TABPAY,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await tabpay_crud.link_tabpay_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('TabPay платеж уже зачислил баланс ранее', order_id=payment.order_id)
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
            payment_method=PaymentMethod.TABPAY,
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
            logger.error('Ошибка обработки реферального пополнения TabPay', error=error)

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
                logger.error('Ошибка отправки админ уведомления TabPay', error=error)

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
                logger.error('Ошибка отправки уведомления пользователю TabPay', error=error)

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
            'Обработан TabPay платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_tabpay_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API TabPay и синхронизирует БД.

        Страховка на случай потерянного вебхука: используется ручной проверкой
        из админки и фоновой сверкой.
        """
        try:
            tabpay_crud = import_module('app.database.crud.tabpay')
            payment = await tabpay_crud.get_tabpay_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('TabPay payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {'payment': payment, 'status': payment.status, 'is_paid': True}

            if payment.status in TABPAY_FINAL_STATUSES:
                return {'payment': payment, 'status': payment.status, 'is_paid': False}

            try:
                status_data = (
                    await tabpay_service.get_payment(payment.tabpay_payment_id)
                    if payment.tabpay_payment_id
                    else await tabpay_service.get_payment_by_order_id(payment.order_id)
                )
            except Exception as e:
                logger.error('Error checking TabPay payment status via API', error=e)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

            if not status_data:
                logger.warning('TabPay API check: платёж не найден на стороне провайдера', order_id=payment.order_id)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            tabpay_status = (status_data.get('status') or '').strip().upper()
            internal_status, is_paid = TABPAY_STATUS_MAP.get(tabpay_status, ('pending', False))

            if not is_paid:
                if internal_status != payment.status:
                    payment = await tabpay_crud.update_tabpay_payment_status(
                        db=db,
                        payment=payment,
                        status=internal_status,
                    )
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            if _is_test_flag(status_data.get('isTest')) or payment.is_test:
                # Песочница: статус синхронизируем, баланс не трогаем.
                logger.info('TabPay API check: тестовый платёж, баланс не зачисляется', order_id=payment.order_id)
                if not payment.is_test or internal_status != payment.status:
                    payment.is_test = True
                    payment = await tabpay_crud.update_tabpay_payment_status(
                        db=db,
                        payment=payment,
                        status=internal_status,
                    )
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            # Сверяем сумму так же строго, как в вебхуке.
            received_kopeks = _extract_amount_kopecks(status_data)
            if received_kopeks is None:
                logger.error('TabPay API check: SUCCESS без amountKopecks, зачисление отменено', order_id=order_id)
                return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': False}

            if received_kopeks != payment.amount_kopeks:
                logger.error(
                    'TabPay amount mismatch (API check)',
                    expected_kopeks=payment.amount_kopeks,
                    received_kopeks=received_kopeks,
                    order_id=payment.order_id,
                )
                await tabpay_crud.update_tabpay_payment_status(
                    db=db,
                    payment=payment,
                    status='amount_mismatch',
                    is_paid=False,
                    callback_payload={'check_source': 'api', 'tabpay_status_data': status_data},
                )
                return {'payment': payment, 'status': 'amount_mismatch', 'is_paid': False}

            locked = await tabpay_crud.get_tabpay_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('TabPay: не удалось заблокировать платёж', payment_id=payment.id)
                return None
            payment = locked

            if payment.is_paid:
                logger.info('TabPay платеж уже обработан (api_check)', order_id=payment.order_id)
                return {'payment': payment, 'status': 'success', 'is_paid': True}

            logger.info('TabPay payment confirmed via API', order_id=payment.order_id)

            # Обновляем поля без промежуточного commit — он снял бы FOR UPDATE lock
            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.callback_payload = {'check_source': 'api', 'tabpay_status_data': status_data}
            payment.updated_at = datetime.now(UTC)
            if status_data.get('id'):
                payment.tabpay_payment_id = str(status_data['id'])
            # Ключ события в том же формате, что у вебхука: пришедший следом
            # SUCCESS по этому же платежу не станет зачислять второй раз.
            if payment.tabpay_payment_id:
                tabpay_crud.remember_tabpay_event(payment, f'{payment.tabpay_payment_id}:{tabpay_status}')
            await db.flush()

            await self._finalize_tabpay_payment(db, payment, trigger='api_check')

            return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': payment.is_paid}

        except Exception as e:
            logger.exception('TabPay: ошибка проверки статуса', error=e)
            return None
