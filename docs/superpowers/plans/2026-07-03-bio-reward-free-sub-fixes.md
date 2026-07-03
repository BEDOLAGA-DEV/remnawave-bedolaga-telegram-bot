# BIO-Reward Free Sub Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать создание панельного `_wl`-аккаунта для бесплатной BIO-подписки и исправить 5 багов жизненного цикла (revoke/extend конвертированной платной подписки, отсутствие panel-push, transient fetch-fail).

**Architecture:** Точечные guard'ы в `SubscriptionService._ensure_wl_user_synced` (по `is_bio_reward`), в `BioRewardService._extend_free_sub`/`_revoke`/`check_user`, плюс отвязка `participant.free_subscription_id` при конверсии bio→paid в purchase-сервисе. Panel-push через существующий `update_remnawave_user`. Без изменений схемы БД, без миграций.

**Tech Stack:** Python 3.13 (venv `.venv\Scripts\python.exe`), SQLAlchemy async, aiogram, pytest (async-тесты работают без маркеров через хук в `tests/conftest.py`).

**Спек:** `docs/superpowers/specs/2026-07-03-bio-reward-free-sub-fixes-design.md`
**Ветка:** `feat/bio-reward-free-sub-fixes` (уже создана от master)

**Важные особенности репо:**
- `docs/` в .gitignore — файлы плана/спеки добавлять через `git add -f`.
- Полный `pytest tests/` имеет pre-existing ошибки коллекции — гонять только целевые файлы.
- Тесты пишутся как голые `async def` — conftest сам запускает их в event loop.

---

## Task 0: Тестовый каркас (фабрики + фейки)

**Files:**
- Create: `tests/test_bio_reward_lifecycle.py`

- [ ] **Step 1: Создать файл с фабриками и фейками (тестов пока нет)**

```python
"""Lifecycle tests for bio-reward fixes.

Covers: _wl panel guard for bio subs, extend/revoke guards against
converted-to-paid rows, panel push on extend/revoke, transient
fetch-failure handling in check_user.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace


class FakeDb:
    """Minimal async-session stand-in: identity get() map + commit counter."""

    def __init__(self, get_map: dict | None = None):
        self._get_map = get_map or {}
        self.commits = 0

    async def get(self, model, pk):
        return self._get_map.get(pk)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass

    def add(self, obj):
        pass


class FakeApi:
    """Records RemnaWave API calls; `existing` maps username -> user obj."""

    def __init__(self, existing: dict | None = None):
        self.existing = existing or {}
        self.created: list[dict] = []
        self.updated: list[tuple] = []
        self.deleted: list[str] = []

    async def get_user_by_username(self, username):
        return self.existing.get(username)

    async def create_user(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(uuid=f'uuid-{len(self.created)}')

    async def update_user(self, uuid, **kwargs):
        self.updated.append((uuid, kwargs))
        return SimpleNamespace(uuid=uuid)

    async def delete_user(self, uuid):
        self.deleted.append(uuid)
        return True


def _sub(**over):
    base = dict(
        id=101,
        user_id=7,
        is_bio_reward=True,
        is_trial=True,
        status='active',
        start_date=datetime.now(UTC) - timedelta(days=1),
        end_date=datetime.now(UTC) + timedelta(days=2),
        wl_traffic_limit_gb=None,
        wl_traffic_used_gb=0.0,
        wl_purchased_traffic_gb=0,
        wl_traffic_reset_at=None,
        device_limit=1,
        tariff=None,
        tariff_id=None,
        frozen_at=None,
        connected_squads=[],
        remnawave_uuid=None,
        bio_reward_discount_percent=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _user(**over):
    base = dict(
        id=7,
        telegram_id=555,
        username='tg_user',
        full_name='Test User',
        email=None,
        remnawave_uuid='main-uuid',
        referral_code='refABC',
        balance_kopeks=0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _participant(**over):
    base = dict(
        id=1,
        user_id=7,
        status='active',
        bypass_check=False,
        bio_snapshot='',
        last_bio_seen_at=None,
        last_check_at=None,
        grace_started_at=None,
        cooldown_until=None,
        revoked_at=None,
        opted_in_at=None,
        free_subscription_id=None,
        user=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _bio_cfg(**over):
    base = dict(
        enabled=True,
        discount_percent=10,
        accepted_bio_strings=[],
        match_personal_referral_link=False,
        grace_period_hours=3,
        cooldown_hours=48,
        check_interval_minutes=60,
        free_sub_window_days=3,
        free_sub_squad_uuid=None,
        free_sub_traffic_gb_per_day=1,
        free_sub_device_limit=1,
        notify_on_activate=False,
        notify_on_grace=False,
        notify_on_revoke=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _patch_subscription_service(monkeypatch):
    """Replace SubscriptionService with a recorder; returns list of pushed subs."""
    import app.services.subscription_service as ss_module

    calls: list = []

    class FakeSvc:
        async def update_remnawave_user(
            self, db, sub, *, reset_traffic=False, reset_reason=None, sync_squads=False
        ):
            calls.append(sub)
            return SimpleNamespace(uuid='pushed')

    monkeypatch.setattr(ss_module, 'SubscriptionService', FakeSvc)
    return calls
```

