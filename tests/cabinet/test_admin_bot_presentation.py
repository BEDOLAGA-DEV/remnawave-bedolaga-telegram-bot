from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_bot_presentation as routes
from app.cabinet.routes.admin_bot_presentation import PresentationConfigPayload
from app.services.bot_presentation_service import (
    BotPresentationConfig,
    clear_bot_presentation_cache,
    get_bot_presentation_config,
    set_bot_presentation_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_bot_presentation_cache()
    yield
    clear_bot_presentation_cache()


@pytest.mark.asyncio
async def test_invalid_update_does_not_write_or_mutate_cache(monkeypatch) -> None:
    set_bot_presentation_cache(
        BotPresentationConfig(text_overrides={'MAIN_MENU_ACTION_PROMPT': 'Старый текст'})
    )
    upsert = AsyncMock()
    monkeypatch.setattr(routes, 'upsert_system_setting', upsert)
    db = SimpleNamespace(commit=AsyncMock())

    with pytest.raises(HTTPException) as error:
        await routes.update_bot_presentation_config_route(
            PresentationConfigPayload(
                text_overrides={'MAIN_MENU_RICH_DAYS_LEFT': 'Плейсхолдер потерян'}
            ),
            admin=cast('Any', SimpleNamespace(telegram_id=1)),
            db=cast('Any', db),
        )

    assert error.value.status_code == 400
    upsert.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert get_bot_presentation_config().text_overrides == {
        'MAIN_MENU_ACTION_PROMPT': 'Старый текст'
    }


@pytest.mark.asyncio
async def test_blank_text_override_is_treated_as_reset(monkeypatch) -> None:
    upsert = AsyncMock()
    monkeypatch.setattr(routes, 'upsert_system_setting', upsert)
    db = SimpleNamespace(commit=AsyncMock())

    response = await routes.update_bot_presentation_config_route(
        PresentationConfigPayload(text_overrides={'MAIN_MENU_ACTION_PROMPT': '   '}),
        admin=cast('Any', SimpleNamespace(telegram_id=1)),
        db=cast('Any', db),
    )

    assert response.text_overrides == {}
    assert get_bot_presentation_config().text_overrides == {}


@pytest.mark.asyncio
async def test_valid_update_commits_before_refreshing_cache(monkeypatch) -> None:
    upsert = AsyncMock()
    monkeypatch.setattr(routes, 'upsert_system_setting', upsert)
    db = SimpleNamespace(commit=AsyncMock())

    response = await routes.update_bot_presentation_config_route(
        PresentationConfigPayload(
            emoji_overrides={'PROMO_GROUP_DISCOUNT_TRAFFIC#0:📊': '5368324170671202286'}
        ),
        admin=cast('Any', SimpleNamespace(telegram_id=1)),
        db=cast('Any', db),
    )

    upsert.assert_awaited_once()
    db.commit.assert_awaited_once()
    assert response.emoji_overrides == {
        'PROMO_GROUP_DISCOUNT_TRAFFIC#0:📊': '5368324170671202286'
    }
    assert get_bot_presentation_config().emoji_overrides == {
        'PROMO_GROUP_DISCOUNT_TRAFFIC#0:📊': '5368324170671202286'
    }
