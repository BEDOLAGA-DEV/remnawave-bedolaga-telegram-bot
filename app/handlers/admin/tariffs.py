"""Управление тарифами в админ-панели."""

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_groups_with_counts
from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.tariff import (
    create_tariff,
    delete_tariff,
    get_active_subscriptions_count_by_tariff_id,
    get_tariff_by_id,
    get_tariff_subscriptions_count,
    get_tariffs_with_subscriptions_count,
    update_tariff,
)
from app.database.models import Tariff, User
from app.handlers.admin.tariff_custom_days import format_custom_days_settings, register_custom_days_handlers
from app.handlers.admin.tariff_custom_traffic import (
    format_custom_traffic_settings,
    register_custom_traffic_handlers,
)
from app.handlers.admin.tariff_panel_settings import format_panel_settings, register_panel_settings_handlers
from app.handlers.admin.tariff_server_limits import format_server_limits_summary, register_server_limits_handlers
from app.localization.texts import Texts, get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatting import format_period, format_price_kopeks, format_traffic


logger = structlog.get_logger(__name__)

ITEMS_PER_PAGE = 10


def _parse_period_prices(text: str) -> dict[str, int]:
    """
    Парсит строку с ценами периодов.
    Формат: "30:9900, 90:24900, 180:44900" или "30=9900; 90=24900"
    """
    prices = {}
    text = text.replace(';', ',').replace('=', ':')

    for part in text.split(','):
        part = part.strip()
        if not part:
            continue

        if ':' not in part:
            continue

        period_str, price_str = part.split(':', 1)
        try:
            period = int(period_str.strip())
            price = int(price_str.strip())
            if period > 0 and price >= 0:
                prices[str(period)] = price
        except ValueError:
            continue

    return prices


def _format_period_prices_display(prices: dict[str, int], highlight: int | None = None, language: str = 'ru') -> str:
    """Форматирует цены периодов для отображения; выделенный период отмечен звездой."""
    if not prices:
        return get_texts(language).t('ADMIN_TARIFF_PRICES_NOT_SET', 'Не заданы')

    lines = []
    for period_str in sorted(prices.keys(), key=int):
        period = int(period_str)
        price = prices[period_str]
        mark = ' ⭐' if highlight is not None and int(highlight) == period else ''
        lines.append(f'  • {format_period(period, language=language)}: {format_price_kopeks(price)}{mark}')

    return '\n'.join(lines)


def _format_period_prices_for_edit(prices: dict[str, int]) -> str:
    """Форматирует цены периодов для редактирования."""
    if not prices:
        return '30:9900, 90:24900, 180:44900'

    parts = []
    for period_str in sorted(prices.keys(), key=int):
        parts.append(f'{period_str}:{prices[period_str]}')

    return ', '.join(parts)


def get_tariffs_list_keyboard(
    tariffs: list[tuple[Tariff, int]],
    language: str,
    page: int = 0,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру списка тарифов."""
    texts = get_texts(language)
    buttons = []

    for tariff, subs_count in tariffs:
        status = '✅' if tariff.is_active else '❌'
        button_text = f'{status} {tariff.name} ({subs_count})'
        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f'admin_tariff_view:{tariff.id}')])

    # Пагинация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text='◀️', callback_data=f'admin_tariffs_page:{page - 1}'))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text='▶️', callback_data=f'admin_tariffs_page:{page + 1}'))
    if nav_buttons:
        buttons.append(nav_buttons)

    # Кнопка создания
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_CREATE', '➕ Создать тариф'), callback_data='admin_tariff_create'
            )
        ]
    )

    # Кнопка назад
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_tariff_view_keyboard(
    tariff: Tariff,
    language: str,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру просмотра тарифа."""
    texts = get_texts(language)
    buttons = []

    # Редактирование полей
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_NAME', '✏️ Название'),
                callback_data=f'admin_tariff_edit_name:{tariff.id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_DESCRIPTION', '📝 Описание'),
                callback_data=f'admin_tariff_edit_desc:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_TRAFFIC', '📊 Трафик'),
                callback_data=f'admin_tariff_edit_traffic:{tariff.id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_DEVICES', '📱 Устройства'),
                callback_data=f'admin_tariff_edit_devices:{tariff.id}',
            ),
        ]
    )
    # Цены за периоды только для обычных тарифов (не суточных)
    is_daily = getattr(tariff, 'is_daily', False)
    if not is_daily:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_PRICES', '💰 Цены'),
                    callback_data=f'admin_tariff_edit_prices:{tariff.id}',
                ),
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_TIER', '🎚️ Уровень'),
                    callback_data=f'admin_tariff_edit_tier:{tariff.id}',
                ),
            ]
        )
        # Выделять нечего, пока нет ни одного периода.
        if tariff.period_prices:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_TARIFF_BUTTON_FEATURED_PERIOD', '⭐ Выгодный период'),
                        callback_data=f'admin_tariff_edit_highlight:{tariff.id}',
                    ),
                ]
            )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_TIER', '🎚️ Уровень'),
                    callback_data=f'admin_tariff_edit_tier:{tariff.id}',
                ),
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_DEVICE_PRICE', '📱💰 Цена за устройство'),
                callback_data=f'admin_tariff_edit_device_price:{tariff.id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_MAX_DEVICES', '📱🔒 Макс. устройств'),
                callback_data=f'admin_tariff_edit_max_devices:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_TRIAL_DAYS', '⏰ Дни триала'),
                callback_data=f'admin_tariff_edit_trial_days:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_CUSTOM_TRAFFIC', '⚙️ Произвольный трафик'),
                callback_data=f'admin_tariff_edit_custom_traffic:{tariff.id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_CUSTOM_DAYS', '📅 Произвольные дни'),
                callback_data=f'admin_tariff_edit_custom_days:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_TRAFFIC_TOPUP', '📈 Докупка трафика'),
                callback_data=f'admin_tariff_edit_traffic_topup:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_TRAFFIC_RESET', '🔄 Сброс трафика'),
                callback_data=f'admin_tariff_edit_reset_mode:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_SERVERS', '🌐 Серверы'),
                callback_data=f'admin_tariff_edit_squads:{tariff.id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_PROMO_GROUPS', '👥 Промогруппы'),
                callback_data=f'admin_tariff_edit_promo:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_SERVER_LIMITS', '🗄️ Лимиты по серверам'),
                callback_data=f'admin_tariff_edit_server_limits:{tariff.id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_MORE', '⚙️ Ещё настройки'),
                callback_data=f'admin_tariff_edit_more:{tariff.id}',
            ),
        ]
    )

    # Суточный режим - только для уже суточных тарифов показываем настройки
    # Новые тарифы делаются суточными только при создании
    if is_daily:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_DAILY_PRICE', '💰 Суточная цена'),
                    callback_data=f'admin_tariff_edit_daily_price:{tariff.id}',
                ),
            ]
        )
        # Примечание: отключение суточного режима убрано - это необратимое решение при создании

    # Переключение триала
    if tariff.is_trial_available:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_REMOVE_TRIAL', '🎁 ❌ Убрать триал'),
                    callback_data=f'admin_tariff_toggle_trial:{tariff.id}',
                )
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_SET_TRIAL', '🎁 Сделать триальным'),
                    callback_data=f'admin_tariff_toggle_trial:{tariff.id}',
                )
            ]
        )

    # Переключение активности
    highlight_label = (
        texts.t('ADMIN_TARIFF_BUTTON_UNHIGHLIGHT', '⭐ ❌ Снять выделение тарифа')
        if getattr(tariff, 'is_highlighted', False)
        else texts.t('ADMIN_TARIFF_BUTTON_HIGHLIGHT', '⭐ Выделить тариф')
    )
    buttons.append(
        [InlineKeyboardButton(text=highlight_label, callback_data=f'admin_tariff_toggle_highlight:{tariff.id}')]
    )

    if tariff.is_active:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_DEACTIVATE', '❌ Деактивировать'),
                    callback_data=f'admin_tariff_toggle:{tariff.id}',
                )
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_ACTIVATE', '✅ Активировать'),
                    callback_data=f'admin_tariff_toggle:{tariff.id}',
                )
            ]
        )

    # Удаление
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_DELETE', '🗑️ Удалить'),
                callback_data=f'admin_tariff_delete:{tariff.id}',
            )
        ]
    )

    # Назад к списку
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_tariffs')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _format_traffic_reset_mode(mode: str | None, language: str = 'ru') -> str:
    """Форматирует режим сброса трафика для отображения."""
    mode_labels = {
        'DAY': get_texts(language).t('ADMIN_TARIFF_RESET_DAY', '📅 Ежедневно'),
        'WEEK': get_texts(language).t('ADMIN_TARIFF_RESET_WEEK', '📆 Еженедельно'),
        'MONTH': get_texts(language).t('ADMIN_TARIFF_RESET_MONTH', '🗓️ Ежемесячно'),
        'MONTH_ROLLING': get_texts(language).t('ADMIN_TARIFF_RESET_ROLLING', '🔄 Скользящий месяц'),
        'NO_RESET': get_texts(language).t('ADMIN_TARIFF_RESET_NEVER', '🚫 Никогда'),
    }
    if mode is None:
        return (
            get_texts(language)
            .t('ADMIN_TARIFF_GLOBAL_MODE', '🌐 Глобальная настройка ({v0})')
            .format(v0=settings.DEFAULT_TRAFFIC_RESET_STRATEGY)
        )
    return mode_labels.get(
        mode, get_texts(language).t('ADMIN_TARIFF_UNKNOWN_MODE', '⚠️ Неизвестно ({v0})').format(v0=mode)
    )


def _format_traffic_topup_packages(tariff: Tariff, language: str = 'ru') -> str:
    """Форматирует пакеты докупки трафика для отображения."""
    if not getattr(tariff, 'traffic_topup_enabled', False):
        return get_texts(language).t('ADMIN_TARIFF_DISABLED_STATE', '❌ Отключено')

    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    if not packages:
        return get_texts(language).t('ADMIN_TARIFF_ENABLED_NO_PACKAGES', '✅ Включено, но пакеты не настроены')

    lines = [get_texts(language).t('ADMIN_TARIFF_ENABLED_STATE', '✅ Включено')]
    for gb in sorted(packages.keys()):
        price = packages[gb]
        lines.append(
            get_texts(language)
            .t('ADMIN_TARIFF_PACKAGE_LINE', '  • {v0} ГБ: {v1}')
            .format(v0=gb, v1=format_price_kopeks(price))
        )

    return '\n'.join(lines)