- [ ] **Step 2: Проверить, что файл импортируется (коллекция без ошибок)**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py -v`
Expected: `no tests ran` / collected 0 items, без ошибок импорта.

- [ ] **Step 3: Commit**

```bash
git add tests/test_bio_reward_lifecycle.py
git commit -m "test: scaffolding for bio-reward lifecycle tests"
```

---

## Task 1: Fix 1 — не создавать `_wl` на панели для bio-подписки

**Files:**
- Modify: `app/services/subscription_service.py:856-861` (начало `_ensure_wl_user_synced`, сразу после `username_wl = primary_wl`)
- Test: `tests/test_bio_reward_lifecycle.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_bio_reward_lifecycle.py`:

```python
# ---------- Fix 1: _ensure_wl_user_synced bio guard ----------


async def test_wl_sync_skips_bio_sub_and_deletes_leftover():
    from app.services.subscription_service import SubscriptionService

    svc = SubscriptionService()
    api = FakeApi(existing={'u_555_wl': SimpleNamespace(uuid='wl-uuid')})
    await svc._ensure_wl_user_synced(
        api, _user(), _sub(is_bio_reward=True), True, main_username='u_555'
    )
    assert api.created == []
    assert api.updated == []
    assert api.deleted == ['wl-uuid']


async def test_wl_sync_skips_bio_sub_without_leftover():
    from app.services.subscription_service import SubscriptionService

    svc = SubscriptionService()
    api = FakeApi()
    await svc._ensure_wl_user_synced(
        api, _user(), _sub(is_bio_reward=True), True, main_username='u_555'
    )
    assert api.created == []
    assert api.updated == []
    assert api.deleted == []


async def test_wl_sync_still_creates_wl_for_paid_sub():
    from app.services.subscription_service import SubscriptionService

    svc = SubscriptionService()
    api = FakeApi()
    await svc._ensure_wl_user_synced(
        api,
        _user(),
        _sub(is_bio_reward=False, is_trial=False, wl_traffic_limit_gb=5),
        True,
        main_username='u_555',
    )
    assert len(api.created) == 1
    assert api.created[0]['username'] == 'u_555_wl'
    assert api.deleted == []
```

- [ ] **Step 2: Убедиться, что первые два падают**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py -v -k wl_sync`
Expected: `test_wl_sync_skips_bio_sub_and_deletes_leftover` FAIL (created == 1, deleted пуст), `test_wl_sync_skips_bio_sub_without_leftover` FAIL, `test_wl_sync_still_creates_wl_for_paid_sub` PASS.

- [ ] **Step 3: Реализация — guard в `_ensure_wl_user_synced`**

В `app/services/subscription_service.py`, в `_ensure_wl_user_synced`, сразу после строк:

```python
            primary_wl = self._derive_wl_username(main_username, user, subscription)
            username_wl = primary_wl
```

вставить (до формирования `description`/`wl_kwargs`):

```python
            # Bio-reward free sub: never provision a paired _wl account —
            # the free BIO promo carries no white-list traffic at all.
            # Delete a leftover created before this guard existed.
            # Paid/trial subs keep the unconditional _wl mirror below.
            if getattr(subscription, 'is_bio_reward', False):
                leftover = None
                try:
                    leftover = await api.get_user_by_username(username_wl)
                except RemnaWaveAPIError as lookup_err:
                    if lookup_err.status_code != 404:
                        raise
                if leftover and getattr(leftover, 'uuid', None):
                    logger.info(
                        '🧹 Удаляю _wl аккаунт bio-reward подписки',
                        username_wl=username_wl,
                        subscription_id=getattr(subscription, 'id', None),
                    )
                    await api.delete_user(leftover.uuid)
                return
```

