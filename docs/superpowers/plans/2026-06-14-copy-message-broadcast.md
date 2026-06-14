# Copy-message Broadcast Mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bot admin broadcast mode that copies an admin-composed Telegram message 1:1 to the audience via `bot.copy_message`, plus a test-send (to self or a telegram_id) for both the new copy mode and the existing HTML mode.

**Architecture:** Reuse the existing bot broadcast flow in `app/handlers/admin/messages.py` (target selection, button selector, batched send loop, progress, history). Extract the per-recipient delivery into one shared module-level helper `_deliver_broadcast_to(...)` that branches on `mode` ('copy' | 'html'); the send loop and the test-send both call it. A `broadcast_mode` flag in FSM state routes the compose step (type text vs send a message to copy).

**Tech Stack:** Python 3.13, aiogram 3, pytest (`.venv/Scripts/python.exe -m pytest`).

---

## File Structure

- `app/states.py` — add two FSM states.
- `app/handlers/admin/messages.py` — shared delivery helper, copy entry + capture handler, mode branches in target-selection / preview / confirm, test-send handlers, registration.
- `app/keyboards/admin.py` (or wherever `get_admin_messages_keyboard` lives) — new "📋 Рассылка копией" entry button; reuse confirm keyboard built inline in `messages.py`.
- `tests/handlers/test_broadcast_delivery.py` — new: tests for `_deliver_broadcast_to` (both modes) and the test-send helper.
- `tests/handlers/test_broadcast_copy_capture.py` — new: capture handler stores copy ids.

Conventions: run tests with `.venv/Scripts/python.exe -m pytest <path> -q`. Commit after each task.

---

## Task 1: FSM states

**Files:**
- Modify: `app/states.py:94-96`

- [ ] **Step 1: Add states** after `waiting_for_broadcast_media` / `confirming_broadcast`:

```python
    waiting_for_broadcast_message = State()
    waiting_for_broadcast_media = State()
    confirming_broadcast = State()
    waiting_for_broadcast_copy_source = State()
    waiting_for_broadcast_test_user_id = State()
```

- [ ] **Step 2: Commit**

```bash
git add app/states.py
git commit -m "feat(broadcast): add copy-source and test-user FSM states"
```

---

## Task 2: Shared delivery helper `_deliver_broadcast_to` (TDD)

The raw per-recipient send, branchable by mode. No retry/flood logic here (that stays in the loop). Reused by the send loop and test-send.

**Files:**
- Create: `tests/handlers/test_broadcast_delivery.py`
- Modify: `app/handlers/admin/messages.py` (add module-level helper near other broadcast helpers, ~after line 99)

- [ ] **Step 1: Write the failing test**

```python
# tests/handlers/test_broadcast_delivery.py
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin.messages import _deliver_broadcast_to


@pytest.fixture
def bot():
    return SimpleNamespace(
        copy_message=AsyncMock(),
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_video=AsyncMock(),
        send_document=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_copy_mode_calls_copy_message(bot):
    kb = object()
    await _deliver_broadcast_to(
        bot, 555, mode='copy', message_text='', media_type=None, media_file_id=None,
        copy_from_chat_id=111, copy_source_message_id=222, reply_markup=kb,
    )
    bot.copy_message.assert_awaited_once_with(
        chat_id=555, from_chat_id=111, message_id=222, reply_markup=kb,
    )
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_html_text_mode_calls_send_message(bot):
    kb = object()
    await _deliver_broadcast_to(
        bot, 555, mode='html', message_text='hi', media_type=None, media_file_id=None,
        copy_from_chat_id=None, copy_source_message_id=None, reply_markup=kb,
    )
    bot.send_message.assert_awaited_once_with(
        chat_id=555, text='hi', parse_mode='HTML', reply_markup=kb,
    )
    bot.copy_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_html_photo_mode_calls_send_photo(bot):
    kb = object()
    await _deliver_broadcast_to(
        bot, 555, mode='html', message_text='cap', media_type='photo', media_file_id='fid',
        copy_from_chat_id=None, copy_source_message_id=None, reply_markup=kb,
    )
    bot.send_photo.assert_awaited_once_with(
        chat_id=555, photo='fid', caption='cap', parse_mode='HTML', reply_markup=kb,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_broadcast_delivery.py -q`
