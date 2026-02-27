import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

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


logger = structlog.get_logger(__name__)


def _build_server_edit_view(server, texts):
    status_emoji = (
        texts.t('ADMIN_SQUAD_MIGRATION_STATUS_AVAILABLE', '✅ Доступен')
        if server.is_available
        else texts.t('ADMIN_SERVER_STATUS_UNAVAILABLE', '❌ Недоступен')
    )
    price_text = (
        f'{int(server.price_rubles)} ₽'
        if server.price_kopeks > 0
        else texts.t('DEVICE_CHANGE_FREE', 'Бесплатно')
    )
    promo_groups_text = (
        ', '.join(sorted(pg.name for pg in server.allowed_promo_groups))
        if server.allowed_promo_groups
        else texts.t('ADMIN_SERVER_PROMO_GROUPS_NONE', 'Не выбраны')
    )

    trial_status = (
        texts.t('YES', '✅ Да')
        if server.is_trial_eligible
        else texts.t('ADMIN_SERVER_TRIAL_NO', '⚪️ Нет')
    )

    text = texts.t(
        'ADMIN_SERVER_EDIT_VIEW_TEXT',
        '🌐 <b>Редактирование сервера</b>\n\n'
        '<b>Информация:</b>\n'
        '• ID: {id}\n'
        '• UUID: <code>{uuid}</code>\n'
        '• Название: {display_name}\n'
        '• Оригинальное: {original_name}\n'
        '• Статус: {status}\n\n'
        '<b>Настройки:</b>\n'
        '• Цена: {price}\n'
        '• Код страны: {country_code}\n'
        '• Лимит пользователей: {max_users}\n'
        '• Текущих пользователей: {current_users}\n'
        '• Промогруппы: {promo_groups}\n'
        '• Выдача триала: {trial_status}\n\n'
        '<b>Описание:</b>\n'
        '{description}\n\n'
        'Выберите что изменить:',
    ).format(
        id=server.id,
        uuid=server.squad_uuid,
        display_name=server.display_name,
        original_name=server.original_name or texts.t('ADMIN_SERVER_NOT_SPECIFIED', 'Не указано'),
        status=status_emoji,
        price=price_text,
        country_code=server.country_code or texts.t('ADMIN_SERVER_COUNTRY_NOT_SET', 'Не указан'),
        max_users=server.max_users or texts.t('ADMIN_RW_NO_LIMIT', 'Без лимита'),
        current_users=server.current_users,
        promo_groups=promo_groups_text,
        trial_status=trial_status,
        description=server.description or texts.t('ADMIN_SERVER_NOT_SPECIFIED', 'Не указано'),
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_NAME', '✏️ Название'),
                callback_data=f'admin_server_edit_name_{server.id}',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_PRICE', '💰 Цена'),
                callback_data=f'admin_server_edit_price_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_COUNTRY', '🌍 Страна'),
                callback_data=f'admin_server_edit_country_{server.id}',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_LIMIT', '👥 Лимит'),
                callback_data=f'admin_server_edit_limit_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_USERS_BUTTON', '👥 Юзеры'),
                callback_data=f'admin_server_users_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=(
                    texts.t('ADMIN_SERVER_TRIAL_ASSIGN', '🎁 Выдавать сквад')
                    if not server.is_trial_eligible
                    else texts.t('ADMIN_SERVER_TRIAL_UNASSIGN', '🚫 Не выдавать сквад')
                ),
                callback_data=f'admin_server_trial_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_PROMO_GROUPS_BUTTON', '🎯 Промогруппы'),
                callback_data=f'admin_server_edit_promo_{server.id}',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_EDIT_DESCRIPTION', '📝 Описание'),
                callback_data=f'admin_server_edit_desc_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=(
                    texts.t('ADMIN_SERVER_DISABLE', '❌ Отключить')
                    if server.is_available
                    else texts.t('ADMIN_SERVER_ENABLE', '✅ Включить')
                ),
                callback_data=f'admin_server_toggle_{server.id}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_DELETE', '🗑️ Удалить'),
                callback_data=f'admin_server_delete_{server.id}',
            ),
            types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_servers_list'),
        ],
    ]

    return text, types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _build_server_promo_groups_keyboard(server_id: int, promo_groups, selected_ids, texts):
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
                text=texts.t('ADMIN_SERVER_SAVE_PROMO_GROUPS', '💾 Сохранить'),
                callback_data=f'admin_server_promo_save_{server_id}',
            )
        ]
    )
    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_server_edit_{server_id}')])

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


