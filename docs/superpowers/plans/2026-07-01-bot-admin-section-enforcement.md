# Bot-Admin Section Permissions Enforcement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the already-drawn per-section bot-admin permissions actually enforce, restrict role management to super-admins, and fix the correctness bugs in the grant flow.

**Architecture:** The `AdminPermissionMiddleware` and section map already exist but are never registered — registering it turns the section checkboxes into real gates. A new `super_admin_required` decorator locks role management to ADMIN_IDS. Keyboards are filtered by reusing `resolve_admin_section`. Correctness fixes (FSM wipe, NULL crash, `created_by`, empty-role) are local edits with mocked-DB unit tests.

**Tech Stack:** Python 3.13, aiogram 3, SQLAlchemy 2 (async), pytest with a custom `pytest_pyfunc_call` hook (async tests need no marker). Spec: [docs/superpowers/specs/2026-07-01-bot-admin-section-enforcement-design.md](../specs/2026-07-01-bot-admin-section-enforcement-design.md).

**Testing note (from repo):** the test env stubs `asyncpg`/`aiosqlite`, so there is **no real DB** — every test here uses `unittest.mock` (MagicMock/AsyncMock), no live session. Run tests with the project venv Python 3.13:

```
.venv/Scripts/python.exe -m pytest <path> -v
```

Bare `python` is 3.10 and cannot import `app`. Run only the specific test files added here — a full `pytest` run has pre-existing collection errors unrelated to this work.

---

## Task 1: Preserve `created_by` on role update (C5)

**Files:**
- Modify: `app/database/crud/bot_role.py:56-62`
- Test: `tests/database/test_bot_role_crud.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/database/test_bot_role_crud.py`:

```python
"""set_bot_role must not overwrite the original creator on update."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.database.crud.bot_role import BotRoleCRUD


def _db_returning(existing):
    """A fake AsyncSession whose execute().scalar_one_or_none() returns `existing`."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


async def test_update_preserves_original_created_by():
    existing = SimpleNamespace(user_id=5, permissions=['support'], created_by=999)
    db = _db_returning(existing)

    await BotRoleCRUD.set_bot_role(db, 5, ['users', 'payments'], created_by=222)

    assert existing.permissions == ['users', 'payments']
    assert existing.created_by == 999  # NOT overwritten by editor 222


async def test_create_sets_created_by():
    db = _db_returning(None)  # no existing row

    await BotRoleCRUD.set_bot_role(db, 7, ['support'], created_by=222)

    db.add.assert_called_once()
    created = db.add.call_args.args[0]
    assert created.created_by == 222
    assert created.permissions == ['support']
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/database/test_bot_role_crud.py -v`
Expected: `test_update_preserves_original_created_by` FAILS (assert 222 == 999) because the current code overwrites `created_by`.

- [ ] **Step 3: Write minimal implementation**

In `app/database/crud/bot_role.py`, the update branch currently is:

```python
        if existing is not None:
            existing.permissions = permissions
            existing.created_by = created_by
            await db.flush()
            await db.refresh(existing)
            logger.info('Updated bot admin role', user_id=user_id, permissions=permissions)
            return existing
```

Remove the `created_by` overwrite (keep original creator):

```python
        if existing is not None:
            existing.permissions = permissions
            await db.flush()
            await db.refresh(existing)
            logger.info('Updated bot admin role', user_id=user_id, permissions=permissions)
            return existing
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/database/test_bot_role_crud.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add tests/database/test_bot_role_crud.py app/database/crud/bot_role.py
git commit -m "fix(admin): preserve original created_by when updating a bot role"
```

---

## Task 2: Grant-flow correctness fixes in `bot_roles.py` (C5)

Covers: FSM silent-wipe guard, empty-section guard, NULL `permissions` crash, clearer B1 message.

**Files:**
- Modify: `app/handlers/admin/bot_roles.py:180` (B1 message), `:184` and `:200` (NULL guard), `:212-231` (toggle presence-check), `:236-247` (save guards)
- Test: `tests/handlers/admin/test_bot_roles_fsm.py` (create), plus `tests/handlers/admin/__init__.py` (create, empty file, if missing)

- [ ] **Step 1: Write the failing test**

