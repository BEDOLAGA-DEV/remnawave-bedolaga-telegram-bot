# WL username mirrors main user — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WL-аккаунт на RemnaWave panel всегда называется `<main_account_username>_wl`, дубликаты других форматов автоматически удаляются при ближайшем sync.

**Architecture:** `_ensure_wl_user_synced` принимает имя реального main-аккаунта (то, что вернул `api.update_user` / `api.create_user` / адаптированный legacy_user) и строит WL username от него. После update/create WL рутина чистит дубликаты-кандидаты (`user_<tg>_wl`, `u_<tg>_<sub_id>_wl` по текущей подписке, template-based). Логика построения WL из шаблона (`_build_wl_username`) удаляется как мёртвый код.

**Tech Stack:** Python 3.13, pytest, pytest-asyncio, SQLAlchemy 2.0, AsyncMock/MagicMock.

**Spec:** `docs/superpowers/specs/2026-05-15-wl-username-mirror-main-design.md`

---

## File Structure

**Modified:**
- `app/services/subscription_service.py`
  - `_ensure_wl_user_synced` — добавить kwarg `main_username: str`, изменить тело (новый primary_wl, удалить legacy/hardcoded fallback'и, вызвать cleanup).
  - `create_remnawave_user` (line 238 callsite) — передать `updated_user.username`.
  - Каллсайт около line 608 — то же.
  - Удалить `_build_wl_username` (lines 633-662).
  - Добавить `_derive_wl_username` и `_cleanup_wl_duplicates` private methods.

**Created:**
- `tests/services/test_subscription_service_wl.py` — юнит-тесты для нового пути WL.

**Untouched:**
- Все внешние callers `create_remnawave_user` — публичная сигнатура не меняется (main_username вычисляется внутри).
- RemnaWave API client — методы уже существуют.

---

## Task 1: Scaffold test file + первый failing test для primary_wl из main_username

**Files:**
- Create: `tests/services/test_subscription_service_wl.py`

- [ ] **Step 1: Write the failing test file**

```python
"""Unit tests for SubscriptionService WL username handling.

Tests cover the rule that WL primary username mirrors the actual main-account
username on the RemnaWave panel (legacy 'user_<tg>' or new 'u_<tg>_<sub_id>'),
and that duplicate WL accounts in the other format get cleaned up on sync.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.subscription_service import SubscriptionService


def _make_user(telegram_id: int = 123, user_id: int = 1) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.telegram_id = telegram_id
    user.username = 'tester'
    user.full_name = 'Test User'
    user.email = None
    user.language = 'ru'
    return user


def _make_subscription(sub_id: int = 42, tariff=None) -> MagicMock:
    sub = MagicMock()
    sub.id = sub_id
    sub.tariff_id = 7 if tariff else None
    sub.tariff = tariff
    sub.wl_traffic_limit_gb = 50
    sub.wl_traffic_used_gb = 0.0
    sub.end_date = MagicMock()
    sub.status = 'active'
    return sub


@pytest.mark.asyncio
async def test_primary_wl_username_built_from_main_username_legacy_form():
    """When main is adopted as legacy 'user_<tg>', WL must be 'user_<tg>_wl'."""
    service = SubscriptionService()
    api = MagicMock()
    api.get_user_by_username = AsyncMock(return_value=None)
    api.create_user = AsyncMock(return_value=types.SimpleNamespace(uuid='new-wl-uuid'))
    api.delete_user = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='user_123',
    )

    assert api.create_user.await_count == 1
    create_kwargs = api.create_user.await_args.kwargs
    assert create_kwargs['username'] == 'user_123_wl'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_subscription_service_wl.py::test_primary_wl_username_built_from_main_username_legacy_form -v`

Expected: FAIL with `TypeError: _ensure_wl_user_synced() got an unexpected keyword argument 'main_username'`.

- [ ] **Step 3: Commit failing test**

```bash
git add tests/services/test_subscription_service_wl.py
git commit -m "test(subscription): add failing test for WL username from main_username"
```

---

## Task 2: Add `main_username` kwarg to `_ensure_wl_user_synced` (minimal pass)

**Files:**
- Modify: `app/services/subscription_service.py`

- [ ] **Step 1: Locate the current method definition**

Open `app/services/subscription_service.py`. Find `async def _ensure_wl_user_synced(` (currently near line 664). Confirm params: `self, api, user, subscription, is_actually_active, reset_traffic=False, reset_reason=None`.

- [ ] **Step 2: Add `_derive_wl_username` helper above `_ensure_wl_user_synced`**

Place this method directly above `async def _ensure_wl_user_synced`:

```python
    def _derive_wl_username(
        self,
        main_username: str,
        user: User,
        subscription: Subscription | None,
    ) -> str:
        """Build WL panel username from the resolved main-account username.

        Mirrors '<main>_wl', truncating main to 33 chars first so the final
        string fits the RemnaWave 36-char username cap. If main_username is
        empty (defensive — should not happen at call site), falls back to
        template-based logic to avoid creating an unnamed account.
        """
        cleaned = (main_username or '').strip().rstrip('_-')
        if cleaned:
            trimmed = cleaned[:33].rstrip('_-')
            return f'{trimmed}_wl'
        # Defensive fallback: template-based legacy behaviour.
        base = settings.format_remnawave_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )
        sub_id = getattr(subscription, 'id', None) if subscription else None
        if sub_id:
            stem = f'{base}_{sub_id}'[:33].rstrip('_-')
            return f'{stem}_wl'
        return f'{base[:33].rstrip("_-")}_wl'
```

- [ ] **Step 3: Change `_ensure_wl_user_synced` signature**

Replace:

```python
    async def _ensure_wl_user_synced(
        self,
        api: RemnaWaveAPI,
        user: User,
        subscription: Subscription,
        is_actually_active: bool,
        reset_traffic: bool = False,
        reset_reason: str | None = None,
    ) -> None:
```

with:

```python
    async def _ensure_wl_user_synced(
        self,
        api: RemnaWaveAPI,
        user: User,
        subscription: Subscription,
        is_actually_active: bool,
        main_username: str,
        reset_traffic: bool = False,
        reset_reason: str | None = None,
    ) -> None:
```

- [ ] **Step 4: Replace primary/legacy WL derivation inside method body**

Inside the method (within the outer try block), locate:

```python
            primary_wl, legacy_wl = self._build_wl_username(user, subscription)
            username_wl = primary_wl
```

Replace with:

```python
            primary_wl = self._derive_wl_username(main_username, user, subscription)
            username_wl = primary_wl
            # Legacy lookup fallbacks removed — primary_wl now mirrors the main
            # account name on panel, so the only correct WL account is the one
            # named '<main>_wl'.
```

- [ ] **Step 5: Delete the legacy_wl and hardcoded fallback blocks**

Inside `_ensure_wl_user_synced`, remove these two `if not wl_user ...:` blocks (currently around lines 715-758):

```python
            # Legacy fallback: existing accounts created before per-sub WL
            # naming used user_<tg>_wl (no sub.id). Try the legacy name once
            # before deciding to create a new account.
            if not wl_user and legacy_wl and legacy_wl != primary_wl:
                try:
                    wl_user = await api.get_user_by_username(legacy_wl)
                    if wl_user:
                        logger.info(
                            '♻️ Found legacy WL user, will reuse',
                            legacy=legacy_wl,
                            primary=primary_wl,
                            wl_uuid=wl_user.uuid,
                        )
                        username_wl = legacy_wl
                except Exception as legacy_err:
                    logger.warning('Legacy WL fallback lookup failed', error=legacy_err)

            # Second legacy fallback: pre-template-change WL accounts used the
            # historical default template 'user_<telegram_id>_wl' regardless of
            # the current REMNAWAVE_USER_USERNAME_TEMPLATE. If admin later
            # changed the template to e.g. 'u_{telegram_id}', the first legacy
            # check above would search 'u_<tg>_wl' and miss the actual stored
            # name 'user_<tg>_wl'. Try the hardcoded historical name too.
            if not wl_user and user.telegram_id:
                hardcoded_legacy_wl = f'user_{user.telegram_id}_wl'
                if hardcoded_legacy_wl not in (primary_wl, legacy_wl):
                    try:
                        wl_user = await api.get_user_by_username(hardcoded_legacy_wl)
                        if wl_user:
                            logger.info(
                                '♻️ Found pre-template-change WL user, will reuse',
                                legacy=hardcoded_legacy_wl,
                                primary=primary_wl,
                                wl_uuid=wl_user.uuid,
                            )
                            username_wl = hardcoded_legacy_wl
                    except Exception as hardcoded_err:
                        logger.warning(
                            'Pre-template-change WL fallback lookup failed',
                            error=hardcoded_err,
                        )
```

After removal, the method goes straight from the primary_wl lookup (`wl_user = await api.get_user_by_username(username_wl)`) to the `if wl_user:` update/create branch.

- [ ] **Step 6: Delete the dead `_build_wl_username` method**

Remove the entire `def _build_wl_username` block (currently lines 633-662). Verify nothing else still references it:

Run: `grep -n "_build_wl_username" app/services/subscription_service.py`

Expected: zero matches.

- [ ] **Step 7: Update both internal callsites to pass `main_username=""` for now**

Internal callers in this same file must remain importable while Task 3 fills in real values. Update both call sites to pass an explicit empty string — the defensive fallback in `_derive_wl_username` keeps behaviour unchanged.

At the line-238 callsite:

```python
                main_username_for_wl = ''  # Filled by Task 3
                await self._ensure_wl_user_synced(
                    api,
                    user,
                    subscription,
                    is_actually_active,
                    main_username=main_username_for_wl,
                    reset_traffic=reset_traffic,
                    reset_reason=reset_reason,
                )
```

Apply the same edit at the line-608 callsite.

- [ ] **Step 8: Run the Task 1 test — should now pass**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_subscription_service_wl.py::test_primary_wl_username_built_from_main_username_legacy_form -v`

Expected: PASS (`api.create_user` called with `username='user_123_wl'`).

- [ ] **Step 9: Smoke-import sanity**

Run: `.venv/Scripts/python.exe -c "import app.services.subscription_service; print('OK')"`

Expected: `OK`.

- [ ] **Step 10: Commit**

```bash
git add app/services/subscription_service.py tests/services/test_subscription_service_wl.py
git commit -m "refactor(subscription): _ensure_wl_user_synced derives WL from main_username

Adds main_username kwarg + _derive_wl_username helper. Drops legacy_wl
and hardcoded user_<tg>_wl lookups (no longer needed — primary_wl now
mirrors the main account on panel). Removes dead _build_wl_username."
```

---

## Task 3: Pass real `main_username` from both callsites + cover new-form and truncation cases

**Files:**
- Modify: `app/services/subscription_service.py`
- Modify: `tests/services/test_subscription_service_wl.py`

- [ ] **Step 1: Add failing tests for new-form and truncation**

Append to `tests/services/test_subscription_service_wl.py`:

```python
@pytest.mark.asyncio
async def test_primary_wl_username_built_from_main_username_new_form():
    """When main is created as 'u_<tg>_<sub_id>', WL must be 'u_<tg>_<sub_id>_wl'."""
    service = SubscriptionService()
    api = MagicMock()
    api.get_user_by_username = AsyncMock(return_value=None)
    api.create_user = AsyncMock(return_value=types.SimpleNamespace(uuid='new-wl-uuid'))
    api.delete_user = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='u_123_42',
    )

    create_kwargs = api.create_user.await_args.kwargs
    assert create_kwargs['username'] == 'u_123_42_wl'


