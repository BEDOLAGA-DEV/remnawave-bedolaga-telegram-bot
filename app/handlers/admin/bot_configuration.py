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
        'description': 'YooKassa, CryptoBot, Heleket, CloudPayments, Freekassa, MulenPay, PAL24, Wata, Platega, Tribute, Kassa AI, RioPay, SeverPay и Telegram Stars.',
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
            'EXTERNAL_ADMIN',
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


def _get_group_meta(group_key: str) -> dict[str, object]:
    return CATEGORY_GROUP_METADATA.get(group_key, {})


def _get_group_description(group_key: str) -> str:
    meta = _get_group_meta(group_key)
    return str(meta.get('description', ''))


def _get_group_icon(group_key: str) -> str:
    meta = _get_group_meta(group_key)
    return str(meta.get('icon', '⚙️'))


def _get_group_status(group_key: str) -> tuple[str, str]:
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
            return '🔴', 'Нет активных платежей'
        if active < total:
            return '🟡', f'Активно {active} из {total}'
        return '🟢', 'Все системы активны'

    if key == 'remnawave':
        api_ready = bool(
            settings.REMNAWAVE_API_URL
            and (settings.REMNAWAVE_API_KEY or (settings.REMNAWAVE_USERNAME and settings.REMNAWAVE_PASSWORD))
        )
        return ('🟢', 'API подключено') if api_ready else ('🟡', 'Нужно указать URL и ключи')

    if key == 'server':
        mode = (settings.SERVER_STATUS_MODE or '').lower()
        monitoring_active = mode not in {'', 'disabled'}
        if monitoring_active:
            return '🟢', 'Мониторинг активен'
        if settings.MONITORING_INTERVAL:
            return '🟡', 'Доступны только отчеты'
        return '⚪', 'Мониторинг выключен'

    if key == 'maintenance':
        if settings.MAINTENANCE_MODE:
            return '🟡', 'Режим ТО включен'
        return '🟢', 'Рабочий режим'

    if key == 'notifications':
        user_on = settings.is_notifications_enabled()
        admin_on = settings.is_admin_notifications_enabled()
        if user_on and admin_on:
            return '🟢', 'Все уведомления включены'
        if user_on or admin_on:
            return '🟡', 'Часть уведомлений включена'
        return '⚪', 'Уведомления отключены'

    if key == 'trial':
        if settings.TRIAL_DURATION_DAYS > 0:
            return '🟢', f'{settings.TRIAL_DURATION_DAYS} дней пробного периода'
        return '⚪', 'Триал отключен'

    if key == 'referral':
        active = (
            settings.REFERRAL_COMMISSION_PERCENT
            or settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS
            or settings.REFERRAL_INVITER_BONUS_KOPEKS
        )
        return ('🟢', 'Программа активна') if active else ('⚪', 'Бонусы не заданы')

    if key == 'core':
        token_ok = bool(getattr(settings, 'BOT_TOKEN', ''))
        # Channel subscription channels are now managed via DB (admin panel),
        # not a single CHANNEL_LINK setting. Dashboard cannot async-query DB here.
        if token_ok:
            return '🟢', 'Бот готов к работе'
        return '🟡', 'Проверьте токен бота'

    if key == 'subscriptions':
        price_ready = settings.PRICE_30_DAYS > 0 and settings.AVAILABLE_SUBSCRIPTION_PERIODS
        return ('🟢', 'Тарифы настроены') if price_ready else ('⚪', 'Нужно задать цены')

    if key == 'database':
        mode = (settings.DATABASE_MODE or 'auto').lower()
        if mode == 'postgresql':
            return '🟢', 'PostgreSQL'
        if mode == 'sqlite':
            return '🟡', 'SQLite режим'
        return '🟢', 'Авто режим'

    if key == 'interface':
        branding = bool(settings.ENABLE_LOGO_MODE or settings.MINIAPP_CUSTOM_URL)
        return ('🟢', 'Брендинг настроен') if branding else ('⚪', 'Настройки по умолчанию')

    return '🟢', 'Готово к работе'


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


def _render_dashboard_overview() -> str:
    grouped = _get_grouped_categories()
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
        '⚙️ <b>ПАНЕЛЬ УПРАВЛЕНИЯ БОТОМ</b>',
        '',
        f'Всего параметров: <b>{total_settings}</b> • Переопределено: <b>{total_overrides}</b>',
        '',
        '<b>Группы настроек</b>',
        '',
    ]

    for group_key, title, items in grouped:
        status_icon, status_text = _get_group_status(group_key)
        total = sum(count for _, _, count in items)
        lines.append(f'{status_icon} <b>{title}</b> — {status_text}')
        lines.append(f'└ Настроек: {total}')
        lines.append('')

    lines.append('🔍 Используйте поиск, чтобы быстро найти нужный параметр по ключу или названию.')
    return '\n'.join(lines).strip()


def _build_group_category_index() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for group_key, _title, items in _get_grouped_categories():
        mapping[group_key] = [category_key for category_key, _label, _count in items]
    return mapping


def _perform_settings_search(query: str) -> list[dict[str, object]]:
    normalized = query.strip().lower()
    if not normalized:
        return []

    categories = bot_configuration_service.get_categories()
    group_category_index = _build_group_category_index()
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
            guidance = bot_configuration_service.get_setting_guidance(definition.key)
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
                    'category_label': definition.category_label,
                    'group_key': group_key,
                    'category_page': category_page,
                    'settings_page': settings_page,
                    'token': bot_configuration_service.get_callback_token(definition.key),
                    'value': bot_configuration_service.format_value_human(
                        definition.key,
                        bot_configuration_service.get_current_value(definition.key),
                    ),
                }
            )

    results.sort(key=lambda item: item['name'].lower())
    return results[:20]