Create `tests/handlers/admin/__init__.py` (empty) if it does not exist, then create `tests/handlers/admin/test_bot_roles_fsm.py`:

```python
"""bot_role_save/toggle must not silently wipe permissions on lost FSM state."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery

from app.config import settings
import app.handlers.admin.bot_roles as br


@pytest.fixture
def as_superadmin(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [111])
    return 111


def _callback(admin_id: int, data: str) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = SimpleNamespace(id=admin_id)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def _state(data: dict) -> MagicMock:
    state = MagicMock()
    state.get_data = AsyncMock(return_value=data)
    state.clear = AsyncMock()
    return state


def _db_user():
    return SimpleNamespace(id=1, telegram_id=111, language='ru')


async def test_save_with_lost_state_does_not_wipe(as_superadmin):
    cb = _callback(as_superadmin, 'bot_role_save:5')
    state = _state({})  # state lost -> no 'selected_permissions' key
    db = MagicMock()
    db.commit = AsyncMock()

    with patch.object(br.BotRoleCRUD, 'set_bot_role', new=AsyncMock()) as set_role:
        await br.bot_role_save(cb, db_user=_db_user(), state=state, db=db)

    set_role.assert_not_awaited()
    db.commit.assert_not_awaited()
    cb.answer.assert_awaited()  # user told the session expired


async def test_save_with_empty_selection_rejected(as_superadmin):
    cb = _callback(as_superadmin, 'bot_role_save:5')
    state = _state({'selected_permissions': []})  # explicitly empty
    db = MagicMock()
    db.commit = AsyncMock()

    with patch.object(br.BotRoleCRUD, 'set_bot_role', new=AsyncMock()) as set_role:
        await br.bot_role_save(cb, db_user=_db_user(), state=state, db=db)

    set_role.assert_not_awaited()


async def test_save_happy_path_persists(as_superadmin):
    cb = _callback(as_superadmin, 'bot_role_save:5')
    state = _state({'selected_permissions': ['support']})
    db = MagicMock()
    db.commit = AsyncMock()

    with patch.object(br.BotRoleCRUD, 'set_bot_role', new=AsyncMock()) as set_role, \
         patch.object(br.BotRoleCRUD, 'list_bot_roles', new=AsyncMock(return_value=[])):
        await br.bot_role_save(cb, db_user=_db_user(), state=state, db=db)

    set_role.assert_awaited_once()
    assert set_role.await_args.args[2] == ['support']
    db.commit.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/admin/test_bot_roles_fsm.py -v`
Expected: `test_save_with_lost_state_does_not_wipe` FAILS — current `bot_role_save` writes `[]` and awaits `set_bot_role`.

- [ ] **Step 3: Write minimal implementation**

In `app/handlers/admin/bot_roles.py`, replace `bot_role_save` body (currently lines 236-255) so it guards on lost state and empty selection. Keep the decorators as-is for now (Task 3 swaps them):

```python
async def bot_role_save(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    parts = callback.data.split(':')
    user_id = int(parts[1])

    data = await state.get_data()
    if 'selected_permissions' not in data:
        await callback.answer('Сессия истекла, откройте роль заново.', show_alert=True)
        return

    selected = data.get('selected_permissions', [])
    if not selected:
        await callback.answer('Выберите хотя бы одну секцию.', show_alert=True)
        return

    await state.clear()

    await BotRoleCRUD.set_bot_role(db, user_id, selected, created_by=db_user.id)
    await db.commit()

    await callback.answer('Роль сохранена!', show_alert=True)

    roles = await BotRoleCRUD.list_bot_roles(db)
    await callback.message.edit_text(
        '👑 <b>Роли бота</b>\n\nРоль успешно сохранена.',
        parse_mode='HTML',
        reply_markup=_roles_list_keyboard(roles, db_user.language),
    )
```

In `bot_role_toggle` (lines 219-220), guard the same way so a toggle on lost state does not rebuild selection from `[]`:

```python
    data = await state.get_data()
    if 'selected_permissions' not in data:
        await callback.answer('Сессия истекла, откройте роль заново.', show_alert=True)
        return
    selected = data.get('selected_permissions', [])
```

Fix the NULL-`permissions` crash at line 184 (`bot_role_add_telegram_id`) and line 200 (`bot_role_edit`):

