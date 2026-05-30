# Birthday-бонус Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Поздравлять пользователей с ДР (собранным из Telegram-профиля) и выдавать настраиваемый админом подарок один раз в год, с защитой от абуза.

**Architecture:** ДР собирается оппортунистически в `AuthMiddleware` через `bot.get_chat` (throttle 30 дней). Ежедневный scheduler `BirthdayService` (по образцу `bio_reward_service`) матчит сегодняшние ДР из БД, проверяет антиабуз, выдаёт подарок (balance/subscription_days/promocode) и шлёт поздравление. Настройки — JSON-конфиг (как `NotificationSettingsService`). Всё за флагами (дефолт OFF).

**Tech Stack:** Python 3.12, aiogram 3.x, SQLAlchemy async, Alembic, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-05-30-birthday-bonus-design.md`

**Run tests:** `.venv/Scripts/python.exe -m pytest <path> -v`

---

## File Structure

- `migrations/alembic/versions/0096_add_birthday_fields.py` — миграция (Task 1).
- `app/database/models.py` — 4 поля в `User` (Task 1).
- `app/services/birthday_settings_service.py` — JSON-конфиг (Task 2).
- `app/services/birthday_service.py` — sync + scheduler + грант + singleton (Tasks 3-4).
- `app/middlewares/auth.py` — вызов sync (Task 3).
- `app/handlers/admin/birthday.py` — admin-UI (Task 5).
- `app/config.py`, `main.py`, `app/bot.py` — флаг + wiring (Task 6).
- `tests/services/test_birthday_settings.py`, `tests/services/test_birthday_service.py` — тесты (Tasks 2-4).

---

## Task 1: миграция + поля User

**Files:**
- Create: `migrations/alembic/versions/0096_add_birthday_fields.py`
- Modify: `app/database/models.py` (class User, после OAuth-полей ~line 1903)

- [ ] **Step 1: Add columns to the User model**

In `app/database/models.py`, inside `class User(Base)`, after the OAuth id columns (`vk_id`, ~line 1903), add:

```python
    birth_date = Column(Date(), nullable=True)
    birthday_synced_at = Column(AwareDateTime(), nullable=True)
    birthday_changed_at = Column(AwareDateTime(), nullable=True)
    last_birthday_reward_year = Column(Integer(), nullable=True)
