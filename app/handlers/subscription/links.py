from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import (
    get_device_selection_keyboard,
    get_happ_cryptolink_keyboard,
    get_happ_download_button_row,
)
from app.localization.texts import get_texts
from app.utils.subscription_utils import (
    convert_subscription_link_to_happ_scheme,
    get_display_subscription_link,
    get_happ_cryptolink_redirect_link,
)

from .common import get_platforms_list, load_app_config_async, logger


async def _resolve_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state=None):
    """Resolve subscription — delegates to shared resolve_subscription_from_context."""
    from .common import resolve_subscription_from_context

    return await resolve_subscription_from_context(callback, db_user, db, state)


async def handle_connect_subscription(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    # Проверяем, доступно ли сообщение для редактирования
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)

    # В режиме мульти-тарифов без явного sub_id в callback — показываем выбор подписки.
    if settings.is_multi_tariff_enabled() and callback.data == 'subscription_connect':
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
        if len(active_subs) > 1:
            from datetime import UTC, datetime

            from app.database.crud.tariff import get_tariff_by_id as _get_tariff

            keyboard = []
            for sub in sorted(active_subs, key=lambda s: s.id):
                tariff_name = ''
                if sub.tariff_id:
                    _t = await _get_tariff(db, sub.tariff_id)
                    tariff_name = _t.name if _t else f'#{sub.id}'
                else:
                    tariff_name = f'Подписка #{sub.id}'
                days_left = max(0, (sub.end_date - datetime.now(UTC)).days) if sub.end_date else 0
                keyboard.append(
                    [
                        types.InlineKeyboardButton(
                            text=f'🔗 {tariff_name} ({days_left}д.)',
                            callback_data=f'sl:{sub.id}',
                        )
                    ]
                )
            keyboard.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data='back_to_menu')])
            await callback.message.edit_text(
                '🔗 <b>Подключиться</b>\n\nВыберите подписку:',
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            )
            await callback.answer()
            return

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return
    subscription_link = get_display_subscription_link(subscription)
    hide_subscription_link = settings.should_hide_subscription_link()
    back_cb = f'sm:{sub_id}' if settings.is_multi_tariff_enabled() else 'menu_subscription'

    if not subscription_link:
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_NO_ACTIVE_LINK',
                '⚠ У вас нет активной подписки или ссылка еще генерируется',
            ),
            show_alert=True,
        )
        return

    connect_mode = settings.CONNECT_BUTTON_MODE

    if connect_mode == 'miniapp_subscription':
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                        web_app=types.WebAppInfo(url=subscription_link),
                    )
                ],
                [InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)],
            ]
        )

        await callback.message.edit_text(
            texts.t(
                'SUBSCRIPTION_CONNECT_MINIAPP_MESSAGE',
                """📱 <b>Подключить подписку</b>

🚀 Нажмите кнопку ниже, чтобы открыть подписку в мини-приложении Telegram:""",
            ),
            reply_markup=keyboard,
            parse_mode='HTML',
        )

    elif connect_mode == 'miniapp_custom':
        if not settings.MINIAPP_CUSTOM_URL:
            await callback.answer(
                texts.t(
                    'CUSTOM_MINIAPP_URL_NOT_SET',
                    '⚠ Кастомная ссылка для мини-приложения не настроена',
                ),
                show_alert=True,
            )
            return

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                        web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL),
                    )
                ],
                [InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)],
            ]
        )

        await callback.message.edit_text(
            texts.t(
                'SUBSCRIPTION_CONNECT_CUSTOM_MESSAGE',
                """🚀 <b>Подключить подписку</b>

📱 Нажмите кнопку ниже, чтобы открыть приложение:""",
            ),
            reply_markup=keyboard,
            parse_mode='HTML',
        )

    elif connect_mode == 'link':
        rows = [[InlineKeyboardButton(text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'), url=subscription_link)]]
        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            rows.append(happ_row)
        rows.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)])

        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

        await callback.message.edit_text(
            texts.t(
                'SUBSCRIPTION_CONNECT_LINK_MESSAGE',
                """🚀 <b>Подключить подписку</b>",

🔗 Нажмите кнопку ниже, чтобы открыть ссылку подписки:""",
            ),
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    elif connect_mode == 'happ_cryptolink':
        rows = [
            [
                InlineKeyboardButton(
                    text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                    callback_data=f'open_subscription_link:{sub_id}'
                    if settings.is_multi_tariff_enabled()
                    else 'open_subscription_link',
                )
            ]
        ]
        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            rows.append(happ_row)
        rows.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)])

        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

        await callback.message.edit_text(
            texts.t(
                'SUBSCRIPTION_CONNECT_LINK_MESSAGE',
                """🚀 <b>Подключить подписку</b>",

🔗 Нажмите кнопку ниже, чтобы открыть ссылку подписки:""",
            ),
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    else:
        # Guide mode: load config and build dynamic platform keyboard
        platforms = None
        try:
            config = await load_app_config_async()
            if config:
                platforms = get_platforms_list(config) or None
        except Exception as e:
            logger.warning('Failed to load platforms for guide mode', error=e)

        if not platforms:
            await callback.message.edit_text(
                texts.t(
                    'GUIDE_CONFIG_NOT_SET',
                    '⚠️ <b>Конфигурация не настроена</b>\n\n'
                    'Администратор ещё не настроил конфигурацию приложений.\n'
                    'Обратитесь к администратору.',
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)],
                    ]
                ),
                parse_mode='HTML',
            )
            await callback.answer()
            return

        if hide_subscription_link:
            device_text = texts.t(
                'SUBSCRIPTION_CONNECT_DEVICE_MESSAGE_HIDDEN',
                """📱 <b>Подключить подписку</b>

ℹ️ Ссылка подписки доступна по кнопкам ниже или в разделе "Моя подписка".

💡 <b>Выберите ваше устройство</b> для получения подробной инструкции по настройке:""",
            )
        else:
            device_text = texts.t(
                'SUBSCRIPTION_CONNECT_DEVICE_MESSAGE',
                """📱 <b>Подключить подписку</b>

🔗 <b>Ссылка подписки:</b>
<code>{subscription_url}</code>

💡 <b>Выберите ваше устройство</b> для получения подробной инструкции по настройке:""",
            ).format(subscription_url=subscription_link)

        await callback.message.edit_text(
            device_text,
            reply_markup=get_device_selection_keyboard(db_user.language, platforms=platforms, sub_id=sub_id),
            parse_mode='HTML',
        )

    await callback.answer()


