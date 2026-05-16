import html
import json

import redis.asyncio as aioredis
import structlog
from aiogram import Bot
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import create_referral_earning, get_commission_payment_count, get_user_campaign_id
from app.database.crud.user import add_user_balance, get_user_by_id
from app.database.models import ReferralEarning, TransactionType, User
from app.services.notification_delivery_service import (
    notification_delivery_service,
)
from app.utils.user_utils import get_effective_referral_commission_percent


logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pending referral helpers (Redis)
# ---------------------------------------------------------------------------
_PENDING_REFERRAL_TTL = 7 * 24 * 3600  # 7 days

_redis_client: aioredis.Redis | None = None
_redis_initialized: bool = False


def _get_redis() -> aioredis.Redis | None:
    """Lazy async Redis client for pending referral storage."""
    global _redis_client, _redis_initialized
    if _redis_initialized:
        return _redis_client
    try:
        _redis_client = aioredis.from_url(settings.REDIS_URL)
        _redis_initialized = True
        logger.debug('Redis client for pending referrals initialized')
    except Exception as exc:
        logger.warning('Failed to initialize Redis for pending referrals', error=exc)
        _redis_client = None
        _redis_initialized = True
    return _redis_client


async def save_pending_referral(telegram_id: int, referral_code: str, referrer_id: int) -> bool:
    """Save pending referral to Redis for a not-yet-registered user.

    Called from /start handler immediately after resolving the referral code.
    The pending referral is picked up by create_user() or cabinet auth.
    """
    client = _get_redis()
    if client is None:
        return False
    try:
        key = f'pending_referral:{telegram_id}'
        data = json.dumps({'referral_code': referral_code, 'referrer_id': referrer_id})
        await client.setex(key, _PENDING_REFERRAL_TTL, data)
        logger.info(
            'Saved pending referral to Redis',
            telegram_id=telegram_id,
            referral_code=referral_code,
            referrer_id=referrer_id,
        )
        return True
    except Exception as exc:
        logger.warning('Failed to save pending referral to Redis', error=exc)
        return False


async def get_pending_referral(telegram_id: int) -> dict[str, str | int] | None:
    """Get pending referral from Redis.

    Returns ``{'referral_code': ..., 'referrer_id': ...}`` or ``None``.
    """
    client = _get_redis()
    if client is None:
        return None
    try:
        key = f'pending_referral:{telegram_id}'
        data = await client.get(key)
        if data:
            return json.loads(data)
        return None
    except Exception as exc:
        logger.warning('Failed to get pending referral from Redis', error=exc)
        return None


async def clear_pending_referral(telegram_id: int) -> None:
    """Clear pending referral after successful registration."""
    client = _get_redis()
    if client is None:
        return
    try:
        await client.delete(f'pending_referral:{telegram_id}')
    except Exception:
        pass


