# Bot/Cabinet YooKassa Scope Design

Дата исследования: 2026-06-14

Статус: технический дизайн MVP, без реализации.

## Цель

Разделить YooKassa по месту использования в одном backend-инстансе:

- `bot` - Telegram-бот: пополнение баланса, покупка подписок, trial-платежи, recurrent/autopay для bot flows.
- `cabinet` - Cabinet: пополнение баланса и cabinet flows.

MVP не требует произвольного количества магазинов. Нужны две независимые конфигурации YooKassa, общий backend и обратная совместимость со старым single-shop `YOOKASSA_*`.

## Краткий вывод

Для MVP проще и безопаснее внедрять env-based scopes:

- `YOOKASSA_BOT_*`
- `YOOKASSA_CABINET_*`
- старые `YOOKASSA_*` оставить как fallback/default.

DB-based `yookassa_accounts` полезен как следующий этап, но сейчас он требует отдельного решения по хранению секретов, админскому UI и миграции credentials. Существующая `PaymentMethodConfig` в БД управляет отображением/ограничениями методов оплаты в Cabinet, но не является безопасным хранилищем секретов платежных провайдеров.

Официальный YooKassa SDK в текущем виде лучше не использовать для нескольких credentials в одном процессе: код конфигурирует SDK через глобальный `Configuration.configure(shop_id, secret_key)`. Для scoped credentials безопаснее заменить SDK-вызовы на прямые HTTP-запросы с Basic Auth на каждый request. Вариант с lock вокруг SDK возможен только как временный компромисс, но он сериализует вызовы, остается хрупким и легко ломается при новых местах вызова.

Рекомендуемый webhook для MVP:

- `/yookassa-webhook/bot`
- `/yookassa-webhook/cabinet`
- старый `/yookassa-webhook` оставить для legacy fallback.

## Текущая реализация

### Глобальная конфигурация

Сейчас настройки YooKassa глобальные:

- `app/config.py:421` - `YOOKASSA_ENABLED`.
- `app/config.py:424` - `YOOKASSA_SHOP_ID`.
- `app/config.py:425` - `YOOKASSA_SECRET_KEY`.
- `app/config.py:426` - `YOOKASSA_RETURN_URL`.
- `app/config.py:427` - `YOOKASSA_SBP_ENABLED`.
- `app/config.py:456` - `YOOKASSA_WEBHOOK_PATH`.
- `app/config.py:460` - `YOOKASSA_RECURRENT_ENABLED`.
- `app/config.py:2070` - `is_yookassa_enabled()`.
- `app/config.py:2083` - `get_yookassa_return_url()`.

`settings.is_yookassa_enabled()` сейчас не принимает surface/scope и проверяет только один глобальный магазин.

### YooKassa SDK/service

Основной service:

- `app/services/yookassa_service.py:139` - `YooKassaService.__init__`.
- `app/services/yookassa_service.py:159` - `Configuration.configure(shop_id, secret_key)`.
- `app/services/yookassa_service.py:183` - `create_payment()`.
- `app/services/yookassa_service.py:289` - `create_sbp_payment()`.
- `app/services/yookassa_service.py:405` - `get_payment_info()`.
- `app/services/yookassa_service.py:478` - `create_autopayment()`.

Проверка SDK показала, что `Configuration.configure()` пишет credentials в class-level state, а `Payment.create()` / `Payment.find_one()` создают `ApiClient()` из текущей глобальной `Configuration`. Это небезопасно для параллельных запросов разных магазинов.

### Создание YooKassa платежей

Нижний слой создания:

- `app/services/payment/yookassa.py:92` - `create_yookassa_payment()`.
- `app/services/payment/yookassa.py:196` - `create_yookassa_sbp_payment()`.
- `app/services/payment_service.py:830` - guest payment routes тоже проходят через YooKassa branch.

Telegram bot balance:

- `app/handlers/balance/yookassa.py:22` - старт оплаты картой.
- `app/handlers/balance/yookassa.py:69` - старт СБП.
- `app/handlers/balance/yookassa.py:115` - ввод суммы и создание card payment.
- `app/handlers/balance/yookassa.py:270` - ввод суммы и создание SBP payment.

Telegram subscription/trial:

