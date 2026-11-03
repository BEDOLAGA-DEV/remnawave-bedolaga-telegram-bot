"""
Multi-tariff subscription list handler.

Shows all user subscriptions with per-subscription management.
Only active when MULTI_TARIFF_ENABLED=True.
"""

from __future__ import annotations

import html

import structlog
from aiogram import Router, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    get_all_subscriptions_by_user_id,
    get_subscription_by_id_for_user,
)
from app.database.models import Subscription, SubscriptionStatus, User
from app.localization.texts import Texts, get_texts
from app.utils.legacy_subscription import is_legacy_subscription
from app.utils.subscription_utils import get_display_subscription_link
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

router = Router()


def _status_emoji(sub) -> str:
    """Return status emoji based on subscription's actual status."""
    actual = sub.actual_status
    if actual in ('active', 'trial'):
        return '🟢'
    if actual == 'limited':
        return '🟡'
    return '🔴'


def _status_label(sub, language: str | None = None) -> str:
    """Return a short human-readable status label for non-active subscriptions."""
    texts = get_texts(language)
    actual = sub.actual_status
    if actual == 'expired':
        return texts.t('SUBSCRIPTION_UI_SUFFIX_EXPIRED', ' (Истекла)')
    if actual == 'disabled':
        return texts.t('SUBSCRIPTION_UI_SUFFIX_DISABLED', ' (Отключена)')
    if actual == 'limited':
        return texts.t('SUBSCRIPTION_UI_SUFFIX_LIMITED', ' (Лимит)')
    return ''


def _format_subscription_line(sub, idx: int, language: str | None = None) -> str:
    """Format a single subscription for the list view."""
    texts = get_texts(language)
    tariff_name = sub.tariff.name if sub.tariff else texts.t('SUBSCRIPTION_UI_NAME', 'Подписка')
    emoji = _status_emoji(sub)
    label = _status_label(sub, language)

    # Traffic info
    if sub.traffic_limit_gb == 0:
        traffic = '∞'
    else:
        used = f'{sub.traffic_used_gb:.1f}' if sub.traffic_used_gb else '0'
        traffic = texts.t('SUBSCRIPTION_UI_USED_LIMIT', '{used}/{limit} ГБ').format(
            used=used, limit=sub.traffic_limit_gb
        )

    # Devices
    devices = (
        texts.t('SUBSCRIPTION_UI_DEVICES_SHORT', '{count} устр.').format(
            count=Texts.format_device_limit(sub.device_limit)
        )
        if sub.device_limit is not None
        else ''
    )

    # End date
    end_date = format_local_datetime(sub.end_date, '%d.%m.%Y') if sub.end_date else '—'

    parts = [f'{emoji} <b>{idx}. {tariff_name}</b>{label}']
    parts.append(texts.t('SUBSCRIPTION_UI_LIST_TRAFFIC', '   📊 Трафик: {traffic}').format(traffic=traffic))
    if devices:
        parts.append(texts.t('SUBSCRIPTION_UI_LIST_DEVICES', '   📱 Устройства: {devices}').format(devices=devices))
    parts.append(texts.t('SUBSCRIPTION_UI_LIST_UNTIL', '   📅 До: {date}').format(date=end_date))

    return '\n'.join(parts)


