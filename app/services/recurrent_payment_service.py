"""Сервис рекуррентных автоплатежей через YooKassa.

Находит подписки с autopay, у которых недостаточно баланса для продления,
и пополняет баланс с сохранённой карты. Существующий autopay в
monitoring_service затем спишет баланс и продлит подписку.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import (
    SavedPaymentMethod,
    Subscription,
    SubscriptionStatus,
    User,
    UserPromoGroup,
)


logger = structlog.get_logger(__name__)

# Redis-like in-memory защита от дублей (сбрасывается при рестарте)
_processed_today: set[str] = set()
_processed_date: str = ''


def _reset_daily_guard() -> None:
    global _processed_today, _processed_date
    today = datetime.now(UTC).strftime('%Y-%m-%d')
    if today != _processed_date:
        _processed_today = set()
        _processed_date = today


async def process_recurrent_payments(bot: Bot | None = None) -> dict:
    """
    Основная функция: находит подписки, которым скоро нужно продление,
    у которых недостаточно баланса, и пополняет баланс с сохранённой карты.

    Returns:
        dict: Статистика обработки
    """
    if not settings.YOOKASSA_RECURRENT_ENABLED:
        return {'skipped': True, 'reason': 'recurrent_disabled'}

    if not settings.YOOKASSA_ENABLED:
        return {'skipped': True, 'reason': 'yookassa_disabled'}

    if not settings.ENABLE_AUTOPAY:
        return {'skipped': True, 'reason': 'autopay_disabled'}

    _reset_daily_guard()

    stats = {
        'checked': 0,
        'payments_created': 0,
        'insufficient_no_card': 0,
        'already_processed': 0,
        'errors': 0,
    }

    try:
        async with AsyncSessionLocal() as db:
            try:
                subscriptions = await _find_subscriptions_needing_topup(db)
                stats['checked'] = len(subscriptions)

                for subscription in subscriptions:
                    user = subscription.user
                    if not user:
                        continue

                    guard_key = f'{user.id}_{subscription.id}'
                    if guard_key in _processed_today:
                        stats['already_processed'] += 1
                        continue

                    try:
                        result = await _process_single_subscription(db, subscription, user, bot)
                        if result == 'created':
                            stats['payments_created'] += 1
                            _processed_today.add(guard_key)
                        elif result == 'no_card':
                            stats['insufficient_no_card'] += 1
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

    except Exception as e:
        logger.error('Критическая ошибка рекуррентных платежей', error=e, exc_info=True)
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
) -> str:
    """
    Обрабатывает одну подписку: проверяет баланс, находит карту, создаёт автоплатёж.

    Returns:
        'created' — автоплатёж создан
        'no_card' — нет сохранённой карты
        'skipped' — баланс достаточен или другая причина пропуска
    """
    from app.database.crud.saved_payment_method import get_active_payment_methods_by_user
    from app.services.payment_service import PaymentService
    from app.services.subscription_service import SubscriptionService

    # Рассчитываем стоимость продления
    tariff = getattr(subscription, 'tariff', None)
    if tariff:
        autopay_period = tariff.get_shortest_period() or 30
    else:
        autopay_period = 30

    subscription_service = SubscriptionService()

    try:
        renewal_cost = await subscription_service.calculate_renewal_price(
            subscription,
            autopay_period,
            db,
            user=user,
        )
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

    # Нужно пополнить баланс — ищем сохранённую карту
    saved_methods = await get_active_payment_methods_by_user(db, user.id)
    if not saved_methods:
        return 'no_card'

    # Сумма пополнения = нехватка (минимум YOOKASSA_MIN_AMOUNT_KOPEKS)
    min_amount = settings.YOOKASSA_MIN_AMOUNT_KOPEKS
    topup_amount_kopeks = max(shortage, min_amount)
    topup_amount_rubles = topup_amount_kopeks / 100

    # Создаём автоплатёж
    payment_service = PaymentService()
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
    for saved_method in saved_methods:
        result = await yookassa_service.create_autopayment(
            amount=topup_amount_rubles,
            currency='RUB',
            description=description,
            payment_method_id=saved_method.yookassa_payment_method_id,
            metadata=metadata,
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

        # Успешно — создаём локальную запись платежа
        try:
            result_payment = await payment_service.create_yookassa_payment(
                db=db,
                user_id=user.id,
                amount_kopeks=topup_amount_kopeks,
                description=description,
                metadata=metadata,
            )
            if result_payment:
                logger.info(
                    'Рекуррентный автоплатёж создан',
                    user_id=user.id,
                    subscription_id=subscription.id,
                    amount_kopeks=topup_amount_kopeks,
                    yookassa_payment_id=result.get('id'),
                )
        except Exception as e:
            logger.warning('Ошибка создания локальной записи рекуррентного платежа', error=e)

        # Уведомляем пользователя
        if bot and user.telegram_id:
            try:
                from app.localization.texts import get_texts

                texts = get_texts(user.language)
                status = result.get('status', '')
                if result.get('paid'):
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                                    callback_data='subscription_extend',
                                )
                            ],
                        ]
                    )
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
                elif status == 'pending':
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
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                            callback_data='subscription_extend',
                        )
                    ],
                ]
            )
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

    return 'skipped'
