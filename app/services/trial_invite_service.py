from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Subscription, SubscriptionStatus, User
from app.services.remnawave_retry_queue import remnawave_retry_queue
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


class TrialInviteService:
    def __init__(self) -> None:
        self._subscription_service = SubscriptionService()

    async def reward_inviter_on_trial_activation(self, db, invitee, bot=None) -> None:
        """Extend the inviter's own trial when their invitee activates a trial.

        Best-effort: never raises into the invitee's activation flow.
        """
        try:
            if not settings.TRIAL_INVITE_ENABLED:
                return

            referrer_id = getattr(invitee, 'referred_by_id', None)
            if not referrer_id or referrer_id == invitee.id:
                return

            referrer = await get_user_by_id(db, referrer_id)
            if referrer is None:
                return

            now = datetime.now(UTC)

            # Lock the referrer User row FOR UPDATE and re-read the cap counter
            # from the locked row. Without this, two invitees sharing one referrer
            # both read a stale trial_invite_bonus_days_used (loaded before any
            # lock) and each grant the full extend, blowing past the yearly cap
            # and clobbering the counter. The lock serializes the cap arithmetic.
            locked_ref = await db.execute(
                select(User).where(User.id == referrer.id).with_for_update()
            )
            referrer = locked_ref.scalar_one_or_none()
            if referrer is None:
                return

            locked = await db.execute(
                select(Subscription)
                .where(
                    Subscription.user_id == referrer.id,
                    Subscription.is_trial == True,  # noqa: E712
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.end_date > now,
                )
                .order_by(Subscription.end_date.desc())
                .with_for_update()
            )
            inviter_sub = locked.scalar_one_or_none()
            if inviter_sub is None:
                return

            extend = settings.get_trial_invite_extend_days()
            max_ext = settings.get_trial_invite_max_extension_days()
            used = referrer.trial_invite_bonus_days_used or 0
            remaining = max(0, max_ext - used)
            grant = min(extend, remaining)
            if grant <= 0:
                return

            inviter_sub.end_date = inviter_sub.end_date + timedelta(days=grant)
            referrer.trial_invite_bonus_days_used = used + grant
            referrer.trial_invite_rewarded_count = (referrer.trial_invite_rewarded_count or 0) + 1

            await db.commit()

            try:
                await self._subscription_service.create_remnawave_user(db, inviter_sub)
            except Exception as exc:
                remnawave_retry_queue.enqueue(
                    subscription_id=inviter_sub.id, user_id=referrer.id, action='update',
                )
                logger.warning('trial_invite.panel_sync_failed_enqueued', subscription_id=inviter_sub.id, err=str(exc))

            await self._notify(bot, referrer, grant)
            logger.info('trial_invite.granted', referrer_id=referrer.id, invitee_id=invitee.id, days=grant)

        except Exception as exc:
            logger.error('trial_invite.reward_failed', invitee_id=getattr(invitee, 'id', None), err=str(exc))
            try:
                await db.rollback()
            except Exception:
                pass

    async def _notify(self, bot, referrer, days: int) -> None:
        if bot is None or not getattr(referrer, 'telegram_id', None):
            return
        text = f'🎁 Ваш друг активировал триал — вам +{days} дн. к триалу!'
        try:
            await bot.send_message(referrer.telegram_id, text, parse_mode='HTML')
        except Exception as exc:
            logger.warning('trial_invite.notify_failed', referrer_id=referrer.id, err=str(exc))


trial_invite_service = TrialInviteService()
