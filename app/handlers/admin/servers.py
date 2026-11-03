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
from app.localization.texts import get_texts
from app.services.remnawave_service import RemnaWaveService
from app.states import AdminStates
from app.utils.cache import cache
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _build_server_edit_view(server, language: str = 'ru'):
    status_emoji = (
        get_texts(language).t('ADMIN_SERVER_UI_005', '✅ Доступен')
        if server.is_available
        else get_texts(language).t('ADMIN_SERVER_UI_006', '❌ Недоступен')
    )
    price_text = (
        f'{int(server.price_rubles)} ₽'
        if server.price_kopeks > 0
        else get_texts(language).t('ADMIN_SERVER_UI_007', 'Бесплатно')
    )
    promo_groups_text = (
        ', '.join(sorted(pg.name for pg in server.allowed_promo_groups))
        if server.allowed_promo_groups
        else get_texts(language).t('ADMIN_SERVER_UI_008', 'Не выбраны')
    )

    trial_status = (
        get_texts(language).t('ADMIN_SERVER_UI_009', '✅ Да')
        if server.is_trial_eligible
        else get_texts(language).t('ADMIN_SERVER_UI_010', '⚪️ Нет')
    )

    text = (
        get_texts(language)
        .t(
            'ADMIN_SERVER_UI_000',
            '\n🌐 <b>Редактирование сервера</b>\n\n<b>Информация:</b>\n• ID: {v0}\n• UUID: <code>{v1}</code>\n• Название: {v2}\n• Оригинальное: {v3}\n• Статус: {v4}\n\n<b>Настройки:</b>\n• Цена: {v5}\n• Код страны: {v6}\n• Лимит пользователей: {v7}\n• Текущих пользователей: {v8}\n• Промогруппы: {v9}\n• Выдача триала: {v10}\n\n<b>Описание:</b>\n{v11}\n\nВыберите что изменить:\n',
        )
        .format(
            v0=server.id,
            v1=server.squad_uuid,
            v2=html.escape(server.display_name),
            v3=html.escape(server.original_name)
            if server.original_name
            else get_texts(language).t('ADMIN_SERVER_UI_026', 'Не указано'),
            v4=status_emoji,
            v5=price_text,
            v6=server.country_code or get_texts(language).t('ADMIN_SERVER_UI_024', 'Не указан'),
            v7=server.max_users or get_texts(language).t('ADMIN_SERVER_UI_025', 'Без лимита'),
            v8=server.current_users,
            v9=promo_groups_text,
            v10=trial_status,
            v11=server.description or get_texts(language).t('ADMIN_SERVER_UI_026', 'Не указано'),
        )
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_060', '✏️ Название'),
                callback_data=f'admin_server_edit_name_{server.id}',
            ),
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_061', '💰 Цена'),
                callback_data=f'admin_server_edit_price_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_062', '🌍 Страна'),
                callback_data=f'admin_server_edit_country_{server.id}',
            ),
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_063', '👥 Лимит'),
                callback_data=f'admin_server_edit_limit_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_064', '👥 Юзеры'),
                callback_data=f'admin_server_users_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_094', '🎁 Выдавать в триал')
                if not server.is_trial_eligible
                else get_texts(language).t('ADMIN_SERVER_UI_095', '🚫 Не выдавать в триал'),
                callback_data=f'admin_server_trial_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_065', '🎯 Промогруппы'),
                callback_data=f'admin_server_edit_promo_{server.id}',
            ),
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_066', '📝 Описание'),
                callback_data=f'admin_server_edit_desc_{server.id}',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_096', '❌ Отключить')
                if server.is_available
                else get_texts(language).t('ADMIN_SERVER_UI_097', '✅ Включить'),
                callback_data=f'admin_server_toggle_{server.id}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_067', '🗑️ Удалить'),
                callback_data=f'admin_server_delete_{server.id}',
            ),
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_068', '⬅️ Назад'), callback_data='admin_servers_list'
            ),
        ],
    ]

    return text, types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _build_server_promo_groups_keyboard(server_id: int, promo_groups, selected_ids, language: str = 'ru'):
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
                text=get_texts(language).t('ADMIN_SERVER_UI_069', '💾 Сохранить'),
                callback_data=f'admin_server_promo_save_{server_id}',
            )
        ]
    )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=get_texts(language).t('ADMIN_SERVER_UI_068', '⬅️ Назад'),
                callback_data=f'admin_server_edit_{server_id}',
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


