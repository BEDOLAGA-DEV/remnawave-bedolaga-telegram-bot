# Доступность из РФ: интеграция bschekbot API через кабинет

Дата: 2026-09-05. Статус: проект, ждёт ревью. Репозитории: бот (этот) и кабинет
(`bedolaga-cabinet`). Внешний сервис: bschekbot API v1, `https://bsbord.com/v1`,
OpenAPI `https://bsbord.com/v1/openapi.json`.

## 1. Зачем

Сервис проверяет достижимость серверов из мобильных сетей РФ глазами реальных
симок: ICMP/TCP/SNI-пробы, скан /24 и полноценные тесты конфигов VLESS, VMess,
Trojan, Shadowsocks и Hysteria2 через операторский DPI. Единица проверки — симка:
оператор × федеральный округ × состояние Белого списка (`op_key` вида `mts|цфо|on`).
Каждая симка — отдельная проверка и отдельное списание.

Сервис предназначен в первую очередь для серверов «под Белый список»: конфигов с
подобранными адресами и SNI, которые должны работать у симок с включённым Белым
списком. Обычные VPN-хосты под Белым списком не обязаны открываться, для них честная
проверка идёт по симкам без Белого списка.

Админу кабинета нужно: проверять хосты панели Remnawave и конфиги подписок глазами
операторов, находить чистые адреса у хостера, видеть сводку «какой хост у какой симки
жив» и понимать, сколько это стоит.

## 2. Принятые решения

| Вопрос | Решение |
|---|---|
| Объём v1 | Все четыре возможности API: probe хостов/нод/произвольных целей, VLESS-тест конфигов, скан /24. Только ручные запуски. Автоматика по расписанию — после v1. |
| Где | Отдельный раздел админки кабинета «Доступность из РФ» в группе «Система» плюс ярлыки с карточки ноды и с подписки пользователя, которые открывают раздел с подставленной целью. Модалки «на месте» — потом, поверх того же компонента запуска. Telegram-админка бота не трогается. |
| Архитектура | Модель задач с хранением на сервере: одна таблица задач, таблица легов, таблица предпочтений по целям. Один клиент API, один шлюз платных вызовов, один сервис запуска, один фронт-компонент запуска. |
| Субъекты проверки | Хосты панели (главный), адреса нод (грубо, только ICMP), конфиги подписки панели, произвольные цели, подсети /24. |
| Назначение хоста | «под БС» / «обычный» / «не определено»; хранится у нас, задаёт ожидание и набор симок по умолчанию. |
| Эталон конфигов | Подписка панели Remnawave по `shortUuid` (`BSCHEK_REFERENCE_SUBSCRIPTION`), не пользователь бота. В форме можно подставить другую подписку. |
| Имена | Домен `reachability` (права, роуты, пакет сервиса, таблицы). Всё, что принадлежит провайдеру, — `bschek` (клиент, префикс настроек). |

## 3. Поведение живого API, от которого зависит проект

Разведка на тестовом ключе (≈2900 кредитов ≈ 29 ₽) показала расхождения с
текстовым контрактом. Полный перечень — в приложении А. Ключевое:

1. **Синхронный probe стоит за Cloudflare.** Проба крупнее нескольких легов
   упирается в 100-секундный лимит шлюза: HTTP 524 без тела, деньги списаны.
   Результат достаётся **повтором с тем же Idempotency-Key**: пока проверка идёт,
   API отвечает 409 `request_in_progress`; когда закончилась — 200 с полным
   результатом, бесплатно. Проба «1 цель × 16 симок» шла больше четырёх минут и
   была получена через восемь.
2. **Одновременные пробы не отклоняются, а встают в очередь**: время ответа
   растёт с 3–4 до 40–50 секунд. Длительность probe непредсказуема.
3. **Неизвестный оператор в `op_key` даёт 503 `worker_unavailable` retryable:true**,
   а не 400. Опечатка выглядит как сбой флота.
4. **Preview не возвращает `skipped_*`.** Пропуски видны в реальном ответе только
   для явно перечисленных ключей; для голого оператора («mts») — нет.
5. **Флот меняется за час**: 30 → 31 симка (появилась `yota|цфо|on`), операторы и
   округа вне контракта (letai, dobro, volna, winmobile, sberm, rtk, t-mobile; округ kgd).
6. **OpenAPI описывает только тела запросов и конверт ошибок**, форм ответов в нём
   нет. Источник правды по ответам — записанные фикстуры.
7. **VLESS**: сразу `running`, успех 9–66 с, провал до ~190 с на лег;
   `speed_mbps` всегда 0; отмена даёт `state:"done"` с `cancelled:true` внутри лега,
   незапущенные леги в результате отсутствуют; повторная отмена — 409
   `cannot_cancel_running`; preview нет, цена известна из ответа на запуск
   (~103 кредита за сервер × симка на тарифе gold).
8. **Скан**: одна симка проходит /24 за 11–24 с, семь симок — 234 с;
   `result.operators` содержит только симки с находками; отмена — `state:"cancelled"`
   и результат с нулевой ценой; повторная отмена — 409 `not_running`.
