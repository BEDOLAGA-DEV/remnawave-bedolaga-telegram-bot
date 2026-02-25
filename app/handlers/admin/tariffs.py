"""Управление тарифами в админ-панели."""

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
    get_tariff_by_id,
    get_tariff_subscriptions_count,
    get_tariffs_with_subscriptions_count,
    update_tariff,
)
from app.database.models import Tariff, User
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

ITEMS_PER_PAGE = 10


def _format_traffic(gb: int, texts=None) -> str:
    """Форматирует трафик."""
    gb_unit = texts.t('ADMIN_TARIFF_GB_UNIT', 'ГБ') if texts else 'ГБ'
    if gb == 0:
        return texts.t('ADMIN_TARIFF_UNLIMITED_TRAFFIC', 'Безлимит') if texts else 'Безлимит'
    return f'{gb} {gb_unit}'


def _format_price_kopeks(kopeks: int) -> str:
    """Форматирует цену из копеек в рубли."""
    rubles = kopeks / 100
    if rubles == int(rubles):
        return f'{int(rubles)} ₽'
    return f'{rubles:.2f} ₽'


def _format_period(days: int, texts=None) -> str:
    """Форматирует период."""
    if days == 1:
        return texts.t('ADMIN_TARIFF_PERIOD_ONE_DAY', '1 день') if texts else '1 день'
    if days < 5:
        return (
            texts.t('ADMIN_TARIFF_PERIOD_FEW_DAYS', '{days} дня').format(days=days)
            if texts
            else f'{days} дня'
        )
    if days < 21 or days % 10 >= 5 or days % 10 == 0:
        return (
            texts.t('ADMIN_TARIFF_PERIOD_MANY_DAYS', '{days} дней').format(days=days)
            if texts
            else f'{days} дней'
        )
    if days % 10 == 1:
        return (
            texts.t('ADMIN_TARIFF_PERIOD_SINGLE_DAY_FORM', '{days} день').format(days=days)
            if texts
            else f'{days} день'
        )
    return (
        texts.t('ADMIN_TARIFF_PERIOD_FEW_DAYS', '{days} дня').format(days=days)
        if texts
        else f'{days} дня'
    )


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


def _format_period_prices_display(prices: dict[str, int], texts=None) -> str:
    """Форматирует цены периодов для отображения."""
    if not prices:
        return texts.t('ADMIN_TARIFF_PRICES_NOT_SET', 'Не заданы') if texts else 'Не заданы'

    lines = []
    for period_str in sorted(prices.keys(), key=int):
        period = int(period_str)
        price = prices[period_str]
        lines.append(f'  • {_format_period(period, texts)}: {_format_price_kopeks(price)}')

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
                text=texts.t('ADMIN_TARIFF_CREATE_BUTTON', '➕ Создать тариф'),
                callback_data='admin_tariff_create',
            )
        ]
    )

    # Кнопка назад
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings')])

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
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_NAME', '✏️ Название'),
                callback_data=f'admin_tariff_edit_name:{tariff.id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_DESCRIPTION', '📝 Описание'),
                callback_data=f'admin_tariff_edit_desc:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_TRAFFIC', '📊 Трафик'),
                callback_data=f'admin_tariff_edit_traffic:{tariff.id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_DEVICES', '📱 Устройства'),
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
                    text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_PRICES', '💰 Цены'),
                    callback_data=f'admin_tariff_edit_prices:{tariff.id}',
                ),
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_TIER', '🎚️ Уровень'),
                    callback_data=f'admin_tariff_edit_tier:{tariff.id}',
                ),
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_TIER', '🎚️ Уровень'),
                    callback_data=f'admin_tariff_edit_tier:{tariff.id}',
                ),
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_DEVICE_PRICE', '📱💰 Цена за устройство'),
                callback_data=f'admin_tariff_edit_device_price:{tariff.id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_MAX_DEVICES', '📱🔒 Макс. устройств'),
                callback_data=f'admin_tariff_edit_max_devices:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_TRIAL_DAYS', '⏰ Дни триала'),
                callback_data=f'admin_tariff_edit_trial_days:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_TRAFFIC_TOPUP', '📈 Докупка трафика'),
                callback_data=f'admin_tariff_edit_traffic_topup:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_RESET_MODE', '🔄 Сброс трафика'),
                callback_data=f'admin_tariff_edit_reset_mode:{tariff.id}',
            ),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_SQUADS', '🌐 Серверы'),
                callback_data=f'admin_tariff_edit_squads:{tariff.id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_PROMO_GROUPS', '👥 Промогруппы'),
                callback_data=f'admin_tariff_edit_promo:{tariff.id}',
            ),
        ]
    )

    # Суточный режим - только для уже суточных тарифов показываем настройки
    # Новые тарифы делаются суточными только при создании
    if is_daily:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_BUTTON_EDIT_DAILY_PRICE', '💰 Суточная цена'),
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


def _format_traffic_reset_mode(mode: str | None, texts=None) -> str:
    """Форматирует режим сброса трафика для отображения."""
    mode_labels = {
        'DAY': texts.t('ADMIN_TARIFF_RESET_MODE_DAY', '📅 Ежедневно') if texts else '📅 Ежедневно',
        'WEEK': texts.t('ADMIN_TARIFF_RESET_MODE_WEEK', '📆 Еженедельно') if texts else '📆 Еженедельно',
        'MONTH': texts.t('ADMIN_TARIFF_RESET_MODE_MONTH', '🗓️ Ежемесячно') if texts else '🗓️ Ежемесячно',
        'NO_RESET': texts.t('ADMIN_TARIFF_RESET_MODE_NEVER', '🚫 Никогда') if texts else '🚫 Никогда',
    }
    if mode is None:
        if texts:
            return texts.t(
                'ADMIN_TARIFF_RESET_MODE_GLOBAL',
                '🌐 Глобальная настройка ({strategy})',
            ).format(strategy=settings.DEFAULT_TRAFFIC_RESET_STRATEGY)
        return f'🌐 Глобальная настройка ({settings.DEFAULT_TRAFFIC_RESET_STRATEGY})'
    if texts:
        return mode_labels.get(mode, texts.t('ADMIN_TARIFF_RESET_MODE_UNKNOWN', '⚠️ Неизвестно ({mode})').format(mode=mode))
    return mode_labels.get(mode, f'⚠️ Неизвестно ({mode})')


def _format_traffic_topup_packages(tariff: Tariff, texts=None) -> str:
    """Форматирует пакеты докупки трафика для отображения."""
    if not getattr(tariff, 'traffic_topup_enabled', False):
        return texts.t('ADMIN_TARIFF_TRAFFIC_TOPUP_DISABLED', '❌ Отключено') if texts else '❌ Отключено'

    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    if not packages:
        if texts:
            return texts.t('ADMIN_TARIFF_TRAFFIC_TOPUP_ENABLED_NO_PACKAGES', '✅ Включено, но пакеты не настроены')
        return '✅ Включено, но пакеты не настроены'

    lines = [texts.t('ADMIN_TARIFF_TRAFFIC_TOPUP_ENABLED', '✅ Включено') if texts else '✅ Включено']
    for gb in sorted(packages.keys()):
        price = packages[gb]
        gb_unit = texts.t('ADMIN_TARIFF_GB_UNIT', 'ГБ') if texts else 'ГБ'
        lines.append(f'  • {gb} {gb_unit}: {_format_price_kopeks(price)}')

    return '\n'.join(lines)


