# Birthday-бонус — design

**Date:** 2026-05-30
**Scope:** новый `birthday_service` + миграция User-полей + middleware-sync + admin-настройки
**Status:** Draft
**Feature:** B6 (#3 в pipeline)

## Проблема

Нет механизма поздравления с ДР и подарка. ДР пользователя доступен из
Telegram (`ChatFullInfo.birthdate` через `bot.get_chat(user_id)`), но
сейчас не собирается и не используется. Подарок на ДР — дешёвый goodwill,
повышает удержание и лояльность.

Ограничения Telegram:
- `birthdate` приходит ТОЛЬКО если пользователь задал ДР в профиле И
  приватность пускает бота. Иначе `None`.
- `get_chat` — отдельный API-вызов, нельзя дёргать на всех ежедневно
  (флуд-лимиты).
- Пользователь может сменить ДР в профиле TG на сегодня → попытка абуза.

## Решение

1. **Сбор ДР** — оппортунистический sync в `AuthMiddleware`: при
   интеракции, если ДР ещё не синкали или синк устарел (>30 дней), fire-
   and-forget `get_chat` (по образцу существующего
   `_refresh_remnawave_description`), сохраняем `birth_date`. API не
   дёргается чаще раза в 30 дней на активного юзера.
2. **Грант** — ежедневный scheduler (`BirthdayService`, структурно как
   `bio_reward_service`): читает из БД (без API) пользователей, у кого
   сегодня ДР и кто прошёл антиабуз, выдаёт подарок (тип настраивается
   админом), шлёт поздравление, ставит `last_birthday_reward_year`.
3. **Антиабуз** — три замка: аккаунт старше N дней, ДР стабилен >N дней,
   подарок 1 раз в год.

Источник ДР — **только Telegram** (без ввода в боте). Тип награды —
**настраивается админом** (баланс / дни подписки / промокод).

### Компонент 1: миграция + поля User (0096)

Новая миграция `migrations/alembic/versions/0096_add_birthday_fields.py`
(down_revision `0095`):

```python
op.add_column('users', sa.Column('birth_date', sa.Date(), nullable=True))
op.add_column('users', sa.Column('birthday_synced_at', sa.DateTime(timezone=True), nullable=True))
op.add_column('users', sa.Column('birthday_changed_at', sa.DateTime(timezone=True), nullable=True))
op.add_column('users', sa.Column('last_birthday_reward_year', sa.Integer(), nullable=True))
```

Те же поля в `app/database/models.py` (class User):

```python
birth_date = Column(Date(), nullable=True)
birthday_synced_at = Column(AwareDateTime(), nullable=True)
birthday_changed_at = Column(AwareDateTime(), nullable=True)
last_birthday_reward_year = Column(Integer(), nullable=True)
```

`birth_date` — только месяц/день важны для триггера; год храним как есть
(может быть None в TG `Birthdate.year`). При year=None пишем sentinel-год
(напр. 1900), сравнение всегда по (month, day).

### Компонент 2: оппортунистический sync (middleware)

В `AuthMiddleware.__call__`, в блоке обновления профиля (рядом с
username/first_name, ~стр. 197), после `last_activity`:

```python
if settings.BIRTHDAY_BONUS_ENABLED and _should_sync_birthday(db_user):
    asyncio.create_task(_sync_birthday(db_user.id, db_user.telegram_id))
```

`_should_sync_birthday`: `birthday_synced_at is None` или старше 30 дней.

`_sync_birthday(user_id, telegram_id)` (в `birthday_service`, своя
db-сессия, как `_refresh_remnawave_description`):
1. `chat = await bot.get_chat(telegram_id)`.
2. `bd = chat.birthdate` (aiogram `Birthdate`: `.day/.month/.year`).
3. если `bd` None → ставим только `birthday_synced_at=now`, выходим
   (не перезатираем уже известный `birth_date`).
4. собираем `new_date = date(bd.year or 1900, bd.month, bd.day)`.
5. если `new_date != user.birth_date` → пишем `birth_date=new_date` +
   `birthday_changed_at=now` (фиксируем смену для антиабуза).
6. `birthday_synced_at=now`, commit.

Bot инстанс — birthday_service хранит ссылку на bot (как bio_reward
`self._bot`), сетится при старте в `app/bot.py`.

### Компонент 3: ежедневный грант (scheduler)

`BirthdayService.start_monitoring()` — loop с суточным тиком (по образцу
bio_reward, но проверяем раз в день: спим до следующего прогона; tick
делает идемпотентную работу, так что точное время не критично):

```
while running:
    if BIRTHDAY_BONUS_ENABLED and config.enabled:
        await self._grant_birthday_rewards(db)
    sleep(check_interval)  # напр. 3600с; грант идемпотентен по году
```

`_grant_birthday_rewards(db)`:
1. `today = date.today()` (с учётом таймзоны проекта).
2. Выбрать users где `birth_date IS NOT NULL`, `extract(month)=today.month`,
   `extract(day)=today.day`, `status=ACTIVE`. (29 фев → если сегодня 28 фев
   и год невисокосный, дополнительно матчим birth_date (02,29) — отдельным
   условием.)
3. Для каждого — антиабуз:
   - `last_birthday_reward_year == today.year` → skip (уже дарили).
   - `created_at > now - min_account_age_days` → skip (аккаунт молод).
   - `birthday_changed_at` задан И `> now - dob_stable_days` → skip (ДР
     недавно сменён/установлен).
4. Выдать подарок по `config.reward_type`:
   - `balance` → `add_user_balance(db, user, amount_kopeks)` +
     `create_transaction(...)`.
   - `subscription_days` → продлить активную подписку на N дней (если есть
     активная; если нет — fallback на balance ИЛИ skip — см. ниже).
   - `promocode` → сгенерировать персональный промокод (% скидка,
     лимит 1, срок N дней) — реюз promocode CRUD.
5. `last_birthday_reward_year = today.year`, commit.
6. Поздравление в TG (`_notify`, как bio_reward): «🎂 С днём рождения!
   Дарим …».

### Компонент 4: admin-настройки

JSON-settings (как `NotificationSettingsService`, без config-таблицы).
Новый ключ `birthday_bonus` или поля в существующем settings-сервисе:
- `enabled: bool` (дефолт False)
- `reward_type: str` ('balance' | 'subscription_days' | 'promocode')
- `reward_amount: int` (kopeks для balance / дни для subscription_days /
  процент для promocode)
- `promocode_valid_days: int` (для типа promocode)
- `min_account_age_days: int` (дефолт 7)
- `dob_stable_days: int` (дефолт 7)
- `subscription_days_fallback: str` ('balance' | 'skip') — что делать,
  если reward_type=subscription_days, а активной подписки нет.

Геттеры/сеттеры + admin-UI (toggle + numeric/choice editors), по образцу
churn-save/wave-контролов в `app/handlers/admin/`.

`settings.BIRTHDAY_BONUS_ENABLED` (env, дефолт False) — мастер-выключатель,
И-условие с `config.enabled` (как `BIO_REWARD_ENABLED`).

## Что НЕ входит

- Ввод ДР в боте вручную (источник только TG-профиль).
- Кабинет-UI для ДР.
- Поздравления без подарка / кастомные шаблоны на каждый тип.
- Ретроактивная выдача за прошлые ДР.
- Часовые пояса per-user (используем таймзону проекта для «сегодня»).

## Архитектура

```
AuthMiddleware (per interaction)
  └── if enabled & stale: create_task(_sync_birthday)  ← get_chat, store birth_date

BirthdayService.start_monitoring (daily tick)  [registered in app/bot.py]
  └── _grant_birthday_rewards(db)
        ├── select users: birth_date month/day == today, ACTIVE
        ├── anti-abuse: year-once / account-age / dob-stable
        ├── grant by config.reward_type (balance|days|promocode)
        ├── set last_birthday_reward_year
        └── _notify(🎂 поздравление)

NotificationSettings-like JSON config  ← admin UI toggles/edits
Migration 0096: users += birth_date, birthday_synced_at, birthday_changed_at, last_birthday_reward_year
```

## Поток данных

1. Юзер жмёт что-то → middleware видит stale birthday_synced_at → async
   get_chat → пишет birth_date/synced_at (+ changed_at если сменился).
2. Раз в сутки scheduler матчит сегодняшние ДР из БД.
3. Антиабуз отсекает молодые аккаунты, свежесменённые ДР, повторы за год.
4. Грант по типу + транзакция + год-метка + поздравление.

## Обработка ошибок

- `get_chat` падает / приватность / нет ДР → пишем только synced_at,
  birth_date не трогаем; не валим интеракцию (fire-and-forget, swallow).
- Грант одному юзеру падает → лог, continue (не валит весь прогон, как
  bio_reward).
- reward_type=subscription_days без активной подписки → по
  `subscription_days_fallback`: 'balance' дарит баланс; 'skip' ставит
  year-метку + шлёт поздравление БЕЗ подарка (чтобы не слать каждый тик).
- Идемпотентность: `last_birthday_reward_year == year` гарантирует один
  грант в год даже при многократных тиках в сутки.

## Тестирование

Юнит (`tests/services/test_birthday_service.py`), мок `db`/`bot`/config:
- sync: get_chat вернул birthdate → birth_date записан, synced_at set.
- sync: birthdate None → synced_at set, birth_date НЕ затёрт.
- sync: ДР сменился → birthday_changed_at обновлён.
- grant: сегодня ДР, все замки пройдены, reward=balance → баланс +N,
  транзакция, year-метка, поздравление.
- grant: уже дарили в этом году (year-метка) → skip.
- grant: аккаунт младше min_account_age_days → skip.
- grant: birthday_changed_at свежий (<dob_stable_days) → skip (антиабуз).
- grant: reward=subscription_days, есть активная подписка → +N дней.
- grant: reward=subscription_days, нет подписки, fallback=balance →
  баланс.
- 29 фев в невисокосный год матчится 28 фев.
- settings: дефолты (enabled False, min_age 7, stable 7), сеттеры/клампы.

## Rollback

- Фича за `BIRTHDAY_BONUS_ENABLED` (env) + `config.enabled` (дефолты
  False) — в проде молчит до включения.
- Миграция 0096 обратима (`downgrade` drop_column ×4).
- `git revert` + `alembic downgrade 0095` откатывают.

## Open questions

Нет. subscription_days-без-подписки решено через
`subscription_days_fallback`. Источник ДР (только TG) и тип награды
(админ-конфиг) подтверждены пользователем.
