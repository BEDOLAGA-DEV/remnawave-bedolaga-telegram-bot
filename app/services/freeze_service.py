from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from app.database.models import Subscription, SubscriptionStatus
from app.services.freeze_settings_service import FreezeSettingsService
from app.services.remnawave_retry_queue import remnawave_retry_queue
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


class FreezeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def remaining_year_quota(subscription, max_year_days: int, now: datetime) -> int:
    used = subscription.freeze_days_used_year if subscription.freeze_year == now.year else 0
    return max(0, max_year_days - used)


class FreezeService:
    def __init__(self) -> None:
        self._subscription_service = SubscriptionService()

    async def freeze_subscription(self, db, subscription, user) -> None:
        now = datetime.now(UTC)

        # Self-defending: never freeze when the feature is disabled, regardless
        # of how this method was reached (guards at entry points can be bypassed).
        if not FreezeSettingsService.is_enabled():
            raise FreezeError('disabled', 'Заморозка подписки отключена.')

        if subscription.frozen_at is not None:
            raise FreezeError('already_frozen', 'Подписка уже заморожена.')
        if getattr(subscription, 'is_trial', False):
            raise FreezeError('trial', 'Тестовую подписку нельзя заморозить.')
        tariff = getattr(subscription, 'tariff', None)
        if tariff is not None and getattr(tariff, 'is_daily', False):
            raise FreezeError('daily', 'Суточную подписку нельзя заморозить (используйте паузу).')
        if subscription.status != SubscriptionStatus.ACTIVE.value:
            raise FreezeError('not_active', 'Заморозить можно только активную подписку.')

        min_age = FreezeSettingsService.get_min_subscription_age_days()
        created = getattr(subscription, 'created_at', None)
        if created is not None and (now - created) < timedelta(days=min_age):
            raise FreezeError('too_young', f'Подписка должна быть старше {min_age} дн.')

        cooldown = FreezeSettingsService.get_cooldown_days()
        last = getattr(subscription, 'last_freeze_at', None)
        if last is not None and (now - last) < timedelta(days=cooldown):
            raise FreezeError('cooldown', f'Заморозка доступна не чаще раза в {cooldown} дн.')

        max_year = FreezeSettingsService.get_max_days_per_year()
        remaining = remaining_year_quota(subscription, max_year, now)
        min_freeze = FreezeSettingsService.get_min_freeze_days()
        if remaining < min_freeze:
            raise FreezeError('quota_exhausted', f'Осталось {remaining} дн. заморозки в этом году.')

        max_single = min(FreezeSettingsService.get_max_single_freeze_days(), remaining)

        subscription.frozen_at = now
        subscription.frozen_until = now + timedelta(days=max_single)

        uuid = getattr(subscription, 'remnawave_uuid', None)
        if uuid:
            # disable_remnawave_user also disables the paired _wl (БС-трафик)
            # account, so the freeze covers both the main and БС access.
            ok = await self._subscription_service.disable_remnawave_user(uuid)
            if not ok:
                subscription.frozen_at = None
                subscription.frozen_until = None
                await db.rollback()
                raise FreezeError('panel_error', 'Не удалось отключить доступ. Попробуйте позже.')

        await db.commit()
        logger.info('freeze.frozen', subscription_id=subscription.id, until=subscription.frozen_until)

    async def resume_subscription(self, db, subscription, user, *, reason: str = 'manual') -> None:
        if subscription.frozen_at is None:
            return  # fast-path no-op (re-checked under lock below)

        # Lock the subscription row to serialize concurrent resume attempts
        # (manual user action vs scheduler auto-resume). Without the lock both
        # could read frozen_at, each add `actual` to end_date, and double-credit
        # time. Under FOR UPDATE the second caller waits, then sees frozen_at=None.
        locked = await db.execute(
            select(Subscription).where(Subscription.id == subscription.id).with_for_update()
        )
        subscription = locked.scalar_one_or_none()
        if subscription is None or subscription.frozen_at is None:
            return  # already resumed by a concurrent caller — idempotent

        now = datetime.now(UTC)
        until = subscription.frozen_until or now
        now_capped = min(now, until)
        actual = now_capped - subscription.frozen_at
        if actual.total_seconds() < 0:
            actual = timedelta(0)

        if subscription.end_date is not None:
            subscription.end_date = subscription.end_date + actual

        if subscription.freeze_year != now.year:
            subscription.freeze_days_used_year = 0
            subscription.freeze_year = now.year
        subscription.freeze_days_used_year += math.ceil(actual.total_seconds() / 86400)

        subscription.last_freeze_at = now
        subscription.frozen_at = None
        subscription.frozen_until = None

        await db.commit()

        uuid = getattr(subscription, 'remnawave_uuid', None)
        if uuid:
            # enable_remnawave_user also re-enables the paired _wl (БС-трафик)
            # account, so resume restores both the main and БС access.
            ok = await self._subscription_service.enable_remnawave_user(uuid)
            if not ok:
                remnawave_retry_queue.enqueue(
                    subscription_id=subscription.id, user_id=subscription.user_id, action='update',
                )
                logger.warning('freeze.resume_panel_failed_enqueued', subscription_id=subscription.id)

        logger.info('freeze.resumed', subscription_id=subscription.id, reason=reason)


freeze_service = FreezeService()
