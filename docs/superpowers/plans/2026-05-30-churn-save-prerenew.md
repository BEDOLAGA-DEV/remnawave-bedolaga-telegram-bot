# Churn-save (pre-expiry discount) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send a one-time pre-expiry "save" discount offer to active subscribers who will NOT auto-renew, before their subscription expires.

**Architecture:** New monitoring check `_check_prerenew_save_offers` mirrors the existing post-expiry win-back (`_check_expired_subscription_followups`) but fires before expiry for the at-risk segment (`autopay_enabled == False`). Reuses `DiscountOffer` + claim flow + `notification_sent`/`record_notification` dedup. Gated by a new runtime setting (default OFF).

**Tech Stack:** Python 3.12, aiogram, SQLAlchemy async, pytest + pytest-asyncio. Settings stored in `NotificationSettingsService` (JSON on disk).

**Spec:** `docs/superpowers/specs/2026-05-30-churn-save-prerenew-design.md`

**Run tests with:** `.venv/Scripts/python.exe -m pytest <path> -v`

---

## File Structure

- `app/services/notification_settings_service.py` — add `prerenew_save` defaults + getters/setters (Task 1).
- `app/services/monitoring_service.py` — add `_check_prerenew_save_offers` + `_send_prerenew_save_notification`, register in monitoring loop (Task 2).
- `tests/services/test_notification_settings_prerenew.py` — Task 1 tests.
- `tests/services/test_monitoring_prerenew_save.py` — Task 2 tests.
- `app/handlers/admin/monitoring.py` — admin toggle + numeric edits (Task 3). This file already holds the `expired_second_wave` / `expired_third_wave` admin controls.

---

## Task 1: prerenew_save settings in NotificationSettingsService

**Files:**
- Modify: `app/services/notification_settings_service.py` (add to `_DEFAULTS` ~line 21-35; add methods after `set_second_wave_valid_hours` ~line 179)
- Test: `tests/services/test_notification_settings_prerenew.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_notification_settings_prerenew.py`:

```python
import pytest

from app.services.notification_settings_service import NotificationSettingsService as NSS


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    # Point the on-disk store at a temp file and reset the class cache.
    monkeypatch.setattr(NSS, '_storage_path', tmp_path / 'notification_settings.json')
    monkeypatch.setattr(NSS, '_data', {})
    monkeypatch.setattr(NSS, '_loaded', False)
    yield


def test_prerenew_save_defaults_are_off_with_expected_numbers():
    assert NSS.is_prerenew_save_enabled() is False
    assert NSS.get_prerenew_save_discount_percent() == 15
    assert NSS.get_prerenew_save_valid_hours() == 24
    assert NSS.get_prerenew_save_trigger_hours() == 36


def test_prerenew_save_setters_roundtrip():
    assert NSS.set_prerenew_save_enabled(True) is True
    assert NSS.is_prerenew_save_enabled() is True

    assert NSS.set_prerenew_save_discount_percent(25) is True
    assert NSS.get_prerenew_save_discount_percent() == 25

    assert NSS.set_prerenew_save_valid_hours(48) is True
    assert NSS.get_prerenew_save_valid_hours() == 48

    assert NSS.set_prerenew_save_trigger_hours(12) is True
    assert NSS.get_prerenew_save_trigger_hours() == 12


def test_prerenew_save_values_are_clamped_and_validated():
    NSS.set_prerenew_save_discount_percent(999)
    assert NSS.get_prerenew_save_discount_percent() == 100

    assert NSS.set_prerenew_save_discount_percent('abc') is False

    NSS.set_prerenew_save_valid_hours(0)
    assert NSS.get_prerenew_save_valid_hours() == 1

    NSS.set_prerenew_save_trigger_hours(99999)
    assert NSS.get_prerenew_save_trigger_hours() == 168
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_notification_settings_prerenew.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'is_prerenew_save_enabled'`

- [ ] **Step 3: Add `prerenew_save` to `_DEFAULTS`**

In `app/services/notification_settings_service.py`, inside `_DEFAULTS` (after the `expired_third_wave` block, before the closing `}`):

```python
        'prerenew_save': {
            'enabled': False,
            'discount_percent': 15,
            'valid_hours': 24,
            'trigger_hours': 36,
        },
```