def _build_search_results_keyboard(results: list[dict[str, object]]) -> types.InlineKeyboardMarkup:
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
                text='⬅️ В главное меню',
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
    await state.set_state(BotConfigStates.waiting_for_search_query)
    await state.update_data(botcfg_origin='bot_config')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ В главное меню', callback_data='admin_bot_config')]]
    )

    await callback.message.edit_text(
        '🔍 <b>Поиск по настройкам</b>\n\n'
        'Отправьте часть ключа или названия настройки. \n'
        'Например: <code>yookassa</code> или <code>уведомления</code>.',
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer('Введите запрос', show_alert=False)


@admin_required
@error_handler
async def handle_search_query(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    if message.chat.type != 'private':
        return

    data = await state.get_data()
    if data.get('botcfg_origin') != 'bot_config':
        return

    query = (message.text or '').strip()
    results = _perform_settings_search(query)

    if results:
        keyboard = _build_search_results_keyboard(results)
        lines = [
            '🔍 <b>Результаты поиска</b>',
            f'Запрос: <code>{html.escape(query)}</code>',
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
                        text='⬅️ Попробовать снова',
                        callback_data='botcfg_action:search',
                    )
                ],
                [types.InlineKeyboardButton(text='🏠 Главное меню', callback_data='admin_bot_config')],
            ]
        )
        text = (
            '🔍 <b>Результаты поиска</b>\n\n'
            f'Запрос: <code>{html.escape(query)}</code>\n\n'
            'Ничего не найдено. Попробуйте изменить формулировку.'
        )

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
    lines = [
        '🎯 <b>Готовые пресеты</b>',
        '',
        'Выберите набор параметров, чтобы быстро применить его к боту.',
        '',
    ]
    for key, meta in PRESET_METADATA.items():
        lines.append(f'• <b>{meta["title"]}</b> — {meta["description"]}')
    text = '\n'.join(lines)

    buttons: list[types.InlineKeyboardButton] = []
    for key, meta in PRESET_METADATA.items():
        buttons.append(types.InlineKeyboardButton(text=meta['title'], callback_data=f'botcfg_preset:{key}'))

    rows: list[list[types.InlineKeyboardButton]] = []
    for chunk in _chunk(buttons, 2):
        rows.append(list(chunk))
    rows.append([types.InlineKeyboardButton(text='⬅️ Главное меню', callback_data='admin_bot_config')])

    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


def _format_preset_preview(preset_key: str) -> tuple[str, list[str]]:
    config = PRESET_CONFIGS.get(preset_key, {})
    meta = PRESET_METADATA.get(preset_key, {'title': preset_key, 'description': ''})
    title = meta['title']
    description = meta.get('description', '')

    lines = [f'🎯 <b>{title}</b>']
    if description:
        lines.append(description)
    lines.append('')
    lines.append('Будут установлены следующие значения:')

    for index, (setting_key, new_value) in enumerate(config.items(), start=1):
        current_value = bot_configuration_service.get_current_value(setting_key)
        current_pretty = bot_configuration_service.format_value_human(setting_key, current_value)
        new_pretty = bot_configuration_service.format_value_human(setting_key, new_value)
        lines.append(f'{index}. <code>{setting_key}</code>\n   Текущее: {current_pretty}\n   Новое: {new_pretty}')

    return title, lines


@admin_required
@error_handler
async def preview_preset(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    parts = callback.data.split(':', 1)
    preset_key = parts[1] if len(parts) > 1 else ''
    if preset_key not in PRESET_CONFIGS:
        await callback.answer('Этот пресет недоступен', show_alert=True)
        return

    title, lines = _format_preset_preview(preset_key)
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='✅ Применить', callback_data=f'botcfg_preset_apply:{preset_key}')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='botcfg_action:presets')],
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
    parts = callback.data.split(':', 1)
    preset_key = parts[1] if len(parts) > 1 else ''
    config = PRESET_CONFIGS.get(preset_key)
    if not config:
        await callback.answer('Этот пресет недоступен', show_alert=True)
        return

    applied: list[str] = []
    for setting_key, value in config.items():
        try:
            await bot_configuration_service.set_value(db, setting_key, value)
            applied.append(setting_key)
        except ReadOnlySettingError:
            logger.info(
                'Пропускаем настройку из пресета : только для чтения', setting_key=setting_key, preset_key=preset_key
            )
        except Exception as error:
            logger.warning(
                'Не удалось применить пресет для', preset_key=preset_key, setting_key=setting_key, error=error
            )
    await db.commit()

    title = PRESET_METADATA.get(preset_key, {}).get('title', preset_key)
    summary_lines = [
        f'✅ Пресет <b>{title}</b> применен',
        '',
        f'Изменено параметров: <b>{len(applied)}</b>',
    ]
    if applied:
        summary_lines.append('\n'.join(f'• <code>{key}</code>' for key in applied))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='⬅️ К пресетам', callback_data='botcfg_action:presets')],
            [types.InlineKeyboardButton(text='🏠 Главное меню', callback_data='admin_bot_config')],
        ]
    )

    await callback.message.edit_text(
        '\n'.join(summary_lines),
        parse_mode='HTML',
        reply_markup=keyboard,
    )
    await callback.answer('Настройки обновлены', show_alert=False)


