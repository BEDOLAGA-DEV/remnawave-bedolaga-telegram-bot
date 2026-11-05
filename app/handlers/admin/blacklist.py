"""
Обработчики админ-панели для управления черным списком
"""

import html
import re

import structlog
from aiogram import types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from app.database.models import User
from app.localization.texts import get_texts
from app.services.blacklist_service import blacklist_service
from app.states import BlacklistStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _localize_blacklist_result(message: str, language: str) -> str:
    texts = get_texts(language)
    if message == 'Ошибка обновления черного списка':
        return texts.t('ADMIN_ACCESS_BLACKLIST_UPDATE_ERROR', 'Ошибка обновления черного списка')
    match = re.fullmatch(r'Черный список обновлен успешно\. Записей: ([0-9]+)', message)
    if match:
        return texts.t('ADMIN_ACCESS_BLACKLIST_UPDATED', 'Черный список обновлен успешно. Записей: {count}').format(
            count=match[1]
        )
    return message


@admin_required
@error_handler
async def show_blacklist_settings(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Показывает настройки черного списка
    """
    texts = get_texts(db_user.language)
    logger.info('Вызван обработчик show_blacklist_settings для пользователя', from_user_id=callback.from_user.id)

    is_enabled = blacklist_service.is_blacklist_check_enabled()
    github_url = blacklist_service.get_blacklist_github_url()
    blacklist_count = len(await blacklist_service.get_all_blacklisted_users())

    status_text = (
        texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_000', '✅ Включена')
        if is_enabled
        else texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_001', '❌ Отключена')
    )
    url_text = github_url or texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_002', 'Не задан')

    text = texts.t(
        'ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_003',
        '\n🔐 <b>Настройки черного списка</b>\n\nСтатус: {value1}\nURL к черному списку: <code>{value2}</code>\nКоличество записей: {value3}\n\nДействия:\n',
    ).format(value1=status_text, value2=html.escape(url_text), value3=blacklist_count)

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_004', '🔄 Обновить список')
                if is_enabled
                else texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_005', '🔄 Обновить (откл.)'),
                callback_data='admin_blacklist_update',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_006', '📋 Просмотреть список')
                if is_enabled
                else texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_007', '📋 Просмотр (откл.)'),
                callback_data='admin_blacklist_view',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_008', '✏️ URL к GitHub')
                if not github_url
                else texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_009', '✏️ Изменить URL'),
                callback_data='admin_blacklist_set_url',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_010', '✅ Включить')
                if not is_enabled
                else texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_011', '❌ Отключить'),
                callback_data='admin_blacklist_toggle',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_012', '⬅️ Назад к пользователям'),
                callback_data='admin_users',
            )
        ],
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
    texts = get_texts(db_user.language)
    is_enabled = blacklist_service.is_blacklist_check_enabled()

    # В реальной реализации нужно будет изменить настройку в базе данных
    # или в системе настроек, но сейчас просто покажем статус
    new_status = not is_enabled
    status_text = (
        texts.t('ADMIN_BLACKLIST_UI_TOGGLE_BLACKLIST_013', 'включена')
        if new_status
        else texts.t('ADMIN_BLACKLIST_UI_TOGGLE_BLACKLIST_014', 'отключена')
    )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_BLACKLIST_UI_TOGGLE_BLACKLIST_015',
            'Статус проверки черного списка: {value1}\n\nДля изменения статуса проверки черного списка измените значение\n<code>BLACKLIST_CHECK_ENABLED</code> в файле <code>.env</code>',
        ).format(value1=status_text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_BLACKLIST_UI_TOGGLE_BLACKLIST_016', '🔄 Обновить статус'),
                        callback_data='admin_blacklist_settings',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_BLACKLIST_UI_TOGGLE_BLACKLIST_017', '⬅️ Назад'),
                        callback_data='admin_blacklist_settings',
                    )
                ],
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
    texts = get_texts(db_user.language)
    success, message = await blacklist_service.force_update_blacklist()
    message = html.escape(_localize_blacklist_result(message, db_user.language))

    if success:
        await callback.message.edit_text(
            f'✅ {message}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_006', '📋 Просмотреть список'),
                            callback_data='admin_blacklist_view',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_BLACKLIST_UI_UPDATE_BLACKLIST_019', '🔄 Ручное обновление'),
                            callback_data='admin_blacklist_update',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_BLACKLIST_UI_TOGGLE_BLACKLIST_017', '⬅️ Назад'),
                            callback_data='admin_blacklist_settings',
                        )
                    ],
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_BLACKLIST_UI_UPDATE_BLACKLIST_021', '❌ Ошибка обновления: {value1}').format(value1=message),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_BLACKLIST_UI_UPDATE_BLACKLIST_022', '🔄 Повторить'),
                            callback_data='admin_blacklist_update',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_BLACKLIST_UI_TOGGLE_BLACKLIST_017', '⬅️ Назад'),
                            callback_data='admin_blacklist_settings',
                        )
                    ],
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
    texts = get_texts(db_user.language)
    blacklist_users = await blacklist_service.get_all_blacklisted_users()

    if not blacklist_users:
        text = texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_USERS_024', 'Черный список пуст')
    else:
        text = texts.t(
            'ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_USERS_025', '🔐 <b>Черный список ({value1} записей)</b>\n\n'
        ).format(value1=len(blacklist_users))

        # Показываем первые 20 записей
        for i, (tg_id, username, reason) in enumerate(blacklist_users[:20], 1):
            text += f'{i}. <code>{tg_id}</code> {html.escape(username or "")} — {html.escape(reason or "")}\n'

        if len(blacklist_users) > 20:
            text += texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_USERS_026', '\n... и еще {value1} записей').format(
                value1=len(blacklist_users) - 20
            )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_USERS_027', '🔄 Обновить'),
                        callback_data='admin_blacklist_view',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_BLACKLIST_UI_TOGGLE_BLACKLIST_017', '⬅️ Назад'),
                        callback_data='admin_blacklist_settings',
                    )
                ],
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
    texts = get_texts(db_user.language)
    current_url = blacklist_service.get_blacklist_github_url() or texts.t(
        'ADMIN_BLACKLIST_UI_START_SET_BLACKLIST_URL_029', 'не задан'
    )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_BLACKLIST_UI_START_SET_BLACKLIST_URL_030',
            'Введите новый URL к файлу черного списка на GitHub\n\nТекущий URL: {value1}\n\nПример: https://raw.githubusercontent.com/username/repository/main/blacklist.txt\n\nДля отмены используйте команду /cancel',
        ).format(value1=html.escape(current_url)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_BLACKLIST_UI_TOGGLE_BLACKLIST_017', '⬅️ Назад'),
                        callback_data='admin_blacklist_settings',
                    )
                ]
            ]
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
    texts = get_texts(db_user.language)
    if await state.get_state() != BlacklistStates.waiting_for_blacklist_url.state:
        return

    url = message.text.strip()

    # В реальной реализации нужно сохранить URL в систему настроек
    # В текущей реализации просто выводим сообщение
    if url.lower() in ['/cancel', 'отмена', 'cancel']:
        await message.answer(
            texts.t('ADMIN_BLACKLIST_UI_PROCESS_BLACKLIST_URL_033', 'Настройка URL отменена'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_BLACKLIST_UI_PROCESS_BLACKLIST_URL_039', '🔐 Настройки черного списка'),
                            callback_data='admin_blacklist_settings',
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
            texts.t(
                'ADMIN_BLACKLIST_UI_PROCESS_BLACKLIST_URL_035',
                '❌ Некорректный URL. URL должен начинаться с http:// или https://',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_BLACKLIST_UI_PROCESS_BLACKLIST_URL_039', '🔐 Настройки черного списка'),
                            callback_data='admin_blacklist_settings',
                        )
                    ]
                ]
            ),
        )
        return

    # В реальной системе здесь нужно сохранить URL в базу данных настроек
    # или в систему конфигурации

    await message.answer(
        texts.t(
            'ADMIN_BLACKLIST_UI_PROCESS_BLACKLIST_URL_037',
            '✅ URL к черному списку установлен:\n<code>{value1}</code>\n\nДля применения изменений перезапустите бота или измените значение\n<code>BLACKLIST_GITHUB_URL</code> в файле <code>.env</code>',
        ).format(value1=html.escape(url)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_BLACKLIST_UI_SHOW_BLACKLIST_SETTINGS_004', '🔄 Обновить список'),
                        callback_data='admin_blacklist_update',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_BLACKLIST_UI_PROCESS_BLACKLIST_URL_039', '🔐 Настройки черного списка'),
                        callback_data='admin_blacklist_settings',
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