def format_tariff_info(tariff: Tariff, language: str, subs_count: int = 0) -> str:
    """Форматирует информацию о тарифе."""
    get_texts(language)

    status = (
        get_texts(language).t('ADMIN_TARIFF_ACTIVE_STATE', '✅ Активен')
        if tariff.is_active
        else get_texts(language).t('ADMIN_TARIFF_INACTIVE_STATE', '❌ Неактивен')
    )
    if getattr(tariff, 'is_highlighted', False):
        status += get_texts(language).t('ADMIN_TARIFF_HIGHLIGHT_MARK', ' · ⭐ выделен')
    traffic = format_traffic(tariff.traffic_limit_gb, language=language)
    prices_display = _format_period_prices_display(
        tariff.period_prices or {}, tariff.highlight_period_days, language=language
    )

    # Форматируем список серверов
    squads_list = tariff.allowed_squads or []
    squads_display = (
        get_texts(language).t('ADMIN_TARIFF_SERVER_COUNT', '{v0} серверов').format(v0=len(squads_list))
        if squads_list
        else get_texts(language).t('ADMIN_TARIFF_ALL_SERVERS', 'Все серверы')
    )

    # Форматируем промогруппы
    promo_groups = tariff.allowed_promo_groups or []
    if promo_groups:
        promo_display = ', '.join(pg.name for pg in promo_groups)
    else:
        promo_display = get_texts(language).t('ADMIN_TARIFF_AVAILABLE_ALL', 'Доступен всем')

    trial_status = (
        get_texts(language).t('ADMIN_TARIFF_YES', '✅ Да')
        if tariff.is_trial_available
        else get_texts(language).t('ADMIN_TARIFF_NO', '❌ Нет')
    )

    # Форматируем дни триала
    trial_days = getattr(tariff, 'trial_duration_days', None)
    if trial_days:
        trial_days_display = get_texts(language).t('ADMIN_TARIFF_DAY_COUNT', '{v0} дней').format(v0=trial_days)
    else:
        trial_days_display = (
            get_texts(language)
            .t('ADMIN_TARIFF_DEFAULT_TRIAL_DAYS', 'По умолчанию ({v0} дней)')
            .format(v0=settings.TRIAL_DURATION_DAYS)
        )

    # Форматируем цену за устройство
    device_price = getattr(tariff, 'device_price_kopeks', None)
    if device_price is not None and device_price > 0:
        device_price_display = format_price_kopeks(device_price) + get_texts(language).t(
            'ADMIN_TARIFF_PER_MONTH', '/мес'
        )
    else:
        device_price_display = get_texts(language).t('ADMIN_TARIFF_UNAVAILABLE', 'Недоступно')

    # Форматируем макс. устройств
    max_devices = getattr(tariff, 'max_device_limit', None)
    if max_devices is not None and max_devices > 0:
        max_devices_display = str(max_devices)
    else:
        max_devices_display = get_texts(language).t('ADMIN_TARIFF_UNLIMITED', '∞ (без лимита)')

    # Форматируем произвольный трафик и докупку трафика
    custom_traffic_display = format_custom_traffic_settings(tariff, language=language)
    custom_days_display = format_custom_days_settings(tariff, language=language)
    traffic_topup_display = _format_traffic_topup_packages(tariff, language=language)
    server_limits_display = format_server_limits_summary(tariff, language=language)
    panel_settings_display = format_panel_settings(tariff, language=language)

    # Форматируем режим сброса трафика
    traffic_reset_mode = getattr(tariff, 'traffic_reset_mode', None)
    traffic_reset_display = _format_traffic_reset_mode(traffic_reset_mode, language=language)

    # Форматируем суточный тариф
    is_daily = getattr(tariff, 'is_daily', False)
    daily_price_kopeks = getattr(tariff, 'daily_price_kopeks', 0)

    # Формируем блок цен в зависимости от типа тарифа
    if is_daily:
        price_block = (
            get_texts(language)
            .t('ADMIN_TARIFF_DAILY_PRICE_BLOCK', '<b>💰 Суточная цена:</b> {v0}/день')
            .format(v0=format_price_kopeks(daily_price_kopeks))
        )
        tariff_type = get_texts(language).t('ADMIN_TARIFF_DAILY_TYPE', '🔄 Суточный')
    else:
        price_block = (
            get_texts(language).t('ADMIN_TARIFF_PERIOD_PRICE_BLOCK', '<b>Цены:</b>\n{v0}').format(v0=prices_display)
        )
        tariff_type = get_texts(language).t('ADMIN_TARIFF_PERIOD_TYPE', '📅 Периодный')

    return (
        get_texts(language)
        .t(
            'ADMIN_TARIFF_DETAIL',
            '📦 <b>Тариф: {v0}</b>\n\n{v1} | {v2}\n🎚️ Уровень: {v3}\n📊 Порядок: {v4}\n\n<b>Параметры:</b>\n• Трафик: {v5}\n• Устройств: {v6}\n• Макс. устройств: {v7}\n• Цена за доп. устройство: {v8}\n• Триал: {v9}\n• Дней триала: {v10}\n\n<b>Произвольный трафик:</b>\n{v11}\n\n<b>Произвольные дни:</b>\n{v12}\n\n<b>Докупка трафика:</b>\n{v13}\n\n<b>Сброс трафика:</b> {v14}\n\n{v15}\n\n<b>Серверы:</b> {v16}\n<b>Лимиты по серверам:</b> {v17}\n<b>Промогруппы:</b> {v18}\n\n<b>Панель и прочее:</b>\n{v19}\n\n📊 Подписок на тарифе: {v20}\n\n{v21}',
        )
        .format(
            v0=html.escape(tariff.name),
            v1=status,
            v2=tariff_type,
            v3=tariff.tier_level,
            v4=tariff.display_order,
            v5=traffic,
            v6=Texts.format_device_limit(tariff.device_limit),
            v7=max_devices_display,
            v8=device_price_display,
            v9=trial_status,
            v10=trial_days_display,
            v11=custom_traffic_display,
            v12=custom_days_display,
            v13=traffic_topup_display,
            v14=traffic_reset_display,
            v15=price_block,
            v16=squads_display,
            v17=server_limits_display,
            v18=promo_display,
            v19=panel_settings_display,
            v20=subs_count,
            v21=f'📝 {html.escape(tariff.description)}' if tariff.description else '',
        )
    )


