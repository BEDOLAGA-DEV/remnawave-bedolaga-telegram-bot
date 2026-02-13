import logging

from aiogram import Dispatcher, F, types
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.subscription import (
    get_all_subscriptions,
    get_expired_subscriptions,
    get_expiring_subscriptions,
    get_subscriptions_statistics,
)
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime


def get_country_flag(country_name: str) -> str:
    flags = {
        'USA': '🇺🇸',
        'United States': '🇺🇸',
        'US': '🇺🇸',
        'Germany': '🇩🇪',
        'DE': '🇩🇪',
        'Deutschland': '🇩🇪',
        'Netherlands': '🇳🇱',
        'NL': '🇳🇱',
        'Holland': '🇳🇱',
        'United Kingdom': '🇬🇧',
        'UK': '🇬🇧',
        'GB': '🇬🇧',
        'Japan': '🇯🇵',
        'JP': '🇯🇵',
        'France': '🇫🇷',
        'FR': '🇫🇷',
        'Canada': '🇨🇦',
        'CA': '🇨🇦',
        'Russia': '🇷🇺',
        'RU': '🇷🇺',
        'Singapore': '🇸🇬',
        'SG': '🇸🇬',
    }
    return flags.get(country_name, '🌍')


async def get_users_by_countries(db: AsyncSession) -> dict:
    try:
        result = await db.execute(
            select(User.preferred_location, func.count(User.id))
            .where(User.preferred_location.isnot(None))
            .group_by(User.preferred_location)
        )

        stats = {}
        for location, count in result.fetchall():
            if location:
                stats[location] = count

        return stats
    except Exception as e:
        logger.error(f'Ошибка получения статистики по странам: {e}')
        return {}


logger = logging.getLogger(__name__)


