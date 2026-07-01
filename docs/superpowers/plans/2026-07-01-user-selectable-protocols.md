# User-Selectable Protocols (internal-squads) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a subscriber freely toggle which RemnaWave internal-squads (protocols) are active on their subscription, from a new "Протоколы" screen in subscription settings, with one admin-designated squad as the default "main".

**Architecture:** Reuse the existing `ServerSquad` model + `Subscription.connected_squads` JSON list + existing panel-push (`update_remnawave_user(sync_squads=True)`). Add a single `is_default` flag to `ServerSquad` (the "main"). The pool of selectable protocols = `is_available=True` squads filtered by promo group. Selection is free (no charge), multi-select, minimum one. Default assignment on purchase is untouched; a pure helper `resolve_effective_squads` treats an empty list as "main".

**Tech Stack:** Python 3.13, aiogram (Telegram), SQLAlchemy async, Alembic, pytest. Spec: [2026-07-01-user-selectable-protocols-design.md](../specs/2026-07-01-user-selectable-protocols-design.md).

**Test runner (project quirk):** use the venv interpreter. On Windows PowerShell:
`.venv\Scripts\python.exe -m pytest <path>::<test> -v`
Tests stub `aiosqlite`/`asyncpg` (no live DB) — DB code is tested with mocks; pure functions are tested directly.

---

## File Structure

- `app/database/models.py` — add `ServerSquad.is_default` column.
- `migrations/alembic/versions/0119_add_is_default_to_server_squads.py` — new migration (Create).
- `app/database/crud/server_squad.py` — add `resolve_effective_squads` (pure), `get_default_protocol_squad_uuid`, `set_default_server_squad`.
- `app/keyboards/inline.py` — add `get_manage_protocols_keyboard`; add "🧩 Протоколы" button in `get_updated_subscription_settings_keyboard`.
- `app/handlers/subscription/protocols.py` — new user-facing handler (Create).
- `app/handlers/subscription/purchase.py` — register the three protocol callbacks.
- `app/handlers/admin/servers.py` — "⭐ Сделать основным" badge + button + handler + registration.
- Tests under `tests/` (see each task).

---

## Task 1: Add `is_default` column to ServerSquad

**Files:**
- Modify: `app/database/models.py:3291` (inside `class ServerSquad`)
- Test: `tests/database/test_server_squad_is_default.py`

- [ ] **Step 1: Write the failing test**

Create `tests/database/test_server_squad_is_default.py`:

```python
def test_server_squad_has_is_default_column():
    from app.database.models import ServerSquad

    columns = ServerSquad.__table__.columns
    assert 'is_default' in columns
    assert columns['is_default'].nullable is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/database/test_server_squad_is_default.py -v`
Expected: FAIL — `assert 'is_default' in columns` (KeyError/AssertionError).

- [ ] **Step 3: Add the column**

In `app/database/models.py`, inside `class ServerSquad`, right after the line:

```python
    is_trial_eligible = Column(Boolean, default=False, nullable=False)
```

add:

```python
    is_default = Column(Boolean, default=False, nullable=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/database/test_server_squad_is_default.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/database/models.py tests/database/test_server_squad_is_default.py
git commit -m "feat(squads): add is_default flag to ServerSquad model"
```

---

## Task 2: Alembic migration for `is_default`

**Files:**
- Create: `migrations/alembic/versions/0119_add_is_default_to_server_squads.py`

> Head note: the real Alembic head on this branch is `0118` (files run 0001..0118; nothing revises 0118). The new migration chains onto it as `0119`.

- [ ] **Step 1: Confirm 0118 is the current head**

Run: `git grep -n "down_revision" -- migrations/alembic/versions | Select-String "'0118'"`
Expected: NO output (nothing already revises 0118 → it is the head). If something does, bump the new revision id accordingly.

- [ ] **Step 2: Create the migration file**

Create `migrations/alembic/versions/0119_add_is_default_to_server_squads.py`:

```python
"""add is_default to server_squads

Revision ID: 0119
Revises: 0118
Create Date: 2026-07-01

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0119'
down_revision: Union[str, None] = '0118'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'server_squads',
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )


def downgrade() -> None:
    op.drop_column('server_squads', 'is_default')
```

- [ ] **Step 3: Sanity-check the file imports**

