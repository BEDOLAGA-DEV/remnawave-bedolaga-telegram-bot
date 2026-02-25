import asyncio
from datetime import datetime, timedelta

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.keyboards.admin import get_monitoring_keyboard
from app.localization.texts import get_texts
from app.services.monitoring_service import monitoring_service
from app.services.nalogo_queue_service import nalogo_queue_service
from app.services.notification_settings_service import NotificationSettingsService
from app.services.traffic_monitoring_service import (
    traffic_monitoring_scheduler,
)
from app.states import AdminStates
from app.utils.decorators import admin_required
from app.utils.pagination import paginate_list


logger = structlog.get_logger(__name__)
router = Router()


def _format_toggle(enabled: bool, texts) -> str:
    return (
        texts.t('ADMIN_MONITORING_STATUS_SHORT_ON', '🟢 Вкл')
        if enabled
        else texts.t('ADMIN_MONITORING_STATUS_SHORT_OFF', '🔴 Выкл')
    )


def _build_notification_settings_view(language: str):
    texts = get_texts(language)
    config = NotificationSettingsService.get_config()

    second_percent = NotificationSettingsService.get_second_wave_discount_percent()
    second_hours = NotificationSettingsService.get_second_wave_valid_hours()
    third_percent = NotificationSettingsService.get_third_wave_discount_percent()
    third_hours = NotificationSettingsService.get_third_wave_valid_hours()
    third_days = NotificationSettingsService.get_third_wave_trigger_days()

    trial_channel_status = _format_toggle(config.get('trial_channel_unsubscribed', {}).get('enabled', True), texts)
    expired_1d_status = _format_toggle(config['expired_1d'].get('enabled', True), texts)
    second_wave_status = _format_toggle(config['expired_second_wave'].get('enabled', True), texts)
    third_wave_status = _format_toggle(config['expired_third_wave'].get('enabled', True), texts)

    summary_text = texts.t(
        'ADMIN_MONITORING_NOTIFY_SETTINGS_TEXT',
        '🔔 <b>Уведомления пользователям</b>\n\n'
        '• Отписка от канала: {trial_channel_status}\n'
        '• 1 день после истечения: {expired_1d_status}\n'
        '• 2-3 дня (скидка {second_percent}% / {second_hours} ч): {second_wave_status}\n'
        '• {third_days} дней (скидка {third_percent}% / {third_hours} ч): {third_wave_status}',
    ).format(
        trial_channel_status=trial_channel_status,
        expired_1d_status=expired_1d_status,
        second_percent=second_percent,
        second_hours=second_hours,
        second_wave_status=second_wave_status,
        third_days=third_days,
        third_percent=third_percent,
        third_hours=third_hours,
        third_wave_status=third_wave_status,
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_TOGGLE_TRIAL_CHANNEL_BUTTON', '{status} • Отписка от канала').format(
                        status=trial_channel_status
                    ),
                    callback_data='admin_mon_notify_toggle_trial_channel',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_TEST_TRIAL_CHANNEL_BUTTON', '🧪 Тест: отписка от канала'),
                    callback_data='admin_mon_notify_preview_trial_channel',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_TOGGLE_EXPIRED_1D_BUTTON', '{status} • 1 день после истечения').format(
                        status=expired_1d_status
                    ),
                    callback_data='admin_mon_notify_toggle_expired_1d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_TEST_EXPIRED_1D_BUTTON', '🧪 Тест: 1 день после истечения'),
                    callback_data='admin_mon_notify_preview_expired_1d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_TOGGLE_EXPIRED_2D_BUTTON', '{status} • 2-3 дня со скидкой').format(
                        status=second_wave_status
                    ),
                    callback_data='admin_mon_notify_toggle_expired_2d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_TEST_EXPIRED_2D_BUTTON', '🧪 Тест: скидка 2-3 день'),
                    callback_data='admin_mon_notify_preview_expired_2d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_EDIT_2D_PERCENT_BUTTON', '✏️ Скидка 2-3 дня: {percent}%').format(
                        percent=second_percent
                    ),
                    callback_data='admin_mon_notify_edit_2d_percent',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_EDIT_2D_HOURS_BUTTON', '⏱️ Срок скидки 2-3 дня: {hours} ч').format(
                        hours=second_hours
                    ),
                    callback_data='admin_mon_notify_edit_2d_hours',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_TOGGLE_EXPIRED_ND_BUTTON', '{status} • {days} дней со скидкой').format(
                        status=third_wave_status,
                        days=third_days,
                    ),
                    callback_data='admin_mon_notify_toggle_expired_nd',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_TEST_EXPIRED_ND_BUTTON', '🧪 Тест: скидка спустя дни'),
                    callback_data='admin_mon_notify_preview_expired_nd',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_EDIT_ND_PERCENT_BUTTON', '✏️ Скидка {days} дней: {percent}%').format(
                        days=third_days,
                        percent=third_percent,
                    ),
                    callback_data='admin_mon_notify_edit_nd_percent',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_EDIT_ND_HOURS_BUTTON', '⏱️ Срок скидки {days} дней: {hours} ч').format(
                        days=third_days,
                        hours=third_hours,
                    ),
                    callback_data='admin_mon_notify_edit_nd_hours',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_EDIT_ND_THRESHOLD_BUTTON', '📆 Порог уведомления: {days} дн.').format(
                        days=third_days
                    ),
                    callback_data='admin_mon_notify_edit_nd_threshold',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NOTIFY_SEND_ALL_TESTS_BUTTON', '🧪 Отправить все тесты'),
                    callback_data='admin_mon_notify_preview_all',
                )
            ],
            [InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_mon_settings')],
        ]
    )

    return summary_text, keyboard