Expected: FAIL — `ImportError: cannot import name '_deliver_broadcast_to'`.

- [ ] **Step 3: Implement the helper** in `app/handlers/admin/messages.py` (module level, after `create_broadcast_keyboard`):

```python
async def _deliver_broadcast_to(
    bot,
    telegram_id: int,
    *,
    mode: str,
    message_text: str,
    media_type: str | None,
    media_file_id: str | None,
    copy_from_chat_id: int | None,
    copy_source_message_id: int | None,
    reply_markup=None,
) -> None:
    """Deliver ONE broadcast message to a recipient. Raises Telegram errors to the caller.

    mode == 'copy' -> bot.copy_message (1:1 copy of a source message).
    mode == 'html' -> send_message / send_<media> with parse_mode='HTML' (legacy).
    """
    if mode == 'copy':
        await bot.copy_message(
            chat_id=telegram_id,
            from_chat_id=copy_from_chat_id,
            message_id=copy_source_message_id,
            reply_markup=reply_markup,
        )
        return

    if media_file_id and media_type in ('photo', 'video', 'document'):
        send_method = {
            'photo': bot.send_photo,
            'video': bot.send_video,
            'document': bot.send_document,
        }[media_type]
        media_kwarg = {'photo': 'photo', 'video': 'video', 'document': 'document'}[media_type]
        if len(message_text) <= 1024:
            await send_method(
                chat_id=telegram_id,
                **{media_kwarg: media_file_id},
                caption=message_text,
                parse_mode='HTML',
                reply_markup=reply_markup,
            )
        else:
            await send_method(chat_id=telegram_id, **{media_kwarg: media_file_id})
            await bot.send_message(
                chat_id=telegram_id, text=message_text, parse_mode='HTML', reply_markup=reply_markup
            )
        return

    await bot.send_message(
        chat_id=telegram_id, text=message_text, parse_mode='HTML', reply_markup=reply_markup
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_broadcast_delivery.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Refactor `send_single_broadcast` to call the helper.** In `confirm_broadcast` (`app/handlers/admin/messages.py`, the `try:` body at ~lines 1265-1314), replace the inline send block with:

```python
                await _deliver_broadcast_to(
                    callback.bot,
                    telegram_id,
                    mode=broadcast_mode,
                    message_text=message_text,
                    media_type=media_type,
                    media_file_id=media_file_id if has_media else None,
                    copy_from_chat_id=copy_from_chat_id,
                    copy_source_message_id=copy_source_message_id,
                    reply_markup=broadcast_keyboard,
                )
                return 'sent'
```

(where `broadcast_mode`, `copy_from_chat_id`, `copy_source_message_id` are read from `data` at the top of `confirm_broadcast` — see Task 4.)

- [ ] **Step 6: Run the broadcast test module**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_broadcast_delivery.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/handlers/admin/messages.py tests/handlers/test_broadcast_delivery.py
git commit -m "feat(broadcast): extract shared _deliver_broadcast_to delivery helper"
```

---

## Task 3: Copy-mode entry, target branch, capture handler (TDD for capture)

**Files:**
- Modify: keyboard module with `get_admin_messages_keyboard` — add entry button.
- Modify: `app/handlers/admin/messages.py` — `start_copy_broadcast`, branch next-state on mode in `select_broadcast_target` (~line 776) and `show_custom_broadcast` (~line 724), `process_broadcast_copy_source`.
- Create: `tests/handlers/test_broadcast_copy_capture.py`

- [ ] **Step 1: Add entry button.** In `get_admin_messages_keyboard`, add a row:

```python
[types.InlineKeyboardButton(text='📋 Рассылка копией', callback_data='admin_msg_copy')]
```

- [ ] **Step 2: Add `start_copy_broadcast`** in `messages.py` — sets mode then shows the standard "all/by-sub" target menu (reuse `show_broadcast_targets`):

```python
@admin_required
@error_handler
async def start_copy_broadcast(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    await state.update_data(broadcast_mode='copy')
    await show_broadcast_targets(callback, db_user, state)
```

- [ ] **Step 3: Default mode = 'html' for the legacy entries.** In `show_broadcast_targets` (line 539), at the start set the default when not copy:

```python
    data = await state.get_data()
    if data.get('broadcast_mode') != 'copy':
        await state.update_data(broadcast_mode='html')
```

- [ ] **Step 4: Branch next-state after target selection.** In `select_broadcast_target` (~line 776) and `show_custom_broadcast` (~line 724), replace `await state.set_state(AdminStates.waiting_for_broadcast_message)` with a mode branch:

```python
    data = await state.get_data()
    if data.get('broadcast_mode') == 'copy':
        await state.set_state(AdminStates.waiting_for_broadcast_copy_source)
        # use the same prompt-send mechanism already in this handler
        # (callback.message.edit_text / message.answer):
        await <prompt>(
            '📋 <b>Рассылка копией</b>\n\n'
            'Отправьте сообщение для рассылки — с любым форматированием/медиа.\n'
            'Бот скопирует его 1:1 каждому. <b>Не удаляйте</b> его до конца рассылки.',
            parse_mode='HTML',
        )
    else:
        await state.set_state(AdminStates.waiting_for_broadcast_message)
        await <existing text prompt>
```

- [ ] **Step 5: Write the failing capture test**

```python
# tests/handlers/test_broadcast_copy_capture.py
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handlers.admin.messages as m
from app.handlers.admin.messages import process_broadcast_copy_source


@pytest.mark.asyncio
async def test_capture_stores_copy_ids(monkeypatch):
    captured = {}

    async def update_data(**kw):
        captured.update(kw)

    # show_button_selector is exercised elsewhere; stub it here to isolate capture
    monkeypatch.setattr(m, 'show_button_selector', AsyncMock())

    state = SimpleNamespace(
        update_data=AsyncMock(side_effect=update_data),
        set_state=AsyncMock(),
        get_data=AsyncMock(return_value={'broadcast_mode': 'copy'}),
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=777), message_id=4242, content_type='text', answer=AsyncMock(),
    )
    db_user = SimpleNamespace(language='ru')

    await process_broadcast_copy_source(message, db_user, state)

    assert captured['copy_from_chat_id'] == 777
    assert captured['copy_source_message_id'] == 4242
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_broadcast_copy_capture.py -q`
Expected: FAIL — handler missing.

- [ ] **Step 7: Implement `process_broadcast_copy_source`** — stores ids, then reuses the existing button selector (which leads to the confirm preview):

```python
@admin_required
@error_handler
async def process_broadcast_copy_source(message: types.Message, db_user: User, state: FSMContext):
    if message.content_type in ('new_chat_members', 'left_chat_member', 'pinned_message'):
        await message.answer('❌ Это сообщение нельзя скопировать. Отправьте обычное сообщение.')
        return

    await state.update_data(
        copy_from_chat_id=message.chat.id,
        copy_source_message_id=message.message_id,
        has_media=False,
        media_type=None,
        media_file_id=None,
        broadcast_message='📋 Рассылка копией',
    )
    await show_button_selector(message, db_user, state)
```

Note: the `@admin_required`/`@error_handler` decorators may pass `db`/`state` positionally — match the signature of the sibling `process_broadcast_media` handler exactly.

- [ ] **Step 8: Run capture test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_broadcast_copy_capture.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/states.py app/handlers/admin/messages.py app/keyboards/admin.py tests/handlers/test_broadcast_copy_capture.py
git commit -m "feat(broadcast): copy-mode entry, target branch, source capture"
```

---

## Task 4: Mode-aware preview + confirm send

**Files:**
- Modify: `app/handlers/admin/messages.py` — the confirm-preview function (builds keyboard ~line 1102) and `confirm_broadcast` (~line 1163).

- [ ] **Step 1: Read `broadcast_mode` + copy ids at the top of `confirm_broadcast`** (after the existing `data = await state.get_data()`, ~line 1164):

```python
    broadcast_mode = data.get('broadcast_mode', 'html')
    copy_from_chat_id = data.get('copy_from_chat_id')
    copy_source_message_id = data.get('copy_source_message_id')