9. **Reality-хосты**: cert-validated TCP (`tcp_is_tls:true`) даёт ложный `blocked`
   (сертификат принадлежит dest). Судить надо по `sni[]` с настоящим SNI хоста и по
   VLESS-тесту.
10. Мелочи с последствиями: кириллица в query без percent-encoding → 400; дедуп целей
    чувствителен к регистру; `/account` отдаёт `webhook_secret`; `refunded` бывает
    частичным без флапа; недокументированные коды (`parse_failed`); «не найдено» у VLESS
    — 200 с `state:"not_found"`, у скана — 404; отмена завершённого VLESS — 404,
    завершённого скана — 409; троттлинг 1 req/s на платные POST реален.

## 4. Архитектура

### 4.1 Бот

```
app/external/bschek_api.py                 клиент HTTP (aiohttp), ошибки, разбор ответов
app/services/reachability/
    __init__.py
    gate.py            шлюз платных вызовов (очередь, интервал, 429)
    units.py           кэш и раскрытие симок, валидация op_key, расчёт пропусков
    targets.py         разрешение целей: хосты, ноды, конфиги, произвольные, /24
    links.py           разбор ссылок vless/vmess/trojan/ss/hysteria2 (чистые функции)
    verdict.py         вердикт лега и соответствие ожиданию (чистые функции)
    pricing.py         preview/оценка, потолок, цена лега VLESS
    jobs.py            жизненный цикл задач, фон, повторы, отмена, обходчик
    service.py         фасад для роутов
app/cabinet/routes/admin_reachability.py   роуты /admin/reachability
app/cabinet/schemas/reachability.py        pydantic-схемы
app/database/models.py                     + ReachabilityJob, ReachabilityLeg, ReachabilityTargetPref
app/database/crud/reachability.py          CRUD и запрос сводки
migrations/alembic/0115_create_reachability_tables.py
app/external/remnawave_api.py              + RemnaWaveHost, get_all_hosts()
app/config.py                              + BSCHEK_* и хелперы
app/services/system_settings_service.py    + категория BSCHEK
app/services/permission_service.py         + секция reachability
app/services/rbac_bootstrap_service.py     + reachability:* у Admin
```

Файлы держатся до ~400 строк; всё, что растёт, дробится по ответственности.

### 4.2 Кабинет

```
src/api/reachability.ts                       типы и вызовы API
src/pages/AdminReachability.tsx               тонкая страница: шапка, статус, вкладки
src/components/admin/reachability/
    StatusBar.tsx  UnitPicker.tsx  LaunchPanel.tsx  JobProgress.tsx
    HostsTargetList.tsx  NodesTargetList.tsx  SubscriptionConfigs.tsx
    CustomTargetInput.tsx  CidrInput.tsx
    ProbeResult.tsx  VlessResult.tsx  ScanResult.tsx
    HostsSummaryMatrix.tsx  JobsHistory.tsx
    useReachabilityJob.ts  verdict.ts  deepLink.ts  (+ *.test.ts)
src/App.tsx                маршрут /admin/reachability (PermissionRoute reachability:read)
src/pages/AdminPanel.tsx   пункт меню в группе system
src/components/admin/constants.ts  подраздел настроек с категорией BSCHEK
src/components/icons/index.tsx     иконка раздела (Phosphor)
src/locales/{ru,en,zh,fa}.json     admin.reachability.*, admin.nav.reachability, категория
src/pages/AdminRemnawave.tsx       кнопка-ярлык на карточке ноды
src/components/admin/userDetail/SubscriptionTab.tsx  кнопка-ярлык у подписки
```

## 5. Настройки и права

Настройки — поля `Settings` в `config.py`, попадают в реестр `system_settings_service`
(редактируются из кабинета, переменные окружения перекрывают БД):

| Ключ | Тип / умолчание | Смысл |
|---|---|---|
| `BSCHEK_ENABLED` | bool / false | Включатель интеграции |
| `BSCHEK_API_URL` | str / `https://bsbord.com/v1` | База API |
| `BSCHEK_API_KEY` | str \| None / None | Ключ `bsk_live_…`; секрет, маскируется реестром |
| `BSCHEK_REQUEST_TIMEOUT` | int / 200 | Таймаут HTTP-клиента, секунды |
| `BSCHEK_REFERENCE_SUBSCRIPTION` | str \| None / None | `shortUuid` эталонной подписки панели |
| `BSCHEK_JOB_COST_LIMIT_KOPEKS` | int / 0 | Потолок цены одной задачи, 0 — без потолка |

Категория реестра `BSCHEK`: заголовок «📶 Доступность из РФ (bschekbot)», префикс
`BSCHEK_` → `BSCHEK`. Хелперы `settings.is_bschek_enabled()` /
`is_bschek_configured()`. В дереве настроек кабинета — подраздел в группе системных с
категорией `['BSCHEK']`.

Права: секция `reachability` с действиями `read` (раздел, история, сводка, симки,
баланс) и `run` (запуск платных задач и отмена). Роль Admin получает `reachability:*`,
Moderator — ничего (владелец выдаст сам). Настройки — под существующими `settings:*`.

## 6. Модель данных

Типы JSON — `JSON` SQLAlchemy (работает и в SQLite, и в PostgreSQL). Перечисления — строки,
проверяются в коде.

