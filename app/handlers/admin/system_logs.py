from datetime import UTC, datetime
from html import escape
from pathlib import Path

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import admin_required, error_handler
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

LOG_PREVIEW_LIMIT = 2300


def _resolve_log_path() -> Path:
    log_path = Path(settings.LOG_FILE)
    if not log_path.is_absolute():
        log_path = Path.cwd() / log_path
    return log_path


def _format_preview_block(text: str) -> str:
    escaped_text = escape(text) if text else ''
    return f'<blockquote expandable><pre><code>{escaped_text}</code></pre></blockquote>'


def _build_logs_message(log_path: Path, language: str = 'ru') -> str:
    if not log_path.exists():
        message = (
            get_texts(language)
            .t(
                'ADMIN_SYSTEM_LOGS_UI_000',
                '🧾 <b>Системные логи</b>\n\nФайл <code>{v0}</code> пока не создан.\nЛоги появятся автоматически после первой записи.',
            )
            .format(v0=log_path)
        )
        return message

    try:
        content = log_path.read_text(encoding='utf-8', errors='ignore')
    except Exception as error:  # pragma: no cover - защита от проблем чтения
        logger.error('Ошибка чтения лог-файла', log_path=log_path, error=error)
        message = (
            get_texts(language)
            .t(
                'ADMIN_SYSTEM_LOGS_UI_007',
                '❌ <b>Ошибка чтения логов</b>\n\nНе удалось прочитать файл <code>{v0}</code>.',
            )
            .format(v0=log_path)
        )
        return message

    total_length = len(content)
    stats = log_path.stat()
    updated_at = datetime.fromtimestamp(stats.st_mtime, tz=UTC)

    if not content:
        preview_text = get_texts(language).t('ADMIN_SYSTEM_LOGS_UI_001', 'Лог-файл пуст.')
        truncated = False
    else:
        preview_text = content[-LOG_PREVIEW_LIMIT:]
        truncated = total_length > LOG_PREVIEW_LIMIT

    details_lines = [
        get_texts(language).t('ADMIN_SYSTEM_LOGS_UI_002', '🧾 <b>Системные логи</b>'),
        '',
        get_texts(language).t('ADMIN_SYSTEM_LOGS_UI_003', '📁 <b>Файл:</b> <code>{v0}</code>').format(v0=log_path),
        get_texts(language)
        .t('ADMIN_SYSTEM_LOGS_UI_004', '🕒 <b>Обновлен:</b> {v0}')
        .format(v0=format_local_datetime(updated_at, '%d.%m.%Y %H:%M:%S')),
        get_texts(language).t('ADMIN_SYSTEM_LOGS_UI_005', '🧮 <b>Размер:</b> {v0} символов').format(v0=total_length),
        (
            get_texts(language)
            .t('ADMIN_SYSTEM_LOGS_UI_008', '👇 Показаны последние {v0} символов.')
            .format(v0=LOG_PREVIEW_LIMIT)
            if truncated
            else get_texts(language).t('ADMIN_SYSTEM_LOGS_UI_009', '📄 Показано все содержимое файла.')
        ),
        '',
        _format_preview_block(preview_text),
    ]

    return '\n'.join(details_lines)


def _get_logs_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_SYSTEM_LOGS_UI_014', '🔄 Обновить'),
                    callback_data='admin_system_logs_refresh',
                )
            ],
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_SYSTEM_LOGS_UI_015', '⬇️ Скачать лог'),
                    callback_data='admin_system_logs_download',
                )
            ],
            [
                InlineKeyboardButton(
                    text=get_texts(language).t('ADMIN_SYSTEM_LOGS_UI_016', '⬅️ Назад'),
                    callback_data='admin_submenu_system',
                )
            ],
        ]
    )


@admin_required
@error_handler
async def show_system_logs(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    log_path = _resolve_log_path()
    message = _build_logs_message(log_path, language=db_user.language)

    reply_markup = _get_logs_keyboard(language=db_user.language)
    await callback.message.edit_text(message, reply_markup=reply_markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def refresh_system_logs(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    log_path = _resolve_log_path()
    message = _build_logs_message(log_path, language=db_user.language)

    reply_markup = _get_logs_keyboard(language=db_user.language)
    await callback.message.edit_text(message, reply_markup=reply_markup, parse_mode='HTML')
    await callback.answer(get_texts(db_user.language).t('ADMIN_SYSTEM_LOGS_UI_010', '🔄 Обновлено'))


@admin_required
@error_handler
async def download_system_logs(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    log_path = _resolve_log_path()

    if not log_path.exists() or not log_path.is_file():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_SYSTEM_LOGS_UI_011', '❌ Лог-файл не найден'), show_alert=True
        )
        return

    try:
        await callback.answer(get_texts(db_user.language).t('ADMIN_SYSTEM_LOGS_UI_012', '⬇️ Отправляю лог...'))

        document = FSInputFile(log_path)
        stats = log_path.stat()
        updated_at = format_local_datetime(datetime.fromtimestamp(stats.st_mtime, tz=UTC), '%d.%m.%Y %H:%M:%S')
        caption = (
            get_texts(db_user.language)
            .t(
                'ADMIN_SYSTEM_LOGS_UI_006',
                '🧾 Лог-файл <code>{v0}</code>\n📁 Путь: <code>{v1}</code>\n🕒 Обновлен: {v2}',
            )
            .format(v0=log_path.name, v1=log_path, v2=updated_at)
        )
        await callback.message.answer_document(document=document, caption=caption, parse_mode='HTML')
    except Exception as error:  # pragma: no cover - защита от ошибок отправки
        logger.error('Ошибка отправки лог-файла', log_path=log_path, error=error)
        await callback.message.answer(
            get_texts(db_user.language).t(
                'ADMIN_SYSTEM_LOGS_UI_013',
                '❌ <b>Не удалось отправить лог-файл</b>\n\nПроверьте журналы приложения или повторите попытку позже.',
            ),
            parse_mode='HTML',
        )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(
        show_system_logs,
        F.data == 'admin_system_logs',
    )
    dp.callback_query.register(
        refresh_system_logs,
        F.data == 'admin_system_logs_refresh',
    )
    dp.callback_query.register(
        download_system_logs,
        F.data == 'admin_system_logs_download',
    )
