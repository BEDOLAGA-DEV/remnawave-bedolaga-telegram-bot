# HAPP / INCY App Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user choose HAPP or INCY before connecting; INCY produces an `incy://crypt1/...` deep link (ported AES‑256‑GCM encoder) plus its own GitHub‑release‑driven download flow.

**Architecture:** A small parallel "app" step is inserted after the existing connect flow resolves a single subscription. The HAPP path reproduces current behavior unchanged (the whole `CONNECT_BUTTON_MODE` rendering is extracted into a helper). The INCY path is new: a vendored keymat + a pure encryptor produce the deep link; a release‑resolver service maps the latest `incy-platforms` GitHub release assets to download URLs. No DB schema change — app choice is ephemeral.

**Tech Stack:** Python 3.13, aiogram 3, SQLAlchemy async, `cryptography` (AESGCM, already a dep), aiohttp, pytest + pytest-asyncio, structlog.

**Spec:** `docs/superpowers/specs/2026-06-25-incy-app-selection-design.md`

**Run tests with:** `.venv/Scripts/python.exe -m pytest <path> -v` (bare `python` is 3.10 and cannot import `app`).

**Reference — verified port facts (from spec):**
- `K = SHA256(b"incy"+b"deep"+b"crypt1"+b"v2026.06" + assetA[1024:1056] + assetB[2048:2080])`
- `SHA256(K).hex() == "b6bf708471cc90043232967660aade86a50b4e57929db2e53c5fa34db624c08c"`
- payload = `json.dumps({"url":..,"v":1[,"n":name]}, sort_keys=True, separators=(',',':'), ensure_ascii=False)`
- wire = `iv(12) || AESGCM ciphertext+tag(16)` → base64url no padding → `incy://crypt1/<wire>`

---

## Task 1: Vendor INCY keymat blobs

**Files:**
- Create: `app/utils/incy_keymat.py`
- Test: `tests/utils/test_incy_keymat.py`

- [ ] **Step 1: Fetch the keymat source from the published library**

Run (writes a temp file you will read, then delete):
```bash
curl -sSL https://raw.githubusercontent.com/INCY-DEV/incy-link-encoder/main/src/keymat.ts -o _scratch_keymat.ts
```
Expected: `_scratch_keymat.ts` (~11 KB) containing two `export const KEYMAT_A_B64 = '...'` / `KEYMAT_B_B64 = '...'` lines.

- [ ] **Step 2: Create `app/utils/incy_keymat.py` with the two base64 constants**

Open `_scratch_keymat.ts`, copy the two base64 string literals **verbatim** into the module below (replace the `PASTE_..._HERE` placeholders with the exact strings between the single quotes; do not wrap or alter them):

```python
"""Vendored INCY keymat (auto-generated artifact, copied verbatim from
https://github.com/INCY-DEV/incy-link-encoder `src/keymat.ts`).

Two opaque 4096-byte blobs, base64-inlined. The link encoder derives the
AES-256-GCM key from fixed-offset slices of these. Do not edit by hand —
refresh from upstream `keymat.ts` if the published clients rotate the scheme
(the fingerprint check in incy_link.py will fail loudly if these drift).
"""

KEYMAT_A_B64 = 'PASTE_KEYMAT_A_B64_HERE'
KEYMAT_B_B64 = 'PASTE_KEYMAT_B_B64_HERE'
```

- [ ] **Step 3: Write the failing test**

`tests/utils/test_incy_keymat.py`:
```python
import base64

from app.utils.incy_keymat import KEYMAT_A_B64, KEYMAT_B_B64


def test_keymat_blobs_decode_to_4096_bytes():
    assert len(base64.b64decode(KEYMAT_A_B64)) == 4096
    assert len(base64.b64decode(KEYMAT_B_B64)) == 4096
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/utils/test_incy_keymat.py -v`
Expected: PASS (2 assertions). If FAIL on length, the paste was truncated — re-copy from `_scratch_keymat.ts`.

- [ ] **Step 5: Delete the scratch file**

Run:
```bash
rm -f _scratch_keymat.ts
```

- [ ] **Step 6: Commit**

```bash
git add app/utils/incy_keymat.py tests/utils/test_incy_keymat.py
git commit -m "feat(incy): vendor incy-link-encoder keymat blobs"
```

---

## Task 2: INCY link encryptor

**Files:**
- Create: `app/utils/incy_link.py`
- Test: `tests/utils/test_incy_link.py`

- [ ] **Step 1: Write the failing tests**

`tests/utils/test_incy_link.py`:
```python
import base64
import hashlib

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
    import pytest
    with pytest.raises(ValueError):
        encrypt_incy_link('')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/utils/test_incy_link.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.utils.incy_link'`.

- [ ] **Step 3: Write the implementation**

`app/utils/incy_link.py`:
```python
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

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.utils.incy_keymat import KEYMAT_A_B64, KEYMAT_B_B64

logger = structlog.get_logger(__name__)

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/utils/test_incy_link.py -v`
Expected: PASS (7 tests). The fingerprint test is the critical one.

- [ ] **Step 5: Commit**

```bash
git add app/utils/incy_link.py tests/utils/test_incy_link.py
git commit -m "feat(incy): port incy-link-encoder to Python (AES-256-GCM crypt1)"
```

---

## Task 3: Generalize the scheme-redirect helper

**Files:**
- Modify: `app/utils/subscription_utils.py:142-170` (`get_happ_cryptolink_redirect_link`)
- Test: `tests/utils/test_scheme_redirect_link.py`

- [ ] **Step 1: Write the failing test**

`tests/utils/test_scheme_redirect_link.py`:
```python
from app.utils.subscription_utils import build_scheme_redirect_link


def test_returns_none_when_template_empty():
    assert build_scheme_redirect_link('incy://crypt1/abc', None) is None
    assert build_scheme_redirect_link('incy://crypt1/abc', '') is None


def test_appends_url_encoded_link_when_template_ends_with_eq():
    out = build_scheme_redirect_link('incy://crypt1/a b', 'https://r.example/?redirect_to=')
    assert out == 'https://r.example/?redirect_to=incy%3A%2F%2Fcrypt1%2Fa%20b'


def test_substitutes_link_placeholder():
    out = build_scheme_redirect_link('incy://crypt1/abc', 'https://r.example/?to={link}')
    assert out == 'https://r.example/?to=incy%3A%2F%2Fcrypt1%2Fabc'


def test_returns_none_for_empty_deep_link():
    assert build_scheme_redirect_link('', 'https://r.example/?redirect_to=') is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/utils/test_scheme_redirect_link.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_scheme_redirect_link'`.

