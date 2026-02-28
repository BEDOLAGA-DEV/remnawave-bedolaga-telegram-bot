# SHKeeper: развёртывание и подключение к боту

Этот документ описывает полный путь:

1. поднять SHKeeper,
2. выдать API-ключ,
3. подключить SHKeeper к Bedolaga Bot,
4. проверить оплату и callback.

## 1. Что нужно заранее

- сервер с публичным HTTPS-доменом для SHKeeper (например, `https://pay.example.com`);
- сервер с ботом и публичным HTTPS-доменом для webhook бота (например, `https://bot.example.com`);
- доступ к настройкам reverse proxy (Nginx/Caddy/Traefik);
- Telegram-бот уже запущен и доступен по HTTP API (обычно порт `8080`).

## 2. Развёртывание SHKeeper

Официальный способ из репозитория SHKeeper — через `k3s` и `helm`.

Оригинальные инструкции:
- [README SHKeeper](https://github.com/vsys-host/shkeeper.io)
- [Install with k3s](https://github.com/vsys-host/shkeeper.io/blob/master/docs/en/install-with-k3s.md)
- [Install from source](https://github.com/vsys-host/shkeeper.io/blob/master/docs/en/install-from-source.md)
- [How to use API](https://github.com/vsys-host/shkeeper.io/blob/master/docs/en/how-to-use-api.md)

### Быстрый путь (k3s)

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable=traefik" sh -s -
sudo chmod 644 /etc/rancher/k3s/k3s.yaml

helm repo add shkeeper https://helmrepo.shkeeper.app
helm install shkeeper shkeeper/shkeeper \
  --namespace shkeeper --create-namespace \
  --set ingress.enabled=true \
  --set ingress.hosts[0].host=pay.example.com
```

После запуска:
- откройте веб-интерфейс SHKeeper;
- создайте пользователя/кошелёк;
- в настройках пользователя создайте `API key` для REST API.

## 3. Как бот использует SHKeeper

В интеграции бота используются:
- создание инвойса: `POST /api/v1/{crypto}/payment_request`;
- проверка статуса: `GET /api/v1/invoices/{external_id}`;
- callback в бот с заголовком `X-Shkeeper-API-Key`.

При создании платежа в рублях бот автоматически конвертирует сумму в USD по курсу (внутренний конвертер бота) и отправляет сумму в SHKeeper.

## 4. Настройка `.env` в боте

Добавьте в `.env`:

```env
SHKEEPER_ENABLED=true
SHKEEPER_DISPLAY_NAME=SHKeeper
SHKEEPER_BASE_URL=https://pay.example.com
SHKEEPER_API_KEY=ваш_api_key_из_shkeeper

# Отдельный ключ для callback (рекомендуется).
# Если пусто, используется SHKEEPER_API_KEY.
SHKEEPER_CALLBACK_API_KEY=ваш_callback_key

# Криптовалюта/сеть для инвойсов SHKeeper
SHKEEPER_CRYPTO=USDT

SHKEEPER_MIN_AMOUNT_KOPEKS=10000
SHKEEPER_MAX_AMOUNT_KOPEKS=100000000
SHKEEPER_REQUEST_TIMEOUT=30

SHKEEPER_WEBHOOK_PATH=/shkeeper-webhook
SHKEEPER_WEBHOOK_HOST=0.0.0.0
SHKEEPER_WEBHOOK_PORT=8090
```

Обязательные условия:
- `SHKEEPER_ENABLED=true`;
- `SHKEEPER_API_KEY` заполнен;
- `WEBHOOK_URL` у бота должен быть корректным публичным HTTPS URL.

Пример:

```env
WEBHOOK_URL=https://bot.example.com
SHKEEPER_WEBHOOK_PATH=/shkeeper-webhook
```

В этом случае SHKeeper callback-адрес уходит в инвойс как:

`https://bot.example.com/shkeeper-webhook`

## 5. Перезапуск (миграции автоматически)

После изменения `.env`:

```bash
make reload
```

`make reload` достаточно: при старте бот сам применяет Alembic-миграции автоматически.

Или через docker compose:

```bash
docker compose up -d --build
```

## 6. Reverse proxy для webhook (Caddy/Nginx)

Ниже примеры, как опубликовать endpoint webhook бота `https://bot.example.com/shkeeper-webhook`.

### 6.1 Caddy

Если весь домен бота уже проксируется на `127.0.0.1:8080`, отдельный блок не нужен.  
Если хотите явно выделить webhook-путь:

```caddyfile
bot.example.com {
    encode gzip

    handle /shkeeper-webhook* {
        reverse_proxy 127.0.0.1:8080
    }

    # Остальные API/webhook пути бота
    handle {
        reverse_proxy 127.0.0.1:8080
    }
}
```

### 6.2 Nginx

```nginx
server {
    server_name bot.example.com;

    location /shkeeper-webhook {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Остальные API/webhook пути бота
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Важно:
- endpoint должен быть доступен извне по HTTPS;
- путь должен совпадать с `SHKEEPER_WEBHOOK_PATH`;
- в SHKeeper callback URL должен указывать на домен бота, а не на внутренний IP.

## 7. Что включается в интерфейсе бота

После успешной настройки:
- в Telegram-меню пополнения появится метод `SHKeeper`;
- метод будет доступен в Cabinet/MiniApp;
- callback начнёт обрабатываться через `POST /shkeeper-webhook`.

## 8. Проверка работоспособности

### 8.1 Проверка health

```bash
curl -s https://bot.example.com/health/payment-webhooks
```

Ожидается поле:
- `"shkeeper_enabled": true`

### 8.2 Тестовый callback в бот

```bash
curl -i -X POST "https://bot.example.com/shkeeper-webhook" \
  -H "Content-Type: application/json" \
  -H "X-Shkeeper-API-Key: ваш_callback_key" \
  -d '{
    "external_id": "shk_1_test",
    "id": "test-invoice-id",
    "status": "PAID",
    "paid": true
  }'
```

Ожидается HTTP `202 Accepted` (callback принят).

## 9. Частые проблемы

### `invalid_signature` на webhook

Проверьте:
- заголовок `X-Shkeeper-API-Key`;
- совпадает ли он с `SHKEEPER_CALLBACK_API_KEY` (или с `SHKEEPER_API_KEY`, если callback key пустой).

### Метод не появился в боте

Проверьте:
- `SHKEEPER_ENABLED=true`;
- `SHKEEPER_API_KEY` не пустой;
- бот перезапущен после изменения `.env`;
- в админке способ оплаты не отключён (Payment Method Config).

### Ошибка создания платежа

Проверьте:
- доступность `SHKEEPER_BASE_URL` из контейнера бота;
- корректность `SHKEEPER_CRYPTO`;
- логи бота (`logs/payments.log` и общий лог).

### Повторные callback от SHKeeper

Бот возвращает `202` на успешно принятый callback.  
Если повторы остаются, проверьте сетевые ошибки между SHKeeper и ботом (TLS, proxy, firewall, timeout).
