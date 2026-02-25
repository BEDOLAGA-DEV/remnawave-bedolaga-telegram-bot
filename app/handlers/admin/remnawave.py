import math
from datetime import datetime
from typing import Any

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.server_squad import (
    count_active_users_for_squad,
    get_all_server_squads,
    get_server_squad_by_uuid,
)
from app.database.models import User
from app.keyboards.admin import (
    get_admin_remnawave_keyboard,
    get_node_management_keyboard,
    get_squad_edit_keyboard,
    get_squad_management_keyboard,
)
from app.localization.texts import get_texts
from app.services.remnawave_service import RemnaWaveConfigurationError, RemnaWaveService
from app.services.remnawave_sync_service import (
    RemnaWaveAutoSyncStatus,
    remnawave_sync_service,
)
from app.services.system_settings_service import bot_configuration_service
from app.states import (
    RemnaWaveSyncStates,
    SquadCreateStates,
    SquadMigrationStates,
    SquadRenameStates,
)
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_bytes, format_datetime


logger = structlog.get_logger(__name__)

squad_inbound_selections = {}
squad_create_data = {}

MIGRATION_PAGE_SIZE = 8


def _format_duration(seconds: float, texts) -> str:
    if seconds < 1:
        return texts.t('ADMIN_RW_DURATION_LT_ONE_SEC', 'менее 1с')

    minutes, sec = divmod(int(seconds), 60)
    if minutes:
        if sec:
            return texts.t('ADMIN_RW_DURATION_MIN_SEC', '{minutes} мин {sec} с').format(minutes=minutes, sec=sec)
        return texts.t('ADMIN_RW_DURATION_MIN', '{minutes} мин').format(minutes=minutes)
    return texts.t('ADMIN_RW_DURATION_SEC', '{sec} с').format(sec=sec)


def _format_user_stats(stats: dict[str, Any] | None, texts) -> str:
    if not stats:
        return texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')

    created = stats.get('created', 0)
    updated = stats.get('updated', 0)
    deleted = stats.get('deleted', stats.get('deactivated', 0))
    errors = stats.get('errors', 0)

    return texts.t(
        'ADMIN_RW_AUTO_SYNC_USER_STATS',
        '• Создано: {created}\n• Обновлено: {updated}\n• Деактивировано: {deleted}\n• Ошибок: {errors}',
    ).format(created=created, updated=updated, deleted=deleted, errors=errors)


def _format_server_stats(stats: dict[str, Any] | None, texts) -> str:
    if not stats:
        return texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')

    created = stats.get('created', 0)
    updated = stats.get('updated', 0)
    removed = stats.get('removed', 0)
    total = stats.get('total', 0)

    return texts.t(
        'ADMIN_RW_AUTO_SYNC_SERVER_STATS',
        '• Создано: {created}\n• Обновлено: {updated}\n• Удалено: {removed}\n• Всего в панели: {total}',
    ).format(created=created, updated=updated, removed=removed, total=total)


def _build_auto_sync_view(status: RemnaWaveAutoSyncStatus, texts) -> tuple[str, types.InlineKeyboardMarkup]:
    times_text = ', '.join(t.strftime('%H:%M') for t in status.times) if status.times else texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')
    next_run_text = format_datetime(status.next_run) if status.next_run else texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')

    if status.last_run_finished_at:
        finished_text = format_datetime(status.last_run_finished_at)
        started_text = (
            format_datetime(status.last_run_started_at)
            if status.last_run_started_at
            else texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')
        )
        duration = status.last_run_finished_at - status.last_run_started_at if status.last_run_started_at else None
        duration_text = f' ({_format_duration(duration.total_seconds(), texts)})' if duration else ''
        reason_map = {
            'manual': texts.t('ADMIN_RW_AUTO_SYNC_REASON_MANUAL', 'вручную'),
            'auto': texts.t('ADMIN_RW_AUTO_SYNC_REASON_AUTO', 'по расписанию'),
            'immediate': texts.t('ADMIN_RW_AUTO_SYNC_REASON_IMMEDIATE', 'при включении'),
        }
        reason_text = reason_map.get(status.last_run_reason or '', texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—'))
        result_icon = '✅' if status.last_run_success else '❌'
        result_label = (
            texts.t('ADMIN_RW_AUTO_SYNC_RESULT_SUCCESS', 'успешно')
            if status.last_run_success
            else texts.t('ADMIN_RW_AUTO_SYNC_RESULT_WITH_ERRORS', 'с ошибками')
        )
        error_block = (
            '\n'
            + texts.t('ADMIN_RW_AUTO_SYNC_ERROR_LINE', '⚠️ Ошибка: {error}').format(error=status.last_run_error)
            if status.last_run_error
            else ''
        )
        last_run_text = texts.t(
            'ADMIN_RW_AUTO_SYNC_LAST_RUN',
            '{result_icon} {result_label}\n'
            '• Старт: {started_text}\n'
            '• Завершено: {finished_text}{duration_text}\n'
            '• Причина запуска: {reason_text}{error_block}',
        ).format(
            result_icon=result_icon,
            result_label=result_label,
            started_text=started_text,
            finished_text=finished_text,
            duration_text=duration_text,
            reason_text=reason_text,
            error_block=error_block,
        )
    elif status.last_run_started_at:
        last_run_text = (
            texts.t('ADMIN_RW_AUTO_SYNC_RUNNING_NOT_FINISHED', '⏳ Синхронизация началась, но еще не завершилась')
            if status.is_running
            else texts.t('ADMIN_RW_AUTO_SYNC_LAST_RUN_STARTED', 'ℹ️ Последний запуск: {started_at}').format(
                started_at=format_datetime(status.last_run_started_at)
            )
        )
    else:
        last_run_text = texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')

    running_text = (
        texts.t('ADMIN_RW_AUTO_SYNC_RUNNING', '⏳ Выполняется сейчас')
        if status.is_running
        else texts.t('ADMIN_RW_AUTO_SYNC_WAITING', 'Ожидание')
    )
    toggle_text = (
        texts.t('ADMIN_SERVER_DISABLE', '❌ Отключить')
        if status.enabled
        else texts.t('ADMIN_SERVER_ENABLE', '✅ Включить')
    )

    text = texts.t(
        'ADMIN_RW_AUTO_SYNC_VIEW_TEXT',
        '🔄 <b>Автосинхронизация RemnaWave</b>\n\n'
        '⚙️ <b>Статус:</b> {status_text}\n'
        '🕒 <b>Расписание:</b> {times_text}\n'
        '📅 <b>Следующий запуск:</b> {next_run}\n'
        '⏱️ <b>Состояние:</b> {running_text}\n\n'
        '📊 <b>Последний запуск:</b>\n'
        '{last_run_text}\n\n'
        '👥 <b>Пользователи:</b>\n'
        '{user_stats}\n\n'
        '🌐 <b>Серверы:</b>\n'
        '{server_stats}\n',
    ).format(
        status_text=(
            texts.t('ADMIN_BLACKLIST_STATUS_ENABLED', '✅ Включена')
            if status.enabled
            else texts.t('ADMIN_BLACKLIST_STATUS_DISABLED', '❌ Отключена')
        ),
        times_text=times_text,
        next_run=next_run_text if status.enabled else texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—'),
        running_text=running_text,
        last_run_text=last_run_text,
        user_stats=_format_user_stats(status.last_user_stats, texts),
        server_stats=_format_server_stats(status.last_server_stats, texts),
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_AUTO_SYNC_RUN_NOW_BUTTON', '🔁 Запустить сейчас'),
                    callback_data='remnawave_auto_sync_run',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=toggle_text,
                    callback_data='remnawave_auto_sync_toggle',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_AUTO_SYNC_CHANGE_SCHEDULE_BUTTON', '🕒 Изменить расписание'),
                    callback_data='remnawave_auto_sync_times',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data='admin_rw_sync',
                )
            ],
        ]
    )

    return text, keyboard


def _format_migration_server_label(texts, server) -> str:
    status = (
        texts.t('ADMIN_SQUAD_MIGRATION_STATUS_AVAILABLE', '✅ Доступен')
        if getattr(server, 'is_available', True)
        else texts.t('ADMIN_SQUAD_MIGRATION_STATUS_UNAVAILABLE', '🚫 Недоступен')
    )
    return texts.t(
        'ADMIN_SQUAD_MIGRATION_SERVER_LABEL',
        '{name} — 👥 {users} ({status})',
    ).format(name=server.display_name, users=server.current_users, status=status)


def _build_migration_keyboard(
    texts,
    squads,
    page: int,
    total_pages: int,
    stage: str,
    *,
    exclude_uuid: str = None,
):
    prefix = 'admin_migration_source' if stage == 'source' else 'admin_migration_target'
    rows = []
    has_items = False

    button_template = texts.t(
        'ADMIN_SQUAD_MIGRATION_SQUAD_BUTTON',
        '🌍 {name} — 👥 {users} ({status})',
    )

    for squad in squads:
        if exclude_uuid and squad.squad_uuid == exclude_uuid:
            continue

        has_items = True
        status = (
            texts.t('ADMIN_SQUAD_MIGRATION_STATUS_AVAILABLE_SHORT', '✅')
            if getattr(squad, 'is_available', True)
            else texts.t('ADMIN_SQUAD_MIGRATION_STATUS_UNAVAILABLE_SHORT', '🚫')
        )
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=button_template.format(
                        name=squad.display_name,
                        users=squad.current_users,
                        status=status,
                    ),
                    callback_data=f'{prefix}_{squad.squad_uuid}',
                )
            ]
        )

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(
                types.InlineKeyboardButton(
                    text='⬅️',
                    callback_data=f'{prefix}_page_{page - 1}',
                )
            )
        nav_buttons.append(
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_SQUAD_MIGRATION_PAGE',
                    'Стр. {page}/{pages}',
                ).format(page=page, pages=total_pages),
                callback_data='admin_migration_page_info',
            )
        )
        if page < total_pages:
            nav_buttons.append(
                types.InlineKeyboardButton(
                    text='➡️',
                    callback_data=f'{prefix}_page_{page + 1}',
                )
            )
        rows.append(nav_buttons)

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.CANCEL,
                callback_data='admin_migration_cancel',
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=rows), has_items


async def _fetch_migration_page(
    db: AsyncSession,
    page: int,
):
    squads, total = await get_all_server_squads(
        db,
        page=max(1, page),
        limit=MIGRATION_PAGE_SIZE,
    )
    total_pages = max(1, math.ceil(total / MIGRATION_PAGE_SIZE))

    page = max(page, 1)
    if page > total_pages:
        page = total_pages
        squads, total = await get_all_server_squads(
            db,
            page=page,
            limit=MIGRATION_PAGE_SIZE,
        )
        total_pages = max(1, math.ceil(total / MIGRATION_PAGE_SIZE))

    return squads, page, total_pages