def format_tariff_info(tariff: Tariff, language: str, subs_count: int = 0) -> str:
    """Форматирует информацию о тарифе."""
    texts = get_texts(language)

    status = (
        texts.t('ADMIN_TARIFF_STATUS_ACTIVE', '✅ Активен')
        if tariff.is_active
        else texts.t('ADMIN_TARIFF_STATUS_INACTIVE', '❌ Неактивен')
    )
    traffic = _format_traffic(tariff.traffic_limit_gb, texts)
    prices_display = _format_period_prices_display(tariff.period_prices or {}, texts)

    # Форматируем список серверов
    squads_list = tariff.allowed_squads or []
    if squads_list:
        squads_display = texts.t('ADMIN_TARIFF_SQUADS_COUNT', '{count} серверов').format(count=len(squads_list))
    else:
        squads_display = texts.t('ADMIN_TARIFF_SQUADS_ALL', 'Все серверы')

    # Форматируем промогруппы
    promo_groups = tariff.allowed_promo_groups or []
    if promo_groups:
        promo_display = ', '.join(pg.name for pg in promo_groups)
    else:
        promo_display = texts.t('ADMIN_TARIFF_PROMO_AVAILABLE_FOR_ALL', 'Доступен всем')

    trial_status = (
        texts.t('ADMIN_TARIFF_TRIAL_YES', '✅ Да')
        if tariff.is_trial_available
        else texts.t('ADMIN_TARIFF_TRIAL_NO', '❌ Нет')
    )

    # Форматируем дни триала
    trial_days = getattr(tariff, 'trial_duration_days', None)
    if trial_days:
        trial_days_display = texts.t('ADMIN_TARIFF_TRIAL_DAYS_VALUE', '{days} дней').format(days=trial_days)
    else:
        trial_days_display = texts.t(
            'ADMIN_TARIFF_TRIAL_DAYS_DEFAULT',
            'По умолчанию ({days} дней)',
        ).format(days=settings.TRIAL_DURATION_DAYS)

    # Форматируем цену за устройство
    device_price = getattr(tariff, 'device_price_kopeks', None)
    if device_price is not None and device_price > 0:
        device_price_display = _format_price_kopeks(device_price) + texts.t('ADMIN_TARIFF_PER_MONTH_SUFFIX', '/мес')
    else:
        device_price_display = texts.t('ADMIN_TARIFF_NOT_AVAILABLE', 'Недоступно')

    # Форматируем макс. устройств
    max_devices = getattr(tariff, 'max_device_limit', None)
    if max_devices is not None and max_devices > 0:
        max_devices_display = str(max_devices)
    else:
        max_devices_display = texts.t('ADMIN_TARIFF_MAX_DEVICES_UNLIMITED', '∞ (без лимита)')

    # Форматируем докупку трафика
    traffic_topup_display = _format_traffic_topup_packages(tariff, texts)

    # Форматируем режим сброса трафика
    traffic_reset_mode = getattr(tariff, 'traffic_reset_mode', None)
    traffic_reset_display = _format_traffic_reset_mode(traffic_reset_mode, texts)

    # Форматируем суточный тариф
    is_daily = getattr(tariff, 'is_daily', False)
    daily_price_kopeks = getattr(tariff, 'daily_price_kopeks', 0)

    # Формируем блок цен в зависимости от типа тарифа
    if is_daily:
        price_block = texts.t(
            'ADMIN_TARIFF_DAILY_PRICE_BLOCK',
            '<b>💰 Суточная цена:</b> {price}/день',
        ).format(price=_format_price_kopeks(daily_price_kopeks))
        tariff_type = texts.t('ADMIN_TARIFF_TYPE_DAILY', '🔄 Суточный')
    else:
        price_block = texts.t('ADMIN_TARIFF_PERIOD_PRICES_BLOCK', '<b>Цены:</b>\n{prices}').format(prices=prices_display)
        tariff_type = texts.t('ADMIN_TARIFF_TYPE_PERIODIC', '📅 Периодный')

    description_block = (
        texts.t('ADMIN_TARIFF_DESCRIPTION_LINE', '📝 {description}').format(description=tariff.description)
        if tariff.description
        else ''
    )
    return texts.t(
        'ADMIN_TARIFF_INFO_TEMPLATE',
        '📦 <b>Тариф: {tariff_name}</b>\n\n'
        '{status} | {tariff_type}\n'
        '🎚️ Уровень: {tier_level}\n'
        '📊 Порядок: {display_order}\n\n'
        '<b>Параметры:</b>\n'
        '• Трафик: {traffic}\n'
        '• Устройств: {device_limit}\n'
        '• Макс. устройств: {max_devices_display}\n'
        '• Цена за доп. устройство: {device_price_display}\n'
        '• Триал: {trial_status}\n'
        '• Дней триала: {trial_days_display}\n\n'
        '<b>Докупка трафика:</b>\n'
        '{traffic_topup_display}\n\n'
        '<b>Сброс трафика:</b> {traffic_reset_display}\n\n'
        '{price_block}\n\n'
        '<b>Серверы:</b> {squads_display}\n'
        '<b>Промогруппы:</b> {promo_display}\n\n'
        '📊 Подписок на тарифе: {subs_count}\n\n'
        '{description_block}',
    ).format(
        tariff_name=tariff.name,
        status=status,
        tariff_type=tariff_type,
        tier_level=tariff.tier_level,
        display_order=tariff.display_order,
        traffic=traffic,
        device_limit=tariff.device_limit,
        max_devices_display=max_devices_display,
        device_price_display=device_price_display,
        trial_status=trial_status,
        trial_days_display=trial_days_display,
        traffic_topup_display=traffic_topup_display,
        traffic_reset_display=traffic_reset_display,
        price_block=price_block,
        squads_display=squads_display,
        promo_display=promo_display,
        subs_count=subs_count,
        description_block=description_block,
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
            texts.t(
                'ADMIN_TARIFFS_MODE_DISABLED_TEXT',
                '⚠️ <b>Режим тарифов отключен</b>\n\n'
                'Для использования тарифов установите:\n'
                '<code>SALES_MODE=tariffs</code>\n\n'
                'Текущий режим: <code>classic</code>',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings')]]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    tariffs_data = await get_tariffs_with_subscriptions_count(db, include_inactive=True)

    if not tariffs_data:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_TARIFFS_EMPTY_TEXT',
                '📦 <b>Тарифы</b>\n\nТарифы ещё не созданы.\nСоздайте первый тариф для начала работы.',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_TARIFF_CREATE_BUTTON', '➕ Создать тариф'),
                            callback_data='admin_tariff_create',
                        )
                    ],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings')],
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
        texts.t(
            'ADMIN_TARIFFS_LIST_TEXT',
            '📦 <b>Тарифы</b>\n\n'
            'Всего: {total} (активных: {active})\n'
            'Подписок на тарифах: {subs}\n\n'
            'Выберите тариф для просмотра и редактирования:',
        ).format(total=len(tariffs_data), active=active_count, subs=total_subs),
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
    texts = get_texts(db_user.language)
    page = int(callback.data.split(':')[1])

    tariffs_data = await get_tariffs_with_subscriptions_count(db, include_inactive=True)
    total_pages = (len(tariffs_data) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_data = tariffs_data[start_idx:end_idx]

    total_subs = sum(count for _, count in tariffs_data)
    active_count = sum(1 for t, _ in tariffs_data if t.is_active)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFFS_LIST_PAGE_TEXT',
            '📦 <b>Тарифы</b> (стр. {page}/{total_pages})\n\n'
            'Всего: {total} (активных: {active})\n'
            'Подписок на тарифах: {subs}\n\n'
            'Выберите тариф для просмотра и редактирования:',
        ).format(
            page=page + 1,
            total_pages=total_pages,
            total=len(tariffs_data),
            active=active_count,
            subs=total_subs,
        ),
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
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
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
async def toggle_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Переключает активность тарифа."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    tariff = await update_tariff(db, tariff, is_active=not tariff.is_active)
    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    status = (
        texts.t('ADMIN_TARIFF_STATUS_ACTIVATED', 'активирован')
        if tariff.is_active
        else texts.t('ADMIN_TARIFF_STATUS_DEACTIVATED', 'деактивирован')
    )
    await callback.answer(
        texts.t('ADMIN_TARIFF_STATUS_CHANGED', 'Тариф {status}').format(status=status),
        show_alert=True,
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

    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    if tariff.is_trial_available:
        # Снимаем флаг триала
        await clear_trial_tariff(db)
        await callback.answer(texts.t('ADMIN_TARIFF_TRIAL_REMOVED', 'Триал снят с тарифа'), show_alert=True)
    else:
        # Устанавливаем этот тариф как триальный (снимает флаг с других)
        await set_trial_tariff(db, tariff_id)
        await callback.answer(
            texts.t('ADMIN_TARIFF_TRIAL_SET', 'Тариф «{name}» установлен как триальный').format(name=tariff.name),
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
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    is_daily = getattr(tariff, 'is_daily', False)

    if is_daily:
        # Отключаем суточный режим
        tariff = await update_tariff(db, tariff, is_daily=False, daily_price_kopeks=0)
        await callback.answer(texts.t('ADMIN_TARIFF_DAILY_MODE_DISABLED', 'Суточный режим отключен'), show_alert=True)
    else:
        # Включаем суточный режим (с ценой по умолчанию)
        tariff = await update_tariff(db, tariff, is_daily=True, daily_price_kopeks=5000)  # 50 руб по умолчанию
        await callback.answer(
            texts.t(
                'ADMIN_TARIFF_DAILY_MODE_ENABLED',
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    current_price = getattr(tariff, 'daily_price_kopeks', 0)
    current_price / 100 if current_price else 0

    await state.set_state(AdminStates.editing_tariff_daily_price)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_EDIT_DAILY_PRICE_PROMPT',
            '💰 <b>Редактирование суточной цены</b>\n\n'
            'Тариф: {name}\n'
            'Текущая цена: {current_price}/день\n\n'
            'Введите новую цену за день в рублях.\n'
            'Пример: <code>50</code> или <code>99.90</code>',
        ).format(name=tariff.name, current_price=_format_price_kopeks(current_price)),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    # Парсим цену
    try:
        price_rubles = float(message.text.strip().replace(',', '.'))
        if price_rubles <= 0:
            raise ValueError('Цена должна быть положительной')

        price_kopeks = int(price_rubles * 100)
    except ValueError:
        await message.answer(
            texts.t(
                'ADMIN_TARIFF_DAILY_PRICE_INVALID_INPUT',
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
            texts.t('ADMIN_TARIFF_DAILY_CREATED_SUCCESS', '✅ <b>Суточный тариф создан!</b>\n\n')
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
            await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
            await state.clear()
            return

        tariff = await update_tariff(db, tariff, daily_price_kopeks=price_kopeks)
        await state.clear()

        subs_count = await get_tariff_subscriptions_count(db, tariff_id)

        await message.answer(
            texts.t(
                'ADMIN_TARIFF_DAILY_PRICE_SET_SUCCESS',
                '✅ Суточная цена установлена: {price}/день\n\n',
            ).format(price=_format_price_kopeks(price_kopeks))
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
        texts.t(
            'ADMIN_TARIFF_CREATE_STEP1_PROMPT',
            '📦 <b>Создание тарифа</b>\n\n'
            'Шаг 1/6: Введите название тарифа\n\n'
            'Пример: <i>Базовый</i>, <i>Премиум</i>, <i>Бизнес</i>',
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
            texts.t('ADMIN_TARIFF_NAME_TOO_SHORT', 'Название должно быть не короче 2 символов')
        )
        return

    if len(name) > 50:
        await message.answer(
            texts.t('ADMIN_TARIFF_NAME_TOO_LONG', 'Название должно быть не длиннее 50 символов')
        )
        return

    await state.update_data(tariff_name=name)
    await state.set_state(AdminStates.creating_tariff_traffic)

    await message.answer(
        texts.t(
            'ADMIN_TARIFF_CREATE_STEP2_PROMPT',
            '📦 <b>Создание тарифа</b>\n\n'
            'Название: <b>{name}</b>\n\n'
            'Шаг 2/6: Введите лимит трафика в ГБ\n\n'
            'Введите <code>0</code> для безлимитного трафика\n'
            'Пример: <i>100</i>, <i>500</i>, <i>0</i>',
        ).format(name=name),
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
        await message.answer(texts.t('ADMIN_TARIFF_VALUE_NON_NEGATIVE', 'Введите корректное число (0 или больше)'))
        return

    data = await state.get_data()
    await state.update_data(tariff_traffic=traffic)
    await state.set_state(AdminStates.creating_tariff_devices)

    traffic_display = _format_traffic(traffic, texts)

    await message.answer(
        texts.t(
            'ADMIN_TARIFF_CREATE_STEP3_PROMPT',
            '📦 <b>Создание тарифа</b>\n\n'
            'Название: <b>{name}</b>\n'
            'Трафик: <b>{traffic}</b>\n\n'
            'Шаг 3/6: Введите лимит устройств\n\n'
            'Пример: <i>1</i>, <i>3</i>, <i>5</i>',
        ).format(name=data['tariff_name'], traffic=traffic_display),
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
        await message.answer(texts.t('ADMIN_TARIFF_VALUE_MIN_ONE', 'Введите корректное число (1 или больше)'))
        return

    data = await state.get_data()
    await state.update_data(tariff_devices=devices)
    await state.set_state(AdminStates.creating_tariff_tier)

    traffic_display = _format_traffic(data['tariff_traffic'], texts)

    await message.answer(
        texts.t(
            'ADMIN_TARIFF_CREATE_STEP4_PROMPT',
            '📦 <b>Создание тарифа</b>\n\n'
            'Название: <b>{name}</b>\n'
            'Трафик: <b>{traffic}</b>\n'
            'Устройств: <b>{devices}</b>\n\n'
            'Шаг 4/6: Введите уровень тарифа (1-10)\n\n'
            'Уровень используется для визуального отображения\n'
            '1 - базовый, 10 - максимальный\n'
            'Пример: <i>1</i>, <i>2</i>, <i>3</i>',
        ).format(name=data['tariff_name'], traffic=traffic_display, devices=devices),
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
        await message.answer(texts.t('ADMIN_TARIFF_TIER_RANGE_ERROR', 'Введите число от 1 до 10'))
        return

    data = await state.get_data()
    await state.update_data(tariff_tier=tier)

    traffic_display = _format_traffic(data['tariff_traffic'], texts)

    # Шаг 5/6: Выбор типа тарифа
    await message.answer(
        texts.t(
            'ADMIN_TARIFF_CREATE_STEP5_PROMPT',
            '📦 <b>Создание тарифа</b>\n\n'
            'Название: <b>{name}</b>\n'
            'Трафик: <b>{traffic}</b>\n'
            'Устройств: <b>{devices}</b>\n'
            'Уровень: <b>{tier}</b>\n\n'
            'Шаг 5/6: Выберите тип тарифа',
        ).format(name=data['tariff_name'], traffic=traffic_display, devices=data['tariff_devices'], tier=tier),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_TARIFF_TYPE_PERIODIC_BUTTON', '📅 Периодный (месяцы)'),
                        callback_data='tariff_type_periodic',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_TARIFF_TYPE_DAILY_BUTTON', '🔄 Суточный (оплата за день)'),
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

    traffic_display = _format_traffic(data['tariff_traffic'], texts)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_CREATE_STEP6_PERIODIC_PROMPT',
            '📦 <b>Создание тарифа</b>\n\n'
            'Название: <b>{name}</b>\n'
            'Трафик: <b>{traffic}</b>\n'
            'Устройств: <b>{devices}</b>\n'
            'Уровень: <b>{tier}</b>\n'
            'Тип: <b>📅 Периодный</b>\n\n'
            'Шаг 6/6: Введите цены на периоды\n\n'
            'Формат: <code>дней:цена_в_копейках</code>\n'
            'Несколько периодов через запятую\n\n'
            'Пример:\n<code>30:9900, 90:24900, 180:44900, 360:79900</code>',
        ).format(
            name=data['tariff_name'],
            traffic=traffic_display,
            devices=data['tariff_devices'],
            tier=data['tariff_tier'],
        ),
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

    traffic_display = _format_traffic(data['tariff_traffic'], texts)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_CREATE_STEP6_DAILY_PROMPT',
            '📦 <b>Создание суточного тарифа</b>\n\n'
            'Название: <b>{name}</b>\n'
            'Трафик: <b>{traffic}</b>\n'
            'Устройств: <b>{devices}</b>\n'
            'Уровень: <b>{tier}</b>\n'
            'Тип: <b>🔄 Суточный</b>\n\n'
            'Шаг 6/6: Введите суточную цену в рублях\n\n'
            'Пример: <i>50</i> (50 ₽/день), <i>99.90</i> (99.90 ₽/день)',
        ).format(
            name=data['tariff_name'],
            traffic=traffic_display,
            devices=data['tariff_devices'],
            tier=data['tariff_tier'],
        ),
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
    texts = get_texts(db_user.language)

    prices = _parse_period_prices(message.text.strip())

    if not prices:
        await message.answer(
            texts.t(
                'ADMIN_TARIFF_CREATE_PRICES_PARSE_ERROR',
                'Не удалось распознать цены.\n\n'
                'Формат: <code>дней:цена_в_копейках</code>\n'
                'Пример: <code>30:9900, 90:24900</code>',
            ),
            parse_mode='HTML',
        )
        return

    data = await state.get_data()
    await state.update_data(tariff_prices=prices)

    _format_traffic(data['tariff_traffic'])
    _format_period_prices_display(prices)

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
        texts.t('ADMIN_TARIFF_CREATED_SUCCESS', '✅ <b>Тариф создан!</b>\n\n')
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_name)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_NAME_EDIT_PROMPT',
            '✏️ <b>Редактирование названия</b>\n\n'
            'Текущее название: <b>{name}</b>\n\n'
            'Введите новое название:',
        ).format(name=tariff.name),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    name = message.text.strip()
    if len(name) < 2 or len(name) > 50:
        await message.answer(
            texts.t('ADMIN_TARIFF_NAME_LENGTH_ERROR', 'Название должно быть от 2 до 50 символов')
        )
        return

    tariff = await update_tariff(db, tariff, name=name)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        texts.t('ADMIN_TARIFF_NAME_UPDATED_SUCCESS', '✅ Название изменено!\n\n')
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_description)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    current_desc = tariff.description or texts.t('ADMIN_TARIFF_DESCRIPTION_NOT_SET', 'Не задано')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_DESCRIPTION_EDIT_PROMPT',
            '📝 <b>Редактирование описания</b>\n\n'
            'Текущее описание:\n{current_description}\n\n'
            'Введите новое описание (или <code>-</code> для удаления):',
        ).format(current_description=current_desc),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    description = message.text.strip()
    if description == '-':
        description = None

    tariff = await update_tariff(db, tariff, description=description)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        texts.t('ADMIN_TARIFF_DESCRIPTION_UPDATED_SUCCESS', '✅ Описание изменено!\n\n')
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_traffic)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    current_traffic = _format_traffic(tariff.traffic_limit_gb, texts)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_TRAFFIC_EDIT_PROMPT',
            '📊 <b>Редактирование трафика</b>\n\n'
            'Текущий лимит: <b>{current_traffic}</b>\n\n'
            'Введите новый лимит в ГБ (0 = безлимит):',
        ).format(current_traffic=current_traffic),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    try:
        traffic = int(message.text.strip())
        if traffic < 0:
            raise ValueError
    except ValueError:
        await message.answer(texts.t('ADMIN_TARIFF_VALUE_NON_NEGATIVE', 'Введите корректное число (0 или больше)'))
        return

    tariff = await update_tariff(db, tariff, traffic_limit_gb=traffic)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        texts.t('ADMIN_TARIFF_TRAFFIC_UPDATED_SUCCESS', '✅ Трафик изменен!\n\n')
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_devices)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_DEVICES_EDIT_PROMPT',
            '📱 <b>Редактирование устройств</b>\n\n'
            'Текущий лимит: <b>{device_limit}</b>\n\n'
            'Введите новый лимит устройств:',
        ).format(device_limit=tariff.device_limit),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    try:
        devices = int(message.text.strip())
        if devices < 1:
            raise ValueError
    except ValueError:
        await message.answer(texts.t('ADMIN_TARIFF_VALUE_MIN_ONE', 'Введите корректное число (1 или больше)'))
        return

    tariff = await update_tariff(db, tariff, device_limit=devices)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        texts.t('ADMIN_TARIFF_DEVICES_UPDATED_SUCCESS', '✅ Лимит устройств изменен!\n\n')
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_tier)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_TIER_EDIT_PROMPT',
            '🎚️ <b>Редактирование уровня</b>\n\n'
            'Текущий уровень: <b>{tier}</b>\n\n'
            'Введите новый уровень (1-10):',
        ).format(tier=tariff.tier_level),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    try:
        tier = int(message.text.strip())
        if tier < 1 or tier > 10:
            raise ValueError
    except ValueError:
        await message.answer(texts.t('ADMIN_TARIFF_TIER_RANGE_ERROR', 'Введите число от 1 до 10'))
        return

    tariff = await update_tariff(db, tariff, tier_level=tier)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        texts.t('ADMIN_TARIFF_TIER_UPDATED_SUCCESS', '✅ Уровень изменен!\n\n')
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_prices)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    current_prices = _format_period_prices_for_edit(tariff.period_prices or {})
    prices_display = _format_period_prices_display(tariff.period_prices or {}, texts)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_PRICES_EDIT_PROMPT',
            '💰 <b>Редактирование цен</b>\n\n'
            'Текущие цены:\n{prices_display}\n\n'
            'Введите новые цены в формате:\n'
            '<code>{current_prices}</code>\n\n'
            '(дней:цена_в_копейках, через запятую)',
        ).format(prices_display=prices_display, current_prices=current_prices),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    prices = _parse_period_prices(message.text.strip())
    if not prices:
        await message.answer(
            texts.t(
                'ADMIN_TARIFF_PRICES_PARSE_ERROR_EDIT',
                'Не удалось распознать цены.\nФормат: <code>дней:цена</code>\nПример: <code>30:9900, 90:24900</code>',
            ),
            parse_mode='HTML',
        )
        return

    tariff = await update_tariff(db, tariff, period_prices=prices)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        texts.t('ADMIN_TARIFF_PRICES_UPDATED_SUCCESS', '✅ Цены изменены!\n\n')
        + format_tariff_info(tariff, db_user.language, subs_count),
        reply_markup=get_tariff_view_keyboard(tariff, db_user.language),
        parse_mode='HTML',
    )


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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_device_price)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    device_price = getattr(tariff, 'device_price_kopeks', None)
    if device_price is not None and device_price > 0:
        current_price = _format_price_kopeks(device_price) + texts.t('ADMIN_TARIFF_PER_MONTH_SUFFIX', '/мес')
    else:
        current_price = texts.t(
            'ADMIN_TARIFF_DEVICE_PRICE_NOT_AVAILABLE',
            'Недоступно (докупка устройств запрещена)',
        )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_DEVICE_PRICE_EDIT_PROMPT',
            '📱💰 <b>Редактирование цены за устройство</b>\n\n'
            'Текущая цена: <b>{current_price}</b>\n\n'
            'Введите цену в копейках за одно устройство в месяц.\n\n'
            '• <code>0</code> или <code>-</code> — докупка устройств недоступна\n'
            '• Например: <code>5000</code> = 50₽/мес за устройство',
        ).format(current_price=current_price),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
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
                texts.t(
                    'ADMIN_TARIFF_DEVICE_PRICE_INVALID_INPUT',
                    'Введите корректное число (0 или больше).\n'
                    'Для отключения докупки введите <code>0</code> или <code>-</code>',
                ),
                parse_mode='HTML',
            )
            return

    tariff = await update_tariff(db, tariff, device_price_kopeks=device_price)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        texts.t('ADMIN_TARIFF_DEVICE_PRICE_UPDATED_SUCCESS', '✅ Цена за устройство изменена!\n\n')
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_max_devices)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    max_devices = getattr(tariff, 'max_device_limit', None)
    if max_devices is not None and max_devices > 0:
        current_max = str(max_devices)
    else:
        current_max = texts.t('ADMIN_TARIFF_MAX_DEVICES_UNLIMITED', '∞ (без лимита)')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_MAX_DEVICES_EDIT_PROMPT',
            '📱🔒 <b>Редактирование макс. устройств</b>\n\n'
            'Текущее значение: <b>{current_max}</b>\n'
            'Базовое кол-во устройств: <b>{device_limit}</b>\n\n'
            'Введите максимальное количество устройств, которое пользователь может докупить.\n\n'
            '• <code>0</code> или <code>-</code> — без ограничений\n'
            '• Например: <code>5</code> = максимум 5 устройств на тарифе',
        ).format(current_max=current_max, device_limit=tariff.device_limit),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
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
                texts.t(
                    'ADMIN_TARIFF_MAX_DEVICES_INVALID_INPUT',
                    'Введите корректное число (1 или больше).\n'
                    'Для снятия ограничения введите <code>0</code> или <code>-</code>',
                ),
                parse_mode='HTML',
            )
            return

    tariff = await update_tariff(db, tariff, max_device_limit=max_devices)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        texts.t('ADMIN_TARIFF_MAX_DEVICES_UPDATED_SUCCESS', '✅ Макс. устройств изменено!\n\n')
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_trial_days)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    trial_days = getattr(tariff, 'trial_duration_days', None)
    if trial_days:
        current_days = texts.t('ADMIN_TARIFF_TRIAL_DAYS_VALUE', '{days} дней').format(days=trial_days)
    else:
        current_days = texts.t(
            'ADMIN_TARIFF_TRIAL_DAYS_DEFAULT',
            'По умолчанию ({days} дней)',
        ).format(days=settings.TRIAL_DURATION_DAYS)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_TRIAL_DAYS_EDIT_PROMPT',
            '⏰ <b>Редактирование дней триала</b>\n\n'
            'Текущее значение: <b>{current_days}</b>\n\n'
            'Введите количество дней триала.\n\n'
            '• <code>0</code> или <code>-</code> — использовать настройку по умолчанию ({default_days} дней)\n'
            '• Например: <code>7</code> = 7 дней триала',
        ).format(current_days=current_days, default_days=settings.TRIAL_DURATION_DAYS),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
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
                texts.t(
                    'ADMIN_TARIFF_TRIAL_DAYS_INVALID_INPUT',
                    'Введите корректное число дней (1 или больше).\n'
                    'Для использования настройки по умолчанию введите <code>0</code> или <code>-</code>',
                ),
                parse_mode='HTML',
            )
            return

    tariff = await update_tariff(db, tariff, trial_duration_days=trial_days)
    await state.clear()

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    await message.answer(
        texts.t('ADMIN_TARIFF_TRIAL_DAYS_UPDATED_SUCCESS', '✅ Дни триала изменены!\n\n')
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    # Проверяем, безлимитный ли тариф
    if tariff.is_unlimited_traffic:
        await callback.answer(
            texts.t('ADMIN_TARIFF_TOPUP_UNAVAILABLE_UNLIMITED', 'Докупка недоступна для безлимитного тарифа'),
            show_alert=True,
        )
        return

    is_enabled = getattr(tariff, 'traffic_topup_enabled', False)
    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    max_topup_traffic = getattr(tariff, 'max_topup_traffic_gb', 0) or 0

    # Форматируем текущие настройки
    if is_enabled:
        status = texts.t('ADMIN_TARIFF_TRAFFIC_TOPUP_ENABLED', '✅ Включено')
        if packages:
            gb_unit = texts.t('ADMIN_TARIFF_GB_UNIT', 'ГБ')
            packages_display = '\n'.join(
                f'  • {gb} {gb_unit}: {_format_price_kopeks(price)}' for gb, price in sorted(packages.items())
            )
        else:
            packages_display = texts.t('ADMIN_TARIFF_TOPUP_PACKAGES_NOT_CONFIGURED', '  Пакеты не настроены')
    else:
        status = texts.t('ADMIN_TARIFF_TRAFFIC_TOPUP_DISABLED', '❌ Отключено')
        packages_display = texts.t('ADMIN_TARIFF_TOPUP_PACKAGES_EMPTY', '  -')

    # Форматируем лимит
    if max_topup_traffic > 0:
        max_limit_display = f'{max_topup_traffic} {texts.t("ADMIN_TARIFF_GB_UNIT", "ГБ")}'
    else:
        max_limit_display = texts.t('ADMIN_TARIFF_TOPUP_NO_LIMIT', 'Без ограничений')

    buttons = []

    # Переключение вкл/выкл
    if is_enabled:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_DISABLE', '❌ Отключить'),
                    callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
                )
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_ENABLE', '✅ Включить'),
                    callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
                )
            ]
        )

    # Редактирование пакетов и лимита (только если включено)
    if is_enabled:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_CONFIGURE_PACKAGES', '📦 Настроить пакеты'),
                    callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}',
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_MAX_LIMIT', '📊 Макс. лимит трафика'),
                    callback_data=f'admin_tariff_edit_max_topup:{tariff_id}',
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_TOPUP_MENU_TEXT',
            '📈 <b>Докупка трафика для «{name}»</b>\n\n'
            'Статус: {status}\n\n'
            '<b>Пакеты:</b>\n{packages}\n\n'
            '<b>Макс. лимит:</b> {max_limit}\n\n'
            'Пользователи смогут докупать трафик по заданным ценам.',
        ).format(
            name=tariff.name,
            status=status,
            packages=packages_display,
            max_limit=max_limit_display,
        ),
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
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    is_enabled = getattr(tariff, 'traffic_topup_enabled', False)
    new_value = not is_enabled

    tariff = await update_tariff(db, tariff, traffic_topup_enabled=new_value)

    status_text = (
        texts.t('ADMIN_TARIFF_TOPUP_STATUS_ON', 'включена')
        if new_value
        else texts.t('ADMIN_TARIFF_TOPUP_STATUS_OFF', 'отключена')
    )
    await callback.answer(texts.t('ADMIN_TARIFF_TOPUP_TOGGLED', 'Докупка трафика {status}').format(status=status_text))

    # Перерисовываем меню
    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    max_topup_traffic = getattr(tariff, 'max_topup_traffic_gb', 0) or 0

    if new_value:
        status = texts.t('ADMIN_TARIFF_TRAFFIC_TOPUP_ENABLED', '✅ Включено')
        if packages:
            gb_unit = texts.t('ADMIN_TARIFF_GB_UNIT', 'ГБ')
            packages_display = '\n'.join(
                f'  • {gb} {gb_unit}: {_format_price_kopeks(price)}' for gb, price in sorted(packages.items())
            )
        else:
            packages_display = texts.t('ADMIN_TARIFF_TOPUP_PACKAGES_NOT_CONFIGURED', '  Пакеты не настроены')
    else:
        status = texts.t('ADMIN_TARIFF_TRAFFIC_TOPUP_DISABLED', '❌ Отключено')
        packages_display = texts.t('ADMIN_TARIFF_TOPUP_PACKAGES_EMPTY', '  -')

    # Форматируем лимит
    if max_topup_traffic > 0:
        max_limit_display = f'{max_topup_traffic} {texts.t("ADMIN_TARIFF_GB_UNIT", "ГБ")}'
    else:
        max_limit_display = texts.t('ADMIN_TARIFF_TOPUP_NO_LIMIT', 'Без ограничений')

    buttons = []

    if new_value:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_DISABLE', '❌ Отключить'),
                    callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_CONFIGURE_PACKAGES', '📦 Настроить пакеты'),
                    callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}',
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_MAX_LIMIT', '📊 Макс. лимит трафика'),
                    callback_data=f'admin_tariff_edit_max_topup:{tariff_id}',
                )
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_ENABLE', '✅ Включить'),
                    callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_TARIFF_TOPUP_MENU_TEXT',
                '📈 <b>Докупка трафика для «{name}»</b>\n\n'
                'Статус: {status}\n\n'
                '<b>Пакеты:</b>\n{packages}\n\n'
                '<b>Макс. лимит:</b> {max_limit}\n\n'
                'Пользователи смогут докупать трафик по заданным ценам.',
            ).format(
                name=tariff.name,
                status=status,
                packages=packages_display,
                max_limit=max_limit_display,
            ),
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_traffic_topup_packages)
    await state.update_data(tariff_id=tariff_id, language=db_user.language)

    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    current_packages = _format_traffic_topup_packages_for_edit(packages)

    if packages:
        gb_unit = texts.t('ADMIN_TARIFF_GB_UNIT', 'ГБ')
        packages_display = '\n'.join(
            f'  • {gb} {gb_unit}: {_format_price_kopeks(price)}' for gb, price in sorted(packages.items())
        )
    else:
        packages_display = texts.t('ADMIN_TARIFF_TOPUP_PACKAGES_NOT_CONFIGURED_SHORT', '  Не настроены')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_TOPUP_PACKAGES_EDIT_PROMPT',
            '📦 <b>Настройка пакетов докупки трафика</b>\n\n'
            'Тариф: <b>{name}</b>\n\n'
            '<b>Текущие пакеты:</b>\n{packages_display}\n\n'
            'Введите пакеты в формате:\n'
            '<code>{current_packages}</code>\n\n'
            '(ГБ:цена_в_копейках, через запятую)\n'
            'Например: <code>5:5000, 10:9000</code> = 5ГБ за 50₽, 10ГБ за 90₽',
        ).format(
            name=tariff.name,
            packages_display=packages_display,
            current_packages=current_packages,
        ),
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
        await state.clear()
        return

    if not message.text:
        await message.answer(
            texts.t(
                'ADMIN_TARIFF_TOPUP_PACKAGES_TEXT_REQUIRED',
                'Пожалуйста, отправьте текстовое сообщение.\n\n'
                'Формат: <code>ГБ:цена_в_копейках</code>\n'
                'Пример: <code>5:5000, 10:9000, 20:15000</code>',
            ),
            parse_mode='HTML',
        )
        return

    packages = _parse_traffic_topup_packages(message.text.strip())

    if not packages:
        await message.answer(
            texts.t(
                'ADMIN_TARIFF_TOPUP_PACKAGES_PARSE_ERROR',
                'Не удалось распознать пакеты.\n\n'
                'Формат: <code>ГБ:цена_в_копейках</code>\n'
                'Пример: <code>5:5000, 10:9000, 20:15000</code>',
            ),
            parse_mode='HTML',
        )
        return

    # Преобразуем в формат для JSON (строковые ключи)
    packages_json = {str(gb): price for gb, price in packages.items()}

    tariff = await update_tariff(db, tariff, traffic_topup_packages=packages_json)
    await state.clear()

    # Показываем обновленное меню
    gb_unit = texts.t('ADMIN_TARIFF_GB_UNIT', 'ГБ')
    packages_display = '\n'.join(
        f'  • {gb} {gb_unit}: {_format_price_kopeks(price)}' for gb, price in sorted(packages.items())
    )
    max_topup_traffic = getattr(tariff, 'max_topup_traffic_gb', 0) or 0
    max_limit_display = (
        f'{max_topup_traffic} {gb_unit}'
        if max_topup_traffic > 0
        else texts.t('ADMIN_TARIFF_TOPUP_NO_LIMIT', 'Без ограничений')
    )

    buttons = [
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_DISABLE', '❌ Отключить'),
                callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_CONFIGURE_PACKAGES', '📦 Настроить пакеты'),
                callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_MAX_LIMIT', '📊 Макс. лимит трафика'),
                callback_data=f'admin_tariff_edit_max_topup:{tariff_id}',
            )
        ],
        [InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')],
    ]

    await message.answer(
        texts.t('ADMIN_TARIFF_TOPUP_PACKAGES_UPDATED_SUCCESS', '✅ <b>Пакеты обновлены!</b>\n\n')
        + texts.t(
            'ADMIN_TARIFF_TOPUP_MENU_TEXT',
            '📈 <b>Докупка трафика для «{name}»</b>\n\n'
            'Статус: {status}\n\n'
            '<b>Пакеты:</b>\n{packages}\n\n'
            '<b>Макс. лимит:</b> {max_limit}\n\n'
            'Пользователи смогут докупать трафик по заданным ценам.',
        ).format(
            name=tariff.name,
            status=texts.t('ADMIN_TARIFF_TRAFFIC_TOPUP_ENABLED', '✅ Включено'),
            packages=packages_display,
            max_limit=max_limit_display,
        ),
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_tariff_max_topup_traffic)
    await state.update_data(tariff_id=tariff_id)

    current_limit = getattr(tariff, 'max_topup_traffic_gb', 0) or 0
    if current_limit > 0:
        current_display = f'{current_limit} {texts.t("ADMIN_TARIFF_GB_UNIT", "ГБ")}'
    else:
        current_display = texts.t('ADMIN_TARIFF_TOPUP_NO_LIMIT', 'Без ограничений')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_TOPUP_MAX_LIMIT_EDIT_PROMPT',
            '📊 <b>Максимальный лимит трафика</b>\n\n'
            'Тариф: <b>{name}</b>\n'
            'Текущий лимит: <b>{current_limit}</b>\n\n'
            'Введите максимальный общий объем трафика (в ГБ), который может быть на подписке после всех докупок.\n\n'
            '• Например, если тариф дает 100 ГБ и лимит 200 ГБ — пользователь сможет докупить еще 100 ГБ\n'
            '• Введите <code>0</code> для снятия ограничения',
        ).format(name=tariff.name, current_limit=current_display),
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
        await message.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'))
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
            texts.t(
                'ADMIN_TARIFF_TOPUP_MAX_LIMIT_INVALID_INPUT',
                'Введите целое число (0 или больше).\n\n'
                '• <code>0</code> — без ограничений\n'
                '• <code>200</code> — максимум 200 ГБ на подписке',
            ),
            parse_mode='HTML',
        )
        return

    tariff = await update_tariff(db, tariff, max_topup_traffic_gb=new_limit)
    await state.clear()

    # Показываем обновленное меню
    packages = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
    if packages:
        gb_unit = texts.t('ADMIN_TARIFF_GB_UNIT', 'ГБ')
        packages_display = '\n'.join(
            f'  • {gb} {gb_unit}: {_format_price_kopeks(price)}' for gb, price in sorted(packages.items())
        )
    else:
        gb_unit = texts.t('ADMIN_TARIFF_GB_UNIT', 'ГБ')
        packages_display = texts.t('ADMIN_TARIFF_TOPUP_PACKAGES_NOT_CONFIGURED', '  Пакеты не настроены')

    max_limit_display = (
        f'{new_limit} {gb_unit}'
        if new_limit > 0
        else texts.t('ADMIN_TARIFF_TOPUP_NO_LIMIT', 'Без ограничений')
    )

    buttons = [
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_DISABLE', '❌ Отключить'),
                callback_data=f'admin_tariff_toggle_traffic_topup:{tariff_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_CONFIGURE_PACKAGES', '📦 Настроить пакеты'),
                callback_data=f'admin_tariff_edit_topup_packages:{tariff_id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_TOPUP_BUTTON_MAX_LIMIT', '📊 Макс. лимит трафика'),
                callback_data=f'admin_tariff_edit_max_topup:{tariff_id}',
            )
        ],
        [InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')],
    ]

    await message.answer(
        texts.t('ADMIN_TARIFF_TOPUP_LIMIT_UPDATED_SUCCESS', '✅ <b>Лимит обновлен!</b>\n\n')
        + texts.t(
            'ADMIN_TARIFF_TOPUP_MENU_TEXT',
            '📈 <b>Докупка трафика для «{name}»</b>\n\n'
            'Статус: {status}\n\n'
            '<b>Пакеты:</b>\n{packages}\n\n'
            '<b>Макс. лимит:</b> {max_limit}\n\n'
            'Пользователи смогут докупать трафик по заданным ценам.',
        ).format(
            name=tariff.name,
            status=texts.t('ADMIN_TARIFF_TRAFFIC_TOPUP_ENABLED', '✅ Включено'),
            packages=packages_display,
            max_limit=max_limit_display,
        ),
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
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)

    warning = ''
    if subs_count > 0:
        warning = texts.t(
            'ADMIN_TARIFF_DELETE_WARNING_WITH_SUBS',
            '\n\n⚠️ <b>Внимание!</b> На этом тарифе {subs_count} подписок.\nОни будут отвязаны от тарифа.',
        ).format(subs_count=subs_count)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_DELETE_CONFIRM_TEXT',
            '🗑️ <b>Удаление тарифа</b>\n\nВы действительно хотите удалить тариф <b>{name}</b>?{warning}',
        ).format(name=tariff.name, warning=warning),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_TARIFF_DELETE_CONFIRM_BUTTON', '✅ Да, удалить'),
                        callback_data=f'admin_tariff_delete_confirm:{tariff_id}',
                    ),
                    InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}'),
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    tariff_name = tariff.name
    await delete_tariff(db, tariff)

    await callback.answer(
        texts.t('ADMIN_TARIFF_DELETED_ALERT', 'Тариф «{name}» удален').format(name=tariff_name),
        show_alert=True,
    )

    # Возвращаемся к списку
    tariffs_data = await get_tariffs_with_subscriptions_count(db, include_inactive=True)

    if not tariffs_data:
        await callback.message.edit_text(
            texts.t('ADMIN_TARIFFS_EMPTY_AFTER_DELETE', '📦 <b>Тарифы</b>\n\nТарифы ещё не созданы.'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_TARIFF_CREATE_BUTTON', '➕ Создать тариф'),
                            callback_data='admin_tariff_create',
                        )
                    ],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings')],
                ]
            ),
            parse_mode='HTML',
        )
        return

    total_pages = (len(tariffs_data) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    page_data = tariffs_data[:ITEMS_PER_PAGE]

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFFS_LIST_AFTER_DELETE',
            '📦 <b>Тарифы</b>\n\n✅ Тариф «{name}» удален\n\nВсего: {total}',
        ).format(name=tariff_name, total=len(tariffs_data)),
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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    squads, _ = await get_all_server_squads(db)

    if not squads:
        await callback.answer(texts.t('ADMIN_TARIFF_NO_AVAILABLE_SQUADS', 'Нет доступных серверов'), show_alert=True)
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
                    callback_data=f'admin_tariff_toggle_squad:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_SQUADS_BUTTON_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_squads:{tariff_id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_SQUADS_BUTTON_SELECT_ALL', '✅ Выбрать все'),
                callback_data=f'admin_tariff_select_all_squads:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    selected_count = len(current_squads)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_SQUADS_EDIT_TEXT',
            '🌐 <b>Серверы для тарифа «{name}»</b>\n\n'
            'Выбрано: {selected} из {total}\n\n'
            'Если не выбран ни один сервер - доступны все.\n'
            'Нажмите на сервер для выбора/отмены:',
        ).format(name=tariff.name, selected=selected_count, total=len(squads)),
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
    texts = get_texts(db_user.language)
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    squad_uuid = parts[2]

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    current_squads = set(tariff.allowed_squads or [])

    if squad_uuid in current_squads:
        current_squads.remove(squad_uuid)
    else:
        current_squads.add(squad_uuid)

    tariff = await update_tariff(db, tariff, allowed_squads=list(current_squads))

    # Перерисовываем меню
    squads, _ = await get_all_server_squads(db)

    buttons = []
    for squad in squads:
        is_selected = squad.squad_uuid in current_squads
        prefix = '✅' if is_selected else '⬜'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{prefix} {squad.display_name}',
                    callback_data=f'admin_tariff_toggle_squad:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_SQUADS_BUTTON_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_squads:{tariff_id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_SQUADS_BUTTON_SELECT_ALL', '✅ Выбрать все'),
                callback_data=f'admin_tariff_select_all_squads:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_TARIFF_SQUADS_EDIT_TEXT',
                '🌐 <b>Серверы для тарифа «{name}»</b>\n\n'
                'Выбрано: {selected} из {total}\n\n'
                'Если не выбран ни один сервер - доступны все.\n'
                'Нажмите на сервер для выбора/отмены:',
            ).format(name=tariff.name, selected=len(current_squads), total=len(squads)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@admin_required
@error_handler
async def clear_tariff_squads(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Очищает список серверов тарифа."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    tariff = await update_tariff(db, tariff, allowed_squads=[])
    await callback.answer(texts.t('ADMIN_TARIFF_SQUADS_CLEARED', 'Все серверы очищены'))

    # Перерисовываем меню
    squads, _ = await get_all_server_squads(db)

    buttons = []
    for squad in squads:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'⬜ {squad.display_name}',
                    callback_data=f'admin_tariff_toggle_squad:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_SQUADS_BUTTON_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_squads:{tariff_id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_SQUADS_BUTTON_SELECT_ALL', '✅ Выбрать все'),
                callback_data=f'admin_tariff_select_all_squads:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_TARIFF_SQUADS_EDIT_TEXT',
                '🌐 <b>Серверы для тарифа «{name}»</b>\n\n'
                'Выбрано: {selected} из {total}\n\n'
                'Если не выбран ни один сервер - доступны все.\n'
                'Нажмите на сервер для выбора/отмены:',
            ).format(name=tariff.name, selected=0, total=len(squads)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass


@admin_required
@error_handler
async def select_all_tariff_squads(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Выбирает все серверы для тарифа."""
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    squads, _ = await get_all_server_squads(db)
    all_uuids = [s.squad_uuid for s in squads]

    tariff = await update_tariff(db, tariff, allowed_squads=all_uuids)
    await callback.answer(texts.t('ADMIN_TARIFF_SQUADS_ALL_SELECTED', 'Все серверы выбраны'))

    buttons = []
    for squad in squads:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'✅ {squad.display_name}',
                    callback_data=f'admin_tariff_toggle_squad:{tariff_id}:{squad.squad_uuid}',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_SQUADS_BUTTON_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_squads:{tariff_id}',
            ),
            InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_SQUADS_BUTTON_SELECT_ALL', '✅ Выбрать все'),
                callback_data=f'admin_tariff_select_all_squads:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_TARIFF_SQUADS_EDIT_TEXT',
                '🌐 <b>Серверы для тарифа «{name}»</b>\n\n'
                'Выбрано: {selected} из {total}\n\n'
                'Если не выбран ни один сервер - доступны все.\n'
                'Нажмите на сервер для выбора/отмены:',
            ).format(name=tariff.name, selected=len(squads), total=len(squads)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass


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
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    promo_groups_data = await get_promo_groups_with_counts(db)

    if not promo_groups_data:
        await callback.answer(texts.t('ADMIN_TARIFF_NO_PROMO_GROUPS', 'Нет промогрупп'), show_alert=True)
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
                text=texts.t('ADMIN_TARIFF_PROMO_BUTTON_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_promo:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    selected_count = len(current_groups)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_PROMO_EDIT_TEXT',
            '👥 <b>Промогруппы для тарифа «{name}»</b>\n\n'
            'Выбрано: {selected}\n\n'
            'Если не выбрана ни одна группа - тариф доступен всем.\n'
            'Выберите группы, которым доступен этот тариф:',
        ).format(name=tariff.name, selected=selected_count),
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

    texts = get_texts(db_user.language)
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    promo_group_id = int(parts[2])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
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
                text=texts.t('ADMIN_TARIFF_PROMO_BUTTON_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_promo:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_TARIFF_PROMO_EDIT_TEXT',
                '👥 <b>Промогруппы для тарифа «{name}»</b>\n\n'
                'Выбрано: {selected}\n\n'
                'Если не выбрана ни одна группа - тариф доступен всем.\n'
                'Выберите группы, которым доступен этот тариф:',
            ).format(name=tariff.name, selected=len(current_groups)),
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

    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    await set_tariff_promo_groups(db, tariff, [])
    await callback.answer(texts.t('ADMIN_TARIFF_PROMO_CLEARED', 'Все промогруппы очищены'))

    # Перерисовываем меню
    promo_groups_data = await get_promo_groups_with_counts(db)

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
                text=texts.t('ADMIN_TARIFF_PROMO_BUTTON_CLEAR_ALL', '🔄 Очистить все'),
                callback_data=f'admin_tariff_clear_promo:{tariff_id}',
            ),
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff_id}')])

    try:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_TARIFF_PROMO_EDIT_TEXT',
                '👥 <b>Промогруппы для тарифа «{name}»</b>\n\n'
                'Выбрано: {selected}\n\n'
                'Если не выбрана ни одна группа - тариф доступен всем.\n'
                'Выберите группы, которым доступен этот тариф:',
            ).format(name=tariff.name, selected=0),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass


# ==================== Режим сброса трафика ====================

TRAFFIC_RESET_MODES = ['DAY', 'WEEK', 'MONTH', 'NO_RESET']


def get_traffic_reset_mode_keyboard(tariff_id: int, current_mode: str | None, language: str) -> InlineKeyboardMarkup:
    """Создает клавиатуру для выбора режима сброса трафика."""
    texts = get_texts(language)
    buttons = []

    # Кнопка "Глобальная настройка"
    global_mode_text = texts.t(
        'ADMIN_TARIFF_RESET_MODE_GLOBAL',
        '🌐 Глобальная настройка ({strategy})',
    ).format(strategy=settings.DEFAULT_TRAFFIC_RESET_STRATEGY)
    global_label = f'{"✅ " if current_mode is None else ""}{global_mode_text}'
    buttons.append(
        [InlineKeyboardButton(text=global_label, callback_data=f'admin_tariff_set_reset_mode:{tariff_id}:GLOBAL')]
    )

    # Кнопки для каждого режима
    for mode_value in TRAFFIC_RESET_MODES:
        is_selected = current_mode == mode_value
        mode_label = _format_traffic_reset_mode(mode_value, texts)
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
    texts = get_texts(db_user.language)
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    current_mode = getattr(tariff, 'traffic_reset_mode', None)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_RESET_MODE_EDIT_TEXT',
            '🔄 <b>Режим сброса трафика для тарифа «{name}»</b>\n\n'
            'Текущий режим: {current_mode}\n\n'
            'Выберите, когда сбрасывать использованный трафик у подписчиков этого тарифа:\n\n'
            '• <b>Глобальная настройка</b> — использовать значение из конфига бота\n'
            '• <b>Ежедневно</b> — сброс каждый день\n'
            '• <b>Еженедельно</b> — сброс каждую неделю\n'
            '• <b>Ежемесячно</b> — сброс каждый месяц\n'
            '• <b>Никогда</b> — трафик накапливается за весь период подписки',
        ).format(
            name=tariff.name,
            current_mode=_format_traffic_reset_mode(current_mode, texts),
        ),
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
    texts = get_texts(db_user.language)
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    new_mode = parts[2]

    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND', 'Тариф не найден'), show_alert=True)
        return

    # Преобразуем GLOBAL в None
    if new_mode == 'GLOBAL':
        new_mode = None

    # Обновляем тариф
    tariff = await update_tariff(db, tariff, traffic_reset_mode=new_mode)

    mode_display = _format_traffic_reset_mode(new_mode, texts)
    await callback.answer(
        texts.t('ADMIN_TARIFF_RESET_MODE_CHANGED', 'Режим сброса изменён: {mode}').format(mode=mode_display),
        show_alert=True,
    )

    # Обновляем клавиатуру
    await callback.message.edit_text(
        texts.t(
            'ADMIN_TARIFF_RESET_MODE_EDIT_TEXT',
            '🔄 <b>Режим сброса трафика для тарифа «{name}»</b>\n\n'
            'Текущий режим: {current_mode}\n\n'
            'Выберите, когда сбрасывать использованный трафик у подписчиков этого тарифа:\n\n'
            '• <b>Глобальная настройка</b> — использовать значение из конфига бота\n'
            '• <b>Ежедневно</b> — сброс каждый день\n'
            '• <b>Еженедельно</b> — сброс каждую неделю\n'
            '• <b>Ежемесячно</b> — сброс каждый месяц\n'
            '• <b>Никогда</b> — трафик накапливается за весь период подписки',
        ).format(name=tariff.name, current_mode=mode_display),
        reply_markup=get_traffic_reset_mode_keyboard(tariff_id, new_mode, db_user.language),
        parse_mode='HTML',
    )


def register_handlers(dp: Dispatcher):
    """Регистрирует обработчики для управления тарифами."""
    # Список тарифов
    dp.callback_query.register(show_tariffs_list, F.data == 'admin_tariffs')
    dp.callback_query.register(show_tariffs_page, F.data.startswith('admin_tariffs_page:'))

    # Просмотр и переключение
    dp.callback_query.register(view_tariff, F.data.startswith('admin_tariff_view:'))
    dp.callback_query.register(
        toggle_tariff, F.data.startswith('admin_tariff_toggle:') & ~F.data.startswith('admin_tariff_toggle_trial:')
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

    # Редактирование трафика
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

    # Редактирование докупки трафика
    dp.callback_query.register(start_edit_tariff_traffic_topup, F.data.startswith('admin_tariff_edit_traffic_topup:'))
    dp.callback_query.register(toggle_tariff_traffic_topup, F.data.startswith('admin_tariff_toggle_traffic_topup:'))
    dp.callback_query.register(
        start_edit_traffic_topup_packages, F.data.startswith('admin_tariff_edit_topup_packages:')
    )
    dp.message.register(process_edit_traffic_topup_packages, AdminStates.editing_tariff_traffic_topup_packages)

    # Редактирование макс. лимита докупки трафика
    dp.callback_query.register(start_edit_max_topup_traffic, F.data.startswith('admin_tariff_edit_max_topup:'))
    dp.message.register(process_edit_max_topup_traffic, AdminStates.editing_tariff_max_topup_traffic)

    # Удаление
    dp.callback_query.register(confirm_delete_tariff, F.data.startswith('admin_tariff_delete:'))
    dp.callback_query.register(delete_tariff_confirmed, F.data.startswith('admin_tariff_delete_confirm:'))

    # Редактирование серверов
    dp.callback_query.register(start_edit_tariff_squads, F.data.startswith('admin_tariff_edit_squads:'))
    dp.callback_query.register(toggle_tariff_squad, F.data.startswith('admin_tariff_toggle_squad:'))
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
