# 🚀 Remnawave Bedolaga Bot

> **🆕 Новый веб-кабинет (Cabinet WebApp) https://github.com/BEDOLAGA-DEV/bedolaga-cabinet/**
>
> Вышла новая версия личного кабинета пользователя — веб-интерфейс для управления подписками!
>
> **Переменные окружения для Cabinet:**
> | Переменная | Описание | По умолчанию |
> |------------|----------|--------------|
> | `CABINET_ENABLED` | Включить личный кабинет | `false` |
> | `CABINET_JWT_SECRET` | Секретный ключ для JWT токенов | `BOT_TOKEN` |
> | `CABINET_ACCESS_TOKEN_EXPIRE_MINUTES` | Время жизни access token (минуты) | `15` |
> | `CABINET_REFRESH_TOKEN_EXPIRE_DAYS` | Время жизни refresh token (дни) | `7` |
> | `CABINET_ALLOWED_ORIGINS` | Разрешённые origins для CORS | — |
> | `CABINET_EMAIL_VERIFICATION_ENABLED` | Включить верификацию email | `false` |
> | `CABINET_EMAIL_VERIFICATION_EXPIRE_HOURS` | Время жизни токена верификации email (часы) | `24` |
> | `CABINET_PASSWORD_RESET_EXPIRE_HOURS` | Время жизни токена сброса пароля (часы) | `1` |
>
> Для работы email-уведомлений настройте SMTP-переменные: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`, `SMTP_USE_TLS`.

<div align="center">

<img width="1024" height="1024" alt="Image" src="https://github.com/user-attachments/assets/17ad0128-231d-4553-9f4b-ce0644da796c" />

**🤖 Современный Telegram-бот для управления VPN подписками через Remnawave API**

_Полнофункциональное решение с управлением пользователями, платежами и администрированием_

[![Python](https://img.shields.io/badge/Python-3.13+-blue?logo=python&logoColor=white)](https://python.org)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15+-blue?logo=postgresql&logoColor=white)](https://postgresql.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/Fr1ngg/remnawave-bedolaga-telegram-bot?style=social)](https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot/stargazers)

[🚀 Быстрый старт](#-быстрый-старт) • [📖 Функционал](#-функционал) • [🳠Docker](#-docker-развертывание) • [💻 Локальная разработка](#-локальная-разработка) • [💬 Поддержка](#-поддержка-и-сообщество)

</div>

---

## 🧪 [Тестирование бота](https://t.me/zero_ping_vpn_bot?start=Git)

## 💬 **[Bedolaga Chat](https://t.me/+wTdMtSWq8YdmZmVi)** - Для общения, вопросов, предложений

---

## 🌟 Почему Bedolaga?

Бот Бедолага не добрый и не милый.
Он просто делает вашу работу вместо вас, принимает оплату, выдаёт подписки, интегрируется с Remnawave и тихо ненавидит всех, кто ещё не подключил его.

Вы хотите продавать VPN — Бедолага позволит это делать.
Вы хотите спать — он позволит и это...

### ⚡ **Полная автоматизация VPN бизнеса**

- 🎯 **Готовое решение** - разверни за 5 минут, начни продавать сегодня
- 💰 **Многоканальные платежи** - Telegram Stars + Tribute + CryptoBot + Heleket + YooKassa (СБП + карты) + MulenPay + PayPalych (СБП + карты) + Platega (карты + СБП) + WATA + Freekassa (NSPK СБП + карты) + CloudPayments (карты + СБП)
- 🔄 **Автоматизация 99%** - от регистрации до продления подписок
- 📱 **MiniApp лк** - личный кабинет с возможностью покупки/продления подписки
- 📊 **Детальная аналитика** - полная картина вашего бизнеса
- 💬 **Уведомления в топики** об: Активация триала 💎 Покупка подписки 🔄 Конверсия из триала в платную ⏰ Продление подписки 💰 Пополнение баланса 🚧 Включении тех работ ♻️ Появлении новой версии бота

### 🎛️ **Гибкость конфигурации**

- 🌐 **Умный выбор серверов** - автоматический пропуск при одном сервере, мультивыбор
- 📱 **Управление устройствами** - от 1 до неограниченного количества
- 📊 **Режимы продажи трафика** - фиксированный лимит или выбор пакетов
- 🎯 **Режим тарифов** - готовые тарифные планы с фиксированными параметрами
- 🎁 **Промо-система** - коды на деньги, дни подписки, триал-периоды
- 🔧 **Гибкие тарифы** - от 5GB до безлимита, от 14 дней до года
- 🛒 **Умная корзина** - сохранение параметров подписки при недостатке баланса

### 💪 **Enterprise готовность**

- 🗃️ **Современная архитектура** - AsyncIO, PostgreSQL, Redis, модульная структура
- 🔒 **Безопасность** - интеграция с системой защиты панели через куки-аутентификацию
- 📈 **Масштабируемость** - от стартапа до крупного бизнеса
- 🔧 **Мониторинг** - автоматическое управление режимом тех. работ
- 🛡️ **Защита панели** - поддержка [remnawave-reverse-proxy](https://github.com/eGamesAPI/remnawave-reverse-proxy)
- 🗄️ **Бекапы/Восстановление** - автобекапы и восстановление бд прямо в боте с уведомлениями в топики
- ✔️ **Проверка на подписку** - проверяет подписку на канал
- 🔄 **Автосинхронизация** - фоновая синхронизация подписок и серверов(сквадов) с Remnawave по расписанию
- 📝 **Ротация логов** - автоматическая очистка и архивация старых логов
- 🚫 **Система ограничений** - блокировка пользователей по различным критериям
- 🎮 **Конкурсы и игры** - ежедневные игры с призами и реферальные конкурсы
- 📡 **Модем-подписки** - дополнительный функционал для устройств-модемов

### 📚 Поддерживаемые методы авторизации

| Конфигурация     | Authorization       | X-Api-Key           |
| ---------------- | ------------------- | ------------------- |
| Только API Key   | Bearer <api_key>    | <api_key>           |
| Basic Auth       | Bearer <api_key>    | Basic <user:pass>   |
| Caddy + API Key  | Basic <caddy_token> | <api_key>           |
| Cookies (eGames) | Bearer <api_key>    | <api_key> + cookies |

---

## 🚀 Быстрый старт

```bash
# 1. Скачай репозиторий
git clone https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot.git
cd remnawave-bedolaga-telegram-bot

# 2. Создай необходимые директории
mkdir -p ./logs ./data ./data/backups ./data/referral_qr
chmod -R 755 ./logs ./data
sudo chown -R 1000:1000 ./logs ./data

# 3.Установи «Docker» командой:
sudo curl -fsSL https://get.docker.com | sh

# 4.Установи «make» следующей командой:
apt install make -y

make up             # Поднять контейнеры

make help           # Показать все команды
make down           # Остановить контейнеры
make reload         # Перезапустить
make reload-follow  # Перезапустить с логами
make test           # Запустить тесты
```

### 🳠Ручной Docker запуск

Если не хочется пользоваться мастером, можно настроить всё вручную:

```bash
# 1. Скачай репозиторий
git clone https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot.git
cd remnawave-bedolaga-telegram-bot

# 2. Настрой конфиг
cp .env.example .env
nano .env  # Заполни токены и настройки

# 3. Создай необходимые директории
mkdir -p ./logs ./data ./data/backups ./data/referral_qr
chmod -R 755 ./logs ./data
sudo chown -R 1000:1000 ./logs ./data

# 4. Запусти всё разом
docker compose up -d

# 5. Проверь статус
docker compose logs
```

---

## 🌐 Настройка веб-сервера и обратного проксирования

> Встроенный FastAPI-сервер обслуживает Telegram webhook, платежные webhooks, административное API и статические файлы миниапки на **одном порту 8080**. Снаружи вы публикуете только HTTPS-прокси, которое проксирует весь трафик на этот порт.

### 1. Выбор режима запуска

| `BOT_RUN_MODE` | Что делает                                                                  | Когда использовать                               |
| -------------- | --------------------------------------------------------------------------- | ------------------------------------------------ |
| `polling`      | Бот опрашивает Telegram через long polling. HTTP-сервер можно не поднимать. | Локальная отладка или отсутствие внешнего HTTPS. |
| `webhook`      | Aiogram получает апдейты только через вебхук.                               | Продакшн и серверы за HTTPS-прокси.              |

### 2. Минимальные настройки для webhook

Для запуска бота в режиме webhook минимально необходимы следующие параметры в `.env`:

```env
# Обязательные базовые настройки
BOT_TOKEN=1234567890:AABBCCdd...
ADMIN_IDS=123456789,987654321

# Настройки webhook
BOT_RUN_MODE=webhook
WEBHOOK_URL=https://hooks.domain.com
WEBHOOK_PATH=/webhook
WEBHOOK_SECRET_TOKEN=super-secret-token

# Настройки Web API
WEB_API_ENABLED=true
WEB_API_PORT=8080
WEB_API_ALLOWED_ORIGINS=https://miniapp.domain.com
WEB_API_DEFAULT_TOKEN=super-secret-token

