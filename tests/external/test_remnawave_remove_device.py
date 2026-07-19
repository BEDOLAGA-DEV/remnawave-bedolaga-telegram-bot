"""RemnaWaveAPI.remove_device must report success based on the ACTUAL panel result,
not just "no exception was raised".

The panel's POST /api/hwid/devices/delete returns the user's remaining devices
({response: {total, devices}}). Previously remove_device returned True for any
non-error response (so a no-op delete looked successful) and returned False on a
404 (which actually means the device is already gone == success). Both are fixed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError


def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


async def test_success_when_target_hwid_absent_from_remaining_list():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'total': 1, 'devices': [{'hwid': 'OTHER'}]}})

    assert await api.remove_device('user-uuid', 'TARGET') is True


async def test_failure_when_panel_acks_but_hwid_still_present():
    api = _api()
    api._make_request = AsyncMock(
        return_value={'response': {'total': 2, 'devices': [{'hwid': 'TARGET'}, {'hwid': 'OTHER'}]}}
    )

    # Panel accepted the request (no error) but the device is still bound → NOT deleted.
    assert await api.remove_device('user-uuid', 'TARGET') is False


async def test_404_is_treated_as_success():
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('not found', 404))

    # Device/user already absent — that's the desired end state.
    assert await api.remove_device('user-uuid', 'TARGET') is True


async def test_other_api_error_is_failure():
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('server error', 500))

    assert await api.remove_device('user-uuid', 'TARGET') is False


async def test_transient_exception_is_failure():
    api = _api()
    api._make_request = AsyncMock(side_effect=RuntimeError('connection reset'))

    assert await api.remove_device('user-uuid', 'TARGET') is False


async def test_bare_ack_without_device_list_is_success():
    """Panels that reply with just an ack (no devices echo) keep the old behaviour."""
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {}})

    assert await api.remove_device('user-uuid', 'TARGET') is True


async def test_empty_response_is_success():
    api = _api()
    api._make_request = AsyncMock(return_value={})

    assert await api.remove_device('user-uuid', 'TARGET') is True
