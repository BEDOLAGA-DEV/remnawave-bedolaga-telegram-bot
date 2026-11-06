import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.admin import get_admin_main_keyboard, get_maintenance_keyboard
from app.localization.texts import get_texts
from app.services.maintenance_service import maintenance_service
from app.services.system_settings_service import bot_configuration_service
from app.utils.decorators import admin_required, error_handler
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)


class MaintenanceStates(StatesGroup):
    waiting_for_reason = State()
    waiting_for_notification_message = State()


# Дописывается к ответу, когда MAINTENANCE_MODE задан в .env. Такой ключ попадает
# в ENV_OVERRIDE_KEYS: переключение ложится в БД, но к настройкам не применяется, и
# на старте set_bot читает значение из окружения. Без этого предупреждения админ
# видит «выключено», а после перезапуска техработы включаются снова — и выглядит
# это как самопроизвольное включение.
_ENV_LOCKED_WARNING = (
    '\n\n⚠️ MAINTENANCE_MODE задан в .env, поэтому переключение не переживёт '
    'перезапуск. Уберите строку из .env, чтобы управлять режимом отсюда.'
)


def _panel_description(summary: dict, language: str) -> str:
    texts = get_texts(language)
    labels = {
        'online': ('ADMIN_MAINTENANCE_052', '🟢 Панель работает нормально'),
        'offline': ('ADMIN_MAINTENANCE_053', '🔴 Панель недоступна'),
        'degraded': ('ADMIN_MAINTENANCE_054', '🟡 Панель работает со сбоями'),
        'maintenance': ('ADMIN_MAINTENANCE_PANEL_MAINTENANCE', '🔧 Панель на обслуживании'),
        'error': ('ADMIN_MAINTENANCE_PANEL_ERROR', '❌ Ошибка проверки статуса'),
    }
    known = labels.get(summary.get('status'))
    if known:
        return texts.t(*known)
    return summary.get('description') or texts.t('ADMIN_MAINTENANCE_033', '❓ Статус неизвестен')


def _maintenance_env_locked() -> bool:
    return bot_configuration_service.is_env_overridden('MAINTENANCE_MODE')


async def _persist_maintenance_mode(db: AsyncSession, enabled: bool) -> None:
    try:
        await bot_configuration_service.set_value(db, 'MAINTENANCE_MODE', enabled)
        await db.commit()
    except Exception as error:
        logger.error('Не удалось сохранить MAINTENANCE_MODE после переключения из бота', enabled=enabled, error=error)


