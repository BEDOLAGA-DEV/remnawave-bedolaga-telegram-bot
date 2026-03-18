"""Mixin для интеграции с внешним платёжным шлюзом."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class ExternalGatewayPaymentMixin:
    """Mixin для работы с платежами через внешний шлюз (paygate)."""

    async def create_external_gateway_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
    ) -> dict[str, Any] | None:
        """Создаёт платёж через внешний шлюз.

        Returns:
            Словарь с данными платежа или None при ошибке.
        """
        if not settings.is_external_gateway_enabled():
            logger.error('External Gateway не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.EXTERNAL_GATEWAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'External Gateway: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                min=settings.EXTERNAL_GATEWAY_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.EXTERNAL_GATEWAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'External Gateway: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                max=settings.EXTERNAL_GATEWAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Генерируем уникальный order_id
        order_id = f'bg_{user_id or "guest"}_{int(datetime.now(UTC).timestamp())}_{uuid.uuid4().hex[:8]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.EXTERNAL_GATEWAY_CURRENCY

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'type': 'balance_topup',
        }

        # Формируем callback URL
        webhook_path = settings.EXTERNAL_GATEWAY_WEBHOOK_PATH
        webhook_host = settings.EXTERNAL_GATEWAY_WEBHOOK_HOST.replace('https://', '').replace('http://', '').rstrip('/')
        webhook_port = settings.EXTERNAL_GATEWAY_WEBHOOK_PORT
        scheme = 'https' if webhook_port == 443 else 'http'
        port_suffix = '' if webhook_port in (80, 443) else f':{webhook_port}'
        callback_url = f'{scheme}://{webhook_host}{port_suffix}{webhook_path}'

        # Return URL
        return_url = settings.EXTERNAL_GATEWAY_RETURN_URL or None

        # Метод оплаты (stripe/paypal/пусто)
        method = settings.EXTERNAL_GATEWAY_PAYMENT_METHOD or None

        try:
            # Вызываем внешний шлюз
            gateway_response = await self.external_gateway_service.create_payment(
                amount=amount_rubles,
                currency=currency,
                order_id=order_id,
                callback_url=callback_url,
                description=description,
                return_url=return_url,
                method=method,
                metadata=metadata,
            )

            if not gateway_response:
                logger.error('External Gateway: пустой ответ от шлюза')
                return None

            gateway_order_id = gateway_response.get('order_id')
            redirect_url = gateway_response.get('redirect_url')

            if not redirect_url:
                logger.error('External Gateway: нет redirect_url в ответе', response=gateway_response)
                return None

            # Сохраняем в БД
            ext_gw_crud = import_module('app.database.crud.external_gateway')
            local_payment = await ext_gw_crud.create_external_gateway_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                redirect_url=redirect_url,
                gateway_order_id=str(gateway_order_id) if gateway_order_id is not None else None,
                metadata_json=metadata,
            )

            logger.info(
                'External Gateway: создан платёж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                gateway_order_id=gateway_order_id,
            )

            return {
                'order_id': order_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'redirect_url': redirect_url,
                'local_payment_id': local_payment.id,
                'gateway_order_id': gateway_order_id,
            }

        except Exception as e:
            logger.exception('External Gateway: ошибка создания платежа', e=e)
            return None

    async def process_external_gateway_callback(
        self,
        db: AsyncSession,
        callback_data: dict[str, Any],
    ) -> bool:
        """Обрабатывает callback от внешнего шлюза.

        Args:
            db: Сессия БД
            callback_data: Данные callback (JSON body)

        Returns:
            True если платёж успешно обработан.
        """
        try:
            external_order_id = callback_data.get('external_order_id')
            status = callback_data.get('status')

            if not external_order_id:
                logger.warning('External Gateway callback: нет external_order_id', data=callback_data)
                return False

            ext_gw_crud = import_module('app.database.crud.external_gateway')
            payment = await ext_gw_crud.get_external_gateway_payment_by_order_id(db, external_order_id)

            if not payment:
                logger.warning('External Gateway callback: платёж не найден', order_id=external_order_id)
                return False

            # Проверка дублирования
            if payment.is_paid:
                logger.info('External Gateway callback: платёж уже обработан', order_id=external_order_id)
                return True

            if status != 'completed':
                logger.info('External Gateway callback: статус не completed', status=status, order_id=external_order_id)
                # Обновляем статус, но не финализируем
                await ext_gw_crud.update_external_gateway_payment_status(
                    db=db,
                    payment=payment,
                    status=status or 'failed',
                    callback_payload=callback_data,
                )
                return False

            # Обновляем статус на completed
            gateway_order_id = callback_data.get('order_id')
            gateway_payment_id = callback_data.get('payment_id')
            payment_method_name = callback_data.get('method')
            amount_converted = callback_data.get('amount_converted')

            payment = await ext_gw_crud.update_external_gateway_payment_status(
                db=db,
                payment=payment,
                status='completed',
                is_paid=True,
                gateway_order_id=str(gateway_order_id) if gateway_order_id is not None else None,
                gateway_payment_id=str(gateway_payment_id) if gateway_payment_id else None,
                payment_method_name=payment_method_name,
                amount_converted=float(amount_converted) if amount_converted is not None else None,
                callback_payload=callback_data,
            )

            # Финализируем
            return await self._finalize_external_gateway_payment(db, payment, trigger='callback')

        except Exception as e:
            logger.exception('External Gateway callback: ошибка обработки', e=e)
            return False

    async def _finalize_external_gateway_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления."""
        payment_module = import_module('app.services.payment_service')

        ext_gw_crud = import_module('app.database.crud.external_gateway')
        locked = await ext_gw_crud.get_external_gateway_payment_by_id_for_update(db, payment.id)
        if not locked:
            logger.error('External Gateway: не удалось заблокировать платёж', payment_id=payment.id)
            return False
        payment = locked

        if payment.transaction_id:
            logger.info(
                'External Gateway: платёж уже привязан к транзакции',
                order_id=payment.order_id,
                trigger=trigger,
            )
            return True

        # --- Guest purchase flow ---
        gw_metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=gw_metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.order_id,
            provider_name='external_gateway',
        )
        if guest_result is not None:
            return True

        # Получаем пользователя
        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error(
                'Пользователь не найден для External Gateway платежа',
                user_id=payment.user_id,
                order_id=payment.order_id,
                trigger=trigger,
            )
            return False

        # Создаем транзакцию
        external_id = payment.gateway_payment_id or payment.order_id
        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment.amount_kopeks,
            description=f'Пополнение через {settings.get_external_gateway_display_name()} (#{external_id})',
            payment_method=PaymentMethod.EXTERNAL_GATEWAY,
            external_id=external_id,
            is_completed=True,
            created_at=getattr(payment, 'created_at', None),
            commit=False,
        )

        # Связываем платеж с транзакцией
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
            payment_method=PaymentMethod.EXTERNAL_GATEWAY,
            external_id=external_id,
        )

        # Обработка реферального пополнения
        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, payment.amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения External Gateway', error=error)

        if was_first_topup and not user.has_made_first_topup:
            user.has_made_first_topup = True
            await db.commit()

        await db.refresh(user)
        await db.refresh(payment)

        # Отправка уведомления админам
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
                logger.error('Ошибка отправки админ уведомления External Gateway', error=error)

        # Отправка уведомления пользователю
        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                display_name = settings.get_external_gateway_display_name_html()
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
                logger.error('Ошибка отправки уведомления пользователю External Gateway', error=error)

        # Автопокупка подписки и уведомление о корзине
        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка при работе с корзиной External Gateway', user_id=user.id, error=error, exc_info=True)

        logger.info(
            '✅ Обработан External Gateway платёж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def get_external_gateway_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через внешний шлюз по локальному ID."""
        ext_gw_crud = import_module('app.database.crud.external_gateway')

        payment = await ext_gw_crud.get_external_gateway_payment_by_id(db, local_payment_id)
        if not payment:
            logger.warning('External Gateway payment not found', local_payment_id=local_payment_id)
            return None

        if payment.is_paid:
            return {
                'payment': payment,
                'status': 'completed',
                'is_paid': True,
            }

        # Пробуем проверить статус через шлюз
        # gateway_order_id — внутренний ID paygate (числовой), order_id — наш bg_xxx
        status_order_id = payment.gateway_order_id or payment.order_id
        try:
            response = await self.external_gateway_service.check_status(status_order_id)
            if response and response.get('success'):
                gw_status = response.get('status', '')

                if gw_status == 'completed':
                    logger.info('External Gateway payment confirmed via status check', order_id=payment.order_id)

                    payment = await ext_gw_crud.update_external_gateway_payment_status(
                        db=db,
                        payment=payment,
                        status='completed',
                        is_paid=True,
                        gateway_order_id=str(response.get('order_id')) if response.get('order_id') else None,
                        callback_payload={'check_source': 'status_api', 'response': response},
                    )

                    await self._finalize_external_gateway_payment(db, payment, trigger='status_check')
        except Exception as e:
            logger.error('Error checking External Gateway payment status', e=e)

        return {
            'payment': payment,
            'status': payment.status or 'pending',
            'is_paid': payment.is_paid,
        }