### 6.1 `reachability_jobs`

| Поле | Тип | Смысл |
|---|---|---|
| `id` | int PK | |
| `kind` | str | `probe` / `vless` / `scan` |
| `status` | str | `pending` → `running` → `done` / `failed` / `cancelled` |
| `phase` | str \| null | у `running`: `submitting`, `waiting`, `retrieving`, `polling`, `cancelling` |
| `trigger` | str | `manual` (v1); `scheduled` зарезервировано |
| `started_by_user_id` | int FK users | админ |
| `idempotency_key` | str unique | uuid4, генерируется при создании, не меняется |
| `external_id` | int \| null | `scan_id` / `test_id`; у probe пусто |
| `last_request_id` | str \| null | `X-Request-Id` последнего ответа |
| `request` | JSON | тело запроса к API как ушло (для повторов байт в байт) |
| `targets` | JSON | список целей (см. 7.1) |
| `units_requested` | JSON | ключи/селекторы как заказал админ |
| `units_resolved` | JSON | раскрытие по свежему списку симок на момент запуска |
| `units_effective` | JSON | что реально пошло по ответу API (`operators`/`units`) |
| `skipped` | JSON | наши расчётные пропуски + `skipped_dpi_off`/`skipped_unavailable` из ответа |
| `dpi` | str | `on` / `off` / `any` |
| `estimated_kopeks` | int \| null | preview или оценка |
| `estimate_is_exact` | bool | true — preview, false — оценка (VLESS) |
| `cost_kopeks` | int \| null | списание по ответу |
| `refunded_kopeks` | int \| null | возврат по ответу |
| `result` | JSON \| null | сырой итоговый ответ целиком |
| `error_code`, `error_message` | str \| null | |
| `retryable` | bool \| null | из `details.retryable` |
| `attempts` | int | число обращений к API по задаче |
| `created_at`, `started_at`, `finished_at`, `updated_at` | datetime | |

Индексы: `(kind, created_at)`, `(status)`, `(started_by_user_id)`, `(external_id)`.

### 6.2 `reachability_legs` — только probe и vless

| Поле | Смысл |
|---|---|
| `id`, `job_id` FK, `kind` | |
| `target_key` | нормализованный `адрес:порт` (нижний регистр) |
| `target_kind`, `target_ref` | тип цели и uuid хоста / uuid ноды / shortUuid подписки |
| `op_key`, `operator`, `region`, `dpi` | из лега |
| `verdict` | `reachable` / `blocked` / `down` / `unknown` / `cancelled` |
| `matches_expectation` | bool \| null (null — назначение не определено) |
| `raw` | JSON лега как пришёл |
| `checked_at` | время завершения задачи |

Индексы: `(target_key, op_key, checked_at)`, `(job_id)`. Скан в леги не раскладывается
(до тысячи адресов на симку), его результат живёт в `jobs.result`.

### 6.3 `reachability_target_prefs`

| Поле | Смысл |
|---|---|
| `target_kind`, `target_ref` | уникальная пара: `host` + uuid хоста, `node` + uuid ноды |
| `purpose` | `bs` / `regular` / `unknown` |
| `excluded` | bool — не показывать в сводке |
| `note` | str \| null |
| `updated_by_user_id`, `updated_at` | |

Во второй версии эта же таблица скажет планировщику, что мониторить.

### 6.4 Вердикт и ожидание (`verdict.py`, чистые функции)

Probe-лег:

- `leg.ok == false` → `unknown` (проба не выполнена; `error` показывается).
- Если запрашивалась SNI-проба с настоящим SNI хоста: `sni[host].verdict == alive` → `reachable`;
  `blocked`/`refused` (таймаут рукопожатия, RST под БС) → `blocked`; `down` → см. дальше.
- Иначе TCP: `tcp_is_tls:false` и `tcp.ok` → `reachable`; `tcp_is_tls:true` и `verdict alive` → `reachable`;
  `verdict blocked` → `blocked`, **кроме** Reality-хоста (назначение `bs` или SNI ≠ адресу):
  тогда решает SNI-проба, а без неё — `unknown` с подсказкой «добавьте SNI-пробу».
- Только ICMP: `icmp.ok` → `reachable`, иначе `down`.
- Ничего не открылось, соединения не установились → `down`.

VLESS-лег: `cancelled:true` → `cancelled`; `ok && tunnel_up` и хотя бы одна цель `ok` →
`reachable`; `tunnel_up` и все цели закрыты (`zombie_tcp`, `dataplane_dead`) → `blocked`;
`tcp_ok:false` (`tcp_timeout`) → `down`; иное → `unknown`.

Ожидание: назначение `bs` → у симок `dpi:on` ожидается `reachable`; назначение `regular` →
у `dpi:off` ожидается `reachable`, у `dpi:on` ожидание отсутствует (справочная строка);
назначение `unknown` → `matches_expectation = null`. `down`/`unknown` никогда не
«соответствуют».

## 7. Разрешение целей

### 7.1 Единый формат цели