`RemnaWaveAPIError` уже импортирован в модуле. Не-404 ошибки уходят в существующий внешний `except Exception` метода (лог + выход) — синк основного аккаунта не ломается.

- [ ] **Step 4: Тесты зелёные**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py -v -k wl_sync`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/subscription_service.py tests/test_bio_reward_lifecycle.py
git commit -m "fix(bio-reward): never create paired _wl panel account for bio free subs"
```

---

## Task 2: Fix 6 — transient fetch-fail не запускает grace/revoke

**Files:**
- Modify: `app/services/bio_reward_service.py:296-297` (`check_user`, после `bio = await self._fetch_bio(...)`)
- Modify: `app/handlers/bio_reward.py:206-227` (dict `answers` в `opt_in`), `app/handlers/bio_reward.py:241-242` (`recheck`)
- Test: `tests/test_bio_reward_lifecycle.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_bio_reward_lifecycle.py`:

```python
# ---------- Fix 6: transient fetch failure ----------


def _patch_get_config(monkeypatch, cfg):
    from app.database.crud import bio_reward as bio_crud_module

    async def fake_get_config(db):
        return cfg

    monkeypatch.setattr(bio_crud_module, 'get_config', fake_get_config)


async def test_check_user_fetch_failure_keeps_state(monkeypatch):
    from app.database.models import BioRewardStatus
    from app.services.bio_reward_service import BioRewardService

    _patch_get_config(monkeypatch, _bio_cfg())
    svc = BioRewardService()  # bot не установлен -> _fetch_bio вернёт None
    participant = _participant(status=BioRewardStatus.ACTIVE.value, bio_snapshot='keep-me')
    outcome = await svc.check_user(FakeDb(), participant, user=_user())
    assert outcome == 'fetch_failed'
    assert participant.status == BioRewardStatus.ACTIVE.value
    assert participant.bio_snapshot == 'keep-me'
    assert participant.grace_started_at is None


async def test_check_user_empty_bio_still_starts_grace(monkeypatch):
    from app.database.models import BioRewardStatus
    from app.services.bio_reward_service import BioRewardService

    _patch_get_config(monkeypatch, _bio_cfg())
    svc = BioRewardService()

    async def fake_fetch(telegram_id):
        return ''  # bio реально пуст — это НЕ ошибка запроса

    svc._fetch_bio = fake_fetch
    participant = _participant(status=BioRewardStatus.ACTIVE.value)
    outcome = await svc.check_user(FakeDb(), participant, user=_user())
    assert outcome == 'grace_started'
    assert participant.status == BioRewardStatus.GRACE.value


async def test_check_user_fetch_failure_with_bypass_still_matches(monkeypatch):
    from app.database.models import BioRewardStatus
    from app.services.bio_reward_service import BioRewardService

    _patch_get_config(monkeypatch, _bio_cfg())
    svc = BioRewardService()
    participant = _participant(
        status=BioRewardStatus.ACTIVE.value, bypass_check=True, free_subscription_id=None
    )
    outcome = await svc.check_user(FakeDb(), participant, user=_user())
    assert outcome == 'extended'
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py -v -k fetch_failure_keeps_state`
Expected: FAIL — outcome == 'grace_started', статус ушёл в GRACE.

(`empty_bio_still_starts_grace` и `bypass_still_matches` должны проходить уже сейчас — это регрессионная фиксация текущего поведения.)

- [ ] **Step 3: Реализация — guard в `check_user`**

В `app/services/bio_reward_service.py`, в `check_user`, заменить:

```python
        bio = await self._fetch_bio(user.telegram_id)
        participant.bio_snapshot = bio or ''
```

на:

```python
        bio = await self._fetch_bio(user.telegram_id)
        if bio is None and not participant.bypass_check:
            # Transient fetch failure (Telegram API error, flood limit,
            # network). NOT the same as "bio removed": leave the state
            # machine untouched and retry next tick. Only last_check_at
            # is persisted.
            await db.commit()
            return 'fetch_failed'
        participant.bio_snapshot = bio or ''
```

