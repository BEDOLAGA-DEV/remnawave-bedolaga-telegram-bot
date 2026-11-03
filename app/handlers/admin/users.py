import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.campaign import (
    get_campaign_registration_by_user,
    get_campaign_statistics,
)
from app.database.crud.promo_group import get_promo_groups_with_counts
from app.database.crud.server_squad import (
    get_all_server_squads,
    get_server_squad_by_id,
    get_server_squad_by_uuid,
)
from app.database.crud.tariff import get_all_tariffs, get_tariff_by_id
from app.database.crud.user import (
    get_referrals,
    get_user_by_email,
    get_user_by_id,
    get_user_by_telegram_id,
    get_user_by_username,
)
from app.database.models import Subscription, SubscriptionStatus, TransactionType, User, UserStatus
from app.keyboards.admin import (
    get_admin_pagination_keyboard,
    get_admin_users_filters_keyboard,
    get_admin_users_keyboard,
    get_confirmation_keyboard,
    get_user_management_keyboard,
    get_user_promo_group_keyboard,
    get_user_restrictions_keyboard,
)
from app.localization.texts import Texts, get_texts
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.services.user_service import UserService
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime, format_time_ago
from app.utils.formatting import user_html_link
from app.utils.photo_message import safe_edit_or_resend
from app.utils.subscription_time import format_time_left, local_days_until
from app.utils.timezone import format_local_datetime
from app.utils.user_utils import get_effective_referral_commission_percent


logger = structlog.get_logger(__name__)


# =============================================================================
# Конфигурация фильтров пользователей
# =============================================================================


class UserFilterType(Enum):
    """Типы фильтрации пользователей."""

    BALANCE = 'balance'
    CAMPAIGN = 'campaign'
    POTENTIAL_CUSTOMERS = 'potential_customers'


@dataclass
class UserFilterConfig:
    """Конфигурация для типа фильтра."""

    fsm_state: Any  # State из AdminStates
    title: str
    empty_message: str
    pagination_prefix: str
    order_param: str  # параметр для get_users_page


# Конфигурация для каждого типа фильтра
USER_FILTER_CONFIGS: dict[UserFilterType, UserFilterConfig] = {
    UserFilterType.BALANCE: UserFilterConfig(
        fsm_state=AdminStates.viewing_user_from_balance_list,
        title='👥 <b>Список пользователей по балансу</b>',
        empty_message='👥 Пользователи не найдены',
        pagination_prefix='admin_users_balance_list',
        order_param='order_by_balance',
    ),
    UserFilterType.CAMPAIGN: UserFilterConfig(
        fsm_state=AdminStates.viewing_user_from_campaign_list,
        title='👥 <b>Пользователи по кампании регистрации</b>',
        empty_message='📢 Пользователи с кампанией не найдены',
        pagination_prefix='admin_users_campaign_list',
        order_param='',  # использует специальный метод
    ),
    UserFilterType.POTENTIAL_CUSTOMERS: UserFilterConfig(
        fsm_state=AdminStates.viewing_user_from_potential_customers_list,
        title='👥 <b>Потенциальные клиенты</b>',
        empty_message='💰 Потенциальные клиенты не найдены',
        pagination_prefix='admin_users_potential_customers_list',
        order_param='',  # использует специальный метод
    ),
}


def _get_user_status_emoji(user: User) -> str:
    """Возвращает эмодзи статуса пользователя."""
    if user.status == UserStatus.ACTIVE.value:
        return '✅'
    if user.status == UserStatus.BLOCKED.value:
        return '🚫'
    return '🗑️'


def _get_subscription_emoji(user: User) -> str:
    """Возвращает эмодзи подписки пользователя."""
    subscriptions = getattr(user, 'subscriptions', None) or []
    if not subscriptions:
        return '❌'
    # Check if any subscription is active
    active = [s for s in subscriptions if s.is_active]
    if not active:
        return '⏰'
    # Check if any is trial
    if any(s.is_trial for s in active):
        return '🎁'
    return '💎'


def _build_user_button_text(
    user: User, filter_type: UserFilterType, extra_data: dict[str, Any] | None = None, language: str = 'ru'
) -> str:
    """
    Формирует текст кнопки пользователя в зависимости от типа фильтра.

    Args:
        user: Пользователь
        filter_type: Тип фильтра
        extra_data: Дополнительные данные (spending_map, campaign_map и т.д.)
        language: Язык пользователя
    """
    texts = get_texts(language)
    status_emoji = _get_user_status_emoji(user)
    sub_emoji = _get_subscription_emoji(user)

    if filter_type == UserFilterType.BALANCE:
        button_text = f'{status_emoji} {sub_emoji} {user.full_name}'
        if user.balance_kopeks > 0:
            button_text += f' | 💰 {settings.format_price(user.balance_kopeks)}'
        # Use first active subscription from subscriptions list
        first_sub = next((s for s in (getattr(user, 'subscriptions', None) or []) if s.is_active), None)
        if first_sub and first_sub.end_date:
            days_left = local_days_until(first_sub.end_date)
            button_text += texts.t('ADMIN_USERS_LIST_013', ' | 📅 {value1}д').format(value1=days_left)

    elif filter_type == UserFilterType.CAMPAIGN:
        info = extra_data.get(user.id, {}) if extra_data else {}
        campaign_name = info.get('campaign_name') or texts.t('ADMIN_USERS_LIST_015', 'Без кампании')
        registered_at = info.get('registered_at')
        registered_display = (
            format_datetime(registered_at) if registered_at else texts.t('ADMIN_USERS_LIST_016', 'неизвестно')
        )
        button_text = f'{status_emoji} {user.full_name} | 📢 {campaign_name} | 📅 {registered_display}'

    else:
        button_text = f'{status_emoji} {sub_emoji} {user.full_name}'

    # Обрезка длинных имён
    if len(button_text) > 60:
        short_name = user.full_name[:17] + '...' if len(user.full_name) > 20 else user.full_name
        # Пересобираем с коротким именем
        if filter_type == UserFilterType.BALANCE:
            button_text = f'{status_emoji} {sub_emoji} {short_name}'
            if user.balance_kopeks > 0:
                button_text += f' | 💰 {settings.format_price(user.balance_kopeks)}'
        else:
            button_text = f'{status_emoji} {short_name}'

    return button_text


async def _show_users_list_filtered(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    filter_type: UserFilterType,
    page: int = 1,
) -> None:
    """
    Универсальная функция отображения отфильтрованного списка пользователей.

    Args:
        callback: Callback query
        db_user: Текущий администратор
        db: Сессия БД
        state: FSM состояние
        filter_type: Тип фильтра
        page: Номер страницы
    """
    config = USER_FILTER_CONFIGS[filter_type]
    texts = get_texts(db_user.language)
    title_key, empty_key = {
        UserFilterType.BALANCE: ('ADMIN_USERS_LIST_007', 'ADMIN_USERS_LIST_008'),
        UserFilterType.CAMPAIGN: ('ADMIN_USERS_LIST_009', 'ADMIN_USERS_LIST_010'),
        UserFilterType.POTENTIAL_CUSTOMERS: ('ADMIN_USERS_LIST_011', 'ADMIN_USERS_LIST_012'),
    }[filter_type]
    config_title = texts.t(title_key, config.title)
    config_empty_message = texts.t(empty_key, config.empty_message)

    # Устанавливаем FSM состояние
    await state.set_state(config.fsm_state)

    user_service = UserService()
    extra_data: dict[str, Any] | None = None

    # Получаем данные в зависимости от типа фильтра
    if filter_type == UserFilterType.CAMPAIGN:
        users_data = await user_service.get_users_by_campaign_page(db, page=page, limit=10)
        extra_data = users_data.get('campaigns', {})
    else:
        kwargs = {'db': db, 'page': page, 'limit': 10, config.order_param: True}
        users_data = await user_service.get_users_page(**kwargs)

    users = users_data.get('users', [])

    # Если нет пользователей
    if not users:
        await callback.message.edit_text(config_empty_message, reply_markup=get_admin_users_keyboard(db_user.language))
        await callback.answer()
        return

    # Формируем текст заголовка
    text = texts.t('ADMIN_USERS_LIST_000', '{value1} (стр. {value2}/{value3})\n\n').format(
        value1=config_title, value2=page, value3=users_data['total_pages']
    )
    text += texts.t('ADMIN_USERS_LIST_001', 'Нажмите на пользователя для управления:')

    # Формируем клавиатуру
    keyboard = []
    for user in users:
        button_text = _build_user_button_text(user, filter_type, extra_data, db_user.language)
        keyboard.append([types.InlineKeyboardButton(text=button_text, callback_data=f'admin_user_manage_{user.id}')])

    # Пагинация
    if users_data['total_pages'] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data['current_page'],
            users_data['total_pages'],
            config.pagination_prefix,
            'admin_users',
            db_user.language,
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    # Дополнительные кнопки
    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_LIST_017', '🔍 Поиск'),
                    callback_data='admin_users_search',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_LIST_018', '📊 Статистика'),
                    callback_data='admin_users_stats',
                ),
            ],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIST_019', '⬅️ Назад'), callback_data='admin_users')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_users_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)

    text = (
        get_texts(db_user.language)
        .t(
            'ADMIN_USERS_LIST_002',
            '\n👥 <b>Управление пользователями</b>\n\n📊 <b>Статистика:</b>\n• Всего: {value1}\n• Активных: {value2}\n• Заблокированных: {value3}\n\n📈 <b>Новые пользователи:</b>\n• Сегодня: {value4}\n• За неделю: {value5}\n• За месяц: {value6}\n\nВыберите действие:\n',
        )
        .format(
            value1=stats['total_users'],
            value2=stats['active_users'],
            value3=stats['blocked_users'],
            value4=stats['new_today'],
            value5=stats['new_week'],
            value6=stats['new_month'],
        )
    )

    await callback.message.edit_text(text, reply_markup=get_admin_users_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_filters(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    text = get_texts(db_user.language).t(
        'ADMIN_USERS_LIST_003', '⚙️ <b>Фильтры пользователей</b>\n\nВыберите фильтр для отображения пользователей:\n'
    )

    await callback.message.edit_text(text, reply_markup=get_admin_users_filters_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_list(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext, page: int = 1
):
    # Сбрасываем состояние, так как мы в обычном списке
    texts = get_texts(db_user.language)
    await state.set_state(None)

    user_service = UserService()
    users_data = await user_service.get_users_page(db, page=page, limit=10)

    if not users_data['users']:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_LIST_008', '👥 Пользователи не найдены'),
            reply_markup=get_admin_users_keyboard(db_user.language),
        )
        await callback.answer()
        return

    text = texts.t('ADMIN_USERS_LIST_004', '👥 <b>Список пользователей</b> (стр. {value1}/{value2})\n\n').format(
        value1=page, value2=users_data['total_pages']
    )
    text += texts.t('ADMIN_USERS_LIST_001', 'Нажмите на пользователя для управления:')

    keyboard = []

    for user in users_data['users']:
        if user.status == UserStatus.ACTIVE.value:
            status_emoji = '✅'
        elif user.status == UserStatus.BLOCKED.value:
            status_emoji = '🚫'
        else:
            status_emoji = '🗑️'

        subscription_emoji = ''
        subs = getattr(user, 'subscriptions', None) or []
        subscription = next((s for s in subs if s.is_active), subs[0] if subs else None)
        if subscription:
            if subscription.is_trial:
                subscription_emoji = '🎁'
            elif subscription.is_active:
                subscription_emoji = '💎'
            else:
                subscription_emoji = '⏰'
        else:
            subscription_emoji = '❌'

        button_text = f'{status_emoji} {subscription_emoji} {user.full_name}'

        if user.balance_kopeks > 0:
            button_text += f' | 💰 {settings.format_price(user.balance_kopeks)}'

        button_text += f' | 📅 {format_time_ago(user.created_at, db_user.language)}'

        if len(button_text) > 60:
            short_name = user.full_name
            if len(short_name) > 20:
                short_name = short_name[:17] + '...'

            button_text = f'{status_emoji} {subscription_emoji} {short_name}'
            if user.balance_kopeks > 0:
                button_text += f' | 💰 {settings.format_price(user.balance_kopeks)}'

        keyboard.append([types.InlineKeyboardButton(text=button_text, callback_data=f'admin_user_manage_{user.id}')])

    if users_data['total_pages'] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data['current_page'], users_data['total_pages'], 'admin_users_list', 'admin_users', db_user.language
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_LIST_017', '🔍 Поиск'),
                    callback_data='admin_users_search',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_LIST_018', '📊 Статистика'),
                    callback_data='admin_users_stats',
                ),
            ],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIST_019', '⬅️ Назад'), callback_data='admin_users')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_users_list_by_balance(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext, page: int = 1
):
    """Список пользователей, отсортированный по балансу (убывание)."""
    await _show_users_list_filtered(callback, db_user, db, state, UserFilterType.BALANCE, page)


@admin_required
@error_handler
async def show_users_ready_to_renew(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext, page: int = 1
):
    """Показывает пользователей с истекшей подпиской и балансом >= порога."""
    await state.set_state(AdminStates.viewing_user_from_ready_to_renew_list)

    texts = get_texts(db_user.language)
    threshold = getattr(
        settings,
        'SUBSCRIPTION_RENEWAL_BALANCE_THRESHOLD_KOPEKS',
        20000,
    )

    user_service = UserService()
    users_data = await user_service.get_users_ready_to_renew(
        db,
        min_balance_kopeks=threshold,
        page=page,
        limit=10,
    )

    amount_text = settings.format_price(threshold)
    header = texts.t(
        'ADMIN_USERS_FILTER_RENEW_READY_TITLE',
        '♻️ Пользователи готовы к продлению',
    )
    description = texts.t(
        'ADMIN_USERS_FILTER_RENEW_READY_DESC',
        'Подписка истекла, а на балансе осталось {amount} или больше.',
    ).format(amount=amount_text)

    if not users_data['users']:
        empty_text = texts.t(
            'ADMIN_USERS_FILTER_RENEW_READY_EMPTY',
            'Сейчас нет пользователей, которые подходят под этот фильтр.',
        )
        await callback.message.edit_text(
            f'{header}\n\n{description}\n\n{empty_text}',
            reply_markup=get_admin_users_keyboard(db_user.language),
        )
        await callback.answer()
        return

    text = f'{header}\n\n{description}\n\n'
    text += texts.t('ADMIN_USERS_LIST_001', 'Нажмите на пользователя для управления:')

    keyboard = []
    current_time = datetime.now(UTC)

    for user in users_data['users']:
        subscription = user.subscription  # Uses primary subscription (multi-tariff compatible via property)
        status_emoji = '✅' if user.status == UserStatus.ACTIVE.value else '🚫'
        subscription_emoji = '❌'
        expired_days = '?'

        if subscription:
            if subscription.is_trial:
                subscription_emoji = '🎁'
            elif subscription.is_active:
                subscription_emoji = '💎'
            else:
                subscription_emoji = '⏰'

            if subscription.end_date:
                delta = current_time - subscription.end_date
                expired_days = delta.days

        button_text = texts.t(
            'ADMIN_USERS_LIST_006', '{value1} {value2} {value3} | 💰 {value4} | ⏰ {value5}д ист.'
        ).format(
            value1=status_emoji,
            value2=subscription_emoji,
            value3=user.full_name,
            value4=settings.format_price(user.balance_kopeks),
            value5=expired_days,
        )

        if len(button_text) > 60:
            short_name = user.full_name
            if len(short_name) > 20:
                short_name = short_name[:17] + '...'
            button_text = (
                f'{status_emoji} {subscription_emoji} {short_name} | 💰 {settings.format_price(user.balance_kopeks)}'
            )

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=f'admin_user_manage_{user.id}',
                )
            ]
        )

    if users_data['total_pages'] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data['current_page'],
            users_data['total_pages'],
            'admin_users_ready_to_renew_list',
            'admin_users_ready_to_renew_filter',
            db_user.language,
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_LIST_017', '🔍 Поиск'),
                    callback_data='admin_users_search',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_LIST_018', '📊 Статистика'),
                    callback_data='admin_users_stats',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_LIST_019', '⬅️ Назад'),
                    callback_data='admin_users',
                )
            ],
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_potential_customers(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext, page: int = 1
):
    """Показывает пользователей без активной подписки с балансом >= месячной цены."""
    await state.set_state(AdminStates.viewing_user_from_potential_customers_list)

    texts = get_texts(db_user.language)
    from app.config import PERIOD_PRICES

    monthly_price = PERIOD_PRICES.get(30, 99000)

    user_service = UserService()
    users_data = await user_service.get_potential_customers(
        db,
        min_balance_kopeks=monthly_price,
        page=page,
        limit=10,
    )

    amount_text = settings.format_price(monthly_price)
    header = texts.t(
        'ADMIN_USERS_FILTER_POTENTIAL_CUSTOMERS_TITLE',
        '💰 Потенциальные клиенты',
    )
    description = texts.t(
        'ADMIN_USERS_FILTER_POTENTIAL_CUSTOMERS_DESC',
        'Пользователи без активной подписки с балансом {amount} или больше.',
    ).format(amount=amount_text)

    if not users_data['users']:
        empty_text = texts.t(
            'ADMIN_USERS_FILTER_POTENTIAL_CUSTOMERS_EMPTY',
            'Сейчас нет пользователей, которые подходят под этот фильтр.',
        )
        await callback.message.edit_text(
            f'{header}\n\n{description}\n\n{empty_text}',
            reply_markup=get_admin_users_keyboard(db_user.language),
        )
        await callback.answer()
        return

    text = f'{header}\n\n{description}\n\n'
    text += texts.t('ADMIN_USERS_LIST_001', 'Нажмите на пользователя для управления:')

    keyboard = []

    for user in users_data['users']:
        subscription = user.subscription  # Uses primary subscription (multi-tariff compatible via property)
        status_emoji = '✅' if user.status == UserStatus.ACTIVE.value else '🚫'
        subscription_emoji = '❌'

        if subscription:
            if subscription.is_trial:
                subscription_emoji = '🎁'
            elif subscription.is_active:
                subscription_emoji = '💎'
            else:
                subscription_emoji = '⏰'

        button_text = (
            f'{status_emoji} {subscription_emoji} {user.full_name} | 💰 {settings.format_price(user.balance_kopeks)}'
        )

        if len(button_text) > 60:
            short_name = user.full_name
            if len(short_name) > 20:
                short_name = short_name[:17] + '...'
            button_text = (
                f'{status_emoji} {subscription_emoji} {short_name} | 💰 {settings.format_price(user.balance_kopeks)}'
            )

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=button_text,
                    callback_data=f'admin_user_manage_{user.id}',
                )
            ]
        )

    if users_data['total_pages'] > 1:
        pagination_row = get_admin_pagination_keyboard(
            users_data['current_page'],
            users_data['total_pages'],
            'admin_users_potential_customers_list',
            'admin_users_potential_customers_filter',
            db_user.language,
        ).inline_keyboard[0]
        keyboard.append(pagination_row)

    keyboard.extend(
        [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_LIST_017', '🔍 Поиск'),
                    callback_data='admin_users_search',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_LIST_018', '📊 Статистика'),
                    callback_data='admin_users_stats',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_LIST_019', '⬅️ Назад'),
                    callback_data='admin_users',
                )
            ],
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_users_list_by_campaign(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext, page: int = 1
):
    """Список пользователей по кампании регистрации."""
    await _show_users_list_filtered(callback, db_user, db, state, UserFilterType.CAMPAIGN, page)


@admin_required
@error_handler
async def handle_users_list_pagination_fixed(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    try:
        callback_parts = callback.data.split('_')
        page = int(callback_parts[-1])
        await show_users_list(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error('Ошибка парсинга номера страницы', error=e)
        await show_users_list(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_users_balance_list_pagination(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    try:
        callback_parts = callback.data.split('_')
        page = int(callback_parts[-1])
        await show_users_list_by_balance(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error('Ошибка парсинга номера страницы', error=e)
        await show_users_list_by_balance(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_users_ready_to_renew_pagination(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    try:
        page = int(callback.data.split('_')[-1])
        await show_users_ready_to_renew(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error('Ошибка парсинга номера страницы', error=e)
        await show_users_ready_to_renew(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_potential_customers_pagination(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    try:
        page = int(callback.data.split('_')[-1])
        await show_potential_customers(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error('Ошибка парсинга номера страницы', error=e)
        await show_potential_customers(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def handle_users_campaign_list_pagination(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    try:
        callback_parts = callback.data.split('_')
        page = int(callback_parts[-1])
        await show_users_list_by_campaign(callback, db_user, db, state, page)
    except (ValueError, IndexError) as e:
        logger.error('Ошибка парсинга номера страницы', error=e)
        await show_users_list_by_campaign(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def start_user_search(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_USERS_LIST_014',
            '🔍 <b>Поиск пользователя</b>\n\nВведите для поиска:\n• Telegram ID\n• Username (без @)\n• Имя или фамилию\n\nИли нажмите /cancel для отмены',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_LIST_020', '❌ Отмена'),
                        callback_data='admin_users',
                    )
                ]
            ]
        ),
    )

    await state.set_state(AdminStates.waiting_for_user_search)
    await callback.answer()


@admin_required
@error_handler
async def show_users_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)

    from sqlalchemy import func, or_, select

    current_time = datetime.now(UTC)

    active_subscription_query = (
        select(func.count(Subscription.id))
        .join(User, Subscription.user_id == User.id)
        .where(
            User.status == UserStatus.ACTIVE.value,
            Subscription.status.in_(
                [
                    SubscriptionStatus.ACTIVE.value,
                    SubscriptionStatus.TRIAL.value,
                ]
            ),
            Subscription.end_date > current_time,
        )
    )
    users_with_subscription = (await db.execute(active_subscription_query)).scalar() or 0

    trial_subscription_query = (
        select(func.count(Subscription.id))
        .join(User, Subscription.user_id == User.id)
        .where(
            User.status == UserStatus.ACTIVE.value,
            Subscription.end_date > current_time,
            or_(
                Subscription.status == SubscriptionStatus.TRIAL.value,
                Subscription.is_trial.is_(True),
            ),
        )
    )
    trial_users = (await db.execute(trial_subscription_query)).scalar() or 0

    users_without_subscription = max(
        stats['active_users'] - users_with_subscription,
        0,
    )

    avg_balance_result = await db.execute(
        select(func.avg(User.balance_kopeks)).where(User.status == UserStatus.ACTIVE.value)
    )
    avg_balance = avg_balance_result.scalar() or 0

    text = texts.t(
        'ADMIN_USERS_LIST_005',
        '\n📊 <b>Детальная статистика пользователей</b>\n\n👥 <b>Общие показатели:</b>\n• Всего: {value1}\n• Активных: {value2}\n• Заблокированных: {value3}\n\n📱 <b>Подписки:</b>\n• С активной подпиской: {value4}\n• На триале: {value5}\n• Без подписки: {value6}\n\n💰 <b>Финансы:</b>\n• Средний баланс: {value7}\n\n📈 <b>Регистрации:</b>\n• Сегодня: {value8}\n• За неделю: {value9}\n• За месяц: {value10}\n\n📊 <b>Активность:</b>\n• Конверсия в подписку: {value11:.1f}%\n• Доля триальных: {value12:.1f}%\n',
    ).format(
        value1=stats['total_users'],
        value2=stats['active_users'],
        value3=stats['blocked_users'],
        value4=users_with_subscription,
        value5=trial_users,
        value6=users_without_subscription,
        value7=settings.format_price(int(avg_balance)),
        value8=stats['new_today'],
        value9=stats['new_week'],
        value10=stats['new_month'],
        value11=users_with_subscription / max(stats['active_users'], 1) * 100,
        value12=trial_users / max(users_with_subscription, 1) * 100,
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_LIST_021', '🔄 Обновить'),
                        callback_data='admin_users_stats',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_LIST_019', '⬅️ Назад'),
                        callback_data='admin_users',
                    )
                ],
            ]
        ),
    )
    await callback.answer()


async def _render_user_subscription_overview(
    callback: types.CallbackQuery,
    db: AsyncSession,
    user_id: int,
    subscription_id: int | None = None,
    language: str = 'ru',
) -> bool:
    texts = get_texts(language)
    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USERS_PROFILE_024', '❌ Пользователь не найден'), show_alert=True)
        return False

    user = profile['user']

    # Multi-tariff: show subscription picker if multiple subscriptions and no explicit choice
    if settings.is_multi_tariff_enabled() and not subscription_id:
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        all_subs = await get_active_subscriptions_by_user_id(db, user_id)
        await db.refresh(user, ['subscriptions'])
        all_user_subs = getattr(user, 'subscriptions', []) or []
        subs_list = all_user_subs if len(all_user_subs) > len(all_subs) else all_subs

        if len(subs_list) > 1:
            user_link = user_html_link(user)
            user_id_display = user.telegram_id or user.email or f'#{user.id}'
            text = texts.t('ADMIN_USERS_PROFILE_015', '📱 <b>Выберите подписку для управления</b>\n\n')
            text += f'👤 {user_link} (ID: <code>{user_id_display}</code>)\n\n'
            text += texts.t('ADMIN_USERS_PROFILE_016', 'У пользователя <b>{value1}</b> подписок:\n\n').format(
                value1=len(subs_list)
            )

            picker_keyboard = []
            for sub in sorted(subs_list, key=lambda s: s.id):
                status_emoji = '✅' if sub.is_active else ('⏸' if sub.status == 'disabled' else '❌')
                tariff_name = ''
                if sub.tariff_id:
                    tariff = await get_tariff_by_id(db, sub.tariff_id)
                    tariff_name = f' • {html.escape(tariff.name)}' if tariff else ''

                days_left = local_days_until(sub.end_date) if sub.end_date else 0
                btn_text = texts.t('ADMIN_USERS_PROFILE_025', '{value1} #{value2}{value3} ({value4}д.)').format(
                    value1=status_emoji, value2=sub.id, value3=tariff_name, value4=days_left
                )
                picker_keyboard.append(
                    [
                        types.InlineKeyboardButton(
                            text=btn_text,
                            callback_data=f'admin_user_sub_select_{user_id}_{sub.id}',
                        )
                    ]
                )

            picker_keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_PROFILE_039', '⬅️ К пользователю'),
                        callback_data=f'admin_user_manage_{user_id}',
                    )
                ]
            )
            await callback.message.edit_text(
                text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=picker_keyboard)
            )
            return True
        if len(subs_list) == 1:
            subscription_id = subs_list[0].id

    # Fetch specific subscription if subscription_id provided
    if subscription_id and settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_subscription_by_id_for_user

        subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    else:
        subscription = profile['subscription']

    # Suffix used to route back to this exact subscription in multi-tariff mode
    _sid = f'_s{subscription.id}' if settings.is_multi_tariff_enabled() and subscription else ''

    text = texts.t('ADMIN_USERS_PROFILE_000', '📱 <b>Подписка и настройки пользователя</b>\n\n')
    user_link = user_html_link(user)
    user_id_display = user.telegram_id or user.email or f'#{user.id}'
    text += f'👤 {user_link} (ID: <code>{user_id_display}</code>)\n\n'

    keyboard = []

    if subscription:
        status_emoji = '✅' if subscription.is_active else '❌'
        type_emoji = '🎁' if subscription.is_trial else '💎'

        traffic_display = f'{subscription.traffic_used_gb:.1f}/'
        if subscription.traffic_limit_gb == 0:
            traffic_display += texts.t('ADMIN_USERS_PROFILE_017', '♾️ ГБ')
        else:
            traffic_display += texts.t('ADMIN_USERS_PROFILE_018', '{value1} ГБ').format(
                value1=subscription.traffic_limit_gb
            )

        text += texts.t('ADMIN_USERS_PROFILE_005', '<b>Статус:</b> {value1} {value2}\n').format(
            value1=status_emoji,
            value2=texts.t('ADMIN_USERS_PROFILE_035', 'Активна')
            if subscription.is_active
            else texts.t('ADMIN_USERS_PROFILE_036', 'Неактивна'),
        )
        text += texts.t('ADMIN_USERS_PROFILE_006', '<b>Тип:</b> {value1} {value2}\n').format(
            value1=type_emoji,
            value2=texts.t('ADMIN_USERS_PROFILE_037', 'Триал')
            if subscription.is_trial
            else texts.t('ADMIN_USERS_PROFILE_038', 'Платная'),
        )

        # Отображение тарифа
        if subscription.tariff_id:
            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff:
                text += texts.t('ADMIN_USERS_PROFILE_026', '<b>Тариф:</b> 📦 {value1}\n').format(
                    value1=html.escape(tariff.name)
                )
            else:
                text += texts.t('ADMIN_USERS_PROFILE_027', '<b>Тариф:</b> ID {value1} (удалён)\n').format(
                    value1=subscription.tariff_id
                )

        text += texts.t('ADMIN_USERS_PROFILE_007', '<b>Начало:</b> {value1}\n').format(
            value1=format_datetime(subscription.start_date)
        )
        text += texts.t('ADMIN_USERS_PROFILE_008', '<b>Окончание:</b> {value1}\n').format(
            value1=format_datetime(subscription.end_date)
        )
        text += texts.t('ADMIN_USERS_PROFILE_009', '<b>Трафик:</b> {value1}\n').format(value1=traffic_display)
        text += texts.t('ADMIN_USERS_PROFILE_010', '<b>Устройства:</b> {value1}\n').format(
            value1=Texts.format_device_limit(subscription.device_limit)
        )

        if subscription.is_active:
            text += texts.t('ADMIN_USERS_PROFILE_019', '<b>Осталось:</b> {value1}\n').format(
                value1=format_time_left(texts, subscription.end_date)
            )

        current_squads = subscription.connected_squads or []
        if current_squads:
            text += texts.t('ADMIN_USERS_PROFILE_020', '\n<b>Подключенные серверы:</b>\n')
            for squad_uuid in current_squads:
                try:
                    server = await get_server_squad_by_uuid(db, squad_uuid)
                    if server:
                        text += f'• {html.escape(server.display_name)}\n'
                    else:
                        text += texts.t('ADMIN_USERS_PROFILE_042', '• {value1}... (неизвестный)\n').format(
                            value1=squad_uuid[:8]
                        )
                except Exception as e:
                    logger.error('Ошибка получения сервера', squad_uuid=squad_uuid, error=e)
                    text += texts.t('ADMIN_USERS_PROFILE_043', '• {value1}... (ошибка загрузки)\n').format(
                        value1=squad_uuid[:8]
                    )
        else:
            text += texts.t('ADMIN_USERS_PROFILE_021', '\n<b>Подключенные серверы:</b> отсутствуют\n')

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_044', '⏰ Продлить'),
                    callback_data=f'admin_sub_extend_{user_id}{_sid}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_045', '💳 Купить подписку'),
                    callback_data=f'admin_sub_buy_{user_id}{_sid}',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_046', '🔄 Тип подписки'),
                    callback_data=f'admin_sub_change_type_{user_id}{_sid}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_047', '📊 Добавить трафик'),
                    callback_data=f'admin_sub_traffic_{user_id}{_sid}',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_048', '🌍 Сменить сервер'),
                    callback_data=f'admin_user_change_server_{user_id}{_sid}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_049', '📱 Устройства'),
                    callback_data=f'admin_user_devices_{user_id}{_sid}',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_050', '🛠️ Лимит трафика'),
                    callback_data=f'admin_user_traffic_{user_id}{_sid}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_051', '🔄 Сбросить устройства'),
                    callback_data=f'admin_user_reset_devices_{user_id}{_sid}',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_052', '💳 Автоплатёж'),
                    callback_data=f'admin_user_autopay_{user_id}{_sid}',
                ),
            ],
        ]

        # Кнопки тарифов в режиме тарифов
        if settings.is_tariffs_mode():
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_PROFILE_057', '📦 Сменить тариф'),
                        callback_data=f'admin_sub_change_tariff_{user_id}{_sid}',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_PROFILE_058', '💳 Купить тариф'),
                        callback_data=f'admin_tariff_buy_{user_id}{_sid}',
                    ),
                ]
            )

        if subscription.is_active:
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_PROFILE_059', '🚫 Деактивировать'),
                        callback_data=f'admin_sub_deactivate_{user_id}{_sid}',
                    )
                ]
            )
        else:
            row = [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_053', '✅ Активировать'),
                    callback_data=f'admin_sub_activate_{user_id}{_sid}',
                ),
            ]
            if settings.is_multi_tariff_enabled() and subscription_id:
                row.append(
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_PROFILE_060', '🗑 Удалить'),
                        callback_data=f'admin_sub_delete_{user_id}{_sid}',
                    )
                )
            keyboard.append(row)

        # Обнулить подписку «как будто не оформляли»: сбросить наспамленные дни и доступ,
        # отключить в панели — но СОХРАНИТЬ пользователя и его тикеты (в отличие от удаления).
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_054', '🧹 Обнулить подписку'),
                    callback_data=f'admin_sub_reset_{user_id}{_sid}',
                )
            ]
        )
    else:
        text += texts.t('ADMIN_USERS_PROFILE_011', '❌ <b>Подписка отсутствует</b>\n\n')
        text += texts.t('ADMIN_USERS_PROFILE_012', 'Пользователь еще не активировал подписку.')

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_055', '🎁 Выдать триал'),
                    callback_data=f'admin_sub_grant_trial_{user_id}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PROFILE_056', '💎 Выдать подписку'),
                    callback_data=f'admin_sub_grant_{user_id}',
                ),
            ]
        ]

    if subscription is not None:
        from app.services.cispay_recurrent_guard import has_live_cispay

        if await has_live_cispay(db, subscription.id):
            from app.handlers.subscription.cispay_recurrent import format_cispay_status
            from app.services.cispay_recurrent_checkout import get_cispay_agreement_state

            info = await get_cispay_agreement_state(db, user_id=user_id, subscription_id=subscription.id)
            text += '\n\n' + html.escape(format_cispay_status(info, texts))
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_PROFILE_061', 'Отключить автоплатёж CisPay'),
                        callback_data=f'admin_cisrec_cancel:{user_id}:{subscription.id}',
                    )
                ]
            )

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_PROFILE_039', '⬅️ К пользователю'),
                callback_data=f'admin_user_manage_{user_id}',
            )
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    return True


