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
   cleanup). The delivery branch for copy mode calls a small extracted helper:

   ```
   async def _copy_message_to(bot, telegram_id, from_chat_id, source_message_id, reply_markup):
       await bot.copy_message(
           chat_id=telegram_id,
           from_chat_id=from_chat_id,
           message_id=source_message_id,
           reply_markup=reply_markup,
       )
   ```

5. **History.** `BroadcastHistory` is recorded as today; `message_text` is set to
   the placeholder `'📋 Рассылка копией'`. No migration — the copy source lives in
   FSM state only, for the duration of the immediate send.

## Error handling

Same classification as the existing loop: `TelegramRetryAfter` → global pause +
retry; `TelegramForbiddenError` / `bot was blocked` / `user is deactivated` /
`chat not found` → blocked; other `TelegramBadRequest` → failed. If the admin
deletes the source message mid-broadcast, `copy_message` raises `TelegramBadRequest`
→ counted as failed; the prompt warns the admin not to delete it.

## Testing

- Unit: `_copy_message_to` calls `bot.copy_message` with the correct
  `chat_id`/`from_chat_id`/`message_id`/`reply_markup` (mock bot).
- Unit: the capture handler stores `copy_from_chat_id` and
  `copy_source_message_id` from the incoming message into FSM state.

## Files touched (anticipated)

- `app/handlers/admin/messages.py` — new menu entry, FSM state, capture handler,
  copy branch in the send loop, `_copy_message_to` helper, handler registration.
- `app/states.py` — new `waiting_for_broadcast_copy_source` state.
- `app/keyboards/*` — new menu button (broadcast menu / media keyboard area).
- `tests/handlers/` — new tests for the helper and capture handler.
