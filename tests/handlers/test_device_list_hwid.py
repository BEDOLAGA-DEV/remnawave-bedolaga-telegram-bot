"""Display exact device identifiers without interpreting their HTML."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.subscription import devices


@pytest.mark.parametrize('hwid', ['masked12-remaining<&>device-id', '', None])
async def test_bot_device_list_exposes_full_hwid(monkeypatch, hwid):
    device = {'hwid': hwid, 'platform': 'Android', 'deviceModel': 'Masked phone'}
    monkeypatch.setattr(devices, '_enrich_devices_with_aliases', AsyncMock(return_value=[device]))
    monkeypatch.setattr(devices, 'get_devices_management_keyboard', lambda *args, **kwargs: None)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()))
    await devices.show_devices_page(callback, SimpleNamespace(id=7, language='ru'), [device])
    message = callback.message.edit_text.await_args.args[0]
    if hwid:
        assert '<code>masked12-remaining&lt;&amp;&gt;device-id</code>' in message
    else:
        assert '<code>' not in message


async def test_device_hwid_stays_on_its_page(monkeypatch):
    rows = [{'hwid': f'masked-device-{i}', 'platform': 'Android', 'deviceModel': 'Phone'} for i in range(6)]
    monkeypatch.setattr(devices, '_enrich_devices_with_aliases', AsyncMock(return_value=rows))
    monkeypatch.setattr(devices, 'get_devices_management_keyboard', lambda *args, **kwargs: None)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()))
    await devices.show_devices_page(callback, SimpleNamespace(id=7, language='ru'), rows, page=2)
    message = callback.message.edit_text.await_args.args[0]
    assert '<code>masked-device-5</code>' in message
    assert 'masked-device-0' not in message