```

Ensure `Date` is imported from sqlalchemy at the top of models.py (the `from sqlalchemy import (...)` block). If `Date` is absent, add it. `Integer` and `AwareDateTime` are already used in this file.

- [ ] **Step 2: Create the migration**

Create `migrations/alembic/versions/0096_add_birthday_fields.py`:

```python
"""add birthday fields to users

Revision ID: 0096
Revises: 0095
Create Date: 2026-05-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0096'
down_revision: Union[str, None] = '0095'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('birth_date', sa.Date(), nullable=True))
    op.add_column('users', sa.Column('birthday_synced_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('birthday_changed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('last_birthday_reward_year', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'last_birthday_reward_year')
    op.drop_column('users', 'birthday_changed_at')
    op.drop_column('users', 'birthday_synced_at')
    op.drop_column('users', 'birth_date')
```

- [ ] **Step 3: Verify model imports and migration chain**

Run: `.venv/Scripts/python.exe -c "import app.database.models; print('models OK')"`
Expected: no ImportError.
Run: `.venv/Scripts/python.exe -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('alembic.ini')); print('heads:', s.get_heads())"`
Expected: single head `('0096',)`. If `alembic.ini` path differs, locate it (repo root) and adjust. If multiple heads appear, the down_revision chain is broken — fix `down_revision`.

- [ ] **Step 4: Commit**

```bash
git add app/database/models.py migrations/alembic/versions/0096_add_birthday_fields.py
git commit -m "feat(birthday): add birth_date + tracking fields to users (migration 0096)"
```

---

## Task 2: birthday settings service

**Files:**
- Create: `app/services/birthday_settings_service.py`
- Test: `tests/services/test_birthday_settings.py`

**Context:** Mirror the JSON-on-disk pattern of `app/services/notification_settings_service.py` (class-level `_storage_path`, `_data`, `_loaded`, `_DEFAULTS`, `_load`/`_apply_defaults`/`_save`/`_get`/`_set_field`). Single config key `birthday_bonus`.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_birthday_settings.py`:

```python
import pytest

from app.services.birthday_settings_service import BirthdaySettingsService as BSS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(BSS, '_storage_path', tmp_path / 'birthday_settings.json')
    monkeypatch.setattr(BSS, '_data', {})
    monkeypatch.setattr(BSS, '_loaded', False)
    yield


def test_defaults():
    assert BSS.is_enabled() is False
    assert BSS.get_reward_type() == 'balance'
    assert BSS.get_reward_amount() == 10000
    assert BSS.get_min_account_age_days() == 7
    assert BSS.get_dob_stable_days() == 7
    assert BSS.get_promocode_valid_days() == 7
    assert BSS.get_subscription_days_fallback() == 'balance'


def test_setters_roundtrip():
    assert BSS.set_enabled(True) is True
    assert BSS.is_enabled() is True
    assert BSS.set_reward_type('promocode') is True
    assert BSS.get_reward_type() == 'promocode'
    assert BSS.set_reward_amount(500) is True
    assert BSS.get_reward_amount() == 500
    assert BSS.set_min_account_age_days(14) is True
    assert BSS.get_min_account_age_days() == 14
    assert BSS.set_subscription_days_fallback('skip') is True
    assert BSS.get_subscription_days_fallback() == 'skip'


def test_validation_rejects_bad_values():
    assert BSS.set_reward_type('bogus') is False
    assert BSS.get_reward_type() == 'balance'  # unchanged
    assert BSS.set_reward_amount(-5) is False
    assert BSS.set_subscription_days_fallback('nonsense') is False
    BSS.set_min_account_age_days(99999)
    assert BSS.get_min_account_age_days() == 365  # clamped
```

- [ ] **Step 2: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_birthday_settings.py -v`
Expected: FAIL — module `birthday_settings_service` does not exist.

- [ ] **Step 3: Implement the service**

Create `app/services/birthday_settings_service.py`:

```python
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import structlog


logger = structlog.get_logger(__name__)

_REWARD_TYPES = ('balance', 'subscription_days', 'promocode')
_FALLBACKS = ('balance', 'skip')


class BirthdaySettingsService:
    """Runtime-editable birthday-bonus settings stored on disk."""

    _storage_path: Path = Path('data/birthday_settings.json')
    _data: dict[str, Any] = {}
    _loaded: bool = False

    _DEFAULTS: dict[str, Any] = {
        'birthday_bonus': {
            'enabled': False,
            'reward_type': 'balance',
            'reward_amount': 10000,          # kopeks (balance) / days / percent
            'promocode_valid_days': 7,
            'min_account_age_days': 7,
            'dob_stable_days': 7,
            'subscription_days_fallback': 'balance',
        }
    }

    @classmethod
    def _ensure_dir(cls) -> None:
        try:
            cls._storage_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover
            logger.error('birthday_settings.mkdir_failed', exc=exc)

    @classmethod
    def _load(cls) -> None:
        if cls._loaded:
            return
        cls._ensure_dir()
        try:
            if cls._storage_path.exists():
                raw = cls._storage_path.read_text(encoding='utf-8')
                cls._data = json.loads(raw) if raw.strip() else {}
            else:
                cls._data = {}
        except Exception as exc:
            logger.error('birthday_settings.load_failed', exc=exc)
            cls._data = {}
        if cls._apply_defaults():
            cls._save()
        cls._loaded = True

    @classmethod
    def _apply_defaults(cls) -> bool:
        changed = False
        for key, defaults in cls._DEFAULTS.items():
            current = cls._data.get(key)
            if not isinstance(current, dict):
                cls._data[key] = deepcopy(defaults)
                changed = True
                continue
            for dk, dv in defaults.items():
                if dk not in current:
                    current[dk] = dv
                    changed = True
        return changed

    @classmethod
    def _save(cls) -> bool:
        cls._ensure_dir()
        try:
            cls._storage_path.write_text(
                json.dumps(cls._data, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            return True
        except Exception as exc:
            logger.error('birthday_settings.save_failed', exc=exc)
            return False

    @classmethod
    def _get(cls) -> dict[str, Any]:
        cls._load()
        value = cls._data.get('birthday_bonus')
        if not isinstance(value, dict):
            value = deepcopy(cls._DEFAULTS['birthday_bonus'])
            cls._data['birthday_bonus'] = value
        return value

    @classmethod
    def _set_field(cls, field: str, value: Any) -> bool:
        cls._load()
        section = cls._get()
        section[field] = value
        cls._data['birthday_bonus'] = section
        return cls._save()

    @classmethod
    def get_config(cls) -> dict[str, Any]:
        cls._load()
        return deepcopy(cls._get())

    # enabled
    @classmethod
    def is_enabled(cls) -> bool:
        return bool(cls._get().get('enabled', False))

    @classmethod
    def set_enabled(cls, enabled: bool) -> bool:
        return cls._set_field('enabled', bool(enabled))

    # reward_type
    @classmethod
    def get_reward_type(cls) -> str:
        value = cls._get().get('reward_type', 'balance')
        return value if value in _REWARD_TYPES else 'balance'

    @classmethod
    def set_reward_type(cls, value: str) -> bool:
        if value not in _REWARD_TYPES:
            return False
        return cls._set_field('reward_type', value)

    # reward_amount
    @classmethod
    def get_reward_amount(cls) -> int:
        try:
            return max(0, int(cls._get().get('reward_amount', 10000)))
        except (TypeError, ValueError):
            return 10000

    @classmethod
    def set_reward_amount(cls, value: int) -> bool:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return False
        if v < 0:
            return False
        return cls._set_field('reward_amount', v)

    # promocode_valid_days
    @classmethod
    def get_promocode_valid_days(cls) -> int:
        try:
            return max(1, min(365, int(cls._get().get('promocode_valid_days', 7))))
        except (TypeError, ValueError):
            return 7

    @classmethod
    def set_promocode_valid_days(cls, value: int) -> bool:
        try:
            v = max(1, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('promocode_valid_days', v)

    # min_account_age_days
    @classmethod
    def get_min_account_age_days(cls) -> int:
        try:
            return max(0, min(365, int(cls._get().get('min_account_age_days', 7))))
        except (TypeError, ValueError):
            return 7

    @classmethod
    def set_min_account_age_days(cls, value: int) -> bool:
        try:
            v = max(0, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('min_account_age_days', v)

    # dob_stable_days
    @classmethod
    def get_dob_stable_days(cls) -> int:
        try:
            return max(0, min(365, int(cls._get().get('dob_stable_days', 7))))
        except (TypeError, ValueError):
            return 7

    @classmethod
    def set_dob_stable_days(cls, value: int) -> bool:
        try:
            v = max(0, min(365, int(value)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('dob_stable_days', v)

    # subscription_days_fallback
    @classmethod
    def get_subscription_days_fallback(cls) -> str:
        value = cls._get().get('subscription_days_fallback', 'balance')
        return value if value in _FALLBACKS else 'balance'

    @classmethod
    def set_subscription_days_fallback(cls, value: str) -> bool:
        if value not in _FALLBACKS:
            return False
        return cls._set_field('subscription_days_fallback', value)
```

- [ ] **Step 4: Run → PASS (3 tests)**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_birthday_settings.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/services/birthday_settings_service.py tests/services/test_birthday_settings.py
git commit -m "feat(birthday): runtime settings service"
```

---

## Task 3: opportunistic birthdate sync (service fn + middleware hook)

**Files:**
- Create: `app/services/birthday_service.py` (sync part + singleton; scheduler added in Task 4)
- Modify: `app/middlewares/auth.py`
- Test: `tests/services/test_birthday_service.py` (sync tests)

**Context:** aiogram `bot.get_chat(telegram_id)` returns a `ChatFullInfo` with `.birthdate` (an aiogram `Birthdate` with `.day`, `.month`, `.year` where `year` may be `None`), or `.birthdate is None`. The middleware already runs fire-and-forget tasks (`asyncio.create_task(_refresh_remnawave_description(...))`) — mirror that. Use a fresh `AsyncSessionLocal()` inside the task. Singleton pattern: end the module with `birthday_service = BirthdayService()` and a `set_bot(self, bot)` method (mirror `bio_reward_service`).

- [ ] **Step 1: Write the failing sync tests**

Create `tests/services/test_birthday_service.py`:

```python
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.birthday_service as bs
from app.services.birthday_service import BirthdayService


class _Birthdate:
    def __init__(self, day, month, year=None):
        self.day, self.month, self.year = day, month, year


def _user(**kw):
    base = dict(
        id=1, telegram_id=555, birth_date=None,
        birthday_synced_at=None, birthday_changed_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _ctx:
    def __init__(self, db):
        self._db = db
    async def __aenter__(self):
        return self._db
    async def __aexit__(self, *a):
        return False


@pytest.fixture
def service():
    svc = BirthdayService()
    svc._bot = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_sync_stores_birthdate(service, monkeypatch):
    user = _user()
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    db.commit = AsyncMock()
    monkeypatch.setattr(bs, 'AsyncSessionLocal', lambda: _ctx(db))
    service._bot.get_chat = AsyncMock(return_value=SimpleNamespace(birthdate=_Birthdate(15, 6, 1990)))

    await service.sync_user_birthday(1, 555)

    assert user.birth_date == date(1990, 6, 15)
    assert user.birthday_synced_at is not None
    assert user.birthday_changed_at is not None


@pytest.mark.asyncio
async def test_sync_none_birthdate_keeps_existing(service, monkeypatch):
    user = _user(birth_date=date(1990, 6, 15))
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    db.commit = AsyncMock()
    monkeypatch.setattr(bs, 'AsyncSessionLocal', lambda: _ctx(db))
    service._bot.get_chat = AsyncMock(return_value=SimpleNamespace(birthdate=None))

    await service.sync_user_birthday(1, 555)

    assert user.birth_date == date(1990, 6, 15)  # untouched
    assert user.birthday_synced_at is not None


@pytest.mark.asyncio
async def test_sync_change_updates_changed_at(service, monkeypatch):
    user = _user(birth_date=date(1990, 6, 15), birthday_changed_at=datetime(2020, 1, 1, tzinfo=UTC))
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    db.commit = AsyncMock()
    monkeypatch.setattr(bs, 'AsyncSessionLocal', lambda: _ctx(db))
    service._bot.get_chat = AsyncMock(return_value=SimpleNamespace(birthdate=_Birthdate(3, 3, 1992)))

    await service.sync_user_birthday(1, 555)

    assert user.birth_date == date(1992, 3, 3)
    assert user.birthday_changed_at > datetime(2020, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sync_get_chat_failure_is_swallowed(service, monkeypatch):
    user = _user()
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    db.commit = AsyncMock()
    monkeypatch.setattr(bs, 'AsyncSessionLocal', lambda: _ctx(db))
    service._bot.get_chat = AsyncMock(side_effect=RuntimeError('flood'))

    # must not raise
    await service.sync_user_birthday(1, 555)
```

- [ ] **Step 2: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_birthday_service.py -v`
Expected: FAIL — module/method missing.

- [ ] **Step 3: Implement the sync part of the service**

Create `app/services/birthday_service.py`:

```python
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import structlog
from aiogram import Bot

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.services.birthday_settings_service import BirthdaySettingsService


logger = structlog.get_logger(__name__)

SYNC_STALE_DAYS = 30
_SENTINEL_YEAR = 1900


def should_sync_birthday(user: User) -> bool:
    """True if we have never synced this user's birthday, or the sync is stale."""
    synced = getattr(user, 'birthday_synced_at', None)
    if synced is None:
        return True
    return (datetime.now(UTC) - synced) >= timedelta(days=SYNC_STALE_DAYS)


