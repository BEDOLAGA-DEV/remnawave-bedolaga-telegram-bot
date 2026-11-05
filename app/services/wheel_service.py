"""
Сервис колеса удачи (Fortune Wheel) с RTP алгоритмом.
"""

import math
import random
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_subscription_by_user_id
from app.database.crud.user import add_user_balance
from app.database.crud.wheel import (
    create_wheel_spin,
    get_or_create_wheel_config,
    get_user_spins_today,
    get_wheel_prizes,
    get_wheel_statistics,
)
from app.database.models import (
    PromoCode,
    PromoCodeType,
    Subscription,
    User,
    WheelConfig,
    WheelPrize,
    WheelPrizeType,
    WheelSpinPaymentType,
)
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


@dataclass
class SpinResult:
    """Результат спина колеса."""

    success: bool
    prize_id: int | None = None
    prize_type: str | None = None
    prize_value: int = 0
    prize_display_name: str = ''
    emoji: str = '🎁'
    color: str = '#3B82F6'
    rotation_degrees: float = 0.0
    message: str = ''
    promocode: str | None = None
    error: str | None = None


@dataclass
class EligibleSubscription:
    """Подписка, доступная для оплаты колеса днями."""

    id: int
    tariff_name: str | None
    days_left: int


@dataclass
class SpinAvailability:
    """Доступность спина для пользователя."""

    can_spin: bool
    reason: str | None = None
    spins_remaining_today: int = 0
    can_pay_stars: bool = False
    can_pay_days: bool = False
    min_subscription_days: int = 0
    user_subscription_days: int = 0
    user_balance_kopeks: int = 0
    required_balance_kopeks: int = 0
    eligible_subscriptions: list[EligibleSubscription] | None = None