async def attach_referrer_if_missing(
    db: AsyncSession,
    user: User,
    *,
    referral_code: str | None = None,
    bot: Bot | None = None,
    source: str,
) -> int | None:
    """Eagerly attach a referrer to ``user`` when one isn't already set.

    Resolution order:
      1. Explicit ``referral_code`` argument (from URL state / FSM /
         API request body).
      2. Redis ``pending_referral`` keyed on ``user.telegram_id`` (the
         /start fallback for the race where the miniapp opens BEFORE
         the bot's /start handler has finished processing).

    Idempotent: if ``user.referred_by_id`` is already set, returns
    ``None`` immediately without firing anything. This is the key
    contract — it lets us call the helper from every entry point
    (bot /start, cabinet /telegram, widget, OIDC) without worrying
    about double-firing ``process_referral_registration`` (which would
    duplicate the ``referral_earning`` audit row).

    Self-referral is blocked at both ID and email level (the email
    check mirrors the pre-existing logic in
    ``_process_referral_code``).

    Side effects when the attachment succeeds:
      * Sets ``user.referred_by_id``, commits, refreshes.
      * Fires ``process_referral_registration`` (admin notification +
        ``referral_earning`` row with reason
        ``referral_registration_pending``).
      * Clears any matching Redis ``pending_referral`` entry.

    Args:
        source: short tag for audit logs (``bot_start``,
            ``cabinet_telegram``, ``cabinet_widget``, ``cabinet_oidc``,
            etc.). Keep stable so log queries stay useful.

    Returns:
        The attached ``referrer_id`` on success, ``None`` when nothing
        was attached (either user already had one, or no valid
        candidate was found).
    """
    if user.referred_by_id is not None:
        return None

    # ------------------------------------------------------------------
    # Candidate resolution.
    # ------------------------------------------------------------------
    from app.database.crud.user import get_user_by_referral_code as _resolve_by_code

    referrer: User | None = None

    if referral_code:
        try:
            referrer = await _resolve_by_code(db, referral_code)
        except Exception as exc:
            logger.warning(
                'attach_referrer_if_missing: failed to resolve referral_code',
                referral_code=referral_code,
                source=source,
                error=str(exc),
            )

    if referrer is None and user.telegram_id is not None:
        pending = await get_pending_referral(user.telegram_id)
        if pending and pending.get('referrer_id'):
            try:
                pending_referrer_id = int(pending['referrer_id'])
            except (TypeError, ValueError):
                pending_referrer_id = None
            if pending_referrer_id is not None:
                referrer = await get_user_by_id(db, pending_referrer_id)

    if referrer is None:
        return None

    # ------------------------------------------------------------------
    # Self-referral guards.
    # ------------------------------------------------------------------
    if referrer.id == user.id:
        return None
    if referrer.telegram_id is not None and user.telegram_id is not None and referrer.telegram_id == user.telegram_id:
        return None
    if referrer.email and user.email and referrer.email.lower() == user.email.lower():
        return None

    # ------------------------------------------------------------------
    # Atomic compare-and-set. The in-memory ``referred_by_id is None``
    # check above is per-session, so two concurrent callers (e.g. bot
    # /start AND cabinet /telegram on a delayed initData call, each
    # with its own AsyncSessionLocal) could both pass it and race to
    # write. A naive ``user.referred_by_id = x; await commit()`` would
    # last-write-win and silently flip an already-attached referrer.
    #
    # Use a conditional UPDATE so the DB itself enforces "attach only
    # when still NULL". ``rowcount == 1`` means we won the race; 0
    # means another session attached first — treat as no-op.
    # ------------------------------------------------------------------
    from sqlalchemy import update as _sa_update

    try:
        update_stmt = (
            _sa_update(User).where(User.id == user.id, User.referred_by_id.is_(None)).values(referred_by_id=referrer.id)
        )
        result = await db.execute(update_stmt)
        await db.commit()
    except Exception as exc:
        logger.error(
            'attach_referrer_if_missing: commit failed',
            user_id=user.id,
            referrer_id=referrer.id,
            source=source,
            error=str(exc),
        )
        try:
            await db.rollback()
        except Exception:
            pass
        return None

    # rowcount == 0 means another session beat us to the attach —
    # don't fire the registration event (the winning session already
    # did) and don't pretend we attached.
    if (result.rowcount or 0) == 0:
        logger.info(
            'attach_referrer_if_missing: lost the attach race, another session won',
            user_id=user.id,
            attempted_referrer_id=referrer.id,
            source=source,
        )
        # Refresh the in-memory object so the caller sees the winning referrer.
        try:
            await db.refresh(user)
        except Exception:
            pass
        return None

    # We won. Mirror the write onto the in-memory ORM attribute so the
    # caller sees the same value as the DB (the conditional UPDATE
    # bypasses ORM attribute machinery). Then refresh as a belt-and-
    # suspenders to surface any server-side defaults (updated_at, etc.).
    user.referred_by_id = referrer.id
    try:
        await db.refresh(user)
    except Exception:
        # Refresh failures are non-fatal — the write committed. Worst
        # case the caller's ORM-attached user shows a stale value
        # until next refetch.
        pass

    logger.info(
        'Referrer attached',
        user_id=user.id,
        referrer_id=referrer.id,
        source=source,
        had_explicit_code=referral_code is not None,
    )

    # Best-effort: clear the Redis pending row so a stale entry can't
    # double-attach via another entry point later.
    if user.telegram_id is not None:
        await clear_pending_referral(user.telegram_id)

    # Fire the registration event. We swallow exceptions because the
    # attachment itself is the load-bearing part — losing the
    # notification or the audit row is a softer failure than losing
    # the referrer link, and the audit row can be reconstructed
    # offline from ``users.referred_by_id``.
    #
    # When the caller didn't pass a bot (cabinet/FastAPI routes don't
    # have one in scope), lazy-create one via ``create_bot()`` so the
    # referrer still receives the Telegram notification. Same pattern
    # as ``_process_referral_code`` in app/cabinet/routes/auth.py.
    try:
        if bot is None:
            from app.bot_factory import create_bot

            async with create_bot() as event_bot:
                await process_referral_registration(db, user.id, referrer.id, bot=event_bot)
        else:
            await process_referral_registration(db, user.id, referrer.id, bot=bot)
    except Exception as exc:
        logger.error(
            'attach_referrer_if_missing: process_referral_registration failed (referrer still attached)',
            user_id=user.id,
            referrer_id=referrer.id,
            source=source,
            error=str(exc),
        )

    return referrer.id


