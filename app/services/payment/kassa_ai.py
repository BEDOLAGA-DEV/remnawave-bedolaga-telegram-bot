"""Mixin для интеграции с KassaAI (api.fk.life)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.kassa_ai_service import kassa_ai_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class KassaAiPaymentMixin:
    """Mixin для работы с платежами KassaAI."""

    async def create_kassa_ai_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        payment_system_id: int | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        Создает платеж KassaAI.

        Args:
            db: Сессия БД
            user_id: ID пользователя
            amount_kopeks: Сумма в копейках
            description: Описание платежа
            email: Email пользователя
            language: Язык интерфейса

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_kassa_ai_enabled():
            logger.error('KassaAI не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.KASSA_AI_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'KassaAI: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                KASSA_AI_MIN_AMOUNT_KOPEKS=settings.KASSA_AI_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.KASSA_AI_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'KassaAI: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                KASSA_AI_MAX_AMOUNT_KOPEKS=settings.KASSA_AI_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id
        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
        else:
            user = None
        tg_id = user.telegram_id if user else (user_id or 'guest')

        # Генерируем уникальный order_id с telegram_id для удобного поиска
        order_id = f'k{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.KASSA_AI_CURRENCY

        # Срок действия платежа (1 час по умолчанию)
        expires_at = datetime.now(UTC) + timedelta(hours=1)

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }
        if extra_metadata:
            metadata.update(extra_metadata)

        try:
            # Используем API для создания заказа
            result = await kassa_ai_service.create_order(
                order_id=order_id,
                amount=amount_rubles,
                currency=currency,
                email=email,
                payment_system_id=settings.KASSA_AI_PAYMENT_SYSTEM_ID,
            )

            payment_url = result.get('location')
            if not payment_url:
                logger.error('KassaAI API не вернул URL платежа')
                return None

            logger.info('KassaAI API: создан заказ order_id url', order_id=order_id, payment_url=payment_url)

            # Импортируем CRUD модуль
            kassa_ai_crud = import_module('app.database.crud.kassa_ai')

            # Сохраняем в БД
            local_payment = await kassa_ai_crud.create_kassa_ai_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_system_id=settings.KASSA_AI_PAYMENT_SYSTEM_ID,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'KassaAI: создан платеж order_id user_id amount',
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
            logger.exception('KassaAI: ошибка создания платежа', e=e)
            return None

    async def process_kassa_ai_webhook(
        self,
        db: AsyncSession,
        *,
        merchant_id: int,
        amount: float,
        order_id: str,
        sign: str,
        intid: str,
        cur_id: int | None = None,
    ) -> bool:
        """
        Обрабатывает webhook от KassaAI.

        Args:
            db: Сессия БД
            merchant_id: ID магазина (MERCHANT_ID)
            amount: Сумма платежа (AMOUNT)
            order_id: Номер заказа (MERCHANT_ORDER_ID)
            sign: Подпись (SIGN)
            intid: ID транзакции KassaAI
            cur_id: ID валюты/платежной системы (CUR_ID)

        Returns:
            True если платеж успешно обработан
        """
        try:
            # Проверка подписи
            if not kassa_ai_service.verify_webhook_signature(merchant_id, amount, order_id, sign):
                logger.warning('KassaAI webhook: неверная подпись для order_id', order_id=order_id)
                return False

            # Импортируем CRUD модуль
            kassa_ai_crud = import_module('app.database.crud.kassa_ai')

            # Получаем платеж из БД
            payment = await kassa_ai_crud.get_kassa_ai_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('KassaAI webhook: платеж не найден order_id', order_id=order_id)
                return False

            # Проверка дублирования
            if payment.is_paid:
                logger.info('KassaAI webhook: платеж уже обработан order_id', order_id=order_id)
                return True

            # Проверка суммы
            expected_amount = payment.amount_kopeks / 100
            if abs(amount - expected_amount) > 0.01:
                logger.warning(
                    'KassaAI webhook: несоответствие суммы ожидалось получено',
                    expected_amount=expected_amount,
                    amount=amount,
                )
                return False

            # Обновляем статус платежа
            callback_payload = {
                'merchant_id': merchant_id,
                'amount': amount,
                'order_id': order_id,
                'intid': intid,
                'cur_id': cur_id,
            }

            payment = await kassa_ai_crud.update_kassa_ai_payment_status(
                db=db,
                payment=payment,
                status='success',
                is_paid=True,
                kassa_ai_order_id=intid,
                payment_system_id=cur_id,
                callback_payload=callback_payload,
            )

            # Финализируем платеж (начисляем баланс, создаем транзакцию)
            return await self._finalize_kassa_ai_payment(db, payment, intid=intid, trigger='webhook')

        except Exception as e:
            logger.exception('KassaAI webhook: ошибка обработки', e=e)
            return False

    async def _finalize_kassa_ai_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        intid: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления."""
        payment_module = import_module('app.services.payment_service')

        kassa_ai_lock_crud = import_module('app.database.crud.kassa_ai')
        locked = await kassa_ai_lock_crud.get_kassa_ai_payment_by_id_for_update(db, payment.id)
        if not locked:
            logger.error('KassaAI: не удалось заблокировать платёж', payment_id=payment.id)
            return False
        payment = locked

        if payment.transaction_id:
            logger.info(
                'KassaAI платеж уже привязан к транзакции (trigger=)', order_id=payment.order_id, trigger=trigger
            )
            return True

        # --- Guest purchase flow (landing page) ---
        kai_metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=kai_metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=str(intid) if intid else payment.order_id,
            provider_name='kassa_ai',
        )
        if guest_result is not None:
            return True

        # Получаем пользователя
        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error(
                'Пользователь не найден для KassaAI платежа (trigger=)',
                user_id=payment.user_id,
                order_id=payment.order_id,
                trigger=trigger,
            )
            return False

        # Создаем транзакцию
        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment.amount_kopeks,
            description=f'Пополнение через KassaAI (#{intid or payment.order_id})',
            payment_method=PaymentMethod.KASSA_AI,
            external_id=str(intid) if intid else payment.order_id,
            is_completed=True,
            created_at=getattr(payment, 'created_at', None),
            commit=False,
        )

        # Связываем платеж с транзакцией (без commit, чтобы сохранить атомарность)
        payment.transaction_id = transaction.id
        payment.updated_at = datetime.now(UTC)
        await db.flush()

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        # Начисляем баланс
        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)
        topup_status = 'Первое пополнение' if was_first_topup else 'Пополнение'

        await db.commit()

        # Emit deferred side-effects after atomic commit
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.KASSA_AI,
            external_id=str(intid) if intid else payment.order_id,
        )

        # Обработка реферального пополнения
        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, payment.amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения KassaAI', error=error)

        if was_first_topup and not user.has_made_first_topup:
            user.has_made_first_topup = True
            await db.commit()

        await db.refresh(user)
        await db.refresh(payment)

        # Отправка уведомления админам
        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import (
                    AdminNotificationService,
                )

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
                logger.error('Ошибка отправки админ уведомления KassaAI', error=error)

        # Отправка уведомления пользователю
        try:
            display_name = settings.get_kassa_ai_display_name()

            if getattr(self, 'bot', None) and user.telegram_id:
                keyboard = await self.build_topup_success_keyboard(user)
                message = (
                    '✅ <b>Пополнение успешно!</b>\n\n'
                    f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                    f'💳 Способ: {display_name}\n'
                    f'🆔 Транзакция: {transaction.id}\n\n'
                    'Баланс пополнен автоматически!'
                )

                await self.bot.send_message(
                    user.telegram_id,
                    message,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            elif user.email and user.email_verified:
                from app.services.notification_delivery_service import notification_delivery_service

                await notification_delivery_service.notify_balance_topup(
                    user=user,
                    amount_kopeks=payment.amount_kopeks,
                    new_balance_kopeks=user.balance_kopeks,
                    bot=getattr(self, 'bot', None),
                )
        except Exception as error:
            logger.error('Ошибка отправки уведомления пользователю KassaAI', error=error)

        # Авто-подписка если в metadata указан тариф
        try:
            meta = {}
            if payment.metadata_json:
                meta = json.loads(payment.metadata_json) if isinstance(payment.metadata_json, str) else payment.metadata_json
            meta_tariff_id = meta.get('tariff_id')
            meta_duration_days = meta.get('duration_days')

            if meta_tariff_id and meta_duration_days:
                await self._auto_create_subscription_from_metadata(
                    db, user, int(meta_tariff_id), int(meta_duration_days), payment.amount_kopeks
                )
        except Exception as error:
            logger.error(
                'Ошибка авто-подписки из metadata KassaAI', user_id=user.id, error=error, exc_info=True
            )

        # Автопокупка подписки и уведомление о корзине
        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя', user_id=user.id, error=error, exc_info=True
            )

        logger.info(
            '✅ Обработан KassaAI платеж для пользователя (trigger=)',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def _auto_create_subscription_from_metadata(
        self,
        db: AsyncSession,
        user: Any,
        tariff_id: int,
        duration_days: int,
        amount_kopeks: int,
    ) -> None:
        """Создаёт подписку автоматически после оплаты если в metadata указан тариф."""
        from app.database.crud.server_squad import get_all_server_squads
        from app.database.crud.subscription import create_paid_subscription, get_subscription_by_user_id
        from app.database.crud.tariff import get_tariff_by_id
        from app.database.crud.transaction import create_transaction
        from app.database.crud.user import subtract_user_balance
        from app.services.subscription_service import SubscriptionService

        tariff = await get_tariff_by_id(db, tariff_id)
        if not tariff or not tariff.is_active:
            logger.warning('Авто-подписка: тариф не найден или неактивен', tariff_id=tariff_id)
            return

        price = tariff.get_price_for_period(duration_days)
        if price is None:
            logger.warning('Авто-подписка: период недоступен', tariff_id=tariff_id, duration_days=duration_days)
            return

        final_price = int(price)
        if user.balance_kopeks < final_price:
            logger.warning(
                'Авто-подписка: недостаточно средств',
                user_id=user.id,
                balance=user.balance_kopeks,
                price=final_price,
            )
            return

        # Проверяем существующую подписку
        existing = await get_subscription_by_user_id(db, user.id)
        if existing:
            logger.info('Авто-подписка: у пользователя уже есть подписка, пропускаем', user_id=user.id)
            return

        # Списываем баланс
        description = f'Покупка тарифа {tariff.name} на {duration_days} дней (авто)'
        success = await subtract_user_balance(db, user, final_price, description)
        if not success:
            logger.error('Авто-подписка: не удалось списать баланс', user_id=user.id)
            return

        # Серверы из тарифа
        squads = tariff.allowed_squads or []
        if not squads:
            all_servers, _ = await get_all_server_squads(db, available_only=True)
            squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

        # Создаём подписку
        subscription = await create_paid_subscription(
            db=db,
            user_id=user.id,
            duration_days=duration_days,
            traffic_limit_gb=tariff.traffic_limit_gb,
            device_limit=tariff.device_limit,
            connected_squads=squads,
            tariff_id=tariff.id,
        )

        # Транзакция
        try:
            await create_transaction(
                db=db,
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=final_price,
                description=description,
            )
        except Exception as tx_error:
            logger.warning('Авто-подписка: не удалось создать транзакцию', error=tx_error)

        # Remnawave
        try:
            subscription_service = SubscriptionService()
            await subscription_service.create_remnawave_user(db, subscription, reset_traffic=True, reset_reason='авто-подписка API')
        except Exception as rw_error:
            logger.warning('Авто-подписка: не удалось обновить Remnawave', error=rw_error)

        logger.info(
            '✅ Авто-подписка создана',
            user_id=user.id,
            tariff_id=tariff_id,
            duration_days=duration_days,
            price=final_price,
        )

    async def check_kassa_ai_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """
        Проверяет статус платежа через API.

        Args:
            db: Сессия БД
            order_id: Номер заказа

        Returns:
            Данные о статусе платежа
        """
        try:
            status_data = await kassa_ai_service.get_order_status(order_id)
            return status_data
        except Exception as e:
            logger.exception('KassaAI: ошибка проверки статуса', e=e)
            return None

    async def get_kassa_ai_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        """
        Проверяет статус платежа KassaAI по локальному ID через API.
        Если платёж оплачен — автоматически начисляет баланс.
        """
        logger.info('KassaAI: checking payment status for id', local_payment_id=local_payment_id)
        kassa_ai_crud = import_module('app.database.crud.kassa_ai')

        payment = await kassa_ai_crud.get_kassa_ai_payment_by_id(db, local_payment_id)
        if not payment:
            logger.warning('KassaAI payment not found: id', local_payment_id=local_payment_id)
            return None

        if payment.is_paid:
            return {
                'payment': payment,
                'status': 'success',
                'is_paid': True,
            }

        if not settings.KASSA_AI_API_KEY:
            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        try:
            # Запрашиваем статус заказа в KassaAI (api.fk.life)
            response = await kassa_ai_service.get_order_status(payment.order_id)

            # KassaAI возвращает список заказов (как Freekassa)
            orders = response.get('orders', [])
            target_order = None

            # Ищем наш заказ в списке
            for order in orders:
                order_key = str(order.get('merchant_order_id') or order.get('paymentId'))
                if order_key == str(payment.order_id):
                    target_order = order
                    break

            if target_order:
                # Статус 1 = Оплачен (как в Freekassa)
                kai_status = int(target_order.get('status', 0))

                if kai_status == 1:
                    logger.info('KassaAI payment confirmed via API', order_id=payment.order_id)

                    callback_payload = {
                        'check_source': 'api',
                        'kai_order_data': target_order,
                    }

                    # ID заказа на стороне KassaAI
                    kai_intid = str(target_order.get('fk_order_id') or target_order.get('id'))

                    # Обновляем статус
                    payment = await kassa_ai_crud.update_kassa_ai_payment_status(
                        db=db,
                        payment=payment,
                        status='success',
                        is_paid=True,
                        kassa_ai_order_id=kai_intid,
                        payment_system_id=int(target_order.get('curID')) if target_order.get('curID') else None,
                        callback_payload=callback_payload,
                    )

                    # Финализируем (начисляем баланс)
                    await self._finalize_kassa_ai_payment(
                        db,
                        payment,
                        intid=kai_intid,
                        trigger='api_check',
                    )
        except Exception as e:
            logger.error('Error checking KassaAI payment status', e=e)

        return {
            'payment': payment,
            'status': payment.status or 'pending',
            'is_paid': payment.is_paid,
        }