# Настройки Remnawave
REMNAWAVE_API_URL=https://your-panel.com
REMNAWAVE_API_KEY=your_api_key
```

**Важно:**

- `WEBHOOK_SECRET_TOKEN` - сгенерируйте командой `openssl rand -hex 32`
- `WEB_API_DEFAULT_TOKEN` - сгенерируйте командой `openssl rand -hex 32`
- `WEBHOOK_URL` должен быть доступен извне по HTTPS
- `WEB_API_ALLOWED_ORIGINS` - укажите домен, где будет размещена миниапка

### 3. Полные настройки webhook (опционально)

```env
BOT_RUN_MODE=webhook
WEBHOOK_URL=https://hooks.domain.com
WEBHOOK_PATH=/webhook
WEBHOOK_SECRET_TOKEN=super-secret-token
WEBHOOK_DROP_PENDING_UPDATES=true
WEBHOOK_MAX_QUEUE_SIZE=1024
WEBHOOK_WORKERS=4
WEBHOOK_ENQUEUE_TIMEOUT=0.1
WEBHOOK_WORKER_SHUTDOWN_TIMEOUT=30.0

WEB_API_ENABLED=true
WEB_API_HOST=0.0.0.0
WEB_API_PORT=8080
WEB_API_ALLOWED_ORIGINS=https://miniapp.domain.com
```

- `WEBHOOK_URL` — публичный HTTPS-домен прокси. К нему автоматически добавится путь из `WEBHOOK_PATH`.
- `WEBHOOK_SECRET_TOKEN` — защитный токен Telegram, обязательно задайте своё значение.
- Очередь можно тюнить через `WEBHOOK_MAX_QUEUE_SIZE`, `WEBHOOK_WORKERS`, `WEBHOOK_ENQUEUE_TIMEOUT` и `WEBHOOK_WORKER_SHUTDOWN_TIMEOUT`.
- Если миниапка или админка доступны по другим доменам, перечислите их через запятую в `WEB_API_ALLOWED_ORIGINS`.

После изменения `.env` перезапустите сервис: `docker compose up -d remnawave_bot`.

### 4. Настройка Docker-сетей

#### Вариант 1: Бот на отдельном сервере

Если бот запускается на отдельном сервере (не там, где панель Remnawave), используйте стандартную конфигурацию:

**docker-compose.yml бота:**

```yaml
services:
  postgres:
    image: postgres:15-alpine
    container_name: remnawave_bot_db
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-remnawave_bot}
      POSTGRES_USER: ${POSTGRES_USER:-remnawave_user}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-secure_password_123}
      POSTGRES_INITDB_ARGS: '--encoding=UTF8 --locale=C'
    volumes:
      - postgres_data:/var/lib/postgresql/data
    networks:
      - bot_network
    healthcheck:
      test:
        [
          'CMD-SHELL',
          'pg_isready -U ${POSTGRES_USER:-remnawave_user} -d ${POSTGRES_DB:-remnawave_bot}',
        ]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s

  redis:
    image: redis:7-alpine
    container_name: remnawave_bot_redis
    restart: unless-stopped
    command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
    volumes:
      - redis_data:/data
    networks:
      - bot_network
    healthcheck:
      test: ['CMD', 'redis-cli', 'ping']
      interval: 30s
      timeout: 10s
      retries: 3

  bot:
    build: .
    container_name: remnawave_bot
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    env_file:
      - .env
    environment:
      DOCKER_ENV: 'true'
      DATABASE_MODE: 'auto'
      POSTGRES_HOST: 'postgres'
      POSTGRES_PORT: '5432'
      POSTGRES_DB: '${POSTGRES_DB:-remnawave_bot}'
      POSTGRES_USER: '${POSTGRES_USER:-remnawave_user}'
      POSTGRES_PASSWORD: '${POSTGRES_PASSWORD:-secure_password_123}'
      REDIS_URL: 'redis://redis:6379/0'
      TZ: 'Europe/Moscow'
      LOCALES_PATH: '${LOCALES_PATH:-/app/locales}'
    volumes:
      - ./logs:/app/logs:rw
      - ./data:/app/data:rw
      - ./locales:/app/locales:rw
      - /etc/timezone:/etc/timezone:ro
      - /etc/localtime:/etc/localtime:ro
      - ./vpn_logo.png:/app/vpn_logo.png:ro
    ports:
      - '${WEB_API_PORT:-8080}:8080'
    networks:
      - bot_network
    healthcheck:
      test:
        [
          'CMD-SHELL',
          'python -c "import requests, os; requests.get(''http://localhost:8080/health'', headers={''X-API-Key'': os.environ.get(''WEB_API_DEFAULT_TOKEN'')}, timeout=5) or exit(1)"',
        ]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s

volumes:
  postgres_data:
    driver: local
  redis_data:
    driver: local

networks:
  bot_network:
    name: remnawave_bot_network
    driver: bridge
    ipam:
      config:
        - subnet: 172.20.0.0/16
          gateway: 172.20.0.1
    driver_opts:
      com.docker.network.driver.mtu: 1350
```

**docker-compose.yml для Caddy (отдельный файл):**

```yaml
services:
  caddy:
    image: caddy:2-alpine
    container_name: remnawave_caddy
    restart: unless-stopped
    ports:
      - '80:80'
      - '443:443'
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config
    networks:
      - bot_network

volumes:
  caddy_data:
  caddy_config:

networks:
  bot_network:
    name: remnawave_bot_network
    external: true
```

#### Вариант 2: Бот на одном сервере с панелью Remnawave

Если бот и панель Remnawave запускаются на одном сервере, подключите бота к сети панели:

**docker-compose.local.yml бота(есть в репо):**

```yaml
services:
  postgres:
    image: postgres:15-alpine
    container_name: remnawave_bot_db
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-remnawave_bot}
      POSTGRES_USER: ${POSTGRES_USER:-remnawave_user}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-secure_password_123}
      POSTGRES_INITDB_ARGS: '--encoding=UTF8 --locale=C'
    volumes:
      - postgres_data:/var/lib/postgresql/data
    networks:
      - bot_network
      - remnawave-network # Подключаем к сети панели
    healthcheck:
      test:
        [
          'CMD-SHELL',
          'pg_isready -U ${POSTGRES_USER:-remnawave_user} -d ${POSTGRES_DB:-remnawave_bot}',
        ]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s

  redis:
    image: redis:7-alpine
    container_name: remnawave_bot_redis
    restart: unless-stopped
    command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
    volumes:
      - redis_data:/data
    networks:
      - bot_network
      - remnawave-network # Подключаем к сети панели
    healthcheck:
      test: ['CMD', 'redis-cli', 'ping']
      interval: 30s
      timeout: 10s
      retries: 3

  bot:
    build: .
    container_name: remnawave_bot
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    env_file:
      - .env
    environment:
      DOCKER_ENV: 'true'
      DATABASE_MODE: 'auto'
      POSTGRES_HOST: 'postgres'
      POSTGRES_PORT: '5432'
      POSTGRES_DB: '${POSTGRES_DB:-remnawave_bot}'
      POSTGRES_USER: '${POSTGRES_USER:-remnawave_user}'
      POSTGRES_PASSWORD: '${POSTGRES_PASSWORD:-secure_password_123}'
      REDIS_URL: 'redis://redis:6379/0'
      TZ: 'Europe/Moscow'
      LOCALES_PATH: '${LOCALES_PATH:-/app/locales}'
    volumes:
      - ./logs:/app/logs:rw
      - ./data:/app/data:rw
      - ./locales:/app/locales:rw
      - /etc/timezone:/etc/timezone:ro
      - /etc/localtime:/etc/localtime:ro
      - ./vpn_logo.png:/app/vpn_logo.png:ro
    ports:
      - '${WEB_API_PORT:-8080}:8080'
    networks:
      - bot_network
      - remnawave-network # Подключаем к сети панели
    healthcheck:
      test:
        [
          'CMD-SHELL',
          'python -c "import requests, os; requests.get(''http://localhost:8080/health'', headers={''X-API-Key'': os.environ.get(''WEB_API_DEFAULT_TOKEN'')}, timeout=5) or exit(1)"',
        ]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s

volumes:
  postgres_data:
    driver: local
  redis_data:
    driver: local

networks:
  bot_network:
    driver: bridge
    ipam:
      config:
        - subnet: 172.20.0.0/16
          gateway: 172.20.0.1
    driver_opts:
      com.docker.network.driver.mtu: 1350

  remnawave-network:
    name: remnawave-network
    external: true # Используем существующую сеть панели
```

**Важно:**

- Сеть `remnawave-network` должна быть создана панелью Remnawave
- В `.env` укажите `REMNAWAVE_API_URL=http://remnawave:3000` (обращение внутри Docker-сети)
- Бот сможет напрямую общаться с панелью без внешних запросов

### 5. Проверка здоровья

Проверьте, что единый сервер отвечает:

```bash
curl -s https://bot.example.com/health/unified | jq
```

