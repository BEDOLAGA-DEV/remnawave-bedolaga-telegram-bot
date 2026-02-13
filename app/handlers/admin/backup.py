import logging
from datetime import datetime

from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.backup_service import backup_service
from app.utils.decorators import admin_required, error_handler


logger = logging.getLogger(__name__)


class BackupStates(StatesGroup):
    waiting_backup_file = State()
    waiting_settings_update = State()


def _t(db_user: User, key: str, **kwargs) -> str:
    text = get_texts(getattr(db_user, 'language', settings.DEFAULT_LANGUAGE)).t(key)
    return text.format(**kwargs) if kwargs else text


def _t_lang(language: str, key: str, **kwargs) -> str:
    text = get_texts(language).t(key)
    return text.format(**kwargs) if kwargs else text


def get_backup_main_keyboard(language: str = 'ru'):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t_lang(language, 'ADMIN_BACKUP_CREATE_BUTTON'), callback_data='backup_create'
                ),
                InlineKeyboardButton(
                    text=_t_lang(language, 'ADMIN_BACKUP_RESTORE_BUTTON'), callback_data='backup_restore'
                ),
            ],
            [
                InlineKeyboardButton(text=_t_lang(language, 'ADMIN_BACKUP_LIST_BUTTON'), callback_data='backup_list'),
                InlineKeyboardButton(
                    text=_t_lang(language, 'ADMIN_BACKUP_SETTINGS_BUTTON'), callback_data='backup_settings'
                ),
            ],
            [InlineKeyboardButton(text=_t_lang(language, 'ADMIN_BACKUP_BACK_BUTTON'), callback_data='admin_panel')],
        ]
    )


def get_backup_list_keyboard(backups: list, language: str, page: int = 1, per_page: int = 5):
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

        button_text = _t_lang(
            language,
            'ADMIN_BACKUP_LIST_ITEM',
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
        [[InlineKeyboardButton(text=_t_lang(language, 'ADMIN_BACKUP_BACK_BUTTON'), callback_data='backup_panel')]]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_backup_manage_keyboard(backup_filename: str, language: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t_lang(language, 'ADMIN_BACKUP_RESTORE_BUTTON'),
                    callback_data=f'backup_restore_file_{backup_filename}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t_lang(language, 'ADMIN_BACKUP_DELETE_BUTTON'),
                    callback_data=f'backup_delete_{backup_filename}',
                )
            ],
            [InlineKeyboardButton(text=_t_lang(language, 'ADMIN_BACKUP_TO_LIST_BUTTON'), callback_data='backup_list')],
        ]
    )


def get_backup_settings_keyboard(settings_obj, language: str):
    auto_status = (
        _t_lang(language, 'ADMIN_BACKUP_ENABLED_STATUS')
        if settings_obj.auto_backup_enabled
        else _t_lang(language, 'ADMIN_BACKUP_DISABLED_STATUS')
    )
    compression_status = (
        _t_lang(language, 'ADMIN_BACKUP_ENABLED_SINGLE_STATUS')
        if settings_obj.compression_enabled
        else _t_lang(language, 'ADMIN_BACKUP_DISABLED_STATUS')
    )
    logs_status = (
        _t_lang(language, 'ADMIN_BACKUP_ENABLED_STATUS')
        if settings_obj.include_logs
        else _t_lang(language, 'ADMIN_BACKUP_DISABLED_STATUS')
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t_lang(language, 'ADMIN_BACKUP_SETTINGS_AUTO_BUTTON', status=auto_status),
                    callback_data='backup_toggle_auto',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t_lang(language, 'ADMIN_BACKUP_SETTINGS_COMPRESSION_BUTTON', status=compression_status),
                    callback_data='backup_toggle_compression',
                )
            ],
            [
                InlineKeyboardButton(
                    text=_t_lang(language, 'ADMIN_BACKUP_SETTINGS_LOGS_BUTTON', status=logs_status),
                    callback_data='backup_toggle_logs',
                )
            ],
            [InlineKeyboardButton(text=_t_lang(language, 'ADMIN_BACKUP_BACK_BUTTON'), callback_data='backup_panel')],
        ]
    )


