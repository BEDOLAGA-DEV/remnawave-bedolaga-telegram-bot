import asyncio
import hashlib
import html
from datetime import UTC, date, datetime, timedelta

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.keyboards.admin import get_monitoring_keyboard
from app.localization.texts import get_texts
from app.services.monitoring_service import monitoring_service
from app.services.nalogo_queue_service import nalogo_queue_service
from app.services.notification_settings_service import NotificationSettingsService
from app.services.system_settings_service import normalize_settings_language
from app.services.traffic_monitoring_service import (
    traffic_monitoring_scheduler,
)
from app.states import AdminStates
from app.utils.decorators import admin_required
from app.utils.pagination import paginate_list
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)
router = Router()


def _monitoring_language(event, db_user: User | None = None, fallback: str | None = None) -> str:
    account_language = getattr(db_user, 'language', None)
    telegram_language = getattr(getattr(event, 'from_user', None), 'language_code', None)
    return normalize_settings_language(account_language, fallback or telegram_language or settings.DEFAULT_LANGUAGE)


def _format_toggle(enabled: bool, language: str = 'ru') -> str:
    texts = get_texts(language)
    return (
        texts.t('ADMIN_MONITORING_UI_FORMAT_TOGGLE_000', '🟢 Вкл')
        if enabled
        else texts.t('ADMIN_MONITORING_UI_FORMAT_TOGGLE_001', '🔴 Выкл')
    )


def _build_notification_settings_view(language: str):
    texts = get_texts(language)
    get_texts(language)
    config = NotificationSettingsService.get_config()

    second_percent = NotificationSettingsService.get_second_wave_discount_percent()
    second_hours = NotificationSettingsService.get_second_wave_valid_hours()
    third_percent = NotificationSettingsService.get_third_wave_discount_percent()
    third_hours = NotificationSettingsService.get_third_wave_valid_hours()
    third_days = NotificationSettingsService.get_third_wave_trigger_days()

    trial_channel_status = _format_toggle(
        config.get('trial_channel_unsubscribed', {}).get('enabled', True), language=language
    )
    expired_1d_status = _format_toggle(config['expired_1d'].get('enabled', True), language=language)
    second_wave_status = _format_toggle(config['expired_second_wave'].get('enabled', True), language=language)
    third_wave_status = _format_toggle(config['expired_third_wave'].get('enabled', True), language=language)

    summary_text = texts.t(
        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_002',
        '🔔 <b>Уведомления пользователям</b>\n\n• Отписка от канала: {value1}\n• 1 день после истечения: {value2}\n• 2-3 дня (скидка {value3}% / {value4} ч): {value5}\n• {value6} дней (скидка {value7}% / {value8} ч): {value9}',
    ).format(
        value1=trial_channel_status,
        value2=expired_1d_status,
        value3=second_percent,
        value4=second_hours,
        value5=second_wave_status,
        value6=third_days,
        value7=third_percent,
        value8=third_hours,
        value9=third_wave_status,
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_003', '{value1} • Отписка от канала'
                    ).format(value1=trial_channel_status),
                    callback_data='admin_mon_notify_toggle_trial_channel',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_004', '🧪 Тест: отписка от канала'
                    ),
                    callback_data='admin_mon_notify_preview_trial_channel',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_005', '{value1} • 1 день после истечения'
                    ).format(value1=expired_1d_status),
                    callback_data='admin_mon_notify_toggle_expired_1d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_006', '🧪 Тест: 1 день после истечения'
                    ),
                    callback_data='admin_mon_notify_preview_expired_1d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_007', '{value1} • 2-3 дня со скидкой'
                    ).format(value1=second_wave_status),
                    callback_data='admin_mon_notify_toggle_expired_2d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_008', '🧪 Тест: скидка 2-3 день'
                    ),
                    callback_data='admin_mon_notify_preview_expired_2d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_009', '✏️ Скидка 2-3 дня: {value1}%'
                    ).format(value1=second_percent),
                    callback_data='admin_mon_notify_edit_2d_percent',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_010', '⏱️ Срок скидки 2-3 дня: {value1} ч'
                    ).format(value1=second_hours),
                    callback_data='admin_mon_notify_edit_2d_hours',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_011',
                        '{value1} • {value2} дней со скидкой',
                    ).format(value1=third_wave_status, value2=third_days),
                    callback_data='admin_mon_notify_toggle_expired_nd',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_012', '🧪 Тест: скидка спустя дни'
                    ),
                    callback_data='admin_mon_notify_preview_expired_nd',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_013', '✏️ Скидка {value1} дней: {value2}%'
                    ).format(value1=third_days, value2=third_percent),
                    callback_data='admin_mon_notify_edit_nd_percent',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_014',
                        '⏱️ Срок скидки {value1} дней: {value2} ч',
                    ).format(value1=third_days, value2=third_hours),
                    callback_data='admin_mon_notify_edit_nd_hours',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_015', '📆 Порог уведомления: {value1} дн.'
                    ).format(value1=third_days),
                    callback_data='admin_mon_notify_edit_nd_threshold',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_BUILD_NOTIFICATION_SETTINGS_VIEW_016', '🧪 Отправить все тесты'),
                    callback_data='admin_mon_notify_preview_all',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                    callback_data='admin_mon_settings',
                )
            ],
        ]
    )

    return summary_text, keyboard


async def _build_notification_preview_message(language: str, notification_type: str):
    texts = get_texts(language)
    now = datetime.now(UTC)
    price_30_days = settings.format_price(settings.PRICE_30_DAYS)

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.keyboards.inline import get_channel_sub_keyboard
    from app.services.channel_subscription_service import channel_subscription_service

    header = texts.t(
        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_PREVIEW_MESSAGE_018', '🧪 <b>Тестовое уведомление мониторинга</b>\n\n'
    )

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
            end_date=format_local_datetime(now - timedelta(days=1), '%d.%m.%Y %H:%M'),
            price=price_30_days,
            tariff_label='',
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
            expires_at=format_local_datetime(now + timedelta(hours=valid_hours), '%d.%m.%Y %H:%M'),
            trigger_days=3,
            tariff_label='',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_PREVIEW_MESSAGE_019', '🎁 Получить скидку'
                        ),
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
            expires_at=format_local_datetime(now + timedelta(hours=valid_hours), '%d.%m.%Y %H:%M'),
            tariff_label='',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_PREVIEW_MESSAGE_019', '🎁 Получить скидку'
                        ),
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
        'ADMIN_MONITORING_UI_BUILD_NOTIFICATION_PREVIEW_MESSAGE_021',
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


async def _render_notification_settings(callback: CallbackQuery, db_user: User | None = None) -> None:
    language = _monitoring_language(callback, db_user)
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
async def admin_monitoring_menu(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        async with AsyncSessionLocal() as db:
            status = await monitoring_service.get_monitoring_status(db)

            running_status = (
                texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_022', '🟢 Работает')
                if status['is_running']
                else texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_023', '🔴 Остановлен')
            )
            last_update = (
                format_local_datetime(status['last_update'], '%H:%M:%S')
                if status['last_update']
                else texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_024', 'Никогда')
            )

            text = texts.t(
                'ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_025',
                '\n🔍 <b>Система мониторинга</b>\n\n📊 <b>Статус:</b> {value1}\n🕐 <b>Последнее обновление:</b> {value2}\n⚙️ <b>Интервал проверки:</b> {value3} мин\n\n📈 <b>Статистика за 24 часа:</b>\n• Всего событий: {value4}\n• Успешных: {value5}\n• Ошибок: {value6}\n• Успешность: {value7}%\n\n🔧 Выберите действие:\n',
            ).format(
                value1=running_status,
                value2=last_update,
                value3=settings.MONITORING_INTERVAL,
                value4=status['stats_24h']['total_events'],
                value5=status['stats_24h']['successful'],
                value6=status['stats_24h']['failed'],
                value7=status['stats_24h']['success_rate'],
            )

            language = _monitoring_language(callback, db_user)
            keyboard = get_monitoring_keyboard(language)
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в админ меню мониторинга', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_026', '❌ Ошибка получения данных'), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_settings')
@admin_required
async def admin_monitoring_settings(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        global_status = (
            texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_SETTINGS_027', '🟢 Включены')
            if NotificationSettingsService.are_notifications_globally_enabled()
            else texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_SETTINGS_028', '🔴 Отключены')
        )
        second_percent = NotificationSettingsService.get_second_wave_discount_percent()
        third_percent = NotificationSettingsService.get_third_wave_discount_percent()
        third_days = NotificationSettingsService.get_third_wave_trigger_days()

        text = texts.t(
            'ADMIN_MONITORING_UI_ADMIN_MONITORING_SETTINGS_029',
            '⚙️ <b>Настройки мониторинга</b>\n\n🔔 <b>Уведомления пользователям:</b> {value1}\n• Скидка 2-3 дня: {value2}%\n• Скидка после {value3} дней: {value4}%\n\nВыберите раздел для настройки.',
        ).format(value1=global_status, value2=second_percent, value3=third_days, value4=third_percent)

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_MONITORING_UI_ADMIN_MONITORING_SETTINGS_030', '🔔 Уведомления пользователям'
                        ),
                        callback_data='admin_mon_notify_settings',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                        callback_data='admin_submenu_settings',
                    )
                ],
            ]
        )

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка отображения настроек мониторинга', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_SETTINGS_032', '❌ Не удалось открыть настройки'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_settings')
@admin_required
async def admin_notify_settings(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        await _render_notification_settings(callback, db_user=db_user)
    except Exception as e:
        logger.error('Ошибка отображения настроек уведомлений', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_ADMIN_NOTIFY_SETTINGS_033', '❌ Не удалось загрузить настройки'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_toggle_trial_channel')
@admin_required
async def toggle_trial_channel_notification(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    enabled = NotificationSettingsService.is_trial_channel_unsubscribed_enabled()
    async with AsyncSessionLocal() as db:
        saved = await NotificationSettingsService.set_trial_channel_unsubscribed_enabled(db, not enabled)
    if not saved:
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_034', '❌ Не удалось сохранить настройку'),
            show_alert=True,
        )
        return
    await callback.answer(
        texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_035', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_036', '⏸️ Отключено')
    )
    await _render_notification_settings(callback, db_user=db_user)


