import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

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
    get_server_ids_by_uuids,
    get_server_squad_by_id,
    get_server_squad_by_uuid,
)
from app.database.crud.tariff import get_all_tariffs, get_tariff_by_id
from app.database.crud.user import (
    get_referrals,
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
from app.localization.texts import get_texts
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.services.user_service import UserService
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime, format_time_ago
from app.utils.subscription_utils import (
    resolve_hwid_device_limit_for_payload,
)
from app.utils.user_utils import get_effective_referral_commission_percent


logger = logging.getLogger(__name__)


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
    title_key: str
    empty_key: str
    pagination_prefix: str
    order_param: str  # параметр для get_users_page


# Конфигурация для каждого типа фильтра
USER_FILTER_CONFIGS: dict[UserFilterType, UserFilterConfig] = {
    UserFilterType.BALANCE: UserFilterConfig(
        fsm_state=AdminStates.viewing_user_from_balance_list,
        title_key='ADMIN_USERS_FILTER_BALANCE_TITLE',
        empty_key='ADMIN_USERS_FILTER_BALANCE_EMPTY',
        pagination_prefix='admin_users_balance_list',
        order_param='order_by_balance',
    ),
    UserFilterType.CAMPAIGN: UserFilterConfig(
        fsm_state=AdminStates.viewing_user_from_campaign_list,
        title_key='ADMIN_USERS_FILTER_CAMPAIGN_TITLE',
        empty_key='ADMIN_USERS_FILTER_CAMPAIGN_EMPTY',
        pagination_prefix='admin_users_campaign_list',
        order_param='',  # использует специальный метод
    ),
    UserFilterType.POTENTIAL_CUSTOMERS: UserFilterConfig(
        fsm_state=AdminStates.viewing_user_from_potential_customers_list,
        title_key='ADMIN_USERS_FILTER_POTENTIAL_CUSTOMERS_TITLE',
        empty_key='ADMIN_USERS_FILTER_POTENTIAL_CUSTOMERS_EMPTY',
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
    if not user.subscription:
        return '❌'
    if user.subscription.is_trial:
        return '🎁'
    if user.subscription.is_active:
        return '💎'
    return '⏰'


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
    status_emoji = _get_user_status_emoji(user)
    sub_emoji = _get_subscription_emoji(user)
    texts = get_texts(language)

    if filter_type == UserFilterType.BALANCE:
        button_text = f'{status_emoji} {sub_emoji} {user.full_name}'
        if user.balance_kopeks > 0:
            button_text += f' | 💰 {settings.format_price(user.balance_kopeks)}'
        if user.subscription and user.subscription.end_date:
            days_left = (user.subscription.end_date - datetime.utcnow()).days
            button_text += f' | 📅 {days_left}{texts.t("ADMIN_USERS_DAYS_SHORT")}'

    elif filter_type == UserFilterType.CAMPAIGN:
        info = extra_data.get(user.id, {}) if extra_data else {}
        campaign_name = info.get('campaign_name') or texts.t('ADMIN_USERS_NO_CAMPAIGN')
        registered_at = info.get('registered_at')
        registered_display = format_datetime(registered_at) if registered_at else texts.t('ADMIN_USERS_UNKNOWN')
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
        await callback.message.edit_text(
            texts.t(config.empty_key), reply_markup=get_admin_users_keyboard(db_user.language)
        )
        await callback.answer()
        return

    # Формируем текст заголовка
    text = texts.t('ADMIN_USERS_PAGE_TITLE').format(
        title=texts.t(config.title_key), page=page, total_pages=users_data['total_pages']
    )
    text += '\n\n' + texts.t('ADMIN_USERS_TAP_USER_FOR_MANAGEMENT')

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
                types.InlineKeyboardButton(text=texts.ADMIN_USERS_SEARCH, callback_data='admin_users_search'),
                types.InlineKeyboardButton(text=texts.ADMIN_USER_STATISTICS, callback_data='admin_users_stats'),
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_users')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_users_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)
    texts = get_texts(db_user.language)
    text = texts.t('ADMIN_USERS_MENU_TEXT').format(
        total_users=stats['total_users'],
        active_users=stats['active_users'],
        blocked_users=stats['blocked_users'],
        new_today=stats['new_today'],
        new_week=stats['new_week'],
        new_month=stats['new_month'],
    )

    await callback.message.edit_text(text, reply_markup=get_admin_users_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_filters(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    text = texts.t('ADMIN_USERS_FILTERS_TEXT')

    await callback.message.edit_text(text, reply_markup=get_admin_users_filters_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_list(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext, page: int = 1
):
    # Сбрасываем состояние, так как мы в обычном списке
    await state.set_state(None)
    texts = get_texts(db_user.language)

    user_service = UserService()
    users_data = await user_service.get_users_page(db, page=page, limit=10)

    if not users_data['users']:
        await callback.message.edit_text(
            texts.t('ADMIN_USERS_LIST_EMPTY'), reply_markup=get_admin_users_keyboard(db_user.language)
        )
        await callback.answer()
        return

    text = texts.t('ADMIN_USERS_LIST_TITLE').format(page=page, total_pages=users_data['total_pages'])
    text += '\n\n' + texts.t('ADMIN_USERS_TAP_USER_FOR_MANAGEMENT')

    keyboard = []

    for user in users_data['users']:
        if user.status == UserStatus.ACTIVE.value:
            status_emoji = '✅'
        elif user.status == UserStatus.BLOCKED.value:
            status_emoji = '🚫'
        else:
            status_emoji = '🗑️'

        subscription_emoji = ''
        if user.subscription:
            if user.subscription.is_trial:
                subscription_emoji = '🎁'
            elif user.subscription.is_active:
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
                types.InlineKeyboardButton(text=texts.ADMIN_USERS_SEARCH, callback_data='admin_users_search'),
                types.InlineKeyboardButton(text=texts.ADMIN_USER_STATISTICS, callback_data='admin_users_stats'),
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_users')],
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
    header = texts.t('ADMIN_USERS_FILTER_RENEW_READY_TITLE')
    description = texts.t('ADMIN_USERS_FILTER_RENEW_READY_DESC').format(amount=amount_text)

    if not users_data['users']:
        empty_text = texts.t('ADMIN_USERS_FILTER_RENEW_READY_EMPTY')
        await callback.message.edit_text(
            f'{header}\n\n{description}\n\n{empty_text}',
            reply_markup=get_admin_users_keyboard(db_user.language),
        )
        await callback.answer()
        return

    text = f'{header}\n\n{description}\n\n'
    text += texts.t('ADMIN_USERS_TAP_USER_FOR_MANAGEMENT')

    keyboard = []
    current_time = datetime.utcnow()

    for user in users_data['users']:
        subscription = user.subscription
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

        button_text = (
            f'{status_emoji} {subscription_emoji} {user.full_name}'
            f' | 💰 {settings.format_price(user.balance_kopeks)}'
            f' | ⏰ {texts.t("ADMIN_USERS_FILTER_RENEW_READY_EXPIRED_SUFFIX").format(days=expired_days)}'
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
                    text=texts.ADMIN_USERS_SEARCH,
                    callback_data='admin_users_search',
                ),
                types.InlineKeyboardButton(
                    text=texts.ADMIN_USER_STATISTICS,
                    callback_data='admin_users_stats',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.BACK,
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
    header = texts.t('ADMIN_USERS_FILTER_POTENTIAL_CUSTOMERS_TITLE')
    description = texts.t('ADMIN_USERS_FILTER_POTENTIAL_CUSTOMERS_DESC').format(amount=amount_text)

    if not users_data['users']:
        empty_text = texts.t('ADMIN_USERS_FILTER_POTENTIAL_CUSTOMERS_EMPTY')
        await callback.message.edit_text(
            f'{header}\n\n{description}\n\n{empty_text}',
            reply_markup=get_admin_users_keyboard(db_user.language),
        )
        await callback.answer()
        return

    text = f'{header}\n\n{description}\n\n'
    text += texts.t('ADMIN_USERS_TAP_USER_FOR_MANAGEMENT')

    keyboard = []

    for user in users_data['users']:
        subscription = user.subscription
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
                    text=texts.ADMIN_USERS_SEARCH,
                    callback_data='admin_users_search',
                ),
                types.InlineKeyboardButton(
                    text=texts.ADMIN_USER_STATISTICS,
                    callback_data='admin_users_stats',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.BACK,
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
        logger.error(f'Ошибка парсинга номера страницы: {e}')
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
        logger.error(f'Ошибка парсинга номера страницы: {e}')
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
        logger.error(f'Ошибка парсинга номера страницы: {e}')
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
        logger.error(f'Ошибка парсинга номера страницы: {e}')
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
        logger.error(f'Ошибка парсинга номера страницы: {e}')
        await show_users_list_by_campaign(callback, db_user, db, state, 1)


@admin_required
@error_handler
async def start_user_search(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_USERS_SEARCH_PROMPT'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_users')]]
        ),
    )

    await state.set_state(AdminStates.waiting_for_user_search)
    await callback.answer()


@admin_required
@error_handler
async def show_users_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)
    texts = get_texts(db_user.language)

    from sqlalchemy import func, or_, select

    current_time = datetime.utcnow()

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

    text = texts.t('ADMIN_USERS_STATS_TEXT').format(
        total_users=stats['total_users'],
        active_users=stats['active_users'],
        blocked_users=stats['blocked_users'],
        users_with_subscription=users_with_subscription,
        trial_users=trial_users,
        users_without_subscription=users_without_subscription,
        avg_balance=settings.format_price(int(avg_balance)),
        new_today=stats['new_today'],
        new_week=stats['new_week'],
        new_month=stats['new_month'],
        conversion=(users_with_subscription / max(stats['active_users'], 1) * 100),
        trial_share=(trial_users / max(users_with_subscription, 1) * 100),
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USERS_REFRESH_BUTTON'), callback_data='admin_users_stats'
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_users')],
            ]
        ),
    )
    await callback.answer()


async def _render_user_subscription_overview(
    callback: types.CallbackQuery, db: AsyncSession, user_id: int, language: str = 'ru'
) -> bool:
    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)
    texts = get_texts(language)

    if not profile:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return False

    user = profile['user']
    subscription = profile['subscription']

    text = texts.t('ADMIN_USER_SUBSCRIPTION_OVERVIEW_TITLE') + '\n\n'
    if user.telegram_id:
        user_link = f'<a href="tg://user?id={user.telegram_id}">{user.full_name}</a>'
        user_id_display = user.telegram_id
    else:
        user_link = f'<b>{user.full_name}</b>'
        user_id_display = user.email or f'#{user.id}'
    text += f'👤 {user_link} (ID: <code>{user_id_display}</code>)\n\n'

    keyboard = []

    if subscription:
        status_emoji = '✅' if subscription.is_active else '❌'
        type_emoji = '🎁' if subscription.is_trial else '💎'

        traffic_display = f'{subscription.traffic_used_gb:.1f}/'
        if subscription.traffic_limit_gb == 0:
            traffic_display += texts.t('ADMIN_USER_SUBSCRIPTION_UNLIMITED')
        else:
            traffic_display += f'{subscription.traffic_limit_gb} {texts.t("ADMIN_USER_SUBSCRIPTION_UNIT_GB")}'

        text += (
            f'<b>{texts.t("ADMIN_USER_SUBSCRIPTION_LABEL_STATUS")}</b> '
            f'{status_emoji} {texts.ADMIN_USER_SUBSCRIPTION_STATUS_ACTIVE if subscription.is_active else texts.ADMIN_USER_SUBSCRIPTION_STATUS_INACTIVE}\n'
        )
        text += (
            f'<b>{texts.t("ADMIN_USER_SUBSCRIPTION_LABEL_TYPE")}</b> '
            f'{type_emoji} {texts.ADMIN_USER_SUBSCRIPTION_TYPE_TRIAL if subscription.is_trial else texts.ADMIN_USER_SUBSCRIPTION_TYPE_PAID}\n'
        )

        # Отображение тарифа
        if subscription.tariff_id:
            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff:
                text += f'<b>{texts.t("ADMIN_USER_SUBSCRIPTION_LABEL_TARIFF")}</b> 📦 {tariff.name}\n'
            else:
                text += texts.t('ADMIN_USER_SUBSCRIPTION_TARIFF_REMOVED').format(id=subscription.tariff_id) + '\n'

        text += f'<b>{texts.t("ADMIN_USER_SUBSCRIPTION_LABEL_START")}</b> {format_datetime(subscription.start_date)}\n'
        text += f'<b>{texts.t("ADMIN_USER_SUBSCRIPTION_LABEL_END")}</b> {format_datetime(subscription.end_date)}\n'
        text += f'<b>{texts.t("ADMIN_USER_SUBSCRIPTION_LABEL_TRAFFIC")}</b> {traffic_display}\n'
        text += f'<b>{texts.t("ADMIN_USER_SUBSCRIPTION_LABEL_DEVICES")}</b> {subscription.device_limit}\n'

        if subscription.is_active:
            days_left = (subscription.end_date - datetime.utcnow()).days
            text += f'<b>{texts.t("ADMIN_USER_SUBSCRIPTION_LABEL_DAYS_LEFT")}</b> {days_left}\n'

        current_squads = subscription.connected_squads or []
        if current_squads:
            text += '\n' + texts.t('ADMIN_USER_SUBSCRIPTION_CONNECTED_SERVERS') + '\n'
            for squad_uuid in current_squads:
                try:
                    server = await get_server_squad_by_uuid(db, squad_uuid)
                    if server:
                        text += f'• {server.display_name}\n'
                    else:
                        text += (
                            f'• {texts.t("ADMIN_USER_SUBSCRIPTION_SERVER_UNKNOWN").format(short_uuid=squad_uuid[:8])}\n'
                        )
                except Exception as e:
                    logger.error('Failed to load server %s: %s', squad_uuid, e)
                    text += (
                        f'• {texts.t("ADMIN_USER_SUBSCRIPTION_SERVER_LOAD_ERROR").format(short_uuid=squad_uuid[:8])}\n'
                    )
        else:
            text += '\n' + texts.t('ADMIN_USER_SUBSCRIPTION_CONNECTED_SERVERS_NONE') + '\n'

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_BUTTON_EXTEND'), callback_data=f'admin_sub_extend_{user_id}'
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_BUTTON_BUY'), callback_data=f'admin_sub_buy_{user_id}'
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_BUTTON_CHANGE_TYPE'),
                    callback_data=f'admin_sub_change_type_{user_id}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_BUTTON_ADD_TRAFFIC'),
                    callback_data=f'admin_sub_traffic_{user_id}',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_BUTTON_CHANGE_SERVER'),
                    callback_data=f'admin_user_change_server_{user_id}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_BUTTON_DEVICES'), callback_data=f'admin_user_devices_{user_id}'
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_BUTTON_TRAFFIC_LIMIT'),
                    callback_data=f'admin_user_traffic_{user_id}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_BUTTON_RESET_DEVICES'),
                    callback_data=f'admin_user_reset_devices_{user_id}',
                ),
            ],
        ]

        if settings.is_modem_enabled():
            modem_status = '✅' if getattr(subscription, 'modem_enabled', False) else '❌'
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_BUTTON_MODEM').format(status=modem_status),
                        callback_data=f'admin_user_modem_{user_id}',
                    )
                ]
            )

        # Кнопки тарифов в режиме тарифов
        if settings.is_tariffs_mode():
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_BUTTON_CHANGE_TARIFF'),
                        callback_data=f'admin_sub_change_tariff_{user_id}',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_BUTTON_BUY_TARIFF'),
                        callback_data=f'admin_tariff_buy_{user_id}',
                    ),
                ]
            )

        if subscription.is_active:
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_BUTTON_DEACTIVATE'),
                        callback_data=f'admin_sub_deactivate_{user_id}',
                    )
                ]
            )
        else:
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_BUTTON_ACTIVATE'),
                        callback_data=f'admin_sub_activate_{user_id}',
                    )
                ]
            )
    else:
        text += texts.t('ADMIN_USER_SUBSCRIPTION_MISSING')

        keyboard = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_BUTTON_GRANT_TRIAL'),
                    callback_data=f'admin_sub_grant_trial_{user_id}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_BUTTON_GRANT_SUBSCRIPTION'),
                    callback_data=f'admin_sub_grant_{user_id}',
                ),
            ]
        ]

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.ADMIN_USER_PROMO_GROUP_BACK, callback_data=f'admin_user_manage_{user_id}'
            )
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    return True