```json
{ "kind": "host|node|subscription_config|custom|cidr",
  "label": "…", "address": "…", "port": 443, "target_key": "адрес:порт",
  "sni": "…", "ref": {"host_uuid"|"node_uuid"|"short_uuid"|null},
  "purpose": "bs|regular|unknown", "raw_link": "…"  }
```

### 7.2 Источники

- **Хосты панели.** `GET /api/hosts` (новый метод клиента Remnawave, dataclass
  `RemnaWaveHost`: uuid, remark, address, port, sni, host, inbound{configProfileUuid,
  configProfileInboundUuid}, isDisabled, isHidden, tag, securityLayer). Probe:
  `адрес:порт`, `probes {tcp, sni}` по умолчанию, `sni_hosts = [sni or host or address]`,
  ICMP по желанию. Отключённые хосты скрыты по умолчанию. Связь с нодой:
  `inbound.configProfileInboundUuid ∈ node.active_inbounds`; запасной путь — адрес хоста
  совпадает с `node.address` или с одним из `node.ips`.
- **Ноды.** Цель — `node.address`, проба только ICMP, подпись «отвечает ли сервер на ping».
  Порт ноды не используется (канал панели). При наличии привязанных хостов кнопка
  предлагает проверить их.
- **Конфиги подписки.** Только `/api/sub/{shortUuid}/info → links[]` (публичный sub-URL
  неизвестному клиенту отдаёт заглушки). Разбор (`links.py`): схемы vless, vmess (base64
  JSON), trojan, ss (base64 `method:pass@` и plain), hysteria2; поля протокол, адрес,
  порт, SNI (`sni`/`host`/`peer`), имя из фрагмента. Заглушки `0.0.0.0:1` и неизвестные
  схемы отбрасываются с пометкой. Выбранные конфиги → `selected_servers` (`{address,
  port, name}`), сырые строки → `raw_input`, не больше 20 за тест (иначе форма делит на
  части). Те же конфиги дают хосты «глазами пользователя»; дедуп с хостами панели по
  `target_key`.
- **Произвольная цель.** Принимает IP, домен, `адрес:порт`, `http(s)://…` (схема
  отбрасывается, порт из URL сохраняется), а также готовые ссылки конфигов (тогда это
  VLESS-тест). Нижний регистр, дедуп после нормализации, приватные/loopback/link-local
  диапазоны отсекаются до отправки.
- **Подсеть /24.** Ручной ввод (строго /24) и кнопка «подсеть этого хоста»: домен
  резолвится на стороне бота, берётся /24 от IP.

### 7.3 Назначение

Эвристика заполняет значение по умолчанию один раз: SNI задан, не равен домену адреса и
не является его поддоменом → `bs`; remark/tag содержит «БС», «BS», «LTE» → `bs`;
иначе `regular`. Дальше решает админ переключателем в списке хостов; выбор хранится в
`reachability_target_prefs`. Назначение задаёт ожидание (6.4) и набор симок по
умолчанию: `bs` → симки с БС, `regular` → без БС; «все» доступно всегда.

### 7.4 Эталонная подписка

`BSCHEK_REFERENCE_SUBSCRIPTION` = `shortUuid` подписки панели. Выбирается в настройках
поиском по подпискам панели (username / uuid / shortUuid через существующий список
подписок панели). Статус раздела показывает имя подписки, число разобранных конфигов и
причину, если эталон не разрешается. Ярлык с карточки пользователя бота даёт
`subscription.remnawave_short_uuid` → тот же путь.

### 7.5 Ярлыки

`/admin/reachability?target=host:<uuid>`, `?target=node:<uuid>` (параметр повторяемый),
`?tab=vless&user=<id>` или `?tab=vless&sub=<shortUuid>`. Раздел открывает нужную вкладку
с подставленными целями.

## 8. Жизненный цикл задачи

### 8.1 Создание (в запросе админа, до траты денег)

1. Разрешить цели (раздел 7); панель недоступна → 503 админу, ничего не потрачено.
2. Свежий список симок (кэш ≤ 60 с); каждый заказанный ключ/селектор раскрыть по нему;
   неизвестный оператор/округ → 400 админу словами.
3. Посчитать пропуски: заказано − раскрыто по фильтру `dpi`.
4. Цена: `POST /probe/preview` или `POST /scans/preview` в момент запуска; для VLESS —
   оценка `n_servers × n_units × цена_лега`, где цена лега = `cost/(n_servers×n_modems)`
   последней завершённой VLESS-задачи, а до неё 110 копеек.
5. Проверить потолок и остаток баланса (`GET /account`, кэш 30 с).
6. Наши блокировки: активная задача `vless` или `scan` → 409 с автором и временем.
7. Вставить задачу `pending`, сгенерировать `idempotency_key`, записать аудит,
   запустить фон со своей сессией БД (сильная ссылка на задачу, как в `admin_promo_offers`).

### 8.2 Probe

Через шлюз: `POST /probe`, таймаут 200 с. 200 → `done`, сохранить результат, стоимость,
возврат, `units_effective`, `skipped`, разложить леги. `BschekGatewayError` (524/502/без
конверта), таймаут или сетевой обрыв → `phase=retrieving`: повтор тем же ключом каждые
15 с первые 2 минуты, затем каждые 30 с до 20 минут; 409 `request_in_progress` — ждать
дальше; 200 — `done`. Исчерпание — задача остаётся `running` с `phase=retrieving`, её
добирает обходчик, в интерфейсе кнопка «Забрать результат».