@router.callback_query(F.data == 'admin_mon_notify_preview_trial_channel')
@admin_required
async def preview_trial_channel_notification(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        language = _monitoring_language(callback, db_user)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'trial_channel_unsubscribed')
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_PREVIEW_TRIAL_CHANNEL_NOTIFICATION_037', '✅ Пример отправлен')
        )
    except Exception as exc:
        logger.error('Failed to send trial channel preview', exc=exc)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_PREVIEW_TRIAL_CHANNEL_NOTIFICATION_038', '❌ Не удалось отправить тест'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_1d')
@admin_required
async def toggle_expired_1d_notification(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    enabled = NotificationSettingsService.is_expired_1d_enabled()
    async with AsyncSessionLocal() as db:
        saved = await NotificationSettingsService.set_expired_1d_enabled(db, not enabled)
    if not saved:
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_034', '❌ Не удалось сохранить настройку'),
            show_alert=True,
        )
        return
    await callback.answer(
        texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_035', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_036', '⏸️ Отключено')
    )
    await _render_notification_settings(callback, db_user=db_user)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_1d')
@admin_required
async def preview_expired_1d_notification(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        language = _monitoring_language(callback, db_user)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_1d')
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_PREVIEW_TRIAL_CHANNEL_NOTIFICATION_037', '✅ Пример отправлен')
        )
    except Exception as exc:
        logger.error('Failed to send expired 1d preview', exc=exc)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_PREVIEW_TRIAL_CHANNEL_NOTIFICATION_038', '❌ Не удалось отправить тест'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_2d')
@admin_required
async def toggle_second_wave_notification(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    enabled = NotificationSettingsService.is_second_wave_enabled()
    async with AsyncSessionLocal() as db:
        saved = await NotificationSettingsService.set_second_wave_enabled(db, not enabled)
    if not saved:
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_034', '❌ Не удалось сохранить настройку'),
            show_alert=True,
        )
        return
    await callback.answer(
        texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_035', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_036', '⏸️ Отключено')
    )
    await _render_notification_settings(callback, db_user=db_user)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_2d')
@admin_required
async def preview_second_wave_notification(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        language = _monitoring_language(callback, db_user)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_2d')
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_PREVIEW_TRIAL_CHANNEL_NOTIFICATION_037', '✅ Пример отправлен')
        )
    except Exception as exc:
        logger.error('Failed to send second wave preview', exc=exc)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_PREVIEW_TRIAL_CHANNEL_NOTIFICATION_038', '❌ Не удалось отправить тест'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_nd')
@admin_required
async def toggle_third_wave_notification(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    enabled = NotificationSettingsService.is_third_wave_enabled()
    async with AsyncSessionLocal() as db:
        saved = await NotificationSettingsService.set_third_wave_enabled(db, not enabled)
    if not saved:
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_034', '❌ Не удалось сохранить настройку'),
            show_alert=True,
        )
        return
    await callback.answer(
        texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_035', '✅ Включено')
        if not enabled
        else texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_036', '⏸️ Отключено')
    )
    await _render_notification_settings(callback, db_user=db_user)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_nd')
@admin_required
async def preview_third_wave_notification(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        language = _monitoring_language(callback, db_user)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_nd')
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_PREVIEW_TRIAL_CHANNEL_NOTIFICATION_037', '✅ Пример отправлен')
        )
    except Exception as exc:
        logger.error('Failed to send third wave preview', exc=exc)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_PREVIEW_TRIAL_CHANNEL_NOTIFICATION_038', '❌ Не удалось отправить тест'),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_notify_preview_all')
@admin_required
async def preview_all_notifications(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        language = _monitoring_language(callback, db_user)
        chat_id = callback.from_user.id
        for notification_type in [
            'trial_channel_unsubscribed',
            'expired_1d',
            'expired_2d',
            'expired_nd',
        ]:
            await _send_notification_preview(callback.bot, chat_id, language, notification_type)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_PREVIEW_ALL_NOTIFICATIONS_054', '✅ Все тестовые уведомления отправлены')
        )
    except Exception as exc:
        logger.error('Failed to send all notification previews', exc=exc)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_PREVIEW_ALL_NOTIFICATIONS_055', '❌ Не удалось отправить тесты'),
            show_alert=True,
        )


