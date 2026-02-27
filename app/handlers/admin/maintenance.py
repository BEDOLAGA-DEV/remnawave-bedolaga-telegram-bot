import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.admin import get_admin_main_keyboard, get_maintenance_keyboard
from app.localization.texts import get_texts
from app.services.maintenance_service import maintenance_service
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


class MaintenanceStates(StatesGroup):
    waiting_for_reason = State()
    waiting_for_notification_message = State()


def _manual_notification_status_names(texts) -> dict[str, str]:
    return {
        'online': texts.t('ADMIN_MAINTENANCE_NOTIFY_STATUS_ONLINE', '🟢 Онлайн'),
        'offline': texts.t('ADMIN_MAINTENANCE_NOTIFY_STATUS_OFFLINE', '🔴 Офлайн'),
        'degraded': texts.t('ADMIN_MAINTENANCE_NOTIFY_STATUS_DEGRADED', '🟡 Проблемы'),
        'maintenance': texts.t('ADMIN_BOTCFG_GROUP_MAINTENANCE_TITLE', '🔧 Обслуживание'),
    }


@admin_required
@error_handler
async def show_maintenance_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)

    status_info = maintenance_service.get_status_info()

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()
        panel_status = await rw_service.get_panel_status_summary()
    except Exception as e:
        logger.error('Ошибка получения статуса панели', error=e)
        panel_status = {
            'description': texts.t('ADMIN_MAINTENANCE_PANEL_CHECK_UNKNOWN', '❓ Не удалось проверить'),
            'has_issues': True,
        }

    status_emoji = '🔧' if status_info['is_active'] else '✅'
    status_text = (
        texts.t('ADMIN_MAINTENANCE_STATUS_ACTIVE', 'Включен')
        if status_info['is_active']
        else texts.t('ADMIN_MAINTENANCE_STATUS_INACTIVE', 'Выключен')
    )

    api_emoji = '✅' if status_info['api_status'] else '❌'
    api_text = (
        texts.t('ADMIN_MAINTENANCE_API_AVAILABLE', 'Доступно')
        if status_info['api_status']
        else texts.t('ADMIN_MAINTENANCE_API_UNAVAILABLE', 'Недоступно')
    )

    monitoring_emoji = '🔄' if status_info['monitoring_active'] else '⏹️'
    monitoring_text = (
        texts.t('ADMIN_MAINTENANCE_MONITORING_ACTIVE', 'Запущен')
        if status_info['monitoring_active']
        else texts.t('ADMIN_MAINTENANCE_MONITORING_INACTIVE', 'Остановлен')
    )
    monitoring_config_text = (
        texts.t('ADMIN_MAINTENANCE_STATUS_ACTIVE', 'Включен')
        if status_info['monitoring_configured']
        else texts.t('ADMIN_MAINTENANCE_MONITORING_AUTOSTART_DISABLED', 'Отключен')
    )
    auto_enable_text = (
        texts.t('ADMIN_MAINTENANCE_AUTO_ENABLE_ENABLED', 'Включено')
        if status_info['auto_enable_configured']
        else texts.t('ADMIN_PROMO_OFFER_LOGS_ACTION_DISABLED', 'Отключено')
    )

    enabled_info = ''
    if status_info['is_active'] and status_info['enabled_at']:
        enabled_time = status_info['enabled_at'].strftime('%d.%m.%Y %H:%M:%S')
        enabled_info = texts.t(
            'ADMIN_MAINTENANCE_ENABLED_AT_LINE',
            '\n📅 <b>Включен:</b> {timestamp}',
        ).format(timestamp=enabled_time)
        if status_info['reason']:
            enabled_info += texts.t(
                'ADMIN_MAINTENANCE_REASON_LINE',
                '\n📝 <b>Причина:</b> {reason}',
            ).format(reason=status_info['reason'])

    last_check_info = ''
    if status_info['last_check']:
        last_check_time = status_info['last_check'].strftime('%H:%M:%S')
        last_check_info = texts.t(
            'ADMIN_MAINTENANCE_LAST_CHECK_LINE',
            '\n🕐 <b>Последняя проверка:</b> {time}',
        ).format(time=last_check_time)

    failures_info = ''
    if status_info['consecutive_failures'] > 0:
        failures_info = texts.t(
            'ADMIN_MAINTENANCE_FAILURES_LINE',
            '\n⚠️ <b>Неудачных проверок подряд:</b> {count}',
        ).format(count=status_info['consecutive_failures'])

    panel_info = texts.t(
        'ADMIN_MAINTENANCE_PANEL_LINE',
        '\n🌐 <b>Панель Remnawave:</b> {description}',
    ).format(description=panel_status['description'])
    if panel_status.get('response_time'):
        panel_info += texts.t(
            'ADMIN_MAINTENANCE_RESPONSE_TIME_LINE',
            '\n⚡ <b>Время отклика:</b> {seconds}с',
        ).format(seconds=panel_status['response_time'])

    message_text = texts.t(
        'ADMIN_MAINTENANCE_PANEL_TEXT',
        '🔧 <b>Управление техническими работами</b>\n\n'
        '{status_emoji} <b>Режим техработ:</b> {status_text}\n'
        '{api_emoji} <b>API Remnawave:</b> {api_text}\n'
        '{monitoring_emoji} <b>Мониторинг:</b> {monitoring_text}\n'
        '🛠️ <b>Автозапуск мониторинга:</b> {monitoring_config_text}\n'
        '⏱️ <b>Интервал проверки:</b> {check_interval}с\n'
        '🤖 <b>Автовключение:</b> {auto_enable_text}\n'
        '{panel_info}{enabled_info}{last_check_info}{failures_info}\n\n'
        'ℹ️ <i>В режиме техработ обычные пользователи не могут использовать бота. '
        'Администраторы имеют полный доступ.</i>',
    ).format(
        status_emoji=status_emoji,
        status_text=status_text,
        api_emoji=api_emoji,
        api_text=api_text,
        monitoring_emoji=monitoring_emoji,
        monitoring_text=monitoring_text,
        monitoring_config_text=monitoring_config_text,
        check_interval=status_info['check_interval'],
        auto_enable_text=auto_enable_text,
        panel_info=panel_info,
        enabled_info=enabled_info,
        last_check_info=last_check_info,
        failures_info=failures_info,
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
            await callback.answer(
                texts.t('ADMIN_MAINTENANCE_DISABLED_ALERT', 'Режим техработ выключен'),
                show_alert=True,
            )
        else:
            await callback.answer(
                texts.t('ADMIN_MAINTENANCE_DISABLE_ERROR_ALERT', 'Ошибка выключения режима техработ'),
                show_alert=True,
            )
    else:
        await state.set_state(MaintenanceStates.waiting_for_reason)
        await callback.message.edit_text(
            texts.t(
                'ADMIN_MAINTENANCE_ENABLE_PROMPT',
                '🔧 <b>Включение режима техработ</b>\n\n'
                'Введите причину включения техработ или отправьте /skip для пропуска:',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='maintenance_panel')]
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
        response_text = texts.t('ADMIN_MAINTENANCE_ENABLED_ALERT', 'Режим техработ включен')
        if reason:
            response_text += texts.t('ADMIN_MAINTENANCE_REASON_APPEND', '\nПричина: {reason}').format(reason=reason)
    else:
        response_text = texts.t('ADMIN_MAINTENANCE_ENABLE_ERROR_ALERT', 'Ошибка включения режима техработ')

    await message.answer(response_text)
    await state.clear()

    maintenance_service.get_status_info()
    await message.answer(
        texts.t('ADMIN_MAINTENANCE_BACK_TO_PANEL_PROMPT', 'Вернуться к панели управления техработами:'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_MAINTENANCE_PANEL_BUTTON', '🔧 Панель техработ'),
                        callback_data='maintenance_panel',
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
            texts.t('ADMIN_MAINTENANCE_MONITORING_STOPPED_ALERT', 'Мониторинг остановлен')
            if success
            else texts.t('ADMIN_MAINTENANCE_MONITORING_STOP_ERROR_ALERT', 'Ошибка остановки мониторинга')
        )
    else:
        success = await maintenance_service.start_monitoring()
        message = (
            texts.t('ADMIN_MAINTENANCE_MONITORING_STARTED_ALERT', 'Мониторинг запущен')
            if success
            else texts.t('ADMIN_MAINTENANCE_MONITORING_START_ERROR_ALERT', 'Ошибка запуска мониторинга')
        )

    await callback.answer(message, show_alert=True)

    await show_maintenance_panel(callback, db_user, db, None)


@admin_required
@error_handler
async def force_api_check(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('ADMIN_MAINTENANCE_API_CHECK_IN_PROGRESS', 'Проверка API...'), show_alert=False)

    check_result = await maintenance_service.force_api_check()

    if check_result['success']:
        status_text = (
            texts.t('ADMIN_MAINTENANCE_API_STATUS_AVAILABLE_SHORT', 'доступно')
            if check_result['api_available']
            else texts.t('ADMIN_MAINTENANCE_API_STATUS_UNAVAILABLE_SHORT', 'недоступно')
        )
        message = texts.t(
            'ADMIN_MAINTENANCE_API_CHECK_RESULT',
            'API {status}\nВремя ответа: {response_time}с',
        ).format(
            status=status_text,
            response_time=check_result['response_time'],
        )
    else:
        message = texts.t(
            'ADMIN_MAINTENANCE_API_CHECK_ERROR',
            'Ошибка проверки: {error}',
        ).format(
            error=check_result.get('error', texts.t('ADMIN_MAINTENANCE_UNKNOWN_ERROR', 'Неизвестная ошибка'))
        )

    await callback.message.answer(message)

    await show_maintenance_panel(callback, db_user, db, None)


@admin_required
@error_handler
async def check_panel_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('ADMIN_MAINTENANCE_PANEL_CHECK_IN_PROGRESS', 'Проверка статуса панели...'), show_alert=False)

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()

        status_data = await rw_service.check_panel_health()

        status_text = {
            'online': texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_ONLINE', '🟢 Панель работает нормально'),
            'offline': texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_OFFLINE', '🔴 Панель недоступна'),
            'degraded': texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_DEGRADED', '🟡 Панель работает со сбоями'),
        }.get(status_data['status'], texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_UNKNOWN', '❓ Статус неизвестен'))

        message_parts = [
            texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_HEADER', '🌐 <b>Статус панели Remnawave</b>\n'),
            status_text,
            texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_RESPONSE_TIME', '⚡ Время отклика: {seconds}с').format(
                seconds=status_data.get('response_time', 0)
            ),
            texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_USERS_ONLINE', '👥 Пользователей онлайн: {count}').format(
                count=status_data.get('users_online', 0)
            ),
            texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_NODES_ONLINE', '🖥️ Нод онлайн: {online}/{total}').format(
                online=status_data.get('nodes_online', 0),
                total=status_data.get('total_nodes', 0),
            ),
        ]

        attempts_used = status_data.get('attempts_used')
        if attempts_used:
            message_parts.append(
                texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_ATTEMPTS', '🔁 Попыток проверки: {count}').format(
                    count=attempts_used
                )
            )

        if status_data.get('api_error'):
            message_parts.append(
                texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_ERROR', '❌ Ошибка: {error}').format(
                    error=status_data['api_error'][:100]
                )
            )

        message = '\n'.join(message_parts)

        await callback.message.answer(message, parse_mode='HTML')

    except Exception as e:
        await callback.message.answer(
            texts.t(
                'ADMIN_MAINTENANCE_PANEL_STATUS_CHECK_ERROR',
                '❌ Ошибка проверки статуса: {error}',
            ).format(error=e)
        )