### 8.3 VLESS и скан

`POST /vless` / `POST /scans` через шлюз → `external_id`, для VLESS цена из ответа
(потолок превышен → немедленная отмена, задача `failed` с причиной «превышен потолок»,
списания нет), для скана `units[]` → `units_effective`. `phase=polling`: VLESS каждые
5 с, скан каждые 4 с. Готовность — по `result_ready` (VLESS) и `state != running` (скан),
статус только из GET, ответ на повторный submit статусом не считается. Таймауты: VLESS
5 мин + 3 мин на лег (потолок 45), скан 3 мин + 1 мин на симку (потолок 40); по
истечении задача остаётся `running`, добирает обходчик.

### 8.4 Отмена

`POST …/cancel`, затем контрольный GET. 409 `cannot_cancel_running` → повторить GET через
2 с; 409 `not_running`, 404 → итог из GET. Статус задачи: у VLESS — по легам
(`cancelled:true`), у скана — по `state`. Стоимость и возврат — из финального GET.

### 8.5 Обходчик и перезапуск

Фоновая петля раз в 60 с (как `monitoring_service.start_monitoring`): все `running`
задачи старше 30 с без активного фона — VLESS/скан по `external_id` через GET, probe —
повтор ключа. При старте бота обходчик поднимает незавершённые задачи.

## 9. API кабинета `/admin/reachability`

| Метод и путь | Право | Смысл |
|---|---|---|
| `GET /status` | read | `enabled`, `configured`, `healthy`, баланс/тариф/срок (без `webhook_secret`), активные задачи (`kind`, `id`, автор, старт), эталон (имя, число конфигов, ошибка) |
| `GET /units?dpi=&operator=&region=` | read | симки из API через кэш ≤60 с |
| `GET /targets/hosts?include_disabled=` | read | хосты панели с назначением, нодами и последним вердиктом по симкам |
| `GET /targets/nodes` | read | ноды с адресом и списком привязанных хостов |
| `GET /targets/subscription?short_uuid=&user_id=` | read | разобранные конфиги подписки (без сырых ссылок в ответе; сырые уходят на сервере) |
| `PUT /targets/prefs` | run | назначение/исключение/заметка для цели |
| `POST /jobs/preview` | read | раскрытые симки, пропуски, цена/оценка, `estimate_is_exact`, предупреждения |
| `POST /jobs` | run | создать и запустить; 409 при занятости; 400 при валидации |
| `GET /jobs?kind=&status=&target=&user_id=&page=` | read | история |
| `GET /jobs/{id}` | read | задача с результатом и легами (фронт опрашивает раз в 3 с) |
| `POST /jobs/{id}/cancel` | run | отмена |
| `POST /jobs/{id}/retrieve` | run | «Забрать результат» для застрявшей probe-задачи |
| `GET /summary/hosts?dpi=on` | read | матрица хост × симка по последним легам |

Тело `POST /jobs`:

```json
{ "kind": "probe|vless|scan",
  "targets": [ {"kind":"host","ref":"<uuid>"}, {"kind":"custom","value":"1.2.3.4:443"},
               {"kind":"subscription_config","short_uuid":"…","index":0}, {"kind":"cidr","value":"192.0.2.0/24"} ],
  "units": ["mts|цфо|on", "tele2|*|on"], "dpi": "on",
  "probes": {"icmp": false, "tcp": true, "sni": true},
  "core": "" }
```

Ошибки — конверт кабинета `{detail}`; создание и отмена пишутся в аудит
(`permission_service.log_action`, `resource_type='reachability_job'`, в `details` — вид,
цели, симки, цена).

## 10. Фронт кабинета

- Один маршрут, ленивая загрузка, `PermissionRoute reachability:read`; пункт меню в
  группе `system` (тест покрытия меню проходит). Вкладки через `?tab=`: **Сводка**,
  **Проверка**, **VLESS-тест**, **Скан /24**, **История**. Настройки — в общей странице
  настроек (категория `BSCHEK`), ссылка из шапки раздела.
- `StatusBar`: баланс в рублях, тариф и срок, занятость (кто и что гонит), состояния
  «выключено»/«не настроено»/«нездорово» со ссылкой в настройки.
- `UnitPicker`: группы по операторам, чипы «с БС / без БС / все», фильтр округа, выбрать
  всё/ничего, счётчик; последний выбор на вид проверки — в `localStorage` под try/catch.
- Выбор целей: `HostsTargetList` (поиск, переключатель назначения, фильтр отключённых),
  `NodesTargetList`, `SubscriptionConfigs` (выбор подписки панели, галочки ≤ 20),
  `CustomTargetInput`, `CidrInput` с «подсетью этого хоста».
- `LaunchPanel` общий: что выбрано, раскрытие и пропуски, цена или оценка с пометкой,
  предупреждения (Reality без SNI-пробы, потолок, занято, баланс), кнопка
  «Запустить за X ₽» — она и есть подтверждение; блокировки выключают кнопку и пишут
  причину.
