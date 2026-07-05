# Premium Emoji in Inline Buttons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace leading regular emoji in all inline keyboard buttons with premium custom emoji (`icon_custom_emoji_id`) via a central aiogram session request-middleware — zero edits to the ~1223 button call sites.

**Architecture:** A transform function in `app/utils/premium_emoji.py` converts `InlineKeyboardMarkup` button-by-button (strip leading mapped emoji, set `icon_custom_emoji_id`, copy-not-mutate). A new `BaseRequestMiddleware` registered on the bot session in `app/bot_factory.py` applies it to `reply_markup` of every outgoing Bot API call. Mapping source is the existing `data/premium_emoji.json`.

**Tech Stack:** Python 3.13 (`.venv\Scripts\python.exe` — bare `python` is 3.10 and can't import `app`), aiogram 3.25.0, pytest + pytest-asyncio (`@pytest.mark.asyncio` decorator convention).

**Spec:** `docs/superpowers/specs/2026-07-05-premium-emoji-inline-buttons-design.md`

**Branch:** `feat/premium-emoji-buttons` (already created; spec committed).

**Verified facts (do not re-derive):**
- `InlineKeyboardButton.icon_custom_emoji_id` exists in installed aiogram 3.25.0; absent in the 3.22.0 pinned in requirements.txt line 2.
- `bot.session.middleware` is a `RequestMiddlewareManager` — has `.register()`, is iterable and sized.
- `BaseRequestMiddleware.__call__` signature: `(self, make_request, bot, method) -> Response`.
- aiogram models are mutable pydantic — that's why the transform must **copy**, never mutate (keyboards are reused across sends).
- `data/premium_emoji.json` format: `{"emojis": {"<emoji_char>": "<document_id>"}}`, 128 entries, all filled.
- `docs/` is gitignored but specs/plans are tracked by convention — use `git add -f` for them.

---

### Task 1: Markup transform in `app/utils/premium_emoji.py`

Refactors the JSON loading into a shared raw-map loader (existing `_load_replacements()` builds `<tg-emoji>` HTML strings from it), adds a leading-emoji regex, and the public `apply_premium_emoji_to_markup()`.

**Files:**
- Modify: `app/utils/premium_emoji.py`
- Create: `tests/test_premium_emoji_markup.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_premium_emoji_markup.py`:

```python
"""Тесты замены ведущих эмодзи inline-кнопок на icon_custom_emoji_id."""

import json

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.utils import premium_emoji
from app.utils.premium_emoji import apply_premium_emoji_to_markup


HEART = '❤️'
HEART_FIRE = '❤️‍\U0001f525'  # ❤️‍🔥 — multi-codepoint, HEART is its prefix


@pytest.fixture()
def emoji_map(monkeypatch, tmp_path):
    mapping = {
        '\U0001f48e': '5000000000000000001',  # 💎
        '◀️': '5000000000000000002',
        HEART: '5000000000000000003',
        HEART_FIRE: '5000000000000000004',
    }
    path = tmp_path / 'premium_emoji.json'
    path.write_text(json.dumps({'emojis': mapping}, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(premium_emoji, '_EMOJI_MAP_PATH', path)
    premium_emoji.reload_premium_emoji()
    yield mapping
    monkeypatch.undo()
    premium_emoji.reload_premium_emoji()


def _kb(*texts: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=f'cb{i}') for i, t in enumerate(texts)],
        ],
    )


def test_leading_emoji_stripped_and_icon_set(emoji_map):
    markup = _kb('\U0001f48e Купить')
    result = apply_premium_emoji_to_markup(markup)
    btn = result.inline_keyboard[0][0]
    assert btn.text == 'Купить'
    assert btn.icon_custom_emoji_id == emoji_map['\U0001f48e']
    assert btn.callback_data == 'cb0'


def test_original_markup_not_mutated(emoji_map):
    markup = _kb('\U0001f48e Купить')
    apply_premium_emoji_to_markup(markup)
    original_btn = markup.inline_keyboard[0][0]
    assert original_btn.text == '\U0001f48e Купить'
    assert original_btn.icon_custom_emoji_id is None


def test_emoji_only_button_untouched(emoji_map):
    markup = _kb('◀️')
    assert apply_premium_emoji_to_markup(markup) is markup


def test_mid_text_emoji_untouched(emoji_map):
    markup = _kb('Купить \U0001f48e')
    assert apply_premium_emoji_to_markup(markup) is markup


def test_multicodepoint_longest_match(emoji_map):
    markup = _kb(f'{HEART_FIRE} Огонь')
    result = apply_premium_emoji_to_markup(markup)
    btn = result.inline_keyboard[0][0]
    assert btn.text == 'Огонь'
    assert btn.icon_custom_emoji_id == emoji_map[HEART_FIRE]


def test_already_set_icon_untouched(emoji_map):
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='\U0001f48e Купить',
                    callback_data='cb',
                    icon_custom_emoji_id='123',
                ),
            ],
        ],
    )
    assert apply_premium_emoji_to_markup(markup) is markup


def test_unmapped_emoji_untouched(emoji_map):
    markup = _kb('\U0001f680 Старт')  # 🚀 отсутствует в маппинге
    assert apply_premium_emoji_to_markup(markup) is markup


def test_none_markup(emoji_map):
    assert apply_premium_emoji_to_markup(None) is None


def test_mixed_row_only_changed_buttons_copied(emoji_map):
    markup = _kb('\U0001f48e Купить', 'Просто текст')
    result = apply_premium_emoji_to_markup(markup)
    assert result is not markup
    assert result.inline_keyboard[0][0].icon_custom_emoji_id == emoji_map['\U0001f48e']
    # Неизменённая кнопка переиспользуется как есть
    assert result.inline_keyboard[0][1] is markup.inline_keyboard[0][1]


def test_empty_mapping_disables(monkeypatch, tmp_path):
    path = tmp_path / 'premium_emoji.json'
    path.write_text(json.dumps({'emojis': {}}), encoding='utf-8')
    monkeypatch.setattr(premium_emoji, '_EMOJI_MAP_PATH', path)
    premium_emoji.reload_premium_emoji()
    try:
        markup = _kb('\U0001f48e Купить')
        assert apply_premium_emoji_to_markup(markup) is markup
    finally:
        monkeypatch.undo()
        premium_emoji.reload_premium_emoji()


def test_text_replacement_still_works_after_refactor(emoji_map):
    out = premium_emoji.apply_premium_emoji('привет \U0001f48e')
    diamond_id = emoji_map['\U0001f48e']
    expected = f'<tg-emoji emoji-id="{diamond_id}">\U0001f48e</tg-emoji>'
    assert expected in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_premium_emoji_markup.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_premium_emoji_to_markup'`

- [ ] **Step 3: Implement in `app/utils/premium_emoji.py`**

3a. Replace the imports block at the top (add the aiogram types import, drop the empty `TYPE_CHECKING` block — `from typing import TYPE_CHECKING` and `if TYPE_CHECKING: pass` go away):

```python
from __future__ import annotations

import json
import re
from pathlib import Path

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
```

3b. Replace the whole `_load_replacements()` function (keep the `_replacement_cache` declaration above it) with a shared raw loader plus a derived builder — behavior for texts stays identical:

```python
# Кеш сырого маппинга: emoji_char -> document_id
_raw_map_cache: dict[str, str] | None = None


def _load_raw_map() -> dict[str, str]:
    """Загружает сырой маппинг emoji -> document_id из JSON."""
    global _raw_map_cache
    if _raw_map_cache is not None:
        return _raw_map_cache

    try:
        with _EMOJI_MAP_PATH.open(encoding='utf-8') as f:
            data = json.load(f)
        raw = data.get('emojis', {})
    except FileNotFoundError:
        _logger.warning('premium_emoji.json not found, premium emoji replacement is disabled')
        _raw_map_cache = {}
        return _raw_map_cache
    except Exception as exc:
        _logger.warning('Failed to load premium_emoji.json', error=exc)
        _raw_map_cache = {}
        return _raw_map_cache

    _raw_map_cache = {
        emoji_char: doc_id.strip()
        for emoji_char, doc_id in raw.items()
        if doc_id and isinstance(doc_id, str) and doc_id.strip()
    }
    return _raw_map_cache


def _load_replacements() -> dict[str, str]:
    """Строит готовые <tg-emoji> замены из сырого маппинга."""
    global _replacement_cache
    if _replacement_cache is not None:
        return _replacement_cache

    raw = _load_raw_map()
    _replacement_cache = {
        emoji_char: f'<tg-emoji emoji-id="{doc_id}">{emoji_char}</tg-emoji>'
        for emoji_char, doc_id in raw.items()
    }
    if _replacement_cache:
        _logger.debug('Loaded premium emoji replacements', count=len(_replacement_cache))
    return _replacement_cache
```

3c. After the existing `apply_premium_emoji()` function, add the leading-pattern cache and the markup transform:

```python
_leading_pattern_cache: re.Pattern[str] | None = None
_leading_pattern_map_id: int = -1


def _get_leading_pattern() -> re.Pattern[str] | None:
    """Regex, матчащий настроенный эмодзи в начале строки (длинные раньше)."""
    global _leading_pattern_cache, _leading_pattern_map_id
    raw = _load_raw_map()
    if not raw:
        return None
    if _leading_pattern_cache is None or id(raw) != _leading_pattern_map_id:
        sorted_emojis = sorted(raw.keys(), key=len, reverse=True)
        _leading_pattern_cache = re.compile(
            '^(?:' + '|'.join(re.escape(e) for e in sorted_emojis) + ')',
        )
        _leading_pattern_map_id = id(raw)
    return _leading_pattern_cache


def _convert_button(
    button: InlineKeyboardButton,
    raw_map: dict[str, str],
    pattern: re.Pattern[str],
) -> InlineKeyboardButton:
    """Кнопка с icon_custom_emoji_id, либо исходная если замена не нужна."""
    if button.icon_custom_emoji_id:
        return button
    text = button.text or ''
    match = pattern.match(text)
    if not match:
        return button
    emoji = match.group(0)
    stripped = text[len(emoji):].lstrip()
    if not stripped:
        # Кнопка из одного эмодзи: Telegram требует непустой text
        return button
    return button.model_copy(
        update={'text': stripped, 'icon_custom_emoji_id': raw_map[emoji]},
    )


def apply_premium_emoji_to_markup(
    markup: InlineKeyboardMarkup | None,
) -> InlineKeyboardMarkup | None:
    """Заменяет ведущие эмодзи inline-кнопок на icon_custom_emoji_id.

    Возвращает новый InlineKeyboardMarkup, если хоть одна кнопка изменилась,
    иначе исходный объект (identity сохраняется). Исходная разметка и её
    кнопки не мутируются — клавиатуры могут переиспользоваться между
    отправками.
    """
    if markup is None or not isinstance(markup, InlineKeyboardMarkup):
        return markup

    raw_map = _load_raw_map()
    if not raw_map:
        return markup
    pattern = _get_leading_pattern()
    if pattern is None:
        return markup

    changed = False
    new_rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for button in row:
            new_button = _convert_button(button, raw_map, pattern)
            if new_button is not button:
                changed = True
            new_row.append(new_button)
        new_rows.append(new_row)

    if not changed:
        return markup
    return InlineKeyboardMarkup(inline_keyboard=new_rows)
```

3d. Replace `reload_premium_emoji()` to reset the new caches too:

```python
def reload_premium_emoji() -> None:
    """Сбрасывает кеши маппинга (для горячей перезагрузки без рестарта)."""
    global _replacement_cache, _pattern_cache, _pattern_map_id
    global _raw_map_cache, _leading_pattern_cache, _leading_pattern_map_id
    _replacement_cache = None
    _pattern_cache = None
    _pattern_map_id = -1
    _raw_map_cache = None
    _leading_pattern_cache = None
    _leading_pattern_map_id = -1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_premium_emoji_markup.py -v`
Expected: all 11 tests PASS

- [ ] **Step 5: Regression — importers of premium_emoji still work**

Run: `.venv\Scripts\python.exe -m pytest tests/test_animated_logo.py -v`
Expected: PASS (this suite imports `message_patch` → `premium_emoji`)

- [ ] **Step 6: Commit**

```bash
git add app/utils/premium_emoji.py tests/test_premium_emoji_markup.py
git commit -m "feat(premium-emoji): markup transform — leading button emoji to icon_custom_emoji_id"
```

---

### Task 2: Session request-middleware

**Files:**
- Create: `app/utils/premium_emoji_middleware.py`
- Modify: `tests/test_premium_emoji_markup.py` (append middleware tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_premium_emoji_markup.py` (imports go to the top of the file with the existing ones):

```python
from aiogram.methods import SendMessage
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.utils.premium_emoji_middleware import PremiumEmojiRequestMiddleware


def _capture():
    captured = {}

    async def fake_make_request(bot, method):
        captured['method'] = method
        return 'ok'

    return captured, fake_make_request


@pytest.mark.asyncio
async def test_middleware_transforms_send_message(emoji_map):
    mw = PremiumEmojiRequestMiddleware()
    captured, fake_make_request = _capture()
    method = SendMessage(chat_id=1, text='hi', reply_markup=_kb('\U0001f48e Купить'))

    result = await mw(fake_make_request, None, method)

    assert result == 'ok'
    btn = captured['method'].reply_markup.inline_keyboard[0][0]
    assert btn.text == 'Купить'
    assert btn.icon_custom_emoji_id == emoji_map['\U0001f48e']


@pytest.mark.asyncio
async def test_middleware_passthrough_reply_keyboard(emoji_map):
    mw = PremiumEmojiRequestMiddleware()
    captured, fake_make_request = _capture()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='\U0001f48e Купить')]])
    method = SendMessage(chat_id=1, text='hi', reply_markup=kb)

    await mw(fake_make_request, None, method)

    assert captured['method'] is method


