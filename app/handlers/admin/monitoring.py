import asyncio
import logging
from datetime import datetime, timedelta

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


logger = logging.getLogger(__name__)
router = Router()


def _format_toggle(enabled: bool, texts) -> str:
    return texts.t('ADMIN_MON_TOGGLE_ON') if enabled else texts.t('ADMIN_MON_TOGGLE_OFF')


def _build_notification_settings_view(language: str):
    texts = get_texts(language)
    config = NotificationSettingsService.get_config()

    second_percent = NotificationSettingsService.get_second_wave_discount_percent()
    second_hours = NotificationSettingsService.get_second_wave_valid_hours()
    third_percent = NotificationSettingsService.get_third_wave_discount_percent()
    third_hours = NotificationSettingsService.get_third_wave_valid_hours()
    third_days = NotificationSettingsService.get_third_wave_trigger_days()

    trial_1h_status = _format_toggle(config['trial_inactive_1h'].get('enabled', True), texts)
    trial_24h_status = _format_toggle(config['trial_inactive_24h'].get('enabled', True), texts)
    trial_channel_status = _format_toggle(config['trial_channel_unsubscribed'].get('enabled', True), texts)
    expired_1d_status = _format_toggle(config['expired_1d'].get('enabled', True), texts)
    second_wave_status = _format_toggle(config['expired_second_wave'].get('enabled', True), texts)
    third_wave_status = _format_toggle(config['expired_third_wave'].get('enabled', True), texts)

    summary_text = texts.t('ADMIN_MON_NOTIFY_SUMMARY').format(
        trial_1h_status=trial_1h_status,
        trial_24h_status=trial_24h_status,
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
                    text=texts.t('ADMIN_MON_NOTIFY_TOGGLE_TRIAL_1H').format(status=trial_1h_status),
                    callback_data='admin_mon_notify_toggle_trial_1h',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TEST_TRIAL_1H'),
                    callback_data='admin_mon_notify_preview_trial_1h',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TOGGLE_TRIAL_24H').format(status=trial_24h_status),
                    callback_data='admin_mon_notify_toggle_trial_24h',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TEST_TRIAL_24H'),
                    callback_data='admin_mon_notify_preview_trial_24h',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TOGGLE_TRIAL_CHANNEL').format(status=trial_channel_status),
                    callback_data='admin_mon_notify_toggle_trial_channel',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TEST_TRIAL_CHANNEL'),
                    callback_data='admin_mon_notify_preview_trial_channel',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TOGGLE_EXPIRED_1D').format(status=expired_1d_status),
                    callback_data='admin_mon_notify_toggle_expired_1d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TEST_EXPIRED_1D'),
                    callback_data='admin_mon_notify_preview_expired_1d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TOGGLE_EXPIRED_2D').format(status=second_wave_status),
                    callback_data='admin_mon_notify_toggle_expired_2d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TEST_EXPIRED_2D'),
                    callback_data='admin_mon_notify_preview_expired_2d',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_EDIT_2D_PERCENT').format(percent=second_percent),
                    callback_data='admin_mon_notify_edit_2d_percent',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_EDIT_2D_HOURS').format(hours=second_hours),
                    callback_data='admin_mon_notify_edit_2d_hours',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TOGGLE_EXPIRED_ND').format(
                        status=third_wave_status,
                        days=third_days,
                    ),
                    callback_data='admin_mon_notify_toggle_expired_nd',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_TEST_EXPIRED_ND'),
                    callback_data='admin_mon_notify_preview_expired_nd',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_EDIT_ND_PERCENT').format(days=third_days, percent=third_percent),
                    callback_data='admin_mon_notify_edit_nd_percent',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_EDIT_ND_HOURS').format(days=third_days, hours=third_hours),
                    callback_data='admin_mon_notify_edit_nd_hours',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_EDIT_ND_THRESHOLD').format(days=third_days),
                    callback_data='admin_mon_notify_edit_nd_threshold',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NOTIFY_SEND_ALL_TESTS'), callback_data='admin_mon_notify_preview_all'
                )
            ],
            [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_settings')],
        ]
    )

    return summary_text, keyboard


def _build_notification_preview_message(language: str, notification_type: str):
    texts = get_texts(language)
    now = datetime.now()
    price_30_days = settings.format_price(settings.PRICE_30_DAYS)

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    header = texts.t('ADMIN_MON_NOTIFY_PREVIEW_HEADER')

    if notification_type == 'trial_inactive_1h':
        template = texts.t('TRIAL_INACTIVE_1H')
        message = template.format(
            price=price_30_days,
            end_date=(now + timedelta(days=settings.TRIAL_DURATION_DAYS)).strftime('%d.%m.%Y %H:%M'),
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('CONNECT_BUTTON'),
                        callback_data='subscription_connect',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('MY_SUBSCRIPTION_BUTTON'),
                        callback_data='menu_subscription',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    elif notification_type == 'trial_inactive_24h':
        template = texts.t('TRIAL_INACTIVE_24H')
        message = template.format(
            price=price_30_days,
            end_date=(now + timedelta(days=1)).strftime('%d.%m.%Y %H:%M'),
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('CONNECT_BUTTON'),
                        callback_data='subscription_connect',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('MY_SUBSCRIPTION_BUTTON'),
                        callback_data='menu_subscription',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    elif notification_type == 'trial_channel_unsubscribed':
        template = texts.t('TRIAL_CHANNEL_UNSUBSCRIBED')
        check_button = texts.t('CHANNEL_CHECK_BUTTON')
        message = template.format(check_button=check_button)
        buttons: list[list[InlineKeyboardButton]] = []
        if settings.CHANNEL_LINK:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('CHANNEL_SUBSCRIBE_BUTTON'),
                        url=settings.CHANNEL_LINK,
                    )
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=check_button,
                    callback_data='sub_channel_check',
                )
            ]
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    elif notification_type == 'expired_1d':
        template = texts.t('SUBSCRIPTION_EXPIRED_1D')
        message = template.format(
            end_date=(now - timedelta(days=1)).strftime('%d.%m.%Y %H:%M'),
            price=price_30_days,
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    elif notification_type == 'expired_2d':
        percent = NotificationSettingsService.get_second_wave_discount_percent()
        valid_hours = NotificationSettingsService.get_second_wave_valid_hours()
        template = texts.t('SUBSCRIPTION_EXPIRED_SECOND_WAVE')
        message = template.format(
            percent=percent,
            expires_at=(now + timedelta(hours=valid_hours)).strftime('%d.%m.%Y %H:%M'),
            trigger_days=3,
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_CLAIM_DISCOUNT_BUTTON'),
                        callback_data='claim_discount_preview',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    elif notification_type == 'expired_nd':
        percent = NotificationSettingsService.get_third_wave_discount_percent()
        valid_hours = NotificationSettingsService.get_third_wave_valid_hours()
        trigger_days = NotificationSettingsService.get_third_wave_trigger_days()
        template = texts.t('SUBSCRIPTION_EXPIRED_THIRD_WAVE')
        message = template.format(
            percent=percent,
            trigger_days=trigger_days,
            expires_at=(now + timedelta(hours=valid_hours)).strftime('%d.%m.%Y %H:%M'),
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_CLAIM_DISCOUNT_BUTTON'),
                        callback_data='claim_discount_preview',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_EXTEND'),
                        callback_data='subscription_extend',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('BALANCE_TOPUP'),
                        callback_data='balance_topup',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('SUPPORT_BUTTON'),
                        callback_data='menu_support',
                    )
                ],
            ]
        )
    else:
        raise ValueError(f'Unsupported notification type: {notification_type}')

    footer = texts.t('ADMIN_MON_NOTIFY_PREVIEW_FOOTER')
    return header + message + footer, keyboard


