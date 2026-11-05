import html
import io
import math
import time
from collections.abc import Iterable
from datetime import UTC, datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.server_squad import (
    get_all_server_squads,
    get_server_squad_by_id,
    get_server_squad_by_uuid,
)
from app.database.models import SystemSetting, User
from app.external.telegram_stars import TelegramStarsService
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.services.remnawave_service import RemnaWaveService
from app.services.system_settings_service import (
    ReadOnlySettingError,
    bot_configuration_service,
)
from app.services.tribute_service import TributeService
from app.states import BotConfigStates
from app.utils.currency_converter import currency_converter
from app.utils.decorators import admin_required, error_handler
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

CATEGORY_PAGE_SIZE = 10
SETTINGS_PAGE_SIZE = 8
SIMPLE_SUBSCRIPTION_SQUADS_PAGE_SIZE = 6

CATEGORY_GROUP_METADATA: dict[str, dict[str, object]] = {
    'core': {
        'title': '🤖 Основные',
        'description': 'Базовые настройки бота, обязательные каналы и ключевые сервисы.',
        'icon': '🤖',
        'categories': (
            'CORE',
            'CHANNEL',
            'TIMEZONE',
            'DATABASE',
            'POSTGRES',
            'SQLITE',
            'REDIS',
            'REMNAWAVE',
        ),
    },
    'support': {
        'title': '💬 Поддержка',
        'description': 'Контакты, режимы тикетов, SLA и уведомления модераторов.',
        'icon': '💬',
        'categories': ('SUPPORT',),
    },
    'payments': {
        'title': '💳 Платежные системы',
        'description': (
            'YooKassa, CryptoBot, Heleket, CloudPayments, Freekassa, MulenPay, PAL24, Wata, '
            'Platega, Tribute, Kassa AI, RioPay, SeverPay, RollyPay, Overpay, AuraPay, '
            'Etoplatezhi, Antilopay, Jupiter, CisPay, Anore, TabPay, ParityPay, Donut, Lava, Cashera и Telegram Stars.'
        ),
        'icon': '💳',
        'categories': (
            'PAYMENT',
            'PAYMENT_VERIFICATION',
            'YOOKASSA',
            'CRYPTOBOT',
            'HELEKET',
            'CLOUDPAYMENTS',
            'FREEKASSA',
            'KASSA_AI',
            'RIOPAY',
            'SEVERPAY',
            'ROLLYPAY',
            'OVERPAY',
            'AURAPAY',
            'ETOPLATEZHI',
            'ANTILOPAY',
            'JUPITER',
            'CISPAY',
            'CASHERA',
            'ANORE',
            'TABPAY',
            'PARITYPAY',
            'DONUT',
            'LAVA',
            'MULENPAY',
            'PAL24',
            'WATA',
            'PLATEGA',
            'TRIBUTE',
            'TELEGRAM',
        ),
    },
    'subscriptions': {
        'title': '📅 Подписки и цены',
        'description': 'Тарифы, простая покупка, периоды, лимиты трафика и автопродление.',
        'icon': '📅',
        'categories': (
            'SUBSCRIPTIONS_CORE',
            'SIMPLE_SUBSCRIPTION',
            'PERIODS',
            'SUBSCRIPTION_PRICES',
            'TRAFFIC',
            'TRAFFIC_PACKAGES',
            'AUTOPAY',
        ),
    },
    'trial': {
        'title': '🎁 Пробный период',
        'description': 'Длительность и ограничения бесплатного доступа.',
        'icon': '🎁',
        'categories': ('TRIAL',),
    },
    'referral': {
        'title': '👥 Реферальная программа',
        'description': 'Бонусы, пороги и уведомления для партнеров.',
        'icon': '👥',
        'categories': ('REFERRAL',),
    },
    'notifications': {
        'title': '🔔 Уведомления',
        'description': 'Пользовательские, админские оповещения и отчеты.',
        'icon': '🔔',
        'categories': ('NOTIFICATIONS', 'ADMIN_NOTIFICATIONS', 'ADMIN_REPORTS'),
    },
    'interface': {
        'title': '🎨 Интерфейс и брендинг',
        'description': 'Логотип, тексты, языки, главное меню, miniapp и deep links.',
        'icon': '🎨',
        'categories': (
            'INTERFACE',
            'INTERFACE_BRANDING',
            'INTERFACE_SUBSCRIPTION',
            'INFO_PAGES',
            'CONNECT_BUTTON',
            'MINIAPP',
            'HAPP',
            'SKIP',
            'LOCALIZATION',
            'ADDITIONAL',
        ),
    },
    'server': {
        'title': '📊 Статус серверов',
        'description': 'Мониторинг серверов, SLA и внешние метрики.',
        'icon': '📊',
        'categories': ('SERVER_STATUS', 'MONITORING'),
    },
    'maintenance': {
        'title': '🔧 Обслуживание',
        'description': 'Режим техработ, бэкапы и проверка обновлений.',
        'icon': '🔧',
        'categories': ('MAINTENANCE', 'BACKUP', 'VERSION'),
    },
    'advanced': {
        'title': '⚡ Расширенные',
        'description': 'Web API, webhook, логирование, модерация и режим отладки.',
        'icon': '⚡',
        'categories': (
            'WEB_API',
            'WEBHOOK',
            'LOG',
            'MODERATION',
            'DEBUG',
        ),
    },
}

CATEGORY_GROUP_ORDER: tuple[str, ...] = (
    'core',
    'support',
    'payments',
    'subscriptions',
    'trial',
    'referral',
    'notifications',
    'interface',
    'server',
    'maintenance',
    'advanced',
)

CATEGORY_GROUP_DEFINITIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
    (
        group_key,
        str(CATEGORY_GROUP_METADATA[group_key]['title']),
        tuple(CATEGORY_GROUP_METADATA[group_key]['categories']),
    )
    for group_key in CATEGORY_GROUP_ORDER
)

CATEGORY_TO_GROUP: dict[str, str] = {}
for _group_key, _title, _category_keys in CATEGORY_GROUP_DEFINITIONS:
    for _category_key in _category_keys:
        CATEGORY_TO_GROUP[_category_key] = _group_key

CATEGORY_FALLBACK_KEY = 'other'
CATEGORY_FALLBACK_TITLE = '📦 Прочие настройки'

PRESET_CONFIGS: dict[str, dict[str, object]] = {
    'recommended': {
        'ENABLE_NOTIFICATIONS': True,
        'ADMIN_NOTIFICATIONS_ENABLED': True,
        'ADMIN_REPORTS_ENABLED': True,
        'MONITORING_INTERVAL': 60,
        'TRIAL_DURATION_DAYS': 3,
    },
    'minimal': {
        'ENABLE_NOTIFICATIONS': False,
        'ADMIN_NOTIFICATIONS_ENABLED': False,
        'ADMIN_REPORTS_ENABLED': False,
        'TRIAL_DURATION_DAYS': 0,
        'REFERRAL_NOTIFICATIONS_ENABLED': False,
    },
    'secure': {
        'MAINTENANCE_AUTO_ENABLE': True,
        'ADMIN_NOTIFICATIONS_ENABLED': True,
        'ADMIN_REPORTS_ENABLED': True,
        'REFERRAL_MINIMUM_TOPUP_KOPEKS': 100000,
        'SERVER_STATUS_MODE': 'disabled',
    },
    'testing': {
        'DEBUG': True,
        'ENABLE_NOTIFICATIONS': False,
        'TRIAL_DURATION_DAYS': 7,
        'SERVER_STATUS_MODE': 'disabled',
        'ADMIN_NOTIFICATIONS_ENABLED': False,
    },
}

PRESET_METADATA: dict[str, dict[str, str]] = {
    'recommended': {
        'title': 'Рекомендуемые настройки',
        'description': 'Баланс между стабильностью и информированием команды.',
    },
    'minimal': {
        'title': 'Минимальная конфигурация',
        'description': 'Подходит для тестового запуска без уведомлений.',
    },
    'secure': {
        'title': 'Максимальная безопасность',
        'description': 'Усиленный контроль доступа и отключение лишних интеграций.',
    },
    'testing': {
        'title': 'Для тестирования',
        'description': 'Включает режим отладки и отключает внешние уведомления.',
    },
}


def _get_group_meta(group_key: str, language: str = 'ru') -> dict[str, object]:
    meta = dict(CATEGORY_GROUP_METADATA.get(group_key, {}))
    texts = get_texts(language)
    for field in ('title', 'description'):
        if field in meta:
            meta[field] = texts.t(f'BOT_SETTINGS_GROUP_{group_key.upper()}_{field.upper()}', str(meta[field]))
    return meta


def _get_preset_meta(preset_key: str, language: str = 'ru') -> dict[str, str]:
    meta = dict(PRESET_METADATA.get(preset_key, {'title': preset_key, 'description': ''}))
    texts = get_texts(language)
    if preset_key in PRESET_METADATA:
        for field in ('title', 'description'):
            meta[field] = texts.t(f'BOT_SETTINGS_PRESET_{preset_key.upper()}_{field.upper()}', meta[field])
    return meta


def _get_group_description(group_key: str, language: str = 'ru') -> str:
    meta = _get_group_meta(group_key, language=language)
    return str(meta.get('description', ''))


def _get_group_icon(group_key: str, language: str = 'ru') -> str:
    meta = _get_group_meta(group_key, language=language)
    return str(meta.get('icon', '⚙️'))


def _get_group_status(group_key: str, language: str = 'ru') -> tuple[str, str]:
    texts = get_texts(language)
    key = group_key
    if key == 'payments':
        payment_statuses = {
            'YooKassa': settings.is_yookassa_enabled(),
            'CryptoBot': settings.is_cryptobot_enabled(),
            'Platega': settings.is_platega_enabled(),
            'CloudPayments': settings.is_cloudpayments_enabled(),
            'Freekassa': settings.is_freekassa_enabled(),
            'Kassa AI': settings.is_kassa_ai_enabled(),
            'RioPay': settings.is_riopay_enabled(),
            'MulenPay': settings.is_mulenpay_enabled(),
            'PAL24': settings.is_pal24_enabled(),
            'Tribute': settings.TRIBUTE_ENABLED,
            'Stars': settings.TELEGRAM_STARS_ENABLED,
        }
        active = sum(1 for value in payment_statuses.values() if value)
        total = len(payment_statuses)
        if active == 0:
            return '🔴', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_000', 'Нет активных платежей')
        if active < total:
            return '🟡', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_001', 'Активно {value1} из {value2}').format(
                value1=active, value2=total
            )
        return '🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_002', 'Все системы активны')

    if key == 'remnawave':
        api_ready = bool(
            settings.REMNAWAVE_API_URL
            and (settings.REMNAWAVE_API_KEY or (settings.REMNAWAVE_USERNAME and settings.REMNAWAVE_PASSWORD))
        )
        return (
            ('🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_003', 'API подключено'))
            if api_ready
            else ('🟡', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_004', 'Нужно указать URL и ключи'))
        )

    if key == 'server':
        mode = (settings.SERVER_STATUS_MODE or '').lower()
        monitoring_active = mode not in {'', 'disabled'}
        if monitoring_active:
            return '🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_005', 'Мониторинг активен')
        if settings.MONITORING_INTERVAL:
            return '🟡', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_006', 'Доступны только отчеты')
        return '⚪', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_007', 'Мониторинг выключен')

    if key == 'maintenance':
        if settings.MAINTENANCE_MODE:
            return '🟡', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_008', 'Режим ТО включен')
        return '🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_009', 'Рабочий режим')

    if key == 'notifications':
        user_on = settings.is_notifications_enabled()
        admin_on = settings.is_admin_notifications_enabled()
        if user_on and admin_on:
            return '🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_010', 'Все уведомления включены')
        if user_on or admin_on:
            return '🟡', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_011', 'Часть уведомлений включена')
        return '⚪', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_012', 'Уведомления отключены')

    if key == 'trial':
        if settings.TRIAL_DURATION_DAYS > 0:
            return '🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_013', '{value1} дней пробного периода').format(
                value1=settings.TRIAL_DURATION_DAYS
            )
        return '⚪', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_014', 'Триал отключен')

    if key == 'referral':
        active = (
            settings.REFERRAL_COMMISSION_PERCENT
            or settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS
            or settings.REFERRAL_INVITER_BONUS_KOPEKS
        )
        return (
            ('🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_015', 'Программа активна'))
            if active
            else ('⚪', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_016', 'Бонусы не заданы'))
        )

    if key == 'core':
        token_ok = bool(getattr(settings, 'BOT_TOKEN', ''))
        # Channel subscription channels are now managed via DB (admin panel),
        # not a single CHANNEL_LINK setting. Dashboard cannot async-query DB here.
        if token_ok:
            return '🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_017', 'Бот готов к работе')
        return '🟡', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_018', 'Проверьте токен бота')

    if key == 'subscriptions':
        price_ready = settings.PRICE_30_DAYS > 0 and settings.AVAILABLE_SUBSCRIPTION_PERIODS
        return (
            ('🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_019', 'Тарифы настроены'))
            if price_ready
            else ('⚪', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_020', 'Нужно задать цены'))
        )

    if key == 'database':
        mode = (settings.DATABASE_MODE or 'auto').lower()
        if mode == 'postgresql':
            return '🟢', 'PostgreSQL'
        if mode == 'sqlite':
            return '🟡', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_021', 'SQLite режим')
        return '🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_022', 'Авто режим')

    if key == 'interface':
        branding = bool(settings.ENABLE_LOGO_MODE or settings.MINIAPP_CUSTOM_URL)
        return (
            ('🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_023', 'Брендинг настроен'))
            if branding
            else ('⚪', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_024', 'Настройки по умолчанию'))
        )

    return '🟢', texts.t('BOT_SETTINGS_UI_GET_GROUP_STATUS_025', 'Готово к работе')