@admin_required
@error_handler
async def show_servers_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_server_statistics(db)

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_SERVER_UI_001',
            '\n🌐 <b>Управление серверами</b>\n\n📊 <b>Статистика:</b>\n• Всего серверов: {v0}\n• Доступные: {v1}\n• Недоступные: {v2}\n• С подключениями: {v3}\n\n💰 <b>Выручка от серверов:</b>\n• Общая: {v4} ₽\n\nВыберите действие:\n',
        )
        .format(
            v0=stats['total_servers'],
            v1=stats['available_servers'],
            v2=stats['unavailable_servers'],
            v3=stats['servers_with_connections'],
            v4=int(stats['total_revenue_rubles']),
        )
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_070', '📋 Список серверов'),
                callback_data='admin_servers_list',
            ),
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_071', '🔄 Синхронизация'),
                callback_data='admin_servers_sync',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_072', '📊 Синхронизировать счетчики'),
                callback_data='admin_servers_sync_counts',
            ),
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_073', '📈 Подробная статистика'),
                callback_data='admin_servers_stats',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_068', '⬅️ Назад'), callback_data='admin_panel'
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_servers_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    servers, total_count = await get_all_server_squads(db, page=page, limit=10)
    total_pages = (total_count + 9) // 10

    if not servers:
        text = get_texts(db_user.language).t(
            'ADMIN_SERVER_UI_011', '🌐 <b>Список серверов</b>\n\n❌ Серверы не найдены.'
        )
    else:
        text = get_texts(db_user.language).t('ADMIN_SERVER_UI_012', '🌐 <b>Список серверов</b>\n\n')
        text += (
            get_texts(db_user.language)
            .t('ADMIN_SERVER_UI_013', '📊 Всего: {v0} | Страница: {v1}/{v2}\n\n')
            .format(v0=total_count, v1=page, v2=total_pages)
        )

        for i, server in enumerate(servers, 1 + (page - 1) * 10):
            status_emoji = '✅' if server.is_available else '❌'
            price_text = (
                f'{int(server.price_rubles)} ₽'
                if server.price_kopeks > 0
                else get_texts(db_user.language).t('ADMIN_SERVER_UI_007', 'Бесплатно')
            )

            text += f'{i}. {status_emoji} {html.escape(server.display_name)}\n'
            text += get_texts(db_user.language).t('ADMIN_SERVER_UI_030', '   💰 Цена: {v0}').format(v0=price_text)

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
        [
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_SERVER_UI_068', '⬅️ Назад'), callback_data='admin_servers'
                )
            ]
        ]
    )

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def sync_servers_with_remnawave(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        get_texts(db_user.language).t(
            'ADMIN_SERVER_UI_031', '🔄 Синхронизация с Remnawave...\n\nПодождите, это может занять время.'
        ),
        reply_markup=None,
    )

    try:
        remnawave_service = RemnaWaveService()
        squads = await remnawave_service.get_all_squads()

        if not squads:
            await callback.message.edit_text(
                get_texts(db_user.language).t(
                    'ADMIN_SERVER_UI_074',
                    '❌ Не удалось получить данные о сквадах из Remnawave.\n\nПроверьте настройки API.',
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_068', '⬅️ Назад'),
                                callback_data='admin_servers',
                            )
                        ]
                    ]
                ),
            )
            return

        created, updated, removed = await sync_with_remnawave(db, squads)

        await cache.delete_pattern('available_countries*')

        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_SERVER_UI_014',
                '\n✅ <b>Синхронизация завершена</b>\n\n📊 <b>Результаты:</b>\n• Создано новых серверов: {v0}\n• Обновлено существующих: {v1}\n• Удалено отсутствующих: {v2}\n• Всего обработано: {v3}\n\nℹ️ Новые серверы созданы как недоступные.\nНастройте их в списке серверов.\n',
            )
            .format(v0=created, v1=updated, v2=removed, v3=len(squads))
        )

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_SERVER_UI_070', '📋 Список серверов'),
                    callback_data='admin_servers_list',
                ),
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_SERVER_UI_098', '🔄 Повторить'),
                    callback_data='admin_servers_sync',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_SERVER_UI_068', '⬅️ Назад'), callback_data='admin_servers'
                )
            ],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Ошибка синхронизации серверов', error=e)
        await callback.message.edit_text(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_075', '❌ Ошибка синхронизации: {v0!s}').format(v0=e),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_SERVER_UI_068', '⬅️ Назад'),
                            callback_data='admin_servers',
                        )
                    ]
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    text, keyboard = _build_server_edit_view(server, language=db_user.language)

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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
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
        get_texts(db_user.language).t('ADMIN_SERVER_UI_015', '🌐 <b>Пользователи сервера</b>'),
        '',
        get_texts(db_user.language).t('ADMIN_SERVER_UI_016', '• Сервер: {v0}').format(v0=safe_name),
        f'• UUID: <code>{safe_uuid}</code>',
        get_texts(db_user.language).t('ADMIN_SERVER_UI_017', '• Подключений: {v0}').format(v0=total_users),
    ]

    if total_pages > 1:
        header.append(
            get_texts(db_user.language)
            .t('ADMIN_SERVER_UI_032', '• Страница: {v0}/{v1}')
            .format(v0=page, v1=total_pages)
        )

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
        text += get_texts(db_user.language).t('ADMIN_SERVER_UI_018', 'Пользователи не найдены.')

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
            subscription_status = get_texts(db_user.language).t('ADMIN_SERVER_UI_045', '❌ Нет подписки')
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
                    text=get_texts(db_user.language).t('ADMIN_SERVER_UI_099', '⬅️ Предыдущая'),
                    callback_data=f'admin_server_users_{server_id}_{page - 1}',
                )
            )

        navigation_buttons.append(
            types.InlineKeyboardButton(
                text=get_texts(db_user.language)
                .t('ADMIN_SERVER_UI_076', 'Стр. {v0}/{v1}')
                .format(v0=page, v1=total_pages),
                callback_data=f'admin_server_users_{server_id}_{page}',
            )
        )

        if page < total_pages:
            navigation_buttons.append(
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_SERVER_UI_100', 'Следующая ➡️'),
                    callback_data=f'admin_server_users_{server_id}_{page + 1}',
                )
            )

        keyboard.append(navigation_buttons)

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_077', '⬅️ К серверу'),
                callback_data=f'admin_server_edit_{server_id}',
            )
        ]
    )

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_078', '⬅️ К списку'),
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
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    new_status = not server.is_available
    await update_server_squad(db, server_id, is_available=new_status)

    await cache.delete_pattern('available_countries*')

    status_text = (
        get_texts(db_user.language).t('ADMIN_SERVER_UI_019', 'включен')
        if new_status
        else get_texts(db_user.language).t('ADMIN_SERVER_UI_020', 'отключен')
    )
    await callback.answer(
        get_texts(db_user.language).t('ADMIN_SERVER_UI_033', '✅ Сервер {v0}!').format(v0=status_text)
    )

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server, language=db_user.language)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def toggle_server_trial_assignment(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    new_status = not server.is_trial_eligible
    await update_server_squad(db, server_id, is_trial_eligible=new_status)

    status_text = (
        get_texts(db_user.language).t('ADMIN_SERVER_UI_021', 'будет выдаваться')
        if new_status
        else get_texts(db_user.language).t('ADMIN_SERVER_UI_022', 'перестанет выдаваться')
    )
    await callback.answer(
        get_texts(db_user.language).t('ADMIN_SERVER_UI_034', '✅ Сквад {v0} в триал').format(v0=status_text)
    )

    server = await get_server_squad_by_id(db, server_id)

    text, keyboard = _build_server_edit_view(server, language=db_user.language)

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def start_server_edit_price(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_price)

    current_price = (
        f'{int(server.price_rubles)} ₽'
        if server.price_kopeks > 0
        else get_texts(db_user.language).t('ADMIN_SERVER_UI_007', 'Бесплатно')
    )

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_SERVER_UI_035',
            '💰 <b>Редактирование цены</b>\n\nТекущая цена: <b>{v0}</b>\n\nОтправьте новую цену в рублях (например: 15.50) или 0 для бесплатного доступа:',
        )
        .format(v0=current_price),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_SERVER_UI_084', '❌ Отмена'),
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
async def process_server_price_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    try:
        price_rubles = float(message.text.replace(',', '.'))

        if price_rubles < 0:
            await message.answer(
                get_texts(db_user.language).t('ADMIN_SERVER_UI_079', '❌ Цена не может быть отрицательной')
            )
            return

        if price_rubles > 10000:
            await message.answer(
                get_texts(db_user.language).t('ADMIN_SERVER_UI_080', '❌ Слишком высокая цена (максимум 10,000 ₽)')
            )
            return

        price_kopeks = int(price_rubles * 100)

        server = await update_server_squad(db, server_id, price_kopeks=price_kopeks)

        if server:
            await state.clear()

            await cache.delete_pattern('available_countries*')

            price_text = (
                f'{int(price_rubles)} ₽'
                if price_kopeks > 0
                else get_texts(db_user.language).t('ADMIN_SERVER_UI_007', 'Бесплатно')
            )
            await message.answer(
                get_texts(db_user.language)
                .t('ADMIN_SERVER_UI_081', '✅ Цена сервера изменена на: <b>{v0}</b>')
                .format(v0=price_text),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_101', '🔙 К серверу'),
                                callback_data=f'admin_server_edit_{server_id}',
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer(
                get_texts(db_user.language).t('ADMIN_SERVER_UI_049', '❌ Ошибка при обновлении сервера')
            )

    except ValueError:
        await message.answer(
            get_texts(db_user.language).t(
                'ADMIN_SERVER_UI_082', '❌ Неверный формат цены. Используйте числа (например: 15.50)'
            )
        )


@admin_required
@error_handler
async def start_server_edit_name(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_name)

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_SERVER_UI_036',
            '✏️ <b>Редактирование названия</b>\n\nТекущее название: <b>{v0}</b>\n\nОтправьте новое название для сервера:',
        )
        .format(v0=html.escape(server.display_name)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_SERVER_UI_084', '❌ Отмена'),
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
async def process_server_name_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    new_name = message.text.strip()

    if len(new_name) > 255:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_046', '❌ Название слишком длинное (максимум 255 символов)')
        )
        return

    if len(new_name) < 3:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_047', '❌ Название слишком короткое (минимум 3 символа)')
        )
        return

    server = await update_server_squad(db, server_id, display_name=new_name)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_SERVER_UI_048', '✅ Название сервера изменено на: <b>{v0}</b>')
            .format(v0=new_name),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_SERVER_UI_101', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(get_texts(db_user.language).t('ADMIN_SERVER_UI_049', '❌ Ошибка при обновлении сервера'))


