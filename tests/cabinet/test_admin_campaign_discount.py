"""Скидочная кампания без процента бонуса не выдаст — кабинет её не сохраняет."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_campaigns as route
from app.cabinet.schemas.campaigns import CampaignCreateRequest, CampaignUpdateRequest


ADMIN = SimpleNamespace(id=1)


def _campaign(**kw: object) -> SimpleNamespace:
    base: dict[str, object] = {
        'id': 7,
        'name': 'curly',
        'start_parameter': 'curly',
        'bonus_type': 'none',
        'tariff_id': None,
        'discount_percent': None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_create_discount_campaign_requires_percent() -> None:
    create = AsyncMock()
    with (
        patch.object(route, 'get_campaign_by_start_parameter', AsyncMock(return_value=None)),
        patch.object(route, 'create_campaign', create),
    ):
        with pytest.raises(HTTPException) as exc:
            await route.create_new_campaign(
                request=CampaignCreateRequest(name='curly', start_parameter='curly', bonus_type='discount'),
                admin=ADMIN,
                db=AsyncMock(),
            )

    assert exc.value.status_code == 400
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_discount_campaign_passes_percent_and_term() -> None:
    create = AsyncMock(return_value=_campaign(bonus_type='discount'))
    with (
        patch.object(route, 'get_campaign_by_start_parameter', AsyncMock(return_value=None)),
        patch.object(route, 'create_campaign', create),
        patch.object(route, 'get_campaign', AsyncMock(return_value='detail')),
    ):
        await route.create_new_campaign(
            request=CampaignCreateRequest(
                name='curly',
                start_parameter='curly',
                bonus_type='discount',
                discount_percent=15,
                discount_duration_hours=72,
            ),
            admin=ADMIN,
            db=AsyncMock(),
        )

    assert create.await_args.kwargs['discount_percent'] == 15
    assert create.await_args.kwargs['discount_duration_hours'] == 72


@pytest.mark.asyncio
async def test_switching_to_discount_requires_percent() -> None:
    update = AsyncMock()
    with (
        patch.object(route, 'get_campaign_by_id', AsyncMock(return_value=_campaign())),
        patch.object(route, 'update_campaign', update),
    ):
        with pytest.raises(HTTPException) as exc:
            await route.update_existing_campaign(
                campaign_id=7,
                request=CampaignUpdateRequest(bonus_type='discount'),
                admin=ADMIN,
                db=AsyncMock(),
            )

    assert exc.value.status_code == 400
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_switching_to_discount_with_percent_updates() -> None:
    update = AsyncMock()
    with (
        patch.object(route, 'get_campaign_by_id', AsyncMock(return_value=_campaign())),
        patch.object(route, 'update_campaign', update),
        patch.object(route, 'get_campaign', AsyncMock(return_value='detail')),
    ):
        await route.update_existing_campaign(
            campaign_id=7,
            request=CampaignUpdateRequest(bonus_type='discount', discount_percent=15, discount_duration_hours=0),
            admin=ADMIN,
            db=AsyncMock(),
        )

    kwargs = update.await_args.kwargs
    assert kwargs['bonus_type'] == 'discount'
    assert kwargs['discount_percent'] == 15
    assert kwargs['discount_duration_hours'] == 0


@pytest.mark.asyncio
async def test_percent_is_capped_at_100() -> None:
    with pytest.raises(ValueError):
        CampaignCreateRequest(name='curly', start_parameter='curly', bonus_type='discount', discount_percent=150)
