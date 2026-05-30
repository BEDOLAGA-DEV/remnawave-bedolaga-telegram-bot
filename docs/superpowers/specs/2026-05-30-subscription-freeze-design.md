# Заморозка подписки (vacation freeze) — design

**Date:** 2026-05-30
**Scope:** обычные (non-daily) подписки: freeze/resume + панель + scheduler auto-resume + admin-настройки + точки входа бот/кабинет
**Status:** Draft
**Feature:** B3 (#4 в pipeline)

## Проблема

Пользователь, который временно не пользуется VPN (отпуск, командировка),
вынужден либо платить за простой, либо потерять оплаченное время при
истечении. Нет способа «заморозить» подписку — приостановить отсчёт
оставшегося времени и отключить доступ на панели, потом возобновить без
потерь.

Для **суточных** тарифов пауза уже есть (`is_daily_paused` + endpoint
`/subscription/pause`). Для **обычных** (период-based) подписок —
механизма нет.

## Решение

Заморозка обычной подписки: сохранить оставшееся время, отключить
пользователя на RemnaWave-панели (`disable_user`), исключить из expiry-
проверок. При разморозке (вручную или авто по дедлайну) — продлить
`end_date` на длительность заморозки (время сохранено), включить на панели
(`enable_user`). Антиабуз — настраивается админом. Точки входа: бот +
кабинет.

RemnaWave API уже имеет `enable_user(uuid)` / `disable_user(uuid)`
(`app/external/remnawave_api.py:646,651`).

### Компонент 1: миграция + поля Subscription (0097)

```python
op.add_column('subscriptions', sa.Column('frozen_at', sa.DateTime(timezone=True), nullable=True))
op.add_column('subscriptions', sa.Column('frozen_until', sa.DateTime(timezone=True), nullable=True))
op.add_column('subscriptions', sa.Column('freeze_days_used_year', sa.Integer(), nullable=False, server_default='0'))
op.add_column('subscriptions', sa.Column('freeze_year', sa.Integer(), nullable=True))
op.add_column('subscriptions', sa.Column('last_freeze_at', sa.DateTime(timezone=True), nullable=True))
```

Модель `Subscription` (`app/database/models.py`):

```python
frozen_at = Column(AwareDateTime(), nullable=True)        # non-null = заморожена
frozen_until = Column(AwareDateTime(), nullable=True)     # авто-resume дедлайн
freeze_days_used_year = Column(Integer, default=0, nullable=False, server_default='0')
freeze_year = Column(Integer, nullable=True)              # год, к которому относится счётчик
last_freeze_at = Column(AwareDateTime(), nullable=True)   # для кулдауна между заморозками
```

Флаг заморозки = `frozen_at IS NOT NULL` (без новой enum-value, по образцу
`is_daily_paused`).

### Компонент 2: `FreezeService` (новый сервис)

`app/services/freeze_service.py`. Чистая бизнес-логика + панель-sync.

`freeze_subscription(db, subscription, user) -> FreezeResult`:
1. Валидация (raise `FreezeError(code, message)` при отказе):
   - подписка `ACTIVE`, не trial, не daily (`tariff.is_daily` False).
   - не заморожена (`frozen_at is None`).
   - возраст подписки `>= min_subscription_age_days` (по `created_at`).
   - кулдаун: `last_freeze_at is None` или `now - last_freeze_at >= cooldown_days`.
   - годовая квота: `_remaining_year_quota(sub) > 0` (с ресетом счётчика
     если `freeze_year != current_year`).
2. `max_single = min(max_single_freeze_days, remaining_year_quota)`.
   Если `remaining_year_quota < min_freeze_days` → отказ (квота почти
   исчерпана, осмысленную заморозку не сделать).
3. `frozen_at = now`, `frozen_until = now + max_single days`.
4. Панель: `disable_user(main_uuid)` + WL-uuid (если есть). Ошибка панели
   → откат БД-изменений + `FreezeError('panel_error')` (не оставляем
   рассинхрон: в БД заморожено, на панели активно).
5. commit, notify.

`resume_subscription(db, subscription, user, *, reason='manual') -> FreezeResult`:
1. Если не заморожена → no-op / FreezeError('not_frozen').
2. `now_capped = min(now, frozen_until)` (не начисляем больше дедлайна).
   `actual = now_capped - frozen_at`.
3. `subscription.end_date += actual` (сохранили оставшееся время).
4. Ресет счётчика если `freeze_year != current_year`, затем
   `freeze_days_used_year += ceil(actual в днях)`; `freeze_year = year`.
5. `last_freeze_at = now`; `frozen_at = None`; `frozen_until = None`.
6. Панель: `enable_user(...)` main + WL. Ошибка панели → НЕ откатываем
   время (юзер заслужил), но enqueue в `remnawave_retry_queue` (как делает
   daily resume) + лог.
7. commit, notify.

Квота-хелпер:
```python
def _remaining_year_quota(sub, max_year_days, now) -> int:
    used = sub.freeze_days_used_year if sub.freeze_year == now.year else 0
    return max(0, max_year_days - used)
```

### Компонент 3: scheduler auto-resume

В `MonitoringService` новый метод `_check_frozen_subscriptions(db)`,
регистрируется в цикле мониторинга. Находит подписки `frozen_at IS NOT
NULL AND frozen_until <= now`, вызывает `FreezeService.resume_subscription(
..., reason='auto')`. Изолировано try/except на подписку.

Также: `check_and_update_subscription_status`
(`crud/subscription.py:1525`) — добавить early-skip
`if subscription.frozen_at is not None: return subscription` (рядом с
существующим `is_daily_paused`-skip), чтобы замороженная подписка не
экспайрилась.

Autopay/expiry/traffic-warn проверки тоже должны пропускать замороженные —
добавить фильтр `Subscription.frozen_at.is_(None)` в их выборки (или
ранний continue). Минимально — там, где это влияет (autopay списания,
expiring-уведомления).

### Компонент 4: admin-настройки

JSON-конфиг `FreezeSettingsService` (как `BirthdaySettingsService`):
- `enabled: bool` (дефолт False)
- `max_days_per_year: int` (дефолт 30)
- `min_subscription_age_days: int` (дефолт 7)
- `cooldown_days: int` (дефолт 7) — между заморозками
- `min_freeze_days: int` (дефолт 3)
- `max_single_freeze_days: int` (дефолт 30)

Геттеры/сеттеры/клампы + admin-UI (mirror birthday admin panel).
Мастер-флаг `settings.SUBSCRIPTION_FREEZE_ENABLED` (env, дефолт False) И
config.enabled.

### Компонент 5: точки входа

**Кабинет:** `POST /subscription/freeze` + `POST /subscription/resume`
в `app/cabinet/routes/subscription_modules/` (рядом с daily.py),
вызывают `FreezeService`. Маппинг `FreezeError.code` → HTTP 400/402/409.

**Бот:** кнопка «❄️ Заморозить / ▶️ Разморозить» в меню подписки
(`app/handlers/subscription/`), callback → `FreezeService`,
показывает оставшуюся квоту/дедлайн. Гейт по `SUBSCRIPTION_FREEZE_ENABLED`.

## Что НЕ входит

- Заморозка суточных тарифов (есть свой pause).
- Заморозка trial.
- Частичная заморозка отдельных серверов.
- Возврат денег за заморозку.
- Заморозка во время активного autopay-цикла с картой (autopay просто
  пропускает замороженные).

## Архитектура

```
freeze_service.FreezeService
  ├── freeze_subscription(db, sub, user)
  │     ├── validate (active/non-trial/non-daily/age/cooldown/quota)
  │     ├── frozen_at=now, frozen_until=now+max_single
  │     ├── api.disable_user(main + WL)   [rollback on panel error]
  │     └── commit + notify
  └── resume_subscription(db, sub, user, reason)
        ├── actual = min(now,frozen_until) - frozen_at
        ├── end_date += actual; freeze_days_used_year += actual.days
        ├── clear frozen_at/until; last_freeze_at=now
        ├── api.enable_user(main + WL)     [retry-queue on panel error]
        └── commit + notify

monitoring loop ── _check_frozen_subscriptions ── auto-resume past frozen_until
crud.check_and_update_subscription_status ── skip if frozen_at not None
FreezeSettingsService (JSON) ── admin UI toggles/edits
entry: cabinet POST /freeze /resume + bot ❄️ button
migration 0097: subscriptions += frozen_at, frozen_until, freeze_days_used_year, freeze_year, last_freeze_at
```

## Поток данных

1. Юзер жмёт «Заморозить» (бот/кабинет) → FreezeService валидирует квоту,
   ставит frozen_at/until, дизейблит на панели, шлёт подтверждение.
2. Замороженная подписка пропускается expiry/autopay/traffic-проверками.
3. Юзер жмёт «Разморозить» ИЛИ scheduler видит `frozen_until <= now` →
   end_date += длительность, счётчик++, enable на панели, нотиф.
4. Годовой счётчик сбрасывается при смене года.

## Обработка ошибок

- Панель-ошибка при freeze → откат БД (не оставляем «заморожено в БД /
  активно на панели»), FreezeError → юзер видит «попробуйте позже».
- Панель-ошибка при resume → время уже восстановлено (commit), enable
  в retry-queue (как daily resume). Юзер не теряет время.
- Двойной клик freeze → второй видит `frozen_at not None` → no-op/ошибка.
- Race auto-resume vs manual: оба идут через `resume_subscription`,
  который чекает `frozen_at is None` первым → идемпотентно.
- Квота на нуле → отказ с понятным сообщением (сколько дней осталось).

## Тестирование

Юнит (`tests/services/test_freeze_service.py`), мок db/api/config:
- freeze happy: active sub, квота есть → frozen_at/until set, disable_user
  вызван, commit.
- freeze отказ: trial / daily / уже заморожена / молодая подписка /
  кулдаун / квота 0 → FreezeError с нужным кодом, панель НЕ трогается.
- freeze panel error → БД-изменения откатаны, frozen_at остался None.
- resume happy: end_date += длительность, счётчик += дни, frozen_at
  очищен, enable_user вызван.
- resume capped at frozen_until: actual не больше дедлайна.
- resume year rollover: freeze_year != текущий → счётчик сброшен.
- resume panel error → время восстановлено, retry-queue enqueued.
- auto-resume: scheduler находит просроченные frozen_until → resume.
- квота-хелпер: used учитывается только если freeze_year == текущий.
- settings: дефолты OFF, клампы, валидация.

## Rollback

- За флагом `SUBSCRIPTION_FREEZE_ENABLED` + config.enabled (дефолт OFF).
- Миграция 0097 обратима (drop_column ×5).
- `git revert` + `alembic downgrade 0096`.

## Open questions

Нет. Лимиты — админ-конфиг (подтверждено). Точки входа — бот + кабинет
(подтверждено). Модель сохранения времени через end_date += длительность.
