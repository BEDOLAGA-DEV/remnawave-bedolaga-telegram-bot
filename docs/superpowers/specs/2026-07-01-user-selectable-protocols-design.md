# Дизайн: пользовательский выбор протоколов (internal-squads)

**Дата:** 2026-07-01
**Ветка:** fix/cryptolink-ensure-crypt5 (или новая feature-ветка)
**Статус:** утверждён к реализации

## Цель

Дать подписчику возможность самому выбирать в настройках подписки, какие
протоколы (internal-squads RemnaWave) у него активны. Сейчас у всех по
умолчанию один сквад — «основной», в котором есть все протоколы. Админ
разбивает основной на несколько мелких сквадов (каждый = подмножество
протоколов) на панели RemnaWave. Пользователь может выключить основной и
включить одну или несколько мелких альтернатив, либо оставить основной.

## Решения (из брейншторма)

| Вопрос | Решение |
|--------|---------|
| Плата | **Бесплатно.** Просто переключатель. Никаких списаний, транзакций, pro-rata. |
| Модель выбора | **Мульти-выбор, минимум 1** активный сквад. Любая комбинация. |
| Набор и дефолт | **Глобально, один набор** для всех. Один сквад помечен как основной (включён по умолчанию). |
| Сосуществование с платными «странами» | **Протоколы — единственный механизм** выбора сквадов. countries.py не используется. |
| Подход | **A** — переиспользовать `ServerSquad` + флаг `is_default`. |

## Что уже есть в коде (переиспользуем)

- `Subscription.connected_squads` — JSON-список UUID сквадов
  ([models.py:2252](../../../app/database/models.py)). Уходит в панель целиком как
  `activeInternalSquads`.
- `ServerSquad` — модель сквада с `squad_uuid`, `display_name`, `is_available`,
  `allowed_promo_groups`, `sort_order` ([models.py:3278](../../../app/database/models.py)).
- Пуш в панель: `update_user(active_internal_squads=[...])`
  ([remnawave_api.py:613](../../../app/external/remnawave_api.py)), обёрнут в
  `subscription_service.update_remnawave_user(db, subscription, sync_squads=True)`.
- Sync сквадов из панели: `sync_with_remnawave`
  ([crud/server_squad.py:310](../../../app/database/crud/server_squad.py)) — уже чистит
  удалённые сквады из `connected_squads`.
- Существующий платный экран выбора сквадов: [countries.py](../../../app/handlers/subscription/countries.py)
  и клавиатура `get_manage_countries_keyboard` ([inline.py:3223](../../../app/keyboards/inline.py)) —
  берём как образец, вырезаем всё про деньги.
- Клавиатура настроек подписки: `get_updated_subscription_settings_keyboard`
  ([inline.py:3705](../../../app/keyboards/inline.py)).
- Админ-экран сквадов: [admin/servers.py](../../../app/handlers/admin/servers.py).

## Архитектура

### 1. Модель данных

- Новый столбец `ServerSquad.is_default: Boolean, default=False, nullable=False`.
  Инвариант: **ровно один** сквад с `is_default=True` (или ноль, если админ ещё
  не назначил — тогда дефолт отсутствует).
- Миграция alembic: add column `is_default` со значением `False` для всех строк.
- Пул выбираемых протоколов = сквады с `is_available=True`, отфильтрованные по
  промогруппе пользователя (существующий `allowed_promo_groups`). Фильтр только
  для видимости, оплаты нет.

### 2. CRUD / хелперы (crud/server_squad.py)

- `get_default_protocol_squad_uuid(db) -> str | None` — UUID основного сквада
  (`is_default=True`), либо `None`.
- `set_default_server_squad(db, squad_id)` — ставит `is_default=True` на указанный,
  снимает у всех остальных (single-true, в одной транзакции).
- `resolve_effective_squads(subscription, default_uuid) -> list[str]` — возвращает
  `connected_squads`, а если он пуст — `[default_uuid]` (или `[]`, если основного
  нет). Чистая функция, без БД.

### 3. Админка (admin/servers.py)

- В карточке сквада: бейдж «⭐ Основной», если `is_default=True`.
- Кнопка «Сделать основным» с callback `admin_server_set_default_{id}` →
  вызывает `set_default_server_squad` → перерисовывает карточку.