- `app/handlers/subscription/purchase.py:735` - trial keyboard показывает YooKassa/SBP по глобальным настройкам.
- `app/handlers/subscription/purchase.py:3736` - trial SBP payment.
- `app/handlers/subscription/purchase.py:3774` - trial card payment.
- `app/handlers/simple_subscription.py:921` - simple subscription YooKassa flow.

Bot keyboards/utilities:

- `app/keyboards/inline.py:1568` - `get_payment_methods_keyboard()`.
- `app/keyboards/inline.py:1590` - добавление YooKassa в keyboard.
- `app/keyboards/inline.py:1602` - добавление SBP в keyboard.
- `app/utils/payment_utils.py:14` - `get_available_payment_methods()`.

Cabinet:

- `app/cabinet/routes/balance.py:138` - `GET /cabinet/balance/payment-methods`.
- `app/cabinet/routes/balance.py:307` - `POST /cabinet/balance/topup`.
- `app/cabinet/routes/balance.py:371` - Cabinet вызывает `create_yookassa_sbp_payment()`.
- `app/cabinet/routes/balance.py:380` - Cabinet вызывает `create_yookassa_payment()`.

Miniapp/web API:

- `app/webapi/routes/miniapp.py:695` - список методов оплаты miniapp.
- `app/webapi/routes/miniapp.py:975` - miniapp SBP payment.
- `app/webapi/routes/miniapp.py:1015` - miniapp card payment.

Guest/landing/gift:

- `app/cabinet/routes/landing.py:735` - проверка guest payment method.
- `app/cabinet/routes/landing.py:840` - создание guest payment.
- `app/cabinet/routes/gift.py:391` - создание gift gateway payment.

Admin/test:

- `app/handlers/admin/bot_configuration.py:1918` - test payment через глобальную YooKassa.

### Webhook, status check, recovery

Webhook registration:

- `app/webserver/payments.py:336` - registration на `settings.YOOKASSA_WEBHOOK_PATH`.
- `app/webserver/payments.py:396` - POST handler.
- `app/external/yookassa_webhook.py:203` - legacy/aiohttp webhook handler.

Webhook processing:

- `app/services/payment/yookassa.py:1364` - `process_yookassa_webhook()`.
- `app/services/payment/yookassa.py:1397` - текущая remote-проверка статуса через глобальный service.
- `app/services/payment/yookassa.py:1405` - поиск локального платежа по `yookassa_payment_id`.
- `app/services/payment/yookassa.py:1472` - восстановление отсутствующей локальной записи из webhook metadata.

Manual status check:

- `app/services/payment/yookassa.py:295` - `get_yookassa_payment_status()`.
- `app/services/payment_verification_service.py:1512` - manual check вызывает YooKassa status.
- `app/cabinet/routes/balance.py:1453` - Cabinet manual check endpoint.
- `app/cabinet/routes/admin_payments.py` - admin manual check uses same verification service.

### Autopay / saved cards

Сохранение payment method:

- `app/services/payment/yookassa.py:1150` - после успешного платежа вызывается `_save_payment_method_if_available()`.
- `app/services/payment/yookassa.py:1213` - `_save_payment_method_if_available()`.
- `app/database/crud/saved_payment_method.py:14` - `create_saved_payment_method()`.

Recurrent/autopay:

- `app/services/recurrent_payment_service.py:72` - основной recurrent process.
- `app/services/recurrent_payment_service.py:233` - обработка одной подписки.
- `app/services/recurrent_payment_service.py:298` - получение active saved payment methods.
- `app/services/recurrent_payment_service.py:329` - `create_autopayment()` через YooKassa.
- `app/services/recurrent_payment_service.py:360` - создание локального `YooKassaPayment` для autopay.

Сейчас `SavedPaymentMethod` не хранит scope/account, хотя `payment_method_id` YooKassa привязан к конкретному магазину.

### Таблицы и модели

YooKassa:

- `app/database/models.py:207` - `YooKassaPayment`.
- `app/database/models.py:256` - `SavedPaymentMethod`.
- `app/database/crud/yookassa.py:15` - create local YooKassa payment.
- `app/database/crud/yookassa.py:82` - get by YooKassa payment id.
- `app/database/crud/yookassa.py:98` - update status.

Transactions:

