# Заморозка подписки (vacation freeze) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать пользователям заморозить обычную (non-daily) подписку: сохранить оставшееся время, отключить на RemnaWave-панели, потом возобновить (вручную или авто) без потерь. С настраиваемым админом антиабузом.

**Architecture:** `FreezeService` инкапсулирует freeze/resume (валидация → панель через существующие `disable_remnawave_user`/`enable_remnawave_user` → сохранение времени в `end_date`). Замороженные подписки пропускаются expiry-проверкой. Scheduler авто-размораживает по дедлайну. Настройки — JSON-конфиг. Точки входа: кабинет (REST) + бот (кнопка). Всё за флагами (дефолт OFF).

**Tech Stack:** Python 3.12, aiogram 3.x, FastAPI, SQLAlchemy async, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-05-30-subscription-freeze-design.md`

**Run tests:** `.venv/Scripts/python.exe -m pytest <path> -v`

---

## File Structure

- `migrations/alembic/versions/0097_add_freeze_fields.py` + `app/database/models.py` — миграция + 5 полей Subscription (Task 1).
- `app/services/freeze_settings_service.py` — JSON-конфиг (Task 2).
- `app/services/freeze_service.py` — FreezeError + FreezeService (Task 3).
- `app/services/monitoring_service.py` + `app/database/crud/subscription.py` — auto-resume + expiry-skip (Task 4).
- `app/cabinet/routes/subscription_modules/freeze.py` + `subscription.py` + `__init__.py` — REST (Task 5).
- `app/handlers/subscription/` + keyboards — бот-кнопка (Task 6).
- `app/handlers/admin/freeze.py` + `app/bot.py` + `app/config.py` + `.env.example` — admin-UI + флаг (Task 7).

Tests: `tests/services/test_freeze_settings.py` (Task 2), `tests/services/test_freeze_service.py` (Task 3).

---

## Task 1: миграция + поля Subscription

**Files:**
- Create: `migrations/alembic/versions/0097_add_freeze_fields.py`
- Modify: `app/database/models.py` (class Subscription, near `is_daily_paused` ~line 2106)

- [ ] **Step 1: Add columns to Subscription model**

In `app/database/models.py`, in `class Subscription`, near `is_daily_paused`/`last_daily_charge_at` (~line 2106-2109), add:

```python
    # Заморозка (vacation freeze) для обычных подписок
    frozen_at = Column(AwareDateTime(), nullable=True)
    frozen_until = Column(AwareDateTime(), nullable=True)
    freeze_days_used_year = Column(Integer, default=0, nullable=False, server_default='0')
    freeze_year = Column(Integer, nullable=True)
    last_freeze_at = Column(AwareDateTime(), nullable=True)