- [ ] **Step 4: Тексты для пользователя в обработчиках**

`app/handlers/bio_reward.py`, dict `answers` в `opt_in` — добавить ключ (рядом с `'noop'`):

```python
        'fetch_failed': '⚠️ Не удалось проверить профиль. Попробуйте позже',
```

Там же в `recheck` заменить:

```python
    outcome = await bio_reward_service.check_user(db, participant, user=db_user)
    await callback.answer(f'Проверено: {outcome}')
```

на:

```python
    outcome = await bio_reward_service.check_user(db, participant, user=db_user)
    if outcome == 'fetch_failed':
        await callback.answer('⚠️ Не удалось проверить профиль. Попробуйте позже', show_alert=True)
    else:
        await callback.answer(f'Проверено: {outcome}')
```

- [ ] **Step 5: Тесты зелёные**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py -v -k check_user`
Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/bio_reward_service.py app/handlers/bio_reward.py tests/test_bio_reward_lifecycle.py
git commit -m "fix(bio-reward): transient bio fetch failure no longer triggers grace/revoke"
```

---

## Task 3: Fix 3 + Fix 5 — guard и panel-push в `_extend_free_sub`

**Files:**
- Modify: `app/services/bio_reward_service.py:422-473` (весь `_extend_free_sub`)
- Test: `tests/test_bio_reward_lifecycle.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_bio_reward_lifecycle.py`:

```python
# ---------- Fix 3 + 5: _extend_free_sub ----------


async def test_extend_detaches_converted_paid_sub(monkeypatch):
    from app.services.bio_reward_service import BioRewardService

    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    sub = _sub(is_bio_reward=False, status='active', end_date=datetime.now(UTC) + timedelta(hours=5))
    old_end = sub.end_date
    participant = _participant(free_subscription_id=101, user=_user())
    await svc._extend_free_sub(FakeDb(get_map={101: sub}), participant, _bio_cfg())
    assert participant.free_subscription_id is None
    assert sub.end_date == old_end
    assert sub.status == 'active'
    assert calls == []


async def test_extend_pushes_end_date_to_panel(monkeypatch):
    from app.services.bio_reward_service import BioRewardService

    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    sub = _sub(is_bio_reward=True, end_date=datetime.now(UTC) + timedelta(days=1))
    participant = _participant(free_subscription_id=101, user=_user())
    await svc._extend_free_sub(FakeDb(get_map={101: sub}), participant, _bio_cfg(free_sub_window_days=3))
    assert sub.end_date > datetime.now(UTC) + timedelta(days=2)
    assert calls == [sub]


async def test_extend_no_push_when_nothing_changed(monkeypatch):
    from app.services.bio_reward_service import BioRewardService

    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    sub = _sub(is_bio_reward=True, end_date=datetime.now(UTC) + timedelta(days=10))
    participant = _participant(free_subscription_id=101, user=_user())
    await svc._extend_free_sub(FakeDb(get_map={101: sub}), participant, _bio_cfg(free_sub_window_days=3))
    assert calls == []
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py -v -k extend`
Expected: `detaches_converted` FAIL (end_date продлён, ссылка не очищена), `pushes_end_date` FAIL (calls пуст — сейчас push только при WL-clear), `no_push_when_nothing_changed` PASS.

- [ ] **Step 3: Реализация — переписать `_extend_free_sub`**

Заменить весь метод `_extend_free_sub` в `app/services/bio_reward_service.py` на:

