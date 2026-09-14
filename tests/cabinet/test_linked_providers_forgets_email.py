"""Список способов входа предупреждает, какой email уйдёт вместе с отвязкой провайдера.

Правило одно с самой отвязкой (``provider_attested_email``): email получен от этого
провайдера и не стал логином (пароля нет). Кабинет показывает это под кнопкой
«Точно отвязать?», чтобы человек не удивился пропавшему адресу.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import account_linking as route


def _user(**overrides) -> SimpleNamespace:
    base = dict(
        id=7,
        telegram_id=1001,
        google_id='g-1',
        yandex_id=None,
        discord_id=None,
        vk_id=None,
        email='person@gmail.com',
        email_verified=True,
        email_verification_source='oauth_google',
        password_hash=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _providers(monkeypatch, user):
    monkeypatch.setattr(route, '_get_active_providers', AsyncMock(return_value=['telegram', 'email', 'google']))
    response = await route.get_linked_providers(user=user, db=AsyncMock())
    return {item.provider: item for item in response.providers}


@pytest.mark.asyncio
async def test_google_row_names_the_email_that_unlinking_forgets(monkeypatch):
    rows = await _providers(monkeypatch, _user())

    assert rows['google'].linked and rows['google'].forgets_email == 'person@gmail.com'
    assert rows['telegram'].forgets_email is None
    assert rows['email'].linked is False, 'email без пароля — не способ входа'


@pytest.mark.asyncio
async def test_nothing_is_forgotten_once_a_password_exists(monkeypatch):
    rows = await _providers(monkeypatch, _user(password_hash='hash'))

    assert rows['google'].forgets_email is None
    assert rows['email'].linked is True


@pytest.mark.asyncio
async def test_email_verified_elsewhere_is_not_tied_to_the_provider(monkeypatch):
    rows = await _providers(monkeypatch, _user(email_verification_source='cabinet'))

    assert rows['google'].forgets_email is None
