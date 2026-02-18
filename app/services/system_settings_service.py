import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional, Union, get_args, get_origin

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    ENV_OVERRIDE_KEYS,
    Settings,
    refresh_period_prices,
    refresh_traffic_prices,
    settings,
)
from app.database.crud.system_setting import (
    delete_system_setting,
    upsert_system_setting,
)
from app.database.database import AsyncSessionLocal
from app.database.models import SystemSetting
from app.database.universal_migration import ensure_default_web_api_token


logger = structlog.get_logger(__name__)


def _title_from_key(key: str) -> str:
    parts = key.split('_')
    if not parts:
        return key
    return ' '.join(part.capitalize() for part in parts)


def _truncate(value: str, max_len: int = 60) -> str:
    value = value.strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + '…'


@dataclass(slots=True)
class SettingDefinition:
    key: str
    category_key: str
    category_label: str
    python_type: type[Any]
    type_label: str
    is_optional: bool

    @property
    def display_name(self) -> str:
        return _title_from_key(self.key)


@dataclass(slots=True)
class ChoiceOption:
    value: Any
    label: str
    description: str | None = None


class ReadOnlySettingError(RuntimeError):
    """Исключение, выбрасываемое при попытке изменить настройку только для чтения."""


class BotConfigurationService:
    EXCLUDED_KEYS: set[str] = {'BOT_TOKEN', 'ADMIN_IDS'}

    READ_ONLY_KEYS: set[str] = {'EXTERNAL_ADMIN_TOKEN', 'EXTERNAL_ADMIN_TOKEN_BOT_ID'}
    PLAIN_TEXT_KEYS: set[str] = {'EXTERNAL_ADMIN_TOKEN', 'EXTERNAL_ADMIN_TOKEN_BOT_ID'}

    CATEGORY_TITLES: dict[str, str] = {
        'CORE': '🤖 Основные настройки',
        'SUPPORT': '💬 Поддержка и тикеты',
        'LOCALIZATION': '🌍 Языки интерфейса',
        'CHANNEL': '📣 Обязательная подписка',
        'TIMEZONE': '🗂 Timezone',
        'PAYMENT': '💳 Общие платежные настройки',
        'PAYMENT_VERIFICATION': '🕵️ Проверка платежей',
        'TELEGRAM': '⭐ Telegram Stars',
        'CRYPTOBOT': '🪙 CryptoBot',
        'HELEKET': '🪙 Heleket',
        'CLOUDPAYMENTS': '💳 CloudPayments',
        'FREEKASSA': '💳 Freekassa',
        'KASSA_AI': '💳 KassaAI',
        'YOOKASSA': '🟣 YooKassa',
        'PLATEGA': '💳 {platega_name}',
        'TRIBUTE': '🎁 Tribute',
        'MULENPAY': '💰 {mulenpay_name}',
        'PAL24': '🏦 PAL24 / PayPalych',
        'WATA': '💠 Wata',
        'EXTERNAL_ADMIN': '🛡️ Внешняя админка',
        'SUBSCRIPTIONS_CORE': '📅 Подписки и лимиты',
        'SIMPLE_SUBSCRIPTION': '⚡ Простая покупка',
        'PERIODS': '📆 Периоды подписок',
        'SUBSCRIPTION_PRICES': '💵 Стоимость тарифов',
        'TRAFFIC': '📊 Трафик',
        'TRAFFIC_PACKAGES': '📦 Пакеты трафика',
        'TRIAL': '🎁 Пробный период',
        'REFERRAL': '👥 Реферальная программа',
        'AUTOPAY': '🔄 Автопродление',
        'NOTIFICATIONS': '🔔 Уведомления пользователям',
        'ADMIN_NOTIFICATIONS': '📣 Оповещения администраторам',
        'ADMIN_REPORTS': '🗂 Автоматические отчеты',
        'INTERFACE': '🎨 Интерфейс и брендинг',
        'INTERFACE_BRANDING': '🖼️ Брендинг',
        'INTERFACE_SUBSCRIPTION': '🔗 Ссылка на подписку',
        'CONNECT_BUTTON': '🚀 Кнопка подключения',
        'MINIAPP': '📱 Mini App',
        'HAPP': '🅷 Happ',
        'SKIP': '⚡ Быстрый старт',
        'ADDITIONAL': '📱 Дополнительные приложения',
        'DATABASE': '💾 База данных',
        'POSTGRES': '🐘 PostgreSQL',
        'SQLITE': '🧱 SQLite',
        'REDIS': '🧠 Redis',
        'REMNAWAVE': '🌐 RemnaWave API',
        'SERVER_STATUS': '📊 Статус серверов',
        'MONITORING': '📈 Мониторинг',
        'MAINTENANCE': '🔧 Обслуживание',
        'BACKUP': '💾 Резервные копии',
        'VERSION': '🔄 Проверка версий',
        'WEB_API': '⚡ Web API',
        'WEBHOOK': '🌐 Webhook',
        'WEBHOOK_NOTIFICATIONS': '📢 Уведомления от вебхуков',
        'LOG': '📝 Логирование',
        'DEBUG': '🧪 Режим разработки',
        'MODERATION': '🛡️ Модерация и фильтры',
        'BAN_NOTIFICATIONS': '🚫 Тексты уведомлений о блокировках',
    }

    CATEGORY_DESCRIPTIONS: dict[str, str] = {
        'CORE': 'Базовые параметры работы бота и обязательные ссылки.',
        'SUPPORT': 'Контакты поддержки, SLA и режимы обработки обращений.',
        'LOCALIZATION': 'Доступные языки, локализация интерфейса и выбор языка.',
        'CHANNEL': 'Настройки обязательной подписки на канал или группу.',
        'TIMEZONE': 'Часовой пояс панели и отображение времени.',
        'PAYMENT': 'Общие тексты платежей, описания чеков и шаблоны.',
        'PAYMENT_VERIFICATION': 'Автоматическая проверка пополнений и интервал выполнения.',
        'YOOKASSA': 'Интеграция с YooKassa: идентификаторы магазина и вебхуки.',
        'CRYPTOBOT': 'CryptoBot и криптоплатежи через Telegram.',
        'HELEKET': 'Heleket: криптоплатежи, ключи мерчанта и вебхуки.',
        'CLOUDPAYMENTS': 'CloudPayments: оплата банковскими картами, Public ID, API Secret и вебхуки.',
        'FREEKASSA': 'Freekassa: ID магазина, API ключ, секретные слова и вебхуки.',
        'KASSA_AI': 'KassaAI: отдельная платёжка api.fk.life с СБП, картами и SberPay.',
        'PLATEGA': '{platega_name}: merchant ID, секрет, ссылки возврата и методы оплаты.',
        'MULENPAY': 'Платежи {mulenpay_name} и параметры магазина.',
        'PAL24': 'PAL24 / PayPalych подключения и лимиты.',
        'TRIBUTE': 'Tribute и донат-сервисы.',
        'TELEGRAM': 'Telegram Stars и их стоимость.',
        'WATA': 'Wata: токен доступа, тип платежа и пределы сумм.',
        'EXTERNAL_ADMIN': 'Токен внешней админки для проверки запросов.',
        'SUBSCRIPTIONS_CORE': 'Лимиты устройств, трафика и базовые цены подписок.',
        'SIMPLE_SUBSCRIPTION': 'Параметры упрощённой покупки: период, трафик, устройства и сквады.',
        'PERIODS': 'Доступные периоды подписок и продлений.',
        'SUBSCRIPTION_PRICES': 'Стоимость подписок по периодам в копейках.',
        'TRAFFIC': 'Лимиты трафика и стратегии сброса.',
        'TRAFFIC_PACKAGES': 'Цены пакетов трафика и конфигурация предложений.',
        'TRIAL': 'Длительность и ограничения пробного периода.',
        'REFERRAL': 'Бонусы и пороги реферальной программы.',
        'AUTOPAY': 'Настройки автопродления и минимальный баланс.',
        'NOTIFICATIONS': 'Пользовательские уведомления и кэширование сообщений.',
        'ADMIN_NOTIFICATIONS': 'Оповещения админам о событиях и тикетах.',
        'ADMIN_REPORTS': 'Автоматические отчеты для команды.',
        'INTERFACE': 'Глобальные параметры интерфейса и брендирования.',
        'INTERFACE_BRANDING': 'Логотип и фирменный стиль.',
        'INTERFACE_SUBSCRIPTION': 'Отображение ссылок и кнопок подписок.',
        'CONNECT_BUTTON': 'Поведение кнопки «Подключиться» и miniapp.',
        'MINIAPP': 'Mini App и кастомные ссылки.',
        'HAPP': 'Интеграция Happ и связанные ссылки.',
        'SKIP': 'Настройки быстрого старта и гайд по подключению.',
        'ADDITIONAL': 'Конфигурация app-config.json, deep links и кеша.',
        'DATABASE': 'Режим работы базы данных и пути до файлов.',
        'POSTGRES': 'Параметры подключения к PostgreSQL.',
        'SQLITE': 'Файл SQLite и резервные параметры.',
        'REDIS': 'Подключение к Redis для кэша.',
        'REMNAWAVE': 'Параметры авторизации и интеграция с RemnaWave API.',
        'SERVER_STATUS': 'Отображение статуса серверов и external URL.',
        'MONITORING': 'Интервалы мониторинга и хранение логов.',
        'MAINTENANCE': 'Режим обслуживания, сообщения и интервалы.',
        'BACKUP': 'Резервное копирование и расписание.',
        'VERSION': 'Отслеживание обновлений репозитория.',
        'WEB_API': 'Web API, токены и права доступа.',
        'WEBHOOK': 'Пути и секреты вебхуков.',
        'WEBHOOK_NOTIFICATIONS': 'Управление уведомлениями, которые получают пользователи при событиях RemnaWave (отключение/активация подписки, устройства, трафик и т.д.).',
        'LOG': 'Уровни логирования и ротация.',
        'DEBUG': 'Отладочные функции и безопасный режим.',
        'MODERATION': 'Настройки фильтров отображаемых имен и защиты от фишинга.',
        'BAN_NOTIFICATIONS': 'Тексты уведомлений о блокировках, которые отправляются пользователям.',
    }

    @staticmethod
    def _format_dynamic_copy(category_key: str | None, value: str) -> str:
        if not value:
            return value
        if category_key == 'MULENPAY':
            return value.format(mulenpay_name=settings.get_mulenpay_display_name())
        if category_key == 'PLATEGA':
            return value.format(platega_name=settings.get_platega_display_name())
        return value

    CATEGORY_KEY_OVERRIDES: dict[str, str] = {
        'DATABASE_URL': 'DATABASE',
        'DATABASE_MODE': 'DATABASE',
        'LOCALES_PATH': 'LOCALIZATION',
        'CHANNEL_SUB_ID': 'CHANNEL',
        'CHANNEL_LINK': 'CHANNEL',
        'CHANNEL_IS_REQUIRED_SUB': 'CHANNEL',
        'BOT_USERNAME': 'CORE',
        'DEFAULT_LANGUAGE': 'LOCALIZATION',
        'AVAILABLE_LANGUAGES': 'LOCALIZATION',
        'LANGUAGE_SELECTION_ENABLED': 'LOCALIZATION',
        'DEFAULT_DEVICE_LIMIT': 'SUBSCRIPTIONS_CORE',
        'DEFAULT_TRAFFIC_LIMIT_GB': 'SUBSCRIPTIONS_CORE',
        'MAX_DEVICES_LIMIT': 'SUBSCRIPTIONS_CORE',
        'PRICE_PER_DEVICE': 'SUBSCRIPTIONS_CORE',
        'DEVICES_SELECTION_ENABLED': 'SUBSCRIPTIONS_CORE',
        'DEVICES_SELECTION_DISABLED_AMOUNT': 'SUBSCRIPTIONS_CORE',
        'BASE_SUBSCRIPTION_PRICE': 'SUBSCRIPTIONS_CORE',
        'SALES_MODE': 'SUBSCRIPTIONS_CORE',
        'DEFAULT_TRAFFIC_RESET_STRATEGY': 'TRAFFIC',
        'RESET_TRAFFIC_ON_PAYMENT': 'TRAFFIC',
        'TRAFFIC_SELECTION_MODE': 'TRAFFIC',
        'FIXED_TRAFFIC_LIMIT_GB': 'TRAFFIC',
        'AVAILABLE_SUBSCRIPTION_PERIODS': 'PERIODS',
        'AVAILABLE_RENEWAL_PERIODS': 'PERIODS',
        'PRICE_14_DAYS': 'SUBSCRIPTION_PRICES',
        'PRICE_30_DAYS': 'SUBSCRIPTION_PRICES',
        'PRICE_60_DAYS': 'SUBSCRIPTION_PRICES',
        'PRICE_90_DAYS': 'SUBSCRIPTION_PRICES',
        'PRICE_180_DAYS': 'SUBSCRIPTION_PRICES',
        'PRICE_360_DAYS': 'SUBSCRIPTION_PRICES',
        'PAID_SUBSCRIPTION_USER_TAG': 'SUBSCRIPTION_PRICES',
        'TRAFFIC_PACKAGES_CONFIG': 'TRAFFIC_PACKAGES',
        'BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED': 'SUBSCRIPTIONS_CORE',
        'BASE_PROMO_GROUP_PERIOD_DISCOUNTS': 'SUBSCRIPTIONS_CORE',
        'DEFAULT_AUTOPAY_ENABLED': 'AUTOPAY',
        'DEFAULT_AUTOPAY_DAYS_BEFORE': 'AUTOPAY',
        'MIN_BALANCE_FOR_AUTOPAY_KOPEKS': 'AUTOPAY',
        'TRIAL_WARNING_HOURS': 'TRIAL',
        'TRIAL_USER_TAG': 'TRIAL',
        'SUPPORT_USERNAME': 'SUPPORT',
        'SUPPORT_MENU_ENABLED': 'SUPPORT',
        'SUPPORT_SYSTEM_MODE': 'SUPPORT',
        'SUPPORT_TICKET_SLA_ENABLED': 'SUPPORT',
        'SUPPORT_TICKET_SLA_MINUTES': 'SUPPORT',
        'SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS': 'SUPPORT',
        'SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES': 'SUPPORT',
        'ADMIN_NOTIFICATIONS_ENABLED': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_CHAT_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_REPORTS_ENABLED': 'ADMIN_REPORTS',
        'ADMIN_REPORTS_CHAT_ID': 'ADMIN_REPORTS',
        'ADMIN_REPORTS_TOPIC_ID': 'ADMIN_REPORTS',
        'ADMIN_REPORTS_SEND_TIME': 'ADMIN_REPORTS',
        'PAYMENT_SERVICE_NAME': 'PAYMENT',
        'PAYMENT_BALANCE_DESCRIPTION': 'PAYMENT',
        'PAYMENT_SUBSCRIPTION_DESCRIPTION': 'PAYMENT',
        'PAYMENT_BALANCE_TEMPLATE': 'PAYMENT',
        'PAYMENT_SUBSCRIPTION_TEMPLATE': 'PAYMENT',
        'AUTO_PURCHASE_AFTER_TOPUP_ENABLED': 'PAYMENT',
        'SIMPLE_SUBSCRIPTION_ENABLED': 'SIMPLE_SUBSCRIPTION',
        'SIMPLE_SUBSCRIPTION_PERIOD_DAYS': 'SIMPLE_SUBSCRIPTION',
        'SIMPLE_SUBSCRIPTION_DEVICE_LIMIT': 'SIMPLE_SUBSCRIPTION',
        'SIMPLE_SUBSCRIPTION_TRAFFIC_GB': 'SIMPLE_SUBSCRIPTION',
        'SIMPLE_SUBSCRIPTION_SQUAD_UUID': 'SIMPLE_SUBSCRIPTION',
        'DISABLE_TOPUP_BUTTONS': 'PAYMENT',
        'SUPPORT_TOPUP_ENABLED': 'PAYMENT',
        'ENABLE_NOTIFICATIONS': 'NOTIFICATIONS',
        'NOTIFICATION_RETRY_ATTEMPTS': 'NOTIFICATIONS',
        'NOTIFICATION_CACHE_HOURS': 'NOTIFICATIONS',
        'MONITORING_LOGS_RETENTION_DAYS': 'MONITORING',
        'MONITORING_INTERVAL': 'MONITORING',
        'TRAFFIC_MONITORING_ENABLED': 'MONITORING',
        'TRAFFIC_MONITORING_INTERVAL_HOURS': 'MONITORING',
        'TRAFFIC_MONITORED_NODES': 'MONITORING',
        'TRAFFIC_SNAPSHOT_TTL_HOURS': 'MONITORING',
        'TRAFFIC_FAST_CHECK_ENABLED': 'MONITORING',
        'TRAFFIC_FAST_CHECK_INTERVAL_MINUTES': 'MONITORING',
        'TRAFFIC_FAST_CHECK_THRESHOLD_GB': 'MONITORING',
        'TRAFFIC_DAILY_CHECK_ENABLED': 'MONITORING',
        'TRAFFIC_DAILY_CHECK_TIME': 'MONITORING',
        'TRAFFIC_DAILY_THRESHOLD_GB': 'MONITORING',
        'TRAFFIC_IGNORED_NODES': 'MONITORING',
        'TRAFFIC_EXCLUDED_USER_UUIDS': 'MONITORING',
        'TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES': 'MONITORING',
        'SUSPICIOUS_NOTIFICATIONS_TOPIC_ID': 'MONITORING',
        'TRAFFIC_CHECK_BATCH_SIZE': 'MONITORING',
        'TRAFFIC_CHECK_CONCURRENCY': 'MONITORING',
        'ENABLE_LOGO_MODE': 'INTERFACE_BRANDING',
        'LOGO_FILE': 'INTERFACE_BRANDING',
        'HIDE_SUBSCRIPTION_LINK': 'INTERFACE_SUBSCRIPTION',
        'MAIN_MENU_MODE': 'INTERFACE',
        'CABINET_BUTTON_STYLE': 'INTERFACE',
        'CONNECT_BUTTON_MODE': 'CONNECT_BUTTON',
        'MINIAPP_CUSTOM_URL': 'CONNECT_BUTTON',
        'APP_CONFIG_PATH': 'ADDITIONAL',
        'ENABLE_DEEP_LINKS': 'ADDITIONAL',
        'APP_CONFIG_CACHE_TTL': 'ADDITIONAL',
        'INACTIVE_USER_DELETE_MONTHS': 'MAINTENANCE',
        'MAINTENANCE_MESSAGE': 'MAINTENANCE',
        'MAINTENANCE_CHECK_INTERVAL': 'MAINTENANCE',
        'MAINTENANCE_AUTO_ENABLE': 'MAINTENANCE',
        'MAINTENANCE_RETRY_ATTEMPTS': 'MAINTENANCE',
        'WEBHOOK_URL': 'WEBHOOK',
        'WEBHOOK_SECRET': 'WEBHOOK',
        'VERSION_CHECK_ENABLED': 'VERSION',
        'VERSION_CHECK_REPO': 'VERSION',
        'VERSION_CHECK_INTERVAL_HOURS': 'VERSION',
        'TELEGRAM_STARS_RATE_RUB': 'TELEGRAM',
        'REMNAWAVE_USER_DESCRIPTION_TEMPLATE': 'REMNAWAVE',
        'REMNAWAVE_USER_USERNAME_TEMPLATE': 'REMNAWAVE',
        'REMNAWAVE_AUTO_SYNC_ENABLED': 'REMNAWAVE',
        'REMNAWAVE_AUTO_SYNC_TIMES': 'REMNAWAVE',
        'CABINET_REMNA_SUB_CONFIG': 'MINIAPP',
    }

    CATEGORY_PREFIX_OVERRIDES: dict[str, str] = {
        'SUPPORT_': 'SUPPORT',
        'ADMIN_NOTIFICATIONS': 'ADMIN_NOTIFICATIONS',
        'ADMIN_REPORTS': 'ADMIN_REPORTS',
        'CHANNEL_': 'CHANNEL',
        'POSTGRES_': 'POSTGRES',
        'SQLITE_': 'SQLITE',
        'REDIS_': 'REDIS',
        'REMNAWAVE': 'REMNAWAVE',
        'TRIAL_': 'TRIAL',
        'TRAFFIC_PACKAGES': 'TRAFFIC_PACKAGES',
        'PRICE_TRAFFIC': 'TRAFFIC_PACKAGES',
        'TRAFFIC_': 'TRAFFIC',
        'REFERRAL_': 'REFERRAL',
        'AUTOPAY_': 'AUTOPAY',
        'TELEGRAM_STARS': 'TELEGRAM',
        'TRIBUTE_': 'TRIBUTE',
        'YOOKASSA_': 'YOOKASSA',
        'CRYPTOBOT_': 'CRYPTOBOT',
        'HELEKET_': 'HELEKET',
        'CLOUDPAYMENTS_': 'CLOUDPAYMENTS',
        'FREEKASSA_': 'FREEKASSA',
        'KASSA_AI_': 'KASSA_AI',
        'PLATEGA_': 'PLATEGA',
        'MULENPAY_': 'MULENPAY',
        'PAL24_': 'PAL24',
        'PAYMENT_': 'PAYMENT',
        'PAYMENT_VERIFICATION_': 'PAYMENT_VERIFICATION',
        'WATA_': 'WATA',
        'EXTERNAL_ADMIN_': 'EXTERNAL_ADMIN',
        'SIMPLE_SUBSCRIPTION_': 'SIMPLE_SUBSCRIPTION',
        'CONNECT_BUTTON_HAPP': 'HAPP',
        'HAPP_': 'HAPP',
        'SKIP_': 'SKIP',
        'MINIAPP_': 'MINIAPP',
        'MONITORING_': 'MONITORING',
        'NOTIFICATION_': 'NOTIFICATIONS',
        'SERVER_STATUS': 'SERVER_STATUS',
        'MAINTENANCE_': 'MAINTENANCE',
        'VERSION_CHECK': 'VERSION',
        'BACKUP_': 'BACKUP',
        'WEBHOOK_NOTIFY_': 'WEBHOOK_NOTIFICATIONS',
        'WEBHOOK_': 'WEBHOOK',
        'LOG_': 'LOG',
        'WEB_API_': 'WEB_API',
        'DEBUG': 'DEBUG',
        'DISPLAY_NAME_': 'MODERATION',
        'BAN_MSG_': 'BAN_NOTIFICATIONS',
    }

    CHOICES: dict[str, list[ChoiceOption]] = {
        'DATABASE_MODE': [
            ChoiceOption('auto', '🤖 Авто'),
            ChoiceOption('postgresql', '🐘 PostgreSQL'),
            ChoiceOption('sqlite', '💾 SQLite'),
        ],
        'REMNAWAVE_AUTH_TYPE': [
            ChoiceOption('api_key', '🔑 API Key'),
            ChoiceOption('basic_auth', '🧾 Basic Auth'),
        ],
        'REMNAWAVE_USER_DELETE_MODE': [
            ChoiceOption('delete', '🗑 Удалять'),
            ChoiceOption('disable', '🚫 Деактивировать'),
        ],
        'TRAFFIC_SELECTION_MODE': [
            ChoiceOption('selectable', '📦 Выбор пакетов'),
            ChoiceOption('fixed', '📏 Фиксированный лимит'),
            ChoiceOption('fixed_with_topup', '📏 Фикс. лимит + докупка'),
        ],
        'DEFAULT_TRAFFIC_RESET_STRATEGY': [
            ChoiceOption('NO_RESET', '♾️ Без сброса'),
            ChoiceOption('DAY', '📅 Ежедневно'),
            ChoiceOption('WEEK', '🗓 Еженедельно'),
            ChoiceOption('MONTH', '📆 Ежемесячно'),
        ],
        'SUPPORT_SYSTEM_MODE': [
            ChoiceOption('tickets', '🎫 Только тикеты'),
            ChoiceOption('contact', '💬 Только контакт'),
            ChoiceOption('both', '🔁 Оба варианта'),
        ],
        'CONNECT_BUTTON_MODE': [
            ChoiceOption('guide', '📘 Гайд'),
            ChoiceOption('miniapp_subscription', '🧾 Mini App подписка'),
            ChoiceOption('miniapp_custom', '🧩 Mini App (ссылка)'),
            ChoiceOption('link', '🔗 Прямая ссылка'),
            ChoiceOption('happ_cryptolink', '🪙 Happ CryptoLink'),
        ],
        'MAIN_MENU_MODE': [
            ChoiceOption('default', '📋 Полное меню'),
            ChoiceOption('cabinet', '🏠 Cabinet (МиниАпп)'),
        ],
        'CABINET_BUTTON_STYLE': [
            ChoiceOption('', '🎨 По секциям (авто)'),
            ChoiceOption('primary', '🔵 Синий'),
            ChoiceOption('success', '🟢 Зелёный'),
            ChoiceOption('danger', '🔴 Красный'),
        ],
        'SALES_MODE': [
            ChoiceOption('classic', '📋 Классический (периоды из .env)'),
            ChoiceOption('tariffs', '📦 Тарифы (из кабинета)'),
        ],
        'SERVER_STATUS_MODE': [
            ChoiceOption('disabled', '🚫 Отключено'),
            ChoiceOption('external_link', '🌐 Внешняя ссылка'),
            ChoiceOption('external_link_miniapp', '🧭 Mini App ссылка'),
            ChoiceOption('xray', '📊 XRay Checker'),
        ],
        'YOOKASSA_PAYMENT_MODE': [
            ChoiceOption('full_payment', '💳 Полная оплата'),
            ChoiceOption('partial_payment', '🪙 Частичная оплата'),
            ChoiceOption('advance', '💼 Аванс'),
            ChoiceOption('full_prepayment', '📦 Полная предоплата'),
            ChoiceOption('partial_prepayment', '📦 Частичная предоплата'),
            ChoiceOption('credit', '💰 Кредит'),
            ChoiceOption('credit_payment', '💸 Погашение кредита'),
        ],
        'YOOKASSA_PAYMENT_SUBJECT': [
            ChoiceOption('commodity', '📦 Товар'),
            ChoiceOption('excise', '🥃 Подакцизный товар'),
            ChoiceOption('job', '🛠 Работа'),
            ChoiceOption('service', '🧾 Услуга'),
            ChoiceOption('gambling_bet', '🎲 Ставка'),
            ChoiceOption('gambling_prize', '🏆 Выигрыш'),
            ChoiceOption('lottery', '🎫 Лотерея'),
            ChoiceOption('lottery_prize', '🎁 Приз лотереи'),
            ChoiceOption('intellectual_activity', '🧠 Интеллектуальная деятельность'),
            ChoiceOption('payment', '💱 Платеж'),
            ChoiceOption('agent_commission', '🤝 Комиссия агента'),
            ChoiceOption('composite', '🧩 Композитный'),
            ChoiceOption('another', '📄 Другое'),
        ],
        'YOOKASSA_VAT_CODE': [
            ChoiceOption(1, '1 — НДС не облагается'),
            ChoiceOption(2, '2 — НДС 0%'),
            ChoiceOption(3, '3 — НДС 10%'),
            ChoiceOption(4, '4 — НДС 20%'),
            ChoiceOption(5, '5 — НДС 10/110'),
            ChoiceOption(6, '6 — НДС 20/120'),
            ChoiceOption(7, '7 — НДС 5%'),
            ChoiceOption(8, '8 — НДС 7%'),
            ChoiceOption(9, '9 — НДС 5/105'),
            ChoiceOption(10, '10 — НДС 7/107'),
            ChoiceOption(11, '11 — НДС 22%'),
            ChoiceOption(12, '12 — НДС 22/122'),
        ],
        'MULENPAY_LANGUAGE': [
            ChoiceOption('ru', '🇷🇺 Русский'),
            ChoiceOption('en', '🇬🇧 Английский'),
        ],
        'LOG_LEVEL': [
            ChoiceOption('DEBUG', '🐞 Debug'),
            ChoiceOption('INFO', 'ℹ️ Info'),
            ChoiceOption('WARNING', '⚠️ Warning'),
            ChoiceOption('ERROR', '❌ Error'),
            ChoiceOption('CRITICAL', '🔥 Critical'),
        ],
        'TRIAL_DISABLED_FOR': [
            ChoiceOption('none', '✅ Включён для всех'),
            ChoiceOption('email', '📧 Отключён для Email'),
            ChoiceOption('telegram', '📱 Отключён для Telegram'),
            ChoiceOption('all', '🚫 Отключён для всех'),
        ],
    }

    SETTING_HINTS: dict[str, dict[str, str]] = {
        # ===== DATABASE =====
        'DATABASE_MODE': {
            'description': 'Режим базы данных: auto — автовыбор (PostgreSQL в Docker, SQLite локально), postgresql — принудительно PostgreSQL, sqlite — принудительно SQLite.',
            'format': 'Выберите режим.',
            'example': 'auto | postgresql | sqlite',
        },
        # ===== CORE =====
        'BOT_USERNAME': {
            'description': 'Username бота без символа @. Автоопределяется при запуске.',
            'format': 'Строка без символа @.',
            'example': 'my_vpn_bot',
        },
        'SUPPORT_USERNAME': {
            'description': (
                'Ссылка на поддержку. Может быть Telegram username (например, @support) '
                'или полный URL (например, https://t.me/support_bot).'
            ),
            'format': 'Username с @ или полный URL.',
            'example': '@my_support или https://t.me/support_bot',
        },
        # ===== SUPPORT =====
        'SUPPORT_MENU_ENABLED': {
            'description': 'Показывать меню поддержки в интерфейсе бота.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'SUPPORT_SYSTEM_MODE': {
            'description': (
                'Режим системы поддержки: tickets — только тикеты, '
                'contact — только контакт поддержки, both — оба варианта.'
            ),
            'format': 'Выберите один из режимов.',
            'example': 'tickets | contact | both',
        },
        'SUPPORT_TICKET_SLA_ENABLED': {
            'description': 'Включить SLA для тикетов поддержки (напоминания о просроченных тикетах).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'SUPPORT_TICKET_SLA_MINUTES, SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES',
        },
        'SUPPORT_TICKET_SLA_MINUTES': {
            'description': 'Лимит времени для ответа модераторов на тикет в минутах.',
            'format': 'Целое число от 1 до 1440.',
            'example': '5',
            'warning': 'Слишком низкое значение может вызвать частые напоминания, слишком высокое — ухудшить SLA.',
            'dependencies': 'SUPPORT_TICKET_SLA_ENABLED, SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES',
        },
        'SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS': {
            'description': 'Интервал проверки SLA тикетов в секундах.',
            'format': 'Целое число секунд (int).',
            'example': '300',
            'dependencies': 'SUPPORT_TICKET_SLA_ENABLED',
        },
        'SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES': {
            'description': 'Кулдаун между напоминаниями SLA в минутах.',
            'format': 'Целое число минут (int).',
            'example': '30',
            'dependencies': 'SUPPORT_TICKET_SLA_ENABLED',
        },
        # ===== LOCALIZATION =====
        'DEFAULT_LANGUAGE': {
            'description': 'Язык по умолчанию для новых пользователей.',
            'format': 'Код языка (ru, en, ua, zh, fa).',
            'example': 'ru',
        },
        'AVAILABLE_LANGUAGES': {
            'description': 'Доступные языки интерфейса через запятую.',
            'format': 'Коды языков через запятую.',
            'example': 'ru,en,ua,zh,fa',
        },
        'LANGUAGE_SELECTION_ENABLED': {
            'description': 'Включить выбор языка при старте и кнопку в меню.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        # ===== CHANNEL =====
        'CHANNEL_SUB_ID': {
            'description': 'ID канала для проверки подписки. Для закрытых каналов используйте префикс -100.',
            'format': 'ID канала (число).',
            'example': '-1001234567890',
        },
        'CHANNEL_LINK': {
            'description': 'Ссылка на канал для кнопки подписки.',
            'format': 'URL или t.me ссылка.',
            'example': 'https://t.me/my_channel',
        },
        'CHANNEL_IS_REQUIRED_SUB': {
            'description': 'Требовать обязательную подписку на канал для использования бота.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': 'При включении пользователи без подписки не смогут использовать бота.',
            'dependencies': 'CHANNEL_SUB_ID, CHANNEL_LINK',
        },
        'CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE': {
            'description': 'Отключать триальные подписки при отписке от канала.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'CHANNEL_IS_REQUIRED_SUB',
        },
        'CHANNEL_REQUIRED_FOR_ALL': {
            'description': 'Требовать подписку на канал для ВСЕХ пользователей (включая платных).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': 'При включении даже платные пользователи должны быть подписаны на канал.',
            'dependencies': 'CHANNEL_IS_REQUIRED_SUB',
        },
        # ===== TIMEZONE =====
        'TIMEZONE': {
            'description': 'Часовой пояс для отображения времени и расписаний.',
            'format': 'Идентификатор часового пояса (например, Europe/Moscow, UTC).',
            'example': 'Europe/Moscow',
        },
        # ===== SALES_MODE =====
        'SALES_MODE': {
            'description': (
                'Режим продажи подписок. '
                '«Классический» — выбор периода из .env (PRICE_14_DAYS и т.д.). '
                '«Тарифы» — готовые тарифные планы из кабинета с серверами и лимитами.'
            ),
            'format': 'Выберите один из доступных режимов.',
            'example': 'classic | tariffs',
            'warning': (
                'При смене режима логика покупки подписки полностью меняется. '
                'В режиме «Тарифы» пользователи выбирают готовый тарифный план.'
            ),
        },
        'YOOKASSA_ENABLED': {
            'description': (
                'Включает оплату через YooKassa. Требует корректных идентификаторов магазина и секретного ключа.'
            ),
            'format': 'Булево значение: выберите "Включить" или "Выключить".',
            'example': 'Включено при полностью настроенной интеграции.',
            'warning': 'При включении без Shop ID и Secret Key пользователи увидят ошибки при оплате.',
            'dependencies': 'YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, YOOKASSA_RETURN_URL',
        },
        'YOOKASSA_SHOP_ID': {
            'description': 'Идентификатор магазина в YooKassa.',
            'format': 'Строка из личного кабинета YooKassa.',
            'example': '123456',
            'dependencies': 'YOOKASSA_ENABLED',
        },
        'YOOKASSA_SECRET_KEY': {
            'description': 'Секретный ключ магазина YooKassa.',
            'format': 'Строка из личного кабинета YooKassa.',
            'example': 'test_...',
            'warning': 'Храните ключ в секрете. Не публикуйте в открытых источниках.',
            'dependencies': 'YOOKASSA_ENABLED',
        },
        'YOOKASSA_RETURN_URL': {
            'description': 'URL для возврата пользователя после оплаты.',
            'format': 'Полный URL с https.',
            'example': 'https://your-domain.com/payment-success',
            'dependencies': 'YOOKASSA_ENABLED',
        },
        'YOOKASSA_SBP_ENABLED': {
            'description': 'Включить оплату через СБП (Систему быстрых платежей) в YooKassa.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': 'Требует активации СБП в личном кабинете YooKassa.',
            'dependencies': 'YOOKASSA_ENABLED',
        },
        'YOOKASSA_VAT_CODE': {
            'description': (
                'Код НДС для чеков: 1 — не облагается, 2 — 0%, 3 — 10%, 4 — 20%, '
                '5 — 10/110, 6 — 20/120, 7 — 5%, 8 — 7%, 9 — 5/105, 10 — 7/107, 11 — 22%, 12 — 22/122.'
            ),
            'format': 'Число от 1 до 12.',
            'example': '1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12',
            'dependencies': 'YOOKASSA_ENABLED',
        },
        'YOOKASSA_PAYMENT_MODE': {
            'description': 'Способ расчёта: full_payment — полная оплата, partial_payment — частичная, advance — аванс, full_prepayment — полная предоплата, partial_prepayment — частичная предоплата, credit — кредит, credit_payment — погашение кредита.',
            'format': 'Выберите из списка.',
            'example': 'full_payment | partial_payment | advance | full_prepayment | partial_prepayment | credit | credit_payment',
            'dependencies': 'YOOKASSA_ENABLED',
        },
        'YOOKASSA_PAYMENT_SUBJECT': {
            'description': 'Предмет расчёта: commodity — товар, excise — подакцизный, job — работа, service — услуга, gambling_bet — ставка, gambling_prize — выигрыш, lottery — лотерея, lottery_prize — приз, intellectual_activity — интеллектуальная деятельность, payment — платеж, agent_commission — комиссия агента, composite — композитный, another — другое.',
            'format': 'Выберите из списка.',
            'example': 'commodity | excise | job | service | gambling_bet | gambling_prize | lottery | lottery_prize | intellectual_activity | payment | agent_commission | composite | another',
            'dependencies': 'YOOKASSA_ENABLED',
        },
        'YOOKASSA_MIN_AMOUNT_KOPEKS': {
            'description': 'Минимальная сумма пополнения через YooKassa в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '5000',
            'dependencies': 'YOOKASSA_ENABLED',
        },
        'YOOKASSA_MAX_AMOUNT_KOPEKS': {
            'description': 'Максимальная сумма пополнения через YooKassa в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '1000000',
            'dependencies': 'YOOKASSA_ENABLED',
        },
        'YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED': {
            'description': 'Показывать кнопки быстрого выбора суммы пополнения.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'YOOKASSA_ENABLED',
        },
        # ===== PAYMENT GENERAL =====
        'PAYMENT_SERVICE_NAME': {
            'description': 'Название сервиса в описании платежей.',
            'format': 'Строка (str).',
            'example': 'Интернет-сервис',
            'warning': 'Используется в чеках и описаниях платежей. Избегайте слов-триггеров для платёжных систем.',
        },
        'PAYMENT_BALANCE_DESCRIPTION': {
            'description': 'Описание пополнения баланса в платёжных системах.',
            'format': 'Строка (str).',
            'example': 'Пополнение баланса',
        },
        'PAYMENT_SUBSCRIPTION_DESCRIPTION': {
            'description': 'Описание оплаты подписки в платёжных системах.',
            'format': 'Строка (str).',
            'example': 'Оплата подписки',
        },
        'PAYMENT_BALANCE_TEMPLATE': {
            'description': 'Шаблон описания пополнения. Плейсхолдеры: {service_name}, {description}.',
            'format': 'Строка с плейсхолдерами.',
            'example': '{service_name} - {description}',
        },
        'PAYMENT_SUBSCRIPTION_TEMPLATE': {
            'description': 'Шаблон описания подписки. Плейсхолдеры: {service_name}, {description}.',
            'format': 'Строка с плейсхолдерами.',
            'example': '{service_name} - {description}',
        },
        'DISABLE_TOPUP_BUTTONS': {
            'description': 'Отключить кнопки выбора суммы пополнения (оставить только ручной ввод).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'SUPPORT_TOPUP_ENABLED': {
            'description': 'Разрешить пополнение баланса через поддержку.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        # ===== SUBSCRIPTIONS_CORE =====
        'DEFAULT_DEVICE_LIMIT': {
            'description': 'Количество устройств по умолчанию при покупке платной подписки.',
            'format': 'Целое число от 1.',
            'example': '3',
        },
        'MAX_DEVICES_LIMIT': {
            'description': 'Максимум устройств, доступных к покупке. 0 = без лимита.',
            'format': 'Целое число (int).',
            'example': '15',
        },
        'PRICE_PER_DEVICE': {
            'description': 'Цена за дополнительное устройство в копейках. DEFAULT_DEVICE_LIMIT устройств идёт бесплатно.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '10000',
        },
        'DEVICES_SELECTION_ENABLED': {
            'description': 'Разрешает пользователям выбирать количество устройств при покупке и продлении подписки.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': 'При отключении пользователи не смогут докупать устройства из интерфейса бота.',
        },
        'DEVICES_SELECTION_DISABLED_AMOUNT': {
            'description': (
                'Лимит устройств, который автоматически назначается, когда выбор количества устройств выключен. '
                'Значение 0 отключает назначение устройств.'
            ),
            'format': 'Целое число от 0 и выше.',
            'example': '3',
            'warning': 'При 0 RemnaWave не получит лимит устройств, пользователям не показываются цифры в интерфейсе.',
        },
        'BASE_SUBSCRIPTION_PRICE': {
            'description': 'Базовая цена подписки в копейках (добавляется к стоимости периода).',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '100',
        },
        # ===== PERIODS =====
        'AVAILABLE_SUBSCRIPTION_PERIODS': {
            'description': 'Доступные периоды подписки в днях через запятую.',
            'format': 'Числа через запятую.',
            'example': '30,90,180,360',
        },
        'AVAILABLE_RENEWAL_PERIODS': {
            'description': 'Доступные периоды продления в днях через запятую.',
            'format': 'Числа через запятую.',
            'example': '30,90,180',
        },
        # ===== SUBSCRIPTION_PRICES =====
        'PRICE_14_DAYS': {
            'description': 'Цена подписки на 14 дней в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '7000',
        },
        'PRICE_30_DAYS': {
            'description': 'Цена подписки на 30 дней в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '10000',
        },
        'PRICE_60_DAYS': {
            'description': 'Цена подписки на 60 дней в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '18000',
        },
        'PRICE_90_DAYS': {
            'description': 'Цена подписки на 90 дней в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '25000',
        },
        'PRICE_180_DAYS': {
            'description': 'Цена подписки на 180 дней в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '45000',
        },
        'PRICE_360_DAYS': {
            'description': 'Цена подписки на 360 дней в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '80000',
        },
        'PAID_SUBSCRIPTION_USER_TAG': {
            'description': ('Тег, который бот ставит пользователю при покупке платной подписки в панели RemnaWave.'),
            'format': 'До 16 символов: заглавные A-Z, цифры и подчёркивание.',
            'example': 'PAID_USER',
            'warning': 'Если тег не задан или невалиден, существующий тег не будет изменён.',
            'dependencies': 'Оплата подписки и интеграция с RemnaWave',
        },
        'SIMPLE_SUBSCRIPTION_ENABLED': {
            'description': 'Показывает в меню пункт с быстрой покупкой подписки.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': 'Если остались не настроенные параметры, предложение может вести себя некорректно.',
        },
        'SIMPLE_SUBSCRIPTION_PERIOD_DAYS': {
            'description': 'Период подписки, который предлагается при быстрой покупке.',
            'format': 'Выберите один из доступных периодов.',
            'example': '30 дн. — 990 ₽',
            'warning': 'Не забудьте настроить цену периода в блоке «Стоимость тарифов».',
        },
        'SIMPLE_SUBSCRIPTION_DEVICE_LIMIT': {
            'description': 'Сколько устройств получит пользователь вместе с подпиской по быстрой покупке.',
            'format': 'Выберите число устройств.',
            'example': '2 устройства',
            'warning': 'Значение не должно превышать допустимый лимит в настройках подписок.',
        },
        'SIMPLE_SUBSCRIPTION_TRAFFIC_GB': {
            'description': 'Объём трафика, включённый в простую подписку (0 = безлимит).',
            'format': 'Выберите пакет трафика.',
            'example': 'Безлимит',
        },
        'SIMPLE_SUBSCRIPTION_SQUAD_UUID': {
            'description': (
                'Привязка быстрой подписки к конкретному скваду. Оставьте пустым для любого доступного сервера.'
            ),
            'format': 'Выберите сквад из списка или очистите значение.',
            'example': 'd4aa2b8c-9a36-4f31-93a2-6f07dad05fba',
            'warning': 'Убедитесь, что выбранный сквад активен и доступен для подписки.',
        },
        # ===== TRAFFIC =====
        'TRAFFIC_SELECTION_MODE': {
            'description': (
                'Режим выбора трафика: selectable — пользователь выбирает пакет, '
                'fixed — фиксированный лимит без выбора, fixed_with_topup — фиксированный с докупкой.'
            ),
            'format': 'Выберите один из режимов.',
            'example': 'selectable | fixed | fixed_with_topup',
            'warning': 'В режиме fixed пользователи не смогут выбирать и докупать трафик.',
        },
        'FIXED_TRAFFIC_LIMIT_GB': {
            'description': 'Фиксированный лимит трафика в ГБ (для режимов fixed и fixed_with_topup). 0 = безлимит.',
            'format': 'Целое число ГБ (int).',
            'example': '100',
            'dependencies': 'TRAFFIC_SELECTION_MODE=fixed или fixed_with_topup',
        },
        'DEFAULT_TRAFFIC_LIMIT_GB': {
            'description': 'Лимит трафика по умолчанию для подписок из админки.',
            'format': 'Целое число ГБ (int).',
            'example': '100',
        },
        'DEFAULT_TRAFFIC_RESET_STRATEGY': {
            'description': 'Стратегия сброса трафика: NO_RESET — без сброса, DAY — ежедневно, WEEK — еженедельно, MONTH — ежемесячно.',
            'format': 'Выберите из списка.',
            'example': 'NO_RESET | DAY | WEEK | MONTH',
        },
        'RESET_TRAFFIC_ON_PAYMENT': {
            'description': 'Сбрасывать трафик при каждой оплате подписки.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'TRAFFIC_TOPUP_ENABLED': {
            'description': 'Включить функцию докупки трафика к существующей подписке.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'BUY_TRAFFIC_BUTTON_VISIBLE': {
            'description': 'Показывать кнопку "Докупить трафик" в меню.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        # ===== TRAFFIC_PACKAGES =====
        'TRAFFIC_PACKAGES_CONFIG': {
            'description': (
                'Конфигурация пакетов трафика. Формат: гб:цена_в_копейках:enabled через запятую. '
                '0 ГБ = безлимит.'
            ),
            'format': 'Строка формата: 5:2000:true,10:3500:true,0:20000:true',
            'example': '5:2000:true,10:3500:true,25:7000:true,100:15000:true,0:20000:true',
            'warning': 'Некорректный формат будет проигнорирован.',
        },
        'TRAFFIC_TOPUP_PACKAGES_CONFIG': {
            'description': 'Отдельные пакеты для докупки трафика. Если пусто — используется TRAFFIC_PACKAGES_CONFIG.',
            'format': 'Формат как TRAFFIC_PACKAGES_CONFIG.',
            'example': '10:5000:true,25:10000:true,50:15000:true',
        },
        'TRAFFIC_RESET_PRICE_MODE': {
            'description': (
                'Режим расчёта цены сброса трафика: period — фиксированная цена, '
                'traffic — по текущему пакету, traffic_with_purchased — базовый + докупленный (рекомендуется).'
            ),
            'format': 'Выберите режим.',
            'example': 'traffic_with_purchased',
            'warning': 'Режим period может привести к абьюзу, если базовая цена низкая.',
        },
        'TRAFFIC_RESET_BASE_PRICE': {
            'description': 'Базовая цена сброса трафика в копейках. 0 = использовать PRICE_30_DAYS.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '500',
        },
        # ===== TRIAL =====
        'TRIAL_DURATION_DAYS': {
            'description': 'Длительность пробной подписки в днях.',
            'format': 'Целое число дней (int).',
            'example': '3',
        },
        'TRIAL_TRAFFIC_LIMIT_GB': {
            'description': 'Лимит трафика для триала в ГБ.',
            'format': 'Целое число ГБ (int).',
            'example': '10',
        },
        'TRIAL_DEVICE_LIMIT': {
            'description': 'Количество устройств для пробной подписки.',
            'format': 'Целое число (int).',
            'example': '1',
        },
        'TRIAL_PAYMENT_ENABLED': {
            'description': 'Включить платный триал (требует оплату для активации пробного периода).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'TRIAL_ACTIVATION_PRICE',
        },
        'TRIAL_ACTIVATION_PRICE': {
            'description': 'Цена активации триала в копейках. 0 = бесплатный триал.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '3500',
            'dependencies': 'TRIAL_PAYMENT_ENABLED=true',
        },
        'TRIAL_TARIFF_ID': {
            'description': (
                'ID тарифа для триала в режиме тарифов. 0 = использовать стандартные настройки. '
                'Параметры триала берутся из тарифа (traffic_limit_gb, device_limit, allowed_squads).'
            ),
            'format': 'Целое число (ID тарифа).',
            'example': '2',
            'dependencies': 'SALES_MODE=tariffs',
        },
        'TRIAL_ADD_REMAINING_DAYS_TO_PAID': {
            'description': 'Добавлять оставшиеся дни триала при покупке платной подписки.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'TRIAL_USER_TAG': {
            'description': (
                'Тег, который бот передаст пользователю при активации триальной подписки в панели RemnaWave.'
            ),
            'format': 'До 16 символов: заглавные A-Z, цифры и подчёркивание.',
            'example': 'TRIAL_USER',
            'warning': 'Неверный формат будет проигнорирован при создании пользователя.',
            'dependencies': 'Активация триала и включенная интеграция с RemnaWave',
        },
        'TRIAL_WARNING_HOURS': {
            'description': 'За сколько часов до окончания отправлять предупреждение о завершении триала.',
            'format': 'Целое число часов (int).',
            'example': '24',
        },
        'TRIAL_DISABLED_FOR': {
            'description': (
                'Отключить триал для определённых типов пользователей: none — доступен всем, '
                'email — отключён для email-пользователей, telegram — для Telegram, all — для всех.'
            ),
            'format': 'Выберите из списка.',
            'example': 'none | email | telegram | all',
        },
        # ===== REFERRAL =====
        'REFERRAL_PROGRAM_ENABLED': {
            'description': 'Включить реферальную программу.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'REFERRAL_MINIMUM_TOPUP_KOPEKS': {
            'description': 'Минимальное пополнение для активации реферального бонуса в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '10000',
        },
        'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS': {
            'description': 'Бонус рефералу при первом пополнении в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '10000',
        },
        'REFERRAL_INVITER_BONUS_KOPEKS': {
            'description': 'Бонус пригласившему при первом пополнении реферала в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '10000',
        },
        'REFERRAL_COMMISSION_PERCENT': {
            'description': 'Процент комиссии с пополнений рефералов.',
            'format': 'Целое число от 0 до 100.',
            'example': '25',
        },
        'REFERRAL_NOTIFICATIONS_ENABLED': {
            'description': 'Включить уведомления о реферальных начислениях.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'REFERRAL_NOTIFICATION_RETRY_ATTEMPTS': {
            'description': 'Количество попыток отправки уведомления о реферальном бонусе.',
            'format': 'Целое число (int).',
            'example': '3',
        },
        'REFERRAL_WITHDRAWAL_ENABLED': {
            'description': 'Включить возможность вывода реферального баланса.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS': {
            'description': 'Минимальная сумма вывода реферального баланса в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '50000',
            'dependencies': 'REFERRAL_WITHDRAWAL_ENABLED',
        },
        'REFERRAL_WITHDRAWAL_COOLDOWN_DAYS': {
            'description': 'Интервал между запросами на вывод в днях.',
            'format': 'Целое число дней (int).',
            'example': '30',
            'dependencies': 'REFERRAL_WITHDRAWAL_ENABLED',
        },
        'REFERRAL_WITHDRAWAL_ONLY_REFERRAL_BALANCE': {
            'description': 'Выводить только реферальный баланс (true) или весь баланс (false).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'REFERRAL_WITHDRAWAL_ENABLED',
        },
        # ===== AUTOPAY =====
        'ENABLE_AUTOPAY': {
            'description': 'Глобально включить функцию автопродления подписок.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'AUTOPAY_WARNING_DAYS': {
            'description': 'Дни до окончания подписки для отправки предупреждений (через запятую).',
            'format': 'Числа через запятую.',
            'example': '3,1',
        },
        'DEFAULT_AUTOPAY_ENABLED': {
            'description': 'Включить автопродление для новых пользователей по умолчанию.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'ENABLE_AUTOPAY',
        },
        'DEFAULT_AUTOPAY_DAYS_BEFORE': {
            'description': 'За сколько дней до окончания автоматически продлевать.',
            'format': 'Целое число дней (int).',
            'example': '3',
            'dependencies': 'ENABLE_AUTOPAY',
        },
        'MIN_BALANCE_FOR_AUTOPAY_KOPEKS': {
            'description': 'Минимальный баланс для автопродления в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '10000',
            'dependencies': 'ENABLE_AUTOPAY',
        },
        'SUBSCRIPTION_RENEWAL_BALANCE_THRESHOLD_KOPEKS': {
            'description': 'Порог баланса для фильтра «готовы к продлению» в копейках.',
            'format': 'Целое число в копейках (int). 100 = 1₽.',
            'example': '20000',
        },
        'CRYPTOBOT_ENABLED': {
            'description': 'Разрешает принимать криптоплатежи через CryptoBot.',
            'format': 'Булево значение (bool).',
            'example': 'Включите после указания токена API и секрета вебхука.',
            'warning': 'Пустой токен или неверный вебхук приведут к отказам платежей.',
            'dependencies': 'CRYPTOBOT_API_TOKEN, CRYPTOBOT_WEBHOOK_SECRET',
        },
        'CRYPTOBOT_API_TOKEN': {
            'description': 'API токен CryptoBot из @CryptoBot.',
            'format': 'Строка токена.',
            'example': '123456789:AAzQcZWQqQAbsfgPnOLr4FHC8Doa4L7KryC',
            'dependencies': 'CRYPTOBOT_ENABLED',
        },
        'CRYPTOBOT_WEBHOOK_SECRET': {
            'description': 'Секрет для проверки подписи вебхуков CryptoBot.',
            'format': 'Строка (str).',
            'example': 'your_webhook_secret_here',
            'dependencies': 'CRYPTOBOT_ENABLED',
        },
        'CRYPTOBOT_TESTNET': {
            'description': 'Использовать тестовую сеть CryptoBot.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'CRYPTOBOT_ENABLED',
        },
        'CRYPTOBOT_DEFAULT_ASSET': {
            'description': 'Криптовалюта по умолчанию.',
            'format': 'Код криптовалюты.',
            'example': 'USDT',
            'dependencies': 'CRYPTOBOT_ENABLED',
        },
        'CRYPTOBOT_ASSETS': {
            'description': 'Доступные криптовалюты через запятую.',
            'format': 'Коды через запятую.',
            'example': 'USDT,TON,BTC,ETH',
            'dependencies': 'CRYPTOBOT_ENABLED',
        },
        'CRYPTOBOT_INVOICE_EXPIRES_HOURS': {
            'description': 'Время жизни счёта в часах.',
            'format': 'Целое число часов (int).',
            'example': '24',
            'dependencies': 'CRYPTOBOT_ENABLED',
        },
        # ===== NOTIFICATIONS =====
        'ENABLE_NOTIFICATIONS': {
            'description': 'Включить отправку уведомлений пользователям (об истечении подписки, предупреждения и т.д.).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'NOTIFICATION_RETRY_ATTEMPTS': {
            'description': 'Количество попыток повторной отправки уведомления при ошибке.',
            'format': 'Целое число (int).',
            'example': '3',
        },
        'NOTIFICATION_CACHE_HOURS': {
            'description': 'Время кэширования уведомлений в часах (защита от дублирования).',
            'format': 'Целое число часов (int).',
            'example': '24',
        },
        # ===== ADMIN_NOTIFICATIONS =====
        'ADMIN_NOTIFICATIONS_ENABLED': {
            'description': 'Включить отправку уведомлений администраторам о событиях бота.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'ADMIN_NOTIFICATIONS_CHAT_ID',
        },
        'ADMIN_NOTIFICATIONS_CHAT_ID': {
            'description': 'ID чата/канала для уведомлений. Для закрытых каналов используйте префикс -100.',
            'format': 'ID чата (число).',
            'example': '-1001234567890',
            'dependencies': 'ADMIN_NOTIFICATIONS_ENABLED',
        },
        'ADMIN_NOTIFICATIONS_TOPIC_ID': {
            'description': 'ID топика для уведомлений (для форумов/групп с топиками).',
            'format': 'Целое число или пусто.',
            'example': '123',
            'dependencies': 'ADMIN_NOTIFICATIONS_ENABLED',
        },
        'PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED': {
            'description': (
                'Запускает фоновую проверку ожидающих пополнений и повторно обращается '
                'к платёжным провайдерам без участия администратора.'
            ),
            'format': 'Булево значение (bool).',
            'example': 'Включено, чтобы автоматически перепроверять зависшие платежи.',
            'warning': 'Требует активных интеграций YooKassa, {mulenpay_name}, PayPalych, WATA или CryptoBot.',
        },
        'PAYMENT_VERIFICATION_AUTO_CHECK_INTERVAL_MINUTES': {
            'description': ('Интервал между автоматическими проверками ожидающих пополнений в минутах.'),
            'format': 'Целое число не меньше 1.',
            'example': '10',
            'warning': 'Слишком малый интервал может привести к частым обращениям к платёжным API.',
            'dependencies': 'PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED',
        },
        'BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED': {
            'description': ('Включает применение базовых скидок на периоды подписок в групповых промо.'),
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': 'Скидки применяются только если указаны корректные пары периодов и процентов.',
        },
        'BASE_PROMO_GROUP_PERIOD_DISCOUNTS': {
            'description': ('Список скидок для групп: каждая пара задаёт дни периода и процент скидки.'),
            'format': 'Через запятую пары вида &lt;дней&gt;:&lt;скидка&gt;.',
            'example': '30:10,60:20,90:30,180:50,360:65',
            'warning': 'Некорректные записи будут проигнорированы. Процент ограничен 0-100.',
        },
        'AUTO_PURCHASE_AFTER_TOPUP_ENABLED': {
            'description': (
                'При достаточном балансе автоматически оформляет сохранённую подписку сразу после пополнения.'
            ),
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': ('Используйте с осторожностью: средства будут списаны мгновенно, если корзина найдена.'),
        },
        'SUPPORT_TICKET_SLA_MINUTES': {
            'description': 'Лимит времени для ответа модераторов на тикет в минутах.',
            'format': 'Целое число от 1 до 1440 (int).',
            'example': '5 | 15 | 30 | 60',
            'warning': 'Слишком низкое значение может вызвать частые напоминания, слишком высокое — ухудшить SLA.',
            'dependencies': 'SUPPORT_TICKET_SLA_ENABLED, SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES',
        },
        'DISPLAY_NAME_BANNED_KEYWORDS': {
            'description': (
                'Список слов и фрагментов, при наличии которых в отображаемом имени пользователь будет заблокирован.'
            ),
            'format': 'Перечислите ключевые слова через запятую или с новой строки.',
            'example': 'support, security, служебн',
            'warning': 'Слишком агрессивные фильтры могут блокировать добросовестных пользователей.',
            'dependencies': 'Фильтр отображаемых имен',
        },
        # ===== INTERFACE =====
        'MAIN_MENU_MODE': {
            'description': (
                'Режим главного меню: default — классический (все кнопки внутри Telegram), '
                'cabinet — режим с MiniApp кабинетом.'
            ),
            'format': 'Выберите режим.',
            'example': 'default | cabinet',
            'dependencies': 'MINIAPP_CUSTOM_URL для режима cabinet',
        },
        'CABINET_BUTTON_STYLE': {
            'description': 'Стиль кнопок в режиме Cabinet (Bot API 9.4): primary — синий, success — зелёный, danger — красный, пусто — по секциям.',
            'format': 'Выберите стиль или оставьте пустым для авто.',
            'example': '(пусто) | primary | success | danger',
            'dependencies': 'MAIN_MENU_MODE=cabinet',
        },
        'PRICE_ROUNDING_ENABLED': {
            'description': 'Округление цен при отображении (≤50 коп вниз, >50 коп вверх).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        # ===== INTERFACE_BRANDING =====
        'ENABLE_LOGO_MODE': {
            'description': 'Показывать логотип в сообщениях бота.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'LOGO_FILE': {
            'description': 'Путь к файлу логотипа.',
            'format': 'Имя файла в корне проекта.',
            'example': 'vpn_logo.png',
            'dependencies': 'ENABLE_LOGO_MODE',
        },
        # ===== INTERFACE_SUBSCRIPTION =====
        'HIDE_SUBSCRIPTION_LINK': {
            'description': 'Скрыть ссылку подключения в информации о подписке.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'DISABLE_WEB_PAGE_PREVIEW': {
            'description': 'Отключить превью ссылок в сообщениях бота.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        # ===== CONNECT_BUTTON =====
        'CONNECT_BUTTON_MODE': {
            'description': (
                'Режим кнопки «Подключиться»: guide — гайд, miniapp_subscription — MiniApp подписка, '
                'miniapp_custom — кастомный URL, link — прямая ссылка, happ_cryptolink — Happ CryptoLink.'
            ),
            'format': 'Выберите режим.',
            'example': 'guide | miniapp_subscription | miniapp_custom | link | happ_cryptolink',
        },
        'MINIAPP_CUSTOM_URL': {
            'description': 'Кастомный URL для MiniApp (обязателен при CONNECT_BUTTON_MODE=miniapp_custom).',
            'format': 'Полный URL с https.',
            'example': 'https://app.example.com',
            'dependencies': 'CONNECT_BUTTON_MODE=miniapp_custom',
        },
        # ===== MINIAPP =====
        'MINIAPP_PURCHASE_URL': {
            'description': 'URL страницы покупки в MiniApp.',
            'format': 'Полный URL с https.',
            'example': 'https://app.example.com/buy',
        },
        'MINIAPP_STATIC_PATH': {
            'description': 'Путь к статическим файлам MiniApp.',
            'format': 'Относительный путь.',
            'example': 'miniapp',
        },
        'MINIAPP_SERVICE_NAME_EN': {
            'description': 'Название сервиса в MiniApp на английском.',
            'format': 'Строка (str).',
            'example': 'My VPN Service',
        },
        'MINIAPP_SERVICE_NAME_RU': {
            'description': 'Название сервиса в MiniApp на русском.',
            'format': 'Строка (str).',
            'example': 'Мой VPN Сервис',
        },
        'MINIAPP_SERVICE_DESCRIPTION_EN': {
            'description': 'Описание сервиса в MiniApp на английском.',
            'format': 'Строка (str).',
            'example': 'Secure & Fast Connection',
        },
        'MINIAPP_SERVICE_DESCRIPTION_RU': {
            'description': 'Описание сервиса в MiniApp на русском.',
            'format': 'Строка (str).',
            'example': 'Безопасное и быстрое подключение',
        },
        'MINIAPP_TICKETS_ENABLED': {
            'description': 'Включить раздел тикетов в MiniApp.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'MINIAPP_SUPPORT_TYPE': {
            'description': 'Тип поддержки в MiniApp: tickets — тикеты, profile — профиль, url — кастомный URL.',
            'format': 'Выберите тип.',
            'example': 'tickets | profile | url',
        },
        'MINIAPP_SUPPORT_URL': {
            'description': 'Кастомный URL для поддержки в MiniApp (при MINIAPP_SUPPORT_TYPE=url).',
            'format': 'Полный URL.',
            'example': 'https://support.example.com',
            'dependencies': 'MINIAPP_SUPPORT_TYPE=url',
        },
        'CABINET_REMNA_SUB_CONFIG': {
            'description': (
                'UUID конфигурации страницы подписки из RemnaWave. '
                'Позволяет синхронизировать список приложений напрямую из панели.'
            ),
            'format': 'UUID конфигурации из раздела Subscription Page Configs в RemnaWave.',
            'example': 'd4aa2b8c-9a36-4f31-93a2-6f07dad05fba',
            'warning': 'Убедитесь, что конфигурация существует в панели и содержит нужные приложения.',
            'dependencies': 'Настроенное подключение к RemnaWave API',
        },
        # ===== HAPP =====
        'CONNECT_BUTTON_HAPP_DOWNLOAD_ENABLED': {
            'description': 'Показывать кнопки скачивания Happ в режиме happ_cryptolink.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'CONNECT_BUTTON_MODE=happ_cryptolink',
        },
        'HAPP_CRYPTOLINK_REDIRECT_TEMPLATE': {
            'description': 'Шаблон URL для редиректа Happ (т.к. ссылки happ:// не поддерживаются Telegram).',
            'format': 'URL с параметром redirect_to=',
            'example': 'https://sub.domain.com/redirect/?redirect_to=',
            'warning': 'Без этой ссылки кнопка «Подключиться» не будет работать.',
            'dependencies': 'CONNECT_BUTTON_MODE=happ_cryptolink',
        },
        'HAPP_DOWNLOAD_LINK_IOS': {
            'description': 'Ссылка на скачивание Happ для iOS.',
            'format': 'URL App Store.',
            'example': 'https://apps.apple.com/app/happ',
            'dependencies': 'CONNECT_BUTTON_HAPP_DOWNLOAD_ENABLED',
        },
        'HAPP_DOWNLOAD_LINK_ANDROID': {
            'description': 'Ссылка на скачивание Happ для Android.',
            'format': 'URL Google Play или APK.',
            'example': 'https://play.google.com/store/apps/details?id=happ',
            'dependencies': 'CONNECT_BUTTON_HAPP_DOWNLOAD_ENABLED',
        },
        'HAPP_DOWNLOAD_LINK_MACOS': {
            'description': 'Ссылка на скачивание Happ для macOS.',
            'format': 'URL.',
            'example': 'https://github.com/happ/releases/macos',
            'dependencies': 'CONNECT_BUTTON_HAPP_DOWNLOAD_ENABLED',
        },
        'HAPP_DOWNLOAD_LINK_WINDOWS': {
            'description': 'Ссылка на скачивание Happ для Windows.',
            'format': 'URL.',
            'example': 'https://github.com/happ/releases/windows',
            'dependencies': 'CONNECT_BUTTON_HAPP_DOWNLOAD_ENABLED',
        },
        'HAPP_DOWNLOAD_LINK_PC': {
            'description': 'Универсальная ссылка для ПК (если MACOS и WINDOWS не заданы отдельно).',
            'format': 'URL.',
            'example': 'https://github.com/happ/releases',
            'dependencies': 'CONNECT_BUTTON_HAPP_DOWNLOAD_ENABLED',
        },
        # ===== SKIP =====
        'SKIP_RULES_ACCEPT': {
            'description': 'Пропустить принятие правил при старте бота.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'SKIP_REFERRAL_CODE': {
            'description': 'Пропустить запрос реферального кода при регистрации.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        # ===== ADDITIONAL =====
        'APP_CONFIG_PATH': {
            'description': 'Путь к конфигурации приложений (app-config.json).',
            'format': 'Относительный или абсолютный путь.',
            'example': 'app-config.json',
        },
        'ENABLE_DEEP_LINKS': {
            'description': 'Включить deep links для бота.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'REMNAWAVE_API_URL': {
            'description': 'Базовый адрес панели RemnaWave, с которой синхронизируется бот.',
            'format': 'Полный URL вида https://panel.example.com.',
            'example': 'https://panel.remnawave.net',
            'warning': 'Недоступный адрес приведет к ошибкам при управлении VPN-учетками.',
            'dependencies': 'REMNAWAVE_API_KEY или REMNAWAVE_USERNAME/REMNAWAVE_PASSWORD',
        },
        'REMNAWAVE_API_KEY': {
            'description': 'API ключ для авторизации в панели RemnaWave.',
            'format': 'Строка ключа из панели.',
            'example': 'your_api_key_here',
            'dependencies': 'REMNAWAVE_AUTH_TYPE=api_key',
        },
        'REMNAWAVE_AUTH_TYPE': {
            'description': 'Тип авторизации в панели: api_key — API ключ, basic_auth — Basic Auth с логином и паролем.',
            'format': 'Выберите тип.',
            'example': 'api_key | basic_auth',
        },
        'REMNAWAVE_USERNAME': {
            'description': 'Имя пользователя для Basic Auth в панели.',
            'format': 'Строка (str).',
            'example': 'admin',
            'dependencies': 'REMNAWAVE_AUTH_TYPE=basic_auth',
        },
        'REMNAWAVE_PASSWORD': {
            'description': 'Пароль для Basic Auth в панели.',
            'format': 'Строка (str).',
            'example': 'password',
            'dependencies': 'REMNAWAVE_AUTH_TYPE=basic_auth',
        },
        'REMNAWAVE_SECRET_KEY': {
            'description': 'Секретный ключ (для панелей установленных скриптом eGames). Формат: XXXXXXX:DDDDDDDD.',
            'format': 'Строка (str).',
            'example': 'ABC1234:99887766',
        },
        'REMNAWAVE_USER_DELETE_MODE': {
            'description': 'Режим удаления пользователей из панели: delete — полностью удалить, disable — только деактивировать.',
            'format': 'Выберите режим.',
            'example': 'delete | disable',
        },
        'REMNAWAVE_AUTO_SYNC_ENABLED': {
            'description': 'Автоматически запускает синхронизацию пользователей и серверов с панелью RemnaWave.',
            'format': 'Булево значение (bool).',
            'example': 'Включено при корректно настроенных API-ключах.',
            'warning': 'При включении без расписания синхронизация не будет выполнена.',
            'dependencies': 'REMNAWAVE_AUTO_SYNC_TIMES',
        },
        'REMNAWAVE_AUTO_SYNC_TIMES': {
            'description': ('Список времени в формате HH:MM, когда запускается автосинхронизация в течение суток.'),
            'format': 'Перечислите время через запятую или с новой строки (например, 03:00, 15:00).',
            'example': '03:00, 15:00',
            'warning': (
                'Минимальный интервал между запусками не ограничен, но слишком частые синхронизации нагружают панель.'
            ),
            'dependencies': 'REMNAWAVE_AUTO_SYNC_ENABLED',
        },
        'REMNAWAVE_USER_DESCRIPTION_TEMPLATE': {
            'description': (
                'Шаблон текста, который бот передает в поле Description при создании '
                'или обновлении пользователя в панели RemnaWave.'
            ),
            'format': ('Доступные плейсхолдеры: {full_name}, {username}, {username_clean}, {telegram_id}.'),
            'example': 'Bot user: {full_name} {username}',
            'warning': 'Плейсхолдер {username} автоматически очищается, если у пользователя нет @username.',
        },
        'REMNAWAVE_USER_USERNAME_TEMPLATE': {
            'description': (
                'Шаблон имени пользователя, которое создаётся в панели RemnaWave для телеграм-пользователя.'
            ),
            'format': ('Доступные плейсхолдеры: {full_name}, {username}, {username_clean}, {telegram_id}.'),
            'example': 'vpn_{username_clean}_{telegram_id}',
            'warning': (
                'Недопустимые символы автоматически заменяются на подчёркивания. '
                'Если результат пустой, используется user_{telegram_id}.'
            ),
        },
        # ===== REMNAWAVE_WEBHOOK =====
        'REMNAWAVE_WEBHOOK_ENABLED': {
            'description': 'Включить приём вебхуков от панели RemnaWave (real-time события).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'REMNAWAVE_WEBHOOK_SECRET (минимум 32 символа)',
        },
        'REMNAWAVE_WEBHOOK_PATH': {
            'description': 'Путь для приёма вебхуков от RemnaWave.',
            'format': 'Путь начинающийся с /.',
            'example': '/remnawave-webhook',
            'dependencies': 'REMNAWAVE_WEBHOOK_ENABLED',
        },
        'REMNAWAVE_WEBHOOK_SECRET': {
            'description': 'Общий секрет для подписи HMAC-SHA256 (минимум 32 символа).',
            'format': 'Строка минимум 32 символа. Сгенерируйте: openssl rand -hex 32',
            'example': 'your_32_char_or_longer_secret_here',
            'warning': 'Этот же секрет указывается в панели RemnaWave при создании вебхука.',
            'dependencies': 'REMNAWAVE_WEBHOOK_ENABLED',
        },
        # ===== SERVER_STATUS =====
        'SERVER_STATUS_MODE': {
            'description': (
                'Режим отображения статуса серверов: disabled — отключено, '
                'external_link — внешняя ссылка, external_link_miniapp — MiniApp, xray — XrayChecker.'
            ),
            'format': 'Выберите режим.',
            'example': 'disabled | external_link | external_link_miniapp | xray',
        },
        'SERVER_STATUS_EXTERNAL_URL': {
            'description': 'URL внешнего мониторинга (для режимов external_link и external_link_miniapp).',
            'format': 'Полный URL.',
            'example': 'https://status.example.com',
            'dependencies': 'SERVER_STATUS_MODE=external_link или external_link_miniapp',
        },
        'SERVER_STATUS_METRICS_URL': {
            'description': 'URL метрик XrayChecker (для режима xray).',
            'format': 'Полный URL.',
            'example': 'https://xray.example.com/metrics',
            'dependencies': 'SERVER_STATUS_MODE=xray',
        },
        'SERVER_STATUS_METRICS_USERNAME': {
            'description': 'Имя пользователя для Basic Auth к метрикам.',
            'format': 'Строка (str).',
            'example': 'admin',
            'dependencies': 'SERVER_STATUS_MODE=xray',
        },
        'SERVER_STATUS_METRICS_PASSWORD': {
            'description': 'Пароль для Basic Auth к метрикам.',
            'format': 'Строка (str).',
            'example': 'password',
            'dependencies': 'SERVER_STATUS_MODE=xray',
        },
        'SERVER_STATUS_METRICS_VERIFY_SSL': {
            'description': 'Проверять SSL сертификат при запросе метрик.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'SERVER_STATUS_MODE=xray',
        },
        'SERVER_STATUS_REQUEST_TIMEOUT': {
            'description': 'Таймаут запроса к метрикам в секундах.',
            'format': 'Целое число секунд (int).',
            'example': '10',
            'dependencies': 'SERVER_STATUS_MODE=xray',
        },
        'SERVER_STATUS_ITEMS_PER_PAGE': {
            'description': 'Количество серверов на странице в режиме интеграции.',
            'format': 'Целое число (int).',
            'example': '10',
            'dependencies': 'SERVER_STATUS_MODE=xray',
        },
        # ===== MONITORING =====
        'MONITORING_INTERVAL': {
            'description': 'Интервал мониторинга в секундах.',
            'format': 'Целое число секунд (int).',
            'example': '60',
        },
        'MONITORING_LOGS_RETENTION_DAYS': {
            'description': 'Время хранения логов мониторинга в днях.',
            'format': 'Целое число дней (int).',
            'example': '30',
        },
        # ===== TRAFFIC MONITORING =====
        'TRAFFIC_MONITORING_ENABLED': {
            'description': 'Включить мониторинг трафика пользователей.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'Redis, TRAFFIC_MONITORING_INTERVAL_HOURS',
        },
        'TRAFFIC_THRESHOLD_GB_PER_DAY': {
            'description': 'Порог трафика в ГБ за сутки для уведомлений.',
            'format': 'Число с плавающей точкой (float).',
            'example': '10.0 | 50.0 | 100.5',
        },
        'TRAFFIC_MONITORING_INTERVAL_HOURS': {
            'description': 'Интервал проверки трафика в часах.',
            'format': 'Целое число (int).',
            'example': '24',
        },
        'TRAFFIC_FAST_CHECK_ENABLED': {
            'description': 'Включить быструю проверку трафика.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'TRAFFIC_FAST_CHECK_INTERVAL_MINUTES, TRAFFIC_FAST_CHECK_THRESHOLD_GB',
        },
        'TRAFFIC_FAST_CHECK_INTERVAL_MINUTES': {
            'description': 'Интервал быстрой проверки трафика в минутах.',
            'format': 'Целое число (int).',
            'example': '10',
            'dependencies': 'TRAFFIC_FAST_CHECK_ENABLED',
        },
        'TRAFFIC_FAST_CHECK_THRESHOLD_GB': {
            'description': 'Порог дельты трафика в ГБ для быстрой проверки.',
            'format': 'Число с плавающей точкой (float).',
            'example': '5.0 | 10.0 | 25.5',
            'dependencies': 'TRAFFIC_FAST_CHECK_ENABLED',
        },
        'TRAFFIC_DAILY_CHECK_ENABLED': {
            'description': 'Включить суточную проверку трафика.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'TRAFFIC_DAILY_CHECK_TIME, TRAFFIC_DAILY_THRESHOLD_GB',
        },
        'TRAFFIC_DAILY_CHECK_TIME': {
            'description': 'Время суточной проверки трафика (UTC).',
            'format': 'Строка времени HH:MM.',
            'example': '00:00 | 03:00 | 12:00',
            'dependencies': 'TRAFFIC_DAILY_CHECK_ENABLED',
        },
        'TRAFFIC_DAILY_THRESHOLD_GB': {
            'description': 'Порог суточного трафика в ГБ для уведомления.',
            'format': 'Число с плавающей точкой (float).',
            'example': '50.0 | 100.0 | 250.5',
            'dependencies': 'TRAFFIC_DAILY_CHECK_ENABLED',
        },
        'TRAFFIC_MONITORED_NODES': {
            'description': 'UUID нод для мониторинга через запятую. Пусто = все ноды.',
            'format': 'Строка UUID через запятую или пусто.',
            'example': 'uuid1,uuid2,uuid3',
        },
        'TRAFFIC_IGNORED_NODES': {
            'description': 'UUID нод для исключения из мониторинга.',
            'format': 'Строка UUID через запятую.',
            'example': 'uuid1,uuid2',
        },
        'TRAFFIC_EXCLUDED_USER_UUIDS': {
            'description': 'UUID пользователей для исключения из мониторинга.',
            'format': 'Строка UUID через запятую.',
            'example': 'uuid1,uuid2',
        },
        'TRAFFIC_CHECK_BATCH_SIZE': {
            'description': 'Размер батча для получения пользователей.',
            'format': 'Целое число (int).',
            'example': '1000',
        },
        'TRAFFIC_CHECK_CONCURRENCY': {
            'description': 'Количество параллельных запросов.',
            'format': 'Целое число (int).',
            'example': '10',
        },
        'TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES': {
            'description': 'Кулдаун уведомлений по одному пользователю.',
            'format': 'Целое число минут (int).',
            'example': '60',
        },
        'TRAFFIC_SNAPSHOT_TTL_HOURS': {
            'description': 'TTL для snapshot трафика в Redis.',
            'format': 'Целое число часов (int).',
            'example': '24',
        },
        'SUSPICIOUS_NOTIFICATIONS_TOPIC_ID': {
            'description': 'ID топика для уведомлений о подозрительном трафике.',
            'format': 'Целое число или пусто (int | None).',
            'example': '123',
        },
        'INACTIVE_USER_DELETE_MONTHS': {
            'description': 'Через сколько месяцев неактивности удалять пользователей.',
            'format': 'Целое число месяцев (int).',
            'example': '3',
        },
        # ===== MAINTENANCE =====
        'MAINTENANCE_MODE': {
            'description': 'Переводит бота в режим технического обслуживания и скрывает действия для пользователей.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': 'Не забудьте отключить после завершения работ, иначе бот останется недоступен.',
            'dependencies': 'MAINTENANCE_MESSAGE, MAINTENANCE_CHECK_INTERVAL',
        },
        'MAINTENANCE_MESSAGE': {
            'description': 'Сообщение для пользователей в режиме техработ.',
            'format': 'Строка (str).',
            'example': 'Сервис временно недоступен. Попробуйте через 30 минут.',
        },
        'MAINTENANCE_CHECK_INTERVAL': {
            'description': 'Интервал проверки доступности панели в секундах.',
            'format': 'Целое число секунд (int).',
            'example': '30 | 60 | 120',
        },
        'MAINTENANCE_AUTO_ENABLE': {
            'description': 'Автоматически включать режим техработ при недоступности панели.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'MAINTENANCE_MONITORING_ENABLED': {
            'description': 'Управляет автоматическим запуском мониторинга панели Remnawave при старте бота.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': 'При отключении мониторинг можно запустить вручную из панели администратора.',
            'dependencies': 'MAINTENANCE_CHECK_INTERVAL',
        },
        'MAINTENANCE_RETRY_ATTEMPTS': {
            'description': 'Сколько раз повторять проверку панели Remnawave перед фиксацией недоступности.',
            'format': 'Целое число (int) не меньше 1.',
            'example': '1 | 3 | 5',
            'warning': 'Большие значения увеличивают время реакции на реальные сбои, но помогают избежать ложных срабатываний.',
            'dependencies': 'MAINTENANCE_CHECK_INTERVAL',
        },
        # ===== BACKUP =====
        'BACKUP_AUTO_ENABLED': {
            'description': 'Включить автоматическое резервное копирование базы данных.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'BACKUP_INTERVAL_HOURS': {
            'description': 'Интервал между бэкапами в часах.',
            'format': 'Целое число часов (int).',
            'example': '24',
            'dependencies': 'BACKUP_AUTO_ENABLED',
        },
        'BACKUP_TIME': {
            'description': 'Время создания бэкапа (формат HH:MM).',
            'format': 'Время HH:MM.',
            'example': '03:00',
            'dependencies': 'BACKUP_AUTO_ENABLED',
        },
        'BACKUP_MAX_KEEP': {
            'description': 'Максимальное количество хранимых бэкапов.',
            'format': 'Целое число (int).',
            'example': '7',
            'dependencies': 'BACKUP_AUTO_ENABLED',
        },
        'BACKUP_COMPRESSION': {
            'description': 'Сжимать бэкапы.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'BACKUP_AUTO_ENABLED',
        },
        'BACKUP_INCLUDE_LOGS': {
            'description': 'Включать логи в бэкап.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'BACKUP_AUTO_ENABLED',
        },
        'BACKUP_LOCATION': {
            'description': 'Путь для хранения бэкапов.',
            'format': 'Абсолютный или относительный путь.',
            'example': '/app/data/backups',
            'dependencies': 'BACKUP_AUTO_ENABLED',
        },
        'BACKUP_SEND_ENABLED': {
            'description': 'Отправлять бэкапы в Telegram канал.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'BACKUP_SEND_CHAT_ID',
        },
        'BACKUP_SEND_CHAT_ID': {
            'description': 'ID канала для отправки бэкапов.',
            'format': 'ID чата (число).',
            'example': '-1001234567890',
            'dependencies': 'BACKUP_SEND_ENABLED',
        },
        'EXTERNAL_ADMIN_TOKEN': {
            'description': 'Приватный токен, который использует внешняя админка для проверки запросов.',
            'format': 'Значение генерируется автоматически из username бота и его токена и доступно только для чтения.',
            'example': 'Генерируется автоматически',
            'warning': 'Токен обновится при смене username или токена бота.',
            'dependencies': 'Username телеграм-бота, токен бота',
        },
        'EXTERNAL_ADMIN_TOKEN_BOT_ID': {
            'description': 'Идентификатор телеграм-бота, с которым связан токен внешней админки.',
            'format': 'Проставляется автоматически после первого запуска и не редактируется вручную.',
            'example': '123456789',
            'warning': 'Несовпадение ID блокирует обновление токена, предотвращая его подмену на другом боте.',
            'dependencies': 'Результат вызова getMe() в Telegram Bot API',
        },
        # ===== WEB_API =====
        'WEB_API_ENABLED': {
            'description': 'Включить Web API для внешних интеграций.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEB_API_HOST': {
            'description': 'Хост для прослушивания Web API.',
            'format': 'IP адрес.',
            'example': '0.0.0.0',
            'dependencies': 'WEB_API_ENABLED',
        },
        'WEB_API_PORT': {
            'description': 'Порт для Web API.',
            'format': 'Целое число (int).',
            'example': '8080',
            'dependencies': 'WEB_API_ENABLED',
        },
        'WEB_API_WORKERS': {
            'description': 'Количество воркеров Web API.',
            'format': 'Целое число (int).',
            'example': '1',
            'dependencies': 'WEB_API_ENABLED',
        },
        'WEB_API_ALLOWED_ORIGINS': {
            'description': 'Разрешённые origins для CORS через запятую. * = все.',
            'format': 'Строка origins через запятую.',
            'example': '*',
            'dependencies': 'WEB_API_ENABLED',
        },
        'WEB_API_DOCS_ENABLED': {
            'description': 'Включить Swagger/OpenAPI документацию.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'WEB_API_ENABLED',
        },
        'WEB_API_TITLE': {
            'description': 'Название API для документации.',
            'format': 'Строка (str).',
            'example': 'Remnawave Bot Admin API',
            'dependencies': 'WEB_API_ENABLED, WEB_API_DOCS_ENABLED',
        },
        'WEB_API_VERSION': {
            'description': 'Версия API для документации.',
            'format': 'Строка версии.',
            'example': '1.0.0',
            'dependencies': 'WEB_API_ENABLED',
        },
        'WEB_API_DEFAULT_TOKEN': {
            'description': 'Токен по умолчанию для начальной настройки.',
            'format': 'Строка токена.',
            'example': 'your_bootstrap_token',
            'dependencies': 'WEB_API_ENABLED',
        },
        'WEB_API_DEFAULT_TOKEN_NAME': {
            'description': 'Название токена по умолчанию.',
            'format': 'Строка (str).',
            'example': 'Bootstrap Token',
            'dependencies': 'WEB_API_ENABLED',
        },
        'WEB_API_REQUEST_LOGGING': {
            'description': 'Логировать запросы к Web API.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'WEB_API_ENABLED',
        },
        # ===== WEBHOOK (бот) =====
        'BOT_RUN_MODE': {
            'description': 'Режим работы бота: polling — long polling (опрос серверов Telegram), webhook — приём вебхуков от Telegram.',
            'format': 'Выберите режим.',
            'example': 'polling | webhook',
        },
        'WEBHOOK_URL': {
            'description': 'Базовый URL для вебхуков бота.',
            'format': 'Полный URL с https.',
            'example': 'https://your-domain.com',
            'dependencies': 'BOT_RUN_MODE=webhook',
        },
        'WEBHOOK_PATH': {
            'description': 'Путь для вебхука бота.',
            'format': 'Путь начинающийся с /.',
            'example': '/webhook',
            'dependencies': 'BOT_RUN_MODE=webhook',
        },
        'WEBHOOK_SECRET_TOKEN': {
            'description': 'Секретный токен для проверки вебхуков.',
            'format': 'Строка (str).',
            'example': 'your_secret_token',
            'dependencies': 'BOT_RUN_MODE=webhook',
        },
        'WEBHOOK_DROP_PENDING_UPDATES': {
            'description': 'Удалять накопившиеся обновления при старте.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'BOT_RUN_MODE=webhook',
        },
        'WEBHOOK_MAX_QUEUE_SIZE': {
            'description': 'Максимальный размер очереди обновлений.',
            'format': 'Целое число (int).',
            'example': '1024',
            'dependencies': 'BOT_RUN_MODE=webhook',
        },
        'WEBHOOK_WORKERS': {
            'description': 'Количество воркеров для обработки вебхуков.',
            'format': 'Целое число (int).',
            'example': '4',
            'dependencies': 'BOT_RUN_MODE=webhook',
        },
        # ===== LOG =====
        'LOG_LEVEL': {
            'description': 'Уровень логирования: DEBUG — отладка, INFO — информация, WARNING — предупреждения, ERROR — ошибки, CRITICAL — критические.',
            'format': 'Выберите уровень.',
            'example': 'DEBUG | INFO | WARNING | ERROR | CRITICAL',
        },
        'LOG_FILE': {
            'description': 'Путь к файлу логов.',
            'format': 'Путь к файлу.',
            'example': 'logs/bot.log',
        },
        'LOG_COLORS': {
            'description': 'ANSI-цвета в консоли (true — цветной вывод, false — plain-text).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'LOG_ROTATION_ENABLED': {
            'description': 'Включить новую систему ротации логов.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'LOG_ROTATION_TIME': {
            'description': 'Время ротации логов (формат HH:MM).',
            'format': 'Время HH:MM.',
            'example': '00:00',
            'dependencies': 'LOG_ROTATION_ENABLED',
        },
        'LOG_ROTATION_KEEP_DAYS': {
            'description': 'Хранить архивы логов N дней.',
            'format': 'Целое число дней (int).',
            'example': '7',
            'dependencies': 'LOG_ROTATION_ENABLED',
        },
        'LOG_ROTATION_COMPRESS': {
            'description': 'Сжимать архивы логов (gzip).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'LOG_ROTATION_ENABLED',
        },
        'LOG_ROTATION_SEND_TO_TELEGRAM': {
            'description': 'Отправлять архивы логов в Telegram канал.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'LOG_ROTATION_ENABLED',
        },
        # ===== DEBUG =====
        'DEBUG': {
            'description': 'Включить режим отладки.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': 'В продакшене рекомендуется выключить для производительности.',
        },
        # ===== TELEGRAM STARS =====
        'TELEGRAM_STARS_ENABLED': {
            'description': 'Включить оплату через Telegram Stars.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'TELEGRAM_STARS_RATE_RUB': {
            'description': 'Курс Telegram Stars к рублю (сколько рублей за 1 звезду).',
            'format': 'Число с плавающей точкой (float).',
            'example': '1.79 (1 звезда = 1.79 ₽)',
            'dependencies': 'TELEGRAM_STARS_ENABLED',
        },
        'TELEGRAM_STARS_DISPLAY_NAME': {
            'description': 'Название кнопки Telegram Stars в интерфейсе.',
            'format': 'Строка (str).',
            'example': 'Telegram Stars',
            'dependencies': 'TELEGRAM_STARS_ENABLED',
        },
        # ===== TRIBUTE =====
        'TRIBUTE_ENABLED': {
            'description': 'Включить интеграцию с Tribute.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'TRIBUTE_API_KEY, TRIBUTE_DONATE_LINK',
        },
        'TRIBUTE_API_KEY': {
            'description': 'API ключ Tribute.',
            'format': 'Строка (str).',
            'example': 'your_api_key',
            'dependencies': 'TRIBUTE_ENABLED',
        },
        'TRIBUTE_DONATE_LINK': {
            'description': 'Ссылка на страницу доната Tribute.',
            'format': 'URL.',
            'example': 'https://donate.tribute.app/your_link',
            'dependencies': 'TRIBUTE_ENABLED',
        },
        # ===== HELEKET =====
        'HELEKET_ENABLED': {
            'description': 'Включить криптоплатежи через Heleket.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'HELEKET_MERCHANT_ID, HELEKET_API_KEY',
        },
        'HELEKET_MERCHANT_ID': {
            'description': 'Идентификатор мерчанта Heleket.',
            'format': 'Строка (str).',
            'example': 'your_merchant_id',
            'dependencies': 'HELEKET_ENABLED',
        },
        'HELEKET_API_KEY': {
            'description': 'API ключ Heleket.',
            'format': 'Строка (str).',
            'example': 'your_api_key',
            'dependencies': 'HELEKET_ENABLED',
        },
        'HELEKET_DEFAULT_CURRENCY': {
            'description': 'Криптовалюта по умолчанию для Heleket.',
            'format': 'Код валюты.',
            'example': 'USDT',
            'dependencies': 'HELEKET_ENABLED',
        },
        'HELEKET_MARKUP_PERCENT': {
            'description': 'Наценка на криптоплатежи в процентах.',
            'format': 'Число с плавающей точкой (float).',
            'example': '0.0 | 5.0 | 10.5',
            'dependencies': 'HELEKET_ENABLED',
        },
        # ===== MULENPAY =====
        'MULENPAY_ENABLED': {
            'description': 'Включить платежи через MulenPay.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'MULENPAY_API_KEY, MULENPAY_SECRET_KEY, MULENPAY_SHOP_ID',
        },
        'MULENPAY_API_KEY': {
            'description': 'API ключ MulenPay.',
            'format': 'Строка (str).',
            'example': 'your_api_key',
            'dependencies': 'MULENPAY_ENABLED',
        },
        'MULENPAY_SECRET_KEY': {
            'description': 'Секретный ключ MulenPay.',
            'format': 'Строка (str).',
            'example': 'your_secret_key',
            'dependencies': 'MULENPAY_ENABLED',
        },
        'MULENPAY_SHOP_ID': {
            'description': 'ID магазина MulenPay.',
            'format': 'Целое число (int).',
            'example': '123',
            'dependencies': 'MULENPAY_ENABLED',
        },
        'MULENPAY_DISPLAY_NAME': {
            'description': 'Название кнопки MulenPay в интерфейсе.',
            'format': 'Строка (str).',
            'example': 'Mulen Pay',
            'dependencies': 'MULENPAY_ENABLED',
        },
        'MULENPAY_MIN_AMOUNT_KOPEKS': {
            'description': 'Минимальная сумма через MulenPay в копейках.',
            'format': 'Целое число (int).',
            'example': '10000',
            'dependencies': 'MULENPAY_ENABLED',
        },
        'MULENPAY_MAX_AMOUNT_KOPEKS': {
            'description': 'Максимальная сумма через MulenPay в копейках.',
            'format': 'Целое число (int).',
            'example': '10000000',
            'dependencies': 'MULENPAY_ENABLED',
        },
        'MULENPAY_LANGUAGE': {
            'description': 'Язык интерфейса оплаты MulenPay.',
            'format': 'Выберите язык.',
            'example': 'ru | en',
            'dependencies': 'MULENPAY_ENABLED',
        },
        # ===== PAL24 =====
        'PAL24_ENABLED': {
            'description': 'Включить платежи через PAL24 (PayPalych).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'PAL24_API_TOKEN, PAL24_SHOP_ID',
        },
        'PAL24_API_TOKEN': {
            'description': 'API токен PAL24.',
            'format': 'Строка (str).',
            'example': 'your_api_token',
            'dependencies': 'PAL24_ENABLED',
        },
        'PAL24_SHOP_ID': {
            'description': 'ID магазина PAL24.',
            'format': 'Строка (str).',
            'example': 'your_shop_id',
            'dependencies': 'PAL24_ENABLED',
        },
        'PAL24_SIGNATURE_TOKEN': {
            'description': 'Токен подписи PAL24.',
            'format': 'Строка (str).',
            'example': 'your_signature_token',
            'dependencies': 'PAL24_ENABLED',
        },
        'PAL24_SBP_BUTTON_VISIBLE': {
            'description': 'Показывать кнопку СБП в PAL24.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'PAL24_ENABLED',
        },
        'PAL24_CARD_BUTTON_VISIBLE': {
            'description': 'Показывать кнопку оплаты картой в PAL24.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'PAL24_ENABLED',
        },
        'PAL24_MIN_AMOUNT_KOPEKS': {
            'description': 'Минимальная сумма через PAL24 в копейках.',
            'format': 'Целое число (int).',
            'example': '10000',
            'dependencies': 'PAL24_ENABLED',
        },
        'PAL24_MAX_AMOUNT_KOPEKS': {
            'description': 'Максимальная сумма через PAL24 в копейках.',
            'format': 'Целое число (int).',
            'example': '100000000',
            'dependencies': 'PAL24_ENABLED',
        },
        # ===== WATA =====
        'WATA_ENABLED': {
            'description': 'Включить платежи через Wata.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'WATA_ACCESS_TOKEN, WATA_TERMINAL_PUBLIC_ID',
        },
        'WATA_ACCESS_TOKEN': {
            'description': 'Токен доступа Wata.',
            'format': 'Строка (str).',
            'example': 'your_access_token',
            'dependencies': 'WATA_ENABLED',
        },
        'WATA_TERMINAL_PUBLIC_ID': {
            'description': 'Публичный ID терминала Wata.',
            'format': 'Строка (str).',
            'example': 'your_terminal_id',
            'dependencies': 'WATA_ENABLED',
        },
        'WATA_PAYMENT_TYPE': {
            'description': 'Тип платежа Wata: card — только карта, sbp — только СБП, all — все способы.',
            'format': 'Выберите тип.',
            'example': 'card | sbp | all',
            'dependencies': 'WATA_ENABLED',
        },
        'WATA_MIN_AMOUNT_KOPEKS': {
            'description': 'Минимальная сумма через Wata в копейках.',
            'format': 'Целое число (int).',
            'example': '10000',
            'dependencies': 'WATA_ENABLED',
        },
        'WATA_MAX_AMOUNT_KOPEKS': {
            'description': 'Максимальная сумма через Wata в копейках.',
            'format': 'Целое число (int).',
            'example': '10000000',
            'dependencies': 'WATA_ENABLED',
        },
        # ===== CLOUDPAYMENTS =====
        'CLOUDPAYMENTS_ENABLED': {
            'description': 'Включить платежи через CloudPayments.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'CLOUDPAYMENTS_PUBLIC_ID, CLOUDPAYMENTS_API_SECRET',
        },
        'CLOUDPAYMENTS_PUBLIC_ID': {
            'description': 'Public ID CloudPayments.',
            'format': 'Строка (str).',
            'example': 'your_public_id',
            'dependencies': 'CLOUDPAYMENTS_ENABLED',
        },
        'CLOUDPAYMENTS_API_SECRET': {
            'description': 'API Secret CloudPayments.',
            'format': 'Строка (str).',
            'example': 'your_api_secret',
            'dependencies': 'CLOUDPAYMENTS_ENABLED',
        },
        'CLOUDPAYMENTS_SKIN': {
            'description': 'Скин виджета CloudPayments: mini — минимальный, classic — классический, modern — современный.',
            'format': 'Выберите скин.',
            'example': 'mini | classic | modern',
            'dependencies': 'CLOUDPAYMENTS_ENABLED',
        },
        'CLOUDPAYMENTS_TEST_MODE': {
            'description': 'Тестовый режим CloudPayments.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'CLOUDPAYMENTS_ENABLED',
        },
        # ===== FREEKASSA =====
        'FREEKASSA_ENABLED': {
            'description': 'Включить платежи через Freekassa.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'FREEKASSA_SHOP_ID, FREEKASSA_API_KEY, FREEKASSA_SECRET_WORD_1, FREEKASSA_SECRET_WORD_2',
        },
        'FREEKASSA_SHOP_ID': {
            'description': 'ID магазина Freekassa.',
            'format': 'Целое число (int).',
            'example': '123456',
            'dependencies': 'FREEKASSA_ENABLED',
        },
        'FREEKASSA_API_KEY': {
            'description': 'API ключ Freekassa.',
            'format': 'Строка (str).',
            'example': 'your_api_key',
            'dependencies': 'FREEKASSA_ENABLED',
        },
        'FREEKASSA_SECRET_WORD_1': {
            'description': 'Секретное слово 1 Freekassa (для формы оплаты).',
            'format': 'Строка (str).',
            'example': 'your_secret_1',
            'dependencies': 'FREEKASSA_ENABLED',
        },
        'FREEKASSA_SECRET_WORD_2': {
            'description': 'Секретное слово 2 Freekassa (для webhook).',
            'format': 'Строка (str).',
            'example': 'your_secret_2',
            'dependencies': 'FREEKASSA_ENABLED',
        },
        'FREEKASSA_PAYMENT_SYSTEM_ID': {
            'description': 'Способ оплаты Freekassa: пусто = форма выбора, 42 = обычный СБП, 44 = NSPK СБП.',
            'format': 'Целое число или пусто.',
            'example': '44',
            'dependencies': 'FREEKASSA_ENABLED',
        },
        'FREEKASSA_USE_API': {
            'description': 'Использовать API для создания заказов (обязательно для NSPK СБП).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'FREEKASSA_ENABLED',
        },
        # ===== KASSA_AI =====
        'KASSA_AI_ENABLED': {
            'description': 'Включить платежи через KassaAI (api.fk.life).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'KASSA_AI_SHOP_ID, KASSA_AI_API_KEY, KASSA_AI_SECRET_WORD_2',
        },
        'KASSA_AI_SHOP_ID': {
            'description': 'ID магазина KassaAI.',
            'format': 'Целое число (int).',
            'example': '123456',
            'dependencies': 'KASSA_AI_ENABLED',
        },
        'KASSA_AI_API_KEY': {
            'description': 'API ключ KassaAI.',
            'format': 'Строка (str).',
            'example': 'your_api_key',
            'dependencies': 'KASSA_AI_ENABLED',
        },
        'KASSA_AI_SECRET_WORD_2': {
            'description': 'Секретное слово 2 KassaAI (для webhook).',
            'format': 'Строка (str).',
            'example': 'your_secret',
            'dependencies': 'KASSA_AI_ENABLED',
        },
        'KASSA_AI_PAYMENT_SYSTEM_ID': {
            'description': 'Способ оплаты KassaAI: 44 = СБП (QR), 36 = Карты РФ, 43 = SberPay.',
            'format': 'Целое число (int).',
            'example': '44',
            'dependencies': 'KASSA_AI_ENABLED',
        },
        # ===== PLATEGA =====
        'PLATEGA_ENABLED': {
            'description': 'Включить платежи через Platega.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'PLATEGA_MERCHANT_ID, PLATEGA_SECRET',
        },
        'PLATEGA_MERCHANT_ID': {
            'description': 'Merchant ID Platega.',
            'format': 'Строка (str).',
            'example': 'your_merchant_id',
            'dependencies': 'PLATEGA_ENABLED',
        },
        'PLATEGA_SECRET': {
            'description': 'Секрет Platega.',
            'format': 'Строка (str).',
            'example': 'your_secret',
            'dependencies': 'PLATEGA_ENABLED',
        },
        'PLATEGA_ACTIVE_METHODS': {
            'description': 'Активные методы оплаты Platega: 2-СБП, 10-Карты RUB, 11-Банковские, 12-Международные, 13-Крипто.',
            'format': 'Числа через запятую.',
            'example': '2,10,11,12,13',
            'dependencies': 'PLATEGA_ENABLED',
        },
        'PLATEGA_DISPLAY_NAME': {
            'description': 'Название кнопки Platega в интерфейсе.',
            'format': 'Строка (str).',
            'example': 'Platega',
            'dependencies': 'PLATEGA_ENABLED',
        },
        # ===== NALOGO =====
        'NALOGO_ENABLED': {
            'description': 'Включить автоматическую отправку чеков в налоговую (NaloGO).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'NALOGO_INN, NALOGO_PASSWORD',
        },
        'NALOGO_INN': {
            'description': 'ИНН самозанятого для NaloGO.',
            'format': 'Строка из 12 цифр.',
            'example': '123456789012',
            'dependencies': 'NALOGO_ENABLED',
        },
        'NALOGO_PASSWORD': {
            'description': 'Пароль от личного кабинета налоговой.',
            'format': 'Строка (str).',
            'example': 'your_password',
            'dependencies': 'NALOGO_ENABLED',
        },
        'NALOGO_QUEUE_CHECK_INTERVAL': {
            'description': 'Интервал проверки очереди чеков в секундах.',
            'format': 'Целое число секунд (int).',
            'example': '300',
            'dependencies': 'NALOGO_ENABLED',
        },
        'NALOGO_QUEUE_MAX_ATTEMPTS': {
            'description': 'Максимум попыток отправки одного чека.',
            'format': 'Целое число (int).',
            'example': '10',
            'dependencies': 'NALOGO_ENABLED',
        },
        # ===== CONTESTS =====
        'CONTESTS_ENABLED': {
            'description': 'Включить конкурсную систему.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'CONTESTS_BUTTON_VISIBLE': {
            'description': 'Показывать кнопку конкурсов в меню.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'CONTESTS_ENABLED',
        },
        # ===== BLACKLIST =====
        'BLACKLIST_CHECK_ENABLED': {
            'description': 'Включить проверку пользователей по чёрному списку.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'BLACKLIST_GITHUB_URL',
        },
        'BLACKLIST_GITHUB_URL': {
            'description': 'URL к файлу чёрного списка на GitHub.',
            'format': 'URL raw файла.',
            'example': 'https://raw.githubusercontent.com/.../blacklist.txt',
            'dependencies': 'BLACKLIST_CHECK_ENABLED',
        },
        'BLACKLIST_UPDATE_INTERVAL_HOURS': {
            'description': 'Интервал обновления чёрного списка в часах.',
            'format': 'Целое число часов (int).',
            'example': '24',
            'dependencies': 'BLACKLIST_CHECK_ENABLED',
        },
        'BLACKLIST_IGNORE_ADMINS': {
            'description': 'Игнорировать администраторов при проверке чёрного списка.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'dependencies': 'BLACKLIST_CHECK_ENABLED',
        },
        'DISPOSABLE_EMAIL_CHECK_ENABLED': {
            'description': 'Включить проверку на одноразовые email при регистрации.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        # ===== AUTO_PURCHASE =====
        'AUTO_PURCHASE_AFTER_TOPUP_ENABLED': {
            'description': (
                'При достаточном балансе автоматически оформляет сохранённую подписку сразу после пополнения.'
            ),
            'format': 'Булево значение (bool).',
            'example': 'true | false',
            'warning': ('Используйте с осторожностью: средства будут списаны мгновенно, если корзина найдена.'),
        },
        # ===== ACTIVATE_BUTTON =====
        'ACTIVATE_BUTTON_VISIBLE': {
            'description': 'Показывать кнопку активации.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'ACTIVATE_BUTTON_TEXT': {
            'description': 'Текст кнопки активации.',
            'format': 'Строка (str).',
            'example': 'активировать',
            'dependencies': 'ACTIVATE_BUTTON_VISIBLE',
        },
        'WEBHOOK_NOTIFY_USER_ENABLED': {
            'description': (
                'Глобальный переключатель уведомлений пользователям от вебхуков RemnaWave. '
                'При выключении ни одно уведомление не отправляется, независимо от остальных настроек.'
            ),
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_SUB_STATUS': {
            'description': 'Уведомления об отключении и активации подписки администратором.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_SUB_EXPIRED': {
            'description': 'Уведомления об истечении подписки.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_SUB_EXPIRING': {
            'description': 'Предупреждения о скором истечении подписки (72ч, 48ч, 24ч до окончания).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_SUB_LIMITED': {
            'description': 'Уведомление при достижении лимита трафика.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_TRAFFIC_RESET': {
            'description': 'Уведомление о сбросе счётчика трафика.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_SUB_DELETED': {
            'description': 'Уведомление при удалении пользователя из панели.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_SUB_REVOKED': {
            'description': 'Уведомление при обновлении ключей подписки (revoke).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_FIRST_CONNECTED': {
            'description': 'Уведомление при первом подключении к VPN.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_NOT_CONNECTED': {
            'description': 'Напоминание, что пользователь ещё не подключился к VPN.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_BANDWIDTH_THRESHOLD': {
            'description': 'Предупреждение при приближении к лимиту трафика (порог в %).',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
        'WEBHOOK_NOTIFY_DEVICES': {
            'description': 'Уведомления о подключении и отключении устройств.',
            'format': 'Булево значение (bool).',
            'example': 'true | false',
        },
    }

    @classmethod
    def get_category_description(cls, category_key: str) -> str:
        description = cls.CATEGORY_DESCRIPTIONS.get(category_key, '')
        return cls._format_dynamic_copy(category_key, description)

    @classmethod
    def is_toggle(cls, key: str) -> bool:
        definition = cls.get_definition(key)
        return definition.python_type is bool

    @classmethod
    def is_read_only(cls, key: str) -> bool:
        return key in cls.READ_ONLY_KEYS

    @classmethod
    def _is_env_override(cls, key: str) -> bool:
        return key in cls._env_override_keys

    @classmethod
    def _format_numeric_with_unit(cls, key: str, value: float) -> str | None:
        if isinstance(value, bool):
            return None
        upper_key = key.upper()
        if any(suffix in upper_key for suffix in ('PRICE', '_KOPEKS', 'AMOUNT')):
            try:
                return settings.format_price(int(value))
            except Exception:
                return f'{value}'
        if upper_key.endswith('_PERCENT') or 'PERCENT' in upper_key:
            return f'{value}%'
        if upper_key.endswith('_HOURS'):
            return f'{value} ч'
        if upper_key.endswith('_MINUTES'):
            return f'{value} мин'
        if upper_key.endswith('_SECONDS'):
            return f'{value} сек'
        if upper_key.endswith('_DAYS'):
            return f'{value} дн'
        if upper_key.endswith('_GB'):
            return f'{value} ГБ'
        if upper_key.endswith('_MB'):
            return f'{value} МБ'
        return None

    @classmethod
    def _split_comma_values(cls, text: str) -> list[str] | None:
        raw = (text or '').strip()
        if not raw or ',' not in raw:
            return None
        parts = [segment.strip() for segment in raw.split(',') if segment.strip()]
        return parts or None

    @classmethod
    def format_value_human(cls, key: str, value: Any) -> str:
        if key == 'SIMPLE_SUBSCRIPTION_SQUAD_UUID':
            if value is None:
                return 'Любой доступный'
            if isinstance(value, str):
                cleaned_value = value.strip()
                if not cleaned_value:
                    return 'Любой доступный'

        if value is None:
            return '—'

        if isinstance(value, bool):
            return '✅ ВКЛЮЧЕНО' if value else '❌ ВЫКЛЮЧЕНО'

        if isinstance(value, (int, float)):
            formatted = cls._format_numeric_with_unit(key, value)
            return formatted or str(value)

        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return '—'
            if key in cls.PLAIN_TEXT_KEYS:
                return cleaned
            if any(keyword in key.upper() for keyword in ('TOKEN', 'SECRET', 'PASSWORD', 'KEY')):
                return '••••••••'
            items = cls._split_comma_values(cleaned)
            if items:
                return ', '.join(items)
            return cleaned

        if isinstance(value, (list, tuple, set)):
            return ', '.join(str(item) for item in value)

        if isinstance(value, dict):
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return str(value)

        return str(value)

    @classmethod
    def get_setting_guidance(cls, key: str) -> dict[str, str]:
        definition = cls.get_definition(key)
        original = cls.get_original_value(key)
        type_label = definition.type_label
        hints = dict(cls.SETTING_HINTS.get(key, {}))

        base_description = (
            hints.get('description')
            or f'Параметр <b>{definition.display_name}</b> управляет категорией «{definition.category_label}».'
        )
        base_format = hints.get('format') or (
            'Булево значение (да/нет).'
            if definition.python_type is bool
            else 'Введите значение соответствующего типа (число или строку).'
        )
        example = hints.get('example') or (cls.format_value_human(key, original) if original is not None else '—')
        warning = hints.get('warning') or ('Неверные значения могут привести к некорректной работе бота.')
        dependencies = hints.get('dependencies') or definition.category_label

        return {
            'description': base_description,
            'format': base_format,
            'example': example,
            'warning': warning,
            'dependencies': dependencies,
            'type': type_label,
        }

    _definitions: dict[str, SettingDefinition] = {}
    _original_values: dict[str, Any] = settings.model_dump()
    _overrides_raw: dict[str, str | None] = {}
    _env_override_keys: set[str] = set(ENV_OVERRIDE_KEYS)
    _callback_tokens: dict[str, str] = {}
    _token_to_key: dict[str, str] = {}
    _choice_tokens: dict[str, dict[Any, str]] = {}
    _choice_token_lookup: dict[str, dict[str, Any]] = {}

    @classmethod
    def initialize_definitions(cls) -> None:
        if cls._definitions:
            return

        for key, field in Settings.model_fields.items():
            if key in cls.EXCLUDED_KEYS:
                continue

            annotation = field.annotation
            python_type, is_optional = cls._normalize_type(annotation)
            type_label = cls._type_to_label(python_type, is_optional)

            category_key = cls._resolve_category_key(key)
            category_label = cls.CATEGORY_TITLES.get(
                category_key,
                category_key.capitalize() if category_key else 'Прочее',
            )
            category_label = cls._format_dynamic_copy(category_key, category_label)

            cls._definitions[key] = SettingDefinition(
                key=key,
                category_key=category_key or 'other',
                category_label=category_label,
                python_type=python_type,
                type_label=type_label,
                is_optional=is_optional,
            )

            cls._register_callback_token(key)
            if key in cls.CHOICES:
                cls._ensure_choice_tokens(key)

    @classmethod
    def _resolve_category_key(cls, key: str) -> str:
        override = cls.CATEGORY_KEY_OVERRIDES.get(key)
        if override:
            return override

        for prefix, category in sorted(
            cls.CATEGORY_PREFIX_OVERRIDES.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if key.startswith(prefix):
                return category

        if '_' not in key:
            return key.upper()
        prefix = key.split('_', 1)[0]
        return prefix.upper()

    @classmethod
    def _normalize_type(cls, annotation: Any) -> tuple[type[Any], bool]:
        if annotation is None:
            return str, True

        origin = get_origin(annotation)
        if origin is Union:
            args = [arg for arg in get_args(annotation) if arg is not type(None)]
            if len(args) == 1:
                nested_type, nested_optional = cls._normalize_type(args[0])
                return nested_type, True
            return str, True

        if annotation in {int, float, bool, str}:
            return annotation, False

        if annotation in {Optional[int], Optional[float], Optional[bool], Optional[str]}:
            nested = get_args(annotation)[0]
            return nested, True

        # Paths, lists, dicts и прочее будем хранить как строки
        return str, False

    @classmethod
    def _type_to_label(cls, python_type: type[Any], is_optional: bool) -> str:
        base = {
            bool: 'bool',
            int: 'int',
            float: 'float',
            str: 'str',
        }.get(python_type, 'str')
        return f'optional[{base}]' if is_optional else base

    @classmethod
    def get_categories(cls) -> list[tuple[str, str, int]]:
        cls.initialize_definitions()
        categories: dict[str, list[SettingDefinition]] = {}

        for definition in cls._definitions.values():
            categories.setdefault(definition.category_key, []).append(definition)

        result: list[tuple[str, str, int]] = []
        for category_key, items in categories.items():
            label = items[0].category_label
            result.append((category_key, label, len(items)))

        result.sort(key=lambda item: item[1])
        return result

    @classmethod
    def get_settings_for_category(cls, category_key: str) -> list[SettingDefinition]:
        cls.initialize_definitions()
        filtered = [definition for definition in cls._definitions.values() if definition.category_key == category_key]
        filtered.sort(key=lambda definition: definition.key)
        return filtered

    @classmethod
    def get_definition(cls, key: str) -> SettingDefinition:
        cls.initialize_definitions()
        return cls._definitions[key]

    @classmethod
    def has_override(cls, key: str) -> bool:
        if cls._is_env_override(key):
            return False
        return key in cls._overrides_raw

    @classmethod
    def get_current_value(cls, key: str) -> Any:
        return getattr(settings, key)

    @classmethod
    def get_original_value(cls, key: str) -> Any:
        return cls._original_values.get(key)

    @classmethod
    def format_value(cls, value: Any) -> str:
        if value is None:
            return '—'
        if isinstance(value, bool):
            return '✅ Да' if value else '❌ Нет'
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (list, dict, tuple, set)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return str(value)
        return str(value)

    @classmethod
    def format_value_for_list(cls, key: str) -> str:
        value = cls.get_current_value(key)
        formatted = cls.format_value_human(key, value)
        if formatted == '—':
            return formatted
        return _truncate(formatted)

    @classmethod
    def get_choice_options(cls, key: str) -> list[ChoiceOption]:
        cls.initialize_definitions()
        dynamic = cls._get_dynamic_choice_options(key)
        if dynamic is not None:
            cls.CHOICES[key] = dynamic
            cls._invalidate_choice_cache(key)
            return dynamic
        return cls.CHOICES.get(key, [])

    @classmethod
    def _invalidate_choice_cache(cls, key: str) -> None:
        cls._choice_tokens.pop(key, None)
        cls._choice_token_lookup.pop(key, None)

    @classmethod
    def _get_dynamic_choice_options(cls, key: str) -> list[ChoiceOption] | None:
        if key == 'SIMPLE_SUBSCRIPTION_PERIOD_DAYS':
            return cls._build_simple_subscription_period_choices()
        if key == 'SIMPLE_SUBSCRIPTION_DEVICE_LIMIT':
            return cls._build_simple_subscription_device_choices()
        if key == 'SIMPLE_SUBSCRIPTION_TRAFFIC_GB':
            return cls._build_simple_subscription_traffic_choices()
        return None

    @staticmethod
    def _build_simple_subscription_period_choices() -> list[ChoiceOption]:
        raw_periods = str(getattr(settings, 'AVAILABLE_SUBSCRIPTION_PERIODS', '') or '')
        period_values: set[int] = set()

        for segment in raw_periods.split(','):
            segment = segment.strip()
            if not segment:
                continue
            try:
                period = int(segment)
            except ValueError:
                continue
            if period > 0:
                period_values.add(period)

        fallback_period = getattr(settings, 'SIMPLE_SUBSCRIPTION_PERIOD_DAYS', 30) or 30
        try:
            fallback_period = int(fallback_period)
        except (TypeError, ValueError):
            fallback_period = 30
        period_values.add(max(1, fallback_period))

        options: list[ChoiceOption] = []
        for days in sorted(period_values):
            price_attr = f'PRICE_{days}_DAYS'
            price_value = getattr(settings, price_attr, None)
            if not isinstance(price_value, int):
                price_value = settings.BASE_SUBSCRIPTION_PRICE

            label = f'{days} дн.'
            try:
                if isinstance(price_value, int):
                    label = f'{label} — {settings.format_price(price_value)}'
            except Exception:
                logger.debug('Не удалось форматировать цену для периода', days=days, exc_info=True)

            options.append(ChoiceOption(days, label))

        return options

    @classmethod
    def _build_simple_subscription_device_choices(cls) -> list[ChoiceOption]:
        default_limit = getattr(settings, 'DEFAULT_DEVICE_LIMIT', 1) or 1
        try:
            default_limit = int(default_limit)
        except (TypeError, ValueError):
            default_limit = 1

        max_limit = getattr(settings, 'MAX_DEVICES_LIMIT', default_limit) or default_limit
        try:
            max_limit = int(max_limit)
        except (TypeError, ValueError):
            max_limit = default_limit

        current_limit = getattr(settings, 'SIMPLE_SUBSCRIPTION_DEVICE_LIMIT', default_limit) or default_limit
        try:
            current_limit = int(current_limit)
        except (TypeError, ValueError):
            current_limit = default_limit

        upper_bound = max(default_limit, max_limit, current_limit, 1)
        upper_bound = min(max(upper_bound, 1), 50)

        options: list[ChoiceOption] = []
        for count in range(1, upper_bound + 1):
            label = f'{count} {cls._pluralize_devices(count)}'
            if count == default_limit:
                label = f'{label} (по умолчанию)'
            options.append(ChoiceOption(count, label))

        return options

    @staticmethod
    def _build_simple_subscription_traffic_choices() -> list[ChoiceOption]:
        try:
            packages = settings.get_traffic_packages()
        except Exception as error:
            logger.warning('Не удалось получить пакеты трафика', error=error, exc_info=True)
            packages = []

        traffic_values: set[int] = {0}
        for package in packages:
            gb_value = package.get('gb')
            try:
                gb = int(gb_value)
            except (TypeError, ValueError):
                continue
            if gb >= 0:
                traffic_values.add(gb)

        default_limit = getattr(settings, 'DEFAULT_TRAFFIC_LIMIT_GB', 0) or 0
        try:
            default_limit = int(default_limit)
        except (TypeError, ValueError):
            default_limit = 0
        if default_limit >= 0:
            traffic_values.add(default_limit)

        current_limit = getattr(settings, 'SIMPLE_SUBSCRIPTION_TRAFFIC_GB', default_limit)
        try:
            current_limit = int(current_limit)
        except (TypeError, ValueError):
            current_limit = default_limit
        if current_limit >= 0:
            traffic_values.add(current_limit)

        options: list[ChoiceOption] = []
        for gb in sorted(traffic_values):
            if gb <= 0:
                label = 'Безлимит'
            else:
                label = f'{gb} ГБ'

            price_label = None
            for package in packages:
                try:
                    package_gb = int(package.get('gb'))
                except (TypeError, ValueError):
                    continue
                if package_gb != gb:
                    continue
                price_raw = package.get('price')
                try:
                    price_value = int(price_raw)
                    if price_value >= 0:
                        price_label = settings.format_price(price_value)
                except (TypeError, ValueError):
                    continue
                break

            if price_label:
                label = f'{label} — {price_label}'

            options.append(ChoiceOption(gb, label))

        return options

    @staticmethod
    def _pluralize_devices(count: int) -> str:
        count = abs(int(count))
        last_two = count % 100
        last_one = count % 10
        if 11 <= last_two <= 14:
            return 'устройств'
        if last_one == 1:
            return 'устройство'
        if 2 <= last_one <= 4:
            return 'устройства'
        return 'устройств'

    @classmethod
    def has_choices(cls, key: str) -> bool:
        return bool(cls.get_choice_options(key))

    @classmethod
    def get_callback_token(cls, key: str) -> str:
        cls.initialize_definitions()
        return cls._callback_tokens[key]

    @classmethod
    def resolve_callback_token(cls, token: str) -> str:
        cls.initialize_definitions()
        return cls._token_to_key[token]

    @classmethod
    def get_choice_token(cls, key: str, value: Any) -> str | None:
        cls.initialize_definitions()
        cls._ensure_choice_tokens(key)
        return cls._choice_tokens.get(key, {}).get(value)

    @classmethod
    def resolve_choice_token(cls, key: str, token: str) -> Any:
        cls.initialize_definitions()
        cls._ensure_choice_tokens(key)
        return cls._choice_token_lookup.get(key, {})[token]

    @classmethod
    def _register_callback_token(cls, key: str) -> None:
        if key in cls._callback_tokens:
            return

        base = hashlib.blake2s(key.encode('utf-8'), digest_size=6).hexdigest()
        candidate = base
        counter = 1
        while candidate in cls._token_to_key and cls._token_to_key[candidate] != key:
            suffix = cls._encode_base36(counter)
            candidate = f'{base}{suffix}'[:16]
            counter += 1

        cls._callback_tokens[key] = candidate
        cls._token_to_key[candidate] = key

    @classmethod
    def _ensure_choice_tokens(cls, key: str) -> None:
        if key in cls._choice_tokens:
            return

        options = cls.CHOICES.get(key, [])
        value_to_token: dict[Any, str] = {}
        token_to_value: dict[str, Any] = {}

        for index, option in enumerate(options):
            token = cls._encode_base36(index)
            value_to_token[option.value] = token
            token_to_value[token] = option.value

        cls._choice_tokens[key] = value_to_token
        cls._choice_token_lookup[key] = token_to_value

    @staticmethod
    def _encode_base36(number: int) -> str:
        if number < 0:
            raise ValueError('number must be non-negative')
        alphabet = '0123456789abcdefghijklmnopqrstuvwxyz'
        if number == 0:
            return '0'
        result = []
        while number:
            number, rem = divmod(number, 36)
            result.append(alphabet[rem])
        return ''.join(reversed(result))

    @classmethod
    async def initialize(cls) -> None:
        cls.initialize_definitions()

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(SystemSetting))
            rows = result.scalars().all()

        overrides: dict[str, str | None] = {}
        for row in rows:
            if row.key in cls._definitions:
                overrides[row.key] = row.value

        for key, raw_value in overrides.items():
            if cls._is_env_override(key):
                logger.debug('Пропускаем настройку из БД: используется значение из окружения', key=key)
                continue
            try:
                parsed_value = cls.deserialize_value(key, raw_value)
            except Exception as error:
                logger.error('Не удалось применить настройку', key=key, error=error)
                continue

            cls._overrides_raw[key] = raw_value
            cls._apply_to_settings(key, parsed_value)

        await cls._sync_default_web_api_token()

    @classmethod
    async def reload(cls) -> None:
        cls._overrides_raw.clear()
        await cls.initialize()

    @classmethod
    def deserialize_value(cls, key: str, raw_value: str | None) -> Any:
        if raw_value is None:
            return None

        definition = cls.get_definition(key)
        python_type = definition.python_type

        if python_type is bool:
            value_lower = raw_value.strip().lower()
            if value_lower in {'1', 'true', 'on', 'yes', 'да'}:
                return True
            if value_lower in {'0', 'false', 'off', 'no', 'нет'}:
                return False
            raise ValueError(f'Неверное булево значение: {raw_value}')

        if python_type is int:
            return int(raw_value)

        if python_type is float:
            return float(raw_value)

        return raw_value

    @classmethod
    def serialize_value(cls, key: str, value: Any) -> str | None:
        if value is None:
            return None

        definition = cls.get_definition(key)
        python_type = definition.python_type

        if python_type is bool:
            return 'true' if value else 'false'
        if python_type in {int, float}:
            return str(value)
        return str(value)

    @classmethod
    def parse_user_value(cls, key: str, user_input: str) -> Any:
        definition = cls.get_definition(key)
        text = (user_input or '').strip()

        if text.lower() in {'отмена', 'cancel'}:
            raise ValueError('Ввод отменен пользователем')

        if definition.is_optional and text.lower() in {'none', 'null', 'пусто', ''}:
            return None

        python_type = definition.python_type

        if python_type is bool:
            lowered = text.lower()
            if lowered in {'1', 'true', 'on', 'yes', 'да', 'вкл', 'enable', 'enabled'}:
                return True
            if lowered in {'0', 'false', 'off', 'no', 'нет', 'выкл', 'disable', 'disabled'}:
                return False
            raise ValueError("Введите 'true' или 'false' (или 'да'/'нет')")

        if python_type is int:
            parsed_value: Any = int(text)
        elif python_type is float:
            parsed_value = float(text.replace(',', '.'))
        else:
            parsed_value = text

        choices = cls.get_choice_options(key)
        if choices:
            allowed_values = {option.value for option in choices}
            if python_type is str:
                lowered_map = {str(option.value).lower(): option.value for option in choices}
                normalized = lowered_map.get(str(parsed_value).lower())
                if normalized is not None:
                    parsed_value = normalized
                elif parsed_value not in allowed_values:
                    readable = ', '.join(f'{option.label} ({cls.format_value(option.value)})' for option in choices)
                    raise ValueError(f'Доступные значения: {readable}')
            elif parsed_value not in allowed_values:
                readable = ', '.join(f'{option.label} ({cls.format_value(option.value)})' for option in choices)
                raise ValueError(f'Доступные значения: {readable}')

        return parsed_value

    @classmethod
    async def set_value(
        cls,
        db: AsyncSession,
        key: str,
        value: Any,
        *,
        force: bool = False,
    ) -> None:
        if cls.is_read_only(key) and not force:
            raise ReadOnlySettingError(f'Setting {key} is read-only')

        raw_value = cls.serialize_value(key, value)
        await upsert_system_setting(db, key, raw_value)
        if cls._is_env_override(key):
            logger.info('Настройка сохранена в БД, но не применена: значение задаётся через окружение', key=key)
            cls._overrides_raw.pop(key, None)
        else:
            cls._overrides_raw[key] = raw_value
            cls._apply_to_settings(key, value)

        if key in {'WEB_API_DEFAULT_TOKEN', 'WEB_API_DEFAULT_TOKEN_NAME'}:
            await cls._sync_default_web_api_token()

    @classmethod
    async def reset_value(
        cls,
        db: AsyncSession,
        key: str,
        *,
        force: bool = False,
    ) -> None:
        if cls.is_read_only(key) and not force:
            raise ReadOnlySettingError(f'Setting {key} is read-only')

        await delete_system_setting(db, key)
        cls._overrides_raw.pop(key, None)
        if cls._is_env_override(key):
            logger.info('Настройка сброшена в БД, используется значение из окружения', key=key)
        else:
            original = cls.get_original_value(key)
            cls._apply_to_settings(key, original)

        if key in {'WEB_API_DEFAULT_TOKEN', 'WEB_API_DEFAULT_TOKEN_NAME'}:
            await cls._sync_default_web_api_token()

    @classmethod
    def _apply_to_settings(cls, key: str, value: Any) -> None:
        if cls._is_env_override(key):
            logger.debug('Пропуск применения настройки : значение задано через окружение', key=key)
            return
        try:
            setattr(settings, key, value)
            if key in {
                'PRICE_14_DAYS',
                'PRICE_30_DAYS',
                'PRICE_60_DAYS',
                'PRICE_90_DAYS',
                'PRICE_180_DAYS',
                'PRICE_360_DAYS',
            }:
                refresh_period_prices()
            elif key.startswith('PRICE_TRAFFIC_') or key == 'TRAFFIC_PACKAGES_CONFIG':
                refresh_traffic_prices()
            elif key in {'REMNAWAVE_AUTO_SYNC_ENABLED', 'REMNAWAVE_AUTO_SYNC_TIMES'}:
                try:
                    from app.services.remnawave_sync_service import remnawave_sync_service

                    remnawave_sync_service.schedule_refresh(
                        run_immediately=(key == 'REMNAWAVE_AUTO_SYNC_ENABLED' and bool(value))
                    )
                except Exception as error:
                    logger.error('Не удалось обновить сервис автосинхронизации RemnaWave', error=error)
            elif key == 'SUPPORT_SYSTEM_MODE':
                try:
                    from app.services.support_settings_service import SupportSettingsService

                    SupportSettingsService.set_system_mode(str(value))
                except Exception as error:
                    logger.error('Не удалось синхронизировать SupportSettingsService', error=error)
            elif key in {
                'REMNAWAVE_API_URL',
                'REMNAWAVE_API_KEY',
                'REMNAWAVE_SECRET_KEY',
                'REMNAWAVE_USERNAME',
                'REMNAWAVE_PASSWORD',
                'REMNAWAVE_AUTH_TYPE',
            }:
                try:
                    from app.services.remnawave_sync_service import remnawave_sync_service

                    remnawave_sync_service.refresh_configuration()
                except Exception as error:
                    logger.error('Не удалось обновить конфигурацию сервиса автосинхронизации RemnaWave', error=error)
        except Exception as error:
            logger.error('Не удалось применить значение', key=key, setting_value=value, error=error)

    @staticmethod
    async def _sync_default_web_api_token() -> None:
        default_token = (settings.WEB_API_DEFAULT_TOKEN or '').strip()
        if not default_token:
            return

        success = await ensure_default_web_api_token()
        if not success:
            logger.warning(
                'Не удалось синхронизировать бутстрап токен веб-API после обновления настроек',
            )

    @classmethod
    def get_setting_summary(cls, key: str) -> dict[str, Any]:
        definition = cls.get_definition(key)
        current = cls.get_current_value(key)
        original = cls.get_original_value(key)
        has_override = cls.has_override(key)

        return {
            'key': key,
            'name': definition.display_name,
            'current': cls.format_value_human(key, current),
            'original': cls.format_value_human(key, original),
            'type': definition.type_label,
            'category_key': definition.category_key,
            'category_label': definition.category_label,
            'has_override': has_override,
            'is_read_only': cls.is_read_only(key),
        }


bot_configuration_service = BotConfigurationService