def _build_subscriptions_keyboard(
    subscriptions: list, language: str, gift_enabled: bool = False
) -> types.InlineKeyboardMarkup:
    """Build inline keyboard with per-subscription management buttons."""
    texts = get_texts(language)
    buttons = []
    for idx, sub in enumerate(subscriptions, 1):
        tariff_name = (
            sub.tariff.name if sub.tariff else texts.t('SUBSCRIPTION_UI_NAME_ID', 'Подписка #{id}').format(id=sub.id)
        )
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=f'⚙️ {tariff_name}',
                    callback_data=f'sm:{sub.id}',
                )
            ]
        )

    # "Buy another tariff" button. Пока у человека есть старая подписка (без
    # тарифа при включённых тарифах), не предлагаем: сперва переход на тариф —
    # иначе покупка заведёт вторую подписку рядом с непродлеваемой старой.
    texts = get_texts(language)
    if not any(is_legacy_subscription(sub) for sub in subscriptions):
        buy_text = getattr(texts, 'MENU_BUY_SUBSCRIPTION', 'Купить ещё тариф')
        buttons.append(
            [
                types.InlineKeyboardButton(text=f'➕ {buy_text}', callback_data='menu_buy'),
            ]
        )
    if gift_enabled:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('GIFT_SUBSCRIPTION_BUTTON', '🎁 Подарить подписку'),
                    callback_data='subscription_gift',
                )
            ]
        )
    # Back button
    buttons.append(
        [
            types.InlineKeyboardButton(text=texts.t('SUBSCRIPTION_UI_BACK', '◀️ Назад'), callback_data='back_to_menu'),
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_subscription_detail_keyboard(
    sub_id: int, sub=None, language: str | None = None
) -> types.InlineKeyboardMarkup:
    """Build keyboard for single subscription management.

    For expired/disabled subscriptions, only 'Renew' and 'Back' are shown —
    connection link and traffic/device management are irrelevant.
    """
    texts = get_texts(language)
    is_inactive = sub is not None and sub.actual_status in ('expired', 'disabled')

    buttons = []

    if not is_inactive:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_CONNECT_LINK', '🔗 Ссылка подключения'), callback_data=f'sl:{sub_id}'
                )
            ]
        )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('SUBSCRIPTION_UI_RENEW_BUTTON', '🔄 Продлить'), callback_data=f'se:{sub_id}'
            )
        ]
    )

    if not is_inactive:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_AUTOPAY_BUTTON', '💳 Автоплатеж'),
                    callback_data='subscription_autopay',
                )
            ]
        )
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_TRAFFIC_BUTTON', '📊 Трафик'), callback_data=f'st:{sub_id}'
                )
            ]
        )
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_DEVICES_BUTTON', '📱 Устройства'), callback_data=f'sd:{sub_id}'
                )
            ]
        )

    if is_inactive:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_DELETE_BUTTON', '🗑 Удалить подписку'),
                    callback_data=f'sub_del:{sub_id}',
                )
            ]
        )

    if not is_inactive and settings.is_subscription_revoke_enabled():
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_REISSUE_BUTTON', '🔄 Перевыпустить'),
                    callback_data=f'sr:{sub_id}',
                )
            ]
        )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('SUBSCRIPTION_UI_BACK_TO_LIST', '◀️ К списку подписок'), callback_data='my_subscriptions'
            )
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_my_subscriptions(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext | None = None,
) -> None:
    """Show list of all user subscriptions."""
    if not settings.is_multi_tariff_enabled():
        # Fallback to legacy single subscription view
        return

    texts = get_texts(db_user.language)
    gift_enabled = True
    subscriptions = await get_all_subscriptions_by_user_id(db, db_user.id)
    subscriptions = [sub for sub in subscriptions if not sub.is_pending_trial]

    if not subscriptions:
        text = texts.t('SUBSCRIPTION_UI_LIST_EMPTY', '📋 <b>Мои подписки</b>\n\nУ вас нет подписок.')
        buttons = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_BUY', '🛒 Купить подписку'),
                    callback_data='menu_buy',
                )
            ],
        ]
        if gift_enabled:
            buttons.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('GIFT_SUBSCRIPTION_BUTTON', '🎁 Подарить подписку'),
                        callback_data='subscription_gift',
                    )
                ]
            )
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_BACK', '◀️ Назад'),
                    callback_data='back_to_menu',
                )
            ]
        )
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=buttons)
    else:
        lines = [texts.t('SUBSCRIPTION_UI_LIST_TITLE', '📋 <b>Мои подписки</b>\n')]
        for idx, sub in enumerate(subscriptions, 1):
            lines.append(_format_subscription_line(sub, idx, db_user.language))
            lines.append('')  # empty line between subscriptions
        text = '\n'.join(lines)
        keyboard = _build_subscriptions_keyboard(subscriptions, db_user.language, gift_enabled=gift_enabled)

    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def show_subscription_detail(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Show detail view for a single subscription (IDOR protected)."""
    parts = callback.data.split(':')
    if len(parts) < 2:
        await callback.answer(
            get_texts(getattr(db_user, 'language', None)).t('SUBSCRIPTION_UI_INVALID_FORMAT', 'Неверный формат'),
            show_alert=True,
        )
        return

    sub_id = int(parts[1])
    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)

    if not subscription or subscription.is_pending_trial:
        await callback.answer(
            get_texts(getattr(db_user, 'language', None)).t('SUBSCRIPTION_UI_NOT_FOUND', 'Подписка не найдена'),
            show_alert=True,
        )
        return

    # Persist active sub_id so downstream handlers without sub_id in callback_data
    # (e.g. 'subscription_autopay') can resolve the right subscription via FSM.
    await state.update_data(active_subscription_id=sub_id)

    texts = get_texts(db_user.language)
    tariff_name = subscription.tariff.name if subscription.tariff else texts.t('SUBSCRIPTION_UI_NAME', 'Подписка')

    # Traffic
    if subscription.traffic_limit_gb == 0:
        traffic = texts.t('SUBSCRIPTION_UI_GB_VALUE', '{amount} ГБ').format(amount='∞')
    else:
        used = f'{subscription.traffic_used_gb:.1f}' if subscription.traffic_used_gb else '0'
        traffic = texts.t('SUBSCRIPTION_UI_USED_LIMIT_SPACED', '{used} / {limit} ГБ').format(
            used=used, limit=subscription.traffic_limit_gb
        )

    end_date = format_local_datetime(subscription.end_date, '%d.%m.%Y %H:%M') if subscription.end_date else '—'
    status_keys = {
        '🔴 Истекла': ('SUBSCRIPTION_STATUS_EXPIRED', '🔴'),
        '🎯 Тестовая': ('SUBSCRIPTION_STATUS_TRIAL', '🎯'),
        '🟢 Активна': ('SUBSCRIPTION_STATUS_ACTIVE', '🟢'),
        '⚫ Отключена': ('SUBSCRIPTION_STATUS_DISABLED', '⚫'),
        '⚠️ Трафик исчерпан': ('SUBSCRIPTION_STATUS_LIMITED', '⚠️'),
        '❓ Неизвестно': ('SUBSCRIPTION_STATUS_UNKNOWN', '❓'),
    }
    status = subscription.status_display
    if status in status_keys:
        status_key, emoji = status_keys[status]
        status = f'{emoji} {texts.t(status_key)}'

    text = texts.t(
        'SUBSCRIPTION_UI_DETAIL',
        '📋 <b>{name}</b>\n\nСтатус: {status}\n📊 Трафик: {traffic}\n📱 Устройства: {devices}\n📅 До: {date}\n',
    ).format(
        name=tariff_name,
        status=status,
        traffic=traffic,
        devices=Texts.format_device_limit(subscription.device_limit),
        date=end_date,
    )

    subscription_link = get_display_subscription_link(subscription)
    if subscription_link and not settings.should_hide_subscription_link():
        text += f'\n🔗 <code>{html.escape(subscription_link)}</code>'

    keyboard = _build_subscription_detail_keyboard(sub_id, sub=subscription, language=db_user.language)

    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def _resolve_and_store_sub(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> Subscription | None:
    """Extract sub_id from callback, validate ownership, store in FSM state."""
    texts = get_texts(getattr(db_user, 'language', None))
    sub_id = _extract_sub_id(callback)
    if sub_id is None:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_INVALID_FORMAT', 'Неверный формат'),
            show_alert=True,
        )
        return None

    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_NOT_FOUND', 'Подписка не найдена'),
            show_alert=True,
        )
        return None

    # Store in FSM state so downstream handlers can use it
    await state.update_data(active_subscription_id=sub_id)
    return subscription


async def handle_subscription_link(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: sl:{sub_id} → connect subscription link handler."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    from .links import handle_connect_subscription

    await handle_connect_subscription(callback, db_user, db, state)


async def handle_subscription_extend(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: se:{sub_id} → extend/renew subscription handler."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    from .purchase import handle_extend_subscription

    await handle_extend_subscription(callback, db_user, db, state)


async def handle_subscription_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: st:{sub_id} → traffic management handler."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    from .traffic import handle_add_traffic

    await handle_add_traffic(callback, db_user, db, state)


async def handle_subscription_devices(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: sd:{sub_id} → devices menu with buy + manage options."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    sub_id = subscription.id

    # Проверяем доступность докупки устройств
    can_buy_devices = False
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        tariff_device_price = getattr(tariff, 'device_price_kopeks', None) if tariff else None
        can_buy_devices = bool(tariff_device_price and tariff_device_price > 0)
    else:
        can_buy_devices = settings.is_devices_selection_enabled()

    current_devices = Texts.format_device_limit(subscription.device_limit)
    text = (
        get_texts(db_user.language)
        .t(
            'SUBSCRIPTION_UI_DEVICES_MENU',
            '📱 <b>Устройства</b>\n\nТекущий лимит: {devices} устройств\n\nВыберите действие:',
        )
        .format(devices=current_devices)
    )

    keyboard = []
    if can_buy_devices:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=get_texts(getattr(db_user, 'language', None)).t(
                        'SUBSCRIPTION_UI_BUY_DEVICES', '➕ Докупить устройства'
                    ),
                    callback_data=f'change_devices_menu:{sub_id}',
                )
            ]
        )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=get_texts(getattr(db_user, 'language', None)).t(
                    'SUBSCRIPTION_UI_MANAGE_DEVICES', '📱 Управление устройствами'
                ),
                callback_data=f'device_management:{sub_id}',
            )
        ]
    )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=get_texts(getattr(db_user, 'language', None)).t('SUBSCRIPTION_UI_BACK', '◀️ Назад'),
                callback_data=f'sm:{sub_id}',
            )
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


async def handle_change_devices_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: change_devices_menu:{sub_id} → buy/change device limit."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    from .devices import handle_change_devices

    await handle_change_devices(callback, db_user, db, state)


async def handle_device_management_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: device_management:{sub_id} → manage connected devices."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    from .devices import handle_device_management

    await handle_device_management(callback, db_user, db, state)


async def handle_subscription_delete_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Show delete confirmation for an expired/disabled subscription."""
    sub_id = _extract_sub_id(callback)
    if sub_id is None:
        await callback.answer(
            get_texts(getattr(db_user, 'language', None)).t('SUBSCRIPTION_UI_INVALID_FORMAT', 'Неверный формат'),
            show_alert=True,
        )
        return

    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer(
            get_texts(getattr(db_user, 'language', None)).t('SUBSCRIPTION_UI_NOT_FOUND', 'Подписка не найдена'),
            show_alert=True,
        )
        return

    if subscription.actual_status not in ('expired', 'disabled'):
        await callback.answer(
            get_texts(getattr(db_user, 'language', None)).t(
                'SUBSCRIPTION_UI_DELETE_EXPIRED_ONLY', 'Можно удалить только истекшую или отключённую подписку'
            ),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)
    tariff_name = subscription.tariff.name if subscription.tariff else texts.t('SUBSCRIPTION_UI_NAME', 'Подписка')

    text = texts.t(
        'SUBSCRIPTION_UI_DELETE_CONFIRM',
        '🗑 <b>Удалить подписку «{name}»?</b>\n\n⚠️ Подписка будет удалена безвозвратно.\nВсе данные, устройства и настройки будут потеряны.\nЭто действие нельзя отменить.',
    ).format(name=tariff_name)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_CONFIRM_DELETE', '🗑 Да, удалить'),
                    callback_data=f'sub_del_yes:{sub_id}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_UI_CANCEL', '◀️ Отмена'),
                    callback_data=f'sm:{sub_id}',
                )
            ],
        ]
    )

    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def handle_subscription_delete_execute(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Actually delete an expired/disabled subscription."""
    texts = get_texts(getattr(db_user, 'language', None))
    sub_id = _extract_sub_id(callback)
    if sub_id is None:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_INVALID_FORMAT', 'Неверный формат'),
            show_alert=True,
        )
        return

    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_NOT_FOUND', 'Подписка не найдена'),
            show_alert=True,
        )
        return

    deletable_statuses = {SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value}
    if getattr(subscription, 'actual_status', subscription.status) not in deletable_statuses:
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_DELETE_EXPIRED_ONLY', 'Можно удалить только истекшую или отключённую подписку'),
            show_alert=True,
        )
        return

    # Порядок удаления (грейс-гард → автоплатежи → панель → строка) живёт в общем
    # сервисе: своя копия здесь расходилась с ним — звала delete_user напрямую,
    # мимо REMNAWAVE_USER_DELETE_MODE и мимо правил адресации общего аккаунта
    # однотарифного режима.
    from app.services.grace_access_runtime import GraceAccessDeletionBlocked
    from app.services.subscription_deletion_service import delete_subscription_record

    try:
        await delete_subscription_record(db, subscription, deleted_by=f'user:{db_user.id}')
    except GraceAccessDeletionBlocked:
        await callback.answer(
            texts.t(
                'SUBSCRIPTION_UI_DELETE_GRACE_BLOCKED',
                'Подписку нельзя удалить, пока действует временный доступ для продления.',
            ),
            show_alert=True,
        )
        return

    logger.info(
        'Subscription deleted by user via bot',
        subscription_id=sub_id,
        user_id=db_user.id,
    )

    await callback.answer(texts.t('SUBSCRIPTION_UI_DELETED', 'Подписка удалена'), show_alert=True)

    # Return to subscriptions list
    await show_my_subscriptions(callback, db_user, db, state)


def _extract_sub_id(callback: types.CallbackQuery) -> int | None:
    """Extract subscription ID from callback_data format 'prefix:sub_id'."""
    parts = (callback.data or '').split(':')
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except (ValueError, TypeError):
            return None
    return None