@admin_required
@error_handler
async def show_maintenance_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    get_texts(db_user.language)

    status_info = maintenance_service.get_status_info()

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()
        panel_status = await rw_service.get_panel_status_summary()
    except Exception as e:
        logger.error('Ошибка получения статуса панели', error=e)
        panel_status = {'description': texts.t('ADMIN_MAINTENANCE_041', '❓ Не удалось проверить'), 'has_issues': True}

    status_emoji = '🔧' if status_info['is_active'] else '✅'
    status_text = (
        texts.t('ADMIN_MAINTENANCE_003', 'Включен')
        if status_info['is_active']
        else texts.t('ADMIN_MAINTENANCE_004', 'Выключен')
    )

    api_emoji = '✅' if status_info['api_status'] else '❌'
    api_text = (
        texts.t('ADMIN_MAINTENANCE_005', 'Доступно')
        if status_info['api_status']
        else texts.t('ADMIN_MAINTENANCE_006', 'Недоступно')
    )

    monitoring_emoji = '🔄' if status_info['monitoring_active'] else '⏹️'
    monitoring_text = (
        texts.t('ADMIN_MAINTENANCE_007', 'Запущен')
        if status_info['monitoring_active']
        else texts.t('ADMIN_MAINTENANCE_008', 'Остановлен')
    )

    enabled_info = ''
    if status_info['is_active'] and status_info['enabled_at']:
        enabled_time = format_local_datetime(status_info['enabled_at'], '%d.%m.%Y %H:%M:%S')
        enabled_info = texts.t('ADMIN_MAINTENANCE_009', '\n📅 <b>Включен:</b> {value1}').format(value1=enabled_time)
        if status_info['reason']:
            enabled_info += texts.t('ADMIN_MAINTENANCE_021', '\n📝 <b>Причина:</b> {value1}').format(
                value1=html.escape(status_info['reason'])
            )

    last_check_info = ''
    if status_info['last_check']:
        last_check_time = format_local_datetime(status_info['last_check'], '%H:%M:%S')
        last_check_info = texts.t('ADMIN_MAINTENANCE_010', '\n🕐 <b>Последняя проверка:</b> {value1}').format(
            value1=last_check_time
        )

    failures_info = ''
    if status_info['consecutive_failures'] > 0:
        failures_info = texts.t('ADMIN_MAINTENANCE_011', '\n⚠️ <b>Неудачных проверок подряд:</b> {value1}').format(
            value1=status_info['consecutive_failures']
        )

    panel_info = texts.t('ADMIN_MAINTENANCE_001', '\n🌐 <b>Панель Remnawave:</b> {value1}').format(
        value1=html.escape(_panel_description(panel_status, db_user.language))
    )
    if panel_status.get('response_time'):
        panel_info += texts.t('ADMIN_MAINTENANCE_012', '\n⚡ <b>Время отклика:</b> {value1}с').format(
            value1=panel_status['response_time']
        )

    message_text = texts.t(
        'ADMIN_MAINTENANCE_002',
        '\n🔧 <b>Управление техническими работами</b>\n\n{value1} <b>Режим техработ:</b> {value2}\n{value3} <b>API Remnawave:</b> {value4}\n{value5} <b>Мониторинг:</b> {value6}\n🛠️ <b>Автозапуск мониторинга:</b> {value7}\n⏱️ <b>Интервал проверки:</b> {value8}с\n🤖 <b>Автовключение:</b> {value9}\n{value10}\n{value11}\n{value12}\n{value13}\n\nℹ️ <i>В режиме техработ обычные пользователи не могут использовать бота. Администраторы имеют полный доступ.</i>\n',
    ).format(
        value1=status_emoji,
        value2=status_text,
        value3=api_emoji,
        value4=api_text,
        value5=monitoring_emoji,
        value6=monitoring_text,
        value7=texts.t('ADMIN_MAINTENANCE_003', 'Включен')
        if status_info['monitoring_configured']
        else texts.t('ADMIN_MAINTENANCE_042', 'Отключен'),
        value8=status_info['check_interval'],
        value9=texts.t('ADMIN_MAINTENANCE_043', 'Включено')
        if status_info['auto_enable_configured']
        else texts.t('ADMIN_MAINTENANCE_044', 'Отключено'),
        value10=panel_info,
        value11=enabled_info,
        value12=last_check_info,
        value13=failures_info,
    )

    await callback.message.edit_text(
        message_text,
        reply_markup=get_maintenance_keyboard(
            db_user.language,
            status_info['is_active'],
            status_info['monitoring_active'],
            panel_status.get('has_issues', False),
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_maintenance_mode(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    is_active = maintenance_service.is_maintenance_active()

    if is_active:
        success = await maintenance_service.disable_maintenance()
        if success:
            await _persist_maintenance_mode(db, False)
        if success:
            text = texts.t('ADMIN_MAINTENANCE_022', 'Режим техработ выключен')
            if _maintenance_env_locked():
                text += texts.t('ADMIN_MAINTENANCE_000', _ENV_LOCKED_WARNING)
            await callback.answer(text, show_alert=True)
        else:
            await callback.answer(
                texts.t('ADMIN_MAINTENANCE_050', 'Ошибка выключения режима техработ'), show_alert=True
            )
    else:
        await state.set_state(MaintenanceStates.waiting_for_reason)
        await callback.message.edit_text(
            texts.t(
                'ADMIN_MAINTENANCE_045',
                '🔧 <b>Включение режима техработ</b>\n\nВведите причину включения техработ или отправьте /skip для пропуска:',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_MAINTENANCE_059', '❌ Отмена'), callback_data='maintenance_panel'
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_maintenance_reason(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    current_state = await state.get_state()

    if current_state != MaintenanceStates.waiting_for_reason:
        return

    reason = None
    if message.text and message.text != '/skip':
        reason = message.text[:200]

    success = await maintenance_service.enable_maintenance(reason=reason, auto=False)
    if success:
        await _persist_maintenance_mode(db, True)

    if success:
        response_text = texts.t('ADMIN_MAINTENANCE_013', 'Режим техработ включен')
        if reason:
            response_text += texts.t('ADMIN_MAINTENANCE_023', '\nПричина: {value1}').format(value1=html.escape(reason))
        if _maintenance_env_locked():
            response_text += texts.t('ADMIN_MAINTENANCE_000', _ENV_LOCKED_WARNING)
    else:
        response_text = texts.t('ADMIN_MAINTENANCE_014', 'Ошибка включения режима техработ')

    await message.answer(response_text)
    await state.clear()

    maintenance_service.get_status_info()
    await message.answer(
        texts.t('ADMIN_MAINTENANCE_024', 'Вернуться к панели управления техработами:'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_MAINTENANCE_060', '🔧 Панель техработ'), callback_data='maintenance_panel'
                    )
                ]
            ]
        ),
    )


@admin_required
@error_handler
async def toggle_monitoring(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    status_info = maintenance_service.get_status_info()

    if status_info['monitoring_active']:
        success = await maintenance_service.stop_monitoring()
        message = (
            texts.t('ADMIN_MAINTENANCE_025', 'Мониторинг остановлен')
            if success
            else texts.t('ADMIN_MAINTENANCE_026', 'Ошибка остановки мониторинга')
        )
    else:
        success = await maintenance_service.start_monitoring()
        message = (
            texts.t('ADMIN_MAINTENANCE_027', 'Мониторинг запущен')
            if success
            else texts.t('ADMIN_MAINTENANCE_028', 'Ошибка запуска мониторинга')
        )

    await callback.answer(message, show_alert=True)

    await show_maintenance_panel(callback, db_user, db, None)


@admin_required
@error_handler
async def force_api_check(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('ADMIN_MAINTENANCE_029', 'Проверка API...'), show_alert=False)

    check_result = await maintenance_service.force_api_check()

    if check_result['success']:
        status_text = (
            texts.t('ADMIN_MAINTENANCE_030', 'доступно')
            if check_result['api_available']
            else texts.t('ADMIN_MAINTENANCE_031', 'недоступно')
        )
        message = texts.t('ADMIN_MAINTENANCE_015', 'API {value1}\nВремя ответа: {value2}с').format(
            value1=status_text, value2=check_result['response_time']
        )
    else:
        message = texts.t('ADMIN_MAINTENANCE_016', 'Ошибка проверки: {value1}').format(
            value1=check_result.get('error', texts.t('ADMIN_MAINTENANCE_051', 'Неизвестная ошибка'))
        )

    await callback.message.answer(message)

    await show_maintenance_panel(callback, db_user, db, None)


@admin_required
@error_handler
async def check_panel_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('ADMIN_MAINTENANCE_032', 'Проверка статуса панели...'), show_alert=False)

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()

        status_data = await rw_service.check_panel_health()

        status_text = {
            'online': texts.t('ADMIN_MAINTENANCE_052', '🟢 Панель работает нормально'),
            'offline': texts.t('ADMIN_MAINTENANCE_053', '🔴 Панель недоступна'),
            'degraded': texts.t('ADMIN_MAINTENANCE_054', '🟡 Панель работает со сбоями'),
        }.get(status_data['status'], texts.t('ADMIN_MAINTENANCE_033', '❓ Статус неизвестен'))

        message_parts = [
            texts.t('ADMIN_MAINTENANCE_034', '🌐 <b>Статус панели Remnawave</b>\n'),
            f'{status_text}',
            texts.t('ADMIN_MAINTENANCE_035', '⚡ Время отклика: {value1}с').format(
                value1=status_data.get('response_time', 0)
            ),
            texts.t('ADMIN_MAINTENANCE_036', '👥 Пользователей онлайн: {value1}').format(
                value1=status_data.get('users_online', 0)
            ),
            texts.t('ADMIN_MAINTENANCE_037', '🖥️ Нод онлайн: {value1}/{value2}').format(
                value1=status_data.get('nodes_online', 0), value2=status_data.get('total_nodes', 0)
            ),
        ]

        attempts_used = status_data.get('attempts_used')
        if attempts_used:
            message_parts.append(
                texts.t('ADMIN_MAINTENANCE_046', '🔁 Попыток проверки: {value1}').format(value1=attempts_used)
            )

        if status_data.get('api_error'):
            message_parts.append(
                texts.t('ADMIN_MAINTENANCE_047', '❌ Ошибка: {value1}').format(
                    value1=html.escape(status_data['api_error'][:100])
                )
            )

        message = '\n'.join(message_parts)

        await callback.message.answer(message, parse_mode='HTML')

    except Exception as e:
        await callback.message.answer(
            texts.t('ADMIN_MAINTENANCE_055', '❌ Ошибка проверки статуса: {value1!s}').format(value1=e)
        )


@admin_required
@error_handler
async def send_manual_notification(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await state.set_state(MaintenanceStates.waiting_for_notification_message)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_MAINTENANCE_017', '🟢 Онлайн'), callback_data='manual_notify_online'
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_MAINTENANCE_018', '🔴 Офлайн'), callback_data='manual_notify_offline'
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_MAINTENANCE_019', '🟡 Проблемы'), callback_data='manual_notify_degraded'
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_MAINTENANCE_020', '🔧 Обслуживание'), callback_data='manual_notify_maintenance'
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_MAINTENANCE_059', '❌ Отмена'), callback_data='maintenance_panel'
                )
            ],
        ]
    )

    await callback.message.edit_text(
        texts.t('ADMIN_MAINTENANCE_038', '📢 <b>Ручная отправка уведомления</b>\n\nВыберите статус для уведомления:'),
        reply_markup=keyboard,
    )


