"""Bio-reward service: state machine, free-sub lifecycle, paid-sub recalc, scheduler.

Lifecycle states (BioRewardStatus):
    PENDING  -> ACTIVE       (bio matched on first check)
    ACTIVE   -> GRACE        (bio missing on a check)
    GRACE    -> ACTIVE       (bio reappeared inside grace window)
    GRACE    -> COOLDOWN     (grace expired, free sub revoked)
    COOLDOWN -> PENDING      (cooldown elapsed, user may opt-in again)

Failsafe: free subscription end_date is `now + free_sub_window_days` and is
extended by +1 day on every successful check (capped at the same window).
If the scheduler stops, the sub naturally expires within the window.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import bio_reward as bio_crud
from app.database.crud.subscription import generate_unique_short_id
from app.database.crud.transaction import create_transaction
from app.database.database import AsyncSessionLocal
from app.database.models import (
    BioRewardConfig,
    BioRewardParticipant,
    BioRewardStatus,
    PaymentMethod,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
)


logger = structlog.get_logger(__name__)


# ---------- Bio matching ----------


# Supported placeholders for accepted_bio_strings:
#   {{bot_username}}     -> "nozapretbot"
#   {{bot_mention}}      -> "@nozapretbot"
#   {{user_ref}}         -> user's referral code (e.g. "refRvEFY4DH")
#   {{user_ref_link}}    -> "https://t.me/<bot>?start=<code>"
PLACEHOLDER_KEYS = ('{{bot_username}}', '{{bot_mention}}', '{{user_ref}}', '{{user_ref_link}}')


def expand_bio_template(
    template: str,
    *,
    bot_username: str | None = None,
    user: User | None = None,
) -> str:
    """Replace placeholders in an admin-configured bio template with concrete values."""
    if not template:
        return ''
    bot_username = (bot_username or '').lstrip('@')
    code = (getattr(user, 'referral_code', None) or '').strip() if user else ''
    ref_link = f'https://t.me/{bot_username}?start={code}' if (bot_username and code) else ''
    return (
        template
        .replace('{{bot_username}}', bot_username)
        .replace('{{bot_mention}}', f'@{bot_username}' if bot_username else '')
        .replace('{{user_ref}}', code)
        .replace('{{user_ref_link}}', ref_link)
    )


def build_personal_referral_tokens(user: User) -> list[str]:
    """Tokens that prove the bio references this user's personal referral link."""
    tokens: list[str] = []
    code = (getattr(user, 'referral_code', None) or '').strip() if user else ''
    if code:
        tokens.append(code)
        tokens.append(f'start={code}')
        tokens.append(f'start={code.lower()}')
    return tokens


def _placeholder_resolutions(
    template: str, *, bot_username: str | None, user: User | None
) -> dict[str, str]:
    """Map of placeholders present in template to their resolved values."""
    bot_username = (bot_username or '').lstrip('@')
    code = (getattr(user, 'referral_code', None) or '').strip() if user else ''
    ref_link = f'https://t.me/{bot_username}?start={code}' if (bot_username and code) else ''
    full = {
        '{{bot_username}}': bot_username,
        '{{bot_mention}}': f'@{bot_username}' if bot_username else '',
        '{{user_ref}}': code,
        '{{user_ref_link}}': ref_link,
    }
    return {k: v for k, v in full.items() if k in template}


def bio_matches(
    bio: str | None,
    cfg: BioRewardConfig,
    personal_tokens: list[str],
    *,
    bot_username: str | None = None,
    user: User | None = None,
) -> bool:
    """Case-insensitive substring match.

    Each accepted_bio_strings entry may contain placeholders (see
    ``PLACEHOLDER_KEYS``); they are expanded per-user before matching so the
    same template works for every participant.

    Templates whose placeholders cannot be fully resolved (e.g. ``{{user_ref}}``
    when the user has no referral_code) are skipped entirely to avoid false
    positives where a template degenerates to a generic prefix.
    """
    if not bio:
        return False
    haystack = bio.lower()
    for needle in cfg.accepted_bio_strings or []:
        if not needle:
            continue
        template = str(needle)
        resolutions = _placeholder_resolutions(template, bot_username=bot_username, user=user)
        if any(v == '' for v in resolutions.values()):
            continue  # at least one placeholder unresolved → skip
        rendered = expand_bio_template(template, bot_username=bot_username, user=user)
        if not rendered.strip():
            continue
        if rendered.lower() in haystack:
            return True
    if cfg.match_personal_referral_link:
        for token in personal_tokens:
            if token and token.lower() in haystack:
                return True
    return False


