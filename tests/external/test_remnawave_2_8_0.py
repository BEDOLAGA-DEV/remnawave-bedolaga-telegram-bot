"""Regression tests for the Remnawave 2.8.0 API changes.

Covers the breaking changes that affect this bot:
  * single-node restart now requires ``forceRestart`` in the request body;
  * users are fetched via cursor-based (keyset) pagination (``/api/users/stream``).

The HWID ``userUuid`` → ``userId`` rename is exercised indirectly: the delete
request payload is unchanged (still ``userUuid``), so ``test_remnawave_remove_device``
remains valid; the response-side rename is consumed in ``admin_traffic`` routes.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError


def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


@pytest.fixture(autouse=True)
def _local_happ_encryption_off(monkeypatch):
    """Тесты fallback-цепочки ниже проверяют путь панель -> внешний Happ API;
    локальное RSA-шифрование (основной путь по умолчанию) закоротило бы их,
    поэтому здесь оно выключено и включается явно в тестах локального шифрования."""
    from app.config import settings

    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', False)
    # Кэш ключуется по URL и не знает о подменах ключа в тестах — чистим между тестами.
    RemnaWaveAPI._happ_local_cache.clear()


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


def _reset_happ_state() -> None:
    RemnaWaveAPI._happ_encrypt_unavailable = False
    RemnaWaveAPI._happ_api_disabled_until = 0.0
    RemnaWaveAPI._happ_api_cache.clear()
    RemnaWaveAPI._happ_api_failed_urls.clear()
    RemnaWaveAPI._happ_local_cache.clear()


async def test_happ_encrypt_404_disables_panel_endpoint_and_falls_back():
    """2.8.0 removed POST /api/system/tools/happ/encrypt → 404 must disable further
    panel calls, but the official Happ API fallback must still produce a crypt5 link."""
    _reset_happ_state()
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('Not Found', 404, {}))
    api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/encrypted-x')
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') == 'happ://crypt5/encrypted-x'
        assert RemnaWaveAPI._happ_encrypt_unavailable is True

        # Subsequent calls short-circuit without touching the removed endpoint.
        api._make_request.reset_mock()
        api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/encrypted-y')
        assert await api.encrypt_happ_crypto_link('https://sub.example/y') == 'happ://crypt5/encrypted-y'
        api._make_request.assert_not_called()
    finally:
        _reset_happ_state()


async def test_happ_encrypt_non_404_error_keeps_endpoint_enabled():
    """A transient 5xx must NOT permanently disable happ-encrypt (only a 404 = removed)."""
    _reset_happ_state()
    api = _api()
    api._make_request = AsyncMock(side_effect=RemnaWaveAPIError('boom', 500, {}))
    api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/encrypted')
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') == 'happ://crypt5/encrypted'
        assert RemnaWaveAPI._happ_encrypt_unavailable is False
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_caches_by_subscription_url():
    """The client is recreated per request — the crypt5 cache must live on the class
    so the external Happ API is hit once per subscription URL until the DB save."""
    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/encrypted')
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') == 'happ://crypt5/encrypted'

        other = _api()
        other._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/should-not-be-called')
        assert await other.encrypt_happ_crypto_link('https://sub.example/x') == 'happ://crypt5/encrypted'
        other._call_happ_crypto_api.assert_not_called()
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_cooldown_after_failure():
    """A Happ API outage must not stall hot paths — one failure pauses further calls."""
    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(side_effect=TimeoutError('slow'))
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') is None
        assert RemnaWaveAPI._happ_api_disabled_until > 0

        api._call_happ_crypto_api.reset_mock()
        assert await api.encrypt_happ_crypto_link('https://sub.example/y') is None
        api._call_happ_crypto_api.assert_not_called()
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_rejects_unexpected_payload_per_url():
    """A non-happ:// body is a per-URL problem: never cached as a link, never retried,
    but it must NOT arm the global cooldown (other URLs keep working)."""
    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(return_value='<html>rate limited</html>')
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') is None
        assert not RemnaWaveAPI._happ_api_cache
        assert RemnaWaveAPI._happ_api_disabled_until == 0.0

        # Same URL is not retried, a different URL still goes through.
        api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/ok')
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') is None
        api._call_happ_crypto_api.assert_not_called()
        assert await api.encrypt_happ_crypto_link('https://sub.example/y') == 'happ://crypt5/ok'
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_4xx_does_not_poison_global_cooldown():
    """A 4xx rejection of one URL must not disable the fallback for everyone."""
    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(side_effect=RemnaWaveAPIError('bad url', 422, {}))
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/bad') is None
        assert RemnaWaveAPI._happ_api_disabled_until == 0.0

        api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/ok')
        assert await api.encrypt_happ_crypto_link('https://sub.example/good') == 'happ://crypt5/ok'
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_429_arms_cooldown_not_per_url_ban():
    """429 is service throttling: pause globally, but the URL must stay retryable."""
    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(side_effect=RemnaWaveAPIError('slow down', 429, {}))
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') is None
        assert RemnaWaveAPI._happ_api_disabled_until > 0
        assert 'https://sub.example/x' not in RemnaWaveAPI._happ_api_failed_urls
    finally:
        _reset_happ_state()


async def test_enrich_uses_external_fallback_only_in_cryptolink_mode(monkeypatch):
    """enrich runs on every get_user_by_*: subscription URLs must not go to the
    external Happ API unless the bot actually needs crypt links (happ_cryptolink
    mode); the cabinet generates missing links on demand in the app-config flow."""
    from types import SimpleNamespace

    from app.config import settings

    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    api = _api()
    api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/enriched')
    user = SimpleNamespace(happ_crypto_link=None, subscription_url='https://sub.example/x')
    try:
        monkeypatch.setattr(type(settings), 'is_happ_cryptolink_mode', lambda self: False)
        assert (await api.enrich_user_with_happ_link(user)).happ_crypto_link is None
        api._call_happ_crypto_api.assert_not_called()

        monkeypatch.setattr(type(settings), 'is_happ_cryptolink_mode', lambda self: True)
        assert (await api.enrich_user_with_happ_link(user)).happ_crypto_link == 'happ://crypt5/enriched'
    finally:
        _reset_happ_state()


async def test_happ_api_fallback_disabled_by_setting(monkeypatch):
    """HAPP_CRYPTOLINK_API_FALLBACK_ENABLED=false must skip the external service."""
    from app.config import settings

    _reset_happ_state()
    RemnaWaveAPI._happ_encrypt_unavailable = True
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_API_FALLBACK_ENABLED', False)
    api = _api()
    api._call_happ_crypto_api = AsyncMock(return_value='happ://crypt5/encrypted')
    try:
        assert await api.encrypt_happ_crypto_link('https://sub.example/x') is None
        api._call_happ_crypto_api.assert_not_called()
    finally:
        _reset_happ_state()


async def test_happ_local_encryption_roundtrip(monkeypatch):
    """Локальное шифрование должно давать happ://crypt4/<base64>, расшифровываемый
    приватным ключом (та же схема PKCS#1 v1.5, что у subpage панели). Настоящий
    приватный ключ есть только у Happ, поэтому roundtrip — на тестовой паре."""
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA

    import app.external.remnawave_api as rw
    from app.config import settings

    keypair = RSA.generate(2048)
    monkeypatch.setattr(rw, 'HAPP_CRYPTO_V4_PUBLIC_KEY', keypair.publickey().export_key().decode())
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', True)

    api = _api()
    api._make_request = AsyncMock()
    api._call_happ_crypto_api = AsyncMock()

    link = await api.encrypt_happ_crypto_link('https://sub.example/x')

    assert link is not None and link.startswith('happ://crypt4/')
    blob = base64.b64decode(link.removeprefix('happ://crypt4/'))
    assert PKCS1_v1_5.new(keypair).decrypt(blob, None) == b'https://sub.example/x'
    # Локальный путь не должен трогать ни панель, ни внешний сервис.
    api._make_request.assert_not_called()
    api._call_happ_crypto_api.assert_not_called()


async def test_happ_local_encryption_real_key_single_rsa4096_block(monkeypatch):
    """Со вшитым ключом Happ v4 (RSA-4096) шифртекст — один блок в 512 байт,
    как у ссылок, которые генерирует официальная страница подписки."""
    from app.config import settings

    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', True)

    link = RemnaWaveAPI._encrypt_locally('https://sub.example/x')

    assert link is not None and link.startswith('happ://crypt4/')
    assert len(base64.b64decode(link.removeprefix('happ://crypt4/'))) == 512


async def test_happ_local_encryption_rejects_oversized_payload(monkeypatch):
    """PKCS#1 v1.5 вмещает size_in_bytes()-11: слишком длинная ссылка -> None,
    а не исключение (дальше цепочка уйдёт в панель/внешний API)."""
    from app.config import settings

    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', True)

    assert RemnaWaveAPI._encrypt_locally('https://sub.example/' + 'x' * 600) is None


async def test_happ_local_encryption_disabled_by_setting():
    """HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED=false должен пропустить локальный
    путь (fixture уже выключила флаг) — цепочка идёт в панель/внешний API."""
    assert RemnaWaveAPI._encrypt_locally('https://sub.example/x') is None


async def test_happ_local_encryption_stable_for_same_url(monkeypatch):
    """Паддинг PKCS#1 v1.5 случайный, поэтому без кэша каждый вызов давал бы новую
    ссылку для того же URL — и синки (сравнение с сохранённой subscription_crypto_link)
    записывали бы ложное «изменение» на каждом проходе. Кэш держит ссылку стабильной
    в рамках процесса."""
    from app.config import settings

    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', True)

    first = RemnaWaveAPI._encrypt_locally('https://sub.example/x')
    second = RemnaWaveAPI._encrypt_locally('https://sub.example/x')

    assert first is not None
    assert first == second
    # Другой URL по-прежнему шифруется независимо.
    assert RemnaWaveAPI._encrypt_locally('https://sub.example/y') != first


async def test_enrich_uses_local_encryption_without_network(monkeypatch):
    """С локальным шифрованием enrich заполняет crypt-ссылку в любом режиме бота,
    не делая ни одного сетевого вызова (ни в панель, ни во внешний Happ API)."""
    from types import SimpleNamespace

    from app.config import settings

    _reset_happ_state()
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED', True)
    monkeypatch.setattr(type(settings), 'is_happ_cryptolink_mode', lambda self: False)
    api = _api()
    api._make_request = AsyncMock()
    api._call_happ_crypto_api = AsyncMock()
    user = SimpleNamespace(happ_crypto_link=None, subscription_url='https://sub.example/x')
    try:
        enriched = await api.enrich_user_with_happ_link(user)
        assert enriched.happ_crypto_link is not None
        assert enriched.happ_crypto_link.startswith('happ://crypt4/')
        api._make_request.assert_not_called()
        api._call_happ_crypto_api.assert_not_called()
    finally:
        _reset_happ_state()