@admin_required
@error_handler
async def handle_manual_notification(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    status_map = {
        'manual_notify_online': 'online',
        'manual_notify_offline': 'offline',
        'manual_notify_degraded': 'degraded',
        'manual_notify_maintenance': 'maintenance',
    }

    status = status_map.get(callback.data)
    if not status:
        await callback.answer(texts.t('ADMIN_MAINTENANCE_048', 'Неизвестный статус'))
        return

    await state.update_data(notification_status=status)

    status_names = {
        'online': texts.t('ADMIN_MAINTENANCE_017', '🟢 Онлайн'),
        'offline': texts.t('ADMIN_MAINTENANCE_018', '🔴 Офлайн'),
        'degraded': texts.t('ADMIN_MAINTENANCE_019', '🟡 Проблемы'),
        'maintenance': texts.t('ADMIN_MAINTENANCE_020', '🔧 Обслуживание'),
    }

    await callback.message.edit_text(
        texts.t(
            'ADMIN_MAINTENANCE_039',
            '📢 <b>Отправка уведомления: {value1}</b>\n\nВведите сообщение для уведомления или отправьте /skip для отправки без дополнительного текста:',
        ).format(value1=status_names[status]),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_MAINTENANCE_059', '❌ Отмена'), callback_data='maintenance_panel'
                    )
                ]
            ]
        ),
    )


