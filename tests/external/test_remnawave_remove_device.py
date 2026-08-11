"""HWID device deletion: RemnaWaveAPI.remove_device / reset_user_devices.

Two contracts are covered here.

1) The RESULT contract. `remove_device` must report success based on the ACTUAL
   panel result, not just "no exception was raised". The panel's
   POST /api/hwid/devices/delete returns the user's remaining devices
   ({response: {total, devices}}). Previously remove_device returned True for any
   non-error response (so a no-op delete looked successful) and returned False on
   a 404 (which actually means the device is already gone == success).

2) The REQUEST contract (API 3.0.0). The panel identifies a user by its numeric
   `id`, so the body is `{'userId': int, 'hwid': ...}` — NOT the old
   `{'userUuid': <uuid>, ...}`. These tests assert the request body explicitly:
   without that, the userUuid -> userId rename would have slipped through
   unnoticed (every assertion below still "passed" while the client silently
   refused to talk to the panel at all).
   `reset_user_devices` is likewise ONE POST /api/hwid/devices/delete-all with
   `{'userId': int}` instead of the old per-device delete loop.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError


PANEL_USER_ID = 4242


def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


# ---------------------------------------------------------------------------
# remove_device — форма запроса (API 3.0.0)
# ---------------------------------------------------------------------------


async def test_remove_device_posts_numeric_user_id_in_body():
    """Тело запроса — {'userId': int, 'hwid': str}; никакого userUuid."""
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    assert await api.remove_device(PANEL_USER_ID, 'TARGET') is True

    api._make_request.assert_awaited_once()
    args, kwargs = api._make_request.await_args
    assert args == ('POST', '/api/hwid/devices/delete')
    assert kwargs['data'] == {'userId': PANEL_USER_ID, 'hwid': 'TARGET'}
    assert isinstance(kwargs['data']['userId'], int)
    assert 'userUuid' not in kwargs['data']


async def test_remove_device_coerces_digit_string_id_to_int():
    """БД отдаёт BigInteger, но JSON/FSM могут донести строку — коерсим до запроса."""
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    assert await api.remove_device('4242', 'TARGET') is True

    assert api._make_request.await_args.kwargs['data'] == {'userId': 4242, 'hwid': 'TARGET'}


async def test_remove_device_rejects_uuid_id_without_hitting_the_panel():
    """Протухший UUID вместо id — наша битая ссылка, а не запрос к панели.

    Маршруты 3.0.0 параметризованы z.coerce.number(), поэтому UUID дал бы 400
    (а не 404) — отсекаем на границе клиента, чтобы мусор не уходил в сеть.
    """
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    assert await api.remove_device('6e0d1f6c-0000-4000-8000-000000000000', 'TARGET') is False

    api._make_request.assert_not_awaited()


# ---------------------------------------------------------------------------
# remove_device — трактовка ответа
# ---------------------------------------------------------------------------


async def test_success_when_target_hwid_absent_from_remaining_list():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'total': 1, 'devices': [{'hwid': 'OTHER'}]}})

    assert await api.remove_device(PANEL_USER_ID, 'TARGET') is True


async def test_failure_when_panel_acks_but_hwid_still_present():
    api = _api()
    api._make_request = AsyncMock(
        return_value={'response': {'total': 2, 'devices': [{'hwid': 'TARGET'}, {'hwid': 'OTHER'}]}}
    )

    # Panel accepted the request (no error) but the device is still bound → NOT deleted.
    assert await api.remove_device(PANEL_USER_ID, 'TARGET') is False


async def test_404_is_treated_as_success():
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('not found', 404))

    # Device/user already absent — that's the desired end state.
    assert await api.remove_device(PANEL_USER_ID, 'TARGET') is True
    # …и это именно ответ панели, а не отказ клиента отправить запрос.
    api._make_request.assert_awaited_once()


async def test_other_api_error_is_failure():
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('server error', 500))

    assert await api.remove_device(PANEL_USER_ID, 'TARGET') is False
    api._make_request.assert_awaited_once()


async def test_transient_exception_is_failure():
    api = _api()
    api._make_request = AsyncMock(side_effect=RuntimeError('connection reset'))

    assert await api.remove_device(PANEL_USER_ID, 'TARGET') is False
    api._make_request.assert_awaited_once()


async def test_bare_ack_without_device_list_is_success():
    """Panels that reply with just an ack (no devices echo) keep the old behaviour."""
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {}})

    assert await api.remove_device(PANEL_USER_ID, 'TARGET') is True


async def test_empty_response_is_success():
    api = _api()
    api._make_request = AsyncMock(return_value={})

    assert await api.remove_device(PANEL_USER_ID, 'TARGET') is True


# ---------------------------------------------------------------------------
# reset_user_devices — один delete-all вместо цикла удалений
# ---------------------------------------------------------------------------


async def test_reset_user_devices_is_a_single_delete_all_call():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {}})

    assert await api.reset_user_devices(PANEL_USER_ID) is True

    # Ровно ОДИН запрос: никакого «прочитать список и удалить по одному».
    api._make_request.assert_awaited_once()
    args, kwargs = api._make_request.await_args
    assert args == ('POST', '/api/hwid/devices/delete-all')
    assert kwargs['data'] == {'userId': PANEL_USER_ID}
    assert isinstance(kwargs['data']['userId'], int)


async def test_reset_user_devices_coerces_digit_string_id_to_int():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {}})

    assert await api.reset_user_devices('4242') is True

    assert api._make_request.await_args.kwargs['data'] == {'userId': 4242}


async def test_reset_user_devices_rejects_uuid_id_without_hitting_the_panel():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {}})

    assert await api.reset_user_devices('6e0d1f6c-0000-4000-8000-000000000000') is False

    api._make_request.assert_not_awaited()


async def test_reset_user_devices_404_is_success():
    """Пользователя/устройств уже нет — цель достигнута."""
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('not found', 404))

    assert await api.reset_user_devices(PANEL_USER_ID) is True


@pytest.mark.parametrize(
    'failure',
    [RemnaWaveAPIError('server error', 500), RuntimeError('connection reset')],
    ids=['api_error', 'transient'],
)
async def test_reset_user_devices_failure_is_reported(failure):
    api = _api()
    api._make_request = AsyncMock(side_effect=failure)

    assert await api.reset_user_devices(PANEL_USER_ID) is False