@admin_required
@error_handler
async def show_squad_migration_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    await state.clear()

    squads, page, total_pages = await _fetch_migration_page(db, page=1)
    keyboard, has_items = _build_migration_keyboard(
        texts,
        squads,
        page,
        total_pages,
        'source',
    )

    message = (
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>Переезд сквадов</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECT_SOURCE',
            'Выберите сквад, из которого нужно переехать:',
        )
    )

    if not has_items:
        message += '\n\n' + texts.t(
            'ADMIN_SQUAD_MIGRATION_NO_OPTIONS',
            'Нет доступных сквадов. Добавьте новые или отмените операцию.',
        )

    await state.set_state(SquadMigrationStates.selecting_source)

    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def paginate_migration_source(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    if await state.get_state() != SquadMigrationStates.selecting_source:
        await callback.answer()
        return

    try:
        page = int(callback.data.split('_page_')[-1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    squads, page, total_pages = await _fetch_migration_page(db, page=page)
    texts = get_texts(db_user.language)
    keyboard, has_items = _build_migration_keyboard(
        texts,
        squads,
        page,
        total_pages,
        'source',
    )

    message = (
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>Переезд сквадов</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECT_SOURCE',
            'Выберите сквад, из которого нужно переехать:',
        )
    )

    if not has_items:
        message += '\n\n' + texts.t(
            'ADMIN_SQUAD_MIGRATION_NO_OPTIONS',
            'Нет доступных сквадов. Добавьте новые или отмените операцию.',
        )

    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_migration_source_selection(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    if await state.get_state() != SquadMigrationStates.selecting_source:
        await callback.answer()
        return

    if '_page_' in callback.data:
        await callback.answer()
        return

    source_uuid = callback.data.replace('admin_migration_source_', '', 1)

    texts = get_texts(db_user.language)
    server = await get_server_squad_by_uuid(db, source_uuid)

    if not server:
        await callback.answer(
            texts.t(
                'ADMIN_SQUAD_MIGRATION_SQUAD_NOT_FOUND',
                'Сквад не найден или недоступен.',
            ),
            show_alert=True,
        )
        return

    await state.update_data(
        source_uuid=server.squad_uuid,
        source_display=_format_migration_server_label(texts, server),
    )

    squads, page, total_pages = await _fetch_migration_page(db, page=1)
    keyboard, has_items = _build_migration_keyboard(
        texts,
        squads,
        page,
        total_pages,
        'target',
        exclude_uuid=server.squad_uuid,
    )

    message = (
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>Переезд сквадов</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECTED_SOURCE',
            'Источник: {source}',
        ).format(source=_format_migration_server_label(texts, server))
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECT_TARGET',
            'Выберите сквад, в который нужно переехать:',
        )
    )

    if not has_items:
        message += '\n\n' + texts.t(
            'ADMIN_SQUAD_MIGRATION_TARGET_EMPTY',
            'Нет других сквадов для переезда. Отмените операцию или создайте новые сквады.',
        )

    await state.set_state(SquadMigrationStates.selecting_target)

    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def paginate_migration_target(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    if await state.get_state() != SquadMigrationStates.selecting_target:
        await callback.answer()
        return

    try:
        page = int(callback.data.split('_page_')[-1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    data = await state.get_data()
    source_uuid = data.get('source_uuid')
    if not source_uuid:
        await callback.answer()
        return

    texts = get_texts(db_user.language)

    squads, page, total_pages = await _fetch_migration_page(db, page=page)
    keyboard, has_items = _build_migration_keyboard(
        texts,
        squads,
        page,
        total_pages,
        'target',
        exclude_uuid=source_uuid,
    )

    source_display = data.get('source_display') or source_uuid

    message = (
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>Переезд сквадов</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECTED_SOURCE',
            'Источник: {source}',
        ).format(source=source_display)
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECT_TARGET',
            'Выберите сквад, в который нужно переехать:',
        )
    )

    if not has_items:
        message += '\n\n' + texts.t(
            'ADMIN_SQUAD_MIGRATION_TARGET_EMPTY',
            'Нет других сквадов для переезда. Отмените операцию или создайте новые сквады.',
        )

    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_migration_target_selection(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    current_state = await state.get_state()
    if current_state != SquadMigrationStates.selecting_target:
        await callback.answer()
        return

    if '_page_' in callback.data:
        await callback.answer()
        return

    data = await state.get_data()
    source_uuid = data.get('source_uuid')

    if not source_uuid:
        await callback.answer()
        return

    target_uuid = callback.data.replace('admin_migration_target_', '', 1)

    texts = get_texts(db_user.language)

    if target_uuid == source_uuid:
        await callback.answer(
            texts.t(
                'ADMIN_SQUAD_MIGRATION_SAME_SQUAD',
                'Нельзя выбрать тот же сквад.',
            ),
            show_alert=True,
        )
        return

    target_server = await get_server_squad_by_uuid(db, target_uuid)
    if not target_server:
        await callback.answer(
            texts.t(
                'ADMIN_SQUAD_MIGRATION_SQUAD_NOT_FOUND',
                'Сквад не найден или недоступен.',
            ),
            show_alert=True,
        )
        return

    source_display = data.get('source_display') or source_uuid

    users_to_move = await count_active_users_for_squad(db, source_uuid)

    await state.update_data(
        target_uuid=target_server.squad_uuid,
        target_display=_format_migration_server_label(texts, target_server),
        migration_count=users_to_move,
    )

    await state.set_state(SquadMigrationStates.confirming)

    message_lines = [
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>Переезд сквадов</b>'),
        '',
        texts.t(
            'ADMIN_SQUAD_MIGRATION_CONFIRM_DETAILS',
            'Проверьте параметры переезда:',
        ),
        texts.t(
            'ADMIN_SQUAD_MIGRATION_CONFIRM_SOURCE',
            '• Из: {source}',
        ).format(source=source_display),
        texts.t(
            'ADMIN_SQUAD_MIGRATION_CONFIRM_TARGET',
            '• В: {target}',
        ).format(target=_format_migration_server_label(texts, target_server)),
        texts.t(
            'ADMIN_SQUAD_MIGRATION_CONFIRM_COUNT',
            '• Пользователей к переносу: {count}',
        ).format(count=users_to_move),
        '',
        texts.t(
            'ADMIN_SQUAD_MIGRATION_CONFIRM_PROMPT',
            'Подтвердите выполнение операции.',
        ),
    ]

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_SQUAD_MIGRATION_CONFIRM_BUTTON',
                        '✅ Подтвердить',
                    ),
                    callback_data='admin_migration_confirm',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_SQUAD_MIGRATION_CHANGE_TARGET',
                        '🔄 Изменить сервер назначения',
                    ),
                    callback_data='admin_migration_change_target',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.CANCEL,
                    callback_data='admin_migration_cancel',
                )
            ],
        ]
    )

    await callback.message.edit_text(
        '\n'.join(message_lines),
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def change_migration_target(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    data = await state.get_data()
    source_uuid = data.get('source_uuid')

    if not source_uuid:
        await callback.answer()
        return

    await state.set_state(SquadMigrationStates.selecting_target)

    texts = get_texts(db_user.language)
    squads, page, total_pages = await _fetch_migration_page(db, page=1)
    keyboard, has_items = _build_migration_keyboard(
        texts,
        squads,
        page,
        total_pages,
        'target',
        exclude_uuid=source_uuid,
    )

    source_display = data.get('source_display') or source_uuid

    message = (
        texts.t('ADMIN_SQUAD_MIGRATION_TITLE', '🚚 <b>Переезд сквадов</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECTED_SOURCE',
            'Источник: {source}',
        ).format(source=source_display)
        + '\n\n'
        + texts.t(
            'ADMIN_SQUAD_MIGRATION_SELECT_TARGET',
            'Выберите сквад, в который нужно переехать:',
        )
    )

    if not has_items:
        message += '\n\n' + texts.t(
            'ADMIN_SQUAD_MIGRATION_TARGET_EMPTY',
            'Нет других сквадов для переезда. Отмените операцию или создайте новые сквады.',
        )

    await callback.message.edit_text(
        message,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_squad_migration(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    current_state = await state.get_state()
    if current_state != SquadMigrationStates.confirming:
        await callback.answer()
        return

    data = await state.get_data()
    source_uuid = data.get('source_uuid')
    target_uuid = data.get('target_uuid')

    if not source_uuid or not target_uuid:
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    remnawave_service = RemnaWaveService()

    await callback.answer(texts.t('ADMIN_SQUAD_MIGRATION_IN_PROGRESS', 'Запускаю переезд...'))

    try:
        result = await remnawave_service.migrate_squad_users(
            db,
            source_uuid=source_uuid,
            target_uuid=target_uuid,
        )
    except RemnaWaveConfigurationError as error:
        message = texts.t(
            'ADMIN_SQUAD_MIGRATION_API_ERROR',
            '❌ RemnaWave API не настроен: {error}',
        ).format(error=str(error))
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_SQUAD_MIGRATION_BACK_BUTTON',
                            '⬅️ В Remnawave',
                        ),
                        callback_data='admin_remnawave',
                    )
                ]
            ]
        )
        await callback.message.edit_text(message, reply_markup=reply_markup)
        await state.clear()
        return

    source_display = data.get('source_display') or source_uuid
    target_display = data.get('target_display') or target_uuid

    if not result.get('success'):
        error_message = result.get('message') or ''
        error_code = result.get('error') or 'unexpected'
        message = texts.t(
            'ADMIN_SQUAD_MIGRATION_ERROR',
            '❌ Не удалось выполнить переезд (код: {code}). {details}',
        ).format(code=error_code, details=error_message)
        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_SQUAD_MIGRATION_BACK_BUTTON',
                            '⬅️ В Remnawave',
                        ),
                        callback_data='admin_remnawave',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_SQUAD_MIGRATION_NEW_BUTTON',
                            '🔁 Новый переезд',
                        ),
                        callback_data='admin_rw_migration',
                    )
                ],
            ]
        )
        await callback.message.edit_text(message, reply_markup=reply_markup)
        await state.clear()
        return

    message_lines = [
        texts.t('ADMIN_SQUAD_MIGRATION_SUCCESS_TITLE', '✅ Переезд завершен'),
        '',
        texts.t('ADMIN_SQUAD_MIGRATION_CONFIRM_SOURCE', '• Из: {source}').format(source=source_display),
        texts.t('ADMIN_SQUAD_MIGRATION_CONFIRM_TARGET', '• В: {target}').format(target=target_display),
        '',
        texts.t(
            'ADMIN_SQUAD_MIGRATION_RESULT_TOTAL',
            'Найдено подписок: {count}',
        ).format(count=result.get('total', 0)),
        texts.t(
            'ADMIN_SQUAD_MIGRATION_RESULT_UPDATED',
            'Перенесено: {count}',
        ).format(count=result.get('updated', 0)),
    ]

    panel_updated = result.get('panel_updated', 0)
    panel_failed = result.get('panel_failed', 0)

    if panel_updated:
        message_lines.append(
            texts.t(
                'ADMIN_SQUAD_MIGRATION_RESULT_PANEL_UPDATED',
                'Обновлено в панели: {count}',
            ).format(count=panel_updated)
        )
    if panel_failed:
        message_lines.append(
            texts.t(
                'ADMIN_SQUAD_MIGRATION_RESULT_PANEL_FAILED',
                'Не удалось обновить в панели: {count}',
            ).format(count=panel_failed)
        )

    reply_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_SQUAD_MIGRATION_NEW_BUTTON',
                        '🔁 Новый переезд',
                    ),
                    callback_data='admin_rw_migration',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_SQUAD_MIGRATION_BACK_BUTTON',
                        '⬅️ В Remnawave',
                    ),
                    callback_data='admin_remnawave',
                )
            ],
        ]
    )

    await callback.message.edit_text(
        '\n'.join(message_lines),
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    await state.clear()


@admin_required
@error_handler
async def cancel_squad_migration(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    await state.clear()

    message = texts.t(
        'ADMIN_SQUAD_MIGRATION_CANCELLED',
        '❌ Переезд отменен.',
    )

    reply_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_SQUAD_MIGRATION_BACK_BUTTON',
                        '⬅️ В Remnawave',
                    ),
                    callback_data='admin_remnawave',
                )
            ]
        ]
    )

    await callback.message.edit_text(message, reply_markup=reply_markup)
    await callback.answer()