async def _build_notification_preview_message(language: str, notification_type: str):
    texts = get_texts(language)
    now = datetime.now()
    price_30_days = settings.format_price(settings.PRICE_30_DAYS)

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    header = texts.t('ADMIN_MONITORING_NOTIFY_PREVIEW_HEADER', '🧪 <b>Тестовое уведомление мониторинга</b>\n\n')

    if notification_type == 'trial_channel_unsubscribed':
        template = texts.get(
            'TRIAL_CHANNEL_UNSUBSCRIBED',
            (
                '🚫 <b>Доступ приостановлен</b>\n\n'
                'Мы не нашли вашу подписку на наш канал, поэтому тестовая подписка отключена.\n\n'
                'Подпишитесь на канал и нажмите «{check_button}», чтобы вернуть доступ.'
            ),
        )
        check_button = texts.t('CHANNEL_CHECK_BUTTON', '✅ Я подписался')
        message = template.format(check_button=check_button)
        # Use all required channels for the preview keyboard
        required_channels = await channel_subscription_service.get_required_channels()
        keyboard = get_channel_sub_keyboard(required_channels, language=language)
    elif notification_type == 'expired_1d':
        template = texts.get(
            'SUBSCRIPTION_EXPIRED_1D',
            (
                '⛔ <b>Подписка закончилась</b>\n\n'
                'Доступ был отключён {end_date}. Продлите подписку, чтобы вернуться в сервис.'
            ),
        )
        message = template.format(
            end_date=(now - timedelta(days=1)).strftime('%d.%m.%Y %H:%M'),
            price=price_30_days,
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON', '🆘 Поддержка'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    elif notification_type == 'expired_2d':
        percent = NotificationSettingsService.get_second_wave_discount_percent()
        valid_hours = NotificationSettingsService.get_second_wave_valid_hours()
        template = texts.get(
            'SUBSCRIPTION_EXPIRED_SECOND_WAVE',
            (
                '🔥 <b>Скидка {percent}% на продление</b>\n\n'
                'Активируйте предложение, чтобы получить дополнительную скидку. '
                'Она суммируется с вашей промогруппой и действует до {expires_at}.'
            ),
        )
        message = template.format(
            percent=percent,
            expires_at=(now + timedelta(hours=valid_hours)).strftime('%d.%m.%Y %H:%M'),
            trigger_days=3,
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_NOTIFY_CLAIM_DISCOUNT_BUTTON', '🎁 Получить скидку'),
                        callback_data='claim_discount_preview',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON', '🆘 Поддержка'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    elif notification_type == 'expired_nd':
        percent = NotificationSettingsService.get_third_wave_discount_percent()
        valid_hours = NotificationSettingsService.get_third_wave_valid_hours()
        trigger_days = NotificationSettingsService.get_third_wave_trigger_days()
        template = texts.get(
            'SUBSCRIPTION_EXPIRED_THIRD_WAVE',
            (
                '🎁 <b>Индивидуальная скидка {percent}%</b>\n\n'
                'Прошло {trigger_days} дней без подписки — возвращайтесь и активируйте дополнительную скидку. '
                'Она суммируется с промогруппой и действует до {expires_at}.'
            ),
        )
        message = template.format(
            percent=percent,
            trigger_days=trigger_days,
            expires_at=(now + timedelta(hours=valid_hours)).strftime('%d.%m.%Y %H:%M'),
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_NOTIFY_CLAIM_DISCOUNT_BUTTON', '🎁 Получить скидку'),
                        callback_data='claim_discount_preview',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON', '🆘 Поддержка'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    else:
        raise ValueError(f'Unsupported notification type: {notification_type}')

    footer = texts.t(
        'ADMIN_MONITORING_NOTIFY_PREVIEW_FOOTER',
        '\n\n<i>Сообщение отправлено только вам для проверки оформления.</i>',
    )
    return header + message + footer, keyboard


async def _send_notification_preview(bot, chat_id: int, language: str, notification_type: str) -> None:
    message, keyboard = await _build_notification_preview_message(language, notification_type)
    await bot.send_message(
        chat_id,
        message,
        parse_mode='HTML',
        reply_markup=keyboard,
    )


async def _render_notification_settings(callback: CallbackQuery) -> None:
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    text, keyboard = _build_notification_settings_view(language)
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)


async def _render_notification_settings_for_state(
    bot,
    chat_id: int,
    message_id: int,
    language: str,
    business_connection_id: str | None = None,
) -> None:
    text, keyboard = _build_notification_settings_view(language)

    edit_kwargs = {
        'text': text,
        'chat_id': chat_id,
        'message_id': message_id,
        'parse_mode': 'HTML',
        'reply_markup': keyboard,
    }

    if business_connection_id:
        edit_kwargs['business_connection_id'] = business_connection_id

    try:
        await bot.edit_message_text(**edit_kwargs)
    except TelegramBadRequest as exc:
        if 'no text in the message to edit' in (exc.message or '').lower():
            caption_kwargs = {
                'chat_id': chat_id,
                'message_id': message_id,
                'caption': text,
                'parse_mode': 'HTML',
                'reply_markup': keyboard,
            }

            if business_connection_id:
                caption_kwargs['business_connection_id'] = business_connection_id

            await bot.edit_message_caption(**caption_kwargs)
        else:
            raise


@router.callback_query(F.data == 'admin_monitoring')
@admin_required
async def admin_monitoring_menu(callback: CallbackQuery):
    try:
        async with AsyncSessionLocal() as db:
            status = await monitoring_service.get_monitoring_status(db)
            language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
            texts = get_texts(language)

            running_status = (
                texts.t('ADMIN_MONITORING_STATUS_RUNNING', '🟢 Работает')
                if status['is_running']
                else texts.t('ADMIN_MONITORING_STATUS_STOPPED', '🔴 Остановлен')
            )
            last_update = (
                status['last_update'].strftime('%H:%M:%S')
                if status['last_update']
                else texts.t('ADMIN_MONITORING_NEVER', 'Никогда')
            )

            text = texts.t(
                'ADMIN_MONITORING_MENU_TEXT',
                '🔍 <b>Система мониторинга</b>\n\n'
                '📊 <b>Статус:</b> {running_status}\n'
                '🕐 <b>Последнее обновление:</b> {last_update}\n'
                '⚙️ <b>Интервал проверки:</b> {interval} мин\n\n'
                '📈 <b>Статистика за 24 часа:</b>\n'
                '• Всего событий: {total_events}\n'
                '• Успешных: {successful}\n'
                '• Ошибок: {failed}\n'
                '• Успешность: {success_rate}%\n\n'
                '🔧 Выберите действие:\n',
            ).format(
                running_status=running_status,
                last_update=last_update,
                interval=settings.MONITORING_INTERVAL,
                total_events=status['stats_24h']['total_events'],
                successful=status['stats_24h']['successful'],
                failed=status['stats_24h']['failed'],
                success_rate=status['stats_24h']['success_rate'],
            )

            keyboard = get_monitoring_keyboard(language)
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в админ меню мониторинга', error=e)
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await callback.answer(texts.t('ADMIN_MONITORING_LOAD_ERROR', '❌ Ошибка получения данных'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_settings')
@admin_required
async def admin_monitoring_settings(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        global_status = (
            texts.t('ADMIN_MONITORING_NOTIFICATIONS_ENABLED', '🟢 Включены')
            if NotificationSettingsService.are_notifications_globally_enabled()
            else texts.t('ADMIN_MONITORING_NOTIFICATIONS_DISABLED', '🔴 Отключены')
        )
        second_percent = NotificationSettingsService.get_second_wave_discount_percent()
        third_percent = NotificationSettingsService.get_third_wave_discount_percent()
        third_days = NotificationSettingsService.get_third_wave_trigger_days()

        text = texts.t(
            'ADMIN_MONITORING_SETTINGS_TEXT',
            '⚙️ <b>Настройки мониторинга</b>\n\n'
            '🔔 <b>Уведомления пользователям:</b> {global_status}\n'
            '• Скидка 2-3 дня: {second_percent}%\n'
            '• Скидка после {third_days} дней: {third_percent}%\n\n'
            'Выберите раздел для настройки.',
        ).format(
            global_status=global_status,
            second_percent=second_percent,
            third_days=third_days,
            third_percent=third_percent,
        )

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_NOTIFY_SETTINGS_BUTTON', '🔔 Уведомления пользователям'),
                        callback_data='admin_mon_notify_settings',
                    )
                ],
                [InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_submenu_settings')],
            ]
        )

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка отображения настроек мониторинга', error=e)
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await callback.answer(texts.t('ADMIN_MONITORING_SETTINGS_OPEN_ERROR', '❌ Не удалось открыть настройки'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_settings')
@admin_required
async def admin_notify_settings(callback: CallbackQuery):
    try:
        await _render_notification_settings(callback)
    except Exception as e:
        logger.error('Ошибка отображения настроек уведомлений', error=e)
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await callback.answer(
            texts.t('ADMIN_MONITORING_NOTIFY_SETTINGS_LOAD_ERROR', '❌ Не удалось загрузить настройки'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_toggle_trial_channel')
@admin_required
async def toggle_trial_channel_notification(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    enabled = NotificationSettingsService.is_trial_channel_unsubscribed_enabled()
    NotificationSettingsService.set_trial_channel_unsubscribed_enabled(not enabled)
    await callback.answer(
        texts.t('ADMIN_MONITORING_TOGGLE_ON', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MONITORING_TOGGLE_OFF', '⏸️ Отключено')
    )
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_trial_channel')
@admin_required
async def preview_trial_channel_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'trial_channel_unsubscribed')
        await callback.answer(texts.t('ADMIN_MONITORING_NOTIFY_PREVIEW_SENT', '✅ Пример отправлен'))
    except Exception as exc:
        logger.error('Ошибка отправки теста отписки от канала', exc=exc)
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await callback.answer(texts.t('ADMIN_MONITORING_NOTIFY_TEST_SEND_ERROR', '❌ Не удалось отправить тест'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_1d')
@admin_required
async def toggle_expired_1d_notification(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    enabled = NotificationSettingsService.is_expired_1d_enabled()
    NotificationSettingsService.set_expired_1d_enabled(not enabled)
    await callback.answer(
        texts.t('ADMIN_MONITORING_TOGGLE_ON', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MONITORING_TOGGLE_OFF', '⏸️ Отключено')
    )
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_1d')
@admin_required
async def preview_expired_1d_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_1d')
        await callback.answer(texts.t('ADMIN_MONITORING_NOTIFY_PREVIEW_SENT', '✅ Пример отправлен'))
    except Exception as exc:
        logger.error('Ошибка отправки теста уведомления 1 день', exc=exc)
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await callback.answer(texts.t('ADMIN_MONITORING_NOTIFY_TEST_SEND_ERROR', '❌ Не удалось отправить тест'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_2d')
@admin_required
async def toggle_second_wave_notification(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    enabled = NotificationSettingsService.is_second_wave_enabled()
    NotificationSettingsService.set_second_wave_enabled(not enabled)
    await callback.answer(
        texts.t('ADMIN_MONITORING_TOGGLE_ON', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MONITORING_TOGGLE_OFF', '⏸️ Отключено')
    )
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_2d')
@admin_required
async def preview_second_wave_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_2d')
        await callback.answer(texts.t('ADMIN_MONITORING_NOTIFY_PREVIEW_SENT', '✅ Пример отправлен'))
    except Exception as exc:
        logger.error('Ошибка отправки теста второй волны', exc=exc)
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await callback.answer(texts.t('ADMIN_MONITORING_NOTIFY_TEST_SEND_ERROR', '❌ Не удалось отправить тест'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_nd')
@admin_required
async def toggle_third_wave_notification(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    enabled = NotificationSettingsService.is_third_wave_enabled()
    NotificationSettingsService.set_third_wave_enabled(not enabled)
    await callback.answer(
        texts.t('ADMIN_MONITORING_TOGGLE_ON', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MONITORING_TOGGLE_OFF', '⏸️ Отключено')
    )
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_nd')
@admin_required
async def preview_third_wave_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_nd')
        await callback.answer(texts.t('ADMIN_MONITORING_NOTIFY_PREVIEW_SENT', '✅ Пример отправлен'))
    except Exception as exc:
        logger.error('Ошибка отправки теста третьей волны', exc=exc)
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await callback.answer(texts.t('ADMIN_MONITORING_NOTIFY_TEST_SEND_ERROR', '❌ Не удалось отправить тест'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_preview_all')
@admin_required
async def preview_all_notifications(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        chat_id = callback.from_user.id
        for notification_type in [
            'trial_channel_unsubscribed',
            'expired_1d',
            'expired_2d',
            'expired_nd',
        ]:
            await _send_notification_preview(callback.bot, chat_id, language, notification_type)
        await callback.answer(
            texts.t('ADMIN_MONITORING_NOTIFY_ALL_TESTS_SENT', '✅ Все тестовые уведомления отправлены')
        )
    except Exception as exc:
        logger.error('Ошибка отправки всех тестовых уведомлений', exc=exc)
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await callback.answer(texts.t('ADMIN_MONITORING_NOTIFY_TESTS_SEND_ERROR', '❌ Не удалось отправить тесты'), show_alert=True)


async def _start_notification_value_edit(
    callback: CallbackQuery,
    state: FSMContext,
    setting_key: str,
    field: str,
    prompt_key: str,
    default_prompt: str,
):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    await state.set_state(AdminStates.editing_notification_value)
    await state.update_data(
        notification_setting_key=setting_key,
        notification_setting_field=field,
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
        settings_business_connection_id=(
            str(getattr(callback.message, 'business_connection_id', None))
            if getattr(callback.message, 'business_connection_id', None) is not None
            else None
        ),
        settings_language=language,
    )
    texts = get_texts(language)
    await callback.answer()
    await callback.message.answer(texts.get(prompt_key, default_prompt))


@router.callback_query(F.data == 'admin_mon_notify_edit_2d_percent')
@admin_required
async def edit_second_wave_percent(callback: CallbackQuery, state: FSMContext):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_second_wave',
        'percent',
        'NOTIFY_PROMPT_SECOND_PERCENT',
        texts.t('NOTIFY_PROMPT_SECOND_PERCENT'),
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_2d_hours')
@admin_required
async def edit_second_wave_hours(callback: CallbackQuery, state: FSMContext):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_second_wave',
        'hours',
        'NOTIFY_PROMPT_SECOND_HOURS',
        texts.t('NOTIFY_PROMPT_SECOND_HOURS'),
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_percent')
@admin_required
async def edit_third_wave_percent(callback: CallbackQuery, state: FSMContext):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'percent',
        'NOTIFY_PROMPT_THIRD_PERCENT',
        texts.t('NOTIFY_PROMPT_THIRD_PERCENT'),
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_hours')
@admin_required
async def edit_third_wave_hours(callback: CallbackQuery, state: FSMContext):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'hours',
        'NOTIFY_PROMPT_THIRD_HOURS',
        texts.t('NOTIFY_PROMPT_SECOND_HOURS'),
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_threshold')
@admin_required
async def edit_third_wave_threshold(callback: CallbackQuery, state: FSMContext):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'trigger',
        'NOTIFY_PROMPT_THIRD_DAYS',
        texts.t('NOTIFY_PROMPT_THIRD_DAYS'),
    )


@router.callback_query(F.data == 'admin_mon_start')
@admin_required
async def start_monitoring_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        if monitoring_service.is_running:
            await callback.answer(texts.t('ADMIN_MONITORING_ALREADY_RUNNING', 'ℹ️ Мониторинг уже запущен'))
            return

        if not monitoring_service.bot:
            monitoring_service.bot = callback.bot

        asyncio.create_task(monitoring_service.start_monitoring())

        await callback.answer(texts.t('ADMIN_MONITORING_STARTED', '✅ Мониторинг запущен!'))

        await admin_monitoring_menu(callback)

    except Exception as e:
        logger.error('Ошибка запуска мониторинга', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_START_ERROR', '❌ Ошибка запуска: {error}').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_stop')
@admin_required
async def stop_monitoring_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        if not monitoring_service.is_running:
            await callback.answer(texts.t('ADMIN_MONITORING_ALREADY_STOPPED', 'ℹ️ Мониторинг уже остановлен'))
            return

        monitoring_service.stop_monitoring()
        await callback.answer(texts.t('ADMIN_MONITORING_STOPPED', '⏹️ Мониторинг остановлен!'))

        await admin_monitoring_menu(callback)

    except Exception as e:
        logger.error('Ошибка остановки мониторинга', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_STOP_ERROR', '❌ Ошибка остановки: {error}').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_force_check')
@admin_required
async def force_check_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        await callback.answer(texts.t('ADMIN_MONITORING_FORCE_CHECK_IN_PROGRESS', '⏳ Выполняем проверку подписок...'))

        async with AsyncSessionLocal() as db:
            results = await monitoring_service.force_check_subscriptions(db)

            text = texts.t(
                'ADMIN_MONITORING_FORCE_CHECK_RESULT',
                '✅ <b>Принудительная проверка завершена</b>\n\n'
                '📊 <b>Результаты проверки:</b>\n'
                '• Истекших подписок: {expired}\n'
                '• Истекающих подписок: {expiring}\n'
                '• Готовых к автооплате: {autopay_ready}\n\n'
                '🕐 <b>Время проверки:</b> {checked_at}\n\n'
                'Нажмите "Назад" для возврата в меню мониторинга.\n',
            ).format(
                expired=results['expired'],
                expiring=results['expiring'],
                autopay_ready=results['autopay_ready'],
                checked_at=datetime.now().strftime('%H:%M:%S'),
            )

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_monitoring')]]
            )

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка принудительной проверки', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_FORCE_CHECK_ERROR', '❌ Ошибка проверки: {error}').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_traffic_check')
@admin_required
async def traffic_check_callback(callback: CallbackQuery):
    """Ручная проверка трафика — использует snapshot и дельту."""
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        # Проверяем, включен ли мониторинг трафика
        if not traffic_monitoring_scheduler.is_enabled():
            await callback.answer(
                texts.t(
                    'ADMIN_MONITORING_TRAFFIC_DISABLED_ALERT',
                    '⚠️ Мониторинг трафика отключен в настройках\nВключите TRAFFIC_FAST_CHECK_ENABLED=true в .env',
                ),
                show_alert=True,
            )
            return

        await callback.answer(
            texts.t('ADMIN_MONITORING_TRAFFIC_CHECK_IN_PROGRESS', '⏳ Запускаем проверку трафика (дельта)...')
        )

        # Используем run_fast_check — он сравнивает с snapshot и отправляет уведомления
        from app.services.traffic_monitoring_service import traffic_monitoring_scheduler_v2

        # Устанавливаем бота, если не установлен
        if not traffic_monitoring_scheduler_v2.bot:
            traffic_monitoring_scheduler_v2.set_bot(callback.bot)

        violations = await traffic_monitoring_scheduler_v2.run_fast_check_now()

        # Получаем информацию о snapshot
        snapshot_age = await traffic_monitoring_scheduler_v2.service.get_snapshot_age_minutes()
        threshold_gb = traffic_monitoring_scheduler_v2.service.get_fast_check_threshold_gb()

        text = texts.t(
            'ADMIN_MONITORING_TRAFFIC_CHECK_RESULT_HEADER',
            '📊 <b>Проверка трафика завершена</b>\n\n'
            '🔍 <b>Результаты (дельта):</b>\n'
            '• Превышений за интервал: {violations_count}\n'
            '• Порог дельты: {threshold_gb} ГБ\n'
            '• Возраст snapshot: {snapshot_age} мин\n\n'
            '🕐 <b>Время проверки:</b> {checked_at}\n',
        ).format(
            violations_count=len(violations),
            threshold_gb=threshold_gb,
            snapshot_age=f'{snapshot_age:.1f}',
            checked_at=datetime.now().strftime('%H:%M:%S'),
        )

        if violations:
            text += texts.t('ADMIN_MONITORING_TRAFFIC_DELTA_EXCEEDED_HEADER', '\n⚠️ <b>Превышения дельты:</b>\n')
            for v in violations[:10]:
                name = v.full_name or v.user_uuid[:8]
                text += texts.t('ADMIN_MONITORING_TRAFFIC_DELTA_EXCEEDED_ITEM', '• {name}: +{traffic_gb} ГБ\n').format(
                    name=name,
                    traffic_gb=f'{v.used_traffic_gb:.1f}',
                )
            if len(violations) > 10:
                text += texts.t('ADMIN_MONITORING_TRAFFIC_DELTA_EXCEEDED_MORE', '... и ещё {count}\n').format(
                    count=len(violations) - 10
                )
            text += texts.t(
                'ADMIN_MONITORING_TRAFFIC_NOTIFICATIONS_SENT',
                '\n📨 Уведомления отправлены (с учётом кулдауна)',
            )
        else:
            text += texts.t('ADMIN_MONITORING_TRAFFIC_NO_EXCEEDED', '\n✅ Превышений не обнаружено')

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_SYNC_RETRY', '🔄 Повторить'),
                        callback_data='admin_mon_traffic_check',
                    )
                ],
                [InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_monitoring')],
            ]
        )

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка проверки трафика', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_GENERIC_ERROR', '❌ Ошибка: {error}').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data.startswith('admin_mon_logs'))
@admin_required
async def monitoring_logs_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        page = 1
        if '_page_' in callback.data:
            page = int(callback.data.split('_page_')[1])

        async with AsyncSessionLocal() as db:
            all_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)

            if not all_logs:
                text = texts.t(
                    'ADMIN_MONITORING_LOGS_EMPTY',
                    '📋 <b>Логи мониторинга пусты</b>\n\nСистема еще не выполнила проверки.',
                )
                keyboard = get_monitoring_logs_back_keyboard(language)
                await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
                return

            per_page = 8
            paginated_logs = paginate_list(all_logs, page=page, per_page=per_page)

            text = texts.t(
                'ADMIN_MONITORING_LOGS_HEADER',
                '📋 <b>Логи мониторинга</b> (стр. {page}/{total_pages})\n\n',
            ).format(
                page=page,
                total_pages=paginated_logs.total_pages,
            )

            for log in paginated_logs.items:
                icon = '✅' if log['is_success'] else '❌'
                time_str = log['created_at'].strftime('%m-%d %H:%M')
                event_type = log['event_type'].replace('_', ' ').title()

                message = log['message']
                if len(message) > 45:
                    message = message[:45] + '...'

                text += f'{icon} <code>{time_str}</code> {event_type}\n'
                text += texts.t('ADMIN_MONITORING_LOGS_ITEM_MESSAGE', '   📄 {message}\n\n').format(message=message)

            total_success = sum(1 for log in all_logs if log['is_success'])
            total_failed = len(all_logs) - total_success
            success_rate = round(total_success / len(all_logs) * 100, 1) if all_logs else 0

            text += texts.t(
                'ADMIN_MONITORING_LOGS_STATS',
                '📊 <b>Общая статистика:</b>\n'
                '• Всего событий: {total}\n'
                '• Успешных: {success}\n'
                '• Ошибок: {failed}\n'
                '• Успешность: {success_rate}%',
            ).format(
                total=len(all_logs),
                success=total_success,
                failed=total_failed,
                success_rate=success_rate,
            )

            keyboard = get_monitoring_logs_keyboard(page, paginated_logs.total_pages, language)
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка получения логов', error=e)
        await callback.answer(texts.t('ADMIN_MONITORING_LOGS_LOAD_ERROR', '❌ Ошибка получения логов'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_clear_logs')
@admin_required
async def clear_logs_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        async with AsyncSessionLocal() as db:
            deleted_count = await monitoring_service.cleanup_old_logs(db, days=0)
            await db.commit()

            if deleted_count > 0:
                await callback.answer(
                    texts.t('ADMIN_MONITORING_LOGS_DELETED', '🗑️ Удалено {count} записей логов').format(
                        count=deleted_count
                    )
                )
            else:
                await callback.answer(texts.t('ADMIN_MONITORING_LOGS_ALREADY_EMPTY', 'ℹ️ Логи уже пусты'))

            await monitoring_logs_callback(callback)

    except Exception as e:
        logger.error('Ошибка очистки логов', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_LOGS_CLEAR_ERROR', '❌ Ошибка очистки: {error}').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_test_notifications')
@admin_required
async def test_notifications_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        monitoring_status = (
            texts.t('ADMIN_MONITORING_STATUS_RUNNING', '🟢 Работает')
            if monitoring_service.is_running
            else texts.t('ADMIN_MONITORING_STATUS_STOPPED', '🔴 Остановлен')
        )
        notifications_status = (
            texts.t('ADMIN_MONITORING_NOTIFICATIONS_ENABLED', '🟢 Включены')
            if settings.ENABLE_NOTIFICATIONS
            else texts.t('ADMIN_MONITORING_NOTIFICATIONS_DISABLED', '🔴 Отключены')
        )
        test_message = texts.t(
            'ADMIN_MONITORING_TEST_NOTIFICATION_MESSAGE',
            '🧪 <b>Тестовое уведомление системы мониторинга</b>\n\n'
            'Это тестовое сообщение для проверки работы системы уведомлений.\n\n'
            '📊 <b>Статус системы:</b>\n'
            '• Мониторинг: {monitoring_status}\n'
            '• Уведомления: {notifications_status}\n'
            '• Время теста: {test_time}\n\n'
            '✅ Если вы получили это сообщение, система уведомлений работает корректно!\n',
        ).format(
            monitoring_status=monitoring_status,
            notifications_status=notifications_status,
            test_time=datetime.now().strftime('%H:%M:%S %d.%m.%Y'),
        )

        await callback.bot.send_message(callback.from_user.id, test_message, parse_mode='HTML')

        await callback.answer(texts.t('ADMIN_MONITORING_TEST_NOTIFICATION_SENT', '✅ Тестовое уведомление отправлено!'))

    except Exception as e:
        logger.error('Ошибка отправки тестового уведомления', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_TEST_NOTIFICATION_ERROR', '❌ Ошибка отправки: {error}').format(error=e),
            show_alert=True,
        )



async def _build_monitoring_statistics_text(
    language: str,
    sub_stats: dict,
    mon_status: dict,
    week_logs: list[dict],
) -> str:
    texts = get_texts(language)
    week_success = sum(1 for log in week_logs if log['is_success'])
    week_errors = len(week_logs) - week_success
    week_success_rate = round(week_success / len(week_logs) * 100, 1) if week_logs else 0
    notifications_status = (
        texts.t('ADMIN_MONITORING_STATUS_SHORT_ON')
        if getattr(settings, 'ENABLE_NOTIFICATIONS', True)
        else texts.t('ADMIN_MONITORING_STATUS_SHORT_OFF')
    )

    text = texts.t('ADMIN_MONITORING_STATS_TEXT').format(
        total_subscriptions=sub_stats['total_subscriptions'],
        active_subscriptions=sub_stats['active_subscriptions'],
        trial_subscriptions=sub_stats['trial_subscriptions'],
        paid_subscriptions=sub_stats['paid_subscriptions'],
        successful_24h=mon_status['stats_24h']['successful'],
        failed_24h=mon_status['stats_24h']['failed'],
        success_rate_24h=mon_status['stats_24h']['success_rate'],
        week_events=len(week_logs),
        week_success=week_success,
        week_errors=week_errors,
        week_success_rate=week_success_rate,
        interval=settings.MONITORING_INTERVAL,
        notifications_status=notifications_status,
        autopay_days=', '.join(map(str, settings.get_autopay_warning_days())),
    )

    if not settings.is_nalogo_enabled():
        return text

    nalogo_status = await nalogo_queue_service.get_status()
    queue_len = nalogo_status.get('queue_length', 0)
    total_amount = nalogo_status.get('total_amount', 0)
    running = nalogo_status.get('running', False)
    pending_count = nalogo_status.get('pending_verification_count', 0)
    pending_amount = nalogo_status.get('pending_verification_amount', 0)

    text += texts.t('ADMIN_MONITORING_STATS_NALOGO_SECTION').format(
        status=(
            texts.t('ADMIN_MONITORING_STATUS_RUNNING')
            if running
            else texts.t('ADMIN_MONITORING_STATUS_STOPPED')
        ),
        queue_len=queue_len,
    )
    if queue_len > 0:
        text += texts.t('ADMIN_MONITORING_STATS_NALOGO_AMOUNT_LINE').format(amount=f'{total_amount:,.2f}')
    if pending_count > 0:
        text += texts.t('ADMIN_MONITORING_STATS_NALOGO_PENDING_LINE').format(
            pending_count=pending_count,
            pending_amount=f'{pending_amount:,.2f}',
        )
    return text


async def _build_monitoring_statistics_keyboard(language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    rows: list[list[InlineKeyboardButton]] = []

    if settings.is_nalogo_enabled():
        nalogo_status = await nalogo_queue_service.get_status()
        queue_len = nalogo_status.get('queue_length', 0)
        pending_count = nalogo_status.get('pending_verification_count', 0)

        nalogo_buttons: list[InlineKeyboardButton] = []
        if queue_len > 0:
            nalogo_buttons.append(
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NALOGO_FORCE_PROCESS_BUTTON').format(count=queue_len),
                    callback_data='admin_mon_nalogo_force_process',
                )
            )
        if pending_count > 0:
            nalogo_buttons.append(
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NALOGO_PENDING_BUTTON').format(count=pending_count),
                    callback_data='admin_mon_nalogo_pending',
                )
            )
        nalogo_buttons.append(
            InlineKeyboardButton(
                text=texts.t('ADMIN_MONITORING_NALOGO_RECONCILE_BUTTON'),
                callback_data='admin_mon_receipts_missing',
            )
        )
        rows.append(nalogo_buttons)

    rows.append([InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data == 'admin_mon_statistics')
@admin_required
async def monitoring_statistics_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        async with AsyncSessionLocal() as db:
            from app.database.crud.subscription import get_subscriptions_statistics

            sub_stats = await get_subscriptions_statistics(db)
            mon_status = await monitoring_service.get_monitoring_status(db)

            week_ago = datetime.now() - timedelta(days=7)
            week_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)
            week_logs = [log for log in week_logs if log['created_at'] >= week_ago]

            view_text = await _build_monitoring_statistics_text(language, sub_stats, mon_status, week_logs)
            keyboard = await _build_monitoring_statistics_keyboard(language)
            await callback.message.edit_text(view_text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка получения статистики', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_nalogo_force_process')
@admin_required
async def nalogo_force_process_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        await callback.answer(texts.t('ADMIN_MONITORING_NALOGO_QUEUE_PROCESSING'), show_alert=False)

        result = await nalogo_queue_service.force_process()
        if 'error' in result:
            await callback.answer(
                texts.t('ADMIN_MONITORING_NALOGO_QUEUE_PROCESS_ERROR').format(error=result['error']),
                show_alert=True,
            )
            return

        processed = result.get('processed', 0)
        remaining = result.get('remaining', 0)

        if processed > 0:
            alert_text = texts.t('ADMIN_MONITORING_NALOGO_QUEUE_PROCESSED').format(count=processed)
            if remaining > 0:
                alert_text += '\n' + texts.t('ADMIN_MONITORING_NALOGO_QUEUE_REMAINING').format(count=remaining)
        elif remaining > 0:
            alert_text = texts.t('ADMIN_MONITORING_NALOGO_QUEUE_SERVICE_UNAVAILABLE').format(count=remaining)
        else:
            alert_text = texts.t('ADMIN_MONITORING_NALOGO_QUEUE_EMPTY')

        await callback.answer(alert_text, show_alert=True)
        await monitoring_statistics_callback(callback)

    except Exception as e:
        logger.error('Ошибка принудительной обработки чеков', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e),
            show_alert=True,
        )



@router.callback_query(F.data == 'admin_mon_nalogo_pending')
@admin_required
async def nalogo_pending_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        from app.services.nalogo_service import NaloGoService

        nalogo_service = NaloGoService()
        receipts = await nalogo_service.get_pending_verification_receipts()

        if not receipts:
            await callback.answer(texts.t('ADMIN_MONITORING_NALOGO_PENDING_EMPTY'), show_alert=True)
            return

        view_text = texts.t('ADMIN_MONITORING_NALOGO_PENDING_HEADER').format(count=len(receipts))
        view_text += texts.t('ADMIN_MONITORING_NALOGO_PENDING_HINT')

        rows: list[list[InlineKeyboardButton]] = []
        for index, receipt in enumerate(receipts[:10], 1):
            payment_id = receipt.get('payment_id', 'unknown')
            amount = receipt.get('amount', 0)
            created_at = receipt.get('created_at', '')[:16].replace('T', ' ')
            error = receipt.get('error', '')[:50]

            view_text += texts.t('ADMIN_MONITORING_NALOGO_PENDING_ITEM').format(
                index=index,
                amount=f'{amount:,.2f}',
                created_at=created_at,
                payment_id=f'{payment_id[:20]}...',
            )
            if error:
                view_text += texts.t('ADMIN_MONITORING_NALOGO_PENDING_ITEM_ERROR').format(error=error)
            view_text += '\n'

            rows.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_NALOGO_MARK_VERIFIED_BUTTON').format(index=index),
                        callback_data=f'admin_nalogo_verified:{payment_id[:30]}',
                    ),
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_NALOGO_RETRY_BUTTON').format(index=index),
                        callback_data=f'admin_nalogo_retry:{payment_id[:30]}',
                    ),
                ]
            )

        if len(receipts) > 10:
            view_text += texts.t('ADMIN_MONITORING_NALOGO_PENDING_MORE').format(count=len(receipts) - 10)

        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_NALOGO_CLEAR_PENDING_BUTTON'),
                    callback_data='admin_nalogo_clear_pending',
                )
            ]
        )
        rows.append([InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')])

        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        await callback.message.edit_text(view_text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка просмотра очереди проверки', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data.startswith('admin_nalogo_verified:'))
@admin_required
async def nalogo_mark_verified_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        from app.services.nalogo_service import NaloGoService

        payment_id = callback.data.split(':', 1)[1]
        nalogo_service = NaloGoService()
        removed = await nalogo_service.mark_pending_as_verified(payment_id, receipt_uuid=None, was_created=True)

        if removed:
            await callback.answer(texts.t('ADMIN_MONITORING_NALOGO_MARK_VERIFIED_SUCCESS'), show_alert=True)
            await nalogo_pending_callback(callback)
            return

        await callback.answer(texts.t('ADMIN_MONITORING_NALOGO_RECEIPT_NOT_FOUND'), show_alert=True)

    except Exception as e:
        logger.error('Ошибка пометки чека', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data.startswith('admin_nalogo_retry:'))
@admin_required
async def nalogo_retry_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        from app.services.nalogo_service import NaloGoService

        payment_id = callback.data.split(':', 1)[1]
        nalogo_service = NaloGoService()

        await callback.answer(texts.t('ADMIN_MONITORING_NALOGO_RETRY_IN_PROGRESS'), show_alert=False)
        receipt_uuid = await nalogo_service.retry_pending_receipt(payment_id)

        if receipt_uuid:
            await callback.answer(
                texts.t('ADMIN_MONITORING_NALOGO_RETRY_SUCCESS').format(receipt_uuid=receipt_uuid),
                show_alert=True,
            )
            await nalogo_pending_callback(callback)
            return

        await callback.answer(texts.t('ADMIN_MONITORING_NALOGO_RETRY_FAILED'), show_alert=True)

    except Exception as e:
        logger.error('Ошибка повторной отправки чека', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_nalogo_clear_pending')
@admin_required
async def nalogo_clear_pending_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        from app.services.nalogo_service import NaloGoService

        nalogo_service = NaloGoService()
        count = await nalogo_service.clear_pending_verification()

        await callback.answer(texts.t('ADMIN_MONITORING_NALOGO_CLEAR_PENDING_SUCCESS').format(count=count), show_alert=True)
        await callback.message.edit_text(
            texts.t('ADMIN_MONITORING_NALOGO_CLEAR_PENDING_MESSAGE'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')]]
            ),
        )

    except Exception as e:
        logger.error('Ошибка очистки очереди', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_receipts_missing')
@admin_required
async def receipts_missing_callback(callback: CallbackQuery):
    """Сверка чеков по логам."""
    # Напрямую вызываем сверку по логам
    await _do_reconcile_logs(callback)



@router.callback_query(F.data == 'admin_mon_receipts_link_old')
@admin_required
async def receipts_link_old_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        from datetime import date, timedelta

        from sqlalchemy import and_, select

        from app.database.models import PaymentMethod, Transaction, TransactionType
        from app.services.nalogo_service import NaloGoService

        await callback.answer(texts.t('ADMIN_MONITORING_NALOGO_LINK_OLD_LOADING'), show_alert=False)

        tracking_start_date = datetime(2024, 12, 29, 0, 0, 0)

        async with AsyncSessionLocal() as db:
            query = (
                select(Transaction)
                .where(
                    and_(
                        Transaction.type == TransactionType.DEPOSIT.value,
                        Transaction.payment_method == PaymentMethod.YOOKASSA.value,
                        Transaction.receipt_uuid.is_(None),
                        Transaction.is_completed == True,
                        Transaction.created_at < tracking_start_date,
                    )
                )
                .order_by(Transaction.created_at.desc())
            )

            result = await db.execute(query)
            transactions = result.scalars().all()

            if not transactions:
                await callback.answer(texts.t('ADMIN_MONITORING_NALOGO_LINK_OLD_EMPTY'), show_alert=True)
                return

            nalogo_service = NaloGoService()
            to_date = date.today()
            from_date = to_date - timedelta(days=60)

            incomes = await nalogo_service.get_incomes(
                from_date=from_date,
                to_date=to_date,
                limit=500,
            )

            if not incomes:
                await callback.answer(texts.t('ADMIN_MONITORING_NALOGO_LINK_OLD_INCOMES_ERROR'), show_alert=True)
                return

            incomes_by_amount = {}
            for income in incomes:
                amount = float(income.get('totalAmount', income.get('amount', 0)))
                amount_kopeks = int(amount * 100)
                if amount_kopeks not in incomes_by_amount:
                    incomes_by_amount[amount_kopeks] = []
                incomes_by_amount[amount_kopeks].append(income)

            linked = 0
            for transaction in transactions:
                if transaction.amount_kopeks not in incomes_by_amount:
                    continue

                matching_incomes = incomes_by_amount[transaction.amount_kopeks]
                if not matching_incomes:
                    continue

                income = matching_incomes.pop(0)
                receipt_uuid = income.get('approvedReceiptUuid', income.get('receiptUuid'))
                if not receipt_uuid:
                    continue

                transaction.receipt_uuid = receipt_uuid
                operation_time = income.get('operationTime')
                if operation_time:
                    try:
                        from dateutil.parser import isoparse

                        transaction.receipt_created_at = isoparse(operation_time)
                    except Exception:
                        transaction.receipt_created_at = datetime.utcnow()
                linked += 1

            if linked > 0:
                await db.commit()

            view_text = texts.t('ADMIN_MONITORING_NALOGO_LINK_OLD_RESULT').format(
                total_transactions=len(transactions),
                total_incomes=len(incomes),
                linked=linked,
                not_linked=len(transactions) - linked,
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')],
                ]
            )
            await callback.message.edit_text(view_text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка привязки старых чеков', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_receipts_reconcile')
@admin_required
async def receipts_reconcile_menu_callback(callback: CallbackQuery, state: FSMContext):
    """Меню выбора периода сверки."""

    # Очищаем состояние на случай если остался ввод даты
    await state.clear()

    # Сразу показываем сверку по логам
    await _do_reconcile_logs(callback)



async def _do_reconcile_logs(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        import re
        from collections import defaultdict
        from pathlib import Path

        await callback.answer(texts.t('ADMIN_MONITORING_RECONCILE_LOADING'), show_alert=False)

        log_file_path = Path(settings.LOG_FILE).resolve()
        log_dir = log_file_path.parent
        current_dir = log_dir / 'current'
        payments_log = current_dir / settings.LOG_PAYMENTS_FILE

        if not payments_log.exists():
            try:
                await callback.message.edit_text(
                    texts.t('ADMIN_MONITORING_RECONCILE_LOG_FILE_NOT_FOUND').format(path=payments_log),
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=texts.t('ADMIN_MONITORING_RECONCILE_REFRESH_BUTTON'),
                                    callback_data='admin_mon_reconcile_logs',
                                )
                            ],
                            [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')],
                        ]
                    ),
                )
            except TelegramBadRequest:
                pass
            return

        payment_pattern = re.compile(
            '(\\d{4}-\\d{2}-\\d{2}) \\d{2}:\\d{2}:\\d{2}.*\\u0423\\u0441\\u043f\\u0435\\u0448\\u043d\\u043e \\u043e\\u0431\\u0440\\u0430\\u0431\\u043e\\u0442\\u0430\\u043d \\u043f\\u043b\\u0430\\u0442\\u0435\\u0436 YooKassa ([a-f0-9-]+).*\\u043d\\u0430 ([\\d.]+)\\u20bd'
        )
        receipt_pattern = re.compile(
            '(\\d{4}-\\d{2}-\\d{2}) \\d{2}:\\d{2}:\\d{2}.*\\u0427\\u0435\\u043a NaloGO \\u0441\\u043e\\u0437\\u0434\\u0430\\u043d \\u0434\\u043b\\u044f \\u043f\\u043b\\u0430\\u0442\\u0435\\u0436\\u0430 ([a-f0-9-]+): (\\w+)'
        )

        payments = {}
        receipts = {}

        try:
            with open(payments_log, encoding='utf-8') as logs_file:
                for line in logs_file:
                    payment_match = payment_pattern.search(line)
                    if payment_match:
                        date_str, payment_id, amount = payment_match.groups()
                        payments[payment_id] = {'date': date_str, 'amount': float(amount)}
                        continue

                    receipt_match = receipt_pattern.search(line)
                    if receipt_match:
                        date_str, payment_id, receipt_uuid = receipt_match.groups()
                        receipts[payment_id] = {'date': date_str, 'receipt_uuid': receipt_uuid}
        except Exception as e:
            logger.error('Ошибка чтения логов', error=e)
            await callback.message.edit_text(
                texts.t('ADMIN_MONITORING_RECONCILE_READ_ERROR').format(error=e),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')]]
                ),
            )
            return

        payments_without_receipts = []
        for payment_id, payment_data in payments.items():
            if payment_id not in receipts:
                payments_without_receipts.append(
                    {'payment_id': payment_id, 'date': payment_data['date'], 'amount': payment_data['amount']}
                )

        by_date = defaultdict(list)
        for payment in payments_without_receipts:
            by_date[payment['date']].append(payment)

        total_payments = len(payments)
        total_receipts = len(receipts)
        missing_count = len(payments_without_receipts)
        missing_amount = sum(payment['amount'] for payment in payments_without_receipts)

        view_text = texts.t('ADMIN_MONITORING_RECONCILE_SUMMARY_HEADER').format(
            total_payments=total_payments,
            total_receipts=total_receipts,
        )

        if missing_count == 0:
            view_text += texts.t('ADMIN_MONITORING_RECONCILE_ALL_MATCHED')
        else:
            view_text += texts.t('ADMIN_MONITORING_RECONCILE_MISSING_SUMMARY').format(
                missing_count=missing_count,
                missing_amount=f'{missing_amount:,.2f}',
            )

            sorted_dates = sorted(by_date.keys(), reverse=True)
            for date_str in sorted_dates[:7]:
                date_payments = by_date[date_str]
                date_amount = sum(payment['amount'] for payment in date_payments)
                view_text += texts.t('ADMIN_MONITORING_RECONCILE_MISSING_DATE_ITEM').format(
                    date=date_str,
                    count=len(date_payments),
                    amount=f'{date_amount:,.2f}',
                )

            if len(sorted_dates) > 7:
                view_text += texts.t('ADMIN_MONITORING_RECONCILE_MISSING_DATE_MORE').format(
                    count=len(sorted_dates) - 7
                )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_RECONCILE_REFRESH_BUTTON'),
                        callback_data='admin_mon_reconcile_logs',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_RECONCILE_DETAILS_BUTTON'),
                        callback_data='admin_mon_reconcile_logs_details',
                    )
                ],
                [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')],
            ]
        )

        try:
            await callback.message.edit_text(view_text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass

    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.error('Ошибка сверки по логам', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_reconcile_logs')
@admin_required
async def receipts_reconcile_logs_refresh_callback(callback: CallbackQuery):
    await _do_reconcile_logs(callback)


@router.callback_query(F.data == 'admin_mon_reconcile_logs_details')
@admin_required
async def receipts_reconcile_logs_details_callback(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        import re
        from pathlib import Path

        await callback.answer(texts.t('ADMIN_MONITORING_RECONCILE_DETAILS_LOADING'), show_alert=False)

        log_file_path = Path(settings.LOG_FILE).resolve()
        log_dir = log_file_path.parent
        current_dir = log_dir / 'current'
        payments_log = current_dir / settings.LOG_PAYMENTS_FILE

        if not payments_log.exists():
            await callback.answer(texts.t('ADMIN_MONITORING_RECONCILE_DETAILS_FILE_NOT_FOUND'), show_alert=True)
            return

        payment_pattern = re.compile(
            '(\\d{4}-\\d{2}-\\d{2}) (\\d{2}:\\d{2}:\\d{2}).*\\u0423\\u0441\\u043f\\u0435\\u0448\\u043d\\u043e \\u043e\\u0431\\u0440\\u0430\\u0431\\u043e\\u0442\\u0430\\u043d \\u043f\\u043b\\u0430\\u0442\\u0435\\u0436 YooKassa ([a-f0-9-]+).*\\u043f\\u043e\\u043b\\u044c\\u0437\\u043e\\u0432\\u0430\\u0442\\u0435\\u043b\\u044c (\\d+).*\\u043d\\u0430 ([\\d.]+)\\u20bd'
        )
        receipt_pattern = re.compile('\u0427\u0435\u043a NaloGO \u0441\u043e\u0437\u0434\u0430\u043d \u0434\u043b\u044f \u043f\u043b\u0430\u0442\u0435\u0436\u0430 ([a-f0-9-]+)')

        payments = {}
        receipts = set()

        with open(payments_log, encoding='utf-8') as logs_file:
            for line in logs_file:
                payment_match = payment_pattern.search(line)
                if payment_match:
                    date_str, time_str, payment_id, user_id, amount = payment_match.groups()
                    payments[payment_id] = {
                        'date': date_str,
                        'time': time_str,
                        'user_id': user_id,
                        'amount': float(amount),
                    }
                    continue

                receipt_match = receipt_pattern.search(line)
                if receipt_match:
                    receipts.add(receipt_match.group(1))

        missing = []
        for payment_id, data in payments.items():
            if payment_id not in receipts:
                missing.append({'payment_id': payment_id, **data})

        missing.sort(key=lambda item: (item['date'], item['time']), reverse=True)

        if not missing:
            view_text = texts.t('ADMIN_MONITORING_RECONCILE_ALL_MATCHED')
        else:
            view_text = texts.t('ADMIN_MONITORING_RECONCILE_DETAILS_HEADER').format(count=len(missing))

            for payment in missing[:20]:
                view_text += texts.t('ADMIN_MONITORING_RECONCILE_DETAILS_ITEM').format(
                    date=payment['date'],
                    time=payment['time'],
                    user_id=payment['user_id'],
                    amount=f'{payment["amount"]:.0f}',
                    payment_id=f'{payment["payment_id"][:18]}...',
                )

            if len(missing) > 20:
                view_text += texts.t('ADMIN_MONITORING_RECONCILE_DETAILS_MORE').format(count=len(missing) - 20)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_reconcile_logs')],
            ]
        )

        try:
            await callback.message.edit_text(view_text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass

    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.error('Ошибка детализации', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e),
            show_alert=True,
        )



def get_monitoring_logs_keyboard(current_page: int, total_pages: int, language: str):
    texts = get_texts(language)
    keyboard: list[list[InlineKeyboardButton]] = []

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []

        if current_page > 1:
            nav_row.append(InlineKeyboardButton(text='<', callback_data=f'admin_mon_logs_page_{current_page - 1}'))

        nav_row.append(InlineKeyboardButton(text=f'{current_page}/{total_pages}', callback_data='current_page'))

        if current_page < total_pages:
            nav_row.append(InlineKeyboardButton(text='>', callback_data=f'admin_mon_logs_page_{current_page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_LOGS_REFRESH_BUTTON'),
                    callback_data='admin_mon_logs',
                ),
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_CLEAR'),
                    callback_data='admin_mon_clear_logs',
                ),
            ],
            [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_monitoring_logs_back_keyboard(language: str):
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_LOGS_REFRESH_BUTTON'),
                    callback_data='admin_mon_logs',
                ),
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_LOGS_FILTERS_BUTTON'),
                    callback_data='admin_mon_logs_filters',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_CLEAR_ALL'),
                    callback_data='admin_mon_clear_logs',
                )
            ],
            [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')],
        ]
    )