async def handle_open_subscription_link(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    texts = get_texts(db_user.language)
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return
    subscription_link = get_display_subscription_link(subscription)
    back_cb = f'sm:{sub_id}' if settings.is_multi_tariff_enabled() else 'menu_subscription'

    if not subscription_link:
        await callback.answer(
            texts.t('SUBSCRIPTION_LINK_UNAVAILABLE', '❌ Ссылка подписки недоступна'),
            show_alert=True,
        )
        return

    if settings.is_happ_cryptolink_mode():
        redirect_link = get_happ_cryptolink_redirect_link(subscription_link)
        happ_scheme_link = convert_subscription_link_to_happ_scheme(subscription_link)
        happ_message = (
            texts.t(
                'SUBSCRIPTION_HAPP_OPEN_TITLE',
                '🔗 <b>Подключение через Happ</b>',
            )
            + '\n\n'
            + texts.t(
                'SUBSCRIPTION_HAPP_OPEN_LINK',
                '<a href="{subscription_link}">🔓 Открыть ссылку в Happ</a>',
            ).format(subscription_link=happ_scheme_link)
            + '\n\n'
            + texts.t(
                'SUBSCRIPTION_HAPP_OPEN_HINT',
                '💡 Если ссылка не открывается автоматически, скопируйте её вручную:',
            )
        )

        if redirect_link:
            happ_message += '\n\n' + texts.t(
                'SUBSCRIPTION_HAPP_OPEN_BUTTON_HINT',
                '▶️ Нажмите кнопку "Подключиться" ниже, чтобы открыть Happ и добавить подписку автоматически.',
            )

        happ_message += '\n\n' + texts.t(
            'SUBSCRIPTION_HAPP_CRYPTOLINK_BLOCK',
            '<blockquote expandable><code>{crypto_link}</code></blockquote>',
        ).format(crypto_link=subscription_link)

        keyboard = get_happ_cryptolink_keyboard(
            subscription_link,
            db_user.language,
            redirect_link=redirect_link,
        )

        await callback.message.answer(
            happ_message,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        await callback.answer()
        return

    link_text = (
        texts.t('SUBSCRIPTION_DEVICE_LINK_TITLE', '🔗 <b>Ссылка подписки:</b>')
        + '\n\n'
        + f'<code>{subscription_link}</code>\n\n'
        + texts.t('SUBSCRIPTION_LINK_USAGE_TITLE', '📱 <b>Как использовать:</b>')
        + '\n'
        + '\n'.join(
            [
                texts.t(
                    'SUBSCRIPTION_LINK_STEP1',
                    '1. Нажмите на ссылку выше чтобы её скопировать',
                ),
                texts.t(
                    'SUBSCRIPTION_LINK_STEP2',
                    '2. Откройте ваше VPN приложение',
                ),
                texts.t(
                    'SUBSCRIPTION_LINK_STEP3',
                    '3. Найдите функцию "Добавить подписку" или "Import"',
                ),
                texts.t(
                    'SUBSCRIPTION_LINK_STEP4',
                    '4. Вставьте скопированную ссылку',
                ),
            ]
        )
        + '\n\n'
        + texts.t(
            'SUBSCRIPTION_LINK_HINT',
            '💡 Если ссылка не скопировалась, выделите её вручную и скопируйте.',
        )
    )

    await callback.message.edit_text(
        link_text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                        callback_data=f'subscription_connect:{sub_id}'
                        if settings.is_multi_tariff_enabled()
                        else 'subscription_connect',
                    )
                ],
                [InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)],
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()
