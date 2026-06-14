# Copy-message broadcast mode (bot admin)

**Date:** 2026-06-14
**Status:** Approved (design)

## Problem

The bot's admin broadcast (`app/handlers/admin/messages.py`) makes the admin type
the message body as text and apply formatting via raw HTML (`parse_mode='HTML'`).
That is error-prone and cannot express everything Telegram supports (custom
emoji, mixed entities, native media layout).

## Goal

Let the admin compose a message **in Telegram itself** — with whatever
formatting, entities, and media they want — send it to the bot, and have the bot
broadcast an exact 1:1 copy to the selected audience using `bot.copy_message`.

Before committing to the full broadcast, the admin can **test-send** the exact
message (same delivery path and inline keyboard) to themselves or to a specific
user, to verify how it looks. Test-send works for both the new copy mode and the
existing HTML mode.

## Non-goals

- Per-recipient templating (`{user_name}` etc.). A copy is verbatim by design.
- Albums / media groups (a `copy_message` copies a single message).
- Scheduling. The broadcast runs immediately, like the existing bot flow.
- Touching the existing HTML broadcast flow — it stays as a separate option.

## Approach

Add a **new broadcast mode** alongside the existing one (admin's choice). The new
mode reuses all existing broadcast infrastructure and only swaps the delivery
primitive from `send_message`/`send_photo` to `copy_message`.

## Components & flow

1. **Entry & target.** New menu button "📋 Рассылка копией". It runs the existing
   target-selection UI. FSM state carries `broadcast_mode='copy'`. The
   target-selection handlers branch on this flag to pick the next state.

2. **Capture source.** New FSM state `waiting_for_broadcast_copy_source`. Prompt:
   "Отправьте сообщение для рассылки (любое форматирование/медиа) — бот скопирует
   1:1. Не удаляйте его до конца рассылки." The handler stores
   `copy_from_chat_id = message.chat.id` and `copy_source_message_id =
   message.message_id`. It rejects service/empty messages. Preview = the bot
   copies the message back to the admin, plus a confirm keyboard.

3. **Buttons (optional).** Reuse the existing button selector. The selected inline
   keyboard is passed as `reply_markup` to `copy_message`.

4. **Confirm & send.** Reuse the bot's batched send loop (batch 25, 1s delay,
   global FloodWait pause, 3 retries, in-chat progress edits, blocked-user
   cleanup). Per-recipient delivery goes through one shared helper
   `_deliver_broadcast_to(...)` (see Test send) that branches on `mode`: copy mode
   calls `bot.copy_message(chat_id, from_chat_id, message_id, reply_markup)`; HTML
   mode keeps the current `send_message`/`send_<media>` with `parse_mode='HTML'`.
   The same helper is used by test-send, so a test is a faithful preview.

5. **History.** `BroadcastHistory` is recorded as today; `message_text` is set to
   the placeholder `'📋 Рассылка копией'`. No migration — the copy source lives in
   FSM state only, for the duration of the immediate send.

## Test send (both modes)

A faithful preview before the full broadcast, available on the confirm screen for
**both** the copy mode and the existing HTML mode.

- **Confirm-screen buttons.** "🧪 Тест себе" and "🧪 Тест пользователю", shown next
  to the existing send/cancel buttons.
- **Test to self.** Sends one message to the admin's own `telegram_id`.
- **Test to a user.** New FSM state `waiting_for_broadcast_test_user_id`. The admin
  enters a numeric `telegram_id` (only — no @username resolution). Non-numeric or
  unknown input is rejected with an inline error and the admin can retry.
- **Faithful delivery.** The test uses the **same single-message delivery path** as
  the real broadcast and the same inline keyboard, so it is an exact preview. To
  guarantee this, the per-recipient delivery is extracted into one shared helper
  used by both the test-send and the batch loop, for both modes:

  ```
  async def _deliver_broadcast_to(bot, telegram_id, *, mode, message_text,
                                  media_type, media_file_id,
                                  copy_from_chat_id, copy_source_message_id,
                                  reply_markup):
      # mode == 'copy' -> bot.copy_message(...); mode == 'html' -> send_message/send_<media>(...)
  ```

- **No history.** A test send does not create a `BroadcastHistory` record.
- **Result.** After a test the admin sees "✅ Тест отправлен" (or a failure reason)
  and stays on the confirm screen, free to send for real, re-test, or cancel.
- **Errors.** `TelegramForbiddenError` / `chat not found` → "Пользователь не найден
  или не запускал бота"; other errors → generic failure text. The full broadcast is
  not started by a test.

## Error handling

Same classification as the existing loop: `TelegramRetryAfter` → global pause +
retry; `TelegramForbiddenError` / `bot was blocked` / `user is deactivated` /
`chat not found` → blocked; other `TelegramBadRequest` → failed. If the admin
deletes the source message mid-broadcast, `copy_message` raises `TelegramBadRequest`
→ counted as failed; the prompt warns the admin not to delete it.

## Testing

- Unit: `_deliver_broadcast_to` in `mode='copy'` calls `bot.copy_message` with the
  correct `chat_id`/`from_chat_id`/`message_id`/`reply_markup` (mock bot).
- Unit: `_deliver_broadcast_to` in `mode='html'` calls `send_message` (and the
  media variant) with `parse_mode='HTML'` and the keyboard.
- Unit: the capture handler stores `copy_from_chat_id` and
  `copy_source_message_id` from the incoming message into FSM state.
- Unit: the test-send-to-user handler rejects non-numeric input and resolves a
  numeric `telegram_id`, then delivers via `_deliver_broadcast_to`.

## Files touched (anticipated)

- `app/handlers/admin/messages.py` — new menu entry, capture handler, copy branch
  via the shared `_deliver_broadcast_to` helper, confirm-screen test buttons,
  test-send handlers, handler registration.
- `app/states.py` — new `waiting_for_broadcast_copy_source` and
  `waiting_for_broadcast_test_user_id` states.
- `app/keyboards/*` — new menu button (broadcast menu) and confirm-screen test
  buttons.
- `tests/handlers/` — new tests for `_deliver_broadcast_to` (both modes), the
  capture handler, and the test-send-to-user handler.
