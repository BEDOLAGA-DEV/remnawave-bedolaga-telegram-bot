"""Regression tests for the Remnawave 2.8.0 API changes.

Covers the breaking changes that affect this bot:
  * single-node restart now requires ``forceRestart`` in the request body;
  * users are fetched via cursor-based (keyset) pagination (``/api/users/stream``).

The HWID ``userUuid`` → ``userId`` rename is exercised indirectly: the delete
request payload is unchanged (still ``userUuid``), so ``test_remnawave_remove_device``
remains valid; the response-side rename is consumed in ``admin_traffic`` routes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError


def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


async def test_restart_node_sends_force_restart_body_default_false():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'eventSent': True}})

    assert await api.restart_node('node-uuid') is True

    method, endpoint = api._make_request.call_args.args[:2]
    body = api._make_request.call_args.args[2]
    assert (method, endpoint) == ('POST', '/api/nodes/node-uuid/actions/restart')
    assert body == {'forceRestart': False}


async def test_restart_node_forwards_force_restart_true():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'eventSent': True}})

    await api.restart_node('node-uuid', force_restart=True)

    assert api._make_request.call_args.args[2] == {'forceRestart': True}


async def test_users_page_stream_omits_cursor_on_first_page():
    api = _api()
    api._parse_user = lambda u: u  # bypass heavy user parsing
    api._make_request = AsyncMock(
        return_value={'response': {'users': [{'uuid': 'a'}], 'nextCursor': '5', 'hasMore': True}}
    )

    page = await api.get_all_users_page_stream()

    assert api._make_request.call_args.args[:2] == ('GET', '/api/users/stream')
    # First page: no cursor in params, only size.
    assert api._make_request.call_args.kwargs['params'] == {'size': 500}
    assert page == {'users': [{'uuid': 'a'}], 'nextCursor': '5', 'hasMore': True}


async def test_users_page_stream_passes_cursor_when_given():
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(return_value={'response': {'users': [], 'nextCursor': None, 'hasMore': False}})

    await api.get_all_users_page_stream(cursor='42', size=100)

    assert api._make_request.call_args.kwargs['params'] == {'size': 100, 'cursor': '42'}


async def test_users_stream_follows_cursor_until_exhausted():
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(
        side_effect=[
            {'response': {'users': [{'uuid': 'a'}, {'uuid': 'b'}], 'nextCursor': '2', 'hasMore': True}},
            {'response': {'users': [{'uuid': 'c'}], 'nextCursor': None, 'hasMore': False}},
        ]
    )

    users = await api.get_all_users_stream(size=2)

    assert [u['uuid'] for u in users] == ['a', 'b', 'c']
    assert api._make_request.call_count == 2
    # Second call must carry the nextCursor from the first page.
    assert api._make_request.call_args_list[1].kwargs['params'] == {'size': 2, 'cursor': '2'}


async def test_users_stream_stops_when_next_cursor_is_null_even_if_has_more_true():
    """Defensive: a null nextCursor terminates the scan regardless of hasMore."""
    api = _api()
    api._parse_user = lambda u: u
    api._make_request = AsyncMock(
        return_value={'response': {'users': [{'uuid': 'a'}], 'nextCursor': None, 'hasMore': True}}
    )

    users = await api.get_all_users_stream()

    assert [u['uuid'] for u in users] == ['a']
    assert api._make_request.call_count == 1


async def test_happ_encrypt_disables_itself_after_404():
    """2.8.0 removed POST /api/system/tools/happ/encrypt → 404 must disable further calls."""
    RemnaWaveAPI._happ_encrypt_unavailable = False
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('Not Found', 404, {}))
    try:
        assert await api.encrypt_happ_crypto_link('vless://x') is None
        assert RemnaWaveAPI._happ_encrypt_unavailable is True

        # Subsequent calls short-circuit without touching the removed endpoint.
        api._make_request.reset_mock()
        assert await api.encrypt_happ_crypto_link('vless://y') is None
        api._make_request.assert_not_called()
    finally:
        RemnaWaveAPI._happ_encrypt_unavailable = False


async def test_happ_encrypt_non_404_error_keeps_endpoint_enabled():
    """A transient 5xx must NOT permanently disable happ-encrypt (only a 404 = removed)."""
    RemnaWaveAPI._happ_encrypt_unavailable = False
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('boom', 500, {}))
    try:
        assert await api.encrypt_happ_crypto_link('vless://x') is None
        assert RemnaWaveAPI._happ_encrypt_unavailable is False
    finally:
        RemnaWaveAPI._happ_encrypt_unavailable = False