@admin_required
@error_handler
async def handle_migration_page_info(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    await callback.answer(
        texts.t('ADMIN_SQUAD_MIGRATION_PAGE_HINT', 'Это текущая страница.'),
        show_alert=False,
    )


@admin_required
@error_handler
async def show_remnawave_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    remnawave_service = RemnaWaveService()
    connection_test = await remnawave_service.test_api_connection()

    status = connection_test.get('status')
    if status == 'connected':
        status_emoji = '✅'
    elif status == 'not_configured':
        status_emoji = 'ℹ️'
    else:
        status_emoji = '❌'

    api_url_display = settings.REMNAWAVE_API_URL or texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')

    text = texts.t(
        'ADMIN_RW_MENU_TEXT',
        '🖥️ <b>Управление Remnawave</b>\n\n'
        '📡 <b>Соединение:</b> {status_emoji} {connection_message}\n'
        '🌐 <b>URL:</b> <code>{api_url_display}</code>\n\n'
        'Выберите действие:\n',
    ).format(
        status_emoji=status_emoji,
        connection_message=connection_test.get('message', texts.t('ADMIN_REFERRALS_STATS_NO_DATA', 'Нет данных')),
        api_url_display=api_url_display,
    )

    await callback.message.edit_text(text, reply_markup=get_admin_remnawave_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_system_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.get_system_statistics()

    if 'error' in stats:
        await callback.message.edit_text(
            texts.t('ADMIN_RULES_STATS_FETCH_ERROR_MESSAGE', '❌ Ошибка получения статистики: {error}').format(error=stats['error']),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')]]
            ),
        )
        await callback.answer()
        return

    system = stats.get('system', {})
    users_by_status = stats.get('users_by_status', {})
    server_info = stats.get('server_info', {})
    bandwidth = stats.get('bandwidth', {})
    traffic_periods = stats.get('traffic_periods', {})
    nodes_realtime = stats.get('nodes_realtime', [])
    nodes_weekly = stats.get('nodes_weekly', [])

    memory_total = server_info.get('memory_total', 1)
    memory_used_percent = (server_info.get('memory_used', 0) / memory_total * 100) if memory_total > 0 else 0

    uptime_seconds = server_info.get('uptime_seconds', 0)
    uptime_days = int(uptime_seconds // 86400)
    uptime_hours = int((uptime_seconds % 86400) // 3600)
    uptime_str = texts.t('ADMIN_RW_UPTIME_D_H', '{days}д {hours}ч').format(days=uptime_days, hours=uptime_hours)

    users_status_text = ''
    for status, count in users_by_status.items():
        status_emoji = {'ACTIVE': '✅', 'DISABLED': '❌', 'LIMITED': '⚠️', 'EXPIRED': '⏰'}.get(status, '❓')
        users_status_text += f'  {status_emoji} {status}: {count}\n'

    top_nodes_text = ''
    for i, node in enumerate(nodes_weekly[:3], 1):
        top_nodes_text += f'  {i}. {node["name"]}: {format_bytes(node["total_bytes"])}\n'

    realtime_nodes_text = ''
    for node in nodes_realtime[:3]:
        node_total = node.get('downloadBytes', 0) + node.get('uploadBytes', 0)
        if node_total > 0:
            realtime_nodes_text += (
                texts.t('ADMIN_RW_REALTIME_NODE_ITEM', '  📡 {name}: {traffic}')
                .format(name=node.get('nodeName', 'Unknown'), traffic=format_bytes(node_total))
                + '\n'
            )

    def format_traffic_change(difference_str):
        if not difference_str or difference_str == '0':
            return ''
        if difference_str.startswith('-'):
            return f' (🔻 {difference_str[1:]})'
        return f' (🔺 {difference_str})'

    text = texts.t(
        'ADMIN_RW_SYSTEM_STATS_TEXT',
        '📊 <b>Детальная статистика Remnawave</b>\n\n'
        '🖥️ <b>Сервер:</b>\n'
        '- CPU: {cpu_cores} ядер ({cpu_physical_cores} физ.)\n'
        '- RAM: {memory_used} / {memory_total} ({memory_used_percent:.1f}%)\n'
        '- Свободно: {memory_available}\n'
        '- Uptime: {uptime_str}\n\n'
        '👥 <b>Пользователи ({total_users} всего):</b>\n'
        '- 🟢 Онлайн сейчас: {users_online}\n'
        '- 📅 За сутки: {users_last_day}\n'
        '- 📊 За неделю: {users_last_week}\n'
        '- 💤 Никогда не заходили: {users_never_online}\n\n'
        '<b>Статусы пользователей:</b>\n'
        '{users_status_text}\n\n'
        '🌐 <b>Ноды ({nodes_online} онлайн):</b>',
    ).format(
        cpu_cores=server_info.get('cpu_cores', 0),
        cpu_physical_cores=server_info.get('cpu_physical_cores', 0),
        memory_used=format_bytes(server_info.get('memory_used', 0)),
        memory_total=format_bytes(memory_total),
        memory_used_percent=memory_used_percent,
        memory_available=format_bytes(server_info.get('memory_available', 0)),
        uptime_str=uptime_str,
        total_users=system.get('total_users', 0),
        users_online=system.get('users_online', 0),
        users_last_day=system.get('users_last_day', 0),
        users_last_week=system.get('users_last_week', 0),
        users_never_online=system.get('users_never_online', 0),
        users_status_text=users_status_text,
        nodes_online=system.get('nodes_online', 0),
    )

    if realtime_nodes_text:
        text += '\n' + texts.t('ADMIN_RW_SYSTEM_REALTIME_ACTIVITY', '<b>Реалтайм активность:</b>\n{items}').format(
            items=realtime_nodes_text
        )

    if top_nodes_text:
        text += '\n' + texts.t('ADMIN_RW_SYSTEM_TOP_NODES_WEEK', '<b>Топ нод за неделю:</b>\n{items}').format(
            items=top_nodes_text
        )

    text += '\n' + texts.t(
        'ADMIN_RW_SYSTEM_TRAFFIC_BLOCK',
        '\n📈 <b>Общий трафик пользователей:</b> {total_user_traffic}\n\n'
        '📊 <b>Трафик по периодам:</b>\n'
        '- 2 дня: {traffic_2_days}{traffic_2_days_diff}\n'
        '- 7 дней: {traffic_7_days}{traffic_7_days_diff}\n'
        '- 30 дней: {traffic_30_days}{traffic_30_days_diff}\n'
        '- Месяц: {traffic_month}{traffic_month_diff}\n'
        '- Год: {traffic_year}{traffic_year_diff}\n',
    ).format(
        total_user_traffic=format_bytes(system.get('total_user_traffic', 0)),
        traffic_2_days=format_bytes(traffic_periods.get('last_2_days', {}).get('current', 0)),
        traffic_2_days_diff=format_traffic_change(traffic_periods.get('last_2_days', {}).get('difference', '')),
        traffic_7_days=format_bytes(traffic_periods.get('last_7_days', {}).get('current', 0)),
        traffic_7_days_diff=format_traffic_change(traffic_periods.get('last_7_days', {}).get('difference', '')),
        traffic_30_days=format_bytes(traffic_periods.get('last_30_days', {}).get('current', 0)),
        traffic_30_days_diff=format_traffic_change(traffic_periods.get('last_30_days', {}).get('difference', '')),
        traffic_month=format_bytes(traffic_periods.get('current_month', {}).get('current', 0)),
        traffic_month_diff=format_traffic_change(traffic_periods.get('current_month', {}).get('difference', '')),
        traffic_year=format_bytes(traffic_periods.get('current_year', {}).get('current', 0)),
        traffic_year_diff=format_traffic_change(traffic_periods.get('current_year', {}).get('difference', '')),
    )

    if bandwidth.get('realtime_total', 0) > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_SYSTEM_REALTIME_TRAFFIC',
            '⚡ <b>Реалтайм трафик:</b>\n'
            '- Скачивание: {download}\n'
            '- Загрузка: {upload}\n'
            '- Итого: {total}\n',
        ).format(
            download=format_bytes(bandwidth.get('realtime_download', 0)),
            upload=format_bytes(bandwidth.get('realtime_upload', 0)),
            total=format_bytes(bandwidth.get('realtime_total', 0)),
        )

    text += '\n' + texts.t('ADMIN_RW_UPDATED_AT', '🕒 <b>Обновлено:</b> {updated_at}').format(
        updated_at=format_datetime(stats.get('last_updated', datetime.now()))
    )

    keyboard = [
        [types.InlineKeyboardButton(text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'), callback_data='admin_rw_system')],
        [
            types.InlineKeyboardButton(text=texts.t('ADMIN_RW_BUTTON_NODES', '📈 Ноды'), callback_data='admin_rw_nodes'),
            types.InlineKeyboardButton(text=texts.t('ADMIN_RW_BUTTON_SYNC', '👥 Синхронизация'), callback_data='admin_rw_sync'),
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_traffic_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    remnawave_service = RemnaWaveService()

    try:
        async with remnawave_service.get_api_client() as api:
            bandwidth_stats = await api.get_bandwidth_stats()

            realtime_usage = await api.get_nodes_realtime_usage()

            nodes_stats = await api.get_nodes_statistics()

    except Exception as e:
        await callback.message.edit_text(
            texts.t('ADMIN_RW_TRAFFIC_STATS_ERROR', '❌ Ошибка получения статистики трафика: {error}').format(error=e),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')]]
            ),
        )
        await callback.answer()
        return

    def parse_bandwidth(bandwidth_str):
        return remnawave_service._parse_bandwidth_string(bandwidth_str)

    total_realtime_download = sum(node.get('downloadBytes', 0) for node in realtime_usage)
    total_realtime_upload = sum(node.get('uploadBytes', 0) for node in realtime_usage)
    total_realtime = total_realtime_download + total_realtime_upload

    total_download_speed = sum(node.get('downloadSpeedBps', 0) for node in realtime_usage)
    total_upload_speed = sum(node.get('uploadSpeedBps', 0) for node in realtime_usage)

    periods = {
        'last_2_days': bandwidth_stats.get('bandwidthLastTwoDays', {}),
        'last_7_days': bandwidth_stats.get('bandwidthLastSevenDays', {}),
        'last_30_days': bandwidth_stats.get('bandwidthLast30Days', {}),
        'current_month': bandwidth_stats.get('bandwidthCalendarMonth', {}),
        'current_year': bandwidth_stats.get('bandwidthCurrentYear', {}),
    }

    def format_change(diff_str):
        if not diff_str or diff_str == '0':
            return ''
        if diff_str.startswith('-'):
            return f' 🔻 {diff_str[1:]}'
        return f' 🔺 {diff_str}'

    text = texts.t(
        'ADMIN_RW_TRAFFIC_STATS_TEXT',
        '📊 <b>Статистика трафика Remnawave</b>\n\n'
        '⚡ <b>Реалтайм данные:</b>\n'
        '- Скачивание: {realtime_download}\n'
        '- Загрузка: {realtime_upload}\n'
        '- Общий трафик: {realtime_total}\n\n'
        '🚀 <b>Текущие скорости:</b>\n'
        '- Скорость скачивания: {download_speed}/с\n'
        '- Скорость загрузки: {upload_speed}/с\n'
        '- Общая скорость: {total_speed}/с\n\n'
        '📈 <b>Статистика по периодам:</b>\n\n'
        '<b>За 2 дня:</b>\n'
        '- Текущий: {period_2_current}\n'
        '- Предыдущий: {period_2_previous}\n'
        '- Изменение:{period_2_change}\n\n'
        '<b>За 7 дней:</b>\n'
        '- Текущий: {period_7_current}\n'
        '- Предыдущий: {period_7_previous}\n'
        '- Изменение:{period_7_change}\n\n'
        '<b>За 30 дней:</b>\n'
        '- Текущий: {period_30_current}\n'
        '- Предыдущий: {period_30_previous}\n'
        '- Изменение:{period_30_change}\n\n'
        '<b>Текущий месяц:</b>\n'
        '- Текущий: {period_month_current}\n'
        '- Предыдущий: {period_month_previous}\n'
        '- Изменение:{period_month_change}\n\n'
        '<b>Текущий год:</b>\n'
        '- Текущий: {period_year_current}\n'
        '- Предыдущий: {period_year_previous}\n'
        '- Изменение:{period_year_change}\n',
    ).format(
        realtime_download=format_bytes(total_realtime_download),
        realtime_upload=format_bytes(total_realtime_upload),
        realtime_total=format_bytes(total_realtime),
        download_speed=format_bytes(total_download_speed),
        upload_speed=format_bytes(total_upload_speed),
        total_speed=format_bytes(total_download_speed + total_upload_speed),
        period_2_current=format_bytes(parse_bandwidth(periods['last_2_days'].get('current', '0'))),
        period_2_previous=format_bytes(parse_bandwidth(periods['last_2_days'].get('previous', '0'))),
        period_2_change=format_change(periods['last_2_days'].get('difference', '')),
        period_7_current=format_bytes(parse_bandwidth(periods['last_7_days'].get('current', '0'))),
        period_7_previous=format_bytes(parse_bandwidth(periods['last_7_days'].get('previous', '0'))),
        period_7_change=format_change(periods['last_7_days'].get('difference', '')),
        period_30_current=format_bytes(parse_bandwidth(periods['last_30_days'].get('current', '0'))),
        period_30_previous=format_bytes(parse_bandwidth(periods['last_30_days'].get('previous', '0'))),
        period_30_change=format_change(periods['last_30_days'].get('difference', '')),
        period_month_current=format_bytes(parse_bandwidth(periods['current_month'].get('current', '0'))),
        period_month_previous=format_bytes(parse_bandwidth(periods['current_month'].get('previous', '0'))),
        period_month_change=format_change(periods['current_month'].get('difference', '')),
        period_year_current=format_bytes(parse_bandwidth(periods['current_year'].get('current', '0'))),
        period_year_previous=format_bytes(parse_bandwidth(periods['current_year'].get('previous', '0'))),
        period_year_change=format_change(periods['current_year'].get('difference', '')),
    )

    if realtime_usage:
        text += '\n' + texts.t('ADMIN_RW_TRAFFIC_BY_NODES_TITLE', '🌐 <b>Трафик по нодам (реалтайм):</b>\n')
        for node in sorted(realtime_usage, key=lambda x: x.get('totalBytes', 0), reverse=True):
            node_total = node.get('totalBytes', 0)
            if node_total > 0:
                text += f'- {node.get("nodeName", "Unknown")}: {format_bytes(node_total)}\n'

    if nodes_stats.get('lastSevenDays'):
        text += '\n' + texts.t('ADMIN_RW_TOP_NODES_7D_TITLE', '📊 <b>Топ нод за 7 дней:</b>\n')

        nodes_weekly = {}
        for day_data in nodes_stats['lastSevenDays']:
            node_name = day_data['nodeName']
            if node_name not in nodes_weekly:
                nodes_weekly[node_name] = 0
            nodes_weekly[node_name] += int(day_data['totalBytes'])

        sorted_nodes = sorted(nodes_weekly.items(), key=lambda x: x[1], reverse=True)
        for i, (node_name, total_bytes) in enumerate(sorted_nodes[:5], 1):
            text += f'{i}. {node_name}: {format_bytes(total_bytes)}\n'

    text += '\n' + texts.t('ADMIN_RW_UPDATED_AT', '🕒 <b>Обновлено:</b> {updated_at}').format(
        updated_at=format_datetime(datetime.now())
    )

    keyboard = [
        [types.InlineKeyboardButton(text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'), callback_data='admin_rw_traffic')],
        [
            types.InlineKeyboardButton(text=texts.t('ADMIN_RW_BUTTON_NODES', '📈 Ноды'), callback_data='admin_rw_nodes'),
            types.InlineKeyboardButton(text=texts.t('ADMIN_RW_BUTTON_SYSTEM', '📊 Система'), callback_data='admin_rw_system'),
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_nodes_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    remnawave_service = RemnaWaveService()
    nodes = await remnawave_service.get_all_nodes()

    if not nodes:
        await callback.message.edit_text(
            texts.t('ADMIN_RW_NODES_NOT_FOUND_OR_CONNECTION_ERROR', '🖥️ Ноды не найдены или ошибка подключения'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')]]
            ),
        )
        await callback.answer()
        return

    text = texts.t('ADMIN_RW_NODES_MANAGEMENT_TITLE', '🖥️ <b>Управление нодами</b>\n\n')
    keyboard = []

    for node in nodes:
        status_emoji = '🟢' if node['is_node_online'] else '🔴'
        connection_emoji = '📡' if node['is_connected'] else '📵'

        text += texts.t(
            'ADMIN_RW_NODES_MANAGEMENT_ITEM',
            '{status_emoji} {connection_emoji} <b>{name}</b>\n'
            '🌍 {country_code} • {address}\n'
            '👥 Онлайн: {users_online}\n\n',
        ).format(
            status_emoji=status_emoji,
            connection_emoji=connection_emoji,
            name=node['name'],
            country_code=node['country_code'],
            address=node['address'],
            users_online=node['users_online'] or 0,
        )

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_NODE_MANAGE_BUTTON', '⚙️ {name}').format(name=node['name']),
                    callback_data=f'admin_node_manage_{node["uuid"]}',
                )
            ]
        )

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_RESTART_ALL_NODES_BUTTON', '🔄 Перезагрузить все'),
                    callback_data='admin_restart_all_nodes',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_node_details(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    node_uuid = callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()
    node = await remnawave_service.get_node_details(node_uuid)

    if not node:
        await callback.answer(texts.t('ADMIN_RW_NODE_NOT_FOUND', '❌ Нода не найдена'), show_alert=True)
        return

    status_emoji = '🟢' if node['is_node_online'] else '🔴'
    xray_emoji = '✅' if node['is_xray_running'] else '❌'

    yes_text = texts.t('ADMIN_BACKUP_YES_SHORT', 'Да')
    no_text = texts.t('ADMIN_BACKUP_NO_SHORT', 'Нет')
    dash = texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')
    status_change = format_datetime(node['last_status_change']) if node.get('last_status_change') else dash
    created_at = format_datetime(node['created_at']) if node.get('created_at') else dash
    updated_at = format_datetime(node['updated_at']) if node.get('updated_at') else dash
    notify_percent = f'{node["notify_percent"]}%' if node.get('notify_percent') is not None else dash
    cpu_info = node.get('cpu_model') or dash
    if node.get('cpu_count'):
        cpu_info = f'{node["cpu_count"]}x {cpu_info}'

    xray_uptime = node.get('xray_uptime') or dash
    connection_status = f'📡 {yes_text}' if node['is_connected'] else f'📵 {no_text}'
    disabled_status = f'❌ {yes_text}' if node['is_disabled'] else f'✅ {no_text}'
    traffic_limit = (
        format_bytes(node['traffic_limit_bytes'])
        if node['traffic_limit_bytes']
        else texts.t('ADMIN_RW_NO_LIMIT', 'Без лимита')
    )
    tracking_status = (
        texts.t('ADMIN_USER_STATUS_ACTIVE', '✅ Активен')
        if node.get('is_traffic_tracking_active')
        else texts.t('ADMIN_RW_TRACKING_DISABLED', '❌ Отключен')
    )
    total_ram = node.get('total_ram') or dash
    provider_uuid = node.get('provider_uuid') or dash
    traffic_reset_day = node.get('traffic_reset_day') or dash
    last_status_message = node.get('last_status_message') or dash

    text = texts.t(
        'ADMIN_RW_NODE_DETAILS_TEXT',
        '🖥️ <b>Нода: {name}</b>\n\n'
        '<b>Статус:</b>\n'
        '- Онлайн: {status_emoji} {online_status}\n'
        '- Xray: {xray_emoji} {xray_status}\n'
        '- Подключена: {connection_status}\n'
        '- Отключена: {disabled_status}\n'
        '- Изменение статуса: {status_change}\n'
        '- Сообщение: {last_status_message}\n'
        '- Uptime Xray: {xray_uptime}\n\n'
        '<b>Информация:</b>\n'
        '- Адрес: {address}\n'
        '- Страна: {country_code}\n'
        '- Пользователей онлайн: {users_online}\n'
        '- CPU: {cpu_info}\n'
        '- RAM: {total_ram}\n'
        '- Провайдер: {provider_uuid}\n\n'
        '<b>Трафик:</b>\n'
        '- Использовано: {traffic_used}\n'
        '- Лимит: {traffic_limit}\n'
        '- Трекинг: {tracking_status}\n'
        '- День сброса: {traffic_reset_day}\n'
        '- Уведомления: {notify_percent}\n'
        '- Множитель: {consumption_multiplier}\n\n'
        '<b>Метаданные:</b>\n'
        '- Создана: {created_at}\n'
        '- Обновлена: {updated_at}\n',
    ).format(
        name=node['name'],
        status_emoji=status_emoji,
        online_status=yes_text if node['is_node_online'] else no_text,
        xray_emoji=xray_emoji,
        xray_status=(
            texts.t('ADMIN_MAINTENANCE_MONITORING_ACTIVE', 'Запущен')
            if node['is_xray_running']
            else texts.t('ADMIN_MAINTENANCE_MONITORING_INACTIVE', 'Остановлен')
        ),
        connection_status=connection_status,
        disabled_status=disabled_status,
        status_change=status_change,
        last_status_message=last_status_message,
        xray_uptime=xray_uptime,
        address=node['address'],
        country_code=node['country_code'],
        users_online=node['users_online'],
        cpu_info=cpu_info,
        total_ram=total_ram,
        provider_uuid=provider_uuid,
        traffic_used=format_bytes(node['traffic_used_bytes']),
        traffic_limit=traffic_limit,
        tracking_status=tracking_status,
        traffic_reset_day=traffic_reset_day,
        notify_percent=notify_percent,
        consumption_multiplier=node.get('consumption_multiplier') or 1,
        created_at=created_at,
        updated_at=updated_at,
    )

    await callback.message.edit_text(text, reply_markup=get_node_management_keyboard(node_uuid, db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def manage_node(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    action, node_uuid = callback.data.split('_')[1], callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()
    success = await remnawave_service.manage_node(node_uuid, action)

    if success:
        action_text = {
            'enable': texts.t('ADMIN_BLACKLIST_STATUS_ENABLED_WORD', 'включена'),
            'disable': texts.t('ADMIN_BLACKLIST_STATUS_DISABLED_WORD', 'отключена'),
            'restart': texts.t('ADMIN_RW_NODE_ACTION_RESTARTED', 'перезагружена'),
        }
        await callback.answer(
            texts.t('ADMIN_RW_NODE_ACTION_SUCCESS', '✅ Нода {status}').format(
                status=action_text.get(action, texts.t('ADMIN_RW_NODE_ACTION_PROCESSED', 'обработана'))
            )
        )
    else:
        await callback.answer(texts.t('ADMIN_RW_NODE_ACTION_ERROR', '❌ Ошибка выполнения действия'), show_alert=True)

    await show_node_details(callback, db_user, db)


@admin_required
@error_handler
async def show_node_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    node_uuid = callback.data.split('_')[-1]

    remnawave_service = RemnaWaveService()

    node = await remnawave_service.get_node_details(node_uuid)

    if not node:
        await callback.answer(texts.t('ADMIN_RW_NODE_NOT_FOUND', '❌ Нода не найдена'), show_alert=True)
        return

    status_emoji = '🟢' if node['is_node_online'] else '🔴'
    xray_emoji = '✅' if node['is_xray_running'] else '❌'
    yes_text = texts.t('ADMIN_BACKUP_YES_SHORT', 'Да')
    no_text = texts.t('ADMIN_BACKUP_NO_SHORT', 'Нет')
    dash = texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')

    try:
        from datetime import datetime, timedelta

        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)

        node_usage = await remnawave_service.get_node_user_usage_by_range(node_uuid, start_date, end_date)

        realtime_stats = await remnawave_service.get_nodes_realtime_usage()

        node_realtime = None
        for stats in realtime_stats:
            if stats.get('nodeUuid') == node_uuid:
                node_realtime = stats
                break

        status_change = format_datetime(node['last_status_change']) if node.get('last_status_change') else dash
        created_at = format_datetime(node['created_at']) if node.get('created_at') else dash
        updated_at = format_datetime(node['updated_at']) if node.get('updated_at') else dash
        notify_percent = f'{node["notify_percent"]}%' if node.get('notify_percent') is not None else dash
        cpu_info = node.get('cpu_model') or dash
        if node.get('cpu_count'):
            cpu_info = f'{node["cpu_count"]}x {cpu_info}'

        text = texts.t(
            'ADMIN_RW_NODE_STATS_TEXT',
            '📊 <b>Статистика ноды: {name}</b>\n\n'
            '<b>Статус:</b>\n'
            '- Онлайн: {status_emoji} {online_status}\n'
            '- Xray: {xray_emoji} {xray_status}\n'
            '- Пользователей онлайн: {users_online}\n'
            '- Изменение статуса: {status_change}\n'
            '- Сообщение: {last_status_message}\n'
            '- Uptime Xray: {xray_uptime}\n\n'
            '<b>Ресурсы:</b>\n'
            '- CPU: {cpu_info}\n'
            '- RAM: {total_ram}\n'
            '- Провайдер: {provider_uuid}\n\n'
            '<b>Трафик:</b>\n'
            '- Использовано: {traffic_used}\n'
            '- Лимит: {traffic_limit}\n'
            '- Трекинг: {tracking_status}\n'
            '- День сброса: {traffic_reset_day}\n'
            '- Уведомления: {notify_percent}\n'
            '- Множитель: {consumption_multiplier}\n\n'
            '<b>Метаданные:</b>\n'
            '- Создана: {created_at}\n'
            '- Обновлена: {updated_at}\n',
        ).format(
            name=node['name'],
            status_emoji=status_emoji,
            online_status=yes_text if node['is_node_online'] else no_text,
            xray_emoji=xray_emoji,
            xray_status=(
                texts.t('ADMIN_MAINTENANCE_MONITORING_ACTIVE', 'Запущен')
                if node['is_xray_running']
                else texts.t('ADMIN_MAINTENANCE_MONITORING_INACTIVE', 'Остановлен')
            ),
            users_online=node['users_online'] or 0,
            status_change=status_change,
            last_status_message=node.get('last_status_message') or dash,
            xray_uptime=node.get('xray_uptime') or dash,
            cpu_info=cpu_info,
            total_ram=node.get('total_ram') or dash,
            provider_uuid=node.get('provider_uuid') or dash,
            traffic_used=format_bytes(node['traffic_used_bytes'] or 0),
            traffic_limit=(
                format_bytes(node['traffic_limit_bytes'])
                if node['traffic_limit_bytes']
                else texts.t('ADMIN_RW_NO_LIMIT', 'Без лимита')
            ),
            tracking_status=(
                texts.t('ADMIN_USER_STATUS_ACTIVE', '✅ Активен')
                if node.get('is_traffic_tracking_active')
                else texts.t('ADMIN_RW_TRACKING_DISABLED', '❌ Отключен')
            ),
            traffic_reset_day=node.get('traffic_reset_day') or dash,
            notify_percent=notify_percent,
            consumption_multiplier=node.get('consumption_multiplier') or 1,
            created_at=created_at,
            updated_at=updated_at,
        )

        if node_realtime:
            text += '\n' + texts.t(
                'ADMIN_RW_NODE_STATS_REALTIME',
                '<b>Реалтайм статистика:</b>\n'
                '- Скачано: {download}\n'
                '- Загружено: {upload}\n'
                '- Общий трафик: {total}\n'
                '- Скорость скачивания: {download_speed}/с\n'
                '- Скорость загрузки: {upload_speed}/с\n',
            ).format(
                download=format_bytes(node_realtime.get('downloadBytes', 0)),
                upload=format_bytes(node_realtime.get('uploadBytes', 0)),
                total=format_bytes(node_realtime.get('totalBytes', 0)),
                download_speed=format_bytes(node_realtime.get('downloadSpeedBps', 0)),
                upload_speed=format_bytes(node_realtime.get('uploadSpeedBps', 0)),
            )

        if node_usage:
            text += '\n' + texts.t('ADMIN_RW_NODE_STATS_7D_TITLE', '<b>Статистика за 7 дней:</b>\n')
            total_usage = 0
            for usage in node_usage[-5:]:
                daily_usage = usage.get('total', 0)
                total_usage += daily_usage
                text += f'- {usage.get("date", "N/A")}: {format_bytes(daily_usage)}\n'

            text += '\n' + texts.t('ADMIN_RW_NODE_STATS_7D_TOTAL', '<b>Общий трафик за 7 дней:</b> {total}').format(
                total=format_bytes(total_usage)
            )
        else:
            text += '\n' + texts.t('ADMIN_RW_NODE_STATS_7D_UNAVAILABLE', '<b>Статистика за 7 дней:</b> Данные недоступны')

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'), callback_data=f'node_stats_{node_uuid}')],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_node_manage_{node_uuid}')],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()

    except Exception as e:
        logger.error('Ошибка получения статистики ноды', node_uuid=node_uuid, error=e)

        text = texts.t(
            'ADMIN_RW_NODE_STATS_FALLBACK_TEXT',
            '📊 <b>Статистика ноды: {name}</b>\n\n'
            '<b>Статус:</b>\n'
            '- Онлайн: {status_emoji} {online_status}\n'
            '- Xray: {xray_emoji} {xray_status}\n'
            '- Пользователей онлайн: {users_online}\n'
            '- Изменение статуса: {status_change}\n'
            '- Сообщение: {last_status_message}\n'
            '- Uptime Xray: {xray_uptime}\n\n'
            '<b>Трафик:</b>\n'
            '- Использовано: {traffic_used}\n'
            '- Лимит: {traffic_limit}\n'
            '- Трекинг: {tracking_status}\n'
            '- День сброса: {traffic_reset_day}\n'
            '- Уведомления: {notify_percent}\n'
            '- Множитель: {consumption_multiplier}\n\n'
            '⚠️ <b>Детальная статистика временно недоступна</b>\n'
            'Возможные причины:\n'
            '• Проблемы с подключением к API\n'
            '• Нода недавно добавлена\n'
            '• Недостаточно данных для отображения\n\n'
            '<b>Обновлено:</b> {updated_at}\n',
        ).format(
            name=node['name'],
            status_emoji=status_emoji,
            online_status=yes_text if node['is_node_online'] else no_text,
            xray_emoji=xray_emoji,
            xray_status=(
                texts.t('ADMIN_MAINTENANCE_MONITORING_ACTIVE', 'Запущен')
                if node['is_xray_running']
                else texts.t('ADMIN_MAINTENANCE_MONITORING_INACTIVE', 'Остановлен')
            ),
            users_online=node['users_online'] or 0,
            status_change=format_datetime(node.get('last_status_change')) if node.get('last_status_change') else dash,
            last_status_message=node.get('last_status_message') or dash,
            xray_uptime=node.get('xray_uptime') or dash,
            traffic_used=format_bytes(node['traffic_used_bytes'] or 0),
            traffic_limit=(
                format_bytes(node['traffic_limit_bytes'])
                if node['traffic_limit_bytes']
                else texts.t('ADMIN_RW_NO_LIMIT', 'Без лимита')
            ),
            tracking_status=(
                texts.t('ADMIN_USER_STATUS_ACTIVE', '✅ Активен')
                if node.get('is_traffic_tracking_active')
                else texts.t('ADMIN_RW_TRACKING_DISABLED', '❌ Отключен')
            ),
            traffic_reset_day=node.get('traffic_reset_day') or dash,
            notify_percent=node.get('notify_percent') or dash,
            consumption_multiplier=node.get('consumption_multiplier') or 1,
            updated_at=format_datetime('now'),
        )

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_SYNC_RETRY', '🔄 Попробовать снова'),
                        callback_data=f'node_stats_{node_uuid}',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_node_manage_{node_uuid}')],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()


@admin_required
@error_handler
async def show_squad_details(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    squad_uuid = callback.data.split('_')[-1]
    texts = get_texts(db_user.language)

    remnawave_service = RemnaWaveService()
    squad = await remnawave_service.get_squad_details(squad_uuid)

    if not squad:
        await callback.answer(
            texts.t('ADMIN_SQUAD_NOT_FOUND', '❌ Сквад не найден'),
            show_alert=True,
        )
        return

    text = texts.t(
        'ADMIN_SQUAD_DETAILS_TEMPLATE',
        '🌐 <b>Сквад: {name}</b>\n\n'
        '<b>Информация:</b>\n'
        '- UUID: <code>{uuid}</code>\n'
        '- Участников: {members_count}\n'
        '- Инбаундов: {inbounds_count}\n\n'
        '<b>Инбаунды:</b>\n',
    ).format(
        name=squad['name'],
        uuid=squad['uuid'],
        members_count=squad['members_count'],
        inbounds_count=squad['inbounds_count'],
    )

    if squad.get('inbounds'):
        for inbound in squad['inbounds']:
            text += f'- {inbound["tag"]} ({inbound["type"]})\n'
    else:
        text += texts.t('ADMIN_SQUAD_NO_ACTIVE_INBOUNDS', 'Нет активных инбаундов')

    await callback.message.edit_text(text, reply_markup=get_squad_management_keyboard(squad_uuid, db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def manage_squad_action(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    action = parts[1]
    squad_uuid = parts[-1]
    texts = get_texts(db_user.language)

    remnawave_service = RemnaWaveService()

    if action == 'add_users':
        success = await remnawave_service.add_all_users_to_squad(squad_uuid)
        if success:
            await callback.answer(
                texts.t('ADMIN_SQUAD_ADD_USERS_QUEUED', '✅ Задача добавления пользователей в очередь')
            )
        else:
            await callback.answer(
                texts.t('ADMIN_SQUAD_ADD_USERS_ERROR', '❌ Ошибка добавления пользователей'),
                show_alert=True,
            )

    elif action == 'remove_users':
        success = await remnawave_service.remove_all_users_from_squad(squad_uuid)
        if success:
            await callback.answer(
                texts.t('ADMIN_SQUAD_REMOVE_USERS_QUEUED', '✅ Задача удаления пользователей в очередь')
            )
        else:
            await callback.answer(
                texts.t('ADMIN_SQUAD_REMOVE_USERS_ERROR', '❌ Ошибка удаления пользователей'),
                show_alert=True,
            )

    elif action == 'delete':
        success = await remnawave_service.delete_squad(squad_uuid)
        if success:
            await callback.message.edit_text(
                texts.t('ADMIN_SQUAD_DELETE_SUCCESS', '✅ Сквад успешно удален'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_SQUAD_BACK_TO_LIST', '⬅️ К сквадам'),
                                callback_data='admin_rw_squads',
                            )
                        ]
                    ]
                ),
            )
        else:
            await callback.answer(
                texts.t('ADMIN_SQUAD_DELETE_ERROR', '❌ Ошибка удаления сквада'),
                show_alert=True,
            )
        return

    refreshed_callback = callback.model_copy(update={'data': f'admin_squad_manage_{squad_uuid}'}).as_(callback.bot)

    await show_squad_details(refreshed_callback, db_user, db)


@admin_required
@error_handler
async def show_squad_edit_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    squad_uuid = callback.data.split('_')[-1]
    texts = get_texts(db_user.language)

    remnawave_service = RemnaWaveService()
    squad = await remnawave_service.get_squad_details(squad_uuid)

    if not squad:
        await callback.answer(
            texts.t('ADMIN_SQUAD_NOT_FOUND', '❌ Сквад не найден'),
            show_alert=True,
        )
        return

    text = texts.t(
        'ADMIN_SQUAD_EDIT_TEMPLATE',
        '✏️ <b>Редактирование сквада: {name}</b>\n\n'
        '<b>Текущие инбаунды:</b>\n',
    ).format(name=squad['name'])

    if squad.get('inbounds'):
        for inbound in squad['inbounds']:
            text += f'✅ {inbound["tag"]} ({inbound["type"]})\n'
    else:
        text += texts.t('ADMIN_SQUAD_NO_ACTIVE_INBOUNDS', 'Нет активных инбаундов') + '\n'

    text += '\n' + texts.t('ADMIN_SQUAD_AVAILABLE_ACTIONS', '<b>Доступные действия:</b>')

    await callback.message.edit_text(text, reply_markup=get_squad_edit_keyboard(squad_uuid, db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_squad_inbounds_selection(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    squad_uuid = callback.data.split('_')[-1]
    texts = get_texts(db_user.language)

    remnawave_service = RemnaWaveService()

    squad = await remnawave_service.get_squad_details(squad_uuid)
    all_inbounds = await remnawave_service.get_all_inbounds()

    if not squad:
        await callback.answer(
            texts.t('ADMIN_SQUAD_NOT_FOUND', '❌ Сквад не найден'),
            show_alert=True,
        )
        return

    if not all_inbounds:
        await callback.answer(
            texts.t('ADMIN_SQUAD_NO_AVAILABLE_INBOUNDS', '❌ Нет доступных инбаундов'),
            show_alert=True,
        )
        return

    if squad_uuid not in squad_inbound_selections:
        squad_inbound_selections[squad_uuid] = {inbound['uuid'] for inbound in squad.get('inbounds', [])}

    text = texts.t(
        'ADMIN_SQUAD_INBOUNDS_EDIT_TEMPLATE',
        '🔧 <b>Изменение инбаундов</b>\n\n'
        '<b>Сквад:</b> {name}\n'
        '<b>Текущих инбаундов:</b> {count}\n\n'
        '<b>Доступные инбаунды:</b>\n',
    ).format(name=squad['name'], count=len(squad_inbound_selections[squad_uuid]))

    keyboard = []

    for i, inbound in enumerate(all_inbounds[:15]):
        is_selected = inbound['uuid'] in squad_inbound_selections[squad_uuid]
        emoji = '✅' if is_selected else '☐'

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {inbound["tag"]} ({inbound["type"]})', callback_data=f'sqd_tgl_{i}_{squad_uuid[:8]}'
                )
            ]
        )

    if len(all_inbounds) > 15:
        text += '\n' + texts.t(
            'ADMIN_SQUAD_INBOUNDS_FIRST_15',
            '⚠️ Показано первые 15 из {count} инбаундов',
        ).format(count=len(all_inbounds))

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SQUAD_SAVE_CHANGES', '💾 Сохранить изменения'),
                    callback_data=f'sqd_save_{squad_uuid[:8]}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=f'sqd_edit_{squad_uuid[:8]}')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_squad_rename_form(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    squad_uuid = callback.data.split('_')[-1]
    texts = get_texts(db_user.language)

    remnawave_service = RemnaWaveService()
    squad = await remnawave_service.get_squad_details(squad_uuid)

    if not squad:
        await callback.answer(
            texts.t('ADMIN_SQUAD_NOT_FOUND', '❌ Сквад не найден'),
            show_alert=True,
        )
        return

    await state.update_data(squad_uuid=squad_uuid, squad_name=squad['name'])
    await state.set_state(SquadRenameStates.waiting_for_new_name)

    text = texts.t(
        'ADMIN_SQUAD_RENAME_FORM_TEMPLATE',
        '✏️ <b>Переименование сквада</b>\n\n'
        '<b>Текущее название:</b> {name}\n\n'
        '📝 <b>Введите новое название сквада:</b>\n\n'
        '<i>Требования к названию:</i>\n'
        '• От 2 до 20 символов\n'
        '• Только буквы, цифры, дефисы и подчеркивания\n'
        '• Без пробелов и специальных символов\n\n'
        'Отправьте сообщение с новым названием или нажмите "Отмена" для выхода.',
    ).format(name=squad['name'])

    keyboard = [[types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'cancel_rename_{squad_uuid}')]]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def cancel_squad_rename(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    squad_uuid = callback.data.split('_')[-1]

    await state.clear()

    refreshed_callback = callback.model_copy(update={'data': f'squad_edit_{squad_uuid}'}).as_(callback.bot)

    await show_squad_edit_menu(refreshed_callback, db_user, db)


@admin_required
@error_handler
async def process_squad_new_name(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    squad_uuid = data.get('squad_uuid')
    old_name = data.get('squad_name')

    if not squad_uuid:
        await message.answer(texts.t('ADMIN_SQUAD_SESSION_NOT_FOUND', '❌ Ошибка: сквад не найден'))
        await state.clear()
        return

    new_name = message.text.strip()

    if not new_name:
        await message.answer(texts.t('ADMIN_SQUAD_NAME_EMPTY', '❌ Название не может быть пустым. Попробуйте еще раз:'))
        return

    if len(new_name) < 2 or len(new_name) > 20:
        await message.answer(
            texts.t(
                'ADMIN_SQUAD_NAME_INVALID_LENGTH',
                '❌ Название должно быть от 2 до 20 символов. Попробуйте еще раз:',
            )
        )
        return

    import re

    if not re.match(r'^[A-Za-z0-9_-]+$', new_name):
        await message.answer(
            texts.t(
                'ADMIN_SQUAD_NAME_INVALID_CHARS',
                '❌ Название может содержать только буквы, цифры, дефисы и подчеркивания. Попробуйте еще раз:',
            )
        )
        return

    if new_name == old_name:
        await message.answer(
            texts.t(
                'ADMIN_SQUAD_NAME_SAME',
                '❌ Новое название совпадает с текущим. Введите другое название:',
            )
        )
        return

    remnawave_service = RemnaWaveService()
    success = await remnawave_service.rename_squad(squad_uuid, new_name)

    if success:
        await message.answer(
            texts.t(
                'ADMIN_SQUAD_RENAME_SUCCESS_TEMPLATE',
                '✅ <b>Сквад успешно переименован!</b>\n\n'
                '<b>Старое название:</b> {old_name}\n'
                '<b>Новое название:</b> {new_name}',
            ).format(old_name=old_name, new_name=new_name),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SQUAD_DETAILS_BUTTON', '📋 Детали сквада'),
                            callback_data=f'admin_squad_manage_{squad_uuid}',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SQUAD_BACK_TO_LIST', '⬅️ К сквадам'),
                            callback_data='admin_rw_squads',
                        )
                    ],
                ]
            ),
        )
        await state.clear()
    else:
        await message.answer(
            texts.t(
                'ADMIN_SQUAD_RENAME_ERROR_TEMPLATE',
                '❌ <b>Ошибка переименования сквада</b>\n\n'
                'Возможные причины:\n'
                '• Сквад с таким названием уже существует\n'
                '• Проблемы с подключением к API\n'
                '• Недостаточно прав\n\n'
                'Попробуйте другое название:',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'cancel_rename_{squad_uuid}')]
                ]
            ),
        )


@admin_required
@error_handler
async def toggle_squad_inbound(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    inbound_index = int(parts[2])
    short_squad_uuid = parts[3]

    remnawave_service = RemnaWaveService()
    squads = await remnawave_service.get_all_squads()

    full_squad_uuid = None
    for squad in squads:
        if squad['uuid'].startswith(short_squad_uuid):
            full_squad_uuid = squad['uuid']
            break

    if not full_squad_uuid:
        await callback.answer(
            texts.t('ADMIN_SQUAD_NOT_FOUND', '❌ Сквад не найден'),
            show_alert=True,
        )
        return

    all_inbounds = await remnawave_service.get_all_inbounds()
    if inbound_index >= len(all_inbounds):
        await callback.answer(
            texts.t('ADMIN_SQUAD_INBOUND_NOT_FOUND', '❌ Инбаунд не найден'),
            show_alert=True,
        )
        return

    selected_inbound = all_inbounds[inbound_index]

    if full_squad_uuid not in squad_inbound_selections:
        squad_inbound_selections[full_squad_uuid] = set()

    if selected_inbound['uuid'] in squad_inbound_selections[full_squad_uuid]:
        squad_inbound_selections[full_squad_uuid].remove(selected_inbound['uuid'])
        await callback.answer(
            texts.t('ADMIN_SQUAD_INBOUND_REMOVED', '➖ Убран: {tag}').format(tag=selected_inbound["tag"])
        )
    else:
        squad_inbound_selections[full_squad_uuid].add(selected_inbound['uuid'])
        await callback.answer(
            texts.t('ADMIN_SQUAD_INBOUND_ADDED', '➕ Добавлен: {tag}').format(tag=selected_inbound["tag"])
        )

    current_squad = next((s for s in squads if s['uuid'] == full_squad_uuid), None)
    current_squad_name = current_squad['name'] if current_squad else texts.t('ADMIN_USER_LAST_ACTIVITY_UNKNOWN', 'Неизвестно')

    text = texts.t(
        'ADMIN_SQUAD_INBOUNDS_SELECTED_TEMPLATE',
        '🔧 <b>Изменение инбаундов</b>\n\n'
        '<b>Сквад:</b> {name}\n'
        '<b>Выбрано инбаундов:</b> {count}\n\n'
        '<b>Доступные инбаунды:</b>\n',
    ).format(
        name=current_squad_name,
        count=len(squad_inbound_selections[full_squad_uuid]),
    )

    keyboard = []
    for i, inbound in enumerate(all_inbounds[:15]):
        is_selected = inbound['uuid'] in squad_inbound_selections[full_squad_uuid]
        emoji = '✅' if is_selected else '☐'

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {inbound["tag"]} ({inbound["type"]})',
                    callback_data=f'sqd_tgl_{i}_{short_squad_uuid}',
                )
            ]
        )

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SQUAD_SAVE_CHANGES', '💾 Сохранить изменения'),
                    callback_data=f'sqd_save_{short_squad_uuid}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=f'sqd_edit_{short_squad_uuid}')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))


