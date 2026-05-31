# Speedtest — настройка узлов (требование оператора)

Фича speedtest (кабинет → «Проверить задержку») меряет HTTP round-trip от
браузера пользователя до каждого узла. Чтобы узел можно было измерить, на
нём должен быть поднят лёгкий `/ping`-эндпоинт. Без этого узел показывается
как «недоступен» — фича degrade-friendly, остальные узлы меряются нормально.

## Требования к узлу

1. **HTTPS** на DNS-имени узла с **валидным TLS-сертификатом**
   (Let's Encrypt). Пинг по сырому IP из HTTPS-кабинета невозможен
   (mixed-content + серт по IP редкость) — нужен именно домен.
2. Эндпоинт `GET /ping` → **204 No Content** с CORS-заголовком для домена
   кабинета.

### nginx-сниппет

```nginx
location = /ping {
    add_header Access-Control-Allow-Origin "https://<cabinet-domain>" always;
    add_header Access-Control-Allow-Methods "GET, OPTIONS" always;
    return 204;
}
```

Замените `<cabinet-domain>` на домен вашего кабинета (например
`https://cabinet.example.com`). Ответ 204 без тела — измеряется только
время round-trip, тело клиент не читает (`mode: 'no-cors'`), поэтому даже
без CORS пинг технически сработает, но CORS-заголовок рекомендуется.

## Привязка узла к ping-хосту

Бэкенд НЕ отдаёт сырой IP узла. Для каждого узла нужно знать его
`ping_host` (DNS-имя с TLS). Два способа задать:

### 1. Per-node маппинг (точный)

JSON-настройка `host_mapping` (`SpeedtestSettingsService`,
`data/speedtest_settings.json`): `{ "<node_uuid>": "<ping_host>" }`.
Например:
```json
{
  "speedtest": {
    "enabled": true,
    "host_mapping": {
      "a1b2c3d4-...": "nl1.vpn.example.com",
      "e5f6...": "de1.vpn.example.com"
    }
  }
}
```

### 2. Шаблон (если узлы именуются предсказуемо)

`SPEEDTEST_PING_HOST_TEMPLATE` в `.env`, например:
```
SPEEDTEST_PING_HOST_TEMPLATE={node_name}.vpn.example.com
```
Доступные плейсхолдеры: `{node_name}`, `{country_code}`. Используется,
когда для узла нет записи в `host_mapping`.

Приоритет: `host_mapping[uuid]` → шаблон → узел **исключается** из выдачи
(нечего валидно пинговать).

## Включение

`.env`:
```
SPEEDTEST_ENABLED=true
SPEEDTEST_SAMPLES=5
SPEEDTEST_PING_HOST_TEMPLATE=
```
+ `enabled: true` в `data/speedtest_settings.json` (или через будущую
admin-панель). По умолчанию фича **выключена**.

## Что видит пользователь

- Узлы с валидным ping-host и работающим `/ping` → задержка в мс
  (медиана из N сэмплов, первый отброшен), цвет (зел/жёлт/красн), ⚡«лучший».
- Узлы без `/ping`/TLS/маппинга → «недоступен» (не ломают замер остальных).

## Безопасность

- Эндпоинт `nodes-latency-targets` доступен только авторизованному
  пользователю кабинета с активной/триал подпиской.
- Сырой IP/порт узла НЕ отдаётся — только `ping_host` (DNS), имя, страна,
  online-статус, число онлайн-юзеров.
- `/ping` → 204 без тела: нет амплификации, ответ ≤ запрос.
- Рекомендуется `limit_req` на `/ping` в nginx против абуза.
