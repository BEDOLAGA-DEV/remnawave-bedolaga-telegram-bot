"""Ссылки конфигов подписки панели: /info у новых Remnawave отдаёт пустой список,
поэтому идём по трём ручкам — protected by-short-uuid, устаревший /info, публичный /api/sub (base64)."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from app.services.reachability.panel_links import (
    HWID_HEADERS,
    decode_subscription_body,
    fetch_panel_links,
)


pytestmark = pytest.mark.asyncio

LINK_A = 'vless://00000000-0000-4000-8000-000000000001@a.example:443?security=reality&sni=white.example#A'
LINK_B = 'trojan://pass@b.example:443?security=tls&sni=b.example#B'


class FakePanel:
    def __init__(
        self,
        *,
        protected=None,
        info=None,
        public: str | None = None,
        protected_error: Exception | None = None,
        hwid: str | None = None,
    ):
        self.protected = protected
        self.info = info
        self.public = public
        self.hwid = hwid  # тело для запроса с заголовками устройства; None — панель без HWID
        self.protected_error = protected_error
        self.calls: list[str] = []

    async def get_subscription_links_by_short_uuid(self, short_uuid: str) -> list[str]:
        self.calls.append('protected')
        if self.protected_error is not None:
            raise self.protected_error
        return list(self.protected or [])

    async def get_subscription_info(self, short_uuid: str):
        self.calls.append('info')
        return SimpleNamespace(links=list(self.info or []))

    async def get_public_subscription(self, short_uuid: str, *, user_agent=None, headers=None):
        self.calls.append(f'public:{user_agent}' + (':hwid' if headers else ''))
        if self.public is None:
            raise RuntimeError('404')
        if headers:
            assert headers == HWID_HEADERS
            return self.hwid or '', {}
        return self.public, {'x-hwid-active': 'true'} if self.hwid is not None else {}


async def test_protected_endpoint_wins_when_it_has_links() -> None:
    panel = FakePanel(protected=[LINK_A], info=[LINK_B])
    assert await fetch_panel_links(panel, 'sub-1') == [LINK_A]
    assert panel.calls == ['protected']


async def test_falls_back_to_legacy_info_when_protected_fails_or_is_empty() -> None:
    panel = FakePanel(protected_error=RuntimeError('403 scope'), info=[LINK_B])
    assert await fetch_panel_links(panel, 'sub-1') == [LINK_B]
    assert panel.calls == ['protected', 'info']

    panel = FakePanel(protected=[], info=[LINK_B])
    assert await fetch_panel_links(panel, 'sub-1') == [LINK_B]


async def test_falls_back_to_public_subscription_with_client_user_agent() -> None:
    body = base64.b64encode(f'{LINK_A}\n{LINK_B}\n'.encode()).decode()
    panel = FakePanel(protected=[], info=[], public=body)
    assert await fetch_panel_links(panel, 'sub-1') == [LINK_A, LINK_B]
    assert panel.calls[-1].startswith('public:') and 'Happ' in panel.calls[-1]


async def test_everything_empty_gives_empty_list_not_error() -> None:
    panel = FakePanel(protected=[], info=[], public=None)
    assert await fetch_panel_links(panel, 'sub-1') == []


async def test_prefer_public_takes_client_view_first_and_repeats_with_hwid_headers() -> None:
    stub = 'vless://00000000-0000-4000-8000-000000000001@0.0.0.0:1?security=none#stub'
    real = base64.b64encode(f'{LINK_A}\n{LINK_B}'.encode()).decode()
    panel = FakePanel(protected=[LINK_B], public=stub, hwid=real)
    assert await fetch_panel_links(panel, 'sub-1', prefer_public=True) == [LINK_A, LINK_B]
    assert panel.calls == ['public:Happ/3.5.0', 'public:Happ/3.5.0:hwid']
    # Без предпочтения публичной — сначала API, устройство на HWID-панели не регистрируем.
    panel = FakePanel(protected=[LINK_B], public=stub, hwid=real)
    assert await fetch_panel_links(panel, 'sub-1') == [LINK_B]
    assert panel.calls == ['protected']


async def test_decode_subscription_body_understands_xray_json() -> None:
    payload = json.dumps(
        [
            {
                'remarks': 'Auto',
                'outbounds': [
                    {
                        'tag': 'proxy',
                        'protocol': 'vless',
                        'settings': {'vnext': [{'address': 'a.example', 'port': 443, 'users': [{'id': 'u'}]}]},
                        'streamSettings': {
                            'network': 'tcp',
                            'security': 'reality',
                            'realitySettings': {'serverName': 'white.example'},
                        },
                    }
                ],
            }
        ]
    )
    links = decode_subscription_body(payload)
    assert len(links) == 1 and links[0].startswith('vless://u@a.example:443?') and 'sni=white.example' in links[0]


async def test_decode_subscription_body_accepts_plain_base64_and_garbage() -> None:
    assert decode_subscription_body(f'{LINK_A}\n\n{LINK_B}') == [LINK_A, LINK_B]
    encoded = base64.urlsafe_b64encode(f'{LINK_A}\n{LINK_B}'.encode()).decode().rstrip('=')
    assert decode_subscription_body(encoded) == [LINK_A, LINK_B]
    assert decode_subscription_body('<html>subscription page</html>') == []
    assert decode_subscription_body('') == []