def _get_setting_icon(definition, current_value: object) -> str:
    key_upper = definition.key.upper()

    if definition.python_type is bool:
        return '✅' if bool(current_value) else '❌'

    if bot_configuration_service.has_choices(definition.key):
        return '📋'

    if isinstance(current_value, (int, float)):
        return '🔢'

    if isinstance(current_value, str):
        if not current_value.strip():
            return '⚪'
        if 'URL' in key_upper:
            return '🔗'
        if any(keyword in key_upper for keyword in ('TOKEN', 'SECRET', 'PASSWORD', 'KEY')):
            return '🔒'

    if any(keyword in key_upper for keyword in ('TIME', 'HOUR', 'MINUTE')):
        return '⏱'
    if 'DAYS' in key_upper:
        return '📆'
    if 'GB' in key_upper or 'TRAFFIC' in key_upper:
        return '📊'

    return '⚙️'


def _render_dashboard_overview(language: str = 'ru') -> str:
    texts = get_texts(language)
    grouped = _get_grouped_categories(language=language)
    total_settings = 0
    total_overrides = 0

    for group_key, _title, items in grouped:
        for category_key, _label, count in items:
            total_settings += count
            definitions = bot_configuration_service.get_settings_for_category(category_key)
            total_overrides += sum(
                1 for definition in definitions if bot_configuration_service.has_override(definition.key)
            )

    lines: list[str] = [
        texts.t('BOT_SETTINGS_UI_RENDER_DASHBOARD_OVERVIEW_026', '⚙️ <b>ПАНЕЛЬ УПРАВЛЕНИЯ БОТОМ</b>'),
        '',
        texts.t(
            'BOT_SETTINGS_UI_RENDER_DASHBOARD_OVERVIEW_027',
            'Всего параметров: <b>{value1}</b> • Переопределено: <b>{value2}</b>',
        ).format(value1=total_settings, value2=total_overrides),
        '',
        texts.t('BOT_SETTINGS_UI_RENDER_DASHBOARD_OVERVIEW_028', '<b>Группы настроек</b>'),
        '',
    ]

    for group_key, title, items in grouped:
        status_icon, status_text = _get_group_status(group_key, language=language)
        total = sum(count for _, _, count in items)
        lines.append(f'{status_icon} <b>{title}</b> — {status_text}')
        lines.append(
            texts.t('BOT_SETTINGS_UI_RENDER_DASHBOARD_OVERVIEW_029', '└ Настроек: {value1}').format(value1=total)
        )
        lines.append('')

    lines.append(
        texts.t(
            'BOT_SETTINGS_UI_RENDER_DASHBOARD_OVERVIEW_030',
            '🔍 Используйте поиск, чтобы быстро найти нужный параметр по ключу или названию.',
        )
    )
    return '\n'.join(lines).strip()


def _build_group_category_index(language: str = 'ru') -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for group_key, _title, items in _get_grouped_categories(language=language):
        mapping[group_key] = [category_key for category_key, _label, _count in items]
    return mapping


def _perform_settings_search(query: str, language: str = 'ru') -> list[dict[str, object]]:
    normalized = query.strip().lower()
    if not normalized:
        return []

    categories = bot_configuration_service.get_categories()
    group_category_index = _build_group_category_index(language=language)
    results: list[dict[str, object]] = []

    for category_key, _label, _count in categories:
        definitions = bot_configuration_service.get_settings_for_category(category_key)
        group_key = CATEGORY_TO_GROUP.get(category_key, CATEGORY_FALLBACK_KEY)
        available_categories = group_category_index.get(group_key, [])
        if category_key in available_categories:
            category_index = available_categories.index(category_key)
            category_page = category_index // CATEGORY_PAGE_SIZE + 1
        else:
            category_page = 1

        for definition_index, definition in enumerate(definitions):
            fields = [definition.key.lower(), definition.display_name.lower()]
            guidance = bot_configuration_service.get_setting_guidance(definition.key, language=language)
            fields.extend(
                [
                    guidance.get('description', '').lower(),
                    guidance.get('format', '').lower(),
                    str(guidance.get('dependencies', '')).lower(),
                ]
            )

            if not any(normalized in field for field in fields if field):
                continue

            settings_page = definition_index // SETTINGS_PAGE_SIZE + 1
            results.append(
                {
                    'key': definition.key,
                    'name': definition.display_name,
                    'category_key': category_key,
                    'category_label': bot_configuration_service.get_category_label(definition.category_key, language),
                    'group_key': group_key,
                    'category_page': category_page,
                    'settings_page': settings_page,
                    'token': bot_configuration_service.get_callback_token(definition.key),
                    'value': bot_configuration_service.format_value_human(
                        definition.key, bot_configuration_service.get_current_value(definition.key), language=language
                    ),
                }
            )

    results.sort(key=lambda item: item['name'].lower())
    return results[:20]


def _build_search_results_keyboard(
    results: list[dict[str, object]], language: str = 'ru'
) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    rows: list[list[types.InlineKeyboardButton]] = []
    for result in results:
        group_key = str(result['group_key'])
        category_page = int(result['category_page'])
        settings_page = int(result['settings_page'])
        token = str(result['token'])
        text = f'{result["name"]}'
        if len(text) > 60:
            text = text[:59] + '…'
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=text,
                    callback_data=(f'botcfg_setting:{group_key}:{category_page}:{settings_page}:{token}'),
                )
            ]
        )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_SEARCH_RESULTS_KEYBOARD_031', '⬅️ В главное меню'),
                callback_data='admin_bot_config',
            )
        ]
    )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _parse_env_content(content: str) -> dict[str, str | None]:
    parsed: dict[str, str | None] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        parsed[key.strip()] = value.strip()
    return parsed


@admin_required
@error_handler
async def start_settings_search(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    await state.set_state(BotConfigStates.waiting_for_search_query)
    await state.update_data(botcfg_origin='bot_config')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_BUILD_SEARCH_RESULTS_KEYBOARD_031', '⬅️ В главное меню'),
                    callback_data='admin_bot_config',
                )
            ]
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'BOT_SETTINGS_UI_START_SETTINGS_SEARCH_033',
            '🔍 <b>Поиск по настройкам</b>\n\nОтправьте часть ключа или названия настройки. \nНапример: <code>yookassa</code> или <code>уведомления</code>.',
        ),
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer(texts.t('BOT_SETTINGS_UI_START_SETTINGS_SEARCH_034', 'Введите запрос'), show_alert=False)


@admin_required
@error_handler
async def handle_search_query(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    if message.chat.type != 'private':
        return

    data = await state.get_data()
    if data.get('botcfg_origin') != 'bot_config':
        return

    query = (message.text or '').strip()
    results = _perform_settings_search(query, language=db_user.language)

    if results:
        keyboard = _build_search_results_keyboard(results, language=db_user.language)
        lines = [
            texts.t('BOT_SETTINGS_UI_HANDLE_SEARCH_QUERY_035', '🔍 <b>Результаты поиска</b>'),
            texts.t('BOT_SETTINGS_UI_HANDLE_SEARCH_QUERY_036', 'Запрос: <code>{value1}</code>').format(
                value1=html.escape(query)
            ),
            '',
        ]
        for index, item in enumerate(results, start=1):
            lines.append(f'{index}. {item["name"]} — {item["value"]} ({item["category_label"]})')
        text = '\n'.join(lines)
    else:
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_HANDLE_SEARCH_QUERY_037', '⬅️ Попробовать снова'),
                        callback_data='botcfg_action:search',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_APPLY_PRESET_052', '🏠 Главное меню'),
                        callback_data='admin_bot_config',
                    )
                ],
            ]
        )
        text = texts.t(
            'BOT_SETTINGS_UI_HANDLE_SEARCH_QUERY_039',
            '🔍 <b>Результаты поиска</b>\n\nЗапрос: <code>{value1}</code>\n\nНичего не найдено. Попробуйте изменить формулировку.',
        ).format(value1=html.escape(query))

    await message.answer(text, parse_mode='HTML', reply_markup=keyboard)
    await state.clear()


@admin_required
@error_handler
async def show_presets(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    lines = [
        texts.t('BOT_SETTINGS_UI_SHOW_PRESETS_040', '🎯 <b>Готовые пресеты</b>'),
        '',
        texts.t('BOT_SETTINGS_UI_SHOW_PRESETS_041', 'Выберите набор параметров, чтобы быстро применить его к боту.'),
        '',
    ]
    for key in PRESET_METADATA:
        meta = _get_preset_meta(key, db_user.language)
        lines.append(f'• <b>{meta["title"]}</b> — {meta["description"]}')
    text = '\n'.join(lines)

    buttons: list[types.InlineKeyboardButton] = []
    for key in PRESET_METADATA:
        meta = _get_preset_meta(key, db_user.language)
        buttons.append(types.InlineKeyboardButton(text=meta['title'], callback_data=f'botcfg_preset:{key}'))

    rows: list[list[types.InlineKeyboardButton]] = []
    for chunk in _chunk(buttons, 2):
        rows.append(list(chunk))
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_SHOW_PRESETS_042', '⬅️ Главное меню'), callback_data='admin_bot_config'
            )
        ]
    )

    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


def _format_preset_preview(preset_key: str, language: str = 'ru') -> tuple[str, list[str]]:
    texts = get_texts(language)
    config = PRESET_CONFIGS.get(preset_key, {})
    meta = _get_preset_meta(preset_key, language)
    title = meta['title']
    description = meta.get('description', '')

    lines = [f'🎯 <b>{title}</b>']
    if description:
        lines.append(description)
    lines.append('')
    lines.append(texts.t('BOT_SETTINGS_UI_FORMAT_PRESET_PREVIEW_043', 'Будут установлены следующие значения:'))

    for index, (setting_key, new_value) in enumerate(config.items(), start=1):
        current_value = bot_configuration_service.get_current_value(setting_key)
        current_pretty = bot_configuration_service.format_value_human(setting_key, current_value, language=language)
        new_pretty = bot_configuration_service.format_value_human(setting_key, new_value, language=language)
        lines.append(
            texts.t(
                'BOT_SETTINGS_UI_FORMAT_PRESET_PREVIEW_044',
                '{value1}. <code>{value2}</code>\n   Текущее: {value3}\n   Новое: {value4}',
            ).format(value1=index, value2=setting_key, value3=current_pretty, value4=new_pretty)
        )

    return title, lines