@pytest.mark.asyncio
async def test_middleware_passthrough_no_markup(emoji_map):
    mw = PremiumEmojiRequestMiddleware()
    captured, fake_make_request = _capture()
    method = SendMessage(chat_id=1, text='hi')

    await mw(fake_make_request, None, method)

    assert captured['method'] is method


@pytest.mark.asyncio
async def test_middleware_error_sends_original(emoji_map, monkeypatch):
    import app.utils.premium_emoji_middleware as mw_module

    mw = PremiumEmojiRequestMiddleware()
    captured, fake_make_request = _capture()

    def boom(markup):
        raise RuntimeError('boom')

    monkeypatch.setattr(mw_module, 'apply_premium_emoji_to_markup', boom)
    method = SendMessage(chat_id=1, text='hi', reply_markup=_kb('\U0001f48e Купить'))

    result = await mw(fake_make_request, None, method)

    assert result == 'ok'
    assert captured['method'] is method
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_premium_emoji_markup.py -v`
Expected: collection FAIL — `ModuleNotFoundError: No module named 'app.utils.premium_emoji_middleware'`

- [ ] **Step 3: Create `app/utils/premium_emoji_middleware.py`**

```python
"""Session-middleware: премиум-эмодзи в inline-кнопках исходящих запросов.

Перехватывает каждый исходящий вызов Bot API. Если у метода есть
reply_markup с InlineKeyboardMarkup — ведущие эмодзи кнопок заменяются на
icon_custom_emoji_id (маппинг data/premium_emoji.json). Регистрируется на
bot.session в app/bot_factory.py, поэтому покрывает все пути отправки:
хендлеры, сервисы, рассылки, прямые bot.send_message / edit_reply_markup.
"""