Run: `.venv\Scripts\python.exe -c "import ast; ast.parse(open('migrations/alembic/versions/0119_add_is_default_to_server_squads.py', encoding='utf-8').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add migrations/alembic/versions/0119_add_is_default_to_server_squads.py
git commit -m "feat(squads): migration add is_default to server_squads"
```

---

## Task 3: Pure helper `resolve_effective_squads`

**Files:**
- Modify: `app/database/crud/server_squad.py` (add after `get_effective_tariff_squad_uuids`, ~line 178)
- Test: `tests/database/crud/test_resolve_effective_squads.py`

- [ ] **Step 1: Write the failing test**

Create `tests/database/crud/test_resolve_effective_squads.py`:

```python
def test_resolve_effective_squads():
    from app.database.crud.server_squad import resolve_effective_squads

    assert resolve_effective_squads(['a', 'b'], 'd') == ['a', 'b']
    assert resolve_effective_squads([], 'd') == ['d']
    assert resolve_effective_squads(None, 'd') == ['d']
    assert resolve_effective_squads([], None) == []
    assert resolve_effective_squads(['a', 'a'], 'd') == ['a']
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/database/crud/test_resolve_effective_squads.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_effective_squads'`.

- [ ] **Step 3: Implement the helper**

In `app/database/crud/server_squad.py`, add after the `get_effective_tariff_squad_uuids` function (after line 177):

```python
def resolve_effective_squads(
    connected_squads: list[str] | None,
    default_uuid: str | None,
) -> list[str]:
    """Return connected squads, falling back to the default (main) squad when empty.

    Pure function — no DB. Deduplicates while preserving order.
    """
    squads = [squad_uuid for squad_uuid in (connected_squads or []) if squad_uuid]
    if squads:
        return list(dict.fromkeys(squads))
    if default_uuid:
        return [default_uuid]
    return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/database/crud/test_resolve_effective_squads.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/database/crud/server_squad.py tests/database/crud/test_resolve_effective_squads.py
git commit -m "feat(squads): add resolve_effective_squads pure helper"
```

---

## Task 4: CRUD `get_default_protocol_squad_uuid` + `set_default_server_squad`

**Files:**
- Modify: `app/database/crud/server_squad.py` (add after `resolve_effective_squads`)
- Test: `tests/database/crud/test_set_default_server_squad.py`

- [ ] **Step 1: Write the failing test**

Create `tests/database/crud/test_set_default_server_squad.py`:

```python
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.database.crud.server_squad as ss


@pytest.mark.asyncio
async def test_set_default_clears_others_and_sets_target(monkeypatch):
    server = SimpleNamespace(id=7, squad_uuid='u7', is_default=False)

    async def fake_get(db, server_id):
        return server

    monkeypatch.setattr(ss, 'get_server_squad_by_id', fake_get)

    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    result = await ss.set_default_server_squad(db, 7)

    # Two UPDATE statements: clear all, then set target.
    assert db.execute.await_count == 2
    db.commit.assert_awaited_once()
    assert result is server


@pytest.mark.asyncio
async def test_set_default_returns_none_when_missing(monkeypatch):
    async def fake_get(db, server_id):
        return None

    monkeypatch.setattr(ss, 'get_server_squad_by_id', fake_get)

    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    result = await ss.set_default_server_squad(db, 999)

    assert result is None
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/database/crud/test_set_default_server_squad.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'set_default_server_squad'`.

- [ ] **Step 3: Implement both functions**

In `app/database/crud/server_squad.py`, add right after `resolve_effective_squads`:

```python
async def get_default_protocol_squad_uuid(db: AsyncSession) -> str | None:
    """UUID of the default ("main") squad, or None if none is set."""
    result = await db.execute(
        select(ServerSquad.squad_uuid).where(ServerSquad.is_default.is_(True)).limit(1)
    )
    return result.scalar_one_or_none()


async def set_default_server_squad(db: AsyncSession, server_id: int) -> ServerSquad | None:
    """Mark one squad as the default (main), clearing the flag on all others."""
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        return None

    await db.execute(
        update(ServerSquad).where(ServerSquad.is_default.is_(True)).values(is_default=False)
    )
    await db.execute(
        update(ServerSquad).where(ServerSquad.id == server_id).values(is_default=True)
    )
    await db.commit()

    return await get_server_squad_by_id(db, server_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/database/crud/test_set_default_server_squad.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add app/database/crud/server_squad.py tests/database/crud/test_set_default_server_squad.py
git commit -m "feat(squads): add get_default_protocol_squad_uuid and set_default_server_squad"
```