def _extract_admin_sub_context(callback_data: str) -> tuple[int, int | None]:
    """Extract user_id and optional subscription_id from admin callback_data.

    Patterns:
    - 'admin_sub_extend_123'       -> (123, None)
    - 'admin_sub_extend_123_s456'  -> (123, 456)
    """
    parts = callback_data.split('_')
    subscription_id = None
    if parts[-1].startswith('s') and parts[-1][1:].isdigit():
        subscription_id = int(parts[-1][1:])
        user_id = int(parts[-2])
    else:
        user_id = int(parts[-1])
    return user_id, subscription_id


@admin_required
@error_handler
async def show_user_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])

    if await _render_user_subscription_overview(callback, db, user_id, language=db_user.language):
        await callback.answer()


@admin_required
@error_handler
async def admin_select_user_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Handle subscription picker selection in multi-tariff mode."""
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    subscription_id = int(parts[-1])

    if await _render_user_subscription_overview(
        callback, db, user_id, subscription_id=subscription_id, language=db_user.language
    ):
        await callback.answer()


@admin_required
@error_handler
async def show_user_transactions(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])

    from app.database.crud.transaction import get_user_transactions

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(texts.t('ADMIN_USERS_PROFILE_024', '❌ Пользователь не найден'), show_alert=True)
        return

    transactions = await get_user_transactions(db, user_id, limit=10)

    text = texts.t('ADMIN_USERS_PROFILE_001', '💳 <b>Транзакции пользователя</b>\n\n')
    user_link = user_html_link(user)
    user_id_display = user.telegram_id or user.email or f'#{user.id}'
    text += f'👤 {user_link} (ID: <code>{user_id_display}</code>)\n'
    text += texts.t('ADMIN_USERS_PROFILE_002', '💰 Текущий баланс: {value1}\n\n').format(
        value1=settings.format_price(user.balance_kopeks)
    )

    if transactions:
        text += texts.t('ADMIN_USERS_PROFILE_013', '<b>Последние транзакции:</b>\n\n')

        for transaction in transactions:
            type_emoji = '📈' if transaction.amount_kopeks > 0 else '📉'
            text += f'{type_emoji} {settings.format_price(abs(transaction.amount_kopeks))}\n'
            text += f'📋 {html.escape(transaction.description or "")}\n'
            text += f'📅 {format_datetime(transaction.created_at)}\n\n'
    else:
        text += texts.t('ADMIN_USERS_PROFILE_014', '📭 <b>Транзакции отсутствуют</b>')

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_PROFILE_039', '⬅️ К пользователю'),
                        callback_data=f'admin_user_manage_{user_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_user_delete(callback: types.CallbackQuery, db_user: User):
    user_id = int(callback.data.split('_')[-1])

    await callback.message.edit_text(
        get_texts(db_user.language).t(
            'ADMIN_USERS_PROFILE_022',
            '🗑️ <b>Удаление пользователя</b>\n\n⚠️ <b>ВНИМАНИЕ!</b>\nВы уверены, что хотите удалить этого пользователя?\n\nЭто действие:\n• Пометит пользователя как удаленного\n• Деактивирует его подписку\n• Заблокирует доступ к боту\n\nДанное действие необратимо!',
        ),
        reply_markup=get_confirmation_keyboard(
            f'admin_user_delete_confirm_{user_id}', f'admin_user_manage_{user_id}', db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_user_account(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])

    user_service = UserService()
    delete_result = await user_service.delete_user_account(db, user_id, db_user.id, force_panel_delete=True)

    if delete_result.bot_deleted:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_PROFILE_028', '✅ Пользователь успешно удален'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_PROFILE_062', '👥 К списку пользователей'),
                            callback_data='admin_users_list',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_PROFILE_029', '❌ Ошибка удаления пользователя'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_PROFILE_063', '👤 К пользователю'),
                            callback_data=f'admin_user_manage_{user_id}',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_user_search(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    query = message.text.strip()

    if not query:
        await message.answer(texts.t('ADMIN_USERS_PROFILE_030', '❌ Введите корректный запрос для поиска'))
        return

    user_service = UserService()
    search_results = await user_service.search_users(db, query, page=1, limit=10)

    if not search_results['users']:
        await message.answer(
            texts.t('ADMIN_USERS_PROFILE_031', "🔍 По запросу '<b>{value1}</b>' ничего не найдено").format(
                value1=html.escape(query)
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_PROFILE_040', '⬅️ Назад'),
                            callback_data='admin_users',
                        )
                    ]
                ]
            ),
        )
        await state.clear()
        return

    text = texts.t('ADMIN_USERS_PROFILE_003', "🔍 <b>Результаты поиска:</b> '{value1}'\n\n").format(
        value1=html.escape(query)
    )
    text += texts.t('ADMIN_USERS_PROFILE_004', 'Выберите пользователя:')

    keyboard = []

    for user in search_results['users']:
        if user.status == UserStatus.ACTIVE.value:
            status_emoji = '✅'
        elif user.status == UserStatus.BLOCKED.value:
            status_emoji = '🚫'
        else:
            status_emoji = '🗑️'

        subscription_emoji = ''
        subs = getattr(user, 'subscriptions', None) or []
        subscription = next((s for s in subs if s.is_active), subs[0] if subs else None)
        if subscription:
            if subscription.is_trial:
                subscription_emoji = '🎁'
            elif subscription.is_active:
                subscription_emoji = '💎'
            else:
                subscription_emoji = '⏰'
        else:
            subscription_emoji = '❌'

        button_text = f'{status_emoji} {subscription_emoji} {user.full_name}'

        user_id_display = user.telegram_id or user.email or f'#{user.id}'
        button_text += f' | 🆔 {user_id_display}'

        if user.balance_kopeks > 0:
            button_text += f' | 💰 {settings.format_price(user.balance_kopeks)}'

        if len(button_text) > 60:
            short_name = user.full_name
            if len(short_name) > 15:
                short_name = short_name[:12] + '...'
            button_text = f'{status_emoji} {subscription_emoji} {short_name} | 🆔 {user_id_display}'

        keyboard.append([types.InlineKeyboardButton(text=button_text, callback_data=f'admin_user_manage_{user.id}')])

    keyboard.append(
        [types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_PROFILE_040', '⬅️ Назад'), callback_data='admin_users')]
    )

    await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.clear()


@admin_required
@error_handler
async def show_user_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    # Поддерживаем переход "из тикета": admin_user_manage_{userId}_from_ticket_{ticketId}
    parts = callback.data.split('_')
    try:
        user_id = int(parts[3])  # admin_user_manage_{userId}
    except Exception:
        user_id = int(callback.data.split('_')[-1])
    origin_ticket_id = None
    if 'from' in parts and 'ticket' in parts:
        try:
            origin_ticket_id = int(parts[-1])
        except Exception:
            origin_ticket_id = None
    # Если пришли из тикета — запомним в состоянии, чтобы сохранять кнопку возврата
    try:
        if origin_ticket_id:
            await state.update_data(origin_ticket_id=origin_ticket_id, origin_ticket_user_id=user_id)
    except Exception:
        pass
    # Если не пришло в колбэке — попробуем достать из состояния
    if origin_ticket_id is None:
        try:
            data_state = await state.get_data()
            if data_state.get('origin_ticket_user_id') == user_id:
                origin_ticket_id = data_state.get('origin_ticket_id')
        except Exception:
            pass

    # Проверяем, откуда пришел пользователь
    back_callback = 'admin_users_list'

    # Если callback_data содержит информацию о том, что мы пришли из списка по балансу
    # В реальности это сложно определить, поэтому будем использовать состояние

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_USERS_PROFILE_024', '❌ Пользователь не найден'), show_alert=True
        )
        return

    user = profile['user']
    subscription = profile['subscription']

    texts = get_texts(db_user.language)
    _ui_texts = texts

    status_map = {
        UserStatus.ACTIVE.value: texts.ADMIN_USER_STATUS_ACTIVE,
        UserStatus.BLOCKED.value: texts.ADMIN_USER_STATUS_BLOCKED,
        UserStatus.DELETED.value: texts.ADMIN_USER_STATUS_DELETED,
    }
    status_text = status_map.get(user.status, texts.ADMIN_USER_STATUS_UNKNOWN)

    username_display = f'@{user.username}' if user.username else texts.ADMIN_USER_USERNAME_NOT_SET
    last_activity = (
        format_time_ago(user.last_activity, db_user.language)
        if user.last_activity
        else texts.ADMIN_USER_LAST_ACTIVITY_UNKNOWN
    )

    sections = [
        texts.ADMIN_USER_MANAGEMENT_PROFILE.format(
            name=html.escape(user.full_name),
            telegram_id=user.telegram_id,
            username=username_display,
            status=status_text,
            language=user.language,
            balance=settings.format_price(user.balance_kopeks),
            transactions=profile['transactions_count'],
            registration=format_datetime(user.created_at),
            last_activity=last_activity,
            registration_days=profile['registration_days'],
        )
    ]

    if subscription:
        subscription_type = (
            texts.ADMIN_USER_SUBSCRIPTION_TYPE_TRIAL
            if subscription.is_trial
            else texts.ADMIN_USER_SUBSCRIPTION_TYPE_PAID
        )
        subscription_status = (
            texts.ADMIN_USER_SUBSCRIPTION_STATUS_ACTIVE
            if subscription.is_active
            else texts.ADMIN_USER_SUBSCRIPTION_STATUS_INACTIVE
        )
        traffic_usage = texts.ADMIN_USER_TRAFFIC_USAGE.format(
            used=f'{subscription.traffic_used_gb:.1f}',
            limit=subscription.traffic_limit_gb,
        )
        sections.append(
            texts.ADMIN_USER_MANAGEMENT_SUBSCRIPTION.format(
                type=subscription_type,
                status=subscription_status,
                end_date=format_datetime(subscription.end_date),
                traffic=traffic_usage,
                devices=Texts.format_device_limit(subscription.device_limit),
                countries=len(subscription.connected_squads or []),
            )
        )
    else:
        sections.append(texts.ADMIN_USER_MANAGEMENT_SUBSCRIPTION_NONE)

    # Display promo groups
    primary_group = user.get_primary_promo_group()
    if primary_group:
        sections.append(
            texts.t(
                'ADMIN_USER_PROMO_GROUPS_PRIMARY',
                '⭐ Основная: {name} (Priority: {priority})',
            ).format(name=html.escape(primary_group.name), priority=getattr(primary_group, 'priority', 0))
        )
        sections.append(
            texts.ADMIN_USER_MANAGEMENT_PROMO_GROUP.format(
                name=html.escape(primary_group.name),
                server_discount=primary_group.server_discount_percent,
                traffic_discount=primary_group.traffic_discount_percent,
                device_discount=primary_group.device_discount_percent,
            )
        )

        # Show additional groups if any
        if user.user_promo_groups and len(user.user_promo_groups) > 1:
            additional_groups = [
                upg.promo_group
                for upg in user.user_promo_groups
                if upg.promo_group and upg.promo_group.id != primary_group.id
            ]
            if additional_groups:
                sections.append(
                    texts.t(
                        'ADMIN_USER_PROMO_GROUPS_ADDITIONAL',
                        'Дополнительные группы:',
                    )
                )
                for group in additional_groups:
                    sections.append(
                        texts.t('ADMIN_USER_PROMO_GROUP_PRIORITY_ROW', '  • {name} (Приоритет: {priority})').format(
                            name=html.escape(group.name), priority=getattr(group, 'priority', 0)
                        )
                    )
    else:
        sections.append(texts.ADMIN_USER_MANAGEMENT_PROMO_GROUP_NONE)

    # Показать ограничения пользователя если есть
    restriction_topup = getattr(user, 'restriction_topup', False)
    restriction_subscription = getattr(user, 'restriction_subscription', False)
    if restriction_topup or restriction_subscription:
        restriction_lines = [_ui_texts.t('ADMIN_USERS_PROFILE_023', '⚠️ <b>Ограничения:</b>')]
        if restriction_topup:
            restriction_lines.append(_ui_texts.t('ADMIN_USERS_PROFILE_032', '  • 🚫 Пополнение запрещено'))
        if restriction_subscription:
            restriction_lines.append(_ui_texts.t('ADMIN_USERS_PROFILE_033', '  • 🚫 Продление/покупка запрещена'))
        restriction_reason = getattr(user, 'restriction_reason', None)
        if restriction_reason:
            restriction_lines.append(
                _ui_texts.t('ADMIN_USERS_PROFILE_034', '  📝 Причина: {value1}').format(
                    value1=html.escape(restriction_reason)
                )
            )
        sections.append('\n'.join(restriction_lines))

    text = '\n\n'.join(sections)

    # Проверяем состояние, чтобы определить, откуда пришел пользователь
    current_state = await state.get_state()
    if current_state == AdminStates.viewing_user_from_balance_list:
        back_callback = 'admin_users_balance_filter'
    elif current_state == AdminStates.viewing_user_from_campaign_list:
        back_callback = 'admin_users_campaign_filter'
    elif current_state == AdminStates.viewing_user_from_ready_to_renew_list:
        back_callback = 'admin_users_ready_to_renew_filter'
    elif current_state == AdminStates.viewing_user_from_potential_customers_list:
        back_callback = 'admin_users_potential_customers_filter'

    # Базовая клавиатура профиля
    kb = get_user_management_keyboard(user.id, user.status, db_user.language, back_callback)
    # Если пришли из тикета — добавим в начало кнопку возврата к тикету
    try:
        if origin_ticket_id:
            back_to_ticket_btn = types.InlineKeyboardButton(
                text=_ui_texts.t('ADMIN_USERS_PROFILE_041', '🎫 Вернуться к тикету'),
                callback_data=f'admin_view_ticket_{origin_ticket_id}',
            )
            kb.inline_keyboard.insert(0, [back_to_ticket_btn])
    except Exception:
        pass

    message = callback.message
    if not isinstance(message, types.Message):
        # None или InaccessibleMessage (например, уведомление старше 48ч) — редактировать нельзя
        texts = get_texts(db_user.language)
        await callback.answer(
            texts.t('MESSAGE_TOO_OLD', '⚠️ Сообщение устарело, откройте тикет в панели.'), show_alert=True
        )
        return
    await safe_edit_or_resend(message, text, kb)
    await callback.answer()


async def _build_user_referrals_view(
    db: AsyncSession,
    language: str,
    user_id: int,
    limit: int = 30,
) -> tuple[str, InlineKeyboardMarkup] | None:
    texts = get_texts(language)

    user = await get_user_by_id(db, user_id)
    if not user:
        return None

    referrals = await get_referrals(db, user_id)

    effective_percent = get_effective_referral_commission_percent(user)
    default_percent = settings.REFERRAL_COMMISSION_PERCENT

    header = texts.t(
        'ADMIN_USER_REFERRALS_TITLE',
        '🤝 <b>Рефералы пользователя</b>',
    )
    summary = texts.t(
        'ADMIN_USER_REFERRALS_SUMMARY',
        '👤 {name} (ID: <code>{telegram_id}</code>)\n👥 Всего рефералов: {count}',
    ).format(
        name=html.escape(user.full_name),
        telegram_id=user.telegram_id,
        count=len(referrals),
    )

    lines: list[str] = [header, summary]

    if user.referral_commission_percent is None and settings.is_referral_levels_scheme():
        # В многоуровневой схеме «стандартный процент» не существует: у каждого
        # уровня свой, а пустой означает ноль. Печатать REFERRAL_COMMISSION_PERCENT
        # значит называть админу ставку, по которой ничего не начисляется.
        lines.append(
            texts.t(
                'ADMIN_USER_REFERRAL_COMMISSION_LEVELS',
                '• Процент комиссии: по уровням реферальной схемы',
            )
        )
    elif user.referral_commission_percent is None:
        lines.append(
            texts.t(
                'ADMIN_USER_REFERRAL_COMMISSION_DEFAULT',
                '• Процент комиссии: {percent}% (стандартное значение)',
            ).format(percent=effective_percent)
        )
    else:
        lines.append(
            texts.t(
                'ADMIN_USER_REFERRAL_COMMISSION_CUSTOM',
                '• Индивидуальный процент: {percent}% (стандарт: {default_percent}%)',
            ).format(
                percent=user.referral_commission_percent,
                default_percent=default_percent,
            )
        )

    if referrals:
        lines.append(
            texts.t(
                'ADMIN_USER_REFERRALS_LIST_HEADER',
                '<b>Список рефералов:</b>',
            )
        )
        items = []
        for referral in referrals[:limit]:
            username_part = f', @{referral.username}' if referral.username else ''
            safe_name = html.escape(referral.full_name)
            if referral.telegram_id:
                referral_link = f'<a href="tg://user?id={referral.telegram_id}">{safe_name}</a>'
                referral_id_display = referral.telegram_id
            else:
                referral_link = f'<b>{safe_name}</b>'
                referral_id_display = referral.email or f'#{referral.id}'
            items.append(
                texts.t(
                    'ADMIN_USER_REFERRALS_LIST_ITEM',
                    '• {name} (ID: <code>{telegram_id}</code>{username_part})',
                ).format(
                    name=referral_link,
                    telegram_id=referral_id_display,
                    username_part=username_part,
                )
            )

        lines.append('\n'.join(items))

        if len(referrals) > limit:
            remaining = len(referrals) - limit
            lines.append(
                texts.t(
                    'ADMIN_USER_REFERRALS_LIST_TRUNCATED',
                    '• … и ещё {count} рефералов',
                ).format(count=remaining)
            )
    else:
        lines.append(
            texts.t(
                'ADMIN_USER_REFERRALS_EMPTY',
                'Рефералов пока нет.',
            )
        )

    lines.append(
        texts.t(
            'ADMIN_USER_REFERRALS_EDIT_HINT',
            '✏️ Чтобы изменить список, нажмите «✏️ Редактировать» ниже.',
        )
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_USER_REFERRAL_COMMISSION_EDIT_BUTTON',
                        '📈 Изменить процент',
                    ),
                    callback_data=f'admin_user_referral_percent_{user_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_USER_REFERRALS_EDIT_BUTTON',
                        '✏️ Редактировать',
                    ),
                    callback_data=f'admin_user_referrals_edit_{user_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=f'admin_user_manage_{user_id}',
                )
            ],
        ]
    )

    return '\n\n'.join(lines), keyboard


@admin_required
@error_handler
async def show_user_referrals(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    user_id = int(callback.data.split('_')[-1])

    current_state = await state.get_state()
    if current_state in {AdminStates.editing_user_referrals, AdminStates.editing_user_referral_percent}:
        data = await state.get_data()
        preserved_data = {
            key: value
            for key, value in data.items()
            if key not in {'editing_referrals_user_id', 'referrals_message_id', 'editing_referral_percent_user_id'}
        }
        await state.clear()
        if preserved_data:
            await state.update_data(**preserved_data)

    view = await _build_user_referrals_view(db, db_user.language, user_id)
    if not view:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_USERS_REFERRALS_003', '❌ Пользователь не найден'), show_alert=True
        )
        return

    text, keyboard = view

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
    )
    await callback.answer()


@admin_required
@error_handler
async def start_edit_referral_percent(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    user_id = int(callback.data.split('_')[-1])

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_USERS_REFERRALS_003', '❌ Пользователь не найден'), show_alert=True
        )
        return

    texts = get_texts(db_user.language)

    effective_percent = get_effective_referral_commission_percent(user)
    default_percent = settings.REFERRAL_COMMISSION_PERCENT

    prompt = texts.t(
        'ADMIN_USER_REFERRAL_COMMISSION_PROMPT',
        (
            '📈 <b>Индивидуальный процент реферальной комиссии</b>\n\n'
            'Текущее значение: {current}%\n'
            'Стандартное значение: {default}%\n\n'
            "Отправьте новое значение от 0 до 100 или слово 'стандарт' для сброса."
        ),
    ).format(current=effective_percent, default=default_percent)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='5%',
                    callback_data=f'admin_user_referral_percent_set_{user_id}_5',
                ),
                InlineKeyboardButton(
                    text='10%',
                    callback_data=f'admin_user_referral_percent_set_{user_id}_10',
                ),
            ],
            [
                InlineKeyboardButton(
                    text='15%',
                    callback_data=f'admin_user_referral_percent_set_{user_id}_15',
                ),
                InlineKeyboardButton(
                    text='20%',
                    callback_data=f'admin_user_referral_percent_set_{user_id}_20',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_USER_REFERRAL_COMMISSION_RESET_BUTTON',
                        '♻️ Сбросить на стандартный',
                    ),
                    callback_data=f'admin_user_referral_percent_reset_{user_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=f'admin_user_referrals_{user_id}',
                )
            ],
        ]
    )

    await state.update_data(editing_referral_percent_user_id=user_id)
    await state.set_state(AdminStates.editing_user_referral_percent)

    await callback.message.edit_text(
        prompt,
        reply_markup=keyboard,
    )
    await callback.answer()


async def _update_referral_commission_percent(
    db: AsyncSession,
    user_id: int,
    percent: int | None,
    admin_id: int,
) -> tuple[bool, int | None]:
    try:
        user = await get_user_by_id(db, user_id)
        if not user:
            return False, None

        user.referral_commission_percent = percent
        user.updated_at = datetime.now(UTC)

        await db.commit()

        effective = get_effective_referral_commission_percent(user)

        logger.info(
            'Админ обновил реферальный процент пользователя', admin_id=admin_id, user_id=user_id, percent=percent
        )

        return True, effective
    except Exception as e:
        logger.error('Ошибка обновления реферального процента пользователя', user_id=user_id, e=e)
        try:
            await db.rollback()
        except Exception as rollback_error:
            logger.error('Ошибка отката транзакции', rollback_error=rollback_error)
        return False, None


async def _render_referrals_after_update(
    callback: types.CallbackQuery,
    db: AsyncSession,
    db_user: User,
    user_id: int,
    success_message: str,
):
    view = await _build_user_referrals_view(db, db_user.language, user_id)
    if view:
        text, keyboard = view
        text = f'{success_message}\n\n' + text
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        await callback.message.edit_text(success_message)


@admin_required
@error_handler
async def set_referral_percent_button(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    parts = callback.data.split('_')

    if 'reset' in parts:
        user_id = int(parts[-1])
        percent_value: int | None = None
    else:
        user_id = int(parts[-2])
        percent_value = int(parts[-1])

    texts = get_texts(db_user.language)

    success, effective_percent = await _update_referral_commission_percent(
        db,
        user_id,
        percent_value,
        db_user.id,
    )

    if not success:
        await callback.answer(
            texts.t('ADMIN_USERS_REFERRALS_004', '❌ Не удалось обновить процент'),
            show_alert=True,
        )
        return

    await state.clear()

    success_message = texts.t(
        'ADMIN_USER_REFERRAL_COMMISSION_UPDATED',
        '✅ Процент обновлён: {percent}%',
    ).format(percent=effective_percent)

    await _render_referrals_after_update(callback, db, db_user, user_id, success_message)
    await callback.answer()


@admin_required
@error_handler
async def process_referral_percent_input(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    data = await state.get_data()
    user_id = data.get('editing_referral_percent_user_id')

    if not user_id:
        await message.answer(
            get_texts(db_user.language).t('ADMIN_USERS_REFERRALS_005', '❌ Не удалось определить пользователя')
        )
        return

    raw_text = message.text.strip()
    normalized = raw_text.lower()

    percent_value: int | None

    if normalized in {get_texts(db_user.language).t('ADMIN_USERS_REFERRALS_002', 'стандарт'), 'standard', 'default'}:
        percent_value = None
    else:
        normalized_number = raw_text.replace(',', '.').strip()
        try:
            percent_float = float(normalized_number)
        except (TypeError, ValueError):
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_USER_REFERRAL_COMMISSION_INVALID',
                    "❌ Введите число от 0 до 100 или слово 'стандарт'",
                )
            )
            return

        percent_value = int(round(percent_float))

        if percent_value < 0 or percent_value > 100:
            await message.answer(
                get_texts(db_user.language).t(
                    'ADMIN_USER_REFERRAL_COMMISSION_INVALID',
                    "❌ Введите число от 0 до 100 или слово 'стандарт'",
                )
            )
            return

    texts = get_texts(db_user.language)

    success, effective_percent = await _update_referral_commission_percent(
        db,
        int(user_id),
        percent_value,
        db_user.id,
    )

    if not success:
        await message.answer(texts.t('ADMIN_USERS_REFERRALS_004', '❌ Не удалось обновить процент'))
        return

    await state.clear()

    success_message = texts.t(
        'ADMIN_USER_REFERRAL_COMMISSION_UPDATED',
        '✅ Процент обновлён: {percent}%',
    ).format(percent=effective_percent)

    view = await _build_user_referrals_view(db, db_user.language, int(user_id))
    if view:
        text, keyboard = view
        await message.answer(f'{success_message}\n\n{text}', reply_markup=keyboard)
    else:
        await message.answer(success_message)


@admin_required
@error_handler
async def start_edit_user_referrals(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    user_id = int(callback.data.split('_')[-1])

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_USERS_REFERRALS_003', '❌ Пользователь не найден'), show_alert=True
        )
        return

    texts = get_texts(db_user.language)

    prompt = texts.t(
        'ADMIN_USER_REFERRALS_EDIT_PROMPT',
        (
            '✏️ <b>Редактирование рефералов</b>\n\n'
            'Отправьте список рефералов для пользователя <b>{name}</b> (ID: <code>{telegram_id}</code>):\n'
            '• Используйте TG ID или @username\n'
            '• Значения можно указывать через запятую, пробел или с новой строки\n'
            "• Чтобы очистить список, отправьте 0 или слово 'нет'\n\n"
            'Или нажмите кнопку ниже, чтобы отменить.'
        ),
    ).format(
        name=html.escape(user.full_name),
        telegram_id=user.telegram_id,
    )

    await state.update_data(
        editing_referrals_user_id=user_id,
        referrals_message_id=callback.message.message_id,
    )

    await callback.message.edit_text(
        prompt,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.BACK,
                        callback_data=f'admin_user_referrals_{user_id}',
                    )
                ]
            ]
        ),
    )

    await state.set_state(AdminStates.editing_user_referrals)
    await callback.answer()


@admin_required
@error_handler
async def process_edit_user_referrals(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()

    user_id = data.get('editing_referrals_user_id')
    if not user_id:
        await message.answer(
            texts.t(
                'ADMIN_USER_REFERRALS_STATE_LOST',
                '❌ Не удалось определить пользователя. Попробуйте начать сначала.',
            )
        )
        await state.clear()
        return

    raw_text = message.text.strip()
    lower_text = raw_text.lower()
    clear_keywords = {
        '0',
        texts.t('ADMIN_USERS_REFERRALS_000', 'нет'),
        'none',
        texts.t('ADMIN_USERS_REFERRALS_001', 'пусто'),
        'clear',
    }
    clear_requested = lower_text in clear_keywords

    tokens: list[str] = []
    if not clear_requested:
        parts = re.split(r'[,\n]+', raw_text)
        for part in parts:
            for token in part.split():
                cleaned = token.strip()
                if cleaned and cleaned not in tokens:
                    tokens.append(cleaned)

    found_users: list[User] = []
    not_found: list[str] = []
    skipped_self: list[str] = []
    duplicate_tokens: list[str] = []

    seen_ids = set()

    for token in tokens:
        normalized = token.strip()
        if not normalized:
            continue

        user = None
        if normalized.startswith('#') and normalized[1:].isdigit():
            # Internal user ID lookup: #2351
            user = await get_user_by_id(db, int(normalized[1:]))
        elif '@' in normalized and '.' in normalized:
            # Email lookup: user@example.com
            user = await get_user_by_email(db, normalized)
        elif normalized.startswith('@'):
            # Username lookup: @username
            user = await get_user_by_username(db, normalized.lstrip('@'))
        elif normalized.isdigit():
            # Telegram ID lookup
            user = await get_user_by_telegram_id(db, int(normalized))
        else:
            # Fallback: try as username without @
            user = await get_user_by_username(db, normalized)

        if not user:
            not_found.append(token)
            continue

        if user.id == user_id:
            skipped_self.append(token)
            continue

        if user.id in seen_ids:
            duplicate_tokens.append(token)
            continue

        seen_ids.add(user.id)
        found_users.append(user)

    if not found_users and not clear_requested:
        error_lines = [
            texts.t(
                'ADMIN_USER_REFERRALS_NO_VALID',
                '❌ Не удалось найти ни одного пользователя по введённым данным.',
            )
        ]
        if not_found:
            error_lines.append(
                texts.t(
                    'ADMIN_USER_REFERRALS_INVALID_ENTRIES',
                    'Не найдены: {values}',
                ).format(values=', '.join(not_found))
            )
        if skipped_self:
            error_lines.append(
                texts.t(
                    'ADMIN_USER_REFERRALS_SELF_SKIPPED',
                    'Пропущены значения пользователя: {values}',
                ).format(values=', '.join(skipped_self))
            )
        await message.answer('\n'.join(error_lines))
        return

    user_service = UserService()

    new_referral_ids = [user.id for user in found_users] if not clear_requested else []

    success, details = await user_service.update_user_referrals(
        db,
        user_id,
        new_referral_ids,
        db_user.id,
    )

    if not success:
        await message.answer(
            texts.t(
                'ADMIN_USER_REFERRALS_UPDATE_ERROR',
                '❌ Не удалось обновить рефералов. Попробуйте позже.',
            )
        )
        return

    response_lines = [
        texts.t(
            'ADMIN_USER_REFERRALS_UPDATED',
            '✅ Список рефералов обновлён.',
        )
    ]

    total_referrals = details.get('total', len(new_referral_ids))
    added = details.get('added', 0)
    removed = details.get('removed', 0)

    response_lines.append(
        texts.t(
            'ADMIN_USER_REFERRALS_UPDATED_TOTAL',
            '• Текущий список: {total}',
        ).format(total=total_referrals)
    )

    if added > 0:
        response_lines.append(
            texts.t(
                'ADMIN_USER_REFERRALS_UPDATED_ADDED',
                '• Добавлено: {count}',
            ).format(count=added)
        )

    if removed > 0:
        response_lines.append(
            texts.t(
                'ADMIN_USER_REFERRALS_UPDATED_REMOVED',
                '• Удалено: {count}',
            ).format(count=removed)
        )

    if not_found:
        response_lines.append(
            texts.t(
                'ADMIN_USER_REFERRALS_INVALID_ENTRIES',
                'Не найдены: {values}',
            ).format(values=', '.join(not_found))
        )

    if skipped_self:
        response_lines.append(
            texts.t(
                'ADMIN_USER_REFERRALS_SELF_SKIPPED',
                'Пропущены значения пользователя: {values}',
            ).format(values=', '.join(skipped_self))
        )

    if duplicate_tokens:
        response_lines.append(
            texts.t(
                'ADMIN_USER_REFERRALS_DUPLICATES',
                'Игнорированы дубли: {values}',
            ).format(values=', '.join(duplicate_tokens))
        )

    view = await _build_user_referrals_view(db, db_user.language, user_id)
    message_id = data.get('referrals_message_id')

    if view and message_id:
        try:
            await message.bot.edit_message_text(
                view[0],
                chat_id=message.chat.id,
                message_id=message_id,
                reply_markup=view[1],
            )
        except TelegramBadRequest:
            await message.answer(view[0], reply_markup=view[1])
    elif view:
        await message.answer(view[0], reply_markup=view[1])

    await message.answer('\n'.join(response_lines))
    await state.clear()


async def _render_user_promo_group(message: types.Message, language: str, user: User, promo_groups: list) -> None:
    texts = get_texts(language)

    # Get primary and all user groups
    primary_group = user.get_primary_promo_group()
    user_group_ids = [upg.promo_group_id for upg in user.user_promo_groups] if user.user_promo_groups else []

    # Build current groups section
    if primary_group:
        current_line = texts.t(
            'ADMIN_USER_PROMO_GROUPS_PRIMARY',
            '⭐ Основная: {name} (Priority: {priority})',
        ).format(name=html.escape(primary_group.name), priority=getattr(primary_group, 'priority', 0))

        discount_line = texts.ADMIN_USER_PROMO_GROUP_DISCOUNTS.format(
            servers=primary_group.server_discount_percent,
            traffic=primary_group.traffic_discount_percent,
            devices=primary_group.device_discount_percent,
        )

        # Show additional groups if any
        if len(user_group_ids) > 1:
            additional_groups = [
                upg.promo_group
                for upg in user.user_promo_groups
                if upg.promo_group and upg.promo_group.id != primary_group.id
            ]
            if additional_groups:
                additional_line = (
                    '\n'
                    + texts.t(
                        'ADMIN_USER_PROMO_GROUPS_ADDITIONAL',
                        'Дополнительные группы:',
                    )
                    + '\n'
                )
                for group in additional_groups:
                    additional_line += (
                        texts.t('ADMIN_USER_PROMO_GROUP_PRIORITY_ROW', '  • {name} (Приоритет: {priority})').format(
                            name=html.escape(group.name), priority=getattr(group, 'priority', 0)
                        )
                        + '\n'
                    )
                discount_line += additional_line
    else:
        current_line = texts.t(
            'ADMIN_USER_PROMO_GROUPS_NONE',
            'У пользователя нет промогрупп',
        )
        discount_line = ''

    text = (
        f'{texts.ADMIN_USER_PROMO_GROUP_TITLE}\n\n'
        f'{current_line}\n'
        f'{discount_line}\n\n'
        f'{texts.ADMIN_USER_PROMO_GROUP_SELECT}'
    )

    await message.edit_text(
        text,
        reply_markup=get_user_promo_group_keyboard(
            promo_groups,
            user.id,
            user_group_ids,  # Pass list of all group IDs
            language,
        ),
    )


@admin_required
@error_handler
async def show_user_promo_group(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_USERS_REFERRALS_003', '❌ Пользователь не найден'), show_alert=True
        )
        return

    promo_groups = await get_promo_groups_with_counts(db)
    if not promo_groups:
        texts = get_texts(db_user.language)
        await callback.answer(texts.ADMIN_PROMO_GROUPS_EMPTY, show_alert=True)
        return

    await _render_user_promo_group(callback.message, db_user.language, user, promo_groups)
    await callback.answer()


@admin_required
@error_handler
async def set_user_promo_group(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.database.crud.promo_group import get_promo_group_by_id
    from app.database.crud.user_promo_group import (
        add_user_to_promo_group,
        count_user_promo_groups,
        has_user_promo_group,
        remove_user_from_promo_group,
    )

    parts = callback.data.split('_')
    user_id = int(parts[-2])
    group_id = int(parts[-1])

    texts = get_texts(db_user.language)

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(texts.t('ADMIN_USERS_REFERRALS_003', '❌ Пользователь не найден'), show_alert=True)
        return

    # Check if user already has this group
    has_group = await has_user_promo_group(db, user_id, group_id)

    if has_group:
        # Remove group
        # Check if it's the last group
        groups_count = await count_user_promo_groups(db, user_id)
        if groups_count <= 1:
            await callback.answer(
                texts.t(
                    'ADMIN_USER_PROMO_GROUP_CANNOT_REMOVE_LAST',
                    '❌ Нельзя удалить последнюю промогруппу',
                ),
                show_alert=True,
            )
            return

        group = await get_promo_group_by_id(db, group_id)
        await remove_user_from_promo_group(db, user_id, group_id)
        await callback.answer(
            texts.t(
                'ADMIN_USER_PROMO_GROUP_REMOVED',
                '🗑 Группа «{name}» удалена',
            ).format(name=group.name if group else ''),
            show_alert=True,
        )
    else:
        # Add group
        group = await get_promo_group_by_id(db, group_id)
        if not group:
            await callback.answer(texts.ADMIN_USER_PROMO_GROUP_ERROR, show_alert=True)
            return

        await add_user_to_promo_group(db, user_id, group_id, assigned_by='admin')
        await callback.answer(
            texts.t(
                'ADMIN_USER_PROMO_GROUP_ADDED',
                '✅ Группа «{name}» добавлена',
            ).format(name=group.name),
            show_alert=True,
        )

    # Refresh user data and show updated list
    user = await get_user_by_id(db, user_id)
    promo_groups = await get_promo_groups_with_counts(db)
    await _render_user_promo_group(callback.message, db_user.language, user, promo_groups)


@admin_required
@error_handler
async def start_balance_edit(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])

    await state.update_data(editing_user_id=user_id)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_USERS_RESTRICTIONS_016',
            '💰 <b>Изменение баланса</b>\n\nВведите сумму для изменения баланса:\n• Положительное число для пополнения\n• Отрицательное число для списания\n• Примеры: 100, -50, 25.5\n\nИли нажмите /cancel для отмены',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_RESTRICTIONS_041', '❌ Отмена'),
                        callback_data=f'admin_user_manage_{user_id}',
                    )
                ]
            ]
        ),
    )

    await state.set_state(AdminStates.editing_user_balance)
    await callback.answer()


@admin_required
@error_handler
async def start_send_user_message(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    user_id = int(callback.data.split('_')[-1])

    target_user = await get_user_by_id(db, user_id)
    if not target_user:
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_USERS_RESTRICTIONS_023', '❌ Пользователь не найден'), show_alert=True
        )
        return

    await state.update_data(direct_message_user_id=user_id)

    texts = get_texts(db_user.language)
    prompt = texts.t(
        'ADMIN_USER_SEND_MESSAGE_PROMPT',
        '✉️ <b>Отправка сообщения пользователю</b>\n\n'
        'Введите текст, который бот отправит пользователю.'
        '\n\nВы можете отменить действие командой /cancel или кнопкой ниже.',
    )

    await callback.message.edit_text(
        prompt,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_RESTRICTIONS_041', '❌ Отмена'),
                        callback_data=f'admin_user_manage_{user_id}',
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )

    await state.set_state(AdminStates.sending_user_message)
    await callback.answer()


@admin_required
@error_handler
async def process_send_user_message(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    user_id = data.get('direct_message_user_id')

    if not user_id:
        await message.answer(
            texts.t('ADMIN_USER_SEND_MESSAGE_ERROR_NOT_FOUND', '❌ Пользователь для отправки сообщения не найден')
        )
        await state.clear()
        return

    target_user = await get_user_by_id(db, int(user_id))
    if not target_user:
        await message.answer(
            texts.t('ADMIN_USER_SEND_MESSAGE_ERROR_NOT_FOUND', '❌ Пользователь не найден или был удалён')
        )
        await state.clear()
        return

    text = (message.text or '').strip()
    if not text:
        await message.answer(texts.t('ADMIN_USER_SEND_MESSAGE_EMPTY', '❌ Пожалуйста, введите непустое сообщение'))
        return

    confirmation_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_RESTRICTIONS_040', '👤 К пользователю'),
                    callback_data=f'admin_user_manage_{user_id}',
                )
            ]
        ]
    )

    # Check if user has telegram_id (email-only users cannot receive Telegram messages)
    if not target_user.telegram_id:
        await message.answer(
            texts.t(
                'ADMIN_USER_NO_TELEGRAM_ID',
                '❌ Этот пользователь зарегистрирован только по email и не может получать сообщения в Telegram.',
            ),
            reply_markup=confirmation_keyboard,
        )
        await state.clear()
        return

    try:
        await message.bot.send_message(target_user.telegram_id, text, parse_mode='HTML')
        await message.answer(
            texts.t('ADMIN_USER_SEND_MESSAGE_SUCCESS', '✅ Сообщение отправлено пользователю'),
            reply_markup=confirmation_keyboard,
        )
    except TelegramForbiddenError:
        await message.answer(
            texts.t(
                'ADMIN_USER_SEND_MESSAGE_FORBIDDEN', '⚠️ Пользователь заблокировал бота или не может получить сообщения.'
            ),
            reply_markup=confirmation_keyboard,
        )
    except TelegramBadRequest as err:
        logger.error('Ошибка отправки сообщения пользователю', telegram_id=target_user.telegram_id, err=err)
        await message.answer(
            texts.t(
                'ADMIN_USER_SEND_MESSAGE_BAD_REQUEST',
                '❌ Telegram отклонил сообщение. Проверьте текст и попробуйте ещё раз.',
            ),
            reply_markup=confirmation_keyboard,
        )
        await state.clear()
        return
    except Exception as err:
        logger.error('Неожиданная ошибка отправки сообщения пользователю', telegram_id=target_user.telegram_id, err=err)
        await message.answer(
            texts.t('ADMIN_USER_SEND_MESSAGE_ERROR', '❌ Не удалось отправить сообщение. Попробуйте позже.'),
            reply_markup=confirmation_keyboard,
        )
        await state.clear()
        return

    await state.clear()


@admin_required
@error_handler
async def process_balance_edit(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    user_id = data.get('editing_user_id')

    if not user_id:
        await message.answer(texts.t('ADMIN_USERS_RESTRICTIONS_024', '❌ Ошибка: пользователь не найден'))
        await state.clear()
        return

    try:
        amount_rubles = float(message.text.replace(',', '.'))
        amount_kopeks = int(amount_rubles * 100)

        if abs(amount_kopeks) > 10000000:
            await message.answer(
                texts.t('ADMIN_USERS_RESTRICTIONS_034', '❌ Слишком большая сумма (максимум 100,000 ₽)')
            )
            return

        user_service = UserService()

        description = f'Изменение баланса администратором {db_user.full_name}'
        if amount_kopeks > 0:
            description = f'Пополнение администратором: +{int(amount_rubles)} ₽'
        else:
            description = f'Списание администратором: {int(amount_rubles)} ₽'

        success = await user_service.update_user_balance(
            db, user_id, amount_kopeks, description, db_user.id, bot=message.bot, admin_name=db_user.full_name
        )

        if success:
            action = (
                texts.t('ADMIN_USERS_RESTRICTIONS_025', 'пополнен')
                if amount_kopeks > 0
                else texts.t('ADMIN_USERS_RESTRICTIONS_026', 'списан')
            )
            await message.answer(
                texts.t('ADMIN_USERS_RESTRICTIONS_035', '✅ Баланс пользователя {value1} на {value2}').format(
                    value1=action, value2=settings.format_price(abs(amount_kopeks))
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USERS_RESTRICTIONS_040', '👤 К пользователю'),
                                callback_data=f'admin_user_manage_{user_id}',
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(
                texts.t(
                    'ADMIN_USERS_RESTRICTIONS_036',
                    '❌ Ошибка изменения баланса (возможно, недостаточно средств для списания)',
                )
            )

    except ValueError:
        await message.answer(
            texts.t('ADMIN_USERS_RESTRICTIONS_037', '❌ Введите корректную сумму (например: 100 или -50)')
        )
        return

    await state.clear()


@admin_required
@error_handler
async def confirm_user_block(callback: types.CallbackQuery, db_user: User):
    user_id = int(callback.data.split('_')[-1])

    await callback.message.edit_text(
        get_texts(db_user.language).t(
            'ADMIN_USERS_RESTRICTIONS_017',
            '🚫 <b>Блокировка пользователя</b>\n\nВы уверены, что хотите заблокировать этого пользователя?\nПользователь потеряет доступ к боту.',
        ),
        reply_markup=get_confirmation_keyboard(
            f'admin_user_block_confirm_{user_id}', f'admin_user_manage_{user_id}', db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def block_user(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])

    user_service = UserService()
    success = await user_service.block_user(db, user_id, db_user.id, 'Заблокирован администратором')

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_RESTRICTIONS_027', '✅ Пользователь заблокирован'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_RESTRICTIONS_040', '👤 К пользователю'),
                            callback_data=f'admin_user_manage_{user_id}',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_RESTRICTIONS_028', '❌ Ошибка блокировки пользователя'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_RESTRICTIONS_040', '👤 К пользователю'),
                            callback_data=f'admin_user_manage_{user_id}',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


# ============ УПРАВЛЕНИЕ ОГРАНИЧЕНИЯМИ ПОЛЬЗОВАТЕЛЯ ============


@admin_required
@error_handler
async def show_user_restrictions(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показать меню управления ограничениями пользователя."""
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])
    user = await get_user_by_id(db, user_id)

    if not user:
        await callback.answer(texts.t('ADMIN_USERS_RESTRICTIONS_029', 'Пользователь не найден'), show_alert=True)
        return

    # Формируем текст с информацией об ограничениях
    restriction_topup = getattr(user, 'restriction_topup', False)
    restriction_subscription = getattr(user, 'restriction_subscription', False)
    restriction_reason = getattr(user, 'restriction_reason', None)

    text_lines = [
        texts.t('ADMIN_USERS_RESTRICTIONS_004', '⚠️ <b>Ограничения пользователя</b>'),
        f'👤 {html.escape(user.full_name)}',
        '',
        texts.t('ADMIN_USERS_RESTRICTIONS_005', '✅ — разрешено, 🚫 — запрещено'),
        '',
        texts.t('ADMIN_USERS_RESTRICTIONS_006', '{value1} Пополнение баланса').format(
            value1='🚫' if restriction_topup else '✅'
        ),
        texts.t('ADMIN_USERS_RESTRICTIONS_007', '{value1} Продление/покупка подписки').format(
            value1='🚫' if restriction_subscription else '✅'
        ),
    ]

    if restriction_reason:
        text_lines.append('')
        text_lines.append(
            texts.t('ADMIN_USERS_RESTRICTIONS_012', '📝 <b>Причина:</b> {value1}').format(
                value1=html.escape(restriction_reason)
            )
        )

    keyboard = get_user_restrictions_keyboard(
        user_id=user_id,
        restriction_topup=restriction_topup,
        restriction_subscription=restriction_subscription,
        language=db_user.language,
    )

    await callback.message.edit_text('\n'.join(text_lines), reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def toggle_user_restriction_topup(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Переключить ограничение на пополнение баланса."""
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])
    user = await get_user_by_id(db, user_id)

    if not user:
        await callback.answer(texts.t('ADMIN_USERS_RESTRICTIONS_029', 'Пользователь не найден'), show_alert=True)
        return

    # Переключаем ограничение
    current_value = getattr(user, 'restriction_topup', False)
    user.restriction_topup = not current_value
    await db.commit()

    action = (
        texts.t('ADMIN_USERS_RESTRICTIONS_008', 'установлено')
        if user.restriction_topup
        else texts.t('ADMIN_USERS_RESTRICTIONS_009', 'снято')
    )
    await callback.answer(
        texts.t('ADMIN_USERS_RESTRICTIONS_018', 'Ограничение на пополнение {value1}').format(value1=action),
        show_alert=False,
    )

    # Обновляем меню
    await show_user_restrictions(callback, db_user, db)


@admin_required
@error_handler
async def toggle_user_restriction_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Переключить ограничение на продление/покупку подписки."""
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])
    user = await get_user_by_id(db, user_id)

    if not user:
        await callback.answer(texts.t('ADMIN_USERS_RESTRICTIONS_029', 'Пользователь не найден'), show_alert=True)
        return

    # Переключаем ограничение
    current_value = getattr(user, 'restriction_subscription', False)
    user.restriction_subscription = not current_value
    await db.commit()

    action = (
        texts.t('ADMIN_USERS_RESTRICTIONS_008', 'установлено')
        if user.restriction_subscription
        else texts.t('ADMIN_USERS_RESTRICTIONS_009', 'снято')
    )
    await callback.answer(
        texts.t('ADMIN_USERS_RESTRICTIONS_019', 'Ограничение на подписку {value1}').format(value1=action),
        show_alert=False,
    )

    # Обновляем меню
    await show_user_restrictions(callback, db_user, db)