@router.message(Command('monitoring'))
@admin_required
async def monitoring_command(message: Message):
    language = message.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        async with AsyncSessionLocal() as db:
            status = await monitoring_service.get_monitoring_status(db)

        running_status = (
            texts.t('ADMIN_MONITORING_STATUS_RUNNING')
            if status['is_running']
            else texts.t('ADMIN_MONITORING_STATUS_STOPPED')
        )

        view_text = texts.t('ADMIN_MONITORING_QUICK_STATUS_TEXT').format(
            status=running_status,
            total_events=status['stats_24h']['total_events'],
            success_rate=status['stats_24h']['success_rate'],
        )
        await message.answer(view_text, parse_mode='HTML')

    except Exception as e:
        logger.error('Ошибка команды /monitoring', error=e)
        await message.answer(texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e))



@router.message(AdminStates.editing_notification_value)
async def process_notification_value_input(message: Message, state: FSMContext):
    data = await state.get_data()
    language = (data or {}).get('settings_language') or message.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)

    if not data:
        await state.clear()
        await message.answer(texts.t('ADMIN_MONITORING_SETTINGS_CONTEXT_LOST'))
        return

    raw_value = (message.text or '').strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        await message.answer(texts.t('NOTIFICATION_VALUE_INVALID'))
        return

    key = data.get('notification_setting_key')
    field = data.get('notification_setting_field')

    if (key == 'expired_second_wave' and field == 'percent') or (key == 'expired_third_wave' and field == 'percent'):
        if value < 0 or value > 100:
            await message.answer(texts.t('ADMIN_MONITORING_NOTIFY_PERCENT_RANGE_ERROR'))
            return
    elif (key == 'expired_second_wave' and field == 'hours') or (key == 'expired_third_wave' and field == 'hours'):
        if value < 1 or value > 168:
            await message.answer(texts.t('ADMIN_MONITORING_NOTIFY_HOURS_RANGE_ERROR'))
            return
    elif key == 'expired_third_wave' and field == 'trigger':
        if value < 2:
            await message.answer(texts.t('ADMIN_MONITORING_NOTIFY_TRIGGER_DAYS_RANGE_ERROR'))
            return

    success = False
    if key == 'expired_second_wave' and field == 'percent':
        success = NotificationSettingsService.set_second_wave_discount_percent(value)
    elif key == 'expired_second_wave' and field == 'hours':
        success = NotificationSettingsService.set_second_wave_valid_hours(value)
    elif key == 'expired_third_wave' and field == 'percent':
        success = NotificationSettingsService.set_third_wave_discount_percent(value)
    elif key == 'expired_third_wave' and field == 'hours':
        success = NotificationSettingsService.set_third_wave_valid_hours(value)
    elif key == 'expired_third_wave' and field == 'trigger':
        success = NotificationSettingsService.set_third_wave_trigger_days(value)

    if not success:
        await message.answer(texts.t('NOTIFICATION_VALUE_INVALID'))
        return

    back_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('BACK'),
                    callback_data='admin_mon_notify_settings',
                )
            ]
        ]
    )

    await message.answer(
        texts.t('NOTIFICATION_VALUE_UPDATED'),
        reply_markup=back_keyboard,
    )

    chat_id = data.get('settings_message_chat')
    message_id = data.get('settings_message_id')
    business_connection_id = data.get('settings_business_connection_id')
    if chat_id and message_id:
        await _render_notification_settings_for_state(
            message.bot,
            chat_id,
            message_id,
            language,
            business_connection_id=business_connection_id,
        )

    await state.clear()


