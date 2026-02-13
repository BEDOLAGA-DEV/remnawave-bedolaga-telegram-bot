import logging

from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.admin import get_admin_main_keyboard, get_maintenance_keyboard
from app.localization.texts import get_texts
from app.services.maintenance_service import maintenance_service
from app.utils.decorators import admin_required, error_handler


logger = logging.getLogger(__name__)


class MaintenanceStates(StatesGroup):
    waiting_for_reason = State()
    waiting_for_notification_message = State()


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
        logger.error(f'Ошибка получения статуса панели: {e}')
        panel_status = {'description': texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_UNKNOWN'), 'has_issues': True}

    status_emoji = '🔧' if status_info['is_active'] else '✅'
    status_text = (
        texts.t('ADMIN_MAINTENANCE_STATUS_ENABLED')
        if status_info['is_active']
        else texts.t('ADMIN_MAINTENANCE_STATUS_DISABLED')
    )

    api_emoji = '✅' if status_info['api_status'] else '❌'
    api_text = (
        texts.t('ADMIN_MAINTENANCE_API_AVAILABLE')
        if status_info['api_status']
        else texts.t('ADMIN_MAINTENANCE_API_UNAVAILABLE')
    )

    monitoring_emoji = '🔄' if status_info['monitoring_active'] else '⏹️'
    monitoring_text = (
        texts.t('ADMIN_MAINTENANCE_MONITORING_RUNNING')
        if status_info['monitoring_active']
        else texts.t('ADMIN_MAINTENANCE_MONITORING_STOPPED')
    )

    enabled_info = ''
    if status_info['is_active'] and status_info['enabled_at']:
        enabled_time = status_info['enabled_at'].strftime('%d.%m.%Y %H:%M:%S')
        enabled_info = texts.t('ADMIN_MAINTENANCE_ENABLED_AT_LINE').format(time=enabled_time)
        if status_info['reason']:
            enabled_info += texts.t('ADMIN_MAINTENANCE_REASON_LINE').format(reason=status_info['reason'])

    last_check_info = ''
    if status_info['last_check']:
        last_check_time = status_info['last_check'].strftime('%H:%M:%S')
        last_check_info = texts.t('ADMIN_MAINTENANCE_LAST_CHECK_LINE').format(time=last_check_time)

    failures_info = ''
    if status_info['consecutive_failures'] > 0:
        failures_info = texts.t('ADMIN_MAINTENANCE_FAILURES_LINE').format(count=status_info['consecutive_failures'])

    panel_info = texts.t('ADMIN_MAINTENANCE_PANEL_INFO_LINE').format(description=panel_status['description'])
    if panel_status.get('response_time'):
        panel_info += texts.t('ADMIN_MAINTENANCE_RESPONSE_TIME_LINE').format(seconds=panel_status['response_time'])

    message_text = f"""
{texts.t('ADMIN_MAINTENANCE_TITLE')}

{status_emoji} <b>{texts.t('ADMIN_MAINTENANCE_MODE_LABEL')}:</b> {status_text}
{api_emoji} <b>{texts.t('ADMIN_MAINTENANCE_API_LABEL')}:</b> {api_text}
{monitoring_emoji} <b>{texts.t('ADMIN_MAINTENANCE_MONITORING_LABEL')}:</b> {monitoring_text}
🛠️ <b>{texts.t('ADMIN_MAINTENANCE_MONITORING_AUTOSTART_LABEL')}:</b> {texts.t('ADMIN_MAINTENANCE_STATUS_ENABLED') if status_info['monitoring_configured'] else texts.t('ADMIN_MAINTENANCE_STATUS_DISABLED')}
⏱️ <b>{texts.t('ADMIN_MAINTENANCE_CHECK_INTERVAL_LABEL')}:</b> {status_info['check_interval']}{texts.t('ADMIN_MAINTENANCE_SECONDS_SUFFIX')}
🤖 <b>{texts.t('ADMIN_MAINTENANCE_AUTO_ENABLE_LABEL')}:</b> {texts.t('ADMIN_MAINTENANCE_AUTO_ENABLE_ON') if status_info['auto_enable_configured'] else texts.t('ADMIN_MAINTENANCE_AUTO_ENABLE_OFF')}
{panel_info}
{enabled_info}
{last_check_info}
{failures_info}

ℹ️ <i>{texts.t('ADMIN_MAINTENANCE_INFO_NOTE')}</i>
"""

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
            await callback.answer(texts.t('ADMIN_MAINTENANCE_DISABLED_ALERT'), show_alert=True)
        else:
            await callback.answer(texts.t('ADMIN_MAINTENANCE_DISABLE_ERROR_ALERT'), show_alert=True)
    else:
        await state.set_state(MaintenanceStates.waiting_for_reason)
        await callback.message.edit_text(
            texts.t('ADMIN_MAINTENANCE_ENABLE_PROMPT'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL'), callback_data='maintenance_panel')]
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
        response_text = texts.t('ADMIN_MAINTENANCE_ENABLED')
        if reason:
            response_text += texts.t('ADMIN_MAINTENANCE_REASON_LINE_PLAIN').format(reason=reason)
    else:
        response_text = texts.t('ADMIN_MAINTENANCE_ENABLE_ERROR')

    await message.answer(response_text)
    await state.clear()

    maintenance_service.get_status_info()
    await message.answer(
        texts.t('ADMIN_MAINTENANCE_BACK_TO_PANEL_PROMPT'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_MAINTENANCE_PANEL_BUTTON'), callback_data='maintenance_panel'
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
            texts.t('ADMIN_MAINTENANCE_MONITORING_STOPPED_ALERT')
            if success
            else texts.t('ADMIN_MAINTENANCE_MONITORING_STOP_ERROR_ALERT')
        )
    else:
        success = await maintenance_service.start_monitoring()
        message = (
            texts.t('ADMIN_MAINTENANCE_MONITORING_STARTED_ALERT')
            if success
            else texts.t('ADMIN_MAINTENANCE_MONITORING_START_ERROR_ALERT')
        )

    await callback.answer(message, show_alert=True)

    await show_maintenance_panel(callback, db_user, db, None)


@admin_required
@error_handler
async def force_api_check(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('ADMIN_MAINTENANCE_API_CHECKING_ALERT'), show_alert=False)

    check_result = await maintenance_service.force_api_check()

    if check_result['success']:
        status_text = (
            texts.t('ADMIN_MAINTENANCE_API_AVAILABLE_PLAIN')
            if check_result['api_available']
            else texts.t('ADMIN_MAINTENANCE_API_UNAVAILABLE_PLAIN')
        )
        message = texts.t('ADMIN_MAINTENANCE_API_CHECK_RESULT').format(
            status=status_text, response_time=check_result['response_time']
        )
    else:
        message = texts.t('ADMIN_MAINTENANCE_API_CHECK_ERROR').format(
            error=check_result.get('error', texts.t('ADMIN_MAINTENANCE_UNKNOWN_ERROR'))
        )

    await callback.message.answer(message)

    await show_maintenance_panel(callback, db_user, db, None)


@admin_required
@error_handler
async def check_panel_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('ADMIN_MAINTENANCE_PANEL_CHECKING_ALERT'), show_alert=False)

    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()

        status_data = await rw_service.check_panel_health()

        status_text = {
            'online': texts.t('ADMIN_MAINTENANCE_PANEL_ONLINE'),
            'offline': texts.t('ADMIN_MAINTENANCE_PANEL_OFFLINE'),
            'degraded': texts.t('ADMIN_MAINTENANCE_PANEL_DEGRADED'),
        }.get(status_data['status'], texts.t('ADMIN_MAINTENANCE_PANEL_UNKNOWN'))

        message_parts = [
            texts.t('ADMIN_MAINTENANCE_PANEL_STATUS_TITLE'),
            f'{status_text}',
            texts.t('ADMIN_MAINTENANCE_RESPONSE_TIME_SHORT').format(seconds=status_data.get('response_time', 0)),
            texts.t('ADMIN_MAINTENANCE_USERS_ONLINE_LINE').format(count=status_data.get('users_online', 0)),
            texts.t('ADMIN_MAINTENANCE_NODES_ONLINE_LINE').format(
                online=status_data.get('nodes_online', 0), total=status_data.get('total_nodes', 0)
            ),
        ]

        attempts_used = status_data.get('attempts_used')
        if attempts_used:
            message_parts.append(texts.t('ADMIN_MAINTENANCE_ATTEMPTS_USED_LINE').format(count=attempts_used))

        if status_data.get('api_error'):
            message_parts.append(texts.t('ADMIN_MAINTENANCE_ERROR_LINE').format(error=status_data['api_error'][:100]))

        message = '\n'.join(message_parts)

        await callback.message.answer(message, parse_mode='HTML')

    except Exception as e:
        await callback.message.answer(texts.t('ADMIN_MAINTENANCE_STATUS_CHECK_ERROR').format(error=str(e)))


@admin_required
@error_handler
async def send_manual_notification(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await state.set_state(MaintenanceStates.waiting_for_notification_message)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_MAINTENANCE_NOTIFY_ONLINE_BUTTON'), callback_data='manual_notify_online'
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_MAINTENANCE_NOTIFY_OFFLINE_BUTTON'), callback_data='manual_notify_offline'
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_MAINTENANCE_NOTIFY_DEGRADED_BUTTON'), callback_data='manual_notify_degraded'
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_MAINTENANCE_NOTIFY_MAINTENANCE_BUTTON'),
                    callback_data='manual_notify_maintenance',
                ),
            ],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL'), callback_data='maintenance_panel')],
        ]
    )

    await callback.message.edit_text(
        texts.t('ADMIN_MAINTENANCE_MANUAL_NOTIFY_PROMPT'),
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
        await callback.answer(texts.t('ADMIN_MAINTENANCE_UNKNOWN_STATUS'))
        return

    await state.update_data(notification_status=status)

    status_names = {
        'online': texts.t('ADMIN_MAINTENANCE_NOTIFY_ONLINE_BUTTON'),
        'offline': texts.t('ADMIN_MAINTENANCE_NOTIFY_OFFLINE_BUTTON'),
        'degraded': texts.t('ADMIN_MAINTENANCE_NOTIFY_DEGRADED_BUTTON'),
        'maintenance': texts.t('ADMIN_MAINTENANCE_NOTIFY_MAINTENANCE_BUTTON'),
    }

    await callback.message.edit_text(
        texts.t('ADMIN_MAINTENANCE_MANUAL_NOTIFY_MESSAGE_PROMPT').format(status=status_names[status]),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('ADMIN_CANCEL'), callback_data='maintenance_panel')]
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
        await message.answer(texts.t('ADMIN_MAINTENANCE_NOTIFY_STATUS_MISSING'))
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
            await message.answer(texts.t('ADMIN_MAINTENANCE_NOTIFY_SENT'))
        else:
            await message.answer(texts.t('ADMIN_MAINTENANCE_NOTIFY_SEND_ERROR'))

    except Exception as e:
        logger.error(f'Ошибка отправки ручного уведомления: {e}')
        await message.answer(texts.t('ADMIN_MAINTENANCE_ERROR_GENERIC').format(error=str(e)))

    await state.clear()

    await message.answer(
        texts.t('ADMIN_MAINTENANCE_BACK_TO_PANEL_SHORT'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_MAINTENANCE_PANEL_BUTTON'), callback_data='maintenance_panel'
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