@admin_required
@error_handler
async def show_backup_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    settings_obj = await backup_service.get_backup_settings()

    status_auto = (
        _t(db_user, 'ADMIN_BACKUP_ENABLED_STATUS')
        if settings_obj.auto_backup_enabled
        else _t(db_user, 'ADMIN_BACKUP_DISABLED_STATUS')
    )

    text = _t(
        db_user,
        'ADMIN_BACKUP_PANEL',
        auto_status=status_auto,
        interval_hours=settings_obj.backup_interval_hours,
        keep_count=settings_obj.max_backups_keep,
        compression_status=_t(db_user, 'ADMIN_BACKUP_YES')
        if settings_obj.compression_enabled
        else _t(db_user, 'ADMIN_BACKUP_NO'),
    )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=get_backup_main_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def create_backup_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.answer(_t(db_user, 'ADMIN_BACKUP_CREATE_STARTED_ALERT'))

    progress_msg = await callback.message.edit_text(
        _t(db_user, 'ADMIN_BACKUP_CREATE_PROGRESS'),
        parse_mode='HTML',
    )

    # Создаем бекап
    created_by_id = db_user.telegram_id or db_user.email or f'#{db_user.id}'
    success, message, file_path = await backup_service.create_backup(created_by=created_by_id, compress=True)

    if success:
        await progress_msg.edit_text(
            _t(db_user, 'ADMIN_BACKUP_CREATE_SUCCESS', message=message),
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )
    else:
        await progress_msg.edit_text(
            _t(db_user, 'ADMIN_BACKUP_CREATE_ERROR', message=message),
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
        text = _t(db_user, 'ADMIN_BACKUP_LIST_EMPTY')
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_BACKUP_CREATE_FIRST_BUTTON'), callback_data='backup_create'
                    )
                ],
                [InlineKeyboardButton(text=_t(db_user, 'ADMIN_BACKUP_BACK_BUTTON'), callback_data='backup_panel')],
            ]
        )
    else:
        text = _t(db_user, 'ADMIN_BACKUP_LIST_HEADER', total=len(backups))
        keyboard = get_backup_list_keyboard(backups, db_user.language, page)

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
        await callback.answer(_t(db_user, 'ADMIN_BACKUP_FILE_NOT_FOUND'), show_alert=True)
        return

    try:
        if backup_info.get('timestamp'):
            dt = datetime.fromisoformat(backup_info['timestamp'].replace('Z', '+00:00'))
            date_str = dt.strftime('%d.%m.%Y %H:%M:%S')
        else:
            date_str = _t(db_user, 'ADMIN_BACKUP_UNKNOWN')
    except:
        date_str = _t(db_user, 'ADMIN_BACKUP_DATE_FORMAT_ERROR')

    text = _t(
        db_user,
        'ADMIN_BACKUP_INFO',
        filename=filename,
        created_at=date_str,
        size_mb=f'{backup_info.get("file_size_mb", 0):.2f}',
        tables_count=backup_info.get('tables_count', '?'),
        total_records=f'{backup_info.get("total_records", "?"):,}'
        if isinstance(backup_info.get('total_records'), int)
        else backup_info.get('total_records', '?'),
        compression=_t(db_user, 'ADMIN_BACKUP_YES')
        if backup_info.get('compressed')
        else _t(db_user, 'ADMIN_BACKUP_NO'),
        database_type=backup_info.get('database_type', 'unknown'),
    )

    if backup_info.get('error'):
        text += _t(db_user, 'ADMIN_BACKUP_INFO_ERROR', error=backup_info['error'])

    await callback.message.edit_text(
        text, parse_mode='HTML', reply_markup=get_backup_manage_keyboard(filename, db_user.language)
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_backup_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    filename = callback.data.replace('backup_delete_', '')

    text = _t(db_user, 'ADMIN_BACKUP_DELETE_CONFIRM', filename=filename)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_t(db_user, 'ADMIN_BACKUP_DELETE_CONFIRM_BUTTON'),
                    callback_data=f'backup_delete_confirm_{filename}',
                ),
                InlineKeyboardButton(
                    text=_t(db_user, 'ADMIN_BACKUP_CANCEL_BUTTON'), callback_data=f'backup_manage_{filename}'
                ),
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
            _t(db_user, 'ADMIN_BACKUP_DELETE_SUCCESS', message=message),
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_BACKUP_TO_LIST_FULL_BUTTON'), callback_data='backup_list'
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            _t(db_user, 'ADMIN_BACKUP_DELETE_ERROR', message=message),
            parse_mode='HTML',
            reply_markup=get_backup_manage_keyboard(filename, db_user.language),
        )

    await callback.answer()