async def _send_notification_preview(bot, chat_id: int, language: str, notification_type: str) -> None:
    message, keyboard = _build_notification_preview_message(language, notification_type)
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
                texts.t('ADMIN_MON_STATUS_RUNNING') if status['is_running'] else texts.t('ADMIN_MON_STATUS_STOPPED')
            )
            last_update = (
                status['last_update'].strftime('%H:%M:%S')
                if status['last_update']
                else texts.t('ADMIN_MON_LAST_UPDATE_NEVER')
            )

            text = texts.t('ADMIN_MON_MENU_TEXT').format(
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
        logger.error(f'Ошибка в админ меню мониторинга: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_ERROR_FETCH_DATA'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_settings')
@admin_required
async def admin_monitoring_settings(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        global_status = (
            texts.t('ADMIN_MON_NOTIFICATIONS_ENABLED')
            if NotificationSettingsService.are_notifications_globally_enabled()
            else texts.t('ADMIN_MON_NOTIFICATIONS_DISABLED')
        )
        second_percent = NotificationSettingsService.get_second_wave_discount_percent()
        third_percent = NotificationSettingsService.get_third_wave_discount_percent()
        third_days = NotificationSettingsService.get_third_wave_trigger_days()

        text = texts.t('ADMIN_MON_SETTINGS_TEXT').format(
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
                        text=texts.t('ADMIN_MON_NOTIFY_SETTINGS_BUTTON'), callback_data='admin_mon_notify_settings'
                    )
                ],
                [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_submenu_settings')],
            ]
        )

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка отображения настроек мониторинга: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_ERROR_OPEN_SETTINGS'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_settings')
@admin_required
async def admin_notify_settings(callback: CallbackQuery):
    try:
        await _render_notification_settings(callback)
    except Exception as e:
        logger.error(f'Ошибка отображения настроек уведомлений: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_ERROR_LOAD_SETTINGS'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_trial_1h')
@admin_required
async def toggle_trial_1h_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_trial_inactive_1h_enabled()
    NotificationSettingsService.set_trial_inactive_1h_enabled(not enabled)
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.answer(texts.t('ADMIN_MON_TOGGLE_ENABLED') if not enabled else texts.t('ADMIN_MON_TOGGLE_DISABLED'))
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_trial_1h')
@admin_required
async def preview_trial_1h_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'trial_inactive_1h')
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SENT'))
    except Exception as exc:
        logger.error('Failed to send trial 1h preview: %s', exc)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SEND_FAILED'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_trial_24h')
@admin_required
async def toggle_trial_24h_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_trial_inactive_24h_enabled()
    NotificationSettingsService.set_trial_inactive_24h_enabled(not enabled)
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.answer(texts.t('ADMIN_MON_TOGGLE_ENABLED') if not enabled else texts.t('ADMIN_MON_TOGGLE_DISABLED'))
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_trial_24h')
@admin_required
async def preview_trial_24h_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'trial_inactive_24h')
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SENT'))
    except Exception as exc:
        logger.error('Failed to send trial 24h preview: %s', exc)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SEND_FAILED'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_trial_channel')
@admin_required
async def toggle_trial_channel_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_trial_channel_unsubscribed_enabled()
    NotificationSettingsService.set_trial_channel_unsubscribed_enabled(not enabled)
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.answer(texts.t('ADMIN_MON_TOGGLE_ENABLED') if not enabled else texts.t('ADMIN_MON_TOGGLE_DISABLED'))
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_trial_channel')
@admin_required
async def preview_trial_channel_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'trial_channel_unsubscribed')
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SENT'))
    except Exception as exc:
        logger.error('Failed to send trial channel preview: %s', exc)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SEND_FAILED'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_1d')
@admin_required
async def toggle_expired_1d_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_expired_1d_enabled()
    NotificationSettingsService.set_expired_1d_enabled(not enabled)
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.answer(texts.t('ADMIN_MON_TOGGLE_ENABLED') if not enabled else texts.t('ADMIN_MON_TOGGLE_DISABLED'))
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_1d')
@admin_required
async def preview_expired_1d_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_1d')
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SENT'))
    except Exception as exc:
        logger.error('Failed to send expired 1d preview: %s', exc)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SEND_FAILED'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_2d')
@admin_required
async def toggle_second_wave_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_second_wave_enabled()
    NotificationSettingsService.set_second_wave_enabled(not enabled)
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.answer(texts.t('ADMIN_MON_TOGGLE_ENABLED') if not enabled else texts.t('ADMIN_MON_TOGGLE_DISABLED'))
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_2d')
@admin_required
async def preview_second_wave_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_2d')
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SENT'))
    except Exception as exc:
        logger.error('Failed to send second wave preview: %s', exc)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SEND_FAILED'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_toggle_expired_nd')
@admin_required
async def toggle_third_wave_notification(callback: CallbackQuery):
    enabled = NotificationSettingsService.is_third_wave_enabled()
    NotificationSettingsService.set_third_wave_enabled(not enabled)
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.answer(texts.t('ADMIN_MON_TOGGLE_ENABLED') if not enabled else texts.t('ADMIN_MON_TOGGLE_DISABLED'))
    await _render_notification_settings(callback)


@router.callback_query(F.data == 'admin_mon_notify_preview_expired_nd')
@admin_required
async def preview_third_wave_notification(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await _send_notification_preview(callback.bot, callback.from_user.id, language, 'expired_nd')
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SENT'))
    except Exception as exc:
        logger.error('Failed to send third wave preview: %s', exc)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_SEND_FAILED'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_notify_preview_all')
