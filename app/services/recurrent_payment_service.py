"""Сервис рекуррентных автоплатежей через YooKassa.

Находит подписки с autopay, у которых недостаточно баланса для продления,
и пополняет баланс с сохранённой карты. Существующий autopay в
monitoring_service затем спишет баланс и продлит подписку.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    Subscription,
    SubscriptionStatus,
    User,
    UserPromoGroup,
)


logger = structlog.get_logger(__name__)


@dataclass
class _DailyGuard:
    """Защита от повторной обработки подписок в рамках одного дня."""

    date: str = ''
    processed: set[str] = field(default_factory=set)

    def reset_if_new_day(self) -> None:
        today = datetime.now(UTC).strftime('%Y-%m-%d')
        if today != self.date:
            self.processed = set()
            self.date = today

    def is_processed(self, key: str) -> bool:
        return key in self.processed

    def mark_processed(self, key: str) -> None:
        self.processed.add(key)


_daily_guard = _DailyGuard()


def _build_extend_keyboard(texts) -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой продления подписки для уведомлений."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                    callback_data='subscription_extend',
                )
            ],
        ]
    )


async def process_recurrent_payments(db: AsyncSession, bot: Bot | None = None) -> dict:
    """
    Основная функция: находит подписки, которым скоро нужно продление,
    у которых недостаточно баланса, и пополняет баланс с сохранённой карты.

    Args:
        db: Сессия БД из вызывающего кода (_monitoring_cycle)
        bot: Экземпляр бота для уведомлений

    Returns:
        dict: Статистика обработки
    """
    if not settings.YOOKASSA_RECURRENT_ENABLED:
        return {'skipped': True, 'reason': 'recurrent_disabled'}

    if not settings.YOOKASSA_ENABLED:
        return {'skipped': True, 'reason': 'yookassa_disabled'}

    if not settings.ENABLE_AUTOPAY:
        return {'skipped': True, 'reason': 'autopay_disabled'}

    _daily_guard.reset_if_new_day()

    stats = {
        'checked': 0,
        'payments_created': 0,
        'insufficient_no_card': 0,
        'all_cards_failed': 0,
        'already_processed': 0,
        'errors': 0,
    }

    # Создаём сервисы один раз для всех подписок
    from app.services.payment_service import PaymentService
    from app.services.subscription_service import SubscriptionService

    payment_service = PaymentService()
    subscription_service = SubscriptionService()

    try:
        subscriptions = await _find_subscriptions_needing_topup(db)
        stats['checked'] = len(subscriptions)

        for subscription in subscriptions:
            user = subscription.user
            if not user:
                continue

            guard_key = f'{user.id}_{subscription.id}'
            if _daily_guard.is_processed(guard_key):
                stats['already_processed'] += 1
                continue

            try:
                result = await _process_single_subscription(
                    db,
                    subscription,
                    user,
                    bot,
                    payment_service,
                    subscription_service,
                )
                if result == 'created':
                    stats['payments_created'] += 1
                    _daily_guard.mark_processed(guard_key)
                elif result == 'no_card':
                    stats['insufficient_no_card'] += 1
                    _daily_guard.mark_processed(guard_key)
                elif result == 'all_cards_failed':
                    stats['all_cards_failed'] += 1
                    _daily_guard.mark_processed(guard_key)
                elif result == 'skipped':
                    stats['already_processed'] += 1
            except Exception as e:
                stats['errors'] += 1
                logger.error(
                    'Ошибка обработки рекуррентного платежа',
                    subscription_id=subscription.id,
                    user_id=user.id,
                    error=e,
                    exc_info=True,
                )
    except Exception as e:
        logger.error('Ошибка получения подписок для рекуррентных платежей', error=e, exc_info=True)
        stats['errors'] += 1

    if stats['payments_created'] > 0 or stats['errors'] > 0:
        logger.info('Рекуррентные платежи: итоги', **stats)

    return stats


async def _find_subscriptions_needing_topup(db: AsyncSession) -> list:
    """Находит подписки с autopay, которым скоро нужно продление."""
    current_time = datetime.now(UTC)
    max_days_before = settings.DEFAULT_AUTOPAY_DAYS_BEFORE

    # Максимальный горизонт проверки
    check_horizon = current_time + timedelta(days=max_days_before + 1)

    recently_expired_threshold = current_time - timedelta(hours=48)

    result = await db.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.user).options(
                selectinload(User.promo_group),
                selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            ),
            selectinload(Subscription.tariff),
        )
        .where(
            and_(
                or_(
                    and_(
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.end_date <= check_horizon,
                    ),
                    and_(
                        Subscription.status == SubscriptionStatus.EXPIRED.value,
                        Subscription.end_date >= recently_expired_threshold,
                    ),
                ),
                Subscription.autopay_enabled == True,
                Subscription.is_trial == False,
            )
        )
    )
    return list(result.scalars().all())


async def _process_single_subscription(
    db: AsyncSession,
    subscription: Subscription,
    user: User,
    bot: Bot | None,
    payment_service,
    subscription_service,
) -> str:
    """
    Обрабатывает одну подписку: проверяет баланс, находит карту, создаёт автоплатёж.

    Returns:
        'created' — автоплатёж создан
        'no_card' — нет сохранённой карты
        'all_cards_failed' — все карты не сработали
        'skipped' — баланс достаточен или другая причина пропуска
    """
    from app.database.crud.saved_payment_method import get_active_payment_methods_by_user

    # Рассчитываем стоимость продления
    tariff = getattr(subscription, 'tariff', None)
    if tariff:
        autopay_period = tariff.get_shortest_period() or 30
    else:
        autopay_period = 30

    try:
        from app.services.pricing_engine import pricing_engine

        pricing = await pricing_engine.calculate_renewal_price(
            db,
            subscription,
            autopay_period,
            user=user,
        )
        renewal_cost = pricing.final_total
    except Exception as e:
        logger.error(
            'Ошибка расчёта стоимости для рекуррентного платежа',
            subscription_id=subscription.id,
            user_id=user.id,
            error=e,
        )
        return 'skipped'

    if renewal_cost <= 0:
        return 'skipped'

    # Проверяем, хватает ли баланса
    shortage = renewal_cost - user.balance_kopeks
    if shortage <= 0:
        # Баланса достаточно, обычный autopay справится
        return 'skipped'

    # Используем autopay_days_before конкретной подписки, если задан
    days_before = getattr(subscription, 'autopay_days_before', None) or settings.DEFAULT_AUTOPAY_DAYS_BEFORE
    days_until_expiry = (subscription.end_date - datetime.now(UTC)).total_seconds() / 86400
    if days_until_expiry > days_before and subscription.status != SubscriptionStatus.EXPIRED.value:
        return 'skipped'

    # Нужно пополнить баланс — ищем сохранённую карту
    saved_methods = await get_active_payment_methods_by_user(db, user.id)
    if not saved_methods:
        return 'no_card'

    # Сумма пополнения = нехватка (минимум YOOKASSA_MIN_AMOUNT_KOPEKS)
    min_amount = settings.YOOKASSA_MIN_AMOUNT_KOPEKS
    topup_amount_kopeks = max(shortage, min_amount)
    topup_amount_rubles = topup_amount_kopeks / 100

    # Создаём автоплатёж
    yookassa_service = payment_service.yookassa_service
    if not yookassa_service or not yookassa_service.configured:
        logger.warning('YooKassa сервис не сконфигурирован для рекуррентных платежей')
        return 'skipped'

    description = settings.get_balance_payment_description(topup_amount_kopeks)
    metadata = {
        'user_id': str(user.id),
        'user_telegram_id': str(user.telegram_id) if user.telegram_id else '',
        'purpose': 'recurrent_topup',
        'subscription_id': str(subscription.id),
        'source': 'recurrent_payment_service',
    }

    # Перебираем все сохранённые карты пока не найдём рабочую
    today = datetime.now(UTC).strftime('%Y-%m-%d')
    for saved_method in saved_methods:
        # Детерминированный ключ: при рестарте/повторе YooKassa вернёт тот же платёж
        idem_key = f'recurrent_{subscription.id}_{saved_method.id}_{today}'
        result = await yookassa_service.create_autopayment(
            amount=topup_amount_rubles,
            currency='RUB',
            description=description,
            payment_method_id=saved_method.yookassa_payment_method_id,
            metadata=metadata,
            idempotence_key=idem_key,
        )

        if not result:
            card_display = f'*{saved_method.card_last4}' if saved_method.card_last4 else ''
            logger.warning(
                'Не удалось списать с карты, пробуем следующую',
                user_id=user.id,
                subscription_id=subscription.id,
                payment_method_id=saved_method.yookassa_payment_method_id,
                card_display=card_display,
            )
            continue

        # Успешно — сохраняем локальную запись с привязкой к YooKassa ID
        try:
            from app.database.crud.yookassa import create_yookassa_payment

            yookassa_created_at = None
            if result.get('created_at'):
                try:
                    yookassa_created_at = datetime.fromisoformat(result['created_at'].replace('Z', '+00:00'))
                except Exception:
                    pass

            result_payment = await create_yookassa_payment(
                db=db,
                user_id=user.id,
                yookassa_payment_id=result['id'],
                amount_kopeks=topup_amount_kopeks,
                currency='RUB',
                description=description,
                status=result.get('status', 'pending'),
                metadata_json=metadata,
                yookassa_created_at=yookassa_created_at,
                test_mode=result.get('test_mode', False),
            )
            if result_payment:
                logger.info(
                    'Рекуррентный автоплатёж создан',
                    user_id=user.id,
                    subscription_id=subscription.id,
                    amount_kopeks=topup_amount_kopeks,
                    yookassa_payment_id=result['id'],
                )
        except Exception as e:
            logger.warning('Ошибка создания локальной записи рекуррентного платежа', error=e)

        # Уведомляем пользователя
        if bot and user.telegram_id:
            try:
                from app.localization.texts import get_texts

                texts = get_texts(user.language)
                payment_status = result.get('status', '')
                if result.get('paid'):
                    keyboard = _build_extend_keyboard(texts)
                    msg = texts.t(
                        'RECURRENT_TOPUP_SUCCESS',
                        '✅ <b>Автоплатёж выполнен</b>\n\nБаланс пополнен на {amount} для продления подписки.',
                    ).format(amount=settings.format_price(topup_amount_kopeks))
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=msg,
                        parse_mode='HTML',
                        reply_markup=keyboard,
                    )
                elif payment_status == 'pending':
                    logger.info(
                        'Рекуррентный платёж в обработке',
                        user_id=user.id,
                        yookassa_payment_id=result.get('id'),
                    )
            except Exception as notify_error:
                logger.warning('Ошибка уведомления об автоплатеже', notify_error=notify_error)

        return 'created'

    # Все карты не сработали — уведомляем пользователя
    if bot and user.telegram_id:
        try:
            from app.localization.texts import get_texts

            texts = get_texts(user.language)
            keyboard = _build_extend_keyboard(texts)
            msg = texts.t(
                'RECURRENT_TOPUP_FAILED',
                '❌ <b>Автоплатёж не удался</b>\n\nНе удалось списать {amount} ни с одной сохранённой карты для продления подписки.\n\nПополните баланс вручную, чтобы подписка не прервалась.',
            ).format(amount=settings.format_price(topup_amount_kopeks))
            await bot.send_message(
                chat_id=user.telegram_id,
                text=msg,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        except Exception as notify_error:
            logger.warning('Ошибка уведомления о неудачном автоплатеже', notify_error=notify_error)

    return 'all_cards_failed'