```python
    selected = list(existing.permissions or []) if existing else []   # line 184
```
```python
    selected = list(role.permissions or []) if role else []           # line 200
```

Improve the B1 message at line 180 (`bot_role_add_telegram_id`):

```python
    if not user:
        await message.answer(
            'Пользователь ещё не запускал бота. Попросите его открыть бота, затем выдайте роль.'
        )
        return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/admin/test_bot_roles_fsm.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/handlers/admin/__init__.py tests/handlers/admin/test_bot_roles_fsm.py app/handlers/admin/bot_roles.py
git commit -m "fix(admin): guard bot-role save/toggle against lost FSM state and NULL perms"
```

---

## Task 3: `super_admin_required` decorator, lock role management (C3)

**Files:**
- Modify: `app/utils/decorators.py` (add `super_admin_required` after `admin_required`, ~line 65)
- Modify: `app/handlers/admin/bot_roles.py` (swap `@admin_required` → `@super_admin_required` on all handlers; update import)
- Test: `tests/utils/test_super_admin_required.py` (create), plus `tests/utils/__init__.py` (create if missing)

- [ ] **Step 1: Write the failing test**

Create `tests/utils/__init__.py` (empty) if missing, then `tests/utils/test_super_admin_required.py`:

```python
"""super_admin_required: only ADMIN_IDS pass; a role-admin (even with 'settings') is denied."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery

from app.config import settings
from app.utils.decorators import super_admin_required


def _callback(user_id: int) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = SimpleNamespace(id=user_id)
    cb.answer = AsyncMock()
    return cb


async def test_superadmin_passes(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [111])
    called = {'v': False}

    @super_admin_required
    async def handler(event, **kwargs):
        called['v'] = True
        return 'ok'

    result = await handler(_callback(111))
    assert result == 'ok'
    assert called['v'] is True


async def test_non_superadmin_denied(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [111])
    called = {'v': False}

    @super_admin_required
    async def handler(event, **kwargs):
        called['v'] = True
        return 'ok'

    cb = _callback(222)  # a role-admin, not in ADMIN_IDS
    result = await handler(cb)
    assert result is None
    assert called['v'] is False
    cb.answer.assert_awaited()  # ACCESS_DENIED shown
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/utils/test_super_admin_required.py -v`
Expected: FAIL at import — `cannot import name 'super_admin_required'`.

- [ ] **Step 3: Write minimal implementation**

In `app/utils/decorators.py`, add after `admin_required` (after its `return wrapper`, ~line 65):

```python
def super_admin_required(func: Callable) -> Callable:
    """Allow only superadmins (ADMIN_IDS). Role-based BotAdminRole holders are denied.

    Use for role management, where a section-admin must not be able to grant
    themselves more permissions.
    """

    @functools.wraps(func)
    async def wrapper(event: types.Update, *args, **kwargs) -> Any:
        user = None
        if isinstance(event, (types.Message, types.CallbackQuery)):
            user = event.from_user

        if user and settings.is_admin(user.id):
            return await func(event, *args, **kwargs)

        texts = get_texts()
        try:
            if isinstance(event, types.Message):
                await event.answer(texts.ACCESS_DENIED)
            elif isinstance(event, types.CallbackQuery):
                await event.answer(texts.ACCESS_DENIED, show_alert=True)
        except TelegramBadRequest as e:
            if 'query is too old' not in str(e).lower():
                raise

        logger.warning('super_admin_required: доступ запрещён', user_id=user.id if user else 'Unknown')
        return None

    return wrapper
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/utils/test_super_admin_required.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Apply the decorator to role management handlers**

In `app/handlers/admin/bot_roles.py`, change the import at line 12:

```python
from app.utils.decorators import error_handler, super_admin_required
```

Then replace every `@admin_required` in that file with `@super_admin_required` (handlers: `admin_bot_roles`, `bot_role_view`, `bot_role_add`, `bot_role_add_telegram_id`, `bot_role_edit`, `bot_role_toggle`, `bot_role_save`, `bot_role_delete`, `bot_role_delete_confirm`).

- [ ] **Step 6: Verify the whole file still imports and FSM tests pass**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/admin/test_bot_roles_fsm.py tests/utils/test_super_admin_required.py -v`
Expected: PASS (the FSM tests already run as superadmin, so the decorator swap is transparent).