@admin_required
@error_handler
async def restore_backup_start(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    if callback.data.startswith('backup_restore_file_'):
        # Восстановление из конкретного файла
        filename = callback.data.replace('backup_restore_file_', '')

        text = _t(db_user, 'ADMIN_BACKUP_RESTORE_FILE_CONFIRM', filename=filename)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_BACKUP_RESTORE_CONFIRM_BUTTON'),
                        callback_data=f'backup_restore_execute_{filename}',
                    ),
                    InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_BACKUP_RESTORE_CLEAR_BUTTON'),
                        callback_data=f'backup_restore_clear_{filename}',
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_BACKUP_CANCEL_BUTTON'), callback_data=f'backup_manage_{filename}'
                    )
                ],
            ]
        )
    else:
        text = _t(db_user, 'ADMIN_BACKUP_RESTORE_UPLOAD_PROMPT')

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_BACKUP_PICK_FROM_LIST_BUTTON'), callback_data='backup_list'
                    )
                ],
                [InlineKeyboardButton(text=_t(db_user, 'ADMIN_BACKUP_CANCEL_BUTTON'), callback_data='backup_panel')],
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
        await callback.answer(_t(db_user, 'ADMIN_BACKUP_INVALID_COMMAND'), show_alert=True)
        return

    await callback.answer(_t(db_user, 'ADMIN_BACKUP_RESTORE_STARTED_ALERT'))

    # Показываем прогресс
    action_text = (
        _t(db_user, 'ADMIN_BACKUP_RESTORE_ACTION_CLEAR_AND_RESTORE')
        if clear_existing
        else _t(db_user, 'ADMIN_BACKUP_RESTORE_ACTION_RESTORE')
    )
    progress_msg = await callback.message.edit_text(
        _t(db_user, 'ADMIN_BACKUP_RESTORE_PROGRESS', action=action_text, filename=filename),
        parse_mode='HTML',
    )

    backup_path = backup_service.backup_dir / filename

    success, message = await backup_service.restore_backup(str(backup_path), clear_existing=clear_existing)

    if success:
        await progress_msg.edit_text(
            _t(db_user, 'ADMIN_BACKUP_RESTORE_SUCCESS', message=message),
            parse_mode='HTML',
            reply_markup=get_backup_main_keyboard(db_user.language),
        )
    else:
        await progress_msg.edit_text(
            _t(db_user, 'ADMIN_BACKUP_RESTORE_ERROR', message=message),
            parse_mode='HTML',
            reply_markup=get_backup_manage_keyboard(filename, db_user.language),
        )