@pytest.mark.asyncio
async def test_primary_wl_username_truncated_when_main_too_long():
    """Main usernames longer than 33 chars are truncated before appending _wl."""
    service = SubscriptionService()
    api = MagicMock()
    api.get_user_by_username = AsyncMock(return_value=None)
    api.create_user = AsyncMock(return_value=types.SimpleNamespace(uuid='new-wl-uuid'))
    api.delete_user = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)
    long_main = 'a' * 50  # 50 chars

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username=long_main,
    )

    final = api.create_user.await_args.kwargs['username']
    assert final.endswith('_wl')
    assert len(final) <= 36
    assert final == 'a' * 33 + '_wl'
```

- [ ] **Step 2: Run the new tests — verify behaviour**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_subscription_service_wl.py -v`

Expected: 3/3 PASS (both new tests should pass because `_derive_wl_username` already covers these shapes). If a test fails, double-check `_derive_wl_username` does `main_username[:33].rstrip('_-')` BEFORE appending `_wl`.

- [ ] **Step 3: Pass real main_username at the line-238 callsite**

Inside `create_remnawave_user`, the resolved main account lives in `updated_user`. Replace the placeholder:

```python
                main_username_for_wl = ''  # Filled by Task 3
```

with:

```python
                main_username_for_wl = getattr(updated_user, 'username', '') or ''
```