- [ ] **Step 7: Commit**

```bash
git add tests/utils/__init__.py tests/utils/test_super_admin_required.py app/utils/decorators.py app/handlers/admin/bot_roles.py
git commit -m "feat(admin): super-admin-only role management via super_admin_required"
```

---

## Task 4: Register `AdminPermissionMiddleware` (C1)

**Files:**
- Modify: `app/bot.py` (import + register on `dp.callback_query` after `AuthMiddleware`, ~line 194)
- Test: `tests/middlewares/test_admin_permission.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/middlewares/test_admin_permission.py`:

```python
"""AdminPermissionMiddleware gates admin_* callbacks by BotAdminRole sections."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery

from app.config import settings
from app.middlewares.admin_permission import (
    AdminPermissionMiddleware,
    resolve_admin_section,
)


def test_resolve_admin_section():
    assert resolve_admin_section('admin_users') == 'users'
    assert resolve_admin_section('admin_user_balance_5') == 'users'
    assert resolve_admin_section('admin_tickets') == 'support'
    assert resolve_admin_section('admin_bot_roles') == 'settings'
    assert resolve_admin_section('bot_role_save:5') is None   # not an admin_ callback
    assert resolve_admin_section('admin_totally_unknown') is None


def _event(data: str):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = SimpleNamespace(id=222)
    cb.answer = AsyncMock()
    return cb


def _data(permissions):
    role = SimpleNamespace(permissions=permissions)
    return {
        'db': MagicMock(),
        'db_user': SimpleNamespace(id=1, telegram_id=222, language='ru'),
    }, role


@pytest.fixture
def not_superadmin(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [111])


async def test_denies_missing_section(not_superadmin):
    mw = AdminPermissionMiddleware()
    data, role = _data(['support'])
    handler = AsyncMock(return_value='ran')
    event = _event('admin_users')  # needs 'users', role only has 'support'

    with patch('app.database.crud.bot_role.BotRoleCRUD.get_bot_role',
               new=AsyncMock(return_value=role)):
        result = await mw(handler, event, data)

    handler.assert_not_awaited()
    event.answer.assert_awaited()
    assert result is None


async def test_allows_present_section(not_superadmin):
    mw = AdminPermissionMiddleware()
    data, role = _data(['users'])
    handler = AsyncMock(return_value='ran')
    event = _event('admin_users')

    with patch('app.database.crud.bot_role.BotRoleCRUD.get_bot_role',
               new=AsyncMock(return_value=role)):
        result = await mw(handler, event, data)

    handler.assert_awaited_once()
    assert result == 'ran'


async def test_superadmin_bypass(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [222])  # event user is super
    mw = AdminPermissionMiddleware()
    data, _ = _data([])
    handler = AsyncMock(return_value='ran')
    event = _event('admin_users')

    result = await mw(handler, event, data)
    assert result == 'ran'


async def test_navigation_always_allowed(not_superadmin):
    mw = AdminPermissionMiddleware()
    data, _ = _data([])
    handler = AsyncMock(return_value='ran')
    event = _event('admin_panel')

    result = await mw(handler, event, data)
    assert result == 'ran'
```

- [ ] **Step 2: Run test to verify it passes at unit level**

Run: `.venv/Scripts/python.exe -m pytest tests/middlewares/test_admin_permission.py -v`
Expected: PASS — the middleware class already works; these tests document it. (If any fail, fix before registering.)

- [ ] **Step 3: Register the middleware in `app/bot.py`**

Add the import next to the other middleware imports (near the top of `app/bot.py`, with the other `from app.middlewares...` lines):

```python
from app.middlewares.admin_permission import AdminPermissionMiddleware
```

Register it on `dp.callback_query` **immediately after** the `AuthMiddleware` callback registration (currently line 193). It must run after `AuthMiddleware` so `data['db_user']` is populated:

```python
    dp.callback_query.middleware(AuthMiddleware())
    dp.pre_checkout_query.middleware(AuthMiddleware())
    dp.callback_query.middleware(AdminPermissionMiddleware())  # after Auth: needs db_user
```

- [ ] **Step 4: Verify the bot module imports cleanly**

