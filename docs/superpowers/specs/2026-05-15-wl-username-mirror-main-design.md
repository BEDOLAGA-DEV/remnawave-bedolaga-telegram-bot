# WL username должен зеркалить main user — design

**Date:** 2026-05-15
**Scope:** RemnaWave WL-аккаунты (panel)
**Status:** Draft

## Проблема

Пользовательский main-аккаунт на панели и его WL-двойник должны иметь имена
вида `<main>` и `<main>_wl`. После смены `REMNAWAVE_USER_USERNAME_TEMPLATE` с
`user_{telegram_id}` на `u_{telegram_id}_{sub_id}` сложилась рассинхронизация:

- Main-аккаунт остаётся `user_<tg>` (бот находит и адаптирует его через
  legacy-fallback в `create_remnawave_user`, чтобы не плодить дубликаты).
- WL-имя строится отдельно через `_build_wl_username`, которое вызывает
  `format_remnawave_username(...)` для текущего template'а, получает base
  `u_<tg>` (placeholder `{sub_id}` подставляется пустой строкой через
  `defaultdict(str, ...)`) и формирует `primary_wl = u_<tg>_<sub_id>_wl`.

В результате:

1. При первом sync после смены template'а primary_wl не найден → код тянется
   к `legacy_wl` и `user_<tg>_wl`-fallback, адаптирует старый WL — этот путь
   работает.