@admin_required
@error_handler
async def save_squad_inbounds(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    short_squad_uuid = callback.data.split('_')[-1]
    texts = get_texts(db_user.language)

    remnawave_service = RemnaWaveService()
    squads = await remnawave_service.get_all_squads()

    full_squad_uuid = None
    squad_name = None
    for squad in squads:
        if squad['uuid'].startswith(short_squad_uuid):
            full_squad_uuid = squad['uuid']
            squad_name = squad['name']
            break

    if not full_squad_uuid:
        await callback.answer(
            texts.t('ADMIN_SQUAD_NOT_FOUND', '❌ Сквад не найден'),
            show_alert=True,
        )
        return

    selected_inbounds = squad_inbound_selections.get(full_squad_uuid, set())

    try:
        success = await remnawave_service.update_squad_inbounds(full_squad_uuid, list(selected_inbounds))

        if success:
            squad_inbound_selections.pop(full_squad_uuid, None)

            await callback.message.edit_text(
                texts.t(
                    'ADMIN_SQUAD_INBOUNDS_UPDATED_TEMPLATE',
                    '✅ <b>Инбаунды сквада обновлены</b>\n\n'
                    '<b>Сквад:</b> {name}\n'
                    '<b>Количество инбаундов:</b> {count}',
                ).format(name=squad_name, count=len(selected_inbounds)),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_SQUAD_BACK_TO_LIST', '⬅️ К сквадам'),
                                callback_data='admin_rw_squads',
                            )
                        ],
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_SQUAD_DETAILS_BUTTON', '📋 Детали сквада'),
                                callback_data=f'admin_squad_manage_{full_squad_uuid}',
                            )
                        ],
                    ]
                ),
            )
            await callback.answer(texts.t('ADMIN_SQUAD_CHANGES_SAVED', '✅ Изменения сохранены!'))
        else:
            await callback.answer(
                texts.t('ADMIN_SQUAD_CHANGES_SAVE_ERROR', '❌ Ошибка сохранения изменений'),
                show_alert=True,
            )

    except Exception as e:
        logger.error('Error saving squad inbounds', error=e)
        await callback.answer(
            texts.t('ADMIN_SQUAD_SAVE_ERROR', '❌ Ошибка при сохранении'),
            show_alert=True,
        )


