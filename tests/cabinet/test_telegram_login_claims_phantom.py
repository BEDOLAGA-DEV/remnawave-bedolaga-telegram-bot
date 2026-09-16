"""Кабинетный вход по Telegram забирает фантома с лендинга, а не заводит второй аккаунт.

Сценарий с прода: покупка на лендинге по @username создала фантома (без telegram_id,
подписка на нём), клиент нажал /start в боте и посреди регистрации открыл кабинет —
кабинет завёл нового пользователя, бот увидел «уже активен» и свой claim пропустил.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import auth as cabinet_auth
from app.services import phantom_service


def _phantom() -> SimpleNamespace:
    return SimpleNamespace(id=707, telegram_id=None, username='bax_c4o', auth_type='telegram')


async def _call(**overrides):
    kwargs = {
        'telegram_id': 40985293,
        'username': 'bax_c4o',
        'first_name': 'BaX',
        'last_name': None,
        'language': 'ru',
        'referred_by_id': None,
        'source': 'cabinet_telegram',
    }
    kwargs.update(overrides)
    return await cabinet_auth._create_or_claim_telegram_user(object(), **kwargs)


@pytest.mark.asyncio
async def test_phantom_is_claimed_instead_of_creating_a_second_account(monkeypatch):
    phantom = _phantom()
    claimed = SimpleNamespace(id=707, telegram_id=40985293)
    find = AsyncMock(return_value=phantom)
    claim = AsyncMock(return_value=(True, claimed))
    create = AsyncMock()
    monkeypatch.setattr(cabinet_auth, 'find_phantom_user_by_username', find)
    monkeypatch.setattr(phantom_service, 'claim_phantom', claim)
    monkeypatch.setattr(cabinet_auth, 'create_user', create)

    user = await _call(referred_by_id=5)

    assert user is claimed
    create.assert_not_awaited()
    find.assert_awaited_once()
    assert find.await_args.args[1] == 'bax_c4o'
    claim.assert_awaited_once()
    assert claim.await_args.args[1] is phantom
    assert claim.await_args.kwargs == {
        'telegram_id': 40985293,
        'username': 'bax_c4o',
        'first_name': 'BaX',
        'last_name': None,
        'language': 'ru',
        'referrer_id': 5,
    }


@pytest.mark.asyncio
async def test_without_phantom_user_is_created_as_before(monkeypatch):
    created = SimpleNamespace(id=708, telegram_id=40985293)
    create = AsyncMock(return_value=created)
    claim = AsyncMock()
    monkeypatch.setattr(cabinet_auth, 'find_phantom_user_by_username', AsyncMock(return_value=None))
    monkeypatch.setattr(phantom_service, 'claim_phantom', claim)
    monkeypatch.setattr(cabinet_auth, 'create_user', create)

    user = await _call(language=None)

    assert user is created
    claim.assert_not_awaited()
    create.assert_awaited_once()
    assert create.await_args.kwargs['telegram_id'] == 40985293
    assert create.await_args.kwargs['username'] == 'bax_c4o'
    # язык уходит как пришёл — нормализует сам create_user
    assert create.await_args.kwargs['language'] is None


@pytest.mark.asyncio
async def test_without_username_phantom_is_not_even_looked_up(monkeypatch):
    created = SimpleNamespace(id=709, telegram_id=1)
    find = AsyncMock()
    monkeypatch.setattr(cabinet_auth, 'find_phantom_user_by_username', find)
    monkeypatch.setattr(cabinet_auth, 'create_user', AsyncMock(return_value=created))

    user = await _call(telegram_id=1, username=None)

    assert user is created
    find.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_claim_race_returns_the_existing_user(monkeypatch):
    """claim_phantom вернул (False, existing): telegram_id уже завёл бот — берём его запись."""
    existing = SimpleNamespace(id=708, telegram_id=40985293)
    create = AsyncMock()
    monkeypatch.setattr(cabinet_auth, 'find_phantom_user_by_username', AsyncMock(return_value=_phantom()))
    monkeypatch.setattr(phantom_service, 'claim_phantom', AsyncMock(return_value=(False, existing)))
    monkeypatch.setattr(cabinet_auth, 'create_user', create)

    user = await _call()

    assert user is existing
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_claim_without_fallback_falls_through_to_create(monkeypatch):
    created = SimpleNamespace(id=710, telegram_id=40985293)
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(cabinet_auth, 'find_phantom_user_by_username', AsyncMock(return_value=_phantom()))
    monkeypatch.setattr(phantom_service, 'claim_phantom', AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(cabinet_auth, 'create_user', create)

    user = await _call()

    assert user is created
    create.assert_awaited_once()