@admin_required
@error_handler
async def process_notification_message(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    current_state = await state.get_state()

    if current_state != MaintenanceStates.waiting_for_notification_message:
        return

    data = await state.get_data()
    status = data.get('notification_status')

    if not status:
        await message.answer(texts.t('ADMIN_MAINTENANCE_049', 'Ошибка: статус не выбран'))
        await state.clear()
        return

    notification_message = ''
    if message.text and message.text != '/skip':
        notification_message = message.text[:300]

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()

        success = await rw_service.send_manual_status_notification(message.bot, status, notification_message)

        if success:
            await message.answer(texts.t('ADMIN_MAINTENANCE_056', '✅ Уведомление отправлено'))
        else:
            await message.answer(texts.t('ADMIN_MAINTENANCE_057', '❌ Ошибка отправки уведомления'))

    except Exception as e:
        logger.error('Ошибка отправки ручного уведомления', error=e)
        await message.answer(texts.t('ADMIN_MAINTENANCE_058', '❌ Ошибка: {value1!s}').format(value1=e))

    await state.clear()

    await message.answer(
        texts.t('ADMIN_MAINTENANCE_040', 'Вернуться к панели техработ:'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_MAINTENANCE_060', '🔧 Панель техработ'), callback_data='maintenance_panel'
                    )
                ]
            ]
        ),
    )


@admin_required
@error_handler
async def back_to_admin_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(texts.ADMIN_PANEL, reply_markup=get_admin_main_keyboard(db_user.language))
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_maintenance_panel, F.data == 'maintenance_panel')

    dp.callback_query.register(toggle_maintenance_mode, F.data == 'maintenance_toggle')

    dp.callback_query.register(toggle_monitoring, F.data == 'maintenance_monitoring')

    dp.callback_query.register(force_api_check, F.data == 'maintenance_check_api')

    dp.callback_query.register(check_panel_status, F.data == 'maintenance_check_panel')

    dp.callback_query.register(send_manual_notification, F.data == 'maintenance_manual_notify')

    dp.callback_query.register(handle_manual_notification, F.data.startswith('manual_notify_'))

    dp.callback_query.register(back_to_admin_panel, F.data == 'admin_panel')

    dp.message.register(process_maintenance_reason, MaintenanceStates.waiting_for_reason)

    dp.message.register(process_notification_message, MaintenanceStates.waiting_for_notification_message)