@admin_required
@error_handler
async def preview_preset(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 1)
    preset_key = parts[1] if len(parts) > 1 else ''
    if preset_key not in PRESET_CONFIGS:
        await callback.answer(texts.t('BOT_SETTINGS_UI_PREVIEW_PRESET_045', 'Этот пресет недоступен'), show_alert=True)
        return

    title, lines = _format_preset_preview(preset_key, language=db_user.language)
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_PREVIEW_PRESET_046', '✅ Применить'),
                    callback_data=f'botcfg_preset_apply:{preset_key}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_BUILD_SETTING_KEYBOARD_099', '⬅️ Назад'),
                    callback_data='botcfg_action:presets',
                )
            ],
        ]
    )

    await callback.message.edit_text(
        '\n'.join(lines),
        parse_mode='HTML',
        reply_markup=keyboard,
    )
    await callback.answer()


@admin_required
@error_handler
async def apply_preset(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 1)
    preset_key = parts[1] if len(parts) > 1 else ''
    config = PRESET_CONFIGS.get(preset_key)
    if not config:
        await callback.answer(texts.t('BOT_SETTINGS_UI_PREVIEW_PRESET_045', 'Этот пресет недоступен'), show_alert=True)
        return

    applied: list[str] = []
    for setting_key, value in config.items():
        try:
            await bot_configuration_service.set_value(db, setting_key, value)
            applied.append(setting_key)
        except ReadOnlySettingError:
            logger.info(
                'Пропускаем настройку из пресета: только для чтения', setting_key=setting_key, preset_key=preset_key
            )
        except Exception as error:
            logger.warning(
                'Не удалось применить настройку из пресета', preset_key=preset_key, setting_key=setting_key, error=error
            )
    await db.commit()

    title = _get_preset_meta(preset_key, db_user.language)['title']
    summary_lines = [
        texts.t('BOT_SETTINGS_UI_APPLY_PRESET_049', '✅ Пресет <b>{value1}</b> применен').format(value1=title),
        '',
        texts.t('BOT_SETTINGS_UI_APPLY_PRESET_050', 'Изменено параметров: <b>{value1}</b>').format(value1=len(applied)),
    ]
    if applied:
        summary_lines.append('\n'.join(f'• <code>{key}</code>' for key in applied))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_APPLY_PRESET_051', '⬅️ К пресетам'),
                    callback_data='botcfg_action:presets',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_APPLY_PRESET_052', '🏠 Главное меню'),
                    callback_data='admin_bot_config',
                )
            ],
        ]
    )

    await callback.message.edit_text(
        '\n'.join(summary_lines),
        parse_mode='HTML',
        reply_markup=keyboard,
    )
    await callback.answer(texts.t('BOT_SETTINGS_UI_APPLY_PRESET_053', 'Настройки обновлены'), show_alert=False)


@admin_required
@error_handler
async def export_settings(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    categories = bot_configuration_service.get_categories()
    keys: list[str] = []
    for category_key, _label, _count in categories:
        for definition in bot_configuration_service.get_settings_for_category(category_key):
            keys.append(definition.key)

    keys = sorted(set(keys))
    lines = [
        '# RemnaWave bot configuration export',
        f'# Generated at {datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")}',
    ]

    for setting_key in keys:
        current_value = bot_configuration_service.get_current_value(setting_key)
        raw_value = bot_configuration_service.serialize_value(setting_key, current_value)
        if raw_value is None:
            raw_value = ''
        lines.append(f'{setting_key}={raw_value}')

    content = '\n'.join(lines)
    filename = f'bot-settings-{datetime.now(UTC).strftime("%Y%m%d-%H%M%S")}.env'
    file = types.BufferedInputFile(content.encode('utf-8'), filename=filename)

    await callback.message.answer_document(
        document=file,
        caption=texts.t('BOT_SETTINGS_UI_EXPORT_SETTINGS_054', '📤 Экспорт текущих настроек'),
        parse_mode='HTML',
    )
    await callback.answer(texts.t('BOT_SETTINGS_UI_EXPORT_SETTINGS_055', 'Файл готов'), show_alert=False)


@admin_required
@error_handler
async def start_import_settings(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    await state.set_state(BotConfigStates.waiting_for_import_file)
    await state.update_data(botcfg_origin='bot_config')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_SHOW_PRESETS_042', '⬅️ Главное меню'), callback_data='admin_bot_config'
                )
            ]
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'BOT_SETTINGS_UI_START_IMPORT_SETTINGS_057',
            '📥 <b>Импорт настроек</b>\n\nПрикрепите .env файл или отправьте текстом пары <code>KEY=value</code>.\nНеизвестные параметры будут проигнорированы.',
        ),
        parse_mode='HTML',
        reply_markup=keyboard,
    )
    await callback.answer(texts.t('BOT_SETTINGS_UI_START_IMPORT_SETTINGS_058', 'Загрузите файл .env'), show_alert=False)


@admin_required
@error_handler
async def handle_import_message(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    if message.chat.type != 'private':
        return

    data = await state.get_data()
    if data.get('botcfg_origin') != 'bot_config':
        return

    content = ''
    if message.document:
        buffer = io.BytesIO()
        await message.bot.download(message.document, destination=buffer)
        buffer.seek(0)
        content = buffer.read().decode('utf-8', errors='ignore')
    else:
        content = message.text or ''

    parsed = _parse_env_content(content)
    if not parsed:
        await message.answer(
            texts.t(
                'BOT_SETTINGS_UI_HANDLE_IMPORT_MESSAGE_059',
                '❌ Не удалось найти параметры в файле. Убедитесь, что используется формат KEY=value.',
            ),
            parse_mode='HTML',
        )
        await state.clear()
        return

    applied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for setting_key, raw_value in parsed.items():
        try:
            bot_configuration_service.get_definition(setting_key)
        except KeyError:
            skipped.append(setting_key)
            continue

        value_to_apply: object | None
        try:
            if raw_value in {'', '""'}:
                value_to_apply = None
            else:
                value_to_apply = bot_configuration_service.deserialize_value(
                    setting_key, raw_value, language=db_user.language
                )
        except Exception as error:
            errors.append(f'{setting_key}: {error}')
            continue

        if bot_configuration_service.is_read_only(setting_key):
            skipped.append(setting_key)
            continue
        try:
            await bot_configuration_service.set_value(db, setting_key, value_to_apply)
            applied.append(setting_key)
        except ReadOnlySettingError:
            skipped.append(setting_key)
        except ValueError as error:
            errors.append(f'{setting_key}: {error}')

    await db.commit()

    summary_lines = [
        texts.t('BOT_SETTINGS_UI_HANDLE_IMPORT_MESSAGE_060', '📥 <b>Импорт завершен</b>'),
        texts.t('BOT_SETTINGS_UI_HANDLE_IMPORT_MESSAGE_061', 'Обновлено параметров: <b>{value1}</b>').format(
            value1=len(applied)
        ),
    ]
    if applied:
        summary_lines.append('\n'.join(f'• <code>{key}</code>' for key in applied))

    if skipped:
        summary_lines.append(texts.t('BOT_SETTINGS_UI_HANDLE_IMPORT_MESSAGE_062', '\nПропущено (неизвестные ключи):'))
        summary_lines.append('\n'.join(f'• <code>{key}</code>' for key in skipped))

    if errors:
        summary_lines.append(texts.t('BOT_SETTINGS_UI_HANDLE_IMPORT_MESSAGE_063', '\nОшибки разбора:'))
        summary_lines.append('\n'.join(f'• {html.escape(err)}' for err in errors))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_APPLY_PRESET_052', '🏠 Главное меню'),
                    callback_data='admin_bot_config',
                )
            ]
        ]
    )

    await message.answer('\n'.join(summary_lines), parse_mode='HTML', reply_markup=keyboard)
    await state.clear()


@admin_required
@error_handler
async def show_settings_history(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    result = await db.execute(select(SystemSetting).order_by(SystemSetting.updated_at.desc()).limit(10))
    rows = result.scalars().all()

    lines = [texts.t('BOT_SETTINGS_UI_SHOW_SETTINGS_HISTORY_065', '🕘 <b>История изменений</b>'), '']
    if rows:
        for row in rows:
            timestamp = row.updated_at or row.created_at
            ts_text = format_local_datetime(timestamp, '%d.%m %H:%M') if timestamp else '—'
            try:
                parsed_value = bot_configuration_service.deserialize_value(row.key, row.value)
                formatted_value = bot_configuration_service.format_value_human(
                    row.key, parsed_value, language=db_user.language
                )
            except Exception:
                formatted_value = row.value or '—'
            lines.append(f'{ts_text} • <code>{row.key}</code> = {formatted_value}')
    else:
        lines.append(texts.t('BOT_SETTINGS_UI_SHOW_SETTINGS_HISTORY_066', 'История изменений пуста.'))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_SHOW_PRESETS_042', '⬅️ Главное меню'), callback_data='admin_bot_config'
                )
            ]
        ]
    )

    await callback.message.edit_text('\n'.join(lines), parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_help(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    text = texts.t(
        'BOT_SETTINGS_UI_SHOW_HELP_068',
        '❓ <b>Как работать с панелью</b>\n\n• Навигируйте по категориям, чтобы увидеть связанные настройки.\n• Значок ✳️ рядом с параметром означает, что значение переопределено.\n• Используйте 🔍 поиск для быстрого доступа к нужной настройке.\n• Экспортируйте .env перед крупными изменениями, чтобы иметь резервную копию.\n• Импорт позволяет восстановить конфигурацию или применить шаблон.\n• Все секретные ключи скрываются в интерфейсе автоматически.',
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_APPLY_PRESET_052', '🏠 Главное меню'),
                    callback_data='admin_bot_config',
                )
            ]
        ]
    )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


async def _store_setting_context(
    state: FSMContext,
    *,
    key: str,
    group_key: str,
    category_page: int,
    settings_page: int,
) -> None:
    await state.update_data(
        setting_key=key,
        setting_group_key=group_key,
        setting_category_page=category_page,
        setting_settings_page=settings_page,
        botcfg_origin='bot_config',
        botcfg_timestamp=time.time(),
    )


class BotConfigInputFilter(BaseFilter):
    def __init__(self, timeout: float = 300.0) -> None:
        self.timeout = timeout

    async def __call__(
        self,
        message: types.Message,
        state: FSMContext,
    ) -> bool:
        if not message.text or message.text.startswith('/'):
            return False

        if message.chat.type != 'private':
            return False

        data = await state.get_data()

        if data.get('botcfg_origin') != 'bot_config':
            return False

        if not data.get('setting_key'):
            return False

        timestamp = data.get('botcfg_timestamp')
        if timestamp is None:
            return True

        try:
            return (time.time() - float(timestamp)) <= self.timeout
        except (TypeError, ValueError):
            return False


def _chunk(buttons: Iterable[types.InlineKeyboardButton], size: int) -> Iterable[list[types.InlineKeyboardButton]]:
    buttons_list = list(buttons)
    for index in range(0, len(buttons_list), size):
        yield buttons_list[index : index + size]


def _parse_category_payload(payload: str) -> tuple[str, str, int, int]:
    parts = payload.split(':')
    group_key = parts[1] if len(parts) > 1 else CATEGORY_FALLBACK_KEY
    category_key = parts[2] if len(parts) > 2 else ''

    def _safe_int(value: str, default: int = 1) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    category_page = _safe_int(parts[3]) if len(parts) > 3 else 1
    settings_page = _safe_int(parts[4]) if len(parts) > 4 else 1
    return group_key, category_key, category_page, settings_page


def _parse_group_payload(payload: str) -> tuple[str, int]:
    parts = payload.split(':')
    group_key = parts[1] if len(parts) > 1 else CATEGORY_FALLBACK_KEY
    try:
        page = max(1, int(parts[2]))
    except (IndexError, ValueError):
        page = 1
    return group_key, page


def _get_grouped_categories(language: str = 'ru') -> list[tuple[str, str, list[tuple[str, str, int]]]]:
    categories = bot_configuration_service.get_categories()
    categories_map = {
        key: (bot_configuration_service.get_category_label(key, language), count) for key, _label, count in categories
    }
    used: set[str] = set()
    grouped: list[tuple[str, str, list[tuple[str, str, int]]]] = []

    for group_key, _title, category_keys in CATEGORY_GROUP_DEFINITIONS:
        title = str(_get_group_meta(group_key, language)['title'])
        items: list[tuple[str, str, int]] = []
        for category_key in category_keys:
            if category_key in categories_map:
                label, count = categories_map[category_key]
                items.append((category_key, label, count))
                used.add(category_key)
        if items:
            grouped.append((group_key, title, items))

    remaining = [(key, label, count) for key, (label, count) in categories_map.items() if key not in used]

    if remaining:
        remaining.sort(key=lambda item: item[1])
        grouped.append(
            (
                CATEGORY_FALLBACK_KEY,
                get_texts(language).t('BOT_SETTINGS_GROUP_OTHER_TITLE', CATEGORY_FALLBACK_TITLE),
                remaining,
            )
        )

    return grouped