@admin_required
@error_handler
async def show_user_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])

    if await _render_user_subscription_overview(callback, db, user_id, db_user.language):
        await callback.answer()


@admin_required
@error_handler
async def show_user_transactions(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    from app.database.crud.transaction import get_user_transactions

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    transactions = await get_user_transactions(db, user_id, limit=10)

    text = texts.t('ADMIN_USER_TRANSACTIONS_TITLE') + '\n\n'
    if user.telegram_id:
        user_link = f'<a href="tg://user?id={user.telegram_id}">{user.full_name}</a>'
        user_id_display = user.telegram_id
    else:
        user_link = f'<b>{user.full_name}</b>'
        user_id_display = user.email or f'#{user.id}'
    text += f'👤 {user_link} (ID: <code>{user_id_display}</code>)\n'
    text += f'{texts.t("ADMIN_USER_CURRENT_BALANCE")}: {settings.format_price(user.balance_kopeks)}\n\n'

    if transactions:
        text += texts.t('ADMIN_USER_LAST_TRANSACTIONS') + '\n\n'

        for transaction in transactions:
            type_emoji = '📈' if transaction.amount_kopeks > 0 else '📉'
            text += f'{type_emoji} {settings.format_price(abs(transaction.amount_kopeks))}\n'
            text += f'📋 {transaction.description}\n'
            text += f'📅 {format_datetime(transaction.created_at)}\n\n'
    else:
        text += texts.t('ADMIN_USER_TRANSACTIONS_EMPTY')

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.ADMIN_USER_PROMO_GROUP_BACK, callback_data=f'admin_user_manage_{user_id}'
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
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_USER_DELETE_CONFIRM_TEXT'),
        reply_markup=get_confirmation_keyboard(
            f'admin_user_delete_confirm_{user_id}', f'admin_user_manage_{user_id}', db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def delete_user_account(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    user_service = UserService()
    success = await user_service.delete_user_account(db, user_id, db_user.id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_DELETE_SUCCESS'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USER_BACK_TO_USERS_LIST'),
                            callback_data='admin_users_list',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_DELETE_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USER_BACK_TO_USER'),
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
        await message.answer(texts.t('ADMIN_USERS_SEARCH_INVALID'))
        return

    user_service = UserService()
    search_results = await user_service.search_users(db, query, page=1, limit=10)

    if not search_results['users']:
        await message.answer(
            texts.t('ADMIN_USERS_SEARCH_EMPTY').format(query=query),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_users')]]
            ),
        )
        await state.clear()
        return

    text = texts.t('ADMIN_USERS_SEARCH_RESULTS_TITLE').format(query=query)
    text += '\n\n' + texts.t('ADMIN_USERS_SEARCH_SELECT_USER')

    keyboard = []

    for user in search_results['users']:
        if user.status == UserStatus.ACTIVE.value:
            status_emoji = '✅'
        elif user.status == UserStatus.BLOCKED.value:
            status_emoji = '🚫'
        else:
            status_emoji = '🗑️'

        subscription_emoji = ''
        if user.subscription:
            if user.subscription.is_trial:
                subscription_emoji = '🎁'
            elif user.subscription.is_active:
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

    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_users')])

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
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    user = profile['user']
    subscription = profile['subscription']

    texts = get_texts(db_user.language)

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
            name=user.full_name,
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
                devices=subscription.device_limit,
                countries=len(subscription.connected_squads),
            )
        )
    else:
        sections.append(texts.ADMIN_USER_MANAGEMENT_SUBSCRIPTION_NONE)

    # Display promo groups
    primary_group = user.get_primary_promo_group()
    if primary_group:
        sections.append(
            texts.t('ADMIN_USER_PROMO_GROUPS_PRIMARY').format(
                name=primary_group.name, priority=getattr(primary_group, 'priority', 0)
            )
        )
        sections.append(
            texts.ADMIN_USER_MANAGEMENT_PROMO_GROUP.format(
                name=primary_group.name,
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
                sections.append(texts.t('ADMIN_USER_PROMO_GROUPS_ADDITIONAL'))
                for group in additional_groups:
                    sections.append(f'  • {group.name} (Priority: {getattr(group, "priority", 0)})')
    else:
        sections.append(texts.ADMIN_USER_MANAGEMENT_PROMO_GROUP_NONE)

    # Показать ограничения пользователя если есть
    restriction_topup = getattr(user, 'restriction_topup', False)
    restriction_subscription = getattr(user, 'restriction_subscription', False)
    if restriction_topup or restriction_subscription:
        restriction_lines = [texts.t('ADMIN_USER_RESTRICTIONS_TITLE')]
        if restriction_topup:
            restriction_lines.append(texts.t('ADMIN_USER_RESTRICTION_TOPUP'))
        if restriction_subscription:
            restriction_lines.append(texts.t('ADMIN_USER_RESTRICTION_SUBSCRIPTION'))
        restriction_reason = getattr(user, 'restriction_reason', None)
        if restriction_reason:
            restriction_lines.append(texts.t('ADMIN_USER_RESTRICTION_REASON').format(reason=restriction_reason))
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
                text=texts.t('ADMIN_USER_BACK_TO_TICKET'), callback_data=f'admin_view_ticket_{origin_ticket_id}'
            )
            kb.inline_keyboard.insert(0, [back_to_ticket_btn])
    except Exception:
        pass

    await callback.message.edit_text(text, reply_markup=kb)
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

    header = texts.t('ADMIN_USER_REFERRALS_TITLE')
    summary = texts.t('ADMIN_USER_REFERRALS_SUMMARY').format(
        name=user.full_name,
        telegram_id=user.telegram_id,
        count=len(referrals),
    )

    lines: list[str] = [header, summary]

    if user.referral_commission_percent is None:
        lines.append(texts.t('ADMIN_USER_REFERRAL_COMMISSION_DEFAULT').format(percent=effective_percent))
    else:
        lines.append(
            texts.t('ADMIN_USER_REFERRAL_COMMISSION_CUSTOM').format(
                percent=user.referral_commission_percent,
                default_percent=default_percent,
            )
        )

    if referrals:
        lines.append(texts.t('ADMIN_USER_REFERRALS_LIST_HEADER'))
        items = []
        for referral in referrals[:limit]:
            username_part = f', @{referral.username}' if referral.username else ''
            if referral.telegram_id:
                referral_link = f'<a href="tg://user?id={referral.telegram_id}">{referral.full_name}</a>'
                referral_id_display = referral.telegram_id
            else:
                referral_link = f'<b>{referral.full_name}</b>'
                referral_id_display = referral.email or f'#{referral.id}'
            items.append(
                texts.t('ADMIN_USER_REFERRALS_LIST_ITEM').format(
                    name=referral_link,
                    telegram_id=referral_id_display,
                    username_part=username_part,
                )
            )

        lines.append('\n'.join(items))

        if len(referrals) > limit:
            remaining = len(referrals) - limit
            lines.append(texts.t('ADMIN_USER_REFERRALS_LIST_TRUNCATED').format(count=remaining))
    else:
        lines.append(texts.t('ADMIN_USER_REFERRALS_EMPTY'))

    lines.append(texts.t('ADMIN_USER_REFERRALS_EDIT_HINT'))

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_REFERRAL_COMMISSION_EDIT_BUTTON'),
                    callback_data=f'admin_user_referral_percent_{user_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_REFERRALS_EDIT_BUTTON'),
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
        await callback.answer(get_texts(db_user.language).t('ADMIN_USER_NOT_FOUND'), show_alert=True)
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
        await callback.answer(get_texts(db_user.language).t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    texts = get_texts(db_user.language)

    effective_percent = get_effective_referral_commission_percent(user)
    default_percent = settings.REFERRAL_COMMISSION_PERCENT

    prompt = texts.t('ADMIN_USER_REFERRAL_COMMISSION_PROMPT').format(current=effective_percent, default=default_percent)

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
                    text=texts.t('ADMIN_USER_REFERRAL_COMMISSION_RESET_BUTTON'),
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
        user.updated_at = datetime.utcnow()

        await db.commit()

        effective = get_effective_referral_commission_percent(user)

        logger.info(
            'Админ %s обновил реферальный процент пользователя %s: %s',
            admin_id,
            user_id,
            percent,
        )

        return True, effective
    except Exception as e:
        logger.error(
            'Ошибка обновления реферального процента пользователя %s: %s',
            user_id,
            e,
        )
        try:
            await db.rollback()
        except Exception as rollback_error:
            logger.error('Ошибка отката транзакции: %s', rollback_error)
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
        await callback.answer(texts.t('ADMIN_USER_REFERRAL_COMMISSION_UPDATE_ERROR'), show_alert=True)
        return

    await state.clear()

    success_message = texts.t('ADMIN_USER_REFERRAL_COMMISSION_UPDATED').format(percent=effective_percent)

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
        await message.answer(get_texts(db_user.language).t('ADMIN_USER_REFERRALS_STATE_LOST'))
        return

    raw_text = message.text.strip()
    normalized = raw_text.lower()

    percent_value: int | None

    if normalized in {'стандарт', 'standard', 'default'}:
        percent_value = None
    else:
        normalized_number = raw_text.replace(',', '.').strip()
        try:
            percent_float = float(normalized_number)
        except (TypeError, ValueError):
            await message.answer(get_texts(db_user.language).t('ADMIN_USER_REFERRAL_COMMISSION_INVALID'))
            return

        percent_value = int(round(percent_float))

        if percent_value < 0 or percent_value > 100:
            await message.answer(get_texts(db_user.language).t('ADMIN_USER_REFERRAL_COMMISSION_INVALID'))
            return

    texts = get_texts(db_user.language)

    success, effective_percent = await _update_referral_commission_percent(
        db,
        int(user_id),
        percent_value,
        db_user.id,
    )

    if not success:
        await message.answer(texts.t('ADMIN_USER_REFERRAL_COMMISSION_UPDATE_ERROR'))
        return

    await state.clear()

    success_message = texts.t('ADMIN_USER_REFERRAL_COMMISSION_UPDATED').format(percent=effective_percent)

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
        await callback.answer(get_texts(db_user.language).t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    texts = get_texts(db_user.language)
    prompt = texts.t('ADMIN_USER_REFERRALS_EDIT_PROMPT').format(
        name=user.full_name,
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
        await message.answer(texts.t('ADMIN_USER_REFERRALS_STATE_LOST'))
        await state.clear()
        return

    raw_text = message.text.strip()
    lower_text = raw_text.lower()
    clear_keywords = {'0', 'нет', 'none', 'пусто', 'clear'}
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

        normalized = normalized.removeprefix('@')

        user = None
        if normalized.isdigit():
            try:
                user = await get_user_by_telegram_id(db, int(normalized))
            except ValueError:
                user = None
        else:
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
        error_lines = [texts.t('ADMIN_USER_REFERRALS_NO_VALID')]
        if not_found:
            error_lines.append(texts.t('ADMIN_USER_REFERRALS_INVALID_ENTRIES').format(values=', '.join(not_found)))
        if skipped_self:
            error_lines.append(texts.t('ADMIN_USER_REFERRALS_SELF_SKIPPED').format(values=', '.join(skipped_self)))
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
        await message.answer(texts.t('ADMIN_USER_REFERRALS_UPDATE_ERROR'))
        return

    response_lines = [texts.t('ADMIN_USER_REFERRALS_UPDATED')]

    total_referrals = details.get('total', len(new_referral_ids))
    added = details.get('added', 0)
    removed = details.get('removed', 0)

    response_lines.append(texts.t('ADMIN_USER_REFERRALS_UPDATED_TOTAL').format(total=total_referrals))

    if added > 0:
        response_lines.append(texts.t('ADMIN_USER_REFERRALS_UPDATED_ADDED').format(count=added))

    if removed > 0:
        response_lines.append(texts.t('ADMIN_USER_REFERRALS_UPDATED_REMOVED').format(count=removed))

    if not_found:
        response_lines.append(texts.t('ADMIN_USER_REFERRALS_INVALID_ENTRIES').format(values=', '.join(not_found)))

    if skipped_self:
        response_lines.append(texts.t('ADMIN_USER_REFERRALS_SELF_SKIPPED').format(values=', '.join(skipped_self)))

    if duplicate_tokens:
        response_lines.append(texts.t('ADMIN_USER_REFERRALS_DUPLICATES').format(values=', '.join(duplicate_tokens)))

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

    primary_group = user.get_primary_promo_group()
    user_group_ids = [upg.promo_group_id for upg in user.user_promo_groups] if user.user_promo_groups else []

    if primary_group:
        current_line = texts.t('ADMIN_USER_PROMO_GROUPS_PRIMARY').format(
            name=primary_group.name,
            priority=getattr(primary_group, 'priority', 0),
        )

        discount_line = texts.ADMIN_USER_PROMO_GROUP_DISCOUNTS.format(
            servers=primary_group.server_discount_percent,
            traffic=primary_group.traffic_discount_percent,
            devices=primary_group.device_discount_percent,
        )

        if len(user_group_ids) > 1:
            additional_groups = [
                upg.promo_group
                for upg in user.user_promo_groups
                if upg.promo_group and upg.promo_group.id != primary_group.id
            ]
            if additional_groups:
                additional_line = '\n' + texts.t('ADMIN_USER_PROMO_GROUPS_ADDITIONAL') + '\n'
                for group in additional_groups:
                    additional_line += f'  • {group.name} (Priority: {getattr(group, "priority", 0)})\n'
                discount_line += additional_line
    else:
        current_line = texts.t('ADMIN_USER_PROMO_GROUPS_NONE')
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
            user_group_ids,
            language,
        ),
    )


@admin_required
@error_handler
async def show_user_promo_group(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(get_texts(db_user.language).t('ADMIN_USER_NOT_FOUND'), show_alert=True)
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
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    has_group = await has_user_promo_group(db, user_id, group_id)

    if has_group:
        groups_count = await count_user_promo_groups(db, user_id)
        if groups_count <= 1:
            await callback.answer(texts.t('ADMIN_USER_PROMO_GROUP_CANNOT_REMOVE_LAST'), show_alert=True)
            return

        group = await get_promo_group_by_id(db, group_id)
        await remove_user_from_promo_group(db, user_id, group_id)
        await callback.answer(
            texts.t('ADMIN_USER_PROMO_GROUP_REMOVED').format(name=group.name if group else ''),
            show_alert=True,
        )
    else:
        group = await get_promo_group_by_id(db, group_id)
        if not group:
            await callback.answer(texts.ADMIN_USER_PROMO_GROUP_ERROR, show_alert=True)
            return

        await add_user_to_promo_group(db, user_id, group_id, assigned_by='admin')
        await callback.answer(
            texts.t('ADMIN_USER_PROMO_GROUP_ADDED').format(name=group.name),
            show_alert=True,
        )

    user = await get_user_by_id(db, user_id)
    promo_groups = await get_promo_groups_with_counts(db)
    await _render_user_promo_group(callback.message, db_user.language, user, promo_groups)


@admin_required
@error_handler
async def start_balance_edit(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    user_id = int(callback.data.split('_')[-1])

    await state.update_data(editing_user_id=user_id)

    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_USER_BALANCE_EDIT_PROMPT'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_user_manage_{user_id}')]
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
        await callback.answer(get_texts(db_user.language).t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    await state.update_data(direct_message_user_id=user_id)

    texts = get_texts(db_user.language)
    prompt = texts.t('ADMIN_USER_SEND_MESSAGE_PROMPT')

    await callback.message.edit_text(
        prompt,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_user_manage_{user_id}')]
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
        await message.answer(texts.t('ADMIN_USER_SEND_MESSAGE_ERROR_NOT_FOUND'))
        await state.clear()
        return

    target_user = await get_user_by_id(db, int(user_id))
    if not target_user:
        await message.answer(texts.t('ADMIN_USER_SEND_MESSAGE_ERROR_NOT_FOUND'))
        await state.clear()
        return

    text = (message.text or '').strip()
    if not text:
        await message.answer(texts.t('ADMIN_USER_SEND_MESSAGE_EMPTY'))
        return

    confirmation_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_BACK_TO_USER'), callback_data=f'admin_user_manage_{user_id}'
                )
            ]
        ]
    )

    if not target_user.telegram_id:
        await message.answer(
            texts.t('ADMIN_USER_NO_TELEGRAM_ID'),
            reply_markup=confirmation_keyboard,
        )
        await state.clear()
        return

    try:
        await message.bot.send_message(target_user.telegram_id, text, parse_mode='HTML')
        await message.answer(
            texts.t('ADMIN_USER_SEND_MESSAGE_SUCCESS'),
            reply_markup=confirmation_keyboard,
        )
    except TelegramForbiddenError:
        await message.answer(
            texts.t('ADMIN_USER_SEND_MESSAGE_FORBIDDEN'),
            reply_markup=confirmation_keyboard,
        )
    except TelegramBadRequest as err:
        logger.error('Ошибка отправки сообщения пользователю %s: %s', target_user.telegram_id, err)
        await message.answer(
            texts.t('ADMIN_USER_SEND_MESSAGE_BAD_REQUEST'),
            reply_markup=confirmation_keyboard,
        )
        await state.clear()
        return
    except Exception as err:
        logger.error('Неожиданная ошибка отправки сообщения пользователю %s: %s', target_user.telegram_id, err)
        await message.answer(
            texts.t('ADMIN_USER_SEND_MESSAGE_ERROR'),
            reply_markup=confirmation_keyboard,
        )
        await state.clear()
        return

    await state.clear()