# ---------- Paid-sub recalculation on revoke ----------


def recalc_paid_sub_on_revoke(
    *,
    paid_kopeks: int,
    discount_percent: int,
    total_days: int,
    start_date: datetime,
    now: datetime | None = None,
) -> dict:
    """Compute post-revocation outcome for a paid sub bought with the bio discount."""
    now = now or datetime.now(UTC)
    if total_days <= 0 or paid_kopeks <= 0 or discount_percent <= 0 or discount_percent >= 100:
        return {
            'new_end_date': start_date + timedelta(days=total_days),
            'debit_kopeks': 0,
            'used_days': 0,
            'entitled_days': total_days,
        }

    full_kopeks = paid_kopeks / (1 - discount_percent / 100)
    full_day_price = full_kopeks / total_days
    entitled_days = int(paid_kopeks // full_day_price)
    used_days = max(0, (now - start_date).days)

    if used_days >= entitled_days:
        over = used_days - entitled_days
        debit_kopeks = int(round(over * full_day_price))
        return {
            'new_end_date': now,
            'debit_kopeks': debit_kopeks,
            'used_days': used_days,
            'entitled_days': entitled_days,
        }

    new_end = start_date + timedelta(days=entitled_days)
    return {
        'new_end_date': new_end,
        'debit_kopeks': 0,
        'used_days': used_days,
        'entitled_days': entitled_days,
    }


# ---------- Discount integration helper ----------


async def get_active_discount_percent(db: AsyncSession, user: User | None) -> int:
    """Return bio-reward discount percent if user is in ACTIVE state, else 0.

    Cheap query: 1 row by user_id, no joins. Safe to call in pricing hot path.
    Honors both BIO_REWARD_ENABLED env flag and BioRewardConfig.enabled.
    """
    if user is None or db is None:
        return 0
    if not settings.BIO_REWARD_ENABLED:
        return 0
    try:
        cfg = await bio_crud.get_config(db)
        if not cfg.enabled or cfg.discount_percent <= 0:
            return 0
        participant = await bio_crud.get_participant_by_user_id(db, user.id)
        if participant is None:
            return 0
        if participant.status != BioRewardStatus.ACTIVE.value:
            return 0
        return max(0, min(100, int(cfg.discount_percent)))
    except (AttributeError, TypeError):
        return 0
    except Exception:
        return 0


class BioRewardService:
    def __init__(self):
        self._running = False
        self._bot: Bot | None = None
        self._semaphore = asyncio.Semaphore(settings.BIO_REWARD_SCHEDULER_CONCURRENCY)
        self._bot_username: str | None = None

    def set_bot(self, bot: Bot) -> None:
        self._bot = bot
        self._bot_username = None  # invalidate cache; resolved lazily

    def is_enabled(self) -> bool:
        return bool(settings.BIO_REWARD_ENABLED)

    async def get_bot_username(self) -> str | None:
        """Return cached bot username (without @). Populated on first use."""
        if self._bot_username:
            return self._bot_username
        if self._bot is None:
            return None
        try:
            me = await self._bot.me()
            self._bot_username = (getattr(me, 'username', None) or '').lstrip('@') or None
        except Exception as exc:
            logger.warning('bio_reward.bot_username_fetch_failed', err=str(exc))
            self._bot_username = None
        return self._bot_username

    async def _fetch_bio(self, telegram_id: int) -> str | None:
        if self._bot is None or telegram_id is None:
            # Distinguishes "bot never wired" from transient API errors in
            # logs: without this, a mis-wired bot reads as endless
            # fetch_failed with zero signal.
            logger.warning('bio_reward.fetch_bio.bot_not_set', telegram_id=telegram_id)
            return None
        async with self._semaphore:
            try:
                chat = await self._bot.get_chat(telegram_id)
                return getattr(chat, 'bio', None) or ''
            except Exception as exc:
                logger.warning('bio_reward.fetch_bio_failed', telegram_id=telegram_id, err=str(exc))
                return None

    async def opt_in(self, db: AsyncSession, user: User) -> tuple[BioRewardParticipant, str]:
        cfg = await bio_crud.get_config(db)
        if not (settings.BIO_REWARD_ENABLED and cfg.enabled):
            participant, _ = await bio_crud.get_or_create_participant(db, user.id)
            return participant, 'disabled'

        participant, _ = await bio_crud.get_or_create_participant(db, user.id)

        if participant.status == BioRewardStatus.COOLDOWN.value:
            now = datetime.now(UTC)
            cooldown_end = participant.cooldown_until
            if cooldown_end and cooldown_end > now:
                return participant, 'cooldown'
            await bio_crud.set_status(
                db, participant, BioRewardStatus.PENDING, cooldown_until=None, opted_in_at=now
            )

        await bio_crud.log_event(db, participant.id, 'opt_in')
        outcome = await self.check_user(db, participant, user=user)
        return participant, outcome

    async def check_user(
        self,
        db: AsyncSession,
        participant: BioRewardParticipant,
        *,
        user: User | None = None,
    ) -> str:
        cfg = await bio_crud.get_config(db)
        now = datetime.now(UTC)
        participant.last_check_at = now

        if user is None:
            user = participant.user

        if user is None or user.telegram_id is None:
            await db.commit()
            return 'no_user'

        bio = await self._fetch_bio(user.telegram_id)
        if bio is None and not participant.bypass_check:
            # Transient fetch failure (Telegram API error, flood limit,
            # network). NOT the same as "bio removed": leave the state
            # machine untouched and retry next tick. Only last_check_at
            # is persisted.
            await db.commit()
            return 'fetch_failed'
        participant.bio_snapshot = bio or ''

        tokens = build_personal_referral_tokens(user)
        bot_username = await self.get_bot_username()
        is_match = (
            bio_matches(bio, cfg, tokens, bot_username=bot_username, user=user)
            or bool(participant.bypass_check)
        )
        if is_match:
            participant.last_bio_seen_at = now

        outcome = 'noop'

        if is_match:
            if participant.status == BioRewardStatus.PENDING.value:
                await self._activate(db, participant, user, cfg)
                outcome = 'activated'
            elif participant.status == BioRewardStatus.GRACE.value:
                await bio_crud.set_status(
                    db, participant, BioRewardStatus.ACTIVE, grace_started_at=None
                )
                await self._extend_free_sub(db, participant, cfg)
                await bio_crud.log_event(db, participant.id, 'grace_recovered')
                outcome = 'recovered'
            elif participant.status == BioRewardStatus.ACTIVE.value:
                await self._extend_free_sub(db, participant, cfg)
                outcome = 'extended'
        else:
            if participant.status == BioRewardStatus.ACTIVE.value:
                await self._start_grace(db, participant, cfg)
                outcome = 'grace_started'
            elif participant.status == BioRewardStatus.GRACE.value:
                grace_deadline = (participant.grace_started_at or now) + timedelta(
                    hours=cfg.grace_period_hours
                )
                if now >= grace_deadline:
                    await self._revoke(db, participant, user, cfg)
                    outcome = 'revoked'
                else:
                    outcome = 'grace_pending'
            elif participant.status == BioRewardStatus.PENDING.value:
                outcome = 'pending'

        await db.commit()
        return outcome

    async def _activate(
        self, db: AsyncSession, participant: BioRewardParticipant, user: User, cfg: BioRewardConfig
    ) -> None:
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        active_paid = [
            s for s in await get_active_subscriptions_by_user_id(db, user.id) if not s.is_trial
        ]
        sub: Subscription | None = None
        if not active_paid:
            sub = await self._create_free_sub(db, user, cfg)
            participant.free_subscription_id = sub.id

        await bio_crud.set_status(
            db,
            participant,
            BioRewardStatus.ACTIVE,
            grace_started_at=None,
            cooldown_until=None,
        )
        await bio_crud.log_event(
            db,
            participant.id,
            'activated',
            {'free_subscription_id': sub.id if sub else None, 'has_paid': bool(active_paid)},
        )
        if cfg.notify_on_activate and self._bot and user.telegram_id:
            await self._notify(
                user,
                '🎉 <b>Поздравляем! Бесплатная подписка активирована</b>\n\n'
                'Мы нашли в описании вашего Telegram-профиля нужный текст. Теперь:\n'
                '• 🆓 Бесплатная подписка работает\n'
                f'• 💰 Скидка {cfg.discount_percent}% применяется ко всем платным тарифам\n\n'
                '⚠️ Важно: всё это действует, пока текст в описании остаётся на месте. '
                'Не удаляйте его — иначе подписка отключится.',
            )

    async def _create_free_sub(
        self, db: AsyncSession, user: User, cfg: BioRewardConfig
    ) -> Subscription:
        now = datetime.now(UTC)
        end_date = now + timedelta(days=cfg.free_sub_window_days)
        squads = [cfg.free_sub_squad_uuid] if cfg.free_sub_squad_uuid else []
        short_id = await generate_unique_short_id(db)
        sub = Subscription(
            user_id=user.id,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=True,  # reuse trial infrastructure (expiry, billing checks)
            is_bio_reward=True,  # marker for tag + UI overrides
            start_date=now,
            end_date=end_date,
            traffic_limit_gb=cfg.free_sub_traffic_gb_per_day,
            device_limit=cfg.free_sub_device_limit,
            connected_squads=squads,
            autopay_enabled=False,
            remnawave_short_id=short_id,
            # Bio-reward sub never gets white-list traffic. NULL disables WL per
            # resolve_wl_traffic_for_tariff convention (-1 sentinel maps to NULL).
            wl_traffic_limit_gb=None,
            wl_traffic_used_gb=0.0,
            wl_purchased_traffic_gb=0,
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

        try:
            from app.services.subscription_service import SubscriptionService

            svc = SubscriptionService()
            if user.remnawave_uuid:
                await svc.update_remnawave_user(db, sub, reset_traffic=True, sync_squads=True)
            else:
                await svc.create_remnawave_user(db, sub, reset_traffic=True)
        except Exception as exc:
            logger.warning('bio_reward.remnawave_provision_failed', user_id=user.id, err=str(exc))

        return sub

    async def _extend_free_sub(
        self, db: AsyncSession, participant: BioRewardParticipant, cfg: BioRewardConfig
    ) -> None:
        if not participant.free_subscription_id:
            return
        sub = await db.get(Subscription, participant.free_subscription_id)
        if sub is None:
            return
        if not sub.is_bio_reward:
            # Row was converted to a paid subscription (purchase flow clears
            # the marker). Detach so the scheduler never extends or
            # reactivates someone's paid sub.
            participant.free_subscription_id = None
            await db.commit()
            return
        new_end = datetime.now(UTC) + timedelta(days=cfg.free_sub_window_days)
        end_moved = False
        wl_cleared = False
        if sub.end_date is None or sub.end_date < new_end:
            sub.end_date = new_end
            sub.status = SubscriptionStatus.ACTIVE.value
            end_moved = True
        # Self-heal: bio-reward subs must never carry WL traffic. Older rows
        # (before forward fix in _create_free_sub) inherited the model default
        # of 5 GB; normalise here so next tick converges them.
        if sub.wl_traffic_limit_gb is not None:
            sub.wl_traffic_limit_gb = None
            sub.wl_traffic_used_gb = 0.0
            sub.wl_purchased_traffic_gb = 0
            sub.wl_traffic_reset_at = None
            wl_cleared = True
        if end_moved or wl_cleared:
            await db.commit()
        else:
            return

        # Push to Remnawave: panel sync is authoritative — an un-pushed local
        # extension gets reverted and the panel account expires at the
        # original creation+window. One call per participant per tick.
        try:
            from app.services.subscription_service import SubscriptionService

            user = participant.user
            if user is not None and getattr(user, 'remnawave_uuid', None):
                svc = SubscriptionService()
                await svc.update_remnawave_user(
                    db, sub, reset_traffic=False, sync_squads=False
                )
                logger.info(
                    'bio_reward.free_sub.remnawave_synced',
                    subscription_id=sub.id,
                    end_moved=end_moved,
                    wl_cleared=wl_cleared,
                )
        except Exception as exc:
            logger.warning(
                'bio_reward.free_sub.remnawave_sync_failed',
                subscription_id=sub.id,
                err=str(exc),
            )
            # Best-effort; next scheduler tick will retry via the same path.

    async def _start_grace(
        self, db: AsyncSession, participant: BioRewardParticipant, cfg: BioRewardConfig
    ) -> None:
        now = datetime.now(UTC)
        await bio_crud.set_status(db, participant, BioRewardStatus.GRACE, grace_started_at=now)
        await bio_crud.log_event(
            db, participant.id, 'grace_started', {'grace_hours': cfg.grace_period_hours}
        )
        if cfg.notify_on_grace and self._bot and participant.user and participant.user.telegram_id:
            await self._notify(
                participant.user,
                '⚠️ <b>Не нашли нужный текст в описании профиля</b>\n\n'
                'Похоже, вы убрали текст из описания вашего Telegram-профиля.\n\n'
                f'⏳ У вас есть <b>{cfg.grace_period_hours} ч.</b>, чтобы вернуть его обратно. '
                'Если вернёте — всё продолжит работать, ничего страшного не случится.\n\n'
                '❗ Если не вернёте за это время:\n'
                '• Бесплатная подписка отключится\n'
                '• Если у вас есть платная подписка со скидкой по акции — '
                'с баланса спишется доплата за дни, использованные сверх «честного» срока без скидки',
            )

    async def _revoke(
        self, db: AsyncSession, participant: BioRewardParticipant, user: User, cfg: BioRewardConfig
    ) -> None:
        now = datetime.now(UTC)
        if participant.free_subscription_id:
            sub = await db.get(Subscription, participant.free_subscription_id)
            if sub is not None and sub.status != SubscriptionStatus.DISABLED.value:
                sub.status = SubscriptionStatus.DISABLED.value
                sub.end_date = now
                await db.commit()

        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        total_debit = 0
        paid_subs = await get_active_subscriptions_by_user_id(db, user.id)
        for sub in paid_subs:
            pct = sub.bio_reward_discount_percent
            if not pct:
                continue
            total_days = max(0, (sub.end_date - sub.start_date).days)
            paid_tx = await self._latest_subscription_payment(db, user.id, sub)
            if paid_tx is None:
                continue
            outcome = recalc_paid_sub_on_revoke(
                paid_kopeks=paid_tx.amount_kopeks,
                discount_percent=pct,
                total_days=total_days,
                start_date=sub.start_date,
                now=now,
            )
            sub.end_date = outcome['new_end_date']
            if outcome['new_end_date'] <= now:
                sub.status = SubscriptionStatus.EXPIRED.value
            sub.bio_reward_discount_percent = None
            total_debit += outcome['debit_kopeks']

        actually_debited = 0
        if total_debit > 0:
            actually_debited = min(total_debit, max(0, user.balance_kopeks))
            if actually_debited > 0:
                user.balance_kopeks -= actually_debited
                await create_transaction(
                    db=db,
                    user_id=user.id,
                    type=TransactionType.SUBSCRIPTION_PAYMENT,
                    amount_kopeks=actually_debited,
                    description='Bio-reward revoke: списание за использованные дни сверх скидки',
                    payment_method=PaymentMethod.BALANCE,
                    commit=False,
                )

        cooldown_until = now + timedelta(hours=cfg.cooldown_hours)
        await bio_crud.set_status(
            db,
            participant,
            BioRewardStatus.COOLDOWN,
            grace_started_at=None,
            revoked_at=now,
            cooldown_until=cooldown_until,
        )
        await bio_crud.log_event(
            db,
            participant.id,
            'revoked',
            {'debit_kopeks': actually_debited, 'capped_off': total_debit - actually_debited},
        )

        if cfg.notify_on_revoke and self._bot and user.telegram_id:
            debit_line = (
                f'\n\n💸 С баланса списано <b>{actually_debited / 100:.2f} ₽</b> '
                'за дни, использованные сверх «честного» срока без скидки.'
                if actually_debited else ''
            )
            await self._notify(
                user,
                '❌ <b>Подписка по акции отключена</b>\n\n'
                'Текст так и не вернулся в описание вашего Telegram-профиля.\n\n'
                '• 🆓 Бесплатная подписка отключена\n'
                f'• ⏸ Снова поучаствовать в акции можно через <b>{cfg.cooldown_hours} ч.</b>'
                f'{debit_line}\n\n'
                'Если хотите вернуться к акции позже — просто добавьте нужный текст '
                'в описание после окончания блокировки и нажмите кнопку «Я участвую» снова.',
            )

    async def _latest_subscription_payment(
        self, db: AsyncSession, user_id: int, sub: Subscription
    ) -> Transaction | None:
        from sqlalchemy import select

        window_start = sub.start_date - timedelta(hours=24)
        window_end = sub.start_date + timedelta(hours=24)
        stmt = (
            select(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.created_at >= window_start,
                Transaction.created_at <= window_end,
            )
            .order_by(Transaction.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def _notify(self, user: User, html: str) -> None:
        if self._bot is None or not user.telegram_id:
            return
        try:
            await self._bot.send_message(user.telegram_id, html, parse_mode='HTML')
        except Exception as exc:
            logger.warning('bio_reward.notify_failed', user_id=user.id, err=str(exc))

    async def start_monitoring(self) -> None:
        self._running = True
        logger.info('bio_reward.scheduler.start')
        while self._running:
            interval = 60
            try:
                async with AsyncSessionLocal() as db:
                    cfg = await bio_crud.get_config(db)
                    interval = max(1, cfg.check_interval_minutes)
                    if not (settings.BIO_REWARD_ENABLED and cfg.enabled):
                        await asyncio.sleep(interval * 60)
                        continue

                    for p in await bio_crud.list_participants_in_cooldown_due(db):
                        await bio_crud.set_status(db, p, BioRewardStatus.PENDING, cooldown_until=None)

                    participants = await bio_crud.list_participants_for_check(db)
                    for participant in participants:
                        try:
                            await self.check_user(db, participant, user=participant.user)
                        except Exception as exc:
                            logger.warning(
                                'bio_reward.check_failed', participant_id=participant.id, err=str(exc)
                            )

                    # Daily analytics recompute (cheaper than a separate scheduler task).
                    try:
                        from app.services.bio_reward_analytics import (
                            last_computed_at,
                            recompute_all,
                        )

                        last = await last_computed_at(db)
                        now_utc = datetime.now(UTC)
                        if last is None or (now_utc - last) >= timedelta(hours=24):
                            stats = await recompute_all(db)
                            logger.info('bio_reward.analytics.recomputed', **stats)
                    except Exception as exc:
                        logger.warning('bio_reward.analytics.recompute_failed', err=str(exc))
            except Exception as exc:
                logger.error('bio_reward.scheduler.error', err=str(exc), exc_info=True)
            await asyncio.sleep(interval * 60)

    def stop_monitoring(self) -> None:
        self._running = False
        logger.info('bio_reward.scheduler.stop')


bio_reward_service = BioRewardService()