Run: `.venv/Scripts/python.exe -c "import app.bot; print('ok')"`
Expected: prints `ok` (no import error).

- [ ] **Step 5: Commit**

```bash
git add tests/middlewares/test_admin_permission.py app/bot.py
git commit -m "feat(admin): register AdminPermissionMiddleware so section perms enforce"
```

---

## Task 5: Audit & complete the section map, log unmapped (C2)

**Files:**
- Modify: `app/middlewares/admin_permission.py` (extend `ADMIN_CALLBACK_SECTION_MAP`; log unmapped in `__call__`)
- Test: `tests/middlewares/test_admin_permission.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/middlewares/test_admin_permission.py`:

```python
def test_map_covers_known_gaps():
    # These prefixes were unmapped and leaked to "any admin" before the audit.
    assert resolve_admin_section('admin_rw_nodes') == 'servers'
    assert resolve_admin_section('admin_subs_list') == 'subscriptions'
    assert resolve_admin_section('admin_stats_users') == 'analytics'
    assert resolve_admin_section('admin_mon_start') == 'analytics'
    assert resolve_admin_section('admin_mon_settings') == 'settings'  # more specific wins
    assert resolve_admin_section('admin_msg_all') == 'broadcasts'
    assert resolve_admin_section('admin_campaign_stats_3') == 'promos'
    assert resolve_admin_section('admin_contest_toggle_3') == 'promos'
    assert resolve_admin_section('admin_daily_toggle_3') == 'promos'
    assert resolve_admin_section('admin_wl_analytics') == 'analytics'
    assert resolve_admin_section('admin_mass_delete_start') == 'users'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/middlewares/test_admin_permission.py::test_map_covers_known_gaps -v`
Expected: FAIL — several of these currently resolve to `None`.

- [ ] **Step 3: Extend the section map**

In `app/middlewares/admin_permission.py`, add these entries to `ADMIN_CALLBACK_SECTION_MAP` **inside the matching section group** (order matters — `admin_mon_settings` must stay in the `settings` group, which appears before the `analytics` group, so the broader `admin_mon_` in `analytics` is checked later):

```python
    # users  (add near the other users entries)
    ('admin_mass_delete', 'users'),
    # subscriptions
    ('admin_subs_', 'subscriptions'),
    # promos
    ('admin_campaign_', 'promos'),
    ('admin_contest_', 'promos'),
    ('admin_daily_', 'promos'),
    # broadcasts
    ('admin_msg_', 'broadcasts'),
    ('admin_pinned', 'broadcasts'),
    # servers
    ('admin_rw_', 'servers'),
    # analytics  (this group is already AFTER the settings group in the file)
    ('admin_stats_', 'analytics'),
    ('admin_successful_topups', 'analytics'),
    ('admin_mon_', 'analytics'),
    ('admin_wl_analytics', 'analytics'),
```

- [ ] **Step 4: Log unmapped admin callbacks**

In `app/middlewares/admin_permission.py`, `__call__`, the `required is None` branch (currently lines 152-156) silently falls through. Add an INFO log so remaining gaps surface in production:

```python
        required = resolve_admin_section(cb)
        if required is None:
            logger.info('admin callback not in section map (fallback to any-admin)', callback_data=cb)
            return await handler(event, data)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/middlewares/test_admin_permission.py -v`
Expected: PASS (all, including `test_map_covers_known_gaps` and the earlier `test_resolve_admin_section`).

- [ ] **Step 6: Commit**

```bash
git add tests/middlewares/test_admin_permission.py app/middlewares/admin_permission.py
git commit -m "feat(admin): complete section map and log unmapped admin callbacks"
```

---

## Task 6: Filter admin keyboards by permissions (C4)

**Files:**
- Modify: `app/keyboards/admin.py` (add `filter_admin_keyboard` helper)
- Modify: `app/handlers/admin/main.py` (compute permissions, filter main + 6 submenu keyboards)
- Test: `tests/keyboards/test_admin_keyboard_filter.py` (create), plus `tests/keyboards/__init__.py` (create if missing)

- [ ] **Step 1: Write the failing test**

Create `tests/keyboards/__init__.py` (empty) if missing, then `tests/keyboards/test_admin_keyboard_filter.py`:

```python
"""filter_admin_keyboard drops buttons the admin lacks; superadmin sees all."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.keyboards.admin import filter_admin_keyboard, get_admin_main_keyboard


def _all_callbacks(markup: InlineKeyboardMarkup) -> set[str]:
    return {b.callback_data for row in markup.inline_keyboard for b in row}


def test_superadmin_sees_everything():
    kb = get_admin_main_keyboard('ru')
    filtered = filter_admin_keyboard(kb, permissions=None, is_super=True)
    assert _all_callbacks(filtered) == _all_callbacks(kb)


def test_section_admin_sees_only_permitted_direct_buttons():
    kb = get_admin_main_keyboard('ru')
    filtered = filter_admin_keyboard(kb, permissions=['servers'], is_super=False)
    cbs = _all_callbacks(filtered)

    # direct section button they have
    assert 'admin_servers' in cbs
    # direct section button they lack
    assert 'admin_tariffs' not in cbs
    # role management is super-only, hidden for section admins
    assert 'admin_bot_roles' not in cbs
    # navigation / submenu buttons (section is None) stay visible
    assert 'admin_submenu_users' in cbs


def test_empty_rows_are_dropped():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Servers', callback_data='admin_servers')],
        [InlineKeyboardButton(text='Tariffs', callback_data='admin_tariffs')],
    ])
    filtered = filter_admin_keyboard(kb, permissions=['servers'], is_super=False)
    assert len(filtered.inline_keyboard) == 1
    assert filtered.inline_keyboard[0][0].callback_data == 'admin_servers'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/keyboards/test_admin_keyboard_filter.py -v`
Expected: FAIL at import — `cannot import name 'filter_admin_keyboard'`.

- [ ] **Step 3: Add the filter helper**

In `app/keyboards/admin.py`, add near the top (after the imports, before `get_admin_main_keyboard`):

```python
from app.middlewares.admin_permission import resolve_admin_section

# Callbacks only superadmins may see, regardless of section permissions.
_SUPER_ONLY_CALLBACKS = {'admin_bot_roles'}


def filter_admin_keyboard(
    markup: InlineKeyboardMarkup,
    *,
    permissions: list[str] | None,
    is_super: bool,
) -> InlineKeyboardMarkup:
    """Drop buttons the admin may not use.

    ``permissions=None`` together with ``is_super=True`` means full access
    (superadmin) and the keyboard is returned unchanged. For a section admin,
    a button is kept when its callback has no section (navigation/unmapped) or
    its section is in ``permissions``. Super-only callbacks are hidden.
    """
    if is_super:
        return markup

    allowed = set(permissions or [])
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        kept = []
        for button in row:
            cb = button.callback_data or ''
            if cb in _SUPER_ONLY_CALLBACKS:
                continue
            section = resolve_admin_section(cb)
            if section is None or section in allowed:
                kept.append(button)
        if kept:
            rows.append(kept)
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

> Import note: `app/middlewares/admin_permission.py` imports only `app.config` and `app.localization` — it does NOT import `app.keyboards`, so importing `resolve_admin_section` here introduces no circular import.

- [ ] **Step 4: Run the filter test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/keyboards/test_admin_keyboard_filter.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Apply filtering in the panel + submenu handlers**

In `app/handlers/admin/main.py`, add a helper near the top (after `logger = ...`, ~line 31). It reuses `settings` (already imported at line 9):

```python
from app.keyboards.admin import filter_admin_keyboard  # add to the existing admin keyboard import block


async def _admin_view(db, db_user):
    """Return (permissions, is_super) for keyboard filtering."""
    if settings.is_admin(db_user.telegram_id):
        return None, True
    from app.database.crud.bot_role import BotRoleCRUD

    role = await BotRoleCRUD.get_bot_role(db, db_user.id)
    return (list(role.permissions or []) if role else []), False
```

Then wrap each keyboard. `show_admin_panel` (line 61) becomes:

```python
    permissions, is_super = await _admin_view(db, db_user)
    keyboard = filter_admin_keyboard(
        get_admin_main_keyboard(db_user.language), permissions=permissions, is_super=is_super
    )
    await callback.message.edit_text(admin_text, reply_markup=keyboard)
    await callback.answer()
