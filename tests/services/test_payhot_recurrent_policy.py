from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import payhot_recurrent_policy as policy


@pytest.mark.parametrize('channel', ['telegram', 'cabinet'])
async def test_policy_exposes_only_recurring_methods_from_effective_config(monkeypatch, channel):
    monkeypatch.setitem(settings.__dict__, 'PAYHOT_RECURRENT_ENABLED', True)
    monkeypatch.setattr(
        policy,
        'get_payhot_checkout_policy',
        AsyncMock(return_value={'options': [{'id': 'crypto'}, {'id': 'sberpay'}, {'id': 'sbp'}]}),
    )
    user = SimpleNamespace(status='active', restriction_topup=False, restriction_subscription=False)
    result = await policy.get_payhot_recurrent_policy(AsyncMock(), user=user, channel=channel)
    assert result['enabled'] and result['methods'] == ['sbp']


@pytest.mark.parametrize(
    'user', [None, SimpleNamespace(status='blocked'), SimpleNamespace(status='active', restriction_topup=True)]
)
async def test_guest_and_restricted_users_cannot_enroll(monkeypatch, user):
    monkeypatch.setitem(settings.__dict__, 'PAYHOT_RECURRENT_ENABLED', True)
    assert not (await policy.get_payhot_recurrent_policy(AsyncMock(), user=user, channel='cabinet'))['enabled']


async def test_disabled_flag_does_not_query_checkout(monkeypatch):
    monkeypatch.setitem(settings.__dict__, 'PAYHOT_RECURRENT_ENABLED', False)
    read = AsyncMock()
    monkeypatch.setattr(policy, 'get_payhot_checkout_policy', read)
    assert not (await policy.get_payhot_recurrent_policy(AsyncMock(), user=None, channel='telegram'))['enabled']
    read.assert_not_awaited()
