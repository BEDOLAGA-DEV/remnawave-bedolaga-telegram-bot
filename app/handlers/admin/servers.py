import html

import structlog
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
from app.services.remnawave_service import RemnaWaveService
from app.states import AdminStates
from app.utils.cache import cache
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _build_server_edit_view(server):
    status_emoji = '✅ Доступен' if server.is_available else '❌ Недоступен'
    price_text = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else 'Бесплатно'
    promo_groups_text = (
        ', '.join(sorted(pg.name for pg in server.allowed_promo_groups))
        if server.allowed_promo_groups
        else 'Не выбраны'
    )

    trial_status = '✅ Да' if server.is_trial_eligible else '⚪️ Нет'

    text = f"""
🌐 <b>Редактирование сервера</b>

<b>Информация:</b>
• ID: {server.id}
• UUID: <code>{server.squad_uuid}</code>
• Название: {html.escape(server.display_name)}
• Оригинальное: {html.escape(server.original_name) if server.original_name else 'Не указано'}
• Статус: {status_emoji}

<b>Настройки:</b>
• Цена: {price_text}
• Код страны: {server.country_code or 'Не указан'}
• Лимит пользователей: {server.max_users or 'Без лимита'}
• Текущих пользователей: {server.current_users}
• Промогруппы: {promo_groups_text}
• Выдача триала: {trial_status}

<b>Описание:</b>
{server.description or 'Не указано'}

Выберите что изменить:
"""

    keyboard = [
        [
            types.InlineKeyboardButton(text='✏️ Название', callback_data=f'admin_server_edit_name_{server.id}'),
            types.InlineKeyboardButton(text='💰 Цена', callback_data=f'admin_server_edit_price_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(text='🌍 Страна', callback_data=f'admin_server_edit_country_{server.id}'),
            types.InlineKeyboardButton(text='👥 Лимит', callback_data=f'admin_server_edit_limit_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(text='👥 Юзеры', callback_data=f'admin_server_users_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(
                text='🎁 Выдавать в триал' if not server.is_trial_eligible else '🚫 Не выдавать в триал',
                callback_data=f'admin_server_trial_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(text='🎯 Промогруппы', callback_data=f'admin_server_edit_promo_{server.id}'),
            types.InlineKeyboardButton(text='📝 Описание', callback_data=f'admin_server_edit_desc_{server.id}'),
        ],
        [
            types.InlineKeyboardButton(
                text='❌ Отключить' if server.is_available else '✅ Включить',
                callback_data=f'admin_server_toggle_{server.id}',
            )
        ],
        [
            types.InlineKeyboardButton(text='🗑️ Удалить', callback_data=f'admin_server_delete_{server.id}'),
            types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_servers_list'),
        ],
    ]

    return text, types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _build_server_promo_groups_keyboard(server_id: int, promo_groups, selected_ids):
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
        [types.InlineKeyboardButton(text='💾 Сохранить', callback_data=f'admin_server_promo_save_{server_id}')]
    )
    keyboard.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data=f'admin_server_edit_{server_id}')])

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


