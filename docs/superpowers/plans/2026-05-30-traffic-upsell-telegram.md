# Proactive traffic upsell в Telegram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a subscriber crosses a traffic-usage threshold (80%/95%), also send a Telegram message with direct "buy traffic / upgrade / top-up" buttons — not just the existing web notification — and make the per-user traffic-warning toggle actually gate delivery.

**Architecture:** Extend the existing `MonitoringService._check_traffic_usage_warnings` (web-only today) to additionally push a Telegram message via a new `_send_traffic_upsell_notification`, mirroring the `_send_prerenew_save_notification` pattern. Reuse the existing 7-day dedup (`check_recent_traffic_warning`, whose dedup record is the `UserNotification` row written by `_deliver_web_notification`). Add a `is_traffic_warning_enabled(user)` gate that covers BOTH channels.

**Tech Stack:** Python 3.12, aiogram 3.x, SQLAlchemy async, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-05-30-traffic-upsell-telegram-design.md`

**Run tests with:** `.venv/Scripts/python.exe -m pytest <path> -v`

---

## File Structure

- `app/services/monitoring_service.py` — add pref-gate + Telegram push call inside `_check_traffic_usage_warnings` (~line 636-696), add new `_send_traffic_upsell_notification` method (place right after `_send_prerenew_save_notification`).
- `tests/services/test_monitoring_traffic_upsell.py` — new test file.

---

## Task 1: Telegram traffic-upsell push + pref gate

**Files:**
- Modify: `app/services/monitoring_service.py`
- Test: `tests/services/test_monitoring_traffic_upsell.py`

**Context:** `_check_traffic_usage_warnings` (~line 609) iterates active subs with `traffic_limit_gb > 0`, computes `percent` and `highest_threshold`, dedups via `check_recent_traffic_warning(db, user.id, subscription.id, highest_threshold)` (returns True if a `UserNotification` with category `traffic_warning` + matching `data.subscription_id`/`data.threshold_percent` exists in last 7 days), then calls `_deliver_web_notification(...)` which CREATES that `UserNotification` row (the dedup record) and fans out to inbox/WS/WebPush. Each subscription body is wrapped in its own `try/except`. Helpers `get_texts`, `build_miniapp_or_callback_button`, `_send_message_with_logo`, `_handle_unreachable_user`, `TelegramForbiddenError`, `TelegramBadRequest`, `TelegramNetworkError`, `logger` are imported at module top and used by `_send_prerenew_save_notification`. The buy-traffic callback is `nz!_buy_traffic` (registered in `app/handlers/menu.py:1606` → `handle_add_traffic`). `is_traffic_warning_enabled(user)` lives in `app/utils/notification_prefs.py` (default True).

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_monitoring_traffic_upsell.py`:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.monitoring_service as ms
from app.services.monitoring_service import MonitoringService


def _make_sub(*, sub_id=1, user_id=10, tg_id=555, used=8.5, limit=10):
    user = SimpleNamespace(id=user_id, telegram_id=tg_id, language='ru')
    return SimpleNamespace(
        id=sub_id,
        user_id=user_id,
        user=user,
        status='active',
        traffic_limit_gb=limit,
        traffic_used_gb=used,
    )


@pytest.fixture
def service():
    svc = MonitoringService.__new__(MonitoringService)
    svc.bot = AsyncMock()
    svc._deliver_web_notification = AsyncMock()
    svc._send_traffic_upsell_notification = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    # thresholds 80 & 95
    monkeypatch.setattr(MonitoringService, '_parse_traffic_warning_thresholds', lambda self: [80, 95])
    monkeypatch.setattr('app.database.crud.user_notification.check_recent_traffic_warning', AsyncMock(return_value=False))
    monkeypatch.setattr('app.utils.notification_prefs.is_traffic_warning_enabled', lambda user: True)
    yield


def _db_returning(subs):
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = subs
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_threshold_crossed_sends_web_and_telegram(service):
    db = _db_returning([_make_sub(used=8.5, limit=10)])  # 85% -> crosses 80
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_awaited_once()
    service._send_traffic_upsell_notification.assert_awaited_once()


@pytest.mark.asyncio
async def test_email_only_user_gets_web_but_not_telegram(service):
    db = _db_returning([_make_sub(tg_id=None, used=9.6, limit=10)])  # 96%
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_awaited_once()
    service._send_traffic_upsell_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_pref_disabled_sends_nothing(service, monkeypatch):
    monkeypatch.setattr('app.utils.notification_prefs.is_traffic_warning_enabled', lambda user: False)
    db = _db_returning([_make_sub(used=8.5, limit=10)])
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_not_awaited()
    service._send_traffic_upsell_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedup_hit_sends_nothing(service, monkeypatch):
    monkeypatch.setattr('app.database.crud.user_notification.check_recent_traffic_warning', AsyncMock(return_value=True))
    db = _db_returning([_make_sub(used=8.5, limit=10)])
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_not_awaited()
    service._send_traffic_upsell_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_below_threshold_sends_nothing(service):
    db = _db_returning([_make_sub(used=5.0, limit=10)])  # 50%
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_not_awaited()
    service._send_traffic_upsell_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_failure_does_not_break_web(service):
    service._send_traffic_upsell_notification = AsyncMock(side_effect=RuntimeError('tg down'))
    db = _db_returning([_make_sub(used=8.5, limit=10)])
    # must not raise
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_monitoring_traffic_upsell.py -v`
Expected: FAIL — `_send_traffic_upsell_notification` not called (branch doesn't exist yet), and pref-gate test fails because web is currently sent regardless of pref.

NOTE on `test_telegram_failure_does_not_break_web`: the per-subscription body is already inside a `try/except` that logs and continues, so a raising `_send_traffic_upsell_notification` is swallowed at that level — the test asserts web was delivered (it runs before the TG call) and that no exception escapes. The TG call MUST be placed AFTER `_deliver_web_notification` for this ordering to hold.

- [ ] **Step 3: Add the pref gate + Telegram push in `_check_traffic_usage_warnings`**

In `app/services/monitoring_service.py`, the method already has a local import `from app.database.crud.user_notification import check_recent_traffic_warning` near its top. Add alongside it:

```python
        from app.utils.notification_prefs import is_traffic_warning_enabled