@admin_required
@error_handler
async def export_settings(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
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
        caption='📤 Экспорт текущих настроек',
        parse_mode='HTML',
    )
    await callback.answer('Файл готов', show_alert=False)


@admin_required
@error_handler
async def start_import_settings(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await state.set_state(BotConfigStates.waiting_for_import_file)
    await state.update_data(botcfg_origin='bot_config')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Главное меню', callback_data='admin_bot_config')]]
    )

    await callback.message.edit_text(
        '📥 <b>Импорт настроек</b>\n\n'
        'Прикрепите .env файл или отправьте текстом пары <code>KEY=value</code>.\n'
        'Неизвестные параметры будут проигнорированы.',
        parse_mode='HTML',
        reply_markup=keyboard,
    )
    await callback.answer('Загрузите файл .env', show_alert=False)


@admin_required
@error_handler
async def handle_import_message(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
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
            '❌ Не удалось найти параметры в файле. Убедитесь, что используется формат KEY=value.',
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
                value_to_apply = bot_configuration_service.deserialize_value(setting_key, raw_value)
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

    await db.commit()

    summary_lines = [
        '📥 <b>Импорт завершен</b>',
        f'Обновлено параметров: <b>{len(applied)}</b>',
    ]
    if applied:
        summary_lines.append('\n'.join(f'• <code>{key}</code>' for key in applied))

    if skipped:
        summary_lines.append('\nПропущено (неизвестные ключи):')
        summary_lines.append('\n'.join(f'• <code>{key}</code>' for key in skipped))

    if errors:
        summary_lines.append('\nОшибки разбора:')
        summary_lines.append('\n'.join(f'• {html.escape(err)}' for err in errors))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='🏠 Главное меню', callback_data='admin_bot_config')]]
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
    result = await db.execute(select(SystemSetting).order_by(SystemSetting.updated_at.desc()).limit(10))
    rows = result.scalars().all()

    lines = ['🕘 <b>История изменений</b>', '']
    if rows:
        for row in rows:
            timestamp = row.updated_at or row.created_at
            ts_text = timestamp.strftime('%d.%m %H:%M') if timestamp else '—'
            try:
                parsed_value = bot_configuration_service.deserialize_value(row.key, row.value)
                formatted_value = bot_configuration_service.format_value_human(row.key, parsed_value)
            except Exception:
                formatted_value = row.value or '—'
            lines.append(f'{ts_text} • <code>{row.key}</code> = {formatted_value}')
    else:
        lines.append('История изменений пуста.')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Главное меню', callback_data='admin_bot_config')]]
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
    text = (
        '❓ <b>Как работать с панелью</b>\n\n'
        '• Навигируйте по категориям, чтобы увидеть связанные настройки.\n'
        '• Значок ✳️ рядом с параметром означает, что значение переопределено.\n'
        '• Используйте 🔍 поиск для быстрого доступа к нужной настройке.\n'
        '• Экспортируйте .env перед крупными изменениями, чтобы иметь резервную копию.\n'
        '• Импорт позволяет восстановить конфигурацию или применить шаблон.\n'
        '• Все секретные ключи скрываются в интерфейсе автоматически.'
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='🏠 Главное меню', callback_data='admin_bot_config')]]
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


def _get_grouped_categories() -> list[tuple[str, str, list[tuple[str, str, int]]]]:
    categories = bot_configuration_service.get_categories()
    categories_map = {key: (label, count) for key, label, count in categories}
    used: set[str] = set()
    grouped: list[tuple[str, str, list[tuple[str, str, int]]]] = []

    for group_key, title, category_keys in CATEGORY_GROUP_DEFINITIONS:
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
        grouped.append((CATEGORY_FALLBACK_KEY, CATEGORY_FALLBACK_TITLE, remaining))

    return grouped


