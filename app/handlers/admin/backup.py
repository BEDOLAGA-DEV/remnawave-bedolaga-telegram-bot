import html
from datetime import datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.localization.texts import get_texts
from app.services.backup_service import backup_service
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


class BackupStates(StatesGroup):
    waiting_backup_file = State()
    waiting_settings_update = State()


def get_backup_main_keyboard(language: str = 'ru'):
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_CREATE_BUTTON', '🚀 Создать бекап'),
                    callback_data='backup_create',
                ),
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_RESTORE_BUTTON', '📥 Восстановить'),
                    callback_data='backup_restore',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_LIST_BUTTON', '📋 Список бекапов'),
                    callback_data='backup_list',
                ),
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_SETTINGS_BUTTON', '⚙️ Настройки'),
                    callback_data='backup_settings',
                ),
            ],
            [InlineKeyboardButton(text=texts.t('BACK_BUTTON', '◀️ Назад'), callback_data='admin_panel')],
        ]
    )


def get_backup_list_keyboard(backups: list, page: int = 1, per_page: int = 5, language: str = 'ru'):
    texts = get_texts(language)
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

        button_text = texts.t(
            'ADMIN_BACKUP_LIST_ITEM_TEMPLATE',
            '📦 {date_str} • {size_str} • {records_str} записей',
        ).format(
            date_str=date_str,
            size_str=size_str,
            records_str=records_str,
        )
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

    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_BACK_TO_PANEL_BUTTON', '◀️ Назад'),
                    callback_data='backup_panel',
                )
            ]
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_backup_manage_keyboard(backup_filename: str, language: str = 'ru'):
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_RESTORE_BUTTON', '📥 Восстановить'),
                    callback_data=f'backup_restore_file_{backup_filename}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_DELETE_BUTTON', '🗑️ Удалить'),
                    callback_data=f'backup_delete_{backup_filename}',
                )
            ],
            [InlineKeyboardButton(text=texts.t('ADMIN_BACK_TO_LIST', '⬅️ К списку'), callback_data='backup_list')],
        ]
    )


def get_backup_settings_keyboard(settings_obj, language: str = 'ru'):
    texts = get_texts(language)
    auto_status = (
        texts.t('ADMIN_BACKUP_STATUS_ENABLED_PLURAL', '✅ Включены')
        if settings_obj.auto_backup_enabled
        else texts.t('ADMIN_BACKUP_STATUS_DISABLED_PLURAL', '❌ Отключены')
    )
    compression_status = (
        texts.t('ADMIN_BACKUP_STATUS_ENABLED_SINGULAR', '✅ Включено')
        if settings_obj.compression_enabled
        else texts.t('ADMIN_BACKUP_STATUS_DISABLED_SINGULAR', '❌ Отключено')
    )
    logs_status = (
        texts.t('ADMIN_BACKUP_STATUS_ENABLED_PLURAL', '✅ Включены')
        if settings_obj.include_logs
        else texts.t('ADMIN_BACKUP_STATUS_DISABLED_PLURAL', '❌ Отключены')
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_AUTO_TOGGLE_BUTTON', '🔄 Автобекапы: {status}').format(
                        status=auto_status
                    ),
                    callback_data='backup_toggle_auto',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_COMPRESSION_TOGGLE_BUTTON', '🗜️ Сжатие: {status}').format(
                        status=compression_status
                    ),
                    callback_data='backup_toggle_compression',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_INCLUDE_LOGS_TOGGLE_BUTTON', '📋 Логи в бекапе: {status}').format(
                        status=logs_status
                    ),
                    callback_data='backup_toggle_logs',
                )
            ],
            [InlineKeyboardButton(text=texts.t('BACK_BUTTON', '◀️ Назад'), callback_data='backup_panel')],
        ]
    )