# ---------------------------------------------------------------------------
# Pending campaign helpers (Redis)
#
# Mirrors pending_referral: lets us survive the case where /start <campaign>
# stored campaign_id only in FSM, but the user opened the cabinet WebApp before
# completing bot registration. The cabinet auth route reads this as a fallback
# when the HTTP request didn't carry an explicit campaign_slug.
# ---------------------------------------------------------------------------
_PENDING_CAMPAIGN_TTL = 7 * 24 * 3600  # 7 days


async def save_pending_campaign(telegram_id: int, campaign_slug: str, campaign_id: int) -> bool:
    """Save pending campaign attribution to Redis for a not-yet-registered user.

    Called from /start handler immediately after resolving an advertising campaign.
    Picked up by the cabinet auth route if the user opens the WebApp before
    completing the bot registration flow.
    """
    client = _get_redis()
    if client is None:
        return False
    try:
        key = f'pending_campaign:{telegram_id}'
        data = json.dumps({'campaign_slug': campaign_slug, 'campaign_id': campaign_id})
        await client.setex(key, _PENDING_CAMPAIGN_TTL, data)
        logger.info(
            'Saved pending campaign to Redis',
            telegram_id=telegram_id,
            campaign_slug=campaign_slug,
            campaign_id=campaign_id,
        )
        return True
    except Exception as exc:
        logger.warning('Failed to save pending campaign to Redis', error=exc)
        return False


async def get_pending_campaign(telegram_id: int) -> dict[str, str | int] | None:
    """Get pending campaign from Redis.

    Returns ``{'campaign_slug': ..., 'campaign_id': ...}`` or ``None``.
    """
    client = _get_redis()
    if client is None:
        return None
    try:
        key = f'pending_campaign:{telegram_id}'
        data = await client.get(key)
        if data:
            return json.loads(data)
        return None
    except Exception as exc:
        logger.warning('Failed to get pending campaign from Redis', error=exc)
        return None


async def clear_pending_campaign(telegram_id: int) -> None:
    """Clear pending campaign after successful application."""
    client = _get_redis()
    if client is None:
        return
    try:
        await client.delete(f'pending_campaign:{telegram_id}')
    except Exception:
        pass


