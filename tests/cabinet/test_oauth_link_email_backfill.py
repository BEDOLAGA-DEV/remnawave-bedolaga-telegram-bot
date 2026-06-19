"""Symptom 3 regression: linking a Google/Yandex account to a Telegram-first
(or any no-email) user must backfill the provider's verified email onto the
account, so the card shows the email and email-based features work.

It must NOT overwrite an existing email, and must NOT adopt an address already
owned by another account (that requires the explicit merge flow) — while still
linking the social login successfully.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cabinet.routes.account_linking import _exchange_and_link_oauth


def _provider(email: str | None, email_verified: bool, provider_id: str = 'G1') -> MagicMock:
    prov = MagicMock()
    prov.exchange_code = AsyncMock(return_value={'access_token': 'x'})
    prov.get_user_info = AsyncMock(
        return_value=SimpleNamespace(provider_id=provider_id, email=email, email_verified=email_verified)
    )
    return prov


async def _run_link(user, *, email, email_verified, email_owner=None):
    db = AsyncMock()
    with ExitStack() as s:
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.get_provider',
                MagicMock(return_value=_provider(email, email_verified)),
            )
        )
        s.enter_context(
            patch('app.cabinet.routes.account_linking.get_user_by_oauth_provider', AsyncMock(return_value=None))
        )
        s.enter_context(
            patch('app.cabinet.routes.account_linking.get_user_by_email', AsyncMock(return_value=email_owner))
        )
        set_id = AsyncMock()
        s.enter_context(patch('app.cabinet.routes.account_linking.set_user_oauth_provider_id', set_id))
        result = await _exchange_and_link_oauth(
            db=db,
            user=user,
            provider='google',
            code='code',
            state='state',
            state_data={},
            device_id=None,
            log_context='test',
        )
        return result, set_id, db


@pytest.mark.asyncio
async def test_backfills_verified_email_when_user_has_none() -> None:
    user = SimpleNamespace(id=1, google_id=None, email=None, email_verified=False)

    result, set_id, db = await _run_link(user, email='Foo.Bar@Gmail.com', email_verified=True)

    assert result.success is True
    assert user.email == 'foo.bar@gmail.com'  # adopted and normalized to lowercase
    assert user.email_verified is True
    assert user.email_verification_source == 'oauth_google'
    set_id.assert_awaited_once()  # the social login is still linked
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_does_not_overwrite_existing_email() -> None:
    user = SimpleNamespace(id=1, google_id=None, email='mine@example.com', email_verified=True)

    result, set_id, _ = await _run_link(user, email='other@gmail.com', email_verified=True)

    assert result.success is True
    assert user.email == 'mine@example.com'  # untouched
    set_id.assert_awaited_once()


@pytest.mark.asyncio
async def test_skips_backfill_when_email_owned_by_another_account_but_still_links() -> None:
    user = SimpleNamespace(id=1, google_id=None, email=None, email_verified=False)
    other = SimpleNamespace(id=2)

    result, set_id, db = await _run_link(user, email='taken@gmail.com', email_verified=True, email_owner=other)

    assert result.success is True
    assert user.email is None  # not stolen from the other account
    set_id.assert_awaited_once()  # linking the provider still succeeds
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_does_not_backfill_unverified_provider_email() -> None:
    user = SimpleNamespace(id=1, google_id=None, email=None, email_verified=False)

    result, _set_id, _ = await _run_link(user, email='unverified@gmail.com', email_verified=False)

    assert result.success is True
    assert user.email is None  # never attach an unverified address
