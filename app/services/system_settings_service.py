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
    clear_db_period_prices,
    refresh_classic_period_prices,
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
from app.services.web_api_token_service import ensure_default_web_api_token


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
        'TELEGRAM_WIDGET': '🔐 Telegram Login Widget',
        'TELEGRAM_OIDC': '🔑 Telegram Login (OIDC)',
        'CRYPTOBOT': '🪙 CryptoBot',
        'HELEKET': '🪙 Heleket',
        'CLOUDPAYMENTS': '💳 CloudPayments',
        'FREEKASSA': '💳 Freekassa',
        'KASSA_AI': '💳 KassaAI',
        'RIOPAY': '💳 RioPay',
        'SEVERPAY': '💳 SeverPay',
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
        'RIOPAY': 'RioPay: платёжная система api.riopay.online с поддержкой карт и СБП.',
        'PLATEGA': '{platega_name}: merchant ID, секрет, ссылки возврата и методы оплаты.',
        'MULENPAY': 'Платежи {mulenpay_name} и параметры магазина.',
        'PAL24': 'PAL24 / PayPalych подключения и лимиты.',
        'TRIBUTE': 'Tribute и донат-сервисы.',
        'TELEGRAM': 'Telegram Stars и их стоимость.',
        'TELEGRAM_WIDGET': 'Внешний вид виджета авторизации Telegram на странице входа в кабинет.',
        'TELEGRAM_OIDC': 'OpenID Connect авторизация через Telegram (новая система). Требует настройки в BotFather > Bot Settings > Web Login.',
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
        'ADDITIONAL': 'Конфигурация deep links и кеша.',
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
        'CHANNEL_IS_REQUIRED_SUB': 'CHANNEL',
        'BOT_USERNAME': 'CORE',
        'DEFAULT_LANGUAGE': 'LOCALIZATION',
        'AVAILABLE_LANGUAGES': 'LOCALIZATION',
        'REMNAWAVE_WEBHOOK_NOTIFY_NODE_CONNECTION_STATUS': 'ADMIN_NOTIFICATIONS',
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
        'RESET_TRAFFIC_ON_TARIFF_SWITCH': 'TRAFFIC',
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
        'MULTI_TARIFF_ENABLED': 'SUBSCRIPTIONS_CORE',
        'MAX_ACTIVE_SUBSCRIPTIONS': 'SUBSCRIPTIONS_CORE',
        'BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED': 'SUBSCRIPTIONS_CORE',
        'BASE_PROMO_GROUP_PERIOD_DISCOUNTS': 'SUBSCRIPTIONS_CORE',
        'DEFAULT_AUTOPAY_ENABLED': 'AUTOPAY',
        'DEFAULT_AUTOPAY_DAYS_BEFORE': 'AUTOPAY',
        'MIN_BALANCE_FOR_AUTOPAY_KOPEKS': 'AUTOPAY',
        'TRIAL_WARNING_HOURS': 'TRIAL',
        'TRIAL_USER_TAG': 'TRIAL',
        'SUPPORT_TG_USERNAME': 'SUPPORT',
        'SUPPORT_VK_URL': 'SUPPORT',
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
        'ADMIN_NOTIFICATIONS_PURCHASES_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_RENEWALS_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_TRIALS_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_BALANCE_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_ADDONS_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_PROMO_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
        'ADMIN_NOTIFICATIONS_PARTNERS_TOPIC_ID': 'ADMIN_NOTIFICATIONS',
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
        'TELEGRAM_OIDC_': 'TELEGRAM_OIDC',
        'TELEGRAM_WIDGET_': 'TELEGRAM_WIDGET',
        'TELEGRAM_STARS': 'TELEGRAM',
        'TRIBUTE_': 'TRIBUTE',
        'YOOKASSA_': 'YOOKASSA',
        'CRYPTOBOT_': 'CRYPTOBOT',
        'HELEKET_': 'HELEKET',
        'CLOUDPAYMENTS_': 'CLOUDPAYMENTS',
        'FREEKASSA_': 'FREEKASSA',
        'KASSA_AI_': 'KASSA_AI',
        'RIOPAY_': 'RIOPAY',
        'SEVERPAY_': 'SEVERPAY',
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
        'TELEGRAM_WIDGET_SIZE': [
            ChoiceOption('large', '🔵 Large'),
            ChoiceOption('medium', '🟡 Medium'),
            ChoiceOption('small', '🟢 Small'),
        ],
    }

    SETTING_HINTS: dict[str, dict[str, str]] = {
        'SALES_MODE': {
            'description': (
                'Режим продажи подписок. '
                '«Классический» — выбор периода из .env (PRICE_14_DAYS и т.д.). '
                '«Тарифы» — готовые тарифные планы из кабинета с серверами и лимитами.'
            ),
            'format': 'Выберите один из доступных режимов.',
            'example': 'tariffs',
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
        'SIMPLE_SUBSCRIPTION_ENABLED': {
            'description': 'Показывает в меню пункт с быстрой покупкой подписки.',
            'format': 'Булево значение.',
            'example': 'true',
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
        'MULTI_TARIFF_ENABLED': {
            'description': (
                'Разрешает пользователям покупать несколько тарифов одновременно. '
                'Каждый тариф создаёт отдельную подписку с собственными серверами и лимитами.'
            ),
            'format': 'Булево значение.',
            'example': 'true',
            'warning': (
                'Работает только в режиме продаж «Тарифы». '
                'При включении синхронизация с панелью переключается на мультитарифный режим.'
            ),
            'dependencies': 'SALES_MODE=tariffs, MAX_ACTIVE_SUBSCRIPTIONS',
        },
        'MAX_ACTIVE_SUBSCRIPTIONS': {
            'description': (
                'Максимальное количество одновременных активных подписок у одного пользователя. '
                'Применяется только в мультитарифном режиме.'
            ),
            'format': 'Целое число от 1 и выше.',
            'example': '10',
            'warning': 'Большое значение может усложнить управление подписками для пользователя.',
            'dependencies': 'MULTI_TARIFF_ENABLED',
        },
        'DEVICES_SELECTION_ENABLED': {
            'description': 'Разрешает пользователям выбирать количество устройств при покупке и продлении подписки.',
            'format': 'Булево значение.',
            'example': 'false',
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
        'CRYPTOBOT_ENABLED': {
            'description': 'Разрешает принимать криптоплатежи через CryptoBot.',
            'format': 'Булево значение.',
            'example': 'Включите после указания токена API и секрета вебхука.',
            'warning': 'Пустой токен или неверный вебхук приведут к отказам платежей.',
            'dependencies': 'CRYPTOBOT_API_TOKEN',
        },
        'PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED': {
            'description': (
                'Запускает фоновую проверку ожидающих пополнений и повторно обращается '
                'к платёжным провайдерам без участия администратора.'
            ),
            'format': 'Булево значение.',
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
            'format': 'Булево значение.',
            'example': 'true',
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
            'format': 'Булево значение.',
            'example': 'true',
            'warning': ('Используйте с осторожностью: средства будут списаны мгновенно, если корзина найдена.'),
        },
        'SUPPORT_TICKET_SLA_MINUTES': {
            'description': 'Лимит времени для ответа модераторов на тикет в минутах.',
            'format': 'Целое число от 1 до 1440.',
            'example': '5',
            'warning': 'Слишком низкое значение может вызвать частые напоминания, слишком высокое — ухудшить SLA.',
            'dependencies': 'SUPPORT_TICKET_SLA_ENABLED, SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES',
        },
        'MAINTENANCE_MODE': {
            'description': 'Переводит бота в режим технического обслуживания и скрывает действия для пользователей.',
            'format': 'Булево значение.',
            'example': 'Включено на время плановых работ.',
            'warning': 'Не забудьте отключить после завершения работ, иначе бот останется недоступен.',
            'dependencies': 'MAINTENANCE_MESSAGE, MAINTENANCE_CHECK_INTERVAL',
        },
        'MAINTENANCE_MONITORING_ENABLED': {
            'description': ('Управляет автоматическим запуском мониторинга панели Remnawave при старте бота.'),
            'format': 'Булево значение.',
            'example': 'false',
            'warning': ('При отключении мониторинг можно запустить вручную из панели администратора.'),
            'dependencies': 'MAINTENANCE_CHECK_INTERVAL',
        },
        'MAINTENANCE_RETRY_ATTEMPTS': {
            'description': ('Сколько раз повторять проверку панели Remnawave перед фиксацией недоступности.'),
            'format': 'Целое число не меньше 1.',
            'example': '3',
            'warning': (
                'Большие значения увеличивают время реакции на реальные сбои, но помогают избежать ложных срабатываний.'
            ),
            'dependencies': 'MAINTENANCE_CHECK_INTERVAL',
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
        'REMNAWAVE_API_URL': {
            'description': 'Базовый адрес панели RemnaWave, с которой синхронизируется бот.',
            'format': 'Полный URL вида https://panel.example.com.',
            'example': 'https://panel.remnawave.net',
            'warning': 'Недоступный адрес приведет к ошибкам при управлении VPN-учетками.',
            'dependencies': 'REMNAWAVE_API_KEY или REMNAWAVE_USERNAME/REMNAWAVE_PASSWORD',
        },
        'REMNAWAVE_AUTO_SYNC_ENABLED': {
            'description': 'Автоматически запускает синхронизацию пользователей и серверов с панелью RemnaWave.',
            'format': 'Булево значение.',
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
        'TRIAL_USER_TAG': {
            'description': (
                'Тег, который бот передаст пользователю при активации триальной подписки в панели RemnaWave.'
            ),
            'format': 'До 16 символов: заглавные A-Z, цифры и подчёркивание.',
            'example': 'TRIAL_USER',
            'warning': 'Неверный формат будет проигнорирован при создании пользователя.',
            'dependencies': 'Активация триала и включенная интеграция с RemnaWave',
        },
        'PAID_SUBSCRIPTION_USER_TAG': {
            'description': ('Тег, который бот ставит пользователю при покупке платной подписки в панели RemnaWave.'),
            'format': 'До 16 символов: заглавные A-Z, цифры и подчёркивание.',
            'example': 'PAID_USER',
            'warning': 'Если тег не задан или невалиден, существующий тег не будет изменён.',
            'dependencies': 'Оплата подписки и интеграция с RemnaWave',
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
        'TRAFFIC_MONITORING_ENABLED': {
            'description': (
                'Включает автоматический мониторинг трафика пользователей. '
                'Система отслеживает изменения трафика (дельту) и сохраняет snapshot в Redis. '
                'При превышении порогов отправляются уведомления пользователям и админам.'
            ),
            'format': 'Булево значение.',
            'example': 'true',
            'warning': (
                'Требует настроенного подключения к Redis. '
                'При включении будет запущен фоновый мониторинг трафика по расписанию.'
            ),
            'dependencies': 'Redis, TRAFFIC_MONITORING_INTERVAL_HOURS, TRAFFIC_SNAPSHOT_TTL_HOURS',
        },
        'TRAFFIC_MONITORING_INTERVAL_HOURS': {
            'description': (
                'Интервал проверки трафика в часах. '
                'Каждые N часов система проверяет трафик всех активных пользователей и сравнивает с предыдущим snapshot.'
            ),
            'format': 'Целое число часов (минимум 1).',
            'example': '24',
            'warning': (
                'Слишком маленький интервал может создать большую нагрузку на RemnaWave API. '
                'Рекомендуется 24 часа для ежедневного мониторинга.'
            ),
            'dependencies': 'TRAFFIC_MONITORING_ENABLED',
        },
        'TRAFFIC_MONITORED_NODES': {
            'description': (
                'Список UUID нод для мониторинга трафика через запятую. '
                'Если пусто - мониторятся все ноды. '
                'Позволяет ограничить мониторинг только определенными серверами.'
            ),
            'format': 'UUID через запятую или пусто для всех нод.',
            'example': 'd4aa2b8c-9a36-4f31-93a2-6f07dad05fba, a1b2c3d4-5678-90ab-cdef-1234567890ab',
            'warning': 'UUID должны существовать в RemnaWave, иначе мониторинг не будет работать.',
            'dependencies': 'TRAFFIC_MONITORING_ENABLED',
        },
        'TRAFFIC_SNAPSHOT_TTL_HOURS': {
            'description': (
                'Время жизни (TTL) snapshot трафика в Redis в часах. '
                'Snapshot используется для вычисления дельты (изменения трафика) между проверками. '
                'После истечения TTL snapshot удаляется и создается новый.'
            ),
            'format': 'Целое число часов (минимум 1).',
            'example': '24',
            'warning': (
                'TTL должен быть >= интервала мониторинга. '
                'Если TTL меньше интервала, snapshot будет удален до следующей проверки.'
            ),
            'dependencies': 'TRAFFIC_MONITORING_ENABLED, Redis',
        },
        'TRAFFIC_FAST_CHECK_ENABLED': {
            'description': (
                'Включает быструю проверку трафика. '
                'Система сравнивает текущий трафик со snapshot и уведомляет о превышениях дельты.'
            ),
            'format': 'Булево значение.',
            'example': 'true',
            'warning': 'Требует Redis для хранения snapshot. При отключении проверки не выполняются.',
            'dependencies': 'Redis, TRAFFIC_FAST_CHECK_INTERVAL_MINUTES, TRAFFIC_FAST_CHECK_THRESHOLD_GB',
        },
        'TRAFFIC_FAST_CHECK_INTERVAL_MINUTES': {
            'description': 'Интервал быстрой проверки трафика в минутах.',
            'format': 'Целое число минут (минимум 1).',
            'example': '10',
            'warning': 'Слишком малый интервал создаёт нагрузку на Remnawave API.',
            'dependencies': 'TRAFFIC_FAST_CHECK_ENABLED',
        },
        'TRAFFIC_FAST_CHECK_THRESHOLD_GB': {
            'description': 'Порог дельты трафика в ГБ для быстрой проверки. При превышении отправляется уведомление.',
            'format': 'Число с плавающей точкой.',
            'example': '5.0',
            'warning': 'Слишком низкий порог приведёт к частым уведомлениям.',
            'dependencies': 'TRAFFIC_FAST_CHECK_ENABLED',
        },
        'TRAFFIC_DAILY_CHECK_ENABLED': {
            'description': 'Включает суточную проверку трафика через bandwidth-stats API.',
            'format': 'Булево значение.',
            'example': 'true',
            'warning': 'Проверка выполняется в указанное время (TRAFFIC_DAILY_CHECK_TIME).',
            'dependencies': 'TRAFFIC_DAILY_CHECK_TIME, TRAFFIC_DAILY_THRESHOLD_GB',
        },
        'TRAFFIC_DAILY_CHECK_TIME': {
            'description': 'Время суточной проверки трафика в формате HH:MM (UTC).',
            'format': 'Строка времени HH:MM.',
            'example': '00:00',
            'warning': 'Время указывается в UTC.',
            'dependencies': 'TRAFFIC_DAILY_CHECK_ENABLED',
        },
        'TRAFFIC_DAILY_THRESHOLD_GB': {
            'description': 'Порог суточного трафика в ГБ. При превышении за 24 часа отправляется уведомление.',
            'format': 'Число с плавающей точкой.',
            'example': '50.0',
            'warning': 'Учитывается весь трафик за последние 24 часа.',
            'dependencies': 'TRAFFIC_DAILY_CHECK_ENABLED',
        },
        'TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES': {
            'description': 'Кулдаун уведомлений по одному пользователю в минутах.',
            'format': 'Целое число минут.',
            'example': '60',
            'warning': 'Защита от спама уведомлениями по одному и тому же пользователю.',
        },
        'REMNAWAVE_WEBHOOK_NOTIFY_NODE_CONNECTION_STATUS': {
            'description': (
                'Уведомления администраторам о потере и восстановлении соединения с нодами из webhook-ов RemnaWave.'
            ),
            'format': 'Булево значение.',
            'example': 'false',
            'warning': (
                'Отключает только события node.connection_lost и node.connection_restored. '
                'Остальные инфраструктурные уведомления продолжают отправляться.'
            ),
            'dependencies': 'REMNAWAVE_WEBHOOK_ENABLED, ADMIN_NOTIFICATIONS_ENABLED',
        },
        'WEBHOOK_NOTIFY_USER_ENABLED': {
            'description': (
                'Глобальный переключатель уведомлений пользователям от вебхуков RemnaWave. '
                'При выключении ни одно уведомление не отправляется, независимо от остальных настроек.'
            ),
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_SUB_STATUS': {
            'description': 'Уведомления об отключении и активации подписки администратором.',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_SUB_EXPIRED': {
            'description': 'Уведомления об истечении подписки.',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_SUB_EXPIRING': {
            'description': 'Предупреждения о скором истечении подписки (72ч, 48ч, 24ч до окончания).',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_SUB_LIMITED': {
            'description': 'Уведомление при достижении лимита трафика.',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_TRAFFIC_RESET': {
            'description': 'Уведомление о сбросе счётчика трафика.',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_SUB_DELETED': {
            'description': 'Уведомление при удалении пользователя из панели.',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_SUB_REVOKED': {
            'description': 'Уведомление при обновлении ключей подписки (revoke).',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_FIRST_CONNECTED': {
            'description': 'Уведомление при первом подключении к VPN.',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_NOT_CONNECTED': {
            'description': 'Напоминание, что пользователь ещё не подключился к VPN.',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_BANDWIDTH_THRESHOLD': {
            'description': 'Предупреждение при приближении к лимиту трафика (порог в %).',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_DEVICES': {
            'description': 'Уведомления о подключении и отключении устройств.',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'WEBHOOK_NOTIFY_TORRENT_DETECTED': {
            'description': 'Уведомление пользователю при обнаружении торрент-трафика.',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'RESET_TRAFFIC_ON_TARIFF_SWITCH': {
            'description': (
                'Автоматически сбрасывает счётчик использованного трафика '
                'при переключении пользователя на другой тарифный план. '
                'Сброс происходит через RemnaWave API.'
            ),
            'format': 'Булево значение: выберите "Включить" или "Выключить".',
            'example': 'Включено — трафик обнуляется при каждой смене тарифа.',
            'warning': 'При отключении использованный трафик сохранится после смены тарифа.',
        },
        'RESET_TRAFFIC_ON_PAYMENT': {
            'description': (
                'Автоматически сбрасывает счётчик использованного трафика при любой оплате или продлении подписки.'
            ),
            'format': 'Булево значение: выберите "Включить" или "Выключить".',
            'example': 'Выключено по умолчанию.',
            'warning': 'При включении трафик будет обнуляться при каждом продлении подписки.',
        },
        'TELEGRAM_WIDGET_SIZE': {
            'description': 'Размер кнопки виджета Telegram на странице авторизации.',
            'format': 'Выберите один из доступных размеров.',
            'example': 'large',
        },
        'TELEGRAM_WIDGET_RADIUS': {
            'description': 'Радиус скругления углов кнопки виджета Telegram (в пикселях).',
            'format': 'Целое число от 0 до 20.',
            'example': '8',
            'warning': 'Максимум: 20 для large, 14 для medium, 10 для small.',
        },
        'TELEGRAM_WIDGET_USERPIC': {
            'description': 'Показывать ли аватар пользователя в виджете Telegram после авторизации.',
            'format': 'Булево значение.',
            'example': 'true',
        },
        'TELEGRAM_WIDGET_REQUEST_ACCESS': {
            'description': 'Запрашивать ли у пользователя разрешение на отправку сообщений боту.',
            'format': 'Булево значение.',
            'example': 'true',
            'warning': 'При отключении бот не сможет писать пользователю первым.',
        },
        'TELEGRAM_OIDC_ENABLED': {
            'description': 'Включить авторизацию через новый Telegram Login (OpenID Connect). При включении заменяет legacy виджет.',
            'format': 'Булево значение.',
            'example': 'true',
            'warning': 'Требует заполнения CLIENT_ID и CLIENT_SECRET из BotFather.',
        },
        'TELEGRAM_OIDC_CLIENT_ID': {
            'description': 'ID бота (числовой) из BotFather > Bot Settings > Web Login.',
            'format': 'Числовой ID бота.',
            'example': '8521897198',
            'warning': 'Должен совпадать с ID бота, используемого для авторизации.',
        },
        'TELEGRAM_OIDC_CLIENT_SECRET': {
            'description': 'Секрет для OIDC из BotFather > Bot Settings > Web Login.',
            'format': 'Строка-секрет.',
            'example': 'xxxxxxxxxxxxxxxxxxxxxxxx',
            'warning': 'НЕ совпадает с BOT_TOKEN. Получается отдельно в BotFather.',
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

        # После загрузки всех overrides (включая SALES_MODE) — пересчитать цены,
        # т.к. ensure_tariffs_synced мог загрузить тарифные цены до того как
        # SALES_MODE=classic был применён из system_settings
        refresh_period_prices()
        refresh_classic_period_prices()

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

        if key == 'SALES_MODE' and settings.is_tariffs_mode():
            from app.database.crud.tariff import load_period_prices_from_db

            await load_period_prices_from_db(db)

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

        if key == 'SALES_MODE' and settings.is_tariffs_mode():
            from app.database.crud.tariff import load_period_prices_from_db

            await load_period_prices_from_db(db)

    @classmethod
    def _apply_to_settings(cls, key: str, value: Any) -> None:
        if cls._is_env_override(key):
            logger.debug('Пропуск применения настройки : значение задано через окружение', key=key)
            return
        try:
            setattr(settings, key, value)
            if key == 'SALES_MODE':
                if settings.is_classic_mode():
                    clear_db_period_prices()
                refresh_period_prices()
                refresh_classic_period_prices()
            elif key in {
                'PRICE_14_DAYS',
                'PRICE_30_DAYS',
                'PRICE_60_DAYS',
                'PRICE_90_DAYS',
                'PRICE_180_DAYS',
                'PRICE_360_DAYS',
            }:
                refresh_period_prices()
                refresh_classic_period_prices()
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