@admin_required
async def preview_all_notifications(callback: CallbackQuery):
    try:
        language = callback.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        chat_id = callback.from_user.id
        for notification_type in [
            'trial_inactive_1h',
            'trial_inactive_24h',
            'trial_channel_unsubscribed',
            'expired_1d',
            'expired_2d',
            'expired_nd',
        ]:
            await _send_notification_preview(callback.bot, chat_id, language, notification_type)
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_ALL_SENT'))
    except Exception as exc:
        logger.error('Failed to send all notification previews: %s', exc)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_PREVIEW_ALL_FAILED'), show_alert=True)


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
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
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
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
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
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
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
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await _start_notification_value_edit(
        callback,
        state,
        'expired_third_wave',
        'hours',
        'NOTIFY_PROMPT_THIRD_HOURS',
        texts.t('NOTIFY_PROMPT_THIRD_HOURS'),
    )


@router.callback_query(F.data == 'admin_mon_notify_edit_nd_threshold')
@admin_required
async def edit_third_wave_threshold(callback: CallbackQuery, state: FSMContext):
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
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
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        if monitoring_service.is_running:
            await callback.answer(texts.t('ADMIN_MON_ALREADY_RUNNING'))
            return

        if not monitoring_service.bot:
            monitoring_service.bot = callback.bot

        asyncio.create_task(monitoring_service.start_monitoring())

        await callback.answer(texts.t('ADMIN_MON_STARTED'))

        await admin_monitoring_menu(callback)

    except Exception as e:
        logger.error(f'Ошибка запуска мониторинга: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_START_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_mon_stop')
@admin_required
async def stop_monitoring_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        if not monitoring_service.is_running:
            await callback.answer(texts.t('ADMIN_MON_ALREADY_STOPPED'))
            return

        monitoring_service.stop_monitoring()
        await callback.answer(texts.t('ADMIN_MON_STOPPED'))

        await admin_monitoring_menu(callback)

    except Exception as e:
        logger.error(f'Ошибка остановки мониторинга: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_STOP_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_mon_force_check')
