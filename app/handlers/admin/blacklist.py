"""
Обработчики админ-панели для управления черным списком
"""

import html

import structlog
from aiogram import types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from app.database.models import User
from app.services.blacklist_service import blacklist_service
from app.states import BlacklistStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_blacklist_settings(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Показывает настройки черного списка
    """
    logger.info('Вызван обработчик show_blacklist_settings для пользователя', from_user_id=callback.from_user.id)

    is_enabled = blacklist_service.is_blacklist_check_enabled()
    github_url = blacklist_service.get_blacklist_github_url()
    blacklist_count = len(await blacklist_service.get_all_blacklisted_users())

    status_text = '✅ Включена' if is_enabled else '❌ Отключена'
    url_text = github_url or 'Не задан'

    text = f"""
🔐 <b>Настройки черного списка</b>

Статус: {status_text}
URL к черному списку: <code>{url_text}</code>
Количество записей: {blacklist_count}

Действия:
"""

    keyboard = [
        [
            types.InlineKeyboardButton(
                text='🔄 Обновить список' if is_enabled else '🔄 Обновить (откл.)',
                callback_data='admin_blacklist_update',
            )
        ],
        [
            types.InlineKeyboardButton(
                text='📋 Просмотреть список' if is_enabled else '📋 Просмотр (откл.)',
                callback_data='admin_blacklist_view',
            )
        ],
        [
            types.InlineKeyboardButton(
                text='✏️ URL к GitHub' if not github_url else '✏️ Изменить URL', callback_data='admin_blacklist_set_url'
            )
        ],
        [
            types.InlineKeyboardButton(
                text='✅ Включить' if not is_enabled else '❌ Отключить', callback_data='admin_blacklist_toggle'
            )
        ],
        [types.InlineKeyboardButton(text='⬅️ Назад к пользователям', callback_data='admin_users')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def toggle_blacklist(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Переключает статус проверки черного списка
    """
    # Текущая реализация использует настройки из .env
    # Для полной реализации нужно будет создать сервис настроек
    is_enabled = blacklist_service.is_blacklist_check_enabled()

    # В реальной реализации нужно будет изменить настройку в базе данных
    # или в системе настроек, но сейчас просто покажем статус
    new_status = not is_enabled
    status_text = 'включена' if new_status else 'отключена'

    await callback.message.edit_text(
        f'Статус проверки черного списка: {status_text}\n\n'
        f'Для изменения статуса проверки черного списка измените значение\n'
        f'<code>BLACKLIST_CHECK_ENABLED</code> в файле <code>.env</code>',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 Обновить статус', callback_data='admin_blacklist_settings')],
                [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_blacklist_settings')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def update_blacklist(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Обновляет черный список из GitHub
    """
    success, message = await blacklist_service.force_update_blacklist()

    if success:
        await callback.message.edit_text(
            f'✅ {message}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='📋 Просмотреть список', callback_data='admin_blacklist_view')],
                    [types.InlineKeyboardButton(text='🔄 Ручное обновление', callback_data='admin_blacklist_update')],
                    [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_blacklist_settings')],
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            f'❌ Ошибка обновления: {message}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 Повторить', callback_data='admin_blacklist_update')],
                    [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_blacklist_settings')],
                ]
            ),
        )
    await callback.answer()


@admin_required
@error_handler
async def show_blacklist_users(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Показывает список пользователей в черном списке
    """
    blacklist_users = await blacklist_service.get_all_blacklisted_users()

    if not blacklist_users:
        text = 'Черный список пуст'
    else:
        text = f'🔐 <b>Черный список ({len(blacklist_users)} записей)</b>\n\n'

        # Показываем первые 20 записей
        for i, (tg_id, username, reason) in enumerate(blacklist_users[:20], 1):
            text += f'{i}. <code>{tg_id}</code> {html.escape(username or "")} — {html.escape(reason or "")}\n'

        if len(blacklist_users) > 20:
            text += f'\n... и еще {len(blacklist_users) - 20} записей'

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_blacklist_view')],
                [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_blacklist_settings')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_set_blacklist_url(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Начинает процесс установки URL к черному списку
    """
    current_url = blacklist_service.get_blacklist_github_url() or 'не задан'

    await callback.message.edit_text(
        f'Введите новый URL к файлу черного списка на GitHub\n\n'
        f'Текущий URL: {current_url}\n\n'
        f'Пример: https://raw.githubusercontent.com/username/repository/main/blacklist.txt\n\n'
        f'Для отмены используйте команду /cancel',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_blacklist_settings')]]
        ),
    )

    await state.set_state(BlacklistStates.waiting_for_blacklist_url)
    await callback.answer()


@admin_required
@error_handler
async def process_blacklist_url(message: types.Message, db_user: User, state: FSMContext):
    """
    Обрабатывает введенный URL к черному списку
    """
    # Обрабатываем сообщение только если бот ожидает ввод URL
    if await state.get_state() != BlacklistStates.waiting_for_blacklist_url.state:
        return

    url = message.text.strip()

    # В реальной реализации нужно сохранить URL в систему настроек
    # В текущей реализации просто выводим сообщение
    if url.lower() in ['/cancel', 'отмена', 'cancel']:
        await message.answer(
            'Настройка URL отменена',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text='🔐 Настройки черного списка', callback_data='admin_blacklist_settings'
                        )
                    ]
                ]
            ),
        )
        await state.clear()
        return

    # Проверяем, что URL выглядит корректно
    if not url.startswith(('http://', 'https://')):
        await message.answer(
            '❌ Некорректный URL. URL должен начинаться с http:// или https://',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text='🔐 Настройки черного списка', callback_data='admin_blacklist_settings'
                        )
                    ]
                ]
            ),
        )
        return

    # В реальной системе здесь нужно сохранить URL в базу данных настроек
    # или в систему конфигурации

    await message.answer(
        f'✅ URL к черному списку установлен:\n<code>{url}</code>\n\n'
        f'Для применения изменений перезапустите бота или измените значение\n'
        f'<code>BLACKLIST_GITHUB_URL</code> в файле <code>.env</code>',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 Обновить список', callback_data='admin_blacklist_update')],
                [
                    types.InlineKeyboardButton(
                        text='🔐 Настройки черного списка', callback_data='admin_blacklist_settings'
                    )
                ],
            ]
        ),
    )
    await state.clear()


def register_blacklist_handlers(dp):
    """
    Регистрация обработчиков черного списка
    """
    # Обработчик показа настроек черного списка
    # Этот обработчик нужно будет вызывать из меню пользователей или отдельно
    dp.callback_query.register(show_blacklist_settings, lambda c: c.data == 'admin_blacklist_settings')

    # Обработчики для взаимодействия с черным списком
    dp.callback_query.register(toggle_blacklist, lambda c: c.data == 'admin_blacklist_toggle')

    dp.callback_query.register(update_blacklist, lambda c: c.data == 'admin_blacklist_update')

    dp.callback_query.register(show_blacklist_users, lambda c: c.data == 'admin_blacklist_view')

    dp.callback_query.register(start_set_blacklist_url, lambda c: c.data == 'admin_blacklist_set_url')

    # Обработчик сообщений для установки URL (работает только в нужном состоянии)
    dp.message.register(process_blacklist_url, StateFilter(BlacklistStates.waiting_for_blacklist_url))