async def _start_notification_value_edit(
    callback: CallbackQuery,
    state: FSMContext,
    setting_key: str,
    field: str,
    prompt_key: str,
    default_prompt: str,
    db_user: User | None = None,
):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    language = _monitoring_language(callback, db_user)
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
async def edit_second_wave_percent(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_second_wave',
        'percent',
        'NOTIFY_PROMPT_SECOND_PERCENT',
        texts.t(
            'ADMIN_MONITORING_UI_EDIT_SECOND_WAVE_PERCENT_056',
            'Введите новый процент скидки для уведомления через 2-3 дня (0-100):',
        ),
        db_user=db_user,
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_2d_hours')
@admin_required
async def edit_second_wave_hours(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_second_wave',
        'hours',
        'NOTIFY_PROMPT_SECOND_HOURS',
        texts.t('ADMIN_MONITORING_UI_EDIT_SECOND_WAVE_HOURS_057', 'Введите количество часов действия скидки (1-168):'),
        db_user=db_user,
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_percent')
@admin_required
async def edit_third_wave_percent(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'percent',
        'NOTIFY_PROMPT_THIRD_PERCENT',
        texts.t(
            'ADMIN_MONITORING_UI_EDIT_THIRD_WAVE_PERCENT_058',
            'Введите новый процент скидки для позднего предложения (0-100):',
        ),
        db_user=db_user,
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_hours')
@admin_required
async def edit_third_wave_hours(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'hours',
        'NOTIFY_PROMPT_THIRD_HOURS',
        texts.t('ADMIN_MONITORING_UI_EDIT_SECOND_WAVE_HOURS_057', 'Введите количество часов действия скидки (1-168):'),
        db_user=db_user,
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_threshold')
@admin_required
async def edit_third_wave_threshold(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'trigger',
        'NOTIFY_PROMPT_THIRD_DAYS',
        texts.t(
            'ADMIN_MONITORING_UI_EDIT_THIRD_WAVE_THRESHOLD_060',
            'Через сколько дней после истечения отправлять предложение? (минимум 2):',
        ),
        db_user=db_user,
    )


@router.callback_query(F.data == 'admin_mon_start')
@admin_required
async def start_monitoring_callback(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        if monitoring_service.is_running:
            await callback.answer(
                texts.t('ADMIN_MONITORING_UI_START_MONITORING_CALLBACK_061', 'ℹ️ Мониторинг уже запущен')
            )
            return

        if not monitoring_service.bot:
            monitoring_service.bot = callback.bot

        asyncio.create_task(monitoring_service.start_monitoring())

        await callback.answer(texts.t('ADMIN_MONITORING_UI_START_MONITORING_CALLBACK_062', '✅ Мониторинг запущен!'))

        await admin_monitoring_menu(callback, db_user=db_user)

    except Exception as e:
        logger.error('Ошибка запуска мониторинга', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_START_MONITORING_CALLBACK_063', '❌ Ошибка запуска: {value1}').format(
                value1=str(e)
            ),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_stop')
@admin_required
async def stop_monitoring_callback(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        if not monitoring_service.is_running:
            await callback.answer(
                texts.t('ADMIN_MONITORING_UI_STOP_MONITORING_CALLBACK_064', 'ℹ️ Мониторинг уже остановлен')
            )
            return

        monitoring_service.stop_monitoring()
        await callback.answer(texts.t('ADMIN_MONITORING_UI_STOP_MONITORING_CALLBACK_065', '⏹️ Мониторинг остановлен!'))

        await admin_monitoring_menu(callback, db_user=db_user)

    except Exception as e:
        logger.error('Ошибка остановки мониторинга', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_STOP_MONITORING_CALLBACK_066', '❌ Ошибка остановки: {value1}').format(
                value1=str(e)
            ),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_force_check')
@admin_required
async def force_check_callback(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_FORCE_CHECK_CALLBACK_067', '⏳ Выполняем проверку подписок...')
        )

        async with AsyncSessionLocal() as db:
            results = await monitoring_service.force_check_subscriptions(db)

            text = texts.t(
                'ADMIN_MONITORING_UI_FORCE_CHECK_CALLBACK_068',
                '\n✅ <b>Принудительная проверка завершена</b>\n\n📊 <b>Результаты проверки:</b>\n• Истекших подписок: {value1}\n• Истекающих подписок: {value2}\n• Готовых к автооплате: {value3}\n\n🕐 <b>Время проверки:</b> {value4}\n\nНажмите "Назад" для возврата в меню мониторинга.\n',
            ).format(
                value1=results['expired'],
                value2=results['expiring'],
                value3=results['autopay_ready'],
                value4=format_local_datetime(datetime.now(UTC), '%H:%M:%S'),
            )

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                            callback_data='admin_monitoring',
                        )
                    ]
                ]
            )

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка принудительной проверки', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_FORCE_CHECK_CALLBACK_070', '❌ Ошибка проверки: {value1}').format(
                value1=str(e)
            ),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_traffic_check')
@admin_required
async def traffic_check_callback(callback: CallbackQuery, db_user: User | None = None):
    """Ручная проверка трафика — использует snapshot и дельту."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        # Проверяем, включен ли мониторинг трафика
        if not traffic_monitoring_scheduler.is_enabled():
            await callback.answer(
                texts.t(
                    'ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_071',
                    '⚠️ Мониторинг трафика отключен в настройках\nВключите TRAFFIC_FAST_CHECK_ENABLED=true в .env',
                ),
                show_alert=True,
            )
            return

        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_072', '⏳ Запускаем проверку трафика (дельта)...')
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
            'ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_073',
            '\n📊 <b>Проверка трафика завершена</b>\n\n🔍 <b>Результаты (дельта):</b>\n• Превышений за интервал: {value1}\n• Порог дельты: {value2} ГБ\n• Возраст snapshot: {value3} мин\n\n🕐 <b>Время проверки:</b> {value4}\n',
        ).format(
            value1=len(violations),
            value2=threshold_gb,
            value3=format(snapshot_age, '.1f'),
            value4=format_local_datetime(datetime.now(UTC), '%H:%M:%S'),
        )

        if violations:
            text += texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_074', '\n⚠️ <b>Превышения дельты:</b>\n')
            for v in violations[:10]:
                # Числовой id панели не режется срезом, а при отсутствии идентичности
                # нельзя уронить весь экран в «❌ Ошибка» — показываем прочерк.
                fallback = f'ID {v.user_id}' if v.user_id else '—'
                name = html.escape(v.full_name or '') or fallback
                text += texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_075', '• {value1}: +{value2} ГБ\n').format(
                    value1=name, value2=format(v.used_traffic_gb, '.1f')
                )
            if len(violations) > 10:
                text += texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_076', '... и ещё {value1}\n').format(
                    value1=len(violations) - 10
                )
            text += texts.t(
                'ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_077', '\n📨 Уведомления отправлены (с учётом кулдауна)'
            )
        else:
            text += texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_078', '\n✅ Превышений не обнаружено')

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_079', '🔄 Повторить'),
                        callback_data='admin_mon_traffic_check',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                        callback_data='admin_monitoring',
                    )
                ],
            ]
        )

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка проверки трафика', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_081', '❌ Ошибка: {value1}').format(value1=str(e)),
            show_alert=True,
        )


@router.callback_query(F.data.startswith('admin_mon_logs'))
@admin_required
async def monitoring_logs_callback(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        page = 1
        if '_page_' in callback.data:
            page = int(callback.data.split('_page_')[1])

        async with AsyncSessionLocal() as db:
            all_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)

            if not all_logs:
                text = texts.t(
                    'ADMIN_MONITORING_UI_MONITORING_LOGS_CALLBACK_082',
                    '📋 <b>Логи мониторинга пусты</b>\n\nСистема еще не выполнила проверки.',
                )
                keyboard = get_monitoring_logs_back_keyboard(language=language)
                await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
                return

            per_page = 8
            paginated_logs = paginate_list(all_logs, page=page, per_page=per_page)

            text = texts.t(
                'ADMIN_MONITORING_UI_MONITORING_LOGS_CALLBACK_083',
                '📋 <b>Логи мониторинга</b> (стр. {value1}/{value2})\n\n',
            ).format(value1=page, value2=paginated_logs.total_pages)

            for log in paginated_logs.items:
                icon = '✅' if log['is_success'] else '❌'
                time_str = format_local_datetime(log['created_at'], '%m-%d %H:%M')
                event_type = log['event_type'].replace('_', ' ').title()

                message = log['message']
                if len(message) > 45:
                    message = message[:45] + '...'

                text += f'{icon} <code>{time_str}</code> {event_type}\n'
                text += f'   📄 {message}\n\n'

            total_success = sum(1 for log in all_logs if log['is_success'])
            total_failed = len(all_logs) - total_success
            success_rate = round(total_success / len(all_logs) * 100, 1) if all_logs else 0

            text += texts.t('ADMIN_MONITORING_UI_MONITORING_LOGS_CALLBACK_084', '📊 <b>Общая статистика:</b>\n')
            text += texts.t('ADMIN_MONITORING_UI_MONITORING_LOGS_CALLBACK_085', '• Всего событий: {value1}\n').format(
                value1=len(all_logs)
            )
            text += texts.t('ADMIN_MONITORING_UI_MONITORING_LOGS_CALLBACK_086', '• Успешных: {value1}\n').format(
                value1=total_success
            )
            text += texts.t('ADMIN_MONITORING_UI_MONITORING_LOGS_CALLBACK_087', '• Ошибок: {value1}\n').format(
                value1=total_failed
            )
            text += texts.t('ADMIN_MONITORING_UI_MONITORING_LOGS_CALLBACK_088', '• Успешность: {value1}%').format(
                value1=success_rate
            )

            keyboard = get_monitoring_logs_keyboard(page, paginated_logs.total_pages, language=language)
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка получения логов', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_MONITORING_LOGS_CALLBACK_089', '❌ Ошибка получения логов'), show_alert=True
        )


@router.callback_query(F.data == 'admin_mon_clear_logs')
@admin_required
async def clear_logs_callback(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        async with AsyncSessionLocal() as db:
            deleted_count = await monitoring_service.cleanup_old_logs(db, days=0)
            await db.commit()

            if deleted_count > 0:
                await callback.answer(
                    texts.t('ADMIN_MONITORING_UI_CLEAR_LOGS_CALLBACK_090', '🗑️ Удалено {value1} записей логов').format(
                        value1=deleted_count
                    )
                )
            else:
                await callback.answer(texts.t('ADMIN_MONITORING_UI_CLEAR_LOGS_CALLBACK_091', 'ℹ️ Логи уже пусты'))

            await monitoring_logs_callback(callback, db_user=db_user)

    except Exception as e:
        logger.error('Ошибка очистки логов', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_CLEAR_LOGS_CALLBACK_092', '❌ Ошибка очистки: {value1}').format(value1=str(e)),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_test_notifications')
@admin_required
async def test_notifications_callback(callback: CallbackQuery, db_user: User | None = None):  # noqa: PT028
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        test_message = texts.t(
            'ADMIN_MONITORING_UI_TEST_NOTIFICATIONS_CALLBACK_093',
            '\n🧪 <b>Тестовое уведомление системы мониторинга</b>\n\nЭто тестовое сообщение для проверки работы системы уведомлений.\n\n📊 <b>Статус системы:</b>\n• Мониторинг: {value1}\n• Уведомления: {value2}\n• Время теста: {value3}\n\n✅ Если вы получили это сообщение, система уведомлений работает корректно!\n',
        ).format(
            value1=texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_022', '🟢 Работает')
            if monitoring_service.is_running
            else texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_023', '🔴 Остановлен'),
            value2=texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_SETTINGS_027', '🟢 Включены')
            if settings.ENABLE_NOTIFICATIONS
            else texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_SETTINGS_028', '🔴 Отключены'),
            value3=format_local_datetime(datetime.now(UTC), '%H:%M:%S %d.%m.%Y'),
        )

        await callback.bot.send_message(callback.from_user.id, test_message, parse_mode='HTML')

        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TEST_NOTIFICATIONS_CALLBACK_098', '✅ Тестовое уведомление отправлено!')
        )

    except Exception as e:
        logger.error('Ошибка отправки тестового уведомления', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TEST_NOTIFICATIONS_CALLBACK_099', '❌ Ошибка отправки: {value1}').format(
                value1=str(e)
            ),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_statistics')
@admin_required
async def monitoring_statistics_callback(callback: CallbackQuery, db_user: User | None = None):
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        async with AsyncSessionLocal() as db:
            from app.database.crud.subscription import get_subscriptions_statistics

            sub_stats = await get_subscriptions_statistics(db)

            mon_status = await monitoring_service.get_monitoring_status(db)

            week_ago = datetime.now(UTC) - timedelta(days=7)
            week_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)
            week_logs = [log for log in week_logs if log['created_at'] >= week_ago]

            week_success = sum(1 for log in week_logs if log['is_success'])
            week_errors = len(week_logs) - week_success

            text = texts.t(
                'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_100',
                '\n📊 <b>Статистика мониторинга</b>\n\n📱 <b>Подписки:</b>\n• Всего: {value1}\n• Активных: {value2}\n• Тестовых: {value3}\n• Платных: {value4}\n\n📈 <b>За сегодня:</b>\n• Успешных операций: {value5}\n• Ошибок: {value6}\n• Успешность: {value7}%\n\n📊 <b>За неделю:</b>\n• Всего событий: {value8}\n• Успешных: {value9}\n• Ошибок: {value10}\n• Успешность: {value11}%\n\n🔧 <b>Система:</b>\n• Интервал: {value12} мин\n• Уведомления: {value13}\n• Автооплата: {value14} дней\n',
            ).format(
                value1=sub_stats['total_subscriptions'],
                value2=sub_stats['active_subscriptions'],
                value3=sub_stats['trial_subscriptions'],
                value4=sub_stats['paid_subscriptions'],
                value5=mon_status['stats_24h']['successful'],
                value6=mon_status['stats_24h']['failed'],
                value7=mon_status['stats_24h']['success_rate'],
                value8=len(week_logs),
                value9=week_success,
                value10=week_errors,
                value11=round(week_success / len(week_logs) * 100, 1) if week_logs else 0,
                value12=settings.MONITORING_INTERVAL,
                value13=texts.t('ADMIN_MONITORING_UI_FORMAT_TOGGLE_000', '🟢 Вкл')
                if getattr(settings, 'ENABLE_NOTIFICATIONS', True)
                else texts.t('ADMIN_MONITORING_UI_FORMAT_TOGGLE_001', '🔴 Выкл'),
                value14=', '.join(map(str, settings.get_autopay_warning_days())),
            )

            # Добавляем информацию о чеках NaloGO
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                queue_len = nalogo_status.get('queue_length', 0)
                total_amount = nalogo_status.get('total_amount', 0)
                running = nalogo_status.get('running', False)
                pending_count = nalogo_status.get('pending_verification_count', 0)
                pending_amount = nalogo_status.get('pending_verification_amount', 0)

                nalogo_section = texts.t(
                    'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_103',
                    '\n🧾 <b>Чеки NaloGO:</b>\n• Сервис: {value1}\n• В очереди: {value2} чек(ов)',
                ).format(
                    value1=texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_022', '🟢 Работает')
                    if running
                    else texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_023', '🔴 Остановлен'),
                    value2=queue_len,
                )
                if queue_len > 0:
                    nalogo_section += texts.t(
                        'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_106', '\n• На сумму: {value1} ₽'
                    ).format(value1=format(total_amount, ',.2f'))
                if pending_count > 0:
                    nalogo_section += texts.t(
                        'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_107',
                        '\n⚠️ <b>Требуют проверки: {value1} ({value2} ₽)</b>',
                    ).format(value1=pending_count, value2=format(pending_amount, ',.2f'))
                text += nalogo_section

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            buttons = []
            # Кнопки для работы с чеками NaloGO
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                nalogo_buttons = []
                if nalogo_status.get('queue_length', 0) > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=texts.t(
                                'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_108', '🧾 Отправить ({value1})'
                            ).format(value1=nalogo_status['queue_length']),
                            callback_data='admin_mon_nalogo_force_process',
                        )
                    )
                pending_count = nalogo_status.get('pending_verification_count', 0)
                if pending_count > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=texts.t(
                                'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_109', '⚠️ Проверить ({value1})'
                            ).format(value1=pending_count),
                            callback_data='admin_mon_nalogo_pending',
                        )
                    )
                nalogo_buttons.append(
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_110', '📊 Сверка чеков'),
                        callback_data='admin_mon_receipts_missing',
                    )
                )
                buttons.append(nalogo_buttons)

            buttons.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                        callback_data='admin_monitoring',
                    )
                ]
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка получения статистики', error=e)
        await callback.answer(
            texts.t(
                'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_112', '❌ Ошибка получения статистики: {value1}'
            ).format(value1=str(e)),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_nalogo_force_process')
@admin_required
async def nalogo_force_process_callback(callback: CallbackQuery, db_user: User | None = None):
    """Принудительная отправка чеков из очереди."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_NALOGO_FORCE_PROCESS_CALLBACK_113', '🔄 Запускаю обработку очереди чеков...'),
            show_alert=False,
        )

        result = await nalogo_queue_service.force_process()

        if 'error' in result:
            detail = result['error']
            if detail == 'NaloGO сервис не настроен':
                detail = texts.t('NALOGO_SERVICE_NOT_CONFIGURED', 'NaloGO сервис не настроен')
            await callback.answer(f'❌ {detail}', show_alert=True)
            return

        result.get('message', texts.t('ADMIN_MONITORING_UI_NALOGO_FORCE_PROCESS_CALLBACK_114', 'Готово'))
        processed = result.get('processed', 0)
        remaining = result.get('remaining', 0)

        if processed > 0:
            text = texts.t(
                'ADMIN_MONITORING_UI_NALOGO_FORCE_PROCESS_CALLBACK_115', '✅ Обработано: {value1} чек(ов)'
            ).format(value1=processed)
            if remaining > 0:
                text += texts.t(
                    'ADMIN_MONITORING_UI_NALOGO_FORCE_PROCESS_CALLBACK_116', '\n⏳ Осталось в очереди: {value1}'
                ).format(value1=remaining)
        elif remaining > 0:
            text = texts.t(
                'ADMIN_MONITORING_UI_NALOGO_FORCE_PROCESS_CALLBACK_117',
                '⚠️ Сервис nalog.ru недоступен\n⏳ В очереди: {value1} чек(ов)',
            ).format(value1=remaining)
        else:
            text = texts.t('ADMIN_MONITORING_UI_NALOGO_FORCE_PROCESS_CALLBACK_118', '📭 Очередь пуста')

        await callback.answer(text, show_alert=True)

        # Обновляем страницу статистики
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        # Перезагружаем статистику
        async with AsyncSessionLocal() as db:
            from app.database.crud.subscription import get_subscriptions_statistics

            sub_stats = await get_subscriptions_statistics(db)
            mon_status = await monitoring_service.get_monitoring_status(db)

            week_ago = datetime.now(UTC) - timedelta(days=7)
            week_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)
            week_logs = [log for log in week_logs if log['created_at'] >= week_ago]
            week_success = sum(1 for log in week_logs if log['is_success'])
            week_errors = len(week_logs) - week_success

            stats_text = texts.t(
                'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_100',
                '\n📊 <b>Статистика мониторинга</b>\n\n📱 <b>Подписки:</b>\n• Всего: {value1}\n• Активных: {value2}\n• Тестовых: {value3}\n• Платных: {value4}\n\n📈 <b>За сегодня:</b>\n• Успешных операций: {value5}\n• Ошибок: {value6}\n• Успешность: {value7}%\n\n📊 <b>За неделю:</b>\n• Всего событий: {value8}\n• Успешных: {value9}\n• Ошибок: {value10}\n• Успешность: {value11}%\n\n🔧 <b>Система:</b>\n• Интервал: {value12} мин\n• Уведомления: {value13}\n• Автооплата: {value14} дней\n',
            ).format(
                value1=sub_stats['total_subscriptions'],
                value2=sub_stats['active_subscriptions'],
                value3=sub_stats['trial_subscriptions'],
                value4=sub_stats['paid_subscriptions'],
                value5=mon_status['stats_24h']['successful'],
                value6=mon_status['stats_24h']['failed'],
                value7=mon_status['stats_24h']['success_rate'],
                value8=len(week_logs),
                value9=week_success,
                value10=week_errors,
                value11=round(week_success / len(week_logs) * 100, 1) if week_logs else 0,
                value12=settings.MONITORING_INTERVAL,
                value13=texts.t('ADMIN_MONITORING_UI_FORMAT_TOGGLE_000', '🟢 Вкл')
                if getattr(settings, 'ENABLE_NOTIFICATIONS', True)
                else texts.t('ADMIN_MONITORING_UI_FORMAT_TOGGLE_001', '🔴 Выкл'),
                value14=', '.join(map(str, settings.get_autopay_warning_days())),
            )

            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                queue_len = nalogo_status.get('queue_length', 0)
                total_amount = nalogo_status.get('total_amount', 0)
                running = nalogo_status.get('running', False)

                nalogo_section = texts.t(
                    'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_103',
                    '\n🧾 <b>Чеки NaloGO:</b>\n• Сервис: {value1}\n• В очереди: {value2} чек(ов)',
                ).format(
                    value1=texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_022', '🟢 Работает')
                    if running
                    else texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_023', '🔴 Остановлен'),
                    value2=queue_len,
                )
                if queue_len > 0:
                    nalogo_section += texts.t(
                        'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_106', '\n• На сумму: {value1} ₽'
                    ).format(value1=format(total_amount, ',.2f'))
                stats_text += nalogo_section

            buttons = []
            # Кнопки для работы с чеками NaloGO
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                nalogo_buttons = []
                if nalogo_status.get('queue_length', 0) > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=texts.t(
                                'ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_108', '🧾 Отправить ({value1})'
                            ).format(value1=nalogo_status['queue_length']),
                            callback_data='admin_mon_nalogo_force_process',
                        )
                    )
                nalogo_buttons.append(
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_MONITORING_STATISTICS_CALLBACK_110', '📊 Сверка чеков'),
                        callback_data='admin_mon_receipts_missing',
                    )
                )
                buttons.append(nalogo_buttons)

            buttons.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                        callback_data='admin_monitoring',
                    )
                ]
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await callback.message.edit_text(stats_text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка принудительной обработки чеков', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_081', '❌ Ошибка: {value1}').format(value1=str(e)),
            show_alert=True,
        )


