import html
from datetime import datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.backup_service import backup_service
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


class BackupStates(StatesGroup):
    waiting_backup_file = State()
    waiting_settings_update = State()


def get_backup_main_keyboard(language: str = 'ru'):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='🚀 Создать бекап', callback_data='backup_create'),
                InlineKeyboardButton(text='📥 Восстановить', callback_data='backup_restore'),
            ],
            [
                InlineKeyboardButton(text='📋 Список бекапов', callback_data='backup_list'),
                InlineKeyboardButton(text='⚙️ Настройки', callback_data='backup_settings'),
            ],
            [InlineKeyboardButton(text='◀️ Назад', callback_data='admin_panel')],
        ]
    )


def get_backup_list_keyboard(backups: list, page: int = 1, per_page: int = 5):
    keyboard = []

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_backups = backups[start_idx:end_idx]

    for backup in page_backups:
        try:
            if backup.get('timestamp'):
                dt = datetime.fromisoformat(backup['timestamp'].replace('Z', '+00:00'))
                date_str = dt.strftime('%d.%m %H:%M')
            else:
                date_str = '?'
        except:
            date_str = '?'

        size_str = f'{backup.get("file_size_mb", 0):.1f}MB'
        records_str = backup.get('total_records', '?')

        button_text = f'📦 {date_str} • {size_str} • {records_str} записей'
        callback_data = f'backup_manage_{backup["filename"]}'

        keyboard.append([InlineKeyboardButton(text=button_text, callback_data=callback_data)])

    if len(backups) > per_page:
        total_pages = (len(backups) + per_page - 1) // per_page
        nav_row = []

        if page > 1:
            nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'backup_list_page_{page - 1}'))

        nav_row.append(InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='noop'))

        if page < total_pages:
            nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'backup_list_page_{page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend([[InlineKeyboardButton(text='◀️ Назад', callback_data='backup_panel')]])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_backup_manage_keyboard(backup_filename: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='📥 Восстановить', callback_data=f'backup_restore_file_{backup_filename}')],
            [InlineKeyboardButton(text='🗑️ Удалить', callback_data=f'backup_delete_{backup_filename}')],
            [InlineKeyboardButton(text='◀️ К списку', callback_data='backup_list')],
        ]
    )


def get_backup_settings_keyboard(settings_obj):
    auto_status = '✅ Включены' if settings_obj.auto_backup_enabled else '❌ Отключены'
    compression_status = '✅ Включено' if settings_obj.compression_enabled else '❌ Отключено'
    logs_status = '✅ Включены' if settings_obj.include_logs else '❌ Отключены'

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f'🔄 Автобекапы: {auto_status}', callback_data='backup_toggle_auto')],
            [InlineKeyboardButton(text=f'🗜️ Сжатие: {compression_status}', callback_data='backup_toggle_compression')],
            [InlineKeyboardButton(text=f'📋 Логи в бекапе: {logs_status}', callback_data='backup_toggle_logs')],
            [InlineKeyboardButton(text='◀️ Назад', callback_data='backup_panel')],
        ]
    )


@admin_required
@error_handler
async def show_backup_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    settings_obj = await backup_service.get_backup_settings()

    status_auto = '✅ Включены' if settings_obj.auto_backup_enabled else '❌ Отключены'

    text = f"""🗄️ <b>СИСТЕМА БЕКАПОВ</b>

📊 <b>Статус:</b>
• Автобекапы: {status_auto}
• Интервал: {settings_obj.backup_interval_hours} часов
• Хранить: {settings_obj.max_backups_keep} файлов
• Сжатие: {'Да' if settings_obj.compression_enabled else 'Нет'}

📁 <b>Расположение:</b> <code>/app/data/backups</code>

⚡ <b>Доступные операции:</b>
• Создание полного бекапа всех данных
• Восстановление из файла бекапа
• Управление автоматическими бекапами
"""

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_main_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def create_backup_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.answer('🔄 Создание бекапа запущено...')

    progress_msg = await callback.message.edit_text(
        '🔄 <b>Создание бекапа...</b>\n\n⏳ Экспортируем данные из базы...\nЭто может занять несколько минут.',
        parse_mode='HTML',
    )

    # Создаем бекап
    created_by_id = db_user.telegram_id or db_user.email or f'#{db_user.id}'
    success, message, file_path = await backup_service.create_backup(created_by=created_by_id, compress=True)

    if success:
        await progress_msg.edit_text(
            f'✅ <b>Бекап создан успешно!</b>\n\n{message}',
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )
    else:
        await progress_msg.edit_text(
            f'❌ <b>Ошибка создания бекапа</b>\n\n{html.escape(message)}',
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )


@admin_required
@error_handler
async def show_backup_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = 1
    if callback.data.startswith('backup_list_page_'):
        try:
            page = int(callback.data.split('_')[-1])
        except:
            page = 1

    backups = await backup_service.get_backup_list()

    if not backups:
        text = '📦 <b>Список бекапов пуст</b>\n\nБекапы еще не создавались.'
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='🚀 Создать первый бекап', callback_data='backup_create')],
                [InlineKeyboardButton(text='◀️ Назад', callback_data='backup_panel')],
            ]
        )
    else:
        text = f'📦 <b>Список бекапов</b> (всего: {len(backups)})\n\n'
        text += 'Выберите бекап для управления:'
        keyboard = get_backup_list_keyboard(backups, page)

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def manage_backup_file(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    filename = callback.data.replace('backup_manage_', '')

    backups = await backup_service.get_backup_list()
    backup_info = None

    for backup in backups:
        if backup['filename'] == filename:
            backup_info = backup
            break

    if not backup_info:
        await callback.answer('❌ Файл бекапа не найден', show_alert=True)
        return

    try:
        if backup_info.get('timestamp'):
            dt = datetime.fromisoformat(backup_info['timestamp'].replace('Z', '+00:00'))
            date_str = dt.strftime('%d.%m.%Y %H:%M:%S')
        else:
            date_str = 'Неизвестно'
    except:
        date_str = 'Ошибка формата даты'

    text = f"""📦 <b>Информация о бекапе</b>

📄 <b>Файл:</b> <code>{filename}</code>
📅 <b>Создан:</b> {date_str}
💾 <b>Размер:</b> {backup_info.get('file_size_mb', 0):.2f} MB
📊 <b>Таблиц:</b> {backup_info.get('tables_count', '?')}
📈 <b>Записей:</b> {backup_info.get('total_records', '?'):,}
🗜️ <b>Сжатие:</b> {'Да' if backup_info.get('compressed') else 'Нет'}
🗄️ <b>БД:</b> {backup_info.get('database_type', 'unknown')}
"""

    if backup_info.get('error'):
        text += f'\n⚠️ <b>Ошибка:</b> {backup_info["error"]}'

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_manage_keyboard(filename))
    await callback.answer()


@admin_required
@error_handler
async def delete_backup_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    filename = callback.data.replace('backup_delete_', '')

    text = '🗑️ <b>Удаление бекапа</b>\n\n'
    text += 'Вы уверены, что хотите удалить бекап?\n\n'
    text += f'📄 <code>{filename}</code>\n\n'
    text += '⚠️ <b>Это действие нельзя отменить!</b>'

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='✅ Да, удалить', callback_data=f'backup_delete_confirm_{filename}'),
                InlineKeyboardButton(text='❌ Отмена', callback_data=f'backup_manage_{filename}'),
            ]
        ]
    )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def delete_backup_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    filename = callback.data.replace('backup_delete_confirm_', '')

    success, message = await backup_service.delete_backup(filename)

    if success:
        await callback.message.edit_text(
            f'✅ <b>Бекап удален</b>\n\n{message}',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='📋 К списку бекапов', callback_data='backup_list')]]
            ),
        )
    else:
        await callback.message.edit_text(
            f'❌ <b>Ошибка удаления</b>\n\n{message}',
            parse_mode='HTML',
            reply_markup=get_backup_manage_keyboard(filename),
        )

    await callback.answer()