```

These are captured in the `send_single_broadcast` closure (Task 2 Step 5). `BroadcastHistory.message_text` is already `'📋 Рассылка копией'` for copy mode (set in capture).

- [ ] **Step 2: Make the preview copy-aware.** In the confirm-preview function, when `data.get('broadcast_mode') == 'copy'`: skip rendering `message_text`; send the audience summary as text, then copy the source message to the admin as a faithful preview:

```python
    if data.get('broadcast_mode') == 'copy':
        await safe_edit_or_send_text(callback, preview_text_summary, reply_markup=confirm_kb, parse_mode='HTML')
        await callback.bot.copy_message(
            chat_id=callback.message.chat.id,
            from_chat_id=data['copy_from_chat_id'],
            message_id=data['copy_source_message_id'],
            reply_markup=create_broadcast_keyboard(selected_buttons, db_user.language),
        )
        await callback.answer()
        return
```

where `preview_text_summary` omits the "📝 Сообщение" block (the copy preview is the copied message itself).

- [ ] **Step 3: No unit test for the giant handler** — the delivery primitive is already unit-tested (Task 2). Verified end-to-end in Task 7 by smoke-import + code review.

- [ ] **Step 4: Commit**

```bash
git add app/handlers/admin/messages.py
git commit -m "feat(broadcast): mode-aware preview and copy-mode send path"
```

---

## Task 5: Test-send (self / telegram_id) for both modes (TDD)

**Files:**
- Modify: confirm-preview keyboard (~line 1102) — add test buttons.
- Modify: `app/handlers/admin/messages.py` — `_send_test_broadcast`, `_delivery_kwargs_from_state`, `test_broadcast_self`, `prompt_test_user`, `process_test_user_id`.
- Modify: `tests/handlers/test_broadcast_delivery.py` — add test-send tests.

- [ ] **Step 1: Add test buttons** to the confirm keyboard (after the Отправить row):

```python
    keyboard.append([
        types.InlineKeyboardButton(text='🧪 Тест себе', callback_data='nz!_bcast_test_self'),
        types.InlineKeyboardButton(text='🧪 Тест пользователю', callback_data='nz!_bcast_test_user'),
    ])
```

- [ ] **Step 2: Write the failing tests** (append to `test_broadcast_delivery.py`):

```python
from app.handlers.admin.messages import _send_test_broadcast


@pytest.mark.asyncio
async def test_send_test_broadcast_copy(bot):
    ok, reason = await _send_test_broadcast(
        bot, 999,
        mode='copy', message_text='', media_type=None, media_file_id=None,
        copy_from_chat_id=11, copy_source_message_id=22, reply_markup=None,
    )
    assert ok is True
    bot.copy_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_test_broadcast_reports_blocked(bot):
    from aiogram.exceptions import TelegramForbiddenError
    bot.copy_message.side_effect = TelegramForbiddenError(method='copyMessage', message='blocked')
    ok, reason = await _send_test_broadcast(
        bot, 999, mode='copy', message_text='', media_type=None, media_file_id=None,
        copy_from_chat_id=11, copy_source_message_id=22, reply_markup=None,
    )
    assert ok is False
    assert 'не запускал' in reason or 'не найден' in reason
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_broadcast_delivery.py -q`
Expected: FAIL — `_send_test_broadcast` missing.

- [ ] **Step 4: Implement `_send_test_broadcast`** (module level):

```python
async def _send_test_broadcast(bot, telegram_id: int, **delivery_kwargs) -> tuple[bool, str]:
    """Deliver one message to telegram_id for preview. Returns (ok, reason)."""
    try:
        await _deliver_broadcast_to(bot, telegram_id, **delivery_kwargs)
        return True, ''
    except TelegramForbiddenError:
        return False, 'Пользователь не найден или не запускал бота'
    except TelegramBadRequest as e:
        err = str(e).lower()
        if 'chat not found' in err or 'user is deactivated' in err:
            return False, 'Пользователь не найден или не запускал бота'
        return False, f'Ошибка: {e}'
    except Exception as e:
        return False, f'Ошибка: {e}'
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_broadcast_delivery.py -q`
Expected: PASS.

- [ ] **Step 6: Implement the handlers** (build delivery kwargs from state, call helper, report, stay on confirm):

```python
def _delivery_kwargs_from_state(data, language):
    return dict(
        mode=data.get('broadcast_mode', 'html'),
        message_text=data.get('broadcast_message', ''),
        media_type=data.get('media_type'),
        media_file_id=data.get('media_file_id') if data.get('has_media') else None,
        copy_from_chat_id=data.get('copy_from_chat_id'),
        copy_source_message_id=data.get('copy_source_message_id'),
        reply_markup=create_broadcast_keyboard(
            data.get('selected_buttons') or list(DEFAULT_SELECTED_BUTTONS), language
        ),
    )


