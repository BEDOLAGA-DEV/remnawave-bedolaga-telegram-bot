"""
Сервис для работы с подарочными подписками.

Функциональность:
- Создание gift-подписок с уникальными кодами
- Активация gift-подписок через промокоды
- Расчет цены подарочных подписок
"""
import logging
import secrets
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PromoCode, PromoCodeType, User, TransactionType
from app.database.crud.promocode import create_promocode, get_promocode_by_code
from app.database.crud.transaction import create_transaction
from app.database.crud.user import update_user_balance
from app.database.crud.subscription import create_paid_subscription
from app.utils.pricing_utils import compute_simple_subscription_price
from app.services.subscription_service import SubscriptionService

logger = logging.getLogger(__name__)


class InsufficientBalanceError(Exception):
    """Исключение при недостаточном балансе для покупки gift-подписки."""
    pass


class GiftCodeAlreadyUsedError(Exception):
    """Исключение при попытке активировать уже использованный gift-код."""
    pass


class GiftCodeNotFoundError(Exception):
    """Исключение при попытке активировать несуществующий gift-код."""
    pass


class GiftSubscriptionService:
    """Сервис для управления подарочными подписками."""

    def __init__(self, subscription_service: Optional[SubscriptionService] = None):
        """
        Инициализация сервиса gift-подписок.

        Args:
            subscription_service: Сервис для работы с подписками (опционально).
        """
        self.subscription_service = subscription_service or SubscriptionService()

    async def generate_gift_code(self, db: AsyncSession) -> str:
        """
        Генерирует уникальный код для gift-подписки.

        Формат: GIFT_XXXXXXXXXXXX (всего 17 символов)

        Args:
            db: Сессия базы данных

        Returns:
            Уникальный код gift-подписки

        Raises:
            RuntimeError: Если не удалось сгенерировать уникальный код за 10 попыток
        """
        max_attempts = 10

        for attempt in range(max_attempts):
            # Генерируем криптостойкий случайный код (12 символов в верхнем регистре)
            random_part = secrets.token_urlsafe(9).upper().replace('-', '').replace('_', '')[:12]
            code = f"GIFT_{random_part}"

            # Проверяем на коллизии
            existing = await get_promocode_by_code(db, code)
            if not existing:
                logger.info(f"✅ Сгенерирован уникальный gift-код: {code}")
                return code

            logger.warning(f"⚠️ Коллизия gift-кода {code}, попытка {attempt + 1}/{max_attempts}")

        raise RuntimeError("Не удалось сгенерировать уникальный gift-код за 10 попыток")

    async def calculate_gift_price(
        self,
        db: AsyncSession,
        period_days: int,
        traffic_gb: int,
        devices: int,
        squads: List[str],
        user: Optional[User] = None
    ) -> int:
        """
        Рассчитывает цену gift-подписки.

        Args:
            db: Сессия базы данных
            period_days: Период подписки в днях
            traffic_gb: Лимит трафика в ГБ (0 = безлимит)
            devices: Количество устройств
            squads: Список UUID серверов
            user: Пользователь-даритель (для применения его скидок, если есть)

        Returns:
            Цена в копейках
        """
        # Используем существующую функцию расчета цены подписки
        result = await compute_simple_subscription_price(
            db=db,
            user=user,
            period_days=period_days,
            traffic_gb=traffic_gb,
            device_limit=devices,
            squad_uuids=squads,
            promo_group=None,  # Для gift не применяем промогруппы
            apply_discounts=False,  # Для gift не применяем скидки
        )

        price_kopeks = result["final_price_kopeks"]

        logger.info(
            f"💰 Цена gift-подписки: {price_kopeks/100}₽ "
            f"(период={period_days}д, трафик={traffic_gb}ГБ, устройства={devices})"
        )

        return price_kopeks

    async def create_gift_subscription(
        self,
        db: AsyncSession,
        user: User,
        period_days: int,
        traffic_gb: int,
        devices: int,
        squads: List[str],
    ) -> Dict[str, Any]:
        """
        Создаёт подарочную подписку.

        Args:
            db: Сессия базы данных
            user: Пользователь-даритель
            period_days: Период подписки в днях
            traffic_gb: Лимит трафика в ГБ (0 = безлимит)
            devices: Количество устройств
            squads: Список UUID серверов

        Returns:
            Словарь с результатом:
            {
                "success": True,
                "code": "GIFT_XXXX",
                "deep_link": "https://t.me/bot?start=GIFT_XXXX",
                "price_kopeks": 50000
            }

        Raises:
            InsufficientBalanceError: Недостаточно средств на балансе
            ValueError: Некорректные параметры подписки
        """
        logger.info(
            f"🎁 Создание gift-подписки для user_id={user.id}: "
            f"период={period_days}д, трафик={traffic_gb}ГБ, устройства={devices}"
        )

        # Валидация параметров
        if period_days <= 0:
            raise ValueError("Период подписки должен быть положительным")
        if devices <= 0:
            raise ValueError("Количество устройств должно быть положительным")
        if not squads:
            raise ValueError("Необходимо выбрать хотя бы один сервер")

        # Рассчитываем цену
        price_kopeks = await self.calculate_gift_price(
            db=db,
            period_days=period_days,
            traffic_gb=traffic_gb,
            devices=devices,
            squads=squads,
            user=user
        )

        # Проверяем баланс
        if user.balance_kopeks < price_kopeks:
            logger.warning(
                f"❌ Недостаточно средств для покупки gift: "
                f"требуется {price_kopeks/100}₽, доступно {user.balance_kopeks/100}₽"
            )
            raise InsufficientBalanceError(
                f"Недостаточно средств. Требуется: {price_kopeks/100}₽, "
                f"доступно: {user.balance_kopeks/100}₽"
            )

        # Генерируем уникальный код
        code = await self.generate_gift_code(db)

        # Формируем JSON с параметрами gift-подписки
        gift_params = {
            "traffic_gb": traffic_gb,
            "devices": devices,
            "squads": squads,
            "price_kopeks": price_kopeks
        }
        description_json = json.dumps(gift_params, ensure_ascii=False)

        # Создаём промокод типа GIFT
        promocode = await create_promocode(
            db=db,
            code=code,
            type=PromoCodeType.GIFT,
            subscription_days=period_days,
            max_uses=1,  # Gift-подписка всегда на одно использование
            created_by=user.id,
            description=description_json
        )

        # Списываем баланс
        await update_user_balance(db, user.id, -price_kopeks)
        logger.info(f"💸 Списано {price_kopeks/100}₽ с баланса user_id={user.id}")

        # Создаём транзакцию покупки gift-подписки
        traffic_text = f"{traffic_gb} ГБ" if traffic_gb > 0 else "Безлимит"
        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=-price_kopeks,
            description=f"Покупка gift-подписки ({period_days} дней, {traffic_text}, {devices} устройства)",
            external_id=f"gift_{code}"
        )

        # Формируем deep link
        bot_username = settings.BOT_USERNAME.replace("@", "")
        deep_link = f"https://t.me/{bot_username}?start={code}"

        logger.info(f"✅ Gift-подписка создана: {code}, цена {price_kopeks/100}₽")

        return {
            "success": True,
            "code": code,
            "deep_link": deep_link,
            "price_kopeks": price_kopeks,
            "period_days": period_days,
            "traffic_gb": traffic_gb,
            "devices": devices,
            "squads": squads
        }

    async def activate_gift_subscription(
        self,
        db: AsyncSession,
        user: User,
        code: str
    ) -> Dict[str, Any]:
        """
        Активирует gift-подписку для пользователя.

        Args:
            db: Сессия базы данных
            user: Пользователь-получатель подарка
            code: Код активации gift-подписки

        Returns:
            Словарь с результатом активации:
            {
                "success": True,
                "subscription_id": 123,
                "period_days": 30,
                "traffic_gb": 100,
                "devices": 3,
                "squads": ["uuid1", "uuid2"]
            }

        Raises:
            GiftCodeNotFoundError: Код не найден или недействителен
            GiftCodeAlreadyUsedError: Код уже был использован
        """
        logger.info(f"🎁 Активация gift-подписки {code} для user_id={user.id}")

        # Получаем промокод
        promocode = await get_promocode_by_code(db, code)

        if not promocode:
            logger.warning(f"❌ Gift-код {code} не найден")
            raise GiftCodeNotFoundError("Неверный код или подарок не найден")

        # Проверяем, что это gift-промокод
        if promocode.type != PromoCodeType.GIFT.value:
            logger.warning(f"❌ Код {code} не является gift-подпиской")
            raise GiftCodeNotFoundError("Этот код не является подарочной подпиской")

        # Проверяем валидность (не истёк, не использован)
        if not promocode.is_valid:
            if promocode.current_uses >= promocode.max_uses:
                logger.warning(f"❌ Gift-код {code} уже использован")
                raise GiftCodeAlreadyUsedError("Этот подарок уже был активирован")
            else:
                logger.warning(f"❌ Gift-код {code} истёк")
                raise GiftCodeNotFoundError("Срок действия подарка истёк")

        # Парсим параметры из description
        try:
            gift_params = json.loads(promocode.description or "{}")
            traffic_gb = gift_params.get("traffic_gb", 0)
            devices = gift_params.get("devices", 1)
            squads = gift_params.get("squads", [])
        except json.JSONDecodeError as e:
            logger.error(f"❌ Ошибка парсинга параметров gift-кода {code}: {e}")
            raise GiftCodeNotFoundError("Ошибка чтения параметров подарка")

        # Создаём или продлеваем подписку
        if user.subscription and user.subscription.is_active:
            # Продление существующей подписки
            from app.database.crud.subscription import extend_subscription

            old_end_date = user.subscription.end_date
            await extend_subscription(
                db=db,
                subscription_id=user.subscription.id,
                days=promocode.subscription_days
            )

            # Обновляем параметры если подарок щедрее
            if traffic_gb > user.subscription.traffic_limit_gb:
                user.subscription.traffic_limit_gb = traffic_gb
            if devices > user.subscription.device_limit:
                user.subscription.device_limit = devices

            # Объединяем squads
            existing_squads = set(user.subscription.connected_squads or [])
            new_squads = existing_squads.union(set(squads))
            user.subscription.connected_squads = list(new_squads)

            await db.commit()
            await db.refresh(user.subscription)

            # Обновляем пользователя в RemnaWave
            await self.subscription_service.update_remnawave_user(db, user.subscription)

            logger.info(
                f"✅ Подписка продлена на {promocode.subscription_days} дней "
                f"(до {user.subscription.end_date})"
            )

            result_message = f"продлена на {promocode.subscription_days} дней"
            subscription_id = user.subscription.id
        else:
            # Создание новой подписки
            subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=promocode.subscription_days,
                traffic_limit_gb=traffic_gb,
                device_limit=devices,
                connected_squads=squads
            )

            # Создаём пользователя в RemnaWave
            await self.subscription_service.create_remnawave_user(db, subscription)

            logger.info(
                f"✅ Создана gift-подписка на {promocode.subscription_days} дней "
                f"для user_id={user.id}"
            )

            result_message = f"активирована на {promocode.subscription_days} дней"
            subscription_id = subscription.id

        # Помечаем пользователя как имевшего платную подписку
        user.has_had_paid_subscription = True
        await db.commit()

        # Помечаем промокод как использованный
        from app.database.crud.promocode import use_promocode
        await use_promocode(db, promocode.id, user.id)

        # Создаём транзакцию активации
        traffic_text = f"{traffic_gb} ГБ" if traffic_gb > 0 else "Безлимит"
        gifter_text = f" от пользователя ID {promocode.created_by}" if promocode.created_by else ""
        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=0,
            description=f"Получена gift-подписка{gifter_text} (код: {code})",
            external_id=f"gift_activation_{code}"
        )

        logger.info(f"🎉 Gift-подписка {code} успешно активирована для user_id={user.id}")

        return {
            "success": True,
            "subscription_id": subscription_id,
            "period_days": promocode.subscription_days,
            "traffic_gb": traffic_gb,
            "devices": devices,
            "squads": squads,
            "message": result_message
        }


# Singleton instance
gift_subscription_service = GiftSubscriptionService()