- `app/database/models.py:150` - `PaymentMethod.YOOKASSA = 'yookassa'`.
- `app/database/models.py:2337` - `Transaction`.
- `app/database/models.py:2360` - unique constraint `(external_id, payment_method)`.
- `app/database/models.py:2388` - `receipt_uuid`.

Cabinet payment method config:

- `app/database/models.py:3767` - `PaymentMethodConfig`.
- `app/services/payment_method_config_service.py:18` - `_get_method_defaults()`.
- `app/services/payment_method_config_service.py:267` - default method order includes `yookassa`.
- `app/services/payment_method_config_service.py:300` - seed configs.
- `app/services/payment_method_config_service.py:465` - `get_enabled_methods_for_user()`.
- `app/cabinet/routes/admin_payment_methods.py:141` - admin list configs.
- `app/cabinet/routes/admin_payment_methods.py:191` - admin update config.

`PaymentMethodConfig` сейчас выглядит как кабинетная конфигурация отображения методов оплаты. Она не хранит provider credentials.

### Cabinet frontend

Frontend находится в `bedolaga-cabinet`, но backend routes для оплаты находятся в `bedolaga-telegram-bot`.

Релевантные frontend-файлы:

- `src/api/balance.ts` - `getPaymentMethods()`, `createTopUp()`, saved cards endpoints.
- `src/types/index.ts` - `PaymentMethod`, `PendingPayment`, `SavedCard`, `PaymentMethodConfig`.
- `src/api/adminPaymentMethods.ts` - admin API для payment method configs.

Для MVP пользовательский Cabinet UI может продолжить показывать метод как `YooKassa`. Разделение `bot`/`cabinet` должно быть внутренним.

## Архитектурное решение MVP

### 1. Ввести scope

Добавить явное понятие YooKassa scope:

```python
YooKassaScope = Literal["bot", "cabinet"]
```

Правила:

- Bot handlers всегда запрашивают YooKassa config для `scope='bot'`.
- Cabinet `/cabinet/balance/payment-methods` и `/cabinet/balance/topup` используют `scope='cabinet'`.
- Miniapp лучше считать `cabinet` scope, если он является частью Cabinet/user web flows.
- Landing/gift в MVP можно привязать к `cabinet` scope, потому что эти routes живут в Cabinet backend. Если бизнес хочет отдельную фискализацию/магазин для публичных landing/gift flows, это будущий scope `landing`/`gift`, не часть MVP.

### 2. Env-based config для MVP

Добавить scoped env:

```env
YOOKASSA_BOT_ENABLED=
YOOKASSA_BOT_DISPLAY_NAME=
YOOKASSA_BOT_SHOP_ID=
YOOKASSA_BOT_SECRET_KEY=
YOOKASSA_BOT_RETURN_URL=
YOOKASSA_BOT_SBP_ENABLED=
YOOKASSA_BOT_RECURRENT_ENABLED=

YOOKASSA_CABINET_ENABLED=
YOOKASSA_CABINET_DISPLAY_NAME=
YOOKASSA_CABINET_SHOP_ID=
YOOKASSA_CABINET_SECRET_KEY=
YOOKASSA_CABINET_RETURN_URL=
YOOKASSA_CABINET_SBP_ENABLED=
YOOKASSA_CABINET_RECURRENT_ENABLED=
```

Старые env остаются:

```env
YOOKASSA_ENABLED=
YOOKASSA_DISPLAY_NAME=
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=
YOOKASSA_RETURN_URL=
YOOKASSA_SBP_ENABLED=
YOOKASSA_RECURRENT_ENABLED=
YOOKASSA_WEBHOOK_PATH=
```

Новые helper methods:

```python
settings.get_yookassa_config(scope: str) -> YooKassaConfig
settings.is_yookassa_enabled(scope: str | None = None) -> bool
settings.get_yookassa_return_url(scope: str | None = None) -> str
settings.get_yookassa_display_name(scope: str | None = None) -> str
settings.is_yookassa_sbp_enabled(scope: str | None = None) -> bool
settings.is_yookassa_recurrent_enabled(scope: str | None = None) -> bool
```

Fallback rule:

- Если `YOOKASSA_BOT_*` не задан, `bot` использует старый `YOOKASSA_*`.
- Если `YOOKASSA_CABINET_*` не задан, `cabinet` использует старый `YOOKASSA_*`.
- Если задан scoped `*_ENABLED=false`, этот scope выключен даже при включенном legacy `YOOKASSA_ENABLED`.

Так старые single-shop установки продолжают работать без изменения `.env`, а новые установки могут разделить Bot и Cabinet.

### 3. Почему не DB-based accounts в MVP

DB-based вариант:

```text
yookassa_accounts
  id
  scope
  display_name
  shop_id
  secret_key
  enabled
  sbp_enabled
  recurrent_enabled
  return_url
  default_receipt_email
  min_amount_kopeks
  max_amount_kopeks
```

Плюсы:

- удобно расширять до `landing`, `gift`, `promo`, `default`;
- можно управлять из админки;
- можно хранить account id вместо строкового scope в платежах.

Минусы для текущего MVP:

- нужно безопасное хранение `secret_key`, которого сейчас нет в `PaymentMethodConfig`;
- нужна миграция/админка для credentials;
- нужно решить аудит, маскирование и запрет логирования секретов;
- больше blast radius.

Рекомендация: сначала env-based scopes плюс nullable `yookassa_scope` в платежах. Когда потребуется больше двух магазинов или управление из админки, добавить `yookassa_accounts` и мигрировать `yookassa_scope -> yookassa_account_id`.

### 4. Создание платежей через scoped client

`PaymentService` не должен хранить один глобальный `self.yookassa_service`.

Предлагаемый интерфейс:

```python
await payment_service.create_yookassa_payment(..., scope="bot")
await payment_service.create_yookassa_sbp_payment(..., scope="cabinet")
await payment_service.get_yookassa_payment_status(..., scope_from_payment=True)
```

Внутри:

1. Получить scoped config.
2. Проверить `enabled`, `shop_id`, `secret_key`.
3. Создать платеж через scoped HTTP client.
4. Сохранить `yookassa_scope` в `YooKassaPayment`.
5. Добавить `yookassa_scope` в YooKassa metadata для отладки/recovery.

Bot flows должны передавать `scope='bot'`.

Cabinet flows должны передавать `scope='cabinet'`.

### 5. Direct HTTP вместо SDK global config

Рекомендуется заменить `app/services/yookassa_service.py` на scoped client поверх прямых HTTP-запросов YooKassa API:

- `POST /v3/payments` для card/SBP/autopayment;
- `GET /v3/payments/{payment_id}` для status check;
- Basic Auth на каждый request: `shop_id:secret_key`;
- `Idempotence-Key` на создание платежей;
- timeouts и executor больше не нужны, если используется async HTTP client.

Плюсы:

- credentials живут в request context, а не в global SDK state;
- нет гонки между Bot и Cabinet запросами;
- легче тестировать разные scopes;
- проще логировать без секретов.

SDK + lock можно оставить только если прямой HTTP слишком дорог для этапа 1:

```python
async with yookassa_sdk_lock:
    Configuration.configure(shop_id, secret_key)
    YooKassaPayment.create(...)
```

Но это хуже: один забытый SDK call без lock снова создаст race condition.

### 6. Как Cabinet показывает YooKassa

Для MVP не нужно заводить `yookassa_bot` и `yookassa_cabinet` как отдельные method id.

Оставить внешний method id:

```text
yookassa
```

Внутри resolver использует `surface='cabinet'`.

Рекомендуемый минимальный change:

- `PaymentMethodConfig` остается cabinet-facing таблицей.
- `get_enabled_methods_for_user()` получает или неявно использует `surface='cabinet'`.
- `_get_method_defaults()` для `yookassa` проверяет `settings.is_yookassa_enabled("cabinet")`.
- `sub_options.sbp.enabled` проверяет `settings.is_yookassa_sbp_enabled("cabinet")`.
- `POST /cabinet/balance/topup` принимает прежний `payment_method='yookassa'`, но создает платеж через `scope='cabinet'`.

Не рекомендуется для MVP:

- добавлять `surface` в `PaymentMethodConfig`, если таблица пока используется только Cabinet;
- заводить method ids `yookassa_bot` / `yookassa_cabinet`, потому что это ухудшит UX и расползется по frontend/types/admin.

Если позже та же таблица будет управлять Bot methods, тогда можно добавить `surface` и unique `(surface, method_id)`.