@admin_required
async def force_check_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_FORCE_CHECK_PROGRESS'))

        async with AsyncSessionLocal() as db:
            results = await monitoring_service.force_check_subscriptions(db)

            text = texts.t('ADMIN_MON_FORCE_CHECK_RESULT').format(
                expired=results['expired'],
                expiring=results['expiring'],
                autopay_ready=results['autopay_ready'],
                time=datetime.now().strftime('%H:%M:%S'),
            )

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')]]
            )

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка принудительной проверки: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_FORCE_CHECK_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_mon_traffic_check')
@admin_required
async def traffic_check_callback(callback: CallbackQuery):
    """Ручная проверка трафика — использует snapshot и дельту."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        # Проверяем, включен ли мониторинг трафика
        if not traffic_monitoring_scheduler.is_enabled():
            await callback.answer(
                texts.t('ADMIN_MON_TRAFFIC_DISABLED_ALERT'),
                show_alert=True,
            )
            return

        await callback.answer(texts.t('ADMIN_MON_TRAFFIC_CHECK_PROGRESS'))

        # Используем run_fast_check — он сравнивает с snapshot и отправляет уведомления
        from app.services.traffic_monitoring_service import traffic_monitoring_scheduler_v2

        # Устанавливаем бота, если не установлен
        if not traffic_monitoring_scheduler_v2.bot:
            traffic_monitoring_scheduler_v2.set_bot(callback.bot)

        violations = await traffic_monitoring_scheduler_v2.run_fast_check_now()

        # Получаем информацию о snapshot
        snapshot_age = await traffic_monitoring_scheduler_v2.service.get_snapshot_age_minutes()
        threshold_gb = traffic_monitoring_scheduler_v2.service.get_fast_check_threshold_gb()

        text = texts.t('ADMIN_MON_TRAFFIC_CHECK_RESULT').format(
            violations_count=len(violations),
            threshold_gb=threshold_gb,
            snapshot_age=f'{snapshot_age:.1f}',
            time=datetime.now().strftime('%H:%M:%S'),
        )

        if violations:
            text += texts.t('ADMIN_MON_TRAFFIC_VIOLATIONS_TITLE')
            for v in violations[:10]:
                name = v.full_name or v.user_uuid[:8]
                text += texts.t('ADMIN_MON_TRAFFIC_VIOLATION_LINE').format(
                    name=name,
                    used_traffic_gb=f'{v.used_traffic_gb:.1f}',
                )
            if len(violations) > 10:
                text += texts.t('ADMIN_MON_TRAFFIC_AND_MORE_LINE').format(count=len(violations) - 10)
            text += texts.t('ADMIN_MON_TRAFFIC_NOTIFICATIONS_SENT')
        else:
            text += texts.t('ADMIN_MON_TRAFFIC_NO_VIOLATIONS')

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_TRAFFIC_REPEAT_BUTTON'), callback_data='admin_mon_traffic_check'
                    )
                ],
                [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')],
            ]
        )

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка проверки трафика: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data.startswith('admin_mon_logs'))
@admin_required
async def monitoring_logs_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        page = 1
        if '_page_' in callback.data:
            page = int(callback.data.split('_page_')[1])

        async with AsyncSessionLocal() as db:
            all_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)

            if not all_logs:
                text = texts.t('ADMIN_MON_LOGS_EMPTY_TEXT')
                keyboard = get_monitoring_logs_back_keyboard(
                    callback.from_user.language_code or settings.DEFAULT_LANGUAGE
                )
                await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
                return

            per_page = 8
            paginated_logs = paginate_list(all_logs, page=page, per_page=per_page)

            text = texts.t('ADMIN_MON_LOGS_HEADER').format(
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
                text += f'   📄 {message}\n\n'

            total_success = sum(1 for log in all_logs if log['is_success'])
            total_failed = len(all_logs) - total_success
            success_rate = round(total_success / len(all_logs) * 100, 1) if all_logs else 0

            text += texts.t('ADMIN_MON_LOGS_TOTAL_STATS').format(
                total=len(all_logs),
                success=total_success,
                failed=total_failed,
                success_rate=success_rate,
            )

            keyboard = get_monitoring_logs_keyboard(
                page,
                paginated_logs.total_pages,
                callback.from_user.language_code or settings.DEFAULT_LANGUAGE,
            )
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка получения логов: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_LOGS_FETCH_ERROR'), show_alert=True)


@router.callback_query(F.data == 'admin_mon_clear_logs')
@admin_required
async def clear_logs_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        async with AsyncSessionLocal() as db:
            deleted_count = await monitoring_service.cleanup_old_logs(db, days=0)
            await db.commit()

            if deleted_count > 0:
                await callback.answer(texts.t('ADMIN_MON_LOGS_DELETED').format(count=deleted_count))
            else:
                await callback.answer(texts.t('ADMIN_MON_LOGS_ALREADY_EMPTY'))

            await monitoring_logs_callback(callback)

    except Exception as e:
        logger.error(f'Ошибка очистки логов: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_LOGS_CLEAR_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_mon_test_notifications')
@admin_required
async def test_notifications_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        monitoring_status = (
            texts.t('ADMIN_MON_STATUS_RUNNING')
            if monitoring_service.is_running
            else texts.t('ADMIN_MON_STATUS_STOPPED')
        )
        notifications_status = (
            texts.t('ADMIN_MON_NOTIFICATIONS_ENABLED')
            if settings.ENABLE_NOTIFICATIONS
            else texts.t('ADMIN_MON_NOTIFICATIONS_DISABLED')
        )
        test_message = texts.t('ADMIN_MON_TEST_MESSAGE').format(
            monitoring_status=monitoring_status,
            notifications_status=notifications_status,
            test_time=datetime.now().strftime('%H:%M:%S %d.%m.%Y'),
        )

        await callback.bot.send_message(callback.from_user.id, test_message, parse_mode='HTML')

        await callback.answer(texts.t('ADMIN_MON_TEST_SENT'))

    except Exception as e:
        logger.error(f'Ошибка отправки тестового уведомления: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_TEST_SEND_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_mon_statistics')
@admin_required
async def monitoring_statistics_callback(callback: CallbackQuery):
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        async with AsyncSessionLocal() as db:
            from app.database.crud.subscription import get_subscriptions_statistics

            sub_stats = await get_subscriptions_statistics(db)

            mon_status = await monitoring_service.get_monitoring_status(db)

            week_ago = datetime.now() - timedelta(days=7)
            week_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)
            week_logs = [log for log in week_logs if log['created_at'] >= week_ago]

            week_success = sum(1 for log in week_logs if log['is_success'])
            week_errors = len(week_logs) - week_success

            notifications_status = (
                texts.t('ADMIN_MON_TOGGLE_ON')
                if getattr(settings, 'ENABLE_NOTIFICATIONS', True)
                else texts.t('ADMIN_MON_TOGGLE_OFF')
            )
            text = texts.t('ADMIN_MON_STATS_TEXT').format(
                total_subscriptions=sub_stats['total_subscriptions'],
                active_subscriptions=sub_stats['active_subscriptions'],
                trial_subscriptions=sub_stats['trial_subscriptions'],
                paid_subscriptions=sub_stats['paid_subscriptions'],
                successful_today=mon_status['stats_24h']['successful'],
                failed_today=mon_status['stats_24h']['failed'],
                success_rate_today=mon_status['stats_24h']['success_rate'],
                total_events_week=len(week_logs),
                successful_week=week_success,
                failed_week=week_errors,
                success_rate_week=round(week_success / len(week_logs) * 100, 1) if week_logs else 0,
                interval=settings.MONITORING_INTERVAL,
                notifications_status=notifications_status,
                autopay_days=', '.join(map(str, settings.get_autopay_warning_days())),
            )

            # Добавляем информацию о чеках NaloGO
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                queue_len = nalogo_status.get('queue_length', 0)
                total_amount = nalogo_status.get('total_amount', 0)
                running = nalogo_status.get('running', False)
                pending_count = nalogo_status.get('pending_verification_count', 0)
                pending_amount = nalogo_status.get('pending_verification_amount', 0)

                nalogo_service_status = (
                    texts.t('ADMIN_MON_STATUS_RUNNING') if running else texts.t('ADMIN_MON_STATUS_STOPPED')
                )
                nalogo_section = texts.t('ADMIN_MON_NALOGO_SECTION').format(
                    service_status=nalogo_service_status,
                    queue_len=queue_len,
                )
                if queue_len > 0:
                    nalogo_section += texts.t('ADMIN_MON_NALOGO_TOTAL_AMOUNT_LINE').format(total_amount=total_amount)
                if pending_count > 0:
                    nalogo_section += texts.t('ADMIN_MON_NALOGO_PENDING_LINE').format(
                        pending_count=pending_count,
                        pending_amount=pending_amount,
                    )
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
                            text=texts.t('ADMIN_MON_NALOGO_SEND_BUTTON').format(count=nalogo_status['queue_length']),
                            callback_data='admin_mon_nalogo_force_process',
                        )
                    )
                pending_count = nalogo_status.get('pending_verification_count', 0)
                if pending_count > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MON_NALOGO_CHECK_BUTTON').format(count=pending_count),
                            callback_data='admin_mon_nalogo_pending',
                        )
                    )
                nalogo_buttons.append(
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_NALOGO_RECONCILE_BUTTON'),
                        callback_data='admin_mon_receipts_missing',
                    )
                )
                buttons.append(nalogo_buttons)

            buttons.append([InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка получения статистики: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_STATS_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_mon_nalogo_force_process')
@admin_required
async def nalogo_force_process_callback(callback: CallbackQuery):
    """Принудительная отправка чеков из очереди."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_NALOGO_PROCESS_PROGRESS'), show_alert=False)

        result = await nalogo_queue_service.force_process()

        if 'error' in result:
            await callback.answer(
                texts.t('ADMIN_MON_NALOGO_PROCESS_ERROR').format(error=result['error']), show_alert=True
            )
            return

        processed = result.get('processed', 0)
        remaining = result.get('remaining', 0)

        if processed > 0:
            text = texts.t('ADMIN_MON_NALOGO_PROCESSED').format(count=processed)
            if remaining > 0:
                text += texts.t('ADMIN_MON_NALOGO_REMAINING_LINE').format(count=remaining)
        elif remaining > 0:
            text = texts.t('ADMIN_MON_NALOGO_SERVICE_UNAVAILABLE').format(count=remaining)
        else:
            text = texts.t('ADMIN_MON_NALOGO_QUEUE_EMPTY')

        await callback.answer(text, show_alert=True)

        # Обновляем страницу статистики
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        # Перезагружаем статистику
        async with AsyncSessionLocal() as db:
            from app.database.crud.subscription import get_subscriptions_statistics

            sub_stats = await get_subscriptions_statistics(db)
            mon_status = await monitoring_service.get_monitoring_status(db)

            week_ago = datetime.now() - timedelta(days=7)
            week_logs = await monitoring_service.get_monitoring_logs(db, limit=1000)
            week_logs = [log for log in week_logs if log['created_at'] >= week_ago]
            week_success = sum(1 for log in week_logs if log['is_success'])
            week_errors = len(week_logs) - week_success

            notifications_status = (
                texts.t('ADMIN_MON_TOGGLE_ON')
                if getattr(settings, 'ENABLE_NOTIFICATIONS', True)
                else texts.t('ADMIN_MON_TOGGLE_OFF')
            )
            stats_text = texts.t('ADMIN_MON_STATS_TEXT').format(
                total_subscriptions=sub_stats['total_subscriptions'],
                active_subscriptions=sub_stats['active_subscriptions'],
                trial_subscriptions=sub_stats['trial_subscriptions'],
                paid_subscriptions=sub_stats['paid_subscriptions'],
                successful_today=mon_status['stats_24h']['successful'],
                failed_today=mon_status['stats_24h']['failed'],
                success_rate_today=mon_status['stats_24h']['success_rate'],
                total_events_week=len(week_logs),
                successful_week=week_success,
                failed_week=week_errors,
                success_rate_week=round(week_success / len(week_logs) * 100, 1) if week_logs else 0,
                interval=settings.MONITORING_INTERVAL,
                notifications_status=notifications_status,
                autopay_days=', '.join(map(str, settings.get_autopay_warning_days())),
            )

            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                queue_len = nalogo_status.get('queue_length', 0)
                total_amount = nalogo_status.get('total_amount', 0)
                running = nalogo_status.get('running', False)

                nalogo_service_status = (
                    texts.t('ADMIN_MON_STATUS_RUNNING') if running else texts.t('ADMIN_MON_STATUS_STOPPED')
                )
                nalogo_section = texts.t('ADMIN_MON_NALOGO_SECTION').format(
                    service_status=nalogo_service_status,
                    queue_len=queue_len,
                )
                if queue_len > 0:
                    nalogo_section += texts.t('ADMIN_MON_NALOGO_TOTAL_AMOUNT_LINE').format(total_amount=total_amount)
                stats_text += nalogo_section

            buttons = []
            # Кнопки для работы с чеками NaloGO
            if settings.is_nalogo_enabled():
                nalogo_status = await nalogo_queue_service.get_status()
                nalogo_buttons = []
                if nalogo_status.get('queue_length', 0) > 0:
                    nalogo_buttons.append(
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_MON_NALOGO_SEND_BUTTON').format(count=nalogo_status['queue_length']),
                            callback_data='admin_mon_nalogo_force_process',
                        )
                    )
                nalogo_buttons.append(
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_NALOGO_RECONCILE_BUTTON'),
                        callback_data='admin_mon_receipts_missing',
                    )
                )
                buttons.append(nalogo_buttons)

            buttons.append([InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await callback.message.edit_text(stats_text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка принудительной обработки чеков: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_mon_nalogo_pending')
@admin_required
async def nalogo_pending_callback(callback: CallbackQuery):
    """Просмотр чеков ожидающих ручной проверки."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from app.services.nalogo_service import NaloGoService

        nalogo_service = NaloGoService()
        receipts = await nalogo_service.get_pending_verification_receipts()

        if not receipts:
            await callback.answer(texts.t('ADMIN_MON_NALOGO_NO_PENDING'), show_alert=True)
            return

        text = texts.t('ADMIN_MON_NALOGO_PENDING_HEADER').format(count=len(receipts))
        text += texts.t('ADMIN_MON_NALOGO_PENDING_HINT')

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
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_NALOGO_MARK_CREATED_BUTTON').format(index=i),
                        callback_data=f'admin_nalogo_verified:{payment_id[:30]}',
                    ),
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_NALOGO_RETRY_BUTTON').format(index=i),
                        callback_data=f'admin_nalogo_retry:{payment_id[:30]}',
                    ),
                ]
            )

        if len(receipts) > 10:
            text += texts.t('ADMIN_MON_TRAFFIC_AND_MORE_LINE').format(count=len(receipts) - 10)

        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_MON_NALOGO_CLEAR_VERIFIED_BUTTON'),
                    callback_data='admin_nalogo_clear_pending',
                )
            ]
        )
        buttons.append([InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка просмотра очереди проверки: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data.startswith('admin_nalogo_verified:'))
@admin_required
async def nalogo_mark_verified_callback(callback: CallbackQuery):
    """Пометить чек как созданный в налоговой."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from app.services.nalogo_service import NaloGoService

        payment_id = callback.data.split(':', 1)[1]
        nalogo_service = NaloGoService()

        # Помечаем как проверенный (чек был создан)
        removed = await nalogo_service.mark_pending_as_verified(payment_id, receipt_uuid=None, was_created=True)

        if removed:
            await callback.answer(texts.t('ADMIN_MON_NALOGO_MARKED_CREATED'), show_alert=True)
            # Обновляем список
            await nalogo_pending_callback(callback)
        else:
            await callback.answer(texts.t('ADMIN_MON_NALOGO_RECEIPT_NOT_FOUND'), show_alert=True)

    except Exception as e:
        logger.error(f'Ошибка пометки чека: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data.startswith('admin_nalogo_retry:'))
@admin_required
async def nalogo_retry_callback(callback: CallbackQuery):
    """Повторно отправить чек в налоговую."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from app.services.nalogo_service import NaloGoService

        payment_id = callback.data.split(':', 1)[1]
        nalogo_service = NaloGoService()

        await callback.answer(texts.t('ADMIN_MON_NALOGO_SEND_RECEIPT_PROGRESS'), show_alert=False)

        receipt_uuid = await nalogo_service.retry_pending_receipt(payment_id)

        if receipt_uuid:
            await callback.answer(
                texts.t('ADMIN_MON_NALOGO_RECEIPT_CREATED').format(receipt_uuid=receipt_uuid), show_alert=True
            )
            # Обновляем список
            await nalogo_pending_callback(callback)
        else:
            await callback.answer(texts.t('ADMIN_MON_NALOGO_RECEIPT_CREATE_FAILED'), show_alert=True)

    except Exception as e:
        logger.error(f'Ошибка повторной отправки чека: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_nalogo_clear_pending')
@admin_required
async def nalogo_clear_pending_callback(callback: CallbackQuery):
    """Очистить всю очередь проверки."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from app.services.nalogo_service import NaloGoService

        nalogo_service = NaloGoService()
        count = await nalogo_service.clear_pending_verification()

        await callback.answer(texts.t('ADMIN_MON_NALOGO_CLEARED').format(count=count), show_alert=True)
        # Возвращаемся на статистику
        await callback.message.edit_text(
            texts.t('ADMIN_MON_NALOGO_QUEUE_CLEARED'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')]]
            ),
        )

    except Exception as e:
        logger.error(f'Ошибка очистки очереди: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_mon_receipts_missing')
@admin_required
async def receipts_missing_callback(callback: CallbackQuery):
    """Сверка чеков по логам."""
    # Напрямую вызываем сверку по логам
    await _do_reconcile_logs(callback)


@router.callback_query(F.data == 'admin_mon_receipts_link_old')
@admin_required
async def receipts_link_old_callback(callback: CallbackQuery):
    """Привязать старые чеки из NaloGO к транзакциям по сумме и дате."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from datetime import date, timedelta

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from sqlalchemy import and_, select

        from app.database.models import PaymentMethod, Transaction, TransactionType
        from app.services.nalogo_service import NaloGoService

        await callback.answer(texts.t('ADMIN_MON_RECEIPTS_LOADING_NALOGO'), show_alert=False)

        TRACKING_START_DATE = datetime(2024, 12, 29, 0, 0, 0)

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
                await callback.answer(texts.t('ADMIN_MON_RECEIPTS_NO_OLD_TRANSACTIONS'), show_alert=True)
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
                await callback.answer(texts.t('ADMIN_MON_RECEIPTS_FETCH_NALOGO_FAILED'), show_alert=True)
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

                                    t.receipt_created_at = isoparse(operation_time)
                                except Exception:
                                    t.receipt_created_at = datetime.utcnow()
                            linked += 1

            if linked > 0:
                await db.commit()

            text = texts.t('ADMIN_MON_RECEIPTS_LINK_RESULT').format(
                transactions_total=len(transactions),
                incomes_total=len(incomes),
                linked=linked,
                not_linked=len(transactions) - linked,
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')],
                ]
            )

            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка привязки старых чеков: {e}', exc_info=True)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_mon_receipts_reconcile')
@admin_required
async def receipts_reconcile_menu_callback(callback: CallbackQuery, state: FSMContext):
    """Меню выбора периода сверки."""

    # Очищаем состояние на случай если остался ввод даты
    await state.clear()

    # Сразу показываем сверку по логам
    await _do_reconcile_logs(callback)


async def _do_reconcile_logs(callback: CallbackQuery):
    """Внутренняя функция сверки по логам."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        import re
        from collections import defaultdict
        from pathlib import Path

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        await callback.answer(texts.t('ADMIN_MON_RECONCILE_ANALYZING_LOGS'), show_alert=False)

        # Путь к файлу логов платежей (logs/current/)
        log_file_path = Path(settings.LOG_FILE).resolve()
        log_dir = log_file_path.parent
        current_dir = log_dir / 'current'
        payments_log = current_dir / settings.LOG_PAYMENTS_FILE

        if not payments_log.exists():
            try:
                await callback.message.edit_text(
                    texts.t('ADMIN_MON_RECONCILE_LOG_FILE_MISSING').format(path=payments_log),
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=texts.t('ADMIN_MON_REFRESH_BUTTON'), callback_data='admin_mon_reconcile_logs'
                                )
                            ],
                            [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')],
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
            logger.error(f'Ошибка чтения логов: {e}')
            await callback.message.edit_text(
                texts.t('ADMIN_MON_RECONCILE_LOG_READ_ERROR').format(error=e),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')]]
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

        text = texts.t('ADMIN_MON_RECONCILE_SUMMARY_HEADER').format(
            total_payments=total_payments,
            total_receipts=total_receipts,
        )

        if missing_count == 0:
            text += texts.t('ADMIN_MON_RECONCILE_ALL_HAVE_RECEIPTS')
        else:
            text += texts.t('ADMIN_MON_RECONCILE_MISSING_TOTAL').format(
                missing_count=missing_count,
                missing_amount=missing_amount,
            )

            # Показываем по датам (последние)
            sorted_dates = sorted(by_date.keys(), reverse=True)
            for date_str in sorted_dates[:7]:
                date_payments = by_date[date_str]
                date_amount = sum(p['amount'] for p in date_payments)
                text += texts.t('ADMIN_MON_RECONCILE_MISSING_BY_DATE_LINE').format(
                    date=date_str,
                    count=len(date_payments),
                    amount=date_amount,
                )

            if len(sorted_dates) > 7:
                text += texts.t('ADMIN_MON_RECONCILE_MORE_DAYS').format(count=len(sorted_dates) - 7)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_REFRESH_BUTTON'), callback_data='admin_mon_reconcile_logs'
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_MON_RECONCILE_DETAILS_BUTTON'),
                        callback_data='admin_mon_reconcile_logs_details',
                    )
                ],
                [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_statistics')],
            ]
        )

        try:
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass  # Сообщение не изменилось

    except TelegramBadRequest:
        pass  # Игнорируем если сообщение не изменилось
    except Exception as e:
        logger.error(f'Ошибка сверки по логам: {e}', exc_info=True)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_mon_reconcile_logs')
@admin_required
async def receipts_reconcile_logs_refresh_callback(callback: CallbackQuery):
    """Обновить сверку по логам."""
    await _do_reconcile_logs(callback)


@router.callback_query(F.data == 'admin_mon_reconcile_logs_details')
@admin_required
async def receipts_reconcile_logs_details_callback(callback: CallbackQuery):
    """Детальный список платежей без чеков."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        import re
        from pathlib import Path

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        await callback.answer(texts.t('ADMIN_MON_RECONCILE_LOADING_DETAILS'), show_alert=False)

        # Путь к логам (logs/current/)
        log_file_path = Path(settings.LOG_FILE).resolve()
        log_dir = log_file_path.parent
        current_dir = log_dir / 'current'
        payments_log = current_dir / settings.LOG_PAYMENTS_FILE

        if not payments_log.exists():
            await callback.answer(texts.t('ADMIN_MON_RECONCILE_LOG_FILE_NOT_FOUND'), show_alert=True)
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
            text = texts.t('ADMIN_MON_RECONCILE_ALL_HAVE_RECEIPTS')
        else:
            text = texts.t('ADMIN_MON_RECONCILE_DETAILS_HEADER').format(count=len(missing))

            for p in missing[:20]:
                text += (
                    f'• <b>{p["date"]} {p["time"]}</b>\n'
                    f'  User: {p["user_id"]} | {p["amount"]:.0f}₽\n'
                    f'  <code>{p["payment_id"][:18]}...</code>\n\n'
                )

            if len(missing) > 20:
                text += texts.t('ADMIN_MON_RECONCILE_MORE_PAYMENTS').format(count=len(missing) - 20)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_mon_reconcile_logs')],
            ]
        )

        try:
            await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass

    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.error(f'Ошибка детализации: {e}', exc_info=True)
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


def get_monitoring_logs_keyboard(current_page: int, total_pages: int, language: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    texts = get_texts(language)

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
                InlineKeyboardButton(text=texts.t('ADMIN_MON_REFRESH_BUTTON'), callback_data='admin_mon_logs'),
                InlineKeyboardButton(text=texts.t('ADMIN_MON_CLEAR_BUTTON'), callback_data='admin_mon_clear_logs'),
            ],
            [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_monitoring_logs_back_keyboard(language: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.t('ADMIN_MON_REFRESH_BUTTON'), callback_data='admin_mon_logs'),
                InlineKeyboardButton(text=texts.t('ADMIN_MON_FILTERS_BUTTON'), callback_data='admin_mon_logs_filters'),
            ],
            [InlineKeyboardButton(text=texts.t('ADMIN_MON_CLEAR_LOGS_BUTTON'), callback_data='admin_mon_clear_logs')],
            [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')],
        ]
    )


@router.message(Command('monitoring'))
@admin_required
async def monitoring_command(message: Message):
    try:
        async with AsyncSessionLocal() as db:
            status = await monitoring_service.get_monitoring_status(db)
            texts = get_texts(message.from_user.language_code or settings.DEFAULT_LANGUAGE)

            running_status = (
                texts.t('ADMIN_MON_STATUS_RUNNING') if status['is_running'] else texts.t('ADMIN_MON_STATUS_STOPPED')
            )

            text = texts.t('ADMIN_MON_QUICK_STATUS_TEXT').format(
                running_status=running_status,
                total_events=status['stats_24h']['total_events'],
                success_rate=status['stats_24h']['success_rate'],
            )

            await message.answer(text, parse_mode='HTML')

    except Exception as e:
        logger.error(f'Ошибка команды /monitoring: {e}')
        texts = get_texts(message.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await message.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e))


@router.message(AdminStates.editing_notification_value)
async def process_notification_value_input(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data:
        await state.clear()
        texts = get_texts(message.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await message.answer(texts.t('ADMIN_MON_CONTEXT_LOST'))
        return

    raw_value = (message.text or '').strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        language = data.get('settings_language') or message.from_user.language_code or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        await message.answer(texts.t('NOTIFICATION_VALUE_INVALID'))
        return

    key = data.get('notification_setting_key')
    field = data.get('notification_setting_field')
    language = data.get('settings_language') or message.from_user.language_code or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)

    # Добавляем дополнительные проверки диапазона значений
    if (key == 'expired_second_wave' and field == 'percent') or (key == 'expired_third_wave' and field == 'percent'):
        if value < 0 or value > 100:
            await message.answer(texts.t('ADMIN_MON_PERCENT_RANGE_ERROR'))
            return
    elif (key == 'expired_second_wave' and field == 'hours') or (key == 'expired_third_wave' and field == 'hours'):
        if value < 1 or value > 168:  # Максимум 168 часов (7 дней)
            await message.answer(texts.t('ADMIN_MON_HOURS_RANGE_ERROR'))
            return
    elif key == 'expired_third_wave' and field == 'trigger':
        if value < 2:  # Минимум 2 дня
            await message.answer(texts.t('ADMIN_MON_DAYS_MIN_ERROR'))
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


# ============== Настройки мониторинга трафика ==============


def _format_traffic_toggle(enabled: bool) -> str:
    texts = get_texts(settings.DEFAULT_LANGUAGE)
    return texts.t('ADMIN_MON_TOGGLE_ON') if enabled else texts.t('ADMIN_MON_TOGGLE_OFF')


def _build_traffic_settings_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру настроек мониторинга трафика."""
    texts = get_texts(settings.DEFAULT_LANGUAGE)
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
                    text=texts.t('ADMIN_TRAFFIC_FAST_TOGGLE_BUTTON').format(
                        status=_format_traffic_toggle(fast_enabled)
                    ),
                    callback_data='admin_traffic_toggle_fast',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TRAFFIC_FAST_INTERVAL_BUTTON').format(minutes=fast_interval),
                    callback_data='admin_traffic_edit_fast_interval',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TRAFFIC_FAST_THRESHOLD_BUTTON').format(gb=fast_threshold),
                    callback_data='admin_traffic_edit_fast_threshold',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TRAFFIC_DAILY_TOGGLE_BUTTON').format(
                        status=_format_traffic_toggle(daily_enabled)
                    ),
                    callback_data='admin_traffic_toggle_daily',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TRAFFIC_DAILY_TIME_BUTTON').format(time=daily_time),
                    callback_data='admin_traffic_edit_daily_time',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TRAFFIC_DAILY_THRESHOLD_BUTTON').format(gb=daily_threshold),
                    callback_data='admin_traffic_edit_daily_threshold',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_TRAFFIC_COOLDOWN_BUTTON').format(minutes=cooldown),
                    callback_data='admin_traffic_edit_cooldown',
                )
            ],
            [InlineKeyboardButton(text=texts.t('BACK'), callback_data='admin_monitoring')],
        ]
    )


