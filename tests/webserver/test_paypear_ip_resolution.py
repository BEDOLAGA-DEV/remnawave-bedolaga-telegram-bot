"""Regression tests for non-spoofable webhook client-IP resolution.

PayPear webhooks authenticate (as a fallback to the undocumented signature) against a source-IP
allowlist. The resolver must NOT trust attacker-settable X-Real-IP / X-Forwarded-For headers
from a direct public connection — otherwise a forged header could pass the allowlist and credit
fake payments. It MUST still honour forwarded headers set by a local/private reverse proxy so
genuine webhooks behind nginx keep working.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.webserver.payments import _resolve_proxied_client_ip


PAYPEAR_IP = '158.160.85.101'  # the single documented PayPear source IP


def _request(peer: str | None, headers: dict[str, str]):
    client = SimpleNamespace(host=peer) if peer else None
    return SimpleNamespace(client=client, headers=headers)


def test_direct_public_attacker_cannot_spoof_x_real_ip() -> None:
    req = _request('1.2.3.4', {'x-real-ip': PAYPEAR_IP})
    assert _resolve_proxied_client_ip(req) == '1.2.3.4'  # the real peer, not the spoofed header


def test_direct_public_attacker_cannot_spoof_x_forwarded_for() -> None:
    req = _request('1.2.3.4', {'x-forwarded-for': f'{PAYPEAR_IP}, 9.9.9.9'})
    assert _resolve_proxied_client_ip(req) == '1.2.3.4'


def test_legit_webhook_behind_local_proxy_uses_forwarded_header() -> None:
    # nginx on the same host (loopback peer) sets X-Real-IP to the true client.
    req = _request('127.0.0.1', {'x-real-ip': PAYPEAR_IP})
    assert _resolve_proxied_client_ip(req) == PAYPEAR_IP


def test_legit_webhook_behind_private_proxy_uses_forwarded_header() -> None:
    req = _request('10.0.0.5', {'x-forwarded-for': PAYPEAR_IP})
    assert _resolve_proxied_client_ip(req) == PAYPEAR_IP


def test_direct_paypear_connection_without_proxy() -> None:
    # No reverse proxy: PayPear connects directly, peer IS the source IP.
    req = _request(PAYPEAR_IP, {})
    assert _resolve_proxied_client_ip(req) == PAYPEAR_IP


def test_no_peer_falls_back_to_forwarded() -> None:
    req = _request(None, {'x-real-ip': PAYPEAR_IP})
    assert _resolve_proxied_client_ip(req) == PAYPEAR_IP


def test_malformed_peer_does_not_crash_and_does_not_trust_forwarded() -> None:
    req = _request('not-an-ip', {'x-real-ip': PAYPEAR_IP})
    # An unparseable peer is NOT a recognised local proxy, so the spoofable header is ignored
    # and the raw peer is returned (which then fails the allowlist — safe).
    assert _resolve_proxied_client_ip(req) == 'not-an-ip'