class BirthdayService:
    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._running = False

    def set_bot(self, bot: Bot) -> None:
        self._bot = bot

    def is_enabled(self) -> bool:
        return bool(settings.BIRTHDAY_BONUS_ENABLED)

    async def sync_user_birthday(self, user_id: int, telegram_id: int) -> None:
        """Fire-and-forget: pull birthdate from Telegram profile, store it.

        Swallows all errors — never breaks the triggering interaction.
        """
        if self._bot is None or not telegram_id:
            return
        try:
            chat = await self._bot.get_chat(telegram_id)
        except Exception as exc:
            logger.debug('birthday.get_chat_failed', telegram_id=telegram_id, err=str(exc))
            return

        bd = getattr(chat, 'birthdate', None)
        now = datetime.now(UTC)
        try:
            async with AsyncSessionLocal() as db:
                user = await db.get(User, user_id)
                if user is None:
                    return
                if bd is None:
                    user.birthday_synced_at = now
                    await db.commit()
                    return
                try:
                    new_date = date(bd.year or _SENTINEL_YEAR, bd.month, bd.day)
                except (ValueError, TypeError) as exc:
                    logger.debug('birthday.bad_date', telegram_id=telegram_id, err=str(exc))
                    user.birthday_synced_at = now
                    await db.commit()
                    return
                if user.birth_date != new_date:
                    user.birth_date = new_date
                    user.birthday_changed_at = now
                user.birthday_synced_at = now
                await db.commit()
        except Exception as exc:
            logger.warning('birthday.sync_failed', user_id=user_id, err=str(exc))


