"""
Репозиторий подписок.

Не делает commit - это ответственность UnitOfWork.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    Subscription,
    SubscriptionStatus,
)
from app.database.repositories.base import BaseRepository


logger = logging.getLogger(__name__)


class SubscriptionRepository(BaseRepository[Subscription]):
    """Репозиторий для работы с подписками."""

    model_class = Subscription

    async def get_by_user_id(self, user_id: int) -> Subscription | None:
        """Получить подписку пользователя."""
        result = await self._session.execute(
            select(Subscription)
            .options(
                selectinload(Subscription.user),
                selectinload(Subscription.tariff),
            )
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_trial(
        self,
        user_id: int,
        *,
        duration_days: int | None = None,
        traffic_limit_gb: int | None = None,
        device_limit: int | None = None,
        connected_squads: list[str] | None = None,
        tariff_id: int | None = None,
    ) -> Subscription:
        """
        Создать триальную подписку.

        НЕ делает commit.
        """
        duration_days = duration_days or settings.TRIAL_DURATION_DAYS
        traffic_limit_gb = traffic_limit_gb or settings.TRIAL_TRAFFIC_LIMIT_GB
        if device_limit is None:
            device_limit = settings.TRIAL_DEVICE_LIMIT

        end_date = datetime.utcnow() + timedelta(days=duration_days)

        subscription = Subscription(
            user_id=user_id,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=True,
            start_date=datetime.utcnow(),
            end_date=end_date,
            traffic_limit_gb=traffic_limit_gb,
            device_limit=device_limit,
            connected_squads=connected_squads or [],
            autopay_enabled=settings.is_autopay_enabled_by_default(),
            autopay_days_before=settings.DEFAULT_AUTOPAY_DAYS_BEFORE,
            tariff_id=tariff_id,
        )

        self.add(subscription)
        await self._session.flush()
        await self._session.refresh(subscription)

        logger.info(
            'Создана триальная подписка %s для пользователя %s',
            subscription.id,
            user_id,
        )

        return subscription

    async def create_paid(
        self,
        user_id: int,
        *,
        duration_days: int,
        traffic_limit_gb: int | None = None,
        device_limit: int | None = None,
        connected_squads: list[str] | None = None,
        tariff_id: int | None = None,
    ) -> Subscription:
        """
        Создать платную подписку.

        НЕ делает commit.
        """
        end_date = datetime.utcnow() + timedelta(days=duration_days)

        subscription = Subscription(
            user_id=user_id,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=False,
            start_date=datetime.utcnow(),
            end_date=end_date,
            traffic_limit_gb=traffic_limit_gb,
            device_limit=device_limit or settings.DEFAULT_DEVICE_LIMIT,
            connected_squads=connected_squads or [],
            autopay_enabled=settings.is_autopay_enabled_by_default(),
            autopay_days_before=settings.DEFAULT_AUTOPAY_DAYS_BEFORE,
            tariff_id=tariff_id,
        )

        self.add(subscription)
        await self._session.flush()
        await self._session.refresh(subscription)

        logger.info(
            'Создана платная подписка %s для пользователя %s на %s дней',
            subscription.id,
            user_id,
            duration_days,
        )

        return subscription

    async def extend(
        self,
        subscription: Subscription,
        days: int,
        *,
        add_traffic_gb: int | None = None,
    ) -> Subscription:
        """
        Продлить подписку.

        НЕ делает commit.
        """
        # Если подписка истекла - от текущего времени, иначе - от end_date
        base_date = subscription.end_date or datetime.utcnow()
        if base_date < datetime.utcnow():
            base_date = datetime.utcnow()

        subscription.end_date = base_date + timedelta(days=days)
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.is_trial = False  # Продление всегда превращает в платную

        if add_traffic_gb:
            current_traffic = subscription.traffic_limit_gb or 0
            subscription.traffic_limit_gb = current_traffic + add_traffic_gb

        await self._session.flush()

        logger.info(
            'Подписка %s продлена на %s дней до %s',
            subscription.id,
            days,
            subscription.end_date,
        )

        return subscription

    async def set_status(
        self,
        subscription: Subscription,
        status: SubscriptionStatus,
    ) -> Subscription:
        """Установить статус подписки."""
        subscription.status = status.value
        await self._session.flush()
        return subscription

    async def update_traffic(
        self,
        subscription: Subscription,
        traffic_used_bytes: int,
    ) -> Subscription:
        """Обновить использованный трафик."""
        subscription.traffic_used_gb = traffic_used_bytes / (1024 ** 3)
        await self._session.flush()
        return subscription

    async def count_active(self) -> int:
        """Подсчитать активные подписки."""
        result = await self._session.execute(
            select(func.count())
            .select_from(Subscription)
            .where(
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.end_date > datetime.utcnow(),
                )
            )
        )
        return result.scalar_one()

    async def get_expiring_soon(
        self,
        days: int = 3,
        limit: int = 100,
    ) -> list[Subscription]:
        """Получить подписки, истекающие в ближайшие N дней."""
        threshold = datetime.utcnow() + timedelta(days=days)

        result = await self._session.execute(
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.end_date <= threshold,
                    Subscription.end_date > datetime.utcnow(),
                )
            )
            .order_by(Subscription.end_date)
            .limit(limit)
        )
        return list(result.scalars().all())
