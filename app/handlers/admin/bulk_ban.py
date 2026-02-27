"""
Обработчики команд для массовой блокировки пользователей
"""

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.localization.texts import get_texts
from app.services.bulk_ban_service import bulk_ban_service
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _admin_users_keyboard(button_text: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=button_text, callback_data='admin_users')]]
    )


@admin_required
@error_handler
async def start_bulk_ban_process(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Начало процесса массовой блокировки пользователей
    """
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_BULK_BAN_START_TEXT',
            '🛑 <b>Массовая блокировка пользователей</b>\n\n'
            'Введите список Telegram ID для блокировки.\n\n'
            '<b>Форматы ввода:</b>\n'
            '• По одному ID на строку\n'
            '• Через запятую\n'
            '• Через пробел\n\n'
            'Пример:\n'
            '<code>123456789\n'
            '987654321\n'
            '111222333</code>\n\n'
            'Или:\n'
            '<code>123456789, 987654321, 111222333</code>\n\n'
            'Для отмены используйте команду /cancel',
        ),
        parse_mode='HTML',
        reply_markup=_admin_users_keyboard(texts.t('ADMIN_CANCEL', '❌ Отмена')),
    )

    await state.set_state(AdminStates.waiting_for_bulk_ban_list)
    await callback.answer()


@admin_required
@error_handler
async def process_bulk_ban_list(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """
    Обработка списка Telegram ID и выполнение массовой блокировки
    """
    texts = get_texts(db_user.language)

    if not message.text:
        await message.answer(
            texts.t('ADMIN_BULK_BAN_TEXT_REQUIRED', '❌ Отправьте текстовое сообщение со списком Telegram ID'),
            reply_markup=_admin_users_keyboard(texts.t('ADMIN_BULK_BAN_BACK_BUTTON', '🔙 Назад')),
        )
        return

    input_text = message.text.strip()

    if not input_text:
        await message.answer(
            texts.t('ADMIN_BULK_BAN_INVALID_LIST', '❌ Введите корректный список Telegram ID'),
            reply_markup=_admin_users_keyboard(texts.t('ADMIN_BULK_BAN_BACK_BUTTON', '🔙 Назад')),
        )
        return

    # Парсим ID из текста
    try:
        telegram_ids = await bulk_ban_service.parse_telegram_ids_from_text(input_text)
    except Exception as e:
        logger.error('Ошибка парсинга Telegram ID', error=e)
        await message.answer(
            texts.t('ADMIN_BULK_BAN_PARSE_ERROR', '❌ Ошибка при обработке списка ID. Проверьте формат ввода.'),
            reply_markup=_admin_users_keyboard(texts.t('ADMIN_BULK_BAN_BACK_BUTTON', '🔙 Назад')),
        )
        return

    if not telegram_ids:
        await message.answer(
            texts.t('ADMIN_BULK_BAN_NO_VALID_IDS', '❌ Не найдено корректных Telegram ID в списке'),
            reply_markup=_admin_users_keyboard(texts.t('ADMIN_BULK_BAN_BACK_BUTTON', '🔙 Назад')),
        )
        return

    if len(telegram_ids) > 1000:  # Ограничение на количество ID за раз
        await message.answer(
            texts.t('ADMIN_BULK_BAN_TOO_MANY_IDS', '❌ Слишком много ID в списке ({count}). Максимум: 1000').format(
                count=len(telegram_ids)
            ),
            reply_markup=_admin_users_keyboard(texts.t('ADMIN_BULK_BAN_BACK_BUTTON', '🔙 Назад')),
        )
        return

    # Выполняем массовую блокировку
    try:
        successfully_banned, not_found, error_ids = await bulk_ban_service.ban_users_by_telegram_ids(
            db=db,
            admin_user_id=db_user.id,
            telegram_ids=telegram_ids,
            reason=texts.t('ADMIN_BULK_BAN_REASON', 'Массовая блокировка администратором'),
            bot=message.bot,
            notify_admin=True,
            admin_name=db_user.full_name,
        )

        # Подготавливаем сообщение с результатами
        result_lines = [
            texts.t('ADMIN_BULK_BAN_RESULT_HEADER', '✅ <b>Массовая блокировка завершена</b>'),
            '',
            texts.t('ADMIN_BULK_BAN_RESULT_STATS_HEADER', '📊 <b>Результаты:</b>'),
            texts.t('ADMIN_BULK_BAN_RESULT_SUCCESS_COUNT', '✅ Успешно заблокировано: {count}').format(
                count=successfully_banned
            ),
            texts.t('ADMIN_BULK_BAN_RESULT_NOT_FOUND_COUNT', '❌ Не найдено: {count}').format(count=not_found),
            texts.t('ADMIN_BULK_BAN_RESULT_ERRORS_COUNT', '💥 Ошибок: {count}').format(count=len(error_ids)),
            '',
            texts.t('ADMIN_BULK_BAN_RESULT_TOTAL_COUNT', '📈 Всего обработано: {count}').format(
                count=len(telegram_ids)
            ),
        ]

        if successfully_banned > 0:
            result_lines.append(
                texts.t('ADMIN_BULK_BAN_RESULT_SUCCESS_RATE', '🎯 Процент успеха: {rate}%').format(
                    rate=round((successfully_banned / len(telegram_ids)) * 100, 1)
                )
            )

        # Добавляем информацию об ошибках, если есть
        if error_ids:
            result_lines.extend(
                [
                    '',
                    texts.t('ADMIN_BULK_BAN_RESULT_ERROR_IDS_HEADER', '⚠️ <b>Telegram ID с ошибками:</b>'),
                ]
            )
            error_ids_text = f'<code>{", ".join(map(str, error_ids[:10]))}</code>'  # Показываем первые 10
            if len(error_ids) > 10:
                error_ids_text += texts.t('ADMIN_BULK_BAN_RESULT_ERROR_IDS_MORE', ' и еще {count}...').format(
                    count=len(error_ids) - 10
                )
            result_lines.append(error_ids_text)

        result_text = '\n'.join(result_lines)

        await message.answer(
            result_text,
            parse_mode='HTML',
            reply_markup=_admin_users_keyboard(texts.t('ADMIN_BULK_BAN_TO_USERS_BUTTON', '👥 К пользователям')),
        )

    except Exception as e:
        logger.error('Ошибка при выполнении массовой блокировки', error=e)
        await message.answer(
            texts.t('ADMIN_BULK_BAN_EXECUTION_ERROR', '❌ Произошла ошибка при выполнении массовой блокировки'),
            reply_markup=_admin_users_keyboard(texts.t('ADMIN_BULK_BAN_BACK_BUTTON', '🔙 Назад')),
        )

    await state.clear()


def register_bulk_ban_handlers(dp):
    """
    Регистрация обработчиков команд для массовой блокировки
    """
    # Обработчик команды начала массовой блокировки
    dp.callback_query.register(start_bulk_ban_process, lambda c: c.data == 'admin_bulk_ban_start')

    # Обработчик текстового сообщения с ID для блокировки
    dp.message.register(process_bulk_ban_list, AdminStates.waiting_for_bulk_ban_list)
