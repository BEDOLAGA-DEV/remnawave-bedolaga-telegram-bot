import hashlib
import hmac
import html
import logging
import math
import os
import re
from collections import defaultdict
from datetime import time
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


DEFAULT_DISPLAY_NAME_BANNED_KEYWORDS: list[str] = [
    # Пустой по умолчанию - администратор может добавить ключевые слова через DISPLAY_NAME_BANNED_KEYWORDS
    # Примеры: "tme", "joingroup", "support", "admin"
]

USER_TAG_PATTERN = re.compile(r'^[A-Z0-9_]{1,16}$')


logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    BOT_TOKEN: str
    BOT_USERNAME: str | None = None
    ADMIN_IDS: str = ''
    ADMIN_EMAILS: str = ''  # Comma-separated admin emails for email-only users

    # Test email account for development/testing (bypasses email verification and SMTP)
    TEST_EMAIL: str = ''  # e.g., test@example.com
    TEST_EMAIL_PASSWORD: str = ''  # Password for test account

    SUPPORT_USERNAME: str = '@support'
    SUPPORT_MENU_ENABLED: bool = True
    SUPPORT_SYSTEM_MODE: str = 'both'  # one of: tickets, contact, both
    # SLA for support tickets
    SUPPORT_TICKET_SLA_ENABLED: bool = True
    SUPPORT_TICKET_SLA_MINUTES: int = 5
    SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS: int = 60
    SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES: int = 15

    # MiniApp tickets settings
    MINIAPP_TICKETS_ENABLED: bool = True  # Enable/disable tickets section in miniapp
    MINIAPP_SUPPORT_TYPE: str = 'tickets'  # one of: tickets, profile, url
    MINIAPP_SUPPORT_URL: str = ''  # Custom URL to redirect when tickets disabled (only for url type)

    ADMIN_NOTIFICATIONS_ENABLED: bool = False
    ADMIN_NOTIFICATIONS_CHAT_ID: str | None = None
    ADMIN_NOTIFICATIONS_TOPIC_ID: int | None = None
    ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID: int | None = None
    ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID: int | None = None

    # Настройки очереди чеков NaloGO
    NALOGO_QUEUE_CHECK_INTERVAL: int = 300  # Интервал проверки очереди (секунды)
    NALOGO_QUEUE_RECEIPT_DELAY: int = 3  # Задержка между отправкой чеков (секунды)
    NALOGO_QUEUE_MAX_ATTEMPTS: int = 10  # Максимум попыток отправки чека

    ADMIN_REPORTS_ENABLED: bool = False
    ADMIN_REPORTS_CHAT_ID: str | None = None
    ADMIN_REPORTS_TOPIC_ID: int | None = None
    ADMIN_REPORTS_SEND_TIME: str | None = None

    CHANNEL_SUB_ID: str | None = None
    CHANNEL_LINK: str | None = None
    CHANNEL_IS_REQUIRED_SUB: bool = False
    CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE: bool = True
    CHANNEL_REQUIRED_FOR_ALL: bool = False

    DATABASE_URL: str | None = None

    POSTGRES_HOST: str = 'postgres'
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = 'remnawave_bot'
    POSTGRES_USER: str = 'remnawave_user'
    POSTGRES_PASSWORD: str = 'secure_password_123'

    SQLITE_PATH: str = './data/bot.db'
    LOCALES_PATH: str = './locales'

    TIMEZONE: str = Field(default_factory=lambda: os.getenv('TZ', 'UTC'))

    DATABASE_MODE: str = 'auto'

    REDIS_URL: str = 'redis://localhost:6379/0'
    CART_TTL_SECONDS: int = 3600  # Время жизни корзины пользователя в Redis (1 час)

    REMNAWAVE_API_URL: str | None = None
    REMNAWAVE_API_KEY: str | None = None
    REMNAWAVE_SECRET_KEY: str | None = None

    REMNAWAVE_USERNAME: str | None = None
    REMNAWAVE_PASSWORD: str | None = None
    REMNAWAVE_CADDY_TOKEN: str | None = None
    REMNAWAVE_AUTH_TYPE: str = 'api_key'  # api_key, basic, bearer, cookies, caddy
    REMNAWAVE_USER_DESCRIPTION_TEMPLATE: str = 'Bot user: {full_name} {username}'
    REMNAWAVE_USER_USERNAME_TEMPLATE: str = 'user_{telegram_id}'
    REMNAWAVE_USER_DELETE_MODE: str = 'delete'  # "delete" или "disable"
    REMNAWAVE_AUTO_SYNC_ENABLED: bool = False
    REMNAWAVE_AUTO_SYNC_TIMES: str = '03:00'
    CABINET_REMNA_SUB_CONFIG: str | None = None  # UUID конфига страницы подписки из RemnaWave

    TRIAL_DURATION_DAYS: int = 3
    TRIAL_TRAFFIC_LIMIT_GB: int = 10
    TRIAL_DEVICE_LIMIT: int = 2
    TRIAL_ADD_REMAINING_DAYS_TO_PAID: bool = False
    TRIAL_PAYMENT_ENABLED: bool = False
    TRIAL_ACTIVATION_PRICE: int = 0
    TRIAL_USER_TAG: str | None = None
    TRIAL_DISABLED_FOR: str = 'none'  # none, email, telegram, all
    DEFAULT_TRAFFIC_LIMIT_GB: int = 100
    DEFAULT_DEVICE_LIMIT: int = 1
    DEFAULT_TRAFFIC_RESET_STRATEGY: str = 'MONTH'
    RESET_TRAFFIC_ON_PAYMENT: bool = False
    MAX_DEVICES_LIMIT: int = 20

    TRIAL_WARNING_HOURS: int = 2
    ENABLE_NOTIFICATIONS: bool = True
    NOTIFICATION_RETRY_ATTEMPTS: int = 3

    MONITORING_LOGS_RETENTION_DAYS: int = 30
    NOTIFICATION_CACHE_HOURS: int = 24

    SERVER_STATUS_MODE: str = 'disabled'
    SERVER_STATUS_EXTERNAL_URL: str | None = None
    SERVER_STATUS_METRICS_URL: str | None = None
    SERVER_STATUS_METRICS_USERNAME: str | None = None
    SERVER_STATUS_METRICS_PASSWORD: str | None = None
    SERVER_STATUS_METRICS_VERIFY_SSL: bool = True
    SERVER_STATUS_REQUEST_TIMEOUT: int = 10
    SERVER_STATUS_ITEMS_PER_PAGE: int = 10

    BASE_SUBSCRIPTION_PRICE: int = 50000
    AVAILABLE_SUBSCRIPTION_PERIODS: str = '14,30,60,90,180,360'
    AVAILABLE_RENEWAL_PERIODS: str = '30,90,180'
    PRICE_14_DAYS: int = 50000
    PRICE_30_DAYS: int = 99000
    PRICE_60_DAYS: int = 189000
    PRICE_90_DAYS: int = 269000
    PRICE_180_DAYS: int = 499000
    PRICE_360_DAYS: int = 899000
    PAID_SUBSCRIPTION_USER_TAG: str | None = None

    PRICE_TRAFFIC_5GB: int = 2000
    PRICE_TRAFFIC_10GB: int = 3500
    PRICE_TRAFFIC_25GB: int = 7000
    PRICE_TRAFFIC_50GB: int = 11000
    PRICE_TRAFFIC_100GB: int = 15000
    PRICE_TRAFFIC_250GB: int = 17000
    PRICE_TRAFFIC_500GB: int = 19000
    PRICE_TRAFFIC_1000GB: int = 19500
    PRICE_TRAFFIC_UNLIMITED: int = 20000

    TRAFFIC_PACKAGES_CONFIG: str = ''

    PRICE_PER_DEVICE: int = 5000
    DEVICES_SELECTION_ENABLED: bool = True
    DEVICES_SELECTION_DISABLED_AMOUNT: int | None = None

    # Настройки модема
    MODEM_ENABLED: bool = False
    MODEM_PRICE_PER_MONTH: int = 10000  # Цена модема в копейках за месяц
    MODEM_PERIOD_DISCOUNTS: str = ''  # Скидки на модем: "месяцев:процент,месяцев:процент" (напр. "3:10,6:15,12:20")

    BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED: bool = False
    BASE_PROMO_GROUP_PERIOD_DISCOUNTS: str = ''

    # Режим выбора трафика:
    # - selectable: пользователь выбирает трафик при покупке и может докупать
    # - fixed: фиксированный лимит, без выбора и без докупки
    # - fixed_with_topup: фиксированный лимит при покупке, но докупка разрешена (при продлении сброс до лимита)
    TRAFFIC_SELECTION_MODE: str = 'selectable'
    FIXED_TRAFFIC_LIMIT_GB: int = 100
    BUY_TRAFFIC_BUTTON_VISIBLE: bool = True

    # Режим продаж подписок:
    # - classic: классический режим (выбор серверов, трафика, устройств, периода отдельно)
    # - tariffs: режим тарифов (готовые пакеты с фиксированными параметрами)
    SALES_MODE: str = 'classic'

    # ID тарифа для триала в режиме тарифов (0 = использовать стандартные настройки триала)
    # Если указан ID тарифа, параметры триала берутся из тарифа (traffic_limit_gb, device_limit, allowed_squads)
    # Длительность триала всё равно берётся из TRIAL_DURATION_DAYS
    TRIAL_TARIFF_ID: int = 0

    # Настройки докупки трафика
    TRAFFIC_TOPUP_ENABLED: bool = True  # Включить/выключить функцию докупки трафика
    # Пакеты для докупки трафика (формат: "гб:цена:enabled", пустая строка = использовать TRAFFIC_PACKAGES_CONFIG)
    TRAFFIC_TOPUP_PACKAGES_CONFIG: str = ''

    # Настройки сброса трафика
    # Режимы расчета цены сброса:
    # "period" - фиксированная цена = стоимость периода 30 дней (старое поведение)
    # "traffic" - цена зависит от текущего лимита трафика (цена пакета трафика)
    # "traffic_with_purchased" - цена = базовый трафик + докупленный трафик (рекомендуется)
    TRAFFIC_RESET_PRICE_MODE: str = 'traffic_with_purchased'
    # Базовая цена сброса в копейках (используется если режим "period" или как минимальная цена)
    TRAFFIC_RESET_BASE_PRICE: int = 0  # 0 = использовать PERIOD_PRICES[30]

    REFERRAL_MINIMUM_TOPUP_KOPEKS: int = 10000
    REFERRAL_FIRST_TOPUP_BONUS_KOPEKS: int = 10000
    REFERRAL_INVITER_BONUS_KOPEKS: int = 10000
    REFERRAL_COMMISSION_PERCENT: int = 25

    REFERRAL_PROGRAM_ENABLED: bool = True
    REFERRAL_NOTIFICATIONS_ENABLED: bool = True
    REFERRAL_NOTIFICATION_RETRY_ATTEMPTS: int = 3

    # Настройки вывода реферального баланса
    REFERRAL_WITHDRAWAL_ENABLED: bool = False  # Включить возможность вывода
    REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS: int = 100000  # Мин. сумма вывода (1000₽)
    REFERRAL_WITHDRAWAL_COOLDOWN_DAYS: int = 30  # Частота запросов на вывод
    REFERRAL_WITHDRAWAL_ONLY_REFERRAL_BALANCE: bool = True  # Только реф. баланс (False = реф + свой)
    REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID: int | None = None  # Топик для уведомлений

    # Настройки анализа на подозрительность
    REFERRAL_WITHDRAWAL_SUSPICIOUS_MIN_DEPOSIT_KOPEKS: int = 50000  # Мин. сумма от 1 реферала (500₽)
    REFERRAL_WITHDRAWAL_SUSPICIOUS_MAX_DEPOSITS_PER_MONTH: int = 10  # Макс. пополнений от 1 реферала/мес
    REFERRAL_WITHDRAWAL_SUSPICIOUS_NO_PURCHASES_RATIO: float = 2.0  # Пополнил в X раз больше чем потратил

    # Тестовый режим для вывода (позволяет админам вручную начислять реф. доход)
    REFERRAL_WITHDRAWAL_TEST_MODE: bool = False

    # Конкурсы (глобальный флаг, будет расширяться под разные типы)
    CONTESTS_ENABLED: bool = False
    CONTESTS_BUTTON_VISIBLE: bool = False
    # Для обратной совместимости со старыми конфигами
    REFERRAL_CONTESTS_ENABLED: bool = False

    BLACKLIST_CHECK_ENABLED: bool = False
    BLACKLIST_GITHUB_URL: str | None = None
    BLACKLIST_UPDATE_INTERVAL_HOURS: int = 24
    BLACKLIST_IGNORE_ADMINS: bool = True

    DISPOSABLE_EMAIL_CHECK_ENABLED: bool = True

    # Настройки простой покупки
    SIMPLE_SUBSCRIPTION_ENABLED: bool = False
    SIMPLE_SUBSCRIPTION_PERIOD_DAYS: int = 30
    SIMPLE_SUBSCRIPTION_DEVICE_LIMIT: int = 1
    SIMPLE_SUBSCRIPTION_TRAFFIC_GB: int = 0  # 0 означает безлимит
    SIMPLE_SUBSCRIPTION_SQUAD_UUID: str | None = None

    # Настройки конструктора меню (API)
    MENU_LAYOUT_ENABLED: bool = False  # Включить управление меню через API

    # Настройки мониторинга трафика
    TRAFFIC_MONITORING_ENABLED: bool = False  # Глобальный переключатель (для обратной совместимости)
    TRAFFIC_THRESHOLD_GB_PER_DAY: float = 10.0  # Порог трафика в ГБ за сутки (для обратной совместимости)
    TRAFFIC_MONITORING_INTERVAL_HOURS: int = 24  # Интервал проверки в часах (для обратной совместимости)
    SUSPICIOUS_NOTIFICATIONS_TOPIC_ID: int | None = None

    # Новый мониторинг трафика v2
    # Быстрая проверка (текущий использованный трафик)
    TRAFFIC_FAST_CHECK_ENABLED: bool = False
    TRAFFIC_FAST_CHECK_INTERVAL_MINUTES: int = 10  # Интервал проверки в минутах
    TRAFFIC_FAST_CHECK_THRESHOLD_GB: float = 5.0  # Порог в ГБ для быстрой проверки

    # Суточная проверка (трафик за 24 часа)
    TRAFFIC_DAILY_CHECK_ENABLED: bool = False
    TRAFFIC_DAILY_CHECK_TIME: str = '00:00'  # Время суточной проверки (HH:MM)
    TRAFFIC_DAILY_THRESHOLD_GB: float = 50.0  # Порог суточного трафика в ГБ

    # Фильтрация по серверам (UUID нод через запятую)
    TRAFFIC_MONITORED_NODES: str = ''  # Только эти ноды (пусто = все)
    TRAFFIC_IGNORED_NODES: str = ''  # Исключить эти ноды
    TRAFFIC_EXCLUDED_USER_UUIDS: str = ''  # Исключить пользователей (UUID через запятую)

    # Параллельность и кулдаун
    TRAFFIC_CHECK_BATCH_SIZE: int = 1000  # Размер батча для получения пользователей
    TRAFFIC_CHECK_CONCURRENCY: int = 10  # Параллельных запросов
    TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES: int = 60  # Кулдаун уведомлений (минуты)
    TRAFFIC_SNAPSHOT_TTL_HOURS: int = 24  # TTL для snapshot трафика в Redis (часы)
    # Настройки суточных подписок
    DAILY_SUBSCRIPTIONS_ENABLED: bool = True  # Включить автоматическое списание для суточных тарифов
    DAILY_SUBSCRIPTIONS_CHECK_INTERVAL_MINUTES: int = 30  # Интервал проверки в минутах

    AUTOPAY_WARNING_DAYS: str = '3,1'

    ENABLE_AUTOPAY: bool = False

    DEFAULT_AUTOPAY_ENABLED: bool = False
    DEFAULT_AUTOPAY_DAYS_BEFORE: int = 3
    MIN_BALANCE_FOR_AUTOPAY_KOPEKS: int = 10000
    SUBSCRIPTION_RENEWAL_BALANCE_THRESHOLD_KOPEKS: int = 20000

    MONITORING_INTERVAL: int = 60
    INACTIVE_USER_DELETE_MONTHS: int = 3

    MAINTENANCE_MODE: bool = False
    MAINTENANCE_CHECK_INTERVAL: int = 30
    MAINTENANCE_AUTO_ENABLE: bool = True
    MAINTENANCE_MONITORING_ENABLED: bool = True
    MAINTENANCE_RETRY_ATTEMPTS: int = 1
    MAINTENANCE_MESSAGE: str = '🔧 Ведутся технические работы. Сервис временно недоступен. Попробуйте позже.'

    TELEGRAM_STARS_ENABLED: bool = True
    TELEGRAM_STARS_RATE_RUB: float = 1.3
    TELEGRAM_STARS_DISPLAY_NAME: str = 'Telegram Stars'

    TRIBUTE_ENABLED: bool = False
    TRIBUTE_API_KEY: str | None = None
    TRIBUTE_DONATE_LINK: str | None = None
    TRIBUTE_WEBHOOK_PATH: str = '/tribute-webhook'
    TRIBUTE_WEBHOOK_HOST: str = '0.0.0.0'
    TRIBUTE_WEBHOOK_PORT: int = 8081

    YOOKASSA_ENABLED: bool = False
    YOOKASSA_DISPLAY_NAME: str = 'YooKassa'
    YOOKASSA_SHOP_ID: str | None = None
    YOOKASSA_SECRET_KEY: str | None = None
    YOOKASSA_RETURN_URL: str | None = None
    YOOKASSA_DEFAULT_RECEIPT_EMAIL: str | None = None
    YOOKASSA_VAT_CODE: int = 1
    YOOKASSA_SBP_ENABLED: bool = False
    YOOKASSA_PAYMENT_MODE: str = 'full_payment'
    YOOKASSA_PAYMENT_SUBJECT: str = 'service'
    YOOKASSA_WEBHOOK_PATH: str = '/yookassa-webhook'
    YOOKASSA_WEBHOOK_HOST: str = '0.0.0.0'
    YOOKASSA_WEBHOOK_PORT: int = 8082
    YOOKASSA_TRUSTED_PROXY_NETWORKS: str = ''
    YOOKASSA_MIN_AMOUNT_KOPEKS: int = 5000
    YOOKASSA_MAX_AMOUNT_KOPEKS: int = 1000000
    YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED: bool = False
    DISABLE_TOPUP_BUTTONS: bool = False
    SUPPORT_TOPUP_ENABLED: bool = True
    PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED: bool = False
    PAYMENT_VERIFICATION_AUTO_CHECK_INTERVAL_MINUTES: int = 10

    NALOGO_ENABLED: bool = False
    NALOGO_INN: str | None = None
    NALOGO_PASSWORD: str | None = None
    NALOGO_DEVICE_ID: str | None = None
    NALOGO_STORAGE_PATH: str = './nalogo_tokens.json'

    AUTO_PURCHASE_AFTER_TOPUP_ENABLED: bool = False
    AUTO_ACTIVATE_AFTER_TOPUP_ENABLED: bool = False

    # Показывать предупреждение об активации подписки после пополнения баланса
    # Если True - после пополнения показывает большое сообщение с кнопками:
    # "Активировать", "Продлить", "Добавить устройства"
    SHOW_ACTIVATION_PROMPT_AFTER_TOPUP: bool = False

    # Отключение превью ссылок в сообщениях бота
    DISABLE_WEB_PAGE_PREVIEW: bool = False
    ACTIVATE_BUTTON_VISIBLE: bool = False
    ACTIVATE_BUTTON_TEXT: str = 'активировать'
    PAYMENT_BALANCE_DESCRIPTION: str = 'Пополнение баланса'
    PAYMENT_SUBSCRIPTION_DESCRIPTION: str = 'Оплата подписки'
    PAYMENT_SERVICE_NAME: str = 'Интернет-сервис'
    PAYMENT_BALANCE_TEMPLATE: str = '{service_name} - {description}'
    PAYMENT_SUBSCRIPTION_TEMPLATE: str = '{service_name} - {description}'

    CRYPTOBOT_ENABLED: bool = False
    CRYPTOBOT_DISPLAY_NAME: str = 'CryptoBot'
    CRYPTOBOT_API_TOKEN: str | None = None
    CRYPTOBOT_WEBHOOK_SECRET: str | None = None
    CRYPTOBOT_BASE_URL: str = 'https://pay.crypt.bot'
    CRYPTOBOT_TESTNET: bool = False
    CRYPTOBOT_WEBHOOK_PATH: str = '/cryptobot-webhook'
    CRYPTOBOT_WEBHOOK_PORT: int = 8083
    CRYPTOBOT_DEFAULT_ASSET: str = 'USDT'
    CRYPTOBOT_ASSETS: str = 'USDT,TON,BTC,ETH'
    CRYPTOBOT_INVOICE_EXPIRES_HOURS: int = 24

    HELEKET_ENABLED: bool = False
    HELEKET_DISPLAY_NAME: str = 'Heleket Crypto'
    HELEKET_MERCHANT_ID: str | None = None
    HELEKET_API_KEY: str | None = None
    HELEKET_BASE_URL: str = 'https://api.heleket.com/v1'
    HELEKET_DEFAULT_CURRENCY: str = 'USDT'
    HELEKET_DEFAULT_NETWORK: str | None = None
    HELEKET_INVOICE_LIFETIME: int = 3600
    HELEKET_MARKUP_PERCENT: float = 0.0
    HELEKET_WEBHOOK_PATH: str = '/heleket-webhook'
    HELEKET_WEBHOOK_HOST: str = '0.0.0.0'
    HELEKET_WEBHOOK_PORT: int = 8086
    HELEKET_CALLBACK_URL: str | None = None
    HELEKET_RETURN_URL: str | None = None
    HELEKET_SUCCESS_URL: str | None = None

    MULENPAY_ENABLED: bool = False
    MULENPAY_API_KEY: str | None = None
    MULENPAY_SECRET_KEY: str | None = None
    MULENPAY_SHOP_ID: int | None = None
    MULENPAY_BASE_URL: str = 'https://mulenpay.ru/api'
    MULENPAY_WEBHOOK_PATH: str = '/mulenpay-webhook'
    MULENPAY_DISPLAY_NAME: str = 'Mulen Pay'
    MULENPAY_DESCRIPTION: str = 'Пополнение баланса'
    MULENPAY_LANGUAGE: str = 'ru'
    MULENPAY_VAT_CODE: int = 0

    DISPLAY_NAME_BANNED_KEYWORDS: str = '\n'.join(DEFAULT_DISPLAY_NAME_BANNED_KEYWORDS)
    MULENPAY_PAYMENT_SUBJECT: int = 4
    MULENPAY_PAYMENT_MODE: int = 4
    MULENPAY_MIN_AMOUNT_KOPEKS: int = 10000
    MULENPAY_MAX_AMOUNT_KOPEKS: int = 10000000
    MULENPAY_IFRAME_EXPECTED_ORIGIN: str | None = None

    PAL24_ENABLED: bool = False
    PAL24_DISPLAY_NAME: str = 'PAL24'
    PAL24_API_TOKEN: str | None = None
    PAL24_SHOP_ID: str | None = None
    PAL24_SIGNATURE_TOKEN: str | None = None
    PAL24_BASE_URL: str = 'https://pal24.pro/api/v1/'
    PAL24_WEBHOOK_PATH: str = '/pal24-webhook'
    PAL24_WEBHOOK_PORT: int = 8084
    PAL24_PAYMENT_DESCRIPTION: str = 'Пополнение баланса'
    PAL24_MIN_AMOUNT_KOPEKS: int = 10000
    PAL24_MAX_AMOUNT_KOPEKS: int = 100000000
    PAL24_REQUEST_TIMEOUT: int = 30
    PAL24_SBP_BUTTON_TEXT: str | None = None
    PAL24_CARD_BUTTON_TEXT: str | None = None
    PAL24_SBP_BUTTON_VISIBLE: bool = True
    PAL24_CARD_BUTTON_VISIBLE: bool = True

    PLATEGA_ENABLED: bool = False
    PLATEGA_MERCHANT_ID: str | None = None
    PLATEGA_SECRET: str | None = None
    PLATEGA_DISPLAY_NAME: str = 'Platega'
    PLATEGA_BASE_URL: str = 'https://app.platega.io'
    PLATEGA_RETURN_URL: str | None = None
    PLATEGA_FAILED_URL: str | None = None
    PLATEGA_CURRENCY: str = 'RUB'
    PLATEGA_ACTIVE_METHODS: str = '2,10,11,12,13'
    PLATEGA_MIN_AMOUNT_KOPEKS: int = 10000
    PLATEGA_MAX_AMOUNT_KOPEKS: int = 100000000
    PLATEGA_WEBHOOK_PATH: str = '/platega-webhook'
    PLATEGA_WEBHOOK_HOST: str = '0.0.0.0'
    PLATEGA_WEBHOOK_PORT: int = 8086

    WATA_ENABLED: bool = False
    WATA_DISPLAY_NAME: str = 'Wata'
    WATA_BASE_URL: str = 'https://api.wata.pro/api/h2h'
    WATA_ACCESS_TOKEN: str | None = None
    WATA_TERMINAL_PUBLIC_ID: str | None = None
    WATA_PAYMENT_DESCRIPTION: str = 'Пополнение баланса'
    WATA_PAYMENT_TYPE: str = 'OneTime'
    WATA_SUCCESS_REDIRECT_URL: str | None = None
    WATA_FAIL_REDIRECT_URL: str | None = None
    WATA_LINK_TTL_MINUTES: int | None = None
    WATA_MIN_AMOUNT_KOPEKS: int = 10000
    WATA_MAX_AMOUNT_KOPEKS: int = 100000000
    WATA_REQUEST_TIMEOUT: int = 30
    WATA_WEBHOOK_PATH: str = '/wata-webhook'
    WATA_WEBHOOK_HOST: str = '0.0.0.0'
    WATA_WEBHOOK_PORT: int = 8085
    WATA_PUBLIC_KEY_URL: str | None = None
    WATA_PUBLIC_KEY_CACHE_SECONDS: int = 3600

    # CloudPayments
    CLOUDPAYMENTS_ENABLED: bool = False
    CLOUDPAYMENTS_DISPLAY_NAME: str = 'CloudPayments'
    CLOUDPAYMENTS_PUBLIC_ID: str | None = None
    CLOUDPAYMENTS_API_SECRET: str | None = None
    CLOUDPAYMENTS_API_URL: str = 'https://api.cloudpayments.ru'
    CLOUDPAYMENTS_WIDGET_URL: str = 'https://widget.cloudpayments.ru/show'
    CLOUDPAYMENTS_DESCRIPTION: str = 'Пополнение баланса'
    CLOUDPAYMENTS_CURRENCY: str = 'RUB'
    CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS: int = 5000
    CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS: int = 10000000
    CLOUDPAYMENTS_WEBHOOK_PATH: str = '/cloudpayments-webhook'
    CLOUDPAYMENTS_WEBHOOK_HOST: str = '0.0.0.0'
    CLOUDPAYMENTS_WEBHOOK_PORT: int = 8087
    CLOUDPAYMENTS_RETURN_URL: str | None = None
    CLOUDPAYMENTS_SKIN: str = 'mini'  # mini, classic, modern
    CLOUDPAYMENTS_REQUIRE_EMAIL: bool = False
    CLOUDPAYMENTS_TEST_MODE: bool = False

    # Freekassa
    FREEKASSA_ENABLED: bool = False
    FREEKASSA_SHOP_ID: int | None = None
    FREEKASSA_API_KEY: str | None = None
    FREEKASSA_SECRET_WORD_1: str | None = None  # Для формы оплаты
    FREEKASSA_SECRET_WORD_2: str | None = None  # Для webhook
    FREEKASSA_DISPLAY_NAME: str = 'Freekassa'
    FREEKASSA_CURRENCY: str = 'RUB'
    FREEKASSA_MIN_AMOUNT_KOPEKS: int = 10000  # 100 руб
    FREEKASSA_MAX_AMOUNT_KOPEKS: int = 100000000  # 1 000 000 руб
    FREEKASSA_PAYMENT_TIMEOUT_SECONDS: int = 3600
    FREEKASSA_WEBHOOK_PATH: str = '/freekassa-webhook'
    FREEKASSA_WEBHOOK_HOST: str = '0.0.0.0'
    FREEKASSA_WEBHOOK_PORT: int = 8088
    # Способ оплаты: None = форма выбора, 42 = обычный СБП, 44 = NSPK СБП
    FREEKASSA_PAYMENT_SYSTEM_ID: int | None = None
    # Использовать API для создания заказов (нужно для NSPK СБП)
    FREEKASSA_USE_API: bool = False
    # Публичный IP сервера для Freekassa API (если не задан - определяется автоматически)
    SERVER_PUBLIC_IP: str | None = None

    # KassaAI (api.fk.life) - отдельная платёжка
    KASSA_AI_ENABLED: bool = False
    KASSA_AI_SHOP_ID: int | None = None
    KASSA_AI_API_KEY: str | None = None
    KASSA_AI_SECRET_WORD_2: str | None = None  # Для webhook
    KASSA_AI_DISPLAY_NAME: str = 'KassaAI'
    KASSA_AI_CURRENCY: str = 'RUB'
    KASSA_AI_MIN_AMOUNT_KOPEKS: int = 10000  # 100 руб
    KASSA_AI_MAX_AMOUNT_KOPEKS: int = 100000000  # 1 000 000 руб
    KASSA_AI_WEBHOOK_PATH: str = '/kassa-ai-webhook'
    KASSA_AI_WEBHOOK_HOST: str = '0.0.0.0'
    KASSA_AI_WEBHOOK_PORT: int = 8089
    # Способ оплаты: 44 = СБП (QR код), 36 = Карты РФ, 43 = SberPay
    KASSA_AI_PAYMENT_SYSTEM_ID: int = 44

    MAIN_MENU_MODE: str = 'default'
    CONNECT_BUTTON_MODE: str = 'guide'
    MINIAPP_CUSTOM_URL: str = ''
    MINIAPP_STATIC_PATH: str = 'miniapp'
    MINIAPP_PURCHASE_URL: str = ''
    MINIAPP_SERVICE_NAME_EN: str = 'Bedolaga VPN'
    MINIAPP_SERVICE_NAME_RU: str = 'Bedolaga VPN'
    MINIAPP_SERVICE_DESCRIPTION_EN: str = 'Secure & Fast Connection'
    MINIAPP_SERVICE_DESCRIPTION_RU: str = 'Безопасное и быстрое подключение'
    CONNECT_BUTTON_HAPP_DOWNLOAD_ENABLED: bool = False
    HAPP_CRYPTOLINK_REDIRECT_TEMPLATE: str | None = None
    HAPP_DOWNLOAD_LINK_IOS: str | None = None
    HAPP_DOWNLOAD_LINK_ANDROID: str | None = None
    HAPP_DOWNLOAD_LINK_MACOS: str | None = None
    HAPP_DOWNLOAD_LINK_WINDOWS: str | None = None
    HAPP_DOWNLOAD_LINK_PC: str | None = None
    HIDE_SUBSCRIPTION_LINK: bool = False
    ENABLE_LOGO_MODE: bool = True
    LOGO_FILE: str = 'vpn_logo.png'
    SKIP_RULES_ACCEPT: bool = False
    SKIP_REFERRAL_CODE: bool = False

    DEFAULT_LANGUAGE: str = 'ru'
    AVAILABLE_LANGUAGES: str = 'ru,en'
    LANGUAGE_SELECTION_ENABLED: bool = True

    # Округление цен при отображении (≤50 коп вниз, >50 коп вверх)
    PRICE_ROUNDING_ENABLED: bool = True

    LOG_LEVEL: str = 'INFO'
    LOG_FILE: str = 'logs/bot.log'

    # === Log Rotation Settings ===
    LOG_ROTATION_ENABLED: bool = False  # По умолчанию старое поведение
    LOG_ROTATION_TIME: str = '00:00'  # Время ротации (HH:MM)
    LOG_ROTATION_KEEP_DAYS: int = 7  # Хранить архивы N дней
    LOG_ROTATION_COMPRESS: bool = True  # Сжимать архивы gzip
    LOG_ROTATION_SEND_TO_TELEGRAM: bool = False  # Отправлять в канал
    LOG_ROTATION_CHAT_ID: str | None = None  # Канал для логов (или BACKUP_SEND_CHAT_ID)
    LOG_ROTATION_TOPIC_ID: int | None = None  # Топик в канале

    # Пути к лог-файлам (при LOG_ROTATION_ENABLED=true)
    LOG_DIR: str = 'logs'
    LOG_INFO_FILE: str = 'info.log'
    LOG_WARNING_FILE: str = 'warning.log'
    LOG_ERROR_FILE: str = 'error.log'
    LOG_PAYMENTS_FILE: str = 'payments.log'

    # === Ban Notification Messages ===

    # Сообщение о блокировке за превышение лимита устройств
    # Переменные: {ip_count}, {limit}, {ban_minutes}, {node_info}
    BAN_MSG_PUNISHMENT: str = (
        '🚫 <b>АККАУНТ ЗАБЛОКИРОВАН</b>\n'
        '━━━━━━━━━━━━━━━━━━━━━\n\n'
        '❌ <b>Причина:</b> Превышен лимит устройств\n'
        '{node_info}\n'
        '📊 <b>Детали нарушения:</b>\n'
        '├ 📱 Устройств подключено: <b>{ip_count}</b>\n'
        '├ 📋 Разрешено по тарифу: <b>{limit}</b>\n'
        '└ ⏱ Время блокировки: <b>{ban_minutes} мин</b>\n\n'
        '━━━━━━━━━━━━━━━━━━━━━\n'
        '💡 <b>Что делать:</b>\n'
        '1. Отключите лишние устройства от VPN\n'
        '2. Дождитесь окончания блокировки\n'
        '3. Подключитесь заново\n\n'
        '🔄 Доступ восстановится автоматически'
    )

    # Сообщение о разблокировке
    BAN_MSG_ENABLED: str = (
        '✅ <b>АККАУНТ РАЗБЛОКИРОВАН</b>\n'
        '━━━━━━━━━━━━━━━━━━━━━\n\n'
        '🎉 Ваш аккаунт успешно разблокирован!\n\n'
        'Теперь вы можете снова пользоваться VPN.\n\n'
        '━━━━━━━━━━━━━━━━━━━━━\n'
        '⚠️ <b>Рекомендации:</b>\n'
        '• Следите за количеством устройств\n'
        '• Отключайте VPN когда не используете\n'
        '• Не превышайте лимит по тарифу'
    )

    # Сообщение о блокировке за WiFi
    # Переменные: {ban_minutes}, {network_info}, {node_info}
    BAN_MSG_WIFI: str = (
        '🚫 <b>АККАУНТ ЗАБЛОКИРОВАН</b>\n'
        '━━━━━━━━━━━━━━━━━━━━━\n\n'
        '❌ <b>Причина:</b> Использование WiFi сети\n'
        '{node_info}\n'
        '📊 <b>Детали:</b>\n'
        '├ 📶 Тип подключения: <b>WiFi</b>\n'
        '{network_info}'
        '└ ⏱ Время блокировки: <b>{ban_minutes} мин</b>\n\n'
        '━━━━━━━━━━━━━━━━━━━━━\n'
        '💡 <b>Что делать:</b>\n'
        '1. Отключитесь от WiFi\n'
        '2. Используйте мобильный интернет\n'
        '3. Дождитесь окончания блокировки\n\n'
        '🔄 Доступ восстановится автоматически'
    )

    # Сообщение о блокировке за мобильную сеть
    # Переменные: {ban_minutes}, {network_info}, {node_info}
    BAN_MSG_MOBILE: str = (
        '🚫 <b>АККАУНТ ЗАБЛОКИРОВАН</b>\n'
        '━━━━━━━━━━━━━━━━━━━━━\n\n'
        '❌ <b>Причина:</b> Использование мобильной сети\n'
        '{node_info}\n'
        '📊 <b>Детали:</b>\n'
        '├ 📱 Тип подключения: <b>Мобильная сеть</b>\n'
        '{network_info}'
        '└ ⏱ Время блокировки: <b>{ban_minutes} мин</b>\n\n'
        '━━━━━━━━━━━━━━━━━━━━━\n'
        '💡 <b>Что делать:</b>\n'
        '1. Подключитесь к WiFi\n'
        '2. Дождитесь окончания блокировки\n'
        '3. Используйте VPN только через WiFi\n\n'
        '🔄 Доступ восстановится автоматически'
    )

    # Сообщение-предупреждение
    # Переменные: {warning_message}
    BAN_MSG_WARNING: str = (
        '⚠️ <b>ПРЕДУПРЕЖДЕНИЕ</b>\n'
        '━━━━━━━━━━━━━━━━━━━━━\n\n'
        '{warning_message}\n\n'
        '━━━━━━━━━━━━━━━━━━━━━\n'
        '❗ При повторном нарушении аккаунт будет заблокирован'
    )

    DEBUG: bool = False
    WEBHOOK_URL: str | None = None
    WEBHOOK_PATH: str = '/webhook'
    WEBHOOK_SECRET_TOKEN: str | None = None
    WEBHOOK_DROP_PENDING_UPDATES: bool = True
    WEBHOOK_MAX_QUEUE_SIZE: int = 1024
    WEBHOOK_WORKERS: int = 4
    WEBHOOK_ENQUEUE_TIMEOUT: float = 0.1
    WEBHOOK_WORKER_SHUTDOWN_TIMEOUT: float = 30.0
    BOT_RUN_MODE: str = 'polling'

    WEB_API_ENABLED: bool = False
    WEB_API_HOST: str = '0.0.0.0'
    WEB_API_PORT: int = 8080
    WEB_API_WORKERS: int = 1
    WEB_API_ALLOWED_ORIGINS: str = '*'
    WEB_API_DOCS_ENABLED: bool = False
    WEB_API_TITLE: str = 'Remnawave Bot Admin API'
    WEB_API_VERSION: str = '1.0.0'
    WEB_API_DEFAULT_TOKEN: str | None = None
    WEB_API_DEFAULT_TOKEN_NAME: str = 'Bootstrap Token'
    WEB_API_TOKEN_HASH_ALGORITHM: str = 'sha256'
    WEB_API_REQUEST_LOGGING: bool = True

    APP_CONFIG_PATH: str = 'app-config.json'
    ENABLE_DEEP_LINKS: bool = True
    APP_CONFIG_CACHE_TTL: int = 3600

    VERSION_CHECK_ENABLED: bool = True
    VERSION_CHECK_REPO: str = 'fr1ngg/remnawave-bedolaga-telegram-bot'
    VERSION_CHECK_INTERVAL_HOURS: int = 1

    BACKUP_AUTO_ENABLED: bool = True
    BACKUP_INTERVAL_HOURS: int = 24
    BACKUP_TIME: str = '03:00'
    BACKUP_MAX_KEEP: int = 7
    BACKUP_COMPRESSION: bool = True
    BACKUP_INCLUDE_LOGS: bool = False
    BACKUP_LOCATION: str = '/app/data/backups'
    BACKUP_SEND_ENABLED: bool = False
    BACKUP_SEND_CHAT_ID: str | None = None
    BACKUP_SEND_TOPIC_ID: int | None = None
    BACKUP_ARCHIVE_PASSWORD: str | None = None

    EXTERNAL_ADMIN_TOKEN: str | None = None
    EXTERNAL_ADMIN_TOKEN_BOT_ID: int | None = None

    # Cabinet (Personal Account) settings
    CABINET_ENABLED: bool = False
    CABINET_JWT_SECRET: str | None = None
    CABINET_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    CABINET_REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    CABINET_ALLOWED_ORIGINS: str = ''
    CABINET_EMAIL_VERIFICATION_ENABLED: bool = True
    CABINET_EMAIL_VERIFICATION_EXPIRE_HOURS: int = 24
    CABINET_PASSWORD_RESET_EXPIRE_HOURS: int = 1
    CABINET_EMAIL_CHANGE_CODE_EXPIRE_MINUTES: int = 15  # Email change verification code expiration
    CABINET_EMAIL_AUTH_ENABLED: bool = True  # Enable email registration/login in cabinet
    CABINET_URL: str = 'https://example.com/cabinet'  # Base URL for cabinet (used in verification emails)

    # OAuth 2.0 provider settings for cabinet
    OAUTH_GOOGLE_CLIENT_ID: str = ''
    OAUTH_GOOGLE_CLIENT_SECRET: str = ''
    OAUTH_GOOGLE_ENABLED: bool = False

    OAUTH_YANDEX_CLIENT_ID: str = ''
    OAUTH_YANDEX_CLIENT_SECRET: str = ''
    OAUTH_YANDEX_ENABLED: bool = False

    OAUTH_DISCORD_CLIENT_ID: str = ''
    OAUTH_DISCORD_CLIENT_SECRET: str = ''
    OAUTH_DISCORD_ENABLED: bool = False

    OAUTH_VK_CLIENT_ID: str = ''
    OAUTH_VK_CLIENT_SECRET: str = ''
    OAUTH_VK_ENABLED: bool = False

    # SMTP settings for cabinet email
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str | None = None
    SMTP_FROM_NAME: str = 'VPN Service'
    SMTP_USE_TLS: bool = True

    # Ban System Integration (BedolagaBan monitoring)
    BAN_SYSTEM_ENABLED: bool = False
    BAN_SYSTEM_API_URL: str | None = None  # e.g., http://ban-server:8000
    BAN_SYSTEM_API_TOKEN: str | None = None
    BAN_SYSTEM_REQUEST_TIMEOUT: int = 30

    @field_validator('MAIN_MENU_MODE', mode='before')
    @classmethod
    def normalize_main_menu_mode(cls, value: str | None) -> str:
        if not value:
            return 'default'

        normalized = str(value).strip().lower()
        aliases = {
            'classic': 'default',
            'default': 'default',
            'full': 'default',
            'standard': 'default',
            'text': 'text',
            'text_only': 'text',
            'textual': 'text',
            'minimal': 'text',
        }

        mode = aliases.get(normalized, normalized)
        if mode not in {'default', 'text'}:
            raise ValueError('MAIN_MENU_MODE must be one of: default, text')
        return mode

    @field_validator('SERVER_STATUS_MODE', mode='before')
    @classmethod
    def normalize_server_status_mode(cls, value: str | None) -> str:
        if not value:
            return 'disabled'

        normalized = str(value).strip().lower()
        aliases = {
            'off': 'disabled',
            'none': 'disabled',
            'disabled': 'disabled',
            'external': 'external_link',
            'link': 'external_link',
            'url': 'external_link',
            'external_link': 'external_link',
            'miniapp': 'external_link_miniapp',
            'mini_app': 'external_link_miniapp',
            'mini-app': 'external_link_miniapp',
            'webapp': 'external_link_miniapp',
            'web_app': 'external_link_miniapp',
            'web-app': 'external_link_miniapp',
            'external_link_miniapp': 'external_link_miniapp',
            'xray': 'xray',
            'xraychecker': 'xray',
            'xray_metrics': 'xray',
            'metrics': 'xray',
        }

        mode = aliases.get(normalized, normalized)
        if mode not in {'disabled', 'external_link', 'external_link_miniapp', 'xray'}:
            raise ValueError('SERVER_STATUS_MODE must be one of: disabled, external_link, external_link_miniapp, xray')
        return mode

    @field_validator('SERVER_STATUS_ITEMS_PER_PAGE', mode='before')
    @classmethod
    def ensure_positive_server_status_page_size(cls, value: int | None) -> int:
        try:
            if value is None:
                return 10
            value_int = int(value)
            return max(1, value_int)
        except (TypeError, ValueError):
            return 10

    @field_validator('SERVER_STATUS_REQUEST_TIMEOUT', mode='before')
    @classmethod
    def ensure_positive_server_status_timeout(cls, value: int | None) -> int:
        try:
            if value is None:
                return 10
            value_int = int(value)
            return max(1, value_int)
        except (TypeError, ValueError):
            return 10

    @field_validator('LOG_FILE', mode='before')
    @classmethod
    def ensure_log_dir(cls, v):
        log_path = Path(v)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return str(log_path)

    def get_database_url(self) -> str:
        if self.DATABASE_URL and self.DATABASE_URL.strip():
            return self.DATABASE_URL

        mode = self.DATABASE_MODE.lower()

        if mode == 'sqlite':
            return self._get_sqlite_url()
        if mode == 'postgresql':
            return self._get_postgresql_url()
        if mode == 'auto':
            if os.getenv('DOCKER_ENV') == 'true' or os.path.exists('/.dockerenv'):
                return self._get_postgresql_url()
            return self._get_sqlite_url()
        return self._get_auto_database_url()

    def _get_sqlite_url(self) -> str:
        sqlite_path = Path(self.SQLITE_PATH)
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return f'sqlite+aiosqlite:///{sqlite_path.absolute()}'

    def _get_postgresql_url(self) -> str:
        return (
            f'postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}'
            f'@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}'
        )

    def _get_auto_database_url(self) -> str:
        if os.getenv('DOCKER_ENV') == 'true' or os.path.exists('/.dockerenv'):
            return self._get_postgresql_url()
        return self._get_sqlite_url()

    def is_postgresql(self) -> bool:
        """Проверяет, используется ли PostgreSQL"""
        return 'postgresql' in self.get_database_url()

    def is_sqlite(self) -> bool:
        """Проверяет, используется ли SQLite"""
        return 'sqlite' in self.get_database_url()

    def is_admin(self, telegram_id: int | None = None, email: str | None = None) -> bool:
        """
        Check if user is admin by telegram_id or email.

        Args:
            telegram_id: Telegram user ID
            email: User email address

        Returns:
            True if user is admin
        """
        if telegram_id and telegram_id in self.get_admin_ids():
            return True
        if email and email.lower() in [e.lower() for e in self.get_admin_emails()]:
            return True
        return False

    def get_admin_ids(self) -> list[int]:
        try:
            admin_ids = self.ADMIN_IDS

            if isinstance(admin_ids, str):
                if not admin_ids.strip():
                    return []
                return [int(x.strip()) for x in admin_ids.split(',') if x.strip()]

            return []

        except (ValueError, AttributeError):
            return []

    def get_admin_emails(self) -> list[str]:
        """Get list of admin emails for email-only users."""
        try:
            admin_emails = self.ADMIN_EMAILS

            if isinstance(admin_emails, str):
                if not admin_emails.strip():
                    return []
                return [e.strip().lower() for e in admin_emails.split(',') if e.strip()]

            return []

        except (ValueError, AttributeError):
            return []

    def get_test_email(self) -> str | None:
        """Get test email for development/testing."""
        email = (self.TEST_EMAIL or '').strip().lower()
        return email if email else None

    def get_test_email_password(self) -> str | None:
        """Get test email password."""
        password = (self.TEST_EMAIL_PASSWORD or '').strip()
        return password if password else None

    def is_test_email(self, email: str) -> bool:
        """Check if email is the configured test email."""
        test_email = self.get_test_email()
        if not test_email:
            return False
        return email.lower().strip() == test_email

    def validate_test_email_password(self, email: str, password: str) -> bool:
        """Validate test email credentials."""
        if not self.is_test_email(email):
            return False
        test_password = self.get_test_email_password()
        if not test_password:
            return False
        return password == test_password

    def get_remnawave_auth_params(self) -> dict[str, str | None]:
        return {
            'base_url': self.REMNAWAVE_API_URL,
            'api_key': self.REMNAWAVE_API_KEY,
            'secret_key': self.REMNAWAVE_SECRET_KEY,
            'username': self.REMNAWAVE_USERNAME,
            'password': self.REMNAWAVE_PASSWORD,
            'caddy_token': self.REMNAWAVE_CADDY_TOKEN,
            'auth_type': self.REMNAWAVE_AUTH_TYPE,
        }

    def get_pal24_sbp_button_text(self, fallback: str) -> str:
        value = (self.PAL24_SBP_BUTTON_TEXT or '').strip()
        return value or fallback

    def get_pal24_card_button_text(self, fallback: str) -> str:
        value = (self.PAL24_CARD_BUTTON_TEXT or '').strip()
        return value or fallback

    def is_pal24_sbp_button_visible(self) -> bool:
        return self.PAL24_SBP_BUTTON_VISIBLE

    def is_pal24_card_button_visible(self) -> bool:
        return self.PAL24_CARD_BUTTON_VISIBLE

    def get_remnawave_user_delete_mode(self) -> str:
        """Возвращает режим удаления пользователей: 'delete' или 'disable'"""
        mode = self.REMNAWAVE_USER_DELETE_MODE.lower().strip()
        return mode if mode in ['delete', 'disable'] else 'delete'

    def format_remnawave_user_description(
        self,
        *,
        full_name: str,
        username: str | None,
        telegram_id: int | None,
        email: str | None = None,
        user_id: int | None = None,
    ) -> str:
        """
        Форматирует описание пользователя для RemnaWave.

        Поддерживает как Telegram-пользователей, так и email-only пользователей.
        """
        template = self.REMNAWAVE_USER_DESCRIPTION_TEMPLATE or 'Bot user: {full_name} {username}'
        template_for_formatting = template.replace('@{username}', '{username}')

        username_clean = (username or '').lstrip('@')

        # Формируем идентификатор для description
        identifier_parts = []
        if telegram_id:
            identifier_parts.append(f'TG: {telegram_id}')
        if email:
            identifier_parts.append(f'Email: {email}')
        if user_id and not identifier_parts:
            identifier_parts.append(f'ID: {user_id}')

        values = defaultdict(
            str,
            {
                'full_name': full_name,
                'username': f'@{username_clean}' if username_clean else '',
                'username_clean': username_clean,
                'telegram_id': str(telegram_id) if telegram_id else '',
                'email': email or '',
                'user_id': str(user_id) if user_id else '',
                'identifier': ' | '.join(identifier_parts),
            },
        )

        description = template_for_formatting.format_map(values)

        if not username_clean:
            description = re.sub(r'@(?=\W|$)', '', description)
            description = re.sub(r'\(\s*\)', '', description)

        description = re.sub(r'\s+', ' ', description).strip()
        return description

    def format_remnawave_username(
        self,
        *,
        full_name: str,
        username: str | None,
        telegram_id: int | None,
        email: str | None = None,
        user_id: int | None = None,
    ) -> str:
        """
        Форматирует username для RemnaWave.

        Для email-пользователей (telegram_id=None) использует email prefix + user_id.
        """
        template = self.REMNAWAVE_USER_USERNAME_TEMPLATE or 'user_{telegram_id}'

        username_clean = (username or '').lstrip('@')
        full_name_value = full_name or ''

        # Для email-пользователей формируем уникальный identifier
        if telegram_id:
            identifier = str(telegram_id)
        elif email:
            email_prefix = email.split('@')[0][:10]
            identifier = f'email_{email_prefix}_{user_id}' if user_id else f'email_{email_prefix}'
        elif user_id:
            identifier = f'id_{user_id}'
        else:
            identifier = 'unknown'

        values = defaultdict(
            str,
            {
                'full_name': full_name_value,
                'username': username_clean,
                'username_clean': username_clean,
                'telegram_id': str(telegram_id) if telegram_id else identifier,
                'identifier': identifier,
                'email': email.split('@')[0] if email else '',
                'user_id': str(user_id) if user_id else '',
            },
        )

        raw_username = template.format_map(values).strip()
        # Remnawave разрешает только буквы, цифры, подчёркивания и дефисы
        sanitized_username = re.sub(r'[^0-9A-Za-z_-]+', '_', raw_username)
        sanitized_username = re.sub(r'_+', '_', sanitized_username).strip('_-')

        if not sanitized_username:
            sanitized_username = f'user_{identifier}'

        return sanitized_username[:64]

    @staticmethod
    def parse_daily_time_list(raw_value: str | None) -> list[time]:
        if not raw_value:
            return []

        segments = re.split(r'[\s,;]+', raw_value.strip())
        seen: set[tuple[int, int]] = set()
        parsed: list[time] = []

        for segment in segments:
            if not segment:
                continue

            try:
                hours_str, minutes_str = segment.split(':', 1)
                hours = int(hours_str)
                minutes = int(minutes_str)
            except (ValueError, AttributeError):
                continue

            if not (0 <= hours < 24 and 0 <= minutes < 60):
                continue

            key = (hours, minutes)
            if key in seen:
                continue

            seen.add(key)
            parsed.append(time(hour=hours, minute=minutes))

        parsed.sort()
        return parsed

    def get_remnawave_auto_sync_times(self) -> list[time]:
        return self.parse_daily_time_list(self.REMNAWAVE_AUTO_SYNC_TIMES)

    def get_traffic_monitored_nodes(self) -> list[str]:
        """Возвращает список UUID нод для мониторинга (пусто = все)"""
        if not self.TRAFFIC_MONITORED_NODES:
            return []
        # Убираем комментарии (все после #)
        value = self.TRAFFIC_MONITORED_NODES.split('#')[0].strip()
        if not value:
            return []
        return [n.strip() for n in value.split(',') if n.strip()]

    def get_traffic_ignored_nodes(self) -> list[str]:
        """Возвращает список UUID нод для исключения из мониторинга"""
        if not self.TRAFFIC_IGNORED_NODES:
            return []
        # Убираем комментарии (все после #)
        value = self.TRAFFIC_IGNORED_NODES.split('#')[0].strip()
        if not value:
            return []
        return [n.strip() for n in value.split(',') if n.strip()]

    def get_traffic_excluded_user_uuids(self) -> list[str]:
        """Возвращает список UUID пользователей для исключения из мониторинга (например, тунельные/служебные)"""
        if not self.TRAFFIC_EXCLUDED_USER_UUIDS:
            return []
        # Убираем комментарии (все после #)
        value = self.TRAFFIC_EXCLUDED_USER_UUIDS.split('#')[0].strip()
        if not value:
            return []
        return [uuid.strip().lower() for uuid in value.split(',') if uuid.strip()]

    def get_traffic_daily_check_time(self) -> time | None:
        """Возвращает время суточной проверки трафика"""
        times = self.parse_daily_time_list(self.TRAFFIC_DAILY_CHECK_TIME)
        return times[0] if times else None

    def get_display_name_banned_keywords(self) -> list[str]:
        raw_value = self.DISPLAY_NAME_BANNED_KEYWORDS
        if raw_value is None:
            return []

        if isinstance(raw_value, str):
            candidates = re.split(r'[\n,]+', raw_value)
        else:
            candidates = list(raw_value)

        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = str(candidate).strip().lower()
            if not normalized:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)

        return unique

    def get_autopay_warning_days(self) -> list[int]:
        try:
            days = self.AUTOPAY_WARNING_DAYS
            if isinstance(days, str):
                if not days.strip():
                    return [3, 1]
                return [int(x.strip()) for x in days.split(',') if x.strip()]
            return [3, 1]
        except (ValueError, AttributeError):
            return [3, 1]

    def is_autopay_enabled_by_default(self) -> bool:
        value = getattr(self, 'DEFAULT_AUTOPAY_ENABLED', True)

        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized in {'1', 'true', 'yes', 'on'}

        return bool(value)

    def is_auto_purchase_after_topup_enabled(self) -> bool:
        value = getattr(self, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)

        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized in {'1', 'true', 'yes', 'on'}

        return bool(value)

    def is_auto_activate_after_topup_enabled(self) -> bool:
        """Умная автоактивация после пополнения баланса (без корзины)."""
        value = getattr(self, 'AUTO_ACTIVATE_AFTER_TOPUP_ENABLED', False)

        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized in {'1', 'true', 'yes', 'on'}

        return bool(value)

    def is_quick_amount_buttons_enabled(self) -> bool:
        """Показывать ли кнопки быстрого выбора суммы пополнения."""
        return self.YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED and not self.DISABLE_TOPUP_BUTTONS

    def get_available_languages(self) -> list[str]:
        defaults = ['ru', 'en', 'ua', 'zh']

        try:
            langs = self.AVAILABLE_LANGUAGES
        except AttributeError:
            return defaults

        candidates: list[str]

        if isinstance(langs, str):
            if not langs.strip():
                return defaults
            candidates = [chunk.strip() for chunk in langs.split(',')]
        elif isinstance(langs, (list, tuple, set)):
            candidates = [str(item).strip() for item in langs]
        else:
            return defaults

        cleaned: list[str] = []
        seen: set[str] = set()

        for code in candidates:
            if not code:
                continue

            normalized = code.lower()

            if normalized in seen:
                continue

            seen.add(normalized)
            cleaned.append(code)

        return cleaned or defaults

    def is_language_selection_enabled(self) -> bool:
        return bool(getattr(self, 'LANGUAGE_SELECTION_ENABLED', True))

    def format_price(self, price_kopeks: int, round_kopeks: bool | None = None) -> str:
        """
        Форматирует цену в копейках для отображения пользователю.

        Args:
            price_kopeks: Сумма в копейках
            round_kopeks: Если True, округляет копейки (≤50 вниз, >50 вверх).
                         Если None, использует настройку PRICE_ROUNDING_ENABLED.

        Returns:
            Отформатированная строка цены (например, "150 ₽")
        """
        # Используем настройку если не передано явно
        should_round = round_kopeks if round_kopeks is not None else self.PRICE_ROUNDING_ENABLED

        sign = '-' if price_kopeks < 0 else ''
        abs_kopeks = abs(price_kopeks)
        rubles, kopeks = divmod(abs_kopeks, 100)

        if should_round:
            # Округление: ≤50 коп вниз, >50 коп вверх
            if kopeks > 50:
                rubles += 1
            return f'{sign}{rubles} ₽'

        # Без округления - показываем точное значение
        if kopeks:
            value = f'{sign}{rubles}.{kopeks:02d}'.rstrip('0').rstrip('.')
            return f'{value} ₽'

        return f'{sign}{rubles} ₽'

    def get_reports_chat_id(self) -> str | None:
        if self.ADMIN_REPORTS_CHAT_ID:
            return self.ADMIN_REPORTS_CHAT_ID
        return self.ADMIN_NOTIFICATIONS_CHAT_ID

    def get_reports_topic_id(self) -> int | None:
        return self.ADMIN_REPORTS_TOPIC_ID or None

    def get_reports_send_time(self) -> time | None:
        value = self.ADMIN_REPORTS_SEND_TIME
        if not value:
            return None

        try:
            hours_str, minutes_str = value.strip().split(':', 1)
            hours = int(hours_str)
            minutes = int(minutes_str)
            if not (0 <= hours <= 23 and 0 <= minutes <= 59):
                raise ValueError
            return time(hour=hours, minute=minutes)
        except (ValueError, AttributeError):
            logging.getLogger(__name__).warning('Некорректное значение ADMIN_REPORTS_SEND_TIME: %s', value)
            return None

    def kopeks_to_rubles(self, kopeks: int) -> float:
        return kopeks / 100

    def rubles_to_kopeks(self, rubles: float) -> int:
        return int(rubles * 100)

    @staticmethod
    def _normalize_user_tag(value: str | None, setting_name: str) -> str | None:
        if value is None:
            return None

        cleaned = str(value).strip().upper()
        if not cleaned:
            return None

        if len(cleaned) > 16:
            logger.warning(
                'Некорректная длина %s: максимум 16 символов, получено %s',
                setting_name,
                len(cleaned),
            )
            return None

        if not USER_TAG_PATTERN.fullmatch(cleaned):
            logger.warning(
                'Некорректный формат %s: допустимы только A-Z, 0-9 и подчёркивание',
                setting_name,
            )
            return None

        return cleaned

    def get_trial_warning_hours(self) -> int:
        return self.TRIAL_WARNING_HOURS

    def get_trial_user_tag(self) -> str | None:
        return self._normalize_user_tag(self.TRIAL_USER_TAG, 'TRIAL_USER_TAG')

    def is_trial_disabled_for_user(self, auth_type: str | None) -> bool:
        disabled_for = self.TRIAL_DISABLED_FOR
        if disabled_for == 'all':
            return True
        # 'email' means all non-Telegram users (email, google, yandex, discord, vk, etc.)
        if disabled_for == 'email' and auth_type not in (None, 'telegram'):
            return True
        if disabled_for == 'telegram' and (auth_type is None or auth_type == 'telegram'):
            return True
        return False

    def get_paid_subscription_user_tag(self) -> str | None:
        return self._normalize_user_tag(
            self.PAID_SUBSCRIPTION_USER_TAG,
            'PAID_SUBSCRIPTION_USER_TAG',
        )

    def get_bot_username(self) -> str | None:
        username = getattr(self, 'BOT_USERNAME', None)
        if not username:
            return None
        normalized = str(username).strip().lstrip('@')
        return normalized or None

    def is_notifications_enabled(self) -> bool:
        return self.ENABLE_NOTIFICATIONS

    def get_main_menu_mode(self) -> str:
        return getattr(self, 'MAIN_MENU_MODE', 'default')

    def is_text_main_menu_mode(self) -> bool:
        return self.get_main_menu_mode() == 'text'

    def get_main_menu_miniapp_url(self) -> str | None:
        for candidate in [self.MINIAPP_CUSTOM_URL, self.MINIAPP_PURCHASE_URL]:
            value = (candidate or '').strip()
            if value:
                return value
        return None

    def get_app_config_path(self) -> str:
        if os.path.isabs(self.APP_CONFIG_PATH):
            return self.APP_CONFIG_PATH

        project_root = Path(__file__).parent.parent
        return str(project_root / self.APP_CONFIG_PATH)

    def is_deep_links_enabled(self) -> bool:
        return self.ENABLE_DEEP_LINKS

    def get_miniapp_branding(self) -> dict[str, dict[str, str | None]]:
        def _clean(value: str | None) -> str | None:
            if value is None:
                return None
            value_str = str(value).strip()
            return value_str or None

        name_en = _clean(self.MINIAPP_SERVICE_NAME_EN)
        name_ru = _clean(self.MINIAPP_SERVICE_NAME_RU)
        desc_en = _clean(self.MINIAPP_SERVICE_DESCRIPTION_EN)
        desc_ru = _clean(self.MINIAPP_SERVICE_DESCRIPTION_RU)

        default_name = name_en or name_ru or 'RemnaWave VPN'
        default_description = desc_en or desc_ru or 'Secure & Fast Connection'

        return {
            'service_name': {
                'default': default_name,
                'en': name_en,
                'ru': name_ru,
            },
            'service_description': {
                'default': default_description,
                'en': desc_en,
                'ru': desc_ru,
            },
        }

    def get_app_config_cache_ttl(self) -> int:
        return self.APP_CONFIG_CACHE_TTL

    def build_external_admin_token(self, bot_username: str) -> str:
        """Генерирует детерминированный и криптографически стойкий токен внешней админки."""
        normalized = (bot_username or '').strip().lstrip('@').lower()
        if not normalized:
            raise ValueError('Bot username is required to build external admin token')

        secret = (self.BOT_TOKEN or '').strip()
        if not secret:
            raise ValueError('Bot token is required to build external admin token')

        digest = hmac.new(
            key=secret.encode('utf-8'),
            msg=f'remnawave.external_admin::{normalized}'.encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()
        return digest[:48]

    def get_external_admin_token(self) -> str | None:
        token = (self.EXTERNAL_ADMIN_TOKEN or '').strip()
        return token or None

    def get_external_admin_bot_id(self) -> int | None:
        try:
            return int(self.EXTERNAL_ADMIN_TOKEN_BOT_ID) if self.EXTERNAL_ADMIN_TOKEN_BOT_ID else None
        except (TypeError, ValueError):  # pragma: no cover - защитная ветка для некорректных значений
            logging.getLogger(__name__).warning(
                'Некорректный идентификатор бота для внешней админки: %s',
                self.EXTERNAL_ADMIN_TOKEN_BOT_ID,
            )
            return None

    def is_traffic_selectable(self) -> bool:
        return self.TRAFFIC_SELECTION_MODE.lower() == 'selectable'

    def is_traffic_fixed(self) -> bool:
        """Возвращает True если выбор трафика отключён (fixed или fixed_with_topup)"""
        return self.TRAFFIC_SELECTION_MODE.lower() in ('fixed', 'fixed_with_topup')

    def is_traffic_topup_blocked(self) -> bool:
        """Возвращает True если докупка трафика полностью заблокирована (только fixed)"""
        return self.TRAFFIC_SELECTION_MODE.lower() == 'fixed'

    def get_fixed_traffic_limit(self) -> int:
        return self.FIXED_TRAFFIC_LIMIT_GB

    def is_traffic_topup_enabled(self) -> bool:
        return self.TRAFFIC_TOPUP_ENABLED

    def get_traffic_topup_packages(self) -> list[dict]:
        """Возвращает пакеты для докупки трафика. Если не настроены - использует TRAFFIC_PACKAGES_CONFIG."""
        config_str = self.TRAFFIC_TOPUP_PACKAGES_CONFIG.strip()

        if not config_str:
            # Если не настроены отдельные пакеты для докупки - используем основные
            return self.get_traffic_packages()

        packages = []
        for package_config in config_str.split(','):
            package_config = package_config.strip()
            if not package_config:
                continue

            parts = package_config.split(':')
            if len(parts) >= 2:
                try:
                    gb = int(parts[0])
                    price = int(parts[1])
                    enabled = parts[2].lower() == 'true' if len(parts) > 2 else True
                    packages.append({'gb': gb, 'price': price, 'enabled': enabled})
                except (ValueError, IndexError):
                    continue

        return packages if packages else self.get_traffic_packages()

    def get_traffic_topup_price(self, gb: int | None) -> int:
        """Возвращает цену докупки для указанного количества ГБ."""
        packages = self.get_traffic_topup_packages()
        enabled_packages = [pkg for pkg in packages if pkg['enabled']]

        if not enabled_packages:
            return 0

        # Ищем точное совпадение
        for pkg in enabled_packages:
            if pkg['gb'] == gb:
                return pkg['price']

        # Если не нашли - возвращаем 0
        return 0

    def get_traffic_reset_price_mode(self) -> str:
        return self.TRAFFIC_RESET_PRICE_MODE.lower()

    def get_traffic_reset_base_price(self) -> int:
        return self.TRAFFIC_RESET_BASE_PRICE

    def is_devices_selection_enabled(self) -> bool:
        return self.DEVICES_SELECTION_ENABLED

    def get_devices_selection_disabled_amount(self) -> int | None:
        raw_value = self.DEVICES_SELECTION_DISABLED_AMOUNT

        if raw_value in (None, ''):
            return None

        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            logger.warning(
                'Некорректное значение DEVICES_SELECTION_DISABLED_AMOUNT: %s',
                raw_value,
            )
            return None

        if value < 0:
            return 0

        return value

    def get_disabled_mode_device_limit(self) -> int | None:
        return self.get_devices_selection_disabled_amount()

    def is_modem_enabled(self) -> bool:
        return bool(self.MODEM_ENABLED)

    def is_tariffs_mode(self) -> bool:
        """Проверяет, включен ли режим продаж 'Тарифы'."""
        return self.SALES_MODE == 'tariffs'

    def is_classic_mode(self) -> bool:
        """Проверяет, включен ли классический режим продаж."""
        return self.SALES_MODE != 'tariffs'

    def get_sales_mode(self) -> str:
        """Возвращает текущий режим продаж."""
        return self.SALES_MODE if self.SALES_MODE in ('classic', 'tariffs') else 'classic'

    def get_trial_tariff_id(self) -> int:
        """Возвращает ID тарифа для триала (0 = использовать стандартные настройки)."""
        return max(0, self.TRIAL_TARIFF_ID)

    def get_modem_price_per_month(self) -> int:
        try:
            value = int(self.MODEM_PRICE_PER_MONTH)
        except (TypeError, ValueError):
            logger.warning(
                'Некорректное значение MODEM_PRICE_PER_MONTH: %s',
                self.MODEM_PRICE_PER_MONTH,
            )
            return 10000
        return max(0, value)

    def get_modem_period_discounts(self) -> dict[int, int]:
        """Возвращает скидки на модем по количеству месяцев: {месяцев: процент_скидки}"""
        try:
            config_str = (self.MODEM_PERIOD_DISCOUNTS or '').strip()
            if not config_str:
                return {}

            discounts: dict[int, int] = {}
            for part in config_str.split(','):
                part = part.strip()
                if not part:
                    continue

                months_and_discount = part.split(':')
                if len(months_and_discount) != 2:
                    continue

                months_str, discount_str = months_and_discount
                try:
                    months = int(months_str.strip())
                    discount_percent = int(discount_str.strip())
                except ValueError:
                    continue

                discounts[months] = max(0, min(100, discount_percent))

            return discounts
        except Exception:
            return {}

    def get_modem_period_discount(self, months: int) -> int:
        """Возвращает процент скидки для указанного количества месяцев"""
        if months <= 0:
            return 0

        discounts = self.get_modem_period_discounts()

        # Ищем точное совпадение или ближайшее меньшее
        applicable_discount = 0
        for discount_months, discount_percent in sorted(discounts.items()):
            if months >= discount_months:
                applicable_discount = discount_percent

        return applicable_discount

    def is_trial_paid_activation_enabled(self) -> bool:
        # TRIAL_PAYMENT_ENABLED - главный переключатель платной активации
        # Если выключен - триал бесплатный, независимо от цены
        if not self.TRIAL_PAYMENT_ENABLED:
            return False
        # Если включен - проверяем что цена > 0
        return self.TRIAL_ACTIVATION_PRICE > 0

    def get_trial_activation_price(self) -> int:
        try:
            value = int(self.TRIAL_ACTIVATION_PRICE)
        except (TypeError, ValueError):
            logger.warning(
                'Некорректное значение TRIAL_ACTIVATION_PRICE: %s',
                self.TRIAL_ACTIVATION_PRICE,
            )
            return 0

        if value < 0:
            return 0

        return value

    def is_yookassa_enabled(self) -> bool:
        return self.YOOKASSA_ENABLED and self.YOOKASSA_SHOP_ID is not None and self.YOOKASSA_SECRET_KEY is not None

    def get_yookassa_display_name(self) -> str:
        name = (self.YOOKASSA_DISPLAY_NAME or '').strip()
        return name if name else 'YooKassa'

    def is_nalogo_enabled(self) -> bool:
        return self.NALOGO_ENABLED and self.NALOGO_INN is not None and self.NALOGO_PASSWORD is not None

    def is_support_topup_enabled(self) -> bool:
        return bool(self.SUPPORT_TOPUP_ENABLED)

    def get_yookassa_return_url(self) -> str:
        if self.YOOKASSA_RETURN_URL:
            return self.YOOKASSA_RETURN_URL
        if self.WEBHOOK_URL:
            return f'{self.WEBHOOK_URL}/payment-success'
        return 'https://t.me/'

    def is_cryptobot_enabled(self) -> bool:
        return self.CRYPTOBOT_ENABLED and self.CRYPTOBOT_API_TOKEN is not None

    def get_cryptobot_display_name(self) -> str:
        name = (self.CRYPTOBOT_DISPLAY_NAME or '').strip()
        return name if name else 'CryptoBot'

    def is_heleket_enabled(self) -> bool:
        return self.HELEKET_ENABLED and self.HELEKET_MERCHANT_ID is not None and self.HELEKET_API_KEY is not None

    def get_heleket_display_name(self) -> str:
        name = (self.HELEKET_DISPLAY_NAME or '').strip()
        return name if name else 'Heleket Crypto'

    def is_mulenpay_enabled(self) -> bool:
        return (
            self.MULENPAY_ENABLED
            and self.MULENPAY_API_KEY is not None
            and self.MULENPAY_SECRET_KEY is not None
            and self.MULENPAY_SHOP_ID is not None
        )

    def get_mulenpay_display_name(self) -> str:
        name = (self.MULENPAY_DISPLAY_NAME or '').strip()
        if not name:
            return 'Mulen Pay'
        return name

    def get_mulenpay_display_name_html(self) -> str:
        return html.escape(self.get_mulenpay_display_name())

    def get_mulenpay_expected_origin(self) -> str | None:
        override = (self.MULENPAY_IFRAME_EXPECTED_ORIGIN or '').strip()
        if override:
            return override

        base_url = (self.MULENPAY_BASE_URL or '').strip()
        if not base_url:
            return None

        parsed = urlparse(base_url)
        if parsed.scheme and parsed.netloc:
            return f'{parsed.scheme}://{parsed.netloc}'
        return None

    def is_pal24_enabled(self) -> bool:
        return self.PAL24_ENABLED and self.PAL24_API_TOKEN is not None and self.PAL24_SHOP_ID is not None

    def get_pal24_display_name(self) -> str:
        name = (self.PAL24_DISPLAY_NAME or '').strip()
        return name if name else 'PAL24'

    def is_platega_enabled(self) -> bool:
        return self.PLATEGA_ENABLED and self.PLATEGA_MERCHANT_ID is not None and self.PLATEGA_SECRET is not None

    def get_platega_display_name(self) -> str:
        name = (self.PLATEGA_DISPLAY_NAME or '').strip()
        if not name:
            return 'Platega'
        return name

    def get_platega_display_name_html(self) -> str:
        return html.escape(self.get_platega_display_name())

    def get_platega_return_url(self) -> str | None:
        if self.PLATEGA_RETURN_URL:
            return self.PLATEGA_RETURN_URL
        if self.WEBHOOK_URL:
            return f'{self.WEBHOOK_URL}/payment-success'
        return None

    def get_platega_failed_url(self) -> str | None:
        if self.PLATEGA_FAILED_URL:
            return self.PLATEGA_FAILED_URL
        if self.WEBHOOK_URL:
            return f'{self.WEBHOOK_URL}/payment-failed'
        return None

    def get_platega_active_methods(self) -> list[int]:
        raw_value = str(self.PLATEGA_ACTIVE_METHODS or '')
        normalized = raw_value.replace(';', ',')
        methods: list[int] = []
        seen: set[int] = set()
        for part in normalized.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                method_code = int(part)
            except ValueError:
                logger.warning('Некорректный код метода Platega: %s', part)
                continue
            if method_code in {2, 10, 11, 12, 13} and method_code not in seen:
                methods.append(method_code)
                seen.add(method_code)

        if not methods:
            return [2]

        return methods

    @staticmethod
    def get_platega_method_definitions() -> dict[int, dict[str, str]]:
        return {
            2: {'name': 'СБП (QR)', 'title': '🏦 СБП (QR)'},
            10: {'name': 'Банковские карты (RUB)', 'title': '💳 Карты (RUB)'},
            11: {'name': 'Банковские карты', 'title': '💳 Банковские карты'},
            12: {'name': 'Международные карты', 'title': '🌍 Международные карты'},
            13: {'name': 'Криптовалюта', 'title': '🪙 Криптовалюта'},
        }

    def get_platega_method_display_name(self, method_code: int) -> str:
        definitions = self.get_platega_method_definitions()
        info = definitions.get(method_code)
        if info and info.get('name'):
            return info['name']
        return f'Метод {method_code}'

    def get_platega_method_display_title(self, method_code: int) -> str:
        definitions = self.get_platega_method_definitions()
        info = definitions.get(method_code)
        if not info:
            return f'Platega {method_code}'
        return info.get('title') or info.get('name') or f'Platega {method_code}'

    def is_wata_enabled(self) -> bool:
        return self.WATA_ENABLED and self.WATA_ACCESS_TOKEN is not None and self.WATA_TERMINAL_PUBLIC_ID is not None

    def get_wata_display_name(self) -> str:
        name = (self.WATA_DISPLAY_NAME or '').strip()
        return name if name else 'Wata'

    def is_cloudpayments_enabled(self) -> bool:
        return (
            self.CLOUDPAYMENTS_ENABLED
            and self.CLOUDPAYMENTS_PUBLIC_ID is not None
            and self.CLOUDPAYMENTS_API_SECRET is not None
        )

    def get_cloudpayments_display_name(self) -> str:
        name = (self.CLOUDPAYMENTS_DISPLAY_NAME or '').strip()
        return name if name else 'CloudPayments'

    def is_freekassa_enabled(self) -> bool:
        return (
            self.FREEKASSA_ENABLED
            and self.FREEKASSA_SHOP_ID is not None
            and self.FREEKASSA_API_KEY is not None
            and self.FREEKASSA_SECRET_WORD_1 is not None
            and self.FREEKASSA_SECRET_WORD_2 is not None
        )

    def get_freekassa_display_name(self) -> str:
        name = (self.FREEKASSA_DISPLAY_NAME or '').strip()
        return name if name else 'Freekassa'

    def get_freekassa_display_name_html(self) -> str:
        return html.escape(self.get_freekassa_display_name())

    def is_kassa_ai_enabled(self) -> bool:
        return (
            self.KASSA_AI_ENABLED
            and self.KASSA_AI_SHOP_ID is not None
            and self.KASSA_AI_API_KEY is not None
            and self.KASSA_AI_SECRET_WORD_2 is not None
        )

    def get_kassa_ai_display_name(self) -> str:
        name = (self.KASSA_AI_DISPLAY_NAME or '').strip()
        return name if name else 'KassaAI'

    def get_kassa_ai_display_name_html(self) -> str:
        return html.escape(self.get_kassa_ai_display_name())

    def is_payment_verification_auto_check_enabled(self) -> bool:
        return self.PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED

    def get_payment_verification_auto_check_interval(self) -> int:
        try:
            minutes = int(self.PAYMENT_VERIFICATION_AUTO_CHECK_INTERVAL_MINUTES)
        except (TypeError, ValueError):  # pragma: no cover - защитная проверка конфигурации
            minutes = 10

        if minutes <= 0:
            logger.warning(
                'Некорректный интервал автопроверки платежей: %s. Используется значение по умолчанию 10 минут.',
                self.PAYMENT_VERIFICATION_AUTO_CHECK_INTERVAL_MINUTES,
            )
            return 10

        return minutes

    def get_cryptobot_base_url(self) -> str:
        if self.CRYPTOBOT_TESTNET:
            return 'https://testnet-pay.crypt.bot'
        return self.CRYPTOBOT_BASE_URL

    def get_cryptobot_assets(self) -> list[str]:
        try:
            assets = self.CRYPTOBOT_ASSETS.strip()
            if not assets:
                return ['USDT', 'TON']
            return [asset.strip() for asset in assets.split(',') if asset.strip()]
        except (ValueError, AttributeError):
            return ['USDT', 'TON']

    def get_cryptobot_invoice_expires_seconds(self) -> int:
        return self.CRYPTOBOT_INVOICE_EXPIRES_HOURS * 3600

    def get_heleket_markup_percent(self) -> float:
        try:
            return float(self.HELEKET_MARKUP_PERCENT)
        except (TypeError, ValueError):
            return 0.0

    def get_heleket_lifetime(self) -> int:
        try:
            value = int(self.HELEKET_INVOICE_LIFETIME)
        except (TypeError, ValueError):
            value = 3600
        return max(300, min(43200, value))

    def get_heleket_callback_url(self) -> str | None:
        if self.HELEKET_CALLBACK_URL:
            return self.HELEKET_CALLBACK_URL
        if self.WEBHOOK_URL:
            return f'{self.WEBHOOK_URL}{self.HELEKET_WEBHOOK_PATH}'
        return None

    def is_happ_cryptolink_mode(self) -> bool:
        return self.CONNECT_BUTTON_MODE == 'happ_cryptolink'

    def is_happ_download_button_enabled(self) -> bool:
        return self.is_happ_cryptolink_mode() and self.CONNECT_BUTTON_HAPP_DOWNLOAD_ENABLED

    def should_hide_subscription_link(self) -> bool:
        """Returns True when subscription links must be hidden from the interface."""

        if self.is_happ_cryptolink_mode():
            return False
        return self.HIDE_SUBSCRIPTION_LINK

    def is_contests_enabled(self) -> bool:
        if getattr(self, 'CONTESTS_ENABLED', False):
            return True
        # legacy fallback
        return bool(getattr(self, 'REFERRAL_CONTESTS_ENABLED', False))

    def is_referral_contests_enabled(self) -> bool:
        # kept for backward compatibility
        return self.is_contests_enabled()

    def get_happ_cryptolink_redirect_template(self) -> str | None:
        template = (self.HAPP_CRYPTOLINK_REDIRECT_TEMPLATE or '').strip()
        return template or None

    def get_happ_download_link(self, platform: str) -> str | None:
        platform_key = platform.lower()

        if platform_key == 'pc':
            platform_key = 'windows'

        links = {
            'ios': (self.HAPP_DOWNLOAD_LINK_IOS or '').strip(),
            'android': (self.HAPP_DOWNLOAD_LINK_ANDROID or '').strip(),
            'macos': (self.HAPP_DOWNLOAD_LINK_MACOS or '').strip(),
            'windows': ((self.HAPP_DOWNLOAD_LINK_WINDOWS or '').strip() or (self.HAPP_DOWNLOAD_LINK_PC or '').strip()),
        }
        link = links.get(platform_key)
        return link if link else None

    def is_maintenance_mode(self) -> bool:
        return self.MAINTENANCE_MODE

    def get_maintenance_message(self) -> str:
        return self.MAINTENANCE_MESSAGE

    def get_maintenance_check_interval(self) -> int:
        return self.MAINTENANCE_CHECK_INTERVAL

    def get_maintenance_retry_attempts(self) -> int:
        try:
            attempts = int(self.MAINTENANCE_RETRY_ATTEMPTS)
        except (TypeError, ValueError):
            attempts = 1
        return max(1, attempts)

    def is_base_promo_group_period_discount_enabled(self) -> bool:
        return self.BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED

    def get_base_promo_group_period_discounts(self) -> dict[int, int]:
        try:
            config_str = (self.BASE_PROMO_GROUP_PERIOD_DISCOUNTS or '').strip()
            if not config_str:
                return {}

            discounts: dict[int, int] = {}
            for part in config_str.split(','):
                part = part.strip()
                if not part:
                    continue

                period_and_discount = part.split(':')
                if len(period_and_discount) != 2:
                    continue

                period_str, discount_str = period_and_discount
                try:
                    period_days = int(period_str.strip())
                    discount_percent = int(discount_str.strip())
                except ValueError:
                    continue

                discounts[period_days] = max(0, min(100, discount_percent))

            return discounts
        except Exception:
            return {}

    def get_base_promo_group_period_discount(self, period_days: int | None) -> int:
        if not period_days or not self.is_base_promo_group_period_discount_enabled():
            return 0

        discounts = self.get_base_promo_group_period_discounts()
        return discounts.get(period_days, 0)

    def is_maintenance_auto_enable(self) -> bool:
        return self.MAINTENANCE_AUTO_ENABLE

    def is_maintenance_monitoring_enabled(self) -> bool:
        return self.MAINTENANCE_MONITORING_ENABLED

    def get_available_subscription_periods(self) -> list[int]:
        """
        Возвращает доступные периоды подписки.
        Использует AVAILABLE_SUBSCRIPTION_PERIODS для фильтрации.
        Не фильтрует по цене, т.к. в режиме classic базовая цена может быть 0.
        """

        # Получаем разрешённые периоды из настройки
        try:
            periods_str = self.AVAILABLE_SUBSCRIPTION_PERIODS
            if not periods_str or not periods_str.strip():
                allowed_periods = {14, 30, 60, 90, 180, 360}
            else:
                allowed_periods = set()
                for period_str in periods_str.split(','):
                    period_str = period_str.strip()
                    if period_str:
                        allowed_periods.add(int(period_str))
        except (ValueError, AttributeError):
            allowed_periods = {14, 30, 60, 90, 180, 360}

        # Возвращаем только разрешённые периоды (без фильтрации по цене,
        # т.к. в режиме classic цена складывается из серверов/трафика/устройств)
        periods = sorted(allowed_periods)

        return periods if periods else [30, 90, 180]

    def get_available_renewal_periods(self) -> list[int]:
        """
        Возвращает доступные периоды продления.
        Использует AVAILABLE_RENEWAL_PERIODS для фильтрации.
        Не фильтрует по цене, т.к. в режиме classic базовая цена может быть 0.
        """
        # Получаем разрешённые периоды из настройки
        try:
            periods_str = self.AVAILABLE_RENEWAL_PERIODS
            if not periods_str or not periods_str.strip():
                allowed_periods = {30, 60, 90, 180, 360}
            else:
                allowed_periods = set()
                for period_str in periods_str.split(','):
                    period_str = period_str.strip()
                    if period_str:
                        allowed_periods.add(int(period_str))
        except (ValueError, AttributeError):
            allowed_periods = {30, 60, 90, 180, 360}

        # Возвращаем только разрешённые периоды (без фильтрации по цене)
        periods = sorted(allowed_periods)

        return periods if periods else [30, 90, 180]

    def get_configured_subscription_periods(self) -> list[int]:
        """
        Возвращает настроенные периоды подписки из AVAILABLE_SUBSCRIPTION_PERIODS.
        БЕЗ фильтрации по ценам - используется для админки.
        """
        try:
            periods_str = self.AVAILABLE_SUBSCRIPTION_PERIODS
            if not periods_str or not periods_str.strip():
                return [14, 30, 60, 90, 180, 360]

            periods = []
            for period_str in periods_str.split(','):
                period_str = period_str.strip()
                if period_str:
                    periods.append(int(period_str))
            return sorted(periods) if periods else [14, 30, 60, 90, 180, 360]
        except (ValueError, AttributeError):
            return [14, 30, 60, 90, 180, 360]

    def get_configured_renewal_periods(self) -> list[int]:
        """
        Возвращает настроенные периоды продления из AVAILABLE_RENEWAL_PERIODS.
        БЕЗ фильтрации по ценам - используется для админки.
        """
        try:
            periods_str = self.AVAILABLE_RENEWAL_PERIODS
            if not periods_str or not periods_str.strip():
                return [30, 60, 90, 180, 360]

            periods = []
            for period_str in periods_str.split(','):
                period_str = period_str.strip()
                if period_str:
                    periods.append(int(period_str))
            return sorted(periods) if periods else [30, 60, 90, 180, 360]
        except (ValueError, AttributeError):
            return [30, 60, 90, 180, 360]

    def get_balance_payment_description(self, amount_kopeks: int, telegram_user_id: int | None = None) -> str:
        # Базовое описание
        description = f'{self.PAYMENT_BALANCE_DESCRIPTION} на {self.format_price(amount_kopeks)}'

        # Если передан user_id, добавляем его
        if telegram_user_id is not None:
            description += f' (ID {telegram_user_id})'

        # Формируем финальную строку по шаблону
        return self.PAYMENT_BALANCE_TEMPLATE.format(service_name=self.PAYMENT_SERVICE_NAME, description=description)

    def get_subscription_payment_description(self, period_days: int, amount_kopeks: int) -> str:
        return self.PAYMENT_SUBSCRIPTION_TEMPLATE.format(
            service_name=self.PAYMENT_SERVICE_NAME,
            description=f'{self.PAYMENT_SUBSCRIPTION_DESCRIPTION} на {period_days} дней',
        )

    def get_custom_payment_description(self, description: str) -> str:
        return self.PAYMENT_BALANCE_TEMPLATE.format(service_name=self.PAYMENT_SERVICE_NAME, description=description)

    def get_stars_rate(self) -> float:
        return self.TELEGRAM_STARS_RATE_RUB

    def get_telegram_stars_display_name(self) -> str:
        name = (self.TELEGRAM_STARS_DISPLAY_NAME or '').strip()
        return name if name else 'Telegram Stars'

    def stars_to_rubles(self, stars: int) -> float:
        return stars * self.get_stars_rate()

    def rubles_to_stars(self, rubles: float) -> int:
        rate = self.get_stars_rate()
        if rate <= 0:
            raise ValueError('Stars rate must be positive')
        return max(1, math.ceil(rubles / rate))

    def get_admin_notifications_chat_id(self) -> int | None:
        if not self.ADMIN_NOTIFICATIONS_CHAT_ID:
            return None

        try:
            return int(self.ADMIN_NOTIFICATIONS_CHAT_ID)
        except (ValueError, TypeError):
            return None

    def is_admin_notifications_enabled(self) -> bool:
        return self.ADMIN_NOTIFICATIONS_ENABLED and self.get_admin_notifications_chat_id() is not None

    def get_backup_send_chat_id(self) -> int | None:
        if not self.BACKUP_SEND_CHAT_ID:
            return None

        try:
            return int(self.BACKUP_SEND_CHAT_ID)
        except (ValueError, TypeError):
            return None

    def is_backup_send_enabled(self) -> bool:
        return self.BACKUP_SEND_ENABLED and self.get_backup_send_chat_id() is not None

    def get_backup_archive_password(self) -> str | None:
        password = (self.BACKUP_ARCHIVE_PASSWORD or '').strip()
        return password if password else None

    # === Log Rotation Methods ===

    def is_log_rotation_enabled(self) -> bool:
        """Проверить, включена ли новая система ротации логов."""
        return self.LOG_ROTATION_ENABLED

    def get_log_rotation_chat_id(self) -> int | None:
        """Получить ID канала для отправки логов.

        Если LOG_ROTATION_CHAT_ID не задан, использует BACKUP_SEND_CHAT_ID.
        """
        chat_id = self.LOG_ROTATION_CHAT_ID or self.BACKUP_SEND_CHAT_ID
        if not chat_id:
            return None

        try:
            return int(chat_id)
        except (ValueError, TypeError):
            return None

    def get_log_rotation_topic_id(self) -> int | None:
        """Получить ID топика для отправки логов.

        Если LOG_ROTATION_TOPIC_ID не задан, использует BACKUP_SEND_TOPIC_ID.
        """
        topic_id = self.LOG_ROTATION_TOPIC_ID
        if topic_id is not None:
            return topic_id
        return self.BACKUP_SEND_TOPIC_ID

    def get_referral_settings(self) -> dict:
        return {
            'program_enabled': self.is_referral_program_enabled(),
            'minimum_topup_kopeks': self.REFERRAL_MINIMUM_TOPUP_KOPEKS,
            'first_topup_bonus_kopeks': self.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
            'inviter_bonus_kopeks': self.REFERRAL_INVITER_BONUS_KOPEKS,
            'commission_percent': self.REFERRAL_COMMISSION_PERCENT,
            'notifications_enabled': self.REFERRAL_NOTIFICATIONS_ENABLED,
            'withdrawal_enabled': self.REFERRAL_WITHDRAWAL_ENABLED,
            'withdrawal_min_amount_kopeks': self.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS,
            'withdrawal_cooldown_days': self.REFERRAL_WITHDRAWAL_COOLDOWN_DAYS,
        }

    def is_referral_withdrawal_enabled(self) -> bool:
        """Проверяет, включена ли функция вывода реферального баланса."""
        return self.is_referral_program_enabled() and self.REFERRAL_WITHDRAWAL_ENABLED

    def is_referral_program_enabled(self) -> bool:
        return bool(self.REFERRAL_PROGRAM_ENABLED)

    def is_referral_notifications_enabled(self) -> bool:
        return self.REFERRAL_NOTIFICATIONS_ENABLED

    def get_traffic_packages(self) -> list[dict]:
        import logging

        logger = logging.getLogger(__name__)

        try:
            packages = []
            config_str = self.TRAFFIC_PACKAGES_CONFIG.strip()

            logger.debug(f"CONFIG STRING: '{config_str}'")

            if not config_str:
                logger.debug('CONFIG EMPTY, USING FALLBACK')
                return self._get_fallback_traffic_packages()

            logger.debug('PARSING CONFIG...')

            for package_config in config_str.split(','):
                package_config = package_config.strip()
                if not package_config:
                    continue

                parts = package_config.split(':')
                if len(parts) != 3:
                    continue

                try:
                    gb = int(parts[0])
                    price = int(parts[1])
                    enabled = parts[2].lower() == 'true'

                    packages.append({'gb': gb, 'price': price, 'enabled': enabled})
                except ValueError:
                    continue

            logger.debug(f'PARSED {len(packages)} packages from config')
            return packages if packages else self._get_fallback_traffic_packages()

        except Exception as e:
            logger.info(f'ERROR PARSING CONFIG: {e}')
            return self._get_fallback_traffic_packages()

    def is_version_check_enabled(self) -> bool:
        return self.VERSION_CHECK_ENABLED

    def get_version_check_repo(self) -> str:
        return self.VERSION_CHECK_REPO

    def get_version_check_interval(self) -> int:
        return self.VERSION_CHECK_INTERVAL_HOURS

    def _get_fallback_traffic_packages(self) -> list[dict]:
        try:
            if self.TRAFFIC_PACKAGES_CONFIG.strip():
                packages = []
                for package_config in self.TRAFFIC_PACKAGES_CONFIG.split(','):
                    package_config = package_config.strip()
                    if not package_config:
                        continue

                    parts = package_config.split(':')
                    if len(parts) != 3:
                        continue

                    try:
                        gb = int(parts[0])
                        price = int(parts[1])
                        enabled = parts[2].lower() == 'true'

                        packages.append({'gb': gb, 'price': price, 'enabled': enabled})
                    except ValueError:
                        continue

                if packages:
                    return packages
        except Exception:
            pass

        return [
            {'gb': 5, 'price': self.PRICE_TRAFFIC_5GB, 'enabled': True},
            {'gb': 10, 'price': self.PRICE_TRAFFIC_10GB, 'enabled': True},
            {'gb': 25, 'price': self.PRICE_TRAFFIC_25GB, 'enabled': True},
            {'gb': 50, 'price': self.PRICE_TRAFFIC_50GB, 'enabled': True},
            {'gb': 100, 'price': self.PRICE_TRAFFIC_100GB, 'enabled': True},
            {'gb': 250, 'price': self.PRICE_TRAFFIC_250GB, 'enabled': True},
            {'gb': 500, 'price': self.PRICE_TRAFFIC_500GB, 'enabled': True},
            {'gb': 1000, 'price': self.PRICE_TRAFFIC_1000GB, 'enabled': True},
            {'gb': 0, 'price': self.PRICE_TRAFFIC_UNLIMITED, 'enabled': True},
        ]

    def get_traffic_price(self, gb: int | None) -> int:
        packages = self.get_traffic_packages()
        enabled_packages = [pkg for pkg in packages if pkg['enabled']]

        if not enabled_packages:
            return 0

        if gb is None:
            gb = 0

        for package in enabled_packages:
            if package['gb'] == gb:
                return package['price']

        unlimited_package = next((pkg for pkg in enabled_packages if pkg['gb'] == 0), None)

        if gb <= 0:
            return unlimited_package['price'] if unlimited_package else 0

        finite_packages = [pkg for pkg in enabled_packages if pkg['gb'] > 0]

        if not finite_packages:
            return unlimited_package['price'] if unlimited_package else 0

        max_package = max(finite_packages, key=lambda x: x['gb'])

        if gb >= max_package['gb']:
            return unlimited_package['price'] if unlimited_package else max_package['price']

        suitable_packages = [pkg for pkg in finite_packages if pkg['gb'] >= gb]

        if suitable_packages:
            nearest_package = min(suitable_packages, key=lambda x: x['gb'])
            return nearest_package['price']

        return unlimited_package['price'] if unlimited_package else 0

    def _clean_support_contact(self) -> str:
        return (self.SUPPORT_USERNAME or '').strip()

    def get_support_contact_url(self) -> str | None:
        contact = self._clean_support_contact()

        if not contact:
            return None

        if contact.startswith(('http://', 'https://', 'tg://')):
            return contact

        contact_without_prefix = contact.lstrip('@')

        if contact_without_prefix.startswith(('t.me/', 'telegram.me/', 'telegram.dog/')):
            return f'https://{contact_without_prefix}'

        if contact.startswith(('t.me/', 'telegram.me/', 'telegram.dog/')):
            return f'https://{contact}'

        if '.' in contact_without_prefix:
            return f'https://{contact_without_prefix}'

        if contact_without_prefix:
            return f'https://t.me/{contact_without_prefix}'

        return None

    def get_support_contact_display(self) -> str:
        contact = self._clean_support_contact()

        if not contact:
            return ''

        if contact.startswith('@'):
            return contact

        if contact.startswith(('http://', 'https://', 'tg://')):
            return contact

        if contact.startswith(('t.me/', 'telegram.me/', 'telegram.dog/')):
            url = self.get_support_contact_url()
            return url if url else contact

        contact_without_prefix = contact.lstrip('@')

        if '.' in contact_without_prefix:
            url = self.get_support_contact_url()
            return url if url else contact

        if re.fullmatch(r'[A-Za-z0-9_]{3,}', contact_without_prefix):
            return f'@{contact_without_prefix}'

        return contact

    def get_support_contact_display_html(self) -> str:
        return html.escape(self.get_support_contact_display())

    def get_server_status_mode(self) -> str:
        return self.SERVER_STATUS_MODE

    def is_server_status_enabled(self) -> bool:
        return self.get_server_status_mode() != 'disabled'

    def get_server_status_external_url(self) -> str | None:
        url = (self.SERVER_STATUS_EXTERNAL_URL or '').strip()
        return url or None

    def get_server_status_metrics_url(self) -> str | None:
        url = (self.SERVER_STATUS_METRICS_URL or '').strip()
        return url or None

    def get_server_status_metrics_auth(self) -> tuple[str, str] | None:
        username = (self.SERVER_STATUS_METRICS_USERNAME or '').strip()
        password_raw = self.SERVER_STATUS_METRICS_PASSWORD

        if not username:
            return None

        password = '' if password_raw is None else str(password_raw)
        return username, password

    def get_server_status_items_per_page(self) -> int:
        return max(1, self.SERVER_STATUS_ITEMS_PER_PAGE)

    def get_server_status_request_timeout(self) -> int:
        return max(1, self.SERVER_STATUS_REQUEST_TIMEOUT)

    def is_web_api_enabled(self) -> bool:
        return bool(self.WEB_API_ENABLED)

    def get_web_api_allowed_origins(self) -> list[str]:
        raw = (self.WEB_API_ALLOWED_ORIGINS or '').split(',')
        origins = [origin.strip() for origin in raw if origin.strip()]
        return origins or ['*']

    def get_web_api_docs_config(self) -> dict[str, str | None]:
        if self.WEB_API_DOCS_ENABLED:
            return {
                'docs_url': '/docs',
                'redoc_url': '/redoc',
                'openapi_url': '/openapi.json',
            }

        return {'docs_url': None, 'redoc_url': None, 'openapi_url': None}

    def get_support_system_mode(self) -> str:
        mode = (self.SUPPORT_SYSTEM_MODE or 'both').strip().lower()
        return mode if mode in {'tickets', 'contact', 'both'} else 'both'

    def is_support_tickets_enabled(self) -> bool:
        return self.get_support_system_mode() in {'tickets', 'both'}

    def is_support_contact_enabled(self) -> bool:
        return self.get_support_system_mode() in {'contact', 'both'}

    # MiniApp tickets settings
    def is_miniapp_tickets_enabled(self) -> bool:
        """Check if tickets are enabled in miniapp."""
        return bool(self.MINIAPP_TICKETS_ENABLED)

    def get_miniapp_support_type(self) -> str:
        """Get miniapp support type: tickets, profile, or url."""
        support_type = (self.MINIAPP_SUPPORT_TYPE or 'tickets').strip().lower()
        return support_type if support_type in {'tickets', 'profile', 'url'} else 'tickets'

    def get_miniapp_support_url(self) -> str:
        """Get custom support URL for miniapp (when type is 'url')."""
        return (self.MINIAPP_SUPPORT_URL or '').strip()

    def get_bot_run_mode(self) -> str:
        mode = (self.BOT_RUN_MODE or 'polling').strip().lower()
        if mode not in {'polling', 'webhook', 'both'}:
            return 'polling'
        return mode

    def get_telegram_webhook_path(self) -> str:
        raw_path = (self.WEBHOOK_PATH or '/webhook').strip()
        if not raw_path:
            raw_path = '/webhook'
        if not raw_path.startswith('/'):
            raw_path = '/' + raw_path
        return raw_path

    def get_webhook_queue_maxsize(self) -> int:
        try:
            size = int(self.WEBHOOK_MAX_QUEUE_SIZE)
        except (TypeError, ValueError):
            size = 1024
        return max(1, size)

    def get_webhook_worker_count(self) -> int:
        try:
            workers = int(self.WEBHOOK_WORKERS)
        except (TypeError, ValueError):
            workers = 1
        return max(1, workers)

    def get_webhook_enqueue_timeout(self) -> float:
        try:
            timeout = float(self.WEBHOOK_ENQUEUE_TIMEOUT)
        except (TypeError, ValueError):
            timeout = 0.0
        return max(0.0, timeout)

    def get_webhook_shutdown_timeout(self) -> float:
        try:
            timeout = float(self.WEBHOOK_WORKER_SHUTDOWN_TIMEOUT)
        except (TypeError, ValueError):
            timeout = 30.0
        return max(1.0, timeout)

    def get_telegram_webhook_url(self) -> str | None:
        base_url = (self.WEBHOOK_URL or '').strip()
        if not base_url:
            return None
        path = self.get_telegram_webhook_path()
        return f'{base_url.rstrip("/")}{path}'

    def get_miniapp_static_path(self) -> Path:
        raw_path = (self.MINIAPP_STATIC_PATH or 'miniapp').strip()
        if not raw_path:
            raw_path = 'miniapp'
        return Path(raw_path)

    # Cabinet methods
    def is_cabinet_enabled(self) -> bool:
        return bool(self.CABINET_ENABLED)

    def get_cabinet_jwt_secret(self) -> str:
        if self.CABINET_JWT_SECRET:
            return self.CABINET_JWT_SECRET
        return self.BOT_TOKEN

    def get_cabinet_access_token_expire_minutes(self) -> int:
        return max(1, self.CABINET_ACCESS_TOKEN_EXPIRE_MINUTES)

    def get_cabinet_refresh_token_expire_days(self) -> int:
        return max(1, self.CABINET_REFRESH_TOKEN_EXPIRE_DAYS)

    def get_cabinet_allowed_origins(self) -> list[str]:
        if not self.CABINET_ALLOWED_ORIGINS:
            return []
        return [o.strip() for o in self.CABINET_ALLOWED_ORIGINS.split(',') if o.strip()]

    def is_cabinet_email_verification_enabled(self) -> bool:
        return bool(self.CABINET_EMAIL_VERIFICATION_ENABLED)

    def get_cabinet_email_verification_expire_hours(self) -> int:
        return max(1, self.CABINET_EMAIL_VERIFICATION_EXPIRE_HOURS)

    def get_cabinet_password_reset_expire_hours(self) -> int:
        return max(1, self.CABINET_PASSWORD_RESET_EXPIRE_HOURS)

    def get_cabinet_email_change_code_expire_minutes(self) -> int:
        return max(1, self.CABINET_EMAIL_CHANGE_CODE_EXPIRE_MINUTES)

    def is_cabinet_email_auth_enabled(self) -> bool:
        return bool(self.CABINET_EMAIL_AUTH_ENABLED)

    def is_smtp_configured(self) -> bool:
        # For servers without AUTH, only host and from_email are required
        has_from = bool(self.SMTP_FROM_EMAIL or self.SMTP_USER)
        return bool(self.SMTP_HOST and has_from)

    def get_smtp_from_email(self) -> str | None:
        if self.SMTP_FROM_EMAIL:
            return self.SMTP_FROM_EMAIL
        return self.SMTP_USER

    # OAuth helpers
    def get_oauth_providers_config(self) -> dict[str, dict[str, str | bool]]:
        """Return config for all OAuth providers (enabled or not)."""
        return {
            'google': {
                'client_id': self.OAUTH_GOOGLE_CLIENT_ID,
                'client_secret': self.OAUTH_GOOGLE_CLIENT_SECRET,
                'enabled': self.OAUTH_GOOGLE_ENABLED,
                'display_name': 'Google',
            },
            'yandex': {
                'client_id': self.OAUTH_YANDEX_CLIENT_ID,
                'client_secret': self.OAUTH_YANDEX_CLIENT_SECRET,
                'enabled': self.OAUTH_YANDEX_ENABLED,
                'display_name': 'Yandex',
            },
            'discord': {
                'client_id': self.OAUTH_DISCORD_CLIENT_ID,
                'client_secret': self.OAUTH_DISCORD_CLIENT_SECRET,
                'enabled': self.OAUTH_DISCORD_ENABLED,
                'display_name': 'Discord',
            },
            'vk': {
                'client_id': self.OAUTH_VK_CLIENT_ID,
                'client_secret': self.OAUTH_VK_CLIENT_SECRET,
                'enabled': self.OAUTH_VK_ENABLED,
                'display_name': 'VK',
            },
        }

    def get_enabled_oauth_provider_names(self) -> list[str]:
        """Return list of enabled OAuth provider names."""
        return [name for name, cfg in self.get_oauth_providers_config().items() if cfg['enabled']]

    # Ban System helpers
    def is_ban_system_enabled(self) -> bool:
        return bool(self.BAN_SYSTEM_ENABLED)

    def is_ban_system_configured(self) -> bool:
        return bool(self.BAN_SYSTEM_API_URL and self.BAN_SYSTEM_API_TOKEN)

    def get_ban_system_api_url(self) -> str | None:
        if self.BAN_SYSTEM_API_URL:
            return self.BAN_SYSTEM_API_URL.rstrip('/')
        return None

    def get_ban_system_api_token(self) -> str | None:
        return self.BAN_SYSTEM_API_TOKEN

    def get_ban_system_request_timeout(self) -> int:
        return max(1, self.BAN_SYSTEM_REQUEST_TIMEOUT)

    model_config = {'env_file': '.env', 'env_file_encoding': 'utf-8', 'extra': 'ignore'}

    @field_validator('TIMEZONE')
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:  # pragma: no cover - defensive validation
            raise ValueError(f'Некорректный идентификатор часового пояса: {value}') from exc
        return value


settings = Settings()
ENV_OVERRIDE_KEYS = set(settings.model_fields_set)

_PERIOD_PRICE_FIELDS: dict[int, str] = {
    14: 'PRICE_14_DAYS',
    30: 'PRICE_30_DAYS',
    60: 'PRICE_60_DAYS',
    90: 'PRICE_90_DAYS',
    180: 'PRICE_180_DAYS',
    360: 'PRICE_360_DAYS',
}

# Хранилище периодов/цен из БД (приоритет над .env)
_DB_PERIOD_PRICES: dict[int, int] | None = None


def set_period_prices_from_db(period_prices: dict[int, int]) -> None:
    """
    Устанавливает периоды/цены из БД.
    Вызывается после синхронизации тарифов при запуске бота.
    """
    global _DB_PERIOD_PRICES
    _DB_PERIOD_PRICES = period_prices.copy() if period_prices else None
    refresh_period_prices()


def get_db_period_prices() -> dict[int, int] | None:
    """Возвращает периоды/цены из БД если они загружены."""
    return _DB_PERIOD_PRICES


def refresh_period_prices() -> None:
    """
    Rebuild cached period price mapping.
    Приоритет: БД > .env
    """
    PERIOD_PRICES.clear()

    if _DB_PERIOD_PRICES:
        # Используем цены из БД
        PERIOD_PRICES.update(_DB_PERIOD_PRICES)
    else:
        # Fallback на .env
        PERIOD_PRICES.update(
            {days: getattr(settings, field_name, 0) for days, field_name in _PERIOD_PRICE_FIELDS.items()}
        )


PERIOD_PRICES: dict[int, int] = {}
refresh_period_prices()


def get_traffic_prices() -> dict[int, int]:
    packages = settings.get_traffic_packages()
    return {package['gb']: package['price'] for package in packages}


TRAFFIC_PRICES = get_traffic_prices()


def refresh_traffic_prices():
    global TRAFFIC_PRICES
    TRAFFIC_PRICES = get_traffic_prices()


refresh_traffic_prices()

settings._original_database_url = settings.DATABASE_URL
settings.DATABASE_URL = settings.get_database_url()