```

Apply the same pattern to each submenu handler, filtering the submenu keyboard before passing it as `reply_markup`:

- `show_users_submenu` (line 67) → `get_admin_users_submenu_keyboard`
- `show_promo_submenu` (line 81) → `get_admin_promo_submenu_keyboard`
- `show_communications_submenu` (line 95) → `get_admin_communications_submenu_keyboard`
- `show_support_submenu` (line 109) → `get_admin_support_submenu_keyboard`
- `show_settings_submenu` (line 242) → `get_admin_settings_submenu_keyboard`
- `show_system_submenu` (line 256) → `get_admin_system_submenu_keyboard`

For example, `show_users_submenu`:

```python
    permissions, is_super = await _admin_view(db, db_user)
    keyboard = filter_admin_keyboard(
        get_admin_users_submenu_keyboard(db_user.language), permissions=permissions, is_super=is_super
    )
    await callback.message.edit_text(
        texts.t('ADMIN_USERS_SUBMENU_TITLE', '👥 **Управление пользователями и подписками**\n\n')
        + texts.t('ADMIN_SUBMENU_SELECT_SECTION', 'Выберите нужный раздел:'),
        reply_markup=keyboard,
        parse_mode='Markdown',
    )
```

- [ ] **Step 6: Verify main module imports and all new tests pass**

Run: `.venv/Scripts/python.exe -c "import app.handlers.admin.main; print('ok')"`
Expected: prints `ok`.

Run: `.venv/Scripts/python.exe -m pytest tests/keyboards/test_admin_keyboard_filter.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/keyboards/__init__.py tests/keyboards/test_admin_keyboard_filter.py app/keyboards/admin.py app/handlers/admin/main.py
git commit -m "feat(admin): filter admin keyboards by granted sections"
```

---

## Task 7: Full verification of the new suite

**Files:** none (verification only)

- [ ] **Step 1: Run every test added by this plan**

Run:
```
.venv/Scripts/python.exe -m pytest tests/database/test_bot_role_crud.py tests/handlers/admin/test_bot_roles_fsm.py tests/utils/test_super_admin_required.py tests/middlewares/test_admin_permission.py tests/keyboards/test_admin_keyboard_filter.py -v
```
Expected: all PASS.

- [ ] **Step 2: Import smoke-check the touched app modules**

Run:
```
.venv/Scripts/python.exe -c "import app.bot, app.handlers.admin.main, app.handlers.admin.bot_roles, app.keyboards.admin, app.middlewares.admin_permission, app.utils.decorators; print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 3: Manual acceptance checklist (record results in the PR)**

Against a running bot (super-admin ADMIN_IDS + one test user):
1. Grant the test user a role with only `support`. Confirm they can open Support and get ACCESS_DENIED on Users/Payments/Settings.
2. Confirm the test user does NOT see the `👑 Роли` button and cannot open `admin_bot_roles`.
3. Confirm super-admin still sees and can use everything.
4. Restart the bot, reopen an existing role's permission editor, tap Save — confirm it does NOT wipe (shows "Сессия истекла").

---

## Self-Review

**Spec coverage:**
- C1 (register middleware) → Task 4. ✓
- C2 (audit map + log unmapped) → Task 5. ✓
- C3 (super-admin-only role mgmt) → Task 3. ✓
- C4 (keyboard filtering, `👑 Роли` super-only) → Task 6. ✓
- C5 (FSM wipe, empty-role, NULL crash, created_by, B1 message) → Tasks 1 & 2. ✓
- Success criteria (support-only admin restricted; superadmin full; empty role denied per section; role mgmt super-only; filtered keyboard; tests) → covered across Tasks 1-7 + Task 7 acceptance list. ✓

**Placeholder scan:** No TBD/TODO; every code and test step shows full content. ✓

**Type consistency:** `filter_admin_keyboard(markup, *, permissions, is_super)`, `_admin_view(db, db_user) -> (permissions, is_super)`, `super_admin_required(func)`, `resolve_admin_section(cb) -> str | None`, `set_bot_role(db, user_id, permissions, created_by=None)` used identically across tasks. ✓

**Ordering caveat verified:** in Task 5, `admin_mon_settings` (settings group, earlier in file) resolves before the broader `admin_mon_` (analytics group, later) — test `test_map_covers_known_gaps` asserts both. ✓
