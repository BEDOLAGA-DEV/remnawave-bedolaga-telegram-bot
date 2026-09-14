"""INCY buttons must hand out encrypted links in both button paths.

Subpage configs ship INCY as ``incy://import/{{SUBSCRIPTION_LINK}}``, so without
wrapping the subscription URL travels in the clear inside the button URL — unlike
Happ, which gets ``{{HAPP_CRYPT4_LINK}}``. Happ must keep working untouched.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.cabinet.routes.subscription_modules.status import _create_deep_link, _resolve_button_url
from app.config import settings
from app.handlers.subscription.common import create_deep_link as bot_create_deep_link, resolve_button_url
from app.utils.incy_crypt1 import _KEY_B64, INCY_CRYPT1_DEEP_LINK_PREFIX


SUB_URL = 'https://panel.example/sub/abc123'
CRYPT5 = 'happ://crypt5/encrypted-payload'


def decrypted_url(link: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    payload = link[len(INCY_CRYPT1_DEEP_LINK_PREFIX) :]
    raw = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
    return json.loads(AESGCM(base64.b64decode(_KEY_B64)).decrypt(raw[:12], raw[12:], None))['url']


@pytest.fixture(autouse=True)
def incy_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'INCY_CRYPTOLINK_ENABLED', True)
    monkeypatch.setattr(settings, 'INCY_CRYPTOLINK_PROVIDER_NAME', None)


class TestCabinetButtons:
    def test_incy_template_is_encrypted(self):
        resolved = _resolve_button_url('incy://import/{{SUBSCRIPTION_LINK}}', SUB_URL, None)
        assert decrypted_url(resolved) == SUB_URL

    def test_incy_app_deep_link_is_encrypted(self):
        app = {'urlScheme': 'incy://import/', 'usesCryptoLink': False}
        assert decrypted_url(_create_deep_link(app, SUB_URL, None)) == SUB_URL

    def test_happ_paths_are_unchanged(self):
        assert _resolve_button_url('{{HAPP_CRYPT4_LINK}}', SUB_URL, CRYPT5) == CRYPT5
        assert _resolve_button_url('happ://add/{{SUBSCRIPTION_LINK}}', SUB_URL, None) == f'happ://add/{SUB_URL}'
        app = {'urlScheme': 'happ://add/', 'usesCryptoLink': False}
        assert _create_deep_link(app, SUB_URL, None) == f'happ://add/{SUB_URL}'


class TestBotButtons:
    def test_incy_template_is_encrypted(self):
        assert decrypted_url(resolve_button_url('incy://import/{{SUBSCRIPTION_LINK}}', SUB_URL, None)) == SUB_URL

    def test_incy_app_deep_link_is_encrypted(self):
        assert decrypted_url(bot_create_deep_link({'urlScheme': 'incy://import/'}, SUB_URL)) == SUB_URL

    def test_happ_paths_are_unchanged(self):
        assert resolve_button_url('happ://crypt4/{{HAPP_CRYPT4_LINK}}', SUB_URL, CRYPT5) == CRYPT5
        assert bot_create_deep_link({'urlScheme': 'happ://add/'}, SUB_URL) == f'happ://add/{SUB_URL}'
