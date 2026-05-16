"""CRUD for `user_device_aliases` — local nicknames for HWID devices.

Aliases live ONLY in our DB. They are never pushed to RemnaWave panel.
Scope is per-(user, hwid) so the same physical device shares the alias
across all of a user's subscriptions in multi-tariff mode.
"""

from __future__ import annotations

import structlog
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import UserDeviceAlias


logger = structlog.get_logger(__name__)


# Hard cap matches the model column length. Enforced again at the API/handler
# boundary so we fail with a friendly message before hitting the DB.
ALIAS_MAX_LENGTH: int = 64


def normalize_alias(value: str | None) -> str:
    """Strip + collapse whitespace + cap length. Returns '' for empty/None input."""
    if value is None:
        return ''
    # Collapse all whitespace runs to a single space — pasted line breaks etc.
    collapsed = ' '.join(value.split())
    return collapsed[:ALIAS_MAX_LENGTH]


async def get_aliases_for_user(db: AsyncSession, user_id: int) -> dict[str, str]:
    """Return all device aliases for a user as a {hwid: alias} dict."""
    result = await db.execute(
        select(UserDeviceAlias.hwid, UserDeviceAlias.alias).where(UserDeviceAlias.user_id == user_id)
    )
    return {row.hwid: row.alias for row in result.all()}


async def get_alias(db: AsyncSession, user_id: int, hwid: str) -> str | None:
    """Return a single alias or None when not set."""
    result = await db.execute(
        select(UserDeviceAlias.alias).where(
            UserDeviceAlias.user_id == user_id,
            UserDeviceAlias.hwid == hwid,
        )
    )
    return result.scalar_one_or_none()


async def set_alias(
    db: AsyncSession,
    user_id: int,
    hwid: str,
    alias: str,
    *,
    commit: bool = True,
) -> str:
    """Insert or update an alias.

    Requires a NON-EMPTY `alias` — caller decides explicitly between
    set and delete (use `delete_alias` to clear). This split avoids the
    older `upsert_alias("")` footgun where empty input silently deleted
    the row, which surprised reviewers.

    `commit=True` commits the session (default — bot handlers expect this).
    `commit=False` defers the commit to the caller, useful when the call
    is part of a larger unit of work (e.g. FastAPI route session
    middleware that controls atomicity).

    Returns the alias string actually persisted (post-normalization).
    """
    normalized = normalize_alias(alias)
    if not normalized:
        raise ValueError('set_alias requires a non-empty alias — use delete_alias() to clear')
    if not hwid:
        raise ValueError('hwid is required')

    # NB: column-level `onupdate=func.now()` is ORM-only and does NOT fire on
    # Core pg_insert.on_conflict_do_update's set_={}. Touch updated_at
    # explicitly so audit/sort-by-recent queries see a fresh timestamp.
    stmt = (
        pg_insert(UserDeviceAlias)
        .values(user_id=user_id, hwid=hwid, alias=normalized)
        .on_conflict_do_update(
            index_elements=['user_id', 'hwid'],
            set_={'alias': normalized, 'updated_at': func.now()},
        )
    )
    await db.execute(stmt)
    if commit:
        await db.commit()
    logger.info('Device alias upserted', user_id=user_id, hwid_prefix=hwid[:8], alias_length=len(normalized))
    return normalized


# Backwards-compat alias used by the FSM bot handler. New code should call
# either `set_alias()` (explicit set) or `delete_alias()` (explicit clear).
async def upsert_alias(
    db: AsyncSession,
    user_id: int,
    hwid: str,
    alias: str,
    *,
    commit: bool = True,
) -> str:
    """Deprecated convenience wrapper: empty `alias` deletes the row.

    Kept for the bot's text-handler convenience (one entry-point that takes
    raw user input). API/admin code should prefer `set_alias` / `delete_alias`
    for clearer intent.
    """
    normalized = normalize_alias(alias)
    if not normalized:
        await delete_alias(db, user_id, hwid, commit=commit)
        return ''
    return await set_alias(db, user_id, hwid, normalized, commit=commit)


async def delete_alias(
    db: AsyncSession,
    user_id: int,
    hwid: str,
    *,
    commit: bool = True,
) -> bool:
    """Remove the alias for a (user, hwid) pair.

    Single-statement DELETE with `RETURNING` so we avoid the older
    SELECT-then-DELETE round-trip. Returns True if a row was removed.

    Commits unconditionally when `commit=True` (default) — even when no
    row matched. Otherwise an implicit empty transaction stays open
    until the session closes, which can pin a server connection under
    pgbouncer transaction-mode. Empty-commit is a cheap no-op on
    Postgres directly.
    """
    stmt = (
        sa_delete(UserDeviceAlias)
        .where(UserDeviceAlias.user_id == user_id, UserDeviceAlias.hwid == hwid)
        .returning(UserDeviceAlias.id)
    )
    result = await db.execute(stmt)
    deleted = result.scalar_one_or_none() is not None
    if commit:
        await db.commit()
    return deleted


def attach_aliases_to_devices(devices: list[dict], aliases: dict[str, str]) -> list[dict]:
    """Mutate-and-return: enrich each device dict with a `local_name` field.

    Contract: mutates the input list in place AND returns it. Callers can
    chain (`result = attach_aliases_to_devices(...)`) or rely on the
    in-place behaviour — both are intentionally supported because the
    function is called from both styles already (bot handler chains;
    cabinet endpoint mutates the response payload before serialization).

    `local_name` is `None` when the user hasn't set an alias — callers
    should fall back to a sensible default (platform / deviceModel).
    Empty-string aliases are also normalised to None so the frontend
    never renders a blank label.
    """
    for device in devices:
        hwid = device.get('hwid') or ''
        device['local_name'] = aliases.get(hwid) or None
    return devices