# ============== Traffic Monitoring Settings ==============


def _format_traffic_toggle(enabled: bool, texts) -> str:
    return texts.t('ADMIN_MONITORING_STATUS_SHORT_ON') if enabled else texts.t('ADMIN_MONITORING_STATUS_SHORT_OFF')


def _build_traffic_settings_keyboard(language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    fast_enabled = settings.TRAFFIC_FAST_CHECK_ENABLED
    daily_enabled = settings.TRAFFIC_DAILY_CHECK_ENABLED

    fast_interval = settings.TRAFFIC_FAST_CHECK_INTERVAL_MINUTES
    fast_threshold = settings.TRAFFIC_FAST_CHECK_THRESHOLD_GB
    daily_time = settings.TRAFFIC_DAILY_CHECK_TIME
    daily_threshold = settings.TRAFFIC_DAILY_THRESHOLD_GB
    cooldown = settings.TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_TRAFFIC_FAST_TOGGLE_BUTTON').format(
                        status=_format_traffic_toggle(fast_enabled, texts)
                    ),
                    callback_data='admin_traffic_toggle_fast',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_TRAFFIC_FAST_INTERVAL_BUTTON').format(value=fast_interval),
                    callback_data='admin_traffic_edit_fast_interval',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_TRAFFIC_FAST_THRESHOLD_BUTTON').format(value=fast_threshold),
                    callback_data='admin_traffic_edit_fast_threshold',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_TRAFFIC_DAILY_TOGGLE_BUTTON').format(
                        status=_format_traffic_toggle(daily_enabled, texts)
                    ),
                    callback_data='admin_traffic_toggle_daily',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_TRAFFIC_DAILY_TIME_BUTTON').format(value=daily_time),
                    callback_data='admin_traffic_edit_daily_time',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_TRAFFIC_DAILY_THRESHOLD_BUTTON').format(value=daily_threshold),
                    callback_data='admin_traffic_edit_daily_threshold',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_TRAFFIC_COOLDOWN_BUTTON').format(value=cooldown),
                    callback_data='admin_traffic_edit_cooldown',
                )
            ],
            [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')],
        ]
    )