def _nalogo_pending_token(payment_id: str) -> str:
    # Stable across refresh/restart, bounded by Telegram's 64-byte callback limit.
    return 'v1_' + hashlib.sha256(payment_id.encode()).hexdigest()[:32]


async def _resolve_nalogo_pending_id(service, token: str) -> str | None:
    receipts = await service.get_pending_verification_receipts()
    matches = {
        payment_id
        for receipt in receipts
        if isinstance(payment_id := receipt.get('payment_id'), str)
        and (payment_id == token or _nalogo_pending_token(payment_id) == token)
    }
    # Never prefix-match old truncated callbacks or choose between hash collisions.
    return matches.pop() if len(matches) == 1 else None


@router.callback_query(F.data == 'admin_mon_nalogo_pending')
@admin_required
async def nalogo_pending_callback(callback: CallbackQuery, db_user: User | None = None):
    """Просмотр чеков ожидающих ручной проверки."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from app.services.nalogo_service import NaloGoService

        nalogo_service = NaloGoService()
        receipts = await nalogo_service.get_pending_verification_receipts()

        if not receipts:
            await callback.answer(
                texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_130', '✅ Нет чеков на проверку'), show_alert=True
            )
            return

        text = texts.t(
            'ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_131', '⚠️ <b>Чеки требующие проверки: {value1}</b>\n\n'
        ).format(value1=len(receipts))
        text += texts.t(
            'ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_132', 'Проверьте в lknpd.nalog.ru созданы ли эти чеки.\n\n'
        )

        buttons = []
        for i, receipt in enumerate(receipts[:10], 1):
            payment_id = receipt.get('payment_id', 'unknown')
            amount = receipt.get('amount', 0)
            created_at = receipt.get('created_at', '')[:16].replace('T', ' ')
            error = receipt.get('error', '')[:50]

            text += f'<b>{i}. {amount:,.2f} ₽</b>\n'
            text += f'   📅 {created_at}\n'
            text += f'   🆔 <code>{payment_id[:20]}...</code>\n'
            if error:
                text += f'   ❌ {error}\n'
            text += '\n'

            # Кнопки для каждого чека
            token = _nalogo_pending_token(payment_id)
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_133', '✅ Создан ({value1})').format(
                            value1=i
                        ),
                        callback_data=f'admin_nalogo_verified:{token}',
                    ),
                    InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_134', '🔄 Отправить ({value1})'
                        ).format(value1=i),
                        callback_data=f'admin_nalogo_retry:{token}',
                    ),
                ]
            )

        if len(receipts) > 10:
            text += texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_135', '\n... и ещё {value1} чек(ов)').format(
                value1=len(receipts) - 10
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_136', '🗑 Очистить всё (проверено)'),
                    callback_data='admin_nalogo_clear_pending',
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                    callback_data='admin_mon_statistics',
                )
            ]
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка просмотра очереди проверки', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_081', '❌ Ошибка: {value1}').format(value1=str(e)),
            show_alert=True,
        )


@router.callback_query(F.data.startswith('admin_nalogo_verified:'))
@admin_required
async def nalogo_mark_verified_callback(callback: CallbackQuery, db_user: User | None = None):
    """Пометить чек как созданный в налоговой."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        from app.services.nalogo_service import NaloGoService

        token = callback.data.split(':', 1)[1]
        nalogo_service = NaloGoService()

        payment_id = await _resolve_nalogo_pending_id(nalogo_service, token)
        if payment_id is None:
            await callback.answer(
                texts.t(
                    'ADMIN_MONITORING_UI_NALOGO_MARK_VERIFIED_CALLBACK_139',
                    '❌ Чек не найден или кнопка устарела. Обновите список чеков.',
                ),
                show_alert=True,
            )
            return

        # Помечаем как проверенный (чек был создан)
        removed = await nalogo_service.mark_pending_as_verified(payment_id, receipt_uuid=None, was_created=True)

        if removed:
            await callback.answer(
                texts.t('ADMIN_MONITORING_UI_NALOGO_MARK_VERIFIED_CALLBACK_140', '✅ Чек помечен как созданный'),
                show_alert=True,
            )
            # Обновляем список
            await nalogo_pending_callback(callback, db_user=db_user)
        else:
            await callback.answer(
                texts.t('ADMIN_MONITORING_UI_NALOGO_MARK_VERIFIED_CALLBACK_141', '❌ Чек не найден'), show_alert=True
            )

    except Exception as e:
        logger.error('Ошибка пометки чека', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_081', '❌ Ошибка: {value1}').format(value1=str(e)),
            show_alert=True,
        )