- [ ] **Step 3: Add the generalized helper and make the HAPP function delegate**

In `app/utils/subscription_utils.py`, replace the existing `get_happ_cryptolink_redirect_link` (lines ~142-170) with:

```python
def build_scheme_redirect_link(deep_link: str | None, template: str | None) -> str | None:
    """Wrap a custom-scheme deep link (happ://, incy://, ...) in an HTTP redirect.

    Telegram inline buttons reject custom URL schemes, so the deep link is
    handed to an HTTP redirect host that 302s to the scheme. ``template`` may use
    ``{link}``/``{subscription_link}`` placeholders (filled with the url-encoded
    deep link) or simply end with ``=``/``?``/``&`` to have the encoded link
    appended. Returns None when either argument is empty.
    """
    if not deep_link or not template:
        return None

    encoded_link = quote(deep_link, safe='')
    replacements = {
        '{subscription_link}': encoded_link,
        '{link}': encoded_link,
        '{subscription_link_raw}': deep_link,
        '{link_raw}': deep_link,
    }

    replaced = False
    for placeholder, value in replacements.items():
        if placeholder in template:
            template = template.replace(placeholder, value)
            replaced = True

    if replaced:
        return template
    return f'{template}{encoded_link}'


def get_happ_cryptolink_redirect_link(subscription_link: str | None) -> str | None:
    """Backward-compatible HAPP wrapper over :func:`build_scheme_redirect_link`."""
    template = settings.get_happ_cryptolink_redirect_template()
    return build_scheme_redirect_link(subscription_link, template)
```

(`quote` is already imported at the top of the file: `from urllib.parse import quote, urlparse, urlunparse`.)

- [ ] **Step 4: Run the new test AND the existing HAPP-related tests to verify no regression**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/utils/test_scheme_redirect_link.py tests/utils/test_get_display_subscription_link_override.py -v
```
Expected: PASS (new file passes; the existing override test still passes).

- [ ] **Step 5: Commit**

```bash
git add app/utils/subscription_utils.py tests/utils/test_scheme_redirect_link.py
git commit -m "refactor(subs): generalize redirect helper to build_scheme_redirect_link"
```

---

## Task 4: Config — INCY settings

**Files:**
- Modify: `app/config.py:981` (field block) and `app/config.py:2972` (accessors)
- Modify: `.env.example:1025`
- Modify: `app/services/system_settings_service.py` (category maps)
- Test: `tests/services/test_incy_config.py`

- [ ] **Step 1: Write the failing test**

`tests/services/test_incy_config.py`:
```python
from app.config import settings


def test_incy_defaults_ready_out_of_the_box():
    assert settings.get_incy_subscription_name() == 'INCY' or settings.get_incy_subscription_name()
    assert 'apps.apple.com' in settings.get_incy_ios_url()
    assert 'play.google.com' in settings.get_incy_android_url()
    assert settings.get_incy_platforms_repo() == 'INCY-DEV/incy-platforms'
    assert settings.get_incy_release_cache_ttl() >= 60


def test_incy_redirect_falls_back_to_happ(monkeypatch):
    monkeypatch.setattr(settings, 'INCY_CONNECT_REDIRECT_TEMPLATE', None, raising=False)
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_REDIRECT_TEMPLATE', 'https://r.example/?redirect_to=', raising=False)
    assert settings.get_incy_connect_redirect_template() == 'https://r.example/?redirect_to='

    monkeypatch.setattr(settings, 'INCY_CONNECT_REDIRECT_TEMPLATE', 'https://incy.example/?to=', raising=False)
    assert settings.get_incy_connect_redirect_template() == 'https://incy.example/?to='
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_incy_config.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'get_incy_subscription_name'`.

- [ ] **Step 3: Add the fields**

In `app/config.py`, immediately after line 981 (`HAPP_DOWNLOAD_LINK_PC: str | None = None`), add:

```python
    # ===== INCY =====
    INCY_SUBSCRIPTION_NAME: str = 'INCY'
    INCY_CONNECT_REDIRECT_TEMPLATE: str | None = None
    INCY_IOS_URL: str = 'https://apps.apple.com/us/app/incy/id6756943388'
    INCY_ANDROID_URL: str = 'https://play.google.com/store/apps/details?id=llc.itdev.incy'
    INCY_PLATFORMS_REPO: str = 'INCY-DEV/incy-platforms'
    INCY_RELEASE_CACHE_TTL: int = 21600
```

- [ ] **Step 4: Add the accessors**

In `app/config.py`, immediately after the `get_happ_download_link` method (ends at line ~2973), add:

```python
    def get_incy_subscription_name(self) -> str:
        name = (self.INCY_SUBSCRIPTION_NAME or '').strip()
        return name or 'INCY'

    def get_incy_connect_redirect_template(self) -> str | None:
        template = (self.INCY_CONNECT_REDIRECT_TEMPLATE or '').strip()
        if template:
            return template
        # Same redirect host serves any scheme — fall back to the HAPP template.
        return self.get_happ_cryptolink_redirect_template()

    def get_incy_ios_url(self) -> str | None:
        return (self.INCY_IOS_URL or '').strip() or None

    def get_incy_android_url(self) -> str | None:
        return (self.INCY_ANDROID_URL or '').strip() or None

    def get_incy_platforms_repo(self) -> str:
        repo = (self.INCY_PLATFORMS_REPO or '').strip()
        return repo or 'INCY-DEV/incy-platforms'

    def get_incy_release_cache_ttl(self) -> int:
        try:
            ttl = int(self.INCY_RELEASE_CACHE_TTL)
        except (TypeError, ValueError):
            ttl = 21600
        return max(60, ttl)
```

- [ ] **Step 5: Add the `.env.example` block**

In `.env.example`, immediately after line 1025 (`HAPP_CRYPTOLINK_REDIRECT_TEMPLATE=`), add:

```bash