Полезные диагностические endpoints:

- `/health/unified` — агрегированный статус (режим бота, очередь Telegram, наличие миниапки и платежей). Когда административное API отключено, тот же статус доступен по `/health`.
- `/health/telegram-webhook` — состояние очереди Telegram webhook.
- `/health/payment-webhooks` — какие платёжные интеграции активированы.

### 6. Swagger и документация

- Включите `WEB_API_DOCS_ENABLED=true`, если нужно открыть Swagger UI и OpenAPI. После перезапуска сервиса станут доступны эндпоинты `/docs`, `/doc` (редирект для обратной совместимости), `/redoc` и `/openapi.json`.
- Не забудьте проксировать эти пути через внешний HTTPS-прокси вместе с остальными эндпоинтами бота.
- В продакшене держите `WEB_API_DOCS_ENABLED=false`, чтобы документация не была публичной. При необходимости включайте временно или защищайте прокси базовой авторизацией/IP-фильтрацией.

### 7. Пример Caddy-конфига

`Caddyfile`:

```caddy
# Hooks + API
hooks.domain.com {
    encode gzip zstd

    # Webhook пути для платежных систем
    handle /yookassa-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    handle /platega-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    handle /cryptobot-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    handle /wata-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    handle /heleket-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    handle /tribute-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    handle /pal24-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    handle /mulenpay-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    handle /freekassa-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    handle /cloudpayments-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    handle /riopay-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }
    
    handle /remnawave-webhook {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    # app-config.json с CORS
    handle /app-config.json {
        header Access-Control-Allow-Origin "*"
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    # Все остальные запросы
    handle {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }
}

# Статика для miniapp
miniapp.domain.com {
    encode gzip zstd

    # API эндпоинты /miniapp/* в приложение
    handle /miniapp/* {
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    # app-config.json с CORS
    handle /app-config.json {
        header Access-Control-Allow-Origin "*"
        reverse_proxy remnawave_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            transport http {
                read_buffer 0
            }
        }
    }

    # Статические файлы (корень и всё остальное)
    handle {
        root * /var/www/remnawave-miniapp
        try_files {path} /index.html
        file_server
    }
}
```

**Примечание:** Если прокси запускается на том же сервере, замените `remnawave_bot:8080` на `localhost:8080`

### 8. Пример nginx-конфига

`nginx.conf`:

```nginx
events {}
http {
    include /etc/nginx/mime.types;
    sendfile on;

    upstream remnawave_bot_unified {
        server remnawave_bot:8080;
    }

    # Hooks + API домен
    server {
        listen 80;
        listen 443 ssl http2;
        server_name hooks.domain.com;

        ssl_certificate /etc/ssl/private/hooks.fullchain.pem;
        ssl_certificate_key /etc/ssl/private/hooks.privkey.pem;

        client_max_body_size 32m;

        # Webhook пути для платежных систем
        location = /yookassa-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        location = /platega-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        location = /cryptobot-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        location = /wata-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        location = /heleket-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        location = /tribute-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        location = /pal24-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        location = /mulenpay-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        location = /freekassa-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        location = /cloudpayments-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }
        
        location = /riopay-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        location = /remnawave-webhook {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        # app-config.json с CORS
        location = /app-config.json {
            add_header Access-Control-Allow-Origin "*";
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }

        # Всё остальное
        location / {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }
    }

    # Miniapp домен (статика + API)
    server {
        listen 80;
        listen 443 ssl http2;
        server_name miniapp.domain.com;

        ssl_certificate /etc/ssl/private/miniapp.fullchain.pem;
        ssl_certificate_key /etc/ssl/private/miniapp.privkey.pem;

        client_max_body_size 32m;

        # API эндпоинты /miniapp/*
        location /miniapp/ {
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            proxy_request_buffering off;
        }

        # app-config.json с CORS
        location = /app-config.json {
            add_header Access-Control-Allow-Origin "*";
            proxy_pass http://remnawave_bot_unified;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }

        # Статические файлы
        location / {
            root /var/www/remnawave-miniapp;
            try_files $uri $uri/ /index.html;

            # Кэширование
            expires 1h;
            add_header Cache-Control "public, immutable";
        }
    }
}
```

**Примечание:** Если прокси запускается на том же сервере, замените `remnawave_bot:8080` на `localhost:8080`

Рекомендации:

- Откройте входящие 80/443 в файерволе.
- Если используете Cloudflare/анти-DDoS, разрешите методы `POST` и заголовок `X-Telegram-Bot-Api-Secret-Token`.
- После развёртывания перезапустите бот (`make reload`), чтобы он заново зарегистрировал webhook.

### Быстрая настройка SSL для Nginx в Docker

#### 1. Установка Certbot

```bash
sudo apt update && sudo apt install certbot -y
```

#### 2. Генерация сертификатов

```bash
# Остановите Nginx Docker контейнер
docker compose down

# Сгенерируйте сертификаты (порт 80 должен быть свободен)
sudo certbot certonly --standalone -d hooks.domain.com --agree-tos --email your-email@example.com --non-interactive
sudo certbot certonly --standalone -d miniapp.domain.com --agree-tos --email your-email@example.com --non-interactive
```

#### 3. Копирование сертификатов

```bash
sudo mkdir -p /etc/ssl/private

sudo cp /etc/letsencrypt/live/hooks.domain.com/fullchain.pem /etc/ssl/private/hooks.fullchain.pem
sudo cp /etc/letsencrypt/live/hooks.domain.com/privkey.pem /etc/ssl/private/hooks.privkey.pem
sudo cp /etc/letsencrypt/live/miniapp.domain.com/fullchain.pem /etc/ssl/private/miniapp.fullchain.pem
sudo cp /etc/letsencrypt/live/miniapp.domain.com/privkey.pem /etc/ssl/private/miniapp.privkey.pem

sudo chmod 600 /etc/ssl/private/*.pem
```

#### 4. Обновите docker-compose.yml

```yaml
services:
  nginx:
    image: nginx:latest
    container_name: nginx
    ports:
      - '80:80'
      - '443:443'
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - /etc/ssl/private:/etc/ssl/private:ro
      - /var/www/remnawave-miniapp:/var/www/remnawave-miniapp:ro
    networks:
      - remnawave-network
    restart: unless-stopped

networks:
  remnawave-network:
    external: true
```

#### 5. Запуск Nginx

```bash
docker compose up -d
```

#### 6. Автообновление сертификатов

```bash
# Создайте скрипт обновления
sudo tee /opt/renew-certs.sh > /dev/null <<'EOF'
#!/bin/bash
# Останавливаем Nginx для освобождения порта 80
docker compose -f /путь/к/docker-compose.yml down

# Обновляем сертификаты
certbot renew --quiet

# Копируем обновленные сертификаты
cp /etc/letsencrypt/live/hooks.domain.com/fullchain.pem /etc/ssl/private/hooks.fullchain.pem
cp /etc/letsencrypt/live/hooks.domain.com/privkey.pem /etc/ssl/private/hooks.privkey.pem
cp /etc/letsencrypt/live/miniapp.domain.com/fullchain.pem /etc/ssl/private/miniapp.fullchain.pem
cp /etc/letsencrypt/live/miniapp.domain.com/privkey.pem /etc/ssl/private/miniapp.privkey.pem
chmod 600 /etc/ssl/private/*.pem

# Запускаем Nginx обратно
docker compose -f /путь/к/docker-compose.yml up -d
EOF

# Сделайте скрипт исполняемым
sudo chmod +x /opt/renew-certs.sh

# Добавьте в cron (запуск каждый день в 3:00 AM)
echo "0 3 * * * /opt/renew-certs.sh" | sudo crontab -
```

#### Проверка работы SSL

```bash
curl -I https://hooks.domain.com
curl -I https://miniapp.domain.com
```

**Важно:**

- Замените `domain.com` и `your-email@example.com` на свои данные
- Замените `/путь/к/docker-compose.yml` на реальный путь к вашему docker-compose файлу
- Убедитесь, что DNS записи для доменов указывают на IP вашего сервера

---

## ⚙️ Конфигурация

### 🔧 Основные параметры