def _build_groups_keyboard() -> types.InlineKeyboardMarkup:
    grouped = _get_grouped_categories()
    rows: list[list[types.InlineKeyboardButton]] = []

    for group_key, title, items in grouped:
        sum(count for _, _, count in items)
        status_icon, status_text = _get_group_status(group_key)
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
                text='🔍 Найти настройку',
                callback_data='botcfg_action:search',
            ),
            types.InlineKeyboardButton(
                text='🎯 Пресеты',
                callback_data='botcfg_action:presets',
            ),
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text='📤 Экспорт .env',
                callback_data='botcfg_action:export',
            ),
            types.InlineKeyboardButton(
                text='📥 Импорт .env',
                callback_data='botcfg_action:import',
            ),
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text='🕘 История',
                callback_data='botcfg_action:history',
            ),
            types.InlineKeyboardButton(
                text='❓ Помощь',
                callback_data='botcfg_action:help',
            ),
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text='⬅️ Назад в админку',
                callback_data='admin_submenu_settings',
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _build_categories_keyboard(
    group_key: str,
    group_title: str,
    categories: list[tuple[str, str, int]],
    page: int = 1,
) -> types.InlineKeyboardMarkup:
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
                text='⬅️ К разделам',
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
                    text='🔌 Проверить подключение',
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
        test_payment_buttons.append([_test_button(f'{label} · тест', 'yookassa')])
    elif category_key == 'TRIBUTE':
        label = texts.t('PAYMENT_CARD_TRIBUTE', '💳 Банковская карта (Tribute)')
        test_payment_buttons.append([_test_button(f'{label} · тест', 'tribute')])
    elif category_key == 'MULENPAY':
        label = texts.t(
            'PAYMENT_CARD_MULENPAY',
            '💳 Банковская карта ({mulenpay_name})',
        ).format(mulenpay_name=settings.get_mulenpay_display_name())
        test_payment_buttons.append([_test_button(f'{label} · тест', 'mulenpay')])
    elif category_key == 'WATA':
        label = texts.t('PAYMENT_CARD_WATA', '💳 Банковская карта (WATA)')
        test_payment_buttons.append([_test_button(f'{label} · тест', 'wata')])
    elif category_key == 'PAL24':
        label = texts.t('PAYMENT_CARD_PAL24', '💳 Банковская карта (PayPalych)')
        test_payment_buttons.append([_test_button(f'{label} · тест', 'pal24')])
    elif category_key == 'TELEGRAM':
        label = texts.t('PAYMENT_TELEGRAM_STARS', '⭐ Telegram Stars')
        test_payment_buttons.append([_test_button(f'{label} · тест', 'stars')])
    elif category_key == 'CRYPTOBOT':
        label = texts.t('PAYMENT_CRYPTOBOT', '🪙 Криптовалюта (CryptoBot)')
        test_payment_buttons.append([_test_button(f'{label} · тест', 'cryptobot')])
    elif category_key == 'FREEKASSA':
        label = texts.t('PAYMENT_FREEKASSA', '💳 Freekassa')
        test_payment_buttons.append([_test_button(f'{label} · тест', 'freekassa')])
    elif category_key == 'KASSA_AI':
        label = texts.t('PAYMENT_KASSA_AI', f'💳 {settings.get_kassa_ai_display_name()}')
        test_payment_buttons.append([_test_button(f'{label} · тест', 'kassa_ai')])
    elif category_key == 'RIOPAY':
        label = texts.t('PAYMENT_RIOPAY', f'💳 {settings.get_riopay_display_name()}')
        test_payment_buttons.append([_test_button(f'{label} · тест', 'riopay')])
    elif category_key == 'SEVERPAY':
        label = texts.t('PAYMENT_SEVERPAY', f'💳 {settings.get_severpay_display_name()}')
        test_payment_buttons.append([_test_button(f'{label} · тест', 'severpay')])

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
                text='⬅️ К категориям',
                callback_data=f'botcfg_group:{group_key}:{category_page}',
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _build_setting_keyboard(
    key: str,
    group_key: str,
    category_page: int,
    settings_page: int,
) -> types.InlineKeyboardMarkup:
    definition = bot_configuration_service.get_definition(key)
    rows: list[list[types.InlineKeyboardButton]] = []
    callback_token = bot_configuration_service.get_callback_token(key)
    is_read_only = bot_configuration_service.is_read_only(key)

    choice_options = bot_configuration_service.get_choice_options(key)
    if choice_options and not is_read_only:
        current_value = bot_configuration_service.get_current_value(key)
        choice_buttons: list[types.InlineKeyboardButton] = []
        for option in choice_options:
            choice_token = bot_configuration_service.get_choice_token(key, option.value)
            if choice_token is None:
                continue
            button_text = option.label
            if current_value == option.value and not button_text.startswith('✅'):
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
                    text='🌍 Выбрать сквад',
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
                    text='🔁 Переключить',
                    callback_data=(f'botcfg_toggle:{group_key}:{category_page}:{settings_page}:{callback_token}'),
                )
            ]
        )

    if not is_read_only:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='✏️ Изменить',
                    callback_data=(f'botcfg_edit:{group_key}:{category_page}:{settings_page}:{callback_token}'),
                )
            ]
        )

    if bot_configuration_service.has_override(key) and not is_read_only:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='♻️ Сбросить',
                    callback_data=(f'botcfg_reset:{group_key}:{category_page}:{settings_page}:{callback_token}'),
                )
            ]
        )

    if is_read_only:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text='🔒 Только для чтения',
                    callback_data='botcfg_group:noop',
                )
            ]
        )

    rows.append(
        [
            types.InlineKeyboardButton(
                text='⬅️ Назад',
                callback_data=(f'botcfg_cat:{group_key}:{definition.category_key}:{category_page}:{settings_page}'),
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _render_setting_text(key: str) -> str:
    summary = bot_configuration_service.get_setting_summary(key)
    guidance = bot_configuration_service.get_setting_guidance(key)

    definition = bot_configuration_service.get_definition(key)

    description = guidance.get('description') or '—'
    format_hint = guidance.get('format') or '—'
    example = guidance.get('example') or '—'
    warning = guidance.get('warning') or '—'
    dependencies = guidance.get('dependencies') or '—'
    type_label = guidance.get('type') or summary.get('type') or definition.type_label

    lines = [
        f'🧩 <b>{summary["name"]}</b>',
        f'🔑 Ключ: <code>{summary["key"]}</code>',
        f'📁 Категория: {summary["category_label"]}',
        f'📝 Тип: {type_label}',
        f'📌 Текущее: {summary["current"]}',
    ]

    original_value = summary.get('original')
    if original_value not in {None, ''}:
        lines.append(f'📦 По умолчанию: {original_value}')

    lines.append(f'✳️ Переопределено: {"Да" if summary["has_override"] else "Нет"}')

    if summary.get('is_read_only'):
        lines.append('🔒 Режим: Только для чтения (управляется автоматически)')

    lines.append('')
    if description:
        lines.append(f'📘 Описание: {description}')
    if format_hint:
        lines.append(f'📐 Формат: {format_hint}')
    if example:
        lines.append(f'💡 Пример: {example}')
    if warning:
        lines.append(f'⚠️ Важно: {warning}')
    if dependencies:
        lines.append(f'🔗 Связанные: {dependencies}')

    choices = bot_configuration_service.get_choice_options(key)
    if choices:
        current_raw = bot_configuration_service.get_current_value(key)
        lines.append('')
        lines.append('📋 Доступные значения:')
        for option in choices:
            marker = '✅' if current_raw == option.value else '•'
            value_display = bot_configuration_service.format_value_human(key, option.value)
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
    keyboard = _build_groups_keyboard()
    overview = _render_dashboard_overview()
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
    group_key, page = _parse_group_payload(callback.data)
    grouped = _get_grouped_categories()
    group_lookup = {key: (title, items) for key, title, items in grouped}

    if group_key not in group_lookup:
        await callback.answer('Эта группа больше недоступна', show_alert=True)
        return

    group_title, items = group_lookup[group_key]
    keyboard = _build_categories_keyboard(group_key, group_title, items, page)
    status_icon, status_text = _get_group_status(group_key)
    description = _get_group_description(group_key)
    icon = _get_group_icon(group_key)
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
        lines.append(f'Статус: {status_icon} {status_text}')
    lines.append(f'🏠 → {clean_title}')
    if description:
        lines.append('')
        lines.append(description)
    lines.append('')
    lines.append('📂 Категории группы:')
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
    group_key, category_key, category_page, settings_page = _parse_category_payload(callback.data)
    definitions = bot_configuration_service.get_settings_for_category(category_key)

    if not definitions:
        await callback.answer('В этой категории пока нет настроек', show_alert=True)
        return

    category_label = definitions[0].category_label
    category_description = bot_configuration_service.get_category_description(category_key)
    group_meta = _get_group_meta(group_key)
    group_title = str(group_meta.get('title', group_key))
    group_icon = _get_group_icon(group_key)
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
    text_lines.append('📋 Список настроек категории:')
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
        await callback.answer('Эта настройка больше недоступна', show_alert=True)
        return

    if key != 'SIMPLE_SUBSCRIPTION_SQUAD_UUID':
        await callback.answer('Эта настройка больше недоступна', show_alert=True)
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
    current_display = 'Любой доступный'

    if current_uuid:
        selected_server = next((srv for srv in squads if srv.squad_uuid == current_uuid), None)
        if not selected_server:
            selected_server = await get_server_squad_by_uuid(db, current_uuid)
        if selected_server:
            current_display = selected_server.display_name
        else:
            current_display = current_uuid

    lines = [
        '🌍 <b>Выберите сквад для простой покупки</b>',
        '',
        f'Текущий выбор: {html.escape(current_display)}' if current_display else 'Текущий выбор: —',
        '',
    ]

    if total_count == 0:
        lines.append('❌ Доступные сервера не найдены.')
    else:
        lines.append('Выберите сервер из списка ниже.')
        if total_pages > 1:
            lines.append(f'Страница {page}/{total_pages}')

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
                text='⬅️ Назад',
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
        await callback.answer('Не удалось определить сервер', show_alert=True)
        return

    try:
        key = bot_configuration_service.resolve_callback_token(token)
    except KeyError:
        await callback.answer('Эта настройка больше недоступна', show_alert=True)
        return

    if bot_configuration_service.is_read_only(key):
        await callback.answer('Эта настройка доступна только для чтения', show_alert=True)
        return

    server = await get_server_squad_by_id(db, server_id)
    if not server:
        await callback.answer('Сервер не найден', show_alert=True)
        return

    try:
        await bot_configuration_service.set_value(db, key, server.squad_uuid)
    except ReadOnlySettingError:
        await callback.answer('Эта настройка доступна только для чтения', show_alert=True)
        return

    await db.commit()

    text = _render_setting_text(key)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )
    await callback.answer('Сквад выбран')


@admin_required
@error_handler
async def test_remnawave_connection(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
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
        message = '✅ Подключение успешно'
    elif status == 'not_configured':
        message = f'⚠️ {result.get("message", "RemnaWave API не настроен")}'
    else:
        base_message = result.get('message', 'Ошибка подключения')
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
            await callback.answer('❌ YooKassa отключена', show_alert=True)
            return

        amount_kopeks = 10 * 100
        description = (settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),)
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
            await callback.answer('❌ Не удалось создать тестовый платеж YooKassa', show_alert=True)
            await _refresh_markup()
            return

        confirmation_url = payment_result['confirmation_url']
        message_text = (
            '🧪 <b>Тестовый платеж YooKassa</b>\n\n'
            f'💰 Сумма: {texts.format_price(amount_kopeks)}\n'
            f'🆔 ID: {payment_result["yookassa_payment_id"]}'
        )
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='💳 Оплатить картой',
                        url=confirmation_url,
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text='📊 Проверить статус',
                        callback_data=f'check_yookassa_{payment_result["local_payment_id"]}',
                    )
                ],
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer('✅ Ссылка на платеж YooKassa отправлена', show_alert=True)
        await _refresh_markup()
        return

    if method == 'tribute':
        if not settings.TRIBUTE_ENABLED:
            await callback.answer('❌ Tribute отключен', show_alert=True)
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
            await callback.answer('❌ Не удалось создать платеж Tribute', show_alert=True)
            await _refresh_markup()
            return

        message_text = (
            '🧪 <b>Тестовый платеж Tribute</b>\n\n'
            f'💰 Сумма: {texts.format_price(10 * 100)}\n'
            '🔗 Нажмите кнопку ниже, чтобы открыть ссылку на оплату.'
        )
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='💳 Перейти к оплате',
                        url=payment_url,
                    )
                ]
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer('✅ Ссылка на платеж Tribute отправлена', show_alert=True)
        await _refresh_markup()
        return

    if method == 'mulenpay':
        mulenpay_name = settings.get_mulenpay_display_name()
        mulenpay_name_html = settings.get_mulenpay_display_name_html()
        if not settings.is_mulenpay_enabled():
            await callback.answer(
                f'❌ {mulenpay_name} отключен',
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
                f'❌ Не удалось создать платеж {mulenpay_name}',
                show_alert=True,
            )
            await _refresh_markup()
            return

        payment_url = payment_result['payment_url']
        message_text = (
            f'🧪 <b>Тестовый платеж {mulenpay_name_html}</b>\n\n'
            f'💰 Сумма: {texts.format_price(amount_kopeks)}\n'
            f'🆔 ID: {payment_result["mulen_payment_id"]}'
        )
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='💳 Перейти к оплате',
                        url=payment_url,
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text='📊 Проверить статус',
                        callback_data=f'check_mulenpay_{payment_result["local_payment_id"]}',
                    )
                ],
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(
            f'✅ Ссылка на платеж {mulenpay_name} отправлена',
            show_alert=True,
        )
        await _refresh_markup()
        return

    if method == 'pal24':
        if not settings.is_pal24_enabled():
            await callback.answer('❌ PayPalych отключен', show_alert=True)
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
            await callback.answer('❌ Не удалось создать платеж PayPalych', show_alert=True)
            await _refresh_markup()
            return

        sbp_url = payment_result.get('sbp_url') or payment_result.get('transfer_url') or payment_result.get('link_url')
        card_url = payment_result.get('card_url')
        fallback_url = payment_result.get('link_page_url') or payment_result.get('link_url')

        if not (sbp_url or card_url or fallback_url):
            await callback.answer('❌ Не удалось создать платеж PayPalych', show_alert=True)
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

        message_text = (
            '🧪 <b>Тестовый платеж PayPalych</b>\n\n'
            f'💰 Сумма: {texts.format_price(amount_kopeks)}\n'
            f'🆔 Bill ID: {payment_result["bill_id"]}'
        )
        keyboard_rows = pay_rows + [
            [
                types.InlineKeyboardButton(
                    text='📊 Проверить статус',
                    callback_data=f'check_pal24_{payment_result["local_payment_id"]}',
                )
            ],
        ]

        reply_markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer('✅ Ссылка на платеж PayPalych отправлена', show_alert=True)
        await _refresh_markup()
        return

    if method == 'stars':
        if not settings.TELEGRAM_STARS_ENABLED:
            await callback.answer('❌ Telegram Stars отключены', show_alert=True)
            return

        stars_rate = settings.get_stars_rate()
        amount_kopeks = max(1, int(round(stars_rate * 100)))
        payload = f'admin_stars_test_{db_user.id}_{int(time.time())}'
        try:
            invoice_link = await payment_service.create_stars_invoice(
                amount_kopeks=amount_kopeks,
                description='Тестовый платеж Telegram Stars (админ)',
                payload=payload,
            )
        except Exception:
            invoice_link = None

        if not invoice_link:
            await callback.answer('❌ Не удалось создать платеж Telegram Stars', show_alert=True)
            await _refresh_markup()
            return

        stars_amount = TelegramStarsService.calculate_stars_from_rubles(amount_kopeks / 100)
        message_text = (
            '🧪 <b>Тестовый платеж Telegram Stars</b>\n\n'
            f'💰 Сумма: {texts.format_price(amount_kopeks)}\n'
            f'⭐ К оплате: {stars_amount}'
        )
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
        await callback.answer('✅ Ссылка на платеж Stars отправлена', show_alert=True)
        await _refresh_markup()
        return

    if method == 'cryptobot':
        if not settings.is_cryptobot_enabled():
            await callback.answer('❌ CryptoBot отключен', show_alert=True)
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
            await callback.answer('❌ Не удалось создать платеж CryptoBot', show_alert=True)
            await _refresh_markup()
            return

        payment_url = (
            payment_result.get('bot_invoice_url')
            or payment_result.get('mini_app_invoice_url')
            or payment_result.get('web_app_invoice_url')
        )

        if not payment_url:
            await callback.answer('❌ Не удалось получить ссылку на оплату CryptoBot', show_alert=True)
            await _refresh_markup()
            return

        amount_kopeks = int(amount_rubles * 100)
        message_text = (
            '🧪 <b>Тестовый платеж CryptoBot</b>\n\n'
            f'💰 Сумма к зачислению: {texts.format_price(amount_kopeks)}\n'
            f'💵 К оплате: {amount_usd:.2f} USD\n'
            f'🪙 Актив: {payment_result["asset"]}'
        )
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🪙 Открыть счет', url=payment_url)],
                [
                    types.InlineKeyboardButton(
                        text='📊 Проверить статус',
                        callback_data=f'check_cryptobot_{payment_result["local_payment_id"]}',
                    )
                ],
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer('✅ Ссылка на платеж CryptoBot отправлена', show_alert=True)
        await _refresh_markup()
        return

    if method == 'freekassa':
        if not settings.is_freekassa_enabled():
            await callback.answer('❌ Freekassa отключена', show_alert=True)
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
            await callback.answer('❌ Не удалось создать тестовый платеж Freekassa', show_alert=True)
            await _refresh_markup()
            return

        payment_url = payment_result['payment_url']
        message_text = (
            '🧪 <b>Тестовый платеж Freekassa</b>\n\n'
            f'💰 Сумма: {texts.format_price(amount_kopeks)}\n'
            f'🆔 Order ID: {payment_result["order_id"]}'
        )
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='💳 Перейти к оплате',
                        url=payment_url,
                    )
                ]
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer('✅ Ссылка на платеж Freekassa отправлена', show_alert=True)
        await _refresh_markup()
        return

    if method == 'kassa_ai':
        if not settings.is_kassa_ai_enabled():
            await callback.answer('❌ Kassa AI отключена', show_alert=True)
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
            await callback.answer('❌ Не удалось создать тестовый платеж Kassa AI', show_alert=True)
            await _refresh_markup()
            return

        payment_url = payment_result['payment_url']
        display_name = settings.get_kassa_ai_display_name()
        message_text = (
            f'🧪 <b>Тестовый платеж {display_name}</b>\n\n'
            f'💰 Сумма: {texts.format_price(amount_kopeks)}\n'
            f'🆔 Order ID: {payment_result["order_id"]}'
        )
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='💳 Перейти к оплате',
                        url=payment_url,
                    )
                ]
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(f'✅ Ссылка на платеж {display_name} отправлена', show_alert=True)
        await _refresh_markup()
        return

    if method == 'riopay':
        if not settings.is_riopay_enabled():
            await callback.answer('❌ RioPay отключена', show_alert=True)
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
            await callback.answer('❌ Не удалось создать тестовый платеж RioPay', show_alert=True)
            await _refresh_markup()
            return

        payment_url = payment_result['payment_url']
        display_name = settings.get_riopay_display_name()
        message_text = (
            f'🧪 <b>Тестовый платеж {display_name}</b>\n\n'
            f'💰 Сумма: {texts.format_price(amount_kopeks)}\n'
            f'🆔 Order ID: {payment_result["order_id"]}'
        )
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='💳 Перейти к оплате',
                        url=payment_url,
                    )
                ]
            ]
        )
        await callback.message.answer(message_text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer(f'✅ Ссылка на платеж {display_name} отправлена', show_alert=True)
        await _refresh_markup()
        return

    await callback.answer('❌ Неизвестный способ тестирования платежа', show_alert=True)
    await _refresh_markup()


@admin_required
@error_handler
async def show_bot_config_setting(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
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
        await callback.answer('Эта настройка больше недоступна', show_alert=True)
        return
    text = _render_setting_text(key)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page)
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
        await callback.answer('Эта настройка больше недоступна', show_alert=True)
        return
    if bot_configuration_service.is_read_only(key):
        await callback.answer('Эта настройка доступна только для чтения', show_alert=True)
        return
    definition = bot_configuration_service.get_definition(key)

    summary = bot_configuration_service.get_setting_summary(key)
    texts = get_texts(db_user.language)

    instructions = [
        '✏️ <b>Редактирование настройки</b>',
        f'Название: {summary["name"]}',
        f'Ключ: <code>{summary["key"]}</code>',
        f'Тип: {summary["type"]}',
        f'Текущее значение: {summary["current"]}',
        '\nОтправьте новое значение сообщением.',
    ]

    if definition.is_optional:
        instructions.append("Отправьте 'none' или оставьте пустым для сброса на значение по умолчанию.")

    instructions.append("Для отмены отправьте 'cancel'.")

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


@admin_required
@error_handler
async def handle_edit_setting(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    data = await state.get_data()
    key = data.get('setting_key')
    group_key = data.get('setting_group_key', CATEGORY_FALLBACK_KEY)
    category_page = data.get('setting_category_page', 1)
    settings_page = data.get('setting_settings_page', 1)

    if not key:
        await message.answer('Не удалось определить редактируемую настройку. Попробуйте снова.')
        await state.clear()
        return

    if bot_configuration_service.is_read_only(key):
        await message.answer('⚠️ Эта настройка доступна только для чтения.')
        await state.clear()
        return

    try:
        value = bot_configuration_service.parse_user_value(key, message.text or '')
    except ValueError as error:
        await message.answer(f'⚠️ {error}')
        return

    try:
        await bot_configuration_service.set_value(db, key, value)
    except ReadOnlySettingError:
        await message.answer('⚠️ Эта настройка доступна только для чтения.')
        await state.clear()
        return
    await db.commit()

    text = _render_setting_text(key)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page)
    await message.answer('✅ Настройка обновлена')
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
    data = await state.get_data()

    key = data.get('setting_key')
    group_key = data.get('setting_group_key', CATEGORY_FALLBACK_KEY)
    category_page = int(data.get('setting_category_page', 1) or 1)
    settings_page = int(data.get('setting_settings_page', 1) or 1)

    if not key:
        return

    if bot_configuration_service.is_read_only(key):
        await message.answer('⚠️ Эта настройка доступна только для чтения.')
        await state.clear()
        return

    try:
        value = bot_configuration_service.parse_user_value(key, message.text or '')
    except ValueError as error:
        await message.answer(f'⚠️ {error}')
        return

    try:
        await bot_configuration_service.set_value(db, key, value)
    except ReadOnlySettingError:
        await message.answer('⚠️ Эта настройка доступна только для чтения.')
        await state.clear()
        return
    await db.commit()

    text = _render_setting_text(key)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page)
    await message.answer('✅ Настройка обновлена')
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
        await callback.answer('Эта настройка больше недоступна', show_alert=True)
        return
    if bot_configuration_service.is_read_only(key):
        await callback.answer('Эта настройка доступна только для чтения', show_alert=True)
        return
    try:
        await bot_configuration_service.reset_value(db, key)
    except ReadOnlySettingError:
        await callback.answer('Эта настройка доступна только для чтения', show_alert=True)
        return
    await db.commit()

    text = _render_setting_text(key)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )
    await callback.answer('Сброшено к значению по умолчанию')