birthday_service = BirthdayService()
```

- [ ] **Step 4: Run sync tests → PASS (4 tests)**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_birthday_service.py -v`

- [ ] **Step 5: Hook the sync into AuthMiddleware**

In `app/middlewares/auth.py`, after `db_user.last_activity = datetime.now(UTC)` (~line 197) and before the `if profile_updated:` block, add:

```python
                if settings.BIRTHDAY_BONUS_ENABLED:
                    from app.services.birthday_service import birthday_service, should_sync_birthday

                    if db_user.telegram_id and should_sync_birthday(db_user):
                        asyncio.create_task(
                            birthday_service.sync_user_birthday(db_user.id, db_user.telegram_id)
                        )
```

`asyncio` and `settings` are already imported in this file.

- [ ] **Step 6: Verify import + commit**

Run: `.venv/Scripts/python.exe -c "import app.middlewares.auth; import app.services.birthday_service; print('OK')"`
NOTE: `settings.BIRTHDAY_BONUS_ENABLED` is added in Task 6. Module import here still succeeds (the attribute is only read at call time, not import time). If `import` fails specifically because of that attribute, defer this verification to after Task 6 and note it in the report.

```bash
git add app/services/birthday_service.py app/middlewares/auth.py tests/services/test_birthday_service.py
git commit -m "feat(birthday): opportunistic birthdate sync via middleware"
```