```python
    async def _extend_free_sub(
        self, db: AsyncSession, participant: BioRewardParticipant, cfg: BioRewardConfig
    ) -> None:
        if not participant.free_subscription_id:
            return
        sub = await db.get(Subscription, participant.free_subscription_id)
        if sub is None:
            return
        if not sub.is_bio_reward:
            # Row was converted to a paid subscription (purchase flow clears
            # the marker). Detach so the scheduler never extends or
            # reactivates someone's paid sub.
            participant.free_subscription_id = None
            await db.commit()
            return
        new_end = datetime.now(UTC) + timedelta(days=cfg.free_sub_window_days)
        end_moved = False
        wl_cleared = False
        if sub.end_date is None or sub.end_date < new_end:
            sub.end_date = new_end
            sub.status = SubscriptionStatus.ACTIVE.value
            end_moved = True
        # Self-heal: bio-reward subs must never carry WL traffic. Older rows
        # (before forward fix in _create_free_sub) inherited the model default
        # of 5 GB; normalise here so next tick converges them.
        if sub.wl_traffic_limit_gb is not None:
            sub.wl_traffic_limit_gb = None
            sub.wl_traffic_used_gb = 0.0
            sub.wl_purchased_traffic_gb = 0
            sub.wl_traffic_reset_at = None
            wl_cleared = True
        if end_moved or wl_cleared:
            await db.commit()
        else:
            return

        # Push to Remnawave: panel sync is authoritative — an un-pushed local
        # extension gets reverted and the panel account expires at the
        # original creation+window. One call per participant per tick.
        try:
            from app.services.subscription_service import SubscriptionService

            user = participant.user
            if user is not None and getattr(user, 'remnawave_uuid', None):
                svc = SubscriptionService()
                await svc.update_remnawave_user(
                    db, sub, reset_traffic=False, sync_squads=False
                )
                logger.info(
                    'bio_reward.free_sub.remnawave_synced',
                    subscription_id=sub.id,
                    end_moved=end_moved,
                    wl_cleared=wl_cleared,
                )
        except Exception as exc:
            logger.warning(
                'bio_reward.free_sub.remnawave_sync_failed',
                subscription_id=sub.id,
                err=str(exc),
            )
            # Best-effort; next scheduler tick will retry via the same path.
```

Прежний guard `if sub.is_bio_reward and ...` у WL-clear упрощён до `if sub.wl_traffic_limit_gb is not None` — не-bio строки уже отсечены ранним выходом.

- [ ] **Step 4: Тесты зелёные**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py -v -k extend`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/bio_reward_service.py tests/test_bio_reward_lifecycle.py
git commit -m "fix(bio-reward): guard _extend_free_sub against converted paid subs, push end_date to panel"
```

---

## Task 4: Fix 4 — guard + panel-push в `_revoke`

**Files:**
- Modify: `app/services/bio_reward_service.py:496-505` (начало `_revoke`)
- Test: `tests/test_bio_reward_lifecycle.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_bio_reward_lifecycle.py`:

```python
# ---------- Fix 4: _revoke ----------


def _patch_no_active_paid(monkeypatch):
    import app.database.crud.subscription as sub_crud

    async def no_paid(db, user_id):
        return []

    monkeypatch.setattr(sub_crud, 'get_active_subscriptions_by_user_id', no_paid)


async def test_revoke_does_not_disable_converted_paid_sub(monkeypatch):
    from app.services.bio_reward_service import BioRewardService

    _patch_no_active_paid(monkeypatch)
    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    sub = _sub(is_bio_reward=False, status='active')
    participant = _participant(free_subscription_id=101)
    await svc._revoke(FakeDb(get_map={101: sub}), participant, _user(), _bio_cfg())
    assert sub.status == 'active'
    assert participant.free_subscription_id is None
    assert calls == []


async def test_revoke_disables_bio_sub_and_pushes(monkeypatch):
    from app.database.models import SubscriptionStatus
    from app.services.bio_reward_service import BioRewardService

    _patch_no_active_paid(monkeypatch)
    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    sub = _sub(is_bio_reward=True, status='active')
    participant = _participant(free_subscription_id=101)
    await svc._revoke(FakeDb(get_map={101: sub}), participant, _user(), _bio_cfg())
    assert sub.status == SubscriptionStatus.DISABLED.value
    assert calls == [sub]
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py -v -k revoke`
Expected: `does_not_disable_converted` FAIL (платная отключена), `disables_bio_sub_and_pushes` FAIL (calls пуст).

- [ ] **Step 3: Реализация — начало `_revoke`**

В `app/services/bio_reward_service.py` заменить:

```python
        now = datetime.now(UTC)
        if participant.free_subscription_id:
            sub = await db.get(Subscription, participant.free_subscription_id)
            if sub is not None and sub.status != SubscriptionStatus.DISABLED.value:
                sub.status = SubscriptionStatus.DISABLED.value
                sub.end_date = now
                await db.commit()
```

на:

