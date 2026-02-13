import html
import logging

from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_groups_with_counts
from app.database.crud.server_squad import (
    delete_server_squad,
    get_all_server_squads,
    get_available_server_squads,
    get_server_connected_users,
    get_server_squad_by_id,
    get_server_statistics,
    sync_with_remnawave,
    update_server_squad,
    update_server_squad_promo_groups,
)
from app.database.models import User
from app.localization.texts import get_texts
from app.services.remnawave_service import RemnaWaveService
from app.states import AdminStates
from app.utils.cache import cache
from app.utils.decorators import admin_required, error_handler


logger = logging.getLogger(__name__)


def _t(db_user: User, key: str, **kwargs) -> str:
    text = get_texts(getattr(db_user, 'language', settings.DEFAULT_LANGUAGE)).t(key)
    return text.format(**kwargs) if kwargs else text


def _t_lang(language: str, key: str, **kwargs) -> str:
    text = get_texts(language).t(key)
    return text.format(**kwargs) if kwargs else text


def _build_server_edit_view(server, language: str):
    texts = get_texts(language)
    status_emoji = (
        _t_lang(language, 'ADMIN_SERVER_STATUS_AVAILABLE')
        if server.is_available
        else _t_lang(language, 'ADMIN_SERVER_STATUS_UNAVAILABLE')
    )
    price_text = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else _t_lang(language, 'ADMIN_SERVER_FREE')
    promo_groups_text = (
        ', '.join(sorted(pg.name for pg in server.allowed_promo_groups))
        if server.allowed_promo_groups
        else _t_lang(language, 'ADMIN_SERVER_PROMO_GROUPS_NOT_SELECTED')
    )

    trial_status = (
        _t_lang(language, 'ADMIN_SERVER_TRIAL_YES')
        if server.is_trial_eligible
        else _t_lang(language, 'ADMIN_SERVER_TRIAL_NO')
    )

    text = f"""
{_t_lang(language, 'ADMIN_SERVER_EDIT_TITLE')}

{_t_lang(language, 'ADMIN_SERVER_INFO_HEADER')}
• ID: {server.id}
• UUID: <code>{server.squad_uuid}</code>
• {_t_lang(language, 'ADMIN_SERVER_FIELD_NAME')}: {server.display_name}
• {_t_lang(language, 'ADMIN_SERVER_FIELD_ORIGINAL_NAME')}: {server.original_name or _t_lang(language, 'ADMIN_SERVER_NOT_SPECIFIED')}
• {_t_lang(language, 'ADMIN_SERVER_FIELD_STATUS')}: {status_emoji}

{_t_lang(language, 'ADMIN_SERVER_SETTINGS_HEADER')}
• {_t_lang(language, 'ADMIN_SERVER_FIELD_PRICE')}: {price_text}
• {_t_lang(language, 'ADMIN_SERVER_FIELD_COUNTRY')}: {server.country_code or _t_lang(language, 'ADMIN_SERVER_NOT_SPECIFIED')}
• {_t_lang(language, 'ADMIN_SERVER_FIELD_USER_LIMIT')}: {server.max_users or _t_lang(language, 'ADMIN_SERVER_NO_LIMIT')}
• {_t_lang(language, 'ADMIN_SERVER_FIELD_CURRENT_USERS')}: {server.current_users}
• {_t_lang(language, 'ADMIN_SERVER_FIELD_PROMO_GROUPS')}: {promo_groups_text}
• {_t_lang(language, 'ADMIN_SERVER_FIELD_TRIAL_ASSIGNMENT')}: {trial_status}

{_t_lang(language, 'ADMIN_SERVER_DESCRIPTION_HEADER')}
{server.description or _t_lang(language, 'ADMIN_SERVER_NOT_SPECIFIED')}

{_t_lang(language, 'ADMIN_SERVER_CHOOSE_EDIT_ACTION')}
"""

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_NAME'), callback_data=f'admin_server_edit_name_{server.id}'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_PRICE'), callback_data=f'admin_server_edit_price_{server.id}'
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_COUNTRY'), callback_data=f'admin_server_edit_country_{server.id}'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_LIMIT'), callback_data=f'admin_server_edit_limit_{server.id}'
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_USERS'), callback_data=f'admin_server_users_{server.id}'
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_TRIAL_ENABLE')
                if not server.is_trial_eligible
                else texts.t('ADMIN_SERVER_TRIAL_DISABLE'),
                callback_data=f'admin_server_trial_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_PROMO_GROUPS'), callback_data=f'admin_server_edit_promo_{server.id}'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_DESCRIPTION'), callback_data=f'admin_server_edit_desc_{server.id}'
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_DISABLE') if server.is_available else texts.t('ADMIN_SERVER_ENABLE'),
                callback_data=f'admin_server_toggle_{server.id}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_DELETE'), callback_data=f'admin_server_delete_{server.id}'
            ),
            types.InlineKeyboardButton(text=texts.t('ADMIN_SERVER_BACK'), callback_data='admin_servers_list'),
        ],
    ]

    return text, types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _build_server_promo_groups_keyboard(server_id: int, promo_groups, selected_ids, language: str):
    texts = get_texts(language)
    keyboard = []
    for group in promo_groups:
        emoji = '✅' if group['id'] in selected_ids else '⚪'
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {group["name"]}',
                    callback_data=f'admin_server_promo_toggle_{server_id}_{group["id"]}',
                )
            ]
        )

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_SAVE'), callback_data=f'admin_server_promo_save_{server_id}'
            )
        ]
    )
    keyboard.append(
        [types.InlineKeyboardButton(text=texts.t('ADMIN_SERVER_BACK'), callback_data=f'admin_server_edit_{server_id}')]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


@admin_required
@error_handler
async def show_servers_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_server_statistics(db)

    text = _t(
        db_user,
        'ADMIN_SERVER_MENU',
        total_servers=stats['total_servers'],
        available_servers=stats['available_servers'],
        unavailable_servers=stats['unavailable_servers'],
        servers_with_connections=stats['servers_with_connections'],
        total_revenue=int(stats['total_revenue_rubles']),
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=_t(db_user, 'ADMIN_SERVER_LIST_BUTTON'), callback_data='admin_servers_list'
            ),
            types.InlineKeyboardButton(
                text=_t(db_user, 'ADMIN_SERVER_SYNC_BUTTON'), callback_data='admin_servers_sync'
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=_t(db_user, 'ADMIN_SERVER_SYNC_COUNTS_BUTTON'), callback_data='admin_servers_sync_counts'
            ),
            types.InlineKeyboardButton(
                text=_t(db_user, 'ADMIN_SERVER_DETAILED_STATS_BUTTON'), callback_data='admin_servers_stats'
            ),
        ],
        [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_SERVER_BACK'), callback_data='admin_panel')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_servers_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    servers, total_count = await get_all_server_squads(db, page=page, limit=10)
    total_pages = (total_count + 9) // 10

    if not servers:
        text = _t(db_user, 'ADMIN_SERVER_LIST_EMPTY')
    else:
        text = _t(db_user, 'ADMIN_SERVER_LIST_HEADER', total_count=total_count, page=page, total_pages=total_pages)

        for i, server in enumerate(servers, 1 + (page - 1) * 10):
            status_emoji = '✅' if server.is_available else '❌'
            price_text = (
                f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else _t(db_user, 'ADMIN_SERVER_FREE')
            )

            text += f'{i}. {status_emoji} {server.display_name}\n'
            text += _t(db_user, 'ADMIN_SERVER_LIST_PRICE_LINE', price=price_text)

            if server.max_users:
                text += f' | 👥 {server.current_users}/{server.max_users}'

            text += f'\n   UUID: <code>{server.squad_uuid}</code>\n\n'

    keyboard = []

    for i, server in enumerate(servers):
        row_num = i // 2
        if len(keyboard) <= row_num:
            keyboard.append([])

        status_emoji = '✅' if server.is_available else '❌'
        keyboard[row_num].append(
            types.InlineKeyboardButton(
                text=f'{status_emoji} {server.display_name[:15]}...', callback_data=f'admin_server_edit_{server.id}'
            )
        )

    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text='⬅️', callback_data=f'admin_servers_list_page_{page - 1}'))

        nav_row.append(types.InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='current_page'))

        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text='➡️', callback_data=f'admin_servers_list_page_{page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend(
        [[types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_SERVER_BACK'), callback_data='admin_servers')]]
    )

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def sync_servers_with_remnawave(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        _t(db_user, 'ADMIN_SERVER_SYNC_PROGRESS'),
        reply_markup=None,
    )

    try:
        remnawave_service = RemnaWaveService()
        squads = await remnawave_service.get_all_squads()

        if not squads:
            await callback.message.edit_text(
                _t(db_user, 'ADMIN_SERVER_SYNC_FETCH_FAILED'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=_t(db_user, 'ADMIN_SERVER_BACK'), callback_data='admin_servers'
                            )
                        ]
                    ]
                ),
            )
            return

        created, updated, removed = await sync_with_remnawave(db, squads)

        await cache.delete_pattern('available_countries*')

        text = _t(
            db_user,
            'ADMIN_SERVER_SYNC_DONE',
            created=created,
            updated=updated,
            removed=removed,
            total=len(squads),
        )

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=_t(db_user, 'ADMIN_SERVER_LIST_BUTTON'), callback_data='admin_servers_list'
                ),
                types.InlineKeyboardButton(
                    text=_t(db_user, 'ADMIN_SERVER_RETRY_BUTTON'), callback_data='admin_servers_sync'
                ),
            ],
            [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_SERVER_BACK'), callback_data='admin_servers')],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error(f'Ошибка синхронизации серверов: {e}')
        await callback.message.edit_text(
            _t(db_user, 'ADMIN_SERVER_SYNC_ERROR', error=e),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_SERVER_BACK'), callback_data='admin_servers')]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def show_server_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    text, keyboard = _build_server_edit_view(server, db_user.language)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_server_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    payload = callback.data.split('admin_server_users_', 1)[-1]
    payload_parts = payload.split('_')

    server_id = int(payload_parts[0])
    page = int(payload_parts[1]) if len(payload_parts) > 1 else 1
    page = max(page, 1)
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    users = await get_server_connected_users(db, server_id)
    total_users = len(users)

    page_size = 10
    total_pages = max((total_users + page_size - 1) // page_size, 1)

    page = min(page, total_pages)

    start_index = (page - 1) * page_size
    end_index = start_index + page_size
    page_users = users[start_index:end_index]

    safe_name = html.escape(server.display_name or '—')
    safe_uuid = html.escape(server.squad_uuid or '—')

    header = [
        _t(db_user, 'ADMIN_SERVER_USERS_HEADER'),
        '',
        _t(db_user, 'ADMIN_SERVER_USERS_SERVER', server=safe_name),
        f'• UUID: <code>{safe_uuid}</code>',
        _t(db_user, 'ADMIN_SERVER_USERS_CONNECTIONS', total=total_users),
    ]

    if total_pages > 1:
        header.append(_t(db_user, 'ADMIN_SERVER_USERS_PAGE', page=page, total_pages=total_pages))

    header.append('')

    text = '\n'.join(header)

    def _get_status_icon(status_text: str) -> str:
        if not status_text:
            return ''

        parts = status_text.split(' ', 1)
        return parts[0] if parts else status_text

    if users:
        lines = []
        for index, user in enumerate(page_users, start=start_index + 1):
            safe_user_name = html.escape(user.full_name)
            if user.telegram_id:
                user_link = f'<a href="tg://user?id={user.telegram_id}">{safe_user_name}</a>'
            else:
                user_link = f'<b>{safe_user_name}</b>'
            lines.append(f'{index}. {user_link}')

        text += '\n' + '\n'.join(lines)
    else:
        text += _t(db_user, 'ADMIN_SERVER_USERS_EMPTY')

    keyboard: list[list[types.InlineKeyboardButton]] = []

    for user in page_users:
        display_name = user.full_name
        if len(display_name) > 30:
            display_name = display_name[:27] + '...'

        subscription_status = (
            user.subscription.status_display if user.subscription else _t(db_user, 'ADMIN_SERVER_NO_SUBSCRIPTION')
        )
        status_icon = _get_status_icon(subscription_status)

        if status_icon:
            button_text = f'{status_icon} {display_name}'
        else:
            button_text = display_name

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=f'admin_user_manage_{user.id}',
                )
            ]
        )

    if total_pages > 1:
        navigation_buttons: list[types.InlineKeyboardButton] = []

        if page > 1:
            navigation_buttons.append(
                types.InlineKeyboardButton(
                    text=_t(db_user, 'ADMIN_SERVER_PREVIOUS'),
                    callback_data=f'admin_server_users_{server_id}_{page - 1}',
                )
            )

        navigation_buttons.append(
            types.InlineKeyboardButton(
                text=_t(db_user, 'ADMIN_SERVER_PAGE_LABEL', page=page, total_pages=total_pages),
                callback_data=f'admin_server_users_{server_id}_{page}',
            )
        )

        if page < total_pages:
            navigation_buttons.append(
                types.InlineKeyboardButton(
                    text=_t(db_user, 'ADMIN_SERVER_NEXT'),
                    callback_data=f'admin_server_users_{server_id}_{page + 1}',
                )
            )

        keyboard.append(navigation_buttons)

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=_t(db_user, 'ADMIN_SERVER_TO_SERVER_SHORT'), callback_data=f'admin_server_edit_{server_id}'
            )
        ]
    )

    keyboard.append(
        [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_SERVER_TO_LIST'), callback_data='admin_servers_list')]
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode='HTML',
    )

    await callback.answer()


@admin_required
@error_handler
async def toggle_server_availability(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    new_status = not server.is_available
    await update_server_squad(db, server_id, is_available=new_status)

    await cache.delete_pattern('available_countries*')

    status_text = (
        _t(db_user, 'ADMIN_SERVER_STATUS_ENABLED') if new_status else _t(db_user, 'ADMIN_SERVER_STATUS_DISABLED')
    )
    await callback.answer(_t(db_user, 'ADMIN_SERVER_STATUS_UPDATED', status=status_text))

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server, db_user.language)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def toggle_server_trial_assignment(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    new_status = not server.is_trial_eligible
    await update_server_squad(db, server_id, is_trial_eligible=new_status)

    status_text = (
        _t(db_user, 'ADMIN_SERVER_TRIAL_STATUS_ENABLED')
        if new_status
        else _t(db_user, 'ADMIN_SERVER_TRIAL_STATUS_DISABLED')
    )
    await callback.answer(_t(db_user, 'ADMIN_SERVER_TRIAL_STATUS_UPDATED', status=status_text))

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server, db_user.language)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def start_server_edit_price(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_price)

    current_price = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else _t(db_user, 'ADMIN_SERVER_FREE')

    await callback.message.edit_text(
        _t(db_user, 'ADMIN_SERVER_EDIT_PRICE_PROMPT', current_price=current_price),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_SERVER_CANCEL'), callback_data=f'admin_server_edit_{server_id}'
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_price_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    try:
        price_rubles = float(message.text.replace(',', '.'))

        if price_rubles < 0:
            await message.answer(_t(db_user, 'ADMIN_SERVER_PRICE_NEGATIVE'))
            return

        if price_rubles > 10000:
            await message.answer(_t(db_user, 'ADMIN_SERVER_PRICE_TOO_HIGH'))
            return

        price_kopeks = int(price_rubles * 100)

        server = await update_server_squad(db, server_id, price_kopeks=price_kopeks)

        if server:
            await state.clear()

            await cache.delete_pattern('available_countries*')

            price_text = f'{int(price_rubles)} ₽' if price_kopeks > 0 else _t(db_user, 'ADMIN_SERVER_FREE')
            await message.answer(
                _t(db_user, 'ADMIN_SERVER_PRICE_UPDATED', price=price_text),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=_t(db_user, 'ADMIN_SERVER_TO_SERVER'),
                                callback_data=f'admin_server_edit_{server_id}',
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer(_t(db_user, 'ADMIN_SERVER_UPDATE_ERROR'))

    except ValueError:
        await message.answer(_t(db_user, 'ADMIN_SERVER_PRICE_FORMAT_INVALID'))


@admin_required
@error_handler
async def start_server_edit_name(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_name)

    await callback.message.edit_text(
        _t(db_user, 'ADMIN_SERVER_EDIT_NAME_PROMPT', current_name=server.display_name),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_SERVER_CANCEL'), callback_data=f'admin_server_edit_{server_id}'
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_name_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    new_name = message.text.strip()

    if len(new_name) > 255:
        await message.answer(_t(db_user, 'ADMIN_SERVER_NAME_TOO_LONG'))
        return

    if len(new_name) < 3:
        await message.answer(_t(db_user, 'ADMIN_SERVER_NAME_TOO_SHORT'))
        return

    server = await update_server_squad(db, server_id, display_name=new_name)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        await message.answer(
            _t(db_user, 'ADMIN_SERVER_NAME_UPDATED', name=new_name),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_SERVER_TO_SERVER'), callback_data=f'admin_server_edit_{server_id}'
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(_t(db_user, 'ADMIN_SERVER_UPDATE_ERROR'))


@admin_required
@error_handler
async def delete_server_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    text = f"""
{_t(db_user, 'ADMIN_SERVER_DELETE_TITLE')}

{_t(db_user, 'ADMIN_SERVER_DELETE_CONFIRM_QUESTION')}
<b>{server.display_name}</b>

{_t(db_user, 'ADMIN_SERVER_DELETE_WARNING_HEADER')}
{_t(db_user, 'ADMIN_SERVER_DELETE_WARNING_BODY')}

{_t(db_user, 'ADMIN_SERVER_DELETE_WARNING_IRREVERSIBLE')}
"""

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=_t(db_user, 'ADMIN_SERVER_DELETE_CONFIRM_BUTTON'),
                callback_data=f'admin_server_delete_confirm_{server_id}',
            ),
            types.InlineKeyboardButton(
                text=_t(db_user, 'ADMIN_SERVER_CANCEL'), callback_data=f'admin_server_edit_{server_id}'
            ),
        ]
    ]

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_server_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    success = await delete_server_squad(db, server_id)

    if success:
        await cache.delete_pattern('available_countries*')

        await callback.message.edit_text(
            _t(db_user, 'ADMIN_SERVER_DELETE_SUCCESS', name=server.display_name),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_SERVER_TO_LIST_FULL'), callback_data='admin_servers_list'
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await callback.message.edit_text(
            _t(db_user, 'ADMIN_SERVER_DELETE_FAILED', name=server.display_name),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_SERVER_TO_SERVER'), callback_data=f'admin_server_edit_{server_id}'
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )

    await callback.answer()


@admin_required
@error_handler
async def show_server_detailed_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_server_statistics(db)
    available_servers = await get_available_server_squads(db)

    text = _t(
        db_user,
        'ADMIN_SERVER_DETAILED_STATS',
        total_servers=stats['total_servers'],
        available_servers=stats['available_servers'],
        unavailable_servers=stats['unavailable_servers'],
        servers_with_connections=stats['servers_with_connections'],
        total_revenue=int(stats['total_revenue_rubles']),
        avg_price=int(stats['total_revenue_rubles'] / max(stats['servers_with_connections'], 1)),
    )

    sorted_servers = sorted(available_servers, key=lambda x: x.price_kopeks, reverse=True)

    for i, server in enumerate(sorted_servers[:5], 1):
        price_text = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else _t(db_user, 'ADMIN_SERVER_FREE')
        text += f'{i}. {server.display_name} - {price_text}\n'

    if not sorted_servers:
        text += _t(db_user, 'ADMIN_SERVER_NO_AVAILABLE') + '\n'

    keyboard = [
        [
            types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_SERVER_REFRESH'), callback_data='admin_servers_stats'),
            types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_SERVER_LIST_SHORT'), callback_data='admin_servers_list'),
        ],
        [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_SERVER_BACK'), callback_data='admin_servers')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_server_edit_country(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_country)

    current_country = server.country_code or _t(db_user, 'ADMIN_SERVER_NOT_SPECIFIED')

    await callback.message.edit_text(
        _t(db_user, 'ADMIN_SERVER_EDIT_COUNTRY_PROMPT', current_country=current_country),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_SERVER_CANCEL'), callback_data=f'admin_server_edit_{server_id}'
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_country_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    new_country = message.text.strip().upper()

    if new_country == '-':
        new_country = None
    elif len(new_country) > 5:
        await message.answer(_t(db_user, 'ADMIN_SERVER_COUNTRY_TOO_LONG'))
        return

    server = await update_server_squad(db, server_id, country_code=new_country)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        country_text = new_country or _t(db_user, 'ADMIN_SERVER_REMOVED')
        await message.answer(
            _t(db_user, 'ADMIN_SERVER_COUNTRY_UPDATED', country=country_text),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_SERVER_TO_SERVER'), callback_data=f'admin_server_edit_{server_id}'
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(_t(db_user, 'ADMIN_SERVER_UPDATE_ERROR'))


@admin_required
@error_handler
async def start_server_edit_limit(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_limit)

    current_limit = server.max_users or _t(db_user, 'ADMIN_SERVER_NO_LIMIT')

    await callback.message.edit_text(
        _t(db_user, 'ADMIN_SERVER_EDIT_LIMIT_PROMPT', current_limit=current_limit),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_SERVER_CANCEL'), callback_data=f'admin_server_edit_{server_id}'
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_limit_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    try:
        limit = int(message.text.strip())

        if limit < 0:
            await message.answer(_t(db_user, 'ADMIN_SERVER_LIMIT_NEGATIVE'))
            return

        if limit > 10000:
            await message.answer(_t(db_user, 'ADMIN_SERVER_LIMIT_TOO_HIGH'))
            return

        max_users = limit if limit > 0 else None

        server = await update_server_squad(db, server_id, max_users=max_users)

        if server:
            await state.clear()

            limit_text = (
                _t(db_user, 'ADMIN_SERVER_LIMIT_USERS_VALUE', limit=limit)
                if limit > 0
                else _t(db_user, 'ADMIN_SERVER_NO_LIMIT')
            )
            await message.answer(
                _t(db_user, 'ADMIN_SERVER_LIMIT_UPDATED', limit=limit_text),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=_t(db_user, 'ADMIN_SERVER_TO_SERVER'),
                                callback_data=f'admin_server_edit_{server_id}',
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer(_t(db_user, 'ADMIN_SERVER_UPDATE_ERROR'))

    except ValueError:
        await message.answer(_t(db_user, 'ADMIN_SERVER_LIMIT_FORMAT_INVALID'))


@admin_required
@error_handler
async def start_server_edit_description(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession
):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_description)

    current_desc = server.description or _t(db_user, 'ADMIN_SERVER_NOT_SPECIFIED')

    await callback.message.edit_text(
        _t(db_user, 'ADMIN_SERVER_EDIT_DESCRIPTION_PROMPT', current_description=current_desc),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_SERVER_CANCEL'), callback_data=f'admin_server_edit_{server_id}'
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_description_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    new_description = message.text.strip()

    if new_description == '-':
        new_description = None
    elif len(new_description) > 1000:
        await message.answer(_t(db_user, 'ADMIN_SERVER_DESC_TOO_LONG'))
        return

    server = await update_server_squad(db, server_id, description=new_description)

    if server:
        await state.clear()

        desc_text = new_description or _t(db_user, 'ADMIN_SERVER_REMOVED')
        await cache.delete_pattern('available_countries*')
        await message.answer(
            _t(db_user, 'ADMIN_SERVER_DESCRIPTION_UPDATED', description=desc_text),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_SERVER_TO_SERVER'), callback_data=f'admin_server_edit_{server_id}'
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(_t(db_user, 'ADMIN_SERVER_UPDATE_ERROR'))


@admin_required
@error_handler
async def start_server_edit_promo_groups(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND_ALERT'), show_alert=True)
        return

    promo_groups_data = await get_promo_groups_with_counts(db)
    promo_groups = [
        {'id': group.id, 'name': group.name, 'is_default': group.is_default} for group, _ in promo_groups_data
    ]

    if not promo_groups:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_PROMO_GROUPS_NOT_FOUND'), show_alert=True)
        return

    selected_ids = {pg.id for pg in server.allowed_promo_groups}
    if not selected_ids:
        default_group = next((pg for pg in promo_groups if pg['is_default']), None)
        if default_group:
            selected_ids.add(default_group['id'])

    await state.set_state(AdminStates.editing_server_promo_groups)
    await state.set_data(
        {
            'server_id': server_id,
            'promo_groups': promo_groups,
            'selected_promo_groups': list(selected_ids),
            'server_name': server.display_name,
        }
    )

    text = _t(db_user, 'ADMIN_SERVER_PROMO_CONFIG', server_name=server.display_name)

    await callback.message.edit_text(
        text,
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected_ids, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_server_promo_group(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    parts = callback.data.split('_')
    server_id = int(parts[4])
    group_id = int(parts[5])

    data = await state.get_data()
    if not data or data.get('server_id') != server_id:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_EDIT_SESSION_EXPIRED'), show_alert=True)
        return

    selected = {int(pg_id) for pg_id in data.get('selected_promo_groups', [])}
    promo_groups = data.get('promo_groups', [])

    if group_id in selected:
        if len(selected) == 1:
            await callback.answer(_t(db_user, 'ADMIN_SERVER_PROMO_LAST_REQUIRED'), show_alert=True)
            return
        selected.remove(group_id)
        message = _t(db_user, 'ADMIN_SERVER_PROMO_GROUP_DISABLED')
    else:
        selected.add(group_id)
        message = _t(db_user, 'ADMIN_SERVER_PROMO_GROUP_ADDED')

    await state.update_data(selected_promo_groups=list(selected))

    await callback.message.edit_reply_markup(
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected, db_user.language)
    )
    await callback.answer(message)


@admin_required
@error_handler
async def save_server_promo_groups(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    data = await state.get_data()
    if not data:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NO_DATA_TO_SAVE'), show_alert=True)
        return

    server_id = data.get('server_id')
    selected = data.get('selected_promo_groups', [])

    if not selected:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_SELECT_PROMO_GROUP'), show_alert=True)
        return

    try:
        server = await update_server_squad_promo_groups(db, server_id, selected)
    except ValueError as exc:
        await callback.answer(f'❌ {exc}', show_alert=True)
        return

    if not server:
        await callback.answer(_t(db_user, 'ADMIN_SERVER_NOT_FOUND'), show_alert=True)
        return

    await cache.delete_pattern('available_countries*')
    await state.clear()

    text, keyboard = _build_server_edit_view(server, db_user.language)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer(_t(db_user, 'ADMIN_SERVER_PROMO_GROUPS_UPDATED'))


@admin_required
@error_handler
async def sync_server_user_counts_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(_t(db_user, 'ADMIN_SERVER_SYNC_COUNTS_PROGRESS'), reply_markup=None)

    try:
        from app.database.crud.server_squad import sync_server_user_counts

        updated_count = await sync_server_user_counts(db)

        text = _t(db_user, 'ADMIN_SERVER_SYNC_COUNTS_DONE', updated_count=updated_count)

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=_t(db_user, 'ADMIN_SERVER_LIST_BUTTON'), callback_data='admin_servers_list'
                ),
                types.InlineKeyboardButton(
                    text=_t(db_user, 'ADMIN_SERVER_RETRY_BUTTON'), callback_data='admin_servers_sync_counts'
                ),
            ],
            [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_SERVER_BACK'), callback_data='admin_servers')],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error(f'Ошибка синхронизации счетчиков: {e}')
        await callback.message.edit_text(
            _t(db_user, 'ADMIN_SERVER_SYNC_ERROR', error=e),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=_t(db_user, 'ADMIN_SERVER_BACK'), callback_data='admin_servers')]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def handle_servers_pagination(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = int(callback.data.split('_')[-1])
    await show_servers_list(callback, db_user, db, page)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_servers_menu, F.data == 'admin_servers')
    dp.callback_query.register(show_servers_list, F.data == 'admin_servers_list')
    dp.callback_query.register(sync_servers_with_remnawave, F.data == 'admin_servers_sync')
    dp.callback_query.register(sync_server_user_counts_handler, F.data == 'admin_servers_sync_counts')
    dp.callback_query.register(show_server_detailed_stats, F.data == 'admin_servers_stats')

    dp.callback_query.register(
        show_server_edit_menu,
        F.data.startswith('admin_server_edit_')
        & ~F.data.contains('name')
        & ~F.data.contains('price')
        & ~F.data.contains('country')
        & ~F.data.contains('limit')
        & ~F.data.contains('desc')
        & ~F.data.contains('promo'),
    )
    dp.callback_query.register(toggle_server_availability, F.data.startswith('admin_server_toggle_'))
    dp.callback_query.register(toggle_server_trial_assignment, F.data.startswith('admin_server_trial_'))
    dp.callback_query.register(show_server_users, F.data.startswith('admin_server_users_'))

    dp.callback_query.register(start_server_edit_name, F.data.startswith('admin_server_edit_name_'))
    dp.callback_query.register(start_server_edit_price, F.data.startswith('admin_server_edit_price_'))
    dp.callback_query.register(start_server_edit_country, F.data.startswith('admin_server_edit_country_'))
    dp.callback_query.register(start_server_edit_promo_groups, F.data.startswith('admin_server_edit_promo_'))
    dp.callback_query.register(start_server_edit_limit, F.data.startswith('admin_server_edit_limit_'))
    dp.callback_query.register(start_server_edit_description, F.data.startswith('admin_server_edit_desc_'))

    dp.message.register(process_server_name_edit, AdminStates.editing_server_name)
    dp.message.register(process_server_price_edit, AdminStates.editing_server_price)
    dp.message.register(process_server_country_edit, AdminStates.editing_server_country)
    dp.message.register(process_server_limit_edit, AdminStates.editing_server_limit)
    dp.message.register(process_server_description_edit, AdminStates.editing_server_description)
    dp.callback_query.register(toggle_server_promo_group, F.data.startswith('admin_server_promo_toggle_'))
    dp.callback_query.register(save_server_promo_groups, F.data.startswith('admin_server_promo_save_'))

    dp.callback_query.register(
        delete_server_confirm, F.data.startswith('admin_server_delete_') & ~F.data.contains('confirm')
    )
    dp.callback_query.register(delete_server_execute, F.data.startswith('admin_server_delete_confirm_'))

    dp.callback_query.register(handle_servers_pagination, F.data.startswith('admin_servers_list_page_'))