@admin_required
@error_handler
async def process_balance_edit(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    user_id = data.get('editing_user_id')
    texts = get_texts(db_user.language)

    if not user_id:
        await message.answer(texts.t('ADMIN_USER_BALANCE_EDIT_ERROR_NOT_FOUND'))
        await state.clear()
        return

    try:
        amount_rubles = float(message.text.replace(',', '.'))
        amount_kopeks = int(amount_rubles * 100)

        if abs(amount_kopeks) > 10000000:
            await message.answer(texts.t('ADMIN_USER_BALANCE_EDIT_TOO_LARGE'))
            return

        user_service = UserService()

        description = texts.t('ADMIN_USER_BALANCE_EDIT_DESC').format(admin_name=db_user.full_name)
        if amount_kopeks > 0:
            description = texts.t('ADMIN_USER_BALANCE_EDIT_DESC_TOPUP').format(amount=int(amount_rubles))
        else:
            description = texts.t('ADMIN_USER_BALANCE_EDIT_DESC_DEBIT').format(amount=int(amount_rubles))

        success = await user_service.update_user_balance(
            db, user_id, amount_kopeks, description, db_user.id, bot=message.bot, admin_name=db_user.full_name
        )

        if success:
            action = (
                texts.t('ADMIN_USER_BALANCE_ACTION_TOPUP')
                if amount_kopeks > 0
                else texts.t('ADMIN_USER_BALANCE_ACTION_DEBIT')
            )
            await message.answer(
                texts.t('ADMIN_USER_BALANCE_EDIT_SUCCESS').format(
                    action=action,
                    amount=settings.format_price(abs(amount_kopeks)),
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USER_BACK_TO_USER'),
                                callback_data=f'admin_user_manage_{user_id}',
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USER_BALANCE_EDIT_ERROR'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USER_BALANCE_EDIT_INVALID_AMOUNT'))
        return

    await state.clear()


@admin_required
@error_handler
async def confirm_user_block(callback: types.CallbackQuery, db_user: User):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_USER_BLOCK_CONFIRM_TEXT'),
        reply_markup=get_confirmation_keyboard(
            f'admin_user_block_confirm_{user_id}', f'admin_user_manage_{user_id}', db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def block_user(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    user_service = UserService()
    success = await user_service.block_user(db, user_id, db_user.id, texts.t('ADMIN_USER_BLOCK_REASON'))

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_BLOCK_SUCCESS'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USER_BACK_TO_USER'), callback_data=f'admin_user_manage_{user_id}'
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_BLOCK_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USER_BACK_TO_USER'), callback_data=f'admin_user_manage_{user_id}'
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
    user_id = int(callback.data.split('_')[-1])
    user = await get_user_by_id(db, user_id)
    texts = get_texts(db_user.language)

    if not user:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    # Формируем текст с информацией об ограничениях
    restriction_topup = getattr(user, 'restriction_topup', False)
    restriction_subscription = getattr(user, 'restriction_subscription', False)
    restriction_reason = getattr(user, 'restriction_reason', None)

    text_lines = [
        texts.t('ADMIN_USER_RESTRICTIONS_SCREEN_TITLE'),
        f'👤 {user.full_name}',
        '',
        texts.t('ADMIN_USER_RESTRICTIONS_LEGEND'),
        '',
        f'{"🚫" if restriction_topup else "✅"} {texts.t("ADMIN_USER_RESTRICTION_TOPUP_LABEL")}',
        f'{"🚫" if restriction_subscription else "✅"} {texts.t("ADMIN_USER_RESTRICTION_SUBSCRIPTION_LABEL")}',
    ]

    if restriction_reason:
        text_lines.append('')
        text_lines.append(texts.t('ADMIN_USER_RESTRICTION_REASON').format(reason=restriction_reason))

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
    user_id = int(callback.data.split('_')[-1])
    user = await get_user_by_id(db, user_id)
    texts = get_texts(db_user.language)

    if not user:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    # Переключаем ограничение
    current_value = getattr(user, 'restriction_topup', False)
    user.restriction_topup = not current_value
    await db.commit()

    action = (
        texts.t('ADMIN_USER_RESTRICTION_ACTION_SET')
        if user.restriction_topup
        else texts.t('ADMIN_USER_RESTRICTION_ACTION_UNSET')
    )
    await callback.answer(texts.t('ADMIN_USER_RESTRICTION_TOPUP_TOGGLED').format(action=action), show_alert=False)

    # Обновляем меню
    await show_user_restrictions(callback, db_user, db)


@admin_required
@error_handler
async def toggle_user_restriction_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Переключить ограничение на продление/покупку подписки."""
    user_id = int(callback.data.split('_')[-1])
    user = await get_user_by_id(db, user_id)
    texts = get_texts(db_user.language)

    if not user:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    # Переключаем ограничение
    current_value = getattr(user, 'restriction_subscription', False)
    user.restriction_subscription = not current_value
    await db.commit()

    action = (
        texts.t('ADMIN_USER_RESTRICTION_ACTION_SET')
        if user.restriction_subscription
        else texts.t('ADMIN_USER_RESTRICTION_ACTION_UNSET')
    )
    await callback.answer(
        texts.t('ADMIN_USER_RESTRICTION_SUBSCRIPTION_TOGGLED').format(action=action), show_alert=False
    )

    # Обновляем меню
    await show_user_restrictions(callback, db_user, db)


@admin_required
@error_handler
async def ask_restriction_reason(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Запросить ввод причины ограничения."""
    user_id = int(callback.data.split('_')[-1])
    user = await get_user_by_id(db, user_id)
    texts = get_texts(db_user.language)

    if not user:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    current_reason = getattr(user, 'restriction_reason', None) or ''

    await state.set_state(AdminStates.editing_user_restriction_reason)
    await state.update_data(restriction_user_id=user_id)

    text = texts.t('ADMIN_USER_RESTRICTION_REASON_PROMPT')
    if current_reason:
        text += texts.t('ADMIN_USER_RESTRICTION_REASON_CURRENT').format(reason=current_reason)
    text += texts.t('ADMIN_USER_RESTRICTION_REASON_SUBMIT_HINT')

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_user_restrictions_{user_id}')]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def save_restriction_reason(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Сохранить причину ограничения."""
    data = await state.get_data()
    user_id = data.get('restriction_user_id')
    texts = get_texts(db_user.language)

    if not user_id:
        await message.answer(texts.t('ADMIN_USER_RESTRICTION_ERROR_NOT_FOUND'))
        await state.clear()
        return

    user = await get_user_by_id(db, user_id)
    if not user:
        await message.answer(texts.t('ADMIN_USER_RESTRICTION_ERROR_NOT_FOUND'))
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
        texts.t('ADMIN_USER_RESTRICTION_REASON_SAVED'),
        '',
        texts.t('ADMIN_USER_RESTRICTIONS_SCREEN_TITLE'),
        f'👤 {user.full_name}',
        '',
        f'{"🚫" if restriction_topup else "✅"} {texts.t("ADMIN_USER_RESTRICTION_TOPUP_LABEL")}',
        f'{"🚫" if restriction_subscription else "✅"} {texts.t("ADMIN_USER_RESTRICTION_SUBSCRIPTION_LABEL")}',
        '',
        texts.t('ADMIN_USER_RESTRICTION_REASON').format(reason=reason),
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
    user_id = int(callback.data.split('_')[-1])
    user = await get_user_by_id(db, user_id)
    texts = get_texts(db_user.language)

    if not user:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    # Снимаем все ограничения
    user.restriction_topup = False
    user.restriction_subscription = False
    user.restriction_reason = None
    await db.commit()

    await callback.answer(texts.t('ADMIN_USER_RESTRICTIONS_CLEARED'), show_alert=True)

    # Обновляем меню
    await show_user_restrictions(callback, db_user, db)


@admin_required
@error_handler
async def show_inactive_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    UserService()
    texts = get_texts(db_user.language)

    from app.database.crud.user import get_inactive_users

    inactive_users = await get_inactive_users(db, settings.INACTIVE_USER_DELETE_MONTHS)

    if not inactive_users:
        await callback.message.edit_text(
            texts.t('ADMIN_INACTIVE_USERS_EMPTY').format(months=settings.INACTIVE_USER_DELETE_MONTHS),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_users')]]
            ),
        )
        await callback.answer()
        return

    with_active_sub = sum(1 for u in inactive_users if u.subscription and u.subscription.is_active)
    will_delete = len(inactive_users) - with_active_sub

    text = texts.t('ADMIN_INACTIVE_USERS_TITLE') + '\n'
    text += (
        texts.t('ADMIN_INACTIVE_USERS_SUMMARY').format(
            months=settings.INACTIVE_USER_DELETE_MONTHS, count=len(inactive_users)
        )
        + '\n'
    )
    if with_active_sub > 0:
        text += f'🛡️ С активной подпиской (не будут удалены): {with_active_sub}\n'
        text += f'🗑️ Будет удалено: {will_delete}\n'
    text += '\n'

    for user in inactive_users[:10]:
        if user.telegram_id:
            user_link = f'<a href="tg://user?id={user.telegram_id}">{user.full_name}</a>'
            user_id_display = user.telegram_id
        else:
            user_link = f'<b>{user.full_name}</b>'
            user_id_display = user.email or f'#{user.id}'
        has_active = user.subscription and user.subscription.is_active
        sub_badge = ' 🛡️' if has_active else ''
        text += f'👤 {user_link}{sub_badge}\n'
        text += f'🆔 <code>{user_id_display}</code>\n'
        last_activity_display = (
            format_time_ago(user.last_activity, db_user.language)
            if user.last_activity
            else texts.t('ADMIN_INACTIVE_USERS_NEVER')
        )
        text += f'📅 {last_activity_display}\n\n'

    if len(inactive_users) > 10:
        text += texts.t('ADMIN_INACTIVE_USERS_MORE').format(count=len(inactive_users) - 10)

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_INACTIVE_USERS_CLEANUP_ALL'), callback_data='admin_cleanup_inactive'
            )
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_users')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def confirm_user_unblock(callback: types.CallbackQuery, db_user: User):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_USER_UNBLOCK_CONFIRM_TEXT'),
        reply_markup=get_confirmation_keyboard(
            f'admin_user_unblock_confirm_{user_id}', f'admin_user_manage_{user_id}', db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def unblock_user(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    user_service = UserService()
    success = await user_service.unblock_user(db, user_id, db_user.id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_UNBLOCK_SUCCESS'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USER_BACK_TO_USER'), callback_data=f'admin_user_manage_{user_id}'
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_UNBLOCK_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USER_BACK_TO_USER'), callback_data=f'admin_user_manage_{user_id}'
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def show_user_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    user = profile['user']
    subscription = profile['subscription']

    referral_stats = await get_detailed_referral_stats(db, user.id)
    campaign_registration = await get_campaign_registration_by_user(db, user.id)
    campaign_stats = None
    if campaign_registration:
        campaign_stats = await get_campaign_statistics(db, campaign_registration.campaign_id)

    text = texts.t('ADMIN_USER_STATS_TITLE') + '\n\n'
    if user.telegram_id:
        user_link = f'<a href="tg://user?id={user.telegram_id}">{user.full_name}</a>'
        user_id_display = user.telegram_id
    else:
        user_link = f'<b>{user.full_name}</b>'
        user_id_display = user.email or f'#{user.id}'
    text += f'👤 {user_link} (ID: <code>{user_id_display}</code>)\n\n'

    text += texts.t('ADMIN_USER_STATS_BASIC_SECTION') + '\n'
    text += texts.t('ADMIN_USER_STATS_DAYS_SINCE_REG').format(days=profile['registration_days']) + '\n'
    text += texts.t('ADMIN_USER_STATS_BALANCE').format(balance=settings.format_price(user.balance_kopeks)) + '\n'
    text += texts.t('ADMIN_USER_STATS_TRANSACTIONS').format(count=profile['transactions_count']) + '\n'
    text += texts.t('ADMIN_USER_STATS_LANGUAGE').format(language=user.language) + '\n\n'

    text += texts.t('ADMIN_USER_STATS_SUBSCRIPTION_SECTION') + '\n'
    if subscription:
        sub_status = (
            texts.t('ADMIN_USER_STATS_SUB_ACTIVE')
            if subscription.is_active
            else texts.t('ADMIN_USER_STATS_SUB_INACTIVE')
        )
        sub_type = (
            texts.t('ADMIN_USER_STATS_SUB_TYPE_TRIAL_SUFFIX')
            if subscription.is_trial
            else texts.t('ADMIN_USER_STATS_SUB_TYPE_PAID_SUFFIX')
        )
        text += texts.t('ADMIN_USER_STATS_STATUS').format(status=f'{sub_status}{sub_type}') + '\n'
        text += (
            texts.t('ADMIN_USER_STATS_TRAFFIC').format(
                used=f'{subscription.traffic_used_gb:.1f}',
                limit=subscription.traffic_limit_gb,
            )
            + '\n'
        )
        text += texts.t('ADMIN_USER_STATS_DEVICES').format(count=subscription.device_limit) + '\n'
        text += texts.t('ADMIN_USER_STATS_COUNTRIES').format(count=len(subscription.connected_squads)) + '\n'
    else:
        text += texts.t('ADMIN_USER_STATS_SUB_NONE') + '\n'

    text += '\n' + texts.t('ADMIN_USER_STATS_REFERRAL_SECTION') + '\n'

    if user.referred_by_id:
        referrer = await get_user_by_id(db, user.referred_by_id)
        if referrer:
            text += texts.t('ADMIN_USER_STATS_REFERRED_BY').format(name=referrer.full_name) + '\n'
        else:
            text += texts.t('ADMIN_USER_STATS_REFERRED_BY_MISSING') + '\n'
        if campaign_registration and campaign_registration.campaign:
            text += (
                texts.t('ADMIN_USER_STATS_CAMPAIGN_ADDITIONAL').format(name=campaign_registration.campaign.name) + '\n'
            )
    elif campaign_registration and campaign_registration.campaign:
        text += (
            texts.t('ADMIN_USER_STATS_CAMPAIGN_REGISTRATION').format(name=campaign_registration.campaign.name) + '\n'
        )
        if campaign_registration.created_at:
            text += (
                texts.t('ADMIN_USER_STATS_CAMPAIGN_REG_DATE').format(
                    date=campaign_registration.created_at.strftime('%d.%m.%Y %H:%M')
                )
                + '\n'
            )
    else:
        text += texts.t('ADMIN_USER_STATS_DIRECT_REG') + '\n'

    text += texts.t('ADMIN_USER_STATS_REF_CODE').format(code=user.referral_code) + '\n\n'

    if campaign_registration and campaign_registration.campaign and campaign_stats:
        text += texts.t('ADMIN_USER_STATS_CAMPAIGN_SECTION') + '\n'
        text += texts.t('ADMIN_USER_STATS_CAMPAIGN_NAME').format(name=campaign_registration.campaign.name)
        if campaign_registration.campaign.start_parameter:
            text += texts.t('ADMIN_USER_STATS_CAMPAIGN_PARAM').format(
                param=campaign_registration.campaign.start_parameter
            )
        text += '\n'
        text += texts.t('ADMIN_USER_STATS_CAMPAIGN_TOTAL_REG').format(count=campaign_stats['registrations']) + '\n'
        text += (
            texts.t('ADMIN_USER_STATS_CAMPAIGN_TOTAL_REVENUE').format(
                amount=settings.format_price(campaign_stats['total_revenue_kopeks'])
            )
            + '\n'
        )
        text += (
            texts.t('ADMIN_USER_STATS_CAMPAIGN_TRIAL_USERS').format(
                total=campaign_stats['trial_users_count'],
                active=campaign_stats['active_trials_count'],
            )
            + '\n'
        )
        text += (
            texts.t('ADMIN_USER_STATS_CAMPAIGN_PAID_CONV').format(
                total=campaign_stats['conversion_count'],
                paid=campaign_stats['paid_users_count'],
            )
            + '\n'
        )
        text += texts.t('ADMIN_USER_STATS_CAMPAIGN_CONV_RATE').format(rate=campaign_stats['conversion_rate']) + '\n'
        text += (
            texts.t('ADMIN_USER_STATS_CAMPAIGN_TRIAL_CONV_RATE').format(rate=campaign_stats['trial_conversion_rate'])
            + '\n'
        )
        text += (
            texts.t('ADMIN_USER_STATS_CAMPAIGN_ARPU').format(
                amount=settings.format_price(campaign_stats['avg_revenue_per_user_kopeks'])
            )
            + '\n'
        )
        text += (
            texts.t('ADMIN_USER_STATS_CAMPAIGN_AVG_FIRST_PAYMENT').format(
                amount=settings.format_price(campaign_stats['avg_first_payment_kopeks'])
            )
            + '\n'
        )
        text += '\n'

    if referral_stats['invited_count'] > 0:
        text += texts.t('ADMIN_USER_STATS_REF_EARNINGS_SECTION') + '\n'
        text += texts.t('ADMIN_USER_STATS_REF_INVITED_TOTAL').format(count=referral_stats['invited_count']) + '\n'
        text += texts.t('ADMIN_USER_STATS_REF_ACTIVE').format(count=referral_stats['active_referrals']) + '\n'
        text += (
            texts.t('ADMIN_USER_STATS_REF_TOTAL_EARNED').format(
                amount=settings.format_price(referral_stats['total_earned_kopeks'])
            )
            + '\n'
        )
        text += (
            texts.t('ADMIN_USER_STATS_REF_MONTH_EARNED').format(
                amount=settings.format_price(referral_stats['month_earned_kopeks'])
            )
            + '\n'
        )

        if referral_stats['referrals_detail']:
            text += '\n' + texts.t('ADMIN_USER_STATS_REF_DETAILS_SECTION') + '\n'
            for detail in referral_stats['referrals_detail'][:5]:
                referral_name = detail['referral_name']
                earned = settings.format_price(detail['total_earned_kopeks'])
                status = '🟢' if detail['is_active'] else '🔴'
                text += f'• {status} {referral_name}: {earned}\n'

            if len(referral_stats['referrals_detail']) > 5:
                text += (
                    texts.t('ADMIN_USER_STATS_REF_MORE').format(count=len(referral_stats['referrals_detail']) - 5)
                    + '\n'
                )
    else:
        text += texts.t('ADMIN_USER_STATS_REFERRAL_SECTION') + '\n'
        text += texts.t('ADMIN_USER_STATS_REF_NONE') + '\n'
        text += texts.t('ADMIN_USER_STATS_REF_EARNINGS_NONE') + '\n'

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_BACK_TO_USER'), callback_data=f'admin_user_manage_{user_id}'
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

    referrals_query = select(User).options(selectinload(User.subscription)).where(User.referred_by_id == user_id)

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
    current_time = datetime.utcnow()

    for referral in referrals:
        earned = earnings_by_referral.get(referral.id, 0)

        is_active = False
        if referral.subscription:
            from app.database.models import SubscriptionStatus

            is_active = (
                referral.subscription.status == SubscriptionStatus.ACTIVE.value
                and referral.subscription.end_date > current_time
            )

        referrals_detail.append(
            {
                'referral_id': referral.id,
                'referral_name': referral.full_name,
                'referral_telegram_id': referral.telegram_id,
                'total_earned_kopeks': earned,
                'is_active': is_active,
                'registration_date': referral.created_at,
                'has_subscription': bool(referral.subscription),
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
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    await state.update_data(extending_user_id=user_id)

    await callback.message.edit_text(
        texts.t('ADMIN_USER_SUB_EXTEND_PROMPT'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_DAYS_BUTTON').format(days=-7),
                        callback_data=f'admin_sub_extend_days_{user_id}_-7',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_DAYS_BUTTON').format(days=-30),
                        callback_data=f'admin_sub_extend_days_{user_id}_-30',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_DAYS_BUTTON').format(days=7),
                        callback_data=f'admin_sub_extend_days_{user_id}_7',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_DAYS_BUTTON').format(days=30),
                        callback_data=f'admin_sub_extend_days_{user_id}_30',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_DAYS_BUTTON').format(days=90),
                        callback_data=f'admin_sub_extend_days_{user_id}_90',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_DAYS_BUTTON').format(days=180),
                        callback_data=f'admin_sub_extend_days_{user_id}_180',
                    ),
                ],
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_user_subscription_{user_id}')],
            ]
        ),
    )

    await state.set_state(AdminStates.extending_subscription)
    await callback.answer()


@admin_required
@error_handler
async def process_subscription_extension_days(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    days = int(parts[-1])
    texts = get_texts(db_user.language)

    if days == 0 or days < -365 or days > 365:
        await callback.answer(texts.t('ADMIN_USER_SUB_EXTEND_DAYS_INVALID'), show_alert=True)
        return

    success = await _extend_subscription_by_days(db, user_id, days, db_user.id)

    if success:
        success_text = (
            texts.t('ADMIN_USER_SUB_EXTEND_SUCCESS_POSITIVE').format(days=days)
            if days > 0
            else texts.t('ADMIN_USER_SUB_EXTEND_SUCCESS_NEGATIVE').format(days=abs(days))
        )
        await callback.message.edit_text(
            success_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_EXTEND_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
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
    data = await state.get_data()
    user_id = data.get('extending_user_id')
    texts = get_texts(db_user.language)

    if not user_id:
        await message.answer(texts.t('ADMIN_USER_BALANCE_EDIT_ERROR_NOT_FOUND'))
        await state.clear()
        return

    try:
        days = int(message.text.strip())

        if days == 0 or days < -365 or days > 365:
            await message.answer(texts.t('ADMIN_USER_SUB_EXTEND_DAYS_INVALID'))
            return

        success = await _extend_subscription_by_days(db, user_id, days, db_user.id)

        if success:
            success_text = (
                texts.t('ADMIN_USER_SUB_EXTEND_SUCCESS_POSITIVE').format(days=days)
                if days > 0
                else texts.t('ADMIN_USER_SUB_EXTEND_SUCCESS_NEGATIVE').format(days=abs(days))
            )
            await message.answer(
                success_text,
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USER_SUB_EXTEND_ERROR'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USER_SUB_EXTEND_DAYS_PARSE_ERROR'))
        return

    await state.clear()


@admin_required
@error_handler
async def add_subscription_traffic(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    await state.update_data(traffic_user_id=user_id)

    await callback.message.edit_text(
        texts.t('ADMIN_USER_SUB_ADD_TRAFFIC_PROMPT'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_GB_BUTTON').format(gb=50),
                        callback_data=f'admin_sub_traffic_add_{user_id}_50',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_GB_BUTTON').format(gb=100),
                        callback_data=f'admin_sub_traffic_add_{user_id}_100',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_GB_BUTTON').format(gb=500),
                        callback_data=f'admin_sub_traffic_add_{user_id}_500',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_GB_BUTTON').format(gb=1000),
                        callback_data=f'admin_sub_traffic_add_{user_id}_1000',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_UNLIMITED_BUTTON'),
                        callback_data=f'admin_sub_traffic_add_{user_id}_0',
                    ),
                ],
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_user_subscription_{user_id}')],
            ]
        ),
    )

    await state.set_state(AdminStates.adding_traffic)
    await callback.answer()