@admin_required
@error_handler
async def show_backup_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    settings_obj = await backup_service.get_backup_settings()

    status_auto = (
        texts.t('ADMIN_BACKUP_STATUS_ENABLED_PLURAL', '✅ Включены')
        if settings_obj.auto_backup_enabled
        else texts.t('ADMIN_BACKUP_STATUS_DISABLED_PLURAL', '❌ Отключены')
    )
    compression_yes_no = (
        texts.t('ADMIN_BACKUP_YES_SHORT', 'Да')
        if settings_obj.compression_enabled
        else texts.t('ADMIN_BACKUP_NO_SHORT', 'Нет')
    )

    text = texts.t(
        'ADMIN_BACKUP_PANEL_TEXT',
        """🗄️ <b>СИСТЕМА БЕКАПОВ</b>

📊 <b>Статус:</b>
• Автобекапы: {status_auto}
• Интервал: {interval_hours} часов
• Хранить: {max_backups_keep} файлов
• Сжатие: {compression_yes_no}

📁 <b>Расположение:</b> <code>/app/data/backups</code>

⚡ <b>Доступные операции:</b>
• Создание полного бекапа всех данных
• Восстановление из файла бекапа
• Управление автоматическими бекапами
""",
    ).format(
        status_auto=status_auto,
        interval_hours=settings_obj.backup_interval_hours,
        max_backups_keep=settings_obj.max_backups_keep,
        compression_yes_no=compression_yes_no,
    )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_main_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def create_backup_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('ADMIN_BACKUP_CREATE_STARTED_ALERT', '🔄 Создание бекапа запущено...'))

    progress_msg = await callback.message.edit_text(
        texts.t(
            'ADMIN_BACKUP_CREATE_PROGRESS_TEXT',
            '🔄 <b>Создание бекапа...</b>\n\n⏳ Экспортируем данные из базы...\nЭто может занять несколько минут.',
        ),
        parse_mode='HTML',
    )

    # Создаем бекап
    created_by_id = db_user.telegram_id or db_user.email or f'#{db_user.id}'
    success, message, file_path = await backup_service.create_backup(created_by=created_by_id, compress=True)

    if success:
        await progress_msg.edit_text(
            texts.t('ADMIN_BACKUP_CREATE_SUCCESS_TEXT', '✅ <b>Бекап создан успешно!</b>\n\n{message}').format(
                message=message
            ),
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )
    else:
        await progress_msg.edit_text(
            texts.t('ADMIN_BACKUP_CREATE_ERROR_TEXT', '❌ <b>Ошибка создания бекапа</b>\n\n{message}').format(
                message=message
            ),
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )


@admin_required
@error_handler
async def show_backup_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    page = 1
    if callback.data.startswith('backup_list_page_'):
        try:
            page = int(callback.data.split('_')[-1])
        except:
            page = 1

    backups = await backup_service.get_backup_list()

    if not backups:
        text = texts.t(
            'ADMIN_BACKUP_LIST_EMPTY_TEXT',
            '📦 <b>Список бекапов пуст</b>\n\nБекапы еще не создавались.',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_BACKUP_CREATE_FIRST_BUTTON', '🚀 Создать первый бекап'),
                        callback_data='backup_create',
                    )
                ],
                [InlineKeyboardButton(text=texts.t('BACK_BUTTON', '◀️ Назад'), callback_data='backup_panel')],
            ]
        )
    else:
        text = texts.t('ADMIN_BACKUP_LIST_TEXT', '📦 <b>Список бекапов</b> (всего: {count})\n\n').format(
            count=len(backups)
        )
        text += texts.t('ADMIN_BACKUP_LIST_SELECT_PROMPT', 'Выберите бекап для управления:')
        keyboard = get_backup_list_keyboard(backups, page, language=db_user.language)

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def manage_backup_file(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    filename = callback.data.replace('backup_manage_', '')

    backups = await backup_service.get_backup_list()
    backup_info = None

    for backup in backups:
        if backup['filename'] == filename:
            backup_info = backup
            break

    if not backup_info:
        await callback.answer(texts.t('ADMIN_BACKUP_FILE_NOT_FOUND_ALERT', '❌ Файл бекапа не найден'), show_alert=True)
        return

    try:
        if backup_info.get('timestamp'):
            dt = datetime.fromisoformat(backup_info['timestamp'].replace('Z', '+00:00'))
            date_str = dt.strftime('%d.%m.%Y %H:%M:%S')
        else:
            date_str = texts.t('ADMIN_BACKUP_UNKNOWN_DATE', 'Неизвестно')
    except:
        date_str = texts.t('ADMIN_BACKUP_INVALID_DATE_FORMAT', 'Ошибка формата даты')

    total_records_raw = backup_info.get('total_records')
    total_records = (
        f'{total_records_raw:,}'
        if isinstance(total_records_raw, int | float) and not isinstance(total_records_raw, bool)
        else '?'
    )
    compression_yes_no = (
        texts.t('ADMIN_BACKUP_YES_SHORT', 'Да')
        if backup_info.get('compressed')
        else texts.t('ADMIN_BACKUP_NO_SHORT', 'Нет')
    )

    text = texts.t(
        'ADMIN_BACKUP_FILE_INFO_TEXT',
        """📦 <b>Информация о бекапе</b>

📄 <b>Файл:</b> <code>{filename}</code>
📅 <b>Создан:</b> {date_str}
💾 <b>Размер:</b> {file_size_mb:.2f} MB
📊 <b>Таблиц:</b> {tables_count}
📈 <b>Записей:</b> {total_records}
🗜️ <b>Сжатие:</b> {compression_yes_no}
🗄️ <b>БД:</b> {database_type}
""",
    ).format(
        filename=filename,
        date_str=date_str,
        file_size_mb=backup_info.get('file_size_mb', 0),
        tables_count=backup_info.get('tables_count', '?'),
        total_records=total_records,
        compression_yes_no=compression_yes_no,
        database_type=backup_info.get('database_type', 'unknown'),
    )

    if backup_info.get('error'):
        text += texts.t('ADMIN_BACKUP_FILE_ERROR_TEXT', '\n⚠️ <b>Ошибка:</b> {error}').format(error=backup_info['error'])

    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=get_backup_manage_keyboard(filename, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_backup_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    filename = callback.data.replace('backup_delete_', '')

    text = texts.t(
        'ADMIN_BACKUP_DELETE_CONFIRM_TEXT',
        '🗑️ <b>Удаление бекапа</b>\n\n'
        'Вы уверены, что хотите удалить бекап?\n\n'
        '📄 <code>{filename}</code>\n\n'
        '⚠️ <b>Это действие нельзя отменить!</b>',
    ).format(filename=filename)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_BACKUP_DELETE_CONFIRM_BUTTON', '✅ Да, удалить'),
                    callback_data=f'backup_delete_confirm_{filename}',
                ),
                InlineKeyboardButton(
                    text=texts.t('ADMIN_CANCEL', '❌ Отмена'),
                    callback_data=f'backup_manage_{filename}',
                ),
            ]
        ]
    )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def delete_backup_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    filename = callback.data.replace('backup_delete_confirm_', '')

    success, message = await backup_service.delete_backup(filename)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_BACKUP_DELETE_SUCCESS_TEXT', '✅ <b>Бекап удален</b>\n\n{message}').format(message=message),
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_BACKUP_TO_LIST_BUTTON', '📋 К списку бекапов'),
                            callback_data='backup_list',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_BACKUP_DELETE_ERROR_TEXT', '❌ <b>Ошибка удаления</b>\n\n{message}').format(message=message),
            parse_mode='HTML',
            reply_markup=get_backup_manage_keyboard(filename, db_user.language),
        )

    await callback.answer()