@admin_required
@error_handler
async def handle_backup_file_upload(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    if not message.document:
        await message.answer(
            _t(db_user, 'ADMIN_BACKUP_UPLOAD_PLEASE_SEND_FILE'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_BACKUP_CANCEL_ALT_BUTTON'), callback_data='backup_panel'
                        )
                    ]
                ]
            ),
        )
        return

    document = message.document

    if not (document.file_name.endswith('.json') or document.file_name.endswith('.json.gz')):
        await message.answer(
            _t(db_user, 'ADMIN_BACKUP_UPLOAD_UNSUPPORTED_FORMAT'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_BACKUP_CANCEL_ALT_BUTTON'), callback_data='backup_panel'
                        )
                    ]
                ]
            ),
        )
        return

    if document.file_size > 50 * 1024 * 1024:
        await message.answer(
            _t(db_user, 'ADMIN_BACKUP_UPLOAD_TOO_LARGE'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_BACKUP_CANCEL_ALT_BUTTON'), callback_data='backup_panel'
                        )
                    ]
                ]
            ),
        )
        return

    try:
        file = await message.bot.get_file(document.file_id)

        temp_path = backup_service.backup_dir / f'uploaded_{document.file_name}'

        await message.bot.download_file(file.file_path, temp_path)

        text = _t(
            db_user,
            'ADMIN_BACKUP_UPLOAD_DONE',
            filename=document.file_name,
            size_mb=f'{document.file_size / 1024 / 1024:.2f}',
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_BACKUP_RESTORE_BUTTON_SHORT'),
                        callback_data=f'backup_restore_uploaded_{temp_path.name}',
                    ),
                    InlineKeyboardButton(
                        text=_t(db_user, 'ADMIN_BACKUP_RESTORE_CLEAR_BUTTON'),
                        callback_data=f'backup_restore_uploaded_clear_{temp_path.name}',
                    ),
                ],
                [InlineKeyboardButton(text=_t(db_user, 'ADMIN_BACKUP_CANCEL_BUTTON'), callback_data='backup_panel')],
            ]
        )

        await message.answer(text, parse_mode='HTML', reply_markup=keyboard)
        await state.clear()

    except Exception as e:
        logger.error(f'Ошибка загрузки файла бекапа: {e}')
        await message.answer(
            _t(db_user, 'ADMIN_BACKUP_UPLOAD_ERROR', error=e),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=_t(db_user, 'ADMIN_BACKUP_CANCEL_ALT_BUTTON'), callback_data='backup_panel'
                        )
                    ]
                ]
            ),
        )


@admin_required
@error_handler
async def show_backup_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    settings_obj = await backup_service.get_backup_settings()

    text = _t(
        db_user,
        'ADMIN_BACKUP_SETTINGS',
        auto_status=_t(db_user, 'ADMIN_BACKUP_ENABLED_STATUS')
        if settings_obj.auto_backup_enabled
        else _t(db_user, 'ADMIN_BACKUP_DISABLED_STATUS'),
        interval_hours=settings_obj.backup_interval_hours,
        backup_time=settings_obj.backup_time,
        keep_count=settings_obj.max_backups_keep,
        compression_status=_t(db_user, 'ADMIN_BACKUP_ENABLED_SINGLE_STATUS')
        if settings_obj.compression_enabled
        else _t(db_user, 'ADMIN_BACKUP_DISABLED_STATUS'),
        logs_status=_t(db_user, 'ADMIN_BACKUP_YES_STATUS')
        if settings_obj.include_logs
        else _t(db_user, 'ADMIN_BACKUP_NO_STATUS'),
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
    settings_obj = await backup_service.get_backup_settings()

    if callback.data == 'backup_toggle_auto':
        new_value = not settings_obj.auto_backup_enabled
        await backup_service.update_backup_settings(auto_backup_enabled=new_value)
        status = (
            _t(db_user, 'ADMIN_BACKUP_STATUS_ENABLED_LOWER')
            if new_value
            else _t(db_user, 'ADMIN_BACKUP_STATUS_DISABLED_LOWER')
        )
        await callback.answer(_t(db_user, 'ADMIN_BACKUP_TOGGLE_AUTO_RESULT', status=status))

    elif callback.data == 'backup_toggle_compression':
        new_value = not settings_obj.compression_enabled
        await backup_service.update_backup_settings(compression_enabled=new_value)
        status = (
            _t(db_user, 'ADMIN_BACKUP_STATUS_ENABLED_SINGULAR_LOWER')
            if new_value
            else _t(db_user, 'ADMIN_BACKUP_STATUS_DISABLED_LOWER')
        )
        await callback.answer(_t(db_user, 'ADMIN_BACKUP_TOGGLE_COMPRESSION_RESULT', status=status))

    elif callback.data == 'backup_toggle_logs':
        new_value = not settings_obj.include_logs
        await backup_service.update_backup_settings(include_logs=new_value)
        status = (
            _t(db_user, 'ADMIN_BACKUP_STATUS_ENABLED_LOWER')
            if new_value
            else _t(db_user, 'ADMIN_BACKUP_STATUS_DISABLED_LOWER')
        )
        await callback.answer(_t(db_user, 'ADMIN_BACKUP_TOGGLE_LOGS_RESULT', status=status))

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