- `JobProgress` + `useReachabilityJob`: опрос `GET /jobs/{id}` раз в 3 с, остановка на
  конечном статусе, стадии «отправлено», «ждём операторов» с таймером и ориентиром
  времени, «забираем результат»; отмена; через 25 минут опрос прекращается с текстом
  «результат появится в истории».
- Результаты: `ProbeResult` (таблица цель × симка, бейджи вердикта, раскрытие сырых
  проб), `VlessResult` (сервер × симка: туннель, цели, задержки, причина, ядро, диагноз),
  `ScanResult` (живые адреса по симкам, копирование в буфер; скачивания нет — в Mini App
  оно выбрасывает из приложения).
- `HostsSummaryMatrix`: строки — хосты, столбцы — симки (по умолчанию с БС), ячейка —
  последний вердикт и возраст, цвет по `matches_expectation`, клик → задача.
- `JobsHistory`: фильтры, стоимость и возврат, детали в `Sheet`.
- Ярлыки: кнопка-иконка на карточке ноды (при `reachability:run` и `enabled`), кнопка
  «Проверить через операторов РФ» на вкладке подписки пользователя. Только переходы.
- Канон: заголовок админки `text-xl font-bold text-dark-100`; цвета только токенами
  (`success` — соответствует, `error` — не соответствует, `warning` — неизвестно,
  `dark` — отменено, текст `text-on-*`); радиусы bento/2xl/xl; кнопки примитивом;
  иконки Phosphor через баррель; скелетоны только `Skeleton`/`SkeletonGroup`.
- Локали: `admin.reachability.*`, `admin.nav.reachability`,
  `admin.settings.categories.BSCHEK` — во всех четырёх файлах сразу.

## 11. Деньги и ошибки

### 11.1 Деньги

- Цена считается в момент запуска (preview), не при открытии формы.
- VLESS — оценка (см. 8.1, шаг 4), помечена как оценка; вторая линия — отмена сразу после
  ответа на запуск, если фактическая цена выше потолка.
- Баланс в шапке, обновление после каждой задачи; preview дороже остатка → запуск
  блокируется до 402.
- Один ключ идемпотентности на задачу навсегда; автоматические повторы — только с ним и с
  тем же телом из `jobs.request`. Новый ключ = новая задача по явному «Повторить».
- Хранятся оценка, списание, возврат; история показывает «списано − возврат».
- 500 на платном POST: один повтор тем же ключом через 60 с; пусто → `failed` с
  `request_id` на виду.

### 11.2 Классы ошибок

| Класс | Коды | Поведение |
|---|---|---|
| Доступ/тариф | 401 `unauthenticated`, 403 `api_not_available`/`tier_too_low`/`subscription_required` | задача `failed`; статус интеграции «нездоров» 5 минут, запуски не принимаются; шапка показывает причину и ссылку в настройки |
| Нет денег | 402 `insufficient_credits` | `failed`, текст про баланс |
| Валидация | 400/422 семейство, `blocked_target`, `no_dpi_on`, `unknown_operator`, `cidr_*`, `subscription_not_supported`, `parse_failed`, `no_configs`, `too_many_configs`, `input_too_large`, `webhooks_disabled`, `idempotency_key_*` | предотвращаем до отправки; иначе `failed` с текстом API и `details`; незнакомый код — сообщение API как есть |
| Троттлинг | 429 `rate_limited` | шлюз ждёт `retry_after`, повтор тем же ключом, админ не видит |
| Занято | 409 `test_in_progress`, `scan_in_progress`, `busy`, `too_many_active` | `failed` сразу, «занято на стороне сервиса», `retryable:true`, денег нет |
| Ещё идёт | 409 `request_in_progress` | не ошибка: ждать и повторять тем же ключом |
| Временные | 503 `worker_unavailable`, `scanner_unavailable`, `lte_unavailable`, `maintenance`, `bot_not_ready`, `no_alive_modems` | 3 повтора тем же ключом с паузой `retry_after` или 60 с; потом `failed` retryable |
| Шлюз/сеть | 524, 502 без конверта, таймаут, обрыв | стадия `retrieving` (8.2) |
| Статус скана | `error` ∈ `needs_dpi_off_confirm`, `lte_unavailable`, `interrupted_restart` | `failed` с `retryable` из ответа |
| Статус VLESS | `unknown` → продолжать; `not_found` после полученного id → `failed`, без повтора | |
| Отмена | 409 `cannot_cancel_running`, 409 `not_running`, 404 после отмены | норма, итог из GET |

### 11.3 Что не утекает

Ключ API никуда не возвращается и не логируется; `webhook_secret` отбрасывается на
границе клиента; сырые ссылки конфигов не отдаются фронту; в логах — `request_id`, id
задачи, суммы.

## 12. Тестирование