@router.callback_query(F.data.startswith('admin_nalogo_retry:'))
@admin_required
async def nalogo_retry_callback(callback: CallbackQuery, db_user: User | None = None):
    """Повторно отправить чек в налоговую."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        from app.services.nalogo_service import NaloGoService

        token = callback.data.split(':', 1)[1]
        nalogo_service = NaloGoService()

        payment_id = await _resolve_nalogo_pending_id(nalogo_service, token)
        if payment_id is None:
            await callback.answer(
                texts.t(
                    'ADMIN_MONITORING_UI_NALOGO_MARK_VERIFIED_CALLBACK_139',
                    '❌ Чек не найден или кнопка устарела. Обновите список чеков.',
                ),
                show_alert=True,
            )
            return

        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_NALOGO_RETRY_CALLBACK_144', '🔄 Отправляю чек...'), show_alert=False
        )

        # bot обязателен: без него чек уйдёт в ФНС, но покупатель его не получит
        receipt_uuid = await nalogo_service.retry_pending_receipt(payment_id, bot=callback.bot)

        if receipt_uuid:
            await callback.answer(
                texts.t('ADMIN_MONITORING_UI_NALOGO_RETRY_CALLBACK_145', '✅ Чек создан: {value1}').format(
                    value1=receipt_uuid
                ),
                show_alert=True,
            )
            # Обновляем список
            await nalogo_pending_callback(callback, db_user=db_user)
        else:
            await callback.answer(
                texts.t('ADMIN_MONITORING_UI_NALOGO_RETRY_CALLBACK_146', '❌ Не удалось создать чек'), show_alert=True
            )

    except Exception as e:
        logger.error('Ошибка повторной отправки чека', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_081', '❌ Ошибка: {value1}').format(value1=str(e)),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_nalogo_clear_pending')
@admin_required
async def nalogo_clear_pending_callback(callback: CallbackQuery, db_user: User | None = None):
    """Очистить всю очередь проверки."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        from app.services.nalogo_service import NaloGoService

        nalogo_service = NaloGoService()
        count = await nalogo_service.clear_pending_verification()

        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_NALOGO_CLEAR_PENDING_CALLBACK_148', '✅ Очищено: {value1} чек(ов)').format(
                value1=count
            ),
            show_alert=True,
        )
        # Возвращаемся на статистику
        await callback.message.edit_text(
            texts.t('ADMIN_MONITORING_UI_NALOGO_CLEAR_PENDING_CALLBACK_149', '✅ Очередь проверки очищена'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                            callback_data='admin_mon_statistics',
                        )
                    ]
                ]
            ),
        )

    except Exception as e:
        logger.error('Ошибка очистки очереди', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_081', '❌ Ошибка: {value1}').format(value1=str(e)),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_receipts_missing')
@admin_required
async def receipts_missing_callback(callback: CallbackQuery, db_user: User | None = None):
    """Сверка чеков по логам."""
    # Напрямую вызываем сверку по логам
    await _do_reconcile_logs(callback, db_user=db_user)