def _build_traffic_settings_text() -> str:
    """Строит текст настроек мониторинга трафика."""
    texts = get_texts(settings.DEFAULT_LANGUAGE)
    fast_enabled = settings.TRAFFIC_FAST_CHECK_ENABLED
    daily_enabled = settings.TRAFFIC_DAILY_CHECK_ENABLED

    fast_status = _format_traffic_toggle(fast_enabled)
    daily_status = _format_traffic_toggle(daily_enabled)

    text = texts.t('ADMIN_TRAFFIC_SETTINGS_TEXT').format(
        fast_status=fast_status,
        fast_interval=settings.TRAFFIC_FAST_CHECK_INTERVAL_MINUTES,
        fast_threshold=settings.TRAFFIC_FAST_CHECK_THRESHOLD_GB,
        daily_status=daily_status,
        daily_time=settings.TRAFFIC_DAILY_CHECK_TIME,
        daily_threshold=settings.TRAFFIC_DAILY_THRESHOLD_GB,
        cooldown=settings.TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES,
    )

    # Информация о фильтрах
    monitored_nodes = settings.get_traffic_monitored_nodes()
    ignored_nodes = settings.get_traffic_ignored_nodes()
    excluded_uuids = settings.get_traffic_excluded_user_uuids()

    if monitored_nodes:
        text += texts.t('ADMIN_TRAFFIC_MONITORED_ONLY_LINE').format(count=len(monitored_nodes))
    if ignored_nodes:
        text += texts.t('ADMIN_TRAFFIC_IGNORED_NODES_LINE').format(count=len(ignored_nodes))
    if excluded_uuids:
        text += texts.t('ADMIN_TRAFFIC_EXCLUDED_USERS_LINE').format(count=len(excluded_uuids))

    return text