- [ ] **Step 4: Pass real main_username at the line-608 callsite**

Same edit in the second callsite (`_create_or_update_remnawave_user_single` or whichever method owns it). The local variable is also called `updated_user` on this path.

- [ ] **Step 5: Smoke-import sanity**

Run: `.venv/Scripts/python.exe -c "import app.services.subscription_service; print('OK')"`

Expected: `OK`.

- [ ] **Step 6: Re-run WL tests**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_subscription_service_wl.py -v`

Expected: 3/3 PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/subscription_service.py tests/services/test_subscription_service_wl.py
git commit -m "feat(subscription): pass real main_username from both WL sync callsites

Both create_remnawave_user and _create_or_update_remnawave_user_single
now forward updated_user.username to _ensure_wl_user_synced so the WL
account name mirrors whatever name the main account ended up with on
RemnaWave panel (legacy 'user_<tg>' adoption or new 'u_<tg>_<sub_id>')."
```

---

## Task 4: Add `_cleanup_wl_duplicates` helper + wire it into the sync (TDD)

**Files:**
- Modify: `app/services/subscription_service.py`
- Modify: `tests/services/test_subscription_service_wl.py`

- [ ] **Step 1: Append failing tests for duplicate cleanup**

Append to `tests/services/test_subscription_service_wl.py`:

```python
@pytest.mark.asyncio
async def test_cleanup_deletes_orphan_legacy_wl_when_primary_is_new_form():
    """If main is new-form, the legacy 'user_<tg>_wl' orphan must be deleted."""
    service = SubscriptionService()
    api = MagicMock()
    primary_wl_user = types.SimpleNamespace(uuid='primary-wl-uuid')
    legacy_orphan = types.SimpleNamespace(uuid='legacy-wl-uuid')

    async def fake_get(username: str):
        if username == 'u_123_42_wl':
            return primary_wl_user
        if username == 'user_123_wl':
            return legacy_orphan
        return None

    api.get_user_by_username = AsyncMock(side_effect=fake_get)
    api.update_user = AsyncMock(return_value=primary_wl_user)
    api.delete_user = AsyncMock(return_value=True)
    api.reset_user_devices = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='u_123_42',
    )

    assert api.delete_user.await_count == 1
    api.delete_user.assert_awaited_with('legacy-wl-uuid')


@pytest.mark.asyncio
async def test_cleanup_deletes_orphan_new_form_when_primary_is_legacy():
    """If main is legacy, the new-form 'u_<tg>_<sub_id>_wl' orphan must be deleted."""
    service = SubscriptionService()
    api = MagicMock()
    primary_wl_user = types.SimpleNamespace(uuid='primary-wl-uuid')
    new_orphan = types.SimpleNamespace(uuid='orphan-uuid')

    async def fake_get(username: str):
        if username == 'user_123_wl':
            return primary_wl_user
        if username == 'u_123_42_wl':
            return new_orphan
        return None

    api.get_user_by_username = AsyncMock(side_effect=fake_get)
    api.update_user = AsyncMock(return_value=primary_wl_user)
    api.delete_user = AsyncMock(return_value=True)
    api.reset_user_devices = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='user_123',
    )

    assert api.delete_user.await_count == 1
    api.delete_user.assert_awaited_with('orphan-uuid')


@pytest.mark.asyncio
async def test_cleanup_no_duplicates_no_delete():
    """When no duplicate exists, delete_user must not be called."""
    service = SubscriptionService()
    api = MagicMock()
    primary_wl_user = types.SimpleNamespace(uuid='primary-wl-uuid')

    async def fake_get(username: str):
        if username == 'user_123_wl':
            return primary_wl_user
        return None

    api.get_user_by_username = AsyncMock(side_effect=fake_get)
    api.update_user = AsyncMock(return_value=primary_wl_user)
    api.delete_user = AsyncMock(return_value=True)
    api.reset_user_devices = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='user_123',
    )

    assert api.delete_user.await_count == 0


@pytest.mark.asyncio
async def test_cleanup_delete_failure_does_not_break_flow():
    """If delete_user raises, the sync still completes."""
    service = SubscriptionService()
    api = MagicMock()
    primary_wl_user = types.SimpleNamespace(uuid='primary-wl-uuid')
    orphan = types.SimpleNamespace(uuid='orphan-uuid')

    async def fake_get(username: str):
        if username == 'user_123_wl':
            return primary_wl_user
        if username == 'u_123_42_wl':
            return orphan
        return None

    api.get_user_by_username = AsyncMock(side_effect=fake_get)
    api.update_user = AsyncMock(return_value=primary_wl_user)
    api.delete_user = AsyncMock(side_effect=Exception('boom'))
    api.reset_user_devices = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    # Must NOT raise.
    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='user_123',
    )

    assert api.delete_user.await_count == 1
```