@admin_required
@error_handler
async def restore_backup_start(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    if callback.data.startswith('backup_restore_file_'):
        # Восстановление из конкретного файла
        filename = callback.data.replace('backup_restore_file_', '')

        text = '📥 <b>Восстановление из бекапа</b>\n\n'
        text += f'📄 <b>Файл:</b> <code>{filename}</code>\n\n'
        text += '⚠️ <b>ВНИМАНИЕ!</b>\n'
        text += '• Процесс может занять несколько минут\n'
        text += '• Рекомендуется создать бекап перед восстановлением\n'
        text += '• Существующие данные будут дополнены\n\n'
        text += 'Продолжить восстановление?'

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='✅ Да, восстановить', callback_data=f'backup_restore_execute_{filename}'
                    ),
                    InlineKeyboardButton(
                        text='🗑️ Очистить и восстановить', callback_data=f'backup_restore_clear_{filename}'
                    ),
                ],
                [InlineKeyboardButton(text='❌ Отмена', callback_data=f'backup_manage_{filename}')],
            ]
        )
    else:
        text = """📥 <b>Восстановление из бекапа</b>

📎 Отправьте файл бекапа (.json, .json.gz или .tar.gz)

⚠️ <b>ВАЖНО:</b>
• Файл должен быть создан этой системой бекапов
• Процесс может занять несколько минут
• Рекомендуется создать бекап перед восстановлением

💡 Или выберите из существующих бекапов ниже."""

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='📋 Выбрать из списка', callback_data='backup_list')],
                [InlineKeyboardButton(text='❌ Отмена', callback_data='backup_panel')],
            ]
        )

        await state.set_state(BackupStates.waiting_backup_file)

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def restore_backup_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    if callback.data.startswith('backup_restore_execute_'):
        filename = callback.data.replace('backup_restore_execute_', '')
        clear_existing = False
    elif callback.data.startswith('backup_restore_clear_'):
        filename = callback.data.replace('backup_restore_clear_', '')
        clear_existing = True
    else:
        await callback.answer('❌ Неверный формат команды', show_alert=True)
        return

    await callback.answer('🔄 Восстановление запущено...')

    # Показываем прогресс
    action_text = 'очисткой и восстановлением' if clear_existing else 'восстановлением'
    progress_msg = await callback.message.edit_text(
        f'📥 <b>Восстановление из бекапа...</b>\n\n'
        f'⏳ Работаем с {action_text} данных...\n'
        f'📄 Файл: <code>{filename}</code>\n\n'
        f'Это может занять несколько минут.',
        parse_mode='HTML',
    )

    backup_path = backup_service.backup_dir / filename

    success, message = await backup_service.restore_backup(str(backup_path), clear_existing=clear_existing)

    if success:
        await progress_msg.edit_text(
            f'✅ <b>Восстановление завершено!</b>\n\n{message}',
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )
    else:
        await progress_msg.edit_text(
            f'❌ <b>Ошибка восстановления</b>\n\n{message}',
            parse_mode='HTML',
            reply_markup=get_backup_manage_keyboard(filename),
        )


@admin_required
@error_handler
async def handle_backup_file_upload(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    if not message.document:
        await message.answer(
            '❌ Пожалуйста, отправьте файл бекапа (.json, .json.gz или .tar.gz)',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='◀️ Отмена', callback_data='backup_panel')]]
            ),
        )
        return

    document = message.document
    allowed_extensions = ('.json', '.json.gz', '.tar.gz', '.tar')

    if not document.file_name or not any(document.file_name.endswith(ext) for ext in allowed_extensions):
        await message.answer(
            '❌ Неподдерживаемый формат файла. Загрузите .json, .json.gz или .tar.gz файл',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='◀️ Отмена', callback_data='backup_panel')]]
            ),
        )
        return

    if document.file_size > 50 * 1024 * 1024:
        await message.answer(
            '❌ Файл слишком большой (максимум 50MB)',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='◀️ Отмена', callback_data='backup_panel')]]
            ),
        )
        return

    try:
        file = await message.bot.get_file(document.file_id)

        temp_path = backup_service.backup_dir / f'uploaded_{document.file_name}'

        await message.bot.download_file(file.file_path, temp_path)

        text = f"""📥 <b>Файл загружен</b>

📄 <b>Имя:</b> <code>{document.file_name}</code>
💾 <b>Размер:</b> {document.file_size / 1024 / 1024:.2f} MB

⚠️ <b>ВНИМАНИЕ!</b>
Процесс восстановления изменит данные в базе.
Рекомендуется создать бекап перед восстановлением.

Продолжить?"""

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text='✅ Восстановить', callback_data=f'backup_restore_execute_{temp_path.name}'
                    ),
                    InlineKeyboardButton(
                        text='🗑️ Очистить и восстановить',
                        callback_data=f'backup_restore_clear_{temp_path.name}',
                    ),
                ],
                [InlineKeyboardButton(text='❌ Отмена', callback_data='backup_panel')],
            ]
        )

        await message.answer(text, parse_mode='HTML', reply_markup=keyboard)
        await state.clear()

    except Exception as e:
        logger.error('Ошибка загрузки файла бекапа', error=e)
        await message.answer(
            f'❌ Ошибка загрузки файла: {e!s}',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='◀️ Отмена', callback_data='backup_panel')]]
            ),
        )