### 7. Как Bot показывает YooKassa

Bot keyboards/utilities должны проверять только `bot` scope:

- `settings.is_yookassa_enabled("bot")`;
- `settings.is_yookassa_sbp_enabled("bot")`;
- `settings.get_yookassa_display_name("bot")`;
- `settings.get_yookassa_min/max_amount_kopeks("bot")`.

Если Bot YooKassa выключена, бот не показывает YooKassa, даже если Cabinet YooKassa включена.

Если Cabinet YooKassa выключена, Cabinet не показывает YooKassa, даже если Bot YooKassa включена.

### 8. Webhook

Рекомендуемый MVP:

```text
POST /yookassa-webhook/bot
POST /yookassa-webhook/cabinet
POST /yookassa-webhook          # legacy fallback
```

Scoped path:

1. Router определяет `scope` из path.
2. Handler извлекает `object.id`.
3. Backend ищет локальный платеж по `(yookassa_scope, yookassa_payment_id)`.
4. Backend проверяет статус через credentials этого scope.
5. Если локальной записи нет, recovery создает ее со scope из path и metadata из webhook.

Legacy path:

1. Backend ищет платеж по `yookassa_payment_id`.
2. Если у платежа есть `yookassa_scope`, использует его.
3. Если scope пустой, использует legacy/default config из старых `YOOKASSA_*`.
4. Если локальной записи нет и scope неизвестен, recovery возможен только через legacy/default config. Поэтому для новых scoped магазинов лучше настраивать scoped webhook paths.

Почему два path лучше одного:

- при потерянной локальной записи понятно, какими credentials проверять платеж;
- проще отлаживать логи и настройки YooKassa;
- меньше риск проверить Cabinet payment Bot credentials и наоборот.

Один общий `/yookassa-webhook` можно оставить только как fallback/compatibility, но не как основной вариант для двух магазинов.

### 9. Manual status check

`get_yookassa_payment_status()` должен:

1. загрузить `YooKassaPayment`;
2. прочитать `yookassa_scope`;
3. если scope пустой, использовать legacy/default fallback;
4. проверить remote status через credentials этого scope;
5. обновить локальный payment и обработать success.

Нельзя выбирать credentials по месту вызова manual check. Например, Cabinet admin может проверять старый Bot платеж, и проверка должна идти через scope самого платежа.

### 10. Autopay and saved payment methods

`SavedPaymentMethod` должен хранить YooKassa scope, потому что `payment_method_id` привязан к магазину.

Правила MVP:

- `_save_payment_method_if_available()` сохраняет scope из успешного `YooKassaPayment`.
- `create_saved_payment_method()` uniqueness должен учитывать scope.
- Recurrent service заряжает saved card только credentials того scope, с которым card была сохранена.
- Для subscription autopay по умолчанию использовать `bot` scope, потому что текущие подписочные flows находятся в боте.
- Legacy saved cards без scope обрабатывать через legacy/default config.

Если в будущем Cabinet получит отдельные recurring products, нужно явно хранить preferred autopay scope на subscription/order level. До этого нельзя молча брать Cabinet saved card для Bot subscription, если это не согласовано продуктово.

## NaloGO

### Текущее состояние

YooKassa success создает чек NaloGO:

- `app/services/payment/yookassa.py:1299` - `_create_nalogo_receipt()`.
- `app/services/payment/yookassa.py:1345` - сохранение `transaction.receipt_uuid`.
- `app/services/nalogo_service.py:303` - `create_receipt()`.
- `app/services/nalogo_service.py:316` - dedup key `nalogo:created:{payment_id}`.
- `app/services/nalogo_queue_service.py:156` - queue uses `payment_id`.
- `app/services/guest_purchase_service.py:190` - guest purchase receipt.

Сейчас NaloGO глобальный.

### MVP-допущение

Для MVP можно оставить один глобальный NaloGO только при бизнес-допущении:

> Bot YooKassa и Cabinet YooKassa принадлежат одному налоговому субъекту, и чеки могут выпускаться через один NaloGO аккаунт.

Это нужно явно подтвердить перед production rollout.

Если магазины YooKassa принадлежат разным юрлицам/самозанятым, глобальный NaloGO неправильный. Тогда NaloGO тоже должен стать scoped:

- `NALOGO_BOT_*`;
- `NALOGO_CABINET_*`;
- или DB account, связанный с YooKassa account.

### Изменения NaloGO даже при глобальном аккаунте

Даже если NaloGO остается глобальным, dedup/queue/log context должны учитывать источник:

```text
nalogo:created:yookassa:{scope}:{payment_id}
nalogo:queued:yookassa:{scope}:{payment_id}
```

Минимальный интерфейс:

```python
create_receipt(
    ...,
    payment_provider="yookassa",
    payment_scope=payment.yookassa_scope or "legacy",
    external_payment_id=payment.yookassa_payment_id,
)
```

В `Transaction` желательно добавить:

- `payment_scope`;
- `payment_account_id` или будущий `payment_account_slug`;
- `receipt_uuid`.

Если не менять `Transaction` в MVP, минимум использовать `YooKassaPayment.yookassa_scope` при создании чека и Redis dedup keys.

## Миграции БД

### Обязательные для MVP

1. `yookassa_payments`

Добавить:

```text
yookassa_scope VARCHAR(32) NULL
```

Индекс:

```text
idx_yookassa_payments_scope_payment_id (yookassa_scope, yookassa_payment_id)
```

Старые строки можно оставить `NULL`. Runtime fallback будет считать `NULL` legacy/default. Массовый backfill в `bot` рискован, потому что часть старых платежей могла прийти из Cabinet.

2. `saved_payment_methods`

Добавить:

```text
yookassa_scope VARCHAR(32) NULL
```

Индекс:

```text
idx_saved_payment_methods_user_scope_active (user_id, yookassa_scope, is_active)
```

Пересмотреть uniqueness:

Текущий unique только на `yookassa_payment_method_id`. Для multi-scope лучше unique:

```text
uq_saved_payment_methods_scope_method (yookassa_scope, yookassa_payment_method_id)
```

Для legacy `NULL` нужно аккуратно учесть поведение конкретной БД по unique nullable columns. Если используется PostgreSQL, лучше сделать partial unique indexes:

```sql
CREATE UNIQUE INDEX uq_saved_pm_legacy_method
  ON saved_payment_methods (yookassa_payment_method_id)
  WHERE yookassa_scope IS NULL;

CREATE UNIQUE INDEX uq_saved_pm_scope_method
  ON saved_payment_methods (yookassa_scope, yookassa_payment_method_id)
  WHERE yookassa_scope IS NOT NULL;
```

3. `transactions` - желательно, но можно отложить

Добавить nullable поля:

```text
payment_scope VARCHAR(32) NULL
payment_account_id INTEGER NULL
```

Для MVP это полезно для админки, аудита и NaloGO. Если нужно минимизировать миграции, можно отложить и читать scope из `YooKassaPayment`.

### Не рекомендуется для MVP

Не добавлять `surface` в `payment_method_configs`, если таблица остается кабинетной.

Если позже нужно управлять методами Bot через ту же таблицу, отдельная миграция:

```text
payment_method_configs.surface VARCHAR(32) NOT NULL DEFAULT 'cabinet'
UNIQUE(surface, method_id)
```

### Future DB accounts

Когда понадобится больше scopes:

```text
yookassa_accounts
  id
  scope
  display_name
  shop_id
  secret_key_encrypted
  enabled
  sbp_enabled
  recurrent_enabled
  return_url
  default_receipt_email
  min_amount_kopeks
  max_amount_kopeks
  created_at
  updated_at
```

После этого:

- добавить `yookassa_account_id` в `yookassa_payments`;
- добавить `yookassa_account_id` в `saved_payment_methods`;
- `yookassa_scope` оставить как denormalized/debug field или убрать после миграции.

## Изменения API, Bot UI, Admin UI

### Backend API

`GET /cabinet/balance/payment-methods`:

- остается прежним по контракту;
- возвращает `yookassa` только если `settings.is_yookassa_enabled("cabinet")`;
- SBP option возвращается только если `settings.is_yookassa_sbp_enabled("cabinet")`.

`POST /cabinet/balance/topup`:

- принимает прежний `payment_method='yookassa'`;
- создает платеж через `scope='cabinet'`;
- сохраняет scope в локальный payment.

Saved cards endpoints:

- возвращают только карты нужного scope, если endpoint относится к Cabinet;
- для legacy cards без scope можно показывать только если active config использует legacy/default.

Webhook:

- добавить scoped paths;
- старый path оставить.

### Bot UI

Bot keyboards не меняют UX:

- пользователю показывается `YooKassa`/display name;
- технический scope не показывается;
- Bot видит YooKassa только по `YOOKASSA_BOT_*` или legacy fallback.

### Cabinet Admin UI

Для MVP можно оставить один row `yookassa` в Payment Methods admin. Он означает Cabinet YooKassa.

Что нужно поменять в backend admin response:

- `is_provider_configured` для `yookassa` должен проверять Cabinet scope;
- display/sub-options должны брать Cabinet scope.

Если нужен админский UI для Bot YooKassa, это отдельный экран/settings section, а не второй method id в Cabinet payment methods.

## Backward compatibility

Обязательные правила:

- Старые `YOOKASSA_*` продолжают работать без изменения `.env`.
- Если scoped env не задан, `bot` и `cabinet` используют legacy config.
- Старые `YooKassaPayment.yookassa_scope IS NULL` проверяются через legacy/default config.
- Старые `SavedPaymentMethod.yookassa_scope IS NULL` заряжаются через legacy/default config.
- Старый `/yookassa-webhook` остается зарегистрированным.
- Внешний method id остается `yookassa`.
- Single-shop installation не требует настройки multi-shop.

Важно: если оператор хочет разделить только Cabinet, он может задать `YOOKASSA_CABINET_*`, а Bot продолжит работать через legacy `YOOKASSA_*`.

## Риски

### YooKassa SDK global config

Риск: параллельные запросы Bot и Cabinet могут использовать не те credentials.

Митигировать: direct HTTP client with per-request Basic Auth. Не логировать `secret_key`.

### Webhook races

Риск: YooKassa webhook и manual check одновременно обрабатывают один платеж.

Текущий код уже использует transaction/lock в success path и проверяет duplicate transaction по `external_id/payment_method`. Scope нужно добавить в lookup/dedup context, но старый unique constraint ломать нельзя без отдельного анализа.

### Duplicate payment id между магазинами

YooKassa payment id скорее всего практически глобален, но на это не нужно опираться.

Митигировать:

- хранить `yookassa_scope`;
- искать scoped платежи по `(scope, yookassa_payment_id)`;
- NaloGO keys делать scoped.

### Autopay saved cards

Риск: списать card token через другой магазин.

Митигировать:

- `SavedPaymentMethod.yookassa_scope`;
- recurrent charge только через scope saved method;
- не смешивать Bot и Cabinet saved cards без явного product decision.

### NaloGO legal entity mismatch

Риск: чек выпускается не тем налоговым субъектом.

Митигировать:

- MVP допускает global NaloGO только если оба YooKassa магазина принадлежат одному налоговому субъекту;
- иначе добавить scoped NaloGO credentials.

### Compatibility старых платежей

Риск: старые строки без scope перестают проверяться.

Митигировать:

- nullable scope;
- legacy fallback;
- старый webhook path;
- тесты на старый env-only config.

## План реализации

### Этап 1. Config и compatibility

- Добавить scoped config model/helper methods в `app/config.py`.
- Оставить старые `YOOKASSA_*` как fallback.
- Покрыть unit tests для `bot`, `cabinet`, legacy fallback и explicit disabled scoped config.

Verify:

- legacy `.env` включает YooKassa в обоих scopes;
- `YOOKASSA_CABINET_ENABLED=false` выключает только Cabinet;
- `YOOKASSA_BOT_ENABLED=false` выключает только Bot.

### Этап 2. DB model/CRUD scope

- Добавить `yookassa_scope` в `YooKassaPayment`.
- Добавить `yookassa_scope` в `SavedPaymentMethod`.
- Обновить CRUD create/get/list.
- Подготовить uniqueness saved methods с учетом scope.

Verify:

- старые записи с `NULL` читаются;
- новые платежи сохраняют scope;
- saved card одного scope не используется другим scope.

### Этап 3. Scoped YooKassa client и создание платежей