# ===== INCY =====
# Имя подписки, которое INCY показывает в окне импорта
INCY_SUBSCRIPTION_NAME=INCY
# Редирект для кнопки "Подключиться" INCY (incy:// схему тг не поддерживает).
# Если пусто — используется HAPP_CRYPTOLINK_REDIRECT_TEMPLATE. Пример: https://sub.domain.sub/redirect-page/?redirect_to=
INCY_CONNECT_REDIRECT_TEMPLATE=
# Ссылки на магазины (по умолчанию официальные INCY)
INCY_IOS_URL=https://apps.apple.com/us/app/incy/id6756943388
INCY_ANDROID_URL=https://play.google.com/store/apps/details?id=llc.itdev.incy
# Репозиторий с релизами десктоп-сборок и TTL кеша (сек)
INCY_PLATFORMS_REPO=INCY-DEV/incy-platforms
INCY_RELEASE_CACHE_TTL=21600
```

- [ ] **Step 6: Register the INCY settings category for the admin panel**

In `app/services/system_settings_service.py`:
- In the category-prefix override map (near the `'HAPP_': 'HAPP'` entry, ~line 408) add: `'INCY_': 'INCY',`
- In `CATEGORY_TITLES` (near the `'HAPP'` entry, ~line 126) add: `'INCY': '⚡ INCY',`
- In `CATEGORY_DESCRIPTIONS` (near the `'HAPP'` entry, ~line 197) add: `'INCY': 'Интеграция INCY и ссылки на скачивание.',`

(If the exact dict locations differ, search for the string `'HAPP'` in that file and add the `'INCY'` sibling entry in the same three dicts.)

- [ ] **Step 7: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_incy_config.py -v`
Expected: PASS (2 tests).

- [ ] **Step 8: Commit**

```bash
git add app/config.py .env.example app/services/system_settings_service.py tests/services/test_incy_config.py
git commit -m "feat(incy): add INCY config settings and accessors"
```

---

## Task 5: INCY release resolver service

**Files:**
- Create: `app/services/incy_release_service.py`
- Test: `tests/services/test_incy_release_service.py`

- [ ] **Step 1: Write the failing tests**

`tests/services/test_incy_release_service.py`:
```python
import pytest

from app.services import incy_release_service as svc


def _fake_release():
    base = 'https://github.com/INCY-DEV/incy-platforms/releases/download/desktop-v3.2.3'
    names = [
        'incy-windows-setup.exe',
        'incy-macos-arm64.dmg',
        'incy-macos-intel.dmg',
        'incy-linux-arm64.deb',
        'incy-linux-arm64.rpm',
        'incy-linux-arm64-portable.zip',
        'incy-linux-x64.deb',
        'incy-linux-x64.rpm',
        'incy-linux-x64-portable.zip',
        'some-unrelated-asset.txt',
    ]
    return {
        'tag_name': 'desktop-v3.2.3',
        'assets': [{'name': n, 'browser_download_url': f'{base}/{n}'} for n in names],
    }


def test_build_asset_map_matches_known_filenames():
    m = svc._build_asset_map(_fake_release())
    assert m['windows'].endswith('incy-windows-setup.exe')
    assert m['macos:arm'].endswith('incy-macos-arm64.dmg')
    assert m['macos:intel'].endswith('incy-macos-intel.dmg')
    assert m['linux:arm:deb'].endswith('incy-linux-arm64.deb')
    assert m['linux:arm:rpm'].endswith('incy-linux-arm64.rpm')
    assert m['linux:arm:portable'].endswith('incy-linux-arm64-portable.zip')
    assert m['linux:x64:deb'].endswith('incy-linux-x64.deb')
    assert m['linux:x64:rpm'].endswith('incy-linux-x64.rpm')
    assert m['linux:x64:portable'].endswith('incy-linux-x64-portable.zip')
    # Unrelated asset is ignored
    assert all('unrelated' not in v for v in m.values())


def test_build_asset_map_empty_on_missing_assets():
    assert svc._build_asset_map({'assets': []}) == {}
    assert svc._build_asset_map({}) == {}


@pytest.mark.asyncio
async def test_get_incy_desktop_assets_caches_and_falls_back(monkeypatch):
    svc._reset_cache_for_tests()
    calls = {'n': 0}

    async def fake_fetch():
        calls['n'] += 1
        return _fake_release()

    monkeypatch.setattr(svc, '_fetch_latest_release_json', fake_fetch)

    m1 = await svc.get_incy_desktop_assets()
    m2 = await svc.get_incy_desktop_assets()  # served from cache
    assert m1 == m2
    assert m1['windows'].endswith('incy-windows-setup.exe')
    assert calls['n'] == 1  # second call hit the cache

    # On fetch failure, the stale cache is returned (no crash)
    async def boom():
        raise RuntimeError('github down')

    monkeypatch.setattr(svc, '_fetch_latest_release_json', boom)
    svc._expire_cache_for_tests()
    m3 = await svc.get_incy_desktop_assets()
    assert m3 == m1


@pytest.mark.asyncio
async def test_get_incy_desktop_assets_empty_when_no_cache_and_fetch_fails(monkeypatch):
    svc._reset_cache_for_tests()

    async def boom():
        raise RuntimeError('github down')

    monkeypatch.setattr(svc, '_fetch_latest_release_json', boom)
    assert await svc.get_incy_desktop_assets() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_incy_release_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.incy_release_service'`.

- [ ] **Step 3: Write the implementation**

`app/services/incy_release_service.py`:
```python
"""Resolve INCY desktop installer URLs from the latest incy-platforms release.

Fetches the GitHub "releases/latest" JSON, maps known asset filenames to
platform/arch/pkg keys, and caches the result in memory with a TTL. On a GitHub
error (timeout / rate limit / non-200) the last cached value is returned if
present, else an empty map — never raises to the handler. Pattern mirrors
``app/services/version_service.py``.
"""

import time

import aiohttp
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# Exact upstream asset filename -> internal key.
_FILENAME_TO_KEY: dict[str, str] = {
    'incy-windows-setup.exe': 'windows',
    'incy-macos-arm64.dmg': 'macos:arm',
    'incy-macos-intel.dmg': 'macos:intel',
    'incy-linux-arm64.deb': 'linux:arm:deb',
    'incy-linux-arm64.rpm': 'linux:arm:rpm',
    'incy-linux-arm64-portable.zip': 'linux:arm:portable',
    'incy-linux-x64.deb': 'linux:x64:deb',
    'incy-linux-x64.rpm': 'linux:x64:rpm',
    'incy-linux-x64-portable.zip': 'linux:x64:portable',
}

_cache: dict[str, str] | None = None
_cache_ts: float = 0.0


def _reset_cache_for_tests() -> None:
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0


def _expire_cache_for_tests() -> None:
    global _cache_ts
    _cache_ts = 0.0


def _build_asset_map(release_json: dict) -> dict[str, str]:
    assets = (release_json or {}).get('assets') or []
    result: dict[str, str] = {}
    for asset in assets:
        key = _FILENAME_TO_KEY.get(asset.get('name'))
        if key and asset.get('browser_download_url'):
            result[key] = asset['browser_download_url']
    return result


async def _fetch_latest_release_json() -> dict:
    repo = settings.get_incy_platforms_repo()
    url = f'https://api.github.com/repos/{repo}/releases/latest'
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
        if response.status != 200:
            raise RuntimeError(f'GitHub API status {response.status}')
        return await response.json()


async def get_incy_desktop_assets(force: bool = False) -> dict[str, str]:
    """Return {platform-key: download_url}. Cached for INCY_RELEASE_CACHE_TTL."""
    global _cache, _cache_ts
    ttl = settings.get_incy_release_cache_ttl()
    if not force and _cache is not None and (time.monotonic() - _cache_ts) < ttl:
        return _cache
    try:
        data = await _fetch_latest_release_json()
        _cache = _build_asset_map(data)
        _cache_ts = time.monotonic()
        logger.info('INCY release resolved', tag=data.get('tag_name'), assets=len(_cache))
        return _cache
    except Exception as e:  # noqa: BLE001 - resolver must never crash the handler
        logger.warning('INCY release fetch failed', error=str(e))
        return _cache if _cache is not None else {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_incy_release_service.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/incy_release_service.py tests/services/test_incy_release_service.py
git commit -m "feat(incy): add GitHub release resolver for desktop downloads"
```

