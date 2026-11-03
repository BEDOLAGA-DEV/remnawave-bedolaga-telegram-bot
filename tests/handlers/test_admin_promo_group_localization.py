from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import promo_groups
from app.localization.texts import get_texts


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_missing_group_alert_uses_requested_language(monkeypatch, language):
    callback = SimpleNamespace(data='promo_group_manage_7', answer=AsyncMock())
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(promo_groups, 'get_promo_group_by_id', lookup)

    result = await promo_groups._get_group_or_alert(callback, object(), language)

    assert result is None
    callback.answer.assert_awaited_once_with(get_texts(language).t('ADMIN_PROMO_GROUP_NOT_FOUND'), show_alert=True)
    assert lookup.await_args.args[1] == 7


@pytest.mark.asyncio
async def test_missing_group_edit_uses_saved_language_and_clears_state(monkeypatch):
    message = SimpleNamespace(text='Example', answer=AsyncMock())
    state = SimpleNamespace(get_data=AsyncMock(return_value={'edit_group_id': 7, 'language': 'fa'}), clear=AsyncMock())
    monkeypatch.setattr(promo_groups, 'get_promo_group_by_id', AsyncMock(return_value=None))
    handler = promo_groups.process_edit_group_name
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__

    await handler(message, state, SimpleNamespace(language='en'), object())

    message.answer.assert_awaited_once_with(get_texts('fa').t('ADMIN_PROMO_GROUP_NOT_FOUND'))
    state.clear.assert_awaited_once()