```

`Integer` and `AwareDateTime` are already imported/used in this file.

- [ ] **Step 2: Create migration**

Create `migrations/alembic/versions/0097_add_freeze_fields.py`:

```python
"""add freeze fields to subscriptions

Revision ID: 0097
Revises: 0096
Create Date: 2026-05-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0097'
down_revision: Union[str, None] = '0096'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('frozen_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('subscriptions', sa.Column('frozen_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('subscriptions', sa.Column('freeze_days_used_year', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('subscriptions', sa.Column('freeze_year', sa.Integer(), nullable=True))
    op.add_column('subscriptions', sa.Column('last_freeze_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('subscriptions', 'last_freeze_at')
    op.drop_column('subscriptions', 'freeze_year')
    op.drop_column('subscriptions', 'freeze_days_used_year')
    op.drop_column('subscriptions', 'frozen_until')
    op.drop_column('subscriptions', 'frozen_at')
```

FIRST confirm `0096` is the current single head (it is — `0096_add_birthday_fields.py`). If not, set down_revision to the real head.

- [ ] **Step 3: Verify**

Run: `.venv/Scripts/python.exe -c "import app.database.models; print('models OK')"`
Run: `.venv/Scripts/python.exe -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('alembic.ini')); print('heads:', s.get_heads())"` → expect single head `('0097',)`.

- [ ] **Step 4: Commit**

```bash
git add app/database/models.py migrations/alembic/versions/0097_add_freeze_fields.py
git commit -m "feat(freeze): add freeze tracking fields to subscriptions (migration 0097)"
```

---

## Task 2: freeze settings service

**Files:**
- Create: `app/services/freeze_settings_service.py`
- Test: `tests/services/test_freeze_settings.py`

**Context:** Copy the structure of `app/services/birthday_settings_service.py` EXACTLY (same `_storage_path`/`_data`/`_loaded`/`_DEFAULTS`/`_load`/`_apply_defaults`/`_save`/`_get`/`_set_field` machinery). Single config key `subscription_freeze`. Read `app/services/birthday_settings_service.py` first and mirror it.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_freeze_settings.py`:

```python
import pytest

from app.services.freeze_settings_service import FreezeSettingsService as FSS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(FSS, '_storage_path', tmp_path / 'freeze_settings.json')
    monkeypatch.setattr(FSS, '_data', {})
    monkeypatch.setattr(FSS, '_loaded', False)
    yield


def test_defaults():
    assert FSS.is_enabled() is False
    assert FSS.get_max_days_per_year() == 30
    assert FSS.get_min_subscription_age_days() == 7
    assert FSS.get_cooldown_days() == 7
    assert FSS.get_min_freeze_days() == 3
    assert FSS.get_max_single_freeze_days() == 30


def test_setters_roundtrip():
    assert FSS.set_enabled(True) is True
    assert FSS.is_enabled() is True
    assert FSS.set_max_days_per_year(60) is True
    assert FSS.get_max_days_per_year() == 60
    assert FSS.set_cooldown_days(0) is True
    assert FSS.get_cooldown_days() == 0


def test_validation_and_clamp():
    assert FSS.set_max_days_per_year('x') is False
    FSS.set_max_days_per_year(99999)
    assert FSS.get_max_days_per_year() == 365
    FSS.set_min_freeze_days(0)
    assert FSS.get_min_freeze_days() == 1
```

- [ ] **Step 2: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_freeze_settings.py -v` → ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `app/services/freeze_settings_service.py` mirroring `birthday_settings_service.py`. Config key `'subscription_freeze'`, `_DEFAULTS`:

```python
    _DEFAULTS = {
        'subscription_freeze': {
            'enabled': False,
            'max_days_per_year': 30,
            'min_subscription_age_days': 7,
            'cooldown_days': 7,
            'min_freeze_days': 3,
            'max_single_freeze_days': 30,
        }
    }
```

Provide classmethods (copy the exact `_load`/`_apply_defaults`/`_save`/`_get`/`_set_field`/`get_config` from birthday_settings_service, then):
- `is_enabled()` / `set_enabled(bool)`
- `get_max_days_per_year()` / `set_max_days_per_year(int)` — clamp 0..365
- `get_min_subscription_age_days()` / `set_min_subscription_age_days(int)` — clamp 0..365
- `get_cooldown_days()` / `set_cooldown_days(int)` — clamp 0..365
- `get_min_freeze_days()` / `set_min_freeze_days(int)` — clamp 1..365
- `get_max_single_freeze_days()` / `set_max_single_freeze_days(int)` — clamp 1..365

Each int getter: `try: return max(LO, min(HI, int(value))) except (TypeError, ValueError): return DEFAULT`. Each int setter: `try: v = max(LO, min(HI, int(value))) except (TypeError, ValueError): return False; return self._set_field(...)`. `set_max_days_per_year('x')` must return False (int() raises → return False before clamping).

- [ ] **Step 4: Run → PASS (3 tests)**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_freeze_settings.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/services/freeze_settings_service.py tests/services/test_freeze_settings.py
git commit -m "feat(freeze): runtime settings service"
```

---

## Task 3: FreezeService (core logic)

**Files:**
- Create: `app/services/freeze_service.py`
- Test: `tests/services/test_freeze_service.py`

**Context:** `SubscriptionService` (`app/services/subscription_service.py`) has idempotent `async disable_remnawave_user(uuid) -> bool` (line 915) and `async enable_remnawave_user(uuid) -> bool` (line 951). `SubscriptionStatus.ACTIVE.value == 'active'`. `Subscription` has `status`, `is_trial`, `end_date`, `created_at`, `remnawave_uuid`, `tariff` (with `.is_daily`), and the new freeze fields. `remnawave_retry_queue.enqueue(subscription_id, user_id, action='update')` exists (`app/services/remnawave_retry_queue.py:45`). `FreezeSettingsService` from Task 2. Use `import math` for `ceil`.

- [ ] **Step 1: Write failing tests**

Create `tests/services/test_freeze_service.py`:

```python
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.freeze_service as fs
from app.services.freeze_service import FreezeError, FreezeService


def _sub(**kw):
    now = datetime.now(UTC)
    base = dict(
        id=1, user_id=10, status='active', is_trial=False,
        end_date=now + timedelta(days=20), created_at=now - timedelta(days=60),
        remnawave_uuid='uuid-main', frozen_at=None, frozen_until=None,
        freeze_days_used_year=0, freeze_year=None, last_freeze_at=None,
        tariff=SimpleNamespace(is_daily=False),
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def service():
    svc = FreezeService()
    svc._subscription_service = MagicMock()
    svc._subscription_service.disable_remnawave_user = AsyncMock(return_value=True)
    svc._subscription_service.enable_remnawave_user = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(fs.FreezeSettingsService, 'get_max_days_per_year', classmethod(lambda cls: 30))
    monkeypatch.setattr(fs.FreezeSettingsService, 'get_min_subscription_age_days', classmethod(lambda cls: 7))
    monkeypatch.setattr(fs.FreezeSettingsService, 'get_cooldown_days', classmethod(lambda cls: 7))
    monkeypatch.setattr(fs.FreezeSettingsService, 'get_min_freeze_days', classmethod(lambda cls: 3))
    monkeypatch.setattr(fs.FreezeSettingsService, 'get_max_single_freeze_days', classmethod(lambda cls: 30))
    yield


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_freeze_happy(service):
    sub = _sub()
    db = _db()
    await service.freeze_subscription(db, sub, SimpleNamespace(id=10))
    assert sub.frozen_at is not None
    assert sub.frozen_until is not None
    service._subscription_service.disable_remnawave_user.assert_awaited_once_with('uuid-main')
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_freeze_rejects_trial(service):
    sub = _sub(is_trial=True)
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))
    service._subscription_service.disable_remnawave_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_freeze_rejects_daily(service):
    sub = _sub(tariff=SimpleNamespace(is_daily=True))
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))


@pytest.mark.asyncio
async def test_freeze_rejects_already_frozen(service):
    sub = _sub(frozen_at=datetime.now(UTC))
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))


@pytest.mark.asyncio
async def test_freeze_rejects_young_subscription(service):
    sub = _sub(created_at=datetime.now(UTC) - timedelta(days=2))
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))


@pytest.mark.asyncio
async def test_freeze_rejects_quota_exhausted(service):
    sub = _sub(freeze_year=datetime.now(UTC).year, freeze_days_used_year=30)
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))


@pytest.mark.asyncio
async def test_freeze_panel_failure_rolls_back(service):
    service._subscription_service.disable_remnawave_user = AsyncMock(return_value=False)
    sub = _sub()
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))
    assert sub.frozen_at is None
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_resume_happy_extends_end_date(service):
    now = datetime.now(UTC)
    frozen_at = now - timedelta(days=5)
    sub = _sub(frozen_at=frozen_at, frozen_until=now + timedelta(days=25),
               end_date=now + timedelta(days=10), freeze_year=now.year, freeze_days_used_year=0)
    db = _db()
    old_end = sub.end_date
    await service.resume_subscription(db, sub, SimpleNamespace(id=10), reason='manual')
    assert sub.frozen_at is None
    assert sub.end_date > old_end  # extended by ~5 days
    assert sub.freeze_days_used_year >= 5
    service._subscription_service.enable_remnawave_user.assert_awaited_once_with('uuid-main')


@pytest.mark.asyncio
async def test_resume_capped_at_frozen_until(service):
    now = datetime.now(UTC)
    sub = _sub(frozen_at=now - timedelta(days=40), frozen_until=now - timedelta(days=10),
               end_date=now, freeze_year=now.year)
    db = _db()
    old_end = sub.end_date
    await service.resume_subscription(db, sub, SimpleNamespace(id=10), reason='auto')
    # extension capped at frozen_until - frozen_at = 30 days, not 40
    delta_days = (sub.end_date - old_end).days
    assert delta_days <= 30


@pytest.mark.asyncio
async def test_resume_panel_failure_keeps_time_enqueues(service, monkeypatch):
    service._subscription_service.enable_remnawave_user = AsyncMock(return_value=False)
    enqueue = MagicMock()
    monkeypatch.setattr(fs.remnawave_retry_queue, 'enqueue', enqueue)
    now = datetime.now(UTC)
    sub = _sub(frozen_at=now - timedelta(days=5), frozen_until=now + timedelta(days=25),
               end_date=now + timedelta(days=10), freeze_year=now.year)
    db = _db()
    await service.resume_subscription(db, sub, SimpleNamespace(id=10), reason='manual')
    assert sub.frozen_at is None  # time still restored
    enqueue.assert_called_once()
```

- [ ] **Step 2: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_freeze_service.py -v` → ModuleNotFoundError.

- [ ] **Step 3: Implement FreezeService**

Create `app/services/freeze_service.py`:

```python
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import structlog

from app.database.models import SubscriptionStatus
from app.services.freeze_settings_service import FreezeSettingsService
from app.services.remnawave_retry_queue import remnawave_retry_queue
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


class FreezeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def remaining_year_quota(subscription, max_year_days: int, now: datetime) -> int:
    used = subscription.freeze_days_used_year if subscription.freeze_year == now.year else 0
    return max(0, max_year_days - used)


class FreezeService:
    def __init__(self) -> None:
        self._subscription_service = SubscriptionService()

    async def freeze_subscription(self, db, subscription, user) -> None:
        now = datetime.now(UTC)

        if subscription.frozen_at is not None:
            raise FreezeError('already_frozen', 'Подписка уже заморожена.')
        if getattr(subscription, 'is_trial', False):
            raise FreezeError('trial', 'Тестовую подписку нельзя заморозить.')
        tariff = getattr(subscription, 'tariff', None)
        if tariff is not None and getattr(tariff, 'is_daily', False):
            raise FreezeError('daily', 'Суточную подписку нельзя заморозить (используйте паузу).')
        if subscription.status != SubscriptionStatus.ACTIVE.value:
            raise FreezeError('not_active', 'Заморозить можно только активную подписку.')

        min_age = FreezeSettingsService.get_min_subscription_age_days()
        created = getattr(subscription, 'created_at', None)
        if created is not None and (now - created) < timedelta(days=min_age):
            raise FreezeError('too_young', f'Подписка должна быть старше {min_age} дн.')

        cooldown = FreezeSettingsService.get_cooldown_days()
        last = getattr(subscription, 'last_freeze_at', None)
        if last is not None and (now - last) < timedelta(days=cooldown):
            raise FreezeError('cooldown', f'Заморозка доступна не чаще раза в {cooldown} дн.')

        max_year = FreezeSettingsService.get_max_days_per_year()
        remaining = remaining_year_quota(subscription, max_year, now)
        min_freeze = FreezeSettingsService.get_min_freeze_days()
        if remaining < min_freeze:
            raise FreezeError('quota_exhausted', f'Осталось {remaining} дн. заморозки в этом году.')

        max_single = min(FreezeSettingsService.get_max_single_freeze_days(), remaining)

        subscription.frozen_at = now
        subscription.frozen_until = now + timedelta(days=max_single)

        uuid = getattr(subscription, 'remnawave_uuid', None)
        if uuid:
            ok = await self._subscription_service.disable_remnawave_user(uuid)
            if not ok:
                subscription.frozen_at = None
                subscription.frozen_until = None
                await db.rollback()
                raise FreezeError('panel_error', 'Не удалось отключить доступ. Попробуйте позже.')

        await db.commit()
        logger.info('freeze.frozen', subscription_id=subscription.id, until=subscription.frozen_until)

    async def resume_subscription(self, db, subscription, user, *, reason: str = 'manual') -> None:
        if subscription.frozen_at is None:
            return  # idempotent no-op

        now = datetime.now(UTC)
        until = subscription.frozen_until or now
        now_capped = min(now, until)
        actual = now_capped - subscription.frozen_at
        if actual.total_seconds() < 0:
            actual = timedelta(0)

        if subscription.end_date is not None:
            subscription.end_date = subscription.end_date + actual

        if subscription.freeze_year != now.year:
            subscription.freeze_days_used_year = 0
            subscription.freeze_year = now.year
        subscription.freeze_days_used_year += math.ceil(actual.total_seconds() / 86400)

        subscription.last_freeze_at = now
        subscription.frozen_at = None
        subscription.frozen_until = None

        await db.commit()

        uuid = getattr(subscription, 'remnawave_uuid', None)
        if uuid:
            ok = await self._subscription_service.enable_remnawave_user(uuid)
            if not ok:
                remnawave_retry_queue.enqueue(
                    subscription_id=subscription.id, user_id=subscription.user_id, action='update',
                )
                logger.warning('freeze.resume_panel_failed_enqueued', subscription_id=subscription.id)

        logger.info('freeze.resumed', subscription_id=subscription.id, reason=reason)
```

- [ ] **Step 4: Run → PASS (10 tests)**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_freeze_service.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/services/freeze_service.py tests/services/test_freeze_service.py
git commit -m "feat(freeze): FreezeService freeze/resume with anti-abuse + panel sync"
```

---

## Task 4: scheduler auto-resume + expiry skip-guard

**Files:**
- Modify: `app/database/crud/subscription.py` (`check_and_update_subscription_status`)
- Modify: `app/services/monitoring_service.py` (new `_check_frozen_subscriptions` + loop registration)

- [ ] **Step 1: Add expiry skip-guard**

In `app/database/crud/subscription.py`, in `check_and_update_subscription_status` (~line 1525), right after the function fetches the subscription state and near the existing `is_daily_paused` skip (~line 1538), add at the top of the status-check logic:

```python
    if getattr(subscription, 'frozen_at', None) is not None:
        logger.info('❄️ Подписка заморожена, пропускаем проверку истечения', subscription_id=subscription.id)
        return subscription
```

Place it BEFORE the `is_daily_paused` check or right next to it — anywhere before the `end_date <= current_time` → EXPIRED transition.

- [ ] **Step 2: Add the scheduler test**

Create `tests/services/test_monitoring_frozen.py`:

```python
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.monitoring_service as ms
from app.services.monitoring_service import MonitoringService


@pytest.fixture
def service():
    svc = MonitoringService.__new__(MonitoringService)
    svc.bot = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_check_frozen_resumes_past_deadline(service, monkeypatch):
    now = datetime.now(UTC)
    sub = SimpleNamespace(id=1, user_id=10, frozen_at=now - timedelta(days=40),
                          frozen_until=now - timedelta(days=1))
    result = MagicMock()
    result.scalars.return_value.all.return_value = [sub]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    resume = AsyncMock()
    monkeypatch.setattr(ms, 'freeze_service', SimpleNamespace(resume_subscription=resume))

    await service._check_frozen_subscriptions(db)

    resume.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_frozen_none_due(service, monkeypatch):
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    resume = AsyncMock()
    monkeypatch.setattr(ms, 'freeze_service', SimpleNamespace(resume_subscription=resume))

    await service._check_frozen_subscriptions(db)

    resume.assert_not_awaited()
```

- [ ] **Step 3: Run → FAIL** (`_check_frozen_subscriptions` missing)

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_monitoring_frozen.py -v`

- [ ] **Step 4: Implement `_check_frozen_subscriptions` + module singleton import**

In `app/services/monitoring_service.py`, add a module-level import near the top (after other service imports):

```python
from app.services.freeze_service import FreezeService

freeze_service = FreezeService()
```

(If a circular import results — `freeze_service` imports `subscription_service` which may import monitoring — instead import lazily inside the method: `from app.services.freeze_service import freeze_service` won't exist as a singleton, so create the singleton at module level in `freeze_service.py` too: add `freeze_service = FreezeService()` at the end of `app/services/freeze_service.py` in Task 3. If you're doing Task 4 and that singleton is missing, add it now and note it. Tests monkeypatch `ms.freeze_service`, so the name `freeze_service` must exist at module scope in monitoring_service.)

Add the method (near `_check_expired_subscription_followups`):

```python
    async def _check_frozen_subscriptions(self, db: AsyncSession):
        try:
            now = datetime.now(UTC)
            result = await db.execute(
                select(Subscription).where(
                    Subscription.frozen_at.isnot(None),
                    Subscription.frozen_until.isnot(None),
                    Subscription.frozen_until <= now,
                ).options(selectinload(Subscription.user))
            )
            subs = result.scalars().all()
            for sub in subs:
                try:
                    await freeze_service.resume_subscription(db, sub, sub.user, reason='auto')
                except Exception as exc:
                    logger.warning('freeze.auto_resume_failed', subscription_id=sub.id, err=str(exc))
        except Exception as exc:
            logger.error('freeze.check_frozen_failed', err=str(exc))
```

Register in the monitoring loop (near the other `await self._check_*` calls, ~line 244-255):

```python
                await self._check_frozen_subscriptions(db)
```

- [ ] **Step 5: Run → PASS**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_monitoring_frozen.py tests/services/test_freeze_service.py -v`

- [ ] **Step 6: Commit**

```bash
git add app/services/monitoring_service.py app/services/freeze_service.py app/database/crud/subscription.py tests/services/test_monitoring_frozen.py
git commit -m "feat(freeze): auto-resume scheduler + expiry skip-guard for frozen subs"
```

---

## Task 5: cabinet REST endpoints

**Files:**
- Create: `app/cabinet/routes/subscription_modules/freeze.py`
- Modify: `app/cabinet/routes/subscription_modules/__init__.py`, `app/cabinet/routes/subscription.py`

**Context:** Mirror `daily.py` (`@router.post('/pause')`, `resolve_subscription(db, user, subscription_id)` from `.helpers`, `get_cabinet_db`/`get_current_cabinet_user` from `...dependencies`). Routers are aggregated in `subscription.py` (`from .subscription_modules import (... daily_router ...)` then `router.include_router(daily_router)`), exported in `__init__.py`.

- [ ] **Step 1: Implement freeze.py**

Create `app/cabinet/routes/subscription_modules/freeze.py`:

```python
"""Subscription freeze (vacation) endpoints: POST /freeze, POST /resume."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.freeze_service import FreezeError, FreezeService

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)
router = APIRouter()

