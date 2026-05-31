# Speedtest фаза 1 (client-side ping) — design

**Date:** 2026-05-31
**Scope:** subscriber-gated node-list endpoint + React speedtest-страница (client HTTP-ping) + инфра-требование `/ping` на узлах
**Status:** Draft
**Feature:** #6 в pipeline

## Проблема

Пользователь не знает, какой сервер быстрее лично для него. Нужен
инструмент: открыл кабинет → нажал «Проверить» → в реал-тайме измеряется
задержка от ЕГО устройства до каждого узла → видит лучший.

Ограничение браузера: сырой ICMP недоступен. Меряем **HTTP round-trip**
(`fetch` до `https://<node-ping-host>/ping` + `performance.now()`),
медиана из N сэмплов, keep-alive прогрет. Это HTTP-latency (включает
сеть + TLS-reuse), близко к «пингу», но не ICMP — честно лейблим как
«задержка».

## Решение

3 части:
1. **Backend**: subscriber-gated endpoint `GET /cabinet/subscription/nodes-latency-targets` отдаёт список узлов `{name, country_code, ping_host, is_online, users_online}` для измерения. Только авторизованный кабинет-юзер с активной/триал подпиской.
2. **Frontend**: React-страница `SpeedTest.tsx` — кнопка «Проверить», client-side замер RTT до каждого `ping_host`, сортировка, «лучший» badge, статус online/offline, флаг страны.
3. **Инфра (вне кода бота)**: на каждом узле — HTTPS `/ping`→204 c CORS. Документируется; реализуется оператором в node-setup.

### Компонент 1: ping-host резолвинг

Узел RemnaWave имеет `address` (`RemnaWaveNode.address` — обычно IP).
Пинговать по IP из браузера нельзя (mixed-content: кабинет HTTPS → нужен
валидный TLS, а серт по IP редкость). Поэтому ping-host — **DNS-имя с
валидным сертом**, не сырой IP.

Источник `ping_host` (по приоритету):
1. Админ-маппинг `node_uuid → ping_host` в JSON-настройках
   (`SpeedtestSettingsService`), если задан.
2. Дефолт-шаблон `settings.SPEEDTEST_PING_HOST_TEMPLATE` (напр.
   `'{node_name}.{base_domain}'`) — если узлы именуются предсказуемо.
3. Если ни маппинга, ни шаблона — узел **исключается** из выдачи (нечего
   пинговать валидно). Лог info.

Это разрывает связку «raw IP» и решает mixed-content: бэк отдаёт только
узлы с валидным ping-host.

### Компонент 2: backend endpoint

`app/cabinet/routes/subscription_modules/speedtest.py`:

```python
router = APIRouter()

@router.get('/nodes-latency-targets')
async def nodes_latency_targets(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    if not settings.SPEEDTEST_ENABLED:
        raise HTTPException(404, 'Speedtest disabled')
    # subscriber-gate: активная или триал подписка
    subs = await get_active_subscriptions_by_user_id(db, user.id)
    if not subs:
        raise HTTPException(403, 'Subscription required')
    targets = await speedtest_service.get_ping_targets()
    return {'targets': targets, 'samples': settings.get_speedtest_samples()}
```

`SpeedtestService.get_ping_targets()`:
- `nodes = await api.get_all_nodes()` (кеш ~60с — не дёргать панель на
  каждый клик).
- для каждого node: резолв `ping_host` (компонент 1); пропустить если нет.
- вернуть `[{name, country_code, ping_host, is_online, users_online}]`,
  отсортированный по `country_code`/`name`.
- НЕ отдаём сырой `address`/IP, порт, traffic — только то, что нужно для
  пинга + карточки.

Кеш узлов — простой in-memory TTL (как делают другие сервисы), чтобы
N кликов юзеров не множили запросы к панели.

### Компонент 3: React speedtest-страница

`bedolaga-cabinet/src/pages/SpeedTest.tsx` + `api/speedtest.ts`:

- `api/speedtest.ts`: `getTargets()` → `GET /cabinet/subscription/nodes-latency-targets`.
- Страница: кнопка «Проверить задержку». По клику:
  - грузит targets;
  - для каждого target: N раз (`samples`, дефолт 5) `fetch(https://{ping_host}/ping, {mode:'cors', cache:'no-store'})`, замер `performance.now()` до/после; первый сэмпл (TLS-handshake) отбрасываем, берём **медиану** остальных; таймаут на сэмпл (напр. 3с) через `AbortController`.
  - офлайн/ошибка/таймаут → карточка «недоступен».
  - результаты: цвет (зел <80мс / жёлт 80-150 / красн >150), ⚡«Лучший» (min медиана), флаг страны (`country_code`), online/offline.
  - измерения параллельно (Promise.all) с капом одновременных fetch (напр. 4) для скорости без перегруза.
- Реюз существующих UI-компонентов/стилей кабинета (карточки, как на странице серверов).
- Маршрут + пункт меню (рядом с Connection/Subscription).

### Компонент 4: конфиг