async def _is_commission_limit_reached(db: AsyncSession, referrer_id: int, referral_id: int) -> bool:
    """Проверяет, исчерпан ли лимит комиссионных платежей для пары реферер-реферал."""
    if settings.REFERRAL_MAX_COMMISSION_PAYMENTS <= 0:
        return False
    paid_count = await get_commission_payment_count(db, referrer_id, referral_id)
    if paid_count >= settings.REFERRAL_MAX_COMMISSION_PAYMENTS:
        logger.info(
            'Лимит комиссионных платежей исчерпан',
            referrer_id=referrer_id,
            referral_id=referral_id,
            paid_count=paid_count,
            max_payments=settings.REFERRAL_MAX_COMMISSION_PAYMENTS,
        )
        return True
    return False


async def send_referral_notification(
    bot: Bot,
    telegram_id: int | None,
    message: str,
    user: User | None = None,
    bonus_kopeks: int = 0,
    referral_name: str = '',
):
    """
    Отправляет реферальное уведомление в Telegram или по email.

    Args:
        bot: Telegram Bot instance
        telegram_id: Telegram user ID (может быть None для email-пользователей)
        message: Текст уведомления
        user: User object (для email-only пользователей)
        bonus_kopeks: Сумма бонуса в копейках
        referral_name: Имя реферала
    """
    # Handle email-only users via notification delivery service
    if telegram_id is None:
        if user is not None:
            success = await notification_delivery_service.notify_referral_bonus(
                user=user,
                bonus_kopeks=bonus_kopeks,
                referral_name=referral_name,
                telegram_message=message,
            )
            if success:
                logger.info('✅ Email уведомление о реферале отправлено пользователю', user_id=user.id)
            else:
                logger.warning('⚠️ Не удалось отправить email уведомление пользователю', user_id=user.id)
        else:
            logger.debug('Пропуск уведомления: пользователь без telegram_id и без User object')
        return

    try:
        await bot.send_message(telegram_id, message, parse_mode='HTML')
        logger.info('✅ Уведомление отправлено пользователю', telegram_id=telegram_id)
    except Exception as e:
        logger.error('❌ Ошибка отправки уведомления пользователю', telegram_id=telegram_id, error=e)


