"""Привязка гостевой покупки к рекламной кампании.

Покупатель приходит с рекламного лендинга и оформляет подписку гостем —
auth-флоу, в котором кампания привязывается обычно, здесь не срабатывает.
Слаг лежит в самой покупке (миграция 0106), а привязка выполняется после
доставки подписки.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.guest_purchase_service import _attribute_purchase_campaign


def _purchase(campaign_slug: str | None = 'main', is_gift: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=1, token='tok12345', campaign_slug=campaign_slug, is_gift=is_gift)


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=42)


@pytest.mark.asyncio
async def test_campaign_is_attributed_after_delivery() -> None:
    db = AsyncMock()
    with patch(
        'app.services.campaign_service.AdvertisingCampaignService.attribute_campaign',
        AsyncMock(return_value=SimpleNamespace(success=True, bonus_type='balance')),
    ) as attribute_mock:
        await _attribute_purchase_campaign(db, _purchase(), _user())

    attribute_mock.assert_awaited_once()
    assert attribute_mock.await_args.args[2] == 'main'


@pytest.mark.asyncio
async def test_purchase_without_slug_is_skipped() -> None:
    db = AsyncMock()
    with patch(
        'app.services.campaign_service.AdvertisingCampaignService.attribute_campaign',
        AsyncMock(),
    ) as attribute_mock:
        await _attribute_purchase_campaign(db, _purchase(campaign_slug=None), _user())

    attribute_mock.assert_not_called()


@pytest.mark.asyncio
async def test_gift_is_not_attributed_to_recipient() -> None:
    """Гифт активирует получатель, а по рекламе приходил покупатель — записать
    кампанию на получателя означало бы приписать ей чужого клиента."""
    db = AsyncMock()
    with patch(
        'app.services.campaign_service.AdvertisingCampaignService.attribute_campaign',
        AsyncMock(),
    ) as attribute_mock:
        await _attribute_purchase_campaign(db, _purchase(is_gift=True), _user())

    attribute_mock.assert_not_called()


@pytest.mark.asyncio
async def test_attribution_failure_does_not_propagate() -> None:
    """Подписка уже доставлена и оплачена — упасть здесь значит уронить
    успешный платёж из-за побочного эффекта."""
    db = AsyncMock()
    with patch(
        'app.services.campaign_service.AdvertisingCampaignService.attribute_campaign',
        AsyncMock(side_effect=RuntimeError('boom')),
    ):
        await _attribute_purchase_campaign(db, _purchase(), _user())