@admin_required
@error_handler
async def show_servers_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_server_statistics(db)

    text = f"""
🌐 <b>Управление серверами</b>

📊 <b>Статистика:</b>
• Всего серверов: {stats['total_servers']}
• Доступные: {stats['available_servers']}
• Недоступные: {stats['unavailable_servers']}
• С подключениями: {stats['servers_with_connections']}

💰 <b>Выручка от серверов:</b>
• Общая: {int(stats['total_revenue_rubles'])} ₽

Выберите действие:
"""

    keyboard = [
        [
            types.InlineKeyboardButton(text='📋 Список серверов', callback_data='admin_servers_list'),
            types.InlineKeyboardButton(text='🔄 Синхронизация', callback_data='admin_servers_sync'),
        ],
        [
            types.InlineKeyboardButton(text='📊 Синхронизировать счетчики', callback_data='admin_servers_sync_counts'),
            types.InlineKeyboardButton(text='📈 Подробная статистика', callback_data='admin_servers_stats'),
        ],
        [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_panel')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_servers_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    servers, total_count = await get_all_server_squads(db, page=page, limit=10)
    total_pages = (total_count + 9) // 10

    if not servers:
        text = '🌐 <b>Список серверов</b>\n\n❌ Серверы не найдены.'
    else:
        text = '🌐 <b>Список серверов</b>\n\n'
        text += f'📊 Всего: {total_count} | Страница: {page}/{total_pages}\n\n'

        for i, server in enumerate(servers, 1 + (page - 1) * 10):
            status_emoji = '✅' if server.is_available else '❌'
            price_text = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else 'Бесплатно'

            text += f'{i}. {status_emoji} {html.escape(server.display_name)}\n'
            text += f'   💰 Цена: {price_text}'

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

    keyboard.extend([[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_servers')]])

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def sync_servers_with_remnawave(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        '🔄 Синхронизация с Remnawave...\n\nПодождите, это может занять время.', reply_markup=None
    )

    try:
        remnawave_service = RemnaWaveService()
        squads = await remnawave_service.get_all_squads()

        if not squads:
            await callback.message.edit_text(
                '❌ Не удалось получить данные о сквадах из Remnawave.\n\nПроверьте настройки API.',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_servers')]]
                ),
            )
            return

        created, updated, removed = await sync_with_remnawave(db, squads)

        await cache.delete_pattern('available_countries*')

        text = f"""
✅ <b>Синхронизация завершена</b>

📊 <b>Результаты:</b>
• Создано новых серверов: {created}
• Обновлено существующих: {updated}
• Удалено отсутствующих: {removed}
• Всего обработано: {len(squads)}

ℹ️ Новые серверы созданы как недоступные.
Настройте их в списке серверов.
"""

        keyboard = [
            [
                types.InlineKeyboardButton(text='📋 Список серверов', callback_data='admin_servers_list'),
                types.InlineKeyboardButton(text='🔄 Повторить', callback_data='admin_servers_sync'),
            ],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_servers')],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Ошибка синхронизации серверов', error=e)
        await callback.message.edit_text(
            f'❌ Ошибка синхронизации: {e!s}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_servers')]]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def show_server_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    text, keyboard = _build_server_edit_view(server)

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
        await callback.answer('❌ Сервер не найден!', show_alert=True)
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
        '🌐 <b>Пользователи сервера</b>',
        '',
        f'• Сервер: {safe_name}',
        f'• UUID: <code>{safe_uuid}</code>',
        f'• Подключений: {total_users}',
    ]

    if total_pages > 1:
        header.append(f'• Страница: {page}/{total_pages}')

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
        text += 'Пользователи не найдены.'

    keyboard: list[list[types.InlineKeyboardButton]] = []

    for user in page_users:
        display_name = user.full_name
        if len(display_name) > 30:
            display_name = display_name[:27] + '...'

        if settings.is_multi_tariff_enabled() and hasattr(user, 'subscriptions') and user.subscriptions:
            status_parts = []
            for sub in user.subscriptions:
                emoji = '🟢' if sub.is_active else '🔴'
                name = sub.tariff.name if sub.tariff else f'#{sub.id}'
                status_parts.append(f'{emoji}{name}')
            subscription_status = ', '.join(status_parts)
        elif user.subscription:
            subscription_status = user.subscription.status_display
        else:
            subscription_status = '❌ Нет подписки'
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
                    text='⬅️ Предыдущая',
                    callback_data=f'admin_server_users_{server_id}_{page - 1}',
                )
            )

        navigation_buttons.append(
            types.InlineKeyboardButton(
                text=f'Стр. {page}/{total_pages}',
                callback_data=f'admin_server_users_{server_id}_{page}',
            )
        )

        if page < total_pages:
            navigation_buttons.append(
                types.InlineKeyboardButton(
                    text='Следующая ➡️',
                    callback_data=f'admin_server_users_{server_id}_{page + 1}',
                )
            )

        keyboard.append(navigation_buttons)

    keyboard.append([types.InlineKeyboardButton(text='⬅️ К серверу', callback_data=f'admin_server_edit_{server_id}')])

    keyboard.append([types.InlineKeyboardButton(text='⬅️ К списку', callback_data='admin_servers_list')])

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
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    new_status = not server.is_available
    await update_server_squad(db, server_id, is_available=new_status)

    await cache.delete_pattern('available_countries*')

    status_text = 'включен' if new_status else 'отключен'
    await callback.answer(f'✅ Сервер {status_text}!')

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def toggle_server_trial_assignment(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    new_status = not server.is_trial_eligible
    await update_server_squad(db, server_id, is_trial_eligible=new_status)

    status_text = 'будет выдаваться' if new_status else 'перестанет выдаваться'
    await callback.answer(f'✅ Сквад {status_text} в триал')

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def start_server_edit_price(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_price)

    current_price = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else 'Бесплатно'

    await callback.message.edit_text(
        f'💰 <b>Редактирование цены</b>\n\n'
        f'Текущая цена: <b>{current_price}</b>\n\n'
        f'Отправьте новую цену в рублях (например: 15.50) или 0 для бесплатного доступа:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_server_edit_{server_id}')]
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
            await message.answer('❌ Цена не может быть отрицательной')
            return

        if price_rubles > 10000:
            await message.answer('❌ Слишком высокая цена (максимум 10,000 ₽)')
            return

        price_kopeks = int(price_rubles * 100)

        server = await update_server_squad(db, server_id, price_kopeks=price_kopeks)

        if server:
            await state.clear()

            await cache.delete_pattern('available_countries*')

            price_text = f'{int(price_rubles)} ₽' if price_kopeks > 0 else 'Бесплатно'
            await message.answer(
                f'✅ Цена сервера изменена на: <b>{price_text}</b>',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text='🔙 К серверу', callback_data=f'admin_server_edit_{server_id}'
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer('❌ Ошибка при обновлении сервера')

    except ValueError:
        await message.answer('❌ Неверный формат цены. Используйте числа (например: 15.50)')


@admin_required
@error_handler
async def start_server_edit_name(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_name)

    await callback.message.edit_text(
        f'✏️ <b>Редактирование названия</b>\n\n'
        f'Текущее название: <b>{html.escape(server.display_name)}</b>\n\n'
        f'Отправьте новое название для сервера:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_server_edit_{server_id}')]
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
        await message.answer('❌ Название слишком длинное (максимум 255 символов)')
        return

    if len(new_name) < 3:
        await message.answer('❌ Название слишком короткое (минимум 3 символа)')
        return

    server = await update_server_squad(db, server_id, display_name=new_name)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        await message.answer(
            f'✅ Название сервера изменено на: <b>{new_name}</b>',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔙 К серверу', callback_data=f'admin_server_edit_{server_id}')]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer('❌ Ошибка при обновлении сервера')


@admin_required
@error_handler
async def delete_server_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    text = f"""
🗑️ <b>Удаление сервера</b>

Вы действительно хотите удалить сервер:
<b>{html.escape(server.display_name)}</b>

⚠️ <b>Внимание!</b>
Сервер можно удалить только если к нему нет активных подключений.

Это действие нельзя отменить!
"""

    keyboard = [
        [
            types.InlineKeyboardButton(text='🗑️ Да, удалить', callback_data=f'admin_server_delete_confirm_{server_id}'),
            types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_server_edit_{server_id}'),
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
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    success = await delete_server_squad(db, server_id)

    if success:
        await cache.delete_pattern('available_countries*')

        await callback.message.edit_text(
            f'✅ Сервер <b>{html.escape(server.display_name)}</b> успешно удален!',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='📋 К списку серверов', callback_data='admin_servers_list')]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await callback.message.edit_text(
            f'❌ Не удалось удалить сервер <b>{html.escape(server.display_name)}</b>\n\nВозможно, к нему есть активные подключения.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔙 К серверу', callback_data=f'admin_server_edit_{server_id}')]
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

    text = f"""
📊 <b>Подробная статистика серверов</b>

<b>🌐 Общая информация:</b>
• Всего серверов: {stats['total_servers']}
• Доступные: {stats['available_servers']}
• Недоступные: {stats['unavailable_servers']}
• С активными подключениями: {stats['servers_with_connections']}

<b>💰 Финансовая статистика:</b>
• Общая выручка: {int(stats['total_revenue_rubles'])} ₽
• Средняя цена за сервер: {int(stats['total_revenue_rubles'] / max(stats['servers_with_connections'], 1))} ₽

<b>🔥 Топ серверов по цене:</b>
"""

    sorted_servers = sorted(available_servers, key=lambda x: x.price_kopeks, reverse=True)

    for i, server in enumerate(sorted_servers[:5], 1):
        price_text = f'{int(server.price_rubles)} ₽' if server.price_kopeks > 0 else 'Бесплатно'
        text += f'{i}. {html.escape(server.display_name)} - {price_text}\n'

    if not sorted_servers:
        text += 'Нет доступных серверов\n'

    keyboard = [
        [
            types.InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_servers_stats'),
            types.InlineKeyboardButton(text='📋 Список', callback_data='admin_servers_list'),
        ],
        [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_servers')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_server_edit_country(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_country)

    current_country = server.country_code or 'Не указан'

    await callback.message.edit_text(
        f'🌍 <b>Редактирование кода страны</b>\n\n'
        f'Текущий код страны: <b>{current_country}</b>\n\n'
        f"Отправьте новый код страны (например: RU, US, DE) или '-' для удаления:",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_server_edit_{server_id}')]
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
        await message.answer('❌ Код страны слишком длинный (максимум 5 символов)')
        return

    server = await update_server_squad(db, server_id, country_code=new_country)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        country_text = new_country or 'Удален'
        await message.answer(
            f'✅ Код страны изменен на: <b>{country_text}</b>',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔙 К серверу', callback_data=f'admin_server_edit_{server_id}')]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer('❌ Ошибка при обновлении сервера')


@admin_required
@error_handler
async def start_server_edit_limit(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_limit)

    current_limit = server.max_users or 'Без лимита'

    await callback.message.edit_text(
        f'👥 <b>Редактирование лимита пользователей</b>\n\n'
        f'Текущий лимит: <b>{current_limit}</b>\n\n'
        f'Отправьте новый лимит пользователей (число) или 0 для безлимитного доступа:',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_server_edit_{server_id}')]
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
            await message.answer('❌ Лимит не может быть отрицательным')
            return

        if limit > 10000:
            await message.answer('❌ Слишком большой лимит (максимум 10,000)')
            return

        max_users = limit if limit > 0 else None

        server = await update_server_squad(db, server_id, max_users=max_users)

        if server:
            await state.clear()

            limit_text = f'{limit} пользователей' if limit > 0 else 'Без лимита'
            await message.answer(
                f'✅ Лимит пользователей изменен на: <b>{limit_text}</b>',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text='🔙 К серверу', callback_data=f'admin_server_edit_{server_id}'
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer('❌ Ошибка при обновлении сервера')

    except ValueError:
        await message.answer('❌ Неверный формат числа. Введите целое число.')


@admin_required
@error_handler
async def start_server_edit_description(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession
):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_description)

    current_desc = server.description or 'Не указано'

    await callback.message.edit_text(
        f'📝 <b>Редактирование описания</b>\n\n'
        f'Текущее описание:\n<i>{current_desc}</i>\n\n'
        f"Отправьте новое описание сервера или '-' для удаления:",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_server_edit_{server_id}')]
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
        await message.answer('❌ Описание слишком длинное (максимум 1000 символов)')
        return

    server = await update_server_squad(db, server_id, description=new_description)

    if server:
        await state.clear()

        desc_text = new_description or 'Удалено'
        await cache.delete_pattern('available_countries*')
        await message.answer(
            f'✅ Описание сервера изменено:\n\n<i>{desc_text}</i>',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔙 К серверу', callback_data=f'admin_server_edit_{server_id}')]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer('❌ Ошибка при обновлении сервера')


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
        await callback.answer('❌ Сервер не найден!', show_alert=True)
        return

    promo_groups_data = await get_promo_groups_with_counts(db)
    promo_groups = [
        {'id': group.id, 'name': group.name, 'is_default': group.is_default} for group, _ in promo_groups_data
    ]

    if not promo_groups:
        await callback.answer('❌ Не найдены промогруппы', show_alert=True)
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
        '🎯 <b>Настройка промогрупп</b>\n\n'
        f'Сервер: <b>{html.escape(server.display_name)}</b>\n\n'
        'Выберите промогруппы, которым будет доступен этот сервер.\n'
        'Должна быть выбрана минимум одна промогруппа.'
    )

    await callback.message.edit_text(
        text,
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected_ids),
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
        await callback.answer('⚠️ Сессия редактирования устарела', show_alert=True)
        return

    selected = {int(pg_id) for pg_id in data.get('selected_promo_groups', [])}
    promo_groups = data.get('promo_groups', [])

    if group_id in selected:
        if len(selected) == 1:
            await callback.answer('⚠️ Нельзя отключить последнюю промогруппу', show_alert=True)
            return
        selected.remove(group_id)
        message = 'Промогруппа отключена'
    else:
        selected.add(group_id)
        message = 'Промогруппа добавлена'

    await state.update_data(selected_promo_groups=list(selected))

    await callback.message.edit_reply_markup(
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected)
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
        await callback.answer('⚠️ Нет данных для сохранения', show_alert=True)
        return

    server_id = data.get('server_id')
    selected = data.get('selected_promo_groups', [])

    if not selected:
        await callback.answer('❌ Выберите хотя бы одну промогруппу', show_alert=True)
        return

    try:
        server = await update_server_squad_promo_groups(db, server_id, selected)
    except ValueError as exc:
        await callback.answer(f'❌ {exc}', show_alert=True)
        return

    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return

    await cache.delete_pattern('available_countries*')
    await state.clear()

    text, keyboard = _build_server_edit_view(server)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer('✅ Промогруппы обновлены!')


@admin_required
@error_handler
async def sync_server_user_counts_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text('🔄 Синхронизация счетчиков пользователей...', reply_markup=None)

    try:
        from app.database.crud.server_squad import sync_server_user_counts

        updated_count = await sync_server_user_counts(db)

        text = f"""
✅ <b>Синхронизация завершена</b>

📊 <b>Результат:</b>
• Обновлено серверов: {updated_count}

Счетчики пользователей синхронизированы с реальными данными.
"""

        keyboard = [
            [
                types.InlineKeyboardButton(text='📋 Список серверов', callback_data='admin_servers_list'),
                types.InlineKeyboardButton(text='🔄 Повторить', callback_data='admin_servers_sync_counts'),
            ],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_servers')],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Ошибка синхронизации счетчиков', error=e)
        await callback.message.edit_text(
            f'❌ Ошибка синхронизации: {e!s}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_servers')]]
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
