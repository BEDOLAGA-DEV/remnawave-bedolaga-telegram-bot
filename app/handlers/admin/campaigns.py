import html
import re

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.campaign import (
    create_campaign,
    delete_campaign,
    get_campaign_by_id,
    get_campaign_by_start_parameter,
    get_campaign_statistics,
    get_campaigns_count,
    get_campaigns_list,
    get_campaigns_overview,
    update_campaign,
)
from app.database.crud.server_squad import get_all_server_squads, get_server_squad_by_id
from app.database.crud.tariff import get_all_tariffs, get_tariff_by_id
from app.database.models import User
from app.keyboards.admin import (
    get_admin_campaigns_keyboard,
    get_admin_pagination_keyboard,
    get_campaign_bonus_type_keyboard,
    get_campaign_edit_keyboard,
    get_campaign_management_keyboard,
    get_confirmation_keyboard,
)
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

_CAMPAIGN_PARAM_REGEX = re.compile(r'^[A-Za-z0-9_-]{3,32}$')
_CAMPAIGNS_PAGE_SIZE = 5


def _format_campaign_summary(campaign, texts) -> str:
    status = '🟢 Активна' if campaign.is_active else '⚪️ Выключена'

    if campaign.is_balance_bonus:
        bonus_text = texts.format_price(campaign.balance_bonus_kopeks)
        bonus_info = f'💰 Бонус на баланс: <b>{bonus_text}</b>'
    elif campaign.is_subscription_bonus:
        traffic_text = texts.format_traffic(campaign.subscription_traffic_gb or 0)
        device_limit = campaign.subscription_device_limit
        if device_limit is None:
            device_limit = settings.DEFAULT_DEVICE_LIMIT
        bonus_info = (
            f'📱 Пробная подписка: <b>{campaign.subscription_duration_days or 0} д.</b>\n'
            f'🌐 Трафик: <b>{traffic_text}</b>\n'
            f'📱 Устройства: <b>{device_limit}</b>'
        )
    elif campaign.is_tariff_bonus:
        tariff_name = 'Не выбран'
        if hasattr(campaign, 'tariff') and campaign.tariff:
            tariff_name = campaign.tariff.name
        bonus_info = f'🎁 Тариф: <b>{tariff_name}</b>\n📅 Длительность: <b>{campaign.tariff_duration_days or 0} д.</b>'
    elif campaign.is_none_bonus:
        bonus_info = '🔗 Только ссылка (без награды)'
    else:
        bonus_info = '❓ Неизвестный тип бонуса'

    return (
        f'<b>{html.escape(campaign.name)}</b>\n'
        f'Стартовый параметр: <code>{html.escape(campaign.start_parameter)}</code>\n'
        f'Статус: {status}\n'
        f'{bonus_info}\n'
    )


async def _get_bot_deep_link(callback: types.CallbackQuery, start_parameter: str) -> str:
    bot = await callback.bot.get_me()
    return f'https://t.me/{bot.username}?start={start_parameter}'


async def _get_bot_deep_link_from_message(message: types.Message, start_parameter: str) -> str:
    bot = await message.bot.get_me()
    return f'https://t.me/{bot.username}?start={start_parameter}'


