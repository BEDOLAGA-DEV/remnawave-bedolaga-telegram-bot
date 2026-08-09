"""Сервис рекуррентных автоплатежей через сохранённые карты.

Находит подписки с autopay, у которых недостаточно баланса для продления,
и пополняет баланс с сохранённой карты. Поставщик карты выбирается по
``saved_payment_methods.provider`` — поддерживаются YooKassa и EtoPlatezhi.
Существующий autopay в monitoring_service затем спишет баланс и продлит
подписку.
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


def _build_extend_keyboard(texts, subscription_id: int | None = None) -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой продления подписки для уведомлений."""
    extend_callback = (
        f'se:{subscription_id}' if settings.is_multi_tariff_enabled() and subscription_id else 'subscription_extend'
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                    callback_data=extend_callback,
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
    # Provider-agnostic gate: proceed if ANY recurring provider (YooKassa or
    # EtoPlatezhi) is configured. Skip only when none can charge saved cards.
    from app.services.payment.recurring import is_any_recurring_enabled

    if not is_any_recurring_enabled():
        return {'skipped': True, 'reason': 'recurrent_disabled'}

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

        # _process_single_subscription внутри может flush/commit/savepoint rollback,
        # после чего subscription.user может стать недоступным (MissingGreenlet при
        # попытке lazy load вне greenlet'а). Снимаем id-снимок и в каждой итерации
        # перезагружаем подписку с eager-loaded relationships — N+1, но reliable
        # и не зависит от состояния сессии.
        subscription_ids = [sub.id for sub in subscriptions]

        for sub_id in subscription_ids:
            subscription = await _reload_subscription_with_user(db, sub_id)
            if not subscription:
                continue
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


async def _reload_subscription_with_user(db: AsyncSession, subscription_id: int) -> Subscription | None:
    """Получить подписку с eager-loaded user/promo_groups/tariff по id.

    Используется в loop'е process_recurrent_payments чтобы избежать MissingGreenlet
    при доступе к subscription.user после flush/commit/rollback внутри обработчика.
    """
    result = await db.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.user).options(
                selectinload(User.promo_group),
                selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            ),
            selectinload(Subscription.tariff),
        )
        .where(Subscription.id == subscription_id)
    )
    return result.scalar_one_or_none()


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
                # Layer 2 sanity-guard: подписка должна "пожить" хотя бы 12ч
                # перед тем как мы начнём списывать через recurring карту.
                # Защищает от каскада "юзер только что взял триал → дубль →
                # extend_subscription flip is_trial=False → autopay сразу
                # списывает". Кейс: 19₽ trial → 7 мин → 299₽ recurring.
                Subscription.start_date <= current_time - timedelta(hours=12),
            )
        )
    )
    return list(result.scalars().all())


# Сколько раз за сутки готовы стучаться по одной паре (подписка, карта).
MAX_DAILY_CHARGE_ATTEMPTS = 3

# Статусы строки платежа в нашей БД.
_ROW_PAID_STATUSES = {'success', 'paid'}
_ROW_INFLIGHT_STATUSES = {'pending'}


def _attempt_order_ids(base_key: str) -> list[str]:
    """Все возможные order_id попыток для одной пары (подписка, карта) за сутки."""
    return [base_key] + [f'{base_key}r{n}' for n in range(2, MAX_DAILY_CHARGE_ATTEMPTS + 1)]


def _next_attempt_key(base_key: str, rows: list) -> tuple[str | None, str]:
    """С каким payment_id идти в шлюз. Второй элемент — причина отказа идти.

    Причина важна вызывающему: `paid`/`inflight` означают «карта отработала,
    остальные перебирать не надо», а `exhausted` — «по этой карте на сегодня
    всё, но следующую попробовать стоит».

    ЭП требует уникальный payment_id в рамках проекта и отбивает повтор кодом
    3041 «Payment ID already exists», НЕ пытаясь списать деньги. Раньше мы
    повторяли попытку с тем же ключом: живого списания не происходило, а статус
    строки перезаписывался безликим `error` поверх настоящей причины отказа
    (нет средств / карта заблокирована). Поэтому каждая новая попытка получает
    собственный ключ и собственную строку.
    """
    if any(getattr(r, 'is_paid', False) or (r.status or '').lower() in _ROW_PAID_STATUSES for r in rows):
        return None, 'paid'  # второй charge был бы дублем списания
    if any((r.status or '').lower() in _ROW_INFLIGHT_STATUSES for r in rows):
        return None, 'inflight'  # попытка ещё в полёте, ждём вебхук
    if len(rows) >= MAX_DAILY_CHARGE_ATTEMPTS:
        return None, 'exhausted'
    if not rows:
        return base_key, 'first'
    return f'{base_key}r{len(rows) + 1}', 'retry'