---

## Task 5: Keyboard `get_manage_protocols_keyboard`

**Files:**
- Modify: `app/keyboards/inline.py` (add after `get_manage_countries_keyboard`, ~line 3309)
- Test: `tests/keyboards/test_manage_protocols_keyboard.py`

- [ ] **Step 1: Write the failing test**

Create `tests/keyboards/test_manage_protocols_keyboard.py`:

```python
def test_manage_protocols_keyboard_marks_selection_and_callbacks():
    from app.keyboards.inline import get_manage_protocols_keyboard

    pool = [{'uuid': 'a', 'name': 'Main'}, {'uuid': 'b', 'name': 'Extra'}]
    kb = get_manage_protocols_keyboard(pool, ['a'], 'ru')

    texts = [b.text for row in kb.inline_keyboard for b in row]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]

    assert any(t.startswith('✅') and 'Main' in t for t in texts)
    assert any(t.startswith('⚪') and 'Extra' in t for t in texts)
    assert 'nz!_protocol_toggle_a' in cbs
    assert 'nz!_protocol_toggle_b' in cbs
    assert 'nz!_protocols_apply' in cbs
    assert 'nz!_subscription_settings' in cbs  # back button
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/keyboards/test_manage_protocols_keyboard.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_manage_protocols_keyboard'`.

- [ ] **Step 3: Implement the keyboard**

In `app/keyboards/inline.py`, add immediately after `get_manage_countries_keyboard` returns (after line 3308):

```python
def get_manage_protocols_keyboard(
    protocols: list[dict],
    selected: list[str],
    language: str = DEFAULT_LANGUAGE,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = []

    for proto in protocols:
        uuid = proto['uuid']
        name = proto['name']
        icon = '✅' if uuid in selected else '⚪'
        buttons.append(
            [InlineKeyboardButton(text=f'{icon} {name}', callback_data=f'nz!_protocol_toggle_{uuid}')]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('PROTOCOLS_APPLY_BUTTON', '✅ Применить'),
                callback_data='nz!_protocols_apply',
                style='success',
            )
        ]
    )
    buttons.append(
        [InlineKeyboardButton(text=texts.BACK, callback_data='nz!_subscription_settings', style='danger')]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/keyboards/test_manage_protocols_keyboard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/keyboards/inline.py tests/keyboards/test_manage_protocols_keyboard.py
git commit -m "feat(protocols): add get_manage_protocols_keyboard"
```

---

## Task 6: "🧩 Протоколы" button in subscription settings keyboard

**Files:**
- Modify: `app/keyboards/inline.py:3714-3721` (inside `get_updated_subscription_settings_keyboard`)
- Test: `tests/keyboards/test_settings_has_protocols_button.py`

- [ ] **Step 1: Write the failing test**

Create `tests/keyboards/test_settings_has_protocols_button.py`:

```python
from types import SimpleNamespace


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_protocols_button_present_without_tariff():
    from app.keyboards.inline import get_updated_subscription_settings_keyboard

    kb = get_updated_subscription_settings_keyboard('ru', True, tariff=None, subscription=None)
    assert 'nz!_subscription_protocols' in _cbs(kb)


def test_protocols_button_present_with_tariff():
    from app.keyboards.inline import get_updated_subscription_settings_keyboard

    tariff = SimpleNamespace(device_price_kopeks=0)
    kb = get_updated_subscription_settings_keyboard('ru', True, tariff=tariff, subscription=None)
    assert 'nz!_subscription_protocols' in _cbs(kb)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/keyboards/test_settings_has_protocols_button.py -v`
Expected: FAIL — `assert 'nz!_subscription_protocols' in [...]`.

- [ ] **Step 3: Add the button (ungated by has_tariff)**

In `app/keyboards/inline.py`, inside `get_updated_subscription_settings_keyboard`, locate:

```python
    # Если подписка на тарифе - отключаем страны, модем, трафик
    has_tariff = tariff is not None

    # Для суточных тарифов кнопка паузы теперь в главном меню подписки

    if show_countries_management and not has_tariff:
```