@router.callback_query(F.data == 'admin_mon_receipts_link_old')
@admin_required
async def receipts_link_old_callback(callback: CallbackQuery, db_user: User | None = None):
    """Привязать старые чеки из NaloGO к транзакциям по сумме и дате."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from sqlalchemy import and_, select

        from app.database.models import PaymentMethod, Transaction, TransactionType
        from app.services.nalogo_service import NaloGoService

        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_RECEIPTS_LINK_OLD_CALLBACK_152', '🔄 Загружаю чеки из NaloGO...'),
            show_alert=False,
        )

        TRACKING_START_DATE = datetime(2024, 12, 29, 0, 0, 0, tzinfo=UTC)

        async with AsyncSessionLocal() as db:
            # Получаем старые транзакции без чеков
            query = (
                select(Transaction)
                .where(
                    and_(
                        Transaction.type == TransactionType.DEPOSIT.value,
                        Transaction.payment_method == PaymentMethod.YOOKASSA.value,
                        Transaction.receipt_uuid.is_(None),
                        Transaction.is_completed == True,
                        Transaction.created_at < TRACKING_START_DATE,
                    )
                )
                .order_by(Transaction.created_at.desc())
            )

            result = await db.execute(query)
            transactions = result.scalars().all()

            if not transactions:
                await callback.answer(
                    texts.t(
                        'ADMIN_MONITORING_UI_RECEIPTS_LINK_OLD_CALLBACK_153', '✅ Нет старых транзакций для привязки'
                    ),
                    show_alert=True,
                )
                return

            # Получаем чеки из NaloGO за последние 60 дней
            nalogo_service = NaloGoService()
            to_date = date.today()
            from_date = to_date - timedelta(days=60)

            incomes = await nalogo_service.get_incomes(
                from_date=from_date,
                to_date=to_date,
                limit=500,
            )

            if not incomes:
                await callback.answer(
                    texts.t(
                        'ADMIN_MONITORING_UI_RECEIPTS_LINK_OLD_CALLBACK_154', '❌ Не удалось получить чеки из NaloGO'
                    ),
                    show_alert=True,
                )
                return

            # Создаём словарь чеков по сумме для быстрого поиска
            # Ключ: сумма в копейках, значение: список чеков
            incomes_by_amount = {}
            for income in incomes:
                amount = float(income.get('totalAmount', income.get('amount', 0)))
                amount_kopeks = int(amount * 100)
                if amount_kopeks not in incomes_by_amount:
                    incomes_by_amount[amount_kopeks] = []
                incomes_by_amount[amount_kopeks].append(income)

            linked = 0
            for t in transactions:
                if t.amount_kopeks in incomes_by_amount:
                    matching_incomes = incomes_by_amount[t.amount_kopeks]
                    if matching_incomes:
                        # Берём первый подходящий чек
                        income = matching_incomes.pop(0)
                        receipt_uuid = income.get('approvedReceiptUuid', income.get('receiptUuid'))
                        if receipt_uuid:
                            t.receipt_uuid = receipt_uuid
                            # Парсим дату чека
                            operation_time = income.get('operationTime')
                            if operation_time:
                                try:
                                    from dateutil.parser import isoparse

                                    parsed_time = isoparse(operation_time)
                                    t.receipt_created_at = (
                                        parsed_time if parsed_time.tzinfo else parsed_time.replace(tzinfo=UTC)
                                    )
                                except Exception:
                                    t.receipt_created_at = datetime.now(UTC)
                            linked += 1

            if linked > 0:
                await db.commit()

            text = texts.t('ADMIN_MONITORING_UI_RECEIPTS_LINK_OLD_CALLBACK_155', '🔗 <b>Привязка завершена</b>\n\n')
            text += texts.t(
                'ADMIN_MONITORING_UI_RECEIPTS_LINK_OLD_CALLBACK_156', 'Всего транзакций: {value1}\n'
            ).format(value1=len(transactions))
            text += texts.t('ADMIN_MONITORING_UI_RECEIPTS_LINK_OLD_CALLBACK_157', 'Чеков в NaloGO: {value1}\n').format(
                value1=len(incomes)
            )
            text += texts.t(
                'ADMIN_MONITORING_UI_RECEIPTS_LINK_OLD_CALLBACK_158', 'Привязано: <b>{value1}</b>\n'
            ).format(value1=linked)
            text += texts.t(
                'ADMIN_MONITORING_UI_RECEIPTS_LINK_OLD_CALLBACK_159', 'Не удалось привязать: {value1}'
            ).format(value1=len(transactions) - linked)

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                            callback_data='admin_mon_statistics',
                        )
                    ],
                ]
            )

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка привязки старых чеков', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_081', '❌ Ошибка: {value1}').format(value1=str(e)),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_receipts_reconcile')
@admin_required
async def receipts_reconcile_menu_callback(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    """Меню выбора периода сверки."""

    # Очищаем состояние на случай если остался ввод даты
    await state.clear()

    # Сразу показываем сверку по логам
    await _do_reconcile_logs(callback, db_user=db_user)


async def _do_reconcile_logs(callback: CallbackQuery, db_user: User | None = None):
    """Внутренняя функция сверки по логам."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        import re
        from collections import defaultdict
        from pathlib import Path

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_162', '🔄 Анализирую логи платежей...'), show_alert=False
        )

        # Путь к файлу логов платежей (logs/current/)
        log_file_path = await asyncio.to_thread(Path(settings.LOG_FILE).resolve)
        log_dir = log_file_path.parent
        current_dir = log_dir / 'current'
        payments_log = current_dir / settings.LOG_PAYMENTS_FILE

        if not await asyncio.to_thread(payments_log.exists):
            try:
                await callback.message.edit_text(
                    texts.t(
                        'ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_163',
                        '❌ <b>Файл логов не найден</b>\n\nПуть: <code>{value1}</code>\n\n<i>Логи появятся после первого успешного платежа.</i>',
                    ).format(value1=payments_log),
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=texts.t('ADMIN_MONITORING_UI_GET_MONITORING_LOGS_KEYBOARD_190', '🔄 Обновить'),
                                    callback_data='admin_mon_reconcile_logs',
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                                    callback_data='admin_mon_statistics',
                                )
                            ],
                        ]
                    ),
                )
            except TelegramBadRequest:
                pass  # Сообщение не изменилось
            return

        # Паттерны для парсинга логов
        # Успешный платёж: "Успешно обработан платеж YooKassa 30e3c6fc-000f-5001-9000-1a9c8b242396: пользователь 1046 пополнил баланс на 200.0₽"
        payment_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}.*Успешно обработан платеж YooKassa ([a-f0-9-]+).*на ([\d.]+)₽'
        )
        # Чек создан: "Чек NaloGO создан для платежа 30e3c6fc-000f-5001-9000-1a9c8b242396: 243udsqtik"
        receipt_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}.*Чек NaloGO создан для платежа ([a-f0-9-]+): (\w+)'
        )

        # Читаем и парсим логи
        payments = {}  # payment_id -> {date, amount}
        receipts = {}  # payment_id -> {date, receipt_uuid}

        try:
            with open(payments_log, encoding='utf-8') as f:
                for line in f:
                    # Проверяем платежи
                    match = payment_pattern.search(line)
                    if match:
                        date_str, payment_id, amount = match.groups()
                        payments[payment_id] = {'date': date_str, 'amount': float(amount)}
                        continue

                    # Проверяем чеки
                    match = receipt_pattern.search(line)
                    if match:
                        date_str, payment_id, receipt_uuid = match.groups()
                        receipts[payment_id] = {'date': date_str, 'receipt_uuid': receipt_uuid}
        except Exception as e:
            logger.error('Ошибка чтения логов', error=e)
            await callback.message.edit_text(
                texts.t(
                    'ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_168', '❌ <b>Ошибка чтения логов</b>\n\n{value1}'
                ).format(value1=str(e)),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                                callback_data='admin_mon_statistics',
                            )
                        ]
                    ]
                ),
            )
            return

        # Находим платежи без чеков
        payments_without_receipts = []
        for payment_id, payment_data in payments.items():
            if payment_id not in receipts:
                payments_without_receipts.append(
                    {'payment_id': payment_id, 'date': payment_data['date'], 'amount': payment_data['amount']}
                )

        # Группируем по датам
        by_date = defaultdict(list)
        for p in payments_without_receipts:
            by_date[p['date']].append(p)

        # Формируем отчёт
        total_payments = len(payments)
        total_receipts = len(receipts)
        missing_count = len(payments_without_receipts)
        missing_amount = sum(p['amount'] for p in payments_without_receipts)

        text = texts.t('ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_170', '📋 <b>Сверка по логам</b>\n\n')
        text += texts.t('ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_171', '📦 <b>Всего платежей:</b> {value1}\n').format(
            value1=total_payments
        )
        text += texts.t('ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_172', '🧾 <b>Чеков создано:</b> {value1}\n\n').format(
            value1=total_receipts
        )

        if missing_count == 0:
            text += texts.t('ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_173', '✅ <b>Все платежи имеют чеки!</b>')
        else:
            text += texts.t(
                'ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_174', '⚠️ <b>Без чеков:</b> {value1} платежей на {value2} ₽\n\n'
            ).format(value1=missing_count, value2=format(missing_amount, ',.2f'))

            # Показываем по датам (последние)
            sorted_dates = sorted(by_date.keys(), reverse=True)
            for date_str in sorted_dates[:7]:
                date_payments = by_date[date_str]
                date_amount = sum(p['amount'] for p in date_payments)
                text += texts.t(
                    'ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_175', '• <b>{value1}:</b> {value2} шт. на {value3} ₽\n'
                ).format(value1=date_str, value2=len(date_payments), value3=format(date_amount, ',.2f'))

            if len(sorted_dates) > 7:
                text += texts.t('ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_176', '\n<i>...и ещё {value1} дней</i>').format(
                    value1=len(sorted_dates) - 7
                )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_GET_MONITORING_LOGS_KEYBOARD_190', '🔄 Обновить'),
                        callback_data='admin_mon_reconcile_logs',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_178', '📄 Детали'),
                        callback_data='admin_mon_reconcile_logs_details',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                        callback_data='admin_mon_statistics',
                    )
                ],
            ]
        )

        try:
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass  # Сообщение не изменилось

    except TelegramBadRequest:
        pass  # Игнорируем если сообщение не изменилось
    except Exception as e:
        logger.error('Ошибка сверки по логам', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_081', '❌ Ошибка: {value1}').format(value1=str(e)),
            show_alert=True,
        )


@router.callback_query(F.data == 'admin_mon_reconcile_logs')
@admin_required
async def receipts_reconcile_logs_refresh_callback(callback: CallbackQuery, db_user: User | None = None):
    """Обновить сверку по логам."""
    await _do_reconcile_logs(callback, db_user=db_user)