@admin_required
@error_handler
async def delete_server_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_SERVER_UI_002',
            '\n🗑️ <b>Удаление сервера</b>\n\nВы действительно хотите удалить сервер:\n<b>{v0}</b>\n\n⚠️ <b>Внимание!</b>\nСервер можно удалить только если к нему нет активных подключений.\n\nЭто действие нельзя отменить!\n',
        )
        .format(v0=html.escape(server.display_name))
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_083', '🗑️ Да, удалить'),
                callback_data=f'admin_server_delete_confirm_{server_id}',
            ),
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_084', '❌ Отмена'),
                callback_data=f'admin_server_edit_{server_id}',
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    success = await delete_server_squad(db, server_id)

    if success:
        await cache.delete_pattern('available_countries*')

        await callback.message.edit_text(
            get_texts(db_user.language)
            .t('ADMIN_SERVER_UI_050', '✅ Сервер <b>{v0}</b> успешно удален!')
            .format(v0=html.escape(server.display_name)),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_SERVER_UI_102', '📋 К списку серверов'),
                            callback_data='admin_servers_list',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await callback.message.edit_text(
            get_texts(db_user.language)
            .t(
                'ADMIN_SERVER_UI_051',
                '❌ Не удалось удалить сервер <b>{v0}</b>\n\nВозможно, к нему есть активные подключения.',
            )
            .format(v0=html.escape(server.display_name)),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_SERVER_UI_101', '🔙 К серверу'),
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
    stats = await get_server_statistics(db)
    available_servers = await get_available_server_squads(db)

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_SERVER_UI_003',
            '\n📊 <b>Подробная статистика серверов</b>\n\n<b>🌐 Общая информация:</b>\n• Всего серверов: {v0}\n• Доступные: {v1}\n• Недоступные: {v2}\n• С активными подключениями: {v3}\n\n<b>💰 Финансовая статистика:</b>\n• Общая выручка: {v4} ₽\n• Средняя цена за сервер: {v5} ₽\n\n<b>🔥 Топ серверов по цене:</b>\n',
        )
        .format(
            v0=stats['total_servers'],
            v1=stats['available_servers'],
            v2=stats['unavailable_servers'],
            v3=stats['servers_with_connections'],
            v4=int(stats['total_revenue_rubles']),
            v5=int(stats['total_revenue_rubles'] / max(stats['servers_with_connections'], 1)),
        )
    )

    sorted_servers = sorted(available_servers, key=lambda x: x.price_kopeks, reverse=True)

    for i, server in enumerate(sorted_servers[:5], 1):
        price_text = (
            f'{int(server.price_rubles)} ₽'
            if server.price_kopeks > 0
            else get_texts(db_user.language).t('ADMIN_SERVER_UI_007', 'Бесплатно')
        )
        text += f'{i}. {html.escape(server.display_name)} - {price_text}\n'

    if not sorted_servers:
        text += get_texts(db_user.language).t('ADMIN_SERVER_UI_023', 'Нет доступных серверов\n')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_085', '🔄 Обновить'),
                callback_data='admin_servers_stats',
            ),
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_086', '📋 Список'),
                callback_data='admin_servers_list',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_068', '⬅️ Назад'), callback_data='admin_servers'
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def start_server_edit_country(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_country)

    current_country = server.country_code or get_texts(db_user.language).t('ADMIN_SERVER_UI_024', 'Не указан')

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_SERVER_UI_037',
            "🌍 <b>Редактирование кода страны</b>\n\nТекущий код страны: <b>{v0}</b>\n\nОтправьте новый код страны (например: RU, US, DE) или '-' для удаления:",
        )
        .format(v0=current_country),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_SERVER_UI_084', '❌ Отмена'),
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
async def process_server_country_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    new_country = message.text.strip().upper()

    if new_country == '-':
        new_country = None
    elif len(new_country) > 5:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_087', '❌ Код страны слишком длинный (максимум 5 символов)')
        )
        return

    server = await update_server_squad(db, server_id, country_code=new_country)

    if server:
        await state.clear()

        await cache.delete_pattern('available_countries*')

        country_text = new_country or get_texts(db_user.language).t('ADMIN_SERVER_UI_038', 'Удален')
        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_SERVER_UI_052', '✅ Код страны изменен на: <b>{v0}</b>')
            .format(v0=country_text),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_SERVER_UI_101', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(get_texts(db_user.language).t('ADMIN_SERVER_UI_049', '❌ Ошибка при обновлении сервера'))


