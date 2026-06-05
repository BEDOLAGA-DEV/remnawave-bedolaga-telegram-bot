"""Unit coverage for `user_device_aliases` CRUD helpers.

Covers the normalization rules, alias merge into RemnaWave device dicts,
length-cap behaviour, and the `set_alias` / `upsert_alias` contract
(empty input on `set_alias` raises; legacy `upsert_alias` redirects to
delete). A full DB round-trip is intentionally out of scope here: the
CRUD uses Postgres-specific `pg_insert.on_conflict_do_update`, and the
project does not yet have a testcontainer fixture. The query string
shape is asserted via `db.execute` mocking below, which catches the
`updated_at = func.now()` regression that the code-review flagged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud.user_device_alias import (
    ALIAS_MAX_LENGTH,
    attach_aliases_to_devices,
    normalize_alias,
    set_alias,
    upsert_alias,
)


# ---------------------------------------------------------------------------
# normalize_alias
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        (None, ''),
        ('', ''),
        ('   ', ''),
        ('iPhone Жены', 'iPhone Жены'),
        # Inner-whitespace runs collapse to a single space — pasted line breaks etc.
        ('iPhone  \n\t  Жены', 'iPhone Жены'),
        ('  trim me  ', 'trim me'),
    ],
)
def test_normalize_alias_basic(raw: str | None, expected: str) -> None:
    assert normalize_alias(raw) == expected


def test_normalize_alias_caps_at_max_length() -> None:
    huge = 'A' * (ALIAS_MAX_LENGTH + 100)

    result = normalize_alias(huge)

    assert len(result) == ALIAS_MAX_LENGTH
    assert result == 'A' * ALIAS_MAX_LENGTH


def test_normalize_alias_preserves_unicode() -> None:
    # Cyrillic + emoji + dash — common real-world aliases.
    raw = '🏠 Домашний MacBook —    Pro'

    result = normalize_alias(raw)

    assert result == '🏠 Домашний MacBook — Pro'


# ---------------------------------------------------------------------------
# attach_aliases_to_devices
# ---------------------------------------------------------------------------


def test_attach_aliases_to_devices_sets_local_name_when_match() -> None:
    devices = [
        {'hwid': 'ABC123', 'platform': 'iOS', 'deviceModel': 'iPhone15,2'},
        {'hwid': 'DEF456', 'platform': 'Android', 'deviceModel': 'SM-S908U'},
    ]
    aliases = {'ABC123': 'Жены iPhone'}

    result = attach_aliases_to_devices(devices, aliases)

    assert result[0]['local_name'] == 'Жены iPhone'
    # No alias for DEF456 → explicit None so callers can fall back uniformly.
    assert result[1]['local_name'] is None


def test_attach_aliases_to_devices_handles_empty_aliases() -> None:
    devices = [{'hwid': 'X', 'platform': 'Win'}]

    result = attach_aliases_to_devices(devices, {})

    assert result[0]['local_name'] is None


def test_attach_aliases_to_devices_handles_missing_hwid() -> None:
    """Device without hwid key — alias merge must not crash, just yield None."""
    devices = [{'platform': 'Linux'}]

    result = attach_aliases_to_devices(devices, {'whatever': 'X'})

    assert result[0]['local_name'] is None


def test_attach_aliases_to_devices_is_in_place_mutation() -> None:
    """The helper mutates each dict for cheap downstream rendering."""
    devices = [{'hwid': 'A', 'platform': 'iOS'}]

    result = attach_aliases_to_devices(devices, {'A': 'Mine'})

    assert result is devices  # same list
    assert devices[0]['local_name'] == 'Mine'


def test_attach_aliases_empty_alias_string_falls_back_to_none() -> None:
    """`''` in the alias dict is treated as 'not set' so renderers fall back."""
    devices = [{'hwid': 'A', 'platform': 'iOS'}]

    result = attach_aliases_to_devices(devices, {'A': ''})

    # Empty alias → None (caller can do `device.local_name or device.device_model`).
    assert result[0]['local_name'] is None


# ---------------------------------------------------------------------------
# set_alias / upsert_alias semantic contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_alias_rejects_empty_input() -> None:
    """set_alias is the explicit setter — empty input must raise, not silently delete.

    Regression cover for the original `upsert_alias("")`-as-delete footgun
    flagged in code review.
    """
    db = MagicMock()

    with pytest.raises(ValueError, match='non-empty alias'):
        await set_alias(db, user_id=1, hwid='HWID', alias='')

    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_set_alias_executes_on_conflict_update_touching_updated_at() -> None:
    """The compiled statement must update both `alias` AND `updated_at`.

    Regression cover for the SQLAlchemy `onupdate=func.now()` not firing on
    Core `pg_insert.on_conflict_do_update` issue. Without explicitly touching
    `updated_at` in the `set_` dict, audit/sort-by-recent would lie.

    Sharper than a plain `'updated_at' in compiled` check — that would
    also match a RETURNING clause or column list and silently miss the
    regression. We scope the search to the `DO UPDATE SET …` window.
    """
    db = AsyncMock()

    await set_alias(db, user_id=42, hwid='HWID', alias='Жены iPhone')

    assert db.execute.await_count == 1
    stmt = db.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={'literal_binds': False})).lower()
    # PostgreSQL ON CONFLICT clause must update both columns. Isolate the
    # SET window so the assertion catches a regression where `updated_at`
    # only appears in (say) the INSERT column list, not the UPDATE branch.
    assert 'on conflict' in compiled
    do_update_idx = compiled.index('do update set')
    set_clause = compiled[do_update_idx : do_update_idx + 200]
    assert 'alias' in set_clause
    assert 'updated_at' in set_clause


@pytest.mark.asyncio
async def test_set_alias_with_commit_false_does_not_commit() -> None:
    """commit=False defers commit to caller (cabinet route session middleware)."""
    db = AsyncMock()

    await set_alias(db, user_id=1, hwid='HWID', alias='Test', commit=False)

    db.execute.assert_awaited_once()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_set_alias_with_commit_true_does_commit() -> None:
    """commit=True (default) commits — used by bot FSM handler that has no session middleware."""
    db = AsyncMock()

    await set_alias(db, user_id=1, hwid='HWID', alias='Test')

    db.execute.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_alias_with_empty_input_calls_delete() -> None:
    """Legacy upsert wrapper: empty/whitespace input → delete_alias path."""
    db = AsyncMock()
    # `delete_alias` does `await db.execute(...)` then `.scalar_one_or_none()`
    # on the AWAITED result. Use a sync MagicMock for the result so the
    # `.scalar_one_or_none()` call returns synchronously.
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    result = await upsert_alias(db, user_id=1, hwid='HWID', alias='   ')

    assert result == ''
    db.execute.assert_awaited_once()
    # `delete_alias` commits unconditionally on `commit=True` (default) — an
    # empty no-op commit is cheaper than leaving an implicit txn open under
    # pgbouncer transaction-mode.
    db.commit.assert_awaited_once()