@admin_required
@error_handler
async def show_tariffs_list(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Показывает список тарифов."""
    await state.clear()
    texts = get_texts(db_user.language)

    # Проверяем режим продаж
    if not settings.is_tariffs_mode():
        await callback.message.edit_text(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_MODE_DISABLED',
                '⚠️ <b>Режим тарифов отключен</b>\n\nДля использования тарифов установите:\n<code>SALES_MODE=tariffs</code>\n\nТекущий режим: <code>classic</code>',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')]]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    tariffs_data = await get_tariffs_with_subscriptions_count(db, include_inactive=True)

    if not tariffs_data:
        await callback.message.edit_text(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_EMPTY_LIST',
                '📦 <b>Тарифы</b>\n\nТарифы ещё не созданы.\nСоздайте первый тариф для начала работы.',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_TARIFF_BUTTON_CREATE', '➕ Создать тариф'),
                            callback_data='admin_tariff_create',
                        )
                    ],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
                ]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    total_pages = (len(tariffs_data) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    page_data = tariffs_data[:ITEMS_PER_PAGE]

    total_subs = sum(count for _, count in tariffs_data)
    active_count = sum(1 for t, _ in tariffs_data if t.is_active)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_LIST',
            '📦 <b>Тарифы</b>\n\nВсего: {v0} (активных: {v1})\nПодписок на тарифах: {v2}\n\nВыберите тариф для просмотра и редактирования:',
        )
        .format(v0=len(tariffs_data), v1=active_count, v2=total_subs),
        reply_markup=get_tariffs_list_keyboard(page_data, db_user.language, 0, total_pages),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_tariffs_page(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Показывает страницу списка тарифов."""
    get_texts(db_user.language)
    page = int(callback.data.split(':')[1])

    tariffs_data = await get_tariffs_with_subscriptions_count(db, include_inactive=True)
    total_pages = (len(tariffs_data) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_data = tariffs_data[start_idx:end_idx]

    total_subs = sum(count for _, count in tariffs_data)
    active_count = sum(1 for t, _ in tariffs_data if t.is_active)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_LIST_PAGE',
            '📦 <b>Тарифы</b> (стр. {v0}/{v1})\n\nВсего: {v2} (активных: {v3})\nПодписок на тарифах: {v4}\n\nВыберите тариф для просмотра и редактирования:',
        )
        .format(v0=page + 1, v1=total_pages, v2=len(tariffs_data), v3=active_count, v4=total_subs),
        reply_markup=get_tariffs_list_keyboard(page_data, db_user.language, page, total_pages),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def view_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Просмотр тарифа."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await callback.message.edit_text(
        format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_tariff_highlight(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Отмечает тариф как выгодный — в списке тарифов он показывается с подписью."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    tariff = await update_tariff(db, tariff, is_highlighted=not getattr(tariff, 'is_highlighted', False))
    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await callback.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_HIGHLIGHTED', 'Тариф выделен')
        if tariff.is_highlighted
        else get_texts(db_user.language).t('ADMIN_TARIFF_UNHIGHLIGHTED', 'Выделение снято'),
        show_alert=True,
    )
    await callback.message.edit_text(
        format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def toggle_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Переключает активность тарифа."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    tariff = await update_tariff(db, tariff, is_active=not tariff.is_active)
    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    status = (
        get_texts(db_user.language).t('ADMIN_TARIFF_ACTIVATED', 'активирован')
        if tariff.is_active
        else get_texts(db_user.language).t('ADMIN_TARIFF_DEACTIVATED', 'деактивирован')
    )
    await callback.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_STATUS_CHANGED', 'Тариф {v0}').format(v0=status), show_alert=True
    )

    await callback.message.edit_text(
        format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def toggle_trial_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Переключает тариф как триальный."""
    from app.database.crud.tariff import clear_trial_tariff, set_trial_tariff

    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    if tariff.is_trial_available:
        # Снимаем флаг триала
        await clear_trial_tariff(db)
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_TRIAL_REMOVED', 'Триал снят с тарифа'), show_alert=True
        )
    else:
        # Устанавливаем этот тариф как триальный (снимает флаг с других)
        await set_trial_tariff(db, tariff_id)
        await callback.answer(
            get_texts(db_user.language)
            .t('ADMIN_TARIFF_TRIAL_ASSIGNED', 'Тариф «{v0}» установлен как триальный')
            .format(v0=tariff.name),
            show_alert=True,
        )

    # Перезагружаем тариф
    tariff = await get_tariff_by_id(db, tariff_id)
    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await callback.message.edit_text(
        format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def toggle_daily_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Переключает суточный режим тарифа."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    is_daily = getattr(tariff, 'is_daily', False)

    if is_daily:
        # Отключаем суточный режим
        tariff = await update_tariff(db, tariff, is_daily=False, daily_price_kopeks=0)
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_DAILY_DISABLED', 'Суточный режим отключен'), show_alert=True
        )
    else:
        # Включаем суточный режим (с ценой по умолчанию)
        tariff = await update_tariff(db, tariff, is_daily=True, daily_price_kopeks=5000)  # 50 руб по умолчанию
        await callback.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_DAILY_ENABLED_DEFAULT',
                'Суточный режим включен. Цена: 50 ₽/день\nНастройте цену через кнопку «💰 Суточная цена»',
            ),
            show_alert=True,
        )

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await callback.message.edit_text(
        format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_daily_price(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование суточной цены."""
    texts = get_texts(db_user.language)

    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    current_price = getattr(tariff, 'daily_price_kopeks', 0)
    current_price / 100 if current_price else 0

    await state.set_state(AdminStates.editing_tariff_daily_price)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_DAILY_PRICE',
            '💰 <b>Редактирование суточной цены</b>\n\nТариф: {v0}\nТекущая цена: {v1}/день\n\nВведите новую цену за день в рублях.\nПример: <code>50</code> или <code>99.90</code>',
        )
        .format(v0=html.escape(tariff.name), v1=format_price_kopeks(current_price)),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_daily_price_input(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает ввод суточной цены (создание и редактирование)."""
    get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    # Парсим цену
    try:
        price_rubles = float(message.text.strip().replace(',', '.'))
        if price_rubles <= 0:
            raise ValueError(
                get_texts(db_user.language).t('ADMIN_TARIFF_POSITIVE_PRICE', 'Цена должна быть положительной')
            )

        price_kopeks = int(price_rubles * 100)
    except ValueError:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_INVALID_DAILY_PRICE',
                '❌ Некорректная цена. Введите положительное число.\nПример: <code>50</code> или <code>99.90</code>',
            ),
            parse_mode='HTML',
        )
        return

    # Проверяем - это создание или редактирование
    is_creating = data.get('tariff_is_daily') and not tariff_id

    if is_creating:
        # Создаем новый суточный тариф
        tariff = await create_tariff(
            db,
            name=data['tariff_name'],
            traffic_limit_gb=data['tariff_traffic'],
            device_limit=data['tariff_devices'],
            tier_level=data['tariff_tier'],
            period_prices={},
            is_active=True,
            is_daily=True,
            daily_price_kopeks=price_kopeks,
        )
        await state.clear()

        await message.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_DAILY_CREATED', '✅ <b>Суточный тариф создан!</b>\n\n')
            + format_tariff_info(tariff, db_user.language, 0),
            reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
            parse_mode='HTML',
        )
    else:
        # Редактируем существующий тариф
        if not tariff_id:
            await state.clear()
            return

        tariff = await get_tariff_by_id(db, tariff_id)
        if not tariff:
            await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
            await state.clear()
            return

        tariff = await update_tariff(db, tariff, daily_price_kopeks=price_kopeks)
        await state.clear()

        subs_count = await get_tariff_subscriptions_count(db, tariff_id)

        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_TARIFF_DAILY_PRICE_UPDATED', '✅ Суточная цена установлена: {v0}/день\n\n')
            .format(v0=format_price_kopeks(price_kopeks))
            + format_tariff_info(tariff, db_user.language, subs_count),
            reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
            parse_mode='HTML',
        )


# ============ СОЗДАНИЕ ТАРИФА ============


@admin_required
@error_handler
async def start_create_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает создание тарифа."""
    texts = get_texts(db_user.language)

    await state.set_state(AdminStates.creating_tariff_name)
    await state.update_data(language=db_user.language)

    await callback.message.edit_text(
        get_texts(db_user.language).t(
            'ADMIN_TARIFF_CREATE_START',
            '📦 <b>Создание тарифа</b>\n\nШаг 1/6: Введите название тарифа\n\nПример: <i>Базовый</i>, <i>Премиум</i>, <i>Бизнес</i>',
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_tariff_name(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает название тарифа."""
    texts = get_texts(db_user.language)
    name = message.text.strip()

    if len(name) < 2:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NAME_TOO_SHORT', 'Название должно быть не короче 2 символов')
        )
        return

    if len(name) > 50:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NAME_TOO_LONG', 'Название должно быть не длиннее 50 символов')
        )
        return

    await state.update_data(tariff_name=name)
    await state.set_state(AdminStates.creating_tariff_traffic)

    await message.answer(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_CREATE_TRAFFIC',
            '📦 <b>Создание тарифа</b>\n\nНазвание: <b>{v0}</b>\n\nШаг 2/6: Введите лимит трафика в ГБ\n\nВведите <code>0</code> для безлимитного трафика\nПример: <i>100</i>, <i>500</i>, <i>0</i>',
        )
        .format(v0=name),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def process_tariff_traffic(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает лимит трафика."""
    texts = get_texts(db_user.language)

    try:
        traffic = int(message.text.strip())
        if traffic < 0:
            raise ValueError
    except ValueError:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NONNEGATIVE_NUMBER', 'Введите корректное число (0 или больше)')
        )
        return

    data = await state.get_data()
    await state.update_data(tariff_traffic=traffic)
    await state.set_state(AdminStates.creating_tariff_devices)

    traffic_display = format_traffic(traffic, language=db_user.language)

    await message.answer(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_CREATE_DEVICES',
            '📦 <b>Создание тарифа</b>\n\nНазвание: <b>{v0}</b>\nТрафик: <b>{v1}</b>\n\nШаг 3/6: Введите лимит устройств\n\nПример: <i>1</i>, <i>3</i>, <i>5</i>',
        )
        .format(v0=data['tariff_name'], v1=traffic_display),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def process_tariff_devices(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает лимит устройств."""
    texts = get_texts(db_user.language)

    try:
        devices = int(message.text.strip())
        if devices < 1:
            raise ValueError
    except ValueError:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_POSITIVE_NUMBER', 'Введите корректное число (1 или больше)')
        )
        return

    data = await state.get_data()
    await state.update_data(tariff_devices=devices)
    await state.set_state(AdminStates.creating_tariff_tier)

    traffic_display = format_traffic(data['tariff_traffic'], language=db_user.language)

    await message.answer(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_CREATE_TIER',
            '📦 <b>Создание тарифа</b>\n\nНазвание: <b>{v0}</b>\nТрафик: <b>{v1}</b>\nУстройств: <b>{v2}</b>\n\nШаг 4/6: Введите уровень тарифа (1-10)\n\nУровень используется для визуального отображения\n1 - базовый, 10 - максимальный\nПример: <i>1</i>, <i>2</i>, <i>3</i>',
        )
        .format(v0=data['tariff_name'], v1=traffic_display, v2=devices),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def process_tariff_tier(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает уровень тарифа."""
    texts = get_texts(db_user.language)

    try:
        tier = int(message.text.strip())
        if tier < 1 or tier > 10:
            raise ValueError
    except ValueError:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_TIER_RANGE', 'Введите число от 1 до 10'))
        return

    data = await state.get_data()
    await state.update_data(tariff_tier=tier)

    traffic_display = format_traffic(data['tariff_traffic'], language=db_user.language)

    # Шаг 5/6: Выбор типа тарифа
    await message.answer(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_CREATE_TYPE',
            '📦 <b>Создание тарифа</b>\n\nНазвание: <b>{v0}</b>\nТрафик: <b>{v1}</b>\nУстройств: <b>{v2}</b>\nУровень: <b>{v3}</b>\n\nШаг 5/6: Выберите тип тарифа',
        )
        .format(v0=data['tariff_name'], v1=traffic_display, v2=data['tariff_devices'], v3=tier),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_TARIFF_PERIOD_TYPE_CHOICE', '📅 Периодный (месяцы)'),
                        callback_data='tariff_type_periodic',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=get_texts(db_user.language).t(
                            'ADMIN_TARIFF_DAILY_TYPE_CHOICE', '🔄 Суточный (оплата за день)'
                        ),
                        callback_data='tariff_type_daily',
                    )
                ],
                [InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')],
            ]
        ),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def select_tariff_type_periodic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Выбирает периодный тип тарифа."""
    texts = get_texts(db_user.language)
    data = await state.get_data()

    await state.update_data(tariff_is_daily=False)
    await state.set_state(AdminStates.creating_tariff_prices)

    traffic_display = format_traffic(data['tariff_traffic'], language=db_user.language)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_CREATE_PERIOD_PRICES',
            '📦 <b>Создание тарифа</b>\n\nНазвание: <b>{v0}</b>\nТрафик: <b>{v1}</b>\nУстройств: <b>{v2}</b>\nУровень: <b>{v3}</b>\nТип: <b>📅 Периодный</b>\n\nШаг 6/6: Введите цены на периоды\n\nФормат: <code>дней:цена_в_копейках</code>\nНесколько периодов через запятую\n\nПример:\n<code>30:9900, 90:24900, 180:44900, 360:79900</code>',
        )
        .format(v0=data['tariff_name'], v1=traffic_display, v2=data['tariff_devices'], v3=data['tariff_tier']),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def select_tariff_type_daily(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Выбирает суточный тип тарифа."""
    from app.states import AdminStates

    texts = get_texts(db_user.language)
    data = await state.get_data()

    await state.update_data(tariff_is_daily=True)
    await state.set_state(AdminStates.editing_tariff_daily_price)

    traffic_display = format_traffic(data['tariff_traffic'], language=db_user.language)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_CREATE_DAILY_PRICE',
            '📦 <b>Создание суточного тарифа</b>\n\nНазвание: <b>{v0}</b>\nТрафик: <b>{v1}</b>\nУстройств: <b>{v2}</b>\nУровень: <b>{v3}</b>\nТип: <b>🔄 Суточный</b>\n\nШаг 6/6: Введите суточную цену в рублях\n\nПример: <i>50</i> (50 ₽/день), <i>99.90</i> (99.90 ₽/день)',
        )
        .format(v0=data['tariff_name'], v1=traffic_display, v2=data['tariff_devices'], v3=data['tariff_tier']),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_tariffs')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_tariff_prices(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает цены тарифа."""
    get_texts(db_user.language)

    prices = _parse_period_prices(message.text.strip())

    if not prices:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_INVALID_CREATE_PRICES',
                'Не удалось распознать цены.\n\nФормат: <code>дней:цена_в_копейках</code>\nПример: <code>30:9900, 90:24900</code>',
            ),
            parse_mode='HTML',
        )
        return

    data = await state.get_data()
    await state.update_data(tariff_prices=prices)

    format_traffic(data['tariff_traffic'], language=db_user.language)
    _format_period_prices_display(prices, language=db_user.language)

    # Создаем тариф
    tariff = await create_tariff(
        db,
        name=data['tariff_name'],
        traffic_limit_gb=data['tariff_traffic'],
        device_limit=data['tariff_devices'],
        tier_level=data['tariff_tier'],
        period_prices=prices,
        is_active=True,
    )

    await state.clear()

    subs_count = 0

    await message.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_CREATED', '✅ <b>Тариф создан!</b>\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