@admin_required
@error_handler
async def process_traffic_addition_button(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    gb = int(parts[-1])
    texts = get_texts(db_user.language)

    success = await _add_subscription_traffic(db, user_id, gb, db_user.id)

    if success:
        traffic_text = (
            texts.t('ADMIN_USER_SUB_TRAFFIC_UNLIMITED_VALUE')
            if gb == 0
            else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=gb)
        )
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_TRAFFIC_ADD_SUCCESS').format(traffic=traffic_text),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_TRAFFIC_ADD_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_traffic_addition_text(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    user_id = data.get('traffic_user_id')
    texts = get_texts(db_user.language)

    if not user_id:
        await message.answer(texts.t('ADMIN_USER_BALANCE_EDIT_ERROR_NOT_FOUND'))
        await state.clear()
        return

    try:
        gb = int(message.text.strip())

        if gb < 0 or gb > 10000:
            await message.answer(texts.t('ADMIN_USER_SUB_TRAFFIC_ADD_INVALID_RANGE'))
            return

        success = await _add_subscription_traffic(db, user_id, gb, db_user.id)

        if success:
            traffic_text = (
                texts.t('ADMIN_USER_SUB_TRAFFIC_UNLIMITED_VALUE')
                if gb == 0
                else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=gb)
            )
            await message.answer(
                texts.t('ADMIN_USER_SUB_TRAFFIC_ADD_SUCCESS').format(traffic=traffic_text),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USER_SUB_TRAFFIC_ADD_ERROR'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USER_SUB_TRAFFIC_PARSE_ERROR'))
        return

    await state.clear()


@admin_required
@error_handler
async def deactivate_user_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_USER_SUB_DEACTIVATE_CONFIRM_TEXT'),
        reply_markup=get_confirmation_keyboard(
            f'admin_sub_deactivate_confirm_{user_id}', f'admin_user_subscription_{user_id}', db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_subscription_deactivation(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    success = await _deactivate_user_subscription(db, user_id, db_user.id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_DEACTIVATE_SUCCESS'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_DEACTIVATE_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def activate_user_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    success = await _activate_user_subscription(db, user_id, db_user.id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_ACTIVATE_SUCCESS'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_ACTIVATE_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def grant_trial_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    success = await _grant_trial_subscription(db, user_id, db_user.id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_GRANT_TRIAL_SUCCESS'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_GRANT_TRIAL_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def grant_paid_subscription(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    await state.update_data(granting_user_id=user_id)

    await callback.message.edit_text(
        texts.t('ADMIN_USER_SUB_GRANT_PAID_PROMPT'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_DAYS_BUTTON').format(days=30),
                        callback_data=f'admin_sub_grant_days_{user_id}_30',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_DAYS_BUTTON').format(days=90),
                        callback_data=f'admin_sub_grant_days_{user_id}_90',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_DAYS_BUTTON').format(days=180),
                        callback_data=f'admin_sub_grant_days_{user_id}_180',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_DAYS_BUTTON').format(days=365),
                        callback_data=f'admin_sub_grant_days_{user_id}_365',
                    ),
                ],
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_user_subscription_{user_id}')],
            ]
        ),
    )

    await state.set_state(AdminStates.granting_subscription)
    await callback.answer()


@admin_required
@error_handler
async def process_subscription_grant_days(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    days = int(parts[-1])
    texts = get_texts(db_user.language)

    success = await _grant_paid_subscription(db, user_id, days, db_user.id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_GRANT_PAID_SUCCESS').format(days=days),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_GRANT_PAID_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_subscription_grant_text(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    user_id = data.get('granting_user_id')
    texts = get_texts(db_user.language)

    if not user_id:
        await message.answer(texts.t('ADMIN_USER_BALANCE_EDIT_ERROR_NOT_FOUND'))
        await state.clear()
        return

    try:
        days = int(message.text.strip())

        if days <= 0 or days > 730:
            await message.answer(texts.t('ADMIN_USER_SUB_GRANT_PAID_INVALID_RANGE'))
            return

        success = await _grant_paid_subscription(db, user_id, days, db_user.id)

        if success:
            await message.answer(
                texts.t('ADMIN_USER_SUB_GRANT_PAID_SUCCESS').format(days=days),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USER_SUB_GRANT_PAID_ERROR'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USER_SUB_GRANT_PAID_PARSE_ERROR'))
        return

    await state.clear()


@admin_required
@error_handler
async def show_user_servers_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])

    if await _render_user_subscription_overview(callback, db, user_id):
        await callback.answer()