from __future__ import annotations

import structlog
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import InlineKeyboardMarkup

from app.utils.premium_emoji import apply_premium_emoji_to_markup

logger = structlog.get_logger(__name__)


class PremiumEmojiRequestMiddleware(BaseRequestMiddleware):
    """Подменяет reply_markup исходящих методов на версию с премиум-иконками."""

    async def __call__(self, make_request, bot, method):
        try:
            markup = getattr(method, 'reply_markup', None)
            if isinstance(markup, InlineKeyboardMarkup):
                new_markup = apply_premium_emoji_to_markup(markup)
                if new_markup is not markup:
                    method = method.model_copy(update={'reply_markup': new_markup})
        except Exception as exc:
            # Косметика никогда не должна ломать исходящий запрос
            logger.warning(
                'Premium emoji markup transform failed, sending original',
                error=str(exc),
            )
        return await make_request(bot, method)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_premium_emoji_markup.py -v`
Expected: all 15 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/utils/premium_emoji_middleware.py tests/test_premium_emoji_markup.py
git commit -m "feat(premium-emoji): session request-middleware for outgoing reply_markup"
```

---

### Task 3: Register middleware in bot factory

**Files:**
- Modify: `app/bot_factory.py` (28 lines total — add import + registration before return)
- Modify: `tests/test_premium_emoji_markup.py` (append registration test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_premium_emoji_markup.py`:

```python
def test_create_bot_registers_premium_emoji_middleware(monkeypatch):
    from app import bot_factory

    monkeypatch.setattr(bot_factory.settings, 'get_proxy_url', lambda: None)
    monkeypatch.setattr(bot_factory.settings, 'get_telegram_api_url', lambda: None)

    bot = bot_factory.create_bot(token='42:TEST')

    assert any(
        isinstance(m, PremiumEmojiRequestMiddleware)
        for m in bot.session.middleware
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_premium_emoji_markup.py::test_create_bot_registers_premium_emoji_middleware -v`
Expected: FAIL — `assert False` (no middleware registered)

- [ ] **Step 3: Modify `app/bot_factory.py`**

Add the import after the existing `from app.config import settings`:

```python
from app.utils.premium_emoji_middleware import PremiumEmojiRequestMiddleware
```

Replace the last line `return Bot(token=token or settings.BOT_TOKEN, session=session, **kwargs)` with:

```python
    bot = Bot(token=token or settings.BOT_TOKEN, session=session, **kwargs)
    bot.session.middleware.register(PremiumEmojiRequestMiddleware())
    return bot
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_premium_emoji_markup.py -v`
Expected: all 16 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/bot_factory.py tests/test_premium_emoji_markup.py
git commit -m "feat(premium-emoji): register markup middleware in bot factory"
```

---

### Task 4: Bump aiogram pin + final verification

Without the bump, prod Docker installs aiogram 3.22.0 where `InlineKeyboardButton` has no `icon_custom_emoji_id` — the field would be silently dropped and the feature dead in prod. Local venv already runs 3.25.0.

**Files:**
- Modify: `requirements.txt:2` (`aiogram==3.22.0` → `aiogram==3.25.0`)

- [ ] **Step 1: Edit `requirements.txt` line 2**

```
aiogram==3.25.0
```

- [ ] **Step 2: Verify installed version matches the new pin**

Run: `.venv\Scripts\python.exe -c "import aiogram; print(aiogram.__version__)"`
Expected: `3.25.0`

- [ ] **Step 3: Run the feature suite plus import-heavy neighbors**

Run: `.venv\Scripts\python.exe -m pytest tests/test_premium_emoji_markup.py tests/test_animated_logo.py tests/keyboards -v`
Expected: PASS. (Full `pytest tests/` has pre-existing collection errors unrelated to this change — do not chase them.)

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "build: bump aiogram to 3.25.0 for InlineKeyboardButton.icon_custom_emoji_id"
```

- [ ] **Step 5: Commit the plan document**

```bash
git add -f docs/superpowers/plans/2026-07-05-premium-emoji-inline-buttons.md
git commit -m "docs: implementation plan for premium emoji inline buttons"
```