@admin_required
@error_handler
async def restore_backup_start(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    if callback.data.startswith('backup_restore_file_'):
        # Восстановление из конкретного файла
        filename = callback.data.replace('backup_restore_file_', '')

        text = texts.t(
            'ADMIN_BACKUP_RESTORE_FILE_TEXT',
            '📥 <b>Восстановление из бекапа</b>\n\n'
            '📄 <b>Файл:</b> <code>{filename}</code>\n\n'
            '⚠️ <b>ВНИМАНИЕ!</b>\n'
            '• Процесс может занять несколько минут\n'
            '• Рекомендуется создать бекап перед восстановлением\n'
            '• Существующие данные будут дополнены\n\n'
            'Продолжить восстановление?',
        ).format(filename=filename)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_BACKUP_RESTORE_CONFIRM_BUTTON', '✅ Да, восстановить'),
                        callback_data=f'backup_restore_execute_{filename}',
                    ),
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_BACKUP_RESTORE_CLEAR_BUTTON', '🗑️ Очистить и восстановить'),
                        callback_data=f'backup_restore_clear_{filename}',
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_CANCEL', '❌ Отмена'),
                        callback_data=f'backup_manage_{filename}',
                    )
                ],
            ]
        )
    else:
        text = texts.t(
            'ADMIN_BACKUP_RESTORE_UPLOAD_TEXT',
            """📥 <b>Восстановление из бекапа</b>

📎 Отправьте файл бекапа (.json или .json.gz)

⚠️ <b>ВАЖНО:</b>
• Файл должен быть создан этой системой бекапов
• Процесс может занять несколько минут
• Рекомендуется создать бекап перед восстановлением

💡 Или выберите из существующих бекапов ниже.""",
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_BACKUP_RESTORE_FROM_LIST_BUTTON', '📋 Выбрать из списка'),
                        callback_data='backup_list',
                    )
                ],
                [InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='backup_panel')],
            ]
        )

        await state.set_state(BackupStates.waiting_backup_file)

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def restore_backup_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    if callback.data.startswith('backup_restore_execute_'):
        filename = callback.data.replace('backup_restore_execute_', '')
        clear_existing = False
    elif callback.data.startswith('backup_restore_clear_'):
        filename = callback.data.replace('backup_restore_clear_', '')
        clear_existing = True
    else:
        await callback.answer(texts.t('ADMIN_BACKUP_INVALID_COMMAND_ALERT', '❌ Неверный формат команды'), show_alert=True)
        return

    await callback.answer(texts.t('ADMIN_BACKUP_RESTORE_STARTED_ALERT', '🔄 Восстановление запущено...'))

    # Показываем прогресс
    action_text = (
        texts.t('ADMIN_BACKUP_RESTORE_ACTION_WITH_CLEAR', 'очисткой и восстановлением')
        if clear_existing
        else texts.t('ADMIN_BACKUP_RESTORE_ACTION_APPEND', 'восстановлением')
    )
    progress_msg = await callback.message.edit_text(
        texts.t(
            'ADMIN_BACKUP_RESTORE_PROGRESS_TEXT',
            '📥 <b>Восстановление из бекапа...</b>\n\n'
            '⏳ Работаем с {action_text} данных...\n'
            '📄 Файл: <code>{filename}</code>\n\n'
            'Это может занять несколько минут.',
        ).format(
            action_text=action_text,
            filename=filename,
        ),
        parse_mode='HTML',
    )

    backup_path = backup_service.backup_dir / filename

    success, message = await backup_service.restore_backup(str(backup_path), clear_existing=clear_existing)

    if success:
        await progress_msg.edit_text(
            texts.t('ADMIN_BACKUP_RESTORE_SUCCESS_TEXT', '✅ <b>Восстановление завершено!</b>\n\n{message}').format(
                message=message
            ),
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )
    else:
        await progress_msg.edit_text(
            texts.t('ADMIN_BACKUP_RESTORE_ERROR_TEXT', '❌ <b>Ошибка восстановления</b>\n\n{message}').format(
                message=message
            ),
            parse_mode='HTML',
            reply_markup=get_backup_manage_keyboard(filename, db_user.language),
        )


@admin_required
@error_handler
async def handle_backup_file_upload(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    if not message.document:
        await message.answer(
            texts.t('ADMIN_BACKUP_UPLOAD_REQUIRE_FILE_TEXT', '❌ Пожалуйста, отправьте файл бекапа (.json или .json.gz)'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='backup_panel')]
                ]
            ),
        )
        return

    document = message.document

    if not (document.file_name.endswith('.json') or document.file_name.endswith('.json.gz')):
        await message.answer(
            texts.t(
                'ADMIN_BACKUP_UPLOAD_UNSUPPORTED_FORMAT_TEXT',
                '❌ Неподдерживаемый формат файла. Загрузите .json или .json.gz файл',
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='backup_panel')]
                ]
            ),
        )
        return

    if document.file_size > 50 * 1024 * 1024:
        await message.answer(
            texts.t('ADMIN_BACKUP_UPLOAD_TOO_LARGE_TEXT', '❌ Файл слишком большой (максимум 50MB)'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='backup_panel')]
                ]
            ),
        )
        return

    try:
        file = await message.bot.get_file(document.file_id)

        temp_path = backup_service.backup_dir / f'uploaded_{document.file_name}'

        await message.bot.download_file(file.file_path, temp_path)

        text = texts.t(
            'ADMIN_BACKUP_UPLOAD_SUCCESS_TEXT',
            """📥 <b>Файл загружен</b>

📄 <b>Имя:</b> <code>{file_name}</code>
💾 <b>Размер:</b> {file_size_mb:.2f} MB

⚠️ <b>ВНИМАНИЕ!</b>
Процесс восстановления изменит данные в базе.
Рекомендуется создать бекап перед восстановлением.

Продолжить?""",
        ).format(
            file_name=document.file_name,
            file_size_mb=document.file_size / 1024 / 1024,
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_BACKUP_UPLOAD_RESTORE_BUTTON', '✅ Восстановить'),
                        callback_data=f'backup_restore_uploaded_{temp_path.name}',
                    ),
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_BACKUP_RESTORE_CLEAR_BUTTON', '🗑️ Очистить и восстановить'),
                        callback_data=f'backup_restore_uploaded_clear_{temp_path.name}',
                    ),
                ],
                [InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='backup_panel')],
            ]
        )

        await message.answer(text, parse_mode='HTML', reply_markup=keyboard)
        await state.clear()

    except Exception as e:
        logger.error('Ошибка загрузки файла бекапа', error=e)
        await message.answer(
            texts.t('ADMIN_BACKUP_UPLOAD_ERROR_TEXT', '❌ Ошибка загрузки файла: {error}').format(error=e),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='backup_panel')]
                ]
            ),
        )