@admin_required
@error_handler
async def show_squad_edit_menu_short(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    short_squad_uuid = callback.data.split('_')[-1]
    texts = get_texts(db_user.language)

    remnawave_service = RemnaWaveService()
    squads = await remnawave_service.get_all_squads()

    full_squad_uuid = None
    for squad in squads:
        if squad['uuid'].startswith(short_squad_uuid):
            full_squad_uuid = squad['uuid']
            break

    if not full_squad_uuid:
        await callback.answer(
            texts.t('ADMIN_SQUAD_NOT_FOUND', '❌ Сквад не найден'),
            show_alert=True,
        )
        return

    refreshed_callback = callback.model_copy(update={'data': f'squad_edit_{full_squad_uuid}'}).as_(callback.bot)

    await show_squad_edit_menu(refreshed_callback, db_user, db)


@admin_required
@error_handler
async def start_squad_creation(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await state.set_state(SquadCreateStates.waiting_for_name)

    text = texts.t(
        'ADMIN_SQUAD_CREATE_STEP1_TEMPLATE',
        '➕ <b>Создание нового сквада</b>\n\n'
        '<b>Шаг 1 из 2: Название сквада</b>\n\n'
        '📝 <b>Введите название для нового сквада:</b>\n\n'
        '<i>Требования к названию:</i>\n'
        '• От 2 до 20 символов\n'
        '• Только буквы, цифры, дефисы и подчеркивания\n'
        '• Без пробелов и специальных символов\n\n'
        'Отправьте сообщение с названием или нажмите "Отмена" для выхода.',
    )

    keyboard = [[types.InlineKeyboardButton(text=texts.CANCEL, callback_data='cancel_squad_create')]]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def process_squad_name(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    squad_name = message.text.strip()

    if not squad_name:
        await message.answer(texts.t('ADMIN_SQUAD_NAME_EMPTY', '❌ Название не может быть пустым. Попробуйте еще раз:'))
        return

    if len(squad_name) < 2 or len(squad_name) > 20:
        await message.answer(
            texts.t(
                'ADMIN_SQUAD_NAME_INVALID_LENGTH',
                '❌ Название должно быть от 2 до 20 символов. Попробуйте еще раз:',
            )
        )
        return

    import re

    if not re.match(r'^[A-Za-z0-9_-]+$', squad_name):
        await message.answer(
            texts.t(
                'ADMIN_SQUAD_NAME_INVALID_CHARS',
                '❌ Название может содержать только буквы, цифры, дефисы и подчеркивания. Попробуйте еще раз:',
            )
        )
        return

    await state.update_data(squad_name=squad_name)
    await state.set_state(SquadCreateStates.selecting_inbounds)

    user_id = message.from_user.id
    squad_create_data[user_id] = {'name': squad_name, 'selected_inbounds': set()}

    remnawave_service = RemnaWaveService()
    all_inbounds = await remnawave_service.get_all_inbounds()

    if not all_inbounds:
        await message.answer(
            texts.t(
                'ADMIN_SQUAD_CREATE_NO_INBOUNDS',
                '❌ <b>Нет доступных инбаундов</b>\n\nДля создания сквада необходимо иметь хотя бы один инбаунд.',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SQUAD_BACK_TO_LIST', '⬅️ К сквадам'),
                            callback_data='admin_rw_squads',
                        )
                    ]
                ]
            ),
        )
        await state.clear()
        return

    text = texts.t(
        'ADMIN_SQUAD_CREATE_STEP2_TEMPLATE',
        '➕ <b>Создание сквада: {name}</b>\n\n'
        '<b>Шаг 2 из 2: Выбор инбаундов</b>\n\n'
        '<b>Выбрано инбаундов:</b> 0\n\n'
        '<b>Доступные инбаунды:</b>\n',
    ).format(name=squad_name)

    keyboard = []

    for i, inbound in enumerate(all_inbounds[:15]):
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'☐ {inbound["tag"]} ({inbound["type"]})', callback_data=f'create_tgl_{i}'
                )
            ]
        )

    if len(all_inbounds) > 15:
        text += '\n' + texts.t(
            'ADMIN_SQUAD_INBOUNDS_FIRST_15',
            '⚠️ Показано первые 15 из {count} инбаундов',
        ).format(count=len(all_inbounds))

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SQUAD_CREATE_BUTTON', '✅ Создать сквад'),
                    callback_data='create_squad_finish',
                )
            ],
            [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='cancel_squad_create')],
        ]
    )

    await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))


