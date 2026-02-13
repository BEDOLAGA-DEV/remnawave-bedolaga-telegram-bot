import logging
import re

from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
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


logger = logging.getLogger(__name__)

_CAMPAIGN_PARAM_REGEX = re.compile(r'^[A-Za-z0-9_-]{3,32}$')
_CAMPAIGNS_PAGE_SIZE = 5


def _t(db_user: User, key: str, **kwargs) -> str:
    text = get_texts(getattr(db_user, 'language', settings.DEFAULT_LANGUAGE)).t(key)
    return text.format(**kwargs) if kwargs else text


def _t_lang(language: str, key: str, **kwargs) -> str:
    text = get_texts(language).t(key)
    return text.format(**kwargs) if kwargs else text


def _tf(texts, key: str, **kwargs) -> str:
    text = texts.t(key)
    return text.format(**kwargs) if kwargs else text


def _format_campaign_summary(campaign, texts) -> str:
    status = (
        _tf(texts, 'ADMIN_CAMPAIGN_STATUS_ACTIVE')
        if campaign.is_active
        else _tf(texts, 'ADMIN_CAMPAIGN_STATUS_DISABLED')
    )

    if campaign.is_balance_bonus:
        bonus_text = texts.format_price(campaign.balance_bonus_kopeks)
        bonus_info = _tf(texts, 'ADMIN_CAMPAIGN_SUMMARY_BALANCE_BONUS', amount=bonus_text)
    elif campaign.is_subscription_bonus:
        traffic_text = texts.format_traffic(campaign.subscription_traffic_gb or 0)
        device_limit = campaign.subscription_device_limit
        if device_limit is None:
            device_limit = settings.DEFAULT_DEVICE_LIMIT
        bonus_info = _tf(
            texts,
            'ADMIN_CAMPAIGN_SUMMARY_SUBSCRIPTION_BONUS',
            days=campaign.subscription_duration_days or 0,
            traffic=traffic_text,
            device_limit=device_limit,
        )
    elif campaign.is_tariff_bonus:
        tariff_name = _tf(texts, 'ADMIN_CAMPAIGN_NOT_SELECTED')
        if hasattr(campaign, 'tariff') and campaign.tariff:
            tariff_name = campaign.tariff.name
        bonus_info = _tf(
            texts,
            'ADMIN_CAMPAIGN_SUMMARY_TARIFF_BONUS',
            tariff_name=tariff_name,
            days=campaign.tariff_duration_days or 0,
        )
    elif campaign.is_none_bonus:
        bonus_info = _tf(texts, 'ADMIN_CAMPAIGN_SUMMARY_LINK_ONLY')
    else:
        bonus_info = _tf(texts, 'ADMIN_CAMPAIGN_SUMMARY_UNKNOWN_BONUS')

    return _tf(
        texts,
        'ADMIN_CAMPAIGN_SUMMARY_BLOCK',
        name=campaign.name,
        start_parameter=campaign.start_parameter,
        status=status,
        bonus_info=bonus_info,
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
    language: str,
    *,
    toggle_prefix: str = 'campaign_toggle_server_',
    save_callback: str = 'campaign_servers_save',
    back_callback: str = 'admin_campaigns',
) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard: list[list[types.InlineKeyboardButton]] = []

    for server in servers[:20]:
        is_selected = server.squad_uuid in selected_uuids
        emoji = '✅' if is_selected else ('⚪' if server.is_available else '🔒')
        text = f'{emoji} {server.display_name}'
        keyboard.append([types.InlineKeyboardButton(text=text, callback_data=f'{toggle_prefix}{server.id}')])

    keyboard.append(
        [
            types.InlineKeyboardButton(text=texts.t('ADMIN_CAMPAIGN_SAVE'), callback_data=save_callback),
            types.InlineKeyboardButton(text=texts.t('ADMIN_CAMPAIGN_BACK'), callback_data=back_callback),
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
    text = _tf(
        texts,
        'ADMIN_CAMPAIGN_EDIT_MENU',
        summary=_format_campaign_summary(campaign, texts),
    )

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

    text = _t(
        db_user,
        'ADMIN_CAMPAIGNS_MENU_SUMMARY',
        total=overview['total'],
        active=overview['active'],
        inactive=overview['inactive'],
        registrations=overview['registrations'],
        balance_total=texts.format_price(overview['balance_total']),
        subscription_total=overview['subscription_total'],
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

    text = _t(
        db_user,
        'ADMIN_CAMPAIGNS_OVERALL_STATS',
        total=overview['total'],
        active=overview['active'],
        inactive=overview['inactive'],
        registrations=overview['registrations'],
        balance_total=texts.format_price(overview['balance_total']),
        subscription_total=overview['subscription_total'],
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_CAMPAIGN_BACK'), callback_data='admin_campaigns')]
            ]
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
            _t(db_user, 'ADMIN_CAMPAIGN_LIST_EMPTY'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_CAMPAIGN_CREATE_BUTTON'),
                            callback_data='admin_campaigns_create',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_CAMPAIGN_BACK'), callback_data='admin_campaigns'
                        )
                    ],
                ]
            ),
        )
        await callback.answer()
        return

    text_lines = [_t(db_user, 'ADMIN_CAMPAIGNS_LIST_HEADER')]

    for campaign in campaigns:
        registrations = len(campaign.registrations or [])
        total_balance = sum(r.balance_bonus_kopeks or 0 for r in campaign.registrations or [])
        status = '🟢' if campaign.is_active else '⚪'
        line = f'{status} <b>{campaign.name}</b> — <code>{campaign.start_parameter}</code>\n' + _t(
            db_user,
            'ADMIN_CAMPAIGNS_LIST_ITEM_BASE',
            registrations=registrations,
            balance=texts.format_price(total_balance),
        )
        if campaign.is_subscription_bonus:
            line += _t(
                db_user,
                'ADMIN_CAMPAIGNS_LIST_ITEM_SUBSCRIPTION',
                days=campaign.subscription_duration_days or 0,
            )
        else:
            line += _t(db_user, 'ADMIN_CAMPAIGNS_LIST_ITEM_BALANCE_BONUS')
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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    texts = get_texts(db_user.language)
    stats = await get_campaign_statistics(db, campaign_id)
    deep_link = await _get_bot_deep_link(callback, campaign.start_parameter)

    text = [_t(db_user, 'ADMIN_CAMPAIGN_MANAGE_HEADER')]
    text.append(_format_campaign_summary(campaign, texts))
    text.append(_t(db_user, 'ADMIN_CAMPAIGN_LINK', link=deep_link))
    text.append(_t(db_user, 'ADMIN_CAMPAIGN_STATS_HEADER'))
    text.append(_t(db_user, 'ADMIN_CAMPAIGN_STATS_REGISTRATIONS', registrations=stats['registrations']))
    text.append(_t(db_user, 'ADMIN_CAMPAIGN_STATS_BALANCE_ISSUED', balance=texts.format_price(stats['balance_issued'])))
    text.append(_t(db_user, 'ADMIN_CAMPAIGN_STATS_SUBS_ISSUED', subscriptions=stats['subscription_issued']))
    text.append(_t(db_user, 'ADMIN_CAMPAIGN_STATS_REVENUE', revenue=texts.format_price(stats['total_revenue_kopeks'])))
    text.append(
        _t(
            db_user,
            'ADMIN_CAMPAIGN_STATS_TRIAL_USERS',
            trial_users=stats['trial_users_count'],
            active_trials=stats['active_trials_count'],
        )
    )
    text.append(
        _t(
            db_user,
            'ADMIN_CAMPAIGN_STATS_CONVERSIONS',
            conversion_count=stats['conversion_count'],
            paid_users=stats['paid_users_count'],
        )
    )
    text.append(_t(db_user, 'ADMIN_CAMPAIGN_STATS_CONVERSION_RATE', conversion_rate=f'{stats["conversion_rate"]:.1f}'))
    text.append(
        _t(
            db_user,
            'ADMIN_CAMPAIGN_STATS_TRIAL_CONVERSION_RATE',
            conversion_rate=f'{stats["trial_conversion_rate"]:.1f}',
        )
    )
    text.append(
        _t(
            db_user,
            'ADMIN_CAMPAIGN_STATS_ARPU',
            arpu=texts.format_price(stats['avg_revenue_per_user_kopeks']),
        )
    )
    text.append(
        _t(
            db_user,
            'ADMIN_CAMPAIGN_STATS_AVG_FIRST_PAYMENT',
            avg_payment=texts.format_price(stats['avg_first_payment_kopeks']),
        )
    )
    if stats['last_registration']:
        text.append(
            _t(
                db_user,
                'ADMIN_CAMPAIGN_STATS_LAST',
                last_registration=stats['last_registration'].strftime('%d.%m.%Y %H:%M'),
            )
        )

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
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
        _t(db_user, 'ADMIN_CAMPAIGN_EDIT_NAME_PROMPT', name=campaign.name),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_CAMPAIGN_CANCEL'),
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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_EDIT_SESSION_EXPIRED'))
        await state.clear()
        return

    new_name = message.text.strip()
    if len(new_name) < 3 or len(new_name) > 100:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_NAME_VALIDATION'))
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'))
        await state.clear()
        return

    await update_campaign(db, campaign, name=new_name)
    await state.clear()

    await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_NAME_UPDATED'))

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
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
        _t(db_user, 'ADMIN_CAMPAIGN_EDIT_START_PROMPT', start_parameter=campaign.start_parameter),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_CAMPAIGN_CANCEL'),
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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_EDIT_SESSION_EXPIRED'))
        await state.clear()
        return

    new_param = message.text.strip()
    if not _CAMPAIGN_PARAM_REGEX.match(new_param):
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_START_VALIDATION'))
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'))
        await state.clear()
        return

    existing = await get_campaign_by_start_parameter(db, new_param)
    if existing and existing.id != campaign_id:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_START_IN_USE'))
        return

    await update_campaign(db, campaign, start_parameter=new_param)
    await state.clear()

    await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_START_UPDATED'))

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    if not campaign.is_balance_bonus:
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_BONUS_TYPE_MISMATCH'), show_alert=True)
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
        _t(
            db_user,
            'ADMIN_CAMPAIGN_EDIT_BALANCE_PROMPT',
            balance_bonus=get_texts(db_user.language).format_price(campaign.balance_bonus_kopeks),
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_CAMPAIGN_CANCEL'),
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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_EDIT_SESSION_EXPIRED'))
        await state.clear()
        return

    try:
        amount_rubles = float(message.text.replace(',', '.'))
    except ValueError:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_AMOUNT_INVALID'))
        return

    if amount_rubles <= 0:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_AMOUNT_POSITIVE'))
        return

    amount_kopeks = int(round(amount_rubles * 100))

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'))
        await state.clear()
        return

    if not campaign.is_balance_bonus:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_BONUS_TYPE_MISMATCH'))
        await state.clear()
        return

    await update_campaign(db, campaign, balance_bonus_kopeks=amount_kopeks)
    await state.clear()

    await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_BONUS_UPDATED'))

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


async def _ensure_subscription_campaign(message_or_callback, campaign, language: str) -> bool:
    texts = get_texts(language)
    if campaign.is_balance_bonus:
        if isinstance(message_or_callback, types.CallbackQuery):
            await message_or_callback.answer(
                texts.t('ADMIN_CAMPAIGN_BALANCE_ONLY_FOR_CAMPAIGN'),
                show_alert=True,
            )
        else:
            await message_or_callback.answer(texts.t('ADMIN_CAMPAIGN_SUBSCRIPTION_PARAMS_NOT_AVAILABLE'))
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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign, db_user.language):
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
        _t(
            db_user,
            'ADMIN_CAMPAIGN_EDIT_SUB_DAYS_PROMPT',
            days=campaign.subscription_duration_days or 0,
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_CAMPAIGN_CANCEL'),
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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_EDIT_SESSION_EXPIRED'))
        await state.clear()
        return

    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DAYS_INVALID'))
        return

    if days <= 0 or days > 730:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DAYS_RANGE'))
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'))
        await state.clear()
        return

    if not await _ensure_subscription_campaign(message, campaign, db_user.language):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_duration_days=days)
    await state.clear()

    await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_SUB_DURATION_UPDATED'))

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign, db_user.language):
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
    traffic_text = _t(db_user, 'ADMIN_CAMPAIGN_TRAFFIC_UNLIMITED') if current_traffic == 0 else f'{current_traffic} GB'

    await callback.message.edit_text(
        _t(db_user, 'ADMIN_CAMPAIGN_EDIT_SUB_TRAFFIC_PROMPT', traffic=traffic_text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_CAMPAIGN_CANCEL'),
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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_EDIT_SESSION_EXPIRED'))
        await state.clear()
        return

    try:
        traffic = int(message.text.strip())
    except ValueError:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_TRAFFIC_INVALID'))
        return

    if traffic < 0 or traffic > 10000:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_TRAFFIC_RANGE'))
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'))
        await state.clear()
        return

    if not await _ensure_subscription_campaign(message, campaign, db_user.language):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_traffic_gb=traffic)
    await state.clear()

    await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_TRAFFIC_UPDATED'))

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign, db_user.language):
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
        _t(
            db_user,
            'ADMIN_CAMPAIGN_EDIT_SUB_DEVICES_PROMPT',
            current_devices=current_devices,
            max_devices=settings.MAX_DEVICES_LIMIT,
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_CAMPAIGN_CANCEL'),
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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_EDIT_SESSION_EXPIRED'))
        await state.clear()
        return

    try:
        devices = int(message.text.strip())
    except ValueError:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DEVICES_INVALID'))
        return

    if devices < 1 or devices > settings.MAX_DEVICES_LIMIT:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DEVICES_RANGE', max_devices=settings.MAX_DEVICES_LIMIT))
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'))
        await state.clear()
        return

    if not await _ensure_subscription_campaign(message, campaign, db_user.language):
        await state.clear()
        return

    await update_campaign(db, campaign, subscription_device_limit=devices)
    await state.clear()

    await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DEVICES_UPDATED'))

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign, db_user.language):
        return

    servers, _ = await get_all_server_squads(db, available_only=False)
    if not servers:
        await callback.answer(
            _t(db_user, 'ADMIN_CAMPAIGN_SERVERS_EMPTY_FOR_EDIT'),
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
        db_user.language,
        toggle_prefix=f'campaign_edit_toggle_{campaign_id}_',
        save_callback=f'campaign_edit_servers_save_{campaign_id}',
        back_callback=f'admin_campaign_edit_{campaign_id}',
    )

    await callback.message.edit_text(
        _t(db_user, 'ADMIN_CAMPAIGN_EDIT_SUB_SERVERS_PROMPT'),
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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_SERVER_DETECT_FAILED'), show_alert=True)
        return

    data = await state.get_data()
    campaign_id = data.get('editing_campaign_id')
    if not campaign_id:
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_EDIT_SESSION_EXPIRED_SHORT'), show_alert=True)
        await state.clear()
        return

    server = await get_server_squad_by_id(db, server_id)
    if not server:
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_SERVER_NOT_FOUND'), show_alert=True)
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
        db_user.language,
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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_EDIT_SESSION_EXPIRED_SHORT'), show_alert=True)
        await state.clear()
        return

    selected = list(data.get('campaign_subscription_squads', []))
    if not selected:
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_SELECT_AT_LEAST_ONE_SERVER'), show_alert=True)
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await state.clear()
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    if not await _ensure_subscription_campaign(callback, campaign, db_user.language):
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
    await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_SAVED'))


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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    new_status = not campaign.is_active
    await update_campaign(db, campaign, is_active=new_status)
    status_text = (
        _t(db_user, 'ADMIN_CAMPAIGN_STATUS_ENABLED')
        if new_status
        else _t(db_user, 'ADMIN_CAMPAIGN_STATUS_DISABLED_SHORT')
    )
    logger.info('🔄 Кампания %s переключена: %s', campaign_id, status_text)

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    texts = get_texts(db_user.language)
    stats = await get_campaign_statistics(db, campaign_id)

    text = [_t(db_user, 'ADMIN_CAMPAIGN_STATS_PAGE_HEADER')]
    text.append(_format_campaign_summary(campaign, texts))
    text.append(_t(db_user, 'ADMIN_CAMPAIGN_STATS_REGISTRATIONS_PLAIN', registrations=stats['registrations']))
    text.append(
        _t(db_user, 'ADMIN_CAMPAIGN_STATS_BALANCE_ISSUED_PLAIN', balance=texts.format_price(stats['balance_issued']))
    )
    text.append(_t(db_user, 'ADMIN_CAMPAIGN_STATS_SUBS_ISSUED_PLAIN', subscriptions=stats['subscription_issued']))
    if stats['last_registration']:
        text.append(
            _t(
                db_user,
                'ADMIN_CAMPAIGN_STATS_LAST_REGISTRATION',
                last_registration=stats['last_registration'].strftime('%d.%m.%Y %H:%M'),
            )
        )

    await callback.message.edit_text(
        '\n'.join(text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_CAMPAIGN_BACK'),
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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    text = _t(
        db_user,
        'ADMIN_CAMPAIGN_DELETE_CONFIRM',
        name=campaign.name,
        start_parameter=campaign.start_parameter,
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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    await delete_campaign(db, campaign)
    await callback.message.edit_text(
        _t(db_user, 'ADMIN_CAMPAIGN_DELETED'),
        reply_markup=get_admin_campaigns_keyboard(db_user.language),
    )
    await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_DELETED_ALERT'))


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
        _t(db_user, 'ADMIN_CAMPAIGN_CREATE_PROMPT_NAME'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_CAMPAIGN_BACK'), callback_data='admin_campaigns')]
            ]
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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_NAME_VALIDATION'))
        return

    await state.update_data(campaign_name=name)
    await state.set_state(AdminStates.creating_campaign_start)
    await message.answer(
        _t(db_user, 'ADMIN_CAMPAIGN_CREATE_PROMPT_START'),
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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_START_VALIDATION'))
        return

    existing = await get_campaign_by_start_parameter(db, start_param)
    if existing:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_START_ALREADY_EXISTS'))
        return

    await state.update_data(campaign_start_parameter=start_param)
    await state.set_state(AdminStates.creating_campaign_bonus)
    await message.answer(
        _t(db_user, 'ADMIN_CAMPAIGN_CREATE_PROMPT_BONUS_TYPE'),
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
            _t(db_user, 'ADMIN_CAMPAIGN_CREATE_PROMPT_BALANCE'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_CAMPAIGN_BACK'), callback_data='admin_campaigns'
                        )
                    ]
                ]
            ),
        )
    elif bonus_type == 'subscription':
        await state.set_state(AdminStates.creating_campaign_subscription_days)
        await callback.message.edit_text(
            _t(db_user, 'ADMIN_CAMPAIGN_CREATE_PROMPT_SUB_DAYS'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_CAMPAIGN_BACK'), callback_data='admin_campaigns'
                        )
                    ]
                ]
            ),
        )
    elif bonus_type == 'tariff':
        # Показываем выбор тарифа
        tariffs = await get_all_tariffs(db, include_inactive=False)
        if not tariffs:
            await callback.answer(
                _t(db_user, 'ADMIN_CAMPAIGN_NO_TARIFFS_CREATE_FIRST'),
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
        keyboard.append(
            [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_CAMPAIGN_BACK'), callback_data='admin_campaigns')]
        )

        await state.set_state(AdminStates.creating_campaign_tariff_select)
        await callback.message.edit_text(
            _t(db_user, 'ADMIN_CAMPAIGN_CREATE_PROMPT_SELECT_TARIFF'),
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
        text = _t(db_user, 'ADMIN_CAMPAIGN_CREATED', summary=summary, deep_link=deep_link)

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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_AMOUNT_INVALID'))
        return

    if amount_rubles <= 0:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_AMOUNT_POSITIVE'))
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
    text = _t(db_user, 'ADMIN_CAMPAIGN_CREATED', summary=summary, deep_link=deep_link)

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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DAYS_INVALID'))
        return

    if days <= 0 or days > 730:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DAYS_RANGE'))
        return

    await state.update_data(campaign_subscription_days=days)
    await state.set_state(AdminStates.creating_campaign_subscription_traffic)
    await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_CREATE_PROMPT_SUB_TRAFFIC'))


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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_TRAFFIC_INVALID'))
        return

    if traffic < 0 or traffic > 10000:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_TRAFFIC_RANGE'))
        return

    await state.update_data(campaign_subscription_traffic=traffic)
    await state.set_state(AdminStates.creating_campaign_subscription_devices)
    await message.answer(
        _t(db_user, 'ADMIN_CAMPAIGN_CREATE_PROMPT_SUB_DEVICES', max_devices=settings.MAX_DEVICES_LIMIT)
    )


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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DEVICES_INVALID'))
        return

    if devices < 1 or devices > settings.MAX_DEVICES_LIMIT:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DEVICES_RANGE', max_devices=settings.MAX_DEVICES_LIMIT))
        return

    await state.update_data(campaign_subscription_devices=devices)
    await state.update_data(campaign_subscription_squads=[])
    await state.set_state(AdminStates.creating_campaign_subscription_servers)

    servers, _ = await get_all_server_squads(db, available_only=False)
    if not servers:
        await message.answer(
            _t(db_user, 'ADMIN_CAMPAIGN_SERVERS_EMPTY_FOR_CREATE'),
        )
        await state.clear()
        return

    keyboard = _build_campaign_servers_keyboard(servers, [], db_user.language)
    await message.answer(
        _t(db_user, 'ADMIN_CAMPAIGN_CREATE_PROMPT_SUB_SERVERS'),
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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_SERVER_NOT_FOUND'), show_alert=True)
        return

    data = await state.get_data()
    selected = list(data.get('campaign_subscription_squads', []))

    if server.squad_uuid in selected:
        selected.remove(server.squad_uuid)
    else:
        selected.append(server.squad_uuid)

    await state.update_data(campaign_subscription_squads=selected)

    servers, _ = await get_all_server_squads(db, available_only=False)
    keyboard = _build_campaign_servers_keyboard(servers, selected, db_user.language)

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_SELECT_AT_LEAST_ONE_SERVER'), show_alert=True)
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
    text = _t(db_user, 'ADMIN_CAMPAIGN_CREATED', summary=summary, deep_link=deep_link)

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_TARIFF_NOT_FOUND'), show_alert=True)
        return

    await state.update_data(campaign_tariff_id=tariff_id, campaign_tariff_name=tariff.name)
    await state.set_state(AdminStates.creating_campaign_tariff_days)
    await callback.message.edit_text(
        _t(db_user, 'ADMIN_CAMPAIGN_CREATE_PROMPT_TARIFF_DAYS', tariff_name=tariff.name),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_CAMPAIGN_BACK'), callback_data='admin_campaigns')]
            ]
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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DAYS_INVALID'))
        return

    if days <= 0 or days > 730:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DAYS_RANGE'))
        return

    data = await state.get_data()
    tariff_id = data.get('campaign_tariff_id')

    if not tariff_id:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_TARIFF_NOT_SELECTED'))
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
    text = _t(db_user, 'ADMIN_CAMPAIGN_CREATED', summary=summary, deep_link=deep_link)

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    if not campaign.is_tariff_bonus:
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_TARIFF_TYPE'), show_alert=True)
        return

    tariffs = await get_all_tariffs(db, include_inactive=False)
    if not tariffs:
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NO_TARIFFS'), show_alert=True)
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
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=_t(db_user, 'ADMIN_CAMPAIGN_BACK'), callback_data=f'admin_campaign_edit_{campaign_id}'
            )
        ]
    )

    current_tariff_name = _t(db_user, 'ADMIN_CAMPAIGN_NOT_SELECTED')
    if campaign.tariff:
        current_tariff_name = campaign.tariff.name

    await callback.message.edit_text(
        _t(db_user, 'ADMIN_CAMPAIGN_EDIT_TARIFF_PROMPT', current_tariff_name=current_tariff_name),
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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_TARIFF_NOT_FOUND'), show_alert=True)
        return

    await update_campaign(db, campaign, tariff_id=tariff_id)
    await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_TARIFF_UPDATED', tariff_name=tariff.name))

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
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'), show_alert=True)
        return

    if not campaign.is_tariff_bonus:
        await callback.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_TARIFF_TYPE'), show_alert=True)
        return

    await state.clear()
    await state.set_state(AdminStates.editing_campaign_tariff_days)
    await state.update_data(
        editing_campaign_id=campaign_id,
        campaign_edit_message_id=callback.message.message_id,
    )

    await callback.message.edit_text(
        _t(
            db_user,
            'ADMIN_CAMPAIGN_EDIT_TARIFF_DAYS_PROMPT',
            days=campaign.tariff_duration_days or 0,
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_CAMPAIGN_CANCEL'),
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
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_EDIT_SESSION_EXPIRED'))
        await state.clear()
        return

    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DAYS_INVALID'))
        return

    if days <= 0 or days > 730:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_DAYS_RANGE'))
        return

    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_NOT_FOUND'))
        await state.clear()
        return

    await update_campaign(db, campaign, tariff_duration_days=days)
    await state.clear()

    await message.answer(_t(db_user, 'ADMIN_CAMPAIGN_TARIFF_DURATION_UPDATED'))

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