@admin_required
@error_handler
async def show_servers_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_server_statistics(db)

    text = texts.t(
        'ADMIN_SERVERS_MENU_TEXT',
        '🌐 <b>Управление серверами</b>\n\n'
        '📊 <b>Статистика:</b>\n'
        '• Всего серверов: {total_servers}\n'
        '• Доступные: {available_servers}\n'
        '• Недоступные: {unavailable_servers}\n'
        '• С подключениями: {servers_with_connections}\n\n'
        '💰 <b>Выручка от серверов:</b>\n'
        '• Общая: {total_revenue} ₽\n\n'
        'Выберите действие:',
    ).format(
        total_servers=stats['total_servers'],
        available_servers=stats['available_servers'],
        unavailable_servers=stats['unavailable_servers'],
        servers_with_connections=stats['servers_with_connections'],
        total_revenue=int(stats['total_revenue_rubles']),
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_LIST', '📋 Список серверов'),
                callback_data='admin_servers_list',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_SYNC', '🔄 Синхронизация'),
                callback_data='admin_servers_sync',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_SYNC_COUNTS', '📊 Синхронизировать счетчики'),
                callback_data='admin_servers_sync_counts',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_DETAILED_STATS', '📈 Подробная статистика'),
                callback_data='admin_servers_stats',
            ),
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_servers_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    texts = get_texts(db_user.language)
    servers, total_count = await get_all_server_squads(db, page=page, limit=10)
    total_pages = (total_count + 9) // 10

    if not servers:
        text = texts.t('ADMIN_SERVERS_LIST_EMPTY', '🌐 <b>Список серверов</b>\n\n❌ Серверы не найдены.')
    else:
        text = texts.t('ADMIN_SERVERS_LIST_TITLE', '🌐 <b>Список серверов</b>\n\n')
        text += texts.t('ADMIN_SERVERS_LIST_META', '📊 Всего: {total} | Страница: {page}/{pages}\n\n').format(
            total=total_count, page=page, pages=total_pages
        )

        for i, server in enumerate(servers, 1 + (page - 1) * 10):
            status_emoji = '✅' if server.is_available else '❌'
            price_text = (
                f'{int(server.price_rubles)} ₽'
                if server.price_kopeks > 0
                else texts.t('DEVICE_CHANGE_FREE', 'Бесплатно')
            )

            text += f'{i}. {status_emoji} {server.display_name}\n'
            text += texts.t('ADMIN_SERVERS_LIST_PRICE_LINE', '   💰 Цена: {price}').format(price=price_text)

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

    keyboard.extend([[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_servers')]])

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def sync_servers_with_remnawave(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVERS_SYNC_PROGRESS',
            '🔄 Синхронизация с Remnawave...\n\nПодождите, это может занять время.',
        ),
        reply_markup=None,
    )

    try:
        remnawave_service = RemnaWaveService()
        squads = await remnawave_service.get_all_squads()

        if not squads:
            await callback.message.edit_text(
                texts.t(
                    'ADMIN_SERVERS_SYNC_NO_SQUADS',
                    '❌ Не удалось получить данные о сквадах из Remnawave.\n\nПроверьте настройки API.',
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_servers')]]
                ),
            )
            return

        created, updated, removed = await sync_with_remnawave(db, squads)

        await cache.delete_pattern('available_countries*')

        text = texts.t(
            'ADMIN_SERVERS_SYNC_RESULT_TEXT',
            '✅ <b>Синхронизация завершена</b>\n\n'
            '📊 <b>Результаты:</b>\n'
            '• Создано новых серверов: {created}\n'
            '• Обновлено существующих: {updated}\n'
            '• Удалено отсутствующих: {removed}\n'
            '• Всего обработано: {total}\n\n'
            'ℹ️ Новые серверы созданы как недоступные.\n'
            'Настройте их в списке серверов.',
        ).format(created=created, updated=updated, removed=removed, total=len(squads))

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_LIST', '📋 Список серверов'),
                    callback_data='admin_servers_list',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SYNC_RETRY', '🔄 Повторить'),
                    callback_data='admin_servers_sync',
                ),
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_servers')],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Ошибка синхронизации серверов', error=e)
        await callback.message.edit_text(
            texts.t('ADMIN_SERVERS_SYNC_ERROR', '❌ Ошибка синхронизации: {error}').format(error=e),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_servers')]]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def show_server_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    text, keyboard = _build_server_edit_view(server, texts)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_server_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    payload = callback.data.split('admin_server_users_', 1)[-1]
    payload_parts = payload.split('_')

    server_id = int(payload_parts[0])
    page = int(payload_parts[1]) if len(payload_parts) > 1 else 1
    page = max(page, 1)
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    users = await get_server_connected_users(db, server_id)
    total_users = len(users)

    page_size = 10
    total_pages = max((total_users + page_size - 1) // page_size, 1)

    page = min(page, total_pages)

    start_index = (page - 1) * page_size
    end_index = start_index + page_size
    page_users = users[start_index:end_index]

    dash = texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')
    safe_name = html.escape(server.display_name or dash)
    safe_uuid = html.escape(server.squad_uuid or dash)

    header = [
        texts.t('ADMIN_SERVER_USERS_TITLE', '🌐 <b>Пользователи сервера</b>'),
        '',
        texts.t('ADMIN_SERVER_USERS_SERVER_LINE', '• Сервер: {name}').format(name=safe_name),
        f'• UUID: <code>{safe_uuid}</code>',
        texts.t('ADMIN_SERVER_USERS_CONNECTIONS_LINE', '• Подключений: {count}').format(count=total_users),
    ]

    if total_pages > 1:
        header.append(texts.t('ADMIN_SERVER_USERS_PAGE_LINE', '• Страница: {page}/{pages}').format(page=page, pages=total_pages))

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
        text += texts.t('ADMIN_SERVER_USERS_NOT_FOUND', 'Пользователи не найдены.')

    keyboard: list[list[types.InlineKeyboardButton]] = []

    for user in page_users:
        display_name = user.full_name
        if len(display_name) > 30:
            display_name = display_name[:27] + '...'

        subscription_status = (
            user.subscription.status_display
            if user.subscription
            else texts.t('ADMIN_SERVER_USERS_NO_SUBSCRIPTION', '❌ Нет подписки')
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
                    text=texts.t('ADMIN_SERVER_USERS_PREV', '⬅️ Предыдущая'),
                    callback_data=f'admin_server_users_{server_id}_{page - 1}',
                )
            )

        navigation_buttons.append(
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SQUAD_MIGRATION_PAGE', 'Стр. {page}/{pages}').format(
                    page=page, pages=total_pages
                ),
                callback_data=f'admin_server_users_{server_id}_{page}',
            )
        )

        if page < total_pages:
            navigation_buttons.append(
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVER_USERS_NEXT', 'Следующая ➡️'),
                    callback_data=f'admin_server_users_{server_id}_{page + 1}',
                )
            )

        keyboard.append(navigation_buttons)

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_USERS_BACK_TO_SERVER', '⬅️ К серверу'),
                callback_data=f'admin_server_edit_{server_id}',
            )
        ]
    )

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_BACK_TO_LIST', '⬅️ К списку'),
                callback_data='admin_servers_list',
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
async def toggle_server_availability(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    new_status = not server.is_available
    await update_server_squad(db, server_id, is_available=new_status)

    await cache.delete_pattern('available_countries*')

    status_text = (
        texts.t('AUTOPAY_STATUS_ENABLED', 'включен')
        if new_status
        else texts.t('ADMIN_SERVER_STATUS_DISABLED_WORD', 'отключен')
    )
    await callback.answer(texts.t('ADMIN_SERVER_TOGGLE_SUCCESS', '✅ Сервер {status}!').format(status=status_text))

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server, texts)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def toggle_server_trial_assignment(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    new_status = not server.is_trial_eligible
    await update_server_squad(db, server_id, is_trial_eligible=new_status)

    status_text = (
        texts.t('ADMIN_SERVER_TRIAL_STATUS_ENABLED', 'будет выдаваться')
        if new_status
        else texts.t('ADMIN_SERVER_TRIAL_STATUS_DISABLED', 'перестанет выдаваться')
    )
    await callback.answer(
        texts.t('ADMIN_SERVER_TRIAL_TOGGLE_SUCCESS', '✅ Сквад {status} в триал').format(status=status_text)
    )

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server, texts)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def start_server_edit_price(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_price)

    current_price = (
        f'{int(server.price_rubles)} ₽'
        if server.price_kopeks > 0
        else texts.t('DEVICE_CHANGE_FREE', 'Бесплатно')
    )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVER_EDIT_PRICE_TEXT',
            '💰 <b>Редактирование цены</b>\n\n'
            'Текущая цена: <b>{price}</b>\n\n'
            'Отправьте новую цену в рублях (например: 15.50) или 0 для бесплатного доступа:',
        ).format(price=current_price),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_server_edit_{server_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_price_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    server_id = data.get('server_id')

    try:
        price_rubles = float((message.text or '').replace(',', '.'))

        if price_rubles < 0:
            await message.answer(texts.t('ADMIN_SERVER_PRICE_NEGATIVE', '❌ Цена не может быть отрицательной'))
            return

        if price_rubles > 10000:
            await message.answer(texts.t('ADMIN_SERVER_PRICE_TOO_HIGH', '❌ Слишком высокая цена (максимум 10,000 ₽)'))
            return

        price_kopeks = int(price_rubles * 100)

        server = await update_server_squad(db, server_id, price_kopeks=price_kopeks)

        if server:
            await state.clear()

            await cache.delete_pattern('available_countries*')

            price_text = (
                f'{int(price_rubles)} ₽'
                if price_kopeks > 0
                else texts.t('DEVICE_CHANGE_FREE', 'Бесплатно')
            )
            await message.answer(
                texts.t('ADMIN_SERVER_PRICE_UPDATED', '✅ Цена сервера изменена на: <b>{price}</b>').format(price=price_text),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_SERVER_BACK_TO_SERVER', '🔙 К серверу'),
                                callback_data=f'admin_server_edit_{server_id}',
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer(texts.t('ADMIN_SERVER_UPDATE_ERROR', '❌ Ошибка при обновлении сервера'))

    except ValueError:
        await message.answer(
            texts.t('ADMIN_SERVER_PRICE_INVALID_FORMAT', '❌ Неверный формат цены. Используйте числа (например: 15.50)')
        )


@admin_required
@error_handler
async def start_server_edit_name(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_name)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVER_EDIT_NAME_TEXT',
            '✏️ <b>Редактирование названия</b>\n\n'
            'Текущее название: <b>{name}</b>\n\n'
            'Отправьте новое название для сервера:',
        ).format(name=server.display_name),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_server_edit_{server_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_name_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    server_id = data.get('server_id')

    new_name = (message.text or '').strip()

    if len(new_name) > 255:
        await message.answer(texts.t('ADMIN_SERVER_NAME_TOO_LONG', '❌ Название слишком длинное (максимум 255 символов)'))
        return

    if len(new_name) < 3:
        await message.answer(texts.t('ADMIN_SERVER_NAME_TOO_SHORT', '❌ Название слишком короткое (минимум 3 символа)'))
        return

    server = await update_server_squad(db, server_id, display_name=new_name)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        await message.answer(
            texts.t('ADMIN_SERVER_NAME_UPDATED', '✅ Название сервера изменено на: <b>{name}</b>').format(name=new_name),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVER_BACK_TO_SERVER', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(texts.t('ADMIN_SERVER_UPDATE_ERROR', '❌ Ошибка при обновлении сервера'))


@admin_required
@error_handler
async def delete_server_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    text = texts.t(
        'ADMIN_SERVER_DELETE_CONFIRM_TEXT',
        '🗑️ <b>Удаление сервера</b>\n\n'
        'Вы действительно хотите удалить сервер:\n'
        '<b>{name}</b>\n\n'
        '⚠️ <b>Внимание!</b>\n'
        'Сервер можно удалить только если к нему нет активных подключений.\n\n'
        'Это действие нельзя отменить!',
    ).format(name=server.display_name)

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVER_DELETE_CONFIRM_BUTTON', '🗑️ Да, удалить'),
                callback_data=f'admin_server_delete_confirm_{server_id}',
            ),
            types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_server_edit_{server_id}'),
        ]
    ]

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_server_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    success = await delete_server_squad(db, server_id)

    if success:
        await cache.delete_pattern('available_countries*')

        await callback.message.edit_text(
            texts.t('ADMIN_SERVER_DELETE_SUCCESS', '✅ Сервер <b>{name}</b> успешно удален!').format(
                name=server.display_name
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVER_TO_LIST', '📋 К списку серверов'),
                            callback_data='admin_servers_list',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_SERVER_DELETE_FAIL',
                '❌ Не удалось удалить сервер <b>{name}</b>\n\n'
                'Возможно, к нему есть активные подключения.',
            ).format(name=server.display_name),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVER_BACK_TO_SERVER', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
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
    texts = get_texts(db_user.language)
    stats = await get_server_statistics(db)
    available_servers = await get_available_server_squads(db)

    text = texts.t(
        'ADMIN_SERVERS_DETAILED_STATS_TEXT',
        '📊 <b>Подробная статистика серверов</b>\n\n'
        '<b>🌐 Общая информация:</b>\n'
        '• Всего серверов: {total_servers}\n'
        '• Доступные: {available_servers}\n'
        '• Недоступные: {unavailable_servers}\n'
        '• С активными подключениями: {with_connections}\n\n'
        '<b>💰 Финансовая статистика:</b>\n'
        '• Общая выручка: {total_revenue} ₽\n'
        '• Средняя цена за сервер: {avg_price} ₽\n\n'
        '<b>🔥 Топ серверов по цене:</b>\n',
    ).format(
        total_servers=stats['total_servers'],
        available_servers=stats['available_servers'],
        unavailable_servers=stats['unavailable_servers'],
        with_connections=stats['servers_with_connections'],
        total_revenue=int(stats['total_revenue_rubles']),
        avg_price=int(stats['total_revenue_rubles'] / max(stats['servers_with_connections'], 1)),
    )

    sorted_servers = sorted(available_servers, key=lambda x: x.price_kopeks, reverse=True)

    for i, server in enumerate(sorted_servers[:5], 1):
        price_text = (
            f'{int(server.price_rubles)} ₽'
            if server.price_kopeks > 0
            else texts.t('DEVICE_CHANGE_FREE', 'Бесплатно')
        )
        text += f'{i}. {server.display_name} - {price_text}\n'

    if not sorted_servers:
        text += texts.t('ADMIN_SERVERS_NO_AVAILABLE', 'Нет доступных серверов\n')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'),
                callback_data='admin_servers_stats',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SERVERS_LIST_SHORT', '📋 Список'),
                callback_data='admin_servers_list',
            ),
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_servers')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_server_edit_country(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_country)

    current_country = server.country_code or texts.t('ADMIN_SERVER_COUNTRY_NOT_SET', 'Не указан')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVER_EDIT_COUNTRY_TEXT',
            '🌍 <b>Редактирование кода страны</b>\n\n'
            'Текущий код страны: <b>{country}</b>\n\n'
            "Отправьте новый код страны (например: RU, US, DE) или '-' для удаления:",
        ).format(country=current_country),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_server_edit_{server_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_country_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    server_id = data.get('server_id')

    new_country = (message.text or '').strip().upper()

    if new_country == '-':
        new_country = None
    elif len(new_country) > 5:
        await message.answer(
            texts.t('ADMIN_SERVER_COUNTRY_TOO_LONG', '❌ Код страны слишком длинный (максимум 5 символов)')
        )
        return

    server = await update_server_squad(db, server_id, country_code=new_country)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        country_text = new_country or texts.t('ADMIN_SERVER_DELETED_WORD', 'Удален')
        await message.answer(
            texts.t('ADMIN_SERVER_COUNTRY_UPDATED', '✅ Код страны изменен на: <b>{country}</b>').format(
                country=country_text
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVER_BACK_TO_SERVER', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(texts.t('ADMIN_SERVER_UPDATE_ERROR', '❌ Ошибка при обновлении сервера'))


@admin_required
@error_handler
async def start_server_edit_limit(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_limit)

    current_limit = server.max_users or texts.t('ADMIN_RW_NO_LIMIT', 'Без лимита')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVER_EDIT_LIMIT_TEXT',
            '👥 <b>Редактирование лимита пользователей</b>\n\n'
            'Текущий лимит: <b>{limit}</b>\n\n'
            'Отправьте новый лимит пользователей (число) или 0 для безлимитного доступа:',
        ).format(limit=current_limit),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_server_edit_{server_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_limit_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    server_id = data.get('server_id')

    try:
        limit = int((message.text or '').strip())

        if limit < 0:
            await message.answer(texts.t('ADMIN_SERVER_LIMIT_NEGATIVE', '❌ Лимит не может быть отрицательным'))
            return

        if limit > 10000:
            await message.answer(texts.t('ADMIN_SERVER_LIMIT_TOO_HIGH', '❌ Слишком большой лимит (максимум 10,000)'))
            return

        max_users = limit if limit > 0 else None

        server = await update_server_squad(db, server_id, max_users=max_users)

        if server:
            await state.clear()

            limit_text = (
                texts.t('ADMIN_SERVER_LIMIT_USERS', '{count} пользователей').format(count=limit)
                if limit > 0
                else texts.t('ADMIN_RW_NO_LIMIT', 'Без лимита')
            )
            await message.answer(
                texts.t('ADMIN_SERVER_LIMIT_UPDATED', '✅ Лимит пользователей изменен на: <b>{limit}</b>').format(
                    limit=limit_text
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_SERVER_BACK_TO_SERVER', '🔙 К серверу'),
                                callback_data=f'admin_server_edit_{server_id}',
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer(texts.t('ADMIN_SERVER_UPDATE_ERROR', '❌ Ошибка при обновлении сервера'))

    except ValueError:
        await message.answer(
            texts.t('ADMIN_SERVER_LIMIT_INVALID_FORMAT', '❌ Неверный формат числа. Введите целое число.')
        )


@admin_required
@error_handler
async def start_server_edit_description(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession
):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_description)

    current_desc = server.description or texts.t('ADMIN_SERVER_NOT_SPECIFIED', 'Не указано')

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SERVER_EDIT_DESCRIPTION_TEXT',
            '📝 <b>Редактирование описания</b>\n\n'
            'Текущее описание:\n<i>{description}</i>\n\n'
            "Отправьте новое описание сервера или '-' для удаления:",
        ).format(description=current_desc),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_server_edit_{server_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_server_description_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    server_id = data.get('server_id')

    new_description = (message.text or '').strip()

    if new_description == '-':
        new_description = None
    elif len(new_description) > 1000:
        await message.answer(
            texts.t('ADMIN_SERVER_DESCRIPTION_TOO_LONG', '❌ Описание слишком длинное (максимум 1000 символов)')
        )
        return

    server = await update_server_squad(db, server_id, description=new_description)

    if server:
        await state.clear()

        desc_text = new_description or texts.t('ADMIN_CAMPAIGNS_AUTO_086', 'Удалено')
        await cache.delete_pattern('available_countries*')
        await message.answer(
            texts.t('ADMIN_SERVER_DESCRIPTION_UPDATED', '✅ Описание сервера изменено:\n\n<i>{description}</i>').format(
                description=desc_text
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SERVER_BACK_TO_SERVER', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(texts.t('ADMIN_SERVER_UPDATE_ERROR', '❌ Ошибка при обновлении сервера'))


@admin_required
@error_handler
async def start_server_edit_promo_groups(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    promo_groups_data = await get_promo_groups_with_counts(db)
    promo_groups = [
        {'id': group.id, 'name': group.name, 'is_default': group.is_default} for group, _ in promo_groups_data
    ]

    if not promo_groups:
        await callback.answer(texts.t('ADMIN_SERVER_PROMO_GROUPS_NOT_FOUND', '❌ Не найдены промогруппы'), show_alert=True)
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

    text = (
        texts.t(
            'ADMIN_SERVER_PROMO_GROUPS_EDIT_TEXT',
            '🎯 <b>Настройка промогрупп</b>\n\n'
            'Сервер: <b>{name}</b>\n\n'
            'Выберите промогруппы, которым будет доступен этот сервер.\n'
            'Должна быть выбрана минимум одна промогруппа.',
        ).format(name=server.display_name)
    )

    await callback.message.edit_text(
        text,
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected_ids, texts),
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
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    server_id = int(parts[4])
    group_id = int(parts[5])

    data = await state.get_data()
    if not data or data.get('server_id') != server_id:
        await callback.answer(texts.t('ADMIN_SERVER_PROMO_SESSION_EXPIRED', '⚠️ Сессия редактирования устарела'), show_alert=True)
        return

    selected = {int(pg_id) for pg_id in data.get('selected_promo_groups', [])}
    promo_groups = data.get('promo_groups', [])

    if group_id in selected:
        if len(selected) == 1:
            await callback.answer(
                texts.t('ADMIN_SERVER_PROMO_LAST_GROUP_WARNING', '⚠️ Нельзя отключить последнюю промогруппу'),
                show_alert=True,
            )
            return
        selected.remove(group_id)
        message = texts.t('ADMIN_SERVER_PROMO_GROUP_DISABLED', 'Промогруппа отключена')
    else:
        selected.add(group_id)
        message = texts.t('ADMIN_SERVER_PROMO_GROUP_ADDED', 'Промогруппа добавлена')

    await state.update_data(selected_promo_groups=list(selected))

    await callback.message.edit_reply_markup(
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected, texts)
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
    texts = get_texts(db_user.language)
    data = await state.get_data()
    if not data:
        await callback.answer(texts.t('ADMIN_SERVER_PROMO_NO_DATA', '⚠️ Нет данных для сохранения'), show_alert=True)
        return

    server_id = data.get('server_id')
    selected = data.get('selected_promo_groups', [])

    if not selected:
        await callback.answer(
            texts.t('ADMIN_SERVER_PROMO_SELECT_AT_LEAST_ONE', '❌ Выберите хотя бы одну промогруппу'),
            show_alert=True,
        )
        return

    try:
        server = await update_server_squad_promo_groups(db, server_id, selected)
    except ValueError as exc:
        await callback.answer(f'❌ {exc}', show_alert=True)
        return

    if not server:
        await callback.answer(texts.t('ADMIN_SERVER_NOT_FOUND_ALERT', '❌ Сервер не найден!'), show_alert=True)
        return

    await cache.delete_pattern('available_countries*')
    await state.clear()

    text, keyboard = _build_server_edit_view(server, texts)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer(texts.t('ADMIN_SERVER_PROMO_GROUPS_UPDATED', '✅ Промогруппы обновлены!'))


@admin_required
@error_handler
async def sync_server_user_counts_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_SERVERS_SYNC_COUNTS_PROGRESS', '🔄 Синхронизация счетчиков пользователей...'),
        reply_markup=None,
    )

    try:
        from app.database.crud.server_squad import sync_server_user_counts

        updated_count = await sync_server_user_counts(db)

        text = texts.t(
            'ADMIN_SERVERS_SYNC_COUNTS_RESULT_TEXT',
            '✅ <b>Синхронизация завершена</b>\n\n'
            '📊 <b>Результат:</b>\n'
            '• Обновлено серверов: {updated_count}\n\n'
            'Счетчики пользователей синхронизированы с реальными данными.',
        ).format(updated_count=updated_count)

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SERVERS_LIST', '📋 Список серверов'),
                    callback_data='admin_servers_list',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SYNC_RETRY', '🔄 Повторить'),
                    callback_data='admin_servers_sync_counts',
                ),
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_servers')],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Ошибка синхронизации счетчиков', error=e)
        await callback.message.edit_text(
            texts.t('ADMIN_SERVERS_SYNC_ERROR', '❌ Ошибка синхронизации: {error}').format(error=e),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_servers')]]
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