2. Если хоть раз бот успел создать `u_<tg>_<sub_id>_wl` (например, в окне
   между сменой template'а и патчем 5fbb95e9), последующие sync находят его
   через primary_wl-lookup, а `user_<tg>_wl` остаётся orphan'ом — никто его
   не продлевает, не сбрасывает трафик, и он висит на панели вечно.

Корневая причина — WL-имя строится из template'а, а не из реального
username main-аккаунта на панели.

## Решение

WL primary всегда производный от username main-аккаунта **на панели после
adoption/create**: `primary_wl = <main_username>_wl` (с учётом 36-char лимита
RemnaWave). Дополнительно: при sync смотреть, не существует ли «другого»
формата WL, и удалять дубликат, чтобы прежние orphans вычистились.

### Компонент 1: WL имя из реального main_username

`_ensure_wl_user_synced` получает новый обязательный параметр
`main_username: str` — имя, под которым main-аккаунт сейчас живёт на панели
(то, что вернул `api.update_user` / `api.create_user` после adoption или
создания). Внутри:

```python
truncated = main_username[:33].rstrip('_-')
primary_wl = f'{truncated}_wl'
```

`legacy_wl` (base + `_wl` из template'а) и hardcoded `user_<tg>_wl`-fallback
из текущей реализации удаляются — они становятся не нужны, потому что
primary_wl уже равно `<main_username>_wl` и автоматически совпадает с тем,
что должно быть на панели.

`_build_wl_username` удаляется как мёртвый код (его единственный потребитель
переходит на новую логику).

### Компонент 2: Callers передают main_username

Места вызова `_ensure_wl_user_synced`:

1. `subscription_service.py:238` — внутри `create_remnawave_user`. Передаём
   `created_or_updated_main.username` (объект `RemnaWaveUserResponse`,
   возвращённый `api.create_user` / `api.update_user` / адаптированный
   legacy_user).
2. `subscription_service.py:608` — внутри `_create_single_subscription_user`.
   Аналогично.

Подпись `_ensure_wl_user_synced` остаётся kwargs-friendly:

```python
async def _ensure_wl_user_synced(
    self,
    api: RemnaWaveAPI,
    user: User,
    subscription: Subscription,
    is_actually_active: bool,
    main_username: str,
    reset_traffic: bool = False,
    reset_reason: str | None = None,
) -> None:
```

### Компонент 3: Defensive cleanup дубликата

После того как primary_wl найден или создан, бот пытается найти «другую»
форму WL для этого telegram_id и удалить её, если она существует:

```python
# main_username = реальный username основного аккаунта.
# Кандидаты — все известные форматы WL для этого пользователя.
candidates = {f'user_{user.telegram_id}_wl'}
for sub in await get_user_subscriptions(db, user.id):
    candidates.add(f'u_{user.telegram_id}_{sub.id}_wl')
    # template-based legacy
    base = settings.format_remnawave_username(
        full_name=user.full_name, username=user.username,
        telegram_id=user.telegram_id, email=user.email, user_id=user.id,
    )
    candidates.add(f'{base[:33]}_wl')

for candidate in candidates:
    if candidate == primary_wl:
        continue
    try:
        dup = await api.get_user_by_username(candidate)
    except RemnaWaveAPIError as err:
        if err.status_code == 404:
            continue
        raise
    if dup and dup.uuid != primary_uuid:
        logger.warning(
            '🧹 Удаляю дублирующий WL аккаунт',
            duplicate=candidate, primary=primary_wl,
            duplicate_uuid=dup.uuid,
        )
        try:
            await api.delete_user(dup.uuid)
        except Exception as delete_err:
            logger.warning(
                '⚠️ Не удалось удалить дубликат WL',
                duplicate=candidate, error=delete_err,
            )
```

Аналогично для main — но **только если main_username сам совпадает с одним
из ожидаемых форматов и второй формы быть не должно**. Поскольку legacy
adoption main-аккаунта остаётся (мы НЕ удаляем `user_<tg>` если он
работает), main-cleanup в этом дизайне не предусмотрен (см. scope).

## Что НЕ входит

- Cleanup main-аккаунтов (отдельная тема — adoption main уже корректно).
- One-off migration script для прохода по всем юзерам. Defensive sync
  обработает каждого пользователя при ближайшем renewal/top-up/update.
  Если нужно ускорить — отдельный CLI-script, не входит в эту работу.
- Изменение `REMNAWAVE_USER_USERNAME_TEMPLATE`-парсера. WL-имя теперь
  независимо от template'а — оно следует за main.

## Архитектура

```
create_remnawave_user / _create_single_subscription_user
  ├── adopt/create main_account → returns RemnaWaveUserResponse
  ├── main_username = main_account.username
  └── _ensure_wl_user_synced(..., main_username=main_username)
        ├── primary_wl = f'{main_username[:33]}_wl'
        ├── lookup primary_wl
        ├── update if found, create if not
        └── cleanup_wl_duplicates(api, user, subscription, primary_wl, primary_uuid)
              ├── candidates = [user_<tg>_wl, u_<tg>_<sub_id>_wl, template-based]
              ├── для каждого кандидата ≠ primary_wl: get → delete
              └── log каждое удаление как warning
```

## Поток данных

1. Renewal/top-up/update triggers `create_remnawave_user(db, subscription)`.
2. Method разрешает main-аккаунт (existing UUID lookup → legacy fallback →
   create_new) и сохраняет ссылку на `RemnaWaveUserResponse`.
3. Method передаёт `main_username` в `_ensure_wl_user_synced`.
4. WL sync ищет `<main_username>_wl`, обновляет или создаёт.
5. Cleanup проходится по альтернативным WL-именам, удаляет дубликаты.
6. Логи фиксируют каждое adoption/cleanup для аудита.

## Обработка ошибок

- `api.delete_user(dup.uuid)` падает → ловим, логируем как warning. WL
  primary уже жив и работает — не критично, повторим в следующий sync.
- `api.get_user_by_username(candidate)` 404 → expected, continue.
- Любой другой `RemnaWaveAPIError` при lookup → re-raise (transient —
  обработается outer try/except, который уже есть в
  `_ensure_wl_user_synced`).
- `main_username` пустой/None → используем существующее поведение через
  `_build_wl_username` как fallback. Не должно случаться при нормальном
  flow, защита от регрессии.

## Тестирование

- `tests/services/test_subscription_service.py` (новый файл или дополнение
  существующего): юнит-тесты `_ensure_wl_user_synced`:
  - main_username `user_123` → primary_wl `user_123_wl`, lookup
    `u_123_42_wl` возвращает existing, delete вызван с его uuid.
  - main_username `u_123_42` → primary_wl `u_123_42_wl`, lookup
    `user_123_wl` возвращает existing, delete вызван с его uuid.
  - main_username `user_123`, нет дубликатов → delete не вызван.
  - main_username очень длинный (>33 char) — обрезается до 33 + `_wl`.
  - `api.delete_user` бросает — основной flow не падает, warning в логах.
- Integration smoke: real renewal flow с мок RemnaWaveAPI, проверка что
  после renewal на панели только один WL и он привязан к main.

## Rollback

Изменения локализованы в `app/services/subscription_service.py` + тесты.
Если что-то ломается:
- `git revert <commit>` откатит дизайн целиком.
- Backup-ветка `backup-before-wl-rename` создаётся перед изменениями
  (`git branch backup-before-wl-rename master`).

## Open questions

Нет. План одобрен пользователем: Q1=A (scope WL only), Q3=A (changes
1+2+3 — derive + cleanup duplicates).