@admin_required
@error_handler
async def show_server_selection(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    await _show_servers_for_user(callback, user_id, db, db_user.language)
    await callback.answer()


async def _show_servers_for_user(callback: types.CallbackQuery, user_id: int, db: AsyncSession, language: str):
    try:
        texts = get_texts(language)
        user = await get_user_by_id(db, user_id)
        current_squads = []
        if user and user.subscription:
            current_squads = user.subscription.connected_squads or []

        all_servers, _ = await get_all_server_squads(db, available_only=False)

        servers_to_show = []
        for server in all_servers:
            if server.is_available or server.squad_uuid in current_squads:
                servers_to_show.append(server)

        if not servers_to_show:
            await callback.message.edit_text(
                texts.t('ADMIN_USER_SERVERS_EMPTY'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                            )
                        ]
                    ]
                ),
            )
            return

        text = texts.t('ADMIN_USER_SERVERS_MANAGE_TITLE')
        text += texts.t('ADMIN_USER_SERVERS_MANAGE_HINT') + '\n'
        text += texts.t('ADMIN_USER_SERVERS_STATUS_SELECTED') + '\n'
        text += texts.t('ADMIN_USER_SERVERS_STATUS_AVAILABLE') + '\n'
        text += texts.t('ADMIN_USER_SERVERS_STATUS_INACTIVE') + '\n\n'

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
                display_name += texts.t('ADMIN_USER_SERVERS_INACTIVE_SUFFIX')

            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{emoji} {display_name}', callback_data=f'admin_user_toggle_server_{user_id}_{server.id}'
                    )
                ]
            )

        if len(servers_to_show) > 20:
            text += '\n' + texts.t('ADMIN_USER_SERVERS_SHOWING_LIMIT').format(shown=20, total=len(servers_to_show))

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SERVERS_DONE'), callback_data=f'admin_user_subscription_{user_id}'
                ),
                types.InlineKeyboardButton(
                    text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                ),
            ]
        )

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error(f'Ошибка показа серверов: {e}')


@admin_required
@error_handler
async def toggle_user_server(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    user_id = int(parts[4])
    server_id = int(parts[5])
    texts = get_texts(db_user.language)

    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.subscription:
            await callback.answer(texts.t('ADMIN_USER_OR_SUBSCRIPTION_NOT_FOUND'), show_alert=True)
            return

        server = await get_server_squad_by_id(db, server_id)
        if not server:
            await callback.answer(texts.t('ADMIN_USER_SERVER_NOT_FOUND'), show_alert=True)
            return

        subscription = user.subscription
        current_squads = list(subscription.connected_squads or [])

        if server.squad_uuid in current_squads:
            current_squads.remove(server.squad_uuid)
            action_text = 'удален'
        else:
            current_squads.append(server.squad_uuid)
            action_text = 'добавлен'

        subscription.connected_squads = current_squads
        subscription.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(subscription)

        if user.remnawave_uuid:
            try:
                remnawave_service = RemnaWaveService()
                async with remnawave_service.get_api_client() as api:
                    await api.update_user(
                        uuid=user.remnawave_uuid,
                        active_internal_squads=current_squads,
                        description=settings.format_remnawave_user_description(
                            full_name=user.full_name, username=user.username, telegram_id=user.telegram_id
                        ),
                    )
                logger.info(f'✅ Обновлены серверы в RemnaWave для пользователя {user.telegram_id}')
            except Exception as rw_error:
                logger.error(f'❌ Ошибка обновления RemnaWave: {rw_error}')

        logger.info(f'Админ {db_user.id}: сервер {server.display_name} {action_text} для пользователя {user_id}')

        await refresh_server_selection_screen(callback, user_id, db_user, db)

    except Exception as e:
        logger.error(f'Ошибка переключения сервера: {e}')
        await callback.answer(texts.t('ADMIN_USER_SERVER_UPDATE_ERROR'), show_alert=True)


async def refresh_server_selection_screen(callback: types.CallbackQuery, user_id: int, db_user: User, db: AsyncSession):
    try:
        texts = get_texts(db_user.language)
        user = await get_user_by_id(db, user_id)
        current_squads = []
        if user and user.subscription:
            current_squads = user.subscription.connected_squads or []

        servers, _ = await get_all_server_squads(db, available_only=True)

        if not servers:
            await callback.message.edit_text(
                texts.t('ADMIN_USER_SERVERS_EMPTY'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                            )
                        ]
                    ]
                ),
            )
            return

        text = texts.t('ADMIN_USER_SERVERS_MANAGE_TITLE')
        text += texts.t('ADMIN_USER_SERVERS_MANAGE_HINT_SIMPLE') + '\n\n'

        keyboard = []
        for server in servers[:15]:
            is_selected = server.squad_uuid in current_squads
            emoji = '✅' if is_selected else '⚪'

            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=f'{emoji} {server.display_name}',
                        callback_data=f'admin_user_toggle_server_{user_id}_{server.id}',
                    )
                ]
            )

        if len(servers) > 15:
            text += '\n' + texts.t('ADMIN_USER_SERVERS_SHOWING_LIMIT').format(shown=15, total=len(servers))

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SERVERS_DONE'), callback_data=f'admin_user_subscription_{user_id}'
                ),
                types.InlineKeyboardButton(
                    text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
                ),
            ]
        )

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.error(f'Ошибка обновления экрана серверов: {e}')


@admin_required
@error_handler
async def start_devices_edit(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    await state.update_data(editing_devices_user_id=user_id)

    await callback.message.edit_text(
        texts.t('ADMIN_USER_DEVICES_EDIT_PROMPT'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(text='1', callback_data=f'admin_user_devices_set_{user_id}_1'),
                    types.InlineKeyboardButton(text='2', callback_data=f'admin_user_devices_set_{user_id}_2'),
                    types.InlineKeyboardButton(text='3', callback_data=f'admin_user_devices_set_{user_id}_3'),
                ],
                [
                    types.InlineKeyboardButton(text='5', callback_data=f'admin_user_devices_set_{user_id}_5'),
                    types.InlineKeyboardButton(text='10', callback_data=f'admin_user_devices_set_{user_id}_10'),
                ],
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_user_subscription_{user_id}')],
            ]
        ),
    )

    await state.set_state(AdminStates.editing_user_devices)
    await callback.answer()


@admin_required
@error_handler
async def set_user_devices_button(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    devices = int(parts[-1])
    texts = get_texts(db_user.language)

    success = await _update_user_devices(db, user_id, devices, db_user.id)

    if success:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_DEVICES_EDIT_SUCCESS').format(devices=devices),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USER_SUBSCRIPTION_SETTINGS'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_DEVICES_EDIT_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USER_SUBSCRIPTION_SETTINGS'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def toggle_user_modem(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Переключение модема для пользователя в админке."""
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    subscription = user.subscription
    if not subscription:
        await callback.answer(texts.t('ADMIN_USER_HAS_NO_SUBSCRIPTION'), show_alert=True)
        return

    modem_enabled = getattr(subscription, 'modem_enabled', False) or False

    if modem_enabled:
        # Отключаем модем
        subscription.modem_enabled = False
        if subscription.device_limit and subscription.device_limit > 1:
            subscription.device_limit = subscription.device_limit - 1
        action_text = texts.t('ADMIN_USER_MODEM_ACTION_DISABLED')
    else:
        # Включаем модем
        subscription.modem_enabled = True
        subscription.device_limit = (subscription.device_limit or 1) + 1
        action_text = texts.t('ADMIN_USER_MODEM_ACTION_ENABLED')

    subscription.updated_at = datetime.utcnow()
    await db.commit()

    # Обновляем в RemnaWave
    try:
        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)
    except Exception as e:
        logger.error(f'Ошибка обновления RemnaWave при переключении модема: {e}')

    await db.refresh(subscription)

    modem_status = (
        texts.t('ADMIN_USER_MODEM_STATUS_ENABLED')
        if subscription.modem_enabled
        else texts.t('ADMIN_USER_MODEM_STATUS_DISABLED')
    )

    await callback.message.edit_text(
        texts.t('ADMIN_USER_MODEM_TOGGLE_RESULT').format(
            action=action_text,
            status=modem_status,
            device_limit=subscription.device_limit,
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUBSCRIPTION_SETTINGS'),
                        callback_data=f'admin_user_subscription_{user_id}',
                    )
                ]
            ]
        ),
        parse_mode='HTML',
    )

    logger.info(f'Админ {db_user.telegram_id} {action_text} модем для пользователя {user_id}')
    await callback.answer()


@admin_required
@error_handler
async def process_devices_edit_text(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    user_id = data.get('editing_devices_user_id')
    texts = get_texts(db_user.language)

    if not user_id:
        await message.answer(texts.t('ADMIN_USER_BALANCE_EDIT_ERROR_NOT_FOUND'))
        await state.clear()
        return

    try:
        devices = int(message.text.strip())

        if devices <= 0 or devices > 10:
            await message.answer(texts.t('ADMIN_USER_DEVICES_EDIT_INVALID_RANGE'))
            return

        success = await _update_user_devices(db, user_id, devices, db_user.id)

        if success:
            await message.answer(
                texts.t('ADMIN_USER_DEVICES_EDIT_SUCCESS').format(devices=devices),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USER_SUBSCRIPTION_SETTINGS'),
                                callback_data=f'admin_user_subscription_{user_id}',
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USER_DEVICES_EDIT_ERROR'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USER_DEVICES_EDIT_PARSE_ERROR'))
        return

    await state.clear()


@admin_required
@error_handler
async def start_traffic_edit(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    await state.update_data(editing_traffic_user_id=user_id)

    await callback.message.edit_text(
        texts.t('ADMIN_USER_TRAFFIC_LIMIT_EDIT_PROMPT'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_GB_BUTTON').format(gb=50),
                        callback_data=f'admin_user_traffic_set_{user_id}_50',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_GB_BUTTON').format(gb=100),
                        callback_data=f'admin_user_traffic_set_{user_id}_100',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_GB_BUTTON').format(gb=500),
                        callback_data=f'admin_user_traffic_set_{user_id}_500',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_GB_BUTTON').format(gb=1000),
                        callback_data=f'admin_user_traffic_set_{user_id}_1000',
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_USER_SUB_UNLIMITED_BUTTON'),
                        callback_data=f'admin_user_traffic_set_{user_id}_0',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_user_subscription_{user_id}')],
            ]
        ),
    )

    await state.set_state(AdminStates.editing_user_traffic)
    await callback.answer()


@admin_required
@error_handler
async def set_user_traffic_button(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    user_id = int(parts[-2])
    traffic_gb = int(parts[-1])
    texts = get_texts(db_user.language)

    success = await _update_user_traffic(db, user_id, traffic_gb, db_user.id)

    if success:
        traffic_text = (
            texts.t('ADMIN_USER_SUB_TRAFFIC_UNLIMITED_VALUE')
            if traffic_gb == 0
            else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=traffic_gb)
        )
        await callback.message.edit_text(
            texts.t('ADMIN_USER_TRAFFIC_LIMIT_EDIT_SUCCESS').format(traffic=traffic_text),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USER_SUBSCRIPTION_SETTINGS'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_TRAFFIC_LIMIT_EDIT_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_USER_SUBSCRIPTION_SETTINGS'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


@admin_required
@error_handler
async def process_traffic_edit_text(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    user_id = data.get('editing_traffic_user_id')
    texts = get_texts(db_user.language)

    if not user_id:
        await message.answer(texts.t('ADMIN_USER_BALANCE_EDIT_ERROR_NOT_FOUND'))
        await state.clear()
        return

    try:
        traffic_gb = int(message.text.strip())

        if traffic_gb < 0 or traffic_gb > 10000:
            await message.answer(texts.t('ADMIN_USER_TRAFFIC_LIMIT_EDIT_INVALID_RANGE'))
            return

        success = await _update_user_traffic(db, user_id, traffic_gb, db_user.id)

        if success:
            traffic_text = (
                texts.t('ADMIN_USER_SUB_TRAFFIC_UNLIMITED_VALUE')
                if traffic_gb == 0
                else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=traffic_gb)
            )
            await message.answer(
                texts.t('ADMIN_USER_TRAFFIC_LIMIT_EDIT_SUCCESS').format(traffic=traffic_text),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USER_SUBSCRIPTION_SETTINGS'),
                                callback_data=f'admin_user_subscription_{user_id}',
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(texts.t('ADMIN_USER_TRAFFIC_LIMIT_EDIT_ERROR'))

    except ValueError:
        await message.answer(texts.t('ADMIN_USER_TRAFFIC_LIMIT_EDIT_PARSE_ERROR'))
        return

    await state.clear()


@admin_required
@error_handler
async def confirm_reset_devices(callback: types.CallbackQuery, db_user: User):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_USER_RESET_DEVICES_CONFIRM_TEXT'),
        reply_markup=get_confirmation_keyboard(
            f'admin_user_reset_devices_confirm_{user_id}', f'admin_user_subscription_{user_id}', db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def reset_user_devices(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.remnawave_uuid:
            await callback.answer(texts.t('ADMIN_USER_RESET_DEVICES_NOT_LINKED'), show_alert=True)
            return

        remnawave_service = RemnaWaveService()
        async with remnawave_service.get_api_client() as api:
            success = await api.reset_user_devices(user.remnawave_uuid)

        if success:
            await callback.message.edit_text(
                texts.t('ADMIN_USER_RESET_DEVICES_SUCCESS'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USER_SUBSCRIPTION_SETTINGS'),
                                callback_data=f'admin_user_subscription_{user_id}',
                            )
                        ]
                    ]
                ),
            )
            logger.info(f'Админ {db_user.id} сбросил устройства пользователя {user_id}')
        else:
            await callback.message.edit_text(
                texts.t('ADMIN_USER_RESET_DEVICES_ERROR'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_USER_SUBSCRIPTION_SETTINGS'),
                                callback_data=f'admin_user_subscription_{user_id}',
                            )
                        ]
                    ]
                ),
            )

    except Exception as e:
        logger.error(f'Ошибка сброса устройств: {e}')
        await callback.answer(texts.t('ADMIN_USER_RESET_DEVICES_ERROR'), show_alert=True)


async def _update_user_devices(db: AsyncSession, user_id: int, devices: int, admin_id: int) -> bool:
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.subscription:
            logger.error(f'Пользователь {user_id} или подписка не найдены')
            return False

        subscription = user.subscription
        old_devices = subscription.device_limit
        subscription.device_limit = devices
        subscription.updated_at = datetime.utcnow()

        await db.commit()

        if user.remnawave_uuid:
            try:
                remnawave_service = RemnaWaveService()
                async with remnawave_service.get_api_client() as api:
                    await api.update_user(
                        uuid=user.remnawave_uuid,
                        hwid_device_limit=devices,
                        description=settings.format_remnawave_user_description(
                            full_name=user.full_name, username=user.username, telegram_id=user.telegram_id
                        ),
                    )
                logger.info(f'✅ Обновлен лимит устройств в RemnaWave для пользователя {user.telegram_id}')
            except Exception as rw_error:
                logger.error(f'❌ Ошибка обновления лимита устройств в RemnaWave: {rw_error}')

        logger.info(f'Админ {admin_id} изменил лимит устройств пользователя {user_id}: {old_devices} -> {devices}')
        return True

    except Exception as e:
        logger.error(f'Ошибка обновления лимита устройств: {e}')
        await db.rollback()
        return False


