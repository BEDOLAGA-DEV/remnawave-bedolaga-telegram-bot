# Premium emoji in inline buttons (`icon_custom_emoji_id`) — Design

**Date:** 2026-07-05
**Status:** Approved

## Problem

The bot already replaces regular emoji with premium (custom) emoji in message
texts and captions via `apply_premium_emoji()` (`app/utils/premium_emoji.py`,
mapping in `data/premium_emoji.json`, 128 emoji fully mapped). Inline keyboard
buttons still show regular emoji: button text carries no HTML entities, so the
`<tg-emoji>` mechanism does not apply there.

Bot API 9.x added `InlineKeyboardButton.icon_custom_emoji_id` — a custom emoji
rendered before the button text. The installed aiogram 3.25.0 exposes the
field; the requirements.txt pin (3.22.0) does not, so the pin must be bumped.

There are ~1223 `InlineKeyboardButton(` call sites across 50+ files, plus
DB-driven button texts (admin-configurable menus). Editing call sites is not
viable; the replacement must be a central runtime transform.

## Decision summary

- **Strategy (approved: option A):** strip the leading emoji from the button
  text and set `icon_custom_emoji_id` from the existing mapping. Old clients
  that ignore the field show the button without an emoji — accepted
  degradation.
- **Interception point (approved: session request-middleware):** an aiogram
  `BaseRequestMiddleware` registered on the bot session transforms
  `reply_markup` of every outgoing API call. Covers handlers, services,
  broadcasts, direct `bot.send_message` / `edit_message_reply_markup` calls,
  and dynamically built keyboards with zero call-site edits.
- **No new config flag.** The existing kill-switch applies: an empty/missing
  `data/premium_emoji.json` disables both text and button replacement.

## Components

### 1. Transform function — `app/utils/premium_emoji.py`

```python
apply_premium_emoji_to_markup(
    markup: InlineKeyboardMarkup | None,
) -> InlineKeyboardMarkup | None
```

- Reuses the mapping loaded by `_load_replacements()`. A second cache holds
  the raw `emoji_char -> document_id` map (the existing cache stores rendered
  `<tg-emoji>` strings, unusable for buttons). `reload_premium_emoji()`
  resets both caches.
- Per button: match the leading emoji against the mapping, longest emoji
  first (multi-codepoint emoji must win over their prefixes). On match,
  produce `button.model_copy(update={"text": stripped_text,
  "icon_custom_emoji_id": doc_id})` where `stripped_text` is the text without
  the leading emoji and any following whitespace.
- **No in-place mutation.** Caller-held keyboard objects must stay pristine —
  keyboards can be reused across sends. A new markup object is returned only
  if at least one button changed; otherwise the original object is returned
  (identity preserved).

Skip rules (button left untouched):

- text does not start with a mapped emoji;
- stripping would leave empty text (emoji-only buttons, e.g. `◀️` —
  Telegram requires non-empty button text);
- `icon_custom_emoji_id` is already set (idempotency);
- markup is not `InlineKeyboardMarkup` (reply keyboards pass through).

Emoji in the middle or at the end of the text are left as regular characters —
the icon renders only before the text.

### 2. Session middleware — `app/utils/premium_emoji_middleware.py`

An aiogram `BaseRequestMiddleware`:

```python
async def __call__(self, make_request, bot, method):
    markup = getattr(method, "reply_markup", None)
    if isinstance(markup, InlineKeyboardMarkup):
        new_markup = apply_premium_emoji_to_markup(markup)
        if new_markup is not markup:
            method = method.model_copy(update={"reply_markup": new_markup})
    return await make_request(bot, method)
```

- Covers every API method with a top-level `reply_markup`
  (send_message, edit_message_text, edit_message_reply_markup, send_photo,
  send_animation, send_document, copy_message, …).
- Transform errors are caught, logged, and the original method is sent —
  the middleware must never break an outgoing send.

### 3. Registration — `app/bot_factory.py`

`bot.session.middleware.register(PremiumEmojiRequestMiddleware())` before
returning the bot instance. This covers the main bot and all services that
use the shared instance.

**Known gap (out of scope):** cabinet web routes create ad-hoc
`Bot(token=...)` instances (~10 sites) bypassing the factory. Those sends
already lack premium *text* emoji today; button icons will be consistently
absent there too. A follow-up could route them through `bot_factory`.

### 4. Dependency bump

`requirements.txt`: `aiogram==3.22.0` → `aiogram==3.25.0` (field introduced
after 3.22; without the bump prod Docker builds silently drop the field and
the feature is dead in prod). The local venv already runs 3.25.0.

## Error handling

- Transform never raises out of the middleware: any exception → log warning,
  send the original method unchanged.
- Entitlement risk: `icon_custom_emoji_id` requires the same Fragment-username
  entitlement as custom emoji entities. The bot already sends `<tg-emoji>` in
  production texts, so the entitlement is proven; no special fallback.

## Testing — `tests/test_premium_emoji_markup.py`

- leading mapped emoji → text stripped, `icon_custom_emoji_id` set;
- emoji-only button text → untouched;
- emoji mid-text → untouched;
- multi-codepoint emoji matches longest-first;
- `icon_custom_emoji_id` already set → untouched;
- markup with no changes → same object identity returned;
- middleware: `SendMessage` with inline markup transformed; method with
  reply keyboard passes through unchanged; transform exception → original
  method sent.

## Rollout

No migration, no new settings. Deploy = code + requirements bump. Kill-switch:
empty `data/premium_emoji.json` (mirrors existing text behavior).