- [ ] **Step 4: Add getters/setters**

In the same file, after `set_second_wave_valid_hours` (~line 179), add:

```python
    @classmethod
    def is_prerenew_save_enabled(cls) -> bool:
        return cls.is_enabled('prerenew_save')

    @classmethod
    def set_prerenew_save_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('prerenew_save', enabled)

    @classmethod
    def get_prerenew_save_discount_percent(cls) -> int:
        value = cls._get('prerenew_save').get('discount_percent', 15)
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 15

    @classmethod
    def set_prerenew_save_discount_percent(cls, percent: int) -> bool:
        try:
            percent_int = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('prerenew_save', 'discount_percent', percent_int)

    @classmethod
    def get_prerenew_save_valid_hours(cls) -> int:
        value = cls._get('prerenew_save').get('valid_hours', 24)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 24

    @classmethod
    def set_prerenew_save_valid_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('prerenew_save', 'valid_hours', hours_int)

    @classmethod
    def get_prerenew_save_trigger_hours(cls) -> int:
        value = cls._get('prerenew_save').get('trigger_hours', 36)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 36

    @classmethod
    def set_prerenew_save_trigger_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('prerenew_save', 'trigger_hours', hours_int)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_notification_settings_prerenew.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add app/services/notification_settings_service.py tests/services/test_notification_settings_prerenew.py
git commit -m "feat(churn-save): prerenew_save notification settings"
```

---

## Task 2: `_check_prerenew_save_offers` + sender + loop registration

**Files:**
- Modify: `app/services/monitoring_service.py`
  - register check in the monitoring loop after `await self._check_expiring_subscriptions(db)` (~line 245)
  - add `_check_prerenew_save_offers` and `_send_prerenew_save_notification` methods (place near `_check_expired_subscription_followups`, ~line 1340)
- Test: `tests/services/test_monitoring_prerenew_save.py`

**Context:** All helpers already exist and are imported at module top of `monitoring_service.py`: `notification_sent`, `record_notification`, `upsert_discount_offer`, `get_texts`, `format_local_datetime`, `build_miniapp_or_callback_button`, `select`, `Subscription`, `SubscriptionStatus`, `settings`, `User`, `TelegramForbiddenError`, `TelegramBadRequest`, `TelegramNetworkError`. `_get_expiring_paid_subscriptions(db, days_before)` returns ACTIVE, non-trial, non-daily subs with `.user` and `.tariff` eager-loaded. `is_subscription_expiry_enabled` is imported inside methods from `app.utils.notification_prefs`.

Confirmed signatures:
- `notification_sent(db, user_id, subscription_id, notification_type, days_before=None) -> bool`
- `record_notification(db, user_id, subscription_id, notification_type, days_before=None, *, commit=True) -> None`
- `upsert_discount_offer(db, *, user_id, subscription_id, notification_type, discount_percent, bonus_amount_kopeks, valid_hours, effect_type='percent_discount', extra_data=None) -> DiscountOffer`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_monitoring_prerenew_save.py`:

```python
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.monitoring_service as ms
from app.services.monitoring_service import MonitoringService


def _make_sub(*, sub_id=1, user_id=10, autopay=False, hours_left=10, tg_id=555):
    user = SimpleNamespace(id=user_id, telegram_id=tg_id, language='ru')
    return SimpleNamespace(
        id=sub_id,
        user_id=user_id,
        user=user,
        autopay_enabled=autopay,
        status='active',
        end_date=datetime.now(UTC) + timedelta(hours=hours_left),
        tariff=None,
    )


