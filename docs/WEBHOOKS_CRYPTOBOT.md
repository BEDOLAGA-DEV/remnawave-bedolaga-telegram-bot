# Настройка вебхуков и Crypto Bot (Crypto Pay)

## Общая схема

1. Бот поднимает **единый веб-сервер** (порт 8080), если включён хотя бы один платёжный вебхук или Web API.
2. **Crypto Pay** шлёт уведомления об оплатах на твой URL: `WEBHOOK_URL` + `CRYPTOBOT_WEBHOOK_PATH`.
3. URL должен быть **доступен из интернета по HTTPS** (домен или туннель).

---

## Шаг 1: Публичный URL (HTTPS)

Вебхуки должны принимать запросы извне. Варианты:

- **Свой домен** (например `https://bot.ghostvpn.com`) с reverse proxy (Nginx/Caddy) на порт 8080 контейнера.
- **Туннель** (ngrok, cloudflared и т.п.):
  - ngrok: `ngrok http 8080` → получишь `https://xxxx.ngrok.io`
  - В `.env` задаёшь `WEBHOOK_URL=https://xxxx.ngrok.io` (без слэша в конце).

Без публичного HTTPS URL Crypto Bot не сможет достучаться до бота.

---

## Шаг 2: Переменные в `.env`

```env
# Базовый URL, по которому бот доступен снаружи (обязательно HTTPS)
WEBHOOK_URL=https://твой-домен-или-туннель.ру

# ===== CRYPTO BOT =====
CRYPTOBOT_ENABLED=true
CRYPTOBOT_API_TOKEN=123456789:AAzQcZWQqQAbsfgPnOLr4FHC8Doa4L7KryC
CRYPTOBOT_WEBHOOK_SECRET=твой_секрет_любая_длинная_строка
CRYPTOBOT_WEBHOOK_PATH=/cryptobot-webhook
```

- **CRYPTOBOT_API_TOKEN** — берёшь в [@CryptoBot](https://t.me/CryptoBot) → Crypto Pay → Create App → API Token.
- **CRYPTOBOT_WEBHOOK_SECRET** — придумываешь сам (например случайная строка 32+ символов). Этот же секрет потом укажешь в настройках приложения в Crypto Pay.

Порт **8080** уже проброшен в `docker-compose` (`WEB_API_PORT`). Включать **WEB_API_ENABLED** не обязательно — веб-сервер поднимется автоматически, когда включён Crypto Bot.

---

## Шаг 3: Регистрация webhook в Crypto Pay

1. Открой [@CryptoBot](https://t.me/CryptoBot) → Crypto Pay → твоё приложение (App).
2. В настройках приложения найди раздел **Webhook** / **Webhook URL**.
3. Укажи URL:
   ```
   https://твой-домен-или-туннель.ру/cryptobot-webhook
   ```
   То есть: `WEBHOOK_URL` + значение `CRYPTOBOT_WEBHOOK_PATH` (по умолчанию `/cryptobot-webhook`).
4. Если в Crypto Pay просят **Secret** — укажи то же значение, что и **CRYPTOBOT_WEBHOOK_SECRET** в `.env`.

Документация Crypto Pay: https://help.crypt.bot/crypto-pay-api (раздел про webhooks).

---

## Шаг 4: Перезапуск бота

После правок `.env`:

```bash
docker compose up -d --force-recreate --no-deps bot
```

Проверка логов:

```bash
docker compose logs -f bot
```

Должны появиться строки про запуск веб-сервера и, при включённом Crypto Bot, про `CryptoBot webhook` / платёжные webhook-и.

---

## Проверка

- В логах при старте: блок «Активные webhook endpoints» — должна быть строка с CryptoBot и путём `/cryptobot-webhook`.
- После оплаты через Crypto Pay в боте приходит webhook от Crypto Pay; если URL и секрет верные, платёж обработается и подписка/баланс обновятся.

---

## Локальный тест (туннель)

1. Запусти туннель на порт 8080, например: `ngrok http 8080`.
2. В `.env`: `WEBHOOK_URL=https://xxxx.ngrok-free.app` (адрес из вывода ngrok).
3. В Crypto Pay в качестве Webhook URL укажи: `https://xxxx.ngrok-free.app/cryptobot-webhook`.
4. Перезапусти бота и проверь оплату.
