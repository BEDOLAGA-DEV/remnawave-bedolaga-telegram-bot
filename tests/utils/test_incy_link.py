import base64
import hashlib
import pytest

from app.utils.incy_link import (
    KEY_FINGERPRINT,
    decrypt_incy_link,
    encrypt_incy_link,
    encrypt_incy_link_deterministic,
    _derive_key,
)


def test_key_fingerprint_matches_published_library():
    fp = hashlib.sha256(_derive_key()).hexdigest()
    assert fp == 'b6bf708471cc90043232967660aade86a50b4e57929db2e53c5fa34db624c08c'
    assert fp == KEY_FINGERPRINT


def test_encrypt_prefix_and_no_padding():
    link = encrypt_incy_link('https://sub.example/abc', name='Demo')
    assert link.startswith('incy://crypt1/')
    payload = link[len('incy://crypt1/'):]
    assert '=' not in payload  # base64url, no padding
    assert '+' not in payload and '/' not in payload  # url-safe alphabet


def test_roundtrip_with_name():
    link = encrypt_incy_link('https://sub.example/abc123token', name='NoZapret VPN')
    out = decrypt_incy_link(link)
    assert out == {'url': 'https://sub.example/abc123token', 'name': 'NoZapret VPN'}


def test_roundtrip_without_name():
    link = encrypt_incy_link('https://sub.example/abc123token')
    out = decrypt_incy_link(link)
    assert out == {'url': 'https://sub.example/abc123token'}


def test_name_truncated_to_128_chars():
    long_name = 'x' * 200
    link = encrypt_incy_link('https://sub.example/abc', name=long_name)
    out = decrypt_incy_link(link)
    assert out['name'] == 'x' * 128


def test_deterministic_iv_is_reproducible():
    iv = bytes(range(12))
    a = encrypt_incy_link_deterministic('https://sub.example/abc', iv, name='Demo')
    b = encrypt_incy_link_deterministic('https://sub.example/abc', iv, name='Demo')
    assert a == b
    assert a.startswith('incy://crypt1/')


def test_empty_url_raises():
    with pytest.raises(ValueError):
        encrypt_incy_link('')
