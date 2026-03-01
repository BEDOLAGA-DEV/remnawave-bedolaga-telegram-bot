import re
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RequiredChannel, UserChannelSubscription


logger = structlog.get_logger(__name__)

# Explicit allowlist of fields that can be updated via update_channel()
_UPDATABLE_FIELDS = frozenset(
    {
        'channel_id',
        'channel_link',
        'title',
        'is_active',
        'sort_order',
        'disable_trial_on_leave',
        'disable_paid_on_leave',
    }
)

# Validation patterns for channel_id
_CHANNEL_ID_NUMERIC = re.compile(r'^-100\d{10,13}$')
_BARE_DIGITS = re.compile(r'^\d{10,13}$')


def validate_channel_id(channel_id: str) -> str:
    """Validate and normalize channel_id. Auto-prefixes -100 for bare digits.

    Raises ValueError on invalid input.
    """
    channel_id = channel_id.strip()
    if _CHANNEL_ID_NUMERIC.match(channel_id):
        return channel_id
    if _BARE_DIGITS.match(channel_id):
        return f'-100{channel_id}'
    raise ValueError(
        f'Invalid channel_id format: {channel_id!r}. '
        'Enter numeric channel ID (e.g. 1234567890) — prefix -100 is added automatically'
    )


# -- RequiredChannel CRUD --------------------------------------------------------


async def get_active_channels(db: AsyncSession) -> list[RequiredChannel]:
    """Get all active required channels (sorted by sort_order)."""
    result = await db.execute(
        select(RequiredChannel)
        .where(RequiredChannel.is_active.is_(True))
        .order_by(RequiredChannel.sort_order, RequiredChannel.id)
    )
    return list(result.scalars().all())


async def get_all_channels(db: AsyncSession) -> list[RequiredChannel]:
    """Get all required channels (including inactive)."""
    result = await db.execute(select(RequiredChannel).order_by(RequiredChannel.sort_order, RequiredChannel.id))
    return list(result.scalars().all())


async def get_channel_by_id(db: AsyncSession, channel_db_id: int) -> RequiredChannel | None:
    result = await db.execute(select(RequiredChannel).where(RequiredChannel.id == channel_db_id))
    return result.scalar_one_or_none()


async def get_channel_by_channel_id(db: AsyncSession, channel_id: str) -> RequiredChannel | None:
    result = await db.execute(select(RequiredChannel).where(RequiredChannel.channel_id == channel_id))
    return result.scalar_one_or_none()


async def add_channel(
    db: AsyncSession,
    channel_id: str,
    channel_link: str | None = None,
    title: str | None = None,
    disable_trial_on_leave: bool = True,
    disable_paid_on_leave: bool = False,
) -> RequiredChannel:
    channel_id = validate_channel_id(channel_id)
    channel = RequiredChannel(
        channel_id=channel_id,
        channel_link=channel_link,
        title=title,
        disable_trial_on_leave=disable_trial_on_leave,
        disable_paid_on_leave=disable_paid_on_leave,
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return channel


async def update_channel(
    db: AsyncSession,
    channel_db_id: int,
    **kwargs,
) -> RequiredChannel | None:
    """Update channel fields. Only fields in _UPDATABLE_FIELDS are accepted."""
    channel = await get_channel_by_id(db, channel_db_id)
    if not channel:
        return None

    for key, value in kwargs.items():
        if key not in _UPDATABLE_FIELDS:
            logger.warning('Rejected update of non-updatable field', field=key)
            continue
        if key == 'channel_id' and value is not None:
            value = validate_channel_id(value)
        setattr(channel, key, value)

    channel.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(channel)
    return channel


async def delete_channel(db: AsyncSession, channel_db_id: int) -> bool:
    channel = await get_channel_by_id(db, channel_db_id)
    if not channel:
        return False
    # Also clean up user subscriptions for this channel
    await db.execute(delete(UserChannelSubscription).where(UserChannelSubscription.channel_id == channel.channel_id))
    await db.delete(channel)
    await db.commit()
    return True


async def toggle_channel(db: AsyncSession, channel_db_id: int) -> RequiredChannel | None:
    channel = await get_channel_by_id(db, channel_db_id)
    if not channel:
        return None
    channel.is_active = not channel.is_active
    channel.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(channel)
    return channel


# -- UserChannelSubscription CRUD ------------------------------------------------


async def upsert_user_channel_sub(
    db: AsyncSession,
    telegram_id: int,
    channel_id: str,
    is_member: bool,
) -> None:
    """Upsert user subscription status (PostgreSQL ON CONFLICT)."""
    now = datetime.now(UTC)  # Single timestamp for both INSERT and UPDATE
    stmt = (
        pg_insert(UserChannelSubscription)
        .values(
            telegram_id=telegram_id,
            channel_id=channel_id,
            is_member=is_member,
            checked_at=now,
        )
        .on_conflict_do_update(
            constraint='uq_user_channel_sub',
            set_={
                'is_member': is_member,
                'checked_at': now,
            },
        )
    )
    await db.execute(stmt)
    # NOTE: caller is responsible for commit (allows batching)


async def get_user_channel_subs(
    db: AsyncSession,
    telegram_id: int,
) -> list[UserChannelSubscription]:
    """Get all channel subscriptions for a user."""
    result = await db.execute(select(UserChannelSubscription).where(UserChannelSubscription.telegram_id == telegram_id))
    return list(result.scalars().all())


async def get_user_channel_sub(
    db: AsyncSession,
    telegram_id: int,
    channel_id: str,
) -> UserChannelSubscription | None:
    result = await db.execute(
        select(UserChannelSubscription).where(
            UserChannelSubscription.telegram_id == telegram_id,
            UserChannelSubscription.channel_id == channel_id,
        )
    )
    return result.scalar_one_or_none()


async def bulk_upsert_user_subs(
    db: AsyncSession,
    telegram_id: int,
    subs: dict[str, bool],  # {channel_id: is_member}
) -> None:
    """Batch upsert user subscriptions with single multi-row INSERT."""
    if not subs:
        return
    now = datetime.now(UTC)
    values = [
        {
            'telegram_id': telegram_id,
            'channel_id': channel_id,
            'is_member': is_member,
            'checked_at': now,
        }
        for channel_id, is_member in subs.items()
    ]
    stmt = pg_insert(UserChannelSubscription).values(values)
    stmt = stmt.on_conflict_do_update(
        constraint='uq_user_channel_sub',
        set_={
            'is_member': stmt.excluded.is_member,
            'checked_at': stmt.excluded.checked_at,
        },
    )
    await db.execute(stmt)
    await db.commit()
