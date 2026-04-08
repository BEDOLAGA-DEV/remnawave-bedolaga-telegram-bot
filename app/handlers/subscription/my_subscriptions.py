"""
Multi-tariff subscription list handler.

Shows all user subscriptions with per-subscription management.
Only active when MULTI_TARIFF_ENABLED=True.
"""

from __future__ import annotations

import structlog
from aiogram import Router, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    decrement_subscription_server_counts,
    get_all_subscriptions_by_user_id,
    get_subscription_by_id_for_user,
)
from app.database.models import Subscription, SubscriptionStatus, User
from app.localization.texts import get_texts
from app.services.subscription_service import SubscriptionService


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


def _status_label(sub) -> str:
    """Return a short human-readable status label for non-active subscriptions."""
    actual = sub.actual_status
    if actual == 'expired':
        return ' (Истекла)'
    if actual == 'disabled':
        return ' (Отключена)'
    if actual == 'limited':
        return ' (Лимит)'
    return ''


def _format_subscription_line(sub, idx: int) -> str:
    """Format a single subscription for the list view."""
    tariff_name = sub.tariff.name if sub.tariff else 'Подписка'
    emoji = _status_emoji(sub)
    label = _status_label(sub)

    # Traffic info
    if sub.traffic_limit_gb == 0:
        traffic = '∞'
    else:
        used = f'{sub.traffic_used_gb:.1f}' if sub.traffic_used_gb else '0'
        traffic = f'{used}/{sub.traffic_limit_gb} ГБ'

    # Devices
    devices = f'{sub.device_limit} устр.' if sub.device_limit else ''

    # End date
    end_date = sub.end_date.strftime('%d.%m.%Y') if sub.end_date else '—'

    parts = [f'{emoji} <b>{idx}. {tariff_name}</b>{label}']
    parts.append(f'   📊 Трафик: {traffic}')
    if devices:
        parts.append(f'   📱 Устройства: {devices}')
    parts.append(f'   📅 До: {end_date}')

    return '\n'.join(parts)