@admin_required
@error_handler
async def show_subscriptions_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_subscriptions_statistics(db)

    text = texts.t('ADMIN_SUBS_MENU_TEXT').format(
        total=stats['total_subscriptions'],
        active=stats['active_subscriptions'],
        paid=stats['paid_subscriptions'],
        trial=stats['trial_subscriptions'],
        today=stats['purchased_today'],
        week=stats['purchased_week'],
        month=stats['purchased_month'],
    )

    keyboard = [
        [
            types.InlineKeyboardButton(text=texts.t('ADMIN_SUBS_LIST_BUTTON'), callback_data='admin_subs_list'),
            types.InlineKeyboardButton(text=texts.t('ADMIN_SUBS_EXPIRING_BUTTON'), callback_data='admin_subs_expiring'),
        ],
        [
            types.InlineKeyboardButton(text=texts.t('ADMIN_SUBS_STATS_BUTTON'), callback_data='admin_subs_stats'),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBS_COUNTRIES_BUTTON'), callback_data='admin_subs_countries'
            ),
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_subscriptions_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    texts = get_texts(db_user.language)
    subscriptions, total_count = await get_all_subscriptions(db, page=page, limit=10)
    total_pages = (total_count + 9) // 10

    if not subscriptions:
        text = texts.t('ADMIN_SUBS_LIST_EMPTY')
    else:
        text = texts.t('ADMIN_SUBS_LIST_HEADER').format(
            total=total_count,
            page=page,
            total_pages=total_pages,
        )

        for i, sub in enumerate(subscriptions, 1 + (page - 1) * 10):
            user_info = (
                (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
                if sub.user
                else texts.t('ADMIN_SUBS_UNKNOWN_USER')
            )
            sub_type = '🎁' if sub.is_trial else '💎'
            status = texts.t('ADMIN_SUBS_STATUS_ACTIVE') if sub.is_active else texts.t('ADMIN_SUBS_STATUS_INACTIVE')

            text += f'{i}. {sub_type} {user_info}\n'
            text += texts.t('ADMIN_SUBS_LIST_ENTRY_STATUS').format(
                status=status, end_date=format_datetime(sub.end_date)
            )
            if sub.device_limit > 0:
                text += texts.t('ADMIN_SUBS_LIST_ENTRY_DEVICES').format(device_limit=sub.device_limit)
            text += '\n'

    keyboard = []

    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text='⬅️', callback_data=f'admin_subs_list_page_{page - 1}'))

        nav_row.append(types.InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='current_page'))

        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text='➡️', callback_data=f'admin_subs_list_page_{page + 1}'))

        keyboard.append(nav_row)

    keyboard.extend(
        [
            [types.InlineKeyboardButton(text=texts.t('ADMIN_SUBS_REFRESH_BUTTON'), callback_data='admin_subs_list')],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_subscriptions')],
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_expiring_subscriptions(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    expiring_3d = await get_expiring_subscriptions(db, 3)
    expiring_1d = await get_expiring_subscriptions(db, 1)
    expired = await get_expired_subscriptions(db)

    text = texts.t('ADMIN_SUBS_EXPIRING_TEXT').format(
        expiring_3d=len(expiring_3d),
        expiring_1d=len(expiring_1d),
        expired=len(expired),
    )

    for sub in expiring_3d[:5]:
        user_info = (
            (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
            if sub.user
            else texts.t('ADMIN_SUBS_UNKNOWN_USER')
        )
        sub_type = '🎁' if sub.is_trial else '💎'
        text += texts.t('ADMIN_SUBS_EXPIRING_ENTRY').format(
            subscription_type=sub_type,
            user_info=user_info,
            end_date=format_datetime(sub.end_date),
        )

    if len(expiring_3d) > 5:
        text += texts.t('ADMIN_SUBS_AND_MORE').format(count=len(expiring_3d) - 5)

    text += texts.t('ADMIN_SUBS_EXPIRING_TOMORROW_HEADER')
    for sub in expiring_1d[:5]:
        user_info = (
            (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
            if sub.user
            else texts.t('ADMIN_SUBS_UNKNOWN_USER')
        )
        sub_type = '🎁' if sub.is_trial else '💎'
        text += texts.t('ADMIN_SUBS_EXPIRING_ENTRY').format(
            subscription_type=sub_type,
            user_info=user_info,
            end_date=format_datetime(sub.end_date),
        )

    if len(expiring_1d) > 5:
        text += texts.t('ADMIN_SUBS_AND_MORE').format(count=len(expiring_1d) - 5)

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBS_SEND_REMINDERS_BUTTON'), callback_data='admin_send_expiry_reminders'
            )
        ],
        [types.InlineKeyboardButton(text=texts.t('ADMIN_SUBS_REFRESH_BUTTON'), callback_data='admin_subs_expiring')],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_subscriptions')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_subscriptions_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_subscriptions_statistics(db)

    expiring_3d = await get_expiring_subscriptions(db, 3)
    expiring_7d = await get_expiring_subscriptions(db, 7)
    expired = await get_expired_subscriptions(db)

    text = texts.t('ADMIN_SUBS_STATS_TEXT').format(
        total_subscriptions=stats['total_subscriptions'],
        active_subscriptions=stats['active_subscriptions'],
        inactive_subscriptions=stats['total_subscriptions'] - stats['active_subscriptions'],
        paid_subscriptions=stats['paid_subscriptions'],
        trial_subscriptions=stats['trial_subscriptions'],
        purchased_today=stats['purchased_today'],
        purchased_week=stats['purchased_week'],
        purchased_month=stats['purchased_month'],
        expiring_3d=len(expiring_3d),
        expiring_7d=len(expiring_7d),
        expired=len(expired),
        trial_to_paid_conversion=stats.get('trial_to_paid_conversion', 0),
        renewals_count=stats.get('renewals_count', 0),
    )

    keyboard = [
        # [
        #     types.InlineKeyboardButton(text="📊 Экспорт данных", callback_data="admin_subs_export"),
        #     types.InlineKeyboardButton(text="📈 Графики", callback_data="admin_subs_charts")
        # ],
        # [types.InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_subs_stats")],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_subscriptions')]
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def show_countries_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        from app.services.remnawave_service import RemnaWaveService

        remnawave_service = RemnaWaveService()

        nodes_data = await remnawave_service.get_all_nodes()
        squads_data = await remnawave_service.get_all_squads()

        text = texts.t('ADMIN_SUBS_COUNTRIES_TITLE')

        if nodes_data:
            text += texts.t('ADMIN_SUBS_COUNTRIES_AVAILABLE_SERVERS')
            countries = {}

            for node in nodes_data:
                country_code = node.get('country_code', 'XX')
                country_name = country_code

                if country_name not in countries:
                    countries[country_name] = []
                countries[country_name].append(node)

            for country, nodes in countries.items():
                active_nodes = len([n for n in nodes if n.get('is_connected') and n.get('is_node_online')])
                total_nodes = len(nodes)

                country_flag = get_country_flag(country)
                text += texts.t('ADMIN_SUBS_COUNTRIES_SERVER_ROW').format(
                    country_flag=country_flag,
                    country=country,
                    active_nodes=active_nodes,
                    total_nodes=total_nodes,
                )

                total_users_online = sum(n.get('users_online', 0) or 0 for n in nodes)
                if total_users_online > 0:
                    text += texts.t('ADMIN_SUBS_COUNTRIES_USERS_ONLINE_ROW').format(count=total_users_online)
        else:
            text += texts.t('ADMIN_SUBS_COUNTRIES_SERVERS_LOAD_ERROR')

        if squads_data:
            text += texts.t('ADMIN_SUBS_COUNTRIES_SQUADS_TOTAL').format(count=len(squads_data))

            total_members = sum(squad.get('members_count', 0) for squad in squads_data)
            text += texts.t('ADMIN_SUBS_COUNTRIES_SQUADS_MEMBERS').format(count=total_members)

            text += texts.t('ADMIN_SUBS_COUNTRIES_SQUADS_HEADER')
            for squad in squads_data[:5]:
                name = squad.get('name', texts.t('ADMIN_SUBS_UNKNOWN'))
                members = squad.get('members_count', 0)
                inbounds = squad.get('inbounds_count', 0)
                text += texts.t('ADMIN_SUBS_COUNTRIES_SQUAD_ROW').format(
                    name=name,
                    members=members,
                    inbounds=inbounds,
                )

            if len(squads_data) > 5:
                text += texts.t('ADMIN_SUBS_COUNTRIES_SQUADS_MORE').format(count=len(squads_data) - 5)

        user_stats = await get_users_by_countries(db)
        if user_stats:
            text += texts.t('ADMIN_SUBS_COUNTRIES_USERS_BY_REGION_HEADER')
            for country, count in user_stats.items():
                country_flag = get_country_flag(country)
                text += texts.t('ADMIN_SUBS_COUNTRIES_USERS_BY_REGION_ROW').format(
                    country_flag=country_flag,
                    country=country,
                    count=count,
                )

    except Exception as e:
        logger.error(f'Ошибка получения данных о странах: {e}')
        text = texts.t('ADMIN_SUBS_COUNTRIES_LOAD_FAILED_TEXT').format(error=f'{e!s}')

    keyboard = [
        [types.InlineKeyboardButton(text=texts.t('ADMIN_SUBS_REFRESH_BUTTON'), callback_data='admin_subs_countries')],
        [
            types.InlineKeyboardButton(text=texts.t('ADMIN_SUBS_NODES_STATS_BUTTON'), callback_data='admin_rw_nodes'),
            types.InlineKeyboardButton(text=texts.t('ADMIN_SUBS_SQUADS_BUTTON'), callback_data='admin_rw_squads'),
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_subscriptions')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def send_expiry_reminders(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_SUBS_REMINDERS_SENDING'),
        reply_markup=None,
    )

    expiring_subs = await get_expiring_subscriptions(db, 1)
    sent_count = 0

    for subscription in expiring_subs:
        if subscription.user:
            try:
                user = subscription.user
                # Skip email-only users (no telegram_id)
                if not user.telegram_id:
                    logger.debug(f'Пропуск email-пользователя {user.id} при отправке напоминания')
                    continue

                days_left = max(1, subscription.days_left)

                reminder_text = texts.t('ADMIN_SUBS_REMINDER_TEXT').format(days_left=days_left)

                await callback.bot.send_message(chat_id=user.telegram_id, text=reminder_text)
                sent_count += 1

            except Exception as e:
                logger.error(f'Ошибка отправки напоминания пользователю {subscription.user_id}: {e}')

    await callback.message.edit_text(
        texts.t('ADMIN_SUBS_REMINDERS_SENT').format(sent=sent_count, total=len(expiring_subs)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_subs_expiring')]]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def handle_subscriptions_pagination(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = int(callback.data.split('_')[-1])
    await show_subscriptions_list(callback, db_user, db, page)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_subscriptions_menu, F.data == 'admin_subscriptions')
    dp.callback_query.register(show_subscriptions_list, F.data == 'admin_subs_list')
    dp.callback_query.register(show_expiring_subscriptions, F.data == 'admin_subs_expiring')
    dp.callback_query.register(show_subscriptions_stats, F.data == 'admin_subs_stats')
    dp.callback_query.register(show_countries_management, F.data == 'admin_subs_countries')
    dp.callback_query.register(send_expiry_reminders, F.data == 'admin_send_expiry_reminders')

    dp.callback_query.register(handle_subscriptions_pagination, F.data.startswith('admin_subs_list_page_'))