- [ ] **Step 2: Run tests — verify the new ones fail**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_subscription_service_wl.py -v`

Expected: 4 new tests FAIL (`delete_user` not called — cleanup not implemented). First 3 tests still PASS.

- [ ] **Step 3: Add `_cleanup_wl_duplicates` private helper**

Place this method directly above `_ensure_wl_user_synced` (after `_derive_wl_username`):

```python
    async def _cleanup_wl_duplicates(
        self,
        api: RemnaWaveAPI,
        user: User,
        subscription: Subscription,
        primary_wl: str,
        primary_uuid: str | None,
    ) -> None:
        """Delete other-format WL accounts for this user.

        Iterates known WL naming conventions (legacy 'user_<tg>_wl', new
        'u_<tg>_<sub_id>_wl' for the current subscription, and the
        template-based name derived from `format_remnawave_username`).
        Anything that is NOT primary_wl and does NOT share primary_uuid gets
        deleted. Lookup 404s are expected; delete failures are logged and
        swallowed so the main sync keeps working.
        """
        if not user.telegram_id:
            return

        candidates: set[str] = set()

        # Legacy default-template form.
        candidates.add(f'user_{user.telegram_id}_wl')

        # New per-subscription form for the current subscription.
        sub_id = getattr(subscription, 'id', None)
        if sub_id:
            candidates.add(f'u_{user.telegram_id}_{sub_id}_wl')

        # Template-based form (whatever the current template would generate).
        try:
            base = settings.format_remnawave_username(
                full_name=user.full_name,
                username=user.username,
                telegram_id=user.telegram_id,
                email=user.email,
                user_id=user.id,
            )
            candidates.add(f'{base[:33].rstrip("_-")}_wl')
        except Exception as fmt_err:
            logger.warning('WL cleanup: template format failed', error=fmt_err)

        for candidate in candidates:
            if candidate == primary_wl:
                continue
            try:
                dup = await api.get_user_by_username(candidate)
            except Exception as lookup_err:
                logger.warning(
                    'WL cleanup: lookup failed',
                    candidate=candidate,
                    error=lookup_err,
                )
                continue
            if not dup:
                continue
            dup_uuid = getattr(dup, 'uuid', None)
            if primary_uuid and dup_uuid == primary_uuid:
                # Same account, different alias — skip.
                continue
            logger.warning(
                '🧹 Удаляю дублирующий WL аккаунт',
                duplicate=candidate,
                primary=primary_wl,
                duplicate_uuid=dup_uuid,
            )
            try:
                await api.delete_user(dup_uuid)
            except Exception as delete_err:
                logger.warning(
                    '⚠️ Не удалось удалить дубликат WL',
                    duplicate=candidate,
                    error=delete_err,
                )