def _build_traffic_settings_text(language: str) -> str:
    texts = get_texts(language)
    fast_status = _format_traffic_toggle(settings.TRAFFIC_FAST_CHECK_ENABLED, texts)
    daily_status = _format_traffic_toggle(settings.TRAFFIC_DAILY_CHECK_ENABLED, texts)

    view_text = texts.t('ADMIN_MONITORING_TRAFFIC_SETTINGS_TEXT').format(
        fast_status=fast_status,
        fast_interval=settings.TRAFFIC_FAST_CHECK_INTERVAL_MINUTES,
        fast_threshold=settings.TRAFFIC_FAST_CHECK_THRESHOLD_GB,
        daily_status=daily_status,
        daily_time=settings.TRAFFIC_DAILY_CHECK_TIME,
        daily_threshold=settings.TRAFFIC_DAILY_THRESHOLD_GB,
        cooldown=settings.TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES,
    )

    monitored_nodes = settings.get_traffic_monitored_nodes()
    ignored_nodes = settings.get_traffic_ignored_nodes()
    excluded_uuids = settings.get_traffic_excluded_user_uuids()

    if monitored_nodes:
        view_text += texts.t('ADMIN_MONITORING_TRAFFIC_MONITORED_NODES_LINE').format(count=len(monitored_nodes))
    if ignored_nodes:
        view_text += texts.t('ADMIN_MONITORING_TRAFFIC_IGNORED_NODES_LINE').format(count=len(ignored_nodes))
    if excluded_uuids:
        view_text += texts.t('ADMIN_MONITORING_TRAFFIC_EXCLUDED_USERS_LINE').format(count=len(excluded_uuids))

    return view_text


