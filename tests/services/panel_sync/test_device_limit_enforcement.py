"""An explicit finite panel cap also bounds previously registered devices."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import RemnaWaveAPIError
from app.services.panel_sync.writer import push_subscription
from tests.services.panel_sync.test_writer import NOW, _api, _panel_user, _sub, _user


pytestmark = pytest.mark.asyncio


def api_with_devices(count):
    api = _api(get_user_by_id=_panel_user())
    devices = [
        {
            'userId': 42,
            'hwid': f'local-{i}',
            'createdAt': NOW.isoformat(),
            'updatedAt': (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i)).isoformat(),
        }
        for i in range(count)
    ]

    async def get(_):
        return {'total': len(devices), 'devices': list(devices)}

    async def remove(_, hwid):
        devices[:] = [d for d in devices if d['hwid'] != hwid]
        return True

    api.get_user_devices_all = AsyncMock(side_effect=get)
    api.remove_device = AsyncMock(side_effect=remove)
    return api, devices


async def test_explicit_cap_keeps_most_recent_registrations_and_removes_only_excess():
    api, devices = api_with_devices(4)
    await push_subscription(api, _user(), _sub(remnawave_id=42, device_limit=2), multi_tariff=True, now=NOW)
    assert [d['hwid'] for d in devices] == ['local-2', 'local-3']
    assert api.remove_device.await_count == 2
    assert all(call.args[0] == 42 for call in api.remove_device.await_args_list)


@pytest.mark.parametrize('cap', [0, None])
async def test_unlimited_or_omitted_cap_never_removes_devices(cap):
    api, _ = api_with_devices(4)
    # Payload field filtering deliberately omits the cap in the None case.
    fields = {'hwid_device_limit'} if cap == 0 else {'status'}
    await push_subscription(
        api, _user(), _sub(remnawave_id=42, device_limit=cap), multi_tariff=True, now=NOW, only_fields=fields
    )
    api.get_user_devices_all.assert_not_awaited()
    api.remove_device.assert_not_awaited()


async def test_failed_removal_is_not_reported_as_success_and_next_sync_retries():
    api, devices = api_with_devices(4)
    remove = api.remove_device.side_effect
    api.remove_device = AsyncMock(return_value=False)
    with pytest.raises(RemnaWaveAPIError):
        await push_subscription(api, _user(), _sub(remnawave_id=42, device_limit=2), multi_tariff=True, now=NOW)
    api.remove_device.side_effect = remove
    await push_subscription(api, _user(), _sub(remnawave_id=42, device_limit=2), multi_tariff=True, now=NOW)
    assert len(devices) == 2


async def test_same_cap_with_legal_device_count_preserves_registrations():
    api, devices = api_with_devices(2)
    await push_subscription(api, _user(), _sub(remnawave_id=42, device_limit=2), multi_tariff=True, now=NOW)
    assert len(devices) == 2
    api.remove_device.assert_not_awaited()


@pytest.mark.parametrize(
    'payload',
    [
        {'total': 3, 'devices': []},
        {'total': 1, 'devices': [{'userId': 43, 'hwid': 'other-account'}]},
        {'total': 1, 'devices': [{'userId': 42, 'hwid': ''}]},
    ],
)
async def test_invalid_or_foreign_device_list_never_deletes_anything(payload):
    api, _ = api_with_devices(4)
    api.get_user_devices_all = AsyncMock(return_value=payload)
    with pytest.raises(RemnaWaveAPIError):
        await push_subscription(api, _user(), _sub(remnawave_id=42, device_limit=2), multi_tariff=True, now=NOW)
    api.remove_device.assert_not_awaited()


async def test_device_reappearing_after_removal_requires_retry():
    api, devices = api_with_devices(4)
    original_get = api.get_user_devices_all.side_effect
    reads = 0

    async def get(panel_id):
        nonlocal reads
        reads += 1
        result = await original_get(panel_id)
        if reads == 2:
            devices.append({'userId': 42, 'hwid': 'local-concurrent', 'updatedAt': NOW.isoformat()})
            result = await original_get(panel_id)
        return result

    api.get_user_devices_all.side_effect = get
    with pytest.raises(RemnaWaveAPIError, match='retry required'):
        await push_subscription(api, _user(), _sub(remnawave_id=42, device_limit=2), multi_tariff=True, now=NOW)
    await push_subscription(api, _user(), _sub(remnawave_id=42, device_limit=2), multi_tariff=True, now=NOW)
    assert len(devices) == 2