Insert the protocols button between the `has_tariff` assignment block and the `if show_countries_management` line:

```python
    # Для суточных тарифов кнопка паузы теперь в главном меню подписки

    keyboard.append(
        [
            InlineKeyboardButton(
                text=texts.t('PROTOCOLS_BUTTON', '🧩 Протоколы'),
                callback_data='nz!_subscription_protocols',
                style='primary',
            )
        ]
    )

    if show_countries_management and not has_tariff:
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/keyboards/test_settings_has_protocols_button.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add app/keyboards/inline.py tests/keyboards/test_settings_has_protocols_button.py
git commit -m "feat(protocols): add Протоколы button to subscription settings"
```

---

## Task 7: User handler `protocols.py` (open / toggle / apply)

**Files:**
- Create: `app/handlers/subscription/protocols.py`
- Test: `tests/handlers/test_protocols_handler.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/handlers/test_protocols_handler.py`:

```python
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.handlers.subscription.common as common
import app.handlers.subscription.protocols as protocols


def _cb(data):
    msg = SimpleNamespace(
        edit_text=AsyncMock(), edit_reply_markup=AsyncMock(), answer=AsyncMock()
    )
    return SimpleNamespace(data=data, message=msg, answer=AsyncMock(), bot=None)


def _user():
    return SimpleNamespace(id=1, language='ru', promo_group_id=None)


class _State:
    def __init__(self, data):
        self._d = dict(data)

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)


def test_validate_protocol_selection():
    assert protocols.validate_protocol_selection(['a']) is True
    assert protocols.validate_protocol_selection([]) is False
    assert protocols.validate_protocol_selection(['', None]) is False


def _patch_available(monkeypatch, squads):
    async def fake_avail(db, promo_group_id=None):
        return squads

    monkeypatch.setattr(
        'app.database.crud.server_squad.get_available_server_squads', fake_avail
    )


@pytest.mark.asyncio
async def test_apply_writes_and_pushes(monkeypatch):
    sub = SimpleNamespace(id=5, user_id=1, connected_squads=['a'], updated_at=None)

    async def fake_resolve(cb, u, db, state=None):
        return sub, 5

    monkeypatch.setattr(common, 'resolve_subscription_from_context', fake_resolve)
    _patch_available(
        monkeypatch,
        [
            SimpleNamespace(squad_uuid='a', display_name='Main'),
            SimpleNamespace(squad_uuid='b', display_name='Extra'),
        ],
    )

    push = AsyncMock()
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService.update_remnawave_user', push
    )

    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    state = _State({'protocols': ['b']})
    cb = _cb('nz!_protocols_apply')

    await protocols.apply_protocols_changes(cb, _user(), db, state)

    assert sub.connected_squads == ['b']
    db.commit.assert_awaited_once()
    push.assert_awaited()


@pytest.mark.asyncio
async def test_apply_blocks_empty_selection(monkeypatch):
    sub = SimpleNamespace(id=5, user_id=1, connected_squads=['a'], updated_at=None)

    async def fake_resolve(cb, u, db, state=None):
        return sub, 5

    monkeypatch.setattr(common, 'resolve_subscription_from_context', fake_resolve)
    _patch_available(monkeypatch, [SimpleNamespace(squad_uuid='a', display_name='Main')])

    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    state = _State({'protocols': []})
    cb = _cb('nz!_protocols_apply')

    await protocols.apply_protocols_changes(cb, _user(), db, state)

    cb.answer.assert_awaited()
    db.commit.assert_not_awaited()
    assert sub.connected_squads == ['a']
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/handlers/test_protocols_handler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.handlers.subscription.protocols'`.

- [ ] **Step 3: Create the handler module**

Create `app/handlers/subscription/protocols.py`:

