"""Единая точка привязки пользователя к рекламной кампании.

Логика раньше жила только в кабинетном auth-флоу; гостевой покупке с лендинга
нужна ровно она же, поэтому она вынесена в сервис. Тесты пиннят правила,
которые нельзя потерять при переносе.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.campaign_service import AdvertisingCampaignService


def _db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _campaign(**kw: object) -> SimpleNamespace:
    base: dict[str, object] = {'id': 7, 'name': 'main', 'partner_user_id': None, 'bonus_type': 'balance'}
    base.update(kw)
    return SimpleNamespace(**base)


def _user(user_id: int = 42, referred_by_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, referred_by_id=referred_by_id)


@contextlib.asynccontextmanager
async def _fake_bot():
    """``_link_partner_referral`` открывает бота через ``async with create_bot()``."""
    yield SimpleNamespace()


@pytest.mark.asyncio
async def test_returns_none_for_empty_slug() -> None:
    service = AdvertisingCampaignService()
    assert await service.attribute_campaign(_db(), _user(), None) is None


@pytest.mark.asyncio
async def test_returns_none_when_campaign_not_found() -> None:
    service = AdvertisingCampaignService()
    with patch(
        'app.services.campaign_service.get_campaign_by_start_parameter',
        AsyncMock(return_value=None),
    ):
        assert await service.attribute_campaign(_db(), _user(), 'ghost') is None


@pytest.mark.asyncio
async def test_partner_cannot_be_attributed_to_own_campaign() -> None:
    """Иначе партнёр накрутит себе регистрацию по собственной ссылке."""
    service = AdvertisingCampaignService()
    with patch(
        'app.services.campaign_service.get_campaign_by_start_parameter',
        AsyncMock(return_value=_campaign(partner_user_id=42)),
    ):
        assert await service.attribute_campaign(_db(), _user(user_id=42), 'main') is None


@pytest.mark.asyncio
async def test_partner_own_campaign_stops_before_any_write() -> None:
    """Отказ обязан случиться ДО привязки реферала, а не только в бонусе.

    Возврат ``None`` сам по себе ничего не доказывает: бонус партнёру
    откажет и ``apply_campaign_bonus`` своей проверкой. Но по дороге туда
    ``_link_partner_referral`` успеет записать партнёру реферера-самого-себя.
    """
    service = AdvertisingCampaignService()
    user = _user(user_id=42)
    with (
        patch(
            'app.services.campaign_service.get_campaign_by_start_parameter',
            AsyncMock(return_value=_campaign(partner_user_id=42, is_active=True)),
        ),
        # Регистрации у партнёра нет — иначе отказ придёт от неё, и тест
        # снова перестанет проверять именно партнёрскую проверку.
        patch(
            'app.services.campaign_service.get_campaign_registration_by_user',
            AsyncMock(return_value=None),
        ),
        patch.object(AdvertisingCampaignService, '_link_partner_referral', AsyncMock()) as link_mock,
        patch.object(AdvertisingCampaignService, 'apply_campaign_bonus', AsyncMock()) as apply_mock,
    ):
        assert await service.attribute_campaign(_db(), user, 'main') is None

    link_mock.assert_not_awaited()
    apply_mock.assert_not_called()
    assert user.referred_by_id is None


@pytest.mark.asyncio
async def test_existing_registration_blocks_second_bonus() -> None:
    service = AdvertisingCampaignService()
    with (
        patch(
            'app.services.campaign_service.get_campaign_by_start_parameter',
            AsyncMock(return_value=_campaign()),
        ),
        patch(
            'app.services.campaign_service.get_campaign_registration_by_user',
            AsyncMock(return_value=SimpleNamespace(id=1)),
        ),
        patch.object(AdvertisingCampaignService, 'apply_campaign_bonus', AsyncMock()) as apply_mock,
    ):
        assert await service.attribute_campaign(_db(), _user(), 'main') is None
        apply_mock.assert_not_called()


@pytest.mark.asyncio
async def test_successful_attribution_applies_bonus() -> None:
    service = AdvertisingCampaignService()
    expected = SimpleNamespace(success=True, bonus_type='balance', is_new_registration=True)
    with (
        patch(
            'app.services.campaign_service.get_campaign_by_start_parameter',
            AsyncMock(return_value=_campaign()),
        ),
        patch(
            'app.services.campaign_service.get_campaign_registration_by_user',
            AsyncMock(return_value=None),
        ),
        patch.object(AdvertisingCampaignService, 'apply_campaign_bonus', AsyncMock(return_value=expected)),
    ):
        result = await service.attribute_campaign(_db(), _user(), 'main')

    assert result is expected


@pytest.mark.asyncio
async def test_unsuccessful_bonus_returns_none() -> None:
    service = AdvertisingCampaignService()
    with (
        patch(
            'app.services.campaign_service.get_campaign_by_start_parameter',
            AsyncMock(return_value=_campaign()),
        ),
        patch(
            'app.services.campaign_service.get_campaign_registration_by_user',
            AsyncMock(return_value=None),
        ),
        patch.object(
            AdvertisingCampaignService,
            'apply_campaign_bonus',
            AsyncMock(return_value=SimpleNamespace(success=False)),
        ),
    ):
        assert await service.attribute_campaign(_db(), _user(), 'main') is None


@pytest.mark.asyncio
async def test_partner_is_attached_as_referrer() -> None:
    """Кампания партнёра должна проставить его реферером — иначе он не
    получит комиссию за приведённого клиента."""
    service = AdvertisingCampaignService()
    user = _user(user_id=42, referred_by_id=None)
    with (
        patch(
            'app.services.campaign_service.get_campaign_by_start_parameter',
            AsyncMock(return_value=_campaign(partner_user_id=99)),
        ),
        patch(
            'app.services.campaign_service.get_campaign_registration_by_user',
            AsyncMock(return_value=None),
        ),
        patch.object(AdvertisingCampaignService, '_link_partner_referral', AsyncMock()) as link_mock,
        patch.object(
            AdvertisingCampaignService,
            'apply_campaign_bonus',
            AsyncMock(return_value=SimpleNamespace(success=True, bonus_type='balance')),
        ),
    ):
        await service.attribute_campaign(_db(), user, 'main')

    link_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_link_partner_referral_writes_the_referrer() -> None:
    """Сам факт вызова ничего не гарантирует — проверяем результат.

    Тест выше подменяет ``_link_partner_referral`` целиком, поэтому пустое
    тело метода он не заметит, а партнёр останется без комиссии.
    """
    db = _db()
    user = _user(user_id=42, referred_by_id=None)
    with (
        patch('app.bot_factory.create_bot', lambda *a, **kw: _fake_bot()),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()) as process_mock,
    ):
        await AdvertisingCampaignService()._link_partner_referral(db, user, _campaign(partner_user_id=99))

    assert user.referred_by_id == 99
    db.flush.assert_awaited()
    assert process_mock.await_args.args[1:] == (42, 99)


@pytest.mark.asyncio
async def test_existing_referrer_is_not_overwritten_by_campaign_partner() -> None:
    """Кто привёл первым, тот и получает комиссию — перебивать нельзя."""
    db = _db()
    user = _user(user_id=42, referred_by_id=7)
    with patch('app.services.referral_service.process_referral_registration', AsyncMock()) as process_mock:
        await AdvertisingCampaignService()._link_partner_referral(db, user, _campaign(partner_user_id=99))

    assert user.referred_by_id == 7
    process_mock.assert_not_called()


@pytest.mark.asyncio
async def test_errors_are_swallowed_and_rolled_back() -> None:
    """Привязка кампании — побочный эффект: она не имеет права уронить
    вызывающий флоу (регистрацию в кабинете или доставку подписки)."""
    service = AdvertisingCampaignService()
    db = _db()
    with patch(
        'app.services.campaign_service.get_campaign_by_start_parameter',
        AsyncMock(side_effect=RuntimeError('boom')),
    ):
        assert await service.attribute_campaign(db, _user(), 'main') is None

    db.rollback.assert_awaited()