_freeze_service = FreezeService()

_CODE_TO_STATUS = {
    'already_frozen': status.HTTP_409_CONFLICT,
    'not_frozen': status.HTTP_409_CONFLICT,
    'trial': status.HTTP_400_BAD_REQUEST,
    'daily': status.HTTP_400_BAD_REQUEST,
    'not_active': status.HTTP_400_BAD_REQUEST,
    'too_young': status.HTTP_400_BAD_REQUEST,
    'cooldown': status.HTTP_429_TOO_MANY_REQUESTS,
    'quota_exhausted': status.HTTP_400_BAD_REQUEST,
    'panel_error': status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _guard_enabled() -> None:
    if not settings.SUBSCRIPTION_FREEZE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Freeze disabled')


@router.post('/freeze')
async def freeze_subscription(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None),
) -> dict[str, Any]:
    _guard_enabled()
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')
    try:
        await _freeze_service.freeze_subscription(db, subscription, user)
    except FreezeError as e:
        raise HTTPException(status_code=_CODE_TO_STATUS.get(e.code, 400), detail={'code': e.code, 'message': e.message})
    return {'frozen': True, 'frozen_until': subscription.frozen_until.isoformat() if subscription.frozen_until else None}


@router.post('/resume')
async def resume_subscription(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None),
) -> dict[str, Any]:
    _guard_enabled()
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')
    try:
        await _freeze_service.resume_subscription(db, subscription, user, reason='manual')
    except FreezeError as e:
        raise HTTPException(status_code=_CODE_TO_STATUS.get(e.code, 400), detail={'code': e.code, 'message': e.message})
    return {'frozen': False, 'end_date': subscription.end_date.isoformat() if subscription.end_date else None}