- В списке сквадов: отметка основного рядом с именем.
- Больше ничего в админке не меняем (`is_available`-тумблер и правка имени уже есть).

### 4. Пользовательский экран «Протоколы»

- Новый хендлер `app/handlers/subscription/protocols.py`.
- Клавиатура `get_manage_protocols_keyboard(protocols, selected, current, language)`
  в inline.py — адаптация `get_manage_countries_keyboard` **без** цены, баланса,
  pro-rata, записей в `SubscriptionServer`.
- Кнопка «🧩 Протоколы» в `get_updated_subscription_settings_keyboard`,
  callback `nz!_subscription_protocols`. **Показывается независимо от `has_tariff`**
  (протоколы бесплатны и не зависят от тарифа).
- Поток экрана:
  1. Открытие: загрузить пул (available + промогруппа), отметить галками те, что в
     `connected_squads` (или основной, если список пуст — через
     `resolve_effective_squads`).
  2. Тап по сквадам — toggle в локальном выборе (state).
  3. «✅ Применить» — записать выбор.
- Валидация: **минимум 1** включён. Попытка снять последний → alert «нужен хотя бы
  один протокол» (паттерн [countries.py:365](../../../app/handlers/subscription/countries.py)).
- Применить:
  1. `subscription.connected_squads = selected`
  2. `await subscription_service.update_remnawave_user(db, subscription, sync_squads=True)`
  3. Уведомление пользователю об успехе.
  - **Бесплатно**: ни транзакций, ни списаний, ни `SubscriptionServer`.

### 5. Основной по умолчанию

- Логику покупки/trial **не трогаем** — новые подписки уже получают сквады при
  создании ([subscription_purchase_service.py:1298](../../../app/services/subscription_purchase_service.py) и др.).
- Защитный дефолт через `resolve_effective_squads`: если у подписки
  `connected_squads` пуст, активным считается основной. Применяется в экране
  протоколов и в местах пуша, без изменения флоу покупки.

### 6. Пуш в панель и edge cases

- Пуш только через существующий `update_remnawave_user(sync_squads=True)`.
- Сквад удалён из панели → существующий `sync_with_remnawave` чистит его из
  `connected_squads`. Если удалён основной → лог-warning в админ-нотификации.
- Промогруппы: если активный сквад стал невидим для пользователя — показываем его
  как включённый и разрешаем снять (но не выбрать заново).

### 7. Локализация

Ключи во все локали (`locales/ru,en,ua,fa,zh.json` и зеркало
`app/localization/locales/*`):
- `PROTOCOLS_BUTTON` — «🧩 Протоколы»
- `PROTOCOLS_SCREEN_TITLE` — заголовок/описание экрана
- `PROTOCOLS_APPLY_BUTTON` — «✅ Применить»
- `PROTOCOLS_MIN_ONE_ALERT` — «нужен хотя бы один протокол»
- `PROTOCOLS_UPDATED` — «протоколы обновлены»
- `PROTOCOL_DEFAULT_BADGE` — «⭐ Основной» (админка)

## Тестирование

- `resolve_effective_squads`: пустой список → `[main]`; непустой → без изменений;
  нет основного → `[]`.
- `set_default_server_squad`: единственный `is_default=True` после назначения.
- Экран протоколов: снятие последнего сквада → блок (min-1); Применить пишет
  `connected_squads` и вызывает пуш в панель (mock `update_remnawave_user`).
- Паттерны: `tests/handlers/`, `tests/services/`, `tests/database/crud/`.

## Вне скоупа (не трогаем)

- Платный [countries.py](../../../app/handlers/subscription/countries.py) — остаётся, но
  не используется. Не удаляем.
- Логика покупки, trial, переключения тарифов, цен, баланса.
- `SubscriptionServer` (аудит платных подключений).

## Критерий готовности

1. Админ помечает сквад основным; ровно один основной в БД.
2. У пользователя в настройках подписки есть «🧩 Протоколы».
3. Пользователь включает/выключает сквады (мин. 1), Применить пушит
   `activeInternalSquads` в панель.
4. Новая/trial подписка по умолчанию имеет активным основной.
5. Никаких списаний. Тесты зелёные.