Бот (pytest): контрактные тесты клиента на санитизированных фикстурах разведки
(`tests/fixtures/bschek/`), сторожа «каждый код ошибки размечен» и «каждая фикстура
читается»; шлюз на подменённых часах; сервис задач с фальшивым клиентом по сценариям
(524 → `request_in_progress` → 200; 500 + повтор; потолок до/после; блокировки; отмены;
обходчик; таймауты); вердикт и ожидание по кейсам разведки; разрешение целей и разбор
ссылок; роуты по образцу `test_admin_remnawave_geocheck`; БД на `sqlite_memory` и под
маркером `postgres`; сторожа реестров (настройки, права, роль Admin); живой детектор
дрейфа под маркером `bschek_live` (только бесплатные ручки, пропускается без ключа в
окружении).

Кабинет (vitest): `verdict.ts`, `deepLink.ts`, логика `UnitPicker`, стадии
`useReachabilityJob` на фальшивых таймерах, причины блокировки кнопки, форматирование
копеек; существующие тесты меню и скелетонов; паритет ключей локалей. Сквозной сценарий
через браузерную обвязку с мок-JWT; вёрстка снимается в светлой, тёмной и Mini App.

Процесс: TDD, покрытие новых модулей ≥ 80 %, `ruff format` + `ruff check`, в кабинете
lint/format/type-check/build, чеклист верификации перед сдачей.

## 13. Вне объёма v1

Проверки по расписанию и оповещения; модалки «на месте» на чужих страницах; вебхуки API
(выключены на стороне сервиса); Telegram-админка бота; экспорт результатов файлом.

## Приложение А. Разведка живого API (санитизировано)

Обозначения: `bs-host.example:9443` — хост под Белый список с SNI `whitelisted.example`;
`eu-host.example:443` — обычный VPN-хост. Все IP из тестовых диапазонов.

1. Конверт ошибок единый на 4xx/5xx, `X-Request-Id`, `X-API-Version: 1.1`.
2. Идемпотентность: повтор → тот же ответ, без списания; другое тело → 409
   `idempotency_key_reused`; без заголовка → 400 `idempotency_key_required`.
3. Все цели приватные → 400 `blocked_target`, без списания.
4. Троттлинг: три параллельных платных POST → 200 + два 429 (`retry_after` ≈ 1,
   `Retry-After: 1`). Последовательные запросы 429 не ловят.
5. Второй VLESS поверх идущего → 409 `test_in_progress`; второй скан → 409
   `scan_in_progress` (оба retryable:true).
6. `/operators`: фильтры `dpi`, `operator`, `region` (кириллица только с
   percent-encoding, иначе 400), `probeable`; `?operator=неизвестный` → 200, `n_units:0`.
7. Preview: селекторы `mts`, `mts|*|on`, `*|цфо|on`, `|цфо|on`, `mts||on` работают;
   `["mts","yota|уфо|off"]` без dpi → только `mts|пфо|on`; `dpi:"on"` + off-ключ → 400
   `no_dpi_on` с `details.skipped_dpi_off[{op_key,operator,region,dpi}]`; preview не
   возвращает `skipped_*`; `sni_hosts` без `probes.sni` не тарифицируется; `operators:[]`
   и `null` = все по dpi.
8. 400: старый формат ключа → `unknown_operator`; `*|*|*` и четыре части →
   `invalid_request`; 11 целей → `too_many_targets`; нет целей → `invalid_request`;
   невалидный JSON → **422** с `details.fields[]`; не /24 → `cidr_too_wide` (/23) или
   `cidr_not_24` (/25); `webhook_url` → `webhooks_disabled`.
9. **Неизвестный оператор в ключе → 503 `worker_unavailable` retryable:true**, с
   `details.unavailable[]`.
10. Цены (gold, −7 %): probe 20 кред/цель/симка, +4 за SNI-имя; скан 66/симка (+13 со
    SNI); VLESS ≈103 за сервер × симка; 1 кред = 1 коп.
11. Probe 1 × 1: 3–10 с. Два перекрывающихся probe → оба 200, но 43 и 48 с (очередь,
    `too_many_active` не воспроизвёлся).
12. **Probe 2 цели × 5 симок со SNI → 524 через 125 с, тело пустое, `server: cloudflare`,
    списано 260. Повтор тем же ключом через минуту → 200 за 0,4 с, бесплатно.**
13. **Probe 1 цель × 16 БС-симок → 524 через 125 с, списано 357; 8 повторов по 15 с — все
    409 `request_in_progress`; повтор через ~9 минут → 200, 16 легов, бесплатно.**
14. Тот же ключ через 2,5 с после старта probe → 409 `request_in_progress` за 0,4 с.
    Ключ произвольного формата (не UUID) принимается.
15. Голый `mts` при dpi по умолчанию → одна симка, `skipped_dpi_off` в ответе НЕТ; явные
    ключи с фильтром → `skipped_dpi_off` ЕСТЬ. `operators:[]` + `dpi:"off"` → все 15
    без-БС симок, 21 с.
16. `refunded:1` при трёх/двух успешных легах со смешанным набором on/off — закономерность
    не ясна.
17. Лег probe: `{ok, operator, region, dpi, channel_state, target, error, tcp_is_tls,
    icmp{ok,sent,received,loss_pct,rtt_ms,rtt_avg_ms,rtt_min_ms,rtt_max_ms}|null,
    tcp{ok,received,total,error}|{ok,received,total,verdict,cert_names[],matches_sni}|null,
    sni[{ok,host,verdict,latency_ms,error?}]|null, http{…}|null}`. Вердикты SNI/TCP:
    `alive`, `down`, `refused`, `blocked`. HTTP-проба к `http://…` собирает
    `http://host:443/` и падает «Cleartext HTTP traffic not permitted» (исполнитель на
    Android) — `http://`-цели для HTTP-пробы бесполезны.