```python
from datetime import UTC, datetime

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.inline import get_manage_protocols_keyboard
from app.localization.texts import get_texts

from . import common
from .common import logger


def validate_protocol_selection(selected: list[str]) -> bool:
    """At least one non-empty protocol must remain selected."""
    return len([s for s in (selected or []) if s]) >= 1


async def _build_protocol_pool(db: AsyncSession, promo_group_id, current: list[str] | None) -> list[dict]:
    """Visible squads (by promo group) plus any currently-active squad that fell out of view."""
    from app.database.crud.server_squad import (
        get_available_server_squads,
        get_server_squads_by_uuids,
    )

    squads = await get_available_server_squads(db, promo_group_id=promo_group_id)
    pool = [{'uuid': s.squad_uuid, 'name': s.display_name} for s in squads if s.squad_uuid]

    known = {p['uuid'] for p in pool}
    extra_uuids = [u for u in (current or []) if u and u not in known]
    if extra_uuids:
        for s in await get_server_squads_by_uuids(db, extra_uuids):
            pool.append({'uuid': s.squad_uuid, 'name': s.display_name})

    return pool


async def handle_manage_protocols(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    from app.database.crud.server_squad import (
        get_default_protocol_squad_uuid,
        resolve_effective_squads,
    )

    texts = get_texts(db_user.language)
    subscription, sub_id = await common.resolve_subscription_from_context(callback, db_user, db, state)
    if subscription is None:
        return

    default_uuid = await get_default_protocol_squad_uuid(db)
    current = resolve_effective_squads(subscription.connected_squads, default_uuid)
    pool = await _build_protocol_pool(db, db_user.promo_group_id, current)

    await state.update_data(protocols=list(current))

    text = texts.t(
        'PROTOCOLS_SCREEN_TITLE',
        '🧩 <b>Протоколы</b>\n\nВыберите активные протоколы (минимум один):',
    )
    await callback.message.edit_text(
        text,
        reply_markup=get_manage_protocols_keyboard(pool, list(current), db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


async def handle_toggle_protocol(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    texts = get_texts(db_user.language)
    uuid = callback.data.split('nz!_protocol_toggle_', 1)[1]

    data = await state.get_data()
    selected = list(data.get('protocols', []))

    pool = await _build_protocol_pool(db, db_user.promo_group_id, selected)
    allowed = {p['uuid'] for p in pool}
    if uuid not in allowed:
        await callback.answer(
            texts.t('PROTOCOL_NOT_AVAILABLE', '❌ Протокол недоступен'),
            show_alert=True,
        )
        return

    if uuid in selected:
        if len([s for s in selected if s]) <= 1:
            await callback.answer(
                texts.t('PROTOCOLS_MIN_ONE_ALERT', '❌ Нужен хотя бы один протокол'),
                show_alert=True,
            )
            return
        selected.remove(uuid)
    else:
        selected.append(uuid)

    await state.update_data(protocols=selected)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_manage_protocols_keyboard(pool, selected, db_user.language)
        )
    except Exception as e:
        logger.error('Ошибка обновления клавиатуры протоколов', error=e)

    await callback.answer()


async def apply_protocols_changes(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    texts = get_texts(db_user.language)
    subscription, sub_id = await common.resolve_subscription_from_context(callback, db_user, db, state)
    if subscription is None:
        return

    data = await state.get_data()
    raw_selected = [u for u in data.get('protocols', []) if u]

    pool = await _build_protocol_pool(db, db_user.promo_group_id, raw_selected)
    allowed = {p['uuid'] for p in pool}
    selected = list(dict.fromkeys(u for u in raw_selected if u in allowed))

    if not validate_protocol_selection(selected):
        await callback.answer(
            texts.t('PROTOCOLS_MIN_ONE_ALERT', '❌ Нужен хотя бы один протокол'),
            show_alert=True,
        )
        return

    subscription.connected_squads = selected
    subscription.updated_at = datetime.now(UTC)
    await db.commit()

    from app.services.subscription_service import SubscriptionService

    service = SubscriptionService()
    try:
        await service.update_remnawave_user(db, subscription, sync_squads=True)
    except Exception as rw_err:
        logger.error('Ошибка синхронизации протоколов с RemnaWave', error=rw_err)
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            action='update',
        )

    await db.refresh(subscription)

    await state.update_data(protocols=list(selected))
    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_manage_protocols_keyboard(pool, selected, db_user.language)
        )
    except Exception as e:
        logger.error('Ошибка обновления клавиатуры протоколов', error=e)

    await callback.answer(
        texts.t('PROTOCOLS_UPDATED', '✅ Протоколы обновлены'),
        show_alert=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/handlers/test_protocols_handler.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/handlers/subscription/protocols.py tests/handlers/test_protocols_handler.py
git commit -m "feat(protocols): add user protocol selection handler"
```

