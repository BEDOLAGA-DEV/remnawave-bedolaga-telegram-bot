"""
Сервис для автоматического списания суточных подписок.
Проверяет подписки с суточным тарифом и списывает плату раз в сутки.
Также сбрасывает докупленный трафик по истечении 30 дней.
"""

import asyncio
from datetime import UTC, datetime

import structlog
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    get_daily_subscriptions_for_charge,
    get_disabled_daily_subscriptions_for_resume,
    get_expired_daily_subscriptions_for_recovery,
    suspend_daily_subscription_insufficient_balance,
    update_daily_charge_time,
)
from app.database.crud.transaction import create_transaction
from app.database.crud.user import get_user_by_id, subtract_user_balance
from app.database.database import AsyncSessionLocal
from app.database.models import PaymentMethod, Subscription, SubscriptionStatus, TransactionType, User
from app.localization.texts import get_texts
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service,
)


logger = structlog.get_logger(__name__)


class DailySubscriptionService:
    """
    Сервис автоматического списания для суточных подписок.
    """

    def __init__(self):
        self._running = False
        self._bot: Bot | None = None
        self._check_interval_minutes = 30  # Проверка каждые 30 минут

    def set_bot(self, bot: Bot):
        """Устанавливает бота для отправки уведомлений."""
        self._bot = bot

    def is_enabled(self) -> bool:
        """Проверяет, включен ли сервис суточных подписок."""
        return getattr(settings, 'DAILY_SUBSCRIPTIONS_ENABLED', True)

    def get_check_interval_minutes(self) -> int:
        """Возвращает интервал проверки в минутах."""
        return getattr(settings, 'DAILY_SUBSCRIPTIONS_CHECK_INTERVAL_MINUTES', 30)

    async def process_daily_charges(self) -> dict:
        """
        Обрабатывает суточные списания.

        Returns:
            dict: Статистика обработки
        """
        stats = {
            'checked': 0,
            'charged': 0,
            'suspended': 0,
            'errors': 0,
        }

        try:
            async with AsyncSessionLocal() as db:
                try:
                    subscriptions = await get_daily_subscriptions_for_charge(db)
                    stats['checked'] = len(subscriptions)

                    for subscription in subscriptions:
                        try:
                            result = await self._process_single_charge(db, subscription)
                            if result == 'charged':
                                stats['charged'] += 1
                            elif result == 'suspended':
                                stats['suspended'] += 1
                            elif result == 'error':
                                stats['errors'] += 1
                        except Exception as e:
                            logger.error(
                                'Ошибка обработки суточной подписки',
                                subscription_id=subscription.id,
                                error=e,
                                exc_info=True,
                            )
                            stats['errors'] += 1
                except Exception as e:
                    logger.error('Ошибка при обработке подписок', error=e, exc_info=True)
                    await db.rollback()

        except Exception as e:
            logger.error('Ошибка при получении подписок для списания', error=e, exc_info=True)

        return stats

    async def _process_single_charge(self, db, subscription) -> str:
        """
        Обрабатывает списание для одной подписки.

        Returns:
            str: "charged", "suspended", "error", "skipped"
        """
        user = subscription.user
        if not user:
            user = await get_user_by_id(db, subscription.user_id)

        if not user:
            logger.warning('Пользователь не найден для подписки', subscription_id=subscription.id)
            return 'error'

        tariff = subscription.tariff
        if not tariff:
            logger.warning('Тариф не найден для подписки', subscription_id=subscription.id)
            return 'error'

        raw_daily_price = tariff.daily_price_kopeks
        if raw_daily_price <= 0:
            logger.warning('Некорректная суточная цена для тарифа', tariff_id=tariff.id)
            return 'error'

        # Lock user row to prevent TOCTOU between discount read and balance charge
        from app.database.crud.user import lock_user_for_pricing

        user = await lock_user_for_pricing(db, user.id)

        # Apply group discount to daily price (consistent with PricingEngine._calculate_switch_to_daily)
        from app.services.pricing_engine import PricingEngine

        promo_group = PricingEngine.resolve_promo_group(user)
        daily_group_pct = promo_group.get_discount_percent('period', 1) if promo_group else 0
        daily_price = (
            PricingEngine.apply_discount(raw_daily_price, daily_group_pct) if daily_group_pct > 0 else raw_daily_price
        )

        # Проверяем баланс (при 100% скидке — пропускаем)
        if daily_price > 0 and user.balance_kopeks < daily_price:
            # Недостаточно средств - приостанавливаем подписку
            await suspend_daily_subscription_insufficient_balance(db, subscription)

            # Уведомляем пользователя (rate-limit: 1 раз в 6 часов)
            if self._bot:
                from app.utils.cache import cache

                cache_key = f'daily_insuf_notify:{subscription.id}'
                try:
                    already_notified = await cache.get(cache_key)
                except Exception:
                    already_notified = None

                if not already_notified:
                    await self._notify_insufficient_balance(user, subscription, daily_price)
                    try:
                        await cache.set(cache_key, '1', expire=21600)  # 6 hours
                    except Exception:
                        pass

            logger.info(
                'Подписка приостановлена: недостаточно средств (баланс: требуется: )',
                subscription_id=subscription.id,
                balance_kopeks=user.balance_kopeks,
                daily_price=daily_price,
            )
            return 'suspended'

        # Списываем средства
        description = f'Суточная оплата тарифа «{tariff.name}»'

        try:
            # commit=False для атомарности: баланс, транзакция и charge_time коммитятся вместе
            deducted = await subtract_user_balance(
                db,
                user,
                daily_price,
                description,
                mark_as_paid_subscription=True,
                commit=False,
            )

            if not deducted:
                await db.rollback()
                logger.warning('Не удалось списать средства для подписки', subscription_id=subscription.id)
                return 'error'

            # Создаём транзакцию (без коммита — часть атомарной операции)
            transaction = await create_transaction(
                db=db,
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=daily_price,
                description=description,
                payment_method=PaymentMethod.BALANCE,
                commit=False,
            )

            # Обновляем время последнего списания и продлеваем подписку (без коммита)
            old_end_date = subscription.end_date
            subscription = await update_daily_charge_time(db, subscription, commit=False)

            # Атомарный коммит: баланс + транзакция + charge_time
            await db.commit()
            await db.refresh(user)

            user_id_display = user.telegram_id or user.email or f'#{user.id}'
            logger.info(
                '✅ Суточное списание: подписка сумма коп., пользователь',
                subscription_id=subscription.id,
                daily_price=daily_price,
                user_id_display=user_id_display,
            )

            # Восстанавливаем connected_squads из тарифа, если очищены деактивацией
            try:
                if not subscription.connected_squads:
                    squads = tariff.allowed_squads or []
                    if not squads:
                        from app.database.crud.server_squad import get_all_server_squads

                        all_servers, _ = await get_all_server_squads(db, available_only=True, limit=10000)
                        squads = [s.squad_uuid for s in all_servers if s.squad_uuid]
                    if squads:
                        subscription.connected_squads = squads
                        await db.commit()
                        await db.refresh(subscription)
            except Exception as sq_err:
                logger.warning('Не удалось восстановить connected_squads', error=sq_err)

            # Синхронизируем с Remnawave (обновляем срок подписки)
            try:
                from app.services.subscription_service import SubscriptionService

                subscription_service = SubscriptionService()
                _has_panel_user = (
                    getattr(subscription, 'remnawave_uuid', None)
                    if settings.is_multi_tariff_enabled()
                    else getattr(user, 'remnawave_uuid', None)
                )
                if _has_panel_user:
                    await subscription_service.update_remnawave_user(
                        db,
                        subscription,
                        reset_traffic=False,
                        reset_reason=None,
                        sync_squads=True,
                    )
                else:
                    await subscription_service.create_remnawave_user(
                        db,
                        subscription,
                        reset_traffic=False,
                        reset_reason=None,
                    )
                    # POST может игнорировать activeInternalSquads — отправляем PATCH
                    await db.refresh(user)
                    _sync_uuid = (
                        getattr(subscription, 'remnawave_uuid', None)
                        if settings.is_multi_tariff_enabled()
                        else getattr(user, 'remnawave_uuid', None)
                    )
                    if _sync_uuid and subscription.connected_squads:
                        try:
                            await subscription_service.update_remnawave_user(
                                db,
                                subscription,
                                reset_traffic=False,
                                sync_squads=True,
                            )
                        except Exception as patch_err:
                            logger.warning('Не удалось синхронизировать сквады после создания', error=patch_err)
            except Exception as e:
                logger.warning('Не удалось обновить Remnawave', error=e)
                from app.services.remnawave_retry_queue import remnawave_retry_queue

                if hasattr(subscription, 'id') and hasattr(subscription, 'user_id'):
                    remnawave_retry_queue.enqueue(
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                        action='update' if _has_panel_user else 'create',
                    )

            # Отправляем уведомление администраторам
            try:
                from app.services.subscription_renewal_service import with_admin_notification_service

                await with_admin_notification_service(
                    lambda svc: svc.send_subscription_extension_notification(
                        db,
                        user,
                        subscription,
                        transaction,
                        1,  # 1 день для суточного тарифа
                        old_end_date,
                        new_end_date=subscription.end_date,
                        balance_after=user.balance_kopeks,
                    )
                )
            except Exception as exc:
                logger.warning('Не удалось отправить админ-уведомление о суточном списании', user_id=user.id, exc=exc)

            # Уведомляем пользователя
            if self._bot:
                await self._notify_daily_charge(user, subscription, daily_price)

            return 'charged'

        except Exception as e:
            await db.rollback()
            logger.error(
                'Ошибка при списании средств для подписки', subscription_id=subscription.id, error=e, exc_info=True
            )
            return 'error'

    async def _notify_daily_charge(self, user, subscription, amount_kopeks: int):
        """Уведомляет пользователя о суточном списании."""
        get_texts(getattr(user, 'language', 'ru'))
        amount_rubles = amount_kopeks / 100
        balance_rubles = user.balance_kopeks / 100

        tariff_label = ''
        if settings.is_multi_tariff_enabled() and hasattr(subscription, 'tariff') and subscription.tariff:
            tariff_label = f'\n📦 Тариф: «{subscription.tariff.name}»'
        message = (
            f'💳 <b>Суточное списание</b>\n\n'
            f'Списано: {amount_rubles:.2f} ₽\n'
            f'Остаток баланса: {balance_rubles:.2f} ₽{tariff_label}\n\n'
            f'Следующее списание через 24 часа.'
        )

        # Use unified notification delivery service
        try:
            await notification_delivery_service.notify_daily_debit(
                user=user,
                amount_kopeks=amount_kopeks,
                new_balance_kopeks=user.balance_kopeks,
                bot=self._bot,
                telegram_message=message,
            )
        except Exception as e:
            logger.warning('Не удалось отправить уведомление о списании', error=e)

    async def _notify_insufficient_balance(self, user, subscription, required_amount: int):
        """Уведомляет пользователя о недостатке средств."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        get_texts(getattr(user, 'language', 'ru'))
        required_rubles = required_amount / 100
        balance_rubles = user.balance_kopeks / 100

        tariff_label = ''
        if settings.is_multi_tariff_enabled() and hasattr(subscription, 'tariff') and subscription.tariff:
            tariff_label = f' «{subscription.tariff.name}»'
        message = (
            f'⚠️ <b>Подписка{tariff_label} приостановлена</b>\n\n'
            f'Недостаточно средств для суточной оплаты.\n\n'
            f'Требуется: {required_rubles:.2f} ₽\n'
            f'Баланс: {balance_rubles:.2f} ₽\n\n'
            f'Пополните баланс, чтобы возобновить подписку.'
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='💳 Пополнить баланс', callback_data='menu_balance')],
                [InlineKeyboardButton(text='📱 Моя подписка', callback_data='menu_subscription')],
            ]
        )

        # Use unified notification delivery service
        context = {
            'required_amount': f'{required_rubles:.2f} ₽',
            'current_balance': f'{balance_rubles:.2f} ₽',
        }

        try:
            await notification_delivery_service.send_notification(
                user=user,
                notification_type=NotificationType.DAILY_INSUFFICIENT_FUNDS,
                context=context,
                bot=self._bot,
                telegram_message=message,
                telegram_markup=keyboard,
            )
        except Exception as e:
            logger.warning('Не удалось отправить уведомление о недостатке средств', error=e)

    async def process_traffic_resets(self) -> dict:
        """
        Сбрасывает докупленный трафик у подписок, у которых истёк срок.

        Returns:
            dict: Статистика обработки
        """
        stats = {
            'checked': 0,
            'reset': 0,
            'errors': 0,
        }

        from app.database.models import TrafficPurchase

        try:
            async with AsyncSessionLocal() as db:
                try:
                    # Находим все истекшие докупки
                    now = datetime.now(UTC)
                    query = select(TrafficPurchase).where(TrafficPurchase.expires_at <= now)
                    result = await db.execute(query)
                    expired_purchases = result.scalars().all()
                    stats['checked'] = len(expired_purchases)

                    # Группируем по подпискам для обновления
                    subscriptions_to_update = {}
                    for purchase in expired_purchases:
                        if purchase.subscription_id not in subscriptions_to_update:
                            subscriptions_to_update[purchase.subscription_id] = []
                        subscriptions_to_update[purchase.subscription_id].append(purchase)

                    # Удаляем истекшие докупки и обновляем подписки
                    for subscription_id, purchases in subscriptions_to_update.items():
                        try:
                            await self._reset_subscription_traffic(db, subscription_id, purchases)
                            stats['reset'] += len(purchases)
                        except Exception as e:
                            logger.error(
                                'Ошибка сброса трафика подписки',
                                subscription_id=subscription_id,
                                error=e,
                                exc_info=True,
                            )
                            stats['errors'] += 1
                except Exception as e:
                    logger.error('Ошибка при обработке сброса трафика', error=e, exc_info=True)
                    await db.rollback()

        except Exception as e:
            logger.error('Ошибка при получении подписок для сброса трафика', error=e, exc_info=True)

        return stats

    async def _reset_subscription_traffic(self, db: AsyncSession, subscription_id: int, expired_purchases: list):
        """Сбрасывает истекшие докупки трафика у подписки."""
        from app.database.models import TrafficPurchase

        # Получаем подписку
        subscription_query = select(Subscription).where(Subscription.id == subscription_id)
        subscription_result = await db.execute(subscription_query)
        subscription = subscription_result.scalar_one_or_none()

        if not subscription:
            return

        # Считаем сколько ГБ нужно убрать
        total_expired_gb = sum(p.traffic_gb for p in expired_purchases)
        old_limit = subscription.traffic_limit_gb
        old_purchased = subscription.purchased_traffic_gb or 0

        # КРИТИЧЕСКАЯ ПРОВЕРКА: защита от некорректных данных
        if total_expired_gb > old_purchased:
            logger.error(
                '⚠️ ОШИБКА ДАННЫХ: подписка истекает ГБ, но purchased_traffic_gb ГБ. Сбрасываем только ГБ.',
                subscription_id=subscription.id,
                total_expired_gb=total_expired_gb,
                old_purchased=old_purchased,
                old_purchased_2=old_purchased,
            )
            total_expired_gb = old_purchased

        # Рассчитываем базовый лимит тарифа (без докупок)
        base_limit = old_limit - old_purchased

        # Получаем базовый лимит из тарифа для проверки
        if subscription.tariff_id:
            from app.database.crud.tariff import get_tariff_by_id

            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff:
                tariff_base_limit = tariff.traffic_limit_gb or 0
                # Проверяем, что базовый лимит не отрицательный
                if base_limit < 0:
                    logger.warning(
                        '⚠️ Базовый лимит отрицательный для подписки ГБ. Используем лимит из тарифа: ГБ',
                        subscription_id=subscription.id,
                        base_limit=base_limit,
                        tariff_base_limit=tariff_base_limit,
                    )
                    base_limit = tariff_base_limit

        # Защита от отрицательного базового лимита
        base_limit = max(0, base_limit)

        # Удаляем истекшие записи
        for purchase in expired_purchases:
            await db.delete(purchase)

        # Рассчитываем новый лимит
        new_purchased = old_purchased - total_expired_gb
        new_limit = base_limit + new_purchased

        # Двойная защита: новый лимит не может быть меньше базового
        if new_limit < base_limit:
            logger.error(
                '⚠️ КРИТИЧЕСКАЯ ОШИБКА: новый лимит ( ГБ) меньше базового ( ГБ). Устанавливаем базовый лимит.',
                new_limit=new_limit,
                base_limit=base_limit,
            )
            new_limit = base_limit
            new_purchased = 0

        # Обновляем подписку
        subscription.traffic_limit_gb = max(0, new_limit)
        subscription.purchased_traffic_gb = max(0, new_purchased)

        # Проверяем, остались ли активные докупки
        now = datetime.now(UTC)
        remaining_query = (
            select(TrafficPurchase)
            .where(TrafficPurchase.subscription_id == subscription_id)
            .where(TrafficPurchase.expires_at > now)
        )
        remaining_result = await db.execute(remaining_query)
        remaining_purchases = remaining_result.scalars().all()

        if not remaining_purchases:
            # Нет больше активных докупок - сбрасываем дату
            subscription.traffic_reset_at = None
        else:
            # Устанавливаем дату сброса по ближайшей истекающей докупке
            next_expiry = min(p.expires_at for p in remaining_purchases)
            subscription.traffic_reset_at = next_expiry

        subscription.updated_at = datetime.now(UTC)

        await db.commit()

        logger.info(
            '🔄 Сброс истекших докупок: подписка было ГБ (базовый: ГБ, докуплено: ГБ), стало ГБ (базовый: ГБ, докуплено: ГБ), убрано ГБ из покупок',
            subscription_id=subscription.id,
            old_limit=old_limit,
            base_limit=base_limit,
            old_purchased=old_purchased,
            traffic_limit_gb=subscription.traffic_limit_gb,
            base_limit_2=base_limit,
            new_purchased=new_purchased,
            total_expired_gb=total_expired_gb,
            expired_purchases_count=len(expired_purchases),
        )

        # Синхронизируем с RemnaWave
        try:
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            await subscription_service.update_remnawave_user(db, subscription)
        except Exception as e:
            logger.warning('Не удалось синхронизировать с RemnaWave после сброса трафика', error=e)
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            if hasattr(subscription, 'id') and hasattr(subscription, 'user_id'):
                remnawave_retry_queue.enqueue(
                    subscription_id=subscription.id,
                    user_id=subscription.user_id,
                    action='update',
                )

        # Уведомляем пользователя
        if self._bot and subscription.user_id:
            user = await get_user_by_id(db, subscription.user_id)
            if user:
                await self._notify_traffic_reset(user, subscription, total_expired_gb)

    async def _notify_traffic_reset(self, user: User, subscription: Subscription, reset_gb: int):
        """Уведомляет пользователя о сбросе докупленного трафика."""
        tariff_label = ''
        if settings.is_multi_tariff_enabled() and hasattr(subscription, 'tariff') and subscription.tariff:
            tariff_label = f'\n📦 Тариф: «{subscription.tariff.name}»'
        message = (
            f'ℹ️ <b>Сброс докупленного трафика</b>\n\n'
            f'Ваш докупленный трафик ({reset_gb} ГБ) был сброшен, '
            f'так как прошло 30 дней с момента первой докупки.{tariff_label}\n\n'
            f'Текущий лимит трафика: {subscription.traffic_limit_gb} ГБ\n\n'
            f'Вы можете докупить трафик снова в любое время.'
        )

        context = {
            'reset_gb': reset_gb,
            'current_limit_gb': subscription.traffic_limit_gb,
        }

        # Use unified notification delivery service
        try:
            await notification_delivery_service.send_notification(
                user=user,
                notification_type=NotificationType.TRAFFIC_RESET,
                context=context,
                bot=self._bot,
                telegram_message=message,
            )
        except Exception as e:
            logger.warning('Не удалось отправить уведомление о сбросе трафика', error=e)

    async def process_auto_resume(self) -> dict:
        """
        Возобновляет DISABLED суточные подписки, у которых появился достаточный баланс.
        Также восстанавливает EXPIRED подписки, ошибочно экспайренные другими системами.
        """
        stats = {'resumed': 0, 'recovered': 0, 'errors': 0}

        try:
            async with AsyncSessionLocal() as db:
                # 1. Возобновление DISABLED подписок (недостаточно средств → баланс пополнен)
                try:
                    disabled_subs = await get_disabled_daily_subscriptions_for_resume(db)
                    for subscription in disabled_subs:
                        try:
                            # Только активируем — НЕ ставим last_daily_charge_at,
                            # чтобы _process_single_charge корректно его обновил при списании.
                            # Если списание упадёт, подписка останется без last_daily_charge_at
                            # и будет подхвачена на следующем цикле.
                            subscription.status = SubscriptionStatus.ACTIVE.value
                            await db.commit()
                            await db.refresh(subscription)

                            logger.info(
                                '✅ Суточная подписка возобновлена (DISABLED→ACTIVE, баланс пополнен)',
                                subscription_id=subscription.id,
                                user_id=subscription.user_id,
                            )

                            # Списываем за первые сутки — charge обновит end_date и last_daily_charge_at
                            charge_result = await self._process_single_charge(db, subscription)
                            if charge_result == 'charged':
                                stats['resumed'] += 1
                            elif charge_result == 'error':
                                stats['errors'] += 1
                        except Exception as e:
                            logger.error(
                                'Ошибка возобновления DISABLED подписки',
                                subscription_id=subscription.id,
                                error=e,
                                exc_info=True,
                            )
                            stats['errors'] += 1
                except Exception as e:
                    logger.error('Ошибка при обработке DISABLED подписок', error=e, exc_info=True)

                # 2. Восстановление EXPIRED подписок (ошибочно экспайрены middleware/CRUD)
                try:
                    expired_subs = await get_expired_daily_subscriptions_for_recovery(db)
                    for subscription in expired_subs:
                        try:
                            # Восстанавливаем в ACTIVE — charge обновит end_date и last_daily_charge_at
                            subscription.status = SubscriptionStatus.ACTIVE.value
                            await db.commit()
                            await db.refresh(subscription)

                            logger.warning(
                                '🔄 Суточная подписка восстановлена (EXPIRED→ACTIVE, ошибочный expire)',
                                subscription_id=subscription.id,
                                user_id=subscription.user_id,
                            )

                            # Списываем за сутки
                            charge_result = await self._process_single_charge(db, subscription)
                            if charge_result == 'charged':
                                stats['recovered'] += 1
                            elif charge_result == 'error':
                                stats['errors'] += 1
                        except Exception as e:
                            logger.error(
                                'Ошибка восстановления EXPIRED подписки',
                                subscription_id=subscription.id,
                                error=e,
                                exc_info=True,
                            )
                            stats['errors'] += 1
                except Exception as e:
                    logger.error('Ошибка при обработке EXPIRED подписок', error=e, exc_info=True)

        except Exception as e:
            logger.error('Ошибка в process_auto_resume', error=e, exc_info=True)

        return stats

    async def start_monitoring(self):
        """Запускает периодическую проверку суточных подписок и сброса трафика."""
        self._running = True
        interval_minutes = self.get_check_interval_minutes()

        logger.info('🔄 Запуск сервиса суточных подписок (интервал: мин)', interval_minutes=interval_minutes)

        while self._running:
            try:
                # Восстановление DISABLED/EXPIRED подписок (до основных списаний!)
                resume_stats = await self.process_auto_resume()
                if resume_stats['resumed'] > 0 or resume_stats['recovered'] > 0:
                    logger.info(
                        '📊 Авто-возобновление: возобновлено=, восстановлено=, ошибок=',
                        resumed=resume_stats['resumed'],
                        recovered=resume_stats['recovered'],
                        errors=resume_stats['errors'],
                    )

                # Обработка суточных списаний
                stats = await self.process_daily_charges()

                if stats['charged'] > 0 or stats['suspended'] > 0:
                    logger.info(
                        '📊 Суточные списания: проверено=, списано=, приостановлено=, ошибок',
                        stats=stats['checked'],
                        stats_2=stats['charged'],
                        stats_3=stats['suspended'],
                        stats_4=stats['errors'],
                    )

                # Обработка сброса докупленного трафика
                traffic_stats = await self.process_traffic_resets()
                if traffic_stats['reset'] > 0:
                    logger.info(
                        '📊 Сброс трафика: проверено=, сброшено=, ошибок',
                        traffic_stats=traffic_stats['checked'],
                        traffic_stats_2=traffic_stats['reset'],
                        traffic_stats_3=traffic_stats['errors'],
                    )
            except Exception as e:
                logger.error('Ошибка в цикле проверки суточных подписок', error=e, exc_info=True)

            await asyncio.sleep(interval_minutes * 60)

    def stop_monitoring(self):
        """Останавливает периодическую проверку."""
        self._running = False
        logger.info('⏹️ Сервис суточных подписок остановлен')


# Глобальный экземпляр сервиса
daily_subscription_service = DailySubscriptionService()


__all__ = ['DailySubscriptionService', 'daily_subscription_service']