---

## Task 4: daily grant scheduler

**Files:**
- Modify: `app/services/birthday_service.py` (add grant + scheduler methods)
- Test: `tests/services/test_birthday_service.py` (add grant tests)

**Context:** `add_user_balance(db, user, amount_kopeks, description=..., transaction_type=TransactionType.DEPOSIT)` ALREADY creates the transaction internally (no separate `create_transaction` needed). `extend_subscription(db, subscription, days)` extends an existing subscription. Active subscription lookup: `from app.database.crud.subscription import get_active_subscriptions_by_user_id` (verify the exact name/return in `app/database/crud/subscription.py`; it returns a list). Match-today query uses SQLAlchemy `extract`.

- [ ] **Step 1: Add grant tests**

Append to `tests/services/test_birthday_service.py`:

```python
@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(bs.BirthdaySettingsService, 'is_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(bs.BirthdaySettingsService, 'get_reward_type', classmethod(lambda cls: 'balance'))
    monkeypatch.setattr(bs.BirthdaySettingsService, 'get_reward_amount', classmethod(lambda cls: 10000))
    monkeypatch.setattr(bs.BirthdaySettingsService, 'get_min_account_age_days', classmethod(lambda cls: 7))
    monkeypatch.setattr(bs.BirthdaySettingsService, 'get_dob_stable_days', classmethod(lambda cls: 7))
    monkeypatch.setattr(bs.BirthdaySettingsService, 'get_subscription_days_fallback', classmethod(lambda cls: 'balance'))
    yield


def _bday_user(**kw):
    now = datetime.now(UTC)
    base = dict(
        id=1, telegram_id=555, language='ru',
        birth_date=date(1990, now.month, now.day),
        birthday_changed_at=now - timedelta(days=400),
        created_at=now - timedelta(days=400),
        last_birthday_reward_year=None,
        status='active',
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_grant_balance_reward(service, monkeypatch):
    user = _bday_user()
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    monkeypatch.setattr(service, '_select_birthday_users', AsyncMock(return_value=[user]))
    add_balance = AsyncMock()
    monkeypatch.setattr(bs, 'add_user_balance', add_balance)
    service._notify = AsyncMock()

    await service._grant_birthday_rewards(db)

    add_balance.assert_awaited_once()
    assert user.last_birthday_reward_year == datetime.now(UTC).year
    service._notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_grant_skips_already_rewarded_this_year(service, monkeypatch):
    user = _bday_user(last_birthday_reward_year=datetime.now(UTC).year)
    db = MagicMock(); db.commit = AsyncMock(); db.rollback = AsyncMock()
    monkeypatch.setattr(service, '_select_birthday_users', AsyncMock(return_value=[user]))
    add_balance = AsyncMock()
    monkeypatch.setattr(bs, 'add_user_balance', add_balance)
    service._notify = AsyncMock()

    await service._grant_birthday_rewards(db)

    add_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_skips_young_account(service, monkeypatch):
    user = _bday_user(created_at=datetime.now(UTC) - timedelta(days=2))
    db = MagicMock(); db.commit = AsyncMock(); db.rollback = AsyncMock()
    monkeypatch.setattr(service, '_select_birthday_users', AsyncMock(return_value=[user]))
    add_balance = AsyncMock()
    monkeypatch.setattr(bs, 'add_user_balance', add_balance)
    service._notify = AsyncMock()

    await service._grant_birthday_rewards(db)

    add_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_skips_recently_changed_dob(service, monkeypatch):
    user = _bday_user(birthday_changed_at=datetime.now(UTC) - timedelta(days=2))
    db = MagicMock(); db.commit = AsyncMock(); db.rollback = AsyncMock()
    monkeypatch.setattr(service, '_select_birthday_users', AsyncMock(return_value=[user]))
    add_balance = AsyncMock()
    monkeypatch.setattr(bs, 'add_user_balance', add_balance)
    service._notify = AsyncMock()

    await service._grant_birthday_rewards(db)

    add_balance.assert_not_awaited()
```