@admin_required
@error_handler
async def send_manual_notification(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await state.set_state(MaintenanceStates.waiting_for_notification_message)
    status_names = _manual_notification_status_names(texts)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text=status_names['online'], callback_data='manual_notify_online'),
                types.InlineKeyboardButton(text=status_names['offline'], callback_data='manual_notify_offline'),
            ],
            [
                types.InlineKeyboardButton(text=status_names['degraded'], callback_data='manual_notify_degraded'),
                types.InlineKeyboardButton(text=status_names['maintenance'], callback_data='manual_notify_maintenance'),
            ],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='maintenance_panel')],
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_MAINTENANCE_MANUAL_NOTIFY_SELECT_STATUS',
            '📢 <b>Ручная отправка уведомления</b>\n\nВыберите статус для уведомления:',
        ),
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
        await callback.answer(texts.t('ADMIN_MAINTENANCE_UNKNOWN_STATUS', 'Неизвестный статус'))
        return

    await state.update_data(notification_status=status)

    status_names = _manual_notification_status_names(texts)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_MAINTENANCE_MANUAL_NOTIFY_ENTER_MESSAGE',
            '📢 <b>Отправка уведомления: {status_name}</b>\n\n'
            'Введите сообщение для уведомления или отправьте /skip для отправки без дополнительного текста:',
        ).format(status_name=status_names[status]),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL', '❌ Отмена'), callback_data='maintenance_panel')]
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
        await message.answer(texts.t('ADMIN_MAINTENANCE_NOTIFY_STATUS_NOT_SELECTED', 'Ошибка: статус не выбран'))
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
            await message.answer(texts.t('ADMIN_MAINTENANCE_NOTIFY_SENT', '✅ Уведомление отправлено'))
        else:
            await message.answer(texts.t('ADMIN_MAINTENANCE_NOTIFY_SEND_ERROR', '❌ Ошибка отправки уведомления'))

    except Exception as e:
        logger.error('Ошибка отправки ручного уведомления', error=e)
        await message.answer(
            texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_ERROR', '❌ Ошибка: {error}').format(error=e)
        )

    await state.clear()

    await message.answer(
        texts.t('ADMIN_MAINTENANCE_BACK_TO_PANEL_SHORT_PROMPT', 'Вернуться к панели техработ:'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_MAINTENANCE_PANEL_BUTTON', '🔧 Панель техработ'),
                        callback_data='maintenance_panel',
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
