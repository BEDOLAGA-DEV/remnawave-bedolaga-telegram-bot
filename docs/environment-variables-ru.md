# 📘 Полная документация по переменным окружения

Данная документация содержит детальное описание всех переменных окружения (environment variables), используемых в Telegram-боте Remnawave Bedolaga для управления VPN-подписками.

## 📑 Содержание

- [Telegram бот](#telegram-бот)
- [Система поддержки](#система-поддержки)
- [Личный кабинет (Cabinet)](#личный-кабинет-cabinet)
- [OAuth 2.0 провайдеры](#oauth-20-провайдеры)
- [SMTP настройки](#smtp-настройки)
- [Уведомления администраторов](#уведомления-администраторов)
- [Обязательная подписка на канал](#обязательная-подписка-на-канал)
- [База данных](#база-данных)
- [Redis](#redis)
- [RemnaWave API](#remnawave-api)
- [Webhooks от RemnaWave](#webhooks-от-remnawave)
- [Подписки и продажи](#подписки-и-продажи)
- [Пробная подписка (Trial)](#пробная-подписка-trial)
- [Настройки трафика](#настройки-трафика)
- [Настройки устройств](#настройки-устройств)
- [Тарифы и цены](#тарифы-и-цены)
- [Скидки](#скидки)
- [Модем](#модем)
- [Реферальная система](#реферальная-система)
- [Автопродление](#автопродление)
- [Платёжные системы](#платёжные-системы)
  - [Telegram Stars](#telegram-stars)
  - [Tribute](#tribute)
  - [YooKassa](#yookassa)
  - [NaloGO (чеки)](#nalogo-чеки)
  - [CryptoBot](#cryptobot)
  - [Heleket](#heleket)
  - [MulenPay](#mulenpay)
  - [PAL24 (PayPalych)](#pal24-paypalych)
  - [Platega](#platega)
  - [Wata](#wata)
  - [CloudPayments](#cloudpayments)
  - [Freekassa](#freekassa)
  - [KassaAI](#kassaai)
- [Мониторинг трафика](#мониторинг-трафика)
- [Статус серверов](#статус-серверов)
- [Режим техработ](#режим-техработ)
- [Интерфейс и UX](#интерфейс-и-ux)
- [Локализация](#локализация)
- [Логирование](#логирование)
- [Ротация логов](#ротация-логов)
- [Бэкапы](#бэкапы)
- [Webhook режим бота](#webhook-режим-бота)
- [Web API](#web-api)
- [Ban System (BedolagaBan)](#ban-system-bedolagaban)
- [Чёрный список](#чёрный-список)
- [Конкурсы](#конкурсы)
- [Прочие настройки](#прочие-настройки)

---

## Telegram бот

### BOT_TOKEN
**Тип:** `string` (обязательный)

Токен бота, полученный от [@BotFather](https://t.me/BotFather).

> ⚠️ **Важно:** Также используется для авторизации виджета личного кабинета (Cabinet WebApp) через `Telegram.WebApp.initData`.

```env
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
```

---

### BOT_USERNAME
**Тип:** `string` (опционально)

Username бота без символа `@`. Автоопределяется при запуске, но можно указать явно.

```env
BOT_USERNAME=my_vpn_bot
```

---

### ADMIN_IDS
**Тип:** `string`

Список ID администраторов через запятую. Эти пользователи получают доступ к админ-панели.

```env
ADMIN_IDS=123456789,987654321
```

---

### ADMIN_EMAILS
**Тип:** `string`

Список email-адресов администраторов через запятую. Для email-only пользователей.

```env
ADMIN_EMAILS=admin@example.com,manager@example.com
```

---

### SUPPORT_USERNAME
**Тип:** `string`  
**По умолчанию:** `@support`

Ссылка на поддержку. Может быть:
- Telegram username (например, `@support`)
- Полный URL (например, `https://t.me/support_bot`)

```env
SUPPORT_USERNAME=@my_support
SUPPORT_USERNAME=https://t.me/my_support_bot
```

---

### TEST_EMAIL / TEST_EMAIL_PASSWORD
**Тип:** `string` (опционально)

Тестовый email для разработки. При использовании верификация email пропускается, SMTP не требуется.

```env
TEST_EMAIL=test@example.com
TEST_EMAIL_PASSWORD=testpassword123
```

---

## Система поддержки

### SUPPORT_MENU_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Показывать меню поддержки в интерфейсе бота.

```env
SUPPORT_MENU_ENABLED=true
```

---

### SUPPORT_SYSTEM_MODE
**Тип:** `string`  
**По умолчанию:** `both`  
**Допустимые значения:** `tickets`, `contact`, `both`

Режим системы поддержки:
- `tickets` — только тикеты
- `contact` — только контакт поддержки
- `both` — оба варианта

```env
SUPPORT_SYSTEM_MODE=both
```

---

### SUPPORT_TICKET_SLA_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Включить SLA для тикетов поддержки (напоминания о просроченных тикетах).

```env
SUPPORT_TICKET_SLA_ENABLED=true
```

---

### SUPPORT_TICKET_SLA_MINUTES
**Тип:** `integer`  
**По умолчанию:** `5`

Время SLA в минутах. По истечении отправляется напоминание.

```env
SUPPORT_TICKET_SLA_MINUTES=60
```

---

### SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS
**Тип:** `integer`  
**По умолчанию:** `60`

Интервал проверки SLA в секундах.

```env
SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS=300
```

---

### SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES
**Тип:** `integer`  
**По умолчанию:** `15`

Кулдаун между напоминаниями SLA в минутах.

```env
SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES=30
```

---

### MINIAPP_TICKETS_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Включить раздел тикетов в MiniApp.

```env
MINIAPP_TICKETS_ENABLED=true
```

---

### MINIAPP_SUPPORT_TYPE
**Тип:** `string`  
**По умолчанию:** `tickets`  
**Допустимые значения:** `tickets`, `profile`, `url`

Тип поддержки в MiniApp:
- `tickets` — стандартные тикеты
- `profile` — профиль пользователя
- `url` — кастомный URL

```env
MINIAPP_SUPPORT_TYPE=tickets
```

---

### MINIAPP_SUPPORT_URL
**Тип:** `string`

Кастомный URL для поддержки (только при `MINIAPP_SUPPORT_TYPE=url`).

```env
MINIAPP_SUPPORT_URL=https://support.example.com
```

---

## Личный кабинет (Cabinet)

### CABINET_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить личный кабинет пользователя (веб-интерфейс для управления подпиской).

```env
CABINET_ENABLED=true
```

---

### CABINET_URL
**Тип:** `string`

Базовый URL кабинета для ссылок в email.

```env
CABINET_URL=https://cabinet.example.com
```

---

### CABINET_JWT_SECRET
**Тип:** `string` (опционально)

Секретный ключ для JWT токенов. Если не указан, используется `BOT_TOKEN`.

```env
CABINET_JWT_SECRET=your_super_secret_key_here
```

---

### CABINET_ACCESS_TOKEN_EXPIRE_MINUTES
**Тип:** `integer`  
**По умолчанию:** `15`

Время жизни access token в минутах.

```env
CABINET_ACCESS_TOKEN_EXPIRE_MINUTES=30
```

---

### CABINET_REFRESH_TOKEN_EXPIRE_DAYS
**Тип:** `integer`  
**По умолчанию:** `7`

Время жизни refresh token в днях.

```env
CABINET_REFRESH_TOKEN_EXPIRE_DAYS=30
```

---

### CABINET_ALLOWED_ORIGINS
**Тип:** `string`

Разрешённые origins для CORS через запятую.

```env
CABINET_ALLOWED_ORIGINS=https://cabinet.example.com,https://app.example.com
```

---

### CABINET_EMAIL_VERIFICATION_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Требовать верификацию email (нужна настройка SMTP).

```env
CABINET_EMAIL_VERIFICATION_ENABLED=true
```

---

### CABINET_EMAIL_AUTH_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Разрешить регистрацию/вход по email. Если `false` — только через Telegram.

```env
CABINET_EMAIL_AUTH_ENABLED=true
```

---

### CABINET_EMAIL_VERIFICATION_EXPIRE_HOURS
**Тип:** `integer`  
**По умолчанию:** `24`

Время жизни токена верификации email в часах.

```env
CABINET_EMAIL_VERIFICATION_EXPIRE_HOURS=48
```

---

### CABINET_PASSWORD_RESET_EXPIRE_HOURS
**Тип:** `integer`  
**По умолчанию:** `1`

Время жизни токена сброса пароля в часах.

```env
CABINET_PASSWORD_RESET_EXPIRE_HOURS=2
```

---

### CABINET_EMAIL_CHANGE_CODE_EXPIRE_MINUTES
**Тип:** `integer`  
**По умолчанию:** `15`

Время жизни кода подтверждения смены email в минутах.

```env
CABINET_EMAIL_CHANGE_CODE_EXPIRE_MINUTES=30
```

---

### CABINET_REMNA_SUB_CONFIG
**Тип:** `string` (опционально)

UUID конфига страницы подписки из RemnaWave.

```env
CABINET_REMNA_SUB_CONFIG=550e8400-e29b-41d4-a716-446655440000
```

---

## OAuth 2.0 провайдеры

### Google

```env
OAUTH_GOOGLE_ENABLED=false
OAUTH_GOOGLE_CLIENT_ID=your_client_id.apps.googleusercontent.com
OAUTH_GOOGLE_CLIENT_SECRET=your_client_secret
```

### Yandex

```env
OAUTH_YANDEX_ENABLED=false
OAUTH_YANDEX_CLIENT_ID=your_client_id
OAUTH_YANDEX_CLIENT_SECRET=your_client_secret
```

### Discord

```env
OAUTH_DISCORD_ENABLED=false
OAUTH_DISCORD_CLIENT_ID=your_client_id
OAUTH_DISCORD_CLIENT_SECRET=your_client_secret
```

### VK

```env
OAUTH_VK_ENABLED=false
OAUTH_VK_CLIENT_ID=your_client_id
OAUTH_VK_CLIENT_SECRET=your_client_secret
```

---

## SMTP настройки

Для отправки email в личном кабинете (верификация, сброс пароля).

### SMTP_HOST
**Тип:** `string`

SMTP сервер.

```env
SMTP_HOST=smtp.gmail.com
SMTP_HOST=smtp.yandex.ru
```

---

### SMTP_PORT
**Тип:** `integer`  
**По умолчанию:** `587`

Порт SMTP сервера.

```env
SMTP_PORT=587
SMTP_PORT=465
```

---

### SMTP_USER / SMTP_PASSWORD
**Тип:** `string`

Учётные данные для SMTP авторизации.

```env
SMTP_USER=your_email@gmail.com
SMTP_PASSWORD=your_app_password
```

---

### SMTP_FROM_EMAIL
**Тип:** `string` (опционально)

Email отправителя. Если не указан, используется `SMTP_USER`.

```env
SMTP_FROM_EMAIL=noreply@example.com
```

---

### SMTP_FROM_NAME
**Тип:** `string`  
**По умолчанию:** `VPN Service`

Имя отправителя.

```env
SMTP_FROM_NAME=My VPN Service
```

---

### SMTP_USE_TLS
**Тип:** `boolean`  
**По умолчанию:** `true`

Использовать TLS шифрование.

```env
SMTP_USE_TLS=true
```

---

## Уведомления администраторов

### ADMIN_NOTIFICATIONS_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить отправку уведомлений администраторам.

```env
ADMIN_NOTIFICATIONS_ENABLED=true
```

---

### ADMIN_NOTIFICATIONS_CHAT_ID
**Тип:** `string`

ID чата/канала для уведомлений. Для закрытых каналов используйте префикс `-100`.

```env
ADMIN_NOTIFICATIONS_CHAT_ID=-1001234567890
```

---

### ADMIN_NOTIFICATIONS_TOPIC_ID
**Тип:** `integer` (опционально)

ID топика (для форумов/групп с топиками).

```env
ADMIN_NOTIFICATIONS_TOPIC_ID=123
```

---

### ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID
**Тип:** `integer` (опционально)

ID топика для тикетов поддержки.

```env
ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID=126
```

---

### ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID
**Тип:** `integer` (опционально)

ID топика для уведомлений о чеках NaloGO.

```env
ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID=133
```

---

### ADMIN_REPORTS_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить автоматические отчёты.

```env
ADMIN_REPORTS_ENABLED=true
```

---

### ADMIN_REPORTS_CHAT_ID
**Тип:** `string` (опционально)

Чат для отчётов. По умолчанию используется `ADMIN_NOTIFICATIONS_CHAT_ID`.

```env
ADMIN_REPORTS_CHAT_ID=-1001234567890
```

---

### ADMIN_REPORTS_TOPIC_ID
**Тип:** `integer` (опционально)

ID топика для отчётов.

```env
ADMIN_REPORTS_TOPIC_ID=125
```

---

### ADMIN_REPORTS_SEND_TIME
**Тип:** `string` (опционально)

Время отправки ежедневного отчёта (формат HH:MM).

```env
ADMIN_REPORTS_SEND_TIME=10:00
```

---

## Обязательная подписка на канал

### CHANNEL_SUB_ID
**Тип:** `string` (опционально)

ID канала для проверки подписки.

```env
CHANNEL_SUB_ID=-1001234567890
```

---

### CHANNEL_LINK
**Тип:** `string` (опционально)

Ссылка на канал.

```env
CHANNEL_LINK=https://t.me/my_channel
```

---

### CHANNEL_IS_REQUIRED_SUB
**Тип:** `boolean`  
**По умолчанию:** `false`

Обязательна ли подписка на канал.

```env
CHANNEL_IS_REQUIRED_SUB=true
```

---

### CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE
**Тип:** `boolean`  
**По умолчанию:** `true`

Отключать триальные подписки при отписке от канала.

```env
CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE=true
```

---

### CHANNEL_REQUIRED_FOR_ALL
**Тип:** `boolean`  
**По умолчанию:** `false`

Требовать подписку на канал для ВСЕХ пользователей (включая платных).

```env
CHANNEL_REQUIRED_FOR_ALL=true
```

---

## База данных

### DATABASE_MODE
**Тип:** `string`  
**По умолчанию:** `auto`  
**Допустимые значения:** `auto`, `postgresql`, `sqlite`

Режим базы данных:
- `auto` — автоматический выбор (PostgreSQL в Docker, SQLite локально)
- `postgresql` — принудительно PostgreSQL
- `sqlite` — принудительно SQLite

```env
DATABASE_MODE=auto
```

---

### DATABASE_URL
**Тип:** `string` (опционально)

Полный URL подключения к БД. Если указан, используется напрямую.

```env
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/dbname
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db
```

---

### PostgreSQL настройки

```env
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=remnawave_bot
POSTGRES_USER=remnawave_user
POSTGRES_PASSWORD=secure_password_123
```

---

### SQLite настройки

```env
SQLITE_PATH=./data/bot.db
```

---

### LOCALES_PATH
**Тип:** `string`  
**По умолчанию:** `./locales`

Путь к файлам локализации.

```env
LOCALES_PATH=./custom_locales
```

---

### TIMEZONE / TZ
**Тип:** `string`  
**По умолчанию:** `UTC`

Часовой пояс.

```env
TZ=Europe/Moscow
TIMEZONE=America/New_York
```

---

## Redis

### REDIS_URL
**Тип:** `string`  
**По умолчанию:** `redis://localhost:6379/0`

URL подключения к Redis.

```env
REDIS_URL=redis://redis:6379/0
REDIS_URL=redis://:password@localhost:6379/1
```

---

### CART_TTL_SECONDS
**Тип:** `integer`  
**По умолчанию:** `3600`

Время жизни корзины пользователя в Redis (секунды).

```env
CART_TTL_SECONDS=7200
```

---

## RemnaWave API

### REMNAWAVE_API_URL
**Тип:** `string`

URL панели RemnaWave.

```env
REMNAWAVE_API_URL=https://panel.example.com
```

---

### REMNAWAVE_API_KEY
**Тип:** `string`

API ключ для авторизации.

```env
REMNAWAVE_API_KEY=your_api_key_here
```

---

### REMNAWAVE_AUTH_TYPE
**Тип:** `string`  
**По умолчанию:** `api_key`  
**Допустимые значения:** `api_key`, `basic`, `bearer`, `cookies`, `caddy`

Тип авторизации в панели.

```env
REMNAWAVE_AUTH_TYPE=api_key
```

---

### REMNAWAVE_USERNAME / REMNAWAVE_PASSWORD
**Тип:** `string` (опционально)

Для панелей с Basic Auth.

```env
REMNAWAVE_USERNAME=admin
REMNAWAVE_PASSWORD=password
```

---

### REMNAWAVE_CADDY_TOKEN
**Тип:** `string` (опционально)

Токен для авторизации через Caddy.

```env
REMNAWAVE_CADDY_TOKEN=your_caddy_token
```

---

### REMNAWAVE_SECRET_KEY
**Тип:** `string` (опционально)

Секретный ключ. Для панелей, установленных скриптом eGames, формат: `XXXXXXX:DDDDDDDD`.

```env
REMNAWAVE_SECRET_KEY=ABC1234:99887766
```

---

### REMNAWAVE_USER_DESCRIPTION_TEMPLATE
**Тип:** `string`  
**По умолчанию:** `Bot user: {full_name} {username}`

Шаблон описания пользователя в панели.

**Доступные плейсхолдеры:**
- `{full_name}` — Имя, Фамилия из Telegram
- `{username}` — @логин из Telegram (с @)
- `{username_clean}` — логин из Telegram (без @)
- `{telegram_id}` — ID Telegram

```env
REMNAWAVE_USER_DESCRIPTION_TEMPLATE="VPN User: {full_name} (@{username_clean})"
```

---

### REMNAWAVE_USER_USERNAME_TEMPLATE
**Тип:** `string`  
**По умолчанию:** `user_{telegram_id}`

Шаблон имени пользователя в панели RemnaWave.

```env
REMNAWAVE_USER_USERNAME_TEMPLATE="tg_{telegram_id}"
```

---

### REMNAWAVE_USER_DELETE_MODE
**Тип:** `string`  
**По умолчанию:** `delete`  
**Допустимые значения:** `delete`, `disable`

Режим удаления пользователей из панели:
- `delete` — полностью удалить
- `disable` — только деактивировать

```env
REMNAWAVE_USER_DELETE_MODE=disable
```

---

### REMNAWAVE_AUTO_SYNC_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить автоматическую синхронизацию пользователей.

```env
REMNAWAVE_AUTO_SYNC_ENABLED=true
```

---

### REMNAWAVE_AUTO_SYNC_TIMES
**Тип:** `string`  
**По умолчанию:** `03:00`

Времена синхронизации через запятую (формат HH:MM).

```env
REMNAWAVE_AUTO_SYNC_TIMES=03:00,15:00
```

---

## Webhooks от RemnaWave

### REMNAWAVE_WEBHOOK_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить приём вебхуков от панели RemnaWave.

```env
REMNAWAVE_WEBHOOK_ENABLED=true
```

---

### REMNAWAVE_WEBHOOK_PATH
**Тип:** `string`  
**По умолчанию:** `/remnawave-webhook`

Путь для приёма вебхуков.

```env
REMNAWAVE_WEBHOOK_PATH=/remnawave-webhook
```

---

### REMNAWAVE_WEBHOOK_SECRET
**Тип:** `string` (минимум 32 символа)

Общий секрет для подписи HMAC-SHA256.

```bash
# Генерация секрета:
openssl rand -hex 32
```

```env
REMNAWAVE_WEBHOOK_SECRET=your_32_char_or_longer_secret_here
```

---

### Уведомления от вебхуков

Настройки определяют, какие уведомления получают пользователи от событий вебхуков.

```env
# Глобальный переключатель
WEBHOOK_NOTIFY_USER_ENABLED=true

# Отдельные типы уведомлений
WEBHOOK_NOTIFY_SUB_STATUS=true      # Включение/отключение подписки администратором
WEBHOOK_NOTIFY_SUB_EXPIRED=true     # Истечение подписки
WEBHOOK_NOTIFY_SUB_EXPIRING=true    # Предупреждения о скором истечении
WEBHOOK_NOTIFY_SUB_LIMITED=true     # Достижение лимита трафика
WEBHOOK_NOTIFY_TRAFFIC_RESET=true   # Сброс счётчика трафика
WEBHOOK_NOTIFY_SUB_DELETED=true     # Удаление пользователя из панели
WEBHOOK_NOTIFY_SUB_REVOKED=true     # Обновление ключей подписки
WEBHOOK_NOTIFY_FIRST_CONNECTED=true # Первое подключение к VPN
WEBHOOK_NOTIFY_NOT_CONNECTED=true   # Напоминание о неподключении
WEBHOOK_NOTIFY_BANDWIDTH_THRESHOLD=true # Приближение к лимиту трафика
WEBHOOK_NOTIFY_DEVICES=true         # Подключение/отключение устройств
```

---

## Подписки и продажи

### SALES_MODE
**Тип:** `string`  
**По умолчанию:** `tariffs`  
**Допустимые значения:** `classic`, `tariffs`

Режим продаж подписок:
- `classic` — пользователь выбирает период, серверы, трафик, устройства отдельно
- `tariffs` — пользователь выбирает готовый тариф

```env
SALES_MODE=tariffs
```

---

### AVAILABLE_SUBSCRIPTION_PERIODS
**Тип:** `string`  
**По умолчанию:** `14,30,60,90,180,360`

Доступные периоды подписки через запятую (в днях).

```env
AVAILABLE_SUBSCRIPTION_PERIODS=30,90,180
```

---

### AVAILABLE_RENEWAL_PERIODS
**Тип:** `string`  
**По умолчанию:** `30,90,180`

Доступные периоды продления через запятую.

```env
AVAILABLE_RENEWAL_PERIODS=30,60,90,180,360
```

---

### SIMPLE_SUBSCRIPTION_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить режим простой покупки (одна кнопка = одна подписка).

```env
SIMPLE_SUBSCRIPTION_ENABLED=true
```

---

### SIMPLE_SUBSCRIPTION_PERIOD_DAYS
**Тип:** `integer`  
**По умолчанию:** `30`

Период простой подписки в днях.

```env
SIMPLE_SUBSCRIPTION_PERIOD_DAYS=30
```

---

### SIMPLE_SUBSCRIPTION_DEVICE_LIMIT
**Тип:** `integer`  
**По умолчанию:** `1`

Количество устройств для простой подписки.

```env
SIMPLE_SUBSCRIPTION_DEVICE_LIMIT=3
```

---

### SIMPLE_SUBSCRIPTION_TRAFFIC_GB
**Тип:** `integer`  
**По умолчанию:** `0`

Лимит трафика в ГБ (0 = безлимит).

```env
SIMPLE_SUBSCRIPTION_TRAFFIC_GB=100
```

---

### SIMPLE_SUBSCRIPTION_SQUAD_UUID
**Тип:** `string` (опционально)

UUID сквада для простой подписки.

```env
SIMPLE_SUBSCRIPTION_SQUAD_UUID=550e8400-e29b-41d4-a716-446655440000
```

---

## Пробная подписка (Trial)

### TRIAL_DURATION_DAYS
**Тип:** `integer`  
**По умолчанию:** `3`

Длительность пробной подписки в днях.

```env
TRIAL_DURATION_DAYS=7
```

---

### TRIAL_TRAFFIC_LIMIT_GB
**Тип:** `integer`  
**По умолчанию:** `10`

Лимит трафика для триала в ГБ.

```env
TRIAL_TRAFFIC_LIMIT_GB=5
```

---

### TRIAL_DEVICE_LIMIT
**Тип:** `integer`  
**По умолчанию:** `2`

Количество устройств для триала.

```env
TRIAL_DEVICE_LIMIT=1
```

---

### TRIAL_PAYMENT_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить платный триал.

```env
TRIAL_PAYMENT_ENABLED=true
```

---

### TRIAL_ACTIVATION_PRICE
**Тип:** `integer`  
**По умолчанию:** `0`

Цена активации триала в копейках (1000 = 10 рублей).

```env
TRIAL_ACTIVATION_PRICE=10000
```

---

### TRIAL_TARIFF_ID
**Тип:** `integer`  
**По умолчанию:** `0`

ID тарифа для триала в режиме тарифов. Если 0 — используются стандартные настройки.

```env
TRIAL_TARIFF_ID=5
```

---

### TRIAL_ADD_REMAINING_DAYS_TO_PAID
**Тип:** `boolean`  
**По умолчанию:** `false`

Добавлять оставшиеся дни триала при покупке платной подписки.

```env
TRIAL_ADD_REMAINING_DAYS_TO_PAID=true
```

---

### TRIAL_USER_TAG
**Тип:** `string` (опционально)

Тег для пробных пользователей в RemnaWave (A-Z, 0-9, _, макс. 16 символов).

```env
TRIAL_USER_TAG=TRIAL
```

---

### TRIAL_DISABLED_FOR
**Тип:** `string`  
**По умолчанию:** `none`  
**Допустимые значения:** `none`, `email`, `telegram`, `all`

Отключить триал для определённых типов пользователей:
- `none` — триал доступен всем
- `email` — триал отключён для email-пользователей
- `telegram` — триал отключён для Telegram-пользователей
- `all` — триал отключён для всех

```env
TRIAL_DISABLED_FOR=email
```

---

### TRIAL_WARNING_HOURS
**Тип:** `integer`  
**По умолчанию:** `2`

За сколько часов до окончания отправлять предупреждение о завершении триала.

```env
TRIAL_WARNING_HOURS=24
```

---

## Настройки трафика

### TRAFFIC_SELECTION_MODE
**Тип:** `string`  
**По умолчанию:** `selectable`  
**Допустимые значения:** `selectable`, `fixed`, `fixed_with_topup`

Режим выбора трафика:
- `selectable` — пользователь выбирает трафик при покупке и может докупать
- `fixed` — фиксированный лимит, без выбора и без докупки
- `fixed_with_topup` — фиксированный лимит при покупке, но докупка разрешена

```env
TRAFFIC_SELECTION_MODE=selectable
```

---

### FIXED_TRAFFIC_LIMIT_GB
**Тип:** `integer`  
**По умолчанию:** `100`

Фиксированный лимит трафика в ГБ (для режимов `fixed` и `fixed_with_topup`).

```env
FIXED_TRAFFIC_LIMIT_GB=50
```

---

### DEFAULT_TRAFFIC_LIMIT_GB
**Тип:** `integer`  
**По умолчанию:** `100`

Лимит трафика по умолчанию для подписок из админки.

```env
DEFAULT_TRAFFIC_LIMIT_GB=100
```

---

### DEFAULT_TRAFFIC_RESET_STRATEGY
**Тип:** `string`  
**По умолчанию:** `MONTH`

Стратегия сброса трафика для всех подписок.

```env
DEFAULT_TRAFFIC_RESET_STRATEGY=MONTH
```

---

### RESET_TRAFFIC_ON_PAYMENT
**Тип:** `boolean`  
**По умолчанию:** `false`

Сбрасывать трафик при каждой оплате подписки.

```env
RESET_TRAFFIC_ON_PAYMENT=true
```

---

### TRAFFIC_TOPUP_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Включить функцию докупки трафика.

```env
TRAFFIC_TOPUP_ENABLED=true
```

---

### BUY_TRAFFIC_BUTTON_VISIBLE
**Тип:** `boolean`  
**По умолчанию:** `true`

Показывать кнопку "Докупить трафик" в меню.

```env
BUY_TRAFFIC_BUTTON_VISIBLE=true
```

---

### TRAFFIC_PACKAGES_CONFIG
**Тип:** `string`

Конфигурация пакетов трафика. Формат: `гб:цена_в_копейках:enabled`.

```env
TRAFFIC_PACKAGES_CONFIG="5:2000:true,10:3500:true,25:7000:true,50:11000:true,100:15000:true,0:20000:true"
```

**Примечание:** `0` ГБ означает безлимитный трафик.

---

### TRAFFIC_TOPUP_PACKAGES_CONFIG
**Тип:** `string` (опционально)

Отдельные пакеты для докупки трафика. Если пустой — используется `TRAFFIC_PACKAGES_CONFIG`.

```env
TRAFFIC_TOPUP_PACKAGES_CONFIG="10:5000:true,25:10000:true,50:15000:true"
```

---

### TRAFFIC_RESET_PRICE_MODE
**Тип:** `string`  
**По умолчанию:** `traffic_with_purchased`  
**Допустимые значения:** `period`, `traffic`, `traffic_with_purchased`

Режим расчёта цены сброса трафика:
- `period` — фиксированная цена = стоимость 30 дней (⚠️ может быть абьюзом!)
- `traffic` — цена = стоимость текущего пакета трафика
- `traffic_with_purchased` — цена = базовый + докупленный трафик (рекомендуется)

```env
TRAFFIC_RESET_PRICE_MODE=traffic_with_purchased
```

---

### TRAFFIC_RESET_BASE_PRICE
**Тип:** `integer`  
**По умолчанию:** `0`

Базовая цена сброса в копейках. 0 = использовать `PRICE_30_DAYS`.

```env
TRAFFIC_RESET_BASE_PRICE=10000
```

---

## Настройки устройств

### DEFAULT_DEVICE_LIMIT
**Тип:** `integer`  
**По умолчанию:** `1`

Количество устройств по умолчанию при покупке платной подписки.

```env
DEFAULT_DEVICE_LIMIT=3
```

---

### MAX_DEVICES_LIMIT
**Тип:** `integer`  
**По умолчанию:** `20`

Максимум устройств, доступных к покупке. 0 = без лимита.

```env
MAX_DEVICES_LIMIT=15
```

---

### DEVICES_SELECTION_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Включить выбор количества устройств при покупке.

```env
DEVICES_SELECTION_ENABLED=true
```

---

### DEVICES_SELECTION_DISABLED_AMOUNT
**Тип:** `integer` (опционально)

Единое количество устройств для режима без выбора. 0 = не назначать.

```env
DEVICES_SELECTION_DISABLED_AMOUNT=3
```

---

## Тарифы и цены

### BASE_SUBSCRIPTION_PRICE
**Тип:** `integer`  
**По умолчанию:** `50000`

Базовая цена подписки в копейках (500 рублей).

```env
BASE_SUBSCRIPTION_PRICE=0
```

---

### Цены за периоды

Все цены в копейках (100 копеек = 1 рубль).

```env
PRICE_14_DAYS=7000       # 70 рублей
PRICE_30_DAYS=10000      # 100 рублей
PRICE_60_DAYS=18000      # 180 рублей
PRICE_90_DAYS=25000      # 250 рублей
PRICE_180_DAYS=45000     # 450 рублей
PRICE_360_DAYS=80000     # 800 рублей
```

---

### Цены за трафик (устаревший способ)

Используются, если `TRAFFIC_PACKAGES_CONFIG` не задан.

```env
PRICE_TRAFFIC_5GB=2000
PRICE_TRAFFIC_10GB=3500
PRICE_TRAFFIC_25GB=7000
PRICE_TRAFFIC_50GB=11000
PRICE_TRAFFIC_100GB=15000
PRICE_TRAFFIC_250GB=17000
PRICE_TRAFFIC_500GB=19000
PRICE_TRAFFIC_1000GB=19500
PRICE_TRAFFIC_UNLIMITED=20000
```

---

### PRICE_PER_DEVICE
**Тип:** `integer`  
**По умолчанию:** `5000`

Цена за дополнительное устройство в копейках. `DEFAULT_DEVICE_LIMIT` устройств идёт бесплатно.

```env
PRICE_PER_DEVICE=10000
```

---

### PAID_SUBSCRIPTION_USER_TAG
**Тип:** `string` (опционально)

Тег для платных пользователей в RemnaWave.

```env
PAID_SUBSCRIPTION_USER_TAG=PAID
```

---

## Скидки

### BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить скидки за длительные периоды для базовых пользователей.

```env
BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED=true
```

---

### BASE_PROMO_GROUP_PERIOD_DISCOUNTS
**Тип:** `string`

Скидки за периоды. Формат: `дней:процент`.

```env
BASE_PROMO_GROUP_PERIOD_DISCOUNTS=60:10,90:20,180:40,360:70
```

**Пример:** 90 дней = скидка 20%, 360 дней = скидка 70%.

---

## Модем

### MODEM_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить функционал подключения модема.

```env
MODEM_ENABLED=true
```

---

### MODEM_PRICE_PER_MONTH
**Тип:** `integer`  
**По умолчанию:** `10000`

Цена модема в копейках за месяц.

```env
MODEM_PRICE_PER_MONTH=15000
```

---

### MODEM_PERIOD_DISCOUNTS
**Тип:** `string`

Скидки на модем за длительный срок. Формат: `месяцев:процент`.

```env
MODEM_PERIOD_DISCOUNTS=3:15,6:20,12:25
```

---

## Реферальная система

### REFERRAL_PROGRAM_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Включить реферальную программу.

```env
REFERRAL_PROGRAM_ENABLED=true
```

---

### REFERRAL_MINIMUM_TOPUP_KOPEKS
**Тип:** `integer`  
**По умолчанию:** `10000`

Минимальное пополнение для активации реферального бонуса (в копейках).

```env
REFERRAL_MINIMUM_TOPUP_KOPEKS=10000
```

---

### REFERRAL_FIRST_TOPUP_BONUS_KOPEKS
**Тип:** `integer`  
**По умолчанию:** `10000`

Бонус рефералу при первом пополнении (в копейках).

```env
REFERRAL_FIRST_TOPUP_BONUS_KOPEKS=15000
```

---

### REFERRAL_INVITER_BONUS_KOPEKS
**Тип:** `integer`  
**По умолчанию:** `10000`

Бонус пригласившему при первом пополнении реферала (в копейках).

```env
REFERRAL_INVITER_BONUS_KOPEKS=15000
```

---

### REFERRAL_COMMISSION_PERCENT
**Тип:** `integer`  
**По умолчанию:** `25`

Процент комиссии с пополнений рефералов.

```env
REFERRAL_COMMISSION_PERCENT=30
```

---

### REFERRAL_NOTIFICATIONS_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Включить уведомления о реферальных начислениях.

```env
REFERRAL_NOTIFICATIONS_ENABLED=true
```

---

### REFERRAL_NOTIFICATION_RETRY_ATTEMPTS
**Тип:** `integer`  
**По умолчанию:** `3`

Количество попыток отправки уведомления.

```env
REFERRAL_NOTIFICATION_RETRY_ATTEMPTS=5
```

---

### Вывод реферального баланса

```env
# Включить возможность вывода
REFERRAL_WITHDRAWAL_ENABLED=false

# Минимальная сумма вывода (в копейках)
REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS=50000

# Интервал между запросами на вывод (дни)
REFERRAL_WITHDRAWAL_COOLDOWN_DAYS=30

# Выводить только реферальный баланс (true) или весь (false)
REFERRAL_WITHDRAWAL_ONLY_REFERRAL_BALANCE=true

# ID топика для уведомлений о заявках
REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID=0

# Тестовый режим
REFERRAL_WITHDRAWAL_TEST_MODE=false
```

---

### Анализ подозрительной активности

```env
# Минимальная сумма депозита от реферала
REFERRAL_WITHDRAWAL_SUSPICIOUS_MIN_DEPOSIT_KOPEKS=100000

# Максимум пополнений от одного реферала в месяц
REFERRAL_WITHDRAWAL_SUSPICIOUS_MAX_DEPOSITS_PER_MONTH=10

# Коэффициент (пополнено в X раз больше, чем потрачено)
REFERRAL_WITHDRAWAL_SUSPICIOUS_NO_PURCHASES_RATIO=3.0
```

---

## Автопродление

### ENABLE_AUTOPAY
**Тип:** `boolean`  
**По умолчанию:** `false`

Глобально включить функцию автопродления.

```env
ENABLE_AUTOPAY=true
```

---

### AUTOPAY_WARNING_DAYS
**Тип:** `string`  
**По умолчанию:** `3,1`

Дни до окончания подписки для отправки предупреждений (через запятую).

```env
AUTOPAY_WARNING_DAYS=7,3,1
```

---

### DEFAULT_AUTOPAY_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить автопродление для новых пользователей по умолчанию.

```env
DEFAULT_AUTOPAY_ENABLED=true
```

---

### DEFAULT_AUTOPAY_DAYS_BEFORE
**Тип:** `integer`  
**По умолчанию:** `3`

За сколько дней до окончания автоматически продлевать.

```env
DEFAULT_AUTOPAY_DAYS_BEFORE=1
```

---

### MIN_BALANCE_FOR_AUTOPAY_KOPEKS
**Тип:** `integer`  
**По умолчанию:** `10000`

Минимальный баланс для автопродления (в копейках).

```env
MIN_BALANCE_FOR_AUTOPAY_KOPEKS=5000
```

---

### SUBSCRIPTION_RENEWAL_BALANCE_THRESHOLD_KOPEKS
**Тип:** `integer`  
**По умолчанию:** `20000`

Порог баланса для фильтра «готовы к продлению» (в копейках).

```env
SUBSCRIPTION_RENEWAL_BALANCE_THRESHOLD_KOPEKS=30000
```

---

### DAILY_SUBSCRIPTIONS_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Включить автоматическое списание для суточных тарифов.

```env
DAILY_SUBSCRIPTIONS_ENABLED=true
```

---

### DAILY_SUBSCRIPTIONS_CHECK_INTERVAL_MINUTES
**Тип:** `integer`  
**По умолчанию:** `30`

Интервал проверки суточных подписок в минутах.

```env
DAILY_SUBSCRIPTIONS_CHECK_INTERVAL_MINUTES=15
```

---

## Платёжные системы

### Общие настройки

```env
# Отключить кнопки выбора суммы (только ручной ввод)
DISABLE_TOPUP_BUTTONS=false

# Пополнение через поддержку
SUPPORT_TOPUP_ENABLED=true

# Автопроверка зависших пополнений
PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED=false
PAYMENT_VERIFICATION_AUTO_CHECK_INTERVAL_MINUTES=10
```

---

### Описания платежей

Настройки для изменения описаний платежей (чтобы избежать блокировок платёжных систем).

```env
PAYMENT_SERVICE_NAME=Интернет-сервис
PAYMENT_BALANCE_DESCRIPTION=Пополнение баланса
PAYMENT_SUBSCRIPTION_DESCRIPTION=Оплата подписки
PAYMENT_BALANCE_TEMPLATE={service_name} - {description}
PAYMENT_SUBSCRIPTION_TEMPLATE={service_name} - {description}
```

---

### Telegram Stars

```env
TELEGRAM_STARS_ENABLED=true
TELEGRAM_STARS_RATE_RUB=1.79
TELEGRAM_STARS_DISPLAY_NAME=Telegram Stars
```

---

### Tribute

```env
TRIBUTE_ENABLED=false
TRIBUTE_API_KEY=your_api_key
TRIBUTE_DONATE_LINK=https://donate.tribute.app/your_link
TRIBUTE_WEBHOOK_PATH=/tribute-webhook
TRIBUTE_WEBHOOK_HOST=0.0.0.0
TRIBUTE_WEBHOOK_PORT=8081
```

---

### YooKassa

```env
YOOKASSA_ENABLED=false
YOOKASSA_DISPLAY_NAME=YooKassa
YOOKASSA_SHOP_ID=your_shop_id
YOOKASSA_SECRET_KEY=your_secret_key
YOOKASSA_RETURN_URL=https://your-domain.com/payment-success
YOOKASSA_DEFAULT_RECEIPT_EMAIL=receipts@yourdomain.com

# СБП
YOOKASSA_SBP_ENABLED=false

# Настройки чеков
YOOKASSA_VAT_CODE=1
# Коды НДС: 1-без НДС, 2-0%, 3-10%, 4-20%, 5-10/110, 6-20/120

YOOKASSA_PAYMENT_MODE=full_payment
# Способы: full_payment, partial_payment, advance, full_prepayment, partial_prepayment, credit, credit_payment

YOOKASSA_PAYMENT_SUBJECT=service
# Предметы: commodity, excise, job, service, gambling_bet, gambling_prize, lottery, lottery_prize, intellectual_activity, payment, agent_commission, composite, another

# Webhook
YOOKASSA_WEBHOOK_PATH=/yookassa-webhook
YOOKASSA_WEBHOOK_HOST=0.0.0.0
YOOKASSA_WEBHOOK_PORT=8082
YOOKASSA_TRUSTED_PROXY_NETWORKS=185.71.76.0/24,185.71.77.0/24

# Лимиты
YOOKASSA_MIN_AMOUNT_KOPEKS=5000
YOOKASSA_MAX_AMOUNT_KOPEKS=1000000

# Быстрый выбор суммы
YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED=true
```

---

### NaloGO (чеки)

```env
NALOGO_ENABLED=false
NALOGO_INN=123456789012
NALOGO_PASSWORD=your_password
NALOGO_DEVICE_ID=optional_device_id
NALOGO_STORAGE_PATH=./nalogo_tokens.json
NALOGO_QUEUE_CHECK_INTERVAL=300
NALOGO_QUEUE_RECEIPT_DELAY=3
NALOGO_QUEUE_MAX_ATTEMPTS=10
```

---

### CryptoBot

```env
CRYPTOBOT_ENABLED=false
CRYPTOBOT_DISPLAY_NAME=CryptoBot
CRYPTOBOT_API_TOKEN=your_api_token
CRYPTOBOT_WEBHOOK_SECRET=your_webhook_secret
CRYPTOBOT_BASE_URL=https://pay.crypt.bot
CRYPTOBOT_TESTNET=false
CRYPTOBOT_WEBHOOK_PATH=/cryptobot-webhook
CRYPTOBOT_WEBHOOK_PORT=8081
CRYPTOBOT_DEFAULT_ASSET=USDT
CRYPTOBOT_ASSETS=USDT,TON,BTC,ETH,LTC,BNB,TRX,USDC
CRYPTOBOT_INVOICE_EXPIRES_HOURS=24
```

---

### Heleket

```env
HELEKET_ENABLED=false
HELEKET_DISPLAY_NAME=Heleket Crypto
HELEKET_MERCHANT_ID=your_merchant_id
HELEKET_API_KEY=your_api_key
HELEKET_BASE_URL=https://api.heleket.com/v1
HELEKET_DEFAULT_CURRENCY=USDT
HELEKET_DEFAULT_NETWORK=
HELEKET_INVOICE_LIFETIME=3600
HELEKET_MARKUP_PERCENT=0
HELEKET_WEBHOOK_PATH=/heleket-webhook
HELEKET_WEBHOOK_HOST=0.0.0.0
HELEKET_WEBHOOK_PORT=8086
HELEKET_CALLBACK_URL=
HELEKET_RETURN_URL=
HELEKET_SUCCESS_URL=
```

---

### MulenPay

```env
MULENPAY_ENABLED=false
MULENPAY_DISPLAY_NAME=Mulen Pay
MULENPAY_API_KEY=your_api_key
MULENPAY_SECRET_KEY=your_secret_key
MULENPAY_SHOP_ID=123
MULENPAY_BASE_URL=https://mulenpay.ru/api
MULENPAY_WEBHOOK_PATH=/mulenpay-webhook
MULENPAY_DESCRIPTION=Пополнение баланса
MULENPAY_LANGUAGE=ru
MULENPAY_VAT_CODE=0
MULENPAY_PAYMENT_SUBJECT=4
MULENPAY_PAYMENT_MODE=4
MULENPAY_MIN_AMOUNT_KOPEKS=10000
MULENPAY_MAX_AMOUNT_KOPEKS=10000000
MULENPAY_IFRAME_EXPECTED_ORIGIN=https://mulenpay.ru
MULENPAY_WEBSITE_URL=https://your-cabinet-url.com

# Запрещённые слова в имени пользователя
DISPLAY_NAME_BANNED_KEYWORDS=КАЗИНО,СТАВКИ,CASINO,BET
```

---

### PAL24 (PayPalych)

```env
PAL24_ENABLED=false
PAL24_DISPLAY_NAME=PAL24
PAL24_API_TOKEN=your_api_token
PAL24_SHOP_ID=your_shop_id
PAL24_SIGNATURE_TOKEN=your_signature_token
PAL24_BASE_URL=https://pal24.pro/api/v1/
PAL24_WEBHOOK_PATH=/pal24-webhook
PAL24_PAYMENT_DESCRIPTION=Пополнение баланса
PAL24_MIN_AMOUNT_KOPEKS=10000
PAL24_MAX_AMOUNT_KOPEKS=100000000
PAL24_REQUEST_TIMEOUT=30
PAL24_SBP_BUTTON_VISIBLE=true
PAL24_CARD_BUTTON_VISIBLE=true
PAL24_SBP_BUTTON_TEXT=СБП
PAL24_CARD_BUTTON_TEXT=Карта
```

---

### Platega

```env
PLATEGA_ENABLED=false
PLATEGA_DISPLAY_NAME=Platega
PLATEGA_MERCHANT_ID=your_merchant_id
PLATEGA_SECRET=your_secret
PLATEGA_BASE_URL=https://app.platega.io
PLATEGA_RETURN_URL=
PLATEGA_FAILED_URL=
PLATEGA_CURRENCY=RUB
# Методы: 2-СБП(QR), 10-Карты RUB, 11-Банковские карты, 12-Международные, 13-Крипто
PLATEGA_ACTIVE_METHODS=2,10,11,12,13
PLATEGA_MIN_AMOUNT_KOPEKS=100
PLATEGA_MAX_AMOUNT_KOPEKS=100000000
PLATEGA_WEBHOOK_PATH=/platega-webhook
PLATEGA_WEBHOOK_HOST=0.0.0.0
PLATEGA_WEBHOOK_PORT=8086
```

---

### Wata

```env
WATA_ENABLED=false
WATA_DISPLAY_NAME=Wata
WATA_BASE_URL=https://api.wata.pro/api/h2h
WATA_ACCESS_TOKEN=your_access_token
WATA_TERMINAL_PUBLIC_ID=your_terminal_id
WATA_PAYMENT_DESCRIPTION=Пополнение баланса
WATA_PAYMENT_TYPE=all  # card, sbp, all
WATA_SUCCESS_REDIRECT_URL=
WATA_FAIL_REDIRECT_URL=
WATA_LINK_TTL_MINUTES=60
WATA_MIN_AMOUNT_KOPEKS=10000
WATA_MAX_AMOUNT_KOPEKS=10000000
WATA_REQUEST_TIMEOUT=30
WATA_WEBHOOK_PATH=/wata-webhook
WATA_WEBHOOK_HOST=0.0.0.0
WATA_WEBHOOK_PORT=8087
WATA_PUBLIC_KEY_CACHE_SECONDS=3600
```

---

### CloudPayments

```env
CLOUDPAYMENTS_ENABLED=false
CLOUDPAYMENTS_DISPLAY_NAME=CloudPayments
CLOUDPAYMENTS_PUBLIC_ID=your_public_id
CLOUDPAYMENTS_API_SECRET=your_api_secret
CLOUDPAYMENTS_API_URL=https://api.cloudpayments.ru
CLOUDPAYMENTS_WIDGET_URL=https://widget.cloudpayments.ru/show
CLOUDPAYMENTS_DESCRIPTION=Пополнение баланса
CLOUDPAYMENTS_CURRENCY=RUB
CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS=10000
CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS=10000000
CLOUDPAYMENTS_WEBHOOK_PATH=/cloudpayments-webhook
CLOUDPAYMENTS_WEBHOOK_HOST=0.0.0.0
CLOUDPAYMENTS_WEBHOOK_PORT=8089
CLOUDPAYMENTS_RETURN_URL=
CLOUDPAYMENTS_SKIN=mini  # mini, classic, modern
CLOUDPAYMENTS_REQUIRE_EMAIL=false
CLOUDPAYMENTS_TEST_MODE=false
```

---

### Freekassa

```env
FREEKASSA_ENABLED=false
FREEKASSA_DISPLAY_NAME=Freekassa
FREEKASSA_SHOP_ID=123456
FREEKASSA_API_KEY=your_api_key
FREEKASSA_SECRET_WORD_1=your_secret_1  # Для формы оплаты
FREEKASSA_SECRET_WORD_2=your_secret_2  # Для webhook
FREEKASSA_CURRENCY=RUB
FREEKASSA_MIN_AMOUNT_KOPEKS=10000
FREEKASSA_MAX_AMOUNT_KOPEKS=100000000
FREEKASSA_PAYMENT_TIMEOUT_SECONDS=3600
FREEKASSA_WEBHOOK_PATH=/freekassa-webhook
FREEKASSA_WEBHOOK_HOST=0.0.0.0
FREEKASSA_WEBHOOK_PORT=8088
# Способ оплаты: пусто = форма выбора, 42 = обычный СБП, 44 = NSPK СБП
FREEKASSA_PAYMENT_SYSTEM_ID=
FREEKASSA_USE_API=false
SERVER_PUBLIC_IP=  # Публичный IP для API
```

---

### KassaAI

```env
KASSA_AI_ENABLED=false
KASSA_AI_DISPLAY_NAME=KassaAI
KASSA_AI_SHOP_ID=123456
KASSA_AI_API_KEY=your_api_key
KASSA_AI_SECRET_WORD_2=your_secret
KASSA_AI_CURRENCY=RUB
KASSA_AI_MIN_AMOUNT_KOPEKS=10000
KASSA_AI_MAX_AMOUNT_KOPEKS=100000000
KASSA_AI_WEBHOOK_PATH=/kassa-ai-webhook
KASSA_AI_WEBHOOK_HOST=0.0.0.0
KASSA_AI_WEBHOOK_PORT=8089
# Способ оплаты: 44 = СБП (QR), 36 = Карты РФ, 43 = SberPay
KASSA_AI_PAYMENT_SYSTEM_ID=44
```

---

## Мониторинг трафика

### Быстрая проверка (дельта трафика)

```env
TRAFFIC_FAST_CHECK_ENABLED=false
TRAFFIC_FAST_CHECK_INTERVAL_MINUTES=10
TRAFFIC_FAST_CHECK_THRESHOLD_GB=5.0
```

---

### Суточная проверка

```env
TRAFFIC_DAILY_CHECK_ENABLED=false
TRAFFIC_DAILY_CHECK_TIME=00:00
TRAFFIC_DAILY_THRESHOLD_GB=50.0
```

---

### Топик для уведомлений

```env
SUSPICIOUS_NOTIFICATIONS_TOPIC_ID=14
```

---

### Фильтрация по серверам

```env
# Только эти ноды (UUID через запятую, пусто = все)
TRAFFIC_MONITORED_NODES=

# Исключить эти ноды
TRAFFIC_IGNORED_NODES=

# Исключить пользователей (UUID через запятую)
TRAFFIC_EXCLUDED_USER_UUIDS=
```

---

### Производительность

```env
TRAFFIC_CHECK_BATCH_SIZE=1000
TRAFFIC_CHECK_CONCURRENCY=10
TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES=60
TRAFFIC_SNAPSHOT_TTL_HOURS=24
```

---

### Устаревшие настройки (для обратной совместимости)

```env
TRAFFIC_MONITORING_ENABLED=false
TRAFFIC_THRESHOLD_GB_PER_DAY=10.0
TRAFFIC_MONITORING_INTERVAL_HOURS=24
```

---

## Статус серверов

### SERVER_STATUS_MODE
**Тип:** `string`  
**По умолчанию:** `disabled`  
**Допустимые значения:** `disabled`, `external_link`, `external_link_miniapp`, `xray`

Режим отображения статуса серверов:
- `disabled` — отключено
- `external_link` — ссылка на внешний мониторинг
- `external_link_miniapp` — ссылка в MiniApp
- `xray` — интеграция с XrayChecker

```env
SERVER_STATUS_MODE=xray
```

---

### SERVER_STATUS_EXTERNAL_URL
**Тип:** `string`

URL внешнего мониторинга.

```env
SERVER_STATUS_EXTERNAL_URL=https://status.example.com
```

---

### SERVER_STATUS_METRICS_URL
**Тип:** `string`

URL метрик XrayChecker.

```env
SERVER_STATUS_METRICS_URL=https://xray.example.com/metrics
```

---

### SERVER_STATUS_METRICS_USERNAME / SERVER_STATUS_METRICS_PASSWORD
**Тип:** `string` (опционально)

Basic Auth для метрик.

```env
SERVER_STATUS_METRICS_USERNAME=admin
SERVER_STATUS_METRICS_PASSWORD=password
```

---

### SERVER_STATUS_METRICS_VERIFY_SSL
**Тип:** `boolean`  
**По умолчанию:** `true`

Проверять SSL сертификат.

```env
SERVER_STATUS_METRICS_VERIFY_SSL=true
```

---

### SERVER_STATUS_REQUEST_TIMEOUT
**Тип:** `integer`  
**По умолчанию:** `10`

Таймаут запроса в секундах.

```env
SERVER_STATUS_REQUEST_TIMEOUT=15
```

---

### SERVER_STATUS_ITEMS_PER_PAGE
**Тип:** `integer`  
**По умолчанию:** `10`

Количество серверов на странице.

```env
SERVER_STATUS_ITEMS_PER_PAGE=20
```

---

## Режим техработ

### MAINTENANCE_MODE
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить режим технических работ.

```env
MAINTENANCE_MODE=true
```

---

### MAINTENANCE_MESSAGE
**Тип:** `string`  
**По умолчанию:** `🔧 Ведутся технические работы...`

Сообщение для пользователей.

```env
MAINTENANCE_MESSAGE=Сервис временно недоступен. Попробуйте через 30 минут.
```

---

### MAINTENANCE_CHECK_INTERVAL
**Тип:** `integer`  
**По умолчанию:** `30`

Интервал проверки доступности в секундах.

```env
MAINTENANCE_CHECK_INTERVAL=60
```

---

### MAINTENANCE_AUTO_ENABLE
**Тип:** `boolean`  
**По умолчанию:** `true`

Автоматически включать режим при недоступности панели.

```env
MAINTENANCE_AUTO_ENABLE=true
```

---

### MAINTENANCE_MONITORING_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Мониторинг доступности панели.

```env
MAINTENANCE_MONITORING_ENABLED=true
```

---

### MAINTENANCE_RETRY_ATTEMPTS
**Тип:** `integer`  
**По умолчанию:** `1`

Количество попыток проверки перед включением режима.

```env
MAINTENANCE_RETRY_ATTEMPTS=3
```

---

## Интерфейс и UX

### MAIN_MENU_MODE
**Тип:** `string`  
**По умолчанию:** `default`  
**Допустимые значения:** `default`, `cabinet`

Режим главного меню:
- `default` — классический режим (все кнопки внутри Telegram)
- `cabinet` — режим с MiniApp кабинетом

```env
MAIN_MENU_MODE=cabinet
```

---

### CABINET_BUTTON_STYLE
**Тип:** `string`  
**Допустимые значения:** `primary`, `success`, `danger`, пусто

Стиль кнопок в режиме Cabinet (Bot API 9.4).

```env
CABINET_BUTTON_STYLE=primary
```

---

### CONNECT_BUTTON_MODE
**Тип:** `string`  
**По умолчанию:** `miniapp_subscription`  
**Допустимые значения:** `guide`, `miniapp_subscription`, `miniapp_custom`, `link`, `happ_cryptolink`

Режим кнопки "Подключиться":
- `guide` — открывает гайд подключения
- `miniapp_subscription` — ссылка подписки в MiniApp
- `miniapp_custom` — кастомная ссылка в MiniApp
- `link` — ссылка напрямую в браузере
- `happ_cryptolink` — cryptoLink для Happ

```env
CONNECT_BUTTON_MODE=miniapp_subscription
```

---

### MINIAPP_CUSTOM_URL
**Тип:** `string`

Кастомный URL для MiniApp.

```env
MINIAPP_CUSTOM_URL=https://app.example.com
```

---

### MINIAPP_PURCHASE_URL
**Тип:** `string` (опционально)

URL страницы покупки в MiniApp.

```env
MINIAPP_PURCHASE_URL=https://app.example.com/buy
```

---

### MINIAPP_STATIC_PATH
**Тип:** `string`  
**По умолчанию:** `miniapp`

Путь к статическим файлам MiniApp.

```env
MINIAPP_STATIC_PATH=miniapp
```

---

### MINIAPP_SERVICE_NAME_EN / MINIAPP_SERVICE_NAME_RU
**Тип:** `string`

Название сервиса в MiniApp.

```env
MINIAPP_SERVICE_NAME_EN=My VPN Service
MINIAPP_SERVICE_NAME_RU=Мой VPN Сервис
```

---

### MINIAPP_SERVICE_DESCRIPTION_EN / MINIAPP_SERVICE_DESCRIPTION_RU
**Тип:** `string`

Описание сервиса в MiniApp.

```env
MINIAPP_SERVICE_DESCRIPTION_EN=Secure & Fast Connection
MINIAPP_SERVICE_DESCRIPTION_RU=Безопасное и быстрое подключение
```

---

### Режим happ_cryptolink

```env
CONNECT_BUTTON_HAPP_DOWNLOAD_ENABLED=false
HAPP_CRYPTOLINK_REDIRECT_TEMPLATE=https://sub.domain.com/redirect/?redirect_to=
HAPP_DOWNLOAD_LINK_IOS=https://apps.apple.com/app/happ
HAPP_DOWNLOAD_LINK_ANDROID=https://play.google.com/store/apps/details?id=happ
HAPP_DOWNLOAD_LINK_MACOS=https://github.com/happ/releases/macos
HAPP_DOWNLOAD_LINK_WINDOWS=https://github.com/happ/releases/windows
HAPP_DOWNLOAD_LINK_PC=https://github.com/happ/releases  # Универсальная для ПК
```

---

### HIDE_SUBSCRIPTION_LINK
**Тип:** `boolean`  
**По умолчанию:** `false`

Скрыть ссылку подключения в информации о подписке.

```env
HIDE_SUBSCRIPTION_LINK=true
```

---

### MENU_LAYOUT_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Включить управление меню через API.

```env
MENU_LAYOUT_ENABLED=true
```

---

### ENABLE_LOGO_MODE
**Тип:** `boolean`  
**По умолчанию:** `true`

Показывать логотип в сообщениях.

```env
ENABLE_LOGO_MODE=true
```

---

### LOGO_FILE
**Тип:** `string`  
**По умолчанию:** `vpn_logo.png`

Файл логотипа.

```env
LOGO_FILE=my_logo.png
```

---

### SKIP_RULES_ACCEPT
**Тип:** `boolean`  
**По умолчанию:** `false`

Пропустить принятие правил при старте.

```env
SKIP_RULES_ACCEPT=true
```

---

### SKIP_REFERRAL_CODE
**Тип:** `boolean`  
**По умолчанию:** `false`

Пропустить запрос реферального кода.

```env
SKIP_REFERRAL_CODE=true
```

---

### DISABLE_WEB_PAGE_PREVIEW
**Тип:** `boolean`  
**По умолчанию:** `false`

Отключить превью ссылок в сообщениях.

```env
DISABLE_WEB_PAGE_PREVIEW=true
```

---

### ACTIVATE_BUTTON_VISIBLE
**Тип:** `boolean`  
**По умолчанию:** `false`

Показывать кнопку активации.

```env
ACTIVATE_BUTTON_VISIBLE=true
```

---

### ACTIVATE_BUTTON_TEXT
**Тип:** `string`  
**По умолчанию:** `активировать`

Текст кнопки активации.

```env
ACTIVATE_BUTTON_TEXT=Активировать VPN
```

---

### PRICE_ROUNDING_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Округление цен при отображении (≤50 коп вниз, >50 коп вверх).

```env
PRICE_ROUNDING_ENABLED=true
```

---

## Локализация

### DEFAULT_LANGUAGE
**Тип:** `string`  
**По умолчанию:** `ru`

Язык по умолчанию.

```env
DEFAULT_LANGUAGE=ru
```

---

### AVAILABLE_LANGUAGES
**Тип:** `string`  
**По умолчанию:** `ru,en,ua,zh,fa`

Доступные языки через запятую.

```env
AVAILABLE_LANGUAGES=ru,en,ua,zh,fa
```

---

### LANGUAGE_SELECTION_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Включить выбор языка при старте и кнопку в меню.

```env
LANGUAGE_SELECTION_ENABLED=true
```

---

## Логирование

### LOG_LEVEL
**Тип:** `string`  
**По умолчанию:** `INFO`

Уровень логирования.

```env
LOG_LEVEL=DEBUG
```

---

### LOG_FILE
**Тип:** `string`  
**По умолчанию:** `logs/bot.log`

Путь к файлу логов.

```env
LOG_FILE=logs/bot.log
```

---

### LOG_COLORS
**Тип:** `boolean`  
**По умолчанию:** `true`

ANSI-цвета в консоли.

```env
LOG_COLORS=true
```

---

### DEBUG
**Тип:** `boolean`  
**По умолчанию:** `false`

Режим отладки.

```env
DEBUG=true
```

---

## Ротация логов

```env
# Включить ротацию
LOG_ROTATION_ENABLED=false

# Время ротации (HH:MM)
LOG_ROTATION_TIME=00:00

# Хранить архивы N дней
LOG_ROTATION_KEEP_DAYS=7

# Сжимать архивы
LOG_ROTATION_COMPRESS=true

# Отправлять в Telegram
LOG_ROTATION_SEND_TO_TELEGRAM=false
LOG_ROTATION_CHAT_ID=
LOG_ROTATION_TOPIC_ID=

# Пути к файлам
LOG_DIR=logs
LOG_INFO_FILE=info.log
LOG_WARNING_FILE=warning.log
LOG_ERROR_FILE=error.log
LOG_PAYMENTS_FILE=payments.log
```

---

## Бэкапы

```env
BACKUP_AUTO_ENABLED=true
BACKUP_INTERVAL_HOURS=24
BACKUP_TIME=03:00
BACKUP_MAX_KEEP=7
BACKUP_COMPRESSION=true
BACKUP_INCLUDE_LOGS=false
BACKUP_LOCATION=/app/data/backups

# Отправка в Telegram
BACKUP_SEND_ENABLED=true
BACKUP_SEND_CHAT_ID=-1001234567890
BACKUP_SEND_TOPIC_ID=123

# Шифрование (AES)
BACKUP_ARCHIVE_PASSWORD=your_password
```

---

## Webhook режим бота

```env
BOT_RUN_MODE=polling  # polling или webhook

# Настройки webhook
WEBHOOK_URL=https://your-domain.com
WEBHOOK_PATH=/webhook
WEBHOOK_SECRET_TOKEN=your_secret_token
WEBHOOK_DROP_PENDING_UPDATES=true
WEBHOOK_MAX_QUEUE_SIZE=1024
WEBHOOK_WORKERS=4
WEBHOOK_ENQUEUE_TIMEOUT=0.1
WEBHOOK_WORKER_SHUTDOWN_TIMEOUT=30.0
```

---

## Web API

```env
WEB_API_ENABLED=false
WEB_API_HOST=0.0.0.0
WEB_API_PORT=8080
WEB_API_WORKERS=1
WEB_API_ALLOWED_ORIGINS=*
WEB_API_DOCS_ENABLED=false
WEB_API_TITLE=Remnawave Bot Admin API
WEB_API_VERSION=1.0.0
WEB_API_DEFAULT_TOKEN=
WEB_API_DEFAULT_TOKEN_NAME=Bootstrap Token
WEB_API_TOKEN_HASH_ALGORITHM=sha256
WEB_API_REQUEST_LOGGING=true
```

---

### Внешний админ-токен

Для интеграции с другими ботами/системами.

```env
EXTERNAL_ADMIN_TOKEN=your_external_token
EXTERNAL_ADMIN_TOKEN_BOT_ID=123456789
```

---

## Ban System (BedolagaBan)

```env
BAN_SYSTEM_ENABLED=false
BAN_SYSTEM_API_URL=http://ban-server:8000
BAN_SYSTEM_API_TOKEN=your_api_token
BAN_SYSTEM_REQUEST_TIMEOUT=30
```

---

### Шаблоны сообщений о банах

```env
# Блокировка за превышение лимита устройств
# Плейсхолдеры: {ip_count}, {limit}, {ban_minutes}, {node_info}
BAN_MSG_PUNISHMENT=🚫 <b>АККАУНТ ЗАБЛОКИРОВАН</b>...

# Разблокировка
BAN_MSG_ENABLED=✅ <b>АККАУНТ РАЗБЛОКИРОВАН</b>...

# Блокировка за WiFi
# Плейсхолдеры: {ban_minutes}, {network_info}, {node_info}
BAN_MSG_WIFI=🚫 <b>АККАУНТ ЗАБЛОКИРОВАН</b>...

# Блокировка за мобильную сеть
BAN_MSG_MOBILE=🚫 <b>АККАУНТ ЗАБЛОКИРОВАН</b>...

# Предупреждение
# Плейсхолдеры: {warning_message}
BAN_MSG_WARNING=⚠️ <b>ПРЕДУПРЕЖДЕНИЕ</b>...
```

---

## Чёрный список

```env
BLACKLIST_CHECK_ENABLED=false
BLACKLIST_GITHUB_URL=https://raw.githubusercontent.com/.../blacklist.txt
BLACKLIST_UPDATE_INTERVAL_HOURS=24
BLACKLIST_IGNORE_ADMINS=true
```

---

### DISPOSABLE_EMAIL_CHECK_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `true`

Проверка на одноразовые email при регистрации.

```env
DISPOSABLE_EMAIL_CHECK_ENABLED=true
```

---

## Конкурсы

```env
CONTESTS_ENABLED=false
CONTESTS_BUTTON_VISIBLE=false
REFERRAL_CONTESTS_ENABLED=false  # Устаревшее, используйте CONTESTS_ENABLED
```

---

## Прочие настройки

### APP_CONFIG_PATH
**Тип:** `string`  
**По умолчанию:** `app-config.json`

Путь к конфигурации приложений.

```env
APP_CONFIG_PATH=app-config.json
```

---

### ENABLE_DEEP_LINKS
**Тип:** `boolean`  
**По умолчанию:** `true`

Включить deep links.

```env
ENABLE_DEEP_LINKS=true
```

---

### APP_CONFIG_CACHE_TTL
**Тип:** `integer`  
**По умолчанию:** `3600`

Время кэширования app-config в секундах.

```env
APP_CONFIG_CACHE_TTL=7200
```

---

### Проверка обновлений

```env
VERSION_CHECK_ENABLED=true
VERSION_CHECK_REPO=fr1ngg/remnawave-bedolaga-telegram-bot
VERSION_CHECK_INTERVAL_HOURS=1
```

---

### Уведомления и мониторинг

```env
MONITORING_INTERVAL=60
INACTIVE_USER_DELETE_MONTHS=3
ENABLE_NOTIFICATIONS=true
NOTIFICATION_RETRY_ATTEMPTS=3
MONITORING_LOGS_RETENTION_DAYS=30
NOTIFICATION_CACHE_HOURS=24
```

---

### AUTO_PURCHASE_AFTER_TOPUP_ENABLED
**Тип:** `boolean`  
**По умолчанию:** `false`

Автоматическая покупка из сохранённой корзины после пополнения баланса.

```env
AUTO_PURCHASE_AFTER_TOPUP_ENABLED=true
```

---

## Примечания

1. **Все цены указываются в копейках** (100 копеек = 1 рубль)
2. **Обязательные переменные:** `BOT_TOKEN`, `ADMIN_IDS`, `REMNAWAVE_API_URL`, `REMNAWAVE_API_KEY`
3. **Для Docker** рекомендуется использовать `DATABASE_MODE=auto`
4. **Секреты** (токены, пароли) не должны содержать специальных символов, которые могут быть неправильно интерпретированы

---

## Пример минимального .env файла

```env
# Telegram
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
ADMIN_IDS=123456789

# RemnaWave
REMNAWAVE_API_URL=https://panel.example.com
REMNAWAVE_API_KEY=your_api_key

# База данных
DATABASE_MODE=auto
REDIS_URL=redis://redis:6379/0

# Часовой пояс
TZ=Europe/Moscow
```

---

## Пример полного .env файла

Смотрите файл `.env.example` в корне проекта.

---

*Документация актуальна для версии бота на февраль 2026 года.*