@router.callback_query(F.data == 'admin_mon_traffic_settings')
@admin_required
async def admin_traffic_settings(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        text = _build_traffic_settings_text(language)
        keyboard = _build_traffic_settings_keyboard(language)
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except Exception as e:
        logger.error('Ошибка отображения настроек трафика', error=e)
        await callback.answer(texts.t('ADMIN_MONITORING_TRAFFIC_SETTINGS_LOAD_ERROR'), show_alert=True)


@router.callback_query(F.data == 'admin_traffic_toggle_fast')
@admin_required
async def toggle_fast_check(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        from app.services.system_settings_service import BotConfigurationService

        current = settings.TRAFFIC_FAST_CHECK_ENABLED
        new_value = not current

        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, 'TRAFFIC_FAST_CHECK_ENABLED', new_value)
            await db.commit()

        await callback.answer(texts.t('ADMIN_MONITORING_TOGGLE_ON') if new_value else texts.t('ADMIN_MONITORING_TOGGLE_OFF'))

        text = _build_traffic_settings_text(language)
        keyboard = _build_traffic_settings_keyboard(language)
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка переключения быстрой проверки', error=e)
        await callback.answer(texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_traffic_toggle_daily')
@admin_required
async def toggle_daily_check(callback: CallbackQuery):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    try:
        from app.services.system_settings_service import BotConfigurationService

        current = settings.TRAFFIC_DAILY_CHECK_ENABLED
        new_value = not current

        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, 'TRAFFIC_DAILY_CHECK_ENABLED', new_value)
            await db.commit()

        await callback.answer(texts.t('ADMIN_MONITORING_TOGGLE_ON') if new_value else texts.t('ADMIN_MONITORING_TOGGLE_OFF'))

        text = _build_traffic_settings_text(language)
        keyboard = _build_traffic_settings_keyboard(language)
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка переключения суточной проверки', error=e)
        await callback.answer(texts.t('ADMIN_MONITORING_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_traffic_edit_fast_interval')
@admin_required
async def edit_fast_interval(callback: CallbackQuery, state: FSMContext):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_FAST_CHECK_INTERVAL_MINUTES',
        traffic_setting_type='int',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
        settings_language=language,
    )
    await callback.answer()
    await callback.message.answer(texts.t('ADMIN_MONITORING_TRAFFIC_PROMPT_FAST_INTERVAL'))


@router.callback_query(F.data == 'admin_traffic_edit_fast_threshold')
@admin_required
async def edit_fast_threshold(callback: CallbackQuery, state: FSMContext):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_FAST_CHECK_THRESHOLD_GB',
        traffic_setting_type='float',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
        settings_language=language,
    )
    await callback.answer()
    await callback.message.answer(texts.t('ADMIN_MONITORING_TRAFFIC_PROMPT_FAST_THRESHOLD'))