---

## Task 8: Register protocol callbacks

**Files:**
- Modify: `app/handlers/subscription/purchase.py` (module imports ~line 136; register block ~line 4286)

- [ ] **Step 1: Add the module-level import**

In `app/handlers/subscription/purchase.py`, near the other subscription-handler imports (around line 136 where `apply_countries_changes` is imported), add a new import block:

```python
from app.handlers.subscription.protocols import (
    apply_protocols_changes,
    handle_manage_protocols,
    handle_toggle_protocol,
)
```

- [ ] **Step 2: Register the three callbacks**

In the same file, find the registration lines (around line 4284-4286):

```python
    dp.callback_query.register(handle_manage_country, F.data.startswith('nz!_country_manage_'))

    dp.callback_query.register(apply_countries_changes, F.data == 'nz!_countries_apply')
```

Add immediately after them:

```python
    dp.callback_query.register(handle_manage_protocols, F.data == 'nz!_subscription_protocols')

    dp.callback_query.register(handle_toggle_protocol, F.data.startswith('nz!_protocol_toggle_'))

    dp.callback_query.register(apply_protocols_changes, F.data == 'nz!_protocols_apply')
```

- [ ] **Step 3: Verify the module imports cleanly**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.purchase; print('ok')"`
Expected: `ok` (no ImportError / circular import).

- [ ] **Step 4: Commit**

```bash
git add app/handlers/subscription/purchase.py
git commit -m "feat(protocols): register protocol selection callbacks"
```

---

## Task 9: Admin — mark a squad as "main"

**Files:**
- Modify: `app/handlers/admin/servers.py:31-100` (view builder), add handler near `toggle_server_availability` (~line 436), register near line 1141
- Test: `tests/handlers/admin/test_server_set_default.py`

- [ ] **Step 1: Write the failing test**

Create `tests/handlers/admin/test_server_set_default.py`:

```python
from types import SimpleNamespace


def _server(is_default=False):
    return SimpleNamespace(
        id=7,
        squad_uuid='u7',
        display_name='Main',
        original_name=None,
        is_available=True,
        is_trial_eligible=False,
        is_default=is_default,
        price_kopeks=0,
        price_rubles=0.0,
        country_code=None,
        max_users=None,
        current_users=0,
        allowed_promo_groups=[],
        description=None,
    )


def test_edit_view_has_set_default_button_and_badge():
    from app.handlers.admin.servers import _build_server_edit_view

    text, kb = _build_server_edit_view(_server(is_default=False))
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]

    assert 'admin_server_set_default_7' in cbs
    assert '⭐' in text  # main/badge marker present in the card text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/handlers/admin/test_server_set_default.py -v`
Expected: FAIL — `assert 'admin_server_set_default_7' in cbs`.

- [ ] **Step 3: Add badge to the card text**

In `app/handlers/admin/servers.py`, inside `_build_server_edit_view`, after:

```python
    trial_status = '✅ Да' if server.is_trial_eligible else '⚪️ Нет'
```

add:

```python
    default_status = '⭐ Основной' if getattr(server, 'is_default', False) else '⚪️ Обычный'
```

Then in the card text f-string, change the line:

```python
• Выдача триала: {trial_status}
```

to:

```python
• Выдача триала: {trial_status}
• Тип: {default_status}
```

- [ ] **Step 4: Add the button to the keyboard**

In the same `_build_server_edit_view`, in the `keyboard` list, add a new row right before the `❌ Отключить / ✅ Включить` row:

```python
        [
            types.InlineKeyboardButton(
                text='⭐ Основной (уже)' if getattr(server, 'is_default', False) else '⭐ Сделать основным',
                callback_data=f'admin_server_set_default_{server.id}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text='❌ Отключить' if server.is_available else '✅ Включить',
                callback_data=f'admin_server_toggle_{server.id}',
            )
        ],
```

- [ ] **Step 5: Run the view test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/handlers/admin/test_server_set_default.py -v`
Expected: PASS.

- [ ] **Step 6: Add the callback handler**

In `app/handlers/admin/servers.py`, add after `toggle_server_availability` (after line 457):

```python
@admin_required
@error_handler
async def set_server_as_default(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])

    from app.database.crud.server_squad import set_default_server_squad

    server = await set_default_server_squad(db, server_id)
    if not server:
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    await cache.delete_pattern('available_countries*')
    await callback.answer('⭐ Назначен основным!')

    server = await get_server_squad_by_id(db, server_id)
    text, keyboard = _build_server_edit_view(server)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