@admin_required
@error_handler
async def ask_restriction_reason(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Запросить ввод причины ограничения."""
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])
    user = await get_user_by_id(db, user_id)

    if not user:
        await callback.answer(texts.t('ADMIN_USERS_RESTRICTIONS_029', 'Пользователь не найден'), show_alert=True)
        return

    current_reason = getattr(user, 'restriction_reason', None) or ''

    await state.set_state(AdminStates.editing_user_restriction_reason)
    await state.update_data(restriction_user_id=user_id)

    text = texts.t(
        'ADMIN_USERS_RESTRICTIONS_000',
        '📝 <b>Введите причину ограничения</b>\n\nЭта причина будет показана пользователю при попытке выполнить запрещённое действие.\n\n',
    )
    if current_reason:
        text += texts.t('ADMIN_USERS_RESTRICTIONS_010', 'Текущая причина: <i>{value1}</i>\n\n').format(
            value1=html.escape(current_reason)
        )
    text += texts.t('ADMIN_USERS_RESTRICTIONS_001', 'Отправьте новую причину или /cancel для отмены:')

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_RESTRICTIONS_041', '❌ Отмена'),
                        callback_data=f'admin_user_restrictions_{user_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def save_restriction_reason(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Сохранить причину ограничения."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    user_id = data.get('restriction_user_id')

    if not user_id:
        await message.answer(texts.t('ADMIN_USERS_RESTRICTIONS_030', 'Ошибка: пользователь не найден'))
        await state.clear()
        return

    user = await get_user_by_id(db, user_id)
    if not user:
        await message.answer(texts.t('ADMIN_USERS_RESTRICTIONS_030', 'Ошибка: пользователь не найден'))
        await state.clear()
        return

    reason = message.text.strip()[:500]  # Ограничение 500 символов
    user.restriction_reason = reason
    await db.commit()

    await state.clear()

    # Формируем текст с обновлённой информацией
    restriction_topup = getattr(user, 'restriction_topup', False)
    restriction_subscription = getattr(user, 'restriction_subscription', False)

    text_lines = [
        texts.t('ADMIN_USERS_RESTRICTIONS_011', '✅ <b>Причина ограничения сохранена</b>'),
        '',
        texts.t('ADMIN_USERS_RESTRICTIONS_004', '⚠️ <b>Ограничения пользователя</b>'),
        f'👤 {html.escape(user.full_name)}',
        '',
        texts.t('ADMIN_USERS_RESTRICTIONS_006', '{value1} Пополнение баланса').format(
            value1='🚫' if restriction_topup else '✅'
        ),
        texts.t('ADMIN_USERS_RESTRICTIONS_007', '{value1} Продление/покупка подписки').format(
            value1='🚫' if restriction_subscription else '✅'
        ),
        '',
        texts.t('ADMIN_USERS_RESTRICTIONS_012', '📝 <b>Причина:</b> {value1}').format(value1=html.escape(reason)),
    ]

    keyboard = get_user_restrictions_keyboard(
        user_id=user_id,
        restriction_topup=restriction_topup,
        restriction_subscription=restriction_subscription,
        language=db_user.language,
    )

    await message.answer('\n'.join(text_lines), reply_markup=keyboard)


@admin_required
@error_handler
async def clear_user_restrictions(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Снять все ограничения с пользователя."""
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])
    user = await get_user_by_id(db, user_id)

    if not user:
        await callback.answer(texts.t('ADMIN_USERS_RESTRICTIONS_029', 'Пользователь не найден'), show_alert=True)
        return

    # Снимаем все ограничения
    user.restriction_topup = False
    user.restriction_subscription = False
    user.restriction_reason = None
    await db.commit()

    await callback.answer(texts.t('ADMIN_USERS_RESTRICTIONS_020', 'Все ограничения сняты'), show_alert=True)

    # Обновляем меню
    await show_user_restrictions(callback, db_user, db)