async def _update_user_traffic(db: AsyncSession, user_id: int, traffic_gb: int, admin_id: int) -> bool:
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.subscription:
            logger.error(f'Пользователь {user_id} или подписка не найдены')
            return False

        subscription = user.subscription
        old_traffic = subscription.traffic_limit_gb
        subscription.traffic_limit_gb = traffic_gb
        subscription.updated_at = datetime.utcnow()

        await db.commit()

        if user.remnawave_uuid:
            try:
                from app.external.remnawave_api import TrafficLimitStrategy

                remnawave_service = RemnaWaveService()
                async with remnawave_service.get_api_client() as api:
                    await api.update_user(
                        uuid=user.remnawave_uuid,
                        traffic_limit_bytes=traffic_gb * (1024**3) if traffic_gb > 0 else 0,
                        traffic_limit_strategy=TrafficLimitStrategy.MONTH,
                        description=settings.format_remnawave_user_description(
                            full_name=user.full_name, username=user.username, telegram_id=user.telegram_id
                        ),
                    )
                logger.info(f'✅ Обновлен лимит трафика в RemnaWave для пользователя {user.telegram_id}')
            except Exception as rw_error:
                logger.error(f'❌ Ошибка обновления лимита трафика в RemnaWave: {rw_error}')

        traffic_text_old = 'безлимитный' if old_traffic == 0 else f'{old_traffic} ГБ'
        traffic_text_new = 'безлимитный' if traffic_gb == 0 else f'{traffic_gb} ГБ'
        logger.info(
            f'Админ {admin_id} изменил лимит трафика пользователя {user_id}: {traffic_text_old} -> {traffic_text_new}'
        )
        return True

    except Exception as e:
        logger.error(f'Ошибка обновления лимита трафика: {e}')
        await db.rollback()
        return False


async def _extend_subscription_by_days(db: AsyncSession, user_id: int, days: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import extend_subscription, get_subscription_by_user_id
        from app.services.subscription_service import SubscriptionService

        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f'Подписка не найдена для пользователя {user_id}')
            return False

        await extend_subscription(db, subscription, days)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        if days > 0:
            logger.info(f'Админ {admin_id} продлил подписку пользователя {user_id} на {days} дней')
        else:
            logger.info(f'Админ {admin_id} сократил подписку пользователя {user_id} на {abs(days)} дней')
        return True

    except Exception as e:
        logger.error(f'Ошибка продления подписки: {e}')
        return False


async def _add_subscription_traffic(db: AsyncSession, user_id: int, gb: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import add_subscription_traffic, get_subscription_by_user_id
        from app.services.subscription_service import SubscriptionService

        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f'Подписка не найдена для пользователя {user_id}')
            return False

        if gb == 0:
            subscription.traffic_limit_gb = 0
            await db.commit()
        else:
            await add_subscription_traffic(db, subscription, gb)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        traffic_text = 'безлимитный' if gb == 0 else f'{gb} ГБ'
        logger.info(f'Админ {admin_id} добавил трафик {traffic_text} пользователю {user_id}')
        return True

    except Exception as e:
        logger.error(f'Ошибка добавления трафика: {e}')
        return False


async def _deactivate_user_subscription(db: AsyncSession, user_id: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import deactivate_subscription, get_subscription_by_user_id
        from app.services.subscription_service import SubscriptionService

        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f'Подписка не найдена для пользователя {user_id}')
            return False

        await deactivate_subscription(db, subscription)

        user = await get_user_by_id(db, user_id)
        if user and user.remnawave_uuid:
            subscription_service = SubscriptionService()
            await subscription_service.disable_remnawave_user(user.remnawave_uuid)

        logger.info(f'Админ {admin_id} деактивировал подписку пользователя {user_id}')
        return True

    except Exception as e:
        logger.error(f'Ошибка деактивации подписки: {e}')
        return False


async def _activate_user_subscription(db: AsyncSession, user_id: int, admin_id: int) -> bool:
    try:
        from datetime import datetime

        from app.database.crud.subscription import get_subscription_by_user_id
        from app.database.models import SubscriptionStatus
        from app.services.subscription_service import SubscriptionService

        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f'Подписка не найдена для пользователя {user_id}')
            return False

        subscription.status = SubscriptionStatus.ACTIVE.value
        if subscription.end_date <= datetime.utcnow():
            subscription.end_date = datetime.utcnow() + timedelta(days=1)

        await db.commit()
        await db.refresh(subscription)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        logger.info(f'Админ {admin_id} активировал подписку пользователя {user_id}')
        return True

    except Exception as e:
        logger.error(f'Ошибка активации подписки: {e}')
        return False