@admin_required
@error_handler
async def start_server_edit_limit(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_limit)

    current_limit = server.max_users or get_texts(db_user.language).t('ADMIN_SERVER_UI_025', 'Без лимита')

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_SERVER_UI_039',
            '👥 <b>Редактирование лимита пользователей</b>\n\nТекущий лимит: <b>{v0}</b>\n\nОтправьте новый лимит пользователей (число) или 0 для безлимитного доступа:',
        )
        .format(v0=current_limit),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_SERVER_UI_084', '❌ Отмена'),
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
async def process_server_limit_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    try:
        limit = int(message.text.strip())

        if limit < 0:
            await message.answer(
                get_texts(db_user.language).t('ADMIN_SERVER_UI_088', '❌ Лимит не может быть отрицательным')
            )
            return

        if limit > 10000:
            await message.answer(
                get_texts(db_user.language).t('ADMIN_SERVER_UI_089', '❌ Слишком большой лимит (максимум 10,000)')
            )
            return

        max_users = limit if limit > 0 else None

        server = await update_server_squad(db, server_id, max_users=max_users)

        if server:
            await state.clear()

            limit_text = (
                get_texts(db_user.language).t('ADMIN_SERVER_UI_053', '{v0} пользователей').format(v0=limit)
                if limit > 0
                else get_texts(db_user.language).t('ADMIN_SERVER_UI_025', 'Без лимита')
            )
            await message.answer(
                get_texts(db_user.language)
                .t('ADMIN_SERVER_UI_090', '✅ Лимит пользователей изменен на: <b>{v0}</b>')
                .format(v0=limit_text),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=get_texts(db_user.language).t('ADMIN_SERVER_UI_101', '🔙 К серверу'),
                                callback_data=f'admin_server_edit_{server_id}',
                            )
                        ]
                    ]
                ),
                parse_mode='HTML',
            )
        else:
            await message.answer(
                get_texts(db_user.language).t('ADMIN_SERVER_UI_049', '❌ Ошибка при обновлении сервера')
            )

    except ValueError:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_091', '❌ Неверный формат числа. Введите целое число.')
        )


