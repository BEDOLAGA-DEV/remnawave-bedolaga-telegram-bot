"""Привязка соцсети (Google/Yandex/Discord/VK), которая уже держит другой аккаунт.

#3263: раньше это был отказ 409 «сначала отвяжите соцсеть от того аккаунта», а
отвязать её там нельзя — она единственный способ входа («Cannot unlink last
authentication method»). Тупик: люди заводили дубли и платили повторно.

Теперь конфликт предлагает слияние, как у Telegram: токен ведёт на страницу
/merge с явным подтверждением. Сама привязка при этом не происходит и ничего не
коммитится. Привязка ДРУГОЙ личности поверх уже занятого слота по-прежнему
отклоняется — слияние не перенесло бы её, и вход потерялся бы.
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


async def _run(
    user: SimpleNamespace,
    provider_id: str,
    existing_owner: SimpleNamespace | None,
    *,
    expect_error: bool = True,
):
    db = AsyncMock()
    create_token = AsyncMock(return_value='m' * 40)
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
        s.enter_context(patch('app.cabinet.routes.account_linking.create_merge_token', create_token))
        set_id = AsyncMock()
        s.enter_context(patch('app.cabinet.routes.account_linking.set_user_oauth_provider_id', set_id))
        kwargs = {
            'db': db,
            'user': user,
            'provider': 'google',
            'code': 'code',
            'state': 'state',
            'state_data': {},
            'device_id': None,
            'log_context': 'test',
        }
        if not expect_error:
            return await _exchange_and_link_oauth(**kwargs), set_id, db, create_token
        with pytest.raises(HTTPException) as exc:
            await _exchange_and_link_oauth(**kwargs)
        return exc.value, set_id, db, create_token


@pytest.mark.asyncio
async def test_identity_on_another_account_offers_merge_instead_of_dead_end() -> None:
    """Соцсеть на аккаунте #2 -> токен слияния #1 <- #2; сама привязка не делается."""
    user = SimpleNamespace(id=1, google_id=None)
    other_account = SimpleNamespace(id=2, google_id='G2')

    result, set_id, db, create_token = await _run(
        user, provider_id='G2', existing_owner=other_account, expect_error=False
    )

    assert result.success is False
    assert result.merge_required is True
    assert result.merge_token == 'm' * 40
    create_token.assert_awaited_once_with(
        primary_user_id=1,
        secondary_user_id=2,
        provider='google',
        provider_id='G2',
    )
    set_id.assert_not_awaited()  # переносит только подтверждённое слияние
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_relinking_over_occupied_slot_is_refused_not_overwritten() -> None:
    """User already has a *different* Google linked -> 409, old one preserved."""
    user = SimpleNamespace(id=1, google_id='G1')

    err, set_id, db, create_token = await _run(user, provider_id='G2', existing_owner=None)

    assert err.status_code == status.HTTP_409_CONFLICT
    assert 'already linked to your account' in str(err.detail).lower()
    set_id.assert_not_awaited()  # old G1 not overwritten
    db.commit.assert_not_awaited()
    create_token.assert_not_awaited()


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