@pytest.fixture
def service():
    svc = MonitoringService.__new__(MonitoringService)
    svc.bot = AsyncMock()
    svc._log_monitoring_event = AsyncMock()
    svc._send_prerenew_save_notification = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(ms.NotificationSettingsService, 'are_notifications_globally_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(ms.NotificationSettingsService, 'is_prerenew_save_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(ms.NotificationSettingsService, 'get_prerenew_save_trigger_hours', classmethod(lambda cls: 36))
    monkeypatch.setattr(ms.NotificationSettingsService, 'get_prerenew_save_discount_percent', classmethod(lambda cls: 15))
    monkeypatch.setattr(ms.NotificationSettingsService, 'get_prerenew_save_valid_hours', classmethod(lambda cls: 24))
    monkeypatch.setattr(ms.settings, 'is_multi_tariff_enabled', lambda: False)
    monkeypatch.setattr('app.utils.notification_prefs.is_subscription_expiry_enabled', lambda user: True)
    yield


@pytest.fixture
def offer():
    return SimpleNamespace(id=99, expires_at=datetime.now(UTC) + timedelta(hours=24))


@pytest.mark.asyncio
async def test_at_risk_in_window_creates_offer_and_records(service, offer, monkeypatch):
    sub = _make_sub(autopay=False, hours_left=10)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    upsert = AsyncMock(return_value=offer)
    record = AsyncMock()
    monkeypatch.setattr(ms, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(ms, 'record_notification', record)

    await service._check_prerenew_save_offers(MagicMock())

    upsert.assert_awaited_once()
    assert upsert.await_args.kwargs['notification_type'] == 'prerenew_save'
    assert upsert.await_args.kwargs['discount_percent'] == 15
    service._send_prerenew_save_notification.assert_awaited_once()
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_autopay_enabled_is_skipped(service, monkeypatch):
    sub = _make_sub(autopay=True, hours_left=10)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    upsert = AsyncMock()
    monkeypatch.setattr(ms, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())

    await service._check_prerenew_save_offers(MagicMock())

    upsert.assert_not_awaited()
    service._send_prerenew_save_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_outside_window_is_skipped(service, monkeypatch):
    # trigger_hours=36, sub expires in 50h -> outside window
    sub = _make_sub(autopay=False, hours_left=50)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    upsert = AsyncMock()
    monkeypatch.setattr(ms, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())

    await service._check_prerenew_save_offers(MagicMock())

    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_sent_is_skipped(service, monkeypatch):
    sub = _make_sub(autopay=False, hours_left=10)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=True))
    upsert = AsyncMock()
    monkeypatch.setattr(ms, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())

    await service._check_prerenew_save_offers(MagicMock())

    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_flag_early_returns(service, monkeypatch):
    monkeypatch.setattr(ms.NotificationSettingsService, 'is_prerenew_save_enabled', classmethod(lambda cls: False))
    get_subs = AsyncMock(return_value=[])
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', get_subs)

    await service._check_prerenew_save_offers(MagicMock())

    get_subs.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_failure_does_not_record(service, offer, monkeypatch):
    sub = _make_sub(autopay=False, hours_left=10)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(ms, 'upsert_discount_offer', AsyncMock(return_value=offer))
    record = AsyncMock()
    monkeypatch.setattr(ms, 'record_notification', record)
    service._send_prerenew_save_notification = AsyncMock(return_value=False)

    await service._check_prerenew_save_offers(MagicMock())

    record.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_monitoring_prerenew_save.py -v`
Expected: FAIL with `AttributeError: 'MonitoringService' object has no attribute '_check_prerenew_save_offers'`

- [ ] **Step 3: Add `_check_prerenew_save_offers` method**

In `app/services/monitoring_service.py`, add this method immediately after `_check_expired_subscription_followups` (before `_get_expiring_paid_subscriptions`, ~line 1341):

```python
    async def _check_prerenew_save_offers(self, db: AsyncSession):
        if not NotificationSettingsService.are_notifications_globally_enabled():
            return
        if not NotificationSettingsService.is_prerenew_save_enabled():
            return
        if not self.bot:
            return

        try:
            now = datetime.now(UTC)
            trigger_hours = NotificationSettingsService.get_prerenew_save_trigger_hours()
            trigger_days = max(1, (trigger_hours + 23) // 24)

            subscriptions = await self._get_expiring_paid_subscriptions(db, trigger_days)

            from app.utils.notification_prefs import is_subscription_expiry_enabled

            sent = 0
            for subscription in subscriptions:
                user = subscription.user
                if not user or subscription.end_date is None:
                    continue

                hours_left = (subscription.end_date - now).total_seconds() / 3600
                if not (0 < hours_left <= trigger_hours):
                    continue

                # at-risk: автопродление выключено -> сам не продлится.
                # autopay_enabled продлевается с баланса ИЛИ карты — пропускаем.
                if subscription.autopay_enabled:
                    continue

                if not is_subscription_expiry_enabled(user):
                    continue

                # multi-tariff: пропустить, если есть другая активная подписка
                if settings.is_multi_tariff_enabled():
                    other_active = await db.execute(
                        select(Subscription.id)
                        .where(
                            Subscription.user_id == user.id,
                            Subscription.id != subscription.id,
                            Subscription.status == SubscriptionStatus.ACTIVE.value,
                            Subscription.end_date > now,
                        )
                        .limit(1)
                    )
                    if other_active.scalar_one_or_none() is not None:
                        continue

                if await notification_sent(db, user.id, subscription.id, 'prerenew_save'):
                    continue

                percent = NotificationSettingsService.get_prerenew_save_discount_percent()
                valid_hours = NotificationSettingsService.get_prerenew_save_valid_hours()
                offer = await upsert_discount_offer(
                    db,
                    user_id=user.id,
                    subscription_id=subscription.id,
                    notification_type='prerenew_save',
                    discount_percent=percent,
                    bonus_amount_kopeks=0,
                    valid_hours=valid_hours,
                    effect_type='percent_discount',
                )
                success = await self._send_prerenew_save_notification(
                    user, subscription, percent, offer.expires_at, offer.id, int(hours_left),
                )
                if success:
                    await record_notification(db, user.id, subscription.id, 'prerenew_save')
                    sent += 1

            if sent:
                await self._log_monitoring_event(
                    db,
                    'prerenew_save_sent',
                    f'Churn-save офферы отправлены: {sent}',
                    {'sent': sent},
                )

        except Exception as e:
            logger.error('Ошибка проверки churn-save офферов', error=e)
```

- [ ] **Step 4: Add `_send_prerenew_save_notification` method**

Immediately after the method from Step 3, add (mirrors `_send_expired_discount_notification`):

```python
    async def _send_prerenew_save_notification(
        self,
        user: User,
        subscription: Subscription,
        percent: int,
        expires_at: datetime,
        offer_id: int,
        hours_left: int,
    ) -> bool:
        try:
            texts = get_texts(user.language)

            template = texts.get(
                'SUBSCRIPTION_PRERENEW_SAVE',
                (
                    '⏳ <b>Подписка истекает через {hours_left} ч</b>\n\n'
                    'Продлите сейчас со скидкой {percent}% — не теряйте доступ. '
                    'Скидка суммируется с промогруппой и действует до {expires_at}.'
                ),
            )
            message = template.format(
                hours_left=hours_left,
                percent=percent,
                expires_at=format_local_datetime(expires_at, '%d.%m.%Y %H:%M'),
            )

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            extend_callback = f'se:{subscription.id}' if settings.is_multi_tariff_enabled() else 'subscription_extend'

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        build_miniapp_or_callback_button(
                            text='🎁 Получить скидку', callback_data=f'nz!_claim_discount_{offer_id}'
                        )
                    ],
                    [
                        build_miniapp_or_callback_button(
                            text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                            callback_data=f'nz!_{extend_callback}',
                        )
                    ],
                    [
                        build_miniapp_or_callback_button(
                            text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'),
                            callback_data='nz!_balance_topup',
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=texts.t('SUPPORT_BUTTON', '🆘 Поддержка'), callback_data='nz!_menu_support'
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
            if await self._handle_unreachable_user(user, exc, 'churn-save уведомление'):
                return True
            logger.error(
                'Ошибка Telegram API при отправке churn-save уведомления',
                telegram_id=user.telegram_id,
                exc=exc,
            )
            return False
        except TelegramNetworkError as e:
            logger.warning('Таймаут отправки churn-save уведомления', telegram_id=user.telegram_id, e=e)
            return False
        except Exception as e:
            logger.error('Ошибка отправки churn-save уведомления', telegram_id=user.telegram_id, e=e)
            return False
```

NOTE: `User`, `get_texts`, `build_miniapp_or_callback_button`, `format_local_datetime` are already imported at module top (the existing `_send_expired_discount_notification` uses the same names without local imports). Do not add duplicate imports. If any name is somehow missing, mirror exactly how `_send_expired_discount_notification` references it.

- [ ] **Step 5: Register the check in the monitoring loop**

Find (~line 245):

```python
                await self._check_expiring_subscriptions(db)
```

Add immediately after it:

```python
                await self._check_prerenew_save_offers(db)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_monitoring_prerenew_save.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Run the wider service suite to check for regressions**

Run: `.venv/Scripts/python.exe -m pytest tests/services/ -q`
Expected: No NEW failures vs baseline (pre-existing failures unrelated to this change are acceptable; note the count before and after).

- [ ] **Step 8: Commit**

```bash
git add app/services/monitoring_service.py tests/services/test_monitoring_prerenew_save.py
git commit -m "feat(churn-save): pre-expiry save-offer monitoring check"
```

---

## Task 3: Admin panel controls for prerenew_save

**Files:**
- Modify: `app/handlers/admin/monitoring.py`

**Context:** This handler already renders toggle + numeric-edit controls for `expired_second_wave` and `expired_third_wave` using `NotificationSettingsService.set_second_wave_enabled` / `set_second_wave_discount_percent` / `set_second_wave_valid_hours` (and third-wave equivalents incl. `trigger_days`). Mirror that exact pattern for `prerenew_save`.

- [ ] **Step 1: Read the existing wave controls**

Read `app/handlers/admin/monitoring.py`. Locate the block that renders and handles `expired_second_wave` (toggle button, callback handler, and the FSM/numeric edit flow for `discount_percent` and `valid_hours`) and `expired_third_wave` (which additionally edits `trigger_days`).

- [ ] **Step 2: Add a "Churn-save (до истечения)" section mirroring the wave block**

Add, mirroring the second/third-wave UI exactly:
- A toggle button calling `NotificationSettingsService.set_prerenew_save_enabled(...)`, reading state via `is_prerenew_save_enabled()`.
- Numeric edit entries for:
  - discount percent → `set_prerenew_save_discount_percent` / `get_prerenew_save_discount_percent`
  - valid hours → `set_prerenew_save_valid_hours` / `get_prerenew_save_valid_hours`
  - trigger hours → `set_prerenew_save_trigger_hours` / `get_prerenew_save_trigger_hours`

Use new, unique callback_data keys consistent with the naming convention already used for the wave callbacks in this file (e.g. if waves use `admin_notif_w2_toggle`, use `admin_notif_prerenew_toggle`, `admin_notif_prerenew_percent`, `admin_notif_prerenew_valid`, `admin_notif_prerenew_trigger`). Label the section header `🛟 Churn-save (скидка до истечения)`.

- [ ] **Step 3: Verify the handler imports cleanly**

Run: `.venv/Scripts/python.exe -c "import app.handlers.admin.monitoring"`
Expected: no ImportError / syntax error.

If a router/registration smoke test exists for admin handlers, run it:
Run: `.venv/Scripts/python.exe -m pytest tests/ -k "admin and (notif or monitor)" -q`
Expected: PASS or "no tests ran" (acceptable — admin UI has no unit tests in this repo).

- [ ] **Step 4: Commit**

```bash
git add app/handlers/admin/monitoring.py
git commit -m "feat(churn-save): admin controls for prerenew_save settings"
```

---

## Self-Review Checklist (controller runs before final review)

- [ ] Spec coverage: settings (Task 1), check+sender+loop (Task 2), admin UI (Task 3), default OFF, at-risk = `autopay_enabled == False`, dedup key `prerenew_save`, one offer per sub — all present.
- [ ] No placeholders: every code step shows full code.
- [ ] Type consistency: method names `_check_prerenew_save_offers` / `_send_prerenew_save_notification` and settings methods identical across tasks; `notification_type='prerenew_save'` consistent everywhere.
- [ ] Locale key `SUBSCRIPTION_PRERENEW_SAVE` has an inline default in the sender, so missing locale entries won't break (matches `_send_expired_discount_notification` pattern).

## Out of plan scope (follow-ups)

- Translating `SUBSCRIPTION_PRERENEW_SAVE` into all locale files (ru/en/fa/zh/ua). Inline default covers runtime; add localized strings later.
- Balance-based at-risk detection (autopay-enabled-but-underfunded) — deferred per spec; caught by post-expiry win-back.