- [ ] **Step 2: Run → FAIL** (`_grant_birthday_rewards`/`_select_birthday_users` missing)

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_birthday_service.py -v`

- [ ] **Step 3: Implement grant + scheduler**

Add imports at the top of `app/services/birthday_service.py`:

```python
import asyncio

from sqlalchemy import and_, extract, or_, select

from app.database.crud.subscription import get_active_subscriptions_by_user_id
from app.database.crud.user import add_user_balance
from app.database.models import TransactionType, UserStatus
```

Add these methods to `BirthdayService`:

```python
    async def _select_birthday_users(self, db, today: date) -> list[User]:
        is_leap = (today.year % 4 == 0 and today.year % 100 != 0) or (today.year % 400 == 0)
        if today.month == 2 and today.day == 28 and not is_leap:
            # On 28 Feb of a non-leap year, also match Feb-29 birthdays.
            result = await db.execute(
                select(User).where(
                    User.birth_date.isnot(None),
                    User.status == UserStatus.ACTIVE.value,
                    or_(
                        and_(extract('month', User.birth_date) == 2, extract('day', User.birth_date) == 28),
                        and_(extract('month', User.birth_date) == 2, extract('day', User.birth_date) == 29),
                    ),
                )
            )
            return list(result.scalars().all())
        result = await db.execute(
            select(User).where(
                and_(
                    User.birth_date.isnot(None),
                    extract('month', User.birth_date) == today.month,
                    extract('day', User.birth_date) == today.day,
                    User.status == UserStatus.ACTIVE.value,
                )
            )
        )
        return list(result.scalars().all())

    async def _grant_birthday_rewards(self, db) -> None:
        now = datetime.now(UTC)
        today = now.date()
        try:
            users = await self._select_birthday_users(db, today)
        except Exception as exc:
            logger.error('birthday.select_failed', err=str(exc))
            return

        min_age = BirthdaySettingsService.get_min_account_age_days()
        dob_stable = BirthdaySettingsService.get_dob_stable_days()
        granted = 0
        for user in users:
            try:
                if user.last_birthday_reward_year == today.year:
                    continue
                created = getattr(user, 'created_at', None)
                if created is not None and (now - created) < timedelta(days=min_age):
                    continue
                changed = getattr(user, 'birthday_changed_at', None)
                if changed is not None and (now - changed) < timedelta(days=dob_stable):
                    continue

                rewarded = await self._apply_reward(db, user)
                user.last_birthday_reward_year = today.year
                await db.commit()
                await self._notify(user, rewarded)
                granted += 1
            except Exception as exc:
                logger.warning('birthday.grant_failed', user_id=getattr(user, 'id', None), err=str(exc))
                try:
                    await db.rollback()
                except Exception:
                    pass
        if granted:
            logger.info('birthday.granted', count=granted)

    async def _apply_reward(self, db, user) -> str:
        """Apply the configured reward. Returns a short human description for the notice."""
        reward_type = BirthdaySettingsService.get_reward_type()
        amount = BirthdaySettingsService.get_reward_amount()

        if reward_type == 'subscription_days':
            subs = await get_active_subscriptions_by_user_id(db, user.id)
            if subs:
                from app.database.crud.subscription import extend_subscription

                await extend_subscription(db, subs[0], amount)
                return f'+{amount} дней подписки'
            fallback = BirthdaySettingsService.get_subscription_days_fallback()
            if fallback == 'skip':
                return ''  # no gift, congratulation only
            await add_user_balance(
                db, user, amount, description='🎂 Подарок на день рождения',
                transaction_type=TransactionType.DEPOSIT,
            )
            return f'{amount / 100:.0f} ₽ на баланс'

        if reward_type == 'promocode':
            # First version: credit balance (real personal-promocode minting is a follow-up).
            await add_user_balance(
                db, user, amount, description='🎂 Подарок на день рождения',
                transaction_type=TransactionType.DEPOSIT,
            )
            return f'{amount / 100:.0f} ₽ на баланс'

        # default: balance
        await add_user_balance(
            db, user, amount, description='🎂 Подарок на день рождения',
            transaction_type=TransactionType.DEPOSIT,
        )
        return f'{amount / 100:.0f} ₽ на баланс'

    async def _notify(self, user, reward_desc: str) -> None:
        if self._bot is None or not getattr(user, 'telegram_id', None):
            return
        gift_line = f'\n\nВаш подарок: <b>{reward_desc}</b> 🎁' if reward_desc else ''
        text = f'🎂 <b>С днём рождения!</b>{gift_line}'
        try:
            await self._bot.send_message(user.telegram_id, text, parse_mode='HTML')
        except Exception as exc:
            logger.warning('birthday.notify_failed', user_id=user.id, err=str(exc))

    async def start_monitoring(self) -> None:
        self._running = True
        logger.info('birthday.scheduler.start')
        while self._running:
            interval = 3600
            try:
                if self.is_enabled() and BirthdaySettingsService.is_enabled():
                    async with AsyncSessionLocal() as db:
                        await self._grant_birthday_rewards(db)
            except Exception as exc:
                logger.error('birthday.scheduler.error', err=str(exc), exc_info=True)
            await asyncio.sleep(interval)

    def stop_monitoring(self) -> None:
        self._running = False
        logger.info('birthday.scheduler.stop')
