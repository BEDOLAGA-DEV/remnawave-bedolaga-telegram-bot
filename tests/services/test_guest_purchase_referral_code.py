"""Regression tests for `_find_or_create_user` referral_code population.

Background — the bug (Telegram report #596693)
----------------------------------------------
When a user buys a subscription via a landing page, the bot autoregisters
them through `_find_or_create_user`. Previously, that path built the
`User(...)` row directly (bypassing `app.database.crud.user.create_user`)
and never populated `referral_code`. Result: landing-purchased users had
no referral code → no referral link → could not invite anyone.

These tests pin the fix so the regression does not silently come back.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.guest_purchase_service import _find_or_create_user


def _empty_result() -> SimpleNamespace:
    """Mimic `db.execute(...).scalars().first() -> None`."""
    return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))


def _single_result(user: object) -> SimpleNamespace:
    """Mimic `db.execute(...).scalars().first() -> user`."""
    return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: user))


def _async_nested_ctx() -> MagicMock:
    """Async context manager for `db.begin_nested()`."""
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=None)
    return nested


@pytest.mark.asyncio
async def test_new_email_user_is_created_with_referral_code() -> None:
    """A landing-page email purchase must persist `referral_code` on the new
    `User` row, otherwise the user has no referral link (Telegram report #596693)."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_empty_result())
    db.begin_nested = MagicMock(return_value=_async_nested_ctx())
    db.flush = AsyncMock()
    # `db.add(user)` is sync on AsyncSession — keep it a regular MagicMock
    db.add = MagicMock()

    promo_group = SimpleNamespace(id=1)

    with (
        patch(
            'app.services.guest_purchase_service._get_or_create_default_promo_group',
            AsyncMock(return_value=promo_group),
        ),
        patch(
            'app.services.guest_purchase_service.create_unique_referral_code',
            AsyncMock(return_value='NEWREF01'),
        ),
    ):
        user, is_new = await _find_or_create_user(db, 'email', 'foo@example.com')

    assert is_new is True
    assert user.referral_code == 'NEWREF01', 'new guest email user must be persisted with a generated referral_code'
    # The user must have been added to the session, not just constructed and dropped.
    db.add.assert_called_once_with(user)


@pytest.mark.asyncio
async def test_new_telegram_user_is_created_with_referral_code() -> None:
    """Same guarantee for the telegram-username guest-purchase branch."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_empty_result())
    db.begin_nested = MagicMock(return_value=_async_nested_ctx())
    db.flush = AsyncMock()
    db.add = MagicMock()

    promo_group = SimpleNamespace(id=1)

    with (
        patch(
            'app.services.guest_purchase_service._get_or_create_default_promo_group',
            AsyncMock(return_value=promo_group),
        ),
        patch(
            'app.services.guest_purchase_service.create_unique_referral_code',
            AsyncMock(return_value='TGREF002'),
        ),
    ):
        # pre_resolved_telegram_id avoids the Bot API call branch.
        user, _is_new = await _find_or_create_user(db, 'telegram', 'valid_username', pre_resolved_telegram_id=12345)

    assert user.referral_code == 'TGREF002', 'new guest telegram user must be persisted with a generated referral_code'
    db.add.assert_called_once_with(user)


@pytest.mark.asyncio
async def test_existing_email_user_without_referral_code_is_backfilled() -> None:
    """Legacy users created before the fix (with referral_code=NULL) must be
    healed when they come back through the guest-purchase path."""
    legacy_user = SimpleNamespace(
        id=7,
        password_hash='already-set',
        email_verified=True,
        email_verified_at=object(),
        promo_group_id=1,
        referral_code=None,
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_single_result(legacy_user))

    with (
        patch(
            'app.services.guest_purchase_service.create_unique_referral_code',
            AsyncMock(return_value='HEALED01'),
        ),
    ):
        user, is_new = await _find_or_create_user(db, 'email', 'legacy@example.com')

    assert user is legacy_user
    assert is_new is False
    assert legacy_user.referral_code == 'HEALED01', (
        'legacy guest email user with NULL referral_code must be backfilled, not left broken'
    )


@pytest.mark.asyncio
async def test_existing_email_user_with_referral_code_is_not_overwritten() -> None:
    """Idempotency: if the user already has a referral_code, do not regenerate
    or overwrite it — that would break outstanding referral links."""
    existing_user = SimpleNamespace(
        id=9,
        password_hash='already-set',
        email_verified=True,
        email_verified_at=object(),
        promo_group_id=1,
        referral_code='ORIGINAL',
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_single_result(existing_user))

    create_code_mock = AsyncMock(return_value='SHOULD-NOT-BE-USED')
    with patch('app.services.guest_purchase_service.create_unique_referral_code', create_code_mock):
        user, _ = await _find_or_create_user(db, 'email', 'existing@example.com')

    assert user.referral_code == 'ORIGINAL'
    create_code_mock.assert_not_called()
