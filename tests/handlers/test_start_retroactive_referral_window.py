"""Старый аккаунт не становится рефералом при обычном нажатии /start-ссылки."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import start
from app.services import referral_service


NOW = datetime(2026, 10, 20, 10, tzinfo=UTC)


class _AfterReferral(BaseException):
    """Останавливаем /start до загрузки меню, после проверяемой атрибуции."""


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.mark.parametrize(
    ('age', 'attached'),
    [
        (timedelta(seconds=30), True),
        (timedelta(minutes=10), True),
        (timedelta(minutes=10, seconds=1), False),
        (timedelta(days=200), False),
        (timedelta(seconds=-1), False),
        (None, False),
    ],
)
async def test_start_referral_respects_account_age(monkeypatch, age, attached):
    user = SimpleNamespace(
        id=42,
        telegram_id=42,
        created_at=NOW - age if age is not None else None,
        referred_by_id=None,
        status='active',
        language='ru',
    )
    message = SimpleNamespace(
        text='/start ref_partner', from_user=SimpleNamespace(id=42, username='test'), bot=object()
    )
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock())
    monkeypatch.setattr(start, 'datetime', _Clock)
    monkeypatch.setattr(start, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr(
        start,
        '_evaluate_telegram_registration_access',
        AsyncMock(return_value=SimpleNamespace(allowed=True, reason=None)),
    )
    monkeypatch.setattr(start, 'get_campaign_by_start_parameter', AsyncMock(return_value=None))
    monkeypatch.setattr(
        start, 'get_user_by_referral_code', AsyncMock(return_value=SimpleNamespace(id=77, telegram_id=77))
    )
    monkeypatch.setattr(start, 'find_phantom_user_by_username', AsyncMock(side_effect=_AfterReferral))
    attach = AsyncMock()
    monkeypatch.setattr(referral_service, 'attach_referrer_if_missing', attach)

    with pytest.raises(_AfterReferral):
        await start.cmd_start(message, state, AsyncMock(), db_user=user)

    assert attach.await_count == int(attached)
    if attached:
        assert attach.await_args.kwargs['referral_code'] == 'ref_partner'
        assert attach.await_args.kwargs['source'] == 'bot_start_retroactive'


async def test_new_user_still_saves_pending_referral(monkeypatch):
    message = SimpleNamespace(
        text='/start ref_partner', from_user=SimpleNamespace(id=42, username='test'), bot=object()
    )
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock())
    monkeypatch.setattr(start, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr(
        start,
        '_evaluate_telegram_registration_access',
        AsyncMock(return_value=SimpleNamespace(allowed=True, reason=None)),
    )
    monkeypatch.setattr(start, 'get_campaign_by_start_parameter', AsyncMock(return_value=None))
    monkeypatch.setattr(start, 'get_user_by_telegram_id', AsyncMock(side_effect=[None, _AfterReferral]))
    monkeypatch.setattr(
        start, 'get_user_by_referral_code', AsyncMock(return_value=SimpleNamespace(id=77, telegram_id=77))
    )
    save = AsyncMock()
    monkeypatch.setattr(start, 'save_pending_referral', save)
    with pytest.raises(_AfterReferral):
        await start.cmd_start(message, state, AsyncMock())
    save.assert_awaited_once_with(42, 'ref_partner', 77)
