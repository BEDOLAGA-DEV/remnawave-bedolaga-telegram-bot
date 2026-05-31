from __future__ import annotations

import structlog
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.crud import referral as ref_crud
from app.database.crud import referral_milestone as milestone_crud
from app.database.crud.user import add_user_balance, get_user_by_id
from app.database.crud.user_promo_group import add_user_to_promo_group
from app.database.models import TransactionType, UserReferralMilestoneClaim


logger = structlog.get_logger(__name__)


class ReferralMilestoneService:
    async def reward_milestones(self, db, referrer_id: int, bot=None) -> list[int]:
        """Grant any unclaimed milestones the referrer has reached. Best-effort, idempotent."""
        granted: list[int] = []
        try:
            if not settings.REFERRAL_MILESTONES_ENABLED:
                return granted

            count = await ref_crud.count_paid_referrals(db, referrer_id)
            if count <= 0:
                return granted

            milestones = await milestone_crud.list_active(db)
            reached = [m for m in milestones if m.threshold <= count]
            if not reached:
                return granted

            referrer = await get_user_by_id(db, referrer_id)
            if referrer is None:
                return granted

            claimed = await milestone_crud.get_claimed_milestone_ids(db, referrer_id)

            for m in reached:
                if m.id in claimed:
                    continue
                try:
                    db.add(UserReferralMilestoneClaim(user_id=referrer_id, milestone_id=m.id))
                    await db.flush()
                except IntegrityError:
                    await db.rollback()
                    continue

                if m.reward_type == 'balance':
                    ok = await add_user_balance(
                        db, referrer, m.reward_value,
                        description=f'🎯 Реферальный милстоун: {m.threshold} оплативших',
                        transaction_type=TransactionType.REFERRAL_REWARD, commit=False,
                    )
                    if not ok:
                        await db.rollback()
                        continue
                elif m.reward_type == 'promo_group':
                    await add_user_to_promo_group(db, referrer_id, m.reward_value)
                else:
                    await db.rollback()
                    continue

                await db.commit()
                granted.append(m.id)
                await self._notify(bot, referrer, m)

            return granted
        except Exception as exc:
            logger.error('referral_milestone.reward_failed', referrer_id=referrer_id, err=str(exc))
            try:
                await db.rollback()
            except Exception:
                pass
            return granted

    async def _notify(self, bot, referrer, milestone) -> None:
        if bot is None or not getattr(referrer, 'telegram_id', None):
            return
        title = (milestone.title or {}).get(getattr(referrer, 'language', 'ru')) \
            or (milestone.title or {}).get('ru') or f'{milestone.threshold} рефералов'
        try:
            await bot.send_message(
                referrer.telegram_id,
                f'🎉 <b>Достигнут реферальный милстоун!</b>\n\n{title}',
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.warning('referral_milestone.notify_failed', referrer_id=referrer.id, err=str(exc))


referral_milestone_service = ReferralMilestoneService()
