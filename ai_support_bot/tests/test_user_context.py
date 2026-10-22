from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from ai_support_bot.app.services import user_data


@pytest.mark.asyncio
@pytest.mark.parametrize(('limit', 'expected'), [(0, 'без ограничений'), (10, '10')])
async def test_user_context_includes_known_email_and_explicit_device_limit(monkeypatch, limit, expected):
    user_result = Mock()
    user_result.mappings.return_value.first.return_value = {
        'id': 7, 'email': 'customer@example.com', 'auth_type': 'email',
    }
    subscriptions = Mock()
    subscriptions.mappings.return_value.all.return_value = [{'id': 11, 'device_limit': limit}]
    transactions = Mock()
    transactions.mappings.return_value.all.return_value = []
    session = AsyncMock()
    session.execute.side_effect = [user_result, subscriptions, transactions]
    monkeypatch.setattr(user_data, 'get_main_session', AsyncMock(return_value=session))
    monkeypatch.setattr(user_data.settings_store, 'get_bool', lambda key: False)

    context = await user_data.build_user_context(4242)

    assert 'Email аккаунта: customer@example.com' in context
    assert 'Способ регистрации: email' in context
    assert f'устройств={expected}' in context
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_payment_context_has_exact_time_identifier_and_completion_status(monkeypatch):
    user = Mock()
    user.mappings.return_value.first.return_value = {'id': 7}
    subs = Mock()
    subs.mappings.return_value.all.return_value = []
    transactions = Mock()
    transactions.mappings.return_value.all.return_value = [{
        'id': 123, 'type': 'deposit', 'amount_kopeks': 10000, 'external_id': 'payment-abc',
        'created_at': datetime(2026, 10, 15, 12, 34, tzinfo=UTC), 'is_completed': False,
    }]
    session = AsyncMock()
    session.execute.side_effect = [user, subs, transactions]
    monkeypatch.setattr(user_data, 'get_main_session', AsyncMock(return_value=session))
    monkeypatch.setattr(user_data.settings_store, 'get_bool', lambda key: False)
    context = await user_data.build_user_context(42)
    assert '15.10.2026 12:34 UTC' in context
    assert 'статус=не завершена' in context
    assert 'идентификатор=payment-abc' in context