@admin_required
@error_handler
async def toggle_setting(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
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
        await callback.answer('Эта настройка больше недоступна', show_alert=True)
        return
    if bot_configuration_service.is_read_only(key):
        await callback.answer('Эта настройка доступна только для чтения', show_alert=True)
        return
    current = bot_configuration_service.get_current_value(key)
    new_value = not bool(current)
    try:
        await bot_configuration_service.set_value(db, key, new_value)
    except ReadOnlySettingError:
        await callback.answer('Эта настройка доступна только для чтения', show_alert=True)
        return
    await db.commit()

    text = _render_setting_text(key)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )
    await callback.answer('Обновлено')


@admin_required
@error_handler
async def apply_setting_choice(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
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
        await callback.answer('Эта настройка больше недоступна', show_alert=True)
        return
    if bot_configuration_service.is_read_only(key):
        await callback.answer('Эта настройка доступна только для чтения', show_alert=True)
        return

    try:
        value = bot_configuration_service.resolve_choice_token(key, choice_token)
    except KeyError:
        await callback.answer('Это значение больше недоступно', show_alert=True)
        return

    try:
        await bot_configuration_service.set_value(db, key, value)
    except ReadOnlySettingError:
        await callback.answer('Эта настройка доступна только для чтения', show_alert=True)
        return
    await db.commit()

    text = _render_setting_text(key)
    keyboard = _build_setting_keyboard(key, group_key, category_page, settings_page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await _store_setting_context(
        state,
        key=key,
        group_key=group_key,
        category_page=category_page,
        settings_page=settings_page,
    )
    await callback.answer('Значение обновлено')


# ── Remnawave App Config Selector ──


@admin_required
@error_handler
async def show_remna_config_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession, **kwargs):
    """Show available Remnawave subscription page configs for selection."""
    current_uuid = bot_configuration_service.get_current_value('CABINET_REMNA_SUB_CONFIG')

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            configs = await api.get_subscription_page_configs()
    except Exception as e:
        logger.error('Failed to load Remnawave configs', error=e)
        await callback.answer('Ошибка загрузки конфигов', show_alert=True)
        return

    keyboard: list[list[types.InlineKeyboardButton]] = []

    if not configs:
        text = (
            '📱 <b>Конфиг приложений (Remnawave)</b>\n\n'
            'В Remnawave не найдено конфигураций страниц подписки.\n\n'
            'Создайте конфигурацию в панели Remnawave, затем вернитесь сюда для выбора.'
        )
    else:
        text = '📱 <b>Конфиг приложений (Remnawave)</b>\n\n'
        if current_uuid:
            current_name = next((c.name for c in configs if c.uuid == current_uuid), None)
            if current_name:
                text += f'✅ Текущий: <b>{html.escape(current_name)}</b>\n\n'
            else:
                text += f'⚠️ Текущий UUID не найден: <code>{html.escape(str(current_uuid))}</code>\n\n'
        else:
            text += 'ℹ️ Конфиг не выбран (гайд-режим отключён)\n\n'

        text += 'Выберите конфигурацию для гайд-режима:'

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
                    text='🗑 Сбросить (отключить гайд-режим)',
                    callback_data='admin_remna_clear',
                )
            ]
        )

    keyboard.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_submenu_settings')])

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
    uuid = callback.data.replace('admin_remna_select_', '')

    # Validate UUID format
    import re as _re

    if not _re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', uuid):
        await callback.answer('Некорректный UUID конфигурации', show_alert=True)
        return

    try:
        await bot_configuration_service.set_value(db, 'CABINET_REMNA_SUB_CONFIG', uuid)
        await db.commit()
    except Exception as e:
        logger.error('Failed to save Remnawave config UUID', error=e)
        await callback.answer('Ошибка сохранения', show_alert=True)
        return

    # Invalidate app config cache
    from app.handlers.subscription.common import invalidate_app_config_cache

    invalidate_app_config_cache()

    await callback.answer('✅ Конфиг выбран', show_alert=True)

    # Re-render the menu
    await show_remna_config_menu(callback, db_user=db_user, db=db)


@admin_required
@error_handler
async def clear_remna_config(callback: types.CallbackQuery, db_user: User, db: AsyncSession, **kwargs):
    """Clear the Remnawave config, disabling guide mode until new config is selected."""
    try:
        await bot_configuration_service.set_value(db, 'CABINET_REMNA_SUB_CONFIG', '')
        await db.commit()
    except Exception as e:
        logger.error('Failed to clear Remnawave config', error=e)
        await callback.answer('Ошибка сброса', show_alert=True)
        return

    from app.handlers.subscription.common import invalidate_app_config_cache

    invalidate_app_config_cache()

    await callback.answer('✅ Конфиг сброшен', show_alert=True)
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