async def _charge_etoplatezhi_card(
    db: AsyncSession,
    subscription: Subscription,
    user: User,
    bot: Bot | None,
    saved_method,
    *,
    topup_amount_kopeks: int,
    description: str,
    metadata: dict,
    idem_key: str,
) -> str:
    """Списать пополнение с сохранённой EtoPlatezhi-карты (recurring_id).

    Возвращает 'created' при успешной инициации списания, иначе 'failed'
    (вызывающий цикл пробует следующую карту).
    """
    from app.database.crud.etoplatezhi import (
        create_etoplatezhi_payment,
        get_etoplatezhi_payment_by_order_id,
        get_recurrent_attempts,
        set_etoplatezhi_payment_id_if_missing,
        update_etoplatezhi_payment_status,
    )
    from app.services.payment.recurring import get_provider

    provider = get_provider('etoplatezhi')
    if not provider or not provider.is_enabled():
        return 'failed'

    provider_token = saved_method.provider_token or saved_method.yookassa_payment_method_id
    if not provider_token:
        return 'failed'

    # Per-card method_code so the provider routes to the correct recurring
    # endpoint (card-partner / sberpay / sbp-qr / yoomoney-wallet).
    per_card_meta = dict(metadata)
    per_card_meta['method_code'] = getattr(saved_method, 'method_code', None)

    # Предсоздать pending-запись и закоммитить ДО charge: webhook ищет платёж
    # по order_id из новой сессии — row должен быть виден до коллбека, иначе
    # пополнение молча потеряется.
    try:
        attempts = await get_recurrent_attempts(db, _attempt_order_ids(idem_key))
        attempt_key, reason = _next_attempt_key(idem_key, attempts)
        if attempt_key is None:
            logger.info(
                'Etoplatezhi рекуррент: повторный charge не нужен',
                user_id=user.id,
                subscription_id=subscription.id,
                base_order_id=idem_key,
                reason=reason,
                attempts=len(attempts),
                statuses=[a.status for a in attempts],
            )
            # exhausted — по этой карте на сегодня всё, но следующую карту юзера
            # попробовать стоит. paid/inflight — карта отработала, перебор стоп.
            return 'failed' if reason == 'exhausted' else 'created'

        idem_key = attempt_key
        await create_etoplatezhi_payment(
            db=db,
            user_id=user.id,
            order_id=idem_key,
            amount_kopeks=topup_amount_kopeks,
            currency='RUB',
            description=description,
            payment_method=getattr(saved_method, 'method_code', None) or 'card-partner',
            metadata_json=per_card_meta,
        )
    except Exception as e:
        logger.warning(
            'Не удалось предсоздать запись etoplatezhi, пропускаем карту',
            user_id=user.id,
            subscription_id=subscription.id,
            error=e,
        )
        return 'failed'

    charge = await provider.charge(
        provider_token=str(provider_token),
        amount_kopeks=topup_amount_kopeks,
        description=description,
        metadata=per_card_meta,
        idempotency_key=idem_key,
        user_id=user.id,
    )

    if not charge.success:
        card_display = f'*{saved_method.card_last4}' if saved_method.card_last4 else ''
        logger.warning(
            'Не удалось списать с карты, пробуем следующую',
            user_id=user.id,
            subscription_id=subscription.id,
            provider='etoplatezhi',
            card_display=card_display,
            error=charge.error_message,
        )
        try:
            pending = await get_etoplatezhi_payment_by_order_id(db, idem_key)
            if pending and pending.status == 'pending':
                await update_etoplatezhi_payment_status(db, pending, status='failed')
        except Exception as mark_error:
            logger.warning('Не удалось пометить etoplatezhi платёж как failed', error=mark_error)
        return 'failed'

    # Дописываем provider_payment_id атомарным UPDATE — не трогает status/is_paid,
    # которые мог уже выставить параллельный webhook.
    if charge.provider_payment_id:
        try:
            await set_etoplatezhi_payment_id_if_missing(
                db,
                order_id=idem_key,
                etoplatezhi_payment_id=str(charge.provider_payment_id),
            )
        except Exception as e:
            logger.warning('Ошибка обновления etoplatezhi_payment_id', error=e)

    logger.info(
        'Рекуррентный автоплатёж создан',
        user_id=user.id,
        subscription_id=subscription.id,
        provider='etoplatezhi',
        amount_kopeks=topup_amount_kopeks,
        provider_payment_id=charge.provider_payment_id,
    )

    # Списание инициировано; фактическое пополнение баланса и уведомление об
    # успехе придут через webhook. Здесь — только best-effort лог/уведомление.
    if bot and user.telegram_id:
        try:
            from app.localization.texts import get_texts

            texts = get_texts(user.language)
            raw = charge.raw or {}
            if raw.get('paid'):
                keyboard = _build_extend_keyboard(texts, subscription.id)
                msg = texts.t(
                    'RECURRENT_TOPUP_SUCCESS',
                    '✅ <b>Автоплатёж выполнен</b>\n\nБаланс пополнен на {amount} для продления подписки.',
                ).format(amount=settings.format_price(topup_amount_kopeks))
                if settings.is_multi_tariff_enabled() and hasattr(subscription, 'tariff') and subscription.tariff:
                    msg += f'\n📦 Тариф: «{subscription.tariff.name}»'
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=msg,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
        except Exception as notify_error:
            logger.warning('Ошибка уведомления об автоплатеже', notify_error=notify_error)

    return 'created'


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
        from app.database.crud.user import lock_user_for_pricing
        from app.services.pricing_engine import pricing_engine

        # TOCTOU: lock user row before pricing to prevent concurrent promo/balance races
        user = await lock_user_for_pricing(db, user.id)

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

    # Предыдущая попытка ещё не разрешилась — ждём вебхук, второй charge
    # ушёл бы под новым payment_id (в ключе календарная дата) и списал дважды.
    from app.database.crud.etoplatezhi import get_unresolved_recurrent_payment

    unresolved = await get_unresolved_recurrent_payment(db, subscription.id)
    if unresolved:
        logger.info(
            'Рекуррент: предыдущая попытка ещё в pending, пропускаем проход',
            user_id=user.id,
            subscription_id=subscription.id,
            order_id=unresolved.order_id,
        )
        return 'skipped'

    # Нужно пополнить баланс — ищем сохранённую карту
    saved_methods = await get_active_payment_methods_by_user(db, user.id)
    if not saved_methods:
        return 'no_card'

    # Сумма пополнения = нехватка (минимум YOOKASSA_MIN_AMOUNT_KOPEKS)
    min_amount = settings.YOOKASSA_MIN_AMOUNT_KOPEKS
    topup_amount_kopeks = max(shortage, min_amount)
    topup_amount_rubles = topup_amount_kopeks / 100

    # YooKassa-сервис может быть не сконфигурирован (например, включён только
    # EtoPlatezhi recurrent). Проверяем это внутри цикла для yookassa-карт, не
    # блокируя весь проход целиком.
    yookassa_service = payment_service.yookassa_service

    description = settings.get_balance_payment_description(
        topup_amount_kopeks, telegram_user_id=user.telegram_id, user_db_id=user.id
    )
    metadata = {
        'user_id': str(user.id),
        'user_telegram_id': str(user.telegram_id) if user.telegram_id else '',
        'purpose': 'recurrent_topup',
        'subscription_id': str(subscription.id),
        'source': 'recurrent_payment_service',
        'customer_email': getattr(user, 'email', None) or '',
    }

    # Перебираем все сохранённые карты пока не найдём рабочую
    today = datetime.now(UTC).strftime('%Y-%m-%d')
    for saved_method in saved_methods:
        provider_name = getattr(saved_method, 'provider', None) or 'yookassa'
        # Детерминированный ключ: при рестарте/повторе платформа вернёт тот же платёж
        idem_key = f'recurrent_{subscription.id}_{saved_method.id}_{today}'

        # EtoPlatezhi — отдельный token-charge путь (saved card = recurring_id).
        if provider_name == 'etoplatezhi':
            outcome = await _charge_etoplatezhi_card(
                db,
                subscription,
                user,
                bot,
                saved_method,
                topup_amount_kopeks=topup_amount_kopeks,
                description=description,
                metadata=metadata,
                idem_key=idem_key,
            )
            if outcome == 'created':
                return 'created'
            continue

        # Неизвестный провайдер без локального charge-пути — пропускаем карту.
        if provider_name != 'yookassa':
            continue

        # --- YooKassa path (без изменений) ---
        if not yookassa_service or not yookassa_service.configured:
            logger.warning('YooKassa сервис не сконфигурирован для рекуррентных платежей')
            continue

        provider_token = saved_method.provider_token or saved_method.yookassa_payment_method_id
        result = await yookassa_service.create_autopayment(
            amount=topup_amount_rubles,
            currency='RUB',
            description=description,
            payment_method_id=provider_token,
            metadata=metadata,
            idempotence_key=idem_key,
        )

        if not result:
            card_display = f'*{saved_method.card_last4}' if saved_method.card_last4 else ''
            logger.warning(
                'Не удалось списать с карты, пробуем следующую',
                user_id=user.id,
                subscription_id=subscription.id,
                payment_method_id=provider_token,
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
                    keyboard = _build_extend_keyboard(texts, subscription.id)
                    msg = texts.t(
                        'RECURRENT_TOPUP_SUCCESS',
                        '✅ <b>Автоплатёж выполнен</b>\n\nБаланс пополнен на {amount} для продления подписки.',
                    ).format(amount=settings.format_price(topup_amount_kopeks))
                    if settings.is_multi_tariff_enabled() and hasattr(subscription, 'tariff') and subscription.tariff:
                        msg += f'\n📦 Тариф: «{subscription.tariff.name}»'
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

        if (
            settings.RECURRING_SUCCESS_EMAIL_ENABLED
            and not user.telegram_id
            and result.get('paid')
            and user.email
            and getattr(user, 'email_verified', False)
        ):
            try:
                from app.services.notification_delivery_service import (
                    NotificationType,
                    notification_delivery_service,
                )

                # Не AUTOPAY_SUCCESS: его шаблон пишет «подписка продлена до X»,
                # а этот шаг лишь пополняет баланс картой — продление сделает
                # отдельный джоб, и end_date здесь ещё старый. PAYMENT_RECEIVED
                # честно сообщает «платёж получен, баланс пополнен».
                await notification_delivery_service.send_notification(
                    user=user,
                    notification_type=NotificationType.PAYMENT_RECEIVED,
                    context={
                        'amount_kopeks': topup_amount_kopeks,
                        'amount_rubles': topup_amount_kopeks / 100,
                        'formatted_amount': settings.format_price(topup_amount_kopeks),
                    },
                )
            except Exception as email_error:
                logger.warning('Ошибка email-уведомления об автоплатеже', email_error=email_error)

        return 'created'

    # Все карты не сработали — уведомляем пользователя
    if bot and user.telegram_id:
        try:
            from app.localization.texts import get_texts

            texts = get_texts(user.language)
            keyboard = _build_extend_keyboard(texts, subscription.id)
            msg = texts.t(
                'RECURRENT_TOPUP_FAILED',
                '❌ <b>Автоплатёж не удался</b>\n\nНе удалось списать {amount} ни с одной сохранённой карты для продления подписки.\n\nПополните баланс вручную, чтобы подписка не прервалась.',
            ).format(amount=settings.format_price(topup_amount_kopeks))
            if settings.is_multi_tariff_enabled() and hasattr(subscription, 'tariff') and subscription.tariff:
                msg += f'\n📦 Тариф: «{subscription.tariff.name}»'
            await bot.send_message(
                chat_id=user.telegram_id,
                text=msg,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        except Exception as notify_error:
            logger.warning('Ошибка уведомления о неудачном автоплатеже', notify_error=notify_error)

    if not user.telegram_id and user.email and getattr(user, 'email_verified', False):
        try:
            from app.services.notification_delivery_service import (
                notification_delivery_service,
            )

            await notification_delivery_service.notify_autopay_failed(
                user=user,
                reason='',
            )
        except Exception as email_error:
            logger.warning('Ошибка email-уведомления о неудачном автоплатеже', email_error=email_error)

    return 'all_cards_failed'