@admin_required
@error_handler
async def show_inactive_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    UserService()

    from app.database.crud.user import get_inactive_users

    inactive_users = await get_inactive_users(db, settings.INACTIVE_USER_DELETE_MONTHS)

    if not inactive_users:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_USERS_RESTRICTIONS_031', '✅ Неактивных пользователей (более {value1} месяцев) не найдено'
            ).format(value1=settings.INACTIVE_USER_DELETE_MONTHS),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_RESTRICTIONS_039', '⬅️ Назад'),
                            callback_data='admin_users',
                        )
                    ]
                ]
            ),
        )
        await callback.answer()
        return

    with_active_sub = sum(
        1 for u in inactive_users if any(s.is_active for s in (getattr(u, 'subscriptions', None) or []))
    )
    will_delete = len(inactive_users) - with_active_sub

    text = texts.t('ADMIN_USERS_RESTRICTIONS_002', '🗑️ <b>Неактивные пользователи</b>\n')
    text += texts.t('ADMIN_USERS_RESTRICTIONS_003', 'Без активности более {value1} месяцев: {value2}\n').format(
        value1=settings.INACTIVE_USER_DELETE_MONTHS, value2=len(inactive_users)
    )
    if with_active_sub > 0:
        text += texts.t('ADMIN_USERS_RESTRICTIONS_013', '🛡️ С активной подпиской (не будут удалены): {value1}\n').format(
            value1=with_active_sub
        )
        text += texts.t('ADMIN_USERS_RESTRICTIONS_014', '🗑️ Будет удалено: {value1}\n').format(value1=will_delete)
    text += '\n'

    for user in inactive_users[:10]:
        user_link = user_html_link(user)
        user_id_display = user.telegram_id or user.email or f'#{user.id}'
        has_active = any(s.is_active for s in (getattr(user, 'subscriptions', None) or []))
        sub_badge = ' 🛡️' if has_active else ''
        text += f'👤 {user_link}{sub_badge}\n'
        text += f'🆔 <code>{user_id_display}</code>\n'
        last_activity_display = (
            format_time_ago(user.last_activity, db_user.language)
            if user.last_activity
            else texts.t('ADMIN_USERS_RESTRICTIONS_021', 'Никогда')
        )
        text += f'📅 {last_activity_display}\n\n'

    if len(inactive_users) > 10:
        text += texts.t('ADMIN_USERS_RESTRICTIONS_015', '... и еще {value1} пользователей').format(
            value1=len(inactive_users) - 10
        )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_RESTRICTIONS_038', '🗑️ Очистить всех'),
                callback_data='admin_cleanup_inactive',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_RESTRICTIONS_039', '⬅️ Назад'),
                callback_data='admin_users',
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def confirm_user_unblock(callback: types.CallbackQuery, db_user: User):
    user_id = int(callback.data.split('_')[-1])

    await callback.message.edit_text(
        get_texts(db_user.language).t(
            'ADMIN_USERS_RESTRICTIONS_022',
            '✅ <b>Разблокировка пользователя</b>\n\nВы уверены, что хотите разблокировать этого пользователя?\nПользователь снова получит доступ к боту.',
        ),
        reply_markup=get_confirmation_keyboard(
            f'admin_user_unblock_confirm_{user_id}', f'admin_user_manage_{user_id}', db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def unblock_user(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])

    user_service = UserService()
    success = await user_service.unblock_user(db, user_id, db_user.id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_RESTRICTIONS_032', '✅ Пользователь разблокирован'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_RESTRICTIONS_040', '👤 К пользователю'),
                            callback_data=f'admin_user_manage_{user_id}',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_RESTRICTIONS_033', '❌ Ошибка разблокировки пользователя'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_RESTRICTIONS_040', '👤 К пользователю'),
                            callback_data=f'admin_user_manage_{user_id}',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def show_user_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    user = profile['user']
    subscription = profile['subscription']

    referral_stats = await get_detailed_referral_stats(db, user.id)
    campaign_registration = await get_campaign_registration_by_user(db, user.id)
    campaign_stats = None
    if campaign_registration:
        campaign_stats = await get_campaign_statistics(db, campaign_registration.campaign_id)

    text = texts.t('ADMIN_USERS_STATS_000', '📊 <b>Статистика пользователя</b>\n\n')
    user_link = user_html_link(user)
    user_id_display = user.telegram_id or user.email or f'#{user.id}'
    text += f'👤 {user_link} (ID: <code>{user_id_display}</code>)\n\n'

    text += texts.t('ADMIN_USERS_STATS_001', '<b>Основная информация:</b>\n')
    text += texts.t('ADMIN_USERS_STATS_002', '• Дней с регистрации: {value1}\n').format(
        value1=profile['registration_days']
    )
    text += texts.t('ADMIN_USERS_STATS_003', '• Баланс: {value1}\n').format(
        value1=settings.format_price(user.balance_kopeks)
    )
    text += texts.t('ADMIN_USERS_STATS_004', '• Транзакций: {value1}\n').format(value1=profile['transactions_count'])
    text += texts.t('ADMIN_USERS_STATS_005', '• Язык: {value1}\n\n').format(value1=user.language)

    text += texts.t('ADMIN_USERS_STATS_006', '<b>Подписка:</b>\n')
    if subscription:
        sub_status = (
            texts.t('ADMIN_USERS_STATS_032', '✅ Активна')
            if subscription.is_active
            else texts.t('ADMIN_USERS_STATS_033', '❌ Неактивна')
        )
        sub_type = (
            texts.t('ADMIN_USERS_STATS_034', ' (пробная)')
            if subscription.is_trial
            else texts.t('ADMIN_USERS_STATS_035', ' (платная)')
        )
        text += texts.t('ADMIN_USERS_STATS_009', '• Статус: {value1}{value2}\n').format(
            value1=sub_status, value2=sub_type
        )
        text += texts.t('ADMIN_USERS_STATS_010', '• Трафик: {value1:.1f}/{value2} ГБ\n').format(
            value1=subscription.traffic_used_gb, value2=subscription.traffic_limit_gb
        )
        text += texts.t('ADMIN_USERS_STATS_011', '• Устройства: {value1}\n').format(
            value1=Texts.format_device_limit(subscription.device_limit)
        )
        text += texts.t('ADMIN_USERS_STATS_012', '• Стран: {value1}\n').format(
            value1=len(subscription.connected_squads or [])
        )
    else:
        text += texts.t('ADMIN_USERS_STATS_013', '• Отсутствует\n')

    text += texts.t('ADMIN_USERS_STATS_007', '\n<b>Реферальная программа:</b>\n')

    if user.referred_by_id:
        referrer = await get_user_by_id(db, user.referred_by_id)
        if referrer:
            text += texts.t('ADMIN_USERS_STATS_036', '• Пришел по реферальной ссылке от <b>{value1}</b>\n').format(
                value1=html.escape(referrer.full_name)
            )
        else:
            text += texts.t('ADMIN_USERS_STATS_037', '• Пришел по реферальной ссылке (реферер не найден)\n')
        if campaign_registration and campaign_registration.campaign:
            text += texts.t(
                'ADMIN_USERS_STATS_038', '• Дополнительно зарегистрирован через кампанию <b>{value1}</b>\n'
            ).format(value1=html.escape(campaign_registration.campaign.name))
    elif campaign_registration and campaign_registration.campaign:
        text += texts.t('ADMIN_USERS_STATS_039', '• Регистрация через рекламную кампанию <b>{value1}</b>\n').format(
            value1=html.escape(campaign_registration.campaign.name)
        )
        if campaign_registration.created_at:
            text += texts.t('ADMIN_USERS_STATS_044', '• Дата регистрации по кампании: {value1}\n').format(
                value1=format_local_datetime(campaign_registration.created_at, '%d.%m.%Y %H:%M')
            )
    else:
        text += texts.t('ADMIN_USERS_STATS_040', '• Прямая регистрация\n')

    text += texts.t('ADMIN_USERS_STATS_008', '• Реферальный код: <code>{value1}</code>\n\n').format(
        value1=user.referral_code
    )

    if campaign_registration and campaign_registration.campaign and campaign_stats:
        text += texts.t('ADMIN_USERS_STATS_014', '<b>Рекламная кампания:</b>\n')
        text += texts.t('ADMIN_USERS_STATS_015', '• Название: <b>{value1}</b>').format(
            value1=html.escape(campaign_registration.campaign.name)
        )
        if campaign_registration.campaign.start_parameter:
            text += texts.t('ADMIN_USERS_STATS_041', ' (параметр: <code>{value1}</code>)').format(
                value1=campaign_registration.campaign.start_parameter
            )
        text += '\n'
        text += texts.t('ADMIN_USERS_STATS_016', '• Всего регистраций: {value1}\n').format(
            value1=campaign_stats['registrations']
        )
        text += texts.t('ADMIN_USERS_STATS_017', '• Суммарный доход: {value1}\n').format(
            value1=settings.format_price(campaign_stats['total_revenue_kopeks'])
        )
        text += texts.t('ADMIN_USERS_STATS_018', '• Получили триал: {value1} (активно: {value2})\n').format(
            value1=campaign_stats['trial_users_count'], value2=campaign_stats['active_trials_count']
        )
        text += texts.t(
            'ADMIN_USERS_STATS_019', '• Конверсий в оплату: {value1} (оплативших пользователей: {value2})\n'
        ).format(value1=campaign_stats['conversion_count'], value2=campaign_stats['paid_users_count'])
        text += texts.t('ADMIN_USERS_STATS_020', '• Конверсия в оплату: {value1:.1f}%\n').format(
            value1=campaign_stats['conversion_rate']
        )
        text += texts.t('ADMIN_USERS_STATS_021', '• Конверсия триала: {value1:.1f}%\n').format(
            value1=campaign_stats['trial_conversion_rate']
        )
        text += texts.t('ADMIN_USERS_STATS_022', '• Средний доход на пользователя: {value1}\n').format(
            value1=settings.format_price(campaign_stats['avg_revenue_per_user_kopeks'])
        )
        text += texts.t('ADMIN_USERS_STATS_023', '• Средний первый платеж: {value1}\n').format(
            value1=settings.format_price(campaign_stats['avg_first_payment_kopeks'])
        )
        text += '\n'

    if referral_stats['invited_count'] > 0:
        text += texts.t('ADMIN_USERS_STATS_024', '<b>Доходы от рефералов:</b>\n')
        text += texts.t('ADMIN_USERS_STATS_025', '• Всего приглашено: {value1}\n').format(
            value1=referral_stats['invited_count']
        )
        text += texts.t('ADMIN_USERS_STATS_026', '• Активных рефералов: {value1}\n').format(
            value1=referral_stats['active_referrals']
        )
        text += texts.t('ADMIN_USERS_STATS_027', '• Общий доход: {value1}\n').format(
            value1=settings.format_price(referral_stats['total_earned_kopeks'])
        )
        text += texts.t('ADMIN_USERS_STATS_028', '• Доход за месяц: {value1}\n').format(
            value1=settings.format_price(referral_stats['month_earned_kopeks'])
        )

        if referral_stats['referrals_detail']:
            text += texts.t('ADMIN_USERS_STATS_042', '\n<b>Детали по рефералам:</b>\n')
            for detail in referral_stats['referrals_detail'][:5]:
                referral_name = html.escape(detail['referral_name'])
                earned = settings.format_price(detail['total_earned_kopeks'])
                status = '🟢' if detail['is_active'] else '🔴'
                text += f'• {status} {referral_name}: {earned}\n'

            if len(referral_stats['referrals_detail']) > 5:
                text += texts.t('ADMIN_USERS_STATS_045', '• ... и еще {value1} рефералов\n').format(
                    value1=len(referral_stats['referrals_detail']) - 5
                )
    else:
        text += texts.t('ADMIN_USERS_STATS_029', '<b>Реферальная программа:</b>\n')
        text += texts.t('ADMIN_USERS_STATS_030', '• Рефералов нет\n')
        text += texts.t('ADMIN_USERS_STATS_031', '• Доходов нет\n')

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_STATS_046', '⬅️ К пользователю'),
                        callback_data=f'admin_user_manage_{user_id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


async def get_detailed_referral_stats(db: AsyncSession, user_id: int) -> dict:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.database.crud.referral import get_referral_earnings_by_user, get_user_referral_stats

    base_stats = await get_user_referral_stats(db, user_id)

    referrals_query = (
        select(User)
        .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
        .where(User.referred_by_id == user_id)
    )

    referrals_result = await db.execute(referrals_query)
    referrals = referrals_result.scalars().all()

    earnings_by_referral = {}
    all_earnings = await get_referral_earnings_by_user(db, user_id)

    for earning in all_earnings:
        referral_id = earning.referral_id
        if referral_id not in earnings_by_referral:
            earnings_by_referral[referral_id] = 0
        earnings_by_referral[referral_id] += earning.amount_kopeks

    referrals_detail = []
    current_time = datetime.now(UTC)

    for referral in referrals:
        earned = earnings_by_referral.get(referral.id, 0)

        is_active = False
        ref_subs = getattr(referral, 'subscriptions', None) or []
        ref_sub = next((s for s in ref_subs if s.is_active), ref_subs[0] if ref_subs else None)
        if ref_sub:
            from app.database.models import SubscriptionStatus

            is_active = ref_sub.status == SubscriptionStatus.ACTIVE.value and ref_sub.end_date > current_time

        referrals_detail.append(
            {
                'referral_id': referral.id,
                'referral_name': referral.full_name,
                'referral_telegram_id': referral.telegram_id,
                'total_earned_kopeks': earned,
                'is_active': is_active,
                'registration_date': referral.created_at,
                'has_subscription': bool(ref_subs),
            }
        )

    referrals_detail.sort(key=lambda x: x['total_earned_kopeks'], reverse=True)

    return {
        'invited_count': base_stats['invited_count'],
        'active_referrals': base_stats['active_referrals'],
        'total_earned_kopeks': base_stats['total_earned_kopeks'],
        'month_earned_kopeks': base_stats['month_earned_kopeks'],
        'referrals_detail': referrals_detail,
    }


@admin_required
@error_handler
async def extend_user_subscription(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    await state.update_data(extending_user_id=user_id, admin_subscription_id=subscription_id)

    _sid = f'_s{subscription_id}' if subscription_id else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_USERS_SUBACTIONS_002',
            '⏰ <b>Продление подписки</b>\n\nВведите количество дней для изменения:\n• Положительные значения продлят подписку\n• Отрицательные сократят срок подписки\n• Диапазон: от -365 до 365 дней (0 недопустимо)\n\nИли нажмите /cancel для отмены',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_034', '-7 дней'),
                        callback_data=f'admin_sub_extend_days_{user_id}{_sid}_-7',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_035', '-30 дней'),
                        callback_data=f'admin_sub_extend_days_{user_id}{_sid}_-30',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_036', '7 дней'),
                        callback_data=f'admin_sub_extend_days_{user_id}{_sid}_7',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_037', '30 дней'),
                        callback_data=f'admin_sub_extend_days_{user_id}{_sid}_30',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_038', '90 дней'),
                        callback_data=f'admin_sub_extend_days_{user_id}{_sid}_90',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_039', '180 дней'),
                        callback_data=f'admin_sub_extend_days_{user_id}{_sid}_180',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_040', '❌ Отмена'),
                        callback_data=back_cb,
                    )
                ],
            ]
        ),
    )

    await state.set_state(AdminStates.extending_subscription)
    await callback.answer()


@admin_required
@error_handler
async def process_subscription_extension_days(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    # days is always the last part (may be negative, e.g. '-7')
    days = int(parts[-1])
    # Check if second-to-last part is a subscription_id marker (e.g. 's456')
    if parts[-2].startswith('s') and parts[-2][1:].isdigit():
        subscription_id = int(parts[-2][1:])
        user_id = int(parts[-3])
    else:
        subscription_id = None
        user_id = int(parts[-2])

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    if days == 0 or days < -365 or days > 365:
        await callback.answer(
            texts.t('ADMIN_USERS_SUBACTIONS_013', '❌ Количество дней должно быть от -365 до 365, исключая 0'),
            show_alert=True,
        )
        return

    success = await _extend_subscription_by_days(db, user_id, days, db_user.id, subscription_id=subscription_id)

    if success:
        if days > 0:
            action_text = texts.t('ADMIN_USERS_SUBACTIONS_003', 'продлена на {value1} дней').format(value1=days)
        else:
            action_text = texts.t('ADMIN_USERS_SUBACTIONS_004', 'уменьшена на {value1} дней').format(value1=abs(days))
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_014', '✅ Подписка пользователя {value1}').format(value1=action_text),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_015', '❌ Ошибка продления подписки'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_subscription_extension_text(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    user_id = data.get('extending_user_id')
    subscription_id = data.get('admin_subscription_id')

    if not user_id:
        await message.answer(texts.t('ADMIN_USERS_SUBACTIONS_016', '❌ Ошибка: пользователь не найден'))
        await state.clear()
        return

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    try:
        days = int(message.text.strip())

        if days == 0 or days < -365 or days > 365:
            await message.answer(
                texts.t('ADMIN_USERS_SUBACTIONS_013', '❌ Количество дней должно быть от -365 до 365, исключая 0')
            )
            return

        success = await _extend_subscription_by_days(db, user_id, days, db_user.id, subscription_id=subscription_id)

        if success:
            if days > 0:
                action_text = texts.t('ADMIN_USERS_SUBACTIONS_003', 'продлена на {value1} дней').format(value1=days)
            else:
                action_text = texts.t('ADMIN_USERS_SUBACTIONS_004', 'уменьшена на {value1} дней').format(
                    value1=abs(days)
                )
            await message.answer(
                texts.t('ADMIN_USERS_SUBACTIONS_014', '✅ Подписка пользователя {value1}').format(value1=action_text),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                                callback_data=back_cb,
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USERS_SUBACTIONS_015', '❌ Ошибка продления подписки'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USERS_SUBACTIONS_029', '❌ Введите корректное число дней'))
        return

    await state.clear()


@admin_required
@error_handler
async def add_subscription_traffic(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    await state.update_data(traffic_user_id=user_id, admin_subscription_id=subscription_id)

    _sid = f'_s{subscription_id}' if subscription_id else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_USERS_SUBACTIONS_005',
            '📊 <b>Добавление трафика</b>\n\nВведите количество ГБ для добавления:\n• Например: 50, 100, 500\n• Максимум: 10000 ГБ\n\nИли нажмите /cancel для отмены',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_041', '50 ГБ'),
                        callback_data=f'admin_sub_traffic_add_{user_id}{_sid}_50',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_042', '100 ГБ'),
                        callback_data=f'admin_sub_traffic_add_{user_id}{_sid}_100',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_043', '500 ГБ'),
                        callback_data=f'admin_sub_traffic_add_{user_id}{_sid}_500',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_044', '1000 ГБ'),
                        callback_data=f'admin_sub_traffic_add_{user_id}{_sid}_1000',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_045', '♾️ Безлимит'),
                        callback_data=f'admin_sub_traffic_add_{user_id}{_sid}_0',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_040', '❌ Отмена'),
                        callback_data=back_cb,
                    )
                ],
            ]
        ),
    )

    await state.set_state(AdminStates.adding_traffic)
    await callback.answer()