@admin_required
@error_handler
async def show_backup_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    settings_obj = await backup_service.get_backup_settings()

    text = f"""⚙️ <b>Настройки системы бекапов</b>

🔄 <b>Автоматические бекапы:</b>
• Статус: {'✅ Включены' if settings_obj.auto_backup_enabled else '❌ Отключены'}
• Интервал: {settings_obj.backup_interval_hours} часов
• Время запуска: {settings_obj.backup_time}

📦 <b>Хранение:</b>
• Максимум файлов: {settings_obj.max_backups_keep}
• Сжатие: {'✅ Включено' if settings_obj.compression_enabled else '❌ Отключено'}
• Включать логи: {'✅ Да' if settings_obj.include_logs else '❌ Нет'}

📁 <b>Расположение:</b> <code>{settings_obj.backup_location}</code>
"""

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_settings_keyboard(settings_obj))
    await callback.answer()


@admin_required
@error_handler
async def toggle_backup_setting(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    settings_obj = await backup_service.get_backup_settings()

    if callback.data == 'backup_toggle_auto':
        new_value = not settings_obj.auto_backup_enabled
        await backup_service.update_backup_settings(auto_backup_enabled=new_value)
        status = 'включены' if new_value else 'отключены'
        await callback.answer(f'Автобекапы {status}')

    elif callback.data == 'backup_toggle_compression':
        new_value = not settings_obj.compression_enabled
        await backup_service.update_backup_settings(compression_enabled=new_value)
        status = 'включено' if new_value else 'отключено'
        await callback.answer(f'Сжатие {status}')

    elif callback.data == 'backup_toggle_logs':
        new_value = not settings_obj.include_logs
        await backup_service.update_backup_settings(include_logs=new_value)
        status = 'включены' if new_value else 'отключены'
        await callback.answer(f'Логи в бекапе {status}')

    await show_backup_settings(callback, db_user, db)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_backup_panel, F.data == 'backup_panel')

    dp.callback_query.register(create_backup_handler, F.data == 'backup_create')

    dp.callback_query.register(show_backup_list, F.data.startswith('backup_list'))

    dp.callback_query.register(manage_backup_file, F.data.startswith('backup_manage_'))

    dp.callback_query.register(
        delete_backup_confirm, F.data.startswith('backup_delete_') & ~F.data.startswith('backup_delete_confirm_')
    )

    dp.callback_query.register(delete_backup_execute, F.data.startswith('backup_delete_confirm_'))

    dp.callback_query.register(
        restore_backup_start, F.data.in_(['backup_restore']) | F.data.startswith('backup_restore_file_')
    )

    dp.callback_query.register(
        restore_backup_execute,
        F.data.startswith('backup_restore_execute_') | F.data.startswith('backup_restore_clear_'),
    )

    dp.callback_query.register(show_backup_settings, F.data == 'backup_settings')

    dp.callback_query.register(
        toggle_backup_setting, F.data.in_(['backup_toggle_auto', 'backup_toggle_compression', 'backup_toggle_logs'])
    )

    dp.message.register(handle_backup_file_upload, BackupStates.waiting_backup_file)