# ============ РЕДАКТИРОВАНИЕ ТАРИФА ============


@admin_required
@error_handler
async def start_edit_tariff_name(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование названия тарифа."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_name)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_NAME',
            '✏️ <b>Редактирование названия</b>\n\nТекущее название: <b>{v0}</b>\n\nВведите новое название:',
        )
        .format(v0=html.escape(tariff.name)),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_name(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новое название тарифа."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    name = message.text.strip()
    if len(name) < 2 or len(name) > 50:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NAME_LENGTH', 'Название должно быть от 2 до 50 символов')
        )
        return

    tariff = await update_tariff(db, tariff, name=name)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_NAME_UPDATED', '✅ Название изменено!\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_tariff_description(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование описания тарифа."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_description)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    current_desc = tariff.description or get_texts(db_user.language).t('ADMIN_TARIFF_DESCRIPTION_NOT_SET', 'Не задано')

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_DESCRIPTION',
            '📝 <b>Редактирование описания</b>\n\nТекущее описание:\n{v0}\n\nВведите новое описание (или <code>-</code> для удаления):',
        )
        .format(v0=current_desc),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_description(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новое описание тарифа."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    description = message.text.strip()
    if description == '-':
        description = None

    tariff = await update_tariff(db, tariff, description=description)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_DESCRIPTION_UPDATED', '✅ Описание изменено!\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_tariff_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование трафика тарифа."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_traffic)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    current_traffic = format_traffic(tariff.traffic_limit_gb, language=db_user.language)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_TRAFFIC',
            '📊 <b>Редактирование трафика</b>\n\nТекущий лимит: <b>{v0}</b>\n\nВведите новый лимит в ГБ (0 = безлимит):',
        )
        .format(v0=current_traffic),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_traffic(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новый лимит трафика."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    try:
        traffic = int(message.text.strip())
        if traffic < 0:
            raise ValueError
    except ValueError:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NONNEGATIVE_NUMBER', 'Введите корректное число (0 или больше)')
        )
        return

    tariff = await update_tariff(db, tariff, traffic_limit_gb=traffic)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_TRAFFIC_UPDATED', '✅ Трафик изменен!\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_tariff_devices(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование лимита устройств."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_devices)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_DEVICES',
            '📱 <b>Редактирование устройств</b>\n\nТекущий лимит: <b>{v0}</b>\n\nВведите новый лимит устройств:',
        )
        .format(v0=tariff.device_limit),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_devices(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новый лимит устройств."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    try:
        devices = int(message.text.strip())
        if devices < 1:
            raise ValueError
    except ValueError:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_POSITIVE_NUMBER', 'Введите корректное число (1 или больше)')
        )
        return

    tariff = await update_tariff(db, tariff, device_limit=devices)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_DEVICES_UPDATED', '✅ Лимит устройств изменен!\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_tariff_tier(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование уровня тарифа."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_tier)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_TIER',
            '🎚️ <b>Редактирование уровня</b>\n\nТекущий уровень: <b>{v0}</b>\n\nВведите новый уровень (1-10):',
        )
        .format(v0=tariff.tier_level),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_tier(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новый уровень тарифа."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    try:
        tier = int(message.text.strip())
        if tier < 1 or tier > 10:
            raise ValueError
    except ValueError:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_TIER_RANGE', 'Введите число от 1 до 10'))
        return

    tariff = await update_tariff(db, tariff, tier_level=tier)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_TIER_UPDATED', '✅ Уровень изменен!\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_edit_tariff_prices(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование цен тарифа."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_prices)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    current_prices = _format_period_prices_for_edit(tariff.period_prices or {})
    prices_display = _format_period_prices_display(tariff.period_prices or {}, language=db_user.language)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_PRICES',
            '💰 <b>Редактирование цен</b>\n\nТекущие цены:\n{v0}\n\nВведите новые цены в формате:\n<code>{v1}</code>\n\n(дней:цена_в_копейках, через запятую)',
        )
        .format(v0=prices_display, v1=current_prices),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_prices(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новые цены тарифа."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    prices = _parse_period_prices(message.text.strip())
    if not prices:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_INVALID_EDIT_PRICES',
                'Не удалось распознать цены.\nФормат: <code>дней:цена</code>\nПример: <code>30:9900, 90:24900</code>',
            ),
            parse_mode='HTML',
        )
        return

    tariff = await update_tariff(db, tariff, period_prices=prices)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_PRICES_UPDATED', '✅ Цены изменены!\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


# ============ ВЫГОДНЫЙ ПЕРИОД ============