def _build_groups_keyboard(language: str = 'ru') -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    grouped = _get_grouped_categories(language=language)
    rows: list[list[types.InlineKeyboardButton]] = []

    for group_key, title, items in grouped:
        sum(count for _, _, count in items)
        status_icon, status_text = _get_group_status(group_key, language=language)
        button_text = f'{status_icon} {title} — {status_text}'
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=f'botcfg_group:{group_key}:1',
                )
            ]
        )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_GROUPS_KEYBOARD_070', '🔍 Найти настройку'),
                callback_data='botcfg_action:search',
            ),
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_GROUPS_KEYBOARD_071', '🎯 Пресеты'),
                callback_data='botcfg_action:presets',
            ),
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_GROUPS_KEYBOARD_072', '📤 Экспорт .env'),
                callback_data='botcfg_action:export',
            ),
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_GROUPS_KEYBOARD_073', '📥 Импорт .env'),
                callback_data='botcfg_action:import',
            ),
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_GROUPS_KEYBOARD_074', '🕘 История'),
                callback_data='botcfg_action:history',
            ),
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_GROUPS_KEYBOARD_075', '❓ Помощь'),
                callback_data='botcfg_action:help',
            ),
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_GROUPS_KEYBOARD_076', '⬅️ Назад в админку'),
                callback_data='admin_submenu_settings',
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _build_categories_keyboard(
    group_key: str, group_title: str, categories: list[tuple[str, str, int]], page: int = 1, language: str = 'ru'
) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    total_pages = max(1, math.ceil(len(categories) / CATEGORY_PAGE_SIZE))
    page = max(1, min(page, total_pages))

    start = (page - 1) * CATEGORY_PAGE_SIZE
    end = start + CATEGORY_PAGE_SIZE
    sliced = categories[start:end]

    rows: list[list[types.InlineKeyboardButton]] = []

    buttons: list[types.InlineKeyboardButton] = []
    for category_key, label, count in sliced:
        overrides = 0
        for definition in bot_configuration_service.get_settings_for_category(category_key):
            if bot_configuration_service.has_override(definition.key):
                overrides += 1
        badge = '✳️ •' if overrides else '•'
        button_text = f'{badge} {label} ({count})'
        buttons.append(
            types.InlineKeyboardButton(
                text=button_text,
                callback_data=f'botcfg_cat:{group_key}:{category_key}:{page}:1',
            )
        )

    for chunk in _chunk(buttons, 2):
        rows.append(list(chunk))

    if total_pages > 1:
        nav_row: list[types.InlineKeyboardButton] = []
        if page > 1:
            nav_row.append(
                types.InlineKeyboardButton(
                    text='⬅️',
                    callback_data=f'botcfg_group:{group_key}:{page - 1}',
                )
            )
        nav_row.append(
            types.InlineKeyboardButton(
                text=f'[{page}/{total_pages}]',
                callback_data='botcfg_group:noop',
            )
        )
        if page < total_pages:
            nav_row.append(
                types.InlineKeyboardButton(
                    text='➡️',
                    callback_data=f'botcfg_group:{group_key}:{page + 1}',
                )
            )
        rows.append(nav_row)

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_CATEGORIES_KEYBOARD_077', '⬅️ К разделам'),
                callback_data='admin_bot_config',
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _build_settings_keyboard(
    category_key: str,
    group_key: str,
    category_page: int,
    language: str,
    page: int = 1,
) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    definitions = bot_configuration_service.get_settings_for_category(category_key)
    total_pages = max(1, math.ceil(len(definitions) / SETTINGS_PAGE_SIZE))
    page = max(1, min(page, total_pages))

    start = (page - 1) * SETTINGS_PAGE_SIZE
    end = start + SETTINGS_PAGE_SIZE
    sliced = definitions[start:end]

    rows: list[list[types.InlineKeyboardButton]] = []
    texts = get_texts(language)

    if category_key == 'REMNAWAVE':
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_078', '🔌 Проверить подключение'),
                    callback_data=(f'botcfg_test_remnawave:{group_key}:{category_key}:{category_page}:{page}'),
                )
            ]
        )

    test_payment_buttons: list[list[types.InlineKeyboardButton]] = []

    def _test_button(text: str, method: str) -> types.InlineKeyboardButton:
        return types.InlineKeyboardButton(
            text=text,
            callback_data=(f'botcfg_test_payment:{method}:{group_key}:{category_key}:{category_page}:{page}'),
        )

    if category_key == 'YOOKASSA':
        label = texts.t('PAYMENT_CARD_YOOKASSA', '💳 Банковская карта (YooKassa)')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'yookassa',
                )
            ]
        )
    elif category_key == 'TRIBUTE':
        label = texts.t('PAYMENT_CARD_TRIBUTE', '💳 Банковская карта (Tribute)')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'tribute',
                )
            ]
        )
    elif category_key == 'MULENPAY':
        label = texts.t(
            'PAYMENT_CARD_MULENPAY',
            '💳 Банковская карта ({mulenpay_name})',
        ).format(mulenpay_name=settings.get_mulenpay_display_name())
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'mulenpay',
                )
            ]
        )
    elif category_key == 'WATA':
        label = texts.t('PAYMENT_CARD_WATA', '💳 Банковская карта (WATA)')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'wata',
                )
            ]
        )
    elif category_key == 'PAL24':
        label = texts.t('PAYMENT_CARD_PAL24', '💳 Банковская карта (PayPalych)')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'pal24',
                )
            ]
        )
    elif category_key == 'TELEGRAM':
        label = texts.t('PAYMENT_TELEGRAM_STARS', '⭐ Telegram Stars')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'stars',
                )
            ]
        )
    elif category_key == 'CRYPTOBOT':
        label = texts.t('PAYMENT_CRYPTOBOT', '🪙 Криптовалюта (CryptoBot)')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'cryptobot',
                )
            ]
        )
    elif category_key == 'FREEKASSA':
        label = texts.t('PAYMENT_FREEKASSA', '💳 Freekassa')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'freekassa',
                )
            ]
        )
    elif category_key == 'KASSA_AI':
        label = texts.t('PAYMENT_KASSA_AI', f'💳 {settings.get_kassa_ai_display_name()}')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'kassa_ai',
                )
            ]
        )
    elif category_key == 'RIOPAY':
        label = texts.t('PAYMENT_RIOPAY', f'💳 {settings.get_riopay_display_name()}')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'riopay',
                )
            ]
        )
    elif category_key == 'SEVERPAY':
        label = texts.t('PAYMENT_SEVERPAY', f'💳 {settings.get_severpay_display_name()}')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'severpay',
                )
            ]
        )
    elif category_key == 'ROLLYPAY':
        label = texts.t('PAYMENT_ROLLYPAY', f'💳 {settings.get_rollypay_display_name()}')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'rollypay',
                )
            ]
        )
    elif category_key == 'OVERPAY':
        label = texts.t('PAYMENT_OVERPAY', f'💳 {settings.get_overpay_display_name()}')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'overpay',
                )
            ]
        )
    elif category_key == 'AURAPAY':
        label = texts.t('PAYMENT_AURAPAY', f'💳 {settings.get_aurapay_display_name()}')
        test_payment_buttons.append(
            [
                _test_button(
                    texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_079', '{value1} · тест').format(value1=label),
                    'aurapay',
                )
            ]
        )

    if test_payment_buttons:
        rows.extend(test_payment_buttons)

    for definition in sliced:
        current_value = bot_configuration_service.get_current_value(definition.key)
        value_preview = bot_configuration_service.format_value_for_list(definition.key)
        icon = _get_setting_icon(definition, current_value)
        override_badge = '✳️' if bot_configuration_service.has_override(definition.key) else '•'
        button_text = f'{override_badge} {icon} {definition.display_name}'
        if value_preview != '—':
            button_text += f' · {value_preview}'
        if len(button_text) > 64:
            button_text = button_text[:63] + '…'
        callback_token = bot_configuration_service.get_callback_token(definition.key)
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=(f'botcfg_setting:{group_key}:{category_page}:{page}:{callback_token}'),
                )
            ]
        )

    if total_pages > 1:
        nav_row: list[types.InlineKeyboardButton] = []
        if page > 1:
            nav_row.append(
                types.InlineKeyboardButton(
                    text='⬅️',
                    callback_data=(f'botcfg_cat:{group_key}:{category_key}:{category_page}:{page - 1}'),
                )
            )
        nav_row.append(types.InlineKeyboardButton(text=f'[{page}/{total_pages}]', callback_data='botcfg_cat_page:noop'))
        if page < total_pages:
            nav_row.append(
                types.InlineKeyboardButton(
                    text='➡️',
                    callback_data=(f'botcfg_cat:{group_key}:{category_key}:{category_page}:{page + 1}'),
                )
            )
        rows.append(nav_row)

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_SETTINGS_KEYBOARD_093', '⬅️ К категориям'),
                callback_data=f'botcfg_group:{group_key}:{category_page}',
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _build_setting_keyboard(
    key: str, group_key: str, category_page: int, settings_page: int, language: str = 'ru'
) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    definition = bot_configuration_service.get_definition(key)
    rows: list[list[types.InlineKeyboardButton]] = []
    callback_token = bot_configuration_service.get_callback_token(key)
    is_read_only = bot_configuration_service.is_read_only(key)

    choice_options = bot_configuration_service.get_choice_options(key, language=language)
    if choice_options and not is_read_only:
        current_value = bot_configuration_service.get_current_value(key)
        choice_buttons: list[types.InlineKeyboardButton] = []
        for option in choice_options:
            choice_token = bot_configuration_service.get_choice_token(key, option.value)
            if choice_token is None:
                continue
            button_text = option.label
            # Сравнение через as_choice_key: текущее значение приведено к типу
            # настройки, а вариант описан строкой — у булевой галочка иначе не
            # ставилась бы никогда.
            same = bot_configuration_service.as_choice_key(current_value) == bot_configuration_service.as_choice_key(
                option.value
            )
            if same and not button_text.startswith('✅'):
                button_text = f'✅ {button_text}'
            choice_buttons.append(
                types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=(
                        f'botcfg_choice:{group_key}:{category_page}:{settings_page}:{callback_token}:{choice_token}'
                    ),
                )
            )

        for chunk in _chunk(choice_buttons, 2):
            rows.append(list(chunk))

    if key == 'SIMPLE_SUBSCRIPTION_SQUAD_UUID' and not is_read_only:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_BUILD_SETTING_KEYBOARD_094', '🌍 Выбрать сквад'),
                    callback_data=(
                        f'botcfg_simple_squad:{group_key}:{category_page}:{settings_page}:{callback_token}:1'
                    ),
                )
            ]
        )

    if definition.python_type is bool and not is_read_only:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_BUILD_SETTING_KEYBOARD_095', '🔁 Переключить'),
                    callback_data=(f'botcfg_toggle:{group_key}:{category_page}:{settings_page}:{callback_token}'),
                )
            ]
        )

    if not is_read_only:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_BUILD_SETTING_KEYBOARD_096', '✏️ Изменить'),
                    callback_data=(f'botcfg_edit:{group_key}:{category_page}:{settings_page}:{callback_token}'),
                )
            ]
        )

    if bot_configuration_service.has_override(key) and not is_read_only:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_BUILD_SETTING_KEYBOARD_097', '♻️ Сбросить'),
                    callback_data=(f'botcfg_reset:{group_key}:{category_page}:{settings_page}:{callback_token}'),
                )
            ]
        )

    if is_read_only:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_BUILD_SETTING_KEYBOARD_098', '🔒 Только для чтения'),
                    callback_data='botcfg_group:noop',
                )
            ]
        )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_SETTING_KEYBOARD_099', '⬅️ Назад'),
                callback_data=(f'botcfg_cat:{group_key}:{definition.category_key}:{category_page}:{settings_page}'),
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _render_setting_text(key: str, language: str = 'ru') -> str:
    texts = get_texts(language)
    summary = bot_configuration_service.get_setting_summary(key, language=language)
    guidance = bot_configuration_service.get_setting_guidance(key, language=language)

    definition = bot_configuration_service.get_definition(key)

    description = guidance.get('description') or '—'
    format_hint = guidance.get('format') or '—'
    example = guidance.get('example') or '—'
    warning = guidance.get('warning') or '—'
    dependencies = guidance.get('dependencies') or '—'
    type_label = guidance.get('type') or summary.get('type') or definition.type_label

    lines = [
        f'🧩 <b>{summary["name"]}</b>',
        texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_100', '🔑 Ключ: <code>{value1}</code>').format(
            value1=summary['key']
        ),
        texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_101', '📁 Категория: {value1}').format(
            value1=summary['category_label']
        ),
        texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_102', '📝 Тип: {value1}').format(value1=type_label),
        texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_103', '📌 Текущее: {value1}').format(value1=summary['current']),
    ]

    original_value = summary.get('original')
    if original_value not in {None, ''}:
        lines.append(
            texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_104', '📦 По умолчанию: {value1}').format(
                value1=original_value
            )
        )

    lines.append(
        texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_105', '✳️ Переопределено: {value1}').format(
            value1=texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_106', 'Да')
            if summary['has_override']
            else texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_107', 'Нет')
        )
    )

    if summary.get('is_read_only'):
        lines.append(
            texts.t(
                'BOT_SETTINGS_UI_RENDER_SETTING_TEXT_108', '🔒 Режим: Только для чтения (управляется автоматически)'
            )
        )

    lines.append('')
    if description:
        lines.append(
            texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_109', '📘 Описание: {value1}').format(value1=description)
        )
    if format_hint:
        lines.append(
            texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_110', '📐 Формат: {value1}').format(value1=format_hint)
        )
    if example:
        lines.append(texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_111', '💡 Пример: {value1}').format(value1=example))
    if warning:
        lines.append(texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_112', '⚠️ Важно: {value1}').format(value1=warning))
    if dependencies:
        lines.append(
            texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_113', '🔗 Связанные: {value1}').format(value1=dependencies)
        )

    choices = bot_configuration_service.get_choice_options(key, language=language)
    if choices:
        current_raw = bot_configuration_service.get_current_value(key)
        lines.append('')
        lines.append(texts.t('BOT_SETTINGS_UI_RENDER_SETTING_TEXT_114', '📋 Доступные значения:'))
        for option in choices:
            marker = '✅' if current_raw == option.value else '•'
            value_display = bot_configuration_service.format_value_human(key, option.value, language=language)
            description = option.description or ''
            base_line = f'{marker} {option.label} — <code>{value_display}</code>'
            if description:
                base_line += f'\n└ {description}'
            lines.append(base_line)

    return '\n'.join(lines)


@admin_required
@error_handler
async def show_bot_config_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await state.clear()
    keyboard = _build_groups_keyboard(language=db_user.language)
    overview = _render_dashboard_overview(language=db_user.language)
    await callback.message.edit_text(
        overview,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_bot_config_group(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    group_key, page = _parse_group_payload(callback.data)
    grouped = _get_grouped_categories(language=db_user.language)
    group_lookup = {key: (title, items) for key, title, items in grouped}

    if group_key not in group_lookup:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_BOT_CONFIG_GROUP_115', 'Эта группа больше недоступна'), show_alert=True
        )
        return

    group_title, items = group_lookup[group_key]
    keyboard = _build_categories_keyboard(group_key, group_title, items, page, language=db_user.language)
    status_icon, status_text = _get_group_status(group_key, language=db_user.language)
    description = _get_group_description(group_key, language=db_user.language)
    icon = _get_group_icon(group_key, language=db_user.language)
    raw_title = str(group_title).strip()
    clean_title = raw_title
    if icon and raw_title.startswith(icon):
        clean_title = raw_title[len(icon) :].strip()
    elif ' ' in raw_title:
        possible_icon, remainder = raw_title.split(' ', 1)
        if possible_icon:
            icon = possible_icon
            clean_title = remainder.strip()
    lines = [f'{icon} <b>{clean_title}</b>']
    if status_text:
        lines.append(
            texts.t('BOT_SETTINGS_UI_SHOW_BOT_CONFIG_GROUP_116', 'Статус: {value1} {value2}').format(
                value1=status_icon, value2=status_text
            )
        )
    lines.append(f'🏠 → {clean_title}')
    if description:
        lines.append('')
        lines.append(description)
    lines.append('')
    lines.append(texts.t('BOT_SETTINGS_UI_SHOW_BOT_CONFIG_GROUP_117', '📂 Категории группы:'))
    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_bot_config_category(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    group_key, category_key, category_page, settings_page = _parse_category_payload(callback.data)
    definitions = bot_configuration_service.get_settings_for_category(category_key)

    if not definitions:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_BOT_CONFIG_CATEGORY_118', 'В этой категории пока нет настроек'),
            show_alert=True,
        )
        return

    category_label = bot_configuration_service.get_category_label(category_key, db_user.language)
    category_description = bot_configuration_service.get_category_description(category_key, language=db_user.language)
    group_meta = _get_group_meta(group_key, language=db_user.language)
    group_title = str(group_meta.get('title', group_key))
    group_icon = _get_group_icon(group_key, language=db_user.language)
    raw_group_title = group_title.strip()
    if group_icon and raw_group_title.startswith(group_icon):
        group_plain_title = raw_group_title[len(group_icon) :].strip()
    elif ' ' in raw_group_title:
        possible_icon, remainder = raw_group_title.split(' ', 1)
        group_plain_title = remainder.strip()
        if possible_icon:
            group_icon = possible_icon
    else:
        group_plain_title = raw_group_title
    keyboard = _build_settings_keyboard(
        category_key,
        group_key,
        category_page,
        db_user.language,
        settings_page,
    )
    text_lines = [
        f'🗂 <b>{category_label}</b>',
        f'🏠 → {group_plain_title} → {category_label}',
    ]
    if category_description:
        text_lines.append(category_description)
    text_lines.append('')
    text_lines.append(texts.t('BOT_SETTINGS_UI_SHOW_BOT_CONFIG_CATEGORY_119', '📋 Список настроек категории:'))
    await callback.message.edit_text(
        '\n'.join(text_lines),
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_simple_subscription_squad_selector(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 5)
    group_key = parts[1] if len(parts) > 1 else CATEGORY_FALLBACK_KEY
    try:
        category_page = max(1, int(parts[2])) if len(parts) > 2 else 1
    except ValueError:
        category_page = 1
    try:
        settings_page = max(1, int(parts[3])) if len(parts) > 3 else 1
    except ValueError:
        settings_page = 1
    token = parts[4] if len(parts) > 4 else ''

    try:
        key = bot_configuration_service.resolve_callback_token(token)
    except KeyError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_121', 'Эта настройка больше недоступна'),
            show_alert=True,
        )
        return

    if key != 'SIMPLE_SUBSCRIPTION_SQUAD_UUID':
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_121', 'Эта настройка больше недоступна'),
            show_alert=True,
        )
        return

    try:
        page = max(1, int(parts[5])) if len(parts) > 5 else 1
    except ValueError:
        page = 1

    limit = SIMPLE_SUBSCRIPTION_SQUADS_PAGE_SIZE
    squads, total_count = await get_all_server_squads(
        db,
        available_only=False,
        page=page,
        limit=limit,
    )

    total_count = total_count or 0
    total_pages = max(1, math.ceil(total_count / limit)) if total_count else 1
    if total_count and page > total_pages:
        page = total_pages
        squads, total_count = await get_all_server_squads(
            db,
            available_only=False,
            page=page,
            limit=limit,
        )

    current_uuid = bot_configuration_service.get_current_value(key) or ''
    current_display = texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_122', 'Любой доступный')

    if current_uuid:
        selected_server = next((srv for srv in squads if srv.squad_uuid == current_uuid), None)
        if not selected_server:
            selected_server = await get_server_squad_by_uuid(db, current_uuid)
        if selected_server:
            current_display = selected_server.display_name
        else:
            current_display = current_uuid

    lines = [
        texts.t(
            'BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_123',
            '🌍 <b>Выберите сквад для простой покупки</b>',
        ),
        '',
        texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_124', 'Текущий выбор: {value1}').format(
            value1=html.escape(current_display)
        )
        if current_display
        else texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_125', 'Текущий выбор: —'),
        '',
    ]

    if total_count == 0:
        lines.append(
            texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_126', '❌ Доступные сервера не найдены.')
        )
    else:
        lines.append(
            texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_127', 'Выберите сервер из списка ниже.')
        )
        if total_pages > 1:
            lines.append(
                texts.t(
                    'BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_128', 'Страница {value1}/{value2}'
                ).format(value1=page, value2=total_pages)
            )

    text = '\n'.join(lines)

    keyboard_rows: list[list[types.InlineKeyboardButton]] = []

    for server in squads:
        status_icon = '✅' if server.squad_uuid == current_uuid else ('🟢' if server.is_available else '🔒')
        label_parts = [status_icon, server.display_name]
        if server.country_code:
            label_parts.append(f'({server.country_code.upper()})')
        if isinstance(server.price_kopeks, int) and server.price_kopeks > 0:
            try:
                label_parts.append(f'— {settings.format_price(server.price_kopeks)}')
            except Exception:
                pass
        label = ' '.join(label_parts)

        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=label,
                    callback_data=(
                        f'botcfg_simple_squad_select:{group_key}:{category_page}:{settings_page}:{token}:{server.id}:{page}'
                    ),
                )
            ]
        )

    if total_pages > 1:
        nav_row: list[types.InlineKeyboardButton] = []
        if page > 1:
            nav_row.append(
                types.InlineKeyboardButton(
                    text='⬅️',
                    callback_data=(
                        f'botcfg_simple_squad:{group_key}:{category_page}:{settings_page}:{token}:{page - 1}'
                    ),
                )
            )
        if page < total_pages:
            nav_row.append(
                types.InlineKeyboardButton(
                    text='➡️',
                    callback_data=(
                        f'botcfg_simple_squad:{group_key}:{category_page}:{settings_page}:{token}:{page + 1}'
                    ),
                )
            )
        if nav_row:
            keyboard_rows.append(nav_row)

    keyboard_rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_SETTING_KEYBOARD_099', '⬅️ Назад'),
                callback_data=(f'botcfg_setting:{group_key}:{category_page}:{settings_page}:{token}'),
            )
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def select_simple_subscription_squad(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 6)
    group_key = parts[1] if len(parts) > 1 else CATEGORY_FALLBACK_KEY
    try:
        category_page = max(1, int(parts[2])) if len(parts) > 2 else 1
    except ValueError:
        category_page = 1
    try:
        settings_page = max(1, int(parts[3])) if len(parts) > 3 else 1
    except ValueError:
        settings_page = 1
    token = parts[4] if len(parts) > 4 else ''
    try:
        server_id = int(parts[5]) if len(parts) > 5 else None
    except ValueError:
        server_id = None

    if server_id is None:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_130', 'Не удалось определить сервер'),
            show_alert=True,
        )
        return

    try:
        key = bot_configuration_service.resolve_callback_token(token)
    except KeyError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_121', 'Эта настройка больше недоступна'),
            show_alert=True,
        )
        return

    if bot_configuration_service.is_read_only(key):
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_132', 'Эта настройка доступна только для чтения'),
            show_alert=True,
        )
        return

    server = await get_server_squad_by_id(db, server_id)
    if not server:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_133', 'Сервер не найден'), show_alert=True
        )
        return

    try:
        await bot_configuration_service.set_value(db, key, server.squad_uuid)
    except ReadOnlySettingError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_132', 'Эта настройка доступна только для чтения'),
            show_alert=True,
        )
        return
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        return

    await db.commit()

    text = _render_setting_text(key, language=db_user.language)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page, language=db_user.language)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )
    await callback.answer(texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_135', 'Сквад выбран'))


@admin_required
@error_handler
async def test_remnawave_connection(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 5)
    group_key = parts[1] if len(parts) > 1 else CATEGORY_FALLBACK_KEY
    category_key = parts[2] if len(parts) > 2 else 'REMNAWAVE'

    try:
        category_page = max(1, int(parts[3])) if len(parts) > 3 else 1
    except ValueError:
        category_page = 1

    try:
        settings_page = max(1, int(parts[4])) if len(parts) > 4 else 1
    except ValueError:
        settings_page = 1

    service = RemnaWaveService()
    result = await service.test_api_connection()

    status = result.get('status')
    message: str

    if status == 'connected':
        message = texts.t('BOT_SETTINGS_UI_TEST_REMNAWAVE_CONNECTION_136', '✅ Подключение успешно')
    elif status == 'not_configured':
        message = texts.t('BOT_SETTINGS_UI_TEST_REMNAWAVE_CONNECTION_137', '⚠️ {value1}').format(
            value1=result.get(
                'message', texts.t('BOT_SETTINGS_UI_TEST_REMNAWAVE_CONNECTION_138', 'RemnaWave API не настроен')
            )
        )
    else:
        base_message = result.get(
            'message', texts.t('BOT_SETTINGS_UI_TEST_REMNAWAVE_CONNECTION_139', 'Ошибка подключения')
        )
        status_code = result.get('status_code')
        if status_code:
            message = f'❌ {base_message} (HTTP {status_code})'
        else:
            message = f'❌ {base_message}'

    definitions = bot_configuration_service.get_settings_for_category(category_key)
    if definitions:
        keyboard = _build_settings_keyboard(
            category_key,
            group_key,
            category_page,
            db_user.language,
            settings_page,
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except Exception:
            # ignore inability to refresh markup, main result shown in alert
            pass

    await callback.answer(message, show_alert=True)


@admin_required
@error_handler
async def test_payment_provider(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 6)
    method = parts[1] if len(parts) > 1 else ''
    group_key = parts[2] if len(parts) > 2 else CATEGORY_FALLBACK_KEY
    category_key = parts[3] if len(parts) > 3 else 'PAYMENT'

    try:
        category_page = max(1, int(parts[4])) if len(parts) > 4 else 1
    except ValueError:
        category_page = 1

    try:
        settings_page = max(1, int(parts[5])) if len(parts) > 5 else 1
    except ValueError:
        settings_page = 1

    language = db_user.language
    texts = get_texts(language)
    payment_service = PaymentService(callback.bot)

    message_text: str

    async def _refresh_markup() -> None:
        definitions = bot_configuration_service.get_settings_for_category(category_key)
        if definitions:
            keyboard = _build_settings_keyboard(
                category_key,
                group_key,
                category_page,
                language,
                settings_page,
            )
            try:
                await callback.message.edit_reply_markup(reply_markup=keyboard)
            except Exception:
                pass

    if method == 'yookassa':
        if not settings.is_yookassa_enabled():
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_140', '❌ YooKassa отключена'), show_alert=True
            )
            return

        amount_kopeks = 10 * 100
        description = settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id)
        payment_result = await payment_service.create_yookassa_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=f'Тестовый платеж (админ): {description}',
            metadata={
                'user_telegram_id': str(db_user.telegram_id),
                'purpose': 'admin_test_payment',
                'provider': 'yookassa',
            },
        )

        if not payment_result or not payment_result.get('confirmation_url'):
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_141', '❌ Не удалось создать тестовый платеж YooKassa'),
                show_alert=True,
            )
            await _refresh_markup()
            return

        confirmation_url = payment_result['confirmation_url']
        message_text = texts.t(
            'BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_142',
            '🧪 <b>Тестовый платеж YooKassa</b>\n\n💰 Сумма: {value1}\n🆔 ID: {value2}',
        ).format(value1=texts.format_price(amount_kopeks), value2=payment_result['yookassa_payment_id'])
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_143', '💳 Оплатить картой'),
                        url=confirmation_url,
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_161', '📊 Проверить статус'),
                        callback_data=f'check_yookassa_{payment_result["local_payment_id"]}',
                    )
                ],
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_145', '✅ Ссылка на платеж YooKassa отправлена'),
            show_alert=True,
        )
        await _refresh_markup()
        return

    if method == 'tribute':
        if not settings.TRIBUTE_ENABLED:
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_146', '❌ Tribute отключен'), show_alert=True
            )
            return

        tribute_service = TributeService(callback.bot)
        try:
            payment_url = await tribute_service.create_payment_link(
                user_id=db_user.telegram_id,
                amount_kopeks=10 * 100,
                description='Тестовый платеж Tribute (админ)',
            )
        except Exception:
            payment_url = None

        if not payment_url:
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_147', '❌ Не удалось создать платеж Tribute'),
                show_alert=True,
            )
            await _refresh_markup()
            return

        message_text = texts.t(
            'BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_148',
            '🧪 <b>Тестовый платеж Tribute</b>\n\n💰 Сумма: {value1}\n🔗 Нажмите кнопку ниже, чтобы открыть ссылку на оплату.',
        ).format(value1=texts.format_price(10 * 100))
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_149', '💳 Перейти к оплате'),
                        url=payment_url,
                    )
                ]
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_150', '✅ Ссылка на платеж Tribute отправлена'),
            show_alert=True,
        )
        await _refresh_markup()
        return

    if method == 'mulenpay':
        mulenpay_name = settings.get_mulenpay_display_name()
        mulenpay_name_html = settings.get_mulenpay_display_name_html()
        if not settings.is_mulenpay_enabled():
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_151', '❌ {value1} отключен').format(
                    value1=mulenpay_name
                ),
                show_alert=True,
            )
            return

        amount_kopeks = 1 * 100
        payment_result = await payment_service.create_mulenpay_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=f'Тестовый платеж {mulenpay_name} (админ)',
            language=language,
        )

        if not payment_result or not payment_result.get('payment_url'):
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_152', '❌ Не удалось создать платеж {value1}').format(
                    value1=mulenpay_name
                ),
                show_alert=True,
            )
            await _refresh_markup()
            return

        payment_url = payment_result['payment_url']
        message_text = texts.t(
            'BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_153',
            '🧪 <b>Тестовый платеж {value1}</b>\n\n💰 Сумма: {value2}\n🆔 ID: {value3}',
        ).format(
            value1=mulenpay_name_html,
            value2=texts.format_price(amount_kopeks),
            value3=payment_result['mulen_payment_id'],
        )
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_149', '💳 Перейти к оплате'),
                        url=payment_url,
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_161', '📊 Проверить статус'),
                        callback_data=f'check_mulenpay_{payment_result["local_payment_id"]}',
                    )
                ],
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_156', '✅ Ссылка на платеж {value1} отправлена').format(
                value1=mulenpay_name
            ),
            show_alert=True,
        )
        await _refresh_markup()
        return

    if method == 'pal24':
        if not settings.is_pal24_enabled():
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_157', '❌ PayPalych отключен'), show_alert=True
            )
            return

        amount_kopeks = 10 * 100
        payment_result = await payment_service.create_pal24_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж PayPalych (админ)',
            language=language or 'ru',
        )

        if not payment_result:
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_158', '❌ Не удалось создать платеж PayPalych'),
                show_alert=True,
            )
            await _refresh_markup()
            return

        sbp_url = payment_result.get('sbp_url') or payment_result.get('transfer_url') or payment_result.get('link_url')
        card_url = payment_result.get('card_url')
        fallback_url = payment_result.get('link_page_url') or payment_result.get('link_url')

        if not (sbp_url or card_url or fallback_url):
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_158', '❌ Не удалось создать платеж PayPalych'),
                show_alert=True,
            )
            await _refresh_markup()
            return

        if not sbp_url:
            sbp_url = fallback_url

        default_sbp_text = texts.t(
            'PAL24_SBP_PAY_BUTTON',
            '🏦 Оплатить через PayPalych (СБП)',
        )
        sbp_button_text = settings.get_pal24_sbp_button_text(default_sbp_text)

        default_card_text = texts.t(
            'PAL24_CARD_PAY_BUTTON',
            '💳 Оплатить банковской картой (PayPalych)',
        )
        card_button_text = settings.get_pal24_card_button_text(default_card_text)

        pay_rows: list[list[types.InlineKeyboardButton]] = []
        if sbp_url:
            pay_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=sbp_button_text,
                        url=sbp_url,
                    )
                ]
            )

        if card_url and card_url != sbp_url:
            pay_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=card_button_text,
                        url=card_url,
                    )
                ]
            )

        if not pay_rows and fallback_url:
            pay_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=sbp_button_text,
                        url=fallback_url,
                    )
                ]
            )

        message_text = texts.t(
            'BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_160',
            '🧪 <b>Тестовый платеж PayPalych</b>\n\n💰 Сумма: {value1}\n🆔 Bill ID: {value2}',
        ).format(value1=texts.format_price(amount_kopeks), value2=payment_result['bill_id'])
        keyboard_rows = pay_rows + [
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_161', '📊 Проверить статус'),
                    callback_data=f'check_pal24_{payment_result["local_payment_id"]}',
                )
            ],
        ]

        reply_markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_162', '✅ Ссылка на платеж PayPalych отправлена'),
            show_alert=True,
        )
        await _refresh_markup()
        return

    if method == 'stars':
        if not settings.TELEGRAM_STARS_ENABLED:
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_163', '❌ Telegram Stars отключены'), show_alert=True
            )
            return

        stars_rate = settings.get_stars_rate()
        amount_kopeks = max(1, int(round(stars_rate * 100)))
        payload = f'admin_stars_test_{db_user.id}_{int(time.time())}'
        try:
            invoice_link = await payment_service.create_stars_invoice(
                language=getattr(db_user, 'language', None) or settings.DEFAULT_LANGUAGE,
                amount_kopeks=amount_kopeks,
                description=texts.t('STARS_ADMIN_TEST_DESCRIPTION', 'Тестовый платеж Telegram Stars (админ)'),
                payload=payload,
            )
        except Exception:
            invoice_link = None

        if not invoice_link:
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_164', '❌ Не удалось создать платеж Telegram Stars'),
                show_alert=True,
            )
            await _refresh_markup()
            return

        stars_amount = TelegramStarsService.calculate_stars_from_rubles(amount_kopeks / 100)
        message_text = texts.t(
            'BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_165',
            '🧪 <b>Тестовый платеж Telegram Stars</b>\n\n💰 Сумма: {value1}\n⭐ К оплате: {value2}',
        ).format(value1=texts.format_price(amount_kopeks), value2=stars_amount)
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('PAYMENT_TELEGRAM_STARS', '⭐ Открыть счет'),
                        url=invoice_link,
                    )
                ]
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_166', '✅ Ссылка на платеж Stars отправлена'),
            show_alert=True,
        )
        await _refresh_markup()
        return

    if method == 'cryptobot':
        if not settings.is_cryptobot_enabled():
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_167', '❌ CryptoBot отключен'), show_alert=True
            )
            return

        amount_rubles = 100.0
        try:
            current_rate = await currency_converter.get_usd_to_rub_rate()
        except Exception:
            current_rate = None

        if not current_rate or current_rate <= 0:
            current_rate = 100.0

        amount_usd = round(amount_rubles / current_rate, 2)
        if amount_usd < 1:
            amount_usd = 1.0

        payment_result = await payment_service.create_cryptobot_payment(
            db=db,
            user_id=db_user.id,
            amount_usd=amount_usd,
            asset=settings.CRYPTOBOT_DEFAULT_ASSET,
            description=f'Тестовый платеж CryptoBot {amount_rubles:.0f} ₽ ({amount_usd:.2f} USD)',
            payload=f'admin_cryptobot_test_{db_user.id}_{int(time.time())}',
        )

        if not payment_result:
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_168', '❌ Не удалось создать платеж CryptoBot'),
                show_alert=True,
            )
            await _refresh_markup()
            return

        payment_url = (
            payment_result.get('bot_invoice_url')
            or payment_result.get('mini_app_invoice_url')
            or payment_result.get('web_app_invoice_url')
        )

        if not payment_url:
            await callback.answer(
                texts.t(
                    'BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_169', '❌ Не удалось получить ссылку на оплату CryptoBot'
                ),
                show_alert=True,
            )
            await _refresh_markup()
            return

        amount_kopeks = int(amount_rubles * 100)
        message_text = texts.t(
            'BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_170',
            '🧪 <b>Тестовый платеж CryptoBot</b>\n\n💰 Сумма к зачислению: {value1}\n💵 К оплате: {value2} USD\n🪙 Актив: {value3}',
        ).format(
            value1=texts.format_price(amount_kopeks), value2=format(amount_usd, '.2f'), value3=payment_result['asset']
        )
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_171', '🪙 Открыть счет'), url=payment_url
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_161', '📊 Проверить статус'),
                        callback_data=f'check_cryptobot_{payment_result["local_payment_id"]}',
                    )
                ],
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_173', '✅ Ссылка на платеж CryptoBot отправлена'),
            show_alert=True,
        )
        await _refresh_markup()
        return

    if method == 'freekassa':
        if not settings.is_freekassa_enabled():
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_174', '❌ Freekassa отключена'), show_alert=True
            )
            return

        amount_kopeks = settings.FREEKASSA_MIN_AMOUNT_KOPEKS
        payment_result = await payment_service.create_freekassa_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж Freekassa (админ)',
            email=getattr(db_user, 'email', None),
            language=db_user.language or settings.DEFAULT_LANGUAGE,
        )

        if not payment_result or not payment_result.get('payment_url'):
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_175', '❌ Не удалось создать тестовый платеж Freekassa'),
                show_alert=True,
            )
            await _refresh_markup()
            return

        payment_url = payment_result['payment_url']
        message_text = texts.t(
            'BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_176',
            '🧪 <b>Тестовый платеж Freekassa</b>\n\n💰 Сумма: {value1}\n🆔 Order ID: {value2}',
        ).format(value1=texts.format_price(amount_kopeks), value2=payment_result['order_id'])
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_149', '💳 Перейти к оплате'),
                        url=payment_url,
                    )
                ]
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_178', '✅ Ссылка на платеж Freekassa отправлена'),
            show_alert=True,
        )
        await _refresh_markup()
        return

    if method == 'kassa_ai':
        if not settings.is_kassa_ai_enabled():
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_179', '❌ Kassa AI отключена'), show_alert=True
            )
            return

        amount_kopeks = settings.KASSA_AI_MIN_AMOUNT_KOPEKS
        payment_result = await payment_service.create_kassa_ai_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж Kassa AI (админ)',
            email=getattr(db_user, 'email', None),
            language=db_user.language or settings.DEFAULT_LANGUAGE,
        )

        if not payment_result or not payment_result.get('payment_url'):
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_180', '❌ Не удалось создать тестовый платеж Kassa AI'),
                show_alert=True,
            )
            await _refresh_markup()
            return

        payment_url = payment_result['payment_url']
        display_name = settings.get_kassa_ai_display_name()
        message_text = texts.t(
            'BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_181',
            '🧪 <b>Тестовый платеж {value1}</b>\n\n💰 Сумма: {value2}\n🆔 Order ID: {value3}',
        ).format(value1=display_name, value2=texts.format_price(amount_kopeks), value3=payment_result['order_id'])
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_149', '💳 Перейти к оплате'),
                        url=payment_url,
                    )
                ]
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_156', '✅ Ссылка на платеж {value1} отправлена').format(
                value1=display_name
            ),
            show_alert=True,
        )
        await _refresh_markup()
        return

    if method == 'riopay':
        if not settings.is_riopay_enabled():
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_184', '❌ RioPay отключена'), show_alert=True
            )
            return

        amount_kopeks = settings.RIOPAY_MIN_AMOUNT_KOPEKS
        payment_result = await payment_service.create_riopay_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж RioPay (админ)',
            email=getattr(db_user, 'email', None),
            language=db_user.language or settings.DEFAULT_LANGUAGE,
        )

        if not payment_result or not payment_result.get('payment_url'):
            await callback.answer(
                texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_185', '❌ Не удалось создать тестовый платеж RioPay'),
                show_alert=True,
            )
            await _refresh_markup()
            return

        payment_url = payment_result['payment_url']
        display_name = settings.get_riopay_display_name()
        message_text = texts.t(
            'BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_181',
            '🧪 <b>Тестовый платеж {value1}</b>\n\n💰 Сумма: {value2}\n🆔 Order ID: {value3}',
        ).format(value1=display_name, value2=texts.format_price(amount_kopeks), value3=payment_result['order_id'])
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_149', '💳 Перейти к оплате'),
                        url=payment_url,
                    )
                ]
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_156', '✅ Ссылка на платеж {value1} отправлена').format(
                value1=display_name
            ),
            show_alert=True,
        )
        await _refresh_markup()
        return

    await callback.answer(
        texts.t('BOT_SETTINGS_UI_TEST_PAYMENT_PROVIDER_189', '❌ Неизвестный способ тестирования платежа'),
        show_alert=True,
    )
    await _refresh_markup()


