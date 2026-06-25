"""Port of @incy/link-encoder `encryptLink` to Python.

Encodes an http(s) subscription URL into an ``incy://crypt1/<payload>`` deep
link that the INCY iOS/Android/Desktop clients decode. This is OBFUSCATION,
not security — the key is public (it ships inside every INCY client). It only
hides the subscription URL from casual scanners, exactly like HAPP crypt links.

Verified byte-for-byte against the published library: the derived key
fingerprint matches ``KEY_FINGERPRINT`` and an encrypt→decrypt roundtrip
reproduces the original payload.
"""

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.utils.incy_keymat import KEYMAT_A_B64, KEYMAT_B_B64

# Salt parts concatenated in order, fed into the SHA-256 KDF (see upstream).
_SALT = b'incy' + b'deep' + b'crypt1' + b'v2026.06'
_KEYMAT_A_OFFSET = 1024
_KEYMAT_B_OFFSET = 2048
_KEYMAT_LEN = 32

KEY_FINGERPRINT = 'b6bf708471cc90043232967660aade86a50b4e57929db2e53c5fa34db624c08c'
_SCHEME_PREFIX = 'incy://crypt1/'

_key_cache: bytes | None = None


def _derive_key() -> bytes:
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    a = base64.b64decode(KEYMAT_A_B64)
    b = base64.b64decode(KEYMAT_B_B64)
    if len(a) < _KEYMAT_A_OFFSET + _KEYMAT_LEN or len(b) < _KEYMAT_B_OFFSET + _KEYMAT_LEN:
        raise RuntimeError('incy_link: keymat assets are smaller than expected')
    km_a = a[_KEYMAT_A_OFFSET:_KEYMAT_A_OFFSET + _KEYMAT_LEN]
    km_b = b[_KEYMAT_B_OFFSET:_KEYMAT_B_OFFSET + _KEYMAT_LEN]
    key = hashlib.sha256(_SALT + km_a + km_b).digest()
    fp = hashlib.sha256(key).hexdigest()
    if fp != KEY_FINGERPRINT:
        raise RuntimeError(
            f'incy_link: derived key fingerprint mismatch (expected {KEY_FINGERPRINT}, '
            f'got {fp}) — keymat is out of sync with the published clients.'
        )
    _key_cache = key
    return key


def _b64url_encode(buf: bytes) -> str:
    return base64.urlsafe_b64encode(buf).rstrip(b'=').decode('ascii')


def _b64url_decode(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _build_payload(url: str, name: str | None) -> bytes:
    payload: dict = {'url': url, 'v': 1}
    if name:
        payload['n'] = name[:128]
    # Compact, UTF-8, keys sorted — matches the clients' canonical encoding.
    text = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return text.encode('utf-8')


def _encrypt(url: str, name: str | None, iv: bytes) -> str:
    if not url:
        raise ValueError('encrypt_incy_link: url must be a non-empty string')
    key = _derive_key()
    plaintext = _build_payload(url, name)
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)  # 16-byte tag appended
    wire = iv + ciphertext
    return f'{_SCHEME_PREFIX}{_b64url_encode(wire)}'


def encrypt_incy_link(url: str, name: str | None = None) -> str:
    """Encrypt an http(s) subscription URL into an ``incy://crypt1/...`` link."""
    return _encrypt(url, name, os.urandom(12))


def encrypt_incy_link_deterministic(url: str, iv: bytes, name: str | None = None) -> str:
    """Same as :func:`encrypt_incy_link` but with a caller-supplied 12-byte IV.

    For tests / reproducibility only — never reuse an IV in production.
    """
    if len(iv) != 12:
        raise ValueError('encrypt_incy_link_deterministic: iv must be 12 bytes')
    return _encrypt(url, name, iv)


def decrypt_incy_link(link: str) -> dict:
    """Decrypt an ``incy://crypt1/...`` link back to {'url', 'name'?}. Raises on
    malformed input or authentication failure. Used for tests/verification."""
    if not link or not link.startswith(_SCHEME_PREFIX):
        raise ValueError(f'decrypt_incy_link: expected {_SCHEME_PREFIX} prefix')
    wire = _b64url_decode(link[len(_SCHEME_PREFIX):].rstrip('/'))
    if len(wire) < 12 + 16 + 1:
        raise ValueError('decrypt_incy_link: payload too short')
    iv, rest = wire[:12], wire[12:]
    plaintext = AESGCM(_derive_key()).decrypt(iv, rest, None)
    parsed = json.loads(plaintext.decode('utf-8'))
    result = {'url': parsed['url']}
    if parsed.get('n'):
        result['name'] = parsed['n']
    return result
