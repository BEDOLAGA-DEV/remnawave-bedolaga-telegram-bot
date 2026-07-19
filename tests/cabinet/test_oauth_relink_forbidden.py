"""Security: linking a social login (Google/Yandex/Discord/VK) must never
silently move or merge accounts.

Previously, completing OAuth for a provider identity that was already attached
to another account returned ``merge_required`` and walked the user into an
account MERGE — a surprising, too-easy way to absorb/relink accounts. It must
instead be refused: a social identity belongs to exactly ONE account, and the
owner has to unlink it from the other account first. Linking a *different*
identity over an already-occupied slot must likewise be refused rather than
silently overwriting (orphaning) the previous login.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.account_linking import _exchange_and_link_oauth


def _provider_returning(provider_id: str) -> MagicMock:
    """A fake OAuth provider that exchanges a code and yields ``provider_id``."""
    prov = MagicMock()
    prov.exchange_code = AsyncMock(return_value={'access_token': 'x'})
    prov.get_user_info = AsyncMock(
        return_value=SimpleNamespace(provider_id=provider_id, email=None, email_verified=False)
    )
    return prov


async def _run(user: SimpleNamespace, provider_id: str, existing_owner: SimpleNamespace | None):
    db = AsyncMock()
    with ExitStack() as s:
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.get_provider',
                MagicMock(return_value=_provider_returning(provider_id)),
            )
        )
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.get_user_by_oauth_provider',
                AsyncMock(return_value=existing_owner),
            )
        )
        set_id = AsyncMock()
        s.enter_context(patch('app.cabinet.routes.account_linking.set_user_oauth_provider_id', set_id))
        with pytest.raises(HTTPException) as exc:
            await _exchange_and_link_oauth(
                db=db,
                user=user,
                provider='google',
                code='code',
                state='state',
                state_data={},
                device_id=None,
                log_context='test',
            )
        return exc.value, set_id, db


@pytest.mark.asyncio
async def test_relink_to_another_account_is_refused_not_merged() -> None:
    """Provider identity already on account #2 -> 409, no link, no merge token."""
    user = SimpleNamespace(id=1, google_id=None)
    other_account = SimpleNamespace(id=2, google_id='G2')

    err, set_id, db = await _run(user, provider_id='G2', existing_owner=other_account)

    assert err.status_code == status.HTTP_409_CONFLICT
    assert 'different account' in str(err.detail).lower()
    set_id.assert_not_awaited()  # never linked
    db.commit.assert_not_awaited()  # never merged / committed


@pytest.mark.asyncio
async def test_relinking_over_occupied_slot_is_refused_not_overwritten() -> None:
    """User already has a *different* Google linked -> 409, old one preserved."""
    user = SimpleNamespace(id=1, google_id='G1')

    err, set_id, db = await _run(user, provider_id='G2', existing_owner=None)

    assert err.status_code == status.HTTP_409_CONFLICT
    assert 'already linked to your account' in str(err.detail).lower()
    set_id.assert_not_awaited()  # old G1 not overwritten
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_identity_is_idempotent_no_op() -> None:
    """Re-linking the identity already on this account is a harmless no-op."""
    user = SimpleNamespace(id=1, google_id='G1')
    db = AsyncMock()
    with ExitStack() as s:
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.get_provider',
                MagicMock(return_value=_provider_returning('G1')),
            )
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

    assert result.success is True
    assert result.message == 'already_linked'
    set_id.assert_not_awaited()
    db.commit.assert_not_awaited()