@router.callback_query(F.data == 'admin_mon_reconcile_logs_details')
@admin_required
async def receipts_reconcile_logs_details_callback(callback: CallbackQuery, db_user: User | None = None):
    """Детальный список платежей без чеков."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        import re
        from pathlib import Path

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_RECEIPTS_RECONCILE_LOGS_DETAILS_CALLBACK_181', '🔄 Загружаю детали...'),
            show_alert=False,
        )

        # Путь к логам (logs/current/)
        log_file_path = await asyncio.to_thread(Path(settings.LOG_FILE).resolve)
        log_dir = log_file_path.parent
        current_dir = log_dir / 'current'
        payments_log = current_dir / settings.LOG_PAYMENTS_FILE

        if not await asyncio.to_thread(payments_log.exists):
            await callback.answer(
                texts.t('ADMIN_MONITORING_UI_RECEIPTS_RECONCILE_LOGS_DETAILS_CALLBACK_182', '❌ Файл логов не найден'),
                show_alert=True,
            )
            return

        payment_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}).*Успешно обработан платеж YooKassa ([a-f0-9-]+).*пользователь (\d+).*на ([\d.]+)₽'
        )
        receipt_pattern = re.compile(r'Чек NaloGO создан для платежа ([a-f0-9-]+)')

        payments = {}
        receipts = set()

        with open(payments_log, encoding='utf-8') as f:
            for line in f:
                match = payment_pattern.search(line)
                if match:
                    date_str, time_str, payment_id, user_id, amount = match.groups()
                    payments[payment_id] = {
                        'date': date_str,
                        'time': time_str,
                        'user_id': user_id,
                        'amount': float(amount),
                    }
                    continue

                match = receipt_pattern.search(line)
                if match:
                    receipts.add(match.group(1))

        # Платежи без чеков
        missing = []
        for payment_id, data in payments.items():
            if payment_id not in receipts:
                missing.append({'payment_id': payment_id, **data})

        # Сортируем по дате (новые сверху)
        missing.sort(key=lambda x: (x['date'], x['time']), reverse=True)

        if not missing:
            text = texts.t('ADMIN_MONITORING_UI_DO_RECONCILE_LOGS_173', '✅ <b>Все платежи имеют чеки!</b>')
        else:
            text = texts.t(
                'ADMIN_MONITORING_UI_RECEIPTS_RECONCILE_LOGS_DETAILS_CALLBACK_186',
                '📄 <b>Платежи без чеков ({value1} шт.)</b>\n\n',
            ).format(value1=len(missing))

            for p in missing[:20]:
                text += (
                    f'• <b>{p["date"]} {p["time"]}</b>\n'
                    f'  User: {p["user_id"]} | {p["amount"]:.0f}₽\n'
                    f'  <code>{p["payment_id"][:18]}...</code>\n\n'
                )

            if len(missing) > 20:
                text += texts.t(
                    'ADMIN_MONITORING_UI_RECEIPTS_RECONCILE_LOGS_DETAILS_CALLBACK_187',
                    '<i>...и ещё {value1} платежей</i>',
                ).format(value1=len(missing) - 20)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                        callback_data='admin_mon_reconcile_logs',
                    )
                ],
            ]
        )

        try:
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass

    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.error('Ошибка детализации', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_081', '❌ Ошибка: {value1}').format(value1=str(e)),
            show_alert=True,
        )


def get_monitoring_logs_keyboard(current_page: int, total_pages: int, language: str = 'ru'):
    texts = get_texts(language)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = []

    if total_pages > 1:
        nav_row = []

        if current_page > 1:
            nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'admin_mon_logs_page_{current_page - 1}'))

        nav_row.append(InlineKeyboardButton(text=f'{current_page}/{total_pages}', callback_data='current_page'))

        if current_page < total_pages:
            nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'admin_mon_logs_page_{current_page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_GET_MONITORING_LOGS_KEYBOARD_190', '🔄 Обновить'),
                    callback_data='admin_mon_logs',
                ),
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_GET_MONITORING_LOGS_KEYBOARD_191', '🗑️ Очистить'),
                    callback_data='admin_mon_clear_logs',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                    callback_data='admin_monitoring',
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_monitoring_logs_back_keyboard(language: str = 'ru'):
    texts = get_texts(language)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_GET_MONITORING_LOGS_KEYBOARD_190', '🔄 Обновить'),
                    callback_data='admin_mon_logs',
                ),
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_GET_MONITORING_LOGS_BACK_KEYBOARD_194', '🔍 Фильтры'),
                    callback_data='admin_mon_logs_filters',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_GET_MONITORING_LOGS_BACK_KEYBOARD_195', '🗑️ Очистить логи'),
                    callback_data='admin_mon_clear_logs',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                    callback_data='admin_monitoring',
                )
            ],
        ]
    )


@router.message(Command('monitoring'))
@admin_required
async def monitoring_command(message: Message, db_user: User | None = None):
    language = _monitoring_language(message, db_user)
    texts = get_texts(language)
    try:
        async with AsyncSessionLocal() as db:
            status = await monitoring_service.get_monitoring_status(db)

            running_status = (
                texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_022', '🟢 Работает')
                if status['is_running']
                else texts.t('ADMIN_MONITORING_UI_ADMIN_MONITORING_MENU_023', '🔴 Остановлен')
            )

            text = texts.t(
                'ADMIN_MONITORING_UI_MONITORING_COMMAND_199',
                '\n🔍 <b>Быстрый статус мониторинга</b>\n\n📊 <b>Статус:</b> {value1}\n📈 <b>События за 24ч:</b> {value2}\n✅ <b>Успешность:</b> {value3}%\n\nДля подробного управления используйте админ-панель.\n',
            ).format(
                value1=running_status,
                value2=status['stats_24h']['total_events'],
                value3=status['stats_24h']['success_rate'],
            )

            await message.answer(text, parse_mode='HTML')

    except Exception as e:
        logger.error('Ошибка команды /monitoring', error=e)
        await message.answer(
            texts.t('ADMIN_MONITORING_UI_TRAFFIC_CHECK_CALLBACK_081', '❌ Ошибка: {value1}').format(value1=str(e))
        )


@router.message(AdminStates.editing_notification_value)
async def process_notification_value_input(message: Message, state: FSMContext, db_user: User | None = None):
    language = _monitoring_language(message, db_user)
    texts = get_texts(language)
    data = await state.get_data()
    if not data:
        await state.clear()
        await message.answer(
            texts.t(
                'ADMIN_MONITORING_UI_PROCESS_NOTIFICATION_VALUE_INPUT_201',
                'ℹ️ Контекст утерян, попробуйте снова из меню настроек.',
            )
        )
        return

    raw_value = (message.text or '').strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        language = _monitoring_language(message, db_user, data.get('settings_language'))
        texts = get_texts(language)
        await message.answer(texts.get('NOTIFICATION_VALUE_INVALID', '❌ Введите целое число.'))
        return

    key = data.get('notification_setting_key')
    field = data.get('notification_setting_field')
    language = _monitoring_language(message, db_user, data.get('settings_language'))
    texts = get_texts(language)

    # Добавляем дополнительные проверки диапазона значений
    if (key == 'expired_second_wave' and field == 'percent') or (key == 'expired_third_wave' and field == 'percent'):
        if value < 0 or value > 100:
            await message.answer(
                texts.t(
                    'ADMIN_MONITORING_UI_PROCESS_NOTIFICATION_VALUE_INPUT_202',
                    '❌ Процент скидки должен быть от 0 до 100.',
                )
            )
            return
    elif (key == 'expired_second_wave' and field == 'hours') or (key == 'expired_third_wave' and field == 'hours'):
        if value < 1 or value > 168:  # Максимум 168 часов (7 дней)
            await message.answer(
                texts.t(
                    'ADMIN_MONITORING_UI_PROCESS_NOTIFICATION_VALUE_INPUT_203',
                    '❌ Количество часов должно быть от 1 до 168.',
                )
            )
            return
    elif key == 'expired_third_wave' and field == 'trigger':
        if value < 2:  # Минимум 2 дня
            await message.answer(
                texts.t(
                    'ADMIN_MONITORING_UI_PROCESS_NOTIFICATION_VALUE_INPUT_204',
                    '❌ Количество дней должно быть не менее 2.',
                )
            )
            return

    setters = {
        ('expired_second_wave', 'percent'): NotificationSettingsService.set_second_wave_discount_percent,
        ('expired_second_wave', 'hours'): NotificationSettingsService.set_second_wave_valid_hours,
        ('expired_third_wave', 'percent'): NotificationSettingsService.set_third_wave_discount_percent,
        ('expired_third_wave', 'hours'): NotificationSettingsService.set_third_wave_valid_hours,
        ('expired_third_wave', 'trigger'): NotificationSettingsService.set_third_wave_trigger_days,
    }
    setter = setters.get((key, field))
    success = False
    if setter is not None:
        async with AsyncSessionLocal() as db:
            success = await setter(db, value)

    if not success:
        await message.answer(texts.get('NOTIFICATION_VALUE_INVALID', '❌ Некорректное значение, попробуйте снова.'))
        return

    back_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.get('BACK', '⬅️ Назад'),
                    callback_data='admin_mon_notify_settings',
                )
            ]
        ]
    )

    await message.answer(
        texts.get('NOTIFICATION_VALUE_UPDATED', '✅ Настройки обновлены.'),
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


# ============== Настройки мониторинга трафика ==============


def _format_traffic_toggle(enabled: bool, language: str = 'ru') -> str:
    texts = get_texts(language)
    return (
        texts.t('ADMIN_MONITORING_UI_FORMAT_TOGGLE_000', '🟢 Вкл')
        if enabled
        else texts.t('ADMIN_MONITORING_UI_FORMAT_TOGGLE_001', '🔴 Выкл')
    )


def _build_traffic_settings_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    """Строит клавиатуру настроек мониторинга трафика."""
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
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_KEYBOARD_207', '{value1} Быстрая проверка'
                    ).format(value1=_format_traffic_toggle(fast_enabled, language=language)),
                    callback_data='admin_traffic_toggle_fast',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_KEYBOARD_208', '⏱ Интервал: {value1} мин'
                    ).format(value1=fast_interval),
                    callback_data='admin_traffic_edit_fast_interval',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_KEYBOARD_209', '📊 Порог дельты: {value1} ГБ'
                    ).format(value1=fast_threshold),
                    callback_data='admin_traffic_edit_fast_threshold',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_KEYBOARD_210', '{value1} Суточная проверка'
                    ).format(value1=_format_traffic_toggle(daily_enabled, language=language)),
                    callback_data='admin_traffic_toggle_daily',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_KEYBOARD_211', '🕐 Время проверки: {value1}'
                    ).format(value1=daily_time),
                    callback_data='admin_traffic_edit_daily_time',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_KEYBOARD_212', '📈 Суточный порог: {value1} ГБ'
                    ).format(value1=daily_threshold),
                    callback_data='admin_traffic_edit_daily_threshold',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_KEYBOARD_213', '⏳ Кулдаун: {value1} мин'
                    ).format(value1=cooldown),
                    callback_data='admin_traffic_edit_cooldown',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MONITORING_UI_NALOGO_PENDING_CALLBACK_137', '⬅️ Назад'),
                    callback_data='admin_monitoring',
                )
            ],
        ]
    )


def _build_traffic_settings_text(language: str = 'ru') -> str:
    """Строит текст настроек мониторинга трафика."""
    texts = get_texts(language)
    fast_enabled = settings.TRAFFIC_FAST_CHECK_ENABLED
    daily_enabled = settings.TRAFFIC_DAILY_CHECK_ENABLED

    fast_status = _format_traffic_toggle(fast_enabled, language=language)
    daily_status = _format_traffic_toggle(daily_enabled, language=language)

    text = texts.t(
        'ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_TEXT_215',
        '⚙️ <b>Настройки мониторинга трафика</b>\n\n<b>Быстрая проверка:</b> {value1}\n• Интервал: {value2} мин\n• Порог дельты: {value3} ГБ\n\n<b>Суточная проверка:</b> {value4}\n• Время: {value5} UTC\n• Порог: {value6} ГБ\n\n<b>Общие:</b>\n• Кулдаун уведомлений: {value7} мин\n',
    ).format(
        value1=fast_status,
        value2=settings.TRAFFIC_FAST_CHECK_INTERVAL_MINUTES,
        value3=settings.TRAFFIC_FAST_CHECK_THRESHOLD_GB,
        value4=daily_status,
        value5=settings.TRAFFIC_DAILY_CHECK_TIME,
        value6=settings.TRAFFIC_DAILY_THRESHOLD_GB,
        value7=settings.TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES,
    )

    # Информация о фильтрах
    monitored_nodes = settings.get_traffic_monitored_nodes()
    ignored_nodes = settings.get_traffic_ignored_nodes()
    excluded_user_ids = settings.get_traffic_excluded_user_ids()

    if monitored_nodes:
        text += texts.t(
            'ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_TEXT_216', '• Мониторим только: {value1} нод(ы)\n'
        ).format(value1=len(monitored_nodes))
    if ignored_nodes:
        text += texts.t(
            'ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_TEXT_217', '• Игнорируем: {value1} нод(ы)\n'
        ).format(value1=len(ignored_nodes))
    if excluded_user_ids:
        text += texts.t('ADMIN_MONITORING_UI_BUILD_TRAFFIC_SETTINGS_TEXT_218', '• Исключено юзеров: {value1}\n').format(
            value1=len(excluded_user_ids)
        )

    return text


@router.callback_query(F.data == 'admin_mon_traffic_settings')
@admin_required
async def admin_traffic_settings(callback: CallbackQuery, db_user: User | None = None):
    """Показывает настройки мониторинга трафика."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        text = _build_traffic_settings_text(language=language)
        keyboard = _build_traffic_settings_keyboard(language=language)
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except Exception as e:
        logger.error('Ошибка отображения настроек трафика', error=e)
        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_ADMIN_TRAFFIC_SETTINGS_219', '❌ Ошибка загрузки настроек'), show_alert=True
        )