---

## Task 6: Localization keys (INCY_*)

**Files:**
- Modify: `app/localization/locales/{ru,en,ua,fa,zh}.json`
- Modify: `locales/{ru,en,ua,fa,zh}.json`
- Test: `tests/localization/test_incy_keys_present.py`

- [ ] **Step 1: Write the failing test**

`tests/localization/test_incy_keys_present.py`:
```python
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_KEYS = [
    'APP_CHOICE_PROMPT',
    'APP_CHOICE_HAPP',
    'APP_CHOICE_INCY',
    'INCY_CONNECT_TITLE',
    'INCY_CONNECT_HINT',
    'INCY_DOWNLOAD_BUTTON',
    'INCY_DOWNLOAD_PROMPT',
    'INCY_DOWNLOAD_OPEN_LINK',
    'INCY_DOWNLOAD_LINK_NOT_SET',
    'INCY_PLATFORM_ANDROID',
    'INCY_PLATFORM_IOS',
    'INCY_PLATFORM_WINDOWS',
    'INCY_PLATFORM_MACOS',
    'INCY_PLATFORM_LINUX',
    'INCY_ARCH_ARM',
    'INCY_ARCH_X64',
    'INCY_ARCH_APPLE_SILICON',
    'INCY_ARCH_INTEL',
    'INCY_PKG_DEB',
    'INCY_PKG_RPM',
    'INCY_PKG_PORTABLE',
]

LOCALE_FILES = [
    ROOT / 'app' / 'localization' / 'locales' / 'ru.json',
    ROOT / 'locales' / 'ru.json',
]


def test_required_incy_keys_present_in_ru_locales():
    for path in LOCALE_FILES:
        data = json.loads(path.read_text(encoding='utf-8'))
        missing = [k for k in REQUIRED_KEYS if k not in data]
        assert not missing, f'{path} missing keys: {missing}'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/localization/test_incy_keys_present.py -v`
Expected: FAIL listing all missing keys for `app/localization/locales/ru.json`.

- [ ] **Step 3: Add the keys to `app/localization/locales/ru.json` and `locales/ru.json`**

Add these entries (alphabetically near the `HAPP_*` block, valid JSON — mind trailing commas) to **both** `app/localization/locales/ru.json` and `locales/ru.json`:

```json
"APP_CHOICE_PROMPT": "📲 <b>Выберите приложение</b>\nКакое приложение вы используете?",
"APP_CHOICE_HAPP": "Happ",
"APP_CHOICE_INCY": "INCY",
"INCY_CONNECT_TITLE": "🔗 <b>Подключение через INCY</b>",
"INCY_CONNECT_HINT": "💡 Нажмите ссылку, чтобы открыть INCY, или скопируйте её вручную:",
"INCY_DOWNLOAD_BUTTON": "⬇️ Скачать INCY",
"INCY_DOWNLOAD_PROMPT": "📥 <b>Скачать INCY</b>\nВыберите вашу платформу:",
"INCY_DOWNLOAD_OPEN_LINK": "🔗 Открыть ссылку",
"INCY_DOWNLOAD_LINK_NOT_SET": "❌ Ссылка для этой платформы временно недоступна",
"INCY_PLATFORM_ANDROID": "🤖 Android",
"INCY_PLATFORM_IOS": "🍎 iOS",
"INCY_PLATFORM_WINDOWS": "💻 Windows",
"INCY_PLATFORM_MACOS": "🖥️ macOS",
"INCY_PLATFORM_LINUX": "🐧 Linux",
"INCY_ARCH_ARM": "ARM",
"INCY_ARCH_X64": "x64",
"INCY_ARCH_APPLE_SILICON": "🍏 Apple Silicon",
"INCY_ARCH_INTEL": "💠 Intel",
"INCY_PKG_DEB": "DEB",
"INCY_PKG_RPM": "RPM",
"INCY_PKG_PORTABLE": "Portable (zip)",
```

- [ ] **Step 4: Add the same keys (translated) to the other locales**

Add the same JSON keys to `en.json`, `ua.json`, `fa.json`, `zh.json` in **both** directories. Translate the text values; keep emoji and `{platform}`-style placeholders identical. English example for the message keys (button/arch labels can stay as-is):

```json
"APP_CHOICE_PROMPT": "📲 <b>Choose your app</b>\nWhich app do you use?",
"INCY_CONNECT_TITLE": "🔗 <b>Connect via INCY</b>",
"INCY_CONNECT_HINT": "💡 Tap the link to open INCY, or copy it manually:",
"INCY_DOWNLOAD_BUTTON": "⬇️ Download INCY",
"INCY_DOWNLOAD_PROMPT": "📥 <b>Download INCY</b>\nChoose your platform:",
"INCY_DOWNLOAD_OPEN_LINK": "🔗 Open link",
"INCY_DOWNLOAD_LINK_NOT_SET": "❌ Link for this platform is temporarily unavailable",
```

(For `ua`/`fa`/`zh`, translate equivalently. The presence test only checks `ru.json`; translations for other languages should still be added for completeness.)