@admin_required
@error_handler
async def test_broadcast_self(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    data = await state.get_data()
    kw = _delivery_kwargs_from_state(data, db_user.language)
    ok, reason = await _send_test_broadcast(callback.bot, db_user.telegram_id, **kw)
    await callback.answer('✅ Тест отправлен' if ok else f'❌ {reason}', show_alert=not ok)


@admin_required
@error_handler
async def prompt_test_user(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_broadcast_test_user_id)
    await callback.message.answer('Введите telegram_id пользователя для теста (число):')
    await callback.answer()


@admin_required
@error_handler
async def process_test_user_id(message: types.Message, db_user: User, state: FSMContext):
    raw = (message.text or '').strip()
    if not raw.isdigit():
        await message.answer('❌ Нужен числовой telegram_id. Попробуйте ещё раз:')
        return
    data = await state.get_data()
    kw = _delivery_kwargs_from_state(data, db_user.language)
    ok, reason = await _send_test_broadcast(message.bot, int(raw), **kw)
    await message.answer('✅ Тест отправлен' if ok else f'❌ {reason}')
    await state.set_state(AdminStates.confirming_broadcast)
```

- [ ] **Step 7: Run tests**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_broadcast_delivery.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/handlers/admin/messages.py tests/handlers/test_broadcast_delivery.py
git commit -m "feat(broadcast): test-send to self or telegram_id (both modes)"
```

---

## Task 6: Register all new handlers

**Files:**
- Modify: `app/handlers/admin/messages.py:2032` `register_handlers`.

- [ ] **Step 1: Add registrations** (each appears once):

```python
    dp.callback_query.register(start_copy_broadcast, F.data == 'admin_msg_copy')
    dp.callback_query.register(test_broadcast_self, F.data == 'nz!_bcast_test_self')
    dp.callback_query.register(prompt_test_user, F.data == 'nz!_bcast_test_user')
    dp.message.register(process_broadcast_copy_source, AdminStates.waiting_for_broadcast_copy_source)
    dp.message.register(process_test_user_id, AdminStates.waiting_for_broadcast_test_user_id)
```

- [ ] **Step 2: Smoke-import** the module:

Run: `.venv/Scripts/python.exe -c "import app.handlers.admin.messages"`
Expected: no error.

- [ ] **Step 3: Commit**

```bash
git add app/handlers/admin/messages.py
git commit -m "feat(broadcast): register copy-mode and test-send handlers"
```

---

## Task 7: Full verification

- [ ] **Step 1: Run the new tests**

Run: `.venv/Scripts/python.exe -m pytest tests/handlers/test_broadcast_delivery.py tests/handlers/test_broadcast_copy_capture.py -q`
Expected: all PASS.

- [ ] **Step 2: Smoke-import the handler + states**

Run: `.venv/Scripts/python.exe -c "import app.handlers.admin.messages, app.states"`
Expected: no error.

- [ ] **Step 3: Final review** — confirm: copy entry button present; target→copy-source branch; capture stores ids; preview copies back; send loop passes mode+copy ids to `_deliver_broadcast_to`; test buttons on confirm; both test handlers registered; history `message_text='📋 Рассылка копией'` for copy.

- [ ] **Step 4: Commit any fixups; the feature branch is ready to merge.**

---

## Self-review notes

- **Spec coverage:** entry+target (Task 3), capture (Task 3), optional buttons (reused; passed as reply_markup throughout), confirm+send copy branch (Task 4), history placeholder (Task 3 capture), error handling (reused loop + `_send_test_broadcast`), test-send self/user both modes (Task 5), tests (Tasks 2/3/5). ✅
- **Shared helper name** is `_deliver_broadcast_to` everywhere; test helper is `_send_test_broadcast`. ✅
- **No per-user templating / albums / scheduling** — out of scope, not implemented. ✅
- **Risk:** decorator argument order on handlers — match the existing sibling handler signatures exactly (e.g. `process_broadcast_media`, `confirm_broadcast`).
