# Перенос бота на свой сервер и настройка вебхуков

Цели:
1. **Перенести** бота (Docker Compose) на твой сервер.
2. **Выдать в интернет** веб-сервер бота по домену с HTTPS (reverse proxy).
3. **Настроить вебхуки** (Crypto Bot и при необходимости Telegram webhook).

---

## Чеклист

- [ ] Сервер: Docker и Docker Compose установлены
- [ ] Домен указывает на IP сервера (A-запись)
- [ ] Проект на сервере: код + `.env` с продакшен-значениями
- [ ] Reverse proxy (Nginx/Caddy) с SSL, проксирует на `localhost:8080`
- [ ] В `.env`: `WEBHOOK_URL=https://твой-домен.ru`
- [ ] В `.env`: платёжные вебхуки (Crypto Bot и др.) включены и настроены
- [ ] В панелях платежек (Crypto Pay и т.д.) прописан URL вебхука
- [ ] Бот запущен через `docker compose up -d`, логи без ошибок

---

## 1. Подготовка сервера

### 1.1 Установка Docker и Docker Compose

На Ubuntu/Debian:

```bash
sudo apt update && sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER
```

Выйди из SSH и зайди снова, чтобы группа `docker` применилась.

### 1.2 Домен

- В панели регистратора домена создай **A-запись**: `твой-домен.ru` (или `bot.твой-домен.ru`) → IP твоего сервера.
- Подожди распространения DNS (до нескольких часов).

---

## 2. Размещение проекта на сервере

### 2.1 Клонирование / копирование

Через Git (если проект в репозитории):

```bash
cd /opt   # или /home/ubuntu — как удобно
sudo mkdir -p /opt/ghostvpnbot && sudo chown $USER:$USER /opt/ghostvpnbot
git clone https://github.com/твой-репо/ghostvpnbot.git /opt/ghostvpnbot
cd /opt/ghostvpnbot
```

Если без Git — скопируй папку проекта с локальной машины на сервер (scp/rsync), включая `.env`, `docker-compose.yml`, `Dockerfile`, каталоги `app/`, `locales/`, `vpn_logo.png` и т.д.

### 2.2 Файл `.env` на сервере

- Скопируй свой рабочий `.env` в корень проекта на сервере.
- Обязательно поправь под продакшен:

```env
# Бот и БД
BOT_TOKEN=реальный_токен_от_BotFather
ADMIN_IDS=твой_telegram_id
POSTGRES_PASSWORD=надёжный_пароль

# RemnaWave (если используешь)
REMNAWAVE_API_URL=https://твой-remnawave-адрес
REMNAWAVE_API_KEY=ключ

# URL для вебхуков — твой домен с HTTPS (без слэша в конце)
WEBHOOK_URL=https://bot.твой-домен.ru

# Crypto Bot (если подключаешь)
CRYPTOBOT_ENABLED=true
CRYPTOBOT_API_TOKEN=токен_из_@CryptoBot
CRYPTOBOT_WEBHOOK_SECRET=длинный_секрет_32_символа
```

Остальные переменные (цены, периоды, уведомления) оставь как уже настроено или поправь по необходимости.

---

## 3. Reverse proxy и HTTPS

Веб-сервер бота слушает порт **8080** внутри контейнера. Снаружи он должен быть доступен по **HTTPS** на твоём домене. Для этого перед ботом ставится Nginx (или Caddy).

### 3.1 Установка Nginx и Certbot (SSL)

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 3.2 Конфиг Nginx для бота

Создай файл (подставь свой домен):

```bash
sudo nano /etc/nginx/sites-available/ghostvpnbot
```

Содержимое (замени `bot.твой-домен.ru` на свой домен):

```nginx
server {
    listen 80;
    server_name bot.твой-домен.ru;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
```

Включи сайт и проверь конфиг:

```bash
sudo ln -s /etc/nginx/sites-available/ghostvpnbot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 3.3 Получение SSL (Let's Encrypt)

```bash
sudo certbot --nginx -d bot.твой-домен.ru
```

Certbot сам поправит конфиг Nginx для HTTPS. После этого сайт будет открываться по `https://bot.твой-домен.ru`.

### 3.4 Альтернатива: Caddy

Если используешь Caddy, он сам получает SSL. Пример Caddyfile:

```
bot.твой-домен.ru {
    reverse_proxy localhost:8080
}
```

---

## 4. Запуск бота и проверка вебхуков

### 4.1 Запуск

В каталоге проекта на сервере:

```bash
cd /opt/ghostvpnbot
docker compose up -d --build
```

Проверка логов:

```bash
docker compose logs -f bot
```

Убедись, что в логах есть блок «Активные webhook endpoints» и в нём указан путь для Crypto Bot (и других включённых платёжных вебхуков).

### 4.2 Проверка доступности снаружи

- В браузере: `https://bot.твой-домен.ru/health` — должен ответить сервер (например 200 или 401 без краша).
- В `.env` должно быть: `WEBHOOK_URL=https://bot.твой-домен.ru` (без слэша в конце).

---

## 5. Регистрация вебхуков у платёжных систем

### Crypto Bot (Crypto Pay)

1. [@CryptoBot](https://t.me/CryptoBot) → Crypto Pay → твоё приложение.
2. В настройках приложения укажи **Webhook URL**:
   ```
   https://bot.твой-домен.ru/cryptobot-webhook
   ```
3. Если есть поле для секрета — укажи то же значение, что и `CRYPTOBOT_WEBHOOK_SECRET` в `.env`.

### Другие платёжные системы

Для каждой системы в её панели укажи URL вида:

- `https://bot.твой-домен.ru` + путь вебхука из `.env` (например `/yookassa-webhook`, `/pal24-webhook` и т.д.).

Пути смотри в `.env` (переменные `*_WEBHOOK_PATH`).

---

## 6. Опционально: Telegram в режиме webhook

Если хочешь, чтобы и обновления бота приходили по HTTPS (вместо long polling):

```env
BOT_RUN_MODE=webhook
WEBHOOK_URL=https://bot.твой-домен.ru
WEBHOOK_PATH=/webhook
WEBHOOK_SECRET_TOKEN=случайная_длинная_строка
```

Тогда Telegram будет слать обновления на `https://bot.твой-домен.ru/webhook`. Nginx уже проксирует весь трафик на 8080, отдельный конфиг для `/webhook` не нужен.

---

## 7. Файрвол

Если включён ufw, открой только SSH, HTTP и HTTPS; порт 8080 наружу можно не открывать (доступ только через Nginx на localhost):

```bash
sudo ufw allow 22
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

---

## 8. Автозапуск

При перезагрузке сервера контейнеры поднимутся сами, если использовался `docker compose up -d` (в `docker-compose.yml` у сервисов стоит `restart: unless-stopped`).

Полезные команды:

```bash
cd /opt/ghostvpnbot
docker compose ps          # статус
docker compose logs -f bot # логи
docker compose down        # остановить
docker compose up -d       # запустить снова
```

---

## Итог

1. Сервер подготовлен (Docker, домен → IP).
2. Проект на сервере, `.env` с `WEBHOOK_URL=https://bot.твой-домен.ru` и нужными токенами.
3. Nginx (или Caddy) отдаёт HTTPS и проксирует на `localhost:8080`.
4. Бот запущен через `docker compose up -d`.
5. В панелях Crypto Pay (и других платёжек) прописаны URL вебхуков.

После этого вебхуки будут приходить на твой домен по HTTPS, а бот — обрабатывать платежи и при необходимости работать в режиме webhook для Telegram.