```python
        now = datetime.now(UTC)
        if participant.free_subscription_id:
            sub = await db.get(Subscription, participant.free_subscription_id)
            if sub is not None and not sub.is_bio_reward:
                # Converted to paid by the purchase flow — never disable
                # someone's paid subscription on bio revoke.
                participant.free_subscription_id = None
                sub = None
            if sub is not None and sub.status != SubscriptionStatus.DISABLED.value:
                sub.status = SubscriptionStatus.DISABLED.value
                sub.end_date = now
                await db.commit()
                # Push to Remnawave: without this the user keeps VPN access
                # until the stale panel expire_at, and the bidirectional
                # panel sync can revert the local end_date.
                try:
                    from app.services.subscription_service import SubscriptionService

                    if getattr(user, 'remnawave_uuid', None):
                        svc = SubscriptionService()
                        await svc.update_remnawave_user(
                            db, sub, reset_traffic=False, sync_squads=False
                        )
                except Exception as exc:
                    logger.warning(
                        'bio_reward.revoke.remnawave_sync_failed',
                        subscription_id=sub.id,
                        err=str(exc),
                    )
```

Очистка `participant.free_subscription_id` коммитится дальше по коду существующим `bio_crud.set_status(...)` (COOLDOWN).

- [ ] **Step 4: Тесты зелёные**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py -v -k revoke`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/bio_reward_service.py tests/test_bio_reward_lifecycle.py
git commit -m "fix(bio-reward): revoke must not disable converted paid sub; push disable to panel"
```

---

## Task 5: Fix 2 — конверсия bio→paid отвязывает free sub

**Files:**
- Modify: `app/services/subscription_purchase_service.py:1097-1101`

Прямого юнит-теста нет: метод покупки монолитен, а поведение защищено guard'ами Task 3/4 (даже если отвязка не сработает, extend/revoke больше не тронут конвертированную подписку). Отвязка — гигиена данных + чистый UI участника.

- [ ] **Step 1: Реализация**

В `app/services/subscription_purchase_service.py` заменить:

```python
            subscription.is_trial = False
            # Bio-reward sub is being converted to paid: clear marker so
            # Remnawave tag flips from FREE to PAID and status_display follows.
            if getattr(subscription, 'is_bio_reward', False):
                subscription.is_bio_reward = False
```

на:

```python
            subscription.is_trial = False
            # Bio-reward sub is being converted to paid: clear marker so
            # Remnawave tag flips from FREE to PAID and status_display follows.
            if getattr(subscription, 'is_bio_reward', False):
                subscription.is_bio_reward = False
                # Detach from the bio participant: from now on this row is a
                # paid sub — the bio scheduler must not extend or revoke it.
                try:
                    from app.database.crud import bio_reward as bio_crud

                    _participant = await bio_crud.get_participant_by_user_id(db, user.id)
                    if (
                        _participant is not None
                        and _participant.free_subscription_id == subscription.id
                    ):
                        _participant.free_subscription_id = None
                except Exception as _detach_err:  # pragma: no cover - defensive
                    logger.warning(
                        'bio_reward.detach_failed', user_id=user.id, err=str(_detach_err)
                    )
```

Изменение попадает в существующий `await db.commit()` этого же блока (~строка 1144).

- [ ] **Step 2: Регрессия — все bio-тесты зелёные**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py tests\test_bio_reward.py tests\test_bio_reward_analytics.py -v`
Expected: все PASS.

- [ ] **Step 3: Commit**

```bash
git add app/services/subscription_purchase_service.py
git commit -m "fix(bio-reward): detach participant.free_subscription_id on bio->paid conversion"
```

---

## Task 6: Финальная верификация

**Files:** нет новых.

- [ ] **Step 1: Полный прогон bio-тестов + смоук соседних**

Run: `.venv\Scripts\python.exe -m pytest tests\test_bio_reward_lifecycle.py tests\test_bio_reward.py tests\test_bio_reward_analytics.py -v`
Expected: все PASS, 0 failed.

- [ ] **Step 2: Импорт-смоук изменённых модулей**

Run: `.venv\Scripts\python.exe -c "import app.services.bio_reward_service, app.services.subscription_service, app.services.subscription_purchase_service, app.handlers.bio_reward; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Коммит плана**

```bash
git add -f docs/superpowers/plans/2026-07-03-bio-reward-free-sub-fixes.md
git commit -m "docs: implementation plan for bio-reward free sub fixes"
```