def _highlight_keyboard(tariff: Tariff, language: str) -> InlineKeyboardMarkup:
    """Список периодов тарифа: текущий выделенный отмечен звездой."""
    texts = get_texts(language)
    prices = tariff.period_prices or {}
    current = tariff.highlight_period_days
    buttons = []

    for period_str in sorted(prices.keys(), key=int):
        period = int(period_str)
        mark = '⭐ ' if current is not None and int(current) == period else ''
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{mark}{format_period(period, language=language)} — {format_price_kopeks(prices[period_str])}',
                    callback_data=f'admin_tariff_highlight_set:{tariff.id}:{period}',
                )
            ]
        )

    if current is not None:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_TARIFF_REMOVE_HIGHLIGHT_PROMPT', '🚫 Убрать выделение'),
                    callback_data=f'admin_tariff_highlight_set:{tariff.id}:0',
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff.id}')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_required
@error_handler
async def start_edit_tariff_highlight(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Показывает выбор периода, который будет отмечен как самый выгодный."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    current = tariff.highlight_period_days
    current_display = (
        format_period(int(current), language=db_user.language)
        if current is not None
        else get_texts(db_user.language).t('ADMIN_TARIFF_PERIOD_NOT_SELECTED', 'не выбран')
    )

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_HIGHLIGHT',
            '⭐ <b>Выгодный период</b>\n\nСейчас выделен: <b>{v0}</b>\n\nВыделенный период показывается клиенту с отметкой при покупке, продлении и смене тарифа. Выделить можно только один период.',
        )
        .format(v0=current_display),
        reply_markup=_highlight_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def set_tariff_highlight(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Сохраняет выделенный период. 0 — снять выделение."""
    _, tariff_id_raw, period_raw = callback.data.split(':')
    tariff = await get_tariff_by_id(db, int(tariff_id_raw))

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    period = int(period_raw)
    # Повторное нажатие на уже выделенный период снимает выделение: иначе снять
    # его можно было бы только отдельной кнопкой, которой легко не заметить.
    if period == 0 or (tariff.highlight_period_days is not None and int(tariff.highlight_period_days) == period):
        new_value = None
    else:
        new_value = period

    tariff = await update_tariff(db, tariff, highlight_period_days=new_value)
    subs_count = await get_tariff_subscriptions_count(db, tariff.id)

    await callback.message.edit_text(
        (
            get_texts(db_user.language).t('ADMIN_TARIFF_HIGHLIGHT_REMOVED', '✅ Выделение снято!')
            if new_value is None
            else get_texts(db_user.language)
            .t('ADMIN_TARIFF_PERIOD_HIGHLIGHTED', '✅ Выделен период: {v0}')
            .format(v0=format_period(new_value, language=db_user.language))
        )
        + '\n\n'
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


# ============ РЕДАКТИРОВАНИЕ ЦЕНЫ ЗА УСТРОЙСТВО ============


@admin_required
@error_handler
async def start_edit_tariff_device_price(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование цены за устройство."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_device_price)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    device_price = getattr(tariff, 'device_price_kopeks', None)
    if device_price is not None and device_price > 0:
        current_price = format_price_kopeks(device_price) + get_texts(db_user.language).t(
            'ADMIN_TARIFF_PER_MONTH', '/мес'
        )
    else:
        current_price = get_texts(db_user.language).t(
            'ADMIN_TARIFF_DEVICES_UNAVAILABLE', 'Недоступно (докупка устройств запрещена)'
        )

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_DEVICE_PRICE',
            '📱💰 <b>Редактирование цены за устройство</b>\n\nТекущая цена: <b>{v0}</b>\n\nВведите цену в копейках за одно устройство в месяц.\n\n• <code>0</code> или <code>-</code> — докупка устройств недоступна\n• Например: <code>5000</code> = 50₽/мес за устройство',
        )
        .format(v0=current_price),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_device_price(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новую цену за устройство."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    text = message.text.strip()

    if text == '-' or text == '0':
        device_price = None
    else:
        try:
            device_price = int(text)
            if device_price < 0:
                raise ValueError
        except ValueError:
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_TARIFF_INVALID_DEVICE_PRICE',
                    'Введите корректное число (0 или больше).\nДля отключения докупки введите <code>0</code> или <code>-</code>',
                ),
                parse_mode='HTML',
            )
            return

    tariff = await update_tariff(db, tariff, device_price_kopeks=device_price)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_DEVICE_PRICE_UPDATED', '✅ Цена за устройство изменена!\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


# ============ РЕДАКТИРОВАНИЕ МАКС. УСТРОЙСТВ ============


@admin_required
@error_handler
async def start_edit_tariff_max_devices(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование макс. устройств."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_max_devices)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    max_devices = getattr(tariff, 'max_device_limit', None)
    if max_devices is not None and max_devices > 0:
        current_max = str(max_devices)
    else:
        current_max = get_texts(db_user.language).t('ADMIN_TARIFF_UNLIMITED', '∞ (без лимита)')

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_MAX_DEVICES',
            '📱🔒 <b>Редактирование макс. устройств</b>\n\nТекущее значение: <b>{v0}</b>\nБазовое кол-во устройств: <b>{v1}</b>\n\nВведите максимальное количество устройств, которое пользователь может докупить.\n\n• <code>0</code> или <code>-</code> — без ограничений\n• Например: <code>5</code> = максимум 5 устройств на тарифе',
        )
        .format(v0=current_max, v1=tariff.device_limit),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_max_devices(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новое макс. кол-во устройств."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    text = message.text.strip()

    if text == '-' or text == '0':
        max_devices = None
    else:
        try:
            max_devices = int(text)
            if max_devices < 1:
                raise ValueError
        except ValueError:
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_TARIFF_INVALID_MAX_DEVICES',
                    'Введите корректное число (1 или больше).\nДля снятия ограничения введите <code>0</code> или <code>-</code>',
                ),
                parse_mode='HTML',
            )
            return

    tariff = await update_tariff(db, tariff, max_device_limit=max_devices)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_MAX_DEVICES_UPDATED', '✅ Макс. устройств изменено!\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


# ============ РЕДАКТИРОВАНИЕ ДНЕЙ ТРИАЛА ============


@admin_required
@error_handler
async def start_edit_tariff_trial_days(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование дней триала."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_trial_days)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    trial_days = getattr(tariff, 'trial_duration_days', None)
    if trial_days:
        current_days = get_texts(db_user.language).t('ADMIN_TARIFF_DAY_COUNT', '{v0} дней').format(v0=trial_days)
    else:
        current_days = (
            get_texts(db_user.language)
            .t('ADMIN_TARIFF_DEFAULT_TRIAL_DAYS', 'По умолчанию ({v0} дней)')
            .format(v0=settings.TRIAL_DURATION_DAYS)
        )

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_TRIAL_DAYS',
            '⏰ <b>Редактирование дней триала</b>\n\nТекущее значение: <b>{v0}</b>\n\nВведите количество дней триала.\n\n• <code>0</code> или <code>-</code> — использовать настройку по умолчанию ({v1} дней)\n• Например: <code>7</code> = 7 дней триала',
        )
        .format(v0=current_days, v1=settings.TRIAL_DURATION_DAYS),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_tariff_trial_days(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новое количество дней триала."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    text = message.text.strip()

    if text == '-' or text == '0':
        trial_days = None
    else:
        try:
            trial_days = int(text)
            if trial_days < 1:
                raise ValueError
        except ValueError:
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_TARIFF_INVALID_TRIAL_DAYS',
                    'Введите корректное число дней (1 или больше).\nДля использования настройки по умолчанию введите <code>0</code> или <code>-</code>',
                ),
                parse_mode='HTML',
            )
            return

    tariff = await update_tariff(db, tariff, trial_duration_days=trial_days)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_TRIAL_DAYS_UPDATED', '✅ Дни триала изменены!\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


# ============ РЕДАКТИРОВАНИЕ ДОКУПКИ ТРАФИКА ============


def _parse_traffic_topup_packages(text: str) -> dict[int, int]:
    """
    Парсит строку с пакетами докупки трафика.
    Формат: "5:5000, 10:9000, 20:15000" (ГБ:цена_в_копейках)
    """
    packages = {}
    text = text.replace(';', ',').replace('=', ':')

    for part in text.split(','):
        part = part.strip()
        if not part:
            continue

        if ':' not in part:
            continue

        gb_str, price_str = part.split(':', 1)
        try:
            gb = int(gb_str.strip())
            price = int(price_str.strip())
            if gb > 0 and price > 0:
                packages[gb] = price
        except ValueError:
            continue

    return packages


def _format_traffic_topup_packages_for_edit(packages: dict[int, int]) -> str:
    """Форматирует пакеты докупки для редактирования."""
    if not packages:
        return '5:5000, 10:9000, 20:15000'

    parts = []
    for gb in sorted(packages.keys()):
        parts.append(f'{gb}:{packages[gb]}')

    return ', '.join(parts)


@admin_required
@error_handler
async def start_edit_tariff_traffic_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Показывает меню настройки докупки трафика."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    # Проверяем, безлимитный ли тариф
    if tariff.is_unlimited_traffic:
        await callback.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_TOPUP_UNLIMITED_UNAVAILABLE', 'Докупка недоступна для безлимитного тарифа'
            ),
            show_alert=True,
        )
        return

    is_enabled = getattr(tariff, 'traffic_topup_enabled', False)
    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    max_topup_traffic = getattr(tariff, 'max_topup_traffic_gb', 0) or 0

    # Форматируем текущие настройки
    if is_enabled:
        status = get_texts(db_user.language).t('ADMIN_TARIFF_ENABLED_STATE', '✅ Включено')
        if packages:
            packages_display = '\n'.join(
                get_texts(db_user.language)
                .t('ADMIN_TARIFF_PACKAGE_LINE', '  • {v0} ГБ: {v1}')
                .format(v0=gb, v1=format_price_kopeks(price))
                for gb, price in sorted(packages.items())
            )
        else:
            packages_display = get_texts(db_user.language).t(
                'ADMIN_TARIFF_PACKAGES_NOT_CONFIGURED', '  Пакеты не настроены'
            )
    else:
        status = get_texts(db_user.language).t('ADMIN_TARIFF_DISABLED_STATE', '❌ Отключено')
        packages_display = '  -'

    # Форматируем лимит
    if max_topup_traffic > 0:
        max_limit_display = (
            get_texts(db_user.language).t('ADMIN_TARIFF_TRAFFIC_VALUE', '{v0} ГБ').format(v0=max_topup_traffic)
        )
    else:
        max_limit_display = get_texts(db_user.language).t('ADMIN_TARIFF_NO_LIMIT', 'Без ограничений')

    buttons = []

    # Переключение вкл/выкл
    if is_enabled:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_TARIFF_DISABLE', '❌ Отключить'),
                    callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
                )
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_TARIFF_ENABLE', '✅ Включить'),
                    callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
                )
            ]
        )

    # Редактирование пакетов и лимита (только если включено)
    if is_enabled:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_TARIFF_CONFIGURE_PACKAGES', '📦 Настроить пакеты'),
                    callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}',
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_TARIFF_MAX_TRAFFIC', '📊 Макс. лимит трафика'),
                    callback_data=f'admin_tariff_edit_max_topup:{tariff_id}',
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_TOPUP_DETAIL',
            '📈 <b>Докупка трафика для «{v0}»</b>\n\nСтатус: {v1}\n\n<b>Пакеты:</b>\n{v2}\n\n<b>Макс. лимит:</b> {v3}\n\nПользователи смогут докупать трафик по заданным ценам.',
        )
        .format(v0=html.escape(tariff.name), v1=status, v2=packages_display, v3=max_limit_display),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_tariff_traffic_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Переключает включение/выключение докупки трафика."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    is_enabled = getattr(tariff, 'traffic_topup_enabled', False)
    new_value = not is_enabled

    tariff = await update_tariff(db, tariff, traffic_topup_enabled=new_value)

    status_text = (
        get_texts(db_user.language).t('ADMIN_TARIFF_TOPUP_ENABLED', 'включена')
        if new_value
        else get_texts(db_user.language).t('ADMIN_TARIFF_TOPUP_DISABLED', 'отключена')
    )
    await callback.answer(
        get_texts(db_user.language)
        .t('ADMIN_TARIFF_TOPUP_STATUS_CHANGED', 'Докупка трафика {v0}')
        .format(v0=status_text)
    )

    # Перерисовываем меню
    texts = get_texts(db_user.language)
    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    max_topup_traffic = getattr(tariff, 'max_topup_traffic_gb', 0) or 0

    if new_value:
        status = get_texts(db_user.language).t('ADMIN_TARIFF_ENABLED_STATE', '✅ Включено')
        if packages:
            packages_display = '\n'.join(
                get_texts(db_user.language)
                .t('ADMIN_TARIFF_PACKAGE_LINE', '  • {v0} ГБ: {v1}')
                .format(v0=gb, v1=format_price_kopeks(price))
                for gb, price in sorted(packages.items())
            )
        else:
            packages_display = get_texts(db_user.language).t(
                'ADMIN_TARIFF_PACKAGES_NOT_CONFIGURED', '  Пакеты не настроены'
            )
    else:
        status = get_texts(db_user.language).t('ADMIN_TARIFF_DISABLED_STATE', '❌ Отключено')
        packages_display = '  -'

    # Форматируем лимит
    if max_topup_traffic > 0:
        max_limit_display = (
            get_texts(db_user.language).t('ADMIN_TARIFF_TRAFFIC_VALUE', '{v0} ГБ').format(v0=max_topup_traffic)
        )
    else:
        max_limit_display = get_texts(db_user.language).t('ADMIN_TARIFF_NO_LIMIT', 'Без ограничений')

    buttons = []

    if new_value:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_TARIFF_DISABLE', '❌ Отключить'),
                    callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_TARIFF_CONFIGURE_PACKAGES', '📦 Настроить пакеты'),
                    callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}',
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_TARIFF_MAX_TRAFFIC', '📊 Макс. лимит трафика'),
                    callback_data=f'admin_tariff_edit_max_topup:{tariff_id}',
                )
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_TARIFF_ENABLE', '✅ Включить'),
                    callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_TOPUP_DETAIL',
                '📈 <b>Докупка трафика для «{v0}»</b>\n\nСтатус: {v1}\n\n<b>Пакеты:</b>\n{v2}\n\n<b>Макс. лимит:</b> {v3}\n\nПользователи смогут докупать трафик по заданным ценам.',
            )
            .format(v0=html.escape(tariff.name), v1=status, v2=packages_display, v3=max_limit_display),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass


@admin_required
@error_handler
async def start_edit_traffic_topup_packages(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование пакетов докупки трафика."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_traffic_topup_packages)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    current_packages = _format_traffic_topup_packages_for_edit(packages)

    if packages:
        packages_display = '\n'.join(
            get_texts(db_user.language)
            .t('ADMIN_TARIFF_PACKAGE_LINE', '  • {v0} ГБ: {v1}')
            .format(v0=gb, v1=format_price_kopeks(price))
            for gb, price in sorted(packages.items())
        )
    else:
        packages_display = get_texts(db_user.language).t('ADMIN_TARIFF_NOT_CONFIGURED', '  Не настроены')

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_PACKAGES',
            '📦 <b>Настройка пакетов докупки трафика</b>\n\nТариф: <b>{v0}</b>\n\n<b>Текущие пакеты:</b>\n{v1}\n\nВведите пакеты в формате:\n<code>{v2}</code>\n\n(ГБ:цена_в_копейках, через запятую)\nНапример: <code>5:5000, 10:9000</code> = 5ГБ за 50₽, 10ГБ за 90₽',
        )
        .format(v0=html.escape(tariff.name), v1=packages_display, v2=current_packages),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_edit_traffic_topup:{tariff_id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_traffic_topup_packages(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новые пакеты докупки трафика."""
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    if not message.text:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_PACKAGES_TEXT_REQUIRED',
                'Пожалуйста, отправьте текстовое сообщение.\n\nФормат: <code>ГБ:цена_в_копейках</code>\nПример: <code>5:5000, 10:9000, 20:15000</code>',
            ),
            parse_mode='HTML',
        )
        return

    packages = _parse_traffic_topup_packages(message.text.strip())

    if not packages:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_INVALID_PACKAGES',
                'Не удалось распознать пакеты.\n\nФормат: <code>ГБ:цена_в_копейках</code>\nПример: <code>5:5000, 10:9000, 20:15000</code>',
            ),
            parse_mode='HTML',
        )
        return

    # Преобразуем в формат для JSON (строковые ключи)
    packages_json = {str(gb): price for gb, price in packages.items()}

    tariff = await update_tariff(db, tariff, traffic_topup_packages=packages_json)
    await state.clear()

    # Показываем обновленное меню
    texts = get_texts(db_user.language)
    packages_display = '\n'.join(
        get_texts(db_user.language)
        .t('ADMIN_TARIFF_PACKAGE_LINE', '  • {v0} ГБ: {v1}')
        .format(v0=gb, v1=format_price_kopeks(price))
        for gb, price in sorted(packages.items())
    )
    max_topup_traffic = getattr(tariff, 'max_topup_traffic_gb', 0) or 0
    max_limit_display = (
        get_texts(db_user.language).t('ADMIN_TARIFF_TRAFFIC_VALUE', '{v0} ГБ').format(v0=max_topup_traffic)
        if max_topup_traffic > 0
        else get_texts(db_user.language).t('ADMIN_TARIFF_NO_LIMIT', 'Без ограничений')
    )

    buttons = [
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_DISABLE', '❌ Отключить'),
                callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_CONFIGURE_PACKAGES', '📦 Настроить пакеты'),
                callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_MAX_TRAFFIC', '📊 Макс. лимит трафика'),
                callback_data=f'admin_tariff_edit_max_topup:{tariff_id}',
            )
        ],
        [InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')],
    ]

    await message.answer(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_PACKAGES_UPDATED',
            '✅ <b>Пакеты обновлены!</b>\n\n📈 <b>Докупка трафика для «{v0}»</b>\n\nСтатус: ✅ Включено\n\n<b>Пакеты:</b>\n{v1}\n\n<b>Макс. лимит:</b> {v2}\n\nПользователи смогут докупать трафик по заданным ценам.',
        )
        .format(v0=html.escape(tariff.name), v1=packages_display, v2=max_limit_display),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )


# ============ МАКСИМАЛЬНЫЙ ЛИМИТ ДОКУПКИ ТРАФИКА ============


@admin_required
@error_handler
async def start_edit_max_topup_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начинает редактирование максимального лимита докупки трафика."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await state.set_state(AdminStates.editing_tariff_max_topup_traffic)
    await state.update_data(tariff_id=tariff_id)

    current_limit = getattr(tariff, 'max_topup_traffic_gb', 0) or 0
    if current_limit > 0:
        current_display = (
            get_texts(db_user.language).t('ADMIN_TARIFF_TRAFFIC_VALUE', '{v0} ГБ').format(v0=current_limit)
        )
    else:
        current_display = get_texts(db_user.language).t('ADMIN_TARIFF_NO_LIMIT', 'Без ограничений')

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_MAX_TRAFFIC',
            '📊 <b>Максимальный лимит трафика</b>\n\nТариф: <b>{v0}</b>\nТекущий лимит: <b>{v1}</b>\n\nВведите максимальный общий объем трафика (в ГБ), который может быть на подписке после всех докупок.\n\n• Например, если тариф дает 100 ГБ и лимит 200 ГБ — пользователь сможет докупить еще 100 ГБ\n• Введите <code>0</code> для снятия ограничения',
        )
        .format(v0=html.escape(tariff.name), v1=current_display),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_edit_traffic_topup:{tariff_id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_max_topup_traffic(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обрабатывает новое значение максимального лимита докупки трафика."""
    texts = get_texts(db_user.language)
    state_data = await state.get_data()
    tariff_id = state_data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    # Парсим значение
    text = message.text.strip()
    try:
        new_limit = int(text)
        if new_limit < 0:
            raise ValueError('Negative value')
    except ValueError:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_INVALID_MAX_TRAFFIC',
                'Введите целое число (0 или больше).\n\n• <code>0</code> — без ограничений\n• <code>200</code> — максимум 200 ГБ на подписке',
            ),
            parse_mode='HTML',
        )
        return

    tariff = await update_tariff(db, tariff, max_topup_traffic_gb=new_limit)
    await state.clear()

    # Показываем обновленное меню
    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    if packages:
        packages_display = '\n'.join(
            get_texts(db_user.language)
            .t('ADMIN_TARIFF_PACKAGE_LINE', '  • {v0} ГБ: {v1}')
            .format(v0=gb, v1=format_price_kopeks(price))
            for gb, price in sorted(packages.items())
        )
    else:
        packages_display = get_texts(db_user.language).t(
            'ADMIN_TARIFF_PACKAGES_NOT_CONFIGURED', '  Пакеты не настроены'
        )

    max_limit_display = (
        get_texts(db_user.language).t('ADMIN_TARIFF_TRAFFIC_VALUE', '{v0} ГБ').format(v0=new_limit)
        if new_limit > 0
        else get_texts(db_user.language).t('ADMIN_TARIFF_NO_LIMIT', 'Без ограничений')
    )

    buttons = [
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_DISABLE', '❌ Отключить'),
                callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_CONFIGURE_PACKAGES', '📦 Настроить пакеты'),
                callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_MAX_TRAFFIC', '📊 Макс. лимит трафика'),
                callback_data=f'admin_tariff_edit_max_topup:{tariff_id}',
            )
        ],
        [InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')],
    ]

    await message.answer(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_MAX_TRAFFIC_UPDATED',
            '✅ <b>Лимит обновлен!</b>\n\n📈 <b>Докупка трафика для «{v0}»</b>\n\nСтатус: ✅ Включено\n\n<b>Пакеты:</b>\n{v1}\n\n<b>Макс. лимит:</b> {v2}\n\nПользователи смогут докупать трафик по заданным ценам.',
        )
        .format(v0=html.escape(tariff.name), v1=packages_display, v2=max_limit_display),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )


# ============ УДАЛЕНИЕ ТАРИФА ============


@admin_required
@error_handler
async def confirm_delete_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Запрашивает подтверждение удаления тарифа."""
    get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    active_count = await get_active_subscriptions_count_by_tariff_id(db, tariff_id)

    if active_count > 0:
        total_count = await get_tariff_subscriptions_count(db, tariff_id)
        await callback.message.edit_text(
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_DELETE_ACTIVE_BLOCKED',
                '🗑️ <b>Удаление тарифа</b>\n\nНевозможно удалить тариф <b>{v0}</b>.\n\n⚠️ <b>Активных подписок:</b> {v1} (всего: {v2})\nСначала деактивируйте тариф и дождитесь окончания всех активных подписок, либо переведите подписки на другой тариф.',
            )
            .format(v0=html.escape(tariff.name), v1=active_count, v2=total_count),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_TARIFF_BACK_TO_TARIFF', '◀️ Назад к тарифу'),
                            callback_data=f'admin_tariff_view:{tariff_id}',
                        )
                    ],
                ]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    warning = ''
    if subs_count > 0:
        warning = (
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_DELETE_INACTIVE_WARNING',
                '\n\n⚠️ <b>Внимание!</b> На этом тарифе {v0} неактивных подписок.\nОни потеряют привязку к тарифу.',
            )
            .format(v0=subs_count)
        )

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_DELETE_CONFIRM_PROMPT',
            '🗑️ <b>Удаление тарифа</b>\n\nВы действительно хотите удалить тариф <b>{v0}</b>?{v1}',
        )
        .format(v0=html.escape(tariff.name), v1=warning),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_TARIFF_DELETE_CONFIRM', '✅ Да, удалить'),
                        callback_data=f'admin_tariff_delete_confirm:{tariff_id}',
                    ),
                    InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_TARIFF_CANCEL', '❌ Отмена'),
                        callback_data=f'admin_tariff_view:{tariff_id}',
                    ),
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_tariff_confirmed(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Удаляет тариф после подтверждения."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    # Защита от удаления тарифа с активными подписками (FK RESTRICT)
    active_count = await get_active_subscriptions_count_by_tariff_id(db, tariff.id)
    if active_count > 0:
        await callback.answer(
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_DELETE_ACTIVE_ALERT',
                'Невозможно удалить тариф: {v0} активных подписок. Сначала деактивируйте тариф.',
            )
            .format(v0=active_count),
            show_alert=True,
        )
        return

    tariff_name = tariff.name
    await delete_tariff(db, tariff)

    await callback.answer(
        get_texts(db_user.language).t('ADMIN_TARIFF_DELETED', 'Тариф «{v0}» удален').format(v0=tariff_name),
        show_alert=True,
    )

    # Возвращаемся к списку
    tariffs_data = await get_tariffs_with_subscriptions_count(db, include_inactive=True)

    if not tariffs_data:
        await callback.message.edit_text(
            get_texts(db_user.language).t(
                'ADMIN_TARIFF_EMPTY_LIST_SHORT', '📦 <b>Тарифы</b>\n\nТарифы ещё не созданы.'
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_TARIFF_BUTTON_CREATE', '➕ Создать тариф'),
                            callback_data='admin_tariff_create',
                        )
                    ],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
                ]
            ),
            parse_mode='HTML',
        )
        return

    total_pages = (len(tariffs_data) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    page_data = tariffs_data[:ITEMS_PER_PAGE]

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t('ADMIN_TARIFF_DELETED_LIST', '📦 <b>Тарифы</b>\n\n✅ Тариф «{v0}» удален\n\nВсего: {v1}')
        .format(v0=tariff_name, v1=len(tariffs_data)),
        reply_markup=get_tariffs_list_keyboard(page_data, db_user.language, 0, total_pages),
        parse_mode='HTML',
    )


# ============ РЕДАКТИРОВАНИЕ СЕРВЕРОВ ============