def _build_subscriptions_keyboard(subscriptions: list, language: str) -> types.InlineKeyboardMarkup:
    """Build inline keyboard with per-subscription management buttons."""
    buttons = []
    for idx, sub in enumerate(subscriptions, 1):
        tariff_name = sub.tariff.name if sub.tariff else f'Подписка #{sub.id}'
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=f'⚙️ {tariff_name}',
                    callback_data=f'sm:{sub.id}',
                )
            ]
        )

    # "Buy another tariff" button
    texts = get_texts(language)
    buy_text = getattr(texts, 'MENU_BUY_SUBSCRIPTION', 'Купить ещё тариф')
    buttons.append(
        [
            types.InlineKeyboardButton(text=f'➕ {buy_text}', callback_data='menu_buy'),
        ]
    )
    # Back button
    buttons.append(
        [
            types.InlineKeyboardButton(text='◀️ Назад', callback_data='back_to_menu'),
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_subscription_detail_keyboard(sub_id: int, sub=None) -> types.InlineKeyboardMarkup:
    """Build keyboard for single subscription management.

    For expired/disabled subscriptions, only 'Renew' and 'Back' are shown —
    connection link and traffic/device management are irrelevant.
    """
    is_inactive = sub is not None and sub.actual_status in ('expired', 'disabled')

    buttons = []

    if not is_inactive:
        buttons.append([types.InlineKeyboardButton(text='🔗 Ссылка подключения', callback_data=f'sl:{sub_id}')])

    buttons.append([types.InlineKeyboardButton(text='🔄 Продлить', callback_data=f'se:{sub_id}')])

    if not is_inactive:
        buttons.append([types.InlineKeyboardButton(text='📊 Трафик', callback_data=f'st:{sub_id}')])
        buttons.append([types.InlineKeyboardButton(text='📱 Устройства', callback_data=f'sd:{sub_id}')])

    if is_inactive:
        buttons.append([types.InlineKeyboardButton(text='🗑 Удалить подписку', callback_data=f'sub_del:{sub_id}')])

    buttons.append([types.InlineKeyboardButton(text='◀️ К списку подписок', callback_data='my_subscriptions')])

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

    subscriptions = await get_all_subscriptions_by_user_id(db, db_user.id)

    if not subscriptions:
        text = '📋 <b>Мои подписки</b>\n\nУ вас нет подписок.'
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🛒 Купить подписку', callback_data='menu_buy')],
                [types.InlineKeyboardButton(text='◀️ Назад', callback_data='back_to_menu')],
            ]
        )
    else:
        lines = ['📋 <b>Мои подписки</b>\n']
        for idx, sub in enumerate(subscriptions, 1):
            lines.append(_format_subscription_line(sub, idx))
            lines.append('')  # empty line between subscriptions
        text = '\n'.join(lines)
        keyboard = _build_subscriptions_keyboard(subscriptions, db_user.language)

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
        await callback.answer('Неверный формат', show_alert=True)
        return

    sub_id = int(parts[1])
    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)

    if not subscription:
        await callback.answer('Подписка не найдена', show_alert=True)
        return

    tariff_name = subscription.tariff.name if subscription.tariff else 'Подписка'

    # Traffic
    if subscription.traffic_limit_gb == 0:
        traffic = '∞ ГБ'
    else:
        used = f'{subscription.traffic_used_gb:.1f}' if subscription.traffic_used_gb else '0'
        traffic = f'{used} / {subscription.traffic_limit_gb} ГБ'

    end_date = subscription.end_date.strftime('%d.%m.%Y %H:%M') if subscription.end_date else '—'
    status = subscription.status_display

    text = (
        f'📋 <b>{tariff_name}</b>\n\n'
        f'Статус: {status}\n'
        f'📊 Трафик: {traffic}\n'
        f'📱 Устройства: {subscription.device_limit}\n'
        f'📅 До: {end_date}\n'
    )

    if subscription.subscription_url and not settings.should_hide_subscription_link():
        text += f'\n🔗 <code>{subscription.subscription_url}</code>'

    keyboard = _build_subscription_detail_keyboard(sub_id, sub=subscription)

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
    sub_id = _extract_sub_id(callback)
    if sub_id is None:
        await callback.answer('Неверный формат', show_alert=True)
        return None

    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer('Подписка не найдена', show_alert=True)
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

    current_devices = subscription.device_limit or 0
    text = f'📱 <b>Устройства</b>\n\nТекущий лимит: {current_devices} устройств\n\nВыберите действие:'

    keyboard = []
    if can_buy_devices:
        keyboard.append(
            [types.InlineKeyboardButton(text='➕ Докупить устройства', callback_data=f'change_devices_menu:{sub_id}')]
        )
    keyboard.append(
        [types.InlineKeyboardButton(text='📱 Управление устройствами', callback_data=f'device_management:{sub_id}')]
    )
    keyboard.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data=f'sm:{sub_id}')])

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
        await callback.answer('Неверный формат', show_alert=True)
        return

    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer('Подписка не найдена', show_alert=True)
        return

    if subscription.actual_status not in ('expired', 'disabled'):
        await callback.answer('Можно удалить только истекшую или отключённую подписку', show_alert=True)
        return

    tariff_name = subscription.tariff.name if subscription.tariff else 'Подписка'

    text = (
        f'🗑 <b>Удалить подписку «{tariff_name}»?</b>\n\n'
        '⚠️ Подписка будет удалена безвозвратно.\n'
        'Все данные, устройства и настройки будут потеряны.\n'
        'Это действие нельзя отменить.'
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🗑 Да, удалить', callback_data=f'sub_del_yes:{sub_id}')],
            [types.InlineKeyboardButton(text='◀️ Отмена', callback_data=f'sm:{sub_id}')],
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
    sub_id = _extract_sub_id(callback)
    if sub_id is None:
        await callback.answer('Неверный формат', show_alert=True)
        return

    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer('Подписка не найдена', show_alert=True)
        return

    deletable_statuses = {SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value}
    if getattr(subscription, 'actual_status', subscription.status) not in deletable_statuses:
        await callback.answer('Можно удалить только истекшую или отключённую подписку', show_alert=True)
        return

    # Delete from RemnaWave panel (stops webhooks / phantom notifications)
    if subscription.remnawave_uuid:
        try:
            service = SubscriptionService()
            await service.delete_remnawave_user(subscription.remnawave_uuid)
        except Exception as e:
            logger.warning('Failed to delete RemnaWave user on subscription delete', error=e)

    # Decrement server counts
    await decrement_subscription_server_counts(db, subscription)

    # Hard delete from DB
    await db.delete(subscription)
    await db.commit()

    logger.info(
        'Subscription deleted by user via bot',
        subscription_id=sub_id,
        user_id=db_user.id,
    )

    await callback.answer('Подписка удалена', show_alert=True)

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