@router.callback_query(F.data == 'admin_traffic_toggle_fast')
@admin_required
async def toggle_fast_check(callback: CallbackQuery, db_user: User | None = None):
    """Переключает быструю проверку трафика."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        from app.services.system_settings_service import BotConfigurationService

        current = settings.TRAFFIC_FAST_CHECK_ENABLED
        new_value = not current

        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, 'TRAFFIC_FAST_CHECK_ENABLED', new_value)
            await db.commit()

        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_035', '✅ Включено')
            if new_value
            else texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_036', '⏸️ Отключено')
        )

        # Обновляем отображение
        text = _build_traffic_settings_text(language=language)
        keyboard = _build_traffic_settings_keyboard(language=language)
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка переключения быстрой проверки', error=e)
        await callback.answer(texts.t('ADMIN_MONITORING_UI_TOGGLE_FAST_CHECK_222', '❌ Ошибка'), show_alert=True)


@router.callback_query(F.data == 'admin_traffic_toggle_daily')
@admin_required
async def toggle_daily_check(callback: CallbackQuery, db_user: User | None = None):
    """Переключает суточную проверку трафика."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    try:
        from app.services.system_settings_service import BotConfigurationService

        current = settings.TRAFFIC_DAILY_CHECK_ENABLED
        new_value = not current

        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, 'TRAFFIC_DAILY_CHECK_ENABLED', new_value)
            await db.commit()

        await callback.answer(
            texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_035', '✅ Включено')
            if new_value
            else texts.t('ADMIN_MONITORING_UI_TOGGLE_TRIAL_CHANNEL_NOTIFICATION_036', '⏸️ Отключено')
        )

        text = _build_traffic_settings_text(language=language)
        keyboard = _build_traffic_settings_keyboard(language=language)
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка переключения суточной проверки', error=e)
        await callback.answer(texts.t('ADMIN_MONITORING_UI_TOGGLE_FAST_CHECK_222', '❌ Ошибка'), show_alert=True)


@router.callback_query(F.data == 'admin_traffic_edit_fast_interval')
@admin_required
async def edit_fast_interval(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    """Начинает редактирование интервала быстрой проверки."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_FAST_CHECK_INTERVAL_MINUTES',
        traffic_setting_type='int',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    await callback.message.answer(
        texts.t(
            'ADMIN_MONITORING_UI_EDIT_FAST_INTERVAL_226', '⏱ Введите интервал быстрой проверки в минутах (минимум 1):'
        )
    )


@router.callback_query(F.data == 'admin_traffic_edit_fast_threshold')
@admin_required
async def edit_fast_threshold(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    """Начинает редактирование порога быстрой проверки."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_FAST_CHECK_THRESHOLD_GB',
        traffic_setting_type='float',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    await callback.message.answer(
        texts.t('ADMIN_MONITORING_UI_EDIT_FAST_THRESHOLD_227', '📊 Введите порог дельты трафика в ГБ (например: 5.0):')
    )


@router.callback_query(F.data == 'admin_traffic_edit_daily_time')
@admin_required
async def edit_daily_time(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    """Начинает редактирование времени суточной проверки."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_DAILY_CHECK_TIME',
        traffic_setting_type='time',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    await callback.message.answer(
        texts.t(
            'ADMIN_MONITORING_UI_EDIT_DAILY_TIME_228',
            '🕐 Введите время суточной проверки в формате HH:MM (в часовом поясе бота):\nНапример: 00:00, 03:00, 12:30',
        )
    )


@router.callback_query(F.data == 'admin_traffic_edit_daily_threshold')
@admin_required
async def edit_daily_threshold(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    """Начинает редактирование суточного порога."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_DAILY_THRESHOLD_GB',
        traffic_setting_type='float',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    await callback.message.answer(
        texts.t(
            'ADMIN_MONITORING_UI_EDIT_DAILY_THRESHOLD_229', '📈 Введите суточный порог трафика в ГБ (например: 50.0):'
        )
    )


@router.callback_query(F.data == 'admin_traffic_edit_cooldown')
@admin_required
async def edit_cooldown(callback: CallbackQuery, state: FSMContext, db_user: User | None = None):
    """Начинает редактирование кулдауна уведомлений."""
    language = _monitoring_language(callback, db_user)
    texts = get_texts(language)
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES',
        traffic_setting_type='int',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    await callback.message.answer(
        texts.t('ADMIN_MONITORING_UI_EDIT_COOLDOWN_230', '⏳ Введите кулдаун уведомлений в минутах (минимум 1):')
    )


@router.message(AdminStates.editing_traffic_setting)
async def process_traffic_setting_input(message: Message, state: FSMContext, db_user: User | None = None):
    """Обрабатывает ввод настройки мониторинга трафика."""
    language = _monitoring_language(message, db_user)
    texts = get_texts(language)
    from app.services.system_settings_service import BotConfigurationService

    data = await state.get_data()
    if not data:
        await state.clear()
        await message.answer(
            texts.t(
                'ADMIN_MONITORING_UI_PROCESS_NOTIFICATION_VALUE_INPUT_201',
                'ℹ️ Контекст утерян, попробуйте снова из меню настроек.',
            )
        )
        return

    raw_value = (message.text or '').strip()
    setting_key = data.get('traffic_setting_key')
    setting_type = data.get('traffic_setting_type')

    # Валидация и парсинг значения
    try:
        if setting_type == 'int':
            value = int(raw_value)
            if value < 1:
                raise ValueError(
                    texts.t('ADMIN_MONITORING_UI_PROCESS_TRAFFIC_SETTING_INPUT_232', 'Значение должно быть >= 1')
                )
        elif setting_type == 'float':
            value = float(raw_value.replace(',', '.'))
            if value <= 0:
                raise ValueError(
                    texts.t('ADMIN_MONITORING_UI_PROCESS_TRAFFIC_SETTING_INPUT_233', 'Значение должно быть > 0')
                )
        elif setting_type == 'time':
            # Валидация формата HH:MM
            import re

            if not re.match(r'^\d{1,2}:\d{2}$', raw_value):
                raise ValueError(
                    texts.t(
                        'ADMIN_MONITORING_UI_PROCESS_TRAFFIC_SETTING_INPUT_234',
                        'Неверный формат времени. Используйте HH:MM',
                    )
                )
            parts = raw_value.split(':')
            hours, minutes = int(parts[0]), int(parts[1])
            if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
                raise ValueError(texts.t('ADMIN_MONITORING_UI_PROCESS_TRAFFIC_SETTING_INPUT_235', 'Неверное время'))
            value = f'{hours:02d}:{minutes:02d}'
        else:
            value = raw_value
    except ValueError as e:
        await message.answer(f'❌ {e!s}')
        return

    # Сохраняем значение
    try:
        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, setting_key, value)
            await db.commit()

        back_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_UI_PROCESS_TRAFFIC_SETTING_INPUT_236', '⬅️ К настройкам трафика'),
                        callback_data='admin_mon_traffic_settings',
                    )
                ]
            ]
        )
        await message.answer(
            texts.t('ADMIN_MONITORING_UI_PROCESS_TRAFFIC_SETTING_INPUT_237', '✅ Настройка сохранена!'),
            reply_markup=back_keyboard,
        )

        # Обновляем исходное сообщение с настройками
        chat_id = data.get('settings_message_chat')
        message_id = data.get('settings_message_id')
        if chat_id and message_id:
            try:
                text = _build_traffic_settings_text(language=language)
                keyboard = _build_traffic_settings_keyboard(language=language)
                await message.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text, parse_mode='HTML', reply_markup=keyboard
                )
            except Exception:
                pass  # Игнорируем если сообщение уже удалено

    except Exception as e:
        logger.error('Ошибка сохранения настройки трафика', error=e)
        await message.answer(
            texts.t('ADMIN_MONITORING_UI_PROCESS_TRAFFIC_SETTING_INPUT_238', '❌ Ошибка сохранения: {value1}').format(
                value1=str(e)
            )
        )

    await state.clear()


def register_handlers(dp):
    dp.include_router(router)