18. **Reality**: у `bs-host.example:9443` `tcp_is_tls` даёт `verdict:"blocked"`,
    `cert_names:["CN=*.whitelisted.example"]`, `matches_sni:false`, при этом
    `sni[whitelisted.example].verdict:"alive"` и `http.status:403` от реального nginx.
    У одной симки TCP проходит, а SNI режется; у другой недоступен сам IP. Набор
    `tcp_is_tls` разный у симок одного оператора.
19. `/account` → `{balance_credits, bonus_credits, balance_total, tier, tier_expires_at,
    active_scan, min_interval_sec, webhook_secret}`.
20. OpenAPI 3.1: схемы только `ProbeBody`, `ScanBody`, `VlessBody`, `V1Error`,
    `HTTPValidationError`; форм ответов нет.
21. VLESS submit: `{outcome:"queued", test_id, tx_id?, cost_credits, n_servers, n_modems,
    configs[{name,address,port,protocol}], queue_pos}`; сразу `state:"running"`,
    `queue_pos:null`; `core_requested` `auto`/`stable`/`prerelease` (легаси `new` →
    `prerelease`); авто-детект для Reality/vision выбрал `prerelease`; `used_core` null
    до конца.
22. VLESS-лег: `{ok, stage, protocol, server_name, server_addr, operator, operator_name,
    region, channel_state, used_core, tcp_ok, tunnel_up, tcp_latency_ms, tsp_latency_ms,
    targets[{target,ok}], sni, sni_check_ok, sni_cert_match, sni_check_latency_ms,
    sni_check_error, speed_mbps, speed_error, fail_reason, core_fallback,
    core_fallback_tried, diagnosis, attempts, cancelled}`. `speed_mbps` всегда 0.
    `fail_reason` ∈ `""`, `zombie_tcp`, `tcp_timeout`, `dataplane_dead`, `cancelled`.
    Протоколы в `configs[]`: `vless`, `vmess`, `trojan`, `shadowsocks`, `hysteria2`.
23. VLESS длительность: успех 9–66 с; провал (`zombie_tcp`, БС-симка, обычный хост)
    186 с; 4 чужих протокола × 1 симка — 199 с.
24. Отмена VLESS: `{test_id, cancelled:true, stopped_legs, refunded_credits:0}`, списания
    нет; статус после — `state:"done"`, лег `stage:"cancelled"`, `cancelled:true`,
    `attempts:0`, `used_core:null`; незапущенные леги в `result` отсутствуют; повторная
    отмена → 409 `cannot_cancel_running`; отмена завершённого → 404 `not_found`; повтор
    submit тем же ключом после отмены → прежний ответ с тем же `test_id`.
25. `GET /vless/{чужой id}` → 200 `state:"not_found"`; `GET /scans/{чужой id}` → 404.
26. VLESS-валидация: sub-URL → 400 `subscription_not_supported`; мусор → 400
    `parse_failed` (не документирован); 21 конфиг → 400 `too_many_configs`; >1 МБ → 400
    `input_too_large`. `selected_servers` отбирает подмножество, `selected_modems`
    принимает селекторы (`mts|*|off` → 2 симки).
27. Скан submit: `{outcome:"queued", scan_id, cidr, state:"running", poll,
    units[{op_key,operator,region,dpi}], n_units}`; во время работы ключа `result` нет;
    done: `result{outcome, id, kind, cidr, up_n, total (254 или 256 — непостоянно),
    scan_ok, cost_credits, refunded, operators (только симки с находками), elapsed_sec,
    results[{ip, by_operator{op_key:{icmp,tcp,sni:{имя:bool}|null,operator,region,dpi}}}]}`.
    Только живые адреса. 1 симка — 11–24 с; 7 симок со SNI — 234 с. `scan_ok:false` при
    `up_n:0`.
28. Отмена скана через 4 с: `{scan_id, state:"cancelled", done_ips:0, total_ips:1536
    (256 × симок), n_jobs_stopped:6}`; GET → `state:"cancelled"`, `result_ready:true`,
    `result{cost_credits:0, refunded:true, results:[]}`, ошибки `cancelled_before_result`
    нет; повторная отмена → 409 `not_running`; отмена завершённого → 409 `not_running`.
    Повтор submit скана тем же ключом (и во время, и после) → первоначальный ответ
    `state:"running"`.
29. Флот за час: 30 → 31 симка (добавилась `yota|цфо|on`; `*|цфо|on` раскрылся в 6
    вместо 5, все БС-симки — 16 вместо 15).
30. Публичный sub-URL панели неизвестному UA отдаёт три заглушки `0.0.0.0:1`.
31. `X-Request-Id` дублируется в `details.request_id`; `Retry-After` только у 429.

Записанные фикстуры (санитизированные) переносятся в `tests/fixtures/bschek/` на этапе
реализации.
