from __future__ import annotations

import html
import math
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.discount_offer import get_latest_claimed_offer_for_user
from app.database.models import ServerSquad, SubscriptionTemporaryAccess, User


def _escape_format_braces(text: str) -> str:
    """Escape braces so str.format treats them as literals."""

    return text.replace('{', '{{').replace('}', '}}')


def get_user_active_promo_discount_percent(user: User | None) -> int:
    if not user:
        return 0

    try:
        percent = int(getattr(user, 'promo_offer_discount_percent', 0) or 0)
    except (TypeError, ValueError):
        return 0

    expires_at = getattr(user, 'promo_offer_discount_expires_at', None)
    if expires_at and expires_at <= datetime.now(UTC):
        return 0

    return max(0, min(100, percent))


async def consume_user_promo_offer(db: AsyncSession, user_id: int) -> bool:
    """Consume the user's one-shot promo-offer discount (zeroes out the fields).

    Used by external payment fulfillment handlers (Stars, YooKassa)
    where subtract_user_balance (which normally consumes the offer) is not called.
    Returns True if an offer was actually consumed.
    """
    from app.database.crud.promo_offer_log import log_promo_offer_action

    result = await db.execute(select(User).where(User.id == user_id).with_for_update())
    user = result.scalar_one_or_none()
    if not user:
        return False

    current_percent = int(getattr(user, 'promo_offer_discount_percent', 0) or 0)
    if current_percent <= 0:
        return False

    offer_id = getattr(user, 'promo_offer_discount_source', None)
    user.promo_offer_discount_percent = 0
    user.promo_offer_discount_source = None
    user.promo_offer_discount_expires_at = None
    await db.flush()

    try:
        await log_promo_offer_action(
            db,
            user_id=user_id,
            offer_id=offer_id,
            action='consumed_external_payment',
            discount_percent=current_percent,
        )
    except Exception:
        pass  # Non-critical logging

    return True


def _format_time_left(seconds_left: int, language: str) -> str:
    total_minutes = max(1, math.ceil(seconds_left / 60))
    days, remainder_minutes = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(remainder_minutes, 60)

    language_code = (language or 'ru').split('-')[0].lower()
    if language_code == 'en':
        day_label, hour_label, minute_label = 'd', 'h', 'm'
    else:
        day_label, hour_label, minute_label = 'д', 'ч', 'м'

    parts: list[str] = []
    if days:
        parts.append(f'{days}{day_label}')
    if hours or days:
        parts.append(f'{hours}{hour_label}')
    parts.append(f'{minutes}{minute_label}')
    return ' '.join(parts)


def _build_progress_bar(seconds_left: int, total_seconds: int) -> str:
    if total_seconds <= 0:
        total_seconds = seconds_left or 1

    ratio = max(0.0, min(1.0, seconds_left / total_seconds))
    bar_length = 10
    filled_segments = int(round(ratio * bar_length))
    filled_segments = max(0, min(bar_length, filled_segments))
    if filled_segments == 0 and seconds_left > 0:
        filled_segments = 1

    return f'[{"█" * filled_segments}{"░" * (bar_length - filled_segments)}]'


async def build_promo_offer_timer_line(
    db: AsyncSession,
    user: User,
    texts,
) -> str | None:
    expires_at = getattr(user, 'promo_offer_discount_expires_at', None)
    if not expires_at:
        return None

    now = datetime.now(UTC)
    if expires_at <= now:
        return None

    seconds_left = int((expires_at - now).total_seconds())
    if seconds_left <= 0:
        return None

    total_seconds: int | None = None
    source = getattr(user, 'promo_offer_discount_source', None)

    try:
        offer = await get_latest_claimed_offer_for_user(db, user.id, source)
    except Exception:
        offer = None

    if offer and getattr(offer, 'claimed_at', None):
        total_seconds = int((expires_at - offer.claimed_at).total_seconds())
        if total_seconds <= 0:
            total_seconds = None

    if total_seconds is None and offer:
        extra_data = getattr(offer, 'extra_data', None)
        if isinstance(extra_data, dict):
            raw_duration = extra_data.get('active_discount_hours') or extra_data.get('duration_hours')
        else:
            raw_duration = None
        try:
            if raw_duration:
                total_seconds = int(float(raw_duration) * 3600)
        except (TypeError, ValueError):
            total_seconds = None

    if total_seconds is None or total_seconds <= 0:
        total_seconds = seconds_left

    bar = _build_progress_bar(seconds_left, total_seconds)
    time_left_text = _format_time_left(seconds_left, getattr(texts, 'language', 'ru'))

    template = texts.t(
        'SUBSCRIPTION_PROMO_DISCOUNT_TIMER',
        '⏳ Discount active for {time_left}\n<code>{bar}</code>',
    )
    return template.format(bar=bar, time_left=time_left_text)


