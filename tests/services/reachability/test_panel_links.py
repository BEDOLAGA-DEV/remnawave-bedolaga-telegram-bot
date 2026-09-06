"""Ссылки конфигов подписки панели: /info у новых Remnawave отдаёт пустой список,
поэтому идём по трём ручкам — protected by-short-uuid, устаревший /info, публичный /api/sub (base64)."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from app.services.reachability.panel_links import decode_subscription_body, fetch_panel_links


pytestmark = pytest.mark.asyncio

LINK_A = 'vless://00000000-0000-4000-8000-000000000001@a.example:443?security=reality&sni=white.example#A'
LINK_B = 'trojan://pass@b.example:443?security=tls&sni=b.example#B'


class FakePanel:
    def __init__(
        self, *, protected=None, info=None, public: str | None = None, protected_error: Exception | None = None
    ):
        self.protected = protected
        self.info = info
        self.public = public
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

    async def get_subscription_by_short_uuid(self, short_uuid: str, user_agent: str | None = None) -> str:
        self.calls.append(f'public:{user_agent}')
        if self.public is None:
            raise RuntimeError('404')
        return self.public


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


async def test_decode_subscription_body_accepts_plain_base64_and_garbage() -> None:
    assert decode_subscription_body(f'{LINK_A}\n\n{LINK_B}') == [LINK_A, LINK_B]
    encoded = base64.urlsafe_b64encode(f'{LINK_A}\n{LINK_B}'.encode()).decode().rstrip('=')
    assert decode_subscription_body(encoded) == [LINK_A, LINK_B]
    assert decode_subscription_body('<html>subscription page</html>') == []
    assert decode_subscription_body('') == []