- `settings.SPEEDTEST_ENABLED: bool = False` (env мастер-флаг).
- `settings.SPEEDTEST_SAMPLES: int = 5` + геттер с клампом (3..10).
- `settings.SPEEDTEST_PING_HOST_TEMPLATE: str = ''` (опц. шаблон).
- `SpeedtestSettingsService` (JSON) для маппинга `node_uuid → ping_host`
  + toggle (admin-UI опц.; в v1 — маппинг через JSON-файл/env, admin-UI
  как follow-up). Кламп/валидация хоста (без схемы, без путей — только
  hostname).

### Компонент 5: инфра-требование (вне кода — документация)

Каждый узел, который должен пинговаться, обязан отдавать:
```
location = /ping {
    add_header Access-Control-Allow-Origin "https://<cabinet-domain>" always;
    add_header Access-Control-Allow-Methods "GET, OPTIONS" always;
    return 204;
}
```
+ валидный TLS-серт на `ping_host` (Let's Encrypt). Это часть node-setup
оператора. Документируется в `docs/` + README speedtest. БЕЗ этого узел не
пингуется (карточка «недоступен») — фича degrade-friendly, не падает.

## Что НЕ входит (фаза 1)

- Download/upload speed-тест (фаза 2 — тест-файл на узле, тяжелее).
- Джиттер, packet loss.
- Bot-UI speedtest (только кабинет — браузер нужен для client-ping).
- Авто-выбор/переключение сервера по результату (только показ + ручной
  выбор существующими средствами).
- Per-tariff lock-флаг узлов в выдаче (v1 показывает все доступные).
- Реальный ICMP (браузер не умеет).
- Автоматизация node-setup `/ping` (инфра-таска оператора).

## Архитектура

```
React SpeedTest.tsx ── getTargets() ──> GET /cabinet/subscription/nodes-latency-targets
  │                                         (subscriber-gated, SPEEDTEST_ENABLED)
  │                                          └── SpeedtestService.get_ping_targets()
  │                                                ├── api.get_all_nodes() [60s cache]
  │                                                ├── resolve ping_host (mapping/template; skip if none)
  │                                                └── [{name, country_code, ping_host, is_online, users_online}]
  └── on click: per target, N× fetch(https://ping_host/ping) → median RTT → sort → best
nodes (infra, operator): nginx /ping → 204 + CORS + valid TLS  [REQUIRED, outside bot code]
config: SPEEDTEST_ENABLED / SPEEDTEST_SAMPLES / SPEEDTEST_PING_HOST_TEMPLATE + SpeedtestSettingsService(JSON mapping)
```

## Поток данных

1. Юзер открывает страницу SpeedTest в кабинете → жмёт «Проверить».
2. Фронт грузит targets (бэк проверил подписку + флаг, отдал узлы с
   валидным ping_host).
3. Фронт для каждого узла меряет HTTP-RTT (медиана N, первый отброшен,
   таймаут), параллельно с капом.
4. Сортирует по задержке, помечает лучший, рисует карточки (флаг, online,
   цвет, мс).
5. Узлы без `/ping`/TLS → «недоступен» (не ломает остальные).

## Обработка ошибок

- Узел без ping_host → исключён из выдачи (бэк), не показывается.
- `/ping` недоступен / CORS-fail / таймаут → карточка «недоступен» на
  фронте; остальные узлы меряются нормально.
- Панель `get_all_nodes` падает → endpoint 503 + фронт показывает «не
  удалось загрузить список»; кеш смягчает.
- SPEEDTEST_ENABLED False → endpoint 404, пункт меню скрыт.
- Нет подписки → 403, дружелюбное сообщение «нужна подписка».
- Mixed-content предотвращён: отдаём только https-able ping_host.

## Тестирование

Backend юнит (`tests/.../test_speedtest_service.py` + route-тест):
- get_ping_targets: маппинг есть → ping_host из маппинга; шаблон → из
  шаблона; ни того ни другого → узел исключён.
- кеш: 2 вызова подряд → 1 запрос к панели (мок).
- endpoint: SPEEDTEST_ENABLED False → 404; нет подписки → 403; happy →
  список + samples.
- не отдаёт сырой address/IP в ответе.

Frontend (если в кабинете есть тест-харнес — vitest): медиана-расчёт
(отброс первого сэмпла, медиана), сортировка, best-маркер. Если харнеса
нет — вынести медиану/сортировку в чистую `utils/latency.ts` с unit-тестом;
UI — ручная проверка.

## Rollback

- За `SPEEDTEST_ENABLED` (env, дефолт False) — endpoint 404, меню скрыто.
- Backend: новый router + сервис + config (изолированно). Frontend: новая
  страница + api-модуль + маршрут.
- `git revert` откатывает. Миграции НЕТ (нет новых таблиц/полей).

## Open questions

Решено: подход = client HTTP-ping (юзер→узел); UI = кабинет (React).
Ping-host = DNS с валидным TLS (не raw IP) для обхода mixed-content.
Инфра `/ping`+CORS+TLS на узлах — требование оператора, документируется,
вне кода бота. v1 маппинг ping_host через JSON/шаблон; admin-UI маппинга —
follow-up.