def _build_campaign_servers_keyboard(
    servers,
    selected_uuids: list[str],
    *,
    toggle_prefix: str = 'campaign_toggle_server_',
    save_callback: str = 'campaign_servers_save',
    back_callback: str = 'admin_campaigns',
) -> types.InlineKeyboardMarkup:
    keyboard: list[list[types.InlineKeyboardButton]] = []

    for server in servers[:20]:
        is_selected = server.squad_uuid in selected_uuids
        emoji = '✅' if is_selected else ('⚪' if server.is_available else '🔒')
        text = f'{emoji} {server.display_name}'
        keyboard.append([types.InlineKeyboardButton(text=text, callback_data=f'{toggle_prefix}{server.id}')])

    keyboard.append(
        [
            types.InlineKeyboardButton(text='✅ Сохранить', callback_data=save_callback),
            types.InlineKeyboardButton(text='⬅️ Назад', callback_data=back_callback),
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _render_campaign_edit_menu(
    bot: Bot,
    chat_id: int,
    message_id: int,
    campaign,
    language: str,
    *,
    use_caption: bool = False,
):
    texts = get_texts(language)
    text = f'✏️ <b>Редактирование кампании</b>\n\n{_format_campaign_summary(campaign, texts)}\nВыберите, что изменить:'

    edit_kwargs = dict(
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=get_campaign_edit_keyboard(
            campaign.id,
            bonus_type=campaign.bonus_type,
            language=language,
        ),
        parse_mode='HTML',
    )

    if use_caption:
        await bot.edit_message_caption(
            caption=text,
            **edit_kwargs,
        )
    else:
        await bot.edit_message_text(
            text=text,
            **edit_kwargs,
        )


@admin_required
@error_handler
async def show_campaigns_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    overview = await get_campaigns_overview(db)

    text = (
        '📣 <b>Рекламные кампании</b>\n\n'
        f'Всего кампаний: <b>{overview["total"]}</b>\n'
        f'Активных: <b>{overview["active"]}</b> | Выключены: <b>{overview["inactive"]}</b>\n'
        f'Регистраций: <b>{overview["registrations"]}</b>\n'
        f'Выдано баланса: <b>{texts.format_price(overview["balance_total"])}</b>\n'
        f'Выдано подписок: <b>{overview["subscription_total"]}</b>'
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_admin_campaigns_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_campaigns_overall_stats(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    overview = await get_campaigns_overview(db)

    text = ['📊 <b>Общая статистика кампаний</b>\n']
    text.append(f'Всего кампаний: <b>{overview["total"]}</b>')
    text.append(f'Активны: <b>{overview["active"]}</b>, выключены: <b>{overview["inactive"]}</b>')
    text.append(f'Всего регистраций: <b>{overview["registrations"]}</b>')
    text.append(f'Суммарно выдано баланса: <b>{texts.format_price(overview["balance_total"])}</b>')
    text.append(f'Выдано подписок: <b>{overview["subscription_total"]}</b>')

    await callback.message.edit_text(
        '\n'.join(text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_campaigns')]]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_campaigns_list(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    page = 1
    if callback.data.startswith('admin_campaigns_list_page_'):
        try:
            page = int(callback.data.split('_')[-1])
        except ValueError:
            page = 1

    offset = (page - 1) * _CAMPAIGNS_PAGE_SIZE
    campaigns = await get_campaigns_list(
        db,
        offset=offset,
        limit=_CAMPAIGNS_PAGE_SIZE,
    )
    total = await get_campaigns_count(db)
    total_pages = max(1, (total + _CAMPAIGNS_PAGE_SIZE - 1) // _CAMPAIGNS_PAGE_SIZE)

    if not campaigns:
        await callback.message.edit_text(
            '❌ Рекламные кампании не найдены.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='➕ Создать', callback_data='admin_campaigns_create')],
                    [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_campaigns')],
                ]
            ),
        )
        await callback.answer()
        return

    text_lines = ['📋 <b>Список кампаний</b>\n']

    for campaign in campaigns:
        # Access from instance dict to avoid MissingGreenlet on lazy load
        regs = sa_inspect(campaign).dict.get('registrations', []) or []
        registrations = len(regs)
        total_balance = sum(r.balance_bonus_kopeks or 0 for r in regs)
        status = '🟢' if campaign.is_active else '⚪'
        line = (
            f'{status} <b>{html.escape(campaign.name)}</b> — <code>{html.escape(campaign.start_parameter)}</code>\n'
            f'   Регистраций: {registrations}, баланс: {texts.format_price(total_balance)}'
        )
        if campaign.is_subscription_bonus:
            line += f', подписка: {campaign.subscription_duration_days or 0} д.'
        else:
            line += ', бонус: баланс'
        text_lines.append(line)

    keyboard_rows = [
        [
            types.InlineKeyboardButton(
                text=f'🔍 {campaign.name}',
                callback_data=f'admin_campaign_manage_{campaign.id}',
            )
        ]
        for campaign in campaigns
    ]

    pagination = get_admin_pagination_keyboard(
        current_page=page,
        total_pages=total_pages,
        callback_prefix='admin_campaigns_list',
        back_callback='admin_campaigns',
        language=db_user.language,
    )

    keyboard_rows.extend(pagination.inline_keyboard)

    await callback.message.edit_text(
        '\n'.join(text_lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_campaign_detail(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)

    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    texts = get_texts(db_user.language)
    stats = await get_campaign_statistics(db, campaign_id)
    deep_link = await _get_bot_deep_link(callback, campaign.start_parameter)

    text = ['📣 <b>Управление кампанией</b>\n']
    text.append(_format_campaign_summary(campaign, texts))
    text.append(f'🔗 Ссылка: <code>{deep_link}</code>')
    text.append('\n📊 <b>Статистика</b>')
    text.append(f'• Регистраций: <b>{stats["registrations"]}</b>')
    text.append(f'• Выдано баланса: <b>{texts.format_price(stats["balance_issued"])}</b>')
    text.append(f'• Выдано подписок: <b>{stats["subscription_issued"]}</b>')
    text.append(f'• Доход: <b>{texts.format_price(stats["total_revenue_kopeks"])}</b>')
    text.append(f'• Получили триал: <b>{stats["trial_users_count"]}</b> (активно: {stats["active_trials_count"]})')
    text.append(
        '• Конверсий в оплату: '
        f'<b>{stats["conversion_count"]}</b>'
        f' / пользователей с оплатой: {stats["paid_users_count"]}'
    )
    text.append(f'• Конверсия в оплату: <b>{stats["conversion_rate"]:.1f}%</b>')
    text.append(f'• Конверсия триала: <b>{stats["trial_conversion_rate"]:.1f}%</b>')
    text.append(f'• Средний доход на пользователя: <b>{texts.format_price(stats["avg_revenue_per_user_kopeks"])}</b>')
    text.append(f'• Средний первый платеж: <b>{texts.format_price(stats["avg_first_payment_kopeks"])}</b>')
    if stats['last_registration']:
        text.append(f'• Последняя: {stats["last_registration"].strftime("%d.%m.%Y %H:%M")}')

    await callback.message.edit_text(
        '\n'.join(text),
        reply_markup=get_campaign_management_keyboard(campaign.id, campaign.is_active, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_campaign_edit_menu(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)

    if not campaign:
        await state.clear()
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    await state.clear()

    use_caption = bool(callback.message.caption) and not bool(callback.message.text)

    await _render_campaign_edit_menu(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        campaign,
        db_user.language,
        use_caption=use_caption,
    )
    await callback.answer()


@admin_required
@error_handler
async def start_edit_campaign_name(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_name)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    await callback.message.edit_text(
        (
            '✏️ <b>Изменение названия кампании</b>\n\n'
            f'Текущее название: <b>{html.escape(campaign.name)}</b>\n'
            'Введите новое название (3-100 символов):'
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='❌ Отмена',
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_name(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer('❌ Сессия редактирования устарела. Попробуйте снова.')
        await state.clear()
        return

    new_name = message.text.strip()
    if len(new_name) < 3 or len(new_name) > 100:
        await message.answer('❌ Название должно содержать от 3 до 100 символов. Попробуйте снова.')
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer('❌ Кампания не найдена')
        await state.clear()
        return

    await update_campaign(db, campaign, name=new_name)
    await state.clear()

    await message.answer('✅ Название обновлено.')

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


@admin_required
@error_handler
async def start_edit_campaign_start_parameter(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_start)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    await callback.message.edit_text(
        (
            '🔗 <b>Изменение стартового параметра</b>\n\n'
            f'Текущий параметр: <code>{campaign.start_parameter}</code>\n'
            'Введите новый параметр (латинские буквы, цифры, - или _, 3-32 символа):'
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='❌ Отмена',
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_start_parameter(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer('❌ Сессия редактирования устарела. Попробуйте снова.')
        await state.clear()
        return

    new_param = message.text.strip()
    if not _CAMPAIGN_PARAM_REGEX.match(new_param):
        await message.answer('❌ Разрешены только латинские буквы, цифры, символы - и _. Длина 3-32 символа.')
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer('❌ Кампания не найдена')
        await state.clear()
        return

    existing = await get_campaign_by_start_parameter(db, new_param)
    if existing and existing.id != campaign_id:
        await message.answer('❌ Такой параметр уже используется. Введите другой вариант.')
        return

    await update_campaign(db, campaign, start_parameter=new_param)
    await state.clear()

    await message.answer('✅ Стартовый параметр обновлен.')

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


@admin_required
@error_handler
async def start_edit_campaign_balance_bonus(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    if not campaign.is_balance_bonus:
        await callback.answer('❌ У кампании другой тип бонуса', show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_balance)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    await callback.message.edit_text(
        (
            '💰 <b>Изменение бонуса на баланс</b>\n\n'
            f'Текущий бонус: <b>{get_texts(db_user.language).format_price(campaign.balance_bonus_kopeks)}</b>\n'
            'Введите новую сумму в рублях (например, 100 или 99.5):'
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='❌ Отмена',
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_balance_bonus(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer('❌ Сессия редактирования устарела. Попробуйте снова.')
        await state.clear()
        return

    try:
        amount_rubles = float(message.text.replace(',', '.'))
    except ValueError:
        await message.answer('❌ Введите корректную сумму (например, 100 или 99.5)')
        return

    if amount_rubles <= 0:
        await message.answer('❌ Сумма должна быть больше нуля')
        return

    amount_kopeks = int(round(amount_rubles * 100))

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer('❌ Кампания не найдена')
        await state.clear()
        return

    if not campaign.is_balance_bonus:
        await message.answer('❌ У кампании другой тип бонуса')
        await state.clear()
        return

    await update_campaign(db, campaign, balance_bonus_kopeks=amount_kopeks)
    await state.clear()

    await message.answer('✅ Бонус обновлен.')

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


async def _ensure_subscription_campaign(message_or_callback, campaign) -> bool:
    if campaign.is_balance_bonus:
        if isinstance(message_or_callback, types.CallbackQuery):
            await message_or_callback.answer(
                '❌ Для этой кампании доступен только бонус на баланс',
                show_alert=True,
            )
        else:
            await message_or_callback.answer('❌ Для этой кампании нельзя изменить параметры подписки')
        return False
    return True


@admin_required
@error_handler
async def start_edit_campaign_subscription_days(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign):
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_subscription_days)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    await callback.message.edit_text(
        (
            '📅 <b>Изменение длительности подписки</b>\n\n'
            f'Текущее значение: <b>{campaign.subscription_duration_days or 0} д.</b>\n'
            'Введите новое количество дней (1-730):'
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='❌ Отмена',
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_subscription_days(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer('❌ Сессия редактирования устарела. Попробуйте снова.')
        await state.clear()
        return

    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer('❌ Введите число дней (1-730)')
        return

    if days <= 0 or days > 730:
        await message.answer('❌ Длительность должна быть от 1 до 730 дней')
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer('❌ Кампания не найдена')
        await state.clear()
        return

    if not await _ensure_subscription_campaign(message, campaign):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_duration_days=days)
    await state.clear()

    await message.answer('✅ Длительность подписки обновлена.')

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


@admin_required
@error_handler
async def start_edit_campaign_subscription_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign):
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_subscription_traffic)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    current_traffic = campaign.subscription_traffic_gb or 0
    traffic_text = 'безлимит' if current_traffic == 0 else f'{current_traffic} ГБ'

    await callback.message.edit_text(
        (
            '🌐 <b>Изменение лимита трафика</b>\n\n'
            f'Текущее значение: <b>{traffic_text}</b>\n'
            'Введите новый лимит в ГБ (0 = безлимит, максимум 10000):'
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='❌ Отмена',
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_subscription_traffic(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer('❌ Сессия редактирования устарела. Попробуйте снова.')
        await state.clear()
        return

    try:
        traffic = int(message.text.strip())
    except ValueError:
        await message.answer('❌ Введите целое число (0 или больше)')
        return

    if traffic < 0 or traffic > 10000:
        await message.answer('❌ Лимит трафика должен быть от 0 до 10000 ГБ')
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer('❌ Кампания не найдена')
        await state.clear()
        return

    if not await _ensure_subscription_campaign(message, campaign):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_traffic_gb=traffic)
    await state.clear()

    await message.answer('✅ Лимит трафика обновлен.')

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


@admin_required
@error_handler
async def start_edit_campaign_subscription_devices(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign):
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_subscription_devices)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_edit_message_is_caption=is_caption,
    )

    current_devices = campaign.subscription_device_limit
    if current_devices is None:
        current_devices = settings.DEFAULT_DEVICE_LIMIT

    await callback.message.edit_text(
        (
            '📱 <b>Изменение лимита устройств</b>\n\n'
            f'Текущее значение: <b>{current_devices}</b>\n'
            f'Введите новое количество (1-{settings.MAX_DEVICES_LIMIT}):'
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='❌ Отмена',
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_subscription_devices(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer('❌ Сессия редактирования устарела. Попробуйте снова.')
        await state.clear()
        return

    try:
        devices = int(message.text.strip())
    except ValueError:
        await message.answer('❌ Введите целое число устройств')
        return

    if devices < 1 or devices > settings.MAX_DEVICES_LIMIT:
        await message.answer(f'❌ Количество устройств должно быть от 1 до {settings.MAX_DEVICES_LIMIT}')
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer('❌ Кампания не найдена')
        await state.clear()
        return

    if not await _ensure_subscription_campaign(message, campaign):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_device_limit=devices)
    await state.clear()

    await message.answer('✅ Лимит устройств обновлен.')

    edit_message_id = data.get('campaign_edit_message_id')
    edit_message_is_caption = data.get('campaign_edit_message_is_caption', False)
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
            use_caption=edit_message_is_caption,
        )


@admin_required
@error_handler
async def start_edit_campaign_subscription_servers(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign):
        return

    servers, _ = await get_all_server_squads(db, available_only=False)
    if not servers:
        await callback.answer(
            '❌ Не найдены доступные серверы. Добавьте серверы перед изменением.',
            show_alert=True,
        )
        return

    selected = list(campaign.subscription_squads or [])

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_subscription_servers)
    is_caption = bool(callback.message.caption) and not bool(callback.message.text)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
        campaign_subscription_squads=selected,
        campaign_edit_message_is_caption=is_caption,
    )

    keyboard = _build_campaign_servers_keyboard(
        servers,
        selected,
        toggle_prefix=f'campaign_edit_toggle_{campaign_id}_',
        save_callback=f'campaign_edit_servers_save_{campaign_id}',
        back_callback=f'admin_campaign_edit_{campaign_id}',
    )

    await callback.message.edit_text(
        (
            '🌍 <b>Редактирование доступных серверов</b>\n\n'
            'Нажмите на сервер, чтобы добавить или убрать его из кампании.\n'
            'После выбора нажмите "✅ Сохранить".'
        ),
        reply_markup=keyboard,
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_edit_campaign_server(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    parts = callback.data.split('_')
    try:
        server_id = int(parts[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Не удалось определить сервер', show_alert=True)
        return

    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await callback.answer('❌ Сессия редактирования устарела', show_alert=True)
        await state.clear()
        return

    server = await get_server_squad_by_id(db, server_id)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return

    selected = list(data.get('campaign_subscription_squads', []))

    if server.squad_uuid in selected:
        selected.remove(server.squad_uuid)
    else:
        selected.append(server.squad_uuid)

    await state.update_data(campaign_subscription_squads=selected)

    servers, _ = await get_all_server_squads(db, available_only=False)
    keyboard = _build_campaign_servers_keyboard(
        servers,
        selected,
        toggle_prefix=f'campaign_edit_toggle_{campaign_id}_',
        save_callback=f'campaign_edit_servers_save_{campaign_id}',
        back_callback=f'admin_campaign_edit_{campaign_id}',
    )

    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def save_edit_campaign_subscription_servers(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await callback.answer('❌ Сессия редактирования устарела', show_alert=True)
        await state.clear()
        return

    selected = list(data.get('campaign_subscription_squads', []))
    if not selected:
        await callback.answer('❗ Выберите хотя бы один сервер', show_alert=True)
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await state.clear()
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_squads=selected)
    await state.clear()

    use_caption = bool(callback.message.caption) and not bool(callback.message.text)

    await _render_campaign_edit_menu(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        campaign,
        db_user.language,
        use_caption=use_caption,
    )
    await callback.answer('✅ Сохранено')


@admin_required
@error_handler
async def toggle_campaign_status(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    new_status = not campaign.is_active
    await update_campaign(db, campaign, is_active=new_status)
    status_text = 'включена' if new_status else 'выключена'
    logger.info('🔄 Кампания переключена', campaign_id=campaign_id, status_text=status_text)

    await show_campaign_detail(callback, db_user, db)


@admin_required
@error_handler
async def show_campaign_stats(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    texts = get_texts(db_user.language)
    stats = await get_campaign_statistics(db, campaign_id)

    text = ['📊 <b>Статистика кампании</b>\n']
    text.append(_format_campaign_summary(campaign, texts))
    text.append(f'Регистраций: <b>{stats["registrations"]}</b>')
    text.append(f'Выдано баланса: <b>{texts.format_price(stats["balance_issued"])}</b>')
    text.append(f'Выдано подписок: <b>{stats["subscription_issued"]}</b>')
    if stats['last_registration']:
        text.append(f'Последняя регистрация: {stats["last_registration"].strftime("%d.%m.%Y %H:%M")}')

    await callback.message.edit_text(
        '\n'.join(text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='⬅️ Назад',
                        callback_data=f'admin_campaign_manage_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_delete_campaign(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    text = (
        '🗑️ <b>Удаление кампании</b>\n\n'
        f'Название: <b>{html.escape(campaign.name)}</b>\n'
        f'Параметр: <code>{html.escape(campaign.start_parameter)}</code>\n\n'
        'Вы уверены, что хотите удалить кампанию?'
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_confirmation_keyboard(
            confirm_action=f'admin_campaign_delete_confirm_{campaign_id}',
            cancel_action=f'admin_campaign_manage_{campaign_id}',
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_campaign_confirmed(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    await delete_campaign(db, campaign)
    await callback.message.edit_text(
        '✅ Кампания удалена.',
        reply_markup=get_admin_campaigns_keyboard(db_user.language),
    )
    await callback.answer('Удалено')


@admin_required
@error_handler
async def start_campaign_creation(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    await state.clear()
    await callback.message.edit_text(
        '🆕 <b>Создание рекламной кампании</b>\n\nВведите название кампании:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_campaigns')]]
        ),
    )
    await state.set_state(AdminStates.creating_campaign_name)
    await callback.answer()


@admin_required
@error_handler
async def process_campaign_name(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    name = message.text.strip()
    if len(name) < 3 or len(name) > 100:
        await message.answer('❌ Название должно содержать от 3 до 100 символов. Попробуйте снова.')
        return

    await state.update_data(campaign_name=name)
    await state.set_state(AdminStates.creating_campaign_start)
    await message.answer(
        '🔗 Теперь введите параметр старта (латинские буквы, цифры, - или _):',
    )


@admin_required
@error_handler
async def process_campaign_start_parameter(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    start_param = message.text.strip()
    if not _CAMPAIGN_PARAM_REGEX.match(start_param):
        await message.answer('❌ Разрешены только латинские буквы, цифры, символы - и _. Длина 3-32 символа.')
        return

    existing = await get_campaign_by_start_parameter(db, start_param)
    if existing:
        await message.answer('❌ Кампания с таким параметром уже существует. Введите другой параметр.')
        return

    await state.update_data(campaign_start_parameter=start_param)
    await state.set_state(AdminStates.creating_campaign_bonus)
    await message.answer(
        '🎯 Выберите тип бонуса для кампании:',
        reply_markup=get_campaign_bonus_type_keyboard(db_user.language),
    )


@admin_required
@error_handler
async def select_campaign_bonus_type(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    # Определяем тип бонуса из callback_data
    if callback.data.endswith('balance'):
        bonus_type = 'balance'
    elif callback.data.endswith('subscription'):
        bonus_type = 'subscription'
    elif callback.data.endswith('tariff'):
        bonus_type = 'tariff'
    elif callback.data.endswith('none'):
        bonus_type = 'none'
    else:
        bonus_type = 'balance'

    await state.update_data(campaign_bonus_type=bonus_type)

    if bonus_type == 'balance':
        await state.set_state(AdminStates.creating_campaign_balance)
        await callback.message.edit_text(
            '💰 Введите сумму бонуса на баланс (в рублях):',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_campaigns')]]
            ),
        )
    elif bonus_type == 'subscription':
        await state.set_state(AdminStates.creating_campaign_subscription_days)
        await callback.message.edit_text(
            '📅 Введите длительность пробной подписки в днях (1-730):',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_campaigns')]]
            ),
        )
    elif bonus_type == 'tariff':
        # Показываем выбор тарифа
        tariffs = await get_all_tariffs(db, include_inactive=False)
        if not tariffs:
            await callback.answer(
                '❌ Нет доступных тарифов. Сначала создайте тариф.',
                show_alert=True,
            )
            return

        keyboard = []
        for tariff in tariffs[:15]:  # Максимум 15 тарифов
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'🎁 {tariff.name}',
                        callback_data=f'campaign_select_tariff_{tariff.id}',
                    )
                ]
            )
        keyboard.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_campaigns')])

        await state.set_state(AdminStates.creating_campaign_tariff_select)
        await callback.message.edit_text(
            '🎁 Выберите тариф для выдачи:',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
    elif bonus_type == 'none':
        # Сразу создаём кампанию без бонуса
        data = await state.get_data()
        campaign = await create_campaign(
            db,
            name=data['campaign_name'],
            start_parameter=data['campaign_start_parameter'],
            bonus_type='none',
            created_by=db_user.id,
        )
        await state.clear()

        deep_link = await _get_bot_deep_link(callback, campaign.start_parameter)
        texts = get_texts(db_user.language)
        summary = _format_campaign_summary(campaign, texts)
        text = f'✅ <b>Кампания создана!</b>\n\n{summary}\n🔗 Ссылка: <code>{deep_link}</code>'

        await callback.message.edit_text(
            text,
            reply_markup=get_campaign_management_keyboard(campaign.id, campaign.is_active, db_user.language),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_campaign_balance_value(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    try:
        amount_rubles = float(message.text.replace(',', '.'))
    except ValueError:
        await message.answer('❌ Введите корректную сумму (например, 100 или 99.5)')
        return

    if amount_rubles <= 0:
        await message.answer('❌ Сумма должна быть больше нуля')
        return

    amount_kopeks = int(round(amount_rubles * 100))
    data = await state.get_data()

    campaign = await create_campaign(
        db,
        name=data['campaign_name'],
        start_parameter=data['campaign_start_parameter'],
        bonus_type='balance',
        balance_bonus_kopeks=amount_kopeks,
        created_by=db_user.id,
    )

    await state.clear()

    deep_link = await _get_bot_deep_link_from_message(message, campaign.start_parameter)
    texts = get_texts(db_user.language)
    summary = _format_campaign_summary(campaign, texts)
    text = f'✅ <b>Кампания создана!</b>\n\n{summary}\n🔗 Ссылка: <code>{deep_link}</code>'

    await message.answer(
        text,
        reply_markup=get_campaign_management_keyboard(campaign.id, campaign.is_active, db_user.language),
    )


@admin_required
@error_handler
async def process_campaign_subscription_days(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer('❌ Введите число дней (1-730)')
        return

    if days <= 0 or days > 730:
        await message.answer('❌ Длительность должна быть от 1 до 730 дней')
        return

    await state.update_data(campaign_subscription_days=days)
    await state.set_state(AdminStates.creating_campaign_subscription_traffic)
    await message.answer('🌐 Введите лимит трафика в ГБ (0 = безлимит):')


@admin_required
@error_handler
async def process_campaign_subscription_traffic(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    try:
        traffic = int(message.text.strip())
    except ValueError:
        await message.answer('❌ Введите целое число (0 или больше)')
        return

    if traffic < 0 or traffic > 10000:
        await message.answer('❌ Лимит трафика должен быть от 0 до 10000 ГБ')
        return

    await state.update_data(campaign_subscription_traffic=traffic)
    await state.set_state(AdminStates.creating_campaign_subscription_devices)
    await message.answer(f'📱 Введите количество устройств (1-{settings.MAX_DEVICES_LIMIT}):')


@admin_required
@error_handler
async def process_campaign_subscription_devices(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    try:
        devices = int(message.text.strip())
    except ValueError:
        await message.answer('❌ Введите целое число устройств')
        return

    if devices < 1 or devices > settings.MAX_DEVICES_LIMIT:
        await message.answer(f'❌ Количество устройств должно быть от 1 до {settings.MAX_DEVICES_LIMIT}')
        return

    await state.update_data(campaign_subscription_devices=devices)
    await state.update_data(campaign_subscription_squads=[])
    await state.set_state(AdminStates.creating_campaign_subscription_servers)

    servers, _ = await get_all_server_squads(db, available_only=False)
    if not servers:
        await message.answer(
            '❌ Не найдены доступные серверы. Добавьте сервера перед созданием кампании.',
        )
        await state.clear()
        return

    keyboard = _build_campaign_servers_keyboard(servers, [])
    await message.answer(
        '🌍 Выберите серверы, которые будут доступны по подписке (максимум 20 отображаются).',
        reply_markup=keyboard,
    )


@admin_required
@error_handler
async def toggle_campaign_server(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return

    data = await state.get_data()
    selected = list(data.get('campaign_subscription_squads', []))

    if server.squad_uuid in selected:
        selected.remove(server.squad_uuid)
    else:
        selected.append(server.squad_uuid)

    await state.update_data(campaign_subscription_squads=selected)

    servers, _ = await get_all_server_squads(db, available_only=False)
    keyboard = _build_campaign_servers_keyboard(servers, selected)

    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def finalize_campaign_subscription(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    data = await state.get_data()
    selected = data.get('campaign_subscription_squads', [])

    if not selected:
        await callback.answer('❗ Выберите хотя бы один сервер', show_alert=True)
        return

    campaign = await create_campaign(
        db,
        name=data['campaign_name'],
        start_parameter=data['campaign_start_parameter'],
        bonus_type='subscription',
        subscription_duration_days=data.get('campaign_subscription_days'),
        subscription_traffic_gb=data.get('campaign_subscription_traffic'),
        subscription_device_limit=data.get('campaign_subscription_devices'),
        subscription_squads=selected,
        created_by=db_user.id,
    )

    await state.clear()

    deep_link = await _get_bot_deep_link(callback, campaign.start_parameter)
    texts = get_texts(db_user.language)
    summary = _format_campaign_summary(campaign, texts)
    text = f'✅ <b>Кампания создана!</b>\n\n{summary}\n🔗 Ссылка: <code>{deep_link}</code>'

    await callback.message.edit_text(
        text,
        reply_markup=get_campaign_management_keyboard(campaign.id, campaign.is_active, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_campaign_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Обработка выбора тарифа для кампании."""
    tariff_id = int(callback.data.split('_')[-1])
    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff:
        await callback.answer('❌ Тариф не найден', show_alert=True)
        return

    await state.update_data(campaign_tariff_id=tariff_id, campaign_tariff_name=tariff.name)
    await state.set_state(AdminStates.creating_campaign_tariff_days)
    await callback.message.edit_text(
        f'🎁 Выбран тариф: <b>{html.escape(tariff.name)}</b>\n\n📅 Введите длительность тарифа в днях (1-730):',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_campaigns')]]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_campaign_tariff_days(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Обработка ввода длительности тарифа для кампании."""
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer('❌ Введите число дней (1-730)')
        return

    if days <= 0 or days > 730:
        await message.answer('❌ Длительность должна быть от 1 до 730 дней')
        return

    data = await state.get_data()
    tariff_id = data.get('campaign_tariff_id')

    if not tariff_id:
        await message.answer('❌ Тариф не выбран. Начните создание кампании заново.')
        await state.clear()
        return

    campaign = await create_campaign(
        db,
        name=data['campaign_name'],
        start_parameter=data['campaign_start_parameter'],
        bonus_type='tariff',
        tariff_id=tariff_id,
        tariff_duration_days=days,
        created_by=db_user.id,
    )

    # Перезагружаем кампанию с загруженным tariff relationship
    campaign = await get_campaign_by_id(db, campaign.id)

    await state.clear()

    deep_link = await _get_bot_deep_link_from_message(message, campaign.start_parameter)
    texts = get_texts(db_user.language)
    summary = _format_campaign_summary(campaign, texts)
    text = f'✅ <b>Кампания создана!</b>\n\n{summary}\n🔗 Ссылка: <code>{deep_link}</code>'

    await message.answer(
        text,
        reply_markup=get_campaign_management_keyboard(campaign.id, campaign.is_active, db_user.language),
    )


@admin_required
@error_handler
async def start_edit_campaign_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Начало редактирования тарифа кампании."""
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    if not campaign.is_tariff_bonus:
        await callback.answer("❌ Эта кампания не использует тип 'Тариф'", show_alert=True)
        return

    tariffs = await get_all_tariffs(db, include_inactive=False)
    if not tariffs:
        await callback.answer('❌ Нет доступных тарифов', show_alert=True)
        return

    keyboard = []
    for tariff in tariffs[:15]:
        is_current = campaign.tariff_id == tariff.id
        emoji = '✅' if is_current else '🎁'
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {tariff.name}',
                    callback_data=f'campaign_edit_set_tariff_{campaign_id}_{tariff.id}',
                )
            ]
        )
    keyboard.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data=f'admin_campaign_edit_{campaign_id}')])

    current_tariff_name = 'Не выбран'
    if campaign.tariff:
        current_tariff_name = campaign.tariff.name

    await callback.message.edit_text(
        f'🎁 <b>Изменение тарифа кампании</b>\n\nТекущий тариф: <b>{current_tariff_name}</b>\nВыберите новый тариф:',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def set_campaign_tariff(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Установка тарифа для кампании."""
    parts = callback.data.split('_')
    campaign_id = int(parts[-2])
    tariff_id = int(parts[-1])

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer('❌ Тариф не найден', show_alert=True)
        return

    await update_campaign(db, campaign, tariff_id=tariff_id)
    await callback.answer(f"✅ Тариф изменён на '{tariff.name}'")

    await _render_campaign_edit_menu(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        campaign,
        db_user.language,
    )


@admin_required
@error_handler
async def start_edit_campaign_tariff_days(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Начало редактирования длительности тарифа."""
    campaign_id = int(callback.data.split('_')[-1])
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await callback.answer('❌ Кампания не найдена', show_alert=True)
        return

    if not campaign.is_tariff_bonus:
        await callback.answer("❌ Эта кампания не использует тип 'Тариф'", show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_tariff_days)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
    )

    await callback.message.edit_text(
        f'📅 <b>Изменение длительности тарифа</b>\n\n'
        f'Текущее значение: <b>{campaign.tariff_duration_days or 0} д.</b>\n'
        'Введите новое количество дней (1-730):',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='❌ Отмена',
                        callback_data=f'admin_campaign_edit_{campaign_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_campaign_tariff_days(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    """Обработка ввода новой длительности тарифа."""
    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await message.answer('❌ Сессия редактирования устарела. Попробуйте снова.')
        await state.clear()
        return

    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer('❌ Введите число дней (1-730)')
        return

    if days <= 0 or days > 730:
        await message.answer('❌ Длительность должна быть от 1 до 730 дней')
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer('❌ Кампания не найдена')
        await state.clear()
        return

    await update_campaign(db, campaign, tariff_duration_days=days)
    await state.clear()

    await message.answer('✅ Длительность тарифа обновлена.')

    edit_message_id = data.get('campaign_edit_message_id')
    if edit_message_id:
        await _render_campaign_edit_menu(
            message.bot,
            message.chat.id,
            edit_message_id,
            campaign,
            db_user.language,
        )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_campaigns_menu, F.data == 'admin_campaigns')
    dp.callback_query.register(show_campaigns_overall_stats, F.data == 'admin_campaigns_stats')
    dp.callback_query.register(show_campaigns_list, F.data == 'admin_campaigns_list')
    dp.callback_query.register(show_campaigns_list, F.data.startswith('admin_campaigns_list_page_'))
    dp.callback_query.register(start_campaign_creation, F.data == 'admin_campaigns_create')
    dp.callback_query.register(show_campaign_stats, F.data.startswith('admin_campaign_stats_'))
    dp.callback_query.register(show_campaign_detail, F.data.startswith('admin_campaign_manage_'))
    dp.callback_query.register(start_edit_campaign_name, F.data.startswith('admin_campaign_edit_name_'))
    dp.callback_query.register(
        start_edit_campaign_start_parameter,
        F.data.startswith('admin_campaign_edit_start_'),
    )
    dp.callback_query.register(
        start_edit_campaign_balance_bonus,
        F.data.startswith('admin_campaign_edit_balance_'),
    )
    dp.callback_query.register(
        start_edit_campaign_subscription_days,
        F.data.startswith('admin_campaign_edit_sub_days_'),
    )
    dp.callback_query.register(
        start_edit_campaign_subscription_traffic,
        F.data.startswith('admin_campaign_edit_sub_traffic_'),
    )
    dp.callback_query.register(
        start_edit_campaign_subscription_devices,
        F.data.startswith('admin_campaign_edit_sub_devices_'),
    )
    dp.callback_query.register(
        start_edit_campaign_subscription_servers,
        F.data.startswith('admin_campaign_edit_sub_servers_'),
    )
    dp.callback_query.register(
        save_edit_campaign_subscription_servers,
        F.data.startswith('campaign_edit_servers_save_'),
    )
    dp.callback_query.register(toggle_edit_campaign_server, F.data.startswith('campaign_edit_toggle_'))
    # Tariff handlers ДОЛЖНЫ быть ПЕРЕД общим admin_campaign_edit_
    dp.callback_query.register(start_edit_campaign_tariff_days, F.data.startswith('admin_campaign_edit_tariff_days_'))
    dp.callback_query.register(start_edit_campaign_tariff, F.data.startswith('admin_campaign_edit_tariff_'))
    # Общий паттерн ПОСЛЕДНИМ
    dp.callback_query.register(show_campaign_edit_menu, F.data.startswith('admin_campaign_edit_'))
    dp.callback_query.register(delete_campaign_confirmed, F.data.startswith('admin_campaign_delete_confirm_'))
    dp.callback_query.register(confirm_delete_campaign, F.data.startswith('admin_campaign_delete_'))
    dp.callback_query.register(toggle_campaign_status, F.data.startswith('admin_campaign_toggle_'))
    dp.callback_query.register(finalize_campaign_subscription, F.data == 'campaign_servers_save')
    dp.callback_query.register(toggle_campaign_server, F.data.startswith('campaign_toggle_server_'))
    dp.callback_query.register(select_campaign_bonus_type, F.data.startswith('campaign_bonus_'))
    dp.callback_query.register(select_campaign_tariff, F.data.startswith('campaign_select_tariff_'))
    dp.callback_query.register(set_campaign_tariff, F.data.startswith('campaign_edit_set_tariff_'))

    dp.message.register(process_campaign_name, AdminStates.creating_campaign_name)
    dp.message.register(process_campaign_start_parameter, AdminStates.creating_campaign_start)
    dp.message.register(process_campaign_balance_value, AdminStates.creating_campaign_balance)
    dp.message.register(
        process_campaign_subscription_days,
        AdminStates.creating_campaign_subscription_days,
    )
    dp.message.register(
        process_campaign_subscription_traffic,
        AdminStates.creating_campaign_subscription_traffic,
    )
    dp.message.register(
        process_campaign_subscription_devices,
        AdminStates.creating_campaign_subscription_devices,
    )
    dp.message.register(process_edit_campaign_name, AdminStates.editing_campaign_name)
    dp.message.register(
        process_edit_campaign_start_parameter,
        AdminStates.editing_campaign_start,
    )
    dp.message.register(
        process_edit_campaign_balance_bonus,
        AdminStates.editing_campaign_balance,
    )
    dp.message.register(
        process_edit_campaign_subscription_days,
        AdminStates.editing_campaign_subscription_days,
    )
    dp.message.register(
        process_edit_campaign_subscription_traffic,
        AdminStates.editing_campaign_subscription_traffic,
    )
    dp.message.register(
        process_edit_campaign_subscription_devices,
        AdminStates.editing_campaign_subscription_devices,
    )
    dp.message.register(
        process_campaign_tariff_days,
        AdminStates.creating_campaign_tariff_days,
    )
    dp.message.register(
        process_edit_campaign_tariff_days,
        AdminStates.editing_campaign_tariff_days,
    )