@admin_required
@error_handler
async def toggle_create_inbound(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    inbound_index = int(callback.data.split('_')[-1])
    user_id = callback.from_user.id

    if user_id not in squad_create_data:
        await callback.answer(
            texts.t('ADMIN_SQUAD_SESSION_DATA_NOT_FOUND', '❌ Ошибка: данные сессии не найдены'),
            show_alert=True,
        )
        await state.clear()
        return

    remnawave_service = RemnaWaveService()
    all_inbounds = await remnawave_service.get_all_inbounds()

    if inbound_index >= len(all_inbounds):
        await callback.answer(
            texts.t('ADMIN_SQUAD_INBOUND_NOT_FOUND', '❌ Инбаунд не найден'),
            show_alert=True,
        )
        return

    selected_inbound = all_inbounds[inbound_index]
    selected_inbounds = squad_create_data[user_id]['selected_inbounds']

    if selected_inbound['uuid'] in selected_inbounds:
        selected_inbounds.remove(selected_inbound['uuid'])
        await callback.answer(
            texts.t('ADMIN_SQUAD_INBOUND_REMOVED', '➖ Убран: {tag}').format(tag=selected_inbound["tag"])
        )
    else:
        selected_inbounds.add(selected_inbound['uuid'])
        await callback.answer(
            texts.t('ADMIN_SQUAD_INBOUND_ADDED', '➕ Добавлен: {tag}').format(tag=selected_inbound["tag"])
        )

    squad_name = squad_create_data[user_id]['name']

    text = texts.t(
        'ADMIN_SQUAD_CREATE_STEP2_SELECTED_TEMPLATE',
        '➕ <b>Создание сквада: {name}</b>\n\n'
        '<b>Шаг 2 из 2: Выбор инбаундов</b>\n\n'
        '<b>Выбрано инбаундов:</b> {count}\n\n'
        '<b>Доступные инбаунды:</b>\n',
    ).format(name=squad_name, count=len(selected_inbounds))

    keyboard = []

    for i, inbound in enumerate(all_inbounds[:15]):
        is_selected = inbound['uuid'] in selected_inbounds
        emoji = '✅' if is_selected else '☐'

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{emoji} {inbound["tag"]} ({inbound["type"]})', callback_data=f'create_tgl_{i}'
                )
            ]
        )

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SQUAD_CREATE_BUTTON', '✅ Создать сквад'),
                    callback_data='create_squad_finish',
                )
            ],
            [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='cancel_squad_create')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))


@admin_required
@error_handler
async def finish_squad_creation(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    user_id = callback.from_user.id
    texts = get_texts(db_user.language)

    if user_id not in squad_create_data:
        await callback.answer(
            texts.t('ADMIN_SQUAD_SESSION_DATA_NOT_FOUND', '❌ Ошибка: данные сессии не найдены'),
            show_alert=True,
        )
        await state.clear()
        return

    squad_name = squad_create_data[user_id]['name']
    selected_inbounds = list(squad_create_data[user_id]['selected_inbounds'])

    if not selected_inbounds:
        await callback.answer(
            texts.t('ADMIN_SQUAD_SELECT_AT_LEAST_ONE_INBOUND', '❌ Необходимо выбрать хотя бы один инбаунд'),
            show_alert=True,
        )
        return

    remnawave_service = RemnaWaveService()
    success = await remnawave_service.create_squad(squad_name, selected_inbounds)

    squad_create_data.pop(user_id, None)
    await state.clear()

    if success:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_SQUAD_CREATE_SUCCESS_TEMPLATE',
                '✅ <b>Сквад успешно создан!</b>\n\n'
                '<b>Название:</b> {name}\n'
                '<b>Количество инбаундов:</b> {count}\n\n'
                'Сквад готов к использованию!',
            ).format(name=squad_name, count=len(selected_inbounds)),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SQUAD_LIST_BUTTON', '📋 Список сквадов'),
                            callback_data='admin_rw_squads',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SQUAD_BACK_TO_PANEL', '⬅️ К панели Remnawave'),
                            callback_data='admin_remnawave',
                        )
                    ],
                ]
            ),
        )
        await callback.answer(texts.t('ADMIN_SQUAD_CREATED_ALERT', '✅ Сквад создан!'))
    else:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_SQUAD_CREATE_ERROR_TEMPLATE',
                '❌ <b>Ошибка создания сквада</b>\n\n'
                '<b>Название:</b> {name}\n\n'
                'Возможные причины:\n'
                '• Сквад с таким названием уже существует\n'
                '• Проблемы с подключением к API\n'
                '• Недостаточно прав\n'
                '• Некорректные инбаунды',
            ).format(name=squad_name),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_REFERRALS_LOG_ANALYSIS_RETRY', '🔄 Попробовать снова'),
                            callback_data='admin_squad_create',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_SQUAD_BACK_TO_LIST', '⬅️ К сквадам'),
                            callback_data='admin_rw_squads',
                        )
                    ],
                ]
            ),
        )
        await callback.answer(
            texts.t('ADMIN_SQUAD_CREATE_ERROR', '❌ Ошибка создания сквада'),
            show_alert=True,
        )