```

- [ ] **Step 2: Register the router**

In `app/cabinet/routes/subscription_modules/__init__.py`: add `from .freeze import router as freeze_router` and add `'freeze_router'` to `__all__`.

In `app/cabinet/routes/subscription.py`: add `freeze_router` to the `from .subscription_modules import (...)` block, and add `router.include_router(freeze_router)` next to `router.include_router(daily_router)` (~line 54).

- [ ] **Step 3: Verify import**

Run: `.venv/Scripts/python.exe -c "import app.cabinet.routes.subscription; print('OK')"`
(Note: `settings.SUBSCRIPTION_FREEZE_ENABLED` is added in Task 7 — read at call time, import still succeeds. If import fails ONLY due to that attribute, defer verification to after Task 7 and note it.)

- [ ] **Step 4: Commit**

```bash
git add app/cabinet/routes/subscription_modules/freeze.py app/cabinet/routes/subscription_modules/__init__.py app/cabinet/routes/subscription.py
git commit -m "feat(freeze): cabinet REST endpoints /freeze /resume"
```

---

## Task 6: bot freeze button

**Files:**
- Modify: `app/handlers/subscription/` (the subscription menu handler + keyboard)

**Context:** Find the subscription menu screen in the bot (grep for the callback that renders "my subscription", e.g. `nz!_menu_subscription` / `show_subscription_info`). Add a freeze/resume button + its callback handler. Read how an existing subscription-action button (e.g. autopay toggle or `nz!_buy_traffic`) is wired and mirror it.

- [ ] **Step 1: Locate the subscription menu + an existing action button**

Grep `app/handlers/subscription/` and `app/handlers/menu.py` for where the subscription info screen is built and where buttons like autopay/extend are added + registered (`dp.callback_query.register(...)`). Identify the keyboard-building function and the registration site.

- [ ] **Step 2: Add the freeze button + handler**

- In the subscription keyboard builder: if `settings.SUBSCRIPTION_FREEZE_ENABLED` and the subscription is non-daily/non-trial/active, add a button: when `frozen_at is None` show `❄️ Заморозить` (callback `nz!_freeze_sub`), else `▶️ Разморозить` (callback `nz!_resume_sub`). (If the keyboard builder doesn't have the subscription object handy, gate only on the flag and let the handler validate.)
- Add two handlers mirroring an existing subscription-action handler signature (they receive `callback, db_user, db` per this codebase's pattern — confirm by reading a sibling like the autopay handler):
  ```python
  async def handle_freeze_subscription(callback, db_user, db, ...):
      from app.services.freeze_service import FreezeService, FreezeError
      from app.database.crud.subscription import get_active_subscriptions_by_user_id
      subs = await get_active_subscriptions_by_user_id(db, db_user.id)
      if not subs:
          await callback.answer('Нет активной подписки', show_alert=True); return
      try:
          await FreezeService().freeze_subscription(db, subs[0], db_user)
      except FreezeError as e:
          await callback.answer(e.message, show_alert=True); return
      await callback.answer('❄️ Подписка заморожена')
      # re-render the subscription menu (call the existing render fn)
  ```
  and `handle_resume_subscription` calling `resume_subscription(..., reason='manual')`.
- Register both callbacks in the same place sibling subscription callbacks are registered (`dp.callback_query.register(handle_freeze_subscription, F.data == 'nz!_freeze_sub')`, same for resume).

Match the REAL handler signature + registration mechanism used by sibling subscription handlers (read one first — do not guess the (callback, db_user, db) shape; mirror exactly).

- [ ] **Step 3: Verify import**

Run: `.venv/Scripts/python.exe -c "import app.handlers.subscription; import app.handlers.menu; print('OK')"`

- [ ] **Step 4: Commit**

```bash
git add app/handlers/
git commit -m "feat(freeze): bot freeze/resume button in subscription menu"
```

---

## Task 7: admin panel + config flag + wiring

**Files:**
- Create: `app/handlers/admin/freeze.py`
- Modify: `app/bot.py`, `app/config.py`, `.env.example`

**Context:** Mirror `app/handlers/admin/birthday.py` (created in the birthday feature) — same router-less `register_handlers(dp)` style, numeric edits via shared admin states, entry button in the admin settings keyboard.

- [ ] **Step 1: Add config flag**

In `app/config.py`, near `BIRTHDAY_BONUS_ENABLED`, add:

```python
    SUBSCRIPTION_FREEZE_ENABLED: bool = False  # Master kill-switch for the subscription-freeze feature