@admin_required
@error_handler
async def show_backup_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    settings_obj = await backup_service.get_backup_settings()

    auto_status = (
        texts.t('ADMIN_BACKUP_STATUS_ENABLED_PLURAL', '✅ Включены')
        if settings_obj.auto_backup_enabled
        else texts.t('ADMIN_BACKUP_STATUS_DISABLED_PLURAL', '❌ Отключены')
    )
    compression_status = (
        texts.t('ADMIN_BACKUP_STATUS_ENABLED_SINGULAR', '✅ Включено')
        if settings_obj.compression_enabled
        else texts.t('ADMIN_BACKUP_STATUS_DISABLED_SINGULAR', '❌ Отключено')
    )
    include_logs_status = (
        texts.t('ADMIN_BACKUP_STATUS_YES', '✅ Да')
        if settings_obj.include_logs
        else texts.t('ADMIN_BACKUP_STATUS_NO', '❌ Нет')
    )

    text = texts.t(
        'ADMIN_BACKUP_SETTINGS_TEXT',
        """⚙️ <b>Настройки системы бекапов</b>

🔄 <b>Автоматические бекапы:</b>
• Статус: {auto_status}
• Интервал: {backup_interval_hours} часов
• Время запуска: {backup_time}

📦 <b>Хранение:</b>
• Максимум файлов: {max_backups_keep}
• Сжатие: {compression_status}
• Включать логи: {include_logs_status}

📁 <b>Расположение:</b> <code>{backup_location}</code>
""",
    ).format(
        auto_status=auto_status,
        backup_interval_hours=settings_obj.backup_interval_hours,
        backup_time=settings_obj.backup_time,
        max_backups_keep=settings_obj.max_backups_keep,
        compression_status=compression_status,
        include_logs_status=include_logs_status,
        backup_location=settings_obj.backup_location,
    )

    await callback.message.edit_text(
        text,
        parse_mode='HTML',
        reply_markup=get_backup_settings_keyboard(settings_obj, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_backup_setting(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    settings_obj = await backup_service.get_backup_settings()

    if callback.data == 'backup_toggle_auto':
        new_value = not settings_obj.auto_backup_enabled
        await backup_service.update_backup_settings(auto_backup_enabled=new_value)
        await callback.answer(
            texts.t('ADMIN_BACKUP_TOGGLE_AUTO_ON_ALERT', 'Автобекапы включены')
            if new_value
            else texts.t('ADMIN_BACKUP_TOGGLE_AUTO_OFF_ALERT', 'Автобекапы отключены')
        )

    elif callback.data == 'backup_toggle_compression':
        new_value = not settings_obj.compression_enabled
        await backup_service.update_backup_settings(compression_enabled=new_value)
        await callback.answer(
            texts.t('ADMIN_BACKUP_TOGGLE_COMPRESSION_ON_ALERT', 'Сжатие включено')
            if new_value
            else texts.t('ADMIN_BACKUP_TOGGLE_COMPRESSION_OFF_ALERT', 'Сжатие отключено')
        )

    elif callback.data == 'backup_toggle_logs':
        new_value = not settings_obj.include_logs
        await backup_service.update_backup_settings(include_logs=new_value)
        await callback.answer(
            texts.t('ADMIN_BACKUP_TOGGLE_LOGS_ON_ALERT', 'Логи в бекапе включены')
            if new_value
            else texts.t('ADMIN_BACKUP_TOGGLE_LOGS_OFF_ALERT', 'Логи в бекапе отключены')
        )

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