@admin_required
@error_handler
async def start_edit_tariff_squads(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Показывает меню выбора серверов для тарифа."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    squads, _ = await get_all_server_squads(db, limit=10000)

    if not squads:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NO_SERVERS', 'Нет доступных серверов'), show_alert=True
        )
        return

    current_squads = set(tariff.allowed_squads or [])

    buttons = []
    for squad in squads:
        is_selected = squad.squad_uuid in current_squads
        prefix = '✅' if is_selected else '⬜'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {squad.display_name}',
                    callback_data=f'trf_sq:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_squads:{tariff_id}',
            ),
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_SELECT_ALL', '✅ Выбрать все'),
                callback_data=f'admin_tariff_select_all_squads:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    selected_count = len(current_squads)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_SELECT_SERVERS',
            '🌐 <b>Серверы для тарифа «{v0}»</b>\n\nВыбрано: {v1} из {v2}\n\nЕсли не выбран ни один сервер - доступны все.\nНажмите на сервер для выбора/отмены:',
        )
        .format(v0=html.escape(tariff.name), v1=selected_count, v2=len(squads)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_tariff_squad(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Переключает выбор сервера для тарифа."""
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    squad_uuid = parts[2]

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    current_squads = set(tariff.allowed_squads or [])

    if squad_uuid in current_squads:
        current_squads.remove(squad_uuid)
    else:
        current_squads.add(squad_uuid)

    tariff = await update_tariff(db, tariff, allowed_squads=list(current_squads))

    # Перерисовываем меню
    squads, _ = await get_all_server_squads(db, limit=10000)
    texts = get_texts(db_user.language)

    buttons = []
    for squad in squads:
        is_selected = squad.squad_uuid in current_squads
        prefix = '✅' if is_selected else '⬜'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {squad.display_name}',
                    callback_data=f'trf_sq:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_squads:{tariff_id}',
            ),
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_SELECT_ALL', '✅ Выбрать все'),
                callback_data=f'admin_tariff_select_all_squads:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_SELECT_SERVERS',
                '🌐 <b>Серверы для тарифа «{v0}»</b>\n\nВыбрано: {v1} из {v2}\n\nЕсли не выбран ни один сервер - доступны все.\nНажмите на сервер для выбора/отмены:',
            )
            .format(v0=html.escape(tariff.name), v1=len(current_squads), v2=len(squads)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass

    await callback.answer()

    # Применяем изменения серверов к существующим подпискам
    from app.services.subscription_service import SubscriptionService

    propagate_result = await SubscriptionService().propagate_tariff_squads(db, tariff.id, list(current_squads))
    if propagate_result.failed_ids:
        await callback.message.answer(
            get_texts(db_user.language)
            .t('ADMIN_TARIFF_SYNC_FAILED', '⚠️ {v0} из {v1} подписок не синхронизированы с RemnaWave')
            .format(v0=len(propagate_result.failed_ids), v1=propagate_result.total),
        )


@admin_required
@error_handler
async def clear_tariff_squads(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Очищает список серверов тарифа."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    tariff = await update_tariff(db, tariff, allowed_squads=[])
    await callback.answer(get_texts(db_user.language).t('ADMIN_TARIFF_SERVERS_CLEARED', 'Все серверы очищены'))

    # Перерисовываем меню
    squads, _ = await get_all_server_squads(db, limit=10000)
    texts = get_texts(db_user.language)

    buttons = []
    for squad in squads:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'⬜ {squad.display_name}',
                    callback_data=f'trf_sq:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_squads:{tariff_id}',
            ),
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_SELECT_ALL', '✅ Выбрать все'),
                callback_data=f'admin_tariff_select_all_squads:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_SELECT_SERVERS_EMPTY',
                '🌐 <b>Серверы для тарифа «{v0}»</b>\n\nВыбрано: 0 из {v1}\n\nЕсли не выбран ни один сервер - доступны все.\nНажмите на сервер для выбора/отмены:',
            )
            .format(v0=html.escape(tariff.name), v1=len(squads)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass

    # Применяем изменения серверов к существующим подпискам (пустой список = все серверы)
    from app.services.subscription_service import SubscriptionService

    propagate_result = await SubscriptionService().propagate_tariff_squads(db, tariff.id, [])
    if propagate_result.failed_ids:
        await callback.message.answer(
            get_texts(db_user.language)
            .t('ADMIN_TARIFF_SYNC_FAILED', '⚠️ {v0} из {v1} подписок не синхронизированы с RemnaWave')
            .format(v0=len(propagate_result.failed_ids), v1=propagate_result.total),
        )


@admin_required
@error_handler
async def select_all_tariff_squads(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Выбирает все серверы для тарифа."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    squads, _ = await get_all_server_squads(db, limit=10000)
    all_uuids = [s.squad_uuid for s in squads if s.squad_uuid]

    tariff = await update_tariff(db, tariff, allowed_squads=all_uuids)
    await callback.answer(get_texts(db_user.language).t('ADMIN_TARIFF_SERVERS_SELECTED', 'Все серверы выбраны'))

    texts = get_texts(db_user.language)

    buttons = []
    for squad in squads:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'✅ {squad.display_name}',
                    callback_data=f'trf_sq:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_squads:{tariff_id}',
            ),
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_SELECT_ALL', '✅ Выбрать все'),
                callback_data=f'admin_tariff_select_all_squads:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_SELECT_SERVERS',
                '🌐 <b>Серверы для тарифа «{v0}»</b>\n\nВыбрано: {v1} из {v2}\n\nЕсли не выбран ни один сервер - доступны все.\nНажмите на сервер для выбора/отмены:',
            )
            .format(v0=html.escape(tariff.name), v1=len(squads), v2=len(squads)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass

    # Применяем изменения серверов к существующим подпискам
    from app.services.subscription_service import SubscriptionService

    propagate_result = await SubscriptionService().propagate_tariff_squads(db, tariff.id, all_uuids)
    if propagate_result.failed_ids:
        await callback.message.answer(
            get_texts(db_user.language)
            .t('ADMIN_TARIFF_SYNC_FAILED', '⚠️ {v0} из {v1} подписок не синхронизированы с RemnaWave')
            .format(v0=len(propagate_result.failed_ids), v1=propagate_result.total),
        )


# ============ РЕДАКТИРОВАНИЕ ПРОМОГРУПП ============


@admin_required
@error_handler
async def start_edit_tariff_promo_groups(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Показывает меню выбора промогрупп для тарифа."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    promo_groups_data = await get_promo_groups_with_counts(db)

    if not promo_groups_data:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NO_PROMO_GROUPS', 'Нет промогрупп'), show_alert=True
        )
        return

    current_groups = {pg.id for pg in (tariff.allowed_promo_groups or [])}

    buttons = []
    for promo_group, _ in promo_groups_data:
        is_selected = promo_group.id in current_groups
        prefix = '✅' if is_selected else '⬜'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {promo_group.name}',
                    callback_data=f'admin_tariff_toggle_promo:{tariff_id}:{promo_group.id}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_promo:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    selected_count = len(current_groups)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_SELECT_PROMO_GROUPS',
            '👥 <b>Промогруппы для тарифа «{v0}»</b>\n\nВыбрано: {v1}\n\nЕсли не выбрана ни одна группа - тариф доступен всем.\nВыберите группы, которым доступен этот тариф:',
        )
        .format(v0=html.escape(tariff.name), v1=selected_count),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_tariff_promo_group(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Переключает выбор промогруппы для тарифа."""
    from app.database.crud.tariff import add_promo_group_to_tariff, remove_promo_group_from_tariff

    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    promo_group_id = int(parts[2])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    current_groups = {pg.id for pg in (tariff.allowed_promo_groups or [])}

    if promo_group_id in current_groups:
        await remove_promo_group_from_tariff(db, tariff, promo_group_id)
        current_groups.remove(promo_group_id)
    else:
        await add_promo_group_to_tariff(db, tariff, promo_group_id)
        current_groups.add(promo_group_id)

    # Обновляем тариф из БД
    tariff = await get_tariff_by_id(db, tariff_id)
    current_groups = {pg.id for pg in (tariff.allowed_promo_groups or [])}

    # Перерисовываем меню
    promo_groups_data = await get_promo_groups_with_counts(db)
    texts = get_texts(db_user.language)

    buttons = []
    for promo_group, _ in promo_groups_data:
        is_selected = promo_group.id in current_groups
        prefix = '✅' if is_selected else '⬜'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {promo_group.name}',
                    callback_data=f'admin_tariff_toggle_promo:{tariff_id}:{promo_group.id}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_promo:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_SELECT_PROMO_GROUPS',
                '👥 <b>Промогруппы для тарифа «{v0}»</b>\n\nВыбрано: {v1}\n\nЕсли не выбрана ни одна группа - тариф доступен всем.\nВыберите группы, которым доступен этот тариф:',
            )
            .format(v0=html.escape(tariff.name), v1=len(current_groups)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@admin_required
@error_handler
async def clear_tariff_promo_groups(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Очищает список промогрупп тарифа."""
    from app.database.crud.tariff import set_tariff_promo_groups

    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    await set_tariff_promo_groups(db, tariff, [])
    await callback.answer(get_texts(db_user.language).t('ADMIN_TARIFF_PROMO_GROUPS_CLEARED', 'Все промогруппы очищены'))

    # Перерисовываем меню
    promo_groups_data = await get_promo_groups_with_counts(db)
    texts = get_texts(db_user.language)

    buttons = []
    for promo_group, _ in promo_groups_data:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'⬜ {promo_group.name}',
                    callback_data=f'admin_tariff_toggle_promo:{tariff_id}:{promo_group.id}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_TARIFF_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_promo:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            get_texts(db_user.language)
            .t(
                'ADMIN_TARIFF_SELECT_PROMO_GROUPS_EMPTY',
                '👥 <b>Промогруппы для тарифа «{v0}»</b>\n\nВыбрано: 0\n\nЕсли не выбрана ни одна группа - тариф доступен всем.\nВыберите группы, которым доступен этот тариф:',
            )
            .format(v0=html.escape(tariff.name)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass


# ==================== Режим сброса трафика ====================

TRAFFIC_RESET_MODES = [
    ('DAY', 'ADMIN_TARIFF_RESET_DAY', 'ADMIN_TARIFF_RESET_DAY_DESC'),
    ('WEEK', 'ADMIN_TARIFF_RESET_WEEK', 'ADMIN_TARIFF_RESET_WEEK_DESC'),
    ('MONTH', 'ADMIN_TARIFF_RESET_MONTH', 'ADMIN_TARIFF_RESET_MONTH_DESC'),
    ('MONTH_ROLLING', 'ADMIN_TARIFF_RESET_ROLLING', 'ADMIN_TARIFF_RESET_ROLLING_DESC'),
    ('NO_RESET', 'ADMIN_TARIFF_RESET_NEVER', 'ADMIN_TARIFF_RESET_NEVER_DESC'),
]


def get_traffic_reset_mode_keyboard(tariff_id: int, current_mode: str | None, language: str) -> InlineKeyboardMarkup:
    """Создает клавиатуру для выбора режима сброса трафика."""
    texts = get_texts(language)
    buttons = []

    # Кнопка "Глобальная настройка"
    global_label = (
        get_texts(language)
        .t('ADMIN_TARIFF_GLOBAL_MODE_CHOICE', '{v0}🌐 Глобальная настройка ({v1})')
        .format(v0='✅ ' if current_mode is None else '', v1=settings.DEFAULT_TRAFFIC_RESET_STRATEGY)
    )
    buttons.append(
        [InlineKeyboardButton(text=global_label, callback_data=f'admin_tariff_set_reset_mode:{tariff_id}:GLOBAL')]
    )

    # Кнопки для каждого режима
    for mode_value, mode_label_key, mode_desc_key in TRAFFIC_RESET_MODES:
        mode_label = texts.t(mode_label_key)
        is_selected = current_mode == mode_value
        label = f'{"✅ " if is_selected else ""}{mode_label}'
        buttons.append(
            [InlineKeyboardButton(text=label, callback_data=f'admin_tariff_set_reset_mode:{tariff_id}:{mode_value}')]
        )

    # Кнопка назад
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_required
@error_handler
async def start_edit_traffic_reset_mode(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Начинает редактирование режима сброса трафика."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    current_mode = getattr(tariff, 'traffic_reset_mode', None)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_RESET_MODE',
            '🔄 <b>Режим сброса трафика для тарифа «{v0}»</b>\n\nТекущий режим: {v1}\n\nВыберите, когда сбрасывать использованный трафик у подписчиков этого тарифа:\n\n• <b>Глобальная настройка</b> — использовать значение из конфига бота\n• <b>Ежедневно</b> — сброс каждый день\n• <b>Еженедельно</b> — сброс каждую неделю\n• <b>Ежемесячно</b> — сброс каждый месяц\n• <b>Никогда</b> — трафик накапливается за весь период подписки',
        )
        .format(v0=html.escape(tariff.name), v1=_format_traffic_reset_mode(current_mode, language=db_user.language)),
        reply_markup=get_traffic_reset_mode_keyboard(tariff_id, current_mode, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def set_traffic_reset_mode(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Устанавливает режим сброса трафика для тарифа."""
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    new_mode = parts[2]

    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True
        )
        return

    # Преобразуем GLOBAL в None
    if new_mode == 'GLOBAL':
        new_mode = None

    # Обновляем тариф
    tariff = await update_tariff(db, tariff, traffic_reset_mode=new_mode)

    mode_display = _format_traffic_reset_mode(new_mode, language=db_user.language)
    await callback.answer(
        get_texts(db_user.language)
        .t('ADMIN_TARIFF_RESET_MODE_UPDATED', 'Режим сброса изменён: {v0}')
        .format(v0=mode_display),
        show_alert=True,
    )

    # Обновляем клавиатуру
    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_TARIFF_EDIT_RESET_MODE',
            '🔄 <b>Режим сброса трафика для тарифа «{v0}»</b>\n\nТекущий режим: {v1}\n\nВыберите, когда сбрасывать использованный трафик у подписчиков этого тарифа:\n\n• <b>Глобальная настройка</b> — использовать значение из конфига бота\n• <b>Ежедневно</b> — сброс каждый день\n• <b>Еженедельно</b> — сброс каждую неделю\n• <b>Ежемесячно</b> — сброс каждый месяц\n• <b>Никогда</b> — трафик накапливается за весь период подписки',
        )
        .format(v0=html.escape(tariff.name), v1=mode_display),
        reply_markup=get_traffic_reset_mode_keyboard(tariff_id, new_mode, db_user.language),
        parse_mode='HTML',
    )