@admin_required
@error_handler
async def process_traffic_addition_button(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    gb = int(parts[-1])
    if parts[-2].startswith('s') and parts[-2][1:].isdigit():
        subscription_id = int(parts[-2][1:])
        user_id = int(parts[-3])
    else:
        subscription_id = None
        user_id = int(parts[-2])

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    success = await _add_subscription_traffic(db, user_id, gb, db_user.id, subscription_id=subscription_id)

    if success:
        traffic_text = (
            texts.t('ADMIN_USERS_SUBACTIONS_006', '♾️ безлимитный')
            if gb == 0
            else texts.t('ADMIN_USERS_SUBACTIONS_007', '{value1} ГБ').format(value1=gb)
        )
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_017', '✅ К подписке пользователя добавлен трафик: {value1}').format(
                value1=traffic_text
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_018', '❌ Ошибка добавления трафика'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_traffic_addition_text(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    user_id = data.get('traffic_user_id')
    subscription_id = data.get('admin_subscription_id')

    if not user_id:
        await message.answer(texts.t('ADMIN_USERS_SUBACTIONS_016', '❌ Ошибка: пользователь не найден'))
        await state.clear()
        return

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    try:
        gb = int(message.text.strip())

        if gb < 0 or gb > 10000:
            await message.answer(
                texts.t('ADMIN_USERS_SUBACTIONS_030', '❌ Количество ГБ должно быть от 0 до 10000 (0 = безлимит)')
            )
            return

        success = await _add_subscription_traffic(db, user_id, gb, db_user.id, subscription_id=subscription_id)

        if success:
            traffic_text = (
                texts.t('ADMIN_USERS_SUBACTIONS_006', '♾️ безлимитный')
                if gb == 0
                else texts.t('ADMIN_USERS_SUBACTIONS_007', '{value1} ГБ').format(value1=gb)
            )
            await message.answer(
                texts.t('ADMIN_USERS_SUBACTIONS_017', '✅ К подписке пользователя добавлен трафик: {value1}').format(
                    value1=traffic_text
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                                callback_data=back_cb,
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USERS_SUBACTIONS_018', '❌ Ошибка добавления трафика'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USERS_SUBACTIONS_031', '❌ Введите корректное число ГБ'))
        return

    await state.clear()


@admin_required
@error_handler
async def deactivate_user_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    _sid = f'_s{subscription_id}' if subscription_id else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    await callback.message.edit_text(
        get_texts(db_user.language).t(
            'ADMIN_USERS_SUBACTIONS_008',
            '🚫 <b>Деактивация подписки</b>\n\nВы уверены, что хотите деактивировать подписку этого пользователя?\nПользователь потеряет доступ к сервису.',
        ),
        reply_markup=get_confirmation_keyboard(
            f'admin_sub_deactivate_confirm_{user_id}{_sid}', back_cb, db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_subscription_deactivation(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    success = await _deactivate_user_subscription(db, user_id, db_user.id, subscription_id=subscription_id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_019', '✅ Подписка пользователя деактивирована'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_020', '❌ Ошибка деактивации подписки'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def reset_user_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Подтверждение полного обнуления подписки (с сохранением пользователя и тикетов)."""
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    _sid = f'_s{subscription_id}' if subscription_id else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    await callback.message.edit_text(
        get_texts(db_user.language).t(
            'ADMIN_USERS_SUBACTIONS_009',
            '🧹 <b>Обнуление подписки</b>\n\nПодписка будет полностью обнулена «как будто пользователь её не оформлял»:\n• срок и наспамленные дни сбрасываются\n• трафик и доступ к серверам снимаются\n• пользователь <b>отключается</b> в панели RemnaWave (не удаляется)\n\n✅ Сам пользователь и его тикеты <b>остаются</b> в боте.\nПосле этого он сможет купить тариф с нуля и выбрать срок.',
        ),
        reply_markup=get_confirmation_keyboard(f'admin_sub_reset_confirm_{user_id}{_sid}', back_cb, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_subscription_reset(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    success = await _reset_user_subscription(db, user_id, db_user.id, subscription_id=subscription_id)

    message = (
        texts.t('ADMIN_USERS_SUBACTIONS_000', '✅ Подписка обнулена. Пользователь и его тикеты сохранены.')
        if success
        else texts.t('ADMIN_USERS_SUBACTIONS_001', '❌ Ошибка обнуления подписки')
    )
    await callback.message.edit_text(
        message,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                        callback_data=back_cb,
                    )
                ]
            ]
        ),
    )
    await callback.answer()


async def _reset_user_subscription(
    db: AsyncSession, user_id: int, admin_id: int, subscription_id: int | None = None
) -> bool:
    try:
        from app.services.subscription_service import reset_subscription_with_panel

        subscription = await _resolve_admin_subscription(db, user_id, subscription_id)
        if not subscription:
            logger.error('Подписка не найдена для пользователя', user_id=user_id)
            return False

        user = await get_user_by_id(db, user_id)
        result = await reset_subscription_with_panel(db, user, subscription)

        logger.info(
            'Админ обнулил подписку пользователя (пользователь и тикеты сохранены)',
            admin_id=admin_id,
            user_id=user_id,
            subscription_id=subscription.id,
            panel_disabled=result.get('panel_disabled'),
        )
        return True

    except Exception as e:
        logger.error('Ошибка обнуления подписки', error=e)
        return False


@admin_required
@error_handler
async def delete_user_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Show confirmation for deleting a subscription (multi-tariff only)."""
    texts = get_texts(db_user.language)
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    if not subscription_id or not settings.is_multi_tariff_enabled():
        await callback.answer(
            texts.t('ADMIN_USERS_SUBACTIONS_021', 'Удаление доступно только в мультитарифном режиме'),
            show_alert=True,
        )
        return

    back_cb = f'admin_user_sub_select_{user_id}_{subscription_id}'
    _sid = f'_s{subscription_id}'

    await callback.message.edit_text(
        texts.t(
            'ADMIN_USERS_SUBACTIONS_010',
            '🗑 <b>Удаление подписки</b>\n\n⚠️ Подписка будет полностью удалена из системы.\nЭто действие необратимо!',
        ),
        reply_markup=get_confirmation_keyboard(f'admin_sub_delete_confirm_{user_id}{_sid}', back_cb, db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_subscription_deletion(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Delete a subscription permanently (multi-tariff only)."""
    texts = get_texts(db_user.language)
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    if not subscription_id or not settings.is_multi_tariff_enabled():
        await callback.answer(
            texts.t('ADMIN_USERS_SUBACTIONS_021', 'Удаление доступно только в мультитарифном режиме'),
            show_alert=True,
        )
        return

    from app.database.crud.subscription import get_subscription_by_id_for_user

    subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    if not subscription:
        await callback.answer(texts.t('ADMIN_USERS_SUBACTIONS_022', 'Подписка не найдена'), show_alert=True)
        return

    from app.services.grace_access_runtime import (
        GraceAccessDeletionBlocked,
        ensure_no_open_grace_for_subscriptions,
    )

    try:
        await ensure_no_open_grace_for_subscriptions(db, (subscription.id,))
    except GraceAccessDeletionBlocked:
        await callback.answer(
            texts.t('ADMIN_USERS_SUBACTIONS_032', 'Сначала завершите или восстановите активный grace-доступ.'),
            show_alert=True,
        )
        return

    # Best-effort: stop Platega SBP autopay before the row disappears — the
    # platega_subscriptions record CASCADE-deletes with it, so cancelling
    # after the delete would find nothing to cancel on Platega's side.
    # NOTE: this commits its own transaction internally, which releases the
    # grace-guard's Postgres advisory lock acquired just above. It therefore
    # runs BEFORE any irreversible panel/DB step, and the guard is
    # re-acquired immediately below — closing that window before anything
    # that can't be undone happens.
    from app.services.cashera_recurring_cancel import cancel_cashera_recurring_for_subscription_safe
    from app.services.cispay_recurrent_guard import stage_cispay_cancellation
    from app.services.payment.lava import cancel_lava_recurring_for_subscription_safe
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    await stage_cispay_cancellation(db, subscription.id, reason='subscription_deleted')
    await cancel_platega_recurring_for_subscription_safe(db, subscription.id)

    await cancel_lava_recurring_for_subscription_safe(db, subscription.id)
    await cancel_cashera_recurring_for_subscription_safe(db, subscription.id)
    try:
        await ensure_no_open_grace_for_subscriptions(db, (subscription.id,))
    except GraceAccessDeletionBlocked:
        await callback.answer(
            texts.t('ADMIN_USERS_SUBACTIONS_032', 'Сначала завершите или восстановите активный grace-доступ.'),
            show_alert=True,
        )
        return

    # Disable on Remnawave side first
    panel_user_id = getattr(subscription, 'remnawave_id', None)
    if panel_user_id:
        subscription_service = SubscriptionService()
        await subscription_service.disable_remnawave_user(panel_user_id, db=db)

    # Delete traffic purchases
    from sqlalchemy import delete as sql_delete

    from app.database.models import TrafficPurchase

    await db.execute(sql_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))

    await db.delete(subscription)
    await db.commit()

    logger.info(
        'Админ удалил подписку пользователя',
        admin_id=db_user.id,
        user_id=user_id,
        subscription_id=subscription_id,
    )

    back_cb = f'admin_user_subscription_{user_id}'
    await callback.message.edit_text(
        texts.t('ADMIN_USERS_SUBACTIONS_011', '✅ Подписка удалена'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_047', '📱 К подпискам'),
                        callback_data=back_cb,
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def activate_user_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    success = await _activate_user_subscription(db, user_id, db_user.id, subscription_id=subscription_id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_023', '✅ Подписка пользователя активирована'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_024', '❌ Ошибка активации подписки'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def grant_trial_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])

    success = await _grant_trial_subscription(db, user_id, db_user.id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_025', '✅ Пользователю выдан триальный период'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_026', '❌ Ошибка выдачи триального периода'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def grant_paid_subscription(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    user_id = int(callback.data.split('_')[-1])

    await state.update_data(granting_user_id=user_id)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_USERS_SUBACTIONS_012',
            '💎 <b>Выдача подписки</b>\n\nВведите количество дней подписки:\n• Например: 30, 90, 180, 365\n• Максимум: 730 дней\n\nИли нажмите /cancel для отмены',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_037', '30 дней'),
                        callback_data=f'admin_sub_grant_days_{user_id}_30',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_038', '90 дней'),
                        callback_data=f'admin_sub_grant_days_{user_id}_90',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_039', '180 дней'),
                        callback_data=f'admin_sub_grant_days_{user_id}_180',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_048', '365 дней'),
                        callback_data=f'admin_sub_grant_days_{user_id}_365',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_SUBACTIONS_040', '❌ Отмена'),
                        callback_data=f'admin_user_subscription_{user_id}',
                    )
                ],
            ]
        ),
    )

    await state.set_state(AdminStates.granting_subscription)
    await callback.answer()


@admin_required
@error_handler
async def process_subscription_grant_days(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    days = int(parts[-1])

    success = await _grant_paid_subscription(db, user_id, days, db_user.id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_027', '✅ Пользователю выдана подписка на {value1} дней').format(
                value1=days
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_SUBACTIONS_028', '❌ Ошибка выдачи подписки'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_subscription_grant_text(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    user_id = data.get('granting_user_id')

    if not user_id:
        await message.answer(texts.t('ADMIN_USERS_SUBACTIONS_016', '❌ Ошибка: пользователь не найден'))
        await state.clear()
        return

    try:
        days = int(message.text.strip())

        if days <= 0 or days > 730:
            await message.answer(texts.t('ADMIN_USERS_SUBACTIONS_033', '❌ Количество дней должно быть от 1 до 730'))
            return

        success = await _grant_paid_subscription(db, user_id, days, db_user.id)

        if success:
            await message.answer(
                texts.t('ADMIN_USERS_SUBACTIONS_027', '✅ Пользователю выдана подписка на {value1} дней').format(
                    value1=days
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                                callback_data=f'admin_user_subscription_{user_id}',
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USERS_SUBACTIONS_028', '❌ Ошибка выдачи подписки'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USERS_SUBACTIONS_029', '❌ Введите корректное число дней'))
        return

    await state.clear()


@admin_required
@error_handler
async def show_user_servers_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])

    if await _render_user_subscription_overview(callback, db, user_id, language=db_user.language):
        await callback.answer()


@admin_required
@error_handler
async def show_server_selection(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id, subscription_id = _extract_admin_sub_context(callback.data)
    await _show_servers_for_user(callback, user_id, db, subscription_id=subscription_id, language=db_user.language)
    await callback.answer()


async def _show_servers_for_user(
    callback: types.CallbackQuery,
    user_id: int,
    db: AsyncSession,
    subscription_id: int | None = None,
    language: str = 'ru',
):
    texts = get_texts(language)
    try:
        user = await get_user_by_id(db, user_id)
        current_squads = []
        if user:
            if subscription_id and settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_subscription_by_id_for_user

                subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
            else:
                subscription = await _resolve_admin_subscription(db, user_id)
            if subscription:
                current_squads = subscription.connected_squads or []

        _sid = f'_s{subscription_id}' if subscription_id and settings.is_multi_tariff_enabled() else ''
        back_cb = (
            f'admin_user_sub_select_{user_id}_{subscription_id}'
            if subscription_id and settings.is_multi_tariff_enabled()
            else f'admin_user_subscription_{user_id}'
        )

        all_servers, _ = await get_all_server_squads(db, available_only=False)

        servers_to_show = []
        for server in all_servers:
            if server.is_available or server.squad_uuid in current_squads:
                servers_to_show.append(server)

        if not servers_to_show:
            await callback.message.edit_text(
                texts.t('ADMIN_USERS_LIMITS_019', '❌ Доступные серверы не найдены'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data=back_cb
                            )
                        ]
                    ]
                ),
            )
            return

        text = texts.t('ADMIN_USERS_LIMITS_000', '🌍 <b>Управление серверами</b>\n\n')
        text += texts.t('ADMIN_USERS_LIMITS_001', 'Нажмите на сервер чтобы добавить/убрать:\n')
        text += texts.t('ADMIN_USERS_LIMITS_002', '✅ - выбранный сервер\n')
        text += texts.t('ADMIN_USERS_LIMITS_003', '⚪ - доступный сервер\n')
        text += texts.t('ADMIN_USERS_LIMITS_004', '🔒 - неактивный (только для уже назначенных)\n\n')

        keyboard = []
        selected_servers = [s for s in servers_to_show if s.squad_uuid in current_squads]
        available_servers = [s for s in servers_to_show if s.squad_uuid not in current_squads and s.is_available]
        inactive_servers = [s for s in servers_to_show if s.squad_uuid not in current_squads and not s.is_available]

        sorted_servers = selected_servers + available_servers + inactive_servers

        for server in sorted_servers[:20]:
            is_selected = server.squad_uuid in current_squads

            if is_selected:
                emoji = '✅'
            elif server.is_available:
                emoji = '⚪'
            else:
                emoji = '🔒'

            display_name = server.display_name
            if not server.is_available and not is_selected:
                display_name += texts.t('ADMIN_USERS_LIMITS_013', ' (неактивный)')

            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{emoji} {display_name}',
                        callback_data=f'admin_user_toggle_server_{user_id}_{server.id}{_sid}',
                    )
                ]
            )

        if len(servers_to_show) > 20:
            text += texts.t('ADMIN_USERS_LIMITS_006', '\n📝 Показано первых 20 из {value1} серверов').format(
                value1=len(servers_to_show)
            )

        keyboard.append(
            [
                types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIMITS_031', '✅ Готово'), callback_data=back_cb),
                types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data=back_cb),
            ]
        )

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Ошибка показа серверов', error=e)


@admin_required
@error_handler
async def toggle_user_server(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    user_id = int(parts[4])
    server_id = int(parts[5])
    subscription_id = int(parts[6][1:]) if len(parts) > 6 and parts[6].startswith('s') else None

    try:
        user = await get_user_by_id(db, user_id)
        if subscription_id and settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_subscription_by_id_for_user

            subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
        else:
            subscription = await _resolve_admin_subscription(db, user_id)
        if not user or not subscription:
            await callback.answer(
                texts.t('ADMIN_USERS_LIMITS_020', '❌ Пользователь или подписка не найдены'),
                show_alert=True,
            )
            return

        server = await get_server_squad_by_id(db, server_id)
        if not server:
            await callback.answer(texts.t('ADMIN_USERS_LIMITS_021', '❌ Сервер не найден'), show_alert=True)
            return
        current_squads = list(subscription.connected_squads or [])

        if server.squad_uuid in current_squads:
            current_squads.remove(server.squad_uuid)
            action_text = 'удален'
        else:
            current_squads.append(server.squad_uuid)
            action_text = 'добавлен'

        subscription.connected_squads = current_squads
        subscription.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(subscription)

        try:
            await _push_narrow_change_to_panel(db, user, subscription, fields={'active_internal_squads'})
            logger.info('✅ Обновлены серверы в RemnaWave для пользователя', telegram_id=user.telegram_id)
        except Exception as rw_error:
            logger.error('❌ Ошибка обновления RemnaWave', rw_error=rw_error)

        logger.info(
            'Админ сервер для пользователя',
            db_user_id=db_user.id,
            display_name=server.display_name,
            action_text=action_text,
            user_id=user_id,
        )

        await refresh_server_selection_screen(callback, user_id, db_user, db, subscription_id=subscription_id)

    except Exception as e:
        logger.error('Ошибка переключения сервера', error=e)
        await callback.answer(texts.t('ADMIN_USERS_LIMITS_022', '❌ Ошибка изменения сервера'), show_alert=True)


async def refresh_server_selection_screen(
    callback: types.CallbackQuery,
    user_id: int,
    db_user: User,
    db: AsyncSession,
    subscription_id: int | None = None,
):
    texts = get_texts(db_user.language)
    try:
        user = await get_user_by_id(db, user_id)
        current_squads = []
        if user:
            if subscription_id and settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_subscription_by_id_for_user

                subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
            else:
                subscription = await _resolve_admin_subscription(db, user_id)
            if subscription:
                current_squads = subscription.connected_squads or []

        _sid = f'_s{subscription_id}' if subscription_id and settings.is_multi_tariff_enabled() else ''
        back_cb = (
            f'admin_user_sub_select_{user_id}_{subscription_id}'
            if subscription_id and settings.is_multi_tariff_enabled()
            else f'admin_user_subscription_{user_id}'
        )

        servers, _ = await get_all_server_squads(db, available_only=True)

        if not servers:
            await callback.message.edit_text(
                texts.t('ADMIN_USERS_LIMITS_019', '❌ Доступные серверы не найдены'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'),
                                callback_data=back_cb,
                            )
                        ]
                    ]
                ),
            )
            return

        text = texts.t('ADMIN_USERS_LIMITS_000', '🌍 <b>Управление серверами</b>\n\n')
        text += texts.t('ADMIN_USERS_LIMITS_005', 'Нажмите на сервер чтобы добавить/убрать:\n\n')

        keyboard = []
        for server in servers[:15]:
            is_selected = server.squad_uuid in current_squads
            emoji = '✅' if is_selected else '⚪'

            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{emoji} {server.display_name}',
                        callback_data=f'admin_user_toggle_server_{user_id}_{server.id}{_sid}',
                    )
                ]
            )

        if len(servers) > 15:
            text += texts.t('ADMIN_USERS_LIMITS_007', '\n📝 Показано первых 15 из {value1} серверов').format(
                value1=len(servers)
            )

        keyboard.append(
            [
                types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIMITS_031', '✅ Готово'), callback_data=back_cb),
                types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data=back_cb),
            ]
        )

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error('Ошибка обновления экрана серверов', error=e)


@admin_required
@error_handler
async def start_devices_edit(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    await state.update_data(editing_devices_user_id=user_id, admin_subscription_id=subscription_id)

    _sid = f'_s{subscription_id}' if subscription_id else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id and settings.is_multi_tariff_enabled()
        else f'admin_user_subscription_{user_id}'
    )

    # По всей кодовой базе MAX_DEVICES_LIMIT=0 трактуется как «без ограничения»
    # (см. subscription.py, devices.py, inline.py, miniapp.py — 16+ мест).
    # Если читать буквально — получаем `range(1, 1)` → пустая клавиатура и
    # промпт "от 1 до 0", который блокирует любой ввод (репорт от 2026-05-16).
    raw_limit = settings.MAX_DEVICES_LIMIT
    is_unlimited = raw_limit <= 0
    # Для quick-pick клавиатуры используем разумный потолок 20 (как в других
    # местах UI), для текстового ввода админ может задать любое положительное.
    quick_pick_cap = 20 if is_unlimited else min(raw_limit, 99)
    limit_display = '∞' if is_unlimited else str(raw_limit)

    device_buttons: list[list[types.InlineKeyboardButton]] = []
    row: list[types.InlineKeyboardButton] = []
    for i in range(1, quick_pick_cap + 1):
        row.append(
            types.InlineKeyboardButton(
                text=str(i),
                callback_data=f'admin_user_devices_set_{user_id}{_sid}_{i}',
            )
        )
        if len(row) == 4:
            device_buttons.append(row)
            row = []
    if row:
        device_buttons.append(row)
    device_buttons.append(
        [types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIMITS_023', '❌ Отмена'), callback_data=back_cb)]
    )
    markup = types.InlineKeyboardMarkup(inline_keyboard=device_buttons)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_USERS_LIMITS_008',
            '📱 <b>Изменение количества устройств</b>\n\nВведите новое количество устройств (от 1 до {value1}):\n• Текущее значение будет заменено\n\nИли нажмите /cancel для отмены',
        ).format(value1=limit_display),
        reply_markup=markup,
    )

    await state.set_state(AdminStates.editing_user_devices)
    await callback.answer()


@admin_required
@error_handler
async def set_user_devices_button(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    devices = int(parts[-1])
    if parts[-2].startswith('s') and parts[-2][1:].isdigit():
        subscription_id = int(parts[-2][1:])
        user_id = int(parts[-3])
    else:
        subscription_id = None
        user_id = int(parts[-2])

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id and settings.is_multi_tariff_enabled()
        else f'admin_user_subscription_{user_id}'
    )

    success = await _update_user_devices(db, user_id, devices, db_user.id, subscription_id=subscription_id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_LIMITS_014', '✅ Количество устройств изменено на: {value1}').format(value1=devices),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_LIMITS_038', '📱 Подписка и настройки'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_LIMITS_015', '❌ Ошибка изменения количества устройств'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_LIMITS_038', '📱 Подписка и настройки'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )

    await callback.answer()

    logger.info(
        'Админ изменил устройства для пользователя', telegram_id=db_user.telegram_id, devices=devices, user_id=user_id
    )


@admin_required
@error_handler
async def process_devices_edit_text(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    user_id = data.get('editing_devices_user_id')
    subscription_id = data.get('admin_subscription_id')

    if not user_id:
        await message.answer(texts.t('ADMIN_USERS_LIMITS_016', '❌ Ошибка: пользователь не найден'))
        await state.clear()
        return

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id and settings.is_multi_tariff_enabled()
        else f'admin_user_subscription_{user_id}'
    )

    try:
        devices = int(message.text.strip())

        # MAX_DEVICES_LIMIT=0 ≡ «без ограничения» (та же конвенция, что и в
        # остальной кодовой базе). До фикса валидатор отвергал любое значение,
        # потому что `devices > 0` всегда true для положительных чисел.
        raw_limit = settings.MAX_DEVICES_LIMIT
        is_unlimited = raw_limit <= 0
        if devices <= 0 or (not is_unlimited and devices > raw_limit):
            limit_display = '∞' if is_unlimited else str(raw_limit)
            await message.answer(
                texts.t('ADMIN_USERS_LIMITS_024', '❌ Количество устройств должно быть от 1 до {value1}').format(
                    value1=limit_display
                )
            )
            return

        success = await _update_user_devices(db, user_id, devices, db_user.id, subscription_id=subscription_id)

        if success:
            await message.answer(
                texts.t('ADMIN_USERS_LIMITS_014', '✅ Количество устройств изменено на: {value1}').format(
                    value1=devices
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USERS_LIMITS_038', '📱 Подписка и настройки'),
                                callback_data=back_cb,
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USERS_LIMITS_015', '❌ Ошибка изменения количества устройств'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USERS_LIMITS_025', '❌ Введите корректное число устройств'))
        return

    await state.clear()


@admin_required
@error_handler
async def start_traffic_edit(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    await state.update_data(editing_traffic_user_id=user_id, admin_subscription_id=subscription_id)

    _sid = f'_s{subscription_id}' if subscription_id else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id and settings.is_multi_tariff_enabled()
        else f'admin_user_subscription_{user_id}'
    )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_USERS_LIMITS_009',
            '📊 <b>Изменение лимита трафика</b>\n\nВведите новый лимит трафика в ГБ:\n• 0 - безлимитный трафик\n• Примеры: 50, 100, 500, 1000\n• Максимум: 10000 ГБ\n\nИли нажмите /cancel для отмены',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_LIMITS_033', '50 ГБ'),
                        callback_data=f'admin_user_traffic_set_{user_id}{_sid}_50',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_LIMITS_034', '100 ГБ'),
                        callback_data=f'admin_user_traffic_set_{user_id}{_sid}_100',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_LIMITS_035', '500 ГБ'),
                        callback_data=f'admin_user_traffic_set_{user_id}{_sid}_500',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_LIMITS_036', '1000 ГБ'),
                        callback_data=f'admin_user_traffic_set_{user_id}{_sid}_1000',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_LIMITS_037', '♾️ Безлимит'),
                        callback_data=f'admin_user_traffic_set_{user_id}{_sid}_0',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_LIMITS_023', '❌ Отмена'), callback_data=back_cb
                    )
                ],
            ]
        ),
    )

    await state.set_state(AdminStates.editing_user_traffic)
    await callback.answer()


@admin_required
@error_handler
async def set_user_traffic_button(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    parts = callback.data.split('_')
    traffic_gb = int(parts[-1])
    if parts[-2].startswith('s') and parts[-2][1:].isdigit():
        subscription_id = int(parts[-2][1:])
        user_id = int(parts[-3])
    else:
        subscription_id = None
        user_id = int(parts[-2])

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id and settings.is_multi_tariff_enabled()
        else f'admin_user_subscription_{user_id}'
    )

    success = await _update_user_traffic(db, user_id, traffic_gb, db_user.id, subscription_id=subscription_id)

    if success:
        traffic_text = (
            texts.t('ADMIN_USERS_LIMITS_010', '♾️ безлимитный')
            if traffic_gb == 0
            else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=traffic_gb)
        )
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_LIMITS_017', '✅ Лимит трафика изменен на: {value1}').format(value1=traffic_text),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_LIMITS_038', '📱 Подписка и настройки'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_LIMITS_018', '❌ Ошибка изменения лимита трафика'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_LIMITS_038', '📱 Подписка и настройки'),
                            callback_data=back_cb,
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_traffic_edit_text(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    user_id = data.get('editing_traffic_user_id')
    subscription_id = data.get('admin_subscription_id')

    if not user_id:
        await message.answer(texts.t('ADMIN_USERS_LIMITS_016', '❌ Ошибка: пользователь не найден'))
        await state.clear()
        return

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id and settings.is_multi_tariff_enabled()
        else f'admin_user_subscription_{user_id}'
    )

    try:
        traffic_gb = int(message.text.strip())

        if traffic_gb < 0 or traffic_gb > 10000:
            await message.answer(
                texts.t('ADMIN_USERS_LIMITS_026', '❌ Лимит трафика должен быть от 0 до 10000 ГБ (0 = безлимит)')
            )
            return

        success = await _update_user_traffic(db, user_id, traffic_gb, db_user.id, subscription_id=subscription_id)

        if success:
            traffic_text = (
                texts.t('ADMIN_USERS_LIMITS_010', '♾️ безлимитный')
                if traffic_gb == 0
                else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=traffic_gb)
            )
            await message.answer(
                texts.t('ADMIN_USERS_LIMITS_017', '✅ Лимит трафика изменен на: {value1}').format(value1=traffic_text),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USERS_LIMITS_038', '📱 Подписка и настройки'),
                                callback_data=back_cb,
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USERS_LIMITS_018', '❌ Ошибка изменения лимита трафика'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USERS_LIMITS_027', '❌ Введите корректное число ГБ'))
        return

    await state.clear()


@admin_required
@error_handler
async def confirm_reset_devices(callback: types.CallbackQuery, db_user: User):
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    _sid = f'_s{subscription_id}' if subscription_id else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id and settings.is_multi_tariff_enabled()
        else f'admin_user_subscription_{user_id}'
    )

    await callback.message.edit_text(
        get_texts(db_user.language).t(
            'ADMIN_USERS_LIMITS_012',
            '🔄 <b>Сброс устройств пользователя</b>\n\n⚠️ <b>ВНИМАНИЕ!</b>\nВы уверены, что хотите сбросить все HWID устройства этого пользователя?\n\nЭто действие:\n• Удалит все привязанные устройства\n• Пользователь сможет заново подключить устройства\n• Действие необратимо!\n\nПродолжить?',
        ),
        reply_markup=get_confirmation_keyboard(
            f'admin_user_reset_devices_confirm_{user_id}{_sid}', back_cb, db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def reset_user_devices(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id and settings.is_multi_tariff_enabled()
        else f'admin_user_subscription_{user_id}'
    )

    try:
        user = await get_user_by_id(db, user_id)
        panel_user_id = None
        if subscription_id and settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_subscription_by_id_for_user

            subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
            panel_user_id = getattr(subscription, 'remnawave_id', None) if subscription else None
        elif settings.is_multi_tariff_enabled():
            subscription = await _resolve_admin_subscription(db, user_id)
            panel_user_id = getattr(subscription, 'remnawave_id', None) if subscription else None
        if not panel_user_id:
            panel_user_id = getattr(user, 'remnawave_id', None)
        if not user or not panel_user_id:
            await callback.answer(
                texts.t('ADMIN_USERS_LIMITS_028', '❌ Пользователь не найден или не связан с RemnaWave'),
                show_alert=True,
            )
            return

        remnawave_service = RemnaWaveService()
        async with remnawave_service.get_api_client() as api:
            success = await api.reset_user_devices(panel_user_id)

        if success:
            await callback.message.edit_text(
                texts.t('ADMIN_USERS_LIMITS_029', '✅ Устройства пользователя успешно сброшены'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USERS_LIMITS_038', '📱 Подписка и настройки'),
                                callback_data=back_cb,
                            )
                        ]
                    ]
                ),
            )
            logger.info('Админ сбросил устройства пользователя', db_user_id=db_user.id, user_id=user_id)
        else:
            await callback.message.edit_text(
                texts.t('ADMIN_USERS_LIMITS_030', '❌ Ошибка сброса устройств'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USERS_LIMITS_038', '📱 Подписка и настройки'),
                                callback_data=back_cb,
                            )
                        ]
                    ]
                ),
            )

    except Exception as e:
        logger.error('Ошибка сброса устройств', error=e)
        await callback.answer(texts.t('ADMIN_USERS_LIMITS_030', '❌ Ошибка сброса устройств'), show_alert=True)


async def _push_narrow_change_to_panel(db, user, subscription, *, fields: set[str]) -> None:
    """Донести до панели одно изменение подписки, не трогая соседние поля.

    Админские экраны правят по одному свойству — серверы, лимит устройств, лимит
    трафика. Отправлять при этом полное состояние подписки нельзя: заодно уедут
    дата и статус, которых админ не касался. Поэтому здесь узкий PATCH, но
    собранный тем же сервисом, что и все остальные, — иначе поля снова начнут
    расходиться. Описание аккаунта уезжает всегда: оно про пользователя, а не
    про подписку.
    """
    from app.services.grace_access_runtime import update_panel_user_grace_safe
    from app.services.panel_sync import push_subscription
    from app.services.panel_sync.fields import PANEL_ACCOUNT_METADATA_FIELDS

    remnawave_service = RemnaWaveService()
    try:
        await db.refresh(subscription, ['tariff'])
    except Exception as error:
        # Тариф нужен запросу к панели только для сквадов. Строка могла быть
        # отцеплена от сессии или удалена соседним проходом — тогда идём без
        # него: узкий PATCH трогает лишь то поле, ради которого позван.
        logger.debug('Не удалось догрузить тариф подписки перед PATCH', error=error)

    async with remnawave_service.get_api_client() as api:
        await push_subscription(
            api,
            user,
            subscription,
            db=db,
            only_fields=fields | PANEL_ACCOUNT_METADATA_FIELDS,
            reset_devices=False,
            create_if_missing=False,
            update_call=lambda **kwargs: update_panel_user_grace_safe(api, subscription.id, **kwargs),
        )


async def _update_user_devices(
    db: AsyncSession, user_id: int, devices: int, admin_id: int, subscription_id: int | None = None
) -> bool:
    try:
        user = await get_user_by_id(db, user_id)
        subscription = await _resolve_admin_subscription(db, user_id, subscription_id)
        if not user or not subscription:
            logger.error('Пользователь или подписка не найдены', user_id=user_id)
            return False

        from app.services.cispay_recurrent_guard import require_no_cispay

        await require_no_cispay(db, user_id=user_id, subscription_id=subscription.id)
        old_devices = subscription.device_limit
        subscription.device_limit = devices
        subscription.updated_at = datetime.now(UTC)

        await db.commit()

        try:
            await _push_narrow_change_to_panel(db, user, subscription, fields={'hwid_device_limit'})
            logger.info('✅ Обновлен лимит устройств в RemnaWave для пользователя', telegram_id=user.telegram_id)
        except Exception as rw_error:
            logger.error('❌ Ошибка обновления лимита устройств в RemnaWave', rw_error=rw_error)

        logger.info(
            'Админ изменил лимит устройств пользователя',
            admin_id=admin_id,
            user_id=user_id,
            old_devices=old_devices,
            devices=devices,
        )
        return True

    except Exception as e:
        logger.error('Ошибка обновления лимита устройств', error=e)
        await db.rollback()
        return False


async def _update_user_traffic(
    db: AsyncSession, user_id: int, traffic_gb: int, admin_id: int, subscription_id: int | None = None
) -> bool:
    try:
        user = await get_user_by_id(db, user_id)
        subscription = await _resolve_admin_subscription(db, user_id, subscription_id)
        if not user or not subscription:
            logger.error('Пользователь или подписка не найдены', user_id=user_id)
            return False

        from secrets import token_hex

        from app.services.paid_traffic_reset_service import prepare_admin_tariff_accounting
        from app.services.traffic_base_grant_service import stage_paid_base_grant

        subscription, _ = await prepare_admin_tariff_accounting(db, subscription)
        old_traffic = subscription.traffic_limit_gb
        subscription.traffic_limit_gb = traffic_gb
        subscription.updated_at = datetime.now(UTC)
        if subscription.traffic_accounting_mode == 'managed':
            await stage_paid_base_grant(
                db, subscription, operation_key=f'admin:traffic:{token_hex(16)}', reset_base=False
            )

        await db.commit()

        try:
            await _push_narrow_change_to_panel(
                db, user, subscription, fields={'traffic_limit_bytes', 'traffic_limit_strategy'}
            )
            logger.info('✅ Обновлен лимит трафика в RemnaWave для пользователя', telegram_id=user.telegram_id)
        except Exception as rw_error:
            logger.error('❌ Ошибка обновления лимита трафика в RemnaWave', rw_error=rw_error)

        traffic_text_old = 'безлимитный' if old_traffic == 0 else f'{old_traffic} ГБ'
        traffic_text_new = 'безлимитный' if traffic_gb == 0 else f'{traffic_gb} ГБ'
        logger.info(
            'Админ изменил лимит трафика пользователя',
            admin_id=admin_id,
            user_id=user_id,
            traffic_text_old=traffic_text_old,
            traffic_text_new=traffic_text_new,
        )
        return True

    except Exception as e:
        logger.error('Ошибка обновления лимита трафика', error=e)
        await db.rollback()
        return False


async def _resolve_admin_subscription(
    db: AsyncSession, user_id: int, subscription_id: int | None = None, tariff_id: int | None = None
) -> 'Subscription | None':
    """Resolve subscription for admin operations.

    In multi-tariff without explicit subscription_id:
    - If tariff_id provided → find active sub with that tariff only
    - 1 active sub → use it
    - Multiple → prefer non-daily with most days remaining
    - 0 active → return None
    """
    if settings.is_multi_tariff_enabled():
        if subscription_id:
            from app.database.crud.subscription import get_subscription_by_id_for_user

            return await get_subscription_by_id_for_user(db, subscription_id, user_id)

        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        active_subs = await get_active_subscriptions_by_user_id(db, user_id)

        if not active_subs:
            return None

        # When buying a specific tariff, only match subscription with the same tariff_id
        if tariff_id is not None:
            matching = [s for s in active_subs if s.tariff_id == tariff_id]
            if matching:
                return matching[0]
            # No subscription for this tariff — must create a new one
            return None

        if len(active_subs) == 1:
            return active_subs[0]

        non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
        eligible = non_daily or active_subs
        selected = max(eligible, key=lambda s: s.days_left)
        logger.warning(
            'Admin operation: multiple active subscriptions, auto-selected best',
            user_id=user_id,
            selected_sub_id=selected.id,
            total_active=len(active_subs),
        )
        return selected
    from app.database.crud.subscription import get_subscription_by_user_id

    return await get_subscription_by_user_id(db, user_id)


async def _extend_subscription_by_days(
    db: AsyncSession, user_id: int, days: int, admin_id: int, subscription_id: int | None = None
) -> bool:
    try:
        from app.database.crud.subscription import extend_subscription
        from app.services.subscription_service import SubscriptionService

        subscription = await _resolve_admin_subscription(db, user_id, subscription_id)
        if not subscription:
            logger.error('Подписка не найдена для пользователя', user_id=user_id)
            return False

        await extend_subscription(db, subscription, days, commit=False)
        now = datetime.now(UTC)
        if days < 0 and subscription.end_date <= now:
            subscription.status = SubscriptionStatus.EXPIRED.value
            subscription.grace_suppressed_until = now
        await db.commit()
        await db.refresh(subscription)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        if days > 0:
            logger.info('Админ продлил подписку пользователя на дней', admin_id=admin_id, user_id=user_id, days=days)
        else:
            logger.info(
                'Админ сократил подписку пользователя на дней', admin_id=admin_id, user_id=user_id, value=abs(days)
            )
        return True

    except Exception as e:
        logger.error('Ошибка продления подписки', error=e)
        return False


async def _add_subscription_traffic(
    db: AsyncSession, user_id: int, gb: int, admin_id: int, subscription_id: int | None = None
) -> bool:
    try:
        from app.database.crud.subscription import reactivate_subscription
        from app.services.subscription_service import SubscriptionService
        from app.services.traffic_purchase_service import add_subscription_traffic

        subscription = await _resolve_admin_subscription(db, user_id, subscription_id)
        if not subscription:
            logger.error('Подписка не найдена для пользователя', user_id=user_id)
            return False

        if gb == 0:
            if subscription.traffic_limit_gb:
                from app.services.traffic_accounting_service import transition_to_unlimited_locked
                from app.services.traffic_purchase_service import prepare_traffic_purchase

                subscription, accounting_panel = await prepare_traffic_purchase(db, subscription)
                await transition_to_unlimited_locked(db, subscription, accounting_panel)
                from sqlalchemy import delete

                from app.database.models import TrafficPurchase

                await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
                await db.commit()
        else:
            from app.services.traffic_purchase_service import prepare_traffic_purchase

            accounting_panel = None
            if subscription.traffic_limit_gb:
                subscription, accounting_panel = await prepare_traffic_purchase(db, subscription)
            await add_subscription_traffic(db, subscription, gb, panel=accounting_panel)

        # Реактивируем подписку если она была DISABLED/EXPIRED (например, после LIMITED/EXPIRED в RemnaWave)
        await reactivate_subscription(db, subscription)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        # Явно включаем пользователя на панели (PATCH может не снять LIMITED-статус)
        if subscription.status == 'active':
            panel_user_id = getattr(subscription, 'remnawave_id', None) if settings.is_multi_tariff_enabled() else None
            if not panel_user_id:
                from app.database.crud.user import get_user_by_id

                user = await get_user_by_id(db, user_id)
                panel_user_id = getattr(user, 'remnawave_id', None)
            if panel_user_id:
                await subscription_service.enable_remnawave_user(panel_user_id)

        traffic_text = 'безлимитный' if gb == 0 else f'{gb} ГБ'
        logger.info('Админ добавил трафик пользователю', admin_id=admin_id, traffic_text=traffic_text, user_id=user_id)
        return True

    except Exception as e:
        logger.error('Ошибка добавления трафика', error=e)
        return False


async def _deactivate_user_subscription(
    db: AsyncSession, user_id: int, admin_id: int, subscription_id: int | None = None
) -> bool:
    try:
        from app.database.crud.subscription import (
            deactivate_subscription,
        )
        from app.services.subscription_service import SubscriptionService

        subscription = await _resolve_admin_subscription(db, user_id, subscription_id)
        if not subscription:
            logger.error('Подписка не найдена для пользователя', user_id=user_id)
            return False

        await deactivate_subscription(db, subscription)

        subscription_service = SubscriptionService()
        panel_user_id = getattr(subscription, 'remnawave_id', None) if settings.is_multi_tariff_enabled() else None
        if not panel_user_id:
            user = await get_user_by_id(db, user_id)
            panel_user_id = getattr(user, 'remnawave_id', None)
        if panel_user_id:
            await subscription_service.disable_remnawave_user(panel_user_id)

        logger.info('Админ деактивировал подписку пользователя', admin_id=admin_id, user_id=user_id)
        return True

    except Exception as e:
        logger.error('Ошибка деактивации подписки', error=e)
        return False


async def _activate_user_subscription(
    db: AsyncSession, user_id: int, admin_id: int, subscription_id: int | None = None
) -> bool:
    try:
        from app.database.crud.subscription import reconcile_tariff_traffic_limit
        from app.database.models import SubscriptionStatus
        from app.services.subscription_service import SubscriptionService

        subscription = await _resolve_admin_subscription(db, user_id, subscription_id)
        if not subscription:
            logger.error('Подписка не найдена для пользователя', user_id=user_id)
            return False

        from app.services.cispay_recurrent_guard import require_no_cispay

        await require_no_cispay(db, user_id=user_id, subscription_id=subscription.id)

        from app.services.traffic_accounting_service import stage_authorized_activation

        subscription = await stage_authorized_activation(db, subscription)

        # Оверлей грейса, осевший в подписке (v4.10–4.11), — не её срок: вернуть до расчёта.
        from app.services.grace_access_echo import undo_grace_overlay_echo

        await undo_grace_overlay_echo(db, subscription)
        subscription.status = SubscriptionStatus.ACTIVE.value
        if subscription.end_date <= datetime.now(UTC):
            subscription.end_date = datetime.now(UTC) + timedelta(days=1)
            subscription.pending_panel_expire_at = subscription.end_date
        # Условия тарифа на новый срок: база тарифа + активные докупки.
        await reconcile_tariff_traffic_limit(db, subscription)

        await db.commit()
        await db.refresh(subscription)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        logger.info('Админ активировал подписку пользователя', admin_id=admin_id, user_id=user_id)
        return True

    except Exception as e:
        logger.error('Ошибка активации подписки', error=e)
        return False


async def _grant_trial_subscription(
    db: AsyncSession, user_id: int, admin_id: int, subscription_id: int | None = None
) -> bool:
    try:
        from app.database.crud.subscription import create_trial_subscription
        from app.services.subscription_service import SubscriptionService

        existing_subscription = await _resolve_admin_subscription(db, user_id, subscription_id)
        if existing_subscription and not settings.is_multi_tariff_enabled():
            logger.error('У пользователя уже есть подписка', user_id=user_id)
            return False

        forced_devices = None
        if not settings.is_devices_selection_enabled():
            forced_devices = settings.get_disabled_mode_device_limit()

        subscription = await create_trial_subscription(
            db,
            user_id,
            device_limit=forced_devices,
        )

        subscription_service = SubscriptionService()
        await subscription_service.create_remnawave_user(db, subscription)

        logger.info('Админ выдал триальную подписку пользователю', admin_id=admin_id, user_id=user_id)
        return True

    except Exception as e:
        logger.error('Ошибка выдачи триальной подписки', error=e)
        return False


async def _grant_paid_subscription(
    db: AsyncSession, user_id: int, days: int, admin_id: int, subscription_id: int | None = None
) -> bool:
    try:
        from app.config import settings
        from app.database.crud.subscription import create_paid_subscription
        from app.services.subscription_service import SubscriptionService

        existing_subscription = await _resolve_admin_subscription(db, user_id, subscription_id)
        if existing_subscription and not settings.is_multi_tariff_enabled():
            logger.error('У пользователя уже есть подписка', user_id=user_id)
            return False

        trial_squads: list[str] = []

        try:
            from app.database.crud.server_squad import get_effective_tariff_squad_uuids

            trial_squads = await get_effective_tariff_squad_uuids(db, None)
        except Exception as error:
            logger.error('Не удалось подобрать сквад при выдаче подписки админом', admin_id=admin_id, error=error)

        forced_devices = None
        if not settings.is_devices_selection_enabled():
            forced_devices = settings.get_disabled_mode_device_limit()

        device_limit = settings.DEFAULT_DEVICE_LIMIT
        if forced_devices is not None:
            device_limit = forced_devices

        subscription = await create_paid_subscription(
            db=db,
            user_id=user_id,
            duration_days=days,
            traffic_limit_gb=settings.DEFAULT_TRAFFIC_LIMIT_GB,
            device_limit=device_limit,
            connected_squads=trial_squads,
            update_server_counters=True,
        )

        subscription_service = SubscriptionService()
        await subscription_service.create_remnawave_user(db, subscription)

        logger.info('Админ выдал платную подписку на дней пользователю', admin_id=admin_id, days=days, user_id=user_id)
        return True

    except Exception as e:
        logger.error('Ошибка выдачи платной подписки', error=e)
        return False


async def _calculate_subscription_period_price(
    db: AsyncSession,
    target_user: User,
    subscription: Subscription,
    period_days: int,
    subscription_service: SubscriptionService | None = None,
) -> int:
    """Рассчитывает стоимость подписки для администратора с учётом всех параметров."""
    from app.services.pricing_engine import pricing_engine

    # Загружаем тариф для корректного расчёта в тарифном режиме
    if subscription.tariff_id:
        try:
            await db.refresh(subscription, ['tariff'])
        except Exception as e:
            logger.warning('Не удалось загрузить тариф для расчёта цены', error=e)

    pricing = await pricing_engine.calculate_renewal_price(
        db,
        subscription,
        period_days,
        user=target_user,
    )
    return pricing.final_total


@admin_required
@error_handler
async def cleanup_inactive_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', None))
    user_service = UserService()
    deleted_count, skipped_count = await user_service.cleanup_inactive_users(db)

    text = texts.t(
        'ADMIN_USERS_PURCHASE_000', '✅ Очистка завершена\n\nУдалено неактивных пользователей: {value1}'
    ).format(value1=deleted_count)
    if skipped_count > 0:
        text += texts.t('ADMIN_USERS_PURCHASE_036', '\n⏭️ Пропущено (активная подписка): {value1}').format(
            value1=skipped_count
        )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data='admin_users'
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def change_subscription_type(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', None))
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    _sid = f'_s{subscription_id}' if subscription_id else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(
            texts.t('ADMIN_USERS_LIMITS_020', '❌ Пользователь или подписка не найдены'), show_alert=True
        )
        return

    if subscription_id and settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_subscription_by_id_for_user

        subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    else:
        subscription = profile['subscription']

    if not subscription:
        await callback.answer(
            texts.t('ADMIN_USERS_LIMITS_020', '❌ Пользователь или подписка не найдены'), show_alert=True
        )
        return

    current_type = (
        texts.t('ADMIN_USERS_PURCHASE_037', '🎁 Триал')
        if subscription.is_trial
        else texts.t('ADMIN_USERS_PURCHASE_038', '💎 Платная')
    )

    text = texts.t('ADMIN_USERS_PURCHASE_001', '🔄 <b>Смена типа подписки</b>\n\n')
    text += f'👤 {html.escape(profile["user"].full_name)}\n'
    text += texts.t('ADMIN_USERS_PURCHASE_002', '📱 Текущий тип: {value1}\n\n').format(value1=current_type)
    text += texts.t('ADMIN_USERS_PURCHASE_003', 'Выберите новый тип подписки:')

    keyboard = []

    if subscription.is_trial:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PURCHASE_108', '💎 Сделать платной'),
                    callback_data=f'admin_sub_type_paid_{user_id}{_sid}',
                )
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PURCHASE_109', '🎁 Сделать триальной'),
                    callback_data=f'admin_sub_type_trial_{user_id}{_sid}',
                )
            ]
        )

    keyboard.append([InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data=back_cb)])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


async def _admin_buy_periods(db: AsyncSession, subscription: Subscription) -> list[int] | None:
    """Use the selected tariff's periods; None requires an explicit tariff choice."""
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        return tariff.get_available_periods() if tariff else None
    if settings.is_tariffs_mode():
        return None
    return settings.get_available_subscription_periods()


def _parse_admin_buy_callback(callback_data: str) -> tuple[int, int, int | None, int | None]:
    """Keep legacy callbacks valid while retaining an explicitly pinned subscription."""
    parts = callback_data.split('_')
    subscription_id = None
    if parts[-1].startswith('s'):
        subscription_id = int(parts.pop()[1:])
    return int(parts[4]), int(parts[5]), int(parts[6]) if len(parts) > 6 else None, subscription_id


@admin_required
@error_handler
async def admin_buy_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', None))
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    _sid = f'_s{subscription_id}' if subscription_id is not None else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    target_user = profile['user']

    if subscription_id is not None:
        from app.database.crud.subscription import get_subscription_by_id_for_user

        subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    else:
        subscription = profile['subscription']

    if not subscription:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_063', '❌ У пользователя нет подписки'), show_alert=True)
        return

    available_periods = await _admin_buy_periods(db, subscription)
    if available_periods is None:
        await callback.answer(
            texts.t('ADMIN_USERS_PURCHASE_064', '❌ Сначала выберите тариф для подписки'), show_alert=True
        )
        return

    subscription_service = SubscriptionService()
    period_buttons = []

    for period in available_periods:
        try:
            price_kopeks = await _calculate_subscription_period_price(
                db,
                target_user,
                subscription,
                period,
                subscription_service=subscription_service,
            )
        except Exception as e:
            logger.error(
                'Ошибка расчёта стоимости подписки для пользователя и периода дней',
                telegram_id=target_user.telegram_id,
                period=period,
                e=e,
            )
            continue

        period_buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PURCHASE_110', '{value1} дней ({value2})').format(
                        value1=period, value2=settings.format_price(price_kopeks)
                    ),
                    callback_data=f'admin_buy_sub_confirm_{user_id}_{period}_{price_kopeks}{_sid}',
                )
            ]
        )

    if not period_buttons:
        await callback.answer(
            texts.t('ADMIN_USERS_PURCHASE_065', '❌ Не удалось рассчитать стоимость подписки'), show_alert=True
        )
        return

    period_buttons.append(
        [types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIMITS_023', '❌ Отмена'), callback_data=back_cb)]
    )

    text = texts.t('ADMIN_USERS_PURCHASE_004', '💳 <b>Покупка подписки для пользователя</b>\n\n')
    target_user_link = user_html_link(target_user)
    target_user_id_display = target_user.telegram_id or target_user.email or f'#{target_user.id}'
    text += f'👤 {target_user_link} (ID: {target_user_id_display})\n'
    text += texts.t('ADMIN_USERS_PURCHASE_005', '💰 Баланс пользователя: {value1}\n\n').format(
        value1=settings.format_price(target_user.balance_kopeks)
    )
    traffic_text = (
        texts.t('ADMIN_USERS_PURCHASE_039', 'Безлимит')
        if (subscription.traffic_limit_gb or 0) <= 0
        else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=subscription.traffic_limit_gb)
    )
    devices_limit = subscription.device_limit
    if devices_limit is None:
        devices_limit = settings.DEFAULT_DEVICE_LIMIT
    servers_count = len(subscription.connected_squads or [])
    text += texts.t('ADMIN_USERS_PURCHASE_006', '📶 Трафик: {value1}\n').format(value1=traffic_text)
    text += texts.t('ADMIN_USERS_PURCHASE_007', '📱 Устройства: {value1}\n').format(
        value1=Texts.format_device_limit(devices_limit)
    )
    text += texts.t('ADMIN_USERS_PURCHASE_008', '🌐 Серверов: {value1}\n\n').format(value1=servers_count)
    text += texts.t('ADMIN_USERS_PURCHASE_009', 'Выберите период подписки:\n')

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=period_buttons))
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_subscription_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', None))
    user_id, period_days, price_kopeks_from_callback, subscription_id = _parse_admin_buy_callback(callback.data)
    _sid = f'_s{subscription_id}' if subscription_id is not None else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id is not None
        else f'admin_user_subscription_{user_id}'
    )

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    target_user = profile['user']
    if subscription_id is not None:
        from app.database.crud.subscription import get_subscription_by_id_for_user

        subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    else:
        subscription = profile['subscription']

    if not subscription:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_063', '❌ У пользователя нет подписки'), show_alert=True)
        return

    available_periods = await _admin_buy_periods(db, subscription)
    if available_periods is None:
        await callback.answer(
            texts.t('ADMIN_USERS_PURCHASE_064', '❌ Сначала выберите тариф для подписки'), show_alert=True
        )
        return
    if period_days not in available_periods:
        await callback.answer(
            texts.t('ADMIN_USERS_PURCHASE_066', '❌ Этот период больше недоступен. Выберите период заново'),
            show_alert=True,
        )
        return

    subscription_service = SubscriptionService()

    try:
        price_kopeks = await _calculate_subscription_period_price(
            db,
            target_user,
            subscription,
            period_days,
            subscription_service=subscription_service,
        )
    except Exception as e:
        logger.error(
            'Ошибка расчёта стоимости подписки при подтверждении админом для пользователя',
            telegram_id=target_user.telegram_id,
            e=e,
        )
        await callback.answer(
            texts.t('ADMIN_USERS_PURCHASE_065', '❌ Не удалось рассчитать стоимость подписки'), show_alert=True
        )
        return

    if price_kopeks_from_callback is not None and price_kopeks_from_callback != price_kopeks:
        logger.info(
            'Стоимость подписки для пользователя изменилась с до при подтверждении',
            telegram_id=target_user.telegram_id,
            price_kopeks_from_callback=price_kopeks_from_callback,
            price_kopeks=price_kopeks,
        )

    if target_user.balance_kopeks < price_kopeks:
        missing_kopeks = price_kopeks - target_user.balance_kopeks
        # Без округления — иначе при не хватке <50 копеек все три суммы покажутся
        # одинаковыми и админ увидит «не хватает 0 ₽».
        await callback.message.edit_text(
            texts.t(
                'ADMIN_USERS_PURCHASE_067',
                '❌ Недостаточно средств на балансе пользователя\n\n💰 Баланс пользователя: {value1}\n💳 Стоимость подписки: {value2}\n📉 Не хватает: {value3}\n\nПополните баланс пользователя перед покупкой.',
            ).format(
                value1=settings.format_price(target_user.balance_kopeks, round_kopeks=False),
                value2=settings.format_price(price_kopeks, round_kopeks=False),
                value3=settings.format_price(missing_kopeks, round_kopeks=False),
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_PURCHASE_116', '⬅️ Назад к подписке'), callback_data=back_cb
                        )
                    ]
                ]
            ),
        )
        await callback.answer()
        return

    text = texts.t('ADMIN_USERS_PURCHASE_010', '💳 <b>Подтверждение покупки подписки</b>\n\n')
    target_user_link = user_html_link(target_user)
    target_user_id_display = target_user.telegram_id or target_user.email or f'#{target_user.id}'
    text += f'👤 {target_user_link} (ID: {target_user_id_display})\n'
    text += texts.t('ADMIN_USERS_PURCHASE_011', '📅 Период подписки: {value1} дней\n').format(value1=period_days)
    text += texts.t('ADMIN_USERS_PURCHASE_012', '💰 Стоимость: {value1}\n').format(
        value1=settings.format_price(price_kopeks)
    )
    text += texts.t('ADMIN_USERS_PURCHASE_005', '💰 Баланс пользователя: {value1}\n\n').format(
        value1=settings.format_price(target_user.balance_kopeks)
    )
    traffic_text = (
        texts.t('ADMIN_USERS_PURCHASE_039', 'Безлимит')
        if (subscription.traffic_limit_gb or 0) <= 0
        else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=subscription.traffic_limit_gb)
    )
    devices_limit = subscription.device_limit
    if devices_limit is None:
        devices_limit = settings.DEFAULT_DEVICE_LIMIT
    servers_count = len(subscription.connected_squads or [])
    text += texts.t('ADMIN_USERS_PURCHASE_006', '📶 Трафик: {value1}\n').format(value1=traffic_text)
    text += texts.t('ADMIN_USERS_PURCHASE_007', '📱 Устройства: {value1}\n').format(
        value1=Texts.format_device_limit(devices_limit)
    )
    text += texts.t('ADMIN_USERS_PURCHASE_008', '🌐 Серверов: {value1}\n\n').format(value1=servers_count)
    text += texts.t('ADMIN_USERS_PURCHASE_013', 'Вы уверены, что хотите купить подписку для этого пользователя?')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_BLOCKED_BUTTON_CONFIRM', '✅ Подтвердить'),
                callback_data=f'admin_buy_sub_execute_{user_id}_{period_days}_{price_kopeks}{_sid}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_LIMITS_023', '❌ Отмена'), callback_data=f'admin_sub_buy_{user_id}{_sid}'
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_subscription_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', None))
    user_id, period_days, price_kopeks_from_callback, subscription_id = _parse_admin_buy_callback(callback.data)
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id is not None
        else f'admin_user_subscription_{user_id}'
    )

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    target_user = profile['user']
    if subscription_id is not None:
        from app.database.crud.subscription import get_subscription_by_id_for_user

        subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    else:
        subscription = profile['subscription']

    if not subscription:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_063', '❌ У пользователя нет подписки'), show_alert=True)
        return

    available_periods = await _admin_buy_periods(db, subscription)
    if available_periods is None:
        await callback.answer(
            texts.t('ADMIN_USERS_PURCHASE_064', '❌ Сначала выберите тариф для подписки'), show_alert=True
        )
        return
    if period_days not in available_periods:
        await callback.answer(
            texts.t('ADMIN_USERS_PURCHASE_066', '❌ Этот период больше недоступен. Выберите период заново'),
            show_alert=True,
        )
        return

    subscription_service = SubscriptionService()

    # TOCTOU protection: lock user row before pricing to prevent concurrent balance modifications
    import asyncio

    from app.database.crud.user import lock_user_for_pricing

    try:
        target_user = await lock_user_for_pricing(db, target_user.id)

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        locked_subscription_id = subscription_id if subscription_id is not None else subscription.id
        managed_purchase = await subscription_service._persisted_traffic_mode(db, subscription) in {
            'managed',
            'migrating',
        }
        if managed_purchase:
            from app.services.traffic_accounting_service import lock_accounting_subscription

            subscription, grace_open = await lock_accounting_subscription(db, locked_subscription_id)
            if grace_open or subscription.traffic_accounting_mode != 'managed':
                await db.rollback()
                await callback.answer(
                    texts.t(
                        'ADMIN_USERS_PURCHASE_111', '❌ Дождитесь завершения временного доступа или обновления трафика'
                    ),
                    show_alert=True,
                )
                return
        result = await db.execute(
            select(Subscription)
            .where(Subscription.id == locked_subscription_id, Subscription.user_id == user_id)
            .options(selectinload(Subscription.tariff))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        subscription = result.scalar_one_or_none()
        if subscription is None:
            await db.rollback()
            await callback.answer(
                texts.t('ADMIN_USERS_PURCHASE_089', '❌ У пользователя нет выбранной подписки'), show_alert=True
            )
            return
        from app.services.cispay_recurrent_guard import require_no_cispay

        try:
            await require_no_cispay(db, user_id=user_id, subscription_id=subscription.id)
        except ValueError as error:
            await db.rollback()
            await callback.answer(str(error), show_alert=True)
            return
        available_periods = await _admin_buy_periods(db, subscription)
        if available_periods is None or period_days not in available_periods:
            await db.rollback()
            await callback.answer(
                texts.t('ADMIN_USERS_PURCHASE_090', '❌ Период или тариф больше недоступен. Выберите заново'),
                show_alert=True,
            )
            return

        try:
            price_kopeks = await _calculate_subscription_period_price(
                db,
                target_user,
                subscription,
                period_days,
                subscription_service=subscription_service,
            )
        except Exception as e:
            logger.error(
                'Ошибка расчёта стоимости подписки при списании средств админом для пользователя',
                telegram_id=target_user.telegram_id,
                e=e,
            )
            await db.rollback()
            await callback.answer(
                texts.t('ADMIN_USERS_PURCHASE_065', '❌ Не удалось рассчитать стоимость подписки'), show_alert=True
            )
            return

        if price_kopeks_from_callback is not None and price_kopeks_from_callback != price_kopeks:
            await db.rollback()
            await callback.answer(
                texts.t(
                    'ADMIN_USERS_PURCHASE_091',
                    '❌ Стоимость изменилась. Выберите период заново и подтвердите новую цену',
                ),
                show_alert=True,
            )
            return

        if target_user.balance_kopeks < price_kopeks:
            await db.rollback()
            await callback.answer(
                texts.t('ADMIN_USERS_PURCHASE_092', '❌ Недостаточно средств на балансе пользователя'), show_alert=True
            )
            return

        from app.database.crud.user import subtract_user_balance

        success = await subtract_user_balance(
            db,
            target_user,
            price_kopeks,
            f'Покупка подписки на {period_days} дней (администратор)',
            mark_as_paid_subscription=True,
            commit=False,
        )

        if not success:
            await db.rollback()
            await callback.answer(texts.t('ADMIN_USERS_PURCHASE_093', '❌ Ошибка списания средств'), show_alert=True)
            return

        if subscription:
            # Оверлей грейса, осевший в подписке (v4.10–4.11), — не её срок: вернуть до расчёта.
            from app.services.grace_access_echo import undo_grace_overlay_echo

            await undo_grace_overlay_echo(db, subscription)
            current_time = datetime.now(UTC)
            bonus_period = timedelta()

            from app.database.crud.subscription import should_carry_trial_remaining_days

            if subscription.is_trial and should_carry_trial_remaining_days() and subscription.end_date:
                remaining_trial_delta = subscription.end_date - current_time
                if remaining_trial_delta.total_seconds() > 0:
                    bonus_period = remaining_trial_delta
                    logger.info(
                        'Админ продлевает подписку: добавляем оставшееся время триала пользователю',
                        bonus_period=bonus_period,
                        telegram_id=target_user.telegram_id,
                    )

            extension_base_date = current_time
            if not subscription.is_trial and subscription.end_date and subscription.end_date > current_time:
                extension_base_date = subscription.end_date
            else:
                subscription.start_date = current_time

            subscription.end_date = extension_base_date + timedelta(days=period_days) + bonus_period
            subscription.pending_panel_expire_at = subscription.end_date
            subscription.status = SubscriptionStatus.ACTIVE.value
            subscription.updated_at = current_time

            if subscription.is_trial or not subscription.is_active:
                was_trial = subscription.is_trial
                subscription.is_trial = False
                if subscription.tariff_id is None:
                    if subscription.traffic_limit_gb != 0:
                        subscription.traffic_limit_gb = 0
                    subscription.device_limit = settings.DEFAULT_DEVICE_LIMIT
                if was_trial:
                    subscription.traffic_used_gb = 0.0

            from app.database.crud.transaction import create_transaction, emit_transaction_side_effects

            transaction = await create_transaction(
                db=db,
                user_id=target_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=price_kopeks,
                description=f'Продление подписки на {period_days} дней (администратор)',
                commit=False,
            )
            if managed_purchase:
                from app.services.traffic_accounting_sync import stage_managed_sync
                from app.services.traffic_base_grant_service import stage_paid_base_grant

                # A paid same-limit purchase also re-arms a deliberately
                # retired account. Resize preserves spent quota; it is not a reset.
                await stage_paid_base_grant(
                    db, subscription, operation_key=f'admin:buy:{transaction.id}', reset_base=False
                )
                await stage_managed_sync(db, subscription)
            await db.commit()
            await db.refresh(subscription)
            from app.database.models import PaymentMethod

            await emit_transaction_side_effects(
                db,
                transaction,
                amount_kopeks=price_kopeks,
                user_id=user_id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                payment_method=PaymentMethod.BALANCE,
                description=f'Продление подписки на {period_days} дней (администратор)',
            )

            try:
                from app.services.panel_sync import push_subscription
                from app.services.remnawave_service import RemnaWaveService

                remnawave_service = RemnaWaveService()

                # Тариф нужен сервису для стратегии сброса трафика и внешнего
                # сквада: без предзагрузки обращение к нему упадёт в async-контексте.
                try:
                    await db.refresh(subscription, ['tariff'])
                except Exception:
                    pass

                async with remnawave_service.get_api_client() as api:
                    from app.services.grace_access_runtime import (
                        create_panel_user_grace_safe,
                        update_panel_user_grace_safe,
                    )

                    result = await push_subscription(
                        api,
                        target_user,
                        subscription,
                        db=db,
                        update_call=lambda **kwargs: update_panel_user_grace_safe(api, subscription.id, **kwargs),
                        create_call=lambda **kwargs: create_panel_user_grace_safe(
                            api,
                            subscription.id,
                            adopt_short_uuid=subscription.remnawave_short_uuid,
                            **kwargs,
                        ),
                    )
                    remnawave_user = result.panel_user
                    # Идентичность обязана сохраниться: без неё следующий админский
                    # extend снова уйдёт в create-ветку и наплодит дублей в панели.
                    await db.commit()

                if remnawave_user:
                    logger.info('Пользователь успешно обновлен в RemnaWave', telegram_id=target_user.telegram_id)
                else:
                    logger.error('Ошибка обновления пользователя в RemnaWave', telegram_id=target_user.telegram_id)
            except Exception as e:
                logger.error('Ошибка работы с RemnaWave для пользователя', telegram_id=target_user.telegram_id, error=e)

            message = texts.t('ADMIN_USERS_PURCHASE_052', '✅ Подписка пользователя продлена на {value1} дней').format(
                value1=period_days
            )
        else:
            message = texts.t('ADMIN_USERS_PURCHASE_053', '❌ Ошибка: у пользователя нет существующей подписки')

        target_user_link = user_html_link(target_user)
        target_user_id_display = target_user.telegram_id or target_user.email or f'#{target_user.id}'
        await callback.message.edit_text(
            texts.t(
                'ADMIN_USERS_PURCHASE_068',
                '{value1}\n\n👤 {value2} (ID: {value3})\n💰 Списано: {value4}\n📅 Подписка действительна до: {value5}',
            ).format(
                value1=message,
                value2=target_user_link,
                value3=target_user_id_display,
                value4=settings.format_price(price_kopeks),
                value5=format_datetime(subscription.end_date),
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_PURCHASE_116', '⬅️ Назад к подписке'), callback_data=back_cb
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )

        try:
            if callback.bot and target_user.telegram_id and settings.is_notifications_enabled():
                recipient_texts = get_texts(getattr(target_user, 'language', None))
                tariff_line = ''
                if settings.is_multi_tariff_enabled() and getattr(subscription, 'tariff', None):
                    tariff_line = recipient_texts.t('ADMIN_USERS_PURCHASE_094', '\n📦 Тариф: «{value1}»').format(
                        value1=subscription.tariff.name
                    )
                await callback.bot.send_message(
                    chat_id=target_user.telegram_id,
                    text=recipient_texts.t(
                        'ADMIN_USERS_PURCHASE_114',
                        '💳 <b>Администратор продлил вашу подписку</b>\n\n📅 Подписка продлена на {value1} дней\n💰 Списано с баланса: {value2}\n📅 Подписка действительна до: {value3}{value4}',
                    ).format(
                        value1=period_days,
                        value2=settings.format_price(price_kopeks),
                        value3=format_datetime(subscription.end_date),
                        value4=tariff_line,
                    ),
                    parse_mode='HTML',
                )
        except Exception as e:
            user_id_display = target_user.telegram_id or target_user.email or f'#{target_user.id}'
            logger.error('Ошибка отправки уведомления пользователю', user_id_display=user_id_display, error=e)

        await callback.answer()

    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as e:
        logger.error('Ошибка покупки подписки администратором', error=e)
        await db.rollback()
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_095', '❌ Ошибка при покупке подписки'), show_alert=True)


# ==================== Покупка тарифа администратором ====================


@admin_required
@error_handler
async def admin_buy_tariff(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает список тарифов для покупки админом."""
    texts = get_texts(getattr(db_user, 'language', None))
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    target_user = profile['user']

    # Получаем доступные тарифы
    from app.database.crud.tariff import get_tariffs_for_user

    tariffs = await get_tariffs_for_user(db, target_user)

    if not tariffs:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_USERS_PURCHASE_069',
                '❌ <b>Нет доступных тарифов</b>\n\nСоздайте тарифы в разделе управления тарифами.',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data=back_cb
                        )
                    ]
                ]
            ),
        )
        await callback.answer()
        return

    target_user_link = user_html_link(target_user)
    target_user_id_display = target_user.telegram_id or target_user.email or f'#{target_user.id}'
    text = texts.t('ADMIN_USERS_PURCHASE_014', '💳 <b>Покупка тарифа для пользователя</b>\n\n')
    text += f'👤 {target_user_link} (ID: {target_user_id_display})\n'
    text += texts.t('ADMIN_USERS_PURCHASE_015', '💰 Баланс: {value1}\n\n').format(
        value1=settings.format_price(target_user.balance_kopeks)
    )
    text += texts.t('ADMIN_USERS_PURCHASE_016', '📦 <b>Выберите тариф:</b>\n\n')

    for tariff in tariffs:
        traffic = (
            '♾️'
            if tariff.traffic_limit_gb == 0
            else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=tariff.traffic_limit_gb)
        )
        prices = tariff.period_prices or {}
        min_price = min(prices.values()) if prices else 0
        text += texts.t('ADMIN_USERS_PURCHASE_041', '<b>{value1}</b> — {value2} / {value3} 📱 от {value4}\n').format(
            value1=html.escape(tariff.name),
            value2=traffic,
            value3=tariff.device_limit,
            value4=settings.format_price(min_price),
        )

    keyboard = []
    for tariff in tariffs:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=tariff.name, callback_data=f'admin_tariff_buy_select_{user_id}_{tariff.id}'
                )
            ]
        )

    keyboard.append(
        [types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data=back_cb)]
    )

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


async def _admin_tariff_purchase_quote(db, user, tariff, period):
    """Use the same target, paid device limit and price for preview and execution."""
    from app.services.pricing_engine import pricing_engine

    if (tariff.is_daily and period != 1) or not tariff.has_configured_price_for_period(period):
        raise ValueError('Tariff period is unavailable')
    subscription = await _resolve_admin_subscription(db, user.id, tariff_id=tariff.id)
    if subscription is None and settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_subscription_by_user_and_tariff

        subscription = await get_subscription_by_user_and_tariff(db, user.id, tariff.id, include_inactive=True)
    same_tariff = subscription is not None and subscription.tariff_id == tariff.id
    device_limit = subscription.device_limit if same_tariff else tariff.device_limit
    pricing = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period,
        device_limit=device_limit,
        user=user,
        **(
            {'daily_devices_billing_version': 1}
            if same_tariff and getattr(subscription, 'daily_devices_billing_version', 0) == 1
            else {}
        ),
    )
    return subscription, device_limit, pricing


@admin_required
@error_handler
async def admin_buy_tariff_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает выбор периода для тарифа."""
    texts = get_texts(getattr(db_user, 'language', None))
    parts = callback.data.split('_')
    user_id = int(parts[4])
    tariff_id = int(parts[5])

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    target_user = profile['user']

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_070', '❌ Тариф недоступен'), show_alert=True)
        return

    initial_period = 1 if tariff.is_daily else next(iter(tariff.period_prices or {}), None)
    device_limit = tariff.device_limit
    if initial_period is not None:
        _, device_limit, _ = await _admin_tariff_purchase_quote(db, target_user, tariff, int(initial_period))
    target_user_link = user_html_link(target_user)
    target_user_id_display = target_user.telegram_id or target_user.email or f'#{target_user.id}'
    traffic = (
        texts.t('ADMIN_USERS_LIMITS_037', '♾️ Безлимит')
        if tariff.traffic_limit_gb == 0
        else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=tariff.traffic_limit_gb)
    )

    text = texts.t('ADMIN_USERS_PURCHASE_014', '💳 <b>Покупка тарифа для пользователя</b>\n\n')
    text += f'👤 {target_user_link} (ID: {target_user_id_display})\n'
    text += texts.t('ADMIN_USERS_PURCHASE_015', '💰 Баланс: {value1}\n\n').format(
        value1=settings.format_price(target_user.balance_kopeks)
    )
    text += texts.t('ADMIN_USERS_PURCHASE_017', '📦 <b>Тариф: {value1}</b>\n').format(value1=html.escape(tariff.name))
    text += texts.t('ADMIN_USERS_PURCHASE_018', '📊 Трафик: {value1}\n').format(value1=traffic)
    text += texts.t('ADMIN_USERS_PURCHASE_019', '📱 Устройств: {value1}\n').format(
        value1=Texts.format_device_limit(device_limit)
    )
    text += texts.t('ADMIN_USERS_PURCHASE_008', '🌐 Серверов: {value1}\n\n').format(
        value1=len(tariff.allowed_squads) if tariff.allowed_squads else 0
    )
    text += texts.t('ADMIN_USERS_PURCHASE_020', 'Выберите период:')

    prices = {'1': tariff.daily_price_kopeks} if tariff.is_daily else (tariff.period_prices or {})
    keyboard = []

    for period_str, price in sorted(prices.items(), key=lambda x: int(x[0])):
        period = int(period_str)
        _, device_limit, pricing = await _admin_tariff_purchase_quote(db, target_user, tariff, period)
        price = pricing.final_total
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PURCHASE_112', '{value1} дней — {value2}').format(
                        value1=period, value2=settings.format_price(price)
                    ),
                    callback_data=f'admin_tariff_buy_confirm_{user_id}_{tariff_id}_{period}_{price}',
                )
            ]
        )

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_PURCHASE_096', '⬅️ К тарифам'), callback_data=f'admin_tariff_buy_{user_id}'
            )
        ]
    )

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_tariff_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Подтверждение покупки тарифа."""
    texts = get_texts(getattr(db_user, 'language', None))
    parts = callback.data.split('_')
    user_id = int(parts[4])
    tariff_id = int(parts[5])
    period = int(parts[6])
    price_kopeks = int(parts[7])

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    target_user = profile['user']

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_070', '❌ Тариф недоступен'), show_alert=True)
        return

    try:
        _, device_limit, pricing = await _admin_tariff_purchase_quote(db, target_user, tariff, period)
        price_kopeks = pricing.final_total
    except ValueError:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_097', 'Период тарифа недоступен'), show_alert=True)
        return

    # Проверяем баланс
    if target_user.balance_kopeks < price_kopeks:
        missing = price_kopeks - target_user.balance_kopeks
        await callback.message.edit_text(
            texts.t(
                'ADMIN_USERS_PURCHASE_071',
                '❌ <b>Недостаточно средств</b>\n\n💰 Баланс: {value1}\n💳 Стоимость: {value2}\n📉 Не хватает: {value3}\n\nПополните баланс пользователя перед покупкой.',
            ).format(
                value1=settings.format_price(target_user.balance_kopeks, round_kopeks=False),
                value2=settings.format_price(price_kopeks, round_kopeks=False),
                value3=settings.format_price(missing, round_kopeks=False),
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'),
                            callback_data=f'admin_tariff_buy_select_{user_id}_{tariff_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    target_user_link = user_html_link(target_user)
    target_user_id_display = target_user.telegram_id or target_user.email or f'#{target_user.id}'
    traffic = (
        texts.t('ADMIN_USERS_LIMITS_037', '♾️ Безлимит')
        if tariff.traffic_limit_gb == 0
        else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=tariff.traffic_limit_gb)
    )

    text = texts.t('ADMIN_USERS_PURCHASE_021', '💳 <b>Подтверждение покупки тарифа</b>\n\n')
    text += f'👤 {target_user_link} (ID: {target_user_id_display})\n'
    text += texts.t('ADMIN_USERS_PURCHASE_015', '💰 Баланс: {value1}\n\n').format(
        value1=settings.format_price(target_user.balance_kopeks)
    )
    text += texts.t('ADMIN_USERS_PURCHASE_017', '📦 <b>Тариф: {value1}</b>\n').format(value1=html.escape(tariff.name))
    text += texts.t('ADMIN_USERS_PURCHASE_018', '📊 Трафик: {value1}\n').format(value1=traffic)
    text += texts.t('ADMIN_USERS_PURCHASE_019', '📱 Устройств: {value1}\n').format(
        value1=Texts.format_device_limit(device_limit)
    )
    text += texts.t('ADMIN_USERS_PURCHASE_022', '📅 Период: {value1} дней\n').format(value1=period)
    text += texts.t('ADMIN_USERS_PURCHASE_023', '💰 Стоимость: {value1}\n\n').format(
        value1=settings.format_price(price_kopeks)
    )
    text += texts.t('ADMIN_USERS_PURCHASE_024', 'Подтвердить покупку?')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_BLOCKED_BUTTON_CONFIRM', '✅ Подтвердить'),
                callback_data=f'admin_tariff_buy_exec_{user_id}_{tariff_id}_{period}_{price_kopeks}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_LIMITS_023', '❌ Отмена'),
                callback_data=f'admin_tariff_buy_select_{user_id}_{tariff_id}',
            )
        ],
    ]

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_tariff_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Выполняет покупку тарифа для пользователя."""
    texts = get_texts(getattr(db_user, 'language', None))
    parts = callback.data.split('_')
    user_id = int(parts[4])
    tariff_id = int(parts[5])
    period = int(parts[6])
    price_kopeks_from_callback = int(parts[7])

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    target_user = profile['user']

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_070', '❌ Тариф недоступен'), show_alert=True)
        return

    # TOCTOU protection: lock user row before pricing to prevent concurrent balance modifications
    from app.database.crud.user import lock_user_for_pricing

    target_user = await lock_user_for_pricing(db, target_user.id)

    try:
        existing_subscription, device_limit, result = await _admin_tariff_purchase_quote(
            db, target_user, tariff, period
        )
        price_kopeks = result.final_total
    except Exception as e:
        logger.error(
            'Ошибка расчёта стоимости тарифа при списании средств админом для пользователя',
            telegram_id=target_user.telegram_id,
            e=e,
        )
        await callback.answer(
            texts.t('ADMIN_USERS_PURCHASE_098', '❌ Не удалось рассчитать стоимость тарифа'), show_alert=True
        )
        return

    if price_kopeks_from_callback != price_kopeks:
        logger.info(
            'Стоимость тарифа для пользователя изменилась перед списанием',
            telegram_id=target_user.telegram_id,
            price_kopeks_from_callback=price_kopeks_from_callback,
            price_kopeks=price_kopeks,
        )
        await callback.answer(
            texts.t('ADMIN_USERS_PURCHASE_072', 'Стоимость изменилась. Откройте подтверждение заново.'), show_alert=True
        )
        return

    if target_user.balance_kopeks < price_kopeks:
        await callback.answer(
            texts.t('ADMIN_USERS_PURCHASE_073', '❌ Недостаточно средств на балансе'), show_alert=True
        )
        return

    try:
        from app.database.crud.subscription import (
            create_paid_subscription,
            extend_subscription,
        )
        from app.database.crud.transaction import create_transaction
        from app.database.crud.user import subtract_user_balance
        from app.services.subscription_service import SubscriptionService

        managed_purchase = getattr(existing_subscription, 'traffic_accounting_mode', None) in ('managed', 'migrating')
        # Списываем баланс
        success = await subtract_user_balance(
            db,
            target_user,
            price_kopeks,
            f'Покупка тарифа {tariff.name} на {period} дней (администратор)',
            mark_as_paid_subscription=True,
            commit=not managed_purchase,
        )

        if not success:
            await callback.answer(texts.t('ADMIN_USERS_PURCHASE_093', '❌ Ошибка списания средств'), show_alert=True)
            return

        # Получаем серверы из тарифа
        squads = tariff.allowed_squads or []

        is_tariff_renewal = existing_subscription is not None and existing_subscription.tariff_id == tariff.id
        reset_traffic = settings.RESET_TRAFFIC_ON_PAYMENT
        if is_tariff_renewal:
            from app.services.traffic_reset_policy import should_reset_traffic_on_tariff_purchase

            reset_traffic = should_reset_traffic_on_tariff_purchase(
                is_tariff_change=False, was_trial=existing_subscription.is_trial, paid_kopeks=price_kopeks
            )

        if existing_subscription:
            # Продлеваем существующую подписку
            subscription = await extend_subscription(
                db,
                existing_subscription,
                days=period,
                tariff_id=tariff.id,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=device_limit,
                connected_squads=squads,
                paid_traffic_reset=False,
                reset_used_traffic=reset_traffic,
                commit=not managed_purchase,
            )
        else:
            # Создаем новую подписку
            subscription = await create_paid_subscription(
                db=db,
                user_id=target_user.id,
                duration_days=period,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=device_limit,
                connected_squads=squads,
                tariff_id=tariff.id,
            )

        # Создаем транзакцию
        purchase_transaction = await create_transaction(
            db,
            user_id=target_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price_kopeks,
            description=f'Покупка тарифа {tariff.name} на {period} дней (администратор)',
            commit=not managed_purchase,
        )

        if managed_purchase:
            await db.commit()
            from app.database.crud.transaction import emit_transaction_side_effects

            await emit_transaction_side_effects(
                db,
                purchase_transaction,
                amount_kopeks=price_kopeks,
                user_id=target_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                description=f'Покупка тарифа {tariff.name} на {period} дней (администратор)',
            )

        # Обновляем в Remnawave
        try:
            subscription_service = SubscriptionService()
            await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=reset_traffic,
                reset_reason='покупка тарифа (администратор)',
            )
        except Exception as e:
            logger.error('Ошибка обновления Remnawave', error=e)

        target_user_link = user_html_link(target_user)
        target_user_id_display = target_user.telegram_id or target_user.email or f'#{target_user.id}'
        traffic = (
            texts.t('ADMIN_USERS_LIMITS_037', '♾️ Безлимит')
            if tariff.traffic_limit_gb == 0
            else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=tariff.traffic_limit_gb)
        )

        await callback.message.edit_text(
            texts.t(
                'ADMIN_USERS_PURCHASE_074',
                '✅ <b>Тариф успешно куплен!</b>\n\n👤 {value1} (ID: {value2})\n📦 Тариф: {value3}\n📊 Трафик: {value4}\n📱 Устройств: {value5}\n📅 Период: {value6} дней\n💰 Списано: {value7}\n📅 Действует до: {value8}',
            ).format(
                value1=target_user_link,
                value2=target_user_id_display,
                value3=html.escape(tariff.name),
                value4=traffic,
                value5=Texts.format_device_limit(subscription.device_limit),
                value6=period,
                value7=settings.format_price(price_kopeks),
                value8=format_datetime(subscription.end_date),
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )

        # Уведомляем пользователя
        try:
            if callback.bot and target_user.telegram_id and settings.is_notifications_enabled():
                recipient_texts = get_texts(getattr(target_user, 'language', None))
                await callback.bot.send_message(
                    chat_id=target_user.telegram_id,
                    text=recipient_texts.t(
                        'ADMIN_USERS_PURCHASE_115',
                        '💳 <b>Администратор оформил вам тариф</b>\n\n📦 Тариф: {value1}\n📊 Трафик: {value2}\n📱 Устройств: {value3}\n📅 Период: {value4} дней\n💰 Списано с баланса: {value5}\n📅 Действует до: {value6}',
                    ).format(
                        value1=html.escape(tariff.name),
                        value2=(
                            recipient_texts.t('ADMIN_USERS_LIMITS_037', '♾️ Безлимит')
                            if tariff.traffic_limit_gb == 0
                            else recipient_texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(
                                value1=tariff.traffic_limit_gb
                            )
                        ),
                        value3=Texts.format_device_limit(subscription.device_limit),
                        value4=period,
                        value5=settings.format_price(price_kopeks),
                        value6=format_datetime(subscription.end_date),
                    ),
                    parse_mode='HTML',
                )
        except Exception as e:
            logger.error('Ошибка отправки уведомления пользователю', error=e)

        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_075', '✅ Тариф куплен!'), show_alert=True)

    except Exception as e:
        logger.error('Ошибка покупки тарифа администратором', error=e, exc_info=True)
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_099', '❌ Ошибка при покупке тарифа'), show_alert=True)
        await db.rollback()


@admin_required
@error_handler
async def change_subscription_type_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', None))
    parts = callback.data.split('_')
    # callback: admin_sub_type_{new_type}_{user_id} or admin_sub_type_{new_type}_{user_id}_s{sub_id}
    new_type = parts[3]  # 'paid' or 'trial'
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    success = await _change_subscription_type(db, user_id, new_type, db_user.id, subscription_id)

    if success:
        type_text = (
            texts.t('ADMIN_USERS_PURCHASE_054', 'платной')
            if new_type == 'paid'
            else texts.t('ADMIN_USERS_PURCHASE_055', 'триальной')
        )
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_PURCHASE_076', '✅ Тип подписки успешно изменен на {value1}').format(value1=type_text),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'), callback_data=back_cb
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_PURCHASE_077', '❌ Ошибка изменения типа подписки'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'), callback_data=back_cb
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