```

NOTE: confirm `get_active_subscriptions_by_user_id` exists and returns a list (grep `app/database/crud/subscription.py`). If its name differs, use the real one. If `extend_subscription` has required kwargs beyond `(db, subscription, days)`, pass them per its real signature.

- [ ] **Step 4: Run grant tests → PASS**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_birthday_service.py -v`
Expected: all sync + grant tests PASS (8 total).

- [ ] **Step 5: Commit**

```bash
git add app/services/birthday_service.py tests/services/test_birthday_service.py
git commit -m "feat(birthday): daily grant scheduler with anti-abuse"
```

---

## Task 5: admin panel for birthday settings

**Files:**
- Create: `app/handlers/admin/birthday.py`
- Modify: `app/bot.py` (register handlers ~line 258-259, next to bio_reward)

**Context:** Mirror `app/handlers/admin/bio_reward.py` — a self-contained admin menu for one feature with its own router, `register_handlers(dp)`, render, toggle, and numeric-edit FSM flow.

- [ ] **Step 1: Read the pattern**

Read `app/handlers/admin/bio_reward.py` fully — note its router, `register_handlers(dp)`, menu render, toggle handler, and numeric-edit FSM flow. Also grep how its menu is reached from the main admin keyboard (the callback_data of the button that opens it) so you can add a sibling entry button.

- [ ] **Step 2: Implement `app/handlers/admin/birthday.py`**