```

- [ ] **Step 7: Register the handler**

In the register section (near line 1141, after the `toggle_server_availability` registration), add:

```python
    dp.callback_query.register(set_server_as_default, F.data.startswith('admin_server_set_default_'))
```

- [ ] **Step 8: Verify the module imports cleanly**

Run: `.venv\Scripts\python.exe -c "import app.handlers.admin.servers; print('ok')"`
Expected: `ok`

- [ ] **Step 9: Commit**

```bash
git add app/handlers/admin/servers.py tests/handlers/admin/test_server_set_default.py
git commit -m "feat(protocols): admin action to mark a squad as main/default"
```

---

## Task 10: Full-suite smoke + wrap-up

**Files:** none (verification only)

- [ ] **Step 1: Run all new tests together**

Run:
```
.venv\Scripts\python.exe -m pytest tests/database/test_server_squad_is_default.py tests/database/crud/test_resolve_effective_squads.py tests/database/crud/test_set_default_server_squad.py tests/keyboards/test_manage_protocols_keyboard.py tests/keyboards/test_settings_has_protocols_button.py tests/handlers/test_protocols_handler.py tests/handlers/admin/test_server_set_default.py -v
```
Expected: all PASS.

- [ ] **Step 2: Import-smoke the touched modules**

Run:
```
.venv\Scripts\python.exe -c "import app.handlers.subscription.protocols, app.handlers.subscription.purchase, app.handlers.admin.servers, app.keyboards.inline, app.database.crud.server_squad; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Manual verification checklist (document results in the PR)**

  1. Admin → Серверы → open a squad → "⭐ Сделать основным" → badge shows "⭐ Основной"; exactly one squad is main.
  2. User → subscription → Настройки → "🧩 Протоколы" appears (with and without tariff).
  3. Toggle squads: cannot uncheck the last one (alert). "✅ Применить" updates the checkmarks.
  4. Confirm the RemnaWave panel user's `activeInternalSquads` reflects the new selection.
  5. No balance was charged (free).

---

## Self-Review

**Spec coverage:**
- Model `is_default` → Task 1 + migration Task 2. ✓
- Pool = `is_available` + promo group → reuses `get_available_server_squads` in Task 7. ✓
- `get_default_protocol_squad_uuid`, `set_default_server_squad`, `resolve_effective_squads` → Tasks 3, 4. ✓
- Admin badge + "make main" (single-true) → Task 9 + `set_default_server_squad`. ✓
- User "Протоколы" screen, free, multi-select, min-1, panel push → Tasks 5, 6, 7, 8. ✓
- Default-on-empty via `resolve_effective_squads` → Task 3, used in open handler Task 7. ✓
- Edge case: active-but-invisible squad kept → `_build_protocol_pool` extras, Task 7. ✓
- Panel push via existing `update_remnawave_user(sync_squads=True)` + retry queue → Task 7. ✓
- Purchase/trial flow untouched → no task modifies it (by design). ✓
- Localization → inline `texts.t(KEY, 'default')` fallbacks (project pattern; JSON keys optional, can be added later). ✓

**Placeholder scan:** No TBD/TODO; every code step has full code. ✓

**Type/name consistency:** `is_default` (model/migration/crud/admin); `resolve_effective_squads(connected, default)`; `get_default_protocol_squad_uuid(db)`; `set_default_server_squad(db, server_id)`; `get_manage_protocols_keyboard(protocols, selected, language)`; `validate_protocol_selection(selected)`; `_build_protocol_pool(db, promo_group_id, current)`; callbacks `nz!_subscription_protocols`, `nz!_protocol_toggle_<uuid>`, `nz!_protocols_apply`, `admin_server_set_default_<id>` — all consistent across tasks. ✓

**Known scope decision:** The protocols screen is reached from the subscription-settings screen, which is paid-only in the current code (`SUBSCRIPTION_SETTINGS_PAID_ONLY`). Trial users still receive the main squad by default; in-settings toggling for trials is out of scope for v1 (matches existing settings gating).
