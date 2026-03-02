"""
Сервис колеса удачи (Fortune Wheel) с RTP алгоритмом.
"""

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

        if config.spin_cost_days_enabled:
            subscription = await get_subscription_by_user_id(db, user.id)
            if subscription and subscription.is_active:
                user_subscription_days = subscription.days_left
                # Нужно оставить минимум min_subscription_days_for_day_payment дней после оплаты
                if user_subscription_days >= config.min_subscription_days_for_day_payment + config.spin_cost_days:
                    can_pay_days = True

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
        )

    def calculate_prize_probabilities(
        self, config: WheelConfig, prizes: list[WheelPrize], spin_cost_kopeks: int
    ) -> list[tuple[WheelPrize, float]]:
        """
        Рассчитать вероятности выпадения призов на основе RTP.

        Алгоритм:
        1. Целевая средняя выплата = spin_cost * (RTP / 100)
        2. Для призов с manual_probability - используем его напрямую
        3. Для остальных - рассчитываем веса обратно пропорционально стоимости приза
        4. "Nothing" сектор балансирует систему
        """
        if not prizes:
            return []

        target_payout = spin_cost_kopeks * (config.rtp_percent / 100)

        # Разделяем призы с ручной вероятностью и автоматической
        manual_prizes = []
        auto_prizes = []
        manual_prob_sum = 0.0

        for prize in prizes:
            if prize.manual_probability is not None and prize.manual_probability > 0:
                manual_prizes.append((prize, prize.manual_probability))
                manual_prob_sum += prize.manual_probability
            else:
                auto_prizes.append(prize)

        # Оставшаяся вероятность для авто-призов
        remaining_prob = max(0, 1.0 - manual_prob_sum)

        if not auto_prizes or remaining_prob <= 0:
            # Только ручные призы, нормализуем их
            if manual_prizes:
                total = sum(p[1] for p in manual_prizes)
                return [(p[0], p[1] / total) for p in manual_prizes]
            return []

        # Рассчитываем веса для авто-призов
        # Вес обратно пропорционален стоимости приза (более дорогие выпадают реже)
        weights = []
        for prize in auto_prizes:
            if prize.prize_value_kopeks > 0:
                # Чем дороже приз, тем меньше вес
                weight = target_payout / prize.prize_value_kopeks
            else:
                # "Nothing" или нулевой приз - даем базовый вес
                weight = 1.0
            weights.append((prize, max(weight, 0.01)))  # Минимальный вес 1%

        # Нормализуем веса авто-призов до remaining_prob
        total_weight = sum(w[1] for w in weights)
        auto_probabilities = [(prize, (weight / total_weight) * remaining_prob) for prize, weight in weights]

        # Объединяем
        result = manual_prizes + auto_probabilities

        # Финальная нормализация (на случай погрешностей)
        total = sum(p[1] for p in result)
        if total > 0:
            result = [(p[0], p[1] / total) for p in result]

        return result

    def _select_prize(self, prizes_with_probabilities: list[tuple[WheelPrize, float]]) -> WheelPrize:
        """Выбрать приз на основе вероятностей."""
        if not prizes_with_probabilities:
            raise ValueError('No prizes to select from')

        rand = random.random()
        cumulative = 0.0

        for prize, probability in prizes_with_probabilities:
            cumulative += probability
            if rand <= cumulative:
                return prize

        # Fallback на последний приз
        return prizes_with_probabilities[-1][0]

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

    async def _process_days_payment(self, db: AsyncSession, user: User, config: WheelConfig) -> int:
        """
        Обработать оплату днями подписки.
        Возвращает эквивалент в копейках.
        """
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

        # Синхронизируем с RemnaWave
        try:
            subscription_service = SubscriptionService()
            await subscription_service.update_remnawave_user(db, subscription)
            logger.info('✅ Списание дней синхронизировано с RemnaWave для user_id', user_id=user.id)
        except Exception as e:
            logger.error('⚠️ Ошибка синхронизации списания дней с RemnaWave', error=e)

        return kopeks

    async def _apply_prize(self, db: AsyncSession, user: User, prize: WheelPrize, config: WheelConfig) -> str | None:
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
            )
            logger.info(
                '💰 Начислено ₽ на баланс user_id', prize_value=round(prize.prize_value / 100, 2), user_id=user.id
            )
            return None

        if prize_type == WheelPrizeType.SUBSCRIPTION_DAYS.value:
            # Дни подписки
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
                        )
                        logger.info('💰 Дни конвертированы в баланс для user_id', user_id=user.id)
                else:
                    # Обычная подписка - добавляем дни и синхронизируем с RemnaWave
                    subscription.end_date += timedelta(days=prize.prize_value)
                    subscription.updated_at = datetime.now(UTC)
                    logger.info('📅 Начислено дней подписки user_id', prize_value=prize.prize_value, user_id=user.id)

                    # Синхронизируем с RemnaWave
                    try:
                        subscription_service = SubscriptionService()
                        await subscription_service.update_remnawave_user(db, subscription)
                        logger.info('✅ Синхронизировано с RemnaWave для user_id', user_id=user.id)
                    except Exception as e:
                        logger.error('⚠️ Ошибка синхронизации с RemnaWave', error=e)
            else:
                # Если нет подписки - начисляем на баланс эквивалент
                await add_user_balance(
                    db,
                    user,
                    prize.prize_value_kopeks,
                    description=f'Выигрыш в колесе удачи: {prize.prize_value} дней (на баланс)',
                    create_transaction=True,
                )
                logger.info('💰 Дни конвертированы в баланс для user_id', user_id=user.id)
            return None

        if prize_type == WheelPrizeType.TRAFFIC_GB.value:
            # Бонусный трафик
            subscription = await get_subscription_by_user_id(db, user.id)
            if subscription and subscription.traffic_limit_gb > 0:
                subscription.traffic_limit_gb += prize.prize_value
                subscription.updated_at = datetime.now(UTC)
                logger.info('📊 Начислено трафика user_id', prize_value=prize.prize_value, user_id=user.id)

                # Синхронизируем с RemnaWave
                try:
                    subscription_service = SubscriptionService()
                    await subscription_service.update_remnawave_user(db, subscription)
                    logger.info('✅ Трафик синхронизирован с RemnaWave для user_id', user_id=user.id)
                except Exception as e:
                    logger.error('⚠️ Ошибка синхронизации трафика с RemnaWave', error=e)
            else:
                # Если безлимит или нет подписки - на баланс
                await add_user_balance(
                    db,
                    user,
                    prize.prize_value_kopeks,
                    description=f'Выигрыш в колесе удачи: {prize.prize_value}GB (на баланс)',
                    create_transaction=True,
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

    async def spin(self, db: AsyncSession, user: User, payment_type: str) -> SpinResult:
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
                payment_value_kopeks = await self._process_days_payment(db, user, config)
            else:
                return SpinResult(
                    success=False,
                    error='invalid_payment_type',
                    message='Неверный способ оплаты',
                )

            # 3. Рассчитываем вероятности и выбираем приз
            prizes_with_probs = self.calculate_prize_probabilities(config, prizes, payment_value_kopeks)
            selected_prize = self._select_prize(prizes_with_probs)

            # 4. Рассчитываем угол для анимации
            rotation = self._calculate_rotation(prizes, selected_prize)

            # 5. Применяем приз
            generated_promocode = await self._apply_prize(db, user, selected_prize, config)
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