@router.callback_query(F.data == 'admin_traffic_edit_daily_time')
@admin_required
async def edit_daily_time(callback: CallbackQuery, state: FSMContext):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_DAILY_CHECK_TIME',
        traffic_setting_type='time',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
        settings_language=language,
    )
    await callback.answer()
    await callback.message.answer(texts.t('ADMIN_MONITORING_TRAFFIC_PROMPT_DAILY_TIME'))


@router.callback_query(F.data == 'admin_traffic_edit_daily_threshold')
@admin_required
async def edit_daily_threshold(callback: CallbackQuery, state: FSMContext):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_DAILY_THRESHOLD_GB',
        traffic_setting_type='float',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
        settings_language=language,
    )
    await callback.answer()
    await callback.message.answer(texts.t('ADMIN_MONITORING_TRAFFIC_PROMPT_DAILY_THRESHOLD'))


@router.callback_query(F.data == 'admin_traffic_edit_cooldown')
@admin_required
async def edit_cooldown(callback: CallbackQuery, state: FSMContext):
    language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES',
        traffic_setting_type='int',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
        settings_language=language,
    )
    await callback.answer()
    await callback.message.answer(texts.t('ADMIN_MONITORING_TRAFFIC_PROMPT_COOLDOWN'))


@router.message(AdminStates.editing_traffic_setting)
async def process_traffic_setting_input(message: Message, state: FSMContext):
    from app.services.system_settings_service import BotConfigurationService

    data = await state.get_data()
    language = (data or {}).get('settings_language') or message.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)

    if not data:
        await state.clear()
        await message.answer(texts.t('ADMIN_MONITORING_SETTINGS_CONTEXT_LOST'))
        return

    raw_value = (message.text or '').strip()
    setting_key = data.get('traffic_setting_key')
    setting_type = data.get('traffic_setting_type')

    try:
        if setting_type == 'int':
            value = int(raw_value)
            if value < 1:
                raise ValueError(texts.t('ADMIN_MONITORING_TRAFFIC_MIN_VALUE_ERROR'))
        elif setting_type == 'float':
            value = float(raw_value.replace(',', '.'))
            if value <= 0:
                raise ValueError(texts.t('ADMIN_MONITORING_TRAFFIC_POSITIVE_VALUE_ERROR'))
        elif setting_type == 'time':
            import re

            if not re.match(r'^\d{1,2}:\d{2}$', raw_value):
                raise ValueError(texts.t('ADMIN_MONITORING_TRAFFIC_TIME_FORMAT_ERROR'))
            hours_str, minutes_str = raw_value.split(':')
            hours, minutes = int(hours_str), int(minutes_str)
            if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
                raise ValueError(texts.t('ADMIN_MONITORING_TRAFFIC_TIME_VALUE_ERROR'))
            value = f'{hours:02d}:{minutes:02d}'
        else:
            value = raw_value
    except ValueError as e:
        await message.answer(texts.t('ADMIN_MONITORING_TRAFFIC_INPUT_ERROR').format(error=e))
        return

    try:
        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, setting_key, value)
            await db.commit()

        back_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_TRAFFIC_BACK_TO_SETTINGS_BUTTON'),
                        callback_data='admin_mon_traffic_settings',
                    )
                ]
            ]
        )
        await message.answer(texts.t('ADMIN_MONITORING_TRAFFIC_SETTING_SAVED'), reply_markup=back_keyboard)

        chat_id = data.get('settings_message_chat')
        message_id = data.get('settings_message_id')
        if chat_id and message_id:
            try:
                view_text = _build_traffic_settings_text(language)
                keyboard = _build_traffic_settings_keyboard(language)
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=view_text,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception:
                pass

    except Exception as e:
        logger.error('Ошибка сохранения настройки трафика', error=e)
        await message.answer(texts.t('ADMIN_MONITORING_TRAFFIC_SAVE_ERROR').format(error=e))

    await state.clear()


def register_handlers(dp):
    dp.include_router(router)
