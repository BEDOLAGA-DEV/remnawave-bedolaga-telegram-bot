"""Отвязка OAuth-провайдера забывает email, который аккаунт получил только от него.

Жалоба 2026-09-14: привязка Google дописывала email в аккаунт (backfill), а отвязка
чистила только google_id — email «висел» без способа его убрать, и следующий вход
через Google находил аккаунт по этому email и привязывал Google обратно (цикл).
Если человек поставил пароль — email стал самостоятельным способом входа и остаётся.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.crud.user import clear_user_oauth_provider_id


def _user(**overrides) -> SimpleNamespace:
    base = dict(
        id=7,
        google_id='g-1',
        yandex_id=None,
        discord_id=None,
        vk_id=None,
        email='person@gmail.com',
        email_verified=True,
        email_verified_at=datetime(2026, 9, 1, tzinfo=UTC),
        email_verification_source='oauth_google',
        password_hash=None,
        email_verification_token=None,
        email_verification_expires=None,
        email_change_new='next@gmail.com',
        email_change_code='123456',
        email_change_expires=datetime(2026, 9, 2, tzinfo=UTC),
        password_reset_token='reset',
        password_reset_expires=datetime(2026, 9, 2, tzinfo=UTC),
        updated_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_unlink_forgets_the_email_that_only_this_provider_attested():
    user = _user()

    await clear_user_oauth_provider_id(AsyncMock(), user, 'google')

    assert user.google_id is None
    assert user.email is None and user.email_verified is False and user.email_verified_at is None
    assert user.email_verification_source is None
    assert user.email_change_new is None and user.email_change_code is None
    assert user.password_reset_token is None


@pytest.mark.asyncio
async def test_unlink_keeps_the_email_once_a_password_made_it_a_login():
    user = _user(password_hash='hash')

    await clear_user_oauth_provider_id(AsyncMock(), user, 'google')

    assert user.google_id is None
    assert user.email == 'person@gmail.com' and user.email_verified is True
    assert user.email_verification_source == 'oauth_google'


@pytest.mark.asyncio
async def test_unlink_keeps_an_email_verified_elsewhere():
    user = _user(email_verification_source='cabinet')

    await clear_user_oauth_provider_id(AsyncMock(), user, 'google')

    assert user.email == 'person@gmail.com' and user.email_verified is True


@pytest.mark.asyncio
async def test_unlinking_another_provider_leaves_the_email_alone():
    user = _user(yandex_id='y-1')

    await clear_user_oauth_provider_id(AsyncMock(), user, 'yandex')

    assert user.yandex_id is None and user.google_id == 'g-1'
    assert user.email == 'person@gmail.com'