class FortuneWheelService:
    """Сервис колеса удачи с RTP механикой."""

    def __init__(self):
        pass

    async def check_availability(self, db: AsyncSession, user: User) -> SpinAvailability:
        """Проверить доступность спина для пользователя."""
        config = await get_or_create_wheel_config(db)

        # Колесо выключено
        if not config.is_enabled:
            return SpinAvailability(
                can_spin=False,
                reason='wheel_disabled',
            )

        # Проверяем лимит спинов
        spins_today = await get_user_spins_today(db, user.id)
        spins_remaining = config.daily_spin_limit - spins_today if config.daily_spin_limit > 0 else 999

        if config.daily_spin_limit > 0 and spins_today >= config.daily_spin_limit:
            return SpinAvailability(
                can_spin=False,
                reason='daily_limit_reached',
                spins_remaining_today=0,
            )

        # Проверяем доступные способы оплаты
        can_pay_stars = False
        can_pay_days = False
        user_subscription_days = 0
        required_balance_kopeks = 0

        # Проверяем оплату Stars (конвертируется в рубли из баланса)
        if config.spin_cost_stars_enabled and config.spin_cost_stars > 0:
            stars_rate = Decimal(str(settings.get_stars_rate()))
            rubles = Decimal(config.spin_cost_stars) * stars_rate
            required_balance_kopeks = int(rubles * 100)
            # Проверяем достаточно ли средств на балансе
            if user.balance_kopeks >= required_balance_kopeks:
                can_pay_stars = True

        eligible_subs: list[EligibleSubscription] = []
        if config.spin_cost_days_enabled:
            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            else:
                _single = await get_subscription_by_user_id(db, user.id)
                active_subs = [_single] if _single else []

            min_days_required = config.min_subscription_days_for_day_payment + config.spin_cost_days
            for sub in active_subs:
                if not sub.is_active:
                    continue
                # Exclude daily tariffs — they can't pay with days
                is_daily = sub.tariff and getattr(sub.tariff, 'is_daily', False)
                if is_daily:
                    continue
                if sub.days_left >= min_days_required:
                    tariff_name = sub.tariff.name if sub.tariff else None
                    eligible_subs.append(
                        EligibleSubscription(id=sub.id, tariff_name=tariff_name, days_left=sub.days_left)
                    )

            if eligible_subs:
                can_pay_days = True
                # For backward compat: use best subscription's days
                user_subscription_days = max(s.days_left for s in eligible_subs)

        if not can_pay_stars and not can_pay_days:
            # Определяем причину
            reason = 'no_payment_method_available'
            if config.spin_cost_stars_enabled and user.balance_kopeks < required_balance_kopeks:
                reason = 'insufficient_balance'

            return SpinAvailability(
                can_spin=False,
                reason=reason,
                spins_remaining_today=spins_remaining,
                can_pay_stars=can_pay_stars,
                can_pay_days=can_pay_days,
                min_subscription_days=config.min_subscription_days_for_day_payment,
                user_subscription_days=user_subscription_days,
                user_balance_kopeks=user.balance_kopeks,
                required_balance_kopeks=required_balance_kopeks,
                eligible_subscriptions=eligible_subs or None,
            )

        # Проверяем наличие призов
        prizes = await get_wheel_prizes(db, config.id, active_only=True)
        if not prizes:
            return SpinAvailability(
                can_spin=False,
                reason='no_prizes_configured',
            )

        return SpinAvailability(
            can_spin=True,
            spins_remaining_today=spins_remaining,
            can_pay_stars=can_pay_stars,
            can_pay_days=can_pay_days,
            min_subscription_days=config.min_subscription_days_for_day_payment,
            user_subscription_days=user_subscription_days,
            user_balance_kopeks=user.balance_kopeks,
            required_balance_kopeks=required_balance_kopeks,
            eligible_subscriptions=eligible_subs or None,
        )

    def calculate_prize_probabilities(
        self, config: WheelConfig, prizes: list[WheelPrize], spin_cost_kopeks: int
    ) -> list[tuple[WheelPrize, float]]:
        """
        Рассчитать вероятности выпадения призов на основе RTP.

        Алгоритм:
        1. Целевая средняя выплата = spin_cost * (RTP / 100)
        2. Для призов с manual_probability - используем его напрямую
        3. Из остатка бюджета выводим вероятности обратно пропорционально стоимости приза
        4. Нулевые призы делят оставшуюся вероятность; недостижимая цель логируется
        """
        if not prizes:
            return []

        target_payout = spin_cost_kopeks * (config.rtp_percent / 100)
        manual_prizes = []
        auto_prizes = []
        for prize in prizes:
            if prize.manual_probability is not None and prize.manual_probability > 0:
                manual_prizes.append((prize, prize.manual_probability))
            else:
                auto_prizes.append(prize)

        manual_prob_sum = math.fsum(prob for _, prob in manual_prizes)
        manual_payout = math.fsum(prize.prize_value_kopeks * prob for prize, prob in manual_prizes)
        remaining_prob = max(0.0, 1.0 - manual_prob_sum)
        reasons = []

        if not auto_prizes or remaining_prob <= 0:
            # Preserve the existing relative-weight policy for manual-only tables.
            result = [(prize, prob / manual_prob_sum) for prize, prob in manual_prizes] if manual_prob_sum else []
            if not math.isclose(manual_prob_sum, 1.0):
                reasons.append('manual_probabilities_normalized')
        else:
            paid = [prize for prize in auto_prizes if prize.prize_value_kopeks > 0]
            zero_count = len(auto_prizes) - len(paid)
            inverse_sum = math.fsum(1.0 / prize.prize_value_kopeks for prize in paid)
            if not zero_count:
                # Without a zero-value outcome all probability must pay out.
                # Keep inverse-value weighting, but report that RTP is not controllable.
                reasons.append('no_zero_value_auto_prize')
                result = manual_prizes + [
                    (prize, remaining_prob / (prize.prize_value_kopeks * inverse_sum)) for prize in auto_prizes
                ]
            else:
                budget = max(0.0, target_payout - manual_payout)
                scale = budget / len(paid) if paid else 0.0
                if inverse_sum and scale * inverse_sum > remaining_prob:
                    scale = remaining_prob / inverse_sum
                    reasons.append('insufficient_probability_for_target')
                paid_probability = math.fsum(scale / prize.prize_value_kopeks for prize in paid)
                zero_probability = max(0.0, remaining_prob - paid_probability) / zero_count
                result = manual_prizes + [
                    (prize, scale / prize.prize_value_kopeks if prize.prize_value_kopeks > 0 else zero_probability)
                    for prize in auto_prizes
                ]
                if manual_payout > target_payout:
                    reasons.append('manual_payout_exceeds_target')

        actual_payout = math.fsum(prize.prize_value_kopeks * prob for prize, prob in result)
        if not math.isclose(actual_payout, target_payout, rel_tol=1e-9, abs_tol=1e-7):
            reasons.append('target_unreachable')
        if reasons:
            logger.warning(
                'Wheel RTP target unreachable for prize configuration',
                config_id=getattr(config, 'id', None),
                reasons=reasons,
                target_payout_kopeks=target_payout,
                expected_payout_kopeks=actual_payout,
                spin_cost_kopeks=spin_cost_kopeks,
            )
        return result

    def _select_prize(self, prizes_with_probabilities: list[tuple[WheelPrize, float]]) -> WheelPrize:
        """Выбрать приз на основе вероятностей."""
        if not prizes_with_probabilities:
            raise ValueError('No prizes to select from')

        rand = random.random()
        cumulative = 0.0

        for prize, probability in prizes_with_probabilities:
            cumulative += probability
            if probability > 0 and rand < cumulative:
                return prize

        # Floating-point accumulation may finish just below one; never select a zero-probability outcome.
        for prize, probability in reversed(prizes_with_probabilities):
            if probability > 0:
                return prize
        raise ValueError('No prizes with positive probability')

    def _calculate_rotation(self, prizes: list[WheelPrize], selected_prize: WheelPrize) -> float:
        """
        Рассчитать угол поворота колеса для анимации.
        Возвращает градусы для CSS transform.
        """
        if not prizes:
            return 0.0

        # Находим индекс выбранного приза
        prize_index = next((i for i, p in enumerate(prizes) if p.id == selected_prize.id), 0)

        # Угол одного сектора
        sector_angle = 360 / len(prizes)

        # Базовый угол до центра сектора (от 12 часов по часовой)
        base_angle = prize_index * sector_angle + sector_angle / 2

        # Добавляем случайное смещение внутри сектора (не по краям)
        offset = random.uniform(-sector_angle * 0.3, sector_angle * 0.3)

        # Угол остановки (стрелка сверху, поэтому инвертируем)
        stop_angle = 360 - base_angle + offset

        # Добавляем несколько полных оборотов для эффекта
        full_rotations = random.randint(5, 8) * 360

        return full_rotations + stop_angle

    async def _process_stars_payment(self, db: AsyncSession, user: User, config: WheelConfig) -> int:
        """
        Обработать оплату Stars (списание эквивалента с баланса).
        Возвращает стоимость в копейках.
        """
        # Конвертируем Stars в рубли
        stars_rate = Decimal(str(settings.get_stars_rate()))
        rubles = Decimal(config.spin_cost_stars) * stars_rate
        kopeks = int(rubles * 100)

        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        if user.balance_kopeks < kopeks:
            raise ValueError('Недостаточно средств на балансе')

        # Списываем с баланса
        user.balance_kopeks -= kopeks
        logger.info(
            '💫 Списано ₽ (⭐) с баланса user_id',
            kopeks=round(kopeks / 100, 2),
            spin_cost_stars=config.spin_cost_stars,
            user_id=user.id,
        )

        return kopeks

    async def _process_days_payment(
        self, db: AsyncSession, user: User, config: WheelConfig, subscription: Subscription | None = None
    ) -> int:
        """
        Обработать оплату днями подписки.
        Возвращает эквивалент в копейках.
        """
        if not subscription:
            if settings.is_multi_tariff_enabled():
                raise ValueError('Необходимо указать подписку для оплаты днями (мульти-тариф)')
            subscription = await get_subscription_by_user_id(db, user.id)

        if not subscription or not subscription.is_active:
            raise ValueError('Нет активной подписки')

        if subscription.days_left < config.min_subscription_days_for_day_payment + config.spin_cost_days:
            raise ValueError('Недостаточно дней подписки')

        # Уменьшаем end_date
        subscription.end_date -= timedelta(days=config.spin_cost_days)
        subscription.updated_at = datetime.now(UTC)

        # Оцениваем стоимость в копейках (для статистики)
        # Берем цену 30-дневного периода и делим на 30
        from app.config import PERIOD_PRICES

        price_30_days = PERIOD_PRICES.get(30, settings.PRICE_30_DAYS) or 19900
        daily_price = price_30_days / 30
        kopeks = int(daily_price * config.spin_cost_days)

        logger.info('📅 Списано дней подписки у user_id', spin_cost_days=config.spin_cost_days, user_id=user.id)

        return kopeks

    async def _sync_subscription_after_spin(
        self,
        user: User,
        prize: WheelPrize,
        subscription: Subscription | None = None,
        *,
        payment_type: str | None = None,
    ) -> None:
        """Sync committed entitlements in a separate session; panel rollback cannot expire spin results."""
        spent_days = payment_type == WheelSpinPaymentType.SUBSCRIPTION_DAYS.value
        prize_type = prize.prize_type
        if not spent_days and prize_type not in (
            WheelPrizeType.SUBSCRIPTION_DAYS.value,
            WheelPrizeType.TRAFFIC_GB.value,
        ):
            return
        user_id = user.id
        subscription_id = subscription.id if subscription else None
        if subscription_id is None and settings.is_multi_tariff_enabled():
            return  # Untargeted multi-tariff prizes are converted to balance.
        try:
            from app.database.crud.subscription import get_subscription_by_id_for_user
            from app.database.database import AsyncSessionLocal

            async with AsyncSessionLocal() as sync_db:
                if subscription_id is not None:
                    current = await get_subscription_by_id_for_user(sync_db, subscription_id, user_id)
                else:
                    current = await get_subscription_by_user_id(sync_db, user_id)
                if current is None:
                    return
                daily = current.tariff and getattr(current.tariff, 'is_daily', False)
                days_changed = prize_type == WheelPrizeType.SUBSCRIPTION_DAYS.value and not daily
                traffic_changed = prize_type == WheelPrizeType.TRAFFIC_GB.value and current.traffic_limit_gb > 0
                if spent_days or days_changed or traffic_changed:
                    result = await SubscriptionService().update_remnawave_user(sync_db, current)
                    if result is None:
                        logger.warning('Wheel subscription panel sync did not complete', user_id=user_id)
        except Exception as error:
            logger.warning('Wheel subscription panel sync failed after committed spin', error=str(error))

    async def _apply_prize(
        self,
        db: AsyncSession,
        user: User,
        prize: WheelPrize,
        config: WheelConfig,
        subscription: Subscription | None = None,
    ) -> str | None:
        """
        Применить приз к пользователю.
        Возвращает промокод (если приз - промокод), иначе None.
        """
        prize_type = prize.prize_type

        if prize_type == WheelPrizeType.NOTHING.value:
            logger.info('🎰 Пустой приз для user_id', user_id=user.id)
            return None

        if prize_type == WheelPrizeType.BALANCE_BONUS.value:
            # Пополнение баланса
            await add_user_balance(
                db,
                user,
                prize.prize_value,
                description=f'Выигрыш в колесе удачи: {prize.prize_value / 100:.2f}₽',
                create_transaction=True,
                commit=False,
            )
            logger.info(
                '💰 Начислено ₽ на баланс user_id', prize_value=round(prize.prize_value / 100, 2), user_id=user.id
            )
            return None

        if prize_type == WheelPrizeType.SUBSCRIPTION_DAYS.value:
            # Дни подписки — use provided subscription or fallback
            if not subscription:
                if settings.is_multi_tariff_enabled():
                    # Multi-tariff: нельзя выбрать произвольную подписку, начисляем на баланс
                    await add_user_balance(
                        db,
                        user,
                        prize.prize_value_kopeks,
                        description=f'Выигрыш в колесе удачи: {prize.prize_value} дней (на баланс, мульти-тариф)',
                        create_transaction=True,
                        commit=False,
                    )
                    logger.info(
                        'Мульти-тариф: дни конвертированы в баланс (подписка не указана)',
                        prize_value=prize.prize_value,
                        user_id=user.id,
                    )
                    return None
                subscription = await get_subscription_by_user_id(db, user.id)
            if subscription:
                # Проверяем суточный тариф - для него конвертируем дни в баланс
                is_daily = getattr(subscription, 'is_daily', False) or (
                    subscription.tariff and getattr(subscription.tariff, 'is_daily', False)
                )

                if is_daily:
                    # Для суточных тарифов: дни * суточная_цена = баланс
                    daily_price = 0
                    if subscription.tariff and hasattr(subscription.tariff, 'daily_price_kopeks'):
                        daily_price = subscription.tariff.daily_price_kopeks or 0

                    if daily_price > 0:
                        balance_bonus = prize.prize_value * daily_price
                        await add_user_balance(
                            db,
                            user,
                            balance_bonus,
                            description=f'Выигрыш в колесе удачи: {prize.prize_value} дней → {balance_bonus / 100:.2f}₽',
                            create_transaction=True,
                            commit=False,
                        )
                        logger.info(
                            '💰 Суточный тариф: дней конвертированы в ₽ для user_id',
                            prize_value=prize.prize_value,
                            balance_bonus=round(balance_bonus / 100, 2),
                            user_id=user.id,
                        )
                    else:
                        # Если нет цены - используем prize_value_kopeks
                        await add_user_balance(
                            db,
                            user,
                            prize.prize_value_kopeks,
                            description=f'Выигрыш в колесе удачи: {prize.prize_value} дней (на баланс)',
                            create_transaction=True,
                            commit=False,
                        )
                        logger.info('💰 Дни конвертированы в баланс для user_id', user_id=user.id)
                else:
                    # Оверлей грейса, осевший в подписке (v4.10–4.11), — не её срок: вернуть до расчёта.
                    from app.services.grace_access_echo import undo_grace_overlay_echo

                    await undo_grace_overlay_echo(db, subscription)
                    # Обычная подписка - добавляем дни и синхронизируем с RemnaWave
                    subscription.end_date += timedelta(days=prize.prize_value)
                    subscription.updated_at = datetime.now(UTC)
                    # Условия тарифа на новый срок: база тарифа + активные докупки.
                    from app.database.crud.subscription import reconcile_tariff_traffic_limit

                    await reconcile_tariff_traffic_limit(db, subscription)
                    logger.info('📅 Начислено дней подписки user_id', prize_value=prize.prize_value, user_id=user.id)

            else:
                # Если нет подписки - начисляем на баланс эквивалент
                await add_user_balance(
                    db,
                    user,
                    prize.prize_value_kopeks,
                    description=f'Выигрыш в колесе удачи: {prize.prize_value} дней (на баланс)',
                    create_transaction=True,
                    commit=False,
                )
                logger.info('💰 Дни конвертированы в баланс для user_id', user_id=user.id)
            return None

        if prize_type == WheelPrizeType.TRAFFIC_GB.value:
            # Бонусный трафик — use provided subscription or fallback
            if not subscription:
                if settings.is_multi_tariff_enabled():
                    # Multi-tariff: нельзя выбрать произвольную подписку, начисляем на баланс
                    await add_user_balance(
                        db,
                        user,
                        prize.prize_value_kopeks,
                        description=f'Выигрыш в колесе удачи: {prize.prize_value}GB (на баланс, мульти-тариф)',
                        create_transaction=True,
                        commit=False,
                    )
                    logger.info(
                        'Мульти-тариф: трафик конвертирован в баланс (подписка не указана)',
                        prize_value=prize.prize_value,
                        user_id=user.id,
                    )
                    return None
                subscription = await get_subscription_by_user_id(db, user.id)
            if subscription and subscription.traffic_limit_gb > 0:
                subscription.traffic_limit_gb += prize.prize_value
                subscription.updated_at = datetime.now(UTC)
                logger.info('📊 Начислено трафика user_id', prize_value=prize.prize_value, user_id=user.id)

            else:
                # Если безлимит или нет подписки - на баланс
                await add_user_balance(
                    db,
                    user,
                    prize.prize_value_kopeks,
                    description=f'Выигрыш в колесе удачи: {prize.prize_value}GB (на баланс)',
                    create_transaction=True,
                    commit=False,
                )
            return None

        if prize_type == WheelPrizeType.PROMOCODE.value:
            # Генерация промокода
            promocode = await self._generate_prize_promocode(db, user, prize, config)
            logger.info('🎟️ Сгенерирован промокод для user_id', code=promocode.code, user_id=user.id)
            return promocode.code

        return None

    async def _generate_prize_promocode(
        self, db: AsyncSession, user: User, prize: WheelPrize, config: WheelConfig
    ) -> PromoCode:
        """Сгенерировать уникальный промокод для приза."""
        # Генерируем уникальный код
        code = f'{config.promo_prefix}{secrets.token_hex(4).upper()}'

        # Определяем тип промокода
        if prize.promo_subscription_days > 0:
            promo_type = PromoCodeType.SUBSCRIPTION_DAYS.value
        else:
            promo_type = PromoCodeType.BALANCE.value

        promocode = PromoCode(
            code=code,
            type=promo_type,
            balance_bonus_kopeks=prize.promo_balance_bonus_kopeks,
            subscription_days=prize.promo_subscription_days,
            max_uses=1,
            valid_until=datetime.now(UTC) + timedelta(days=config.promo_validity_days),
            is_active=True,
            created_by=user.id,
        )

        db.add(promocode)
        await db.flush()

        return promocode

    async def spin(
        self, db: AsyncSession, user: User, payment_type: str, *, subscription_id: int | None = None
    ) -> SpinResult:
        """
        Выполнить спин колеса.

        Шаги:
        1. Проверить доступность
        2. Обработать оплату
        3. Рассчитать вероятности и выбрать приз
        4. Применить приз
        5. Создать запись WheelSpin
        6. Вернуть результат
        """
        try:
            # 1. Проверяем доступность
            availability = await self.check_availability(db, user)
            if not availability.can_spin:
                return SpinResult(
                    success=False,
                    error=availability.reason,
                    message=self._get_error_message(availability.reason),
                )

            config = await get_or_create_wheel_config(db)
            prizes = await get_wheel_prizes(db, config.id, active_only=True)

            if not prizes:
                return SpinResult(
                    success=False,
                    error='no_prizes',
                    message='Призы не настроены',
                )

            # Serialize all of this user's spins on the user row and re-check the
            # daily limit *under the lock*. The up-front check in check_availability
            # races with concurrent POST /wheel/spin requests (TOCTTOU): each request
            # gets its own session, all read the same spins_today before any commits,
            # and all pass. Holding this lock through create_wheel_spin — the prize
            # grants below use commit=False so the whole spin is ONE transaction —
            # makes a second concurrent spin block here until the first commits, then
            # observe the updated count. It also serializes the days-payment
            # read-modify-write on the subscription (no lost update).
            from app.database.crud.user import lock_user_for_update

            user = await lock_user_for_update(db, user)
            if config.daily_spin_limit > 0:
                spins_today = await get_user_spins_today(db, user.id)
                if spins_today >= config.daily_spin_limit:
                    return SpinResult(
                        success=False,
                        error='daily_limit_reached',
                        message=self._get_error_message('daily_limit_reached'),
                    )

            # Resolve target subscription for days payment and prize application
            target_subscription = None
            if subscription_id:
                from app.database.crud.subscription import get_subscription_by_id_for_user

                target_subscription = await get_subscription_by_id_for_user(db, subscription_id, user.id)
                if not target_subscription or not target_subscription.is_active:
                    return SpinResult(
                        success=False,
                        error='invalid_subscription',
                        message='Подписка не найдена или неактивна',
                    )
            elif not settings.is_multi_tariff_enabled():
                # Single-tariff: auto-resolve
                target_subscription = await get_subscription_by_user_id(db, user.id)

            # Multi-tariff guard: subscription_id is required for days payment
            if (
                settings.is_multi_tariff_enabled()
                and not target_subscription
                and payment_type == WheelSpinPaymentType.SUBSCRIPTION_DAYS.value
            ):
                return SpinResult(
                    success=False,
                    error='subscription_required',
                    message='Выберите подписку для оплаты днями',
                )

            # 2. Обрабатываем оплату
            if payment_type == WheelSpinPaymentType.TELEGRAM_STARS.value:
                if not availability.can_pay_stars:
                    return SpinResult(
                        success=False,
                        error='cannot_pay_stars',
                        message='Оплата Stars недоступна',
                    )
                payment_amount = config.spin_cost_stars
                payment_value_kopeks = await self._process_stars_payment(db, user, config)
            elif payment_type == WheelSpinPaymentType.SUBSCRIPTION_DAYS.value:
                if not availability.can_pay_days:
                    return SpinResult(
                        success=False,
                        error='cannot_pay_days',
                        message='Оплата днями подписки недоступна',
                    )
                payment_amount = config.spin_cost_days
                payment_value_kopeks = await self._process_days_payment(db, user, config, target_subscription)
            else:
                return SpinResult(
                    success=False,
                    error='invalid_payment_type',
                    message='Неверный способ оплаты',
                )

            # Prize helpers reload locked user/subscription rows with populate_existing.
            # Persist the debit inside this transaction before those reads can overwrite it.
            await db.flush()

            # 3. Рассчитываем вероятности и выбираем приз
            prizes_with_probs = self.calculate_prize_probabilities(config, prizes, payment_value_kopeks)
            selected_prize = self._select_prize(prizes_with_probs)

            # 4. Рассчитываем угол для анимации
            rotation = self._calculate_rotation(prizes, selected_prize)

            # 5. Применяем приз
            generated_promocode = await self._apply_prize(db, user, selected_prize, config, target_subscription)
            promocode_id = None
            if generated_promocode:
                # Получаем ID промокода
                from sqlalchemy import text

                result = await db.execute(
                    text('SELECT id FROM promocodes WHERE code = :code'), {'code': generated_promocode}
                )
                row = result.fetchone()
                if row:
                    promocode_id = row[0]

            # 6. Создаем запись спина
            await create_wheel_spin(
                db=db,
                user_id=user.id,
                prize_id=selected_prize.id,
                payment_type=payment_type,
                payment_amount=payment_amount,
                payment_value_kopeks=payment_value_kopeks,
                prize_type=selected_prize.prize_type,
                prize_value=selected_prize.prize_value,
                prize_display_name=selected_prize.display_name,
                prize_value_kopeks=selected_prize.prize_value_kopeks,
                generated_promocode_id=promocode_id,
                is_applied=True,
            )

            await db.commit()
            await self._sync_subscription_after_spin(
                user,
                selected_prize,
                target_subscription,
                payment_type=payment_type,
            )

            # 7. Формируем результат
            message = self._get_prize_message(selected_prize, generated_promocode)

            return SpinResult(
                success=True,
                prize_id=selected_prize.id,
                prize_type=selected_prize.prize_type,
                prize_value=selected_prize.prize_value,
                prize_display_name=selected_prize.display_name,
                emoji=selected_prize.emoji,
                color=selected_prize.color,
                rotation_degrees=rotation,
                message=message,
                promocode=generated_promocode,
            )

        except ValueError as e:
            await db.rollback()
            return SpinResult(
                success=False,
                error='payment_error',
                message=str(e),
            )
        except Exception as e:
            await db.rollback()
            logger.exception('Ошибка спина колеса для user_id', user_id=user.id, error=e)
            return SpinResult(
                success=False,
                error='internal_error',
                message='Произошла ошибка, попробуйте позже',
            )

    def _get_error_message(self, reason: str | None) -> str:
        """Получить человекочитаемое сообщение об ошибке."""
        messages = {
            'wheel_disabled': 'Колесо удачи временно недоступно',
            'daily_limit_reached': 'Вы достигли лимита спинов на сегодня',
            'no_payment_method_available': 'Нет доступных способов оплаты',
            'no_prizes_configured': 'Призы еще не настроены',
            'insufficient_balance': 'Недостаточно средств на балансе. Пополните баланс для оплаты спина.',
        }
        return messages.get(reason, 'Произошла ошибка')

    def _get_prize_message(self, prize: WheelPrize, promocode: str | None) -> str:
        """Сформировать сообщение о выигрыше."""
        prize_type = prize.prize_type

        if prize_type == WheelPrizeType.NOTHING.value:
            return 'К сожалению, в этот раз не повезло. Попробуйте еще!'

        if prize_type == WheelPrizeType.BALANCE_BONUS.value:
            return f'Поздравляем! Вы выиграли {prize.prize_value / 100:.0f}₽ на баланс!'

        if prize_type == WheelPrizeType.SUBSCRIPTION_DAYS.value:
            days_word = self._pluralize_days(prize.prize_value)
            return f'Поздравляем! Вы выиграли {prize.prize_value} {days_word} подписки!'

        if prize_type == WheelPrizeType.TRAFFIC_GB.value:
            return f'Поздравляем! Вы выиграли {prize.prize_value}GB трафика!'

        if prize_type == WheelPrizeType.PROMOCODE.value:
            return f'Поздравляем! Ваш промокод: {promocode}'

        return 'Поздравляем с выигрышем!'

    def _pluralize_days(self, n: int) -> str:
        """Склонение слова 'день'."""
        if 11 <= n % 100 <= 19:
            return 'дней'
        if n % 10 == 1:
            return 'день'
        if 2 <= n % 10 <= 4:
            return 'дня'
        return 'дней'

    async def get_statistics(
        self, db: AsyncSession, date_from: datetime | None = None, date_to: datetime | None = None
    ) -> dict[str, Any]:
        """Получить статистику колеса."""
        return await get_wheel_statistics(db, date_from, date_to)


# Глобальный экземпляр сервиса
wheel_service = FortuneWheelService()
