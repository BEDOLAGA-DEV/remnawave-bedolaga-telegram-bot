"""Загрузка чужой подписки по URL для поля «Конфиг или подписка»: только публичные http(s)-адреса,
клиентский User-Agent, лимит размера, страница вместо конфигов — понятная ошибка."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from yarl import URL

from app.services.reachability.panel_links import CLIENT_USER_AGENT, HWID_HEADERS
from app.services.reachability.subscriptions import (
    MAX_BODY_BYTES,
    PublicOnlyResolver,
    SubscriptionFetchError,
    fetch_subscription_links,
    is_subscription_url,
    validate_public_url,
)


pytestmark = pytest.mark.asyncio

LINK_A = 'vless://00000000-0000-4000-8000-000000000001@a.example:443?security=reality&sni=white.example#A'
LINK_B = 'trojan://pass@b.example:443?security=tls&sni=b.example#B'


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]


class FakeSession:
    def __init__(
        self,
        body: str | bytes,
        status: int = 200,
        final_url: str | None = None,
        hwid_body: str | None = None,
    ) -> None:
        self.body = body.encode() if isinstance(body, str) else body
        self.status = status
        self.final_url = final_url
        self.hwid_body = hwid_body.encode() if isinstance(hwid_body, str) else hwid_body
        self.requests: list[dict] = []

    def get(self, url: str, **kwargs):
        self.requests.append({'url': url, **kwargs})
        with_hwid = 'x-hwid' in (kwargs.get('headers') or {})
        body = self.hwid_body if with_hwid and self.hwid_body is not None else self.body
        headers = {'x-hwid-active': 'true'} if self.hwid_body is not None else {}
        response = SimpleNamespace(
            status=self.status, url=URL(self.final_url or url), content=_Content(body), headers=headers
        )

        class _Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *exc):
                return None

        return _Ctx()

    async def close(self) -> None:
        return None


def test_is_subscription_url_and_validate_public_url() -> None:
    assert is_subscription_url('https://sub.example/abc') and is_subscription_url('HTTP://x.example/a')
    assert not is_subscription_url('vless://u@h:443') and not is_subscription_url('ya.ru')
    assert validate_public_url('https://sub.example/abc') == 'https://sub.example/abc'
    for bad in (
        'ftp://sub.example/a',
        'http://127.0.0.1/a',
        'http://10.0.0.1/a',
        'http://localhost/a',
        'https://u:p@h.example/a',
        'https:///a',
    ):
        with pytest.raises(SubscriptionFetchError):
            validate_public_url(bad)


async def test_fetch_decodes_base64_body_with_client_user_agent() -> None:
    session = FakeSession(base64.b64encode(f'{LINK_A}\n{LINK_B}'.encode()).decode())
    links = await fetch_subscription_links('https://sub.example/abc', session_factory=lambda: session)
    assert links == [LINK_A, LINK_B]
    assert session.requests[0]['headers']['User-Agent'] == CLIENT_USER_AGENT


async def test_fetch_repeats_with_device_headers_when_panel_requires_hwid() -> None:
    stub = 'vless://00000000-0000-4000-8000-000000000001@0.0.0.0:1?security=none#stub'
    session = FakeSession(stub, hwid_body=f'{LINK_A}\n')
    links = await fetch_subscription_links('https://sub.example/abc', session_factory=lambda: session)
    assert links == [LINK_A]
    assert len(session.requests) == 2 and session.requests[1]['headers']['x-hwid'] == HWID_HEADERS['x-hwid']
    assert session.requests[1]['headers']['User-Agent'] == CLIENT_USER_AGENT


async def test_fetch_accepts_plain_links_and_rejects_pages_errors_and_private_redirects() -> None:
    assert await fetch_subscription_links(
        'https://sub.example/abc', session_factory=lambda: FakeSession(f'{LINK_A}\n')
    ) == [LINK_A]
    with pytest.raises(SubscriptionFetchError, match='конфиг'):
        await fetch_subscription_links(
            'https://sub.example/abc', session_factory=lambda: FakeSession('<html>page</html>')
        )
    with pytest.raises(SubscriptionFetchError, match='404'):
        await fetch_subscription_links('https://sub.example/abc', session_factory=lambda: FakeSession('', status=404))
    with pytest.raises(SubscriptionFetchError):
        await fetch_subscription_links(
            'https://sub.example/abc', session_factory=lambda: FakeSession(LINK_A, final_url='http://127.0.0.1/x')
        )
    with pytest.raises(SubscriptionFetchError, match='велик'):
        await fetch_subscription_links(
            'https://sub.example/abc', session_factory=lambda: FakeSession(b'x' * (MAX_BODY_BYTES + 1))
        )


class _FakeInnerResolver:
    """Резолвер-заглушка: отдаёт заданные адреса, помнит, что закрыт."""

    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses
        self.closed = False

    async def resolve(self, host, port=0, family=0):
        return [
            {'hostname': host, 'host': address, 'port': port, 'family': family, 'proto': 6, 'flags': 0}
            for address in self.addresses
        ]

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_public_only_resolver_passes_public_and_rejects_private() -> None:
    public = PublicOnlyResolver(_FakeInnerResolver(['93.184.216.34', '2606:2800:220:1:248:1893:25c8:1946']))
    assert [item['host'] for item in await public.resolve('sub.example', 443)] == [
        '93.184.216.34',
        '2606:2800:220:1:248:1893:25c8:1946',
    ]
    for private in ('127.0.0.1', '10.0.0.5', '169.254.169.254', '::1', 'fd00::1', 'not-an-ip'):
        inner = _FakeInnerResolver(['93.184.216.34', private])
        with pytest.raises(SubscriptionFetchError, match='служебный адрес'):
            await PublicOnlyResolver(inner).resolve('sub.example', 443)
    inner = _FakeInnerResolver([])
    await PublicOnlyResolver(inner).close()
    assert inner.closed


@pytest.mark.asyncio
async def test_fetch_uses_public_only_resolver_for_the_real_connection() -> None:
    """Домен, глядящий во внутреннюю сеть, отсекается настоящим aiohttp ещё до соединения."""
    import aiohttp

    def session_factory():
        resolver = PublicOnlyResolver(_FakeInnerResolver(['10.0.0.5']))
        return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=resolver))

    with pytest.raises(SubscriptionFetchError, match=r'служебный адрес 10\.0\.0\.5'):
        await fetch_subscription_links('http://internal.example/sub', session_factory=session_factory)
