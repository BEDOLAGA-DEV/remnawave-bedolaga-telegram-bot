"""Old Telegram buttons cannot sell finite slots over an unlimited allowance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.subscription import devices


@pytest.mark.parametrize(
    ('handler', 'data'),
    [
        ('handle_change_devices', 'change_devices'),
        ('confirm_change_devices', 'change_devices_2'),
        ('execute_change_devices', 'change_devices_confirm_2'),
        ('confirm_add_devices', 'add_devices_2'),
    ],
)
async def test_unlimited_callbacks_stop_before_pricing_or_debit(monkeypatch, handler, data):
    user = SimpleNamespace(id=1, language='ru')
    sub = SimpleNamespace(device_limit=0, is_trial=False, tariff_id=1)
    monkeypatch.setattr(devices, '_resolve_subscription', AsyncMock(return_value=(sub, 1)))
    monkeypatch.setattr(devices, 'lock_user_for_pricing', AsyncMock(return_value=user))
    debit = AsyncMock()
    monkeypatch.setattr(devices, 'subtract_user_balance', debit)
    callback = SimpleNamespace(data=data, answer=AsyncMock())
    await getattr(devices, handler)(callback, user, None)
    callback.answer.assert_awaited_once()
    assert callback.answer.call_args.kwargs['show_alert'] is True
    debit.assert_not_awaited()
    assert sub.device_limit == 0
