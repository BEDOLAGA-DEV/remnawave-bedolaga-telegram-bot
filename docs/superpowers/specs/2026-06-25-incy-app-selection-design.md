# Design: HAPP / INCY app selection

**Date:** 2026-06-25
**Branch:** feat/subscription-domain-override (or new feat branch)
**Status:** Approved (design), pending implementation plan

## Problem

The bot currently supports a single client app (HAPP). The connect ("Подключиться")
flow generates a HAPP subscription link directly. We need to let the user choose
between **HAPP** and **INCY** before connecting:

- **HAPP** → connect link generated exactly as today (no change).
- **INCY** → connect link is an `incy://crypt1/<payload>` deep link produced by an
  AES‑256‑GCM encoder ported from
  [INCY-DEV/incy-link-encoder](https://github.com/INCY-DEV/incy-link-encoder).

INCY also needs its own download flow (per‑OS/architecture buttons), with desktop
installer links resolved from the latest GitHub release of
[INCY-DEV/incy-platforms](https://github.com/INCY-DEV/incy-platforms).

## Decisions (confirmed with user)

1. **App choice is ephemeral** — asked every time the user presses "Подключиться".
   No DB column, no migration.
2. **Both apps always available** — no enable flag. The choice screen is always
   shown.
3. **INCY desktop links parsed from the latest GitHub release** (GitHub API), with
   an in‑memory cache (TTL).
4. **INCY connect link delivery mirrors HAPP**: a copyable `<code>` block + a
   tappable `incy://` HTML text link **plus** a "Подключиться" button using the
   same HTTP redirect service the user already runs
   (`https://redirect.virtualsprivate.network/?redirect_to=<urlencoded deep link>`).
   Telegram inline buttons reject custom schemes, so the button uses the HTTP
   redirect; the redirect host forwards `redirect_to` to the `incy://` scheme.

## Verified facts

The Python port of `encryptLink` was validated against the published library
before writing this spec:

- Key derivation: `K = SHA256(b"incy"+b"deep"+b"crypt1"+b"v2026.06" + kmA + kmB)`
  where `kmA = assetA[1024:1056]`, `kmB = assetB[2048:2080]` (each asset is a
  4096‑byte blob, base64‑inlined in the library's `keymat.ts`).
- Derived key fingerprint `SHA256(K)` == `b6bf708471cc90043232967660aade86a50b4e57929db2e53c5fa34db624c08c`
  — **reproduced exactly** in Python.
- Payload JSON is compact, UTF‑8, keys sorted: equivalent to
  `json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)`.
  Keys: `url` (required), `v` = 1, `n` = name (optional, ≤128 chars). Sorted order
  is `n, url, v`.
- Wire = `iv(12) || ciphertext || tag(16)` (the `cryptography` `AESGCM.encrypt`
  appends the 16‑byte tag automatically), base64url **without padding**, prefixed
  `incy://crypt1/`.
- Encrypt → decrypt roundtrip confirmed.

## Architecture

Approach A (chosen): a small parallel "app" abstraction layered on top of the
existing connect flow, minimal coupling, no schema change. The existing HAPP path
is preserved verbatim — it is only relocated behind an app‑choice step.

### Flow

```
Подключиться
  └─ resolve subscription (existing multi-tariff selection stays)
       └─ App-choice screen:  [ HAPP ]  [ INCY ]   (+ back)
            ├─ HAPP → existing connect logic (handle_connect_subscription body, moved)
            └─ INCY → new connect screen:
                 • tappable incy:// HTML link
                 • copyable <code> block
                 • "Подключиться" button (HTTP redirect to incy://)  [if template set]
                 • "⬇️ Скачать INCY" button
                 • back
```

The app-choice keyboard carries the resolved `sub_id` in callback data so it
survives the extra hop. New callbacks:

- `nz!_capp:happ` / `nz!_capp:happ:<sub_id>`
- `nz!_capp:incy` / `nz!_capp:incy:<sub_id>`
- `nz!_incy_dl` (INCY download entry) and nested children (see Download flow).

### Components

**1. INCY link encryptor** — `app/utils/incy_link.py`
- `encrypt_incy_link(url: str, name: str | None = None) -> str`
- Uses `cryptography` `AESGCM` (already a project dependency — see
  `generate_redhash` in `app/utils/subscription_utils.py`).
- Random 12‑byte IV in production.
- Lazy key derivation, cached after first call.
- Fingerprint self‑check on first derivation; raise if mismatch (guards against a
  corrupted/edited keymat).

**2. INCY keymat** — `app/utils/incy_keymat.py`
- Holds `KEYMAT_A_B64`, `KEYMAT_B_B64` vendored verbatim from the library's
  `keymat.ts` (2 × 4096‑byte blobs). Auto‑generated artifact; documented as such.

**3. INCY connect handler** — `app/handlers/subscription/incy.py`
- `plain_url = apply_subscription_domain_override(subscription.subscription_url)`
  — **the plain http(s) subscription URL**, NOT `get_display_subscription_link`
  (which returns the crypt5 link in happ_cryptolink mode). This is the central
  correctness point.
- `deep_link = encrypt_incy_link(plain_url, name=settings.get_incy_subscription_name())`
- `redirect = build_scheme_redirect_link(deep_link, template)` where
  `template = INCY_CONNECT_REDIRECT_TEMPLATE or HAPP_CRYPTOLINK_REDIRECT_TEMPLATE`.
- Message: title + tappable `<a href="{deep_link}">` + hint + expandable
  `<blockquote><code>{deep_link}</code></blockquote>` (mirrors HAPP message
  structure in `handle_open_subscription_link`).
- Keyboard: `[Подключиться → url=redirect]` (only if redirect resolved),
  `[⬇️ Скачать INCY]`, `[back]`.

**4. Shared redirect helper** — `app/utils/subscription_utils.py`
- Generalize `get_happ_cryptolink_redirect_link` into
  `build_scheme_redirect_link(deep_link, template)` (url‑encode `deep_link`,
  substitute `{link}`/`{subscription_link}` placeholders or append). Keep the
  existing HAPP function as a thin wrapper for backward compatibility.

**5. INCY release resolver** — `app/services/incy_release_service.py`
- `async get_incy_desktop_assets() -> dict` — fetches
  `https://api.github.com/repos/{INCY_PLATFORMS_REPO}/releases/latest`, returns a
  map of platform/arch/pkg → `browser_download_url`, **matched by asset filename
  suffix** (robust to tag/name drift), cached in‑memory with a TTL
  (`INCY_RELEASE_CACHE_TTL`, default ~6h).
- On GitHub error / rate‑limit: return last cached value if present; else None
  (handler shows "ссылка временно недоступна"). No crash.
- iOS / Android are static store URLs (`INCY_IOS_URL`, `INCY_ANDROID_URL`,
  defaulting to the user‑provided App Store / Play Store links).

Asset filename map (from release tag `desktop-vX.Y.Z`):

| Key | Filename suffix |
|---|---|
| windows | `incy-windows-setup.exe` |
| macos / arm | `incy-macos-arm64.dmg` |
| macos / intel | `incy-macos-intel.dmg` |
| linux / arm / deb | `incy-linux-arm64.deb` |
| linux / arm / rpm | `incy-linux-arm64.rpm` |
| linux / arm / portable | `incy-linux-arm64-portable.zip` |
| linux / x64 / deb | `incy-linux-x64.deb` |
| linux / x64 / rpm | `incy-linux-x64.rpm` |
| linux / x64 / portable | `incy-linux-x64-portable.zip` |

**6. INCY download flow** — handlers in `app/handlers/subscription/incy.py`,
keyboards in `app/keyboards/inline.py`. Tree:

| Platform | Sub-choice | Result |
|---|---|---|
| Android | — | `INCY_ANDROID_URL` |
| iOS | — | `INCY_IOS_URL` |
| Windows | — | `incy-windows-setup.exe` |
| MacOS | Apple Silicon \| Intel | `incy-macos-arm64.dmg` / `incy-macos-intel.dmg` |
| Linux | ARM \| x64 → DEB \| RPM \| Portable | matching asset |

Callback encoding: `nz!_incy_dl:<platform>[:<arch>[:<pkg>]]`, e.g.
`nz!_incy_dl:linux:x64:rpm`, `nz!_incy_dl:macos:arm`. Leaf nodes resolve to a
download link shown via an "Открыть ссылку" url‑button; back navigation between
levels.

**7. Keyboards** — `app/keyboards/inline.py`
- `get_app_choice_keyboard(language, sub_id)` → HAPP/INCY + back.
- `get_incy_download_platform_keyboard`, `get_incy_download_macos_keyboard`,
  `get_incy_download_linux_arch_keyboard`, `get_incy_download_linux_pkg_keyboard`,
  `get_incy_download_link_keyboard` (mirrors HAPP download keyboards).

**8. Config** — `app/config.py` + `.env.example`
- `INCY_SUBSCRIPTION_NAME: str` — import‑sheet display name (default = service /
  bot name; falls back to a sensible constant).
- `INCY_CONNECT_REDIRECT_TEMPLATE: str | None = None` — HTTP redirect template;
  when unset, falls back to `HAPP_CRYPTOLINK_REDIRECT_TEMPLATE`.
- `INCY_IOS_URL`, `INCY_ANDROID_URL` — store links, defaults preset.
- `INCY_PLATFORMS_REPO: str = 'INCY-DEV/incy-platforms'`.
- `INCY_RELEASE_CACHE_TTL: int` (seconds, default ~21600).
- Accessor methods mirroring existing `get_happ_*` helpers.

**9. Localization** — `app/localization/locales/*.json` (ru/en/ua/fa/zh) and
`locales/*.json`: `INCY_*` keys for the app‑choice screen, connect message,
download prompts, and platform/arch/pkg button labels. Mirror the existing
`HAPP_*` keys.

**10. Wiring** — register new callback handlers (`app/handlers/subscription/__init__.py`,
`app/bot.py` callback router) following the existing `nz!_*` registration pattern.

## Error handling

- INCY connect with no subscription link → existing
  `SUBSCRIPTION_NO_ACTIVE_LINK` / `SUBSCRIPTION_LINK_UNAVAILABLE` alerts.
- Redirect template unset → no "Подключиться" button; copy block + tappable link
  still shown (graceful degradation, same as HAPP).
- GitHub release fetch failure → cached value or "временно недоступна" alert; no
  crash, logged at warning level.
- Keymat fingerprint mismatch at startup/first use → raise with a clear message
  (config/asset corruption is a hard error, not silently wrong links).

## Testing (TDD)

Unit:
- `encrypt_incy_link`: deterministic‑IV vector; `SHA256(key)` fingerprint ==
  `b6bf7084…`; encrypt→decrypt roundtrip; payload JSON key order; name truncation
  at 128; `incy://crypt1/` prefix; base64url has no padding.
- `build_scheme_redirect_link`: url‑encoding, placeholder substitution, append
  fallback, None when template empty.
- INCY release resolver: builds the correct asset map from a mocked
  `releases/latest` JSON; suffix matching; cache hit within TTL; fallback to
  cache / None on fetch error.
- Routing: app‑choice → INCY connect uses the override‑plain URL (not crypt5);
  redirect built from the correct template; HAPP path unchanged.

Manual:
- Full bot flow on a test chat: choose HAPP (unchanged) and INCY (link opens in
  INCY app; download tree reaches each leaf).

## Risks / notes

- GitHub unauthenticated rate limit (60/hr/IP) → cache mitigates; resolver never
  hits the API on every click.
- INCY's keymat/scheme could rotate to `crypt2/` in a future client release; the
  vendored fingerprint check will fail loudly, signalling a keymat refresh is
  needed.
- "This is obfuscation, not security" (per the library) — the INCY key is public;
  the deep link only hides the subscription URL from casual scanners, exactly like
  HAPP's crypt links.

## Out of scope

- Persisting the user's app choice.
- Admin UI to toggle INCY.
- Any change to the existing HAPP connect/download behaviour beyond relocating it
  behind the app‑choice step.