- [ ] **Step 5: Run test + JSON validity check**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/localization/test_incy_keys_present.py -v
.venv/Scripts/python.exe -c "import json,glob; [json.load(open(f,encoding='utf-8')) for f in glob.glob('app/localization/locales/*.json')+glob.glob('locales/*.json')]; print('all locale JSON valid')"
```
Expected: PASS, then `all locale JSON valid` (no JSONDecodeError).

- [ ] **Step 6: Commit**

```bash
git add app/localization/locales/*.json locales/*.json tests/localization/test_incy_keys_present.py
git commit -m "feat(incy): add INCY/app-choice localization keys"
```

---

## Task 7: Keyboards (app choice + INCY download tree)

**Files:**
- Modify: `app/keyboards/inline.py` (add new functions near `get_happ_download_platform_keyboard`, ~line 1094)
- Test: `tests/keyboards/test_incy_keyboards.py`

- [ ] **Step 1: Write the failing test**

`tests/keyboards/test_incy_keyboards.py`:
```python
from app.keyboards.inline import (
    get_app_choice_keyboard,
    get_incy_download_platform_keyboard,
    get_incy_download_macos_keyboard,
    get_incy_download_linux_arch_keyboard,
    get_incy_download_linux_pkg_keyboard,
    get_incy_download_link_keyboard,
)


def _all_callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_app_choice_keyboard_has_happ_and_incy_with_sub_id():
    kb = get_app_choice_keyboard('ru', sub_id=7)
    cbs = _all_callbacks(kb)
    assert 'nz!_capp:happ:7' in cbs
    assert 'nz!_capp:incy:7' in cbs


def test_app_choice_keyboard_without_sub_id():
    kb = get_app_choice_keyboard('ru', sub_id=None)
    cbs = _all_callbacks(kb)
    assert 'nz!_capp:happ' in cbs
    assert 'nz!_capp:incy' in cbs


def test_incy_platform_keyboard_callbacks():
    cbs = _all_callbacks(get_incy_download_platform_keyboard('ru'))
    for c in ['nz!_incy_dl:android', 'nz!_incy_dl:ios', 'nz!_incy_dl:windows',
              'nz!_incy_dl:macos', 'nz!_incy_dl:linux']:
        assert c in cbs


def test_incy_macos_keyboard_callbacks():
    cbs = _all_callbacks(get_incy_download_macos_keyboard('ru'))
    assert 'nz!_incy_dl:macos:arm' in cbs
    assert 'nz!_incy_dl:macos:intel' in cbs


def test_incy_linux_arch_and_pkg_callbacks():
    arch_cbs = _all_callbacks(get_incy_download_linux_arch_keyboard('ru'))
    assert 'nz!_incy_dl:linux:arm' in arch_cbs
    assert 'nz!_incy_dl:linux:x64' in arch_cbs

    pkg_cbs = _all_callbacks(get_incy_download_linux_pkg_keyboard('ru', 'x64'))
    assert 'nz!_incy_dl:linux:x64:deb' in pkg_cbs
    assert 'nz!_incy_dl:linux:x64:rpm' in pkg_cbs
    assert 'nz!_incy_dl:linux:x64:portable' in pkg_cbs


def test_incy_link_keyboard_has_url_button():
    kb = get_incy_download_link_keyboard('ru', 'https://example/file.dmg')
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert 'https://example/file.dmg' in urls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/keyboards/test_incy_keyboards.py -v`
Expected: FAIL with `ImportError: cannot import name 'get_app_choice_keyboard'`.

- [ ] **Step 3: Add the keyboard functions**

In `app/keyboards/inline.py`, after `get_happ_download_link_keyboard` (ends ~line 1122), add:

```python
def get_app_choice_keyboard(language: str, sub_id: int | None = None) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    suffix = f':{sub_id}' if sub_id is not None else ''
    back_cb = f'nz!_sm:{sub_id}' if (sub_id is not None and settings.is_multi_tariff_enabled()) else 'nz!_menu_subscription'
    buttons = [
        [InlineKeyboardButton(text=texts.t('APP_CHOICE_HAPP', 'Happ'), callback_data=f'nz!_capp:happ{suffix}', style='primary')],
        [InlineKeyboardButton(text=texts.t('APP_CHOICE_INCY', 'INCY'), callback_data=f'nz!_capp:incy{suffix}', style='primary')],
        [InlineKeyboardButton(text=texts.BACK, callback_data=back_cb, style='danger')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_incy_download_platform_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = [
        [InlineKeyboardButton(text=texts.t('INCY_PLATFORM_ANDROID', '🤖 Android'), callback_data='nz!_incy_dl:android', style='primary')],
        [InlineKeyboardButton(text=texts.t('INCY_PLATFORM_IOS', '🍎 iOS'), callback_data='nz!_incy_dl:ios', style='primary')],
        [InlineKeyboardButton(text=texts.t('INCY_PLATFORM_WINDOWS', '💻 Windows'), callback_data='nz!_incy_dl:windows', style='primary')],
        [InlineKeyboardButton(text=texts.t('INCY_PLATFORM_MACOS', '🖥️ macOS'), callback_data='nz!_incy_dl:macos', style='primary')],
        [InlineKeyboardButton(text=texts.t('INCY_PLATFORM_LINUX', '🐧 Linux'), callback_data='nz!_incy_dl:linux', style='primary')],
        [InlineKeyboardButton(text=texts.BACK, callback_data='nz!_incy_dl_close', style='danger')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_incy_download_macos_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = [
        [InlineKeyboardButton(text=texts.t('INCY_ARCH_APPLE_SILICON', '🍏 Apple Silicon'), callback_data='nz!_incy_dl:macos:arm', style='primary')],
        [InlineKeyboardButton(text=texts.t('INCY_ARCH_INTEL', '💠 Intel'), callback_data='nz!_incy_dl:macos:intel', style='primary')],
        [InlineKeyboardButton(text=texts.BACK, callback_data='nz!_incy_dl', style='danger')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_incy_download_linux_arch_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = [
        [InlineKeyboardButton(text=texts.t('INCY_ARCH_ARM', 'ARM'), callback_data='nz!_incy_dl:linux:arm', style='primary')],
        [InlineKeyboardButton(text=texts.t('INCY_ARCH_X64', 'x64'), callback_data='nz!_incy_dl:linux:x64', style='primary')],
        [InlineKeyboardButton(text=texts.BACK, callback_data='nz!_incy_dl', style='danger')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_incy_download_linux_pkg_keyboard(language: str, arch: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = [
        [InlineKeyboardButton(text=texts.t('INCY_PKG_DEB', 'DEB'), callback_data=f'nz!_incy_dl:linux:{arch}:deb', style='primary')],
        [InlineKeyboardButton(text=texts.t('INCY_PKG_RPM', 'RPM'), callback_data=f'nz!_incy_dl:linux:{arch}:rpm', style='primary')],
        [InlineKeyboardButton(text=texts.t('INCY_PKG_PORTABLE', 'Portable (zip)'), callback_data=f'nz!_incy_dl:linux:{arch}:portable', style='primary')],
        [InlineKeyboardButton(text=texts.BACK, callback_data='nz!_incy_dl:linux', style='danger')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_incy_download_link_keyboard(language: str, link: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = [
        [InlineKeyboardButton(text=texts.t('INCY_DOWNLOAD_OPEN_LINK', '🔗 Открыть ссылку'), url=link, style='success')],
        [InlineKeyboardButton(text=texts.BACK, callback_data='nz!_incy_dl', style='danger')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
```

(`settings`, `InlineKeyboardButton`, `InlineKeyboardMarkup`, `get_texts`, `DEFAULT_LANGUAGE` are already imported at the top of `inline.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/keyboards/test_incy_keyboards.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/keyboards/inline.py tests/keyboards/test_incy_keyboards.py
git commit -m "feat(incy): add app-choice and INCY download keyboards"
```

---

## Task 8: INCY connect + download handlers

**Files:**
- Create: `app/handlers/subscription/incy.py`
- Test: `tests/handlers/test_incy_handlers.py`

- [ ] **Step 1: Write the failing tests**

`tests/handlers/test_incy_handlers.py`:
```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
import app.handlers.subscription.incy as incy


def _callback(data):
    msg = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock(), delete=AsyncMock())
    return SimpleNamespace(data=data, message=msg, answer=AsyncMock())


def _user():
    return SimpleNamespace(id=1, language='ru')


@pytest.mark.asyncio
async def test_incy_connect_uses_plain_override_url_not_crypt5(monkeypatch):
    monkeypatch.setattr(settings, 'SUBSCRIPTION_DOMAIN_OVERRIDE', 'cdn.example.com', raising=False)
    monkeypatch.setattr(settings, 'INCY_CONNECT_REDIRECT_TEMPLATE', 'https://r.example/?redirect_to=', raising=False)

    sub = SimpleNamespace(
        id=5,
        subscription_url='https://old.host/tok',
        subscription_crypto_link='happ://crypt5SHOULD_NOT_BE_USED',
    )

    async def fake_resolve(callback, db_user, db, state=None):
        return sub, 5

    monkeypatch.setattr(incy, 'resolve_subscription_from_context', fake_resolve)

    captured = {}

    def fake_encrypt(url, name=None):
        captured['url'] = url
        captured['name'] = name
        return 'incy://crypt1/PAYLOAD'

    monkeypatch.setattr(incy, 'encrypt_incy_link', fake_encrypt)

    cb = _callback('nz!_capp:incy:5')
    await incy.handle_connect_incy(cb, _user(), db=None, state=None)

    # Must encrypt the override-applied PLAIN url, never the crypt5 link
    assert captured['url'] == 'https://cdn.example.com/tok'
    cb.message.answer.assert_awaited()  # message shown


@pytest.mark.asyncio
async def test_incy_download_windows_resolves_release(monkeypatch):
    async def fake_assets(force=False):
        return {'windows': 'https://gh/incy-windows-setup.exe'}

    monkeypatch.setattr(incy, 'get_incy_desktop_assets', fake_assets)

    cb = _callback('nz!_incy_dl:windows')
    await incy.handle_incy_download(cb, _user(), db=None, state=None)
    cb.message.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_incy_download_android_uses_store_url(monkeypatch):
    monkeypatch.setattr(settings, 'INCY_ANDROID_URL', 'https://play.google.com/x', raising=False)
    cb = _callback('nz!_incy_dl:android')
    await incy.handle_incy_download(cb, _user(), db=None, state=None)
    cb.message.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_incy_download_macos_menu(monkeypatch):
    cb = _callback('nz!_incy_dl:macos')
    await incy.handle_incy_download(cb, _user(), db=None, state=None)
    cb.message.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_incy_download_missing_asset_alerts(monkeypatch):
    async def fake_assets(force=False):
        return {}  # nothing resolved

    monkeypatch.setattr(incy, 'get_incy_desktop_assets', fake_assets)
    cb = _callback('nz!_incy_dl:windows')
    await incy.handle_incy_download(cb, _user(), db=None, state=None)
    cb.answer.assert_awaited()  # show_alert path
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_incy_handlers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.handlers.subscription.incy'`.

- [ ] **Step 3: Write the implementation**

`app/handlers/subscription/incy.py`:
```python
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.config import settings
from app.database.models import User
from app.keyboards.inline import (
    get_incy_download_linux_arch_keyboard,
    get_incy_download_linux_pkg_keyboard,
    get_incy_download_link_keyboard,
    get_incy_download_macos_keyboard,
    get_incy_download_platform_keyboard,
)
from app.localization.texts import get_texts
from app.services.incy_release_service import get_incy_desktop_assets
from app.utils.incy_link import encrypt_incy_link
from app.utils.subscription_utils import (
    apply_subscription_domain_override,
    build_scheme_redirect_link,
)

from .common import resolve_subscription_from_context

logger = structlog.get_logger(__name__)


async def handle_connect_incy(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    """Render the INCY connect screen: tappable deep link + copy block + redirect."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    subscription, sub_id = await resolve_subscription_from_context(callback, db_user, db, state)
    if subscription is None:
        await callback.answer(
            texts.t('SUBSCRIPTION_LINK_UNAVAILABLE', '❌ Ссылка подписки недоступна'),
            show_alert=True,
        )
        return

    plain_url = apply_subscription_domain_override(getattr(subscription, 'subscription_url', None))
    if not plain_url:
        await callback.answer(
            texts.t('SUBSCRIPTION_NO_ACTIVE_LINK', '⚠ У вас нет активной подписки или ссылка еще генерируется'),
            show_alert=True,
        )
        return

    deep_link = encrypt_incy_link(plain_url, name=settings.get_incy_subscription_name())
    redirect = build_scheme_redirect_link(deep_link, settings.get_incy_connect_redirect_template())

    message_text = (
        texts.t('INCY_CONNECT_TITLE', '🔗 <b>Подключение через INCY</b>')
        + '\n\n'
        + f'<a href="{deep_link}">INCY</a>'
        + '\n\n'
        + texts.t('INCY_CONNECT_HINT', '💡 Нажмите ссылку, чтобы открыть INCY, или скопируйте её вручную:')
        + '\n\n'
        + f'<blockquote expandable><code>{deep_link}</code></blockquote>'
    )

    rows: list[list[InlineKeyboardButton]] = []
    if redirect:
        rows.append([InlineKeyboardButton(text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'), url=redirect, style='success')])
    rows.append([InlineKeyboardButton(text=texts.t('INCY_DOWNLOAD_BUTTON', '⬇️ Скачать INCY'), callback_data='nz!_incy_dl', style='primary')])
    back_cb = f'nz!_sm:{sub_id}' if (sub_id is not None and settings.is_multi_tariff_enabled()) else 'nz!_menu_subscription'
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_cb, style='danger')])

    await callback.message.answer(
        message_text,
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


async def handle_incy_download(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    """Drive the INCY per-platform download tree.

    callback.data shape: ``nz!_incy_dl[:<platform>[:<arch>[:<pkg>]]]``.
    """
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    data = callback.data or 'nz!_incy_dl'

    if data == 'nz!_incy_dl_close':
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer()
        return

    parts = data.split(':')  # ['nz!_incy_dl', platform?, arch?, pkg?]
    segments = parts[1:]

    # Entry — show platform menu.
    if not segments:
        await callback.message.edit_text(
            texts.t('INCY_DOWNLOAD_PROMPT', '📥 <b>Скачать INCY</b>\nВыберите вашу платформу:'),
            reply_markup=get_incy_download_platform_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    platform = segments[0]

    # macOS / Linux intermediate menus
    if platform == 'macos' and len(segments) == 1:
        await callback.message.edit_text(
            texts.t('INCY_DOWNLOAD_PROMPT', '📥 <b>Скачать INCY</b>\nВыберите вашу платформу:'),
            reply_markup=get_incy_download_macos_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    if platform == 'linux' and len(segments) == 1:
        await callback.message.edit_text(
            texts.t('INCY_DOWNLOAD_PROMPT', '📥 <b>Скачать INCY</b>\nВыберите вашу платформу:'),
            reply_markup=get_incy_download_linux_arch_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    if platform == 'linux' and len(segments) == 2:
        arch = segments[1]
        await callback.message.edit_text(
            texts.t('INCY_DOWNLOAD_PROMPT', '📥 <b>Скачать INCY</b>\nВыберите вашу платформу:'),
            reply_markup=get_incy_download_linux_pkg_keyboard(db_user.language, arch),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Leaf nodes -> resolve a URL.
    link = await _resolve_incy_download_url(segments)
    if not link:
        await callback.answer(
            texts.t('INCY_DOWNLOAD_LINK_NOT_SET', '❌ Ссылка для этой платформы временно недоступна'),
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        texts.t('INCY_DOWNLOAD_PROMPT', '📥 <b>Скачать INCY</b>\nВыберите вашу платформу:'),
        reply_markup=get_incy_download_link_keyboard(db_user.language, link),
        parse_mode='HTML',
    )
    await callback.answer()


async def _resolve_incy_download_url(segments: list[str]) -> str | None:
    """Map a leaf callback path to a download URL (store links or release asset)."""
    platform = segments[0]
    if platform == 'android':
        return settings.get_incy_android_url()
    if platform == 'ios':
        return settings.get_incy_ios_url()

    assets = await get_incy_desktop_assets()
    key = ':'.join(segments)  # e.g. 'windows', 'macos:arm', 'linux:x64:rpm'
    return assets.get(key)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_incy_handlers.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/handlers/subscription/incy.py tests/handlers/test_incy_handlers.py
git commit -m "feat(incy): add INCY connect and download handlers"
```

---

## Task 9: Wire app-choice into the connect flow + register handlers

**Files:**
- Modify: `app/handlers/subscription/links.py` (`handle_connect_subscription`, add `handle_connect_app_happ`)
- Modify: `app/handlers/subscription/__init__.py` (exports)
- Modify: `app/handlers/subscription/purchase.py` (imports ~168, registrations ~4310)
- Test: `tests/handlers/test_app_choice_routing.py`

- [ ] **Step 1: Write the failing tests**

`tests/handlers/test_app_choice_routing.py`:
```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
import app.handlers.subscription.links as links


def _callback(data):
    msg = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    return SimpleNamespace(data=data, message=msg, answer=AsyncMock())


def _user():
    return SimpleNamespace(id=1, language='ru')


@pytest.mark.asyncio
async def test_connect_shows_app_choice(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink', raising=False)
    monkeypatch.setattr(settings, 'is_multi_tariff_enabled', lambda: False, raising=False)

    sub = SimpleNamespace(id=5, subscription_url='https://h/tok', subscription_crypto_link='happ://crypt5x')

    async def fake_resolve(callback, db_user, db, state=None):
        return sub, 5

    monkeypatch.setattr(links, 'resolve_subscription_from_context', fake_resolve)
    monkeypatch.setattr(links, 'get_display_subscription_link', lambda s: 'happ://crypt5x')

    cb = _callback('nz!_subscription_connect')
    await links.handle_connect_subscription(cb, _user(), db=None, state=None)

    # The app-choice keyboard must be rendered with both apps
    cb.message.edit_text.assert_awaited()
    _, kwargs = cb.message.edit_text.call_args
    markup = kwargs['reply_markup']
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert any(c.startswith('nz!_capp:happ') for c in cbs)
    assert any(c.startswith('nz!_capp:incy') for c in cbs)


@pytest.mark.asyncio
async def test_app_happ_renders_existing_connect_ui(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink', raising=False)
    monkeypatch.setattr(settings, 'is_multi_tariff_enabled', lambda: False, raising=False)

    sub = SimpleNamespace(id=5, subscription_url='https://h/tok', subscription_crypto_link='happ://crypt5x')

    async def fake_resolve(callback, db_user, db, state=None):
        return sub, 5

    monkeypatch.setattr(links, 'resolve_subscription_from_context', fake_resolve)
    monkeypatch.setattr(links, 'get_display_subscription_link', lambda s: 'happ://crypt5x')

    cb = _callback('nz!_capp:happ:5')
    await links.handle_connect_app_happ(cb, _user(), db=None, state=None)

    cb.message.edit_text.assert_awaited()  # happ_cryptolink connect UI rendered
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_app_choice_routing.py -v`
Expected: FAIL with `AttributeError: module 'app.handlers.subscription.links' has no attribute 'handle_connect_app_happ'`.

- [ ] **Step 3: Refactor `handle_connect_subscription` and add `handle_connect_app_happ`**

In `app/handlers/subscription/links.py`:

(a) Add to the existing import block at the top (the `from app.keyboards.inline import (...)` group):
```python
    get_app_choice_keyboard,
```

(b) In `handle_connect_subscription`, **replace everything from `connect_mode = settings.CONNECT_BUTTON_MODE` (line ~92) through the end of the `else:` guide branch (the final `await callback.answer()` at line ~253)** with the app-choice render below. Keep lines 1-91 (multi-tariff selection, sub resolution, link validation) intact:

```python
    # Ask which app before showing the connect UI. HAPP reproduces the current
    # behavior; INCY uses the incy:// deep-link flow. Choice is ephemeral.
    await callback.message.edit_text(
        texts.t('APP_CHOICE_PROMPT', '📲 <b>Выберите приложение</b>\nКакое приложение вы используете?'),
        reply_markup=get_app_choice_keyboard(db_user.language, sub_id=sub_id),
        parse_mode='HTML',
    )
    await callback.answer()


async def handle_connect_app_happ(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    """Render the existing (HAPP / configured CONNECT_BUTTON_MODE) connect UI."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return
    subscription_link = get_display_subscription_link(subscription)
    hide_subscription_link = settings.should_hide_subscription_link()
    back_cb = f'nz!_sm:{sub_id}' if settings.is_multi_tariff_enabled() else 'nz!_menu_subscription'

    if not subscription_link:
        await callback.answer(
            texts.t('SUBSCRIPTION_NO_ACTIVE_LINK', '⚠ У вас нет активной подписки или ссылка еще генерируется'),
            show_alert=True,
        )
        return

    connect_mode = settings.CONNECT_BUTTON_MODE
```

Then **move the original mode-branching block** (the `if connect_mode == 'miniapp_subscription':` ... through the guide-mode `else:` branch and its final `await callback.answer()`) so it now lives at the end of `handle_connect_app_happ` (its `texts`, `subscription_link`, `sub_id`, `back_cb`, `hide_subscription_link` locals are all defined above). The body is unchanged — only its enclosing function changes.

> Net effect: `handle_connect_subscription` ends right after rendering the app-choice keyboard; `handle_connect_app_happ` contains the verbatim connect-mode rendering that used to follow.

- [ ] **Step 4: Export the new handlers from `__init__.py`**

In `app/handlers/subscription/__init__.py`:
- In the `from .links import (...)` group (line ~75), add `handle_connect_app_happ`.
- Add a new import group: `from .incy import handle_connect_incy, handle_incy_download`.
- Add `'handle_connect_app_happ'`, `'handle_connect_incy'`, `'handle_incy_download'` to `__all__`.

- [ ] **Step 5: Register the callbacks in `purchase.py`**

In `app/handlers/subscription/purchase.py`:
- Add to the `.links` import (line ~168): `handle_connect_app_happ`.
- Add a new import: `from .incy import handle_connect_incy, handle_incy_download`.
- In `register_handlers(dp)`, just after the `handle_connect_subscription` registration (~line 4310), add:
```python
    dp.callback_query.register(handle_connect_app_happ, F.data.startswith('nz!_capp:happ'))
    dp.callback_query.register(handle_connect_incy, F.data.startswith('nz!_capp:incy'))
    dp.callback_query.register(handle_incy_download, F.data.startswith('nz!_incy_dl'))
```

(`nz!_incy_dl` covers `nz!_incy_dl`, `nz!_incy_dl:...` and `nz!_incy_dl_close` via `startswith`. None collide with `nz!_subscription_connect` / `nz!_open_subscription_link`.)

- [ ] **Step 6: Run the routing tests**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_app_choice_routing.py -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Run the full INCY + connect test set to check for regressions**

Run:
```bash
.venv/Scripts/python.exe -m pytest tests/utils/test_incy_link.py tests/utils/test_incy_keymat.py tests/utils/test_scheme_redirect_link.py tests/services/test_incy_release_service.py tests/services/test_incy_config.py tests/keyboards/test_incy_keyboards.py tests/handlers/test_incy_handlers.py tests/handlers/test_app_choice_routing.py tests/localization/test_incy_keys_present.py -v
```
Expected: ALL PASS.

- [ ] **Step 8: Import-smoke the bot wiring**

Run (verifies the new registrations import cleanly with the dispatcher):
```bash
.venv/Scripts/python.exe -c "import app.handlers.subscription as s; print('handlers import OK:', all(hasattr(s, n) for n in ['handle_connect_app_happ','handle_connect_incy','handle_incy_download']))"
```
Expected: `handlers import OK: True`.

- [ ] **Step 9: Commit**

```bash
git add app/handlers/subscription/links.py app/handlers/subscription/__init__.py app/handlers/subscription/purchase.py tests/handlers/test_app_choice_routing.py
git commit -m "feat(incy): insert HAPP/INCY app-choice step into connect flow"
```

---

## Final verification

- [ ] **Run the complete new test suite:**
```bash
.venv/Scripts/python.exe -m pytest tests/utils/test_incy_link.py tests/utils/test_incy_keymat.py tests/utils/test_scheme_redirect_link.py tests/services/test_incy_release_service.py tests/services/test_incy_config.py tests/keyboards/test_incy_keyboards.py tests/handlers/test_incy_handlers.py tests/handlers/test_app_choice_routing.py tests/localization/test_incy_keys_present.py -v
```
Expected: ALL PASS.

- [ ] **Manual smoke (real bot, test chat):**
  - Press "Подключиться" → app-choice screen shows **Happ** and **INCY**.
  - **Happ** → the current connect UI appears unchanged (whatever `CONNECT_BUTTON_MODE` is configured).
  - **INCY** → message with a tappable `incy://` link, a copyable code block, a "Подключиться" button (if a redirect template is set), and "⬇️ Скачать INCY".
  - "⬇️ Скачать INCY" → Android/iOS/Windows give a direct link; macOS → Apple Silicon/Intel; Linux → ARM/x64 → DEB/RPM/Portable, each ending at an "Открыть ссылку" button.
  - Open the INCY link on a device with INCY installed → the subscription imports with the configured name.

---

## Notes for the implementer

- **TDD discipline:** every task writes the test first, watches it fail, then implements. Do not skip the "verify it fails" step.
- **The fingerprint test (Task 2) is the linchpin** — if it ever fails, the keymat blobs were pasted wrong or INCY rotated their scheme; do not "fix" it by changing the expected constant.
- **INCY connect must use `apply_subscription_domain_override(subscription.subscription_url)`** — never `get_display_subscription_link` (that returns the HAPP crypt5 link in happ_cryptolink mode). Task 8's first test guards this.
- **Two locale directories** (`app/localization/locales/` and root `locales/`) must both get the keys.
- Keep commits per-task; the plan is structured so each task leaves the suite green.