def register_handlers(dp: Dispatcher):
    """Регистрирует обработчики для управления тарифами."""
    # Отдельные экраны регистрируются до общего toggle-фильтра.
    register_custom_traffic_handlers(dp)
    register_custom_days_handlers(dp)
    register_panel_settings_handlers(dp)
    register_server_limits_handlers(dp)

    # Список тарифов
    dp.callback_query.register(show_tariffs_list, F.data == 'admin_tariffs')
    dp.callback_query.register(show_tariffs_page, F.data.startswith('admin_tariffs_page:'))

    # Просмотр и переключение
    dp.callback_query.register(view_tariff, F.data.startswith('admin_tariff_view:'))
    dp.callback_query.register(
        toggle_tariff,
        F.data.startswith('admin_tariff_toggle:')
        & ~F.data.startswith('admin_tariff_toggle_trial:')
        & ~F.data.startswith('trf_sq:')
        & ~F.data.startswith('admin_tariff_toggle_promo:')
        & ~F.data.startswith('admin_tariff_toggle_traffic_topup:')
        & ~F.data.startswith('admin_tariff_toggle_custom_traffic:')
        & ~F.data.startswith('admin_tariff_toggle_daily:'),
    )
    dp.callback_query.register(toggle_trial_tariff, F.data.startswith('admin_tariff_toggle_trial:'))

    # Создание тарифа
    dp.callback_query.register(start_create_tariff, F.data == 'admin_tariff_create')
    dp.message.register(process_tariff_name, AdminStates.creating_tariff_name)
    dp.message.register(process_tariff_traffic, AdminStates.creating_tariff_traffic)
    dp.message.register(process_tariff_devices, AdminStates.creating_tariff_devices)
    dp.message.register(process_tariff_tier, AdminStates.creating_tariff_tier)
    dp.callback_query.register(select_tariff_type_periodic, F.data == 'tariff_type_periodic')
    dp.callback_query.register(select_tariff_type_daily, F.data == 'tariff_type_daily')
    dp.message.register(process_tariff_prices, AdminStates.creating_tariff_prices)

    # Редактирование названия
    dp.callback_query.register(start_edit_tariff_name, F.data.startswith('admin_tariff_edit_name:'))
    dp.message.register(process_edit_tariff_name, AdminStates.editing_tariff_name)

    # Редактирование описания
    dp.callback_query.register(start_edit_tariff_description, F.data.startswith('admin_tariff_edit_desc:'))
    dp.message.register(process_edit_tariff_description, AdminStates.editing_tariff_description)

    # Редактирование трафика (traffic_topup BEFORE traffic to avoid prefix conflict)
    dp.callback_query.register(start_edit_tariff_traffic_topup, F.data.startswith('admin_tariff_edit_traffic_topup:'))
    dp.callback_query.register(start_edit_tariff_traffic, F.data.startswith('admin_tariff_edit_traffic:'))
    dp.message.register(process_edit_tariff_traffic, AdminStates.editing_tariff_traffic)

    # Редактирование устройств
    dp.callback_query.register(start_edit_tariff_devices, F.data.startswith('admin_tariff_edit_devices:'))
    dp.message.register(process_edit_tariff_devices, AdminStates.editing_tariff_devices)

    # Редактирование уровня
    dp.callback_query.register(start_edit_tariff_tier, F.data.startswith('admin_tariff_edit_tier:'))
    dp.message.register(process_edit_tariff_tier, AdminStates.editing_tariff_tier)

    # Редактирование цен
    dp.callback_query.register(start_edit_tariff_prices, F.data.startswith('admin_tariff_edit_prices:'))
    dp.callback_query.register(start_edit_tariff_highlight, F.data.startswith('admin_tariff_edit_highlight:'))
    dp.callback_query.register(toggle_tariff_highlight, F.data.startswith('admin_tariff_toggle_highlight:'))
    dp.callback_query.register(set_tariff_highlight, F.data.startswith('admin_tariff_highlight_set:'))
    dp.message.register(process_edit_tariff_prices, AdminStates.editing_tariff_prices)

    # Редактирование цены за устройство
    dp.callback_query.register(start_edit_tariff_device_price, F.data.startswith('admin_tariff_edit_device_price:'))
    dp.message.register(process_edit_tariff_device_price, AdminStates.editing_tariff_device_price)

    # Редактирование макс. устройств
    dp.callback_query.register(start_edit_tariff_max_devices, F.data.startswith('admin_tariff_edit_max_devices:'))
    dp.message.register(process_edit_tariff_max_devices, AdminStates.editing_tariff_max_devices)

    # Редактирование дней триала
    dp.callback_query.register(start_edit_tariff_trial_days, F.data.startswith('admin_tariff_edit_trial_days:'))
    dp.message.register(process_edit_tariff_trial_days, AdminStates.editing_tariff_trial_days)

    # Редактирование докупки трафика (start_edit_tariff_traffic_topup registered above with traffic)
    dp.callback_query.register(toggle_tariff_traffic_topup, F.data.startswith('admin_tariff_toggle_traffic_topup:'))
    dp.callback_query.register(
        start_edit_traffic_topup_packages, F.data.startswith('admin_tariff_edit_topup_packages:')
    )
    dp.message.register(process_edit_traffic_topup_packages, AdminStates.editing_tariff_traffic_topup_packages)

    # Редактирование макс. лимита докупки трафика
    dp.callback_query.register(start_edit_max_topup_traffic, F.data.startswith('admin_tariff_edit_max_topup:'))
    dp.message.register(process_edit_max_topup_traffic, AdminStates.editing_tariff_max_topup_traffic)

    # Удаление (delete_confirm BEFORE delete to avoid prefix conflict)
    dp.callback_query.register(delete_tariff_confirmed, F.data.startswith('admin_tariff_delete_confirm:'))
    dp.callback_query.register(confirm_delete_tariff, F.data.startswith('admin_tariff_delete:'))

    # Редактирование серверов
    dp.callback_query.register(start_edit_tariff_squads, F.data.startswith('admin_tariff_edit_squads:'))
    dp.callback_query.register(toggle_tariff_squad, F.data.startswith('trf_sq:'))
    dp.callback_query.register(clear_tariff_squads, F.data.startswith('admin_tariff_clear_squads:'))
    dp.callback_query.register(select_all_tariff_squads, F.data.startswith('admin_tariff_select_all_squads:'))

    # Редактирование промогрупп
    dp.callback_query.register(start_edit_tariff_promo_groups, F.data.startswith('admin_tariff_edit_promo:'))
    dp.callback_query.register(toggle_tariff_promo_group, F.data.startswith('admin_tariff_toggle_promo:'))
    dp.callback_query.register(clear_tariff_promo_groups, F.data.startswith('admin_tariff_clear_promo:'))

    # Суточный режим
    dp.callback_query.register(toggle_daily_tariff, F.data.startswith('admin_tariff_toggle_daily:'))
    dp.callback_query.register(start_edit_daily_price, F.data.startswith('admin_tariff_edit_daily_price:'))
    dp.message.register(process_daily_price_input, AdminStates.editing_tariff_daily_price)

    # Режим сброса трафика
    dp.callback_query.register(start_edit_traffic_reset_mode, F.data.startswith('admin_tariff_edit_reset_mode:'))
    dp.callback_query.register(set_traffic_reset_mode, F.data.startswith('admin_tariff_set_reset_mode:'))