- Заменить SDK global calls на direct HTTP client или временно обернуть SDK lock.
- Обновить `create_yookassa_payment()` и `create_yookassa_sbp_payment()` на `scope`.
- Проставить `scope='bot'` в bot handlers.
- Проставить `scope='cabinet'` в Cabinet routes.
- Обновить `PaymentMethodConfig` defaults для Cabinet scope.

Verify:

- Bot создает платеж с Bot shop credentials.
- Cabinet создает платеж с Cabinet shop credentials.
- Cabinet methods не показывают YooKassa, если включен только Bot scope.
- Bot keyboard не показывает YooKassa, если включен только Cabinet scope.

### Этап 4. Webhook/status check

- Добавить `/yookassa-webhook/bot` и `/yookassa-webhook/cabinet`.
- Старый `/yookassa-webhook` оставить.
- Передавать scope в `process_yookassa_webhook()`.
- Manual check выбирать credentials из local payment scope.
- Recovery missing payment использовать path scope.

Verify:

- webhook `/bot` проверяет Bot credentials;
- webhook `/cabinet` проверяет Cabinet credentials;
- legacy webhook работает со старым платежом без scope;
- manual check старого платежа без scope работает.

### Этап 5. Autopay/saved payment methods

- Сохранять scope вместе с YooKassa saved payment method.
- Recurrent service выбирать scoped credentials из saved method.
- Для subscription autopay default scope = `bot`.
- Legacy saved methods без scope использовать через legacy/default config.

Verify:

- Bot saved card списывается только Bot credentials;
- Cabinet saved card не используется для Bot subscription без явного правила;
- legacy saved card продолжает работать.

### Этап 6. NaloGO scope context

- Прокинуть provider/scope/external payment id в NaloGO receipt creation.
- Обновить Redis dedup keys:
  - `nalogo:created:yookassa:{scope}:{payment_id}`;
  - `nalogo:queued:yookassa:{scope}:{payment_id}`.
- Добавить scope в logs без secret data.

Verify:

- Bot и Cabinet платежи имеют разные NaloGO dedup keys;
- retry queue не склеивает платежи разных scopes;
- старые queued receipts можно обработать legacy fallback.

### Этап 7. Admin/frontend/tests

- Cabinet frontend contract оставить прежним.
- Admin Payment Methods row `yookassa` проверяет Cabinet provider config.
- Добавить integration tests на `/cabinet/balance/payment-methods`, `/cabinet/balance/topup`, bot keyboard methods, webhook paths и manual status.

Verify:

- пользователь видит обычный `YooKassa`, не `yookassa_bot`;
- админ не обязан настраивать multi-shop для single-shop installation;
- secrets не попадают в logs, API response и git.

## Конкретные файлы для реализации

Backend:

- `app/config.py`
- `app/services/yookassa_service.py`
- `app/services/payment/yookassa.py`
- `app/services/payment_service.py`
- `app/services/payment_method_config_service.py`
- `app/services/payment_verification_service.py`
- `app/services/recurrent_payment_service.py`
- `app/services/nalogo_service.py`
- `app/services/nalogo_queue_service.py`
- `app/services/guest_purchase_service.py`
- `app/webserver/payments.py`
- `app/external/yookassa_webhook.py`
- `app/database/models.py`
- `app/database/crud/yookassa.py`
- `app/database/crud/saved_payment_method.py`
- `migrations/alembic/versions/*`

Bot flows:

- `app/handlers/balance/yookassa.py`
- `app/handlers/subscription/purchase.py`
- `app/handlers/simple_subscription.py`
- `app/keyboards/inline.py`
- `app/utils/payment_utils.py`
- `app/handlers/admin/bot_configuration.py`

Cabinet/API flows:

- `app/cabinet/routes/balance.py`
- `app/cabinet/routes/admin_payment_methods.py`
- `app/cabinet/routes/admin_payments.py`
- `app/webapi/routes/miniapp.py`
- `app/cabinet/routes/landing.py`
- `app/cabinet/routes/gift.py`

Cabinet frontend, если потребуется только текст/типы:

- `bedolaga-cabinet/src/api/balance.ts`
- `bedolaga-cabinet/src/types/index.ts`
- `bedolaga-cabinet/src/api/adminPaymentMethods.ts`

Для MVP frontend изменений, скорее всего, не нужно: backend продолжает возвращать method id `yookassa`.
