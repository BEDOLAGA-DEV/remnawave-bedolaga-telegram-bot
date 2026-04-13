# External Gateway — Универсальный внешний платёжный шлюз

Модуль для интеграции любого внешнего платёжного шлюза с Telegram-ботом Bedolaga.
Позволяет подключить произвольный шлюз (Stripe, PayPal, собственный paygate и т.д.)
через стандартный HTTP API.

---

## Содержание

1. [Обзор](#обзор)
2. [Архитектура](#архитектура)
3. [API-контракт](#api-контракт)
4. [Настройка (.env)](#настройка-env)
5. [Поток оплаты](#поток-оплаты)
6. [Структура файлов](#структура-файлов)
7. [База данных](#база-данных)
8. [Запуск и проверка](#запуск-и-проверка)
9. [Настройка шлюза (Paygate)](#настройка-шлюза-paygate)
10. [Сетевая конфигурация](#сетевая-конфигурация)
11. [Безопасность](#безопасность)
12. [Администрирование](#администрирование)
13. [Отладка и логи](#отладка-и-логи)
14. [FAQ](#faq)

---

## Обзор

### Что это

External Gateway — это универсальный платёжный метод, который работает как прокси
между Telegram-ботом и любым внешним платёжным шлюзом. Шлюз должен реализовывать
три эндпоинта:

| Эндпоинт | Метод | Описание |
|----------|-------|----------|
| `/create` | POST | Создание платежа, возврат ссылки на оплату |
| `/status` | GET | Проверка статуса платежа |
| `/callback` | POST | Уведомление бота об успешной оплате |

### Что поддерживает

- Любая сумма в настраиваемом диапазоне (по умолчанию 100 — 100 000 руб.)
- Автоматическое зачисление баланса по callback
- Ручная проверка статуса кнопкой "Проверить оплату"
- Идемпотентная обработка (дублирующие callback не вызывают двойного зачисления)
- Реферальные начисления
- Уведомления админам и пользователю
- Гостевые покупки (guest purchase)
- Корзина и автопокупка подписки после пополнения

---

## Архитектура

```
┌────────────────────────────────────────────────────────────────────┐
│                       Telegram-бот (Bedolaga)                      │
│                                                                    │
│  ┌──────────────┐  ┌─────────────────┐  ┌──────────────────────┐  │
│  │  Handler      │  │  PaymentService │  │  ExternalGateway     │  │
│  │  (Telegram UI)│→ │  (mixin)        │→ │  Service (HTTP)      │  │
│  └──────────────┘  └─────────────────┘  └──────────┬───────────┘  │
│                                                     │              │
│  ┌──────────────┐  ┌─────────────────┐              │              │
│  │  CRUD         │  │  DB:            │              │              │
│  │  (operations) │← │  external_      │              │              │
│  │              │  │  gateway_       │              │              │
│  │              │  │  payments       │              │              │
│  └──────────────┘  └─────────────────┘              │              │
│                                                     │              │
│  ┌──────────────┐                                   │              │
│  │  FastAPI      │  ← callback (X-Webhook-Secret)   │              │
│  │  Webhook      │                                   │              │
│  └──────────────┘                                   │              │
└─────────────────────────────────────────────────────┼──────────────┘
                                                      │
                                           POST /create.php
                                           GET  /status.php
                                                      │
                                                      ▼
                                         ┌──────────────────────┐
                                         │  Внешний шлюз        │
                                         │  (paygate, любой)    │
                                         │                      │
                                         │  Stripe / PayPal /   │
                                         │  другой провайдер    │
                                         └──────────────────────┘
```

### Компоненты

| Компонент | Файл | Ответственность |
|-----------|------|-----------------|
| HTTP-клиент | `app/services/external_gateway_service.py` | Отправка запросов к шлюзу |
| Mixin | `app/services/payment/external_gateway.py` | Бизнес-логика платежей |
| CRUD | `app/database/crud/external_gateway.py` | Операции с БД |
| Handler | `app/handlers/balance/external_gateway.py` | Telegram UI |
| Webhook | `app/webserver/payments.py` | Приём callback от шлюза |
| Модель | `app/database/models.py` | `ExternalGatewayPayment` |
| Конфиг | `app/config.py` | Переменные окружения |

---

## API-контракт

### 1. Создание платежа (Бот → Шлюз)

```http
POST {EXTERNAL_GATEWAY_URL}{CREATE_PATH}
Headers:
  X-Api-Key: {EXTERNAL_GATEWAY_API_KEY}
  Content-Type: application/json

Body:
{
  "amount": 500.00,
  "currency": "RUB",
  "method": "stripe",                         // опционально
  "order_id": "bg_42_1710000000_a1b2c3d4",    // уникальный ID от бота
  "callback_url": "http://bot:8090/ext-gateway-callback",
  "product_name": "Пополнение баланса",
  "return_url": "https://t.me/mybot",         // опционально
  "metadata": {                               // опционально
    "user_id": 42,
    "amount_kopeks": 50000,
    "type": "balance_topup"
  }
}

Response (200 OK):
{
  "success": true,
  "order_id": 123,                             // ID заказа на стороне шлюза
  "redirect_url": "https://checkout.stripe.com/pay/cs_xxx"
}

Response (ошибка):
{
  "success": false,
  "error": "Invalid amount"
}
```

**Поля запроса:**

| Поле | Тип | Обязательное | Описание |
|------|-----|:---:|----------|
| `amount` | float | да | Сумма в основных единицах валюты (рубли, доллары) |
| `currency` | string | да | Код валюты (RUB, USD и т.д.) |
| `order_id` | string | да | Уникальный ID заказа, сгенерированный ботом |
| `callback_url` | string | да | URL для callback при успешной оплате |
| `product_name` | string | да | Описание платежа |
| `method` | string | нет | Метод оплаты (stripe, paypal). Если пусто — шлюз решает сам |
| `return_url` | string | нет | URL возврата пользователя после оплаты |
| `metadata` | object | нет | Произвольные метаданные |

### 2. Callback при успешной оплате (Шлюз → Бот)

```http
POST {EXTERNAL_GATEWAY_WEBHOOK_PATH}
Headers:
  X-Webhook-Secret: {EXTERNAL_GATEWAY_WEBHOOK_SECRET}
  Content-Type: application/json

Body:
{
  "order_id": 123,                              // ID заказа на стороне шлюза
  "external_order_id": "bg_42_1710000000_a1b2c3d4",  // ID от бота (order_id из create)
  "status": "completed",                        // статус: completed, failed, expired
  "method": "stripe",                           // метод, которым оплатил пользователь
  "amount": 500.00,                             // сумма в исходной валюте
  "currency": "RUB",
  "amount_converted": 5.50,                     // сумма после конвертации (опционально)
  "payment_id": "pi_3abc..."                    // ID платежа у провайдера (опционально)
}
```

**Поля callback:**

| Поле | Тип | Обязательное | Описание |
|------|-----|:---:|----------|
| `external_order_id` | string | да | ID заказа от бота (поле `order_id` из create-запроса) |
| `status` | string | да | Статус: `completed`, `failed`, `expired` |
| `order_id` | int/string | нет | ID заказа на стороне шлюза |
| `method` | string | нет | Метод оплаты |
| `amount` | float | нет | Сумма |
| `currency` | string | нет | Валюта |
| `amount_converted` | float | нет | Конвертированная сумма |
| `payment_id` | string | нет | ID провайдера (Stripe PI, PayPal ID и т.д.) |

**Ответ бота:**
```json
{"success": true}   // платёж обработан
{"success": false}  // ошибка обработки
```

### 3. Проверка статуса (Бот → Шлюз)

```http
GET {EXTERNAL_GATEWAY_URL}{STATUS_PATH}?order_id=bg_42_1710000000_a1b2c3d4
Headers:
  X-Api-Key: {EXTERNAL_GATEWAY_API_KEY}

Response (200 OK):
{
  "success": true,
  "status": "completed",
  "order_id": 123,
  "amount": 500.00,
  "currency": "RUB"
}
```

---

## Настройка (.env)

Добавьте в файл `.env` следующие переменные:

```env
# ===== EXTERNAL GATEWAY (Универсальный внешний шлюз) =====

# Включить/выключить метод оплаты
EXTERNAL_GATEWAY_ENABLED=true

# Базовый URL шлюза (без trailing slash)
EXTERNAL_GATEWAY_URL=https://pay.example.com

# API-ключ для авторизации запросов (заголовок X-Api-Key)
EXTERNAL_GATEWAY_API_KEY=your_api_key

# Секрет для проверки callback (заголовок X-Webhook-Secret)
EXTERNAL_GATEWAY_WEBHOOK_SECRET=your_webhook_secret

# Название кнопки в Telegram
EXTERNAL_GATEWAY_DISPLAY_NAME=Оплата картой

# Эмодзи перед названием
EXTERNAL_GATEWAY_DISPLAY_EMOJI=💳

# Валюта
EXTERNAL_GATEWAY_CURRENCY=RUB

# Лимиты (в копейках: 10000 = 100₽, 10000000 = 100000₽)
EXTERNAL_GATEWAY_MIN_AMOUNT_KOPEKS=10000
EXTERNAL_GATEWAY_MAX_AMOUNT_KOPEKS=10000000

# Пути API шлюза (относительно EXTERNAL_GATEWAY_URL)
EXTERNAL_GATEWAY_CREATE_PATH=/create.php
EXTERNAL_GATEWAY_STATUS_PATH=/status.php

# Вебхук: путь, хост и порт для приёма callback от шлюза
EXTERNAL_GATEWAY_WEBHOOK_PATH=/ext-gateway-callback
EXTERNAL_GATEWAY_WEBHOOK_HOST=0.0.0.0
EXTERNAL_GATEWAY_WEBHOOK_PORT=8090

# URL возврата после оплаты (опционально)
EXTERNAL_GATEWAY_RETURN_URL=https://t.me/YourBotName

# Таймаут оплаты (секунды)
EXTERNAL_GATEWAY_PAYMENT_TIMEOUT_SECONDS=3600

# Метод оплаты (stripe/paypal/пусто = шлюз сам выбирает)
EXTERNAL_GATEWAY_PAYMENT_METHOD=
```

### Минимальная конфигурация

Для запуска достаточно 4 переменных:

```env
EXTERNAL_GATEWAY_ENABLED=true
EXTERNAL_GATEWAY_URL=https://your-gateway.com
EXTERNAL_GATEWAY_API_KEY=your_api_key
EXTERNAL_GATEWAY_WEBHOOK_SECRET=your_secret
```

Остальные параметры имеют значения по умолчанию.

### Условия активации

Метод оплаты активен, когда ВСЕ условия выполнены:
1. `EXTERNAL_GATEWAY_ENABLED=true`
2. `EXTERNAL_GATEWAY_URL` не пустой
3. `EXTERNAL_GATEWAY_API_KEY` не пустой

Проверяется методом `settings.is_external_gateway_enabled()`.

---

## Поток оплаты

### Сценарий 1: Автоматическое зачисление через callback

```
 Пользователь                Бот                   Шлюз                Провайдер
      │                       │                      │                      │
      │  /topup               │                      │                      │
      │──────────────────────>│                      │                      │
      │                       │                      │                      │
      │  Выбирает "💳 Оплата │                      │                      │
      │  картой"              │                      │                      │
      │──────────────────────>│                      │                      │
      │                       │                      │                      │
      │  Вводит "500"         │                      │                      │
      │──────────────────────>│                      │                      │
      │                       │                      │                      │
      │                       │  POST /create.php    │                      │
      │                       │  {amount: 500, ...}  │                      │
      │                       │─────────────────────>│                      │
      │                       │                      │                      │
      │                       │  {redirect_url: ...} │                      │
      │                       │<─────────────────────│                      │
      │                       │                      │                      │
      │  Кнопка "Оплатить"   │                      │                      │
      │  + "Проверить оплату" │                      │                      │
      │<──────────────────────│                      │                      │
      │                       │                      │                      │
      │  Переходит по ссылке  │                      │                      │
      │─────────────────────────────────────────────────────────────────>  │
      │                       │                      │                      │
      │  Оплачивает           │                      │                      │
      │─────────────────────────────────────────────────────────────────>  │
      │                       │                      │                      │
      │                       │                      │  Webhook от          │
      │                       │                      │  провайдера          │
      │                       │                      │<─────────────────────│
      │                       │                      │                      │
      │                       │  POST /ext-gateway-  │                      │
      │                       │  callback            │                      │
      │                       │  {status: completed} │                      │
      │                       │<─────────────────────│                      │
      │                       │                      │                      │
      │                       │  ✅ Создаёт          │                      │
      │                       │  транзакцию,         │                      │
      │                       │  начисляет баланс    │                      │
      │                       │                      │                      │
      │  "✅ Пополнение       │                      │                      │
      │  успешно! 500₽"       │                      │                      │
      │<──────────────────────│                      │                      │
```

### Сценарий 2: Ручная проверка кнопкой

Если callback не пришёл (проблемы с сетью, задержка), пользователь нажимает
кнопку "Проверить оплату":

```
 Пользователь                Бот                   Шлюз
      │                       │                      │
      │  "🔄 Проверить        │                      │
      │  оплату"              │                      │
      │──────────────────────>│                      │
      │                       │                      │
      │                       │  GET /status.php     │
      │                       │  ?order_id=bg_42_... │
      │                       │─────────────────────>│
      │                       │                      │
      │                       │  {status: completed} │
      │                       │<─────────────────────│
      │                       │                      │
      │                       │  ✅ Финализация      │
      │                       │  (транзакция, баланс)│
      │                       │                      │
      │  "✅ Платёж уже       │                      │
      │  обработан!"          │                      │
      │<──────────────────────│                      │
```

### Сценарий 3: Платёж не завершён

```
 Пользователь                Бот
      │                       │
      │  "🔄 Проверить        │
      │  оплату"              │
      │──────────────────────>│
      │                       │
      │  "⏳ Платёж ещё не    │
      │  оплачен."            │
      │<──────────────────────│
```

---

## Структура файлов

### Созданные файлы

```
app/
├── services/
│   ├── external_gateway_service.py          # HTTP-клиент (~115 строк)
│   └── payment/
│       └── external_gateway.py              # Mixin бизнес-логики (~425 строк)
├── database/
│   └── crud/
│       └── external_gateway.py              # CRUD операции (~120 строк)
├── handlers/
│   └── balance/
│       └── external_gateway.py              # Telegram UI (~310 строк)
migrations/
└── alembic/
    └── versions/
        └── 0039_add_external_gateway_payments.py  # Миграция (~40 строк)
```

### Изменённые файлы

```
app/
├── config.py                                # +17 env-переменных, +3 метода
├── database/
│   └── models.py                            # +1 enum, +1 модель
├── services/
│   ├── payment/__init__.py                  # +1 импорт
│   ├── payment_service.py                   # +1 mixin, +init
│   └── payment_method_config_service.py     # +1 метод в defaults, +1 в order
├── handlers/
│   └── balance/
│       └── main.py                          # +routing, +handler registration
├── keyboards/
│   └── inline.py                            # +1 кнопка
└── webserver/
    └── payments.py                          # +1 webhook endpoint
.env.example                                 # +секция EXTERNAL GATEWAY
```

---

## База данных

### Таблица `external_gateway_payments`

Создаётся миграцией `0039_add_external_gateway_payments.py`.

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | INTEGER PK | Внутренний ID |
| `user_id` | INTEGER FK → users.id | ID пользователя |
| `order_id` | VARCHAR(128) UNIQUE | ID заказа от бота (`bg_42_17100...`) |
| `gateway_order_id` | VARCHAR(128) | ID заказа на стороне шлюза |
| `gateway_payment_id` | VARCHAR(255) | ID платежа у провайдера (Stripe PI и т.д.) |
| `amount_kopeks` | INTEGER | Сумма в копейках |
| `currency` | VARCHAR(10) | Валюта (RUB) |
| `amount_converted` | FLOAT | Конвертированная сумма (USD и т.д.) |
| `payment_method_name` | VARCHAR(64) | Название метода (stripe, paypal) |
| `description` | TEXT | Описание платежа |
| `status` | VARCHAR(32) | Статус: pending / completed / failed / expired |
| `is_paid` | BOOLEAN | Флаг оплаты |
| `redirect_url` | TEXT | Ссылка на оплату |
| `metadata_json` | JSON | Метаданные |
| `callback_payload` | JSON | Тело callback от шлюза |
| `transaction_id` | INTEGER FK → transactions.id | Связь с транзакцией |
| `paid_at` | TIMESTAMP | Время оплаты |
| `created_at` | TIMESTAMP | Время создания |
| `updated_at` | TIMESTAMP | Время обновления |

### Формат order_id

Бот генерирует ID в формате:
```
bg_{user_id}_{unix_timestamp}_{random_hex_8}
```
Пример: `bg_42_1710000000_a1b2c3d4`

---

## Запуск и проверка

### Шаг 1: Применить миграцию

```bash
alembic upgrade head
```

### Шаг 2: Настроить .env

```env
EXTERNAL_GATEWAY_ENABLED=true
EXTERNAL_GATEWAY_URL=https://pay.example.com
EXTERNAL_GATEWAY_API_KEY=your_api_key
EXTERNAL_GATEWAY_WEBHOOK_SECRET=your_secret
```

### Шаг 3: Открыть порт для callback

Убедитесь, что порт `8090` (или ваш `EXTERNAL_GATEWAY_WEBHOOK_PORT`) доступен
для входящих запросов от шлюза. В Docker Compose добавьте:

```yaml
services:
  bot:
    ports:
      - "8090:8090"
```

### Шаг 4: Запустить бота

```bash
docker compose up -d
```

### Шаг 5: Проверка

1. Откройте бота в Telegram
2. Нажмите кнопку пополнения баланса
3. В списке методов должна появиться кнопка "💳 Оплата картой"
4. Нажмите на кнопку и введите сумму
5. Нажмите "Оплатить" — откроется страница оплаты шлюза
6. Оплатите (в sandbox режиме — тестовой картой)
7. Дождитесь callback (баланс начислится автоматически) или нажмите "Проверить оплату"

---

## Требования к шлюзу

Любой внешний шлюз, реализующий 3 эндпоинта, совместим с этим модулем.

### Минимальный API шлюза

| Эндпоинт | Метод | Вход | Выход |
|----------|-------|------|-------|
| `/create` | POST | `amount`, `currency`, `order_id`, `callback_url` | `success`, `redirect_url`, `order_id` |
| `/status` | GET | `order_id` (query param) | `success`, `status`, `order_id` |
| Callback | POST → бот | `external_order_id`, `status` | `success` |

### Примеры совместимых шлюзов

- Собственный PHP/Python-скрипт-обёртка над Stripe, PayPal, LiqPay и т.д.
- Любой платёжный агрегатор с REST API (при наличии прослойки-адаптера)
- Мульти-тенантные платёжные платформы с поддержкой webhook

---

## Сетевая конфигурация

### Типичная схема

```
Интернет
    │
    ▼
┌────────────────────────┐
│  Nginx (reverse proxy) │  ← SSL, домен
│  pay.example.com       │
└───────────┬────────────┘
            │
    ┌───────┴───────┐
    ▼               ▼
┌────────┐   ┌───────────┐
│ Шлюз   │   │ Бот       │
│ (PHP)  │   │ (Python)  │
│ :80    │   │ :8090     │
└────────┘   └───────────┘
```

### Callback URL

Шлюз отправляет callback на URL, который бот передаёт в поле `callback_url`
при создании платежа. Формат:

```
http://{EXTERNAL_GATEWAY_WEBHOOK_HOST}:{EXTERNAL_GATEWAY_WEBHOOK_PORT}{EXTERNAL_GATEWAY_WEBHOOK_PATH}
```

По умолчанию: `http://0.0.0.0:8090/ext-gateway-callback`

**Важно:** Если бот и шлюз работают на разных серверах, замените `0.0.0.0`
на реальный адрес/домен бота, доступный для шлюза.

Для продакшена рекомендуется настроить reverse proxy с SSL:
```
https://bot.example.com/ext-gateway-callback
```

И в `.env`:
```env
EXTERNAL_GATEWAY_WEBHOOK_HOST=bot.example.com
EXTERNAL_GATEWAY_WEBHOOK_PORT=443
```

---

## Безопасность

### Аутентификация запросов (Бот → Шлюз)

Каждый запрос от бота к шлюзу содержит заголовок:
```
X-Api-Key: {EXTERNAL_GATEWAY_API_KEY}
```

Шлюз ДОЛЖЕН проверять этот ключ и отклонять запросы без валидного ключа.

### Верификация callback (Шлюз → Бот)

Каждый callback от шлюза должен содержать заголовок:
```
X-Webhook-Secret: {EXTERNAL_GATEWAY_WEBHOOK_SECRET}
```

Бот проверяет секрет через `hmac.compare_digest()` — это защищает от
timing-атак. Запросы с невалидным секретом отклоняются с кодом 401.

### Идемпотентность

- Повторный callback с тем же `external_order_id` и `status: completed`
  не вызывает двойного зачисления — бот проверяет `is_paid` перед обработкой
- Блокировка `SELECT ... FOR UPDATE` предотвращает race condition при
  одновременных callback и status check

### Рекомендации

1. Используйте HTTPS для всех коммуникаций
2. Генерируйте длинные случайные строки для API-ключа и webhook-секрета
3. Ограничьте доступ к порту callback (firewall: только IP шлюза)
4. Регулярно ротируйте ключи

---

## Администрирование

### Управление через админ-панель бота

Метод `external_gateway` автоматически появляется в разделе управления
платёжными методами (`PaymentMethodConfig`). Через админ-панель можно:

- Включить/выключить метод
- Изменить отображаемое название
- Изменить порядок сортировки среди других методов
- Настроить минимальную и максимальную сумму
- Фильтровать по типу пользователя (Telegram / email / все)
- Фильтровать по первому пополнению (да / нет / любое)
- Фильтровать по промо-группам

### SQL-запросы для диагностики

Последние 10 платежей:
```sql
SELECT id, order_id, amount_kopeks/100 as rub, status, is_paid, created_at
FROM external_gateway_payments
ORDER BY created_at DESC
LIMIT 10;
```

Незавершённые платежи:
```sql
SELECT id, order_id, user_id, amount_kopeks/100 as rub, created_at
FROM external_gateway_payments
WHERE status = 'pending' AND is_paid = false
ORDER BY created_at DESC;
```

Статистика по дням:
```sql
SELECT
  DATE(created_at) as day,
  COUNT(*) as total,
  SUM(CASE WHEN is_paid THEN 1 ELSE 0 END) as paid,
  SUM(CASE WHEN is_paid THEN amount_kopeks ELSE 0 END)/100 as sum_rub
FROM external_gateway_payments
GROUP BY DATE(created_at)
ORDER BY day DESC
LIMIT 30;
```

---

## Отладка и логи

### Структурированные логи

Все операции логируются через `structlog`. Ключевые события:

| Событие | Уровень | Когда |
|---------|---------|-------|
| `External Gateway: создан платёж` | INFO | Платёж создан в шлюзе и БД |
| `External Gateway callback: платёж не найден` | WARNING | Callback с неизвестным order_id |
| `External Gateway callback: платёж уже обработан` | INFO | Повторный callback (норма) |
| `External Gateway: не удалось заблокировать платёж` | ERROR | Ошибка блокировки в БД |
| `✅ Обработан External Gateway платёж` | INFO | Успешное зачисление |
| `External gateway create_payment error` | ERROR | Ошибка ответа шлюза |
| `External Gateway webhook: invalid secret` | WARNING | Невалидный секрет |

### Фильтрация логов

```bash
# Все логи External Gateway
docker logs bot 2>&1 | grep -i "external.gateway"

# Только ошибки
docker logs bot 2>&1 | grep -i "external.gateway" | grep -i "error"

# Callback-логи
docker logs bot 2>&1 | grep -i "ext.*gateway.*callback"
```

### Тестирование callback вручную

```bash
curl -X POST http://localhost:8090/ext-gateway-callback \
  -H "X-Webhook-Secret: your_secret" \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": 123,
    "external_order_id": "bg_42_1710000000_a1b2c3d4",
    "status": "completed",
    "method": "stripe",
    "amount": 500.00,
    "currency": "RUB",
    "payment_id": "pi_test123"
  }'
```

### Тестирование проверки статуса

```bash
curl -X GET "https://pay.example.com/status.php?order_id=bg_42_1710000000_a1b2c3d4" \
  -H "X-Api-Key: your_api_key"
```

---

## FAQ

### Как изменить название кнопки?

Измените `EXTERNAL_GATEWAY_DISPLAY_NAME` и `EXTERNAL_GATEWAY_DISPLAY_EMOJI` в `.env`:
```env
EXTERNAL_GATEWAY_DISPLAY_NAME=Stripe / PayPal
EXTERNAL_GATEWAY_DISPLAY_EMOJI=🌐
```

### Как подключить шлюз с другими путями API?

Шлюз должен реализовывать три эндпоинта (create, status, callback)
с совместимым форматом. Измените пути API:
```env
EXTERNAL_GATEWAY_CREATE_PATH=/api/v1/payments
EXTERNAL_GATEWAY_STATUS_PATH=/api/v1/payments/status
```

### Можно ли подключить несколько шлюзов?

В текущей версии поддерживается один экземпляр. Для нескольких шлюзов
нужно создать дополнительные методы (или использовать единый мульти-тенантный
шлюз-агрегатор).

### Что если callback не пришёл?

Пользователь может нажать кнопку "Проверить оплату". Бот запросит статус
через GET /status.php и, если платёж completed, автоматически зачислит баланс.

### Как работает конвертация валют?

Бот передаёт сумму в RUB. Шлюз может конвертировать в USD для Stripe/PayPal
и вернуть `amount_converted` в callback. Это значение сохраняется в БД
для учёта, но баланс начисляется в исходных копейках.

### Безопасно ли хранить API-ключ в .env?

Да, `.env` не коммитится в git (есть в `.gitignore`). Для продакшена
рекомендуется использовать Docker secrets или vault.

### Какой порт нужно открыть?

По умолчанию `8090` (настраивается через `EXTERNAL_GATEWAY_WEBHOOK_PORT`).
Этот порт должен быть доступен для HTTP-запросов от сервера шлюза.