@admin_required
@error_handler
async def show_bot_config_setting(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 4)
    group_key = parts[1] if len(parts) > 1 else CATEGORY_FALLBACK_KEY
    try:
        category_page = max(1, int(parts[2])) if len(parts) > 2 else 1
    except ValueError:
        category_page = 1
    try:
        settings_page = max(1, int(parts[3])) if len(parts) > 3 else 1
    except ValueError:
        settings_page = 1
    token = parts[4] if len(parts) > 4 else ''
    try:
        key = bot_configuration_service.resolve_callback_token(token)
    except KeyError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_121', 'Эта настройка больше недоступна'),
            show_alert=True,
        )
        return
    text = _render_setting_text(key, language=db_user.language)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page, language=db_user.language)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )
    await callback.answer()


@admin_required
@error_handler
async def start_edit_setting(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 4)
    group_key = parts[1] if len(parts) > 1 else CATEGORY_FALLBACK_KEY
    try:
        category_page = max(1, int(parts[2])) if len(parts) > 2 else 1
    except ValueError:
        category_page = 1
    try:
        settings_page = max(1, int(parts[3])) if len(parts) > 3 else 1
    except ValueError:
        settings_page = 1
    token = parts[4] if len(parts) > 4 else ''
    try:
        key = bot_configuration_service.resolve_callback_token(token)
    except KeyError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_121', 'Эта настройка больше недоступна'),
            show_alert=True,
        )
        return
    if bot_configuration_service.is_read_only(key):
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_132', 'Эта настройка доступна только для чтения'),
            show_alert=True,
        )
        return
    definition = bot_configuration_service.get_definition(key)

    summary = bot_configuration_service.get_setting_summary(key, language=db_user.language)
    texts = get_texts(db_user.language)

    instructions = [
        texts.t('BOT_SETTINGS_UI_START_EDIT_SETTING_193', '✏️ <b>Редактирование настройки</b>'),
        texts.t('BOT_SETTINGS_UI_START_EDIT_SETTING_194', 'Название: {value1}').format(value1=summary['name']),
        texts.t('BOT_SETTINGS_UI_START_EDIT_SETTING_195', 'Ключ: <code>{value1}</code>').format(value1=summary['key']),
        texts.t('BOT_SETTINGS_UI_START_EDIT_SETTING_196', 'Тип: {value1}').format(value1=summary['type']),
        texts.t('BOT_SETTINGS_UI_START_EDIT_SETTING_197', 'Текущее значение: {value1}').format(
            value1=summary['current']
        ),
        texts.t('BOT_SETTINGS_UI_START_EDIT_SETTING_198', '\nОтправьте новое значение сообщением.'),
    ]

    if definition.is_optional:
        instructions.append(
            texts.t(
                'BOT_SETTINGS_UI_START_EDIT_SETTING_199',
                "Отправьте 'none' или оставьте пустым для сброса на значение по умолчанию.",
            )
        )

    instructions.append(texts.t('BOT_SETTINGS_UI_START_EDIT_SETTING_200', "Для отмены отправьте 'cancel'."))

    await callback.message.edit_text(
        '\n'.join(instructions),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.BACK,
                        callback_data=(f'botcfg_setting:{group_key}:{category_page}:{settings_page}:{token}'),
                    )
                ]
            ]
        ),
    )

    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )
    await state.set_state(BotConfigStates.waiting_for_value)
    await callback.answer()