```

Inside the per-subscription block, right AFTER the `already_sent` continue-guard and BEFORE `level = ...` / `_deliver_web_notification`, add the pref gate:

```python
                    if not is_traffic_warning_enabled(user):
                        continue
```

Then, immediately AFTER the existing `await self._deliver_web_notification(...)` call (still inside the per-subscription `try`, and keep the existing `logger.info('🚦 ...')`), add the Telegram push:

```python
                    if user.telegram_id and self.bot:
                        await self._send_traffic_upsell_notification(
                            user, subscription, highest_threshold, used_gb, limit_gb,
                        )
```

- [ ] **Step 4: Add `_send_traffic_upsell_notification` method**

Place immediately after `_send_prerenew_save_notification`, mirroring its structure and error handling exactly:

```python
    async def _send_traffic_upsell_notification(
        self,
        user: User,
        subscription: Subscription,
        threshold: int,
        used_gb: float,
        limit_gb: float,
    ) -> bool:
        try:
            texts = get_texts(user.language)
            emoji = '🚨' if threshold >= 95 else '⚠️'
            template = texts.get(
                'TRAFFIC_UPSELL_PUSH',
                (
                    '{emoji} <b>Трафик заканчивается: {threshold}%</b>\n\n'
                    'Использовано {used:.1f} / {limit} ГБ. '
                    'Докупите пакет или поднимите тариф, чтобы не остаться без доступа.'
                ),
            )
            message = template.format(emoji=emoji, threshold=threshold, used=used_gb, limit=limit_gb)

            from aiogram.types import InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        build_miniapp_or_callback_button(
                            text='➕ Докупить трафик', callback_data='nz!_buy_traffic'
                        )
                    ],
                    [
                        build_miniapp_or_callback_button(
                            text=texts.t('MENU_SUBSCRIPTION', '📱 Моя подписка'),
                            callback_data='nz!_menu_subscription',
                        )
                    ],
                    [
                        build_miniapp_or_callback_button(
                            text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'),
                            callback_data='nz!_balance_topup',
                        )
                    ],
                ]
            )

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if await self._handle_unreachable_user(user, exc, 'предупреждение о трафике'):
                return True
            logger.error(
                'Ошибка Telegram API при отправке upsell трафика',
                telegram_id=user.telegram_id,
                exc=exc,
            )
            return False
        except TelegramNetworkError as e:
            logger.warning('Таймаут отправки upsell трафика', telegram_id=user.telegram_id, e=e)
            return False
        except Exception as e:
            logger.error('Ошибка отправки upsell трафика', telegram_id=user.telegram_id, e=e)
            return False
```

IMPORTANT: Before writing, open `_send_prerenew_save_notification` and confirm exact helper names (`get_texts`, `texts.t`, `build_miniapp_or_callback_button`, `_send_message_with_logo`, `_handle_unreachable_user`, the Telegram exception class names). Mirror exactly. Verify `nz!_menu_subscription` is a real registered callback that opens the subscription screen; if the actual callback differs, use the real one (grep `menu_subscription` or the callback used by the "my subscription" menu button). If unsure, fall back to `nz!_back_to_menu` rather than an invalid callback.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_monitoring_traffic_upsell.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Run wider service suite for regressions**

Run: `.venv/Scripts/python.exe -m pytest tests/services/ -q`
Expected: No NEW failures vs baseline (~29 pre-existing failures unrelated; confirm none in monitoring/traffic-upsell files; note counts).

- [ ] **Step 7: Commit**

```bash
git add app/services/monitoring_service.py tests/services/test_monitoring_traffic_upsell.py
git commit -m "feat(traffic-upsell): Telegram push with buy-traffic CTA + pref gate"
```

---

## Self-Review Checklist (controller runs before review)

- [ ] Pref gate (`is_traffic_warning_enabled`) covers BOTH web + TG (placed before `_deliver_web_notification`).
- [ ] TG push placed AFTER web delivery (so web is the dedup-record writer and TG failure can't block web).
- [ ] `nz!_buy_traffic` is the verified buy-traffic callback; subscription/topup callbacks are real.
- [ ] `_send_traffic_upsell_notification` mirrors `_send_prerenew_save_notification` error handling exactly.
- [ ] No new dedup record introduced by TG path (web's `UserNotification` remains the single dedup key).
- [ ] Tests assert real branching (web+TG, email-only, pref-off, dedup, below-threshold, TG-failure-isolation).

## Out of plan scope (follow-ups)

- Localizing `TRAFFIC_UPSELL_PUSH` across locale files (inline default covers runtime).
- WL-traffic-specific buy button (general `nz!_buy_traffic` flow covers it).
- Splitting the pref-gate bugfix into its own commit (optional; included here).