async def build_promo_offer_hint(
    db: AsyncSession,
    user: User,
    texts,
    percent: int | None = None,
) -> str | None:
    if percent is None:
        percent = get_user_active_promo_discount_percent(user)

    if percent <= 0:
        return None

    base_hint = texts.t(
        'SUBSCRIPTION_PROMO_DISCOUNT_HINT',
        '⚡ Extra {percent}% discount is active and will apply automatically. It stacks with other discounts.',
    ).format(percent=percent)

    timer_line = await build_promo_offer_timer_line(db, user, texts)
    if timer_line:
        return f'{base_hint}\n{timer_line}'

    return base_hint


async def build_test_access_hint(
    db: AsyncSession,
    user: User,
    texts,
) -> str | None:
    subscription = getattr(user, 'subscription', None)
    if not subscription:
        return None

    subscription_id = getattr(subscription, 'id', None)
    if not subscription_id:
        return None

    now = datetime.now(UTC)

    result = await db.execute(
        select(SubscriptionTemporaryAccess)
        .options(selectinload(SubscriptionTemporaryAccess.offer))
        .where(
            SubscriptionTemporaryAccess.subscription_id == subscription_id,
            SubscriptionTemporaryAccess.is_active == True,
            SubscriptionTemporaryAccess.expires_at > now,
        )
        .order_by(SubscriptionTemporaryAccess.expires_at.desc())
    )
    entries: Sequence[SubscriptionTemporaryAccess] = result.scalars().all()

    active_entries = [entry for entry in entries if entry.expires_at and entry.expires_at > now]
    if not active_entries:
        return None

    latest_expiry = max(entry.expires_at for entry in active_entries)
    seconds_left = int((latest_expiry - now).total_seconds())
    if seconds_left <= 0:
        return None

    total_seconds: int | None = None
    for entry in active_entries:
        offer = entry.offer
        claimed_at = getattr(offer, 'claimed_at', None) if offer else None
        if claimed_at:
            total = int((entry.expires_at - claimed_at).total_seconds())
            if total > 0 and (total_seconds is None or total > total_seconds):
                total_seconds = total

    if total_seconds is None or total_seconds <= 0:
        total_seconds = seconds_left

    bar = _build_progress_bar(seconds_left, total_seconds)
    time_left_text = _format_time_left(seconds_left, getattr(texts, 'language', 'ru'))

    unique_squad_uuids: list[str] = []
    seen_squads: set[str] = set()
    for entry in active_entries:
        squad_uuid = getattr(entry, 'squad_uuid', None)
        if squad_uuid and squad_uuid not in seen_squads:
            seen_squads.add(squad_uuid)
            unique_squad_uuids.append(squad_uuid)

    squad_display_names: list[str] = []
    if unique_squad_uuids:
        squads_result = await db.execute(
            select(ServerSquad.squad_uuid, ServerSquad.display_name).where(
                ServerSquad.squad_uuid.in_(unique_squad_uuids)
            )
        )
        names_map = {
            squad_uuid: html.escape(display_name) for squad_uuid, display_name in squads_result.all() if display_name
        }
        for squad_uuid in unique_squad_uuids:
            if squad_uuid in names_map:
                squad_display_names.append(names_map[squad_uuid])
            else:
                squad_display_names.append(html.escape(squad_uuid))

    if squad_display_names:
        servers_display = ', '.join(squad_display_names)
    elif unique_squad_uuids:
        servers_display = ', '.join(html.escape(squad_uuid) for squad_uuid in unique_squad_uuids)
    else:
        servers_display = str(len(active_entries))

    header_template = texts.t(
        'MAIN_MENU_TEST_ACCESS_HEADER',
        '🧪 Test servers active: {servers}',
    )
    timer_template = texts.t(
        'MAIN_MENU_TEST_ACCESS_TIMER',
        '⏳ Access active for {time_left}\n<code>{bar}</code>',
    )

    header = header_template.format(servers=_escape_format_braces(servers_display))
    timer_line = timer_template.format(time_left=time_left_text, bar=bar)

    return f'{header}\n{timer_line}'
