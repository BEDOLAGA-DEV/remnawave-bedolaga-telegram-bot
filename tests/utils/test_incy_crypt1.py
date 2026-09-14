"""Tests for INCY crypt1 deep links (``incy://crypt1/...``).

The wire format is fixed by the INCY clients: AES-256-GCM over
base64url(``iv[12] || ciphertext || tag[16]``) with a compact, key-sorted JSON
payload. These tests decrypt with the same shared key, so a regression in the
payload shape or in the wrapper conditions fails here instead of on a user's phone.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.config import settings
from app.utils.incy_crypt1 import (
    _KEY_B64,
    INCY_CRYPT1_DEEP_LINK_PREFIX,
    encrypt_incy_link,
    wrap_incy_deep_link,
)


SUB_URL = 'https://panel.example/sub/abc123'
CRYPT5 = 'happ://crypt5/encrypted-payload'


def decrypt(link: str) -> dict:
    """Decrypt a crypt1 link the way the INCY client does."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    payload = link[len(INCY_CRYPT1_DEEP_LINK_PREFIX) :]
    raw = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
    plaintext = AESGCM(base64.b64decode(_KEY_B64)).decrypt(raw[:12], raw[12:], None)
    return json.loads(plaintext)


@pytest.fixture
def incy_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'INCY_CRYPTOLINK_ENABLED', True)
    monkeypatch.setattr(settings, 'INCY_CRYPTOLINK_PROVIDER_NAME', None)


class TestEncryptIncyLink:
    def test_roundtrip(self):
        link = encrypt_incy_link(SUB_URL)
        assert link.startswith(INCY_CRYPT1_DEEP_LINK_PREFIX)
        assert decrypt(link) == {'url': SUB_URL, 'v': 1}

    def test_provider_name_is_included(self):
        assert decrypt(encrypt_incy_link(SUB_URL, 'My VPN'))['n'] == 'My VPN'

    def test_provider_name_is_truncated(self):
        assert len(decrypt(encrypt_incy_link(SUB_URL, 'x' * 300))['n']) == 128

    def test_payload_keys_are_sorted_and_compact(self):
        # INCY builds the plaintext with sorted keys and no whitespace; keep parity
        # so both encoders produce byte-identical payloads for the same input.
        link = encrypt_incy_link(SUB_URL, 'My VPN')
        payload = link[len(INCY_CRYPT1_DEEP_LINK_PREFIX) :]
        raw = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        plaintext = AESGCM(base64.b64decode(_KEY_B64)).decrypt(raw[:12], raw[12:], None).decode()
        assert plaintext == f'{{"n":"My VPN","url":"{SUB_URL}","v":1}}'

    def test_nonce_is_not_reused(self):
        assert encrypt_incy_link(SUB_URL) != encrypt_incy_link(SUB_URL)

    def test_empty_url_returns_none(self):
        assert encrypt_incy_link('') is None


class TestWrapIncyDeepLink:
    def test_import_scheme_is_encrypted(self, incy_enabled):
        wrapped = wrap_incy_deep_link(f'incy://import/{SUB_URL}', SUB_URL)
        assert decrypt(wrapped) == {'url': SUB_URL, 'v': 1}

    def test_url_is_recovered_from_the_link_when_not_passed(self, incy_enabled):
        wrapped = wrap_incy_deep_link(f'incy://add/{SUB_URL}')
        assert decrypt(wrapped) == {'url': SUB_URL, 'v': 1}

    def test_other_apps_are_untouched(self, incy_enabled):
        assert wrap_incy_deep_link(f'happ://add/{SUB_URL}', SUB_URL) == f'happ://add/{SUB_URL}'
        assert wrap_incy_deep_link(CRYPT5, SUB_URL) == CRYPT5

    def test_already_encrypted_link_is_untouched(self, incy_enabled):
        already = f'{INCY_CRYPT1_DEEP_LINK_PREFIX}payload'
        assert wrap_incy_deep_link(already, SUB_URL) == already

    def test_unresolved_template_is_untouched(self, incy_enabled):
        # The caller checks for leftover {{ }} and skips the button in that case.
        assert wrap_incy_deep_link('incy://import/{{SUBSCRIPTION_LINK}}', None) == 'incy://import/{{SUBSCRIPTION_LINK}}'

    def test_non_http_payload_is_untouched(self, incy_enabled):
        assert wrap_incy_deep_link('incy://connect', None) == 'incy://connect'
        assert wrap_incy_deep_link('incy://routing/add/base64data', None) == 'incy://routing/add/base64data'

    @pytest.mark.parametrize(
        'link',
        [
            'incy://connect',
            'incy://routing/add/base64data',
            f'{INCY_CRYPT1_DEEP_LINK_PREFIX}payload',
        ],
    )
    def test_service_links_survive_a_known_subscription_url(self, incy_enabled, link):
        # Both call sites always pass subscription_url, so the guard must key off the
        # action in the link, not off whether a URL could be recovered from its tail:
        # otherwise every incy:// button in the config collapses into the same
        # import link and stops doing what it was added for.
        assert wrap_incy_deep_link(link, SUB_URL) == link

    def test_base64_payload_is_encrypted_from_the_known_url(self, incy_enabled):
        # isNeedBase64Encoding puts base64 in the tail; crypt1 wants the plain URL.
        assert decrypt(wrap_incy_deep_link('incy://import/YmFzZTY0', SUB_URL)) == {'url': SUB_URL, 'v': 1}

    def test_base64_payload_without_a_known_url_is_untouched(self, incy_enabled):
        assert wrap_incy_deep_link('incy://import/YmFzZTY0') == 'incy://import/YmFzZTY0'

    def test_disabled_setting_keeps_plain_link(self, monkeypatch):
        monkeypatch.setattr(settings, 'INCY_CRYPTOLINK_ENABLED', False)
        assert wrap_incy_deep_link(f'incy://import/{SUB_URL}', SUB_URL) == f'incy://import/{SUB_URL}'

    def test_empty_link_is_untouched(self, incy_enabled):
        assert wrap_incy_deep_link(None, SUB_URL) is None
        assert wrap_incy_deep_link('', SUB_URL) == ''

    def test_falls_back_to_plain_link_when_encryption_fails(self, incy_enabled, monkeypatch):
        monkeypatch.setattr('app.utils.incy_crypt1.encrypt_incy_link', lambda *_, **__: None)
        assert wrap_incy_deep_link(f'incy://import/{SUB_URL}', SUB_URL) == f'incy://import/{SUB_URL}'