```

- [ ] **Step 2: Implement admin panel**

Read `app/handlers/admin/birthday.py` and mirror it for freeze. Provide a menu (`admin_freeze_menu`) showing `FreezeSettingsService.get_config()`, a toggle (`admin_freeze_toggle` → `set_enabled`), and numeric editors for: max_days_per_year, min_subscription_age_days, cooldown_days, min_freeze_days, max_single_freeze_days (callback_data `admin_freeze_edit_*`, new FSM states in the same States class birthday used, e.g. `FreezeAdminStates`). `register_handlers(dp)` mirroring birthday's. Add an entry button `admin_freeze_menu` in the admin settings keyboard next to the birthday button (mirror `app/keyboards/admin.py` where `admin_birthday_menu` was added).

- [ ] **Step 3: Register in app/bot.py**

Add `from app.handlers.admin import freeze as admin_freeze` near the admin imports, and `admin_freeze.register_handlers(dp)` after `admin_birthday.register_handlers(dp)`.

- [ ] **Step 4: Document in .env.example**

```
# Subscription freeze (vacation): let users pause a regular subscription, preserving remaining time.
SUBSCRIPTION_FREEZE_ENABLED=false
```

- [ ] **Step 5: Verify + full feature test run**

Run: `.venv/Scripts/python.exe -c "import app.config; print(app.config.settings.SUBSCRIPTION_FREEZE_ENABLED)"` → `False`.
Run: `.venv/Scripts/python.exe -c "import app.handlers.admin.freeze; import app.bot; import app.cabinet.routes.subscription; print('OK')"`.
Run: `.venv/Scripts/python.exe -m pytest tests/services/test_freeze_settings.py tests/services/test_freeze_service.py tests/services/test_monitoring_frozen.py -v` → all PASS.

- [ ] **Step 6: Regression check**

Run: `.venv/Scripts/python.exe -m pytest tests/services/ -q` → no NEW failures vs baseline (~29 pre-existing acceptable; none in freeze files).

- [ ] **Step 7: Commit**

```bash
git add app/handlers/admin/freeze.py app/bot.py app/config.py .env.example app/keyboards/admin.py app/states.py
git commit -m "feat(freeze): admin panel + config flag + wiring"
```

---

## Self-Review Checklist (controller runs before final review)

- [ ] Migration 0097 single head, down_revision 0096, reversible.
- [ ] Model fields match migration column names exactly.
- [ ] freeze validates active/non-trial/non-daily/age/cooldown/quota before touching panel.
- [ ] freeze panel failure → DB rollback (no "frozen in DB / active on panel" desync).
- [ ] resume: end_date += capped duration, quota counter += days, panel failure keeps time + enqueues retry.
- [ ] frozen subs skipped by expiry check; auto-resume scheduler registered in loop.
- [ ] All gated by `SUBSCRIPTION_FREEZE_ENABLED` (env) AND `FreezeSettingsService.is_enabled()` (default OFF).
- [ ] `freeze_service` singleton exists at module scope in both freeze_service.py and monitoring_service.py (tests monkeypatch `ms.freeze_service`).

## Out of plan scope (follow-ups)

- WL-account explicit disable/enable on freeze (currently follows main on next sync — no wl uuid column).
- Excluding frozen subs from autopay/expiring-notification queries (skip-guard covers expiry transition; tighten others if needed).
- Localizing freeze bot/notification strings across locales (inline RU default).
- Showing remaining freeze quota in the bot/cabinet subscription screen.