@admin_required
@error_handler
async def start_server_edit_description(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession
):
    server_id = int(callback.data.split('_')[-1])
    server = await get_server_squad_by_id(db, server_id)

    if not server:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    await state.set_data({'server_id': server_id})
    await state.set_state(AdminStates.editing_server_description)

    current_desc = server.description or get_texts(db_user.language).t('ADMIN_SERVER_UI_026', 'Не указано')

    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_SERVER_UI_040',
            "📝 <b>Редактирование описания</b>\n\nТекущее описание:\n<i>{v0}</i>\n\nОтправьте новое описание сервера или '-' для удаления:",
        )
        .format(v0=current_desc),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=get_texts(db_user.language).t('ADMIN_SERVER_UI_084', '❌ Отмена'),
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
async def process_server_description_edit(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    data = await state.get_data()
    server_id = data.get('server_id')

    new_description = message.text.strip()

    if new_description == '-':
        new_description = None
    elif len(new_description) > 1000:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_092', '❌ Описание слишком длинное (максимум 1000 символов)')
        )
        return

    server = await update_server_squad(db, server_id, description=new_description)

    if server:
        await state.clear()

        desc_text = new_description or get_texts(db_user.language).t('ADMIN_SERVER_UI_041', 'Удалено')
        await cache.delete_pattern('available_countries*')
        await message.answer(
            get_texts(db_user.language)
            .t('ADMIN_SERVER_UI_054', '✅ Описание сервера изменено:\n\n<i>{v0}</i>')
            .format(v0=desc_text),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_SERVER_UI_101', '🔙 К серверу'),
                            callback_data=f'admin_server_edit_{server_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
    else:
        await message.answer(get_texts(db_user.language).t('ADMIN_SERVER_UI_049', '❌ Ошибка при обновлении сервера'))


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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_044', '❌ Сервер не найден!'), show_alert=True
        )
        return

    promo_groups_data = await get_promo_groups_with_counts(db)
    promo_groups = [
        {'id': group.id, 'name': group.name, 'is_default': group.is_default} for group, _ in promo_groups_data
    ]

    if not promo_groups:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_055', '❌ Не найдены промогруппы'), show_alert=True
        )
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
        get_texts(db_user.language)
        .t(
            'ADMIN_SERVER_UI_004',
            '🎯 <b>Настройка промогрупп</b>\n\nСервер: <b>{v0}</b>\n\nВыберите промогруппы, которым будет доступен этот сервер.\nДолжна быть выбрана минимум одна промогруппа.',
        )
        .format(v0=html.escape(server.display_name))
    )

    await callback.message.edit_text(
        text,
        reply_markup=_build_server_promo_groups_keyboard(
            server_id, promo_groups, selected_ids, language=db_user.language
        ),
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_056', '⚠️ Сессия редактирования устарела'), show_alert=True
        )
        return

    selected = {int(pg_id) for pg_id in data.get('selected_promo_groups', [])}
    promo_groups = data.get('promo_groups', [])

    if group_id in selected:
        if len(selected) == 1:
            await callback.answer(
                get_texts(db_user.language).t('ADMIN_SERVER_UI_093', '⚠️ Нельзя отключить последнюю промогруппу'),
                show_alert=True,
            )
            return
        selected.remove(group_id)
        message = get_texts(db_user.language).t('ADMIN_SERVER_UI_027', 'Промогруппа отключена')
    else:
        selected.add(group_id)
        message = get_texts(db_user.language).t('ADMIN_SERVER_UI_028', 'Промогруппа добавлена')

    await state.update_data(selected_promo_groups=list(selected))

    await callback.message.edit_reply_markup(
        reply_markup=_build_server_promo_groups_keyboard(server_id, promo_groups, selected, language=db_user.language)
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
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_057', '⚠️ Нет данных для сохранения'), show_alert=True
        )
        return

    server_id = data.get('server_id')
    selected = data.get('selected_promo_groups', [])

    if not selected:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_058', '❌ Выберите хотя бы одну промогруппу'),
            show_alert=True,
        )
        return

    try:
        server = await update_server_squad_promo_groups(db, server_id, selected)
    except ValueError as exc:
        await callback.answer(f'❌ {exc}', show_alert=True)
        return

    if not server:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_059', '❌ Сервер не найден'), show_alert=True
        )
        return

    await cache.delete_pattern('available_countries*')
    await state.clear()

    text, keyboard = _build_server_edit_view(server, language=db_user.language)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer(get_texts(db_user.language).t('ADMIN_SERVER_UI_042', '✅ Промогруппы обновлены!'))