async def process_referral_registration(db: AsyncSession, new_user_id: int, referrer_id: int, bot: Bot = None):
    try:
        if new_user_id == referrer_id:
            logger.warning('Self-referral blocked in process_referral_registration', user_id=new_user_id)
            return False

        new_user = await get_user_by_id(db, new_user_id)
        referrer = await get_user_by_id(db, referrer_id)

        if not new_user or not referrer:
            logger.error(
                'Пользователи не найдены: new_user_id=, referrer_id', new_user_id=new_user_id, referrer_id=referrer_id
            )
            return False

        if new_user.referred_by_id != referrer_id:
            logger.error('Пользователь не привязан к рефереру', new_user_id=new_user_id, referrer_id=referrer_id)
            return False

        # Cross-session de-dup. Bot and cabinet run on separate
        # ``AsyncSessionLocal`` instances; the per-session idempotency
        # guard in ``attach_referrer_if_missing`` is not enough on its
        # own. Two layers protect the audit row:
        #
        #   1. Fast-path SELECT below — handles the common case
        #      cheaply, no exception machinery.
        #   2. Partial UNIQUE index ``uq_referral_earnings_registration_pending``
        #      (Alembic 0085) — catches the sub-millisecond window
        #      where both sessions pass the SELECT before either
        #      INSERT commits. On collision the second INSERT raises
        #      ``IntegrityError``, which we swallow as a duplicate.
        #
        # Only the ``referral_registration_pending`` reason is deduped.
        # Bonus rows (``referral_first_topup_bonus``,
        # ``referral_commission_topup``, etc.) are intentionally
        # allowed to repeat and are protected at their own call sites.
        from sqlalchemy import select as _select
        from sqlalchemy.exc import IntegrityError as _IntegrityError

        existing = await db.execute(
            _select(ReferralEarning.id)
            .where(
                ReferralEarning.user_id == referrer_id,
                ReferralEarning.referral_id == new_user_id,
                ReferralEarning.reason == 'referral_registration_pending',
            )
            .limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            logger.info(
                'Referral registration already recorded, skipping duplicate',
                new_user_id=new_user_id,
                referrer_id=referrer_id,
            )
            return True

        campaign_id = await get_user_campaign_id(db, new_user_id)
        try:
            await create_referral_earning(
                db=db,
                user_id=referrer_id,
                referral_id=new_user_id,
                amount_kopeks=0,
                reason='referral_registration_pending',
                campaign_id=campaign_id,
            )
        except _IntegrityError:
            # Lost the race against a concurrent session AFTER our
            # SELECT but BEFORE our commit — the unique index caught it.
            # Roll back so the session is usable for the notification
            # block below.
            await db.rollback()
            logger.info(
                'Referral registration race caught by unique index, treating as duplicate',
                new_user_id=new_user_id,
                referrer_id=referrer_id,
            )
            return True

        try:
            from app.services.referral_contest_service import referral_contest_service

            await referral_contest_service.on_referral_registration(db, new_user_id)
        except Exception as exc:
            logger.debug('Не удалось записать конкурсную регистрацию', exc=exc)

        if bot:
            commission_percent = get_effective_referral_commission_percent(referrer)
            referral_notification = (
                f'🎉 <b>Добро пожаловать!</b>\n\n'
                f'Вы перешли по реферальной ссылке пользователя <b>{html.escape(referrer.full_name)}</b>!'
            )
            if settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0:
                referral_notification += (
                    f'\n\n💰 При первом пополнении от {settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)} '
                    f'вы получите бонус {settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}!'
                )
            await send_referral_notification(bot, new_user.telegram_id, referral_notification, user=new_user)

            inviter_notification = (
                f'👥 <b>Новый реферал!</b>\n\n'
                f'По вашей ссылке зарегистрировался пользователь <b>{html.escape(new_user.full_name)}</b>!\n\n'
                f'💰 Когда он пополнит баланс от {settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)}, '
            )
            if settings.REFERRAL_INVITER_BONUS_KOPEKS > 0 and commission_percent > 0:
                inviter_notification += (
                    f'вы получите {settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)} + '
                    f'{commission_percent}% от суммы пополнения.\n\n'
                )
            elif settings.REFERRAL_INVITER_BONUS_KOPEKS > 0:
                inviter_notification += (
                    f'вы получите {settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)}.\n\n'
                )
            elif commission_percent > 0:
                inviter_notification += f'вы получите {commission_percent}% от суммы.\n\n'
            else:
                inviter_notification += 'вы получите уведомление.\n\n'
            if commission_percent > 0:
                inviter_notification += (
                    f'📈 С каждого последующего пополнения вы будете получать {commission_percent}% комиссии.'
                )
            await send_referral_notification(
                bot, referrer.telegram_id, inviter_notification, user=referrer, referral_name=new_user.full_name
            )

        logger.info(
            '✅ Зарегистрирован реферал для . Бонусы будут выданы после пополнения.',
            new_user_id=new_user_id,
            referrer_id=referrer_id,
        )
        return True

    except Exception as e:
        logger.error('Ошибка обработки реферальной регистрации', error=e)
        return False


async def process_referral_topup(db: AsyncSession, user_id: int, topup_amount_kopeks: int, bot: Bot = None):
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.referred_by_id:
            logger.debug('Пользователь не является рефералом, пропуск комиссии', user_id=user_id)
            return True

        referrer = await get_user_by_id(db, user.referred_by_id)
        if not referrer:
            logger.error(
                'Реферер не найден, комиссия не начислена', referred_by_id=user.referred_by_id, user_id=user_id
            )
            return False

        campaign_id = await get_user_campaign_id(db, user.id)
        commission_percent = get_effective_referral_commission_percent(referrer)

        logger.info(
            'Обработка реферального пополнения',
            user_id=user_id,
            referrer_id=referrer.id,
            topup_amount_kopeks=topup_amount_kopeks,
            campaign_id=campaign_id,
            commission_percent=commission_percent,
            has_made_first_topup=user.has_made_first_topup,
        )
        qualifies_for_first_bonus = topup_amount_kopeks >= settings.REFERRAL_MINIMUM_TOPUP_KOPEKS
        commission_amount = 0
        if commission_percent > 0:
            commission_amount = int(topup_amount_kopeks * commission_percent / 100)

        if not user.has_made_first_topup:
            if not qualifies_for_first_bonus:
                logger.info(
                    'Пополнение на ₽ меньше минимума для первого бонуса, но комиссия будет начислена',
                    user_id=user_id,
                    topup_amount_kopeks=topup_amount_kopeks / 100,
                )

                if commission_amount > 0 and await _is_commission_limit_reached(db, referrer.id, user.id):
                    return True

                if commission_amount > 0:
                    balance_ok = await add_user_balance(
                        db,
                        referrer,
                        commission_amount,
                        f'Комиссия {commission_percent}% с пополнения {user.full_name}',
                        transaction_type=TransactionType.REFERRAL_REWARD,
                        bot=bot,
                    )

                    if balance_ok:
                        await create_referral_earning(
                            db=db,
                            user_id=referrer.id,
                            referral_id=user.id,
                            amount_kopeks=commission_amount,
                            reason='referral_commission_topup',
                            campaign_id=campaign_id,
                        )

                        logger.info(
                            '💰 Комиссия с пополнения: получил ₽ (до первого бонуса)',
                            telegram_id=referrer.telegram_id,
                            commission_amount=commission_amount / 100,
                        )

                        if bot:
                            commission_notification = (
                                f'💰 <b>Реферальная комиссия!</b>\n\n'
                                f'Ваш реферал <b>{html.escape(user.full_name)}</b> пополнил баланс на '
                                f'{settings.format_price(topup_amount_kopeks)}\n\n'
                                f'🎁 Ваша комиссия ({commission_percent}%): '
                                f'{settings.format_price(commission_amount)}\n\n'
                                f'💎 Средства зачислены на ваш баланс.'
                            )
                            await send_referral_notification(
                                bot,
                                referrer.telegram_id,
                                commission_notification,
                                user=referrer,
                                bonus_kopeks=commission_amount,
                                referral_name=user.full_name,
                            )
                    else:
                        logger.error(
                            'Не удалось начислить комиссию на баланс, ReferralEarning не создан',
                            referrer_id=referrer.id,
                            commission_amount=commission_amount,
                        )

                return True

            user.has_made_first_topup = True
            await db.commit()

            try:
                await db.execute(
                    delete(ReferralEarning).where(
                        ReferralEarning.user_id == referrer.id,
                        ReferralEarning.referral_id == user.id,
                        ReferralEarning.reason == 'referral_registration_pending',
                    )
                )
                await db.commit()
                logger.info("🗑️ Удалена запись 'ожидание пополнения' для реферала", user_id=user.id)
            except Exception as e:
                logger.error('Ошибка удаления записи ожидания', error=e)

            if settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0:
                bonus_ok = await add_user_balance(
                    db,
                    user,
                    settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
                    'Бонус за первое пополнение по реферальной программе',
                    transaction_type=TransactionType.REFERRAL_REWARD,
                    bot=bot,
                )
                if bonus_ok:
                    logger.info(
                        '💰 Реферал получил бонус ₽',
                        user_id=user.id,
                        REFERRAL_FIRST_TOPUP_BONUS_KOPEKS=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS / 100,
                    )

                    if bot:
                        bonus_notification = (
                            f'🎉 <b>Бонус получен!</b>\n\n'
                            f'За первое пополнение вы получили бонус '
                            f'{settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}!\n\n'
                            f'💎 Средства зачислены на ваш баланс.'
                        )
                        await send_referral_notification(
                            bot,
                            user.telegram_id,
                            bonus_notification,
                            user=user,
                            bonus_kopeks=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
                        )
                else:
                    logger.error(
                        'Не удалось начислить бонус за первое пополнение',
                        user_id=user.id,
                        bonus_kopeks=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
                    )

            commission_amount = int(topup_amount_kopeks * commission_percent / 100)
            inviter_bonus = settings.REFERRAL_INVITER_BONUS_KOPEKS + commission_amount

            if inviter_bonus > 0:
                balance_ok = await add_user_balance(
                    db,
                    referrer,
                    inviter_bonus,
                    f'Бонус за первое пополнение реферала {user.full_name}',
                    transaction_type=TransactionType.REFERRAL_REWARD,
                    bot=bot,
                )

                if balance_ok:
                    await create_referral_earning(
                        db=db,
                        user_id=referrer.id,
                        referral_id=user.id,
                        amount_kopeks=inviter_bonus,
                        reason='referral_first_topup',
                        campaign_id=campaign_id,
                    )

                    referrer_id = referrer.telegram_id or referrer.email or f'user#{referrer.id}'
                    logger.info(
                        '💰 Реферер получил бонус ₽', referrer_id=referrer_id, inviter_bonus=inviter_bonus / 100
                    )

                    if bot:
                        bonus_parts = []
                        if settings.REFERRAL_INVITER_BONUS_KOPEKS > 0:
                            bonus_parts.append(
                                f'фикс. бонус {settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)}'
                            )
                        if commission_amount > 0:
                            bonus_parts.append(
                                f'комиссия {commission_percent}% = {settings.format_price(commission_amount)}'
                            )
                        bonus_breakdown = ' + '.join(bonus_parts)
                        inviter_bonus_notification = (
                            f'💰 <b>Реферальная награда!</b>\n\n'
                            f'Ваш реферал <b>{html.escape(user.full_name)}</b> сделал первое пополнение '
                            f'на {settings.format_price(topup_amount_kopeks)}!\n\n'
                            f'🎁 Ваша награда: {settings.format_price(inviter_bonus)}'
                            f' ({bonus_breakdown})'
                        )
                        if commission_percent > 0:
                            inviter_bonus_notification += (
                                f'\n\n📈 Теперь с каждого его пополнения вы будете получать '
                                f'{commission_percent}% комиссии.'
                            )
                        await send_referral_notification(
                            bot,
                            referrer.telegram_id,
                            inviter_bonus_notification,
                            user=referrer,
                            bonus_kopeks=inviter_bonus,
                            referral_name=user.full_name,
                        )
                else:
                    logger.error(
                        'Не удалось начислить бонус на баланс, ReferralEarning не создан',
                        referrer_id=referrer.id,
                        inviter_bonus=inviter_bonus,
                    )

        elif commission_amount > 0:
            if await _is_commission_limit_reached(db, referrer.id, user.id):
                return True

            balance_ok = await add_user_balance(
                db,
                referrer,
                commission_amount,
                f'Комиссия {commission_percent}% с пополнения {user.full_name}',
                transaction_type=TransactionType.REFERRAL_REWARD,
                bot=bot,
            )

            if balance_ok:
                await create_referral_earning(
                    db=db,
                    user_id=referrer.id,
                    referral_id=user.id,
                    amount_kopeks=commission_amount,
                    reason='referral_commission_topup',
                    campaign_id=campaign_id,
                )

                referrer_id = referrer.telegram_id or referrer.email or f'user#{referrer.id}'
                logger.info(
                    '💰 Комиссия с пополнения: получил ₽',
                    referrer_id=referrer_id,
                    commission_amount=commission_amount / 100,
                )

                if bot:
                    commission_notification = (
                        f'💰 <b>Реферальная комиссия!</b>\n\n'
                        f'Ваш реферал <b>{html.escape(user.full_name)}</b> пополнил баланс на '
                        f'{settings.format_price(topup_amount_kopeks)}\n\n'
                        f'🎁 Ваша комиссия ({commission_percent}%): '
                        f'{settings.format_price(commission_amount)}\n\n'
                        f'💎 Средства зачислены на ваш баланс.'
                    )
                    await send_referral_notification(
                        bot,
                        referrer.telegram_id,
                        commission_notification,
                        user=referrer,
                        bonus_kopeks=commission_amount,
                        referral_name=user.full_name,
                    )
            else:
                logger.error(
                    'Не удалось начислить комиссию на баланс, ReferralEarning не создан',
                    referrer_id=referrer.id,
                    commission_amount=commission_amount,
                )

        return True

    except Exception as e:
        logger.error('Ошибка обработки пополнения реферала', error=e)
        return False


async def process_referral_purchase(
    db: AsyncSession, user_id: int, purchase_amount_kopeks: int, transaction_id: int = None, bot: Bot = None
):
    """Process referral commission for balance-based subscription purchases.

    INTENTIONALLY UNUSED. This function is NOT called from subscription purchase flows.
    Commission is only earned when referred users make actual payments through payment
    providers (via process_referral_topup). Balance-based subscription purchases
    (from admin credits, campaign bonuses, or promo codes) do NOT trigger commission,
    because the partner already received commission at the time the user topped up
    their balance. Calling this would cause double-commission.

    Kept for potential future use cases where balance-independent purchase tracking
    is needed (e.g. audit trail records with zero commission).
    """
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.referred_by_id:
            return True

        referrer = await get_user_by_id(db, user.referred_by_id)
        if not referrer:
            logger.error('Реферер не найден', referred_by_id=user.referred_by_id)
            return False

        commission_percent = get_effective_referral_commission_percent(referrer)

        commission_amount = int(purchase_amount_kopeks * commission_percent / 100)

        if commission_amount > 0:
            await add_user_balance(
                db, referrer, commission_amount, f'Комиссия {commission_percent}% с покупки {user.full_name}', bot=bot
            )

            campaign_id = await get_user_campaign_id(db, user.id)
            await create_referral_earning(
                db=db,
                user_id=referrer.id,
                referral_id=user.id,
                amount_kopeks=commission_amount,
                reason='referral_commission',
                referral_transaction_id=transaction_id,
                campaign_id=campaign_id,
            )

            referrer_id = referrer.telegram_id or referrer.email or f'user#{referrer.id}'
            logger.info(
                '💰 Комиссия с покупки: получил ₽', referrer_id=referrer_id, commission_amount=commission_amount / 100
            )

            if bot:
                purchase_commission_notification = (
                    f'💰 <b>Комиссия с покупки!</b>\n\n'
                    f'Ваш реферал <b>{html.escape(user.full_name)}</b> совершил покупку на '
                    f'{settings.format_price(purchase_amount_kopeks)}\n\n'
                    f'🎁 Ваша комиссия ({commission_percent}%): '
                    f'{settings.format_price(commission_amount)}\n\n'
                    f'💎 Средства зачислены на ваш баланс.'
                )
                await send_referral_notification(
                    bot,
                    referrer.telegram_id,
                    purchase_commission_notification,
                    user=referrer,
                    bonus_kopeks=commission_amount,
                    referral_name=user.full_name,
                )

        if not user.has_had_paid_subscription:
            user.has_had_paid_subscription = True
            await db.commit()
            logger.info('✅ Пользователь отмечен как имевший платную подписку', user_id=user_id)

        return True

    except Exception as e:
        logger.error('Ошибка обработки покупки реферала', error=e)
        import traceback

        logger.error('Полный traceback', format_exc=traceback.format_exc())
        return False