async def _grant_trial_subscription(db: AsyncSession, user_id: int, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import create_trial_subscription, get_subscription_by_user_id
        from app.services.subscription_service import SubscriptionService

        existing_subscription = await get_subscription_by_user_id(db, user_id)
        if existing_subscription:
            logger.error(f'У пользователя {user_id} уже есть подписка')
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

        logger.info(f'Админ {admin_id} выдал триальную подписку пользователю {user_id}')
        return True

    except Exception as e:
        logger.error(f'Ошибка выдачи триальной подписки: {e}')
        return False


async def _grant_paid_subscription(db: AsyncSession, user_id: int, days: int, admin_id: int) -> bool:
    try:
        from app.config import settings
        from app.database.crud.subscription import create_paid_subscription, get_subscription_by_user_id
        from app.services.subscription_service import SubscriptionService

        existing_subscription = await get_subscription_by_user_id(db, user_id)
        if existing_subscription:
            logger.error(f'У пользователя {user_id} уже есть подписка')
            return False

        trial_squads: list[str] = []

        try:
            from app.database.crud.server_squad import get_random_trial_squad_uuid

            trial_uuid = await get_random_trial_squad_uuid(db)
            if trial_uuid:
                trial_squads = [trial_uuid]
        except Exception as error:
            logger.error(
                'Не удалось подобрать сквад при выдаче подписки админом %s: %s',
                admin_id,
                error,
            )

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

        logger.info(f'Админ {admin_id} выдал платную подписку на {days} дней пользователю {user_id}')
        return True

    except Exception as e:
        logger.error(f'Ошибка выдачи платной подписки: {e}')
        return False


async def _calculate_subscription_period_price(
    db: AsyncSession,
    target_user: User,
    subscription: Subscription,
    period_days: int,
    subscription_service: SubscriptionService | None = None,
) -> int:
    """Рассчитывает стоимость подписки для администратора с учётом всех параметров."""

    service = subscription_service or SubscriptionService()

    connected_squads = list(subscription.connected_squads or [])
    server_ids = []

    if connected_squads:
        try:
            server_ids = await get_server_ids_by_uuids(db, connected_squads)
            if len(server_ids) != len(connected_squads):
                logger.warning(
                    'Не удалось сопоставить все сервера подписки пользователя %s для расчёта цены',
                    target_user.telegram_id,
                )
        except Exception as e:
            logger.error(
                'Не удалось получить идентификаторы серверов для расчёта цены подписки пользователя %s: %s',
                target_user.telegram_id,
                e,
            )
            server_ids = []
    traffic_limit_gb = subscription.traffic_limit_gb
    if traffic_limit_gb is None:
        traffic_limit_gb = settings.DEFAULT_TRAFFIC_LIMIT_GB

    device_limit = subscription.device_limit
    if not device_limit or device_limit < 0:
        device_limit = settings.DEFAULT_DEVICE_LIMIT

    total_price, _ = await service.calculate_subscription_price(
        period_days=period_days,
        traffic_gb=traffic_limit_gb,
        server_squad_ids=server_ids,
        devices=device_limit,
        db=db,
        user=target_user,
        promo_group=target_user.promo_group,
    )

    return total_price


@admin_required
@error_handler
async def cleanup_inactive_users(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_service = UserService()
    deleted_count, skipped_count = await user_service.cleanup_inactive_users(db)
    texts = get_texts(db_user.language)

    text = texts.t('ADMIN_USERS_CLEANUP_DONE').format(count=deleted_count)
    if skipped_count > 0:
        text += f'\n⏭️ Пропущено (активная подписка): {skipped_count}'

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_users')]]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def change_subscription_type(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile or not profile['subscription']:
        await callback.answer(texts.t('ADMIN_USER_OR_SUBSCRIPTION_NOT_FOUND'), show_alert=True)
        return

    subscription = profile['subscription']
    current_type = (
        texts.t('ADMIN_USER_SUBSCRIPTION_TYPE_TRIAL')
        if subscription.is_trial
        else texts.t('ADMIN_USER_SUBSCRIPTION_TYPE_PAID')
    )

    text = texts.t('ADMIN_USER_SUB_TYPE_CHANGE_TITLE') + '\n\n'
    text += f'👤 {profile["user"].full_name}\n'
    text += texts.t('ADMIN_USER_SUB_TYPE_CURRENT').format(type=current_type) + '\n\n'
    text += texts.t('ADMIN_USER_SUB_TYPE_SELECT_PROMPT')

    keyboard = []

    if subscription.is_trial:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_TYPE_MAKE_PAID'), callback_data=f'admin_sub_type_paid_{user_id}'
                )
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_TYPE_MAKE_TRIAL'),
                    callback_data=f'admin_sub_type_trial_{user_id}',
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton(text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}')]
    )

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_subscription(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    target_user = profile['user']
    subscription = profile['subscription']

    if not subscription:
        await callback.answer(texts.t('ADMIN_USER_HAS_NO_SUBSCRIPTION'), show_alert=True)
        return

    available_periods = settings.get_available_subscription_periods()

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
                'Ошибка расчёта стоимости подписки для пользователя %s и периода %s дней: %s',
                target_user.telegram_id,
                period,
                e,
            )
            continue

        period_buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_SUB_PERIOD_PRICE_BUTTON').format(
                        days=period,
                        price=settings.format_price(price_kopeks),
                    ),
                    callback_data=f'admin_buy_sub_confirm_{user_id}_{period}_{price_kopeks}',
                )
            ]
        )

    if not period_buttons:
        await callback.answer(texts.t('ADMIN_USER_SUB_PRICE_CALC_ERROR'), show_alert=True)
        return

    period_buttons.append(
        [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_user_subscription_{user_id}')]
    )

    text = texts.t('ADMIN_USER_SUB_BUY_TITLE') + '\n\n'
    if target_user.telegram_id:
        target_user_link = f'<a href="tg://user?id={target_user.telegram_id}">{target_user.full_name}</a>'
        target_user_id_display = target_user.telegram_id
    else:
        target_user_link = f'<b>{target_user.full_name}</b>'
        target_user_id_display = target_user.email or f'#{target_user.id}'
    text += f'👤 {target_user_link} (ID: {target_user_id_display})\n'
    text += (
        texts.t('ADMIN_USER_SUB_USER_BALANCE_LINE').format(balance=settings.format_price(target_user.balance_kopeks))
        + '\n\n'
    )
    traffic_text = (
        texts.t('ADMIN_USER_SUB_UNLIMITED_TEXT')
        if (subscription.traffic_limit_gb or 0) <= 0
        else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=subscription.traffic_limit_gb)
    )
    devices_limit = subscription.device_limit
    if devices_limit is None:
        devices_limit = settings.DEFAULT_DEVICE_LIMIT
    servers_count = len(subscription.connected_squads or [])
    text += texts.t('ADMIN_USER_SUB_TRAFFIC_LINE').format(traffic=traffic_text) + '\n'
    text += texts.t('ADMIN_USER_SUB_DEVICES_LINE').format(devices=devices_limit) + '\n'
    text += texts.t('ADMIN_USER_SUB_SERVERS_LINE').format(count=servers_count) + '\n\n'
    text += texts.t('ADMIN_USER_SUB_SELECT_PERIOD_PROMPT') + '\n'

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=period_buttons))
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_subscription_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    user_id = int(parts[4])
    period_days = int(parts[5])
    price_kopeks_from_callback = int(parts[6]) if len(parts) > 6 else None
    texts = get_texts(db_user.language)

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    target_user = profile['user']
    subscription = profile['subscription']

    if not subscription:
        await callback.answer(texts.t('ADMIN_USER_HAS_NO_SUBSCRIPTION'), show_alert=True)
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
            'Ошибка расчёта стоимости подписки при подтверждении админом для пользователя %s: %s',
            target_user.telegram_id,
            e,
        )
        await callback.answer(texts.t('ADMIN_USER_SUB_PRICE_CALC_ERROR'), show_alert=True)
        return

    if price_kopeks_from_callback is not None and price_kopeks_from_callback != price_kopeks:
        logger.info(
            'Стоимость подписки для пользователя %s изменилась с %s до %s при подтверждении',
            target_user.telegram_id,
            price_kopeks_from_callback,
            price_kopeks,
        )

    if target_user.balance_kopeks < price_kopeks:
        missing_kopeks = price_kopeks - target_user.balance_kopeks
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_INSUFFICIENT_FUNDS_TEXT').format(
                balance=settings.format_price(target_user.balance_kopeks),
                price=settings.format_price(price_kopeks),
                missing=settings.format_price(missing_kopeks),
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )
        await callback.answer()
        return

    text = texts.t('ADMIN_USER_SUB_BUY_CONFIRM_TITLE') + '\n\n'
    if target_user.telegram_id:
        target_user_link = f'<a href="tg://user?id={target_user.telegram_id}">{target_user.full_name}</a>'
        target_user_id_display = target_user.telegram_id
    else:
        target_user_link = f'<b>{target_user.full_name}</b>'
        target_user_id_display = target_user.email or f'#{target_user.id}'
    text += f'👤 {target_user_link} (ID: {target_user_id_display})\n'
    text += texts.t('ADMIN_USER_SUB_PERIOD_LINE').format(days=period_days) + '\n'
    text += texts.t('ADMIN_USER_SUB_PRICE_LINE').format(price=settings.format_price(price_kopeks)) + '\n'
    text += (
        texts.t('ADMIN_USER_SUB_USER_BALANCE_LINE').format(balance=settings.format_price(target_user.balance_kopeks))
        + '\n\n'
    )
    traffic_text = (
        texts.t('ADMIN_USER_SUB_UNLIMITED_TEXT')
        if (subscription.traffic_limit_gb or 0) <= 0
        else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=subscription.traffic_limit_gb)
    )
    devices_limit = subscription.device_limit
    if devices_limit is None:
        devices_limit = settings.DEFAULT_DEVICE_LIMIT
    servers_count = len(subscription.connected_squads or [])
    text += texts.t('ADMIN_USER_SUB_TRAFFIC_LINE').format(traffic=traffic_text) + '\n'
    text += texts.t('ADMIN_USER_SUB_DEVICES_LINE').format(devices=devices_limit) + '\n'
    text += texts.t('ADMIN_USER_SUB_SERVERS_LINE').format(count=servers_count) + '\n\n'
    text += texts.t('ADMIN_USER_SUB_BUY_CONFIRM_PROMPT')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.CONFIRM,
                callback_data=f'admin_buy_sub_execute_{user_id}_{period_days}_{price_kopeks}',
            )
        ],
        [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_sub_buy_{user_id}')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_subscription_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    user_id = int(parts[4])
    period_days = int(parts[5])
    price_kopeks_from_callback = int(parts[6]) if len(parts) > 6 else None
    texts = get_texts(db_user.language)

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    target_user = profile['user']
    subscription = profile['subscription']

    if not subscription:
        await callback.answer(texts.t('ADMIN_USER_HAS_NO_SUBSCRIPTION'), show_alert=True)
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
            'Ошибка расчёта стоимости подписки при списании средств админом для пользователя %s: %s',
            target_user.telegram_id,
            e,
        )
        await callback.answer(texts.t('ADMIN_USER_SUB_PRICE_CALC_ERROR'), show_alert=True)
        return

    if price_kopeks_from_callback is not None and price_kopeks_from_callback != price_kopeks:
        logger.info(
            'Стоимость подписки для пользователя %s изменилась с %s до %s перед списанием',
            target_user.telegram_id,
            price_kopeks_from_callback,
            price_kopeks,
        )

    if target_user.balance_kopeks < price_kopeks:
        await callback.answer(texts.t('ADMIN_USER_SUB_INSUFFICIENT_FUNDS_ALERT'), show_alert=True)
        return

    try:
        from app.database.crud.user import subtract_user_balance

        success = await subtract_user_balance(
            db,
            target_user,
            price_kopeks,
            texts.t('ADMIN_USER_SUB_PURCHASE_BALANCE_DESC').format(days=period_days),
        )

        if not success:
            await callback.answer(texts.t('ADMIN_USER_SUB_BALANCE_DEBIT_ERROR'), show_alert=True)
            return

        if subscription:
            current_time = datetime.utcnow()
            bonus_period = timedelta()

            if subscription.is_trial and settings.TRIAL_ADD_REMAINING_DAYS_TO_PAID and subscription.end_date:
                remaining_trial_delta = subscription.end_date - current_time
                if remaining_trial_delta.total_seconds() > 0:
                    bonus_period = remaining_trial_delta
                    logger.info(
                        'Админ продлевает подписку: добавляем оставшееся время триала (%s) пользователю %s',
                        bonus_period,
                        target_user.telegram_id,
                    )

            extension_base_date = current_time
            if subscription.end_date and subscription.end_date > current_time:
                extension_base_date = subscription.end_date
            else:
                subscription.start_date = current_time

            subscription.end_date = extension_base_date + timedelta(days=period_days) + bonus_period
            subscription.status = SubscriptionStatus.ACTIVE.value
            subscription.updated_at = current_time

            if subscription.is_trial or not subscription.is_active:
                was_trial = subscription.is_trial
                subscription.is_trial = False
                if subscription.traffic_limit_gb != 0:
                    subscription.traffic_limit_gb = 0
                subscription.device_limit = settings.DEFAULT_DEVICE_LIMIT
                if was_trial:
                    subscription.traffic_used_gb = 0.0

            await db.commit()
            await db.refresh(subscription)

            from app.database.crud.transaction import create_transaction

            await create_transaction(
                db=db,
                user_id=target_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=price_kopeks,
                description=texts.t('ADMIN_USER_SUB_EXTEND_TRANSACTION_DESC').format(days=period_days),
            )

            try:
                from app.external.remnawave_api import TrafficLimitStrategy, UserStatus
                from app.services.remnawave_service import RemnaWaveService

                remnawave_service = RemnaWaveService()

                hwid_limit = resolve_hwid_device_limit_for_payload(subscription)

                if target_user.remnawave_uuid:
                    async with remnawave_service.get_api_client() as api:
                        update_kwargs = dict(
                            uuid=target_user.remnawave_uuid,
                            status=UserStatus.ACTIVE if subscription.is_active else UserStatus.EXPIRED,
                            expire_at=subscription.end_date,
                            traffic_limit_bytes=subscription.traffic_limit_gb * (1024**3)
                            if subscription.traffic_limit_gb > 0
                            else 0,
                            traffic_limit_strategy=TrafficLimitStrategy.MONTH,
                            description=settings.format_remnawave_user_description(
                                full_name=target_user.full_name,
                                username=target_user.username,
                                telegram_id=target_user.telegram_id,
                                email=target_user.email,
                                user_id=target_user.id,
                            ),
                            active_internal_squads=subscription.connected_squads,
                        )

                        if hwid_limit is not None:
                            update_kwargs['hwid_device_limit'] = hwid_limit

                        remnawave_user = await api.update_user(**update_kwargs)
                else:
                    username = settings.format_remnawave_username(
                        full_name=target_user.full_name,
                        username=target_user.username,
                        telegram_id=target_user.telegram_id,
                        email=target_user.email,
                        user_id=target_user.id,
                    )
                    async with remnawave_service.get_api_client() as api:
                        create_kwargs = dict(
                            username=username,
                            expire_at=subscription.end_date,
                            status=UserStatus.ACTIVE if subscription.is_active else UserStatus.EXPIRED,
                            traffic_limit_bytes=subscription.traffic_limit_gb * (1024**3)
                            if subscription.traffic_limit_gb > 0
                            else 0,
                            traffic_limit_strategy=TrafficLimitStrategy.MONTH,
                            telegram_id=target_user.telegram_id,
                            email=target_user.email,
                            description=settings.format_remnawave_user_description(
                                full_name=target_user.full_name,
                                username=target_user.username,
                                telegram_id=target_user.telegram_id,
                                email=target_user.email,
                            ),
                            active_internal_squads=subscription.connected_squads,
                        )

                        if hwid_limit is not None:
                            create_kwargs['hwid_device_limit'] = hwid_limit

                        remnawave_user = await api.create_user(**create_kwargs)

                    if remnawave_user and hasattr(remnawave_user, 'uuid'):
                        target_user.remnawave_uuid = remnawave_user.uuid
                        await db.commit()

                if remnawave_user:
                    logger.info(f'Пользователь {target_user.telegram_id} успешно обновлен в RemnaWave')
                else:
                    logger.error(f'Ошибка обновления пользователя {target_user.telegram_id} в RemnaWave')
            except Exception as e:
                logger.error(f'Ошибка работы с RemnaWave для пользователя {target_user.telegram_id}: {e}')

            message = texts.t('ADMIN_USER_SUB_ADMIN_EXTEND_SUCCESS').format(days=period_days)
        else:
            message = texts.t('ADMIN_USER_SUB_EXISTING_MISSING_ERROR')

        if target_user.telegram_id:
            target_user_link = f'<a href="tg://user?id={target_user.telegram_id}">{target_user.full_name}</a>'
            target_user_id_display = target_user.telegram_id
        else:
            target_user_link = f'<b>{target_user.full_name}</b>'
            target_user_id_display = target_user.email or f'#{target_user.id}'
        await callback.message.edit_text(
            f'{message}\n\n'
            f'👤 {target_user_link} (ID: {target_user_id_display})\n'
            f'{texts.t("ADMIN_USER_SUB_CHARGED_LINE").format(amount=settings.format_price(price_kopeks))}\n'
            f'{texts.t("ADMIN_USER_SUB_VALID_UNTIL_LINE").format(date=format_datetime(subscription.end_date))}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )

        try:
            if callback.bot and target_user.telegram_id:
                await callback.bot.send_message(
                    chat_id=target_user.telegram_id,
                    text=texts.t('ADMIN_USER_SUB_NOTIFICATION_EXTENDED').format(
                        days=period_days,
                        amount=settings.format_price(price_kopeks),
                        date=format_datetime(subscription.end_date),
                    ),
                    parse_mode='HTML',
                )
        except Exception as e:
            user_id_display = target_user.telegram_id or target_user.email or f'#{target_user.id}'
            logger.error(f'Ошибка отправки уведомления пользователю {user_id_display}: {e}')

        await callback.answer()

    except Exception as e:
        logger.error(f'Ошибка покупки подписки администратором: {e}')
        await callback.answer(texts.t('ADMIN_USER_SUB_PURCHASE_ERROR'), show_alert=True)

        await db.rollback()


# ==================== Покупка тарифа администратором ====================


@admin_required
@error_handler
async def admin_buy_tariff(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает список тарифов для покупки админом."""
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    target_user = profile['user']

    # Получаем доступные тарифы
    from app.database.crud.tariff import get_tariffs_for_user

    tariffs = await get_tariffs_for_user(db, target_user)

    if not tariffs:
        await callback.message.edit_text(
            texts.t('ADMIN_TARIFFS_NONE_AVAILABLE'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )
        await callback.answer()
        return

    if target_user.telegram_id:
        target_user_link = f'<a href="tg://user?id={target_user.telegram_id}">{target_user.full_name}</a>'
        target_user_id_display = target_user.telegram_id
    else:
        target_user_link = f'<b>{target_user.full_name}</b>'
        target_user_id_display = target_user.email or f'#{target_user.id}'
    text = texts.t('ADMIN_TARIFF_BUY_TITLE') + '\n\n'
    text += f'👤 {target_user_link} (ID: {target_user_id_display})\n'
    text += (
        texts.t('ADMIN_TARIFF_BALANCE_LINE').format(balance=settings.format_price(target_user.balance_kopeks)) + '\n\n'
    )
    text += texts.t('ADMIN_TARIFF_SELECT_PROMPT') + '\n\n'

    for tariff in tariffs:
        traffic = (
            texts.t('ADMIN_TARIFF_UNLIMITED_SYMBOL')
            if tariff.traffic_limit_gb == 0
            else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=tariff.traffic_limit_gb)
        )
        prices = tariff.period_prices or {}
        min_price = min(prices.values()) if prices else 0
        text += (
            texts.t('ADMIN_TARIFF_LIST_ITEM').format(
                name=tariff.name,
                traffic=traffic,
                devices=tariff.device_limit,
                price=settings.format_price(min_price),
            )
            + '\n'
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
        [
            types.InlineKeyboardButton(
                text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
            )
        ]
    )

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_tariff_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает выбор периода для тарифа."""
    parts = callback.data.split('_')
    user_id = int(parts[4])
    tariff_id = int(parts[5])
    texts = get_texts(db_user.language)

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    target_user = profile['user']

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_AVAILABLE'), show_alert=True)
        return

    if target_user.telegram_id:
        target_user_link = f'<a href="tg://user?id={target_user.telegram_id}">{target_user.full_name}</a>'
        target_user_id_display = target_user.telegram_id
    else:
        target_user_link = f'<b>{target_user.full_name}</b>'
        target_user_id_display = target_user.email or f'#{target_user.id}'
    traffic = (
        texts.t('ADMIN_TARIFF_UNLIMITED_TEXT')
        if tariff.traffic_limit_gb == 0
        else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=tariff.traffic_limit_gb)
    )

    text = texts.t('ADMIN_TARIFF_BUY_TITLE') + '\n\n'
    text += f'👤 {target_user_link} (ID: {target_user_id_display})\n'
    text += (
        texts.t('ADMIN_TARIFF_BALANCE_LINE').format(balance=settings.format_price(target_user.balance_kopeks)) + '\n\n'
    )
    text += texts.t('ADMIN_TARIFF_LINE').format(name=tariff.name) + '\n'
    text += texts.t('ADMIN_TARIFF_TRAFFIC_LINE').format(traffic=traffic) + '\n'
    text += texts.t('ADMIN_TARIFF_DEVICES_LINE').format(count=tariff.device_limit) + '\n'
    text += (
        texts.t('ADMIN_TARIFF_SERVERS_LINE').format(count=len(tariff.allowed_squads) if tariff.allowed_squads else 0)
        + '\n\n'
    )
    text += texts.t('ADMIN_TARIFF_SELECT_PERIOD_ONLY_PROMPT')

    prices = tariff.period_prices or {}
    keyboard = []

    for period_str, price in sorted(prices.items(), key=lambda x: int(x[0])):
        period = int(period_str)
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_TARIFF_PERIOD_PRICE_BUTTON').format(
                        days=period,
                        price=settings.format_price(price),
                    ),
                    callback_data=f'admin_tariff_buy_confirm_{user_id}_{tariff_id}_{period}_{price}',
                )
            ]
        )

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_TARIFF_BACK_TO_LIST'), callback_data=f'admin_tariff_buy_{user_id}'
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
    parts = callback.data.split('_')
    user_id = int(parts[4])
    tariff_id = int(parts[5])
    period = int(parts[6])
    price_kopeks = int(parts[7])
    texts = get_texts(db_user.language)

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    target_user = profile['user']

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_AVAILABLE'), show_alert=True)
        return

    # Проверяем баланс
    if target_user.balance_kopeks < price_kopeks:
        missing = price_kopeks - target_user.balance_kopeks
        await callback.message.edit_text(
            texts.t('ADMIN_TARIFF_INSUFFICIENT_FUNDS_TEXT').format(
                balance=settings.format_price(target_user.balance_kopeks),
                price=settings.format_price(price_kopeks),
                missing=settings.format_price(missing),
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.BACK,
                            callback_data=f'admin_tariff_buy_select_{user_id}_{tariff_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    if target_user.telegram_id:
        target_user_link = f'<a href="tg://user?id={target_user.telegram_id}">{target_user.full_name}</a>'
        target_user_id_display = target_user.telegram_id
    else:
        target_user_link = f'<b>{target_user.full_name}</b>'
        target_user_id_display = target_user.email or f'#{target_user.id}'
    traffic = (
        texts.t('ADMIN_TARIFF_UNLIMITED_TEXT')
        if tariff.traffic_limit_gb == 0
        else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=tariff.traffic_limit_gb)
    )

    text = texts.t('ADMIN_TARIFF_BUY_CONFIRM_TITLE') + '\n\n'
    text += f'👤 {target_user_link} (ID: {target_user_id_display})\n'
    text += (
        texts.t('ADMIN_TARIFF_BALANCE_LINE').format(balance=settings.format_price(target_user.balance_kopeks)) + '\n\n'
    )
    text += texts.t('ADMIN_TARIFF_LINE').format(name=tariff.name) + '\n'
    text += texts.t('ADMIN_TARIFF_TRAFFIC_LINE').format(traffic=traffic) + '\n'
    text += texts.t('ADMIN_TARIFF_DEVICES_LINE').format(count=tariff.device_limit) + '\n'
    text += texts.t('ADMIN_TARIFF_PERIOD_LINE').format(days=period) + '\n'
    text += texts.t('ADMIN_TARIFF_PRICE_LINE').format(price=settings.format_price(price_kopeks)) + '\n\n'
    text += texts.t('ADMIN_TARIFF_BUY_CONFIRM_PROMPT')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.CONFIRM,
                callback_data=f'admin_tariff_buy_exec_{user_id}_{tariff_id}_{period}_{price_kopeks}',
            )
        ],
        [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_buy_select_{user_id}_{tariff_id}')],
    ]

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def admin_buy_tariff_execute(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Выполняет покупку тарифа для пользователя."""
    parts = callback.data.split('_')
    user_id = int(parts[4])
    tariff_id = int(parts[5])
    period = int(parts[6])
    price_kopeks = int(parts[7])
    texts = get_texts(db_user.language)

    user_service = UserService()
    profile = await user_service.get_user_profile(db, user_id)

    if not profile:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    target_user = profile['user']

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, tariff_id)

    if not tariff or not tariff.is_active:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_AVAILABLE'), show_alert=True)
        return

    # Проверяем баланс ещё раз
    if target_user.balance_kopeks < price_kopeks:
        await callback.answer(texts.t('ADMIN_TARIFF_INSUFFICIENT_FUNDS_ALERT'), show_alert=True)
        return

    try:
        from app.database.crud.subscription import (
            create_paid_subscription,
            extend_subscription,
            get_subscription_by_user_id,
        )
        from app.database.crud.transaction import create_transaction
        from app.database.crud.user import subtract_user_balance
        from app.services.subscription_service import SubscriptionService

        # Списываем баланс
        success = await subtract_user_balance(
            db,
            target_user,
            price_kopeks,
            texts.t('ADMIN_TARIFF_PURCHASE_BALANCE_DESC').format(name=tariff.name, days=period),
        )

        if not success:
            await callback.answer(texts.t('ADMIN_TARIFF_BALANCE_DEBIT_ERROR'), show_alert=True)
            return

        # Получаем серверы из тарифа
        squads = tariff.allowed_squads or []

        # Проверяем есть ли подписка
        existing_subscription = await get_subscription_by_user_id(db, target_user.id)

        if existing_subscription:
            # Продлеваем существующую подписку
            subscription = await extend_subscription(
                db,
                existing_subscription,
                days=period,
                tariff_id=tariff.id,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=squads,
            )
        else:
            # Создаем новую подписку
            subscription = await create_paid_subscription(
                db=db,
                user_id=target_user.id,
                duration_days=period,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=squads,
                tariff_id=tariff.id,
            )

        # Обновляем в Remnawave
        try:
            subscription_service = SubscriptionService()
            await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                reset_reason=texts.t('ADMIN_TARIFF_RESET_REASON'),
            )
        except Exception as e:
            logger.error(f'Ошибка обновления Remnawave: {e}')

        # Создаем транзакцию
        await create_transaction(
            db,
            user_id=target_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=-price_kopeks,
            description=texts.t('ADMIN_TARIFF_PURCHASE_TRANSACTION_DESC').format(name=tariff.name, days=period),
        )

        if target_user.telegram_id:
            target_user_link = f'<a href="tg://user?id={target_user.telegram_id}">{target_user.full_name}</a>'
            target_user_id_display = target_user.telegram_id
        else:
            target_user_link = f'<b>{target_user.full_name}</b>'
            target_user_id_display = target_user.email or f'#{target_user.id}'
        traffic = (
            texts.t('ADMIN_TARIFF_UNLIMITED_TEXT')
            if tariff.traffic_limit_gb == 0
            else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=tariff.traffic_limit_gb)
        )

        await callback.message.edit_text(
            f'{texts.t("ADMIN_TARIFF_PURCHASE_SUCCESS_TITLE")}\n\n'
            f'👤 {target_user_link} (ID: {target_user_id_display})\n'
            f'{texts.t("ADMIN_TARIFF_LINE").format(name=tariff.name)}\n'
            f'{texts.t("ADMIN_TARIFF_TRAFFIC_LINE").format(traffic=traffic)}\n'
            f'{texts.t("ADMIN_TARIFF_DEVICES_LINE").format(count=tariff.device_limit)}\n'
            f'{texts.t("ADMIN_TARIFF_PERIOD_LINE").format(days=period)}\n'
            f'{texts.t("ADMIN_USER_SUB_CHARGED_LINE").format(amount=settings.format_price(price_kopeks))}\n'
            f'{texts.t("ADMIN_TARIFF_VALID_UNTIL_LINE").format(date=format_datetime(subscription.end_date))}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
            parse_mode='HTML',
        )

        # Уведомляем пользователя
        try:
            if callback.bot and target_user.telegram_id:
                await callback.bot.send_message(
                    chat_id=target_user.telegram_id,
                    text=texts.t('ADMIN_TARIFF_PURCHASE_NOTIFICATION').format(
                        name=tariff.name,
                        traffic=traffic,
                        devices=tariff.device_limit,
                        days=period,
                        amount=settings.format_price(price_kopeks),
                        date=format_datetime(subscription.end_date),
                    ),
                    parse_mode='HTML',
                )
        except Exception as e:
            logger.error(f'Ошибка отправки уведомления пользователю: {e}')

        await callback.answer(texts.t('ADMIN_TARIFF_PURCHASE_SUCCESS_ALERT'), show_alert=True)

    except Exception as e:
        logger.error(f'Ошибка покупки тарифа администратором: {e}', exc_info=True)
        await callback.answer(texts.t('ADMIN_TARIFF_PURCHASE_ERROR_ALERT'), show_alert=True)
        await db.rollback()


@admin_required
@error_handler
async def change_subscription_type_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    parts = callback.data.split('_')
    new_type = parts[-2]  # 'paid' или 'trial'
    user_id = int(parts[-1])
    texts = get_texts(db_user.language)

    success = await _change_subscription_type(db, user_id, new_type, db_user.id)

    if success:
        type_text = (
            texts.t('ADMIN_USER_SUB_TYPE_GENITIVE_PAID')
            if new_type == 'paid'
            else texts.t('ADMIN_USER_SUB_TYPE_GENITIVE_TRIAL')
        )
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_TYPE_CHANGE_SUCCESS').format(type=type_text),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.edit_text(
            texts.t('ADMIN_USER_SUB_TYPE_CHANGE_ERROR'),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


async def _change_subscription_type(db: AsyncSession, user_id: int, new_type: str, admin_id: int) -> bool:
    try:
        from app.database.crud.subscription import get_subscription_by_user_id
        from app.services.subscription_service import SubscriptionService

        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            logger.error(f'Подписка не найдена для пользователя {user_id}')
            return False

        new_is_trial = new_type == 'trial'

        if subscription.is_trial == new_is_trial:
            logger.info(f'Тип подписки уже установлен корректно для пользователя {user_id}')
            return True

        old_type = 'триальной' if subscription.is_trial else 'платной'
        new_type_text = 'триальной' if new_is_trial else 'платной'

        subscription.is_trial = new_is_trial
        subscription.updated_at = datetime.utcnow()

        if not new_is_trial and subscription.is_trial:
            user = await get_user_by_id(db, user_id)
            if user:
                user.has_had_paid_subscription = True

        await db.commit()

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        logger.info(f'Админ {admin_id} изменил тип подписки пользователя {user_id}: {old_type} -> {new_type_text}')
        return True

    except Exception as e:
        logger.error(f'Ошибка изменения типа подписки: {e}')
        await db.rollback()
        return False


# =============================================================================
# Смена тарифа пользователя администратором
# =============================================================================


@admin_required
@error_handler
async def show_admin_tariff_change(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает список доступных тарифов для смены."""
    user_id = int(callback.data.split('_')[-1])
    texts = get_texts(db_user.language)

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    from app.database.crud.subscription import get_subscription_by_user_id

    subscription = await get_subscription_by_user_id(db, user_id)

    if not subscription:
        await callback.answer(texts.t('ADMIN_USER_HAS_NO_SUBSCRIPTION'), show_alert=True)
        return

    # Получаем все активные тарифы
    tariffs = await get_all_tariffs(db, include_inactive=False)

    if not tariffs:
        await callback.message.edit_text(
            texts.t('ADMIN_TARIFFS_NONE_AVAILABLE'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'),
                            callback_data=f'admin_user_subscription_{user_id}',
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

    text = texts.t('ADMIN_TARIFF_CHANGE_TITLE') + '\n\n'
    if user.telegram_id:
        user_link = f'<a href="tg://user?id={user.telegram_id}">{user.full_name}</a>'
    else:
        user_link = f'<b>{user.full_name}</b> ({user.email or f"#{user.id}"})'
    text += f'👤 {user_link}\n\n'

    if current_tariff:
        text += texts.t('ADMIN_TARIFF_CHANGE_CURRENT_LINE').format(name=current_tariff.name) + '\n\n'
    else:
        text += texts.t('ADMIN_TARIFF_CHANGE_CURRENT_NONE') + '\n\n'

    text += texts.t('ADMIN_TARIFF_CHANGE_SELECT_PROMPT') + '\n'

    keyboard = []
    for tariff in tariffs:
        # Отмечаем текущий тариф
        prefix = '✅ ' if current_tariff and tariff.id == current_tariff.id else ''

        # Описание тарифа
        traffic_str = (
            texts.t('ADMIN_TARIFF_UNLIMITED_SYMBOL')
            if tariff.traffic_limit_gb == 0
            else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=tariff.traffic_limit_gb)
        )
        servers_count = len(tariff.allowed_squads) if tariff.allowed_squads else 0

        button_text = texts.t('ADMIN_TARIFF_CHANGE_BUTTON').format(
            prefix=prefix,
            name=tariff.name,
            devices=tariff.device_limit,
            traffic=traffic_str,
            servers=servers_count,
        )

        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=button_text, callback_data=f'admin_sub_tariff_select_{tariff.id}_{user_id}'
                )
            ]
        )

    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('BACK_TO_SUBSCRIPTION'), callback_data=f'admin_user_subscription_{user_id}'
            )
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def select_admin_tariff_change(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Подтверждение выбора тарифа."""
    parts = callback.data.split('_')
    tariff_id = int(parts[-2])
    user_id = int(parts[-1])
    texts = get_texts(db_user.language)

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND'), show_alert=True)
        return

    from app.database.crud.subscription import get_subscription_by_user_id

    subscription = await get_subscription_by_user_id(db, user_id)

    if not subscription:
        await callback.answer(texts.t('ADMIN_USER_HAS_NO_SUBSCRIPTION'), show_alert=True)
        return

    # Проверяем, если это тот же тариф
    if subscription.tariff_id == tariff_id:
        await callback.answer(texts.t('ADMIN_TARIFF_ALREADY_SET'), show_alert=True)
        return

    traffic_str = (
        texts.t('ADMIN_TARIFF_UNLIMITED_SYMBOL')
        if tariff.traffic_limit_gb == 0
        else texts.t('ADMIN_USER_SUB_GB_VALUE').format(gb=tariff.traffic_limit_gb)
    )
    servers_count = len(tariff.allowed_squads) if tariff.allowed_squads else 0

    text = texts.t('ADMIN_TARIFF_CHANGE_CONFIRM_TITLE') + '\n\n'
    if user.telegram_id:
        user_link = f'<a href="tg://user?id={user.telegram_id}">{user.full_name}</a>'
    else:
        user_link = f'<b>{user.full_name}</b> ({user.email or f"#{user.id}"})'
    text += f'👤 {user_link}\n\n'
    text += texts.t('ADMIN_TARIFF_CHANGE_NEW_LINE').format(name=tariff.name) + '\n'
    text += texts.t('ADMIN_TARIFF_CHANGE_DEVICES_LINE').format(count=tariff.device_limit) + '\n'
    text += texts.t('ADMIN_TARIFF_CHANGE_TRAFFIC_LINE').format(traffic=traffic_str) + '\n'
    text += texts.t('ADMIN_TARIFF_CHANGE_SERVERS_LINE').format(count=servers_count) + '\n\n'
    text += texts.t('ADMIN_TARIFF_CHANGE_WARNING')

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.CONFIRM,
                callback_data=f'admin_sub_tariff_confirm_{tariff_id}_{user_id}',
            ),
            types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_sub_change_tariff_{user_id}'),
        ]
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def confirm_admin_tariff_change(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Применяет смену тарифа."""
    parts = callback.data.split('_')
    tariff_id = int(parts[-2])
    user_id = int(parts[-1])
    texts = get_texts(db_user.language)

    user = await get_user_by_id(db, user_id)
    if not user:
        await callback.answer(texts.t('ADMIN_USER_NOT_FOUND'), show_alert=True)
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer(texts.t('ADMIN_TARIFF_NOT_FOUND'), show_alert=True)
        return

    from app.database.crud.subscription import get_subscription_by_user_id

    subscription = await get_subscription_by_user_id(db, user_id)

    if not subscription:
        await callback.answer(texts.t('ADMIN_USER_HAS_NO_SUBSCRIPTION'), show_alert=True)
        return

    try:
        old_tariff_id = subscription.tariff_id

        # Обновляем параметры подписки в соответствии с тарифом
        subscription.tariff_id = tariff.id
        subscription.device_limit = tariff.device_limit
        subscription.traffic_limit_gb = tariff.traffic_limit_gb
        subscription.connected_squads = tariff.allowed_squads or []
        subscription.updated_at = datetime.utcnow()

        # Сбрасываем докупленный трафик при смене тарифа
        from sqlalchemy import delete as sql_delete

        from app.database.models import TrafficPurchase

        await db.execute(sql_delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
        subscription.purchased_traffic_gb = 0
        subscription.traffic_reset_at = None

        await db.commit()

        # Синхронизируем с RemnaWave
        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        logger.info(
            f'Админ {db_user.id} изменил тариф пользователя {user_id}: {old_tariff_id} -> {tariff_id} ({tariff.name})'
        )

        await callback.message.edit_text(
            f'{texts.t("ADMIN_TARIFF_CHANGE_SUCCESS_TITLE")}\n\n'
            f'{texts.t("ADMIN_TARIFF_CHANGE_NEW_LINE").format(name=tariff.name)}\n'
            f'{texts.t("ADMIN_TARIFF_CHANGE_DEVICES_LINE").format(count=tariff.device_limit)}\n'
            f'{texts.t("ADMIN_TARIFF_CHANGE_TRAFFIC_LINE").format(traffic=(texts.t("ADMIN_TARIFF_UNLIMITED_SYMBOL") if tariff.traffic_limit_gb == 0 else texts.t("ADMIN_USER_SUB_GB_VALUE").format(gb=tariff.traffic_limit_gb)))}\n'
            f'{texts.t("ADMIN_TARIFF_CHANGE_SERVERS_LINE").format(count=len(tariff.allowed_squads) if tariff.allowed_squads else 0)}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )

    except Exception as e:
        logger.error(f'Ошибка смены тарифа: {e}')
        await db.rollback()

        await callback.message.edit_text(
            texts.t('ADMIN_TARIFF_CHANGE_ERROR_TEXT').format(details=e),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_SUBSCRIPTION'),
                            callback_data=f'admin_user_subscription_{user_id}',
                        )
                    ]
                ]
            ),
        )

    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_users_menu, F.data == 'admin_users')

    dp.callback_query.register(show_users_list, F.data == 'admin_users_list')

    dp.callback_query.register(show_users_statistics, F.data == 'admin_users_stats')

    dp.callback_query.register(show_user_subscription, F.data.startswith('admin_user_subscription_'))

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

    dp.callback_query.register(activate_user_subscription, F.data.startswith('admin_sub_activate_'))

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

    dp.callback_query.register(confirm_admin_tariff_change, F.data.startswith('admin_sub_tariff_confirm_'))

    dp.message.register(process_devices_edit_text, AdminStates.editing_user_devices)

    dp.callback_query.register(start_traffic_edit, F.data.startswith('admin_user_traffic_') & ~F.data.contains('set'))

    dp.callback_query.register(set_user_traffic_button, F.data.startswith('admin_user_traffic_set_'))

    dp.message.register(process_traffic_edit_text, AdminStates.editing_user_traffic)

    dp.callback_query.register(
        confirm_reset_devices, F.data.startswith('admin_user_reset_devices_') & ~F.data.contains('confirm')
    )

    dp.callback_query.register(reset_user_devices, F.data.startswith('admin_user_reset_devices_confirm_'))

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
