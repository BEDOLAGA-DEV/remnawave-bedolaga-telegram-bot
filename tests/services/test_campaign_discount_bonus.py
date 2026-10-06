"""Скидка как бонус рекламной кампании.

Переход по ссылке выдаёт ту же персональную скидку, что промопредложения и
промокоды-скидки: процент в ``users.promo_offer_discount_*`` и необязательный
срок. Тесты пиннят правила выдачи: лучшую действующую скидку не перебиваем,
повторная регистрация ничего не выдаёт.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.database.models import AdvertisingCampaign
from app.handlers.start import _apply_campaign_bonus_if_needed
from app.localization.texts import get_texts
from app.services.campaign_service import AdvertisingCampaignService, CampaignBonusResult


def _db() -> AsyncMock:
    db = AsyncMock()
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    return db


def _campaign(**kw: object) -> SimpleNamespace:
    base: dict[str, object] = {
        'id': 7,
        'name': 'curly',
        'bonus_type': 'discount',
        'discount_percent': 20,
        'discount_duration_hours': 48,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _user(percent: int = 0, expires_at: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        telegram_id=None,
        email=None,
        promo_offer_discount_percent=percent,
        promo_offer_discount_source='personal_offer' if percent else None,
        promo_offer_discount_expires_at=expires_at,
    )


async def _apply(user: SimpleNamespace, campaign: SimpleNamespace, *, created: bool = True):
    record = AsyncMock(return_value=(SimpleNamespace(), created))
    with patch('app.services.campaign_service.record_campaign_registration', record):
        result = await AdvertisingCampaignService()._apply_discount_bonus(_db(), user, campaign)
    return result, record


@pytest.mark.asyncio
async def test_discount_is_granted_with_expiry() -> None:
    user = _user()
    result, record = await _apply(user, _campaign())

    assert result.success and result.discount_percent == 20
    assert user.promo_offer_discount_percent == 20
    assert user.promo_offer_discount_source == 'campaign:7'
    left = user.promo_offer_discount_expires_at - datetime.now(UTC)
    assert timedelta(hours=47) < left <= timedelta(hours=48)
    assert result.discount_expires_at == user.promo_offer_discount_expires_at
    assert record.await_args.kwargs['bonus_type'] == 'discount'
    assert record.await_args.kwargs['discount_percent'] == 20


@pytest.mark.asyncio
async def test_zero_hours_means_until_first_purchase() -> None:
    user = _user()
    result, _ = await _apply(user, _campaign(discount_duration_hours=0))

    assert user.promo_offer_discount_percent == 20
    assert user.promo_offer_discount_expires_at is None
    assert result.discount_expires_at is None


@pytest.mark.asyncio
async def test_better_active_discount_is_kept() -> None:
    """Промопредложение на 50 % не сгорает из-за перехода по ссылке на 20 %."""
    expires = datetime.now(UTC) + timedelta(days=1)
    user = _user(percent=50, expires_at=expires)
    result, record = await _apply(user, _campaign())

    assert result.success and result.discount_percent is None
    assert user.promo_offer_discount_percent == 50
    assert user.promo_offer_discount_source == 'personal_offer'
    assert user.promo_offer_discount_expires_at == expires
    # Регистрация по кампании всё равно пишется — без выданной скидки
    assert record.await_args.kwargs['discount_percent'] is None


@pytest.mark.asyncio
async def test_expired_discount_is_replaced() -> None:
    user = _user(percent=50, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    result, _ = await _apply(user, _campaign())

    assert result.discount_percent == 20
    assert user.promo_offer_discount_percent == 20


@pytest.mark.asyncio
async def test_repeat_registration_grants_nothing() -> None:
    """Повторный /start после траты скидки не должен выдать её заново."""
    user = _user()
    result, _ = await _apply(user, _campaign(), created=False)

    assert result.success and result.discount_percent is None
    assert user.promo_offer_discount_percent == 0


@pytest.mark.asyncio
async def test_campaign_without_percent_fails() -> None:
    result, record = await _apply(_user(), _campaign(discount_percent=None))

    assert not result.success
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_discount_campaign_is_dispatched() -> None:
    campaign = AdvertisingCampaign(
        id=7, name='curly', start_parameter='curly', bonus_type='discount', discount_percent=20, is_active=True
    )
    expected = CampaignBonusResult(success=True, bonus_type='discount', discount_percent=20)
    service = AdvertisingCampaignService()
    with patch.object(service, '_apply_discount_bonus', AsyncMock(return_value=expected)) as handler:
        result = await service.apply_campaign_bonus(_db(), _user(), campaign)

    handler.assert_awaited_once()
    assert result is expected


@pytest.mark.asyncio
async def test_start_message_names_discount_and_term() -> None:
    campaign = SimpleNamespace(id=7, name='curly', is_active=True)
    result = CampaignBonusResult(success=True, bonus_type='discount', discount_percent=20)
    with (
        patch('app.handlers.start.get_campaign_by_id', AsyncMock(return_value=campaign)),
        patch.object(AdvertisingCampaignService, 'apply_campaign_bonus', AsyncMock(return_value=result)),
    ):
        text = await _apply_campaign_bonus_if_needed(_db(), _user(), {'campaign_id': 7}, get_texts('ru'))

    assert '20%' in text
    assert 'curly' in text
    assert 'до первой покупки' in text


@pytest.mark.asyncio
async def test_start_message_is_silent_when_nothing_granted() -> None:
    campaign = SimpleNamespace(id=7, name='curly', is_active=True)
    result = CampaignBonusResult(success=True, bonus_type='discount')
    with (
        patch('app.handlers.start.get_campaign_by_id', AsyncMock(return_value=campaign)),
        patch.object(AdvertisingCampaignService, 'apply_campaign_bonus', AsyncMock(return_value=result)),
    ):
        text = await _apply_campaign_bonus_if_needed(_db(), _user(), {'campaign_id': 7}, get_texts('ru'))

    assert text is None
