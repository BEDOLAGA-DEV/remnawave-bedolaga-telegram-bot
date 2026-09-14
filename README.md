<div align="center">

<img src=".github/assets/logo.png" alt="Bedolaga Bot" width="800" />

# Bedolaga Bot

**Telegram-бот для автоматизации VPN-бизнеса на базе [Remnawave](https://github.com/remnawave/backend)**

Принимает оплату, выдаёт подписки, управляет пользователями — пока вы спите.

[![Python 3.13+](https://img.shields.io/badge/Python-3.13+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15+-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://postgresql.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docs.bedolagam.ru/getting-started/docker-deployment)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

[📖 Документация](https://docs.bedolagam.ru) · [🤖 Тестировать бота](https://t.me/zero_ping_vpn_bot?start=Git) · [💬 Чат сообщества](https://t.me/+wTdMtSWq8YdmZmVi)

</div>

---

## 🧩 Что такое Bedolaga?

Bedolaga — полнофункциональная платформа для продажи VPN-подписок через Telegram. Бот интегрируется с панелью [Remnawave](https://github.com/remnawave/backend) и берёт на себя весь цикл: от регистрации пользователя до автопродления подписки.

> 🖥 **[Bedolaga Cabinet](https://github.com/BEDOLAGA-DEV/bedolaga-cabinet)** — веб-кабинет на React + TypeScript, который существенно расширяет возможности бота: личный кабинет, OAuth-авторизация (Google, Yandex, Discord, VK, Telegram OIDC), лендинги, аналитика продаж, RBAC и подарочные подписки.

<div align="center">

<img src=".github/assets/bot-preview.png" alt="Bedolaga Bot — Telegram" width="700" />

</div>

---

## ✨ Возможности

<table>
<tr>
<td width="50%" valign="top">

### 📦 Подписки и тарифы

- 🎯 Гибкие тарифные планы (от X дней до X дней)
- 📊 Трафик: безлимит, фиксированный лимит или пакеты с возможностью докупки
- 📱 Управление устройствами (1–20 на подписку) или отключение лимитов
- 🌍 Автовыбор сервера(Тарифы) или ручной выбор(Конфигуратор подписки - с возможностью докупки)
- 🆓 Пробный период(Возможен платный) с конвертацией в платный
- 🛒 Умная корзина — сохраняет выбор при недостатке баланса
- 🔄 Автопродление за 3 дня до окончания
- 🎁 Подарочные подписки (нативно в Telegram-боте и веб-кабинете) с генерацией deep-link ссылок и передачей через Telegram Share

</td>
<td width="50%" valign="top">

### 💳 Платежи

- 🏦 **27 платёжных провайдеров** одновременно
- 💰 Единый баланс: пополнение любым способом → покупка с баланса
- ⚡ Автопокупка подписки после пополнения
- 💾 Рекуррентные платежи (сохранённые карты)
- 🧾 Фискализация через НалоGo (для самозанятых)
- 🔍 Автопроверка статуса платежей
- 🛍 Гостевые покупки через лендинги

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📣 Маркетинг и продвижение

- 🏷 Промокоды (деньги, дни подписки, триалы)
- 👥 Реферальная программа с выводом средств
- 👥 Партнерская система
- 📨 Рассылки по сегментам пользователей
- 🌐 Кастомные лендинги с аналитикой
- 🎮 Конкурсы и ежедневные игры с призами
- 🎯 Персональные предложения и скидки
- 📈 Маркетинговые кампании с трекингом
- 🌐 Обязательная мультиподписка на каналы с возможностью автоотключения подписки - при отписки от канала

</td>
<td width="50%" valign="top">

### 🛠 Администрирование

- 🤖 Панель управления прямо в Telegram
- 👤 Управление пользователями, подписками, платежами
- 📬 Уведомления в топики: покупки, продления, пополнения
- 💾 Автобэкапы БД с восстановлением из бота
- 🚧 Режим техработ (авто-детект недоступности панели)
- 📡 Мониторинг трафика и аномалий
- 🤝 Партнёрская программа
- 🔐 RBAC: роли и гранулярные права доступа
- 📈 Детальная отчетность с возможностью визуализации Реф сети
- 🔐 Блокировка юзеров из общего черного списка
И многое др...

</td>
</tr>
</table>

---

## 🎁 Подарочные подписки (Subscription Gifting)

Bedolaga поддерживает полный кросс-канальный жизненный цикл покупки, управления и активации подарочных подписок как нативно в Telegram-боте, так и через веб-кабинет [Bedolaga Cabinet](https://github.com/BEDOLAGA-DEV/bedolaga-cabinet).

### 🔑 Канонический формат кода и идентификация

- **Канонический публичный код**: `GIFT_<59_символов>` (например, `GIFT_7b8a1c9...`). Формат специально оптимизирован под ограничение длины deep-link параметров Telegram (`/start GIFT_<59>`).
- **Единая сущность `GuestPurchase`**: Подарок хранится в единой таблице гостевых покупок с 64-символьным токеном. Никаких дополнительных таблиц или миграций базы данных не требуется — канонический код и ссылки детерминированно выводятся из токена.
- **Конфиденциальность**: Полные токены, фрагменты токенов и публичные коды никогда не передаются в callback data, логах, описаниях транзакций или сообщениях об ошибках.

### 📲 Способы активации подарка

1. **Telegram Deep-Link**: Ссылка вида `https://t.me/<bot_username>?start=GIFT_<59_символов>`. При переходе бот сразу регистрирует нового пользователя (или приветствует существующего) и активирует подарочную подписку.
2. **Ручной ввод в Telegram-боте**: Кнопка **«🎁 Подарки» → «Ввести код»**. Принимает канонический код `GIFT_<59>`, исходный 64-значный токен, deep-link ссылку Telegram или URL веб-кабинета. При вводе недействительного кода бот сохраняет состояние для повторной попытки.
3. **Активация в Web Cabinet**: Эндпоинт `/gift/activate` и форма в личном кабинете. Поддерживает канонические коды `GIFT_<59>`, ссылки deep-link/веб-кабинета, а также устаревшие форматы (8/12-символьные префиксы и `GIFT-<12>`).
4. **Прямая веб-ссылка**: `https://<cabinet_url>/buy/gift/<64_символа>` — интерфейс гостевой или авторизованной активации в веб-кабинете.

### 📜 Раздел «Мои подарки» (History & Recovery)

- **Восстановление и шаринг**: В Telegram-боте в меню подписок доступен раздел **«🎁 Мои подарки»**. Покупатель в любой момент может открыть купленный подарок, скопировать канонический код `<code>GIFT_<59></code>`, открыть deep-link или переслать подарок через кнопку Telegram Share (`t.me/share/url`). Восстановление работает даже после очистки FSM-контекста или перезапуска бота без повторных списаний баланса.
- **Единый вид карточки (Source-neutral)**: Подарки, купленные в боте или в веб-кабинете, отображаются в боте идентично — без указания канала покупки, способов оплаты или цены.
- **Статус доставки**: После активации получателем статус подарка меняется на **«Активирован»**, отображается имя получателя (`@username` или email) и дата активации, а кнопки передачи и код скрываются.

### ⚙️ Правила и бизнес-логика для операторов

- **Включение функционала**: Установите `CABINET_GIFT_ENABLED=true` (через `SystemSetting` в БД или админ-панели / `.env`). Флаг управляет каталогом подарков одновременно в боте и веб-кабинете.
- **Настройка тарифов**: Отметьте тарифы, доступные для дарения, флагом `show_in_gift = true` (`SHOW_IN_GIFT`).
- **Синхронизация ссылок**: Настройте `BOT_USERNAME` (username Telegram-бота) и `CABINET_URL` (URL веб-кабинета).
- **Оплата подарков**: В боте подарок оплачивается с внутреннего баланса. При нехватке средств бот предлагает пополнение через любой шлюз с сохранением корзины. В кабинете доступна оплата как с баланса, так и через платёжные шлюзы (YooKassa, CryptoBot, Stars и др.).
- **Запрет самоактивации (Self-claim prohibition)**: Покупатель не может активировать свой собственный подарок ни в боте, ни в кабинете.
- **Право первого заявителя (First-claim ownership)**: Подарок закрепляется за первым активировавшим его пользователем. Попытки активации другими пользователями отклоняются (в кабинете возвращается 404 для защиты от подбора).
- **Идемпотентность**: Повторный ввод кода тем же самым получателем возвращает успешную карточку активации без повторного начисления подписки.
- **Бессрочность хранения**: Оплаченные подарки не сгорают и не имеют срока давности.
- **Управление продажами vs. право на получение**: При отключении `CABINET_GIFT_ENABLED=false` каталог покупки подарков скрывается, но все ранее оплаченные подарки остаются доступны в разделе «Мои подарки» и могут быть активированы получателями.

### 🌐 Cabinet API & Frontend Boundary

Бэкенд предоставляет и тестирует полный контракт API для подарочных подписок:
- **Канонические поля**: `gift_code` (публичный код `GIFT_<59_chars>`), `bot_claim_url` (`https://t.me/<bot>?start=GIFT_<59_chars>`) и `cabinet_claim_url` (`https://<cabinet>/buy/gift/<64_chars>`) возвращаются в ответах `/gift/purchase`, `/gift/purchase/{token}`, `/gift/sent` и `/landing/purchase/{token}`, `/landing/gift/{token}` для всех активных (`PAID` / `PENDING_ACTIVATION`) подарков. Для доставленных подарков (`DELIVERED`) поля действий сбрасываются в `None`, сохраняя историю.
- **Обратная совместимость**: Устаревшие поля (`purchase_token`, `token`, `claim_url`, `bot_claim_link`) сохраняются без изменений. Эндпоинт активации `/gift/activate` продолжает принимать короткие 8/12-символьные коды, префиксы `GIFT-`, канонические `GIFT_...` коды и URL-ссылки.
- **Граница внешнего фронтенда**: Этот репозиторий содержит бэкенд и тесты контракта API. Внешний веб-кабинет ([Bedolaga Cabinet](https://github.com/BEDOLAGA-DEV/bedolaga-cabinet)) разрабатывается в отдельном репозитории и должен отображать новые канонические поля `gift_code`, `bot_claim_url` и `cabinet_claim_url` для визуального паритета с ботом.

---

## 💳 Платёжные провайдеры

<div align="center">

| | Провайдер | Методы оплаты | Валюта |
|:---:|:---|:---|:---:|
| ⭐ | **Telegram Stars** | Звёзды Telegram | XTR |
| 🏦 | **YooKassa** | Карты, СБП | RUB |
| 🏦 | **YooKassa СБП** | Система быстрых платежей | RUB |
| 🪙 | **CryptoBot** | USDT, TON, BTC, ETH | Crypto |
| 🪙 | **Heleket** | USDT, мульти-сеть | Crypto |
| 💳 | **CloudPayments** | Карты, 3D-Secure | RUB |
| 💳 | **Freekassa** | NSPK СБП, карты | RUB |
| 💳 | **Kassa AI** | СБП, карты, SberPay | RUB |
| 💳 | **PayPalych (Pal24)** | Карты, СБП | RUB |
| 🤝 | **[Platega](https://t.me/ArstanPlatega)** 🔸 | Карты, СБП, крипто | RUB |
| 💳 | **WATA** | СБП, Карты | RUB |
| 💳 | **MulenPay** | Карты | RUB |
| 💳 | **RioPay** | Карты | RUB |
| 💳 | **SeverPay** | СБП, карты | RUB |
| 🤝 | **[PayPear](https://t.me/Paymen1_Manager)** 🔸 | Карты, СБП, SberPay, T-Pay | RUB |
| 🤝 | **[RollyPay](https://rollypay.io/?utm_source=bedolaga&utm_medium=community&utm_campaign=integration)** 🔸 | СБП, карты, крипто | RUB → USDT |
| 🤝 | **[AuraPay](https://aurapay.tech/)** 🔸 | Карты, СБП | RUB |
| 🤝 | **[Overpay](https://overpay.pro/)** 🔸 | Карты, СБП | RUB |
| 🦌 | **Antilopay** | Карты, СБП, SberPay (RSA подпись) | RUB |
| 💳 | **Etoplatezhi** | Карты, СБП | RUB |
| 🪐 | **[Jupiter](https://t.me/k_juppiter)** 🔸 | СБП через QR (FPGate P2P v2.1) | RUB |
| 🍩 | **[Donut](https://t.me/donut_payment)** 🔸 | Карты, СБП по телефону, СБП QR (P2P) | RUB |
| 🌋 | **Lava Business** | Карты, СБП (gate.lava.ru) | RUB |
| 💳 | **CisPay** | СБП, карты (api.cispay.app) | RUB |
| 💳 | **TabPay** | СБП, карты с 3-D Secure (tabpay.org) | RUB |
| 💳 | **ParityPay** | СБП, карты (api.paritypay.net) | RUB |
| 🍎 | **Apple In-App Purchase** | Покупки через iOS App Store | USD |
| 📲 | **Tribute** | Telegram-платежи | RUB |

</div>

> 🔸 — официальный партнёр Bedolaga (особые условия по кодовому слову **`bedolaga`**)
>
> Все провайдеры работают параллельно через единый веб-сервер на порту 8080. Подробная настройка — в [документации](https://docs.bedolagam.ru/bot/payments).

<div align="center">
<table>
<tr>
<td align="center">

<img src=".github/assets/platega-logo.jpg" alt="Platega" width="60" />

**🤝 Официальный партнёр Platega**

Bedolaga — официальный партнёр платёжной системы **Platega**.<br>
Пользователи бота получают **особые условия** при подключении по кодовому слову **`bedolaga`**

📩 По вопросам: [@ArstanPlatega](https://t.me/ArstanPlatega)

</td>
</tr>
<tr>
<td align="center">

**🤝 Официальный партнёр PayPear**

Bedolaga — официальный партнёр платёжной системы **[PayPear](https://paypear.ru)**.<br>
Банковские карты, СБП, SberPay и T-Pay — всё через единый API.<br>
Подключение по **спец. условиям** через кодовое слово **`БЕДОЛАГА`**

📩 Менеджер: [@Paymen1_Manager](https://t.me/Paymen1_Manager)

</td>
<td align="center">

**🤝 Официальный партнёр RollyPay**

Bedolaga — официальный партнёр платёжного шлюза **[RollyPay](https://rollypay.io/?utm_source=bedolaga&utm_medium=community&utm_campaign=integration)**.<br>
СБП (от 5%), банковские карты РФ, крипто, вывод в USDT.<br>
Универсальная форма оплаты, высокая проходимость, стабильная работа в каскаде.<br>
Подключение по кодовому слову **`БЕДОЛАГА`** — **спец. условия**

📩 Менеджер: [@rollypay_manager](https://t.me/rollypay_manager) | 🌐 [rollypay.io](https://rollypay.io/?utm_source=bedolaga&utm_medium=community&utm_campaign=integration)

</td>
</tr>
<tr>
<td align="center">

**🤝 Официальный партнёр AuraPay**

Bedolaga — официальный партнёр платёжной системы **[AuraPay](https://aurapay.tech/)**.<br>
Банковские карты и СБП через единый API с быстрой интеграцией.<br>
Подключение по кодовому слову **`БЕДОЛАГА`** — **спец. условия**

📩 Менеджер: [@kickdownm](https://t.me/kickdownm) | 🌐 [aurapay.tech](https://aurapay.tech/)

</td>
<td align="center">

**🤝 Официальный партнёр Overpay**

Bedolaga — официальный партнёр платёжного шлюза **[Overpay](https://overpay.pro/)**.<br>
Банковские карты и СБП, mTLS-авторизация, HPP-интеграция.<br>
Подключение по кодовому слову **`БЕДОЛАГА`** — **спец. условия**

📩 Менеджер: [@A_OverPay](https://t.me/A_OverPay) | 🌐 [overpay.pro](https://overpay.pro/)

</td>
</tr>
<tr>
<td align="center">

**🤝 Официальный партнёр Jupiter (FPGate P2P)**

Bedolaga — официальный партнёр платёжного шлюза **Jupiter** (FPGate P2P v2.1).<br>
Эквайринг СБП через QR-код банковского приложения, HMAC-SHA256 подпись.<br>
Высокая проходимость, callback-driven архитектура, защита от replay-атак.<br>
Подключение по кодовому слову **`БЕДОЛАГА`** — **спец. условия**

📩 Менеджер: [@k_juppiter](https://t.me/k_juppiter)

</td>
<td align="center">

**🤝 Официальный партнёр Donut**

Bedolaga — официальный партнёр платёжной системы **Donut** (Donut P2P).<br>
P2P-оплата картой, СБП по номеру телефона и СБП QR — три метода через единый API.<br>
HMAC-SHA256 подпись, sticky terminal-status guard, защита от amount tampering.<br>
Подключение по кодовому слову **`БЕДОЛАГА`** — **спец. условия**

📩 Менеджер: [@donut_payment](https://t.me/donut_payment)

</td>
</tr>
</table>
</div>

---

## 🚀 Быстрый старт

```bash
git clone https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot.git
cd remnawave-bedolaga-telegram-bot
cp .env.example .env   # заполните переменные
docker compose up -d
```

📖 Подробнее: **[Развёртывание →](https://docs.bedolagam.ru/getting-started/docker-deployment)** · **[Переменные окружения →](https://docs.bedolagam.ru/getting-started/environment)**

---

## 🏗 Стек

| | Компонент | Технология |
|:---:|:---|:---|
| 🐍 | Язык | Python 3.13, полностью async |
| 🤖 | Telegram | aiogram 3.x |
| 🗄 | База данных | PostgreSQL + SQLAlchemy 2.x + Alembic |
| 🔴 | Кэш/очереди | Redis |
| ⚡ | Web-сервер | FastAPI (webhook, платежи, Cabinet API) |
| 📝 | Логирование | structlog |
| 🐳 | Контейнеризация | Docker + Docker Compose |
| 🧹 | Линтер | ruff |

---

## 🖥 Bedolaga Cabinet

<div align="center">

[![Cabinet](https://img.shields.io/badge/Репозиторий-Bedolaga_Cabinet-6366f1?style=for-the-badge&logo=react&logoColor=white)](https://github.com/BEDOLAGA-DEV/bedolaga-cabinet)

<br>

<img src=".github/assets/cabinet-preview.png" alt="Bedolaga Cabinet" width="700" />

</div>

Веб-кабинет на **React + TypeScript**, существенно расширяющий возможности бота:

| | Возможность | Описание |
|:---:|:---|:---|
| 👤 | **Личный кабинет** | Подписки, баланс, устройства, реферальная программа |
| 🔑 | **OAuth-авторизация** | Google, Yandex, Discord, VK, Telegram OIDC |
| 🌐 | **Лендинги** | Кастомные страницы для привлечения клиентов |
| 📊 | **Админ-панель** | Аналитика продаж, RBAC, управление контентом |
| 🎁 | **Подарки** | Покупка и отправка подписок другим пользователям |
| 🔍 | **Поиск платежей** | Поиск по инвойсу, клиенту с фильтрами и статистикой |

---

## 📚 Документация

| | Раздел | Описание |
|:---:|:---|:---|
| 🚀 | [Быстрый старт](https://docs.bedolagam.ru/getting-started/quickstart) | Развёртывание за 5 минут |
| 💳 | [Настройка платежей](https://docs.bedolagam.ru/bot/payments) | 27 провайдеров, webhook, фискализация, Apple IAP |
| 📦 | [Подписки и тарифы](https://docs.bedolagam.ru/bot/subscriptions) | Конфигурация планов и трафика |
| 👥 | [Реферальная программа](https://docs.bedolagam.ru/bot/referral-program) | Партнёрка и вывод средств |
| 🖥 | [Cabinet](https://docs.bedolagam.ru/cabinet/overview) | Настройка веб-кабинета |
| 🏷 | [Промо-система](https://docs.bedolagam.ru/bot/promo-system) | Промокоды, предложения, скидки |
| 🔌 | [API Reference](https://docs.bedolagam.ru/api-reference/overview) | REST API для внешних интеграций |

<div align="center">

**📖 Полная документация: [docs.bedolagam.ru](https://docs.bedolagam.ru)**

</div>

---

## 💬 Сообщество

<div align="center">

[![Telegram Chat](https://img.shields.io/badge/Telegram-Чат_сообщества-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/+wTdMtSWq8YdmZmVi)
[![GitHub Issues](https://img.shields.io/badge/GitHub-Issues-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/issues)

</div>

- 🐛 **Баги и предложения** — [GitHub Issues](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/issues)
- 💬 **Вопросы и обсуждения** — [Telegram-чат](https://t.me/+wTdMtSWq8YdmZmVi)
- 🤖 **Тестирование** — [@zero_ping_vpn_bot](https://t.me/zero_ping_vpn_bot?start=Git)

---

<div align="center">

**[MIT License](LICENSE)** — используйте свободно для личных и коммерческих проектов.

</div>
