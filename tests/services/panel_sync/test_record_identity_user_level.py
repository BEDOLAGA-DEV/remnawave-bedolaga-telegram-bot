"""Синхронизация в мультитарифе записывает аккаунт и человеку, если у него ещё нет.

Иначе после возврата оператора в одиночный режим у человека «нет аккаунта»
(``users.remnawave_id`` пуст) — кабинет и покупки в одиночном режиме его не видят.
Перезаписывать уже записанный аккаунт нельзя: у человека в мультитарифе их несколько.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.panel_sync import writer


def _db() -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _subscription() -> SimpleNamespace:
    return SimpleNamespace(
        id=1, remnawave_id=None, remnawave_short_uuid=None, subscription_url=None, subscription_crypto_link=None
    )


PANEL_USER = SimpleNamespace(id=777, short_uuid='abc', subscription_url='https://sub.example/u', happ_crypto_link=None)


@pytest.mark.asyncio
async def test_multi_tariff_records_account_for_user_without_one():
    user = SimpleNamespace(remnawave_id=None)

    await writer._record_identity(_db(), user, _subscription(), PANEL_USER, multi_tariff=True)

    assert user.remnawave_id == 777


@pytest.mark.asyncio
async def test_multi_tariff_keeps_existing_user_account():
    user = SimpleNamespace(remnawave_id=100)

    await writer._record_identity(_db(), user, _subscription(), PANEL_USER, multi_tariff=True)

    assert user.remnawave_id == 100