| Настройка        | Где взять                                                                                                         | Пример                   |
| ---------------- | ----------------------------------------------------------------------------------------------------------------- | ------------------------ |
| 🤖 **BOT_TOKEN** | [@BotFather](https://t.me/BotFather)                                                                              | `1234567890:AABBCCdd...` |
| 👑 **ADMIN_IDS** | Твой Telegram ID                                                                                                  | `123456789,987654321`    |
| **BOT_RUN_MODE** | определяет способ приёма обновлений: `polling` или `webhook`. |

[Полный список доступных параметров:](.env.example)

### 🌐 Интеграция веб-админки

Подробное пошаговое руководство по запуску административного веб-API и подключению внешней панели находится в [docs/web-admin-integration.md](docs/web-admin-integration.md).

### 🤖 Режимы запуска бота

- `BOT_RUN_MODE` — определяет способ приёма обновлений: `polling` или `webhook`.
- `WEBHOOK_SECRET_TOKEN` — секрет для проверки заголовка `X-Telegram-Bot-Api-Secret-Token` при работе через вебхуки.
- `WEBHOOK_DROP_PENDING_UPDATES` — управляет очисткой очереди сообщений при установке вебхука.
- `WEBHOOK_MAX_QUEUE_SIZE` — ограничивает длину очереди входящих обновлений, чтобы защащаться от перегрузок.
- `WEBHOOK_WORKERS` — количество фоновых воркеров, параллельно обрабатывающих обновления Telegram.
- `WEBHOOK_ENQUEUE_TIMEOUT` — сколько секунд ждать свободного места в очереди перед отказом (0 — немедленный отказ).
- `WEBHOOK_WORKER_SHUTDOWN_TIMEOUT` — таймаут корректного завершения воркеров при остановке приложения.

### 📱 Telegram Mini App ЛК

Инструкция по развёртыванию мини-приложения, публикации статической страницы и настройке reverse-proxy доступна в [docs/miniapp-setup.md](docs/miniapp-setup.md).

Путь к статическим файлам мини-приложения можно переопределить через переменную `MINIAPP_STATIC_PATH`.

### 📊 Статус серверов в главном меню

| Переменная                   | Описание                                                                             | Пример                            |
| ---------------------------- | ------------------------------------------------------------------------------------ | --------------------------------- |
| `SERVER_STATUS_MODE`         | Режим работы кнопки: `disabled`, `external_link`, `external_link_miniapp` или `xray` | `xray`                            |
| `SERVER_STATUS_EXTERNAL_URL` | Прямаяссылка на внешний мониторинг                                                   | `https://status.example.com`      |
| `SERVER_STATUS_METRICS_URL`  | URL страницы метрик XrayChecker                                                      | `https://sub.example.com/metrics` |

### 🛡️ Защита панели Remnawave

Для панелей, защищенных через [remnawave-reverse-proxy](https://github.com/eGamesAPI/remnawave-reverse-proxy):

```env
# Для панелей установленных скриптом eGames
REMNAWAVE_SECRET_KEY=XXXXXXX:DDDDDDDD

# Или если ключ и значение одинаковые
REMNAWAVE_SECRET_KEY=secret_key_name
```

### 📡 Вебхуки Remnawave (real-time события)

Бот может принимать входящие вебхуки от панели Remnawave для мгновенной реакции на события подписок. Это значительно улучшает скорость обновления данных по сравнению с периодической синхронизацией.

#### Поддерживаемые события

| Событие | Описание |
|---------|----------|
| `user.expired` | Подписка истекла |
| `user.disabled` | Подписка деактивирована |
| `user.enabled` | Подписка активирована |
| `user.limited` | Превышен лимит трафика |
| `user.traffic_reset` | Трафик сброшен |
| `user.modified` | Данные подписки изменены (трафик, дата, URL) |
| `user.deleted` | Пользователь удалён |
| `user.revoked` | Ключи подписки отозваны |
| `user.created` | Пользователь создан |
| `user.expires_in_*` | Предупреждения об истечении (72ч, 48ч, 24ч) |
| `user.first_connected` | Первое подключение |
| `user.bandwidth_usage_threshold_reached` | Порог трафика достигнут |
| `user_hwid_devices.*` | Устройство добавлено/удалено |
| `node.*`, `service.*` | Административные события (ноды, сервис) |

#### Настройка

**1. Переменные окружения в `.env`:**

```env
REMNAWAVE_WEBHOOK_ENABLED=true
REMNAWAVE_WEBHOOK_PATH=/remnawave-webhook
REMNAWAVE_WEBHOOK_SECRET=your_secret_min_32_chars_here
```

Сгенерируйте секрет:

```bash
openssl rand -hex 32
```

**2. Настройка в панели Remnawave:**

В env панели Remnawave заполните:

- **URL**: `https://hooks.domain.com/remnawave-webhook`
- **Secret**: тот же секрет, что и в `REMNAWAVE_WEBHOOK_SECRET`

**3. Настройка прокси:**

Добавьте путь `/remnawave-webhook` в конфигурацию обратного прокси.

**Caddy:**

```caddy
handle /remnawave-webhook {
    reverse_proxy remnawave_bot:8080 {
        header_up Host {host}
        header_up X-Real-IP {remote_host}
        transport http {
            read_buffer 0
        }
    }
}
```

**Nginx:**

```nginx
location = /remnawave-webhook {
    proxy_pass http://remnawave_bot_unified;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
    proxy_buffering off;
    proxy_request_buffering off;
}
```

**4. Проверка работоспособности:**

```bash
# Health-check (GET запрос)
curl -s https://hooks.domain.com/remnawave-webhook | jq

# Ожидаемый ответ:
# {"status": "ok", "service": "remnawave_webhook", "enabled": true}
```

**Важно:**
- Секрет должен быть не менее 32 символов
- Бот верифицирует подпись `X-Remnawave-Signature` (HMAC-SHA256) для каждого запроса
- При включённых вебхуках бот автоматически защищает подписки от перезаписи данными из периодической синхронизации в течение 60 секунд после получения события
- Если бот и панель на одном сервере, URL вебхука может быть `http://remnawave_bot:8080/remnawave-webhook` (внутри Docker-сети)

### 💳 Freekassa

Платёжный провайдер [Freekassa](https://freekassa.ru) поддерживает NSPK СБП и банковские карты.

```env
FREEKASSA_ENABLED=true
FREEKASSA_SHOP_ID=12345
FREEKASSA_API_KEY=your_api_key
FREEKASSA_SECRET_WORD_1=secret1
FREEKASSA_SECRET_WORD_2=secret2
FREEKASSA_USE_API=true
FREEKASSA_PAYMENT_SYSTEM_ID=44  # 44 = NSPK СБП, 42 = обычный СБП
```

**Важно:**

- `FREEKASSA_USE_API=true` - использовать API для создания заказов (рекомендуется для NSPK)
- `FREEKASSA_USE_API=false` - редирект на платежную форму
- `FREEKASSA_PAYMENT_SYSTEM_ID=44` - для NSPK СБП (быстрее и надежнее)
- Настройте webhook на `https://your-domain.com/freekassa-webhook`

### 💳 CloudPayments

Платёжный провайдер [CloudPayments](https://cloudpayments.ru) поддерживает оплату банковскими картами и СБП.

```env
CLOUDPAYMENTS_ENABLED=true
CLOUDPAYMENTS_PUBLIC_ID=pk_xxxxxxxxxxxxx
CLOUDPAYMENTS_API_SECRET=your_api_secret
CLOUDPAYMENTS_CURRENCY=RUB
```

**Важно:**

- `CLOUDPAYMENTS_PUBLIC_ID` - Public ID из личного кабинета CloudPayments
- `CLOUDPAYMENTS_API_SECRET` - API Secret для подписи запросов
- Настройте webhook (Pay/Fail уведомления) на `https://your-domain.com/cloudpayments-webhook`
- Поддерживает 3D-Secure для безопасных платежей

### 💳 Platega.io

Платёжный провайдер [Platega.io](https://platega.io) добавляет ещё один способ приёма оплат картой и по СБП. Включите его, если у вас есть кабинет мерчанта и доступ к API.

1. В кабинете Platega получите `Merchant ID` и `Secret` (раздел **Интеграция → API**).
2. В настройках провайдера укажите URL возврата и ошибки. Их можно задать в `.env` (`PLATEGA_RETURN_URL`, `PLATEGA_FAILED_URL`).
3. Активируйте только нужные платёжные методы и пропишите их ID через запятую в `PLATEGA_ACTIVE_METHODS`.
4. Добавьте вебхук `https://your-domain.com/platega-webhook` в личном кабинете Platega.

Пример набора переменных окружения:

```env
PLATEGA_ENABLED=true
PLATEGA_MERCHANT_ID=your_merchant_id
PLATEGA_SECRET=your_secret_key
PLATEGA_RETURN_URL=https://your-domain.com/payments/success
PLATEGA_FAILED_URL=https://your-domain.com/payments/failed
PLATEGA_ACTIVE_METHODS=2,10,11
PLATEGA_MIN_AMOUNT_KOPEKS=100
PLATEGA_MAX_AMOUNT_KOPEKS=5000000
PLATEGA_CURRENCY=RUB
PLATEGA_WEBHOOK_PATH=/platega-webhook
```

Остальные параметры (`PLATEGA_BASE_URL`, `PLATEGA_WEBHOOK_HOST`, `PLATEGA_WEBHOOK_PORT`) оставьте по умолчанию, если работаете через встроенный FastAPI сервер.

### 📊 Режимы продажи трафика

#### **Выбираемые пакеты** (по умолчанию)

```env
TRAFFIC_SELECTION_MODE=selectable
TRAFFIC_PACKAGES_CONFIG="5:2000:false,10:3500:false,25:7000:false,50:11000:true,100:15000:true,250:17000:false,500:19000:false,1000:19500:true,0:20000:true"
```

#### **Фиксированный лимит**

```env
TRAFFIC_SELECTION_MODE=fixed
FIXED_TRAFFIC_LIMIT_GB=100  # 0 = безлимит
TRAFFIC_PACKAGES_CONFIG="100:15000:true"
```

### 🎯 Режим тарифов

Альтернативный режим продаж с готовыми тарифными планами:

```env
# Включение режима тарифов
SALES_MODE=tariffs  # classic | tariffs

# Тарифы настраиваются в админ-панели бота
# Каждый тариф содержит: название, период, трафик, устройства, серверы, цену
```

**Преимущества режима тарифов:**

- Готовые пакеты без сложного выбора параметров
- Упрощенный UX для пользователей
- Возможность создавать акционные тарифы
- Поддержка скидок промогрупп на тарифы

### 💰 Система ценообразования

Цена подписки рассчитывается по формуле:
**Базовая цена + Стоимость трафика + Доп. устройства + Доп. серверы**

**Пример расчета для подписки на 180 дней:**

- Базовый период: 400₽
- Трафик безлимит: 200₽/мес × 6 мес = 1200₽
- 4 устройства: 50₽/мес × 6 мес = 300₽
- 2 сервера: 100₽/мес × 6 мес = 1200₽
- **Итого: 3100₽**

### 📱 Управление устройствами

```env
# Бесплатные устройства в триал подписке
TRIAL_DEVICE_LIMIT=1

# Требовать оплату за активацию триала
TRIAL_PAYMENT_ENABLED=false

# Стоимость активации триала (в копейках)
TRIAL_ACTIVATION_PRICE=0

# Бесплатные устройства в платной подписке
DEFAULT_DEVICE_LIMIT=3

# Максимум устройств для покупки (0 = без лимита)
MAX_DEVICES_LIMIT=15
```

### 💥 Реферальная система

```env
# Включение/выключение реферальной программы
REFERRAL_PROGRAM_ENABLED=true

# Минимальная сумма пополнения для активации бонусов
REFERRAL_MINIMUM_TOPUP_KOPEKS=10000

# Бонус новому пользователю при первом пополнении
REFERRAL_FIRST_TOPUP_BONUS_KOPEKS=10000

# Бонус пригласившему при первом пополнении реферала
REFERRAL_INVITER_BONUS_KOPEKS=10000

# Процент комиссии с последующих пополнений
REFERRAL_COMMISSION_PERCENT=25
```

### 🔄 Автосинхронизация Remnawave

```env
# Включение автоматической синхронизации серверов
REMNAWAVE_AUTO_SYNC_ENABLED=true

# Время синхронизации (через запятую, формат HH:MM)
REMNAWAVE_AUTO_SYNC_TIMES=03:00,15:00
```

### 🛡️ Мониторинг и техническое обслуживание

```env
# Автоматический режим тех. работ
MAINTENANCE_MODE=false
MAINTENANCE_AUTO_ENABLE=true
MAINTENANCE_MONITORING_ENABLED=true
MAINTENANCE_CHECK_INTERVAL=30
MAINTENANCE_RETRY_ATTEMPTS=1

# Интервал проверки состояния панели (секунды)
MONITORING_INTERVAL=60
```

### 🛒 Умная корзина

```env
# Redis для сохранения корзины (требуется)
REDIS_URL=redis://redis:6379/0
```

### 🎮 Конкурсы и игры

```env
# Видимость кнопки конкурсов в главном меню
CONTEST_BUTTON_VISIBLE=true

# Конкурсы и ежедневные игры настраиваются в админ-панели
# Типы призов: баланс, дни подписки, промогруппа
```

**Возможности системы конкурсов:**

- Реферальные конкурсы с рейтингами
- Ежедневные игры с гарантированными призами
- Детальная статистика участников
- Гибкая настройка призов (баланс, подписка, промогруппа)

### 🚫 Система ограничений пользователей

```env
# Управление ограничениями через админ-панель
# Типы ограничений: покупка, триал, пополнение баланса
```

Позволяет блокировать отдельные функции для пользователей без полной блокировки аккаунта.

### 📝 Ротация логов

```env
# Настройка ротации логов
LOG_ROTATION_ENABLED=true
LOG_ROTATION_MAX_SIZE_MB=100
LOG_ROTATION_MAX_FILES=10
```

### 👁️ Управление видимостью

```env
# Скрытие кнопок пополнения баланса
HIDE_TOPUP_BUTTONS=false

# Скрытие реферальной программы
REFERRAL_PROGRAM_VISIBLE=true

# Видимость кнопки конкурсов
CONTEST_BUTTON_VISIBLE=true
```

---

#### ⭐ Функционал

<table>
<tr>
<td width="50%" valign="top">

### 💤 **Для пользователей**

🧭 **Онбординг и доступ**

- 🌐 Выбор языка интерфейса (RU/EN), динамическая локализация
- 📜 Принятие правил, оферты и политики конфиденциальности
- 📡 Проверка подписки на обязательный канал
- 🔗 Deeplink-инвайты, UTM-кампании и реферальные коды

🛒 **Умная покупка подписок**

- 📅 Гибкие периоды (14—360 дней) со скидками
- 📊 Выбор трафика: фиксированный лимит, пакеты или безлимит
- 🌐 Автоматический выбор сервера или мультивыбор
- 📱 Настройка количества устройств и серверов
- 🧾 Динамический калькулятор стоимости
- 💾 **Сквозная корзина** - сохранение параметров при недостатке баланса
- ↩️ Быстрый возврат к оформлению после пополнения
- 🎯 **Режим тарифов** - готовые пакеты без сложного выбора
- 🛒 **Простая покупка** - упрощенный режим для быстрого оформления

🧪 **Тестовая подписка**

- 🎁 Гибко настраиваемый триал и welcome-цепочка
- 🔔 Уведомления об истечении и автоконверсия
- 💎 Автовыдача бонусов за кампании и инвайты
- 🛡️ Контроль обязательной подписки на канал (отключает подписку при отписке)
- 💎 Автовыдача сквада из пула выбранных
- 💳 **Платный триал** с выбором способа оплаты

💰 **Платежи и баланс**

- ⭐ Telegram Stars
- 💳 Tribute
- 💳 YooKassa (СБП + банковские карты)
- 💰 CryptoBot (USDT, TON, BTC, ETH и др.)
- 🪙 Heleket (криптовалюта с наценкой)
- 💳 MulenPay (СБП)
- 🦐 PayPalych/Pal24 (СБП + карты)
- 💳 Platega (СБП + банковские карты)
- 💳 **WATA**
- 💳 **Freekassa** (NSPK СБП + карты)
- 💳 **CloudPayments** (карты + СБП)
- - 💳 **RioPay** (карты + СБП)
- 🔥 Автогенерация счетов и webhook-уведомления
- 💼 История операций
- 🔄 Автоплатёж с настройкой дня списания
- 🎁 Реферальные и промо-бонусы
- ⚡ **Быстрое пополнение** с кнопками быстрых сумм

📱 **Управление подписками**

- 📈 Реальный трафик, устройства и серверы
- 🌐 Переключение серверов и стран
- 📱 Сброс HWID
- 🧩 Смена языка, промогруппы и параметров
- 🧾 Просмотр активных услуг и статуса
- 🔗 Получение ссылок подключения в один клик
- 📡 **Модем-режим** - дополнительные устройства для модемов

🛟 **Поддержка и самообслуживание**

- 🎫 **Система тикетов** с вложениями
- 📚 FAQ, правила, оферта и политика
- 💬 Быстрые ссылки на поддержку

🧩 **Бонусы и промо**

- 🎫 Промокоды на деньги, дни, триал подписку, промогруппу
- 🎁 **Персональные промо-предложения** от админов
- 💰 **Тестовый доступ к серверам** через промо-акции
- 💸 **Автоматические скидки** при оплате и автопродлении
- 💥 Реферальная программа с комиссиями и бонусами
- 📊 Аналитика доходов и конверсии рефералов
- 🔗 Генерация реферальных ссылок и QR кодов

🎮 **Конкурсы и игры**

- 🎲 **Ежедневные игры** с призами (баланс, подписка, промогруппа)
- 🏆 **Реферальные конкурсы** с рейтингами и наградами
- 📊 **Детальная статистика** участия в конкурсах
- 🎁 **Разнообразные призы** - баланс, дни подписки, промогруппы

💎 **Промо-группы и скидки**

- 🏷️ **Система промогрупп** с индивидуальными скидками с приоритетами
- 💰 Скидки на серверы, трафик и устройства
- 📊 **Скидочные уровни за траты** - прозрачная система лояльности
- 📈 Автоматическое повышение уровня при достижении порога
- 🎯 **Скидки за длительные периоды** подписки для базовых юзеров

📱 **Mini App и гайды**

- 🖥️ **Полноценный личный кабинет** в Telegram WebApp
- 📊 Управление подпиской и параметрами
- 💳 Интегрированные платежи
- 🎁 Активация промо-офферов и промокодов
- 📱 Управление устройствами
- 💥 Реферальная статистика
- 📋 FAQ и юридические документы
- 📥 Библиотека загрузочных ссылок для клиентов
- 🛰️ Web API для внешних интеграций
- 🎯 **Отображение тарифа** при режиме тарифов

</td>
<td width="50%" valign="top">

### ⚙️ **Для администраторов**

📊 **Аналитика и отчётность**

- 📈 Дашборды по пользователям, подпискам и трафику
- 💰 Детализация платежей по всем источникам
- 🧮 Продажи по тарифам, устройствам и странам
- 📣 Эффективность кампаний, промокодов и UTM
- 🎯 **Статистика по промо-группам** и скидочным уровням
- 📊 **Расширенная фильтрация** пользователей (баланс, траты, активность, трафик)
- 🏆 **Топ рефереров** по периодам с детальной аналитикой
- 📡 **Мониторинг трафика** и выявление злоупотреблений

💥 **Управление пользователями**

- 🔍 Поиск по ID, имени, юзернейму, Telegram ID и фильтры
- 💰 Ручное изменение баланса
- 📱 Изменение лимитов устройств, трафика, серверов
- 🔄 Сброс HWID и перегенерация подписки
- 🎯 Назначение промогрупп и тарифов
- 💳 **Покупка подписки пользователю** прямо из админки
- ⏰ **Продление/сокращение срока** подписки (±365 дней)
- 🚫 Блокировки с таймером и аудит действий
- 🛡️ **Защита от запрещенных никнеймов** с настраиваемым список банвордов
- 💰 **Установка индивидуального реферального процента юзеру**
- 💬 **Отправка сообщения** конкретному пользователю
- 🚫 **Система ограничений** - блокировка функций (покупка, триал, пополнение)
- 📊 **Расширенные фильтры** - по балансу, тратам, трафику, активности
- 🔄 **Готовы к продлению** - фильтр пользователей для продления
- 🗑️ **Черный список** пользователей с управлением

🎯 **Продажи, маркетинг и удержание**

- 🎫 Промокоды
- 💳 Промо-группы со скидками
- 🎁 **Персональные промо-предложения** с поиском получателей
- 💸 **Тестовые серверы** - временная выдача доступа
- 💰 **Автоматические скидки** при оплате
- 📣 **Рекламные кампании** с deeplink и бонусами
- 📨 Рассылки по сегментам с медиа и кнопками
- 🎨 **Кастомные кнопки** для рассылок
- 📘 Настройка главного меню и приветственных экранов
- 🎮 **Управление конкурсами** - создание и настройка призов
- 🎲 **Ежедневные игры** с настраиваемыми призами
- 📊 **Детальная статистика** реферальных конкурсов
- 👁️ **Видимость кнопок** - управление отображением конкурсов и реферальной программы

🛟 **Поддержка и модерация**

- 🎫 **Центр тикетов** с приоритетами и статусами
- ⏱️ **SLA таймеры** и автоуведомления
- 🧑‍⚖️ Роли модераторов с ограниченным доступом
- 📊 Детальный журнал всех операций
- 🚫 Блокировки нарушителей
- 🧾 История диалогов и быстрые ответы
- 🔔 **Уведомления об ответах** на тикеты для админов

🔔 **Уведомления и коммуникации**

- 📢 **Топики для событий** (покупки, триалы, техработы)
- 🔔 Настройка уведомлений и расписаний
- 📨 **Управление контентом** - политика, оферта, FAQ
- 💬 Автоматические сообщения о задолженностях

🧰 **Обслуживание и DevOps**

- 🚧 Ручной и авто-режим техработ
- 🗒️ Просмотр системных логов и health-check
- 🔄 **Автосинхронизация Remnawave** по расписанию и при старте бота
- ♻️ Проверка обновлений репозитория
- 📊 **Мониторинг серверов** (интеграция с XrayChecker)
- 📝 **Ротация логов** - автоматическая очистка и архивация
- 🔄 **Массовая синхронизация** пользователей с Remnawave

🗄️ **Бекапы и восстановление**

- 🗓️ **Умные автобекапы** с гибким расписанием
- 📦 Ручные бекапы с выбором содержимого
- 📤 Отправка архивов в выделенный чат/топик
- 🔓 Восстановление без остановки бота
- ✅ Автоматическая синхронизация sequences после восстановления

💳 **Биллинг и настройки**

- ⚙️ **Управление ценами** без перезапуска бота
- 📘 **Управление пакетами трафика** (включение/отключение)
- 🧪 Тестовые платежи для каждого провайдера
- 🪙 Управление вебхуками всех платёжных систем
- ⚙️ **Управление настройками из бота** (с приоритетом в .env)
- 🎯 **Режим тарифов** - готовые планы с фиксированными параметрами
- 🛒 **Простая покупка подписки** - упрощенный режим без выбора параметров
- 👁️ **Скрытие кнопок** пополнения баланса
- 👁️ **Скрытие реферальной** программы

⚙️ **Remnawave**

- Синхронизация юзеров из панели в бота (Ручная/автоматическая по таймеру)
- Синхронизация юзеров из бота в панель
- Синхронизация сквадов(серверов) из панели в бота
- Управление нодами/сквадами прямо в боте
- Детальная статистика по нодам/панели
- Создание/Редактивание сквадов в боте

🗃️ **REST API для интеграций**

- 🔌 **FastAPI Web API** с полной документацией
- 🔒 Управление API-ключами и токенами
- 📊 Эндпоинты для подписок, пользователей, транзакций
- 🎁 API промо-системы и рассылок
- 📋 API управления контентом и настройками

</td>
</tr>
</table>

### 🤖 Автоматизация и экосистема

- 🔄 **Мониторинг Remnawave** - регулярная проверка API, автоматическое включение/выключение техработ
- 🔄 **Автосинхронизация серверов** - фоновая синхронизация по расписанию
- 🛒 **Умная корзина** - сохранение параметров подписки в Redis при недостатке баланса
- 🛡️ **Антифрод** - валидация подписки на канал
- 🚫 **Защита от блокировок** - автоблокировка подозрительных никнеймов и имитация фишинг аккаунтов
- 🧠 **Асинхронная архитектура** - aiogram 3, PostgreSQL/SQLite, Redis и очереди задач
- 🌐 **Мультиязычность** - локализации RU/EN, быстрый выбор языка
- 📦 **Интеграция с Remnawave API** - автоматическое создание пользователей и синхронизация
- 🔄 **Миграция сквадов** - массовый перенос пользователей между сквадами
- 🧾 **История операций** - хранение всех транзакций и действий для аудита
- 💸 **Сервис автопроверки транзакций** - автоматическая проверка транзакций в статусе "В ожидании оплаты" за последние 24ч
- 📝 **Ротация логов** - автоматическая очистка и архивация старых логов
- 🎮 **Система конкурсов** - ежедневные игры и реферальные конкурсы с призами

### 🌐 Веб-API и мини-приложение

- ⚙️ **FastAPI Web API** с эндпоинтами для управления всеми аспектами бота
- 🔒 **Управление API-ключами** - выпуск, отзыв, реактивация токенов
- 🛰️ **Mini App** - полноценный личный кабинет внутри Telegram
- 💳 **Интегрированные платежи** в Mini App (Stars, Pal24, YooKassa, WATA, Freekassa, CloudPayments)
- 🧭 **Единый стандартный app-config.json** - централизованная раздача ссылок на клиенты
- 🪙 **Платёжные вебхуки** - встроенные серверы для всех платёжных систем
- 📡 **Мониторинг серверов** - REST-эндпоинты для просмотра нод и статистики

## 🚀 Производительность

| Пользователей | Память | CPU     | Диск   | Описание          |
| ------------- | ------ | ------- | ------ | ----------------- |
| **1,000**     | 512MB  | 1 vCPU  | 10GB   | ✅ Стартап        |
| **10,000**    | 2GB    | 2 vCPU  | 50GB   | ✅ Малый бизнес   |
| **50,000**    | 4GB    | 4 vCPU  | 100GB  | ✅ Средний бизнес |
| **100,000+**  | 8GB+   | 8+ vCPU | 200GB+ | 🚀 Enterprise     |

---

## 🗃️ Технологический стек

### 💪 Современные технологии

- **🐍 Python 3.13+** с AsyncIO - максимальная производительность
- **🗄️ PostgreSQL 15+** - надежное хранение данных
- **⚡ Redis** - быстрое кеширование и сессии (для корзины)
- **🳠Docker** - простое развертывание в любой среде
- **🔗 SQLAlchemy ORM** - безопасная работа с БД
- **🚀 aiogram 3** - современная Telegram Bot API
- **⚡ FastAPI** - высокопроизводительный REST API
- **📦 Pydantic v2** - валидация данных

---

## 🔧 Первичная настройка

После запуска необходимо:

1. **📡 Синхронизация серверов** (обязательно!)

   - Зайди в бот → **Админ панель** → **Подписки** → **Управление серверами**
   - Нажми **Синхронизация** и дождись завершения
   - Без этого пользователи не смогут выбирать страны!

2. **💥 Синхронизация пользователей** (если есть база)

   - **Админ панель** → **Remnawave** → **Синхронизация**
   - **Синхронизировать всех** → дождись импорта

3. **💳 Настройка платежных систем**

   - **Telegram Stars**: Работает автоматически
   - **Tribute**: Настрой webhook на `https://your-domain.com/tribute-webhook`
   - **YooKassa**: Настрой webhook на `https://your-domain.com/yookassa-webhook`
   - **CryptoBot**: Настрой webhook на `https://your-domain.com/cryptobot-webhook`
   - **Heleket**: Настрой webhook на `https://your-domain.com/heleket-webhook`
   - **MulenPay**: Настрой webhook на `https://your-domain.com/mulenpay-webhook`
   - **PayPalych**: Укажи Result URL `https://your-domain.com/pal24-webhook` в кабинете Pal24
   - **Platega**: Настрой webhook на `https://your-domain.com/platega-webhook`
   - **WATA**: Настрой webhook на `https://your-domain.com/wata-webhook`
   - **Freekassa**: Настрой webhook на `https://your-domain.com/freekassa-webhook`
   - **CloudPayments**: Настрой Pay/Fail уведомления на `https://your-domain.com/cloudpayments-webhook`

4. **🔄 Настройка автосинхронизации** (опционально)
   - В `.env` установи `REMNAWAVE_AUTO_SYNC_ENABLED=true`
   - Укажи время синхронизации в `REMNAWAVE_AUTO_SYNC_TIMES=03:00,15:00`

### 🛠️ Настройка уведомлений в топик группы

#### 1. Переменные окружения

Добавьте в файл `.env`:

```env
# Уведомления администраторов
ADMIN_NOTIFICATIONS_ENABLED=true
ADMIN_NOTIFICATIONS_CHAT_ID=-1001234567890  # ID канала/группы
ADMIN_NOTIFICATIONS_TOPIC_ID=123             # ID топика (опционально)
ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID=126      # ID топика для тикетов
```

#### 2. Создание канала

1. **Создайте приватный канал** или группу для уведомлений
2. **Добавьте бота** как администратора с правами отправки сообщений
3. **Получите ID канала**:
   - Отправьте любое сообщение в канал
   - Перешлите его боту @userinfobot
   - Скопируйте Chat ID (например: `-1001234567890`)

#### 3. Настройка топиков (опционально)

Если используете супергруппу с топиками:

1. **Включите топики** в настройках группы
2. **Создайте топики** для уведомлений (например, "Уведомления", "Тикеты")
3. **Получите ID топика** из URL веб-версии Telegram или используйте бота

---

## 🛠 Устранение неполадок

### 🥼 Health Checks

- **Unified сервер**: `http://localhost:8080/health/unified` (или `/health`, если административное API отключено)
- **Telegram webhook**: `http://localhost:8080/health/telegram-webhook`
- **Платёжные webhooks**: `http://localhost:8080/health/payment-webhooks`

### 🔧 Полезные команды

```bash
# Просмотр логов в реальном времени
docker compose logs -f bot

# Статус всех контейнеров
docker compose ps

# Перезапуск только бота
docker compose restart bot

# Проверка базы данных
docker compose exec postgres pg_isready -U remnawave_user

# Подключение к базе данных
docker compose exec postgres psql -U remnawave_user -d remnawave_bot

# Проверка Redis
docker compose exec redis redis-cli ping

# Проверка использования ресурсов
docker stats

# Очистка Docker
docker system prune
```

### 🚨 Частые проблемы и решения

| Проблема                   | Диагностика                                          | Решение                                |
| -------------------------- | ---------------------------------------------------- | -------------------------------------- |
| **Бот не отвечает**        | `docker logs remnawave_bot`                          | Проверь `BOT_TOKEN` и интернет         |
| **Ошибки БД**              | `docker compose ps postgres`                         | Проверь статус PostgreSQL              |
| **Webhook не работает**    | `curl http://localhost:8080/health/telegram-webhook` | Проверь `WEBHOOK_URL`, прокси и секрет |
| **API недоступен**         | Проверь логи бота                                    | Проверь `REMNAWAVE_API_URL`            |
| **Корзина не сохраняется** | `docker compose ps redis`                            | Проверь статус Redis                   |
| **Платежи не проходят**    | Проверь webhook'и                                    | Настрой URL в платежных системах       |

---

## 💡 Использование

### 💤 **Для пользователей**

1. **🚀 Старт** → Найди бота и нажми `/start`
2. **🌐 Язык** → Выбери язык интерфейса (RU/EN)
3. **📋 Правила** → Прими правила сервиса
4. **💰 Баланс** → Пополни через любой удобный способ
5. **🛒 Подписка** → Выбери тариф и параметры
6. **📱 Подключение** → Получи ссылку или конфиг
7. **💥 Партнерка** → Поделись ссылкой и получай бонусы

### ⚙️ **Для администраторов**

Доступ через **"⚙️ Админ панель"**:

- **📦 Подписки** → настройка серверов, цен, синхронизация
- **💥 Пользователи** → поиск, редактирование, блокировка
- **💎 Промо-группы** → управление скидочными группами и уровнями
- **🎁 Промокоды** → создание и статистика
- **🎯 Промо-предложения** → персональные акции и скидки
- **📨 Рассылки** → уведомления по сегментам
- **📣 Кампании** → управление рекламными кампаниями
- **🎫 Тикеты** → система поддержки
- **🔄 Контент** → политика, оферта, FAQ
- **🖥 Remnawave** → мониторинг, синхронизация
- **📊 Статистика** → детальная аналитика

---

## 🛡️ Безопасность

### 🔍 Защита панели Remnawave

Бот поддерживает интеграцию с системой защиты панели:

```env
# Для защищенных панелей
REMNAWAVE_SECRET_KEY=secret_name:secret_value

# Для панелей eGames скрипта
REMNAWAVE_SECRET_KEY=XXXXXXX:DDDDDDDD
```

### 🔒 Дополнительные меры безопасности

- **Валидация всех входящих данных**
- **Rate limiting для защиты от спама**
- **Шифрование чувствительных данных**
- **Автоматическое управление сессиями**
- **Мониторинг подозрительной активности**
- **Защита от запрещенных никнеймов** - автоблокировка имитации официальных аккаунтов
- **Защита от обфусцированных доменов** в отображаемом имени

---

## 🤝 Как помочь проекту

- 🐛 [**Сообщай о багах**](https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot/issues) с подробным описанием
- 💡 [**Предлагай идеи**](https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot/discussions) для улучшения
- ⭐ **Ставь звезды** проекту - это мотивирует разработку!
- 📢 **Рассказывай друзьям** о проекте
- 👍 **[Поддержи разработку](https://t.me/tribute/app?startapp=duUO)** - помоги проекту расти
- 🔧 **Отправляй Pull Requests** - внеси свой вклад в код

---

## 💬 Поддержка и сообщество

### 📞 **Контакты**

- **💬 Telegram:** [@fringg](https://t.me/fringg) - вопросы по разработке (только по делу!)
- **💬 Telegram Group:** [Bedolaga Chat](https://t.me/+wTdMtSWq8YdmZmVi) - общение, вопросы, предложения
- **🐛 Issues:** [GitHub Issues](https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot/issues) - баги и предложения

### 📚 **Полезные ресурсы**

- **📖 [https://docs.remna.st](https://docs.rw)))** - документация панели
- **🤖 [Telegram Bot API](https://core.telegram.org/bots/api)** - API ботов
- **🐳 [Docker Guide](https://docs.docker.com/get-started/)** - обучение Docker
- **🛡️ [Reverse Proxy](https://github.com/eGamesAPI/remnawave-reverse-proxy)** - защита панели

---

### 🎉 **Сообщество**

- **Remnawave Team** - за отличную панель и стабильный API
- **Сообщество Bedolaga** - за активное тестирование и обратную связь
- **Всем пользователям** - за доверие и использование бота

---

## 📋 Roadmap

### 🚧 **В разработке**

- 🌎 **Веб-панель** - полноценная административная панель
- 🔄 **API для интеграций** - подключение внешних сервисов
- 🎨 **Темы оформления** - Новая тема интерфейса Mini App by https://t.me/arpicme

---

## 📄 Лицензия

Проект распространяется под лицензией **MIT**

[📜 Посмотреть лицензию](LICENSE)

---

## 🚀 Начни уже сегодня!

<table align="center">
<tr>
<td align="center">
<h3>🧪 Протестируй бота</h3>
<a href="https://t.me/FringVPN_bot">
<img src="https://img.shields.io/badge/Telegram-Тестовый_бот-blue?style=for-the-badge&logo=telegram" alt="Test Bot">
</a>
</td>
<td align="center">
<h3>💬 Присоединись к сообществу</h3>
<a href="https://t.me/+wTdMtSWq8YdmZmVi">
<img src="https://img.shields.io/badge/Telegram-Bedolaga_Chat-blue?style=for-the-badge&logo=telegram" alt="Community">
</a>
</td>
</tr>
<tr>
<td align="center">
<h3>⭐ Поставь звезду</h3>
<a href="https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot">
<img src="https://img.shields.io/badge/GitHub-Звезда-yellow?style=for-the-badge&logo=github" alt="Star">
</a>
</td>
<td align="center">
<h3>💝 Поддержи проект</h3>
<a href="https://t.me/tribute/app?startapp=duUO">
<img src="https://img.shields.io/badge/Tribute-Донат-green?style=for-the-badge&logo=heart" alt="Donate">
</a>
</td>
</tr>
</table>

---

## 📈 Статистика проекта

![GitHub stars](https://img.shields.io/github/stars/Fr1ngg/remnawave-bedolaga-telegram-bot?style=social)
![GitHub forks](https://img.shields.io/github/forks/Fr1ngg/remnawave-bedolaga-telegram-bot?style=social)
![GitHub watchers](https://img.shields.io/github/watchers/Fr1ngg/remnawave-bedolaga-telegram-bot?style=social)

![GitHub last commit](https://img.shields.io/github/last-commit/Fr1ngg/remnawave-bedolaga-telegram-bot)
![GitHub commit activity](https://img.shields.io/github/commit-activity/m/Fr1ngg/remnawave-bedolaga-telegram-bot)
![GitHub contributors](https://img.shields.io/github/contributors/Fr1ngg/remnawave-bedolaga-telegram-bot)

![GitHub issues](https://img.shields.io/github/issues/Fr1ngg/remnawave-bedolaga-telegram-bot)
![GitHub pull requests](https://img.shields.io/github/issues-pr/Fr1ngg/remnawave-bedolaga-telegram-bot)
![GitHub closed issues](https://img.shields.io/github/issues-closed/Fr1ngg/remnawave-bedolaga-telegram-bot)

</div>

---

## 🎯 Ключевые особенности в цифрах

| Метрика                    | Значение                                                                                                    |
| -------------------------- | ----------------------------------------------------------------------------------------------------------- |
| 💳 **Платёжных систем**    | 11 (Stars, YooKassa, Tribute, CryptoBot, Heleket, MulenPay, Pal24, Platega, WATA, Freekassa, CloudPayments) |
| 🌍 **Языков интерфейса**   | 2 (RU, EN) с возможностью расширения                                                                        |
| 📊 **Периодов подписки**   | 6 (от 14 дней до года)                                                                                      |
| 🎁 **Типов промо-акций**   | 5 (коды, группы, предложения, скидки, кампании)                                                             |
| 🔌 **REST API эндпоинтов** | 50+ для полного управления                                                                                  |
| 📱 **Режимов работы**      | 3 (классический + тарифы + MiniApp focus)                                                                   |
| 🛡️ **Методов авторизации** | 4 (API Key, Bearer, Basic Auth, eGames)                                                                     |
| 🗄️ **Способов хранения**   | 2 (PostgreSQL, SQLite) с автовыбором                                                                        |

---

## 🔥 Почему выбирают Bedolaga?

### 💼 **Для бизнеса**

✅ **Быстрый запуск** - от установки до первых продаж за 10 минут  
✅ **Полная автоматизация** - бот работает 24/7 без вашего участия  
✅ **Прозрачная аналитика** - всегда знаете, сколько зарабатываете  
✅ **Гибкие тарифы** - настройте цены под свою аудиторию  
✅ **Система лояльности** - удерживайте клиентов промо-группами и скидками  
✅ **Масштабируемость** - от 10 до 100,000+ пользователей

### 🛠️ **Для разработчиков**

✅ **Современный стек** - Python 3.13, AsyncIO, PostgreSQL, Redis  
✅ **Модульная архитектура** - легко расширять и модифицировать  
✅ **Полное API** - интегрируйте с любыми сервисами  
✅ **Docker-ready** - разворачивается за минуты  
✅ **Подробная документация** - все описано и понятно  
✅ **Активное сообщество** - помощь в Telegram чате

### 👥 **Для пользователей**

✅ **Простой интерфейс** - интуитивно понятное меню на родном языке  
✅ **Много способов оплаты** - выбирайте удобный вариант  
✅ **Быстрая поддержка** - система тикетов с приоритетами  
✅ **Прозрачность** - всегда видите, за что платите  
✅ **Бонусы и скидки** - реферальная программа и промо-акции  
✅ **Удобное управление** - все в одном месте, в Telegram

---

## 💡 Советы по оптимизации

### ⚡ Повышение производительности

1. **Используйте Redis** для корзины и кэширования
2. **Настройте автосинхронизацию** в ночное время
3. **Включите автобэкапы** с отправкой в Telegram
4. **Оптимизируйте логирование** - LOG_LEVEL=INFO для продакшена
5. **Используйте PostgreSQL** вместо SQLite для больших баз

### 💰 Увеличение продаж

1. **Включите реферальную программу** - пользователи приведут друзей
2. **Настройте промо-группы** - дайте скидки постоянным клиентам
3. **Используйте персональные акции** - реактивируйте неактивных
4. **Запускайте кампании** - привлекайте новых через deeplink
5. **Добавьте быстрое пополнение** - упростите процесс оплаты

### 🎯 Улучшение UX

1. **Включите Mini App режим** - современный интерфейс
2. **Настройте корзину** - пользователи не потеряют выбор
3. **Добавьте FAQ** - ответьте на частые вопросы заранее
4. **Настройте быстрые ответы** - ускорьте поддержку
5. **Используйте уведомления** - держите пользователей в курсе

---

## 🔐 Безопасность и соответствие

### 🛡️ Защита данных

- ✅ Шифрование чувствительных данных в БД
- ✅ Безопасное хранение токенов и ключей
- ✅ Валидация всех входящих данных
- ✅ Защита от SQL-инъекций через ORM
- ✅ Rate limiting для предотвращения злоупотреблений
- ✅ Аудит всех административных действий

### 📋 Юридическое соответствие

- ✅ Политика конфиденциальности (настраивается)
- ✅ Публичная оферта (настраивается)
- ✅ Правила использования (настраивается)
- ✅ История транзакций для аудита
- ✅ Соответствие требованиям платёжных систем
- ✅ GDPR-ready (возможность удаления данных)

---

## 📞 Нужна помощь?

### 🆘 Частые вопросы

<details>
<summary><b>Какие требования к серверу?</b></summary>

Минимальные:

- 1 vCPU
- 512 MB RAM
- 10 GB диск
- Ubuntu 20.04+ или Debian 11+
- Docker и Docker Compose

Рекомендуемые:

- 2+ vCPU
- 2+ GB RAM
- 50+ GB SSD
- Стабильное интернет-соединение

</details>

<details>
<summary><b>Как настроить платёжную систему?</b></summary>

1. Получите ключи API в личном кабинете платёжной системы
2. Добавьте их в `.env` файл
3. Настройте webhook URL в кабинете провайдера
4. Протестируйте через админ-панель бота
5. Включите метод для пользователей

Подробнее: [docs/payment-setup.md](docs/payment-setup.md)

</details>

<details>
<summary><b>Как обновить бота?</b></summary>

**Вручную:**

```bash
cd /root/remnawave-bedolaga-telegram-bot

# Обновление до последнего коммита (до последней мастер ветки, не всегда стабильно):

git pull origin main

# Обновление до конкретной версии (более стабильно, релиз версии):

git fetch --tags
git checkout v2.7.0

# Перезагружаем конты:

make reload

# Перезагружаем конты с логами:

make reload-follow
```

Скрипт автоматически создаст бэкап перед обновлением!

</details>

<details>
<summary><b>Как сделать бэкап?</b></summary>

**Автоматически:**

- Настройте в `.env`: `BACKUP_AUTO_ENABLED=true`
- Бэкапы создаются по расписанию

**Через админ-панель:**

- Админ панель → Настройки → Бэкапы → Создать

</details>

<details>
<summary><b>Бот не отвечает, что делать?</b></summary>

1. Проверьте статус: `docker compose ps`
2. Посмотрите логи: `docker compose logs -f bot`
3. Проверьте BOT_TOKEN в `.env`
4. Убедитесь, что все контейнеры запущены
5. Попробуйте перезапустить: `docker compose restart`

Если не помогло - пишите в [чат поддержки](https://t.me/+wTdMtSWq8YdmZmVi)

</details>

### 💬 Куда обратиться?

- 🐛 **Баг?** → [GitHub Issues](https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot/issues)
- 💡 **Идея?** → [GitHub Discussions](https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot/discussions)
- 🆘 **Вопрос?** → [Bedolaga Chat](https://t.me/+wTdMtSWq8YdmZmVi)
- 📧 **Личное?** → [@fringg](https://t.me/fringg)

---

**Made with ❤️ by [@fringg](https://t.me/fringg) and amazing [contributors](https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot/graphs/contributors)**

<div align="center">

### ⭐ Не забудь поставить звезду проекту!

[![Star History Chart](https://api.star-history.com/svg?repos=Fr1ngg/remnawave-bedolaga-telegram-bot&type=Date)](https://star-history.com/#Fr1ngg/remnawave-bedolaga-telegram-bot&Date)

</div>

</div>