async def _change_subscription_type(
    db: AsyncSession, user_id: int, new_type: str, admin_id: int, subscription_id: int | None = None
) -> bool:
    try:
        from app.services.subscription_service import SubscriptionService

        if subscription_id is not None and (subscription_id <= 0 or not settings.is_multi_tariff_enabled()):
            return False
        subscription = await _resolve_admin_subscription(db, user_id, subscription_id)
        if not subscription:
            logger.error('Подписка не найдена для пользователя', user_id=user_id)
            return False

        new_is_trial = new_type == 'trial'

        if subscription.is_trial == new_is_trial:
            logger.info('Тип подписки уже установлен корректно для пользователя', user_id=user_id)
            return True

        old_type = 'триальной' if subscription.is_trial else 'платной'
        new_type_text = 'триальной' if new_is_trial else 'платной'

        was_trial = subscription.is_trial
        subscription.is_trial = new_is_trial
        subscription.updated_at = datetime.now(UTC)

        if not new_is_trial and was_trial:
            user = await get_user_by_id(db, user_id)
            if user:
                user.has_had_paid_subscription = True

        await db.commit()

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        logger.info(
            'Админ изменил тип подписки пользователя',
            admin_id=admin_id,
            user_id=user_id,
            old_type=old_type,
            new_type_text=new_type_text,
        )
        return True

    except Exception as e:
        logger.error('Ошибка изменения типа подписки', error=e)
        await db.rollback()
        return False