def _build_save_confirmation(key: str, language: str = 'ru') -> str:
    """Сообщение после сохранения настройки.

    set_value всегда пишет значение в БД, но для ключей, заданных через
    окружение, рантайм продолжает использовать значение из .env — «✅ обновлена»
    в этом случае вводит в заблуждение: админ видит подтверждение, а поведение
    бота не меняется (#2749, вся секция рефералки из .env.example). Говорим
    честно, какая переменная блокирует применение и что с ней сделать.
    """
    texts = get_texts(language)
    if bot_configuration_service.is_env_overridden(key):
        return texts.t(
            'BOT_SETTINGS_UI_BUILD_SAVE_CONFIRMATION_201',
            '💾 Сохранено в БД, но <b>не применено</b>: значение задаётся переменной окружения <code>{value1}</code> из .env.\nУберите её из .env и перезапустите бота, чтобы управлять этой настройкой отсюда.',
        ).format(value1=html.escape(key))
    return texts.t('BOT_SETTINGS_UI_BUILD_SAVE_CONFIRMATION_202', '✅ Настройка обновлена')


@admin_required
@error_handler
async def handle_edit_setting(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    key = data.get('setting_key')
    group_key = data.get('setting_group_key', CATEGORY_FALLBACK_KEY)
    category_page = data.get('setting_category_page', 1)
    settings_page = data.get('setting_settings_page', 1)

    if not key:
        await message.answer(
            texts.t(
                'BOT_SETTINGS_UI_HANDLE_EDIT_SETTING_203',
                'Не удалось определить редактируемую настройку. Попробуйте снова.',
            )
        )
        await state.clear()
        return

    if bot_configuration_service.is_read_only(key):
        await message.answer(
            texts.t('BOT_SETTINGS_UI_HANDLE_EDIT_SETTING_204', '⚠️ Эта настройка доступна только для чтения.')
        )
        await state.clear()
        return

    try:
        value = bot_configuration_service.parse_user_value(key, message.text or '', language=db_user.language)
    except ValueError as error:
        await message.answer(f'⚠️ {error}')
        return

    try:
        await bot_configuration_service.set_value(db, key, value)
    except ReadOnlySettingError:
        await message.answer(
            texts.t('BOT_SETTINGS_UI_HANDLE_EDIT_SETTING_204', '⚠️ Эта настройка доступна только для чтения.')
        )
        await state.clear()
        return
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    await db.commit()

    text = _render_setting_text(key, language=db_user.language)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page, language=db_user.language)
    await message.answer(_build_save_confirmation(key, language=db_user.language))
    await message.answer(text, reply_markup=keyboard)
    await state.clear()
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )


@admin_required
@error_handler
async def handle_direct_setting_input(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()

    key = data.get('setting_key')
    group_key = data.get('setting_group_key', CATEGORY_FALLBACK_KEY)
    category_page = int(data.get('setting_category_page', 1) or 1)
    settings_page = int(data.get('setting_settings_page', 1) or 1)

    if not key:
        return

    if bot_configuration_service.is_read_only(key):
        await message.answer(
            texts.t('BOT_SETTINGS_UI_HANDLE_EDIT_SETTING_204', '⚠️ Эта настройка доступна только для чтения.')
        )
        await state.clear()
        return

    try:
        value = bot_configuration_service.parse_user_value(key, message.text or '', language=db_user.language)
    except ValueError as error:
        await message.answer(f'⚠️ {error}')
        return

    try:
        await bot_configuration_service.set_value(db, key, value)
    except ReadOnlySettingError:
        await message.answer(
            texts.t('BOT_SETTINGS_UI_HANDLE_EDIT_SETTING_204', '⚠️ Эта настройка доступна только для чтения.')
        )
        await state.clear()
        return
    except ValueError as error:
        await message.answer(html.escape(str(error)))
        return
    await db.commit()

    text = _render_setting_text(key, language=db_user.language)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page, language=db_user.language)
    await message.answer(_build_save_confirmation(key, language=db_user.language))
    await message.answer(text, reply_markup=keyboard)

    await state.clear()
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )


@admin_required
@error_handler
async def reset_setting(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 4)
    group_key = parts[1] if len(parts) > 1 else CATEGORY_FALLBACK_KEY
    try:
        category_page = max(1, int(parts[2])) if len(parts) > 2 else 1
    except ValueError:
        category_page = 1
    try:
        settings_page = max(1, int(parts[3])) if len(parts) > 3 else 1
    except ValueError:
        settings_page = 1
    token = parts[4] if len(parts) > 4 else ''
    try:
        key = bot_configuration_service.resolve_callback_token(token)
    except KeyError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_121', 'Эта настройка больше недоступна'),
            show_alert=True,
        )
        return
    if bot_configuration_service.is_read_only(key):
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_132', 'Эта настройка доступна только для чтения'),
            show_alert=True,
        )
        return
    try:
        await bot_configuration_service.reset_value(db, key)
    except ReadOnlySettingError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_132', 'Эта настройка доступна только для чтения'),
            show_alert=True,
        )
        return
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await db.commit()

    text = _render_setting_text(key, language=db_user.language)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page, language=db_user.language)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )
    await callback.answer(texts.t('BOT_SETTINGS_UI_RESET_SETTING_211', 'Сброшено к значению по умолчанию'))


@admin_required
@error_handler
async def toggle_setting(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 4)
    group_key = parts[1] if len(parts) > 1 else CATEGORY_FALLBACK_KEY
    try:
        category_page = max(1, int(parts[2])) if len(parts) > 2 else 1
    except ValueError:
        category_page = 1
    try:
        settings_page = max(1, int(parts[3])) if len(parts) > 3 else 1
    except ValueError:
        settings_page = 1
    token = parts[4] if len(parts) > 4 else ''
    try:
        key = bot_configuration_service.resolve_callback_token(token)
    except KeyError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_121', 'Эта настройка больше недоступна'),
            show_alert=True,
        )
        return
    if bot_configuration_service.is_read_only(key):
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_132', 'Эта настройка доступна только для чтения'),
            show_alert=True,
        )
        return
    current = bot_configuration_service.get_current_value(key)
    new_value = not bool(current)
    try:
        await bot_configuration_service.set_value(db, key, new_value)
    except ReadOnlySettingError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_132', 'Эта настройка доступна только для чтения'),
            show_alert=True,
        )
        return
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await db.commit()

    text = _render_setting_text(key, language=db_user.language)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page, language=db_user.language)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )
    await callback.answer(texts.t('BOT_SETTINGS_UI_TOGGLE_SETTING_215', 'Обновлено'))


@admin_required
@error_handler
async def apply_setting_choice(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    parts = callback.data.split(':', 5)
    group_key = parts[1] if len(parts) > 1 else CATEGORY_FALLBACK_KEY
    try:
        category_page = max(1, int(parts[2])) if len(parts) > 2 else 1
    except ValueError:
        category_page = 1
    try:
        settings_page = max(1, int(parts[3])) if len(parts) > 3 else 1
    except ValueError:
        settings_page = 1
    token = parts[4] if len(parts) > 4 else ''
    choice_token = parts[5] if len(parts) > 5 else ''

    try:
        key = bot_configuration_service.resolve_callback_token(token)
    except KeyError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_SIMPLE_SUBSCRIPTION_SQUAD_SELECTOR_121', 'Эта настройка больше недоступна'),
            show_alert=True,
        )
        return
    if bot_configuration_service.is_read_only(key):
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_132', 'Эта настройка доступна только для чтения'),
            show_alert=True,
        )
        return

    try:
        value = bot_configuration_service.resolve_choice_token(key, choice_token)
    except KeyError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_APPLY_SETTING_CHOICE_218', 'Это значение больше недоступно'), show_alert=True
        )
        return

    try:
        await bot_configuration_service.set_value(db, key, value)
    except ReadOnlySettingError:
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_SIMPLE_SUBSCRIPTION_SQUAD_132', 'Эта настройка доступна только для чтения'),
            show_alert=True,
        )
        return
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await db.commit()

    text = _render_setting_text(key, language=db_user.language)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page, language=db_user.language)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )
    await callback.answer(texts.t('BOT_SETTINGS_UI_APPLY_SETTING_CHOICE_220', 'Значение обновлено'))


# ── Remnawave App Config Selector ──


@admin_required
@error_handler
async def show_remna_config_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession, **kwargs):
    """Show available Remnawave subscription page configs for selection."""
    texts = get_texts(db_user.language)
    current_uuid = bot_configuration_service.get_current_value('CABINET_REMNA_SUB_CONFIG')

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            configs = await api.get_subscription_page_configs()
    except Exception as e:
        logger.error('Failed to load Remnawave configs', error=e)
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SHOW_REMNA_CONFIG_MENU_221', 'Ошибка загрузки конфигов'), show_alert=True
        )
        return

    keyboard: list[list[types.InlineKeyboardButton]] = []

    if not configs:
        text = texts.t(
            'BOT_SETTINGS_UI_SHOW_REMNA_CONFIG_MENU_222',
            '📱 <b>Конфиг приложений (Remnawave)</b>\n\nВ Remnawave не найдено конфигураций страниц подписки.\n\nСоздайте конфигурацию в панели Remnawave, затем вернитесь сюда для выбора.',
        )
    else:
        text = texts.t('BOT_SETTINGS_UI_SHOW_REMNA_CONFIG_MENU_223', '📱 <b>Конфиг приложений (Remnawave)</b>\n\n')
        if current_uuid:
            current_name = next((c.name for c in configs if c.uuid == current_uuid), None)
            if current_name:
                text += texts.t('BOT_SETTINGS_UI_SHOW_REMNA_CONFIG_MENU_224', '✅ Текущий: <b>{value1}</b>\n\n').format(
                    value1=html.escape(current_name)
                )
            else:
                text += texts.t(
                    'BOT_SETTINGS_UI_SHOW_REMNA_CONFIG_MENU_225', '⚠️ Текущий UUID не найден: <code>{value1}</code>\n\n'
                ).format(value1=html.escape(str(current_uuid)))
        else:
            text += texts.t(
                'BOT_SETTINGS_UI_SHOW_REMNA_CONFIG_MENU_226', 'ℹ️ Конфиг не выбран (гайд-режим отключён)\n\n'
            )

        text += texts.t('BOT_SETTINGS_UI_SHOW_REMNA_CONFIG_MENU_227', 'Выберите конфигурацию для гайд-режима:')

        for config in configs:
            prefix = '✅ ' if config.uuid == current_uuid else ''
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{prefix}{config.name}',
                        callback_data=f'admin_remna_select_{config.uuid}',
                    )
                ]
            )

    if current_uuid:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('BOT_SETTINGS_UI_SHOW_REMNA_CONFIG_MENU_228', '🗑 Сбросить (отключить гайд-режим)'),
                    callback_data='admin_remna_clear',
                )
            ]
        )

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BOT_SETTINGS_UI_BUILD_SETTING_KEYBOARD_099', '⬅️ Назад'),
                callback_data='admin_submenu_settings',
            )
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def select_remna_config(callback: types.CallbackQuery, db_user: User, db: AsyncSession, **kwargs):
    """Select a Remnawave subscription page config."""
    texts = get_texts(db_user.language)
    uuid = callback.data.replace('admin_remna_select_', '')

    # Validate UUID format
    import re as _re

    if not _re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', uuid):
        await callback.answer(
            texts.t('BOT_SETTINGS_UI_SELECT_REMNA_CONFIG_230', 'Некорректный UUID конфигурации'), show_alert=True
        )
        return

    try:
        await bot_configuration_service.set_value(db, 'CABINET_REMNA_SUB_CONFIG', uuid)
        await db.commit()
    except Exception as e:
        logger.error('Failed to save Remnawave config UUID', error=e)
        await callback.answer(texts.t('BOT_SETTINGS_UI_SELECT_REMNA_CONFIG_231', 'Ошибка сохранения'), show_alert=True)
        return

    # Invalidate app config cache
    from app.handlers.subscription.common import invalidate_app_config_cache

    invalidate_app_config_cache()

    await callback.answer(texts.t('BOT_SETTINGS_UI_SELECT_REMNA_CONFIG_232', '✅ Конфиг выбран'), show_alert=True)

    # Re-render the menu
    await show_remna_config_menu(callback, db_user=db_user, db=db)


@admin_required
@error_handler
async def clear_remna_config(callback: types.CallbackQuery, db_user: User, db: AsyncSession, **kwargs):
    """Clear the Remnawave config, disabling guide mode until new config is selected."""
    texts = get_texts(db_user.language)
    try:
        await bot_configuration_service.set_value(db, 'CABINET_REMNA_SUB_CONFIG', '')
        await db.commit()
    except Exception as e:
        logger.error('Failed to clear Remnawave config', error=e)
        await callback.answer(texts.t('BOT_SETTINGS_UI_CLEAR_REMNA_CONFIG_233', 'Ошибка сброса'), show_alert=True)
        return

    from app.handlers.subscription.common import invalidate_app_config_cache

    invalidate_app_config_cache()

    await callback.answer(texts.t('BOT_SETTINGS_UI_CLEAR_REMNA_CONFIG_234', '✅ Конфиг сброшен'), show_alert=True)
    await show_remna_config_menu(callback, db_user=db_user, db=db)


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(
        show_bot_config_menu,
        F.data == 'admin_bot_config',
    )
    dp.callback_query.register(
        start_settings_search,
        F.data == 'botcfg_action:search',
    )
    dp.callback_query.register(
        show_presets,
        F.data == 'botcfg_action:presets',
    )
    dp.callback_query.register(
        apply_preset,
        F.data.startswith('botcfg_preset_apply:'),
    )
    dp.callback_query.register(
        preview_preset,
        F.data.startswith('botcfg_preset:') & (~F.data.startswith('botcfg_preset_apply:')),
    )
    dp.callback_query.register(
        export_settings,
        F.data == 'botcfg_action:export',
    )
    dp.callback_query.register(
        start_import_settings,
        F.data == 'botcfg_action:import',
    )
    dp.callback_query.register(
        show_settings_history,
        F.data == 'botcfg_action:history',
    )
    dp.callback_query.register(
        show_help,
        F.data == 'botcfg_action:help',
    )
    dp.callback_query.register(
        show_bot_config_group,
        F.data.startswith('botcfg_group:') & (~F.data.endswith(':noop')),
    )
    dp.callback_query.register(
        show_bot_config_category,
        F.data.startswith('botcfg_cat:'),
    )
    dp.callback_query.register(
        test_remnawave_connection,
        F.data.startswith('botcfg_test_remnawave:'),
    )
    dp.callback_query.register(
        test_payment_provider,
        F.data.startswith('botcfg_test_payment:'),
    )
    dp.callback_query.register(
        select_simple_subscription_squad,
        F.data.startswith('botcfg_simple_squad_select:'),
    )
    dp.callback_query.register(
        show_simple_subscription_squad_selector,
        F.data.startswith('botcfg_simple_squad:'),
    )
    dp.callback_query.register(
        show_bot_config_setting,
        F.data.startswith('botcfg_setting:'),
    )
    dp.callback_query.register(
        start_edit_setting,
        F.data.startswith('botcfg_edit:'),
    )
    dp.callback_query.register(
        reset_setting,
        F.data.startswith('botcfg_reset:'),
    )
    dp.callback_query.register(
        toggle_setting,
        F.data.startswith('botcfg_toggle:'),
    )
    dp.callback_query.register(
        apply_setting_choice,
        F.data.startswith('botcfg_choice:'),
    )
    dp.message.register(
        handle_direct_setting_input,
        StateFilter(None),
        F.text,
        BotConfigInputFilter(),
    )
    dp.message.register(
        handle_edit_setting,
        BotConfigStates.waiting_for_value,
    )
    dp.message.register(
        handle_search_query,
        BotConfigStates.waiting_for_search_query,
    )
    dp.message.register(
        handle_import_message,
        BotConfigStates.waiting_for_import_file,
    )
    # Remnawave app config selector
    dp.callback_query.register(
        show_remna_config_menu,
        F.data == 'admin_remna_config',
    )
    dp.callback_query.register(
        select_remna_config,
        F.data.startswith('admin_remna_select_'),
    )
    dp.callback_query.register(
        clear_remna_config,
        F.data == 'admin_remna_clear',
    )