@admin_required
@error_handler
async def cancel_squad_creation(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    user_id = callback.from_user.id

    squad_create_data.pop(user_id, None)
    await state.clear()

    await show_squads_management(callback, db_user, db)


@admin_required
@error_handler
async def restart_all_nodes(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    remnawave_service = RemnaWaveService()
    success = await remnawave_service.restart_all_nodes()

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_RW_RESTART_ALL_SUCCESS', '✅ Команда перезагрузки всех нод отправлена'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RW_BACK_TO_NODES', '⬅️ К нодам'),
                            callback_data='admin_rw_nodes',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_RW_RESTART_ALL_ERROR', '❌ Ошибка перезагрузки нод'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RW_BACK_TO_NODES', '⬅️ К нодам'),
                            callback_data='admin_rw_nodes',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def show_sync_options(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    status = remnawave_sync_service.get_status()
    dash = texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')
    times_text = ', '.join(t.strftime('%H:%M') for t in status.times) if status.times else dash
    next_run_text = format_datetime(status.next_run) if status.next_run else dash
    last_result = dash

    if status.last_run_finished_at:
        result_icon = '✅' if status.last_run_success else '❌'
        result_label = (
            texts.t('ADMIN_RW_AUTO_SYNC_RESULT_SUCCESS', 'успешно')
            if status.last_run_success
            else texts.t('ADMIN_RW_AUTO_SYNC_RESULT_WITH_ERRORS', 'с ошибками')
        )
        finished_text = format_datetime(status.last_run_finished_at)
        last_result = f'{result_icon} {result_label} ({finished_text})'
    elif status.last_run_started_at:
        last_result = texts.t('ADMIN_RW_LAST_RUN_STARTED', '⏳ Запущено {started_at}').format(
            started_at=format_datetime(status.last_run_started_at)
        )

    status_lines = [
        texts.t(
            'ADMIN_RW_SYNC_OPTIONS_STATUS_LINE',
            '⚙️ Статус: {status}',
        ).format(
            status=(
                texts.t('ADMIN_BLACKLIST_STATUS_ENABLED', '✅ Включена')
                if status.enabled
                else texts.t('ADMIN_BLACKLIST_STATUS_DISABLED', '❌ Отключена')
            )
        ),
        texts.t('ADMIN_RW_SYNC_OPTIONS_SCHEDULE_LINE', '🕒 Расписание: {times}').format(times=times_text),
        texts.t('ADMIN_RW_SYNC_OPTIONS_NEXT_RUN_LINE', '📅 Следующий запуск: {next_run}').format(
            next_run=next_run_text if status.enabled else dash
        ),
        texts.t('ADMIN_RW_SYNC_OPTIONS_LAST_RUN_LINE', '📊 Последний запуск: {last_result}').format(
            last_result=last_result
        ),
    ]

    text = (
        texts.t(
            'ADMIN_RW_SYNC_OPTIONS_TEXT',
            '🔄 <b>Синхронизация с Remnawave</b>\n\n'
            '🔄 <b>Полная синхронизация выполняет:</b>\n'
            '• Создание новых пользователей из панели в боте\n'
            '• Обновление данных существующих пользователей\n'
            '• Деактивация подписок пользователей, отсутствующих в панели\n'
            '• Сохранение балансов пользователей\n'
            '• ⏱️ Время выполнения: 2-5 минут\n\n'
            '⚠️ <b>Важно:</b>\n'
            '• Во время синхронизации не выполняйте другие операции\n'
            '• При полной синхронизации подписки пользователей, отсутствующих в панели, будут деактивированы\n'
            '• Рекомендуется делать полную синхронизацию ежедневно\n'
            '• Баланс пользователей НЕ удаляется\n\n'
            '⬆️ <b>Обратная синхронизация:</b>\n'
            '• Отправляет активных пользователей из бота в панель\n'
            '• Используйте при сбоях панели или для восстановления данных',
        )
        + '\n\n'
        + '\n'.join(status_lines)
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RW_SYNC_OPTIONS_RUN_FULL', '🔄 Запустить полную синхронизацию'),
                callback_data='sync_all_users',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SYNC_TO_PANEL', '⬆️ Синхронизация в панель'),
                callback_data='sync_to_panel',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RW_SYNC_OPTIONS_AUTO_SETTINGS', '⚙️ Настройки автосинхронизации'),
                callback_data='admin_rw_auto_sync',
            )
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
    ]

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_auto_sync_settings(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await state.clear()
    texts = get_texts(db_user.language)
    status = remnawave_sync_service.get_status()
    text, keyboard = _build_auto_sync_view(status, texts)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_auto_sync_setting(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await state.clear()
    texts = get_texts(db_user.language)
    new_value = not bool(settings.REMNAWAVE_AUTO_SYNC_ENABLED)
    await bot_configuration_service.set_value(
        db,
        'REMNAWAVE_AUTO_SYNC_ENABLED',
        new_value,
    )
    await db.commit()

    status = remnawave_sync_service.get_status()
    text, keyboard = _build_auto_sync_view(status, texts)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer(
        texts.t(
            'ADMIN_RW_AUTO_SYNC_TOGGLED',
            'Автосинхронизация {status}',
        ).format(
            status=(
                texts.t('ADMIN_BLACKLIST_STATUS_ENABLED_WORD', 'включена')
                if new_value
                else texts.t('ADMIN_BLACKLIST_STATUS_DISABLED_WORD', 'отключена')
            )
        )
    )


@admin_required
@error_handler
async def prompt_auto_sync_schedule(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    status = remnawave_sync_service.get_status()
    current_schedule = (
        ', '.join(t.strftime('%H:%M') for t in status.times) if status.times else texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')
    )

    instructions = (
        texts.t(
            'ADMIN_RW_AUTO_SYNC_SCHEDULE_INSTRUCTIONS',
            '🕒 <b>Настройка расписания автосинхронизации</b>\n\n'
            'Укажите время запуска через запятую или с новой строки в формате HH:MM.\n'
            'Текущее расписание: <code>{current_schedule}</code>\n\n'
            'Примеры: <code>03:00, 15:30</code> или <code>00:15\n06:00\n18:45</code>\n\n'
            'Отправьте <b>отмена</b>, чтобы вернуться без изменений.',
        ).format(current_schedule=current_schedule)
    )

    await state.set_state(RemnaWaveSyncStates.waiting_for_schedule)
    await state.update_data(
        auto_sync_message_id=callback.message.message_id,
        auto_sync_message_chat_id=callback.message.chat.id,
    )

    await callback.message.edit_text(
        instructions,
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.CANCEL,
                        callback_data='remnawave_auto_sync_cancel',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def cancel_auto_sync_schedule(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await state.clear()
    texts = get_texts(db_user.language)
    status = remnawave_sync_service.get_status()
    text, keyboard = _build_auto_sync_view(status, texts)

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer(texts.t('ADMIN_RW_AUTO_SYNC_SCHEDULE_CANCELLED', 'Изменение расписания отменено'))


@admin_required
@error_handler
async def run_auto_sync_now(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    if remnawave_sync_service.get_status().is_running:
        await callback.answer(
            texts.t('ADMIN_RW_SYNC_ALREADY_RUNNING', 'Синхронизация уже выполняется'),
            show_alert=True,
        )
        return

    await state.clear()
    await callback.message.edit_text(
        texts.t(
            'ADMIN_RW_AUTO_SYNC_STARTING',
            '🔄 Запуск автосинхронизации...\n\nПодождите, это может занять несколько минут.',
        ),
        parse_mode='HTML',
    )
    await callback.answer(texts.t('ADMIN_RW_AUTO_SYNC_STARTED', 'Автосинхронизация запущена'))

    result = await remnawave_sync_service.run_sync_now(reason='manual')
    status = remnawave_sync_service.get_status()
    base_text, keyboard = _build_auto_sync_view(status, texts)

    if not result.get('started'):
        await callback.message.edit_text(
            texts.t('ADMIN_RW_SYNC_ALREADY_RUNNING_TITLE', '⚠️ <b>Синхронизация уже выполняется</b>\n\n') + base_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
        return

    if result.get('success'):
        user_stats = result.get('user_stats') or {}
        server_stats = result.get('server_stats') or {}
        summary = (
            texts.t(
                'ADMIN_RW_AUTO_SYNC_SUCCESS_SUMMARY',
                '✅ <b>Синхронизация завершена</b>\n'
                '👥 Пользователи: создано {users_created}, обновлено {users_updated}, '
                'деактивировано {users_deleted}, ошибок {users_errors}\n'
                '🌐 Серверы: создано {servers_created}, обновлено {servers_updated}, удалено {servers_removed}\n\n',
            ).format(
                users_created=user_stats.get("created", 0),
                users_updated=user_stats.get("updated", 0),
                users_deleted=user_stats.get("deleted", user_stats.get("deactivated", 0)),
                users_errors=user_stats.get("errors", 0),
                servers_created=server_stats.get("created", 0),
                servers_updated=server_stats.get("updated", 0),
                servers_removed=server_stats.get("removed", 0),
            )
        )
        final_text = summary + base_text
        await callback.message.edit_text(
            final_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    else:
        error_text = result.get('error') or texts.t('ADMIN_MAINTENANCE_UNKNOWN_ERROR', 'Неизвестная ошибка')
        summary = texts.t(
            'ADMIN_RW_AUTO_SYNC_ERROR_SUMMARY',
            '❌ <b>Синхронизация завершилась с ошибкой</b>\nПричина: {error}\n\n',
        ).format(error=error_text)
        await callback.message.edit_text(
            summary + base_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )


@admin_required
@error_handler
async def save_auto_sync_schedule(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    text = (message.text or '').strip()
    data = await state.get_data()

    if text.lower() in {'отмена', 'cancel'}:
        await state.clear()
        status = remnawave_sync_service.get_status()
        view_text, keyboard = _build_auto_sync_view(status, texts)
        message_id = data.get('auto_sync_message_id')
        chat_id = data.get('auto_sync_message_chat_id', message.chat.id)
        if message_id:
            await message.bot.edit_message_text(
                view_text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        else:
            await message.answer(
                view_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        await message.answer(texts.t('ADMIN_RW_AUTO_SYNC_SCHEDULE_SETUP_CANCELLED', 'Настройка расписания отменена'))
        return

    parsed_times = settings.parse_daily_time_list(text)

    if not parsed_times:
        await message.answer(
            texts.t(
                'ADMIN_RW_AUTO_SYNC_TIME_PARSE_ERROR',
                '❌ Не удалось распознать время. Используйте формат HH:MM, например 03:00 или 18:45.',
            ),
        )
        return

    normalized_value = ', '.join(t.strftime('%H:%M') for t in parsed_times)
    await bot_configuration_service.set_value(
        db,
        'REMNAWAVE_AUTO_SYNC_TIMES',
        normalized_value,
    )
    await db.commit()

    status = remnawave_sync_service.get_status()
    view_text, keyboard = _build_auto_sync_view(status, texts)
    message_id = data.get('auto_sync_message_id')
    chat_id = data.get('auto_sync_message_chat_id', message.chat.id)

    if message_id:
        await message.bot.edit_message_text(
            view_text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    else:
        await message.answer(
            view_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )

    await state.clear()
    await message.answer(texts.t('ADMIN_RW_AUTO_SYNC_SCHEDULE_UPDATED', '✅ Расписание автосинхронизации обновлено'))


@admin_required
@error_handler
async def sync_all_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Выполняет полную синхронизацию всех пользователей"""
    texts = get_texts(db_user.language)

    progress_text = texts.t(
        'ADMIN_RW_SYNC_ALL_PROGRESS',
        '🔄 <b>Выполняется полная синхронизация...</b>\n\n'
        '📋 Этапы:\n'
        '• Загрузка ВСЕХ пользователей из панели Remnawave\n'
        '• Создание новых пользователей в боте\n'
        '• Обновление существующих пользователей\n'
        '• Деактивация подписок отсутствующих пользователей\n'
        '• Сохранение балансов\n\n'
        '⏳ Пожалуйста, подождите...',
    )

    await callback.message.edit_text(progress_text, reply_markup=None)

    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.sync_users_from_panel(db, 'all')

    total_operations = stats['created'] + stats['updated'] + stats.get('deleted', 0)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_SUCCESS', 'успешно завершена')
    elif stats['errors'] < total_operations:
        status_emoji = '⚠️'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_WARNINGS', 'завершена с предупреждениями')
    else:
        status_emoji = '❌'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_ERRORS', 'завершена с ошибками')

    text = texts.t(
        'ADMIN_RW_SYNC_ALL_RESULT_TEMPLATE',
        '{status_emoji} <b>Полная синхронизация {status_text}</b>\n\n'
        '📊 <b>Результат:</b>\n'
        '• 🆕 Создано: {created}\n'
        '• 🔄 Обновлено: {updated}\n'
        '• 🗑️ Деактивировано: {deleted}\n'
        '• ❌ Ошибок: {errors}\n',
    ).format(
        status_emoji=status_emoji,
        status_text=status_text,
        created=stats['created'],
        updated=stats['updated'],
        deleted=stats.get('deleted', 0),
        errors=stats['errors'],
    )

    if stats.get('deleted', 0) > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_SYNC_ALL_DEACTIVATED_INFO',
            '🗑️ <b>Деактивированные подписки:</b>\n'
            'Деактивированы подписки пользователей, которые\n'
            'отсутствуют в панели Remnawave.\n'
            '💰 Балансы пользователей сохранены.\n',
        )

    if stats['errors'] > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_SYNC_ERRORS_HINT',
            '⚠️ <b>Внимание:</b>\n'
            'Некоторые операции завершились с ошибками.\n'
            'Проверьте логи для получения подробной информации.\n',
        )

    text += '\n' + texts.t(
        'ADMIN_RW_SYNC_ALL_RECOMMENDATIONS',
        '💡 <b>Рекомендации:</b>\n'
        '• Полная синхронизация выполнена\n'
        '• Рекомендуется запускать раз в день\n'
        '• Все пользователи из панели синхронизированы\n',
    )

    keyboard = []

    if stats['errors'] > 0:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_BUTTON_RETRY_SYNC', '🔄 Повторить синхронизацию'),
                    callback_data='sync_all_users',
                )
            ]
        )

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_BUTTON_SYSTEM_STATS', '📊 Статистика системы'),
                    callback_data='admin_rw_system',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_BUTTON_NODES', '🌐 Ноды'),
                    callback_data='admin_rw_nodes',
                ),
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def sync_users_to_panel(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_RW_SYNC_TO_PANEL_PROGRESS',
            '⬆️ Выполняется синхронизация данных бота в панель Remnawave...\n\nЭто может занять несколько минут.',
        ),
        reply_markup=None,
    )

    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.sync_users_to_panel(db)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_SUCCESS', 'успешно завершена')
    else:
        status_emoji = '⚠️' if (stats['created'] + stats['updated']) > 0 else '❌'
        status_text = (
            texts.t('ADMIN_RW_STATUS_COMPLETED_WARNINGS', 'завершена с предупреждениями')
            if status_emoji == '⚠️'
            else texts.t('ADMIN_RW_STATUS_COMPLETED_ERRORS', 'завершена с ошибками')
        )

    text = texts.t(
        'ADMIN_RW_SYNC_TO_PANEL_RESULT_TEMPLATE',
        '{status_emoji} <b>Синхронизация в панель {status_text}</b>\n\n'
        '📊 <b>Результаты:</b>\n'
        '• 🆕 Создано: {created}\n'
        '• 🔄 Обновлено: {updated}\n'
        '• ❌ Ошибок: {errors}',
    ).format(
        status_emoji=status_emoji,
        status_text=status_text,
        created=stats["created"],
        updated=stats["updated"],
        errors=stats["errors"],
    )

    keyboard = [
        [types.InlineKeyboardButton(text=texts.t('ADMIN_SYNC_RETRY', '🔄 Повторить'), callback_data='sync_to_panel')],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SYNC_FULL', '🔄 Полная синхронизация'),
                callback_data='sync_all_users',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SYNC_BACK', '⬅️ К синхронизации'),
                callback_data='admin_rw_sync',
            )
        ],
    ]

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_sync_recommendations(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_RW_SYNC_RECOMMENDATIONS_LOADING', '🔍 Анализируем состояние синхронизации...'),
        reply_markup=None,
    )

    remnawave_service = RemnaWaveService()
    recommendations = await remnawave_service.get_sync_recommendations(db)

    priority_emoji = {'low': '🟢', 'medium': '🟡', 'high': '🔴'}

    text = texts.t(
        'ADMIN_RW_SYNC_RECOMMENDATIONS_TEMPLATE',
        '💡 <b>Рекомендации по синхронизации</b>\n\n'
        '{priority_emoji} <b>Приоритет:</b> {priority}\n'
        '⏱️ <b>Время выполнения:</b> {estimated_time}\n\n'
        '<b>Рекомендуемое действие:</b>\n',
    ).format(
        priority_emoji=priority_emoji.get(recommendations['priority'], '🟢'),
        priority=recommendations['priority'].upper(),
        estimated_time=recommendations['estimated_time'],
    )

    if recommendations['sync_type'] == 'all':
        text += texts.t('ADMIN_SYNC_FULL', '🔄 Полная синхронизация')
    elif recommendations['sync_type'] == 'update_only':
        text += texts.t('ADMIN_RW_RECOMMENDATION_ACTION_UPDATE', '📈 Обновление данных')
    elif recommendations['sync_type'] == 'new_only':
        text += texts.t('ADMIN_RW_RECOMMENDATION_ACTION_NEW', '🆕 Синхронизация новых')
    else:
        text += texts.t('ADMIN_RW_RECOMMENDATION_ACTION_NONE', '✅ Синхронизация не требуется')

    text += '\n\n' + texts.t('ADMIN_RW_RECOMMENDATION_REASONS_TITLE', '<b>Причины:</b>\n')
    for reason in recommendations['reasons']:
        text += f'• {reason}\n'

    keyboard = []

    if recommendations['should_sync'] and recommendations['sync_type'] != 'none':
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_RECOMMENDATION_EXECUTE', '✅ Выполнить рекомендацию'),
                    callback_data=f'sync_{recommendations["sync_type"]}_users'
                    if recommendations['sync_type'] != 'update_only'
                    else 'sync_update_data',
                )
            ]
        )

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_BUTTON_OTHER_OPTIONS', '🔄 Другие опции'),
                    callback_data='admin_rw_sync',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def validate_subscriptions(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_RW_VALIDATE_PROGRESS',
            '🔍 Выполняется валидация подписок...\n\nПроверяем данные, может занять несколько минут.',
        ),
        reply_markup=None,
    )

    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.validate_and_fix_subscriptions(db)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_SUCCESS', 'успешно завершена')
    else:
        status_emoji = '⚠️'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_ERRORS', 'завершена с ошибками')

    text = texts.t(
        'ADMIN_RW_VALIDATE_RESULT_TEMPLATE',
        '{status_emoji} <b>Валидация {status_text}</b>\n\n'
        '📊 <b>Результаты:</b>\n'
        '• 🔍 Проверено подписок: {checked}\n'
        '• 🔧 Исправлено подписок: {fixed}\n'
        '• ⚠️ Найдено проблем: {issues_found}\n'
        '• ❌ Ошибок: {errors}\n',
    ).format(
        status_emoji=status_emoji,
        status_text=status_text,
        checked=stats['checked'],
        fixed=stats['fixed'],
        issues_found=stats['issues_found'],
        errors=stats['errors'],
    )

    if stats['fixed'] > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_VALIDATE_FIXED_DETAILS',
            '✅ <b>Исправленные проблемы:</b>\n'
            '• Статусы просроченных подписок\n'
            '• Отсутствующие данные Remnawave\n'
            '• Некорректные лимиты трафика\n'
            '• Настройки устройств\n',
        )

    if stats['errors'] > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_PROCESSING_ERRORS_HINT',
            '⚠️ Обнаружены ошибки при обработке.\nПроверьте логи для подробной информации.',
        )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RW_BUTTON_RETRY_VALIDATION', '🔄 Повторить валидацию'),
                callback_data='sync_validate',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SYNC_FULL', '🔄 Полная синхронизация'),
                callback_data='sync_all_users',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SYNC_BACK', '⬅️ К синхронизации'),
                callback_data='admin_rw_sync',
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def cleanup_subscriptions(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_RW_CLEANUP_PROGRESS',
            '🧹 Выполняется очистка неактуальных подписок...\n\nУдаляем подписки пользователей, отсутствующих в панели.',
        ),
        reply_markup=None,
    )

    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.cleanup_orphaned_subscriptions(db)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_SUCCESS', 'успешно завершена')
    else:
        status_emoji = '⚠️'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_ERRORS', 'завершена с ошибками')

    text = texts.t(
        'ADMIN_RW_CLEANUP_RESULT_TEMPLATE',
        '{status_emoji} <b>Очистка {status_text}</b>\n\n'
        '📊 <b>Результаты:</b>\n'
        '• 🔍 Проверено подписок: {checked}\n'
        '• 🗑️ Деактивировано: {deactivated}\n'
        '• ❌ Ошибок: {errors}\n',
    ).format(
        status_emoji=status_emoji,
        status_text=status_text,
        checked=stats['checked'],
        deactivated=stats['deactivated'],
        errors=stats['errors'],
    )

    if stats['deactivated'] > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_CLEANUP_DEACTIVATED_DETAILS',
            '🗑️ <b>Деактивированные подписки:</b>\n'
            'Отключены подписки пользователей, которые\n'
            'отсутствуют в панели Remnawave.\n',
        )
    else:
        text += '\n' + texts.t(
            'ADMIN_RW_CLEANUP_ALL_ACTUAL',
            '✅ Все подписки актуальны!\nНеактуальных подписок не найдено.',
        )

    if stats['errors'] > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_PROCESSING_ERRORS_HINT',
            '⚠️ Обнаружены ошибки при обработке.\nПроверьте логи для подробной информации.',
        )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RW_BUTTON_RETRY_CLEANUP', '🔄 Повторить очистку'),
                callback_data='sync_cleanup',
            )
        ],
        [types.InlineKeyboardButton(text=texts.t('ADMIN_SYNC_VALIDATE', '🔍 Валидация'), callback_data='sync_validate')],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SYNC_BACK', '⬅️ К синхронизации'),
                callback_data='admin_rw_sync',
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def force_cleanup_all_orphaned_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_RW_FORCE_CLEANUP_PROGRESS',
            '🗑️ Выполняется принудительная очистка всех пользователей, отсутствующих в панели...\n\n'
            '⚠️ ВНИМАНИЕ: Это полностью удалит ВСЕ данные пользователей!\n'
            '📊 Включая: транзакции, реферальные доходы, промокоды, серверы, балансы\n\n'
            '⏳ Пожалуйста, подождите...',
        ),
        reply_markup=None,
    )

    remnawave_service = RemnaWaveService()
    stats = await remnawave_service.cleanup_orphaned_subscriptions(db)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_SUCCESS', 'успешно завершена')
    else:
        status_emoji = '⚠️'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_ERRORS', 'завершена с ошибками')

    text = texts.t(
        'ADMIN_RW_FORCE_CLEANUP_RESULT_TEMPLATE',
        '{status_emoji} <b>Принудительная очистка {status_text}</b>\n\n'
        '📊 <b>Результаты:</b>\n'
        '• 🔍 Проверено подписок: {checked}\n'
        '• 🗑️ Полностью очищено: {deactivated}\n'
        '• ❌ Ошибок: {errors}\n',
    ).format(
        status_emoji=status_emoji,
        status_text=status_text,
        checked=stats['checked'],
        deactivated=stats['deactivated'],
        errors=stats['errors'],
    )

    if stats['deactivated'] > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_FORCE_CLEANUP_DETAILS',
            '🗑️ <b>Полностью очищенные данные:</b>\n'
            '• Подписки сброшены к начальному состоянию\n'
            '• Удалены ВСЕ транзакции пользователей\n'
            '• Удалены ВСЕ реферальные доходы\n'
            '• Удалены использования промокодов\n'
            '• Сброшены балансы к нулю\n'
            '• Удалены подключенные серверы\n'
            '• Сброшены HWID устройства в Remnawave\n'
            '• Очищены Remnawave UUID\n',
        )
    else:
        text += '\n' + texts.t(
            'ADMIN_RW_FORCE_CLEANUP_NOT_FOUND',
            '✅ Неактуальных подписок не найдено!\nВсе пользователи синхронизированы с панелью.',
        )

    if stats['errors'] > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_PROCESSING_ERRORS_HINT',
            '⚠️ Обнаружены ошибки при обработке.\nПроверьте логи для подробной информации.',
        )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RW_BUTTON_RETRY_CLEANUP', '🔄 Повторить очистку'),
                callback_data='force_cleanup_orphaned',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SYNC_FULL', '🔄 Полная синхронизация'),
                callback_data='sync_all_users',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SYNC_BACK', '⬅️ К синхронизации'),
                callback_data='admin_rw_sync',
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def confirm_force_cleanup(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = texts.t(
        'ADMIN_RW_FORCE_CLEANUP_CONFIRM_TEXT',
        '⚠️ <b>ВНИМАНИЕ! ОПАСНАЯ ОПЕРАЦИЯ!</b>\n\n'
        '🗑️ <b>Принудительная очистка полностью удалит:</b>\n'
        '• ВСЕ транзакции пользователей отсутствующих в панели\n'
        '• ВСЕ реферальные доходы и связи\n'
        '• ВСЕ использования промокодов\n'
        '• ВСЕ подключенные серверы подписок\n'
        '• ВСЕ балансы (сброс к нулю)\n'
        '• ВСЕ HWID устройства в Remnawave\n'
        '• ВСЕ Remnawave UUID и ссылки\n\n'
        '⚡ <b>Это действие НЕОБРАТИМО!</b>\n\n'
        'Используйте только если:\n'
        '• Обычная синхронизация не помогает\n'
        '• Нужно полностью очистить "мусорные" данные\n'
        '• После массового удаления пользователей из панели\n\n'
        '❓ <b>Вы действительно хотите продолжить?</b>',
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RW_FORCE_CLEANUP_CONFIRM_BUTTON', '🗑️ ДА, ОЧИСТИТЬ ВСЕ'),
                callback_data='force_cleanup_orphaned',
            )
        ],
        [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_rw_sync')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def sync_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    sync_type = callback.data.split('_')[-2] + '_' + callback.data.split('_')[-1]

    progress_text = texts.t('ADMIN_RW_SYNC_GENERIC_PROGRESS', '🔄 Выполняется синхронизация...\n\n')

    if sync_type == 'all_users':
        progress_text += texts.t(
            'ADMIN_RW_SYNC_GENERIC_TYPE_ALL',
            '📋 Тип: Полная синхронизация\n'
            '• Создание новых пользователей\n'
            '• Обновление существующих\n'
            '• Удаление неактуальных подписок\n',
        )
    elif sync_type == 'new_users':
        progress_text += texts.t(
            'ADMIN_RW_SYNC_GENERIC_TYPE_NEW',
            '📋 Тип: Только новые пользователи\n'
            '• Создание пользователей из панели\n',
        )
    elif sync_type == 'update_data':
        progress_text += texts.t(
            'ADMIN_RW_SYNC_GENERIC_TYPE_UPDATE',
            '📋 Тип: Обновление данных\n'
            '• Обновление информации о трафике\n'
            '• Синхронизация подписок\n',
        )

    progress_text += '\n' + texts.t('ADMIN_RW_PLEASE_WAIT', '⏳ Пожалуйста, подождите...')

    await callback.message.edit_text(progress_text, reply_markup=None)

    remnawave_service = RemnaWaveService()

    sync_map = {'all_users': 'all', 'new_users': 'new_only', 'update_data': 'update_only'}

    stats = await remnawave_service.sync_users_from_panel(db, sync_map.get(sync_type, 'all'))

    total_operations = stats['created'] + stats['updated'] + stats.get('deleted', 0)
    stats['created'] + stats['updated'] + stats.get('deleted', 0)

    if stats['errors'] == 0:
        status_emoji = '✅'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_SUCCESS', 'успешно завершена')
    elif stats['errors'] < total_operations:
        status_emoji = '⚠️'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_WARNINGS', 'завершена с предупреждениями')
    else:
        status_emoji = '❌'
        status_text = texts.t('ADMIN_RW_STATUS_COMPLETED_ERRORS', 'завершена с ошибками')

    text = texts.t(
        'ADMIN_RW_SYNC_GENERIC_RESULT_TITLE',
        '{status_emoji} <b>Синхронизация {status_text}</b>\n\n'
        '📊 <b>Результат:</b>\n',
    ).format(status_emoji=status_emoji, status_text=status_text)

    if sync_type == 'all_users':
        text += texts.t('ADMIN_RW_LINE_CREATED', '• 🆕 Создано: {count}\n').format(count=stats["created"])
        text += texts.t('ADMIN_RW_LINE_UPDATED', '• 🔄 Обновлено: {count}\n').format(count=stats["updated"])
        if 'deleted' in stats:
            text += texts.t('ADMIN_RW_LINE_DELETED', '• 🗑️ Удалено: {count}\n').format(count=stats["deleted"])
        text += texts.t('ADMIN_RW_LINE_ERRORS', '• ❌ Ошибок: {count}\n').format(count=stats["errors"])
    elif sync_type == 'new_users':
        text += texts.t('ADMIN_RW_LINE_CREATED', '• 🆕 Создано: {count}\n').format(count=stats["created"])
        text += texts.t('ADMIN_RW_LINE_ERRORS', '• ❌ Ошибок: {count}\n').format(count=stats["errors"])
        if stats['created'] == 0 and stats['errors'] == 0:
            text += '\n' + texts.t('ADMIN_RW_NEW_USERS_NOT_FOUND', '💡 Новых пользователей не найдено')
    elif sync_type == 'update_data':
        text += texts.t('ADMIN_RW_LINE_UPDATED', '• 🔄 Обновлено: {count}\n').format(count=stats["updated"])
        text += texts.t('ADMIN_RW_LINE_ERRORS', '• ❌ Ошибок: {count}\n').format(count=stats["errors"])
        if stats['updated'] == 0 and stats['errors'] == 0:
            text += '\n' + texts.t('ADMIN_RW_ALL_DATA_ACTUAL', '💡 Все данные актуальны')

    if stats['errors'] > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_SYNC_ERRORS_HINT',
            '⚠️ <b>Внимание:</b>\n'
            'Некоторые операции завершились с ошибками.\n'
            'Проверьте логи для получения подробной информации.',
        )

    if sync_type == 'all_users' and 'deleted' in stats and stats['deleted'] > 0:
        text += '\n' + texts.t(
            'ADMIN_RW_SYNC_DELETED_SUBSCRIPTIONS_DETAILS',
            '🗑️ <b>Удаленные подписки:</b>\n'
            'Деактивированы подписки пользователей,\n'
            'которые отсутствуют в панели Remnawave.',
        )

    text += '\n\n' + texts.t('ADMIN_RW_RECOMMENDATIONS_TITLE', '💡 <b>Рекомендации:</b>\n')
    if sync_type == 'all_users':
        text += texts.t('ADMIN_RW_RECOMMENDATIONS_ALL', '• Полная синхронизация выполнена\n• Рекомендуется запускать раз в день\n')
    elif sync_type == 'new_users':
        text += texts.t(
            'ADMIN_RW_RECOMMENDATIONS_NEW',
            '• Синхронизация новых пользователей\n• Используйте при массовом добавлении\n',
        )
    elif sync_type == 'update_data':
        text += texts.t(
            'ADMIN_RW_RECOMMENDATIONS_UPDATE',
            '• Обновление данных о трафике\n• Запускайте для актуализации статистики\n',
        )

    keyboard = []

    if stats['errors'] > 0:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_BUTTON_RETRY_SYNC', '🔄 Повторить синхронизацию'),
                    callback_data=callback.data,
                )
            ]
        )

    if sync_type != 'all_users':
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SYNC_FULL', '🔄 Полная синхронизация'),
                    callback_data='sync_all_users',
                )
            ]
        )

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_BUTTON_SYSTEM_STATS', '📊 Статистика системы'),
                    callback_data='admin_rw_system',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_RW_BUTTON_NODES', '🌐 Ноды'),
                    callback_data='admin_rw_nodes',
                ),
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_squads_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    remnawave_service = RemnaWaveService()
    squads = await remnawave_service.get_all_squads()

    text = texts.t('ADMIN_SQUAD_MANAGEMENT_TITLE', '🌍 <b>Управление сквадами</b>\n\n')
    keyboard = []

    if squads:
        for squad in squads:
            text += f'🔹 <b>{squad["name"]}</b>\n'
            text += texts.t('ADMIN_SQUAD_MEMBERS_LINE', '👥 Участников: {count}\n').format(count=squad["members_count"])
            text += texts.t('ADMIN_SQUAD_INBOUNDS_LINE', '📡 Инбаундов: {count}\n\n').format(
                count=squad["inbounds_count"]
            )

            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'⚙️ {squad["name"]}', callback_data=f'admin_squad_manage_{squad["uuid"]}'
                    )
                ]
            )
    else:
        text += texts.t('ADMIN_SQUAD_NOT_FOUND_LIST', 'Сквады не найдены')

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SQUAD_CREATE_BUTTON_ALT', '➕ Создать сквад'),
                    callback_data='admin_squad_create',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_remnawave')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_remnawave_menu, F.data == 'admin_remnawave')
    dp.callback_query.register(show_system_stats, F.data == 'admin_rw_system')
    dp.callback_query.register(show_traffic_stats, F.data == 'admin_rw_traffic')
    dp.callback_query.register(show_nodes_management, F.data == 'admin_rw_nodes')
    dp.callback_query.register(show_node_details, F.data.startswith('admin_node_manage_'))
    dp.callback_query.register(show_node_statistics, F.data.startswith('node_stats_'))
    dp.callback_query.register(manage_node, F.data.startswith('node_enable_'))
    dp.callback_query.register(manage_node, F.data.startswith('node_disable_'))
    dp.callback_query.register(manage_node, F.data.startswith('node_restart_'))
    dp.callback_query.register(restart_all_nodes, F.data == 'admin_restart_all_nodes')
    dp.callback_query.register(show_sync_options, F.data == 'admin_rw_sync')
    dp.callback_query.register(show_auto_sync_settings, F.data == 'admin_rw_auto_sync')
    dp.callback_query.register(toggle_auto_sync_setting, F.data == 'remnawave_auto_sync_toggle')
    dp.callback_query.register(prompt_auto_sync_schedule, F.data == 'remnawave_auto_sync_times')
    dp.callback_query.register(cancel_auto_sync_schedule, F.data == 'remnawave_auto_sync_cancel')
    dp.callback_query.register(run_auto_sync_now, F.data == 'remnawave_auto_sync_run')
    dp.callback_query.register(sync_all_users, F.data == 'sync_all_users')
    dp.callback_query.register(sync_users_to_panel, F.data == 'sync_to_panel')
    dp.callback_query.register(show_squad_migration_menu, F.data == 'admin_rw_migration')
    dp.callback_query.register(paginate_migration_source, F.data.startswith('admin_migration_source_page_'))
    dp.callback_query.register(handle_migration_source_selection, F.data.startswith('admin_migration_source_'))
    dp.callback_query.register(paginate_migration_target, F.data.startswith('admin_migration_target_page_'))
    dp.callback_query.register(handle_migration_target_selection, F.data.startswith('admin_migration_target_'))
    dp.callback_query.register(change_migration_target, F.data == 'admin_migration_change_target')
    dp.callback_query.register(confirm_squad_migration, F.data == 'admin_migration_confirm')
    dp.callback_query.register(cancel_squad_migration, F.data == 'admin_migration_cancel')
    dp.callback_query.register(handle_migration_page_info, F.data == 'admin_migration_page_info')
    dp.callback_query.register(show_squads_management, F.data == 'admin_rw_squads')
    dp.callback_query.register(show_squad_details, F.data.startswith('admin_squad_manage_'))
    dp.callback_query.register(manage_squad_action, F.data.startswith('squad_add_users_'))
    dp.callback_query.register(manage_squad_action, F.data.startswith('squad_remove_users_'))
    dp.callback_query.register(manage_squad_action, F.data.startswith('squad_delete_'))
    dp.callback_query.register(
        show_squad_edit_menu, F.data.startswith('squad_edit_') & ~F.data.startswith('squad_edit_inbounds_')
    )
    dp.callback_query.register(show_squad_inbounds_selection, F.data.startswith('squad_edit_inbounds_'))
    dp.callback_query.register(show_squad_rename_form, F.data.startswith('squad_rename_'))
    dp.callback_query.register(cancel_squad_rename, F.data.startswith('cancel_rename_'))
    dp.callback_query.register(toggle_squad_inbound, F.data.startswith('sqd_tgl_'))
    dp.callback_query.register(save_squad_inbounds, F.data.startswith('sqd_save_'))
    dp.callback_query.register(show_squad_edit_menu_short, F.data.startswith('sqd_edit_'))
    dp.callback_query.register(start_squad_creation, F.data == 'admin_squad_create')
    dp.callback_query.register(cancel_squad_creation, F.data == 'cancel_squad_create')
    dp.callback_query.register(toggle_create_inbound, F.data.startswith('create_tgl_'))
    dp.callback_query.register(finish_squad_creation, F.data == 'create_squad_finish')

    dp.message.register(process_squad_new_name, SquadRenameStates.waiting_for_new_name, F.text)

    dp.message.register(process_squad_name, SquadCreateStates.waiting_for_name, F.text)

    dp.message.register(
        save_auto_sync_schedule,
        RemnaWaveSyncStates.waiting_for_schedule,
        F.text,
    )