# =============================================================================
# Смена тарифа пользователя администратором
# =============================================================================


@admin_required
@error_handler
async def show_admin_tariff_change(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает список доступных тарифов для смены."""
    texts = get_texts(getattr(db_user, 'language', None))
    user_id, subscription_id = _extract_admin_sub_context(callback.data)

    _sid = f'_s{subscription_id}' if subscription_id else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    if subscription_id and settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_subscription_by_id_for_user

        subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    else:
        subscription = await _resolve_admin_subscription(db, user_id)

    if not subscription:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_063', '❌ У пользователя нет подписки'), show_alert=True)
        return

    # Получаем все активные тарифы
    tariffs = await get_all_tariffs(db, include_inactive=False)

    if not tariffs:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_USERS_PURCHASE_069',
                '❌ <b>Нет доступных тарифов</b>\n\nСоздайте тарифы в разделе управления тарифами.',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data=back_cb
                        )
                    ]
                ]
            ),
        )
        await callback.answer()
        return

    # Текущий тариф
    current_tariff = None
    if subscription.tariff_id:
        current_tariff = await get_tariff_by_id(db, subscription.tariff_id)

    text = texts.t('ADMIN_USERS_PURCHASE_025', '📦 <b>Смена тарифа пользователя</b>\n\n')
    user_link = user_html_link(user)
    text += f'👤 {user_link}\n\n'

    if current_tariff:
        text += texts.t('ADMIN_USERS_PURCHASE_043', '<b>Текущий тариф:</b> {value1}\n\n').format(
            value1=html.escape(current_tariff.name)
        )
    else:
        text += texts.t('ADMIN_USERS_PURCHASE_044', '<b>Текущий тариф:</b> не установлен\n\n')

    text += texts.t('ADMIN_USERS_PURCHASE_026', 'Выберите новый тариф:\n')

    keyboard = []
    for tariff in tariffs:
        # Отмечаем текущий тариф
        prefix = '✅ ' if current_tariff and tariff.id == current_tariff.id else ''

        # Описание тарифа
        traffic_str = (
            '♾️'
            if tariff.traffic_limit_gb == 0
            else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=tariff.traffic_limit_gb)
        )
        servers_count = len(tariff.allowed_squads) if tariff.allowed_squads else 0

        button_text = texts.t(
            'ADMIN_USERS_PURCHASE_045', '{value1}{value2} ({value3} устр., {value4}, {value5} серв.)'
        ).format(
            value1=prefix,
            value2=tariff.name,
            value3=Texts.format_device_limit(tariff.device_limit),
            value4=traffic_str,
            value5=servers_count,
        )

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=button_text, callback_data=f'admin_sub_tariff_select_{tariff.id}_{user_id}{_sid}'
                )
            ]
        )

    keyboard.append(
        [types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data=back_cb)]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def select_admin_tariff_change(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Подтверждение выбора тарифа."""
    texts = get_texts(getattr(db_user, 'language', None))
    parts = callback.data.split('_')
    # admin_sub_tariff_select_{tariff_id}_{user_id} or admin_sub_tariff_select_{tariff_id}_{user_id}_s{sub_id}
    user_id, subscription_id = _extract_admin_sub_context(callback.data)
    # tariff_id is always the part right before user_id (before optional subscription_id)
    if subscription_id:
        tariff_id = int(parts[-3])
    else:
        tariff_id = int(parts[-2])

    _sid = f'_s{subscription_id}' if subscription_id else ''
    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_078', '❌ Тариф не найден'), show_alert=True)
        return

    if subscription_id and settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_subscription_by_id_for_user

        subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    else:
        subscription = await _resolve_admin_subscription(db, user_id)

    if not subscription:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_063', '❌ У пользователя нет подписки'), show_alert=True)
        return

    # Проверяем, если это тот же тариф
    if subscription.tariff_id == tariff_id:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_079', 'ℹ️ Этот тариф уже установлен'), show_alert=True)
        return

    traffic_str = (
        '♾️'
        if tariff.traffic_limit_gb == 0
        else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=tariff.traffic_limit_gb)
    )
    servers_count = len(tariff.allowed_squads) if tariff.allowed_squads else 0

    text = texts.t('ADMIN_USERS_PURCHASE_027', '📦 <b>Подтверждение смены тарифа</b>\n\n')
    user_link = user_html_link(user)
    text += f'👤 {user_link}\n\n'
    text += texts.t('ADMIN_USERS_PURCHASE_028', '<b>Новый тариф:</b> {value1}\n').format(
        value1=html.escape(tariff.name)
    )
    text += texts.t('ADMIN_USERS_STATS_011', '• Устройства: {value1}\n').format(
        value1=Texts.format_device_limit(tariff.device_limit)
    )
    text += texts.t('ADMIN_USERS_PURCHASE_030', '• Трафик: {value1}\n').format(value1=traffic_str)
    text += texts.t('ADMIN_USERS_PURCHASE_031', '• Серверы: {value1}\n\n').format(value1=servers_count)
    text += texts.t('ADMIN_USERS_PURCHASE_032', '⚠️ Параметры подписки будут обновлены в соответствии с тарифом.\n')
    text += texts.t('ADMIN_USERS_PURCHASE_033', 'Дата окончания подписки не изменится.')

    confirmation_data = f'admin_sub_tariff_confirm_{tariff_id}_{user_id}{_sid}'
    if getattr(subscription, 'traffic_accounting_mode', None) == 'managed':
        from secrets import token_hex

        confirmation_data = f'astc:{subscription.id}:{tariff_id}:{token_hex(8)}'

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_BLOCKED_BUTTON_CONFIRM', '✅ Подтвердить'), callback_data=confirmation_data
            ),
            types.InlineKeyboardButton(text=texts.t('ADMIN_USERS_LIMITS_023', '❌ Отмена'), callback_data=back_cb),
        ]
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def confirm_admin_tariff_change(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Применяет смену тарифа."""
    texts = get_texts(getattr(db_user, 'language', None))
    confirmation_token = None
    if callback.data.startswith('astc:'):
        from app.database.crud.subscription import get_subscription_by_id

        parts = callback.data.split(':')
        if (
            len(parts) != 4
            or not parts[1].isdigit()
            or not parts[2].isdigit()
            or len(parts[3]) != 16
            or any(c not in '0123456789abcdef' for c in parts[3])
        ):
            await callback.answer(
                texts.t('ADMIN_USERS_PURCHASE_100', 'Откройте подтверждение смены тарифа ещё раз.'), show_alert=True
            )
            return
        subscription_id, tariff_id = int(parts[1]), int(parts[2])
        selected = await get_subscription_by_id(db, subscription_id)
        if selected is None:
            await callback.answer(texts.t('ADMIN_USERS_PURCHASE_101', 'Подписка не найдена.'), show_alert=True)
            return
        user_id = selected.user_id
        confirmation_token = parts[3]
    else:
        parts = callback.data.split('_')
        # admin_sub_tariff_confirm_{tariff_id}_{user_id} or admin_sub_tariff_confirm_{tariff_id}_{user_id}_s{sub_id}
        user_id, subscription_id = _extract_admin_sub_context(callback.data)
        if subscription_id:
            tariff_id = int(parts[-3])
        else:
            tariff_id = int(parts[-2])

    back_cb = (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id
        else f'admin_user_subscription_{user_id}'
    )

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(texts.t('ADMIN_USERS_STATS_043', '❌ Пользователь не найден'), show_alert=True)
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_078', '❌ Тариф не найден'), show_alert=True)
        return

    if subscription_id and (settings.is_multi_tariff_enabled() or confirmation_token):
        from app.database.crud.subscription import get_subscription_by_id_for_user

        subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    else:
        subscription = await _resolve_admin_subscription(db, user_id)

    if not subscription:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_063', '❌ У пользователя нет подписки'), show_alert=True)
        return

    from app.services.cispay_recurrent_guard import require_no_cispay

    try:
        await require_no_cispay(db, user_id=user_id, subscription_id=subscription.id)
    except ValueError as error:
        await db.rollback()
        await callback.answer(str(error), show_alert=True)
        return

    try:
        old_tariff_id = subscription.tariff_id

        # Preserve extra purchased devices above the old tariff's base limit
        from app.database.crud.subscription import calc_device_limit_on_admin_tariff_change
        from app.services.paid_traffic_reset_service import prepare_admin_tariff_accounting
        from app.services.traffic_accounting_service import change_managed_tariff_locked

        subscription, managed_panel = await prepare_admin_tariff_accounting(db, subscription)
        confirmation_key = None
        if managed_panel is not None:
            from hashlib import sha256

            from sqlalchemy import select

            from app.database.models import Transaction

            if not confirmation_token:
                await callback.answer(
                    texts.t('ADMIN_USERS_PURCHASE_100', 'Откройте подтверждение смены тарифа ещё раз.'), show_alert=True
                )
                return
            source = f'{db_user.id}:{subscription.id}:{tariff.id}:{confirmation_token}'
            confirmation_key = f'admin-tariff:{sha256(source.encode()).hexdigest()}'
            previous = await db.scalar(select(Transaction.id).where(Transaction.external_id == confirmation_key))
            if previous is not None or subscription.tariff_id == tariff.id:
                await callback.answer(texts.t('ADMIN_USERS_PURCHASE_113', 'Изменение уже применено.'), show_alert=True)
                return

        old_tariff = await get_tariff_by_id(db, subscription.tariff_id) if subscription.tariff_id else None

        if subscription.tariff_id != tariff.id:
            subscription.daily_devices_billing_version = 0
        subscription.tariff_id = tariff.id
        if managed_panel is not None:
            subscription.tariff = tariff
        subscription.device_limit = calc_device_limit_on_admin_tariff_change(
            current_device_limit=subscription.device_limit,
            old_tariff_device_limit=old_tariff.device_limit if old_tariff else None,
            new_tariff_device_limit=tariff.device_limit,
            max_device_limit=tariff.max_device_limit,
        )

        subscription.traffic_limit_gb = tariff.traffic_limit_gb
        subscription.connected_squads = tariff.allowed_squads or []
        subscription.updated_at = datetime.now(UTC)

        # Сбрасываем докупленный трафик при смене тарифа
        from sqlalchemy import delete as sql_delete

        from app.database.models import TrafficPurchase

        await db.execute(sql_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
        subscription.purchased_traffic_gb = 0
        subscription.traffic_reset_at = None

        # Сброс использованного трафика по админ-настройке
        if settings.RESET_TRAFFIC_ON_TARIFF_SWITCH and managed_panel is None:
            subscription.traffic_used_gb = 0.0

        # Записываем транзакцию о смене тарифа
        from app.database.crud.transaction import create_transaction

        change_transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=0,
            description=f"Смена тарифа администратором на '{tariff.name}'",
            external_id=confirmation_key,
            commit=False,
        )

        if managed_panel is not None:
            await change_managed_tariff_locked(
                db,
                subscription,
                managed_panel,
                operation_key=f'admin-switch:{change_transaction.id}',
                reset_base=settings.RESET_TRAFFIC_ON_TARIFF_SWITCH,
            )

        await db.commit()

        # Синхронизируем с RemnaWave (сброс трафика по админ-настройке)
        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(
            db,
            subscription,
            reset_traffic=settings.RESET_TRAFFIC_ON_TARIFF_SWITCH and managed_panel is None,
            reset_reason='смена тарифа (админ)',
            sync_squads=True,
        )

        logger.info(
            'Админ изменил тариф пользователя',
            db_user_id=db_user.id,
            user_id=user_id,
            old_tariff_id=old_tariff_id,
            tariff_id=tariff_id,
            tariff_name=tariff.name,
        )

        await callback.message.edit_text(
            texts.t(
                'ADMIN_USERS_PURCHASE_080',
                '✅ <b>Тариф успешно изменен</b>\n\nНовый тариф: <b>{value1}</b>\n• Устройства: {value2}\n• Трафик: {value3}\n• Серверы: {value4}',
            ).format(
                value1=html.escape(tariff.name),
                value2=Texts.format_device_limit(subscription.device_limit),
                value3='♾️'
                if tariff.traffic_limit_gb == 0
                else texts.t('ADMIN_USERS_LIMITS_011', '{value1} ГБ').format(value1=tariff.traffic_limit_gb),
                value4=len(tariff.allowed_squads) if tariff.allowed_squads else 0,
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'), callback_data=back_cb
                        )
                    ]
                ]
            ),
        )

    except Exception as e:
        logger.error('Ошибка смены тарифа', error=e)
        await db.rollback()

        await callback.message.edit_text(
            texts.t('ADMIN_USERS_PURCHASE_102', '❌ <b>Ошибка смены тарифа</b>\n\nДетали: {value1}').format(
                value1=html.escape(str(e))
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USERS_SUBACTIONS_046', '📱 К подписке'), callback_data=back_cb
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


async def _load_admin_subscription(db: AsyncSession, user_id: int, subscription_id: int | None) -> Subscription | None:
    """Fetch a subscription for admin actions, respecting multi-tariff mode."""
    if subscription_id and settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_subscription_by_id_for_user

        return await get_subscription_by_id_for_user(db, subscription_id, user_id)

    profile = await UserService().get_user_profile(db, user_id)
    if not profile:
        return None
    return profile.get('subscription')


def _admin_autopay_back_cb(user_id: int, subscription_id: int | None) -> str:
    return (
        f'admin_user_sub_select_{user_id}_{subscription_id}'
        if subscription_id and settings.is_multi_tariff_enabled()
        else f'admin_user_subscription_{user_id}'
    )


def _admin_autopay_available_periods(subscription: Subscription) -> list[int]:
    tariff = getattr(subscription, 'tariff', None)
    if tariff:
        periods = tariff.get_available_periods() or []
        if periods:
            return sorted(periods)
    return sorted(settings.get_available_renewal_periods())


@admin_required
@error_handler
async def show_admin_user_autopay(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Admin view of a subscription's autopay settings."""
    texts = get_texts(getattr(db_user, 'language', None))
    user_id, subscription_id = _extract_admin_sub_context(callback.data)
    subscription = await _load_admin_subscription(db, user_id, subscription_id)
    if subscription is None:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_081', '❌ Подписка не найдена'), show_alert=True)
        return

    try:
        await db.refresh(subscription, ['tariff'])
    except Exception:
        pass

    _sid = f'_s{subscription.id}' if subscription_id and settings.is_multi_tariff_enabled() else ''
    status_text = (
        texts.t('ADMIN_USERS_PURCHASE_046', '✅ Включён')
        if subscription.autopay_enabled
        else texts.t('ADMIN_USERS_PURCHASE_047', '❌ Выключен')
    )
    period_value = getattr(subscription, 'autopay_period_days', None)
    period_text = (
        texts.t('ADMIN_USERS_PURCHASE_048', '{value1} дн.').format(value1=period_value)
        if period_value
        else texts.t('ADMIN_USERS_PURCHASE_049', 'по умолчанию (самый дешёвый период тарифа)')
    )

    text = texts.t(
        'ADMIN_USERS_PURCHASE_034',
        '💳 <b>Настройки автоплатежа</b>\n\n<b>Статус:</b> {value1}\n<b>Списание за:</b> {value2} дн. до окончания\n<b>Период продления:</b> {value3}\n\nГлобальный дефолт периода: {value4}',
    ).format(
        value1=status_text,
        value2=subscription.autopay_days_before,
        value3=period_text,
        value4=getattr(settings, 'DEFAULT_AUTOPAY_PERIOD_DAYS', 0)
        or texts.t('ADMIN_USERS_PURCHASE_082', 'самый короткий период тарифа'),
    )

    toggle_label = (
        texts.t('ADMIN_USERS_PURCHASE_050', '❌ Выключить')
        if subscription.autopay_enabled
        else texts.t('ADMIN_USERS_PURCHASE_051', '✅ Включить')
    )
    keyboard = [
        [types.InlineKeyboardButton(text=toggle_label, callback_data=f'admin_user_autopay_toggle_{user_id}{_sid}')],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_PURCHASE_103', '⏰ Дни до списания'),
                callback_data=f'admin_user_autopay_days_{user_id}{_sid}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_PURCHASE_104', '📅 Период продления'),
                callback_data=f'admin_user_autopay_period_{user_id}{_sid}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_PURCHASE_105', '⬅️ К подписке'),
                callback_data=_admin_autopay_back_cb(user_id, subscription_id),
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def toggle_admin_user_autopay(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Admin: flip autopay_enabled on a subscription."""
    texts = get_texts(getattr(db_user, 'language', None))
    from app.database.crud.subscription import update_subscription_autopay

    user_id, subscription_id = _extract_admin_sub_context(callback.data)
    subscription = await _load_admin_subscription(db, user_id, subscription_id)
    if subscription is None:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_081', '❌ Подписка не найдена'), show_alert=True)
        return

    await update_subscription_autopay(db, subscription, not subscription.autopay_enabled)
    await callback.answer(texts.t('ADMIN_USERS_PURCHASE_056', '✅ Сохранено'))
    refreshed_callback = callback.model_copy(
        update={
            'data': f'admin_user_autopay_{user_id}'
            + (f'_s{subscription_id}' if subscription_id and settings.is_multi_tariff_enabled() else '')
        }
    ).as_(callback.bot)
    await show_admin_user_autopay(refreshed_callback, db_user, db)


@admin_required
@error_handler
async def show_admin_user_autopay_days(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Admin: show days-before picker."""
    texts = get_texts(getattr(db_user, 'language', None))
    user_id, subscription_id = _extract_admin_sub_context(callback.data)
    _sid = f'_s{subscription_id}' if subscription_id and settings.is_multi_tariff_enabled() else ''

    keyboard = []
    for days in [1, 3, 7, 14]:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USERS_PURCHASE_048', '{value1} дн.').format(value1=days),
                    callback_data=f'admin_user_autopay_days_set_{user_id}{_sid}_{days}',
                )
            ]
        )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data=f'admin_user_autopay_{user_id}{_sid}'
            )
        ]
    )

    await callback.message.edit_text(
        texts.t('ADMIN_USERS_PURCHASE_057', '⏰ <b>За сколько дней до окончания списывать средства?</b>'),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def set_admin_user_autopay_days(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Admin: persist days-before choice."""
    texts = get_texts(getattr(db_user, 'language', None))
    from app.database.crud.subscription import update_subscription_autopay

    parts = callback.data.split('_')
    days = int(parts[-1])
    if parts[-2].startswith('s') and parts[-2][1:].isdigit():
        subscription_id = int(parts[-2][1:])
        user_id = int(parts[-3])
    else:
        subscription_id = None
        user_id = int(parts[-2])

    subscription = await _load_admin_subscription(db, user_id, subscription_id)
    if subscription is None:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_081', '❌ Подписка не найдена'), show_alert=True)
        return

    await update_subscription_autopay(db, subscription, subscription.autopay_enabled, days_before=days)
    await callback.answer(texts.t('ADMIN_USERS_PURCHASE_058', '✅ Установлено: {value1} дн.').format(value1=days))

    _sid = f'_s{subscription_id}' if subscription_id and settings.is_multi_tariff_enabled() else ''
    refreshed_callback = callback.model_copy(update={'data': f'admin_user_autopay_{user_id}{_sid}'}).as_(callback.bot)
    await show_admin_user_autopay(refreshed_callback, db_user, db)


@admin_required
@error_handler
async def show_admin_user_autopay_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Admin: show period picker."""
    texts = get_texts(getattr(db_user, 'language', None))
    user_id, subscription_id = _extract_admin_sub_context(callback.data)
    subscription = await _load_admin_subscription(db, user_id, subscription_id)
    if subscription is None:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_081', '❌ Подписка не найдена'), show_alert=True)
        return

    try:
        await db.refresh(subscription, ['tariff'])
    except Exception:
        pass

    _sid = f'_s{subscription_id}' if subscription_id and settings.is_multi_tariff_enabled() else ''
    current = getattr(subscription, 'autopay_period_days', None)
    periods = _admin_autopay_available_periods(subscription)

    default_label = texts.t('ADMIN_USERS_PURCHASE_035', '⚙️ По умолчанию (самый дешёвый)')
    if current is None:
        default_label = f'✅ {default_label}'

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=default_label,
                callback_data=f'admin_user_autopay_period_set_{user_id}{_sid}_0',
            )
        ]
    ]
    for days in periods:
        label = texts.t('ADMIN_USERS_PURCHASE_048', '{value1} дн.').format(value1=days)
        if current == days:
            label = f'✅ {label}'
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=label, callback_data=f'admin_user_autopay_period_set_{user_id}{_sid}_{days}'
                )
            ]
        )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_USERS_LIMITS_032', '⬅️ Назад'), callback_data=f'admin_user_autopay_{user_id}{_sid}'
            )
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'ADMIN_USERS_PURCHASE_059',
            '📅 <b>Период автоплатежа</b>\n\nВыберите период, на который автоплатёж будет продлевать подписку.\n<i>"По умолчанию" — глобальный дефолт из .env, иначе самый дешёвый период тарифа.</i>',
        ),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