@router.callback_query(F.data == 'admin_mon_traffic_settings')
@admin_required
async def admin_traffic_settings(callback: CallbackQuery):
    """Показывает настройки мониторинга трафика."""
    try:
        text = _build_traffic_settings_text()
        keyboard = _build_traffic_settings_keyboard()
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except Exception as e:
        logger.error(f'Ошибка отображения настроек трафика: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_TRAFFIC_SETTINGS_LOAD_ERROR'), show_alert=True)


@router.callback_query(F.data == 'admin_traffic_toggle_fast')
@admin_required
async def toggle_fast_check(callback: CallbackQuery):
    """Переключает быструю проверку трафика."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from app.services.system_settings_service import BotConfigurationService

        current = settings.TRAFFIC_FAST_CHECK_ENABLED
        new_value = not current

        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, 'TRAFFIC_FAST_CHECK_ENABLED', new_value)
            await db.commit()

        await callback.answer(
            texts.t('ADMIN_MON_TOGGLE_ENABLED') if new_value else texts.t('ADMIN_MON_TOGGLE_DISABLED')
        )

        # Обновляем отображение
        text = _build_traffic_settings_text()
        keyboard = _build_traffic_settings_keyboard()
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка переключения быстрой проверки: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_traffic_toggle_daily')
@admin_required
async def toggle_daily_check(callback: CallbackQuery):
    """Переключает суточную проверку трафика."""
    try:
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        from app.services.system_settings_service import BotConfigurationService

        current = settings.TRAFFIC_DAILY_CHECK_ENABLED
        new_value = not current

        async with AsyncSessionLocal() as db:
            await BotConfigurationService.set_value(db, 'TRAFFIC_DAILY_CHECK_ENABLED', new_value)
            await db.commit()

        await callback.answer(
            texts.t('ADMIN_MON_TOGGLE_ENABLED') if new_value else texts.t('ADMIN_MON_TOGGLE_DISABLED')
        )

        text = _build_traffic_settings_text()
        keyboard = _build_traffic_settings_keyboard()
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка переключения суточной проверки: {e}')
        texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await callback.answer(texts.t('ADMIN_MON_GENERIC_ERROR').format(error=e), show_alert=True)


@router.callback_query(F.data == 'admin_traffic_edit_fast_interval')
@admin_required
async def edit_fast_interval(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование интервала быстрой проверки."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_FAST_CHECK_INTERVAL_MINUTES',
        traffic_setting_type='int',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.message.answer(texts.t('ADMIN_TRAFFIC_PROMPT_FAST_INTERVAL'))


@router.callback_query(F.data == 'admin_traffic_edit_fast_threshold')
@admin_required
async def edit_fast_threshold(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование порога быстрой проверки."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_FAST_CHECK_THRESHOLD_GB',
        traffic_setting_type='float',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.message.answer(texts.t('ADMIN_TRAFFIC_PROMPT_FAST_THRESHOLD'))


@router.callback_query(F.data == 'admin_traffic_edit_daily_time')
@admin_required
async def edit_daily_time(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование времени суточной проверки."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_DAILY_CHECK_TIME',
        traffic_setting_type='time',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.message.answer(texts.t('ADMIN_TRAFFIC_PROMPT_DAILY_TIME'))


@router.callback_query(F.data == 'admin_traffic_edit_daily_threshold')
@admin_required
async def edit_daily_threshold(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование суточного порога."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_DAILY_THRESHOLD_GB',
        traffic_setting_type='float',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.message.answer(texts.t('ADMIN_TRAFFIC_PROMPT_DAILY_THRESHOLD'))


@router.callback_query(F.data == 'admin_traffic_edit_cooldown')
@admin_required
async def edit_cooldown(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование кулдауна уведомлений."""
    await state.set_state(AdminStates.editing_traffic_setting)
    await state.update_data(
        traffic_setting_key='TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES',
        traffic_setting_type='int',
        settings_message_chat=callback.message.chat.id,
        settings_message_id=callback.message.message_id,
    )
    await callback.answer()
    texts = get_texts(callback.from_user.language_code or settings.DEFAULT_LANGUAGE)
    await callback.message.answer(texts.t('ADMIN_TRAFFIC_PROMPT_COOLDOWN'))