```

- [ ] **Step 4: Wire cleanup into `_ensure_wl_user_synced`**

Refactor the update/create branch at the end of `_ensure_wl_user_synced` to capture the resulting UUID and call cleanup. Replace this region:

```python
            if wl_user:
                logger.info('♻️ _wl пользователь найден, обновляем', username_wl=username_wl, wl_uuid=wl_user.uuid)
                try:
                    updated_wl = await api.update_user(uuid=wl_user.uuid, **wl_kwargs)
                    if reset_traffic:
                        await self._reset_user_traffic(
                            api,
                            updated_wl.uuid,
                            user,
                            reset_reason,
                        )
                    logger.info('✅ Обновлен _wl пользователь', username=username_wl)
                except RemnaWaveAPIError as api_error:
                    if api_error.status_code == 404:
                        logger.warning('⚠️ _wl пользователь не найден при обновлении (404), пробуем создать', username_wl=username_wl)
                        wl_kwargs['username'] = username_wl
                        await api.create_user(**wl_kwargs)
                        logger.info('✅ Пересоздан _wl пользователь после 404', username=username_wl)
                    else:
                        raise api_error
            else:
                logger.info('🆕 _wl пользователь не найден, создаём', username_wl=username_wl)
                wl_kwargs['username'] = username_wl
                created_wl = await api.create_user(**wl_kwargs)
                if reset_traffic:
                    await self._reset_user_traffic(
                        api,
                        created_wl.uuid,
                        user,
                        reset_reason,
                    )
                logger.info('✅ Создан _wl пользователь', username=username_wl)
```

with:

```python
            primary_uuid: str | None = None
            if wl_user:
                logger.info('♻️ _wl пользователь найден, обновляем', username_wl=username_wl, wl_uuid=wl_user.uuid)
                try:
                    updated_wl = await api.update_user(uuid=wl_user.uuid, **wl_kwargs)
                    primary_uuid = updated_wl.uuid
                    if reset_traffic:
                        await self._reset_user_traffic(
                            api,
                            updated_wl.uuid,
                            user,
                            reset_reason,
                        )
                    logger.info('✅ Обновлен _wl пользователь', username=username_wl)
                except RemnaWaveAPIError as api_error:
                    if api_error.status_code == 404:
                        logger.warning('⚠️ _wl пользователь не найден при обновлении (404), пробуем создать', username_wl=username_wl)
                        wl_kwargs['username'] = username_wl
                        created_wl = await api.create_user(**wl_kwargs)
                        primary_uuid = created_wl.uuid
                        logger.info('✅ Пересоздан _wl пользователь после 404', username=username_wl)
                    else:
                        raise api_error
            else:
                logger.info('🆕 _wl пользователь не найден, создаём', username_wl=username_wl)
                wl_kwargs['username'] = username_wl
                created_wl = await api.create_user(**wl_kwargs)
                primary_uuid = created_wl.uuid
                if reset_traffic:
                    await self._reset_user_traffic(
                        api,
                        created_wl.uuid,
                        user,
                        reset_reason,
                    )
                logger.info('✅ Создан _wl пользователь', username=username_wl)

            await self._cleanup_wl_duplicates(
                api, user, subscription, primary_wl, primary_uuid,
            )