@admin_required
@error_handler
async def set_admin_user_autopay_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Admin: persist period choice. Value `0` means clear (use default)."""
    texts = get_texts(getattr(db_user, 'language', None))
    from app.database.crud.subscription import update_subscription_autopay

    parts = callback.data.split('_')
    days = int(parts[-1])
    if parts[-2].startswith('s') and parts[-2][1:].isdigit():
        subscription_id = int(parts[-2][1:])
        user_id = int(parts[-3])
    else:
        subscription_id = None
        user_id = int(parts[-2])

    subscription = await _load_admin_subscription(db, user_id, subscription_id)
    if subscription is None:
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_081', '❌ Подписка не найдена'), show_alert=True)
        return

    if days == 0:
        await update_subscription_autopay(db, subscription, subscription.autopay_enabled, period_days=None)
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_083', '✅ Используется период по умолчанию'))
    else:
        try:
            await db.refresh(subscription, ['tariff'])
        except Exception:
            pass

        available = _admin_autopay_available_periods(subscription)
        if days not in available:
            await callback.answer(
                texts.t('ADMIN_USERS_PURCHASE_106', '❌ Период недоступен для этой подписки'), show_alert=True
            )
            return

        await update_subscription_autopay(db, subscription, subscription.autopay_enabled, period_days=days)
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_084', '✅ Период: {value1} дн.').format(value1=days))

    _sid = f'_s{subscription_id}' if subscription_id and settings.is_multi_tariff_enabled() else ''
    refreshed_callback = callback.model_copy(update={'data': f'admin_user_autopay_{user_id}{_sid}'}).as_(callback.bot)
    await show_admin_user_autopay(refreshed_callback, db_user, db)


@admin_required
@error_handler
async def cancel_admin_cispay(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', None))
    from app.services.cispay_recurrent_service import cancel_cispay_agreement

    parts = callback.data.split(':')
    if len(parts) != 3 or not all(part.isdecimal() for part in parts[1:]):
        await callback.answer(texts.t('ADMIN_USERS_PURCHASE_085', 'Некорректная подписка'), show_alert=True)
        return
    user_id, subscription_id = map(int, parts[1:])
    try:
        result = await cancel_cispay_agreement(db, user_id=user_id, subscription_id=subscription_id, reason='admin')
    except ValueError:
        await callback.answer(texts.t('ADMIN_USERS_SUBACTIONS_022', 'Подписка не найдена'), show_alert=True)
        return
    logger.info(
        'Admin requested CisPay cancellation',
        admin_id=db_user.id,
        user_id=user_id,
        subscription_id=subscription_id,
        operation_status=result['status'],
    )
    await _render_user_subscription_overview(
        callback, db, user_id, subscription_id=subscription_id, language=db_user.language
    )
    await callback.answer(texts.t('ADMIN_USERS_PURCHASE_060', 'Запрос на отключение обработан'))


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(cancel_admin_cispay, F.data.startswith('admin_cisrec_cancel:'))
    dp.callback_query.register(show_users_menu, F.data == 'admin_users')

    dp.callback_query.register(show_users_list, F.data == 'admin_users_list')

    dp.callback_query.register(show_users_statistics, F.data == 'admin_users_stats')

    dp.callback_query.register(show_user_subscription, F.data.startswith('admin_user_subscription_'))

    dp.callback_query.register(admin_select_user_subscription, F.data.startswith('admin_user_sub_select_'))

    dp.callback_query.register(show_user_transactions, F.data.startswith('admin_user_transactions_'))

    dp.callback_query.register(show_user_statistics, F.data.startswith('admin_user_statistics_'))

    dp.callback_query.register(block_user, F.data.startswith('admin_user_block_confirm_'))

    dp.callback_query.register(delete_user_account, F.data.startswith('admin_user_delete_confirm_'))

    dp.callback_query.register(confirm_user_block, F.data.startswith('admin_user_block_') & ~F.data.contains('confirm'))

    dp.callback_query.register(unblock_user, F.data.startswith('admin_user_unblock_confirm_'))

    dp.callback_query.register(
        confirm_user_unblock, F.data.startswith('admin_user_unblock_') & ~F.data.contains('confirm')
    )

    dp.callback_query.register(
        confirm_user_delete, F.data.startswith('admin_user_delete_') & ~F.data.contains('confirm')
    )

    # Регистрация хендлеров ограничений пользователя
    dp.callback_query.register(show_user_restrictions, F.data.startswith('admin_user_restrictions_'))

    dp.callback_query.register(toggle_user_restriction_topup, F.data.startswith('admin_user_restriction_toggle_topup_'))

    dp.callback_query.register(
        toggle_user_restriction_subscription, F.data.startswith('admin_user_restriction_toggle_sub_')
    )

    dp.callback_query.register(ask_restriction_reason, F.data.startswith('admin_user_restriction_reason_'))

    dp.callback_query.register(clear_user_restrictions, F.data.startswith('admin_user_restriction_clear_'))

    dp.message.register(save_restriction_reason, AdminStates.editing_user_restriction_reason)

    dp.callback_query.register(handle_users_list_pagination_fixed, F.data.startswith('admin_users_list_page_'))

    dp.callback_query.register(
        handle_users_balance_list_pagination, F.data.startswith('admin_users_balance_list_page_')
    )

    dp.callback_query.register(
        handle_users_ready_to_renew_pagination, F.data.startswith('admin_users_ready_to_renew_list_page_')
    )

    dp.callback_query.register(
        handle_potential_customers_pagination, F.data.startswith('admin_users_potential_customers_list_page_')
    )

    dp.callback_query.register(
        handle_users_campaign_list_pagination, F.data.startswith('admin_users_campaign_list_page_')
    )

    dp.callback_query.register(start_user_search, F.data == 'admin_users_search')

    dp.message.register(process_user_search, AdminStates.waiting_for_user_search)

    dp.callback_query.register(show_user_management, F.data.startswith('admin_user_manage_'))

    dp.callback_query.register(
        show_user_promo_group,
        F.data.startswith('admin_user_promo_group_') & ~F.data.contains('_set_') & ~F.data.contains('_toggle_'),
    )

    dp.callback_query.register(set_user_promo_group, F.data.startswith('admin_user_promo_group_toggle_'))

    dp.callback_query.register(start_balance_edit, F.data.startswith('admin_user_balance_'))

    dp.message.register(process_balance_edit, AdminStates.editing_user_balance)

    dp.callback_query.register(
        show_user_referrals, F.data.startswith('admin_user_referrals_') & ~F.data.contains('_edit')
    )

    dp.callback_query.register(
        start_edit_referral_percent,
        F.data.startswith('admin_user_referral_percent_') & ~F.data.contains('_set_') & ~F.data.contains('_reset'),
    )

    dp.callback_query.register(
        set_referral_percent_button,
        F.data.startswith('admin_user_referral_percent_set_') | F.data.startswith('admin_user_referral_percent_reset_'),
    )

    dp.message.register(
        process_referral_percent_input,
        AdminStates.editing_user_referral_percent,
    )

    dp.callback_query.register(start_edit_user_referrals, F.data.startswith('admin_user_referrals_edit_'))

    dp.message.register(process_edit_user_referrals, AdminStates.editing_user_referrals)

    dp.callback_query.register(start_send_user_message, F.data.startswith('admin_user_send_message_'))

    dp.message.register(process_send_user_message, AdminStates.sending_user_message)

    dp.callback_query.register(show_inactive_users, F.data == 'admin_users_inactive')

    dp.callback_query.register(cleanup_inactive_users, F.data == 'admin_cleanup_inactive')

    dp.callback_query.register(
        extend_user_subscription,
        F.data.startswith('admin_sub_extend_') & ~F.data.contains('days') & ~F.data.contains('confirm'),
    )

    dp.callback_query.register(process_subscription_extension_days, F.data.startswith('admin_sub_extend_days_'))

    dp.message.register(process_subscription_extension_text, AdminStates.extending_subscription)

    dp.callback_query.register(
        add_subscription_traffic, F.data.startswith('admin_sub_traffic_') & ~F.data.contains('add')
    )

    dp.callback_query.register(process_traffic_addition_button, F.data.startswith('admin_sub_traffic_add_'))

    dp.message.register(process_traffic_addition_text, AdminStates.adding_traffic)

    dp.callback_query.register(
        deactivate_user_subscription, F.data.startswith('admin_sub_deactivate_') & ~F.data.contains('confirm')
    )

    dp.callback_query.register(confirm_subscription_deactivation, F.data.startswith('admin_sub_deactivate_confirm_'))

    dp.callback_query.register(
        reset_user_subscription, F.data.startswith('admin_sub_reset_') & ~F.data.contains('confirm')
    )

    dp.callback_query.register(confirm_subscription_reset, F.data.startswith('admin_sub_reset_confirm_'))

    dp.callback_query.register(activate_user_subscription, F.data.startswith('admin_sub_activate_'))

    dp.callback_query.register(
        delete_user_subscription, F.data.startswith('admin_sub_delete_') & ~F.data.contains('confirm')
    )
    dp.callback_query.register(confirm_subscription_deletion, F.data.startswith('admin_sub_delete_confirm_'))

    dp.callback_query.register(grant_trial_subscription, F.data.startswith('admin_sub_grant_trial_'))

    dp.callback_query.register(
        grant_paid_subscription,
        F.data.startswith('admin_sub_grant_') & ~F.data.contains('trial') & ~F.data.contains('days'),
    )

    dp.callback_query.register(process_subscription_grant_days, F.data.startswith('admin_sub_grant_days_'))

    dp.message.register(process_subscription_grant_text, AdminStates.granting_subscription)

    dp.callback_query.register(show_user_servers_management, F.data.startswith('admin_user_servers_'))

    dp.callback_query.register(show_server_selection, F.data.startswith('admin_user_change_server_'))

    dp.callback_query.register(
        toggle_user_server,
        F.data.startswith('admin_user_toggle_server_') & ~F.data.endswith('_add') & ~F.data.endswith('_remove'),
    )

    dp.callback_query.register(start_devices_edit, F.data.startswith('admin_user_devices_') & ~F.data.contains('set'))

    dp.callback_query.register(set_user_devices_button, F.data.startswith('admin_user_devices_set_'))

    # Смена тарифа пользователя
    dp.callback_query.register(show_admin_tariff_change, F.data.startswith('admin_sub_change_tariff_'))

    dp.callback_query.register(select_admin_tariff_change, F.data.startswith('admin_sub_tariff_select_'))

    dp.callback_query.register(
        confirm_admin_tariff_change, F.data.startswith('admin_sub_tariff_confirm_') | F.data.startswith('astc:')
    )

    dp.message.register(process_devices_edit_text, AdminStates.editing_user_devices)

    dp.callback_query.register(start_traffic_edit, F.data.startswith('admin_user_traffic_') & ~F.data.contains('set'))

    dp.callback_query.register(set_user_traffic_button, F.data.startswith('admin_user_traffic_set_'))

    dp.message.register(process_traffic_edit_text, AdminStates.editing_user_traffic)

    dp.callback_query.register(
        confirm_reset_devices, F.data.startswith('admin_user_reset_devices_') & ~F.data.contains('confirm')
    )

    dp.callback_query.register(reset_user_devices, F.data.startswith('admin_user_reset_devices_confirm_'))

    # Админ — настройки автоплатежа конкретной подписки
    # Порядок важен: более специфичные фильтры регистрируем раньше.
    dp.callback_query.register(set_admin_user_autopay_days, F.data.startswith('admin_user_autopay_days_set_'))
    dp.callback_query.register(set_admin_user_autopay_period, F.data.startswith('admin_user_autopay_period_set_'))
    dp.callback_query.register(toggle_admin_user_autopay, F.data.startswith('admin_user_autopay_toggle_'))
    dp.callback_query.register(
        show_admin_user_autopay_days,
        F.data.startswith('admin_user_autopay_days_') & ~F.data.contains('_set_'),
    )
    dp.callback_query.register(
        show_admin_user_autopay_period,
        F.data.startswith('admin_user_autopay_period_') & ~F.data.contains('_set_'),
    )
    dp.callback_query.register(
        show_admin_user_autopay,
        F.data.startswith('admin_user_autopay_')
        & ~F.data.contains('_toggle_')
        & ~F.data.contains('_days_')
        & ~F.data.contains('_period_'),
    )

    dp.callback_query.register(change_subscription_type, F.data.startswith('admin_sub_change_type_'))

    dp.callback_query.register(change_subscription_type_confirm, F.data.startswith('admin_sub_type_'))

    # Регистрация обработчика покупки подписки администратором
    dp.callback_query.register(admin_buy_subscription, F.data.startswith('admin_sub_buy_'))

    # Регистрация дополнительных обработчиков для покупки подписки
    dp.callback_query.register(admin_buy_subscription_confirm, F.data.startswith('admin_buy_sub_confirm_'))

    dp.callback_query.register(admin_buy_subscription_execute, F.data.startswith('admin_buy_sub_execute_'))

    # Регистрация обработчиков для покупки тарифа администратором
    dp.callback_query.register(
        admin_buy_tariff,
        F.data.startswith('admin_tariff_buy_')
        & ~F.data.startswith('admin_tariff_buy_select_')
        & ~F.data.startswith('admin_tariff_buy_confirm_')
        & ~F.data.startswith('admin_tariff_buy_exec_'),
    )

    dp.callback_query.register(admin_buy_tariff_period, F.data.startswith('admin_tariff_buy_select_'))

    dp.callback_query.register(admin_buy_tariff_confirm, F.data.startswith('admin_tariff_buy_confirm_'))

    dp.callback_query.register(admin_buy_tariff_execute, F.data.startswith('admin_tariff_buy_exec_'))

    # Регистрация обработчиков для фильтрации пользователей
    dp.callback_query.register(show_users_filters, F.data == 'admin_users_filters')

    dp.callback_query.register(show_users_list_by_balance, F.data == 'admin_users_balance_filter')

    dp.callback_query.register(show_users_ready_to_renew, F.data == 'admin_users_ready_to_renew_filter')

    dp.callback_query.register(show_potential_customers, F.data == 'admin_users_potential_customers_filter')

    dp.callback_query.register(show_users_list_by_campaign, F.data == 'admin_users_campaign_filter')