@router.message(AdminStates.editing_traffic_setting)
async def process_traffic_setting_input(message: Message, state: FSMContext):
    """Обрабатывает ввод настройки мониторинга трафика."""
    from app.services.system_settings_service import BotConfigurationService

    data = await state.get_data()
    if not data:
        await state.clear()
        texts = get_texts(message.from_user.language_code or settings.DEFAULT_LANGUAGE)
        await message.answer(texts.t('ADMIN_MON_CONTEXT_LOST'))
        return

    texts = get_texts(message.from_user.language_code or settings.DEFAULT_LANGUAGE)
    raw_value = (message.text or '').strip()
    setting_key = data.get('traffic_setting_key')
    setting_type = data.get('traffic_setting_type')

    # Валидация и парсинг значения
    try:
        if setting_type == 'int':
            value = int(raw_value)
            if value < 1:
                raise ValueError(texts.t('ADMIN_TRAFFIC_VALUE_MIN_1_ERROR'))
        elif setting_type == 'float':
            value = float(raw_value.replace(',', '.'))
            if value <= 0:
                raise ValueError(texts.t('ADMIN_TRAFFIC_VALUE_POSITIVE_ERROR'))
        elif setting_type == 'time':
            # Валидация формата HH:MM
            import re

            if not re.match(r'^\d{1,2}:\d{2}$', raw_value):
                raise ValueError(texts.t('ADMIN_TRAFFIC_TIME_FORMAT_ERROR'))
            parts = raw_value.split(':')
            hours, minutes = int(parts[0]), int(parts[1])
            if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
                raise ValueError(texts.t('ADMIN_TRAFFIC_TIME_VALUE_ERROR'))
            value = f'{hours:02d}:{minutes:02d}'
        else:
            value = raw_value
    except ValueError as e:
        await message.answer(texts.t('ADMIN_TRAFFIC_INPUT_ERROR').format(error=e))
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
                        text=texts.t('ADMIN_TRAFFIC_BACK_TO_SETTINGS_BUTTON'),
                        callback_data='admin_mon_traffic_settings',
                    )
                ]
            ]
        )
        await message.answer(texts.t('ADMIN_TRAFFIC_SETTING_SAVED'), reply_markup=back_keyboard)

        # Обновляем исходное сообщение с настройками
        chat_id = data.get('settings_message_chat')
        message_id = data.get('settings_message_id')
        if chat_id and message_id:
            try:
                text = _build_traffic_settings_text()
                keyboard = _build_traffic_settings_keyboard()
                await message.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text, parse_mode='HTML', reply_markup=keyboard
                )
            except Exception:
                pass  # Игнорируем если сообщение уже удалено

    except Exception as e:
        logger.error(f'Ошибка сохранения настройки трафика: {e}')
        await message.answer(texts.t('ADMIN_TRAFFIC_SAVE_ERROR').format(error=e))

    await state.clear()


def register_handlers(dp):
    dp.include_router(router)