@admin_required
@error_handler
async def sync_server_user_counts_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        get_texts(db_user.language).t('ADMIN_SERVER_UI_043', '🔄 Синхронизация счетчиков пользователей...'),
        reply_markup=None,
    )

    try:
        from app.database.crud.server_squad import sync_server_user_counts

        updated_count = await sync_server_user_counts(db)

        text = (
            get_texts(db_user.language)
            .t(
                'ADMIN_SERVER_UI_029',
                '\n✅ <b>Синхронизация завершена</b>\n\n📊 <b>Результат:</b>\n• Обновлено серверов: {v0}\n\nСчетчики пользователей синхронизированы с реальными данными.\n',
            )
            .format(v0=updated_count)
        )

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_SERVER_UI_070', '📋 Список серверов'),
                    callback_data='admin_servers_list',
                ),
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_SERVER_UI_098', '🔄 Повторить'),
                    callback_data='admin_servers_sync_counts',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=get_texts(db_user.language).t('ADMIN_SERVER_UI_068', '⬅️ Назад'), callback_data='admin_servers'
                )
            ],
        ]

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Ошибка синхронизации счетчиков', error=e)
        await callback.message.edit_text(
            get_texts(db_user.language).t('ADMIN_SERVER_UI_075', '❌ Ошибка синхронизации: {v0!s}').format(v0=e),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=get_texts(db_user.language).t('ADMIN_SERVER_UI_068', '⬅️ Назад'),
                            callback_data='admin_servers',
                        )
                    ]
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