```

- [ ] **Step 5: Run tests — verify all pass**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_subscription_service_wl.py -v`

Expected: 7/7 PASS.

- [ ] **Step 6: Smoke-import sanity**

Run: `.venv/Scripts/python.exe -c "import app.services.subscription_service; print('OK')"`

Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add app/services/subscription_service.py tests/services/test_subscription_service_wl.py
git commit -m "feat(subscription): cleanup orphan WL accounts on sync

Adds _cleanup_wl_duplicates that, after _ensure_wl_user_synced resolves
primary_wl, deletes any other-format WL ('user_<tg>_wl' vs
'u_<tg>_<sub_id>_wl' vs template-based) that belongs to the same user
but lives under a different uuid. Lookup 404s and delete failures are
swallowed so the main sync keeps working."
```

---

## Task 5: Regression sweep + cleanup checks

**Files:** None modified — verification only.

- [ ] **Step 1: Run the targeted WL test file**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_subscription_service_wl.py -v`

Expected: 7/7 PASS.

- [ ] **Step 2: Run the full tests/services suite**

Run: `.venv/Scripts/python.exe -m pytest tests/services/ -q --tb=no`

Expected: same pass/fail counts as on `master` (this plan must NOT introduce new failures). Pre-existing failures from commits b98ebbfd / cfdcf2be are out of scope.

- [ ] **Step 3: py_compile check**

Run: `.venv/Scripts/python.exe -m py_compile app/services/subscription_service.py`

Expected: no output.

- [ ] **Step 4: Verify dead code removal**

Run: `grep -rn "_build_wl_username" app/ tests/`

Expected: zero matches.

- [ ] **Step 5: Verify both callsites pass `main_username`**

Run: `grep -n "main_username" app/services/subscription_service.py`

Expected at minimum five lines:
1. `def _derive_wl_username(...)` parameter.
2. `async def _ensure_wl_user_synced(...)` parameter.
3. Line-238 callsite kwarg.
4. Line-608 callsite kwarg.
5. The two `main_username_for_wl = getattr(updated_user, 'username', '') or ''` assignments (counts as additional matches).

- [ ] **Step 6: No new commit required if steps 1-5 are clean**

If any drive-by fix is needed (e.g., stray comment, formatting), commit it; otherwise this task finishes as a pure verification gate.

---

## Self-review checklist

**Spec coverage:**
- Component 1 (WL from main_username) — Tasks 1, 2, 3.
- Component 2 (Callers pass main_username) — Task 3.
- Component 3 (Cleanup duplicates) — Task 4.
- Spec testing list (5 cases: legacy primary + new orphan, new primary + legacy orphan, no dups, long main, delete failure swallowed) — all five mapped to tests in Tasks 1, 3, 4.

**Placeholders:** none ("TBD"/"implement later"/"similar to" — нет).

**Type consistency:** `main_username: str`, `primary_wl: str`, `primary_uuid: str | None` consistent across `_derive_wl_username`, `_ensure_wl_user_synced`, `_cleanup_wl_duplicates`.

**Rollback note:** if anything breaks mid-execution, `git reset --hard master` and start over from a fresh branch. Optional `git branch backup-before-wl-rename master` before Task 1.