Create an admin menu with `@admin_required`-guarded handlers (mirror bio_reward exactly):
- Entry callback (e.g. `admin_birthday_menu`) rendering current `BirthdaySettingsService.get_config()`: enabled, reward_type, reward_amount, promocode_valid_days, min_account_age_days, dob_stable_days, subscription_days_fallback.
- Toggle enabled → `set_enabled(not is_enabled())`.
- Cycle `reward_type` button → balance → subscription_days → promocode → balance via `set_reward_type`.
- Cycle `subscription_days_fallback` button → balance ↔ skip.
- Numeric editors (own FSM states, mirror bio_reward's numeric edit): reward_amount, promocode_valid_days, min_account_age_days, dob_stable_days. Each validates via the setter's bool return (show error on False).
- `register_handlers(dp)` registering all the above.
- Add an entry button into the admin menu next to where bio_reward is reachable (mirror that button, new callback_data `admin_birthday_menu`).

- [ ] **Step 3: Register in app/bot.py**

Add the import near the other admin handler imports (top of file), mirroring `from app.handlers.admin import bio_reward as admin_bio_reward` → `from app.handlers.admin import birthday as admin_birthday`. After `admin_bio_reward.register_handlers(dp)` (~line 259), add:

```python
    admin_birthday.register_handlers(dp)
```

- [ ] **Step 4: Verify import**

Run: `.venv/Scripts/python.exe -c "import app.handlers.admin.birthday; import app.bot; print('OK')"`
Expected: no ImportError/SyntaxError.

- [ ] **Step 5: Commit**

```bash
git add app/handlers/admin/birthday.py app/bot.py
git commit -m "feat(birthday): admin settings panel"
```

---

## Task 6: config flag + scheduler wiring

**Files:**
- Modify: `app/config.py` (add `BIRTHDAY_BONUS_ENABLED`)
- Modify: `main.py` (set_bot + scheduler launch + shutdown cancel)
- Modify: `.env.example` (document the flag)

- [ ] **Step 1: Add the env flag**

In `app/config.py`, near `BIO_REWARD_ENABLED` (~line 375), add:

```python
    BIRTHDAY_BONUS_ENABLED: bool = False  # Master kill-switch for the birthday-bonus feature
```

- [ ] **Step 2: Wire set_bot + scheduler in main.py**

In `main.py`, in the `set_bot` block (~line 310, after `bio_reward_service.set_bot(bot)`), add:

```python
        from app.services.birthday_service import birthday_service
        birthday_service.set_bot(bot)
```
(Or add `from app.services.birthday_service import birthday_service` to the top-level imports next to `bio_reward_service` and call `birthday_service.set_bot(bot)` here — match the file's import style.)

In the scheduler-launch area, right after the Bio-reward scheduler stage (~line 663), add:

```python
        async with timeline.stage(
            'Birthday scheduler',
            '🎂',
            success_message='Birthday scheduler запущен',
        ) as stage:
            if birthday_service.is_enabled():
                birthday_task = asyncio.create_task(birthday_service.start_monitoring())
                stage.log('Birthday-bonus активен')
            else:
                birthday_task = None
                stage.skip('BIRTHDAY_BONUS_ENABLED=False')
```

In the shutdown section (~line 889, where `bio_reward_task` is cancelled), add an analogous guard for `birthday_task`, mirroring the exact cancel pattern used for `bio_reward_task` (e.g. `if 'birthday_task' in locals() and birthday_task and not birthday_task.done(): birthday_task.cancel()`).

- [ ] **Step 3: Document in .env.example**

Add to `.env.example` near other feature flags:

```
# Birthday bonus: congratulate users on their Telegram-profile birthday and grant a configurable gift once a year.
BIRTHDAY_BONUS_ENABLED=false
```

- [ ] **Step 4: Verify config + run the full birthday test set**

Run: `.venv/Scripts/python.exe -c "import app.config; print(app.config.settings.BIRTHDAY_BONUS_ENABLED)"` → prints `False`.
Run: `.venv/Scripts/python.exe -m pytest tests/services/test_birthday_settings.py tests/services/test_birthday_service.py -v`
Expected: all PASS.

- [ ] **Step 5: Regression check**

Run: `.venv/Scripts/python.exe -m pytest tests/services/ -q`
Expected: no NEW failures vs baseline (~29 pre-existing acceptable; confirm none in birthday files).

- [ ] **Step 6: Commit**

```bash
git add app/config.py main.py .env.example
git commit -m "feat(birthday): config flag + scheduler wiring"
```

---

## Self-Review Checklist (controller runs before final review)

- [ ] Migration 0096 single head, down_revision 0095, reversible.
- [ ] Model fields match migration column names exactly.
- [ ] Sync is fire-and-forget, swallows errors, throttled 30d, never overwrites known birth_date with None.
- [ ] Grant anti-abuse: year-once + account-age + dob-stable all enforced; idempotent per year.
- [ ] 29 Feb → 28 Feb fallback in non-leap years.
- [ ] All gated by `BIRTHDAY_BONUS_ENABLED` (env) AND `BirthdaySettingsService.is_enabled()` (default OFF both).
- [ ] `add_user_balance` used for balance (it creates the transaction itself — no double transaction).
- [ ] subscription_days fallback honored (balance or skip-with-congrats).
- [ ] Scheduler registered in main.py with start + shutdown-cancel.

## Out of plan scope (follow-ups)

- Real personal promocode minting for `reward_type=promocode` (currently credits balance-equivalent; replace with promocode CRUD when wiring is confirmed). FLAGGED — note in final report.
- Localizing birthday congratulation text across locale files (inline RU default for now).
- Per-user timezone for "today" (uses project timezone).
- Cabinet UI for viewing/clearing stored birth_date.
