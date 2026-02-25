import structlog
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
        logger.error('Ошибка получения статистики по странам', error=e)
        return {}


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_subscriptions_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_subscriptions_statistics(db)

    text = texts.t(
        'ADMIN_SUBSCRIPTIONS_MENU_TEXT',
        '📱 <b>Управление подписками</b>\n\n'
        '📊 <b>Статистика:</b>\n'
        '- Всего: {total_subscriptions}\n'
        '- Активных: {active_subscriptions}\n'
        '- Платных: {paid_subscriptions}\n'
        '- Триальных: {trial_subscriptions}\n\n'
        '📈 <b>Продажи:</b>\n'
        '- Сегодня: {purchased_today}\n'
        '- За неделю: {purchased_week}\n'
        '- За месяц: {purchased_month}\n\n'
        'Выберите действие:',
    ).format(
        total_subscriptions=stats['total_subscriptions'],
        active_subscriptions=stats['active_subscriptions'],
        paid_subscriptions=stats['paid_subscriptions'],
        trial_subscriptions=stats['trial_subscriptions'],
        purchased_today=stats['purchased_today'],
        purchased_week=stats['purchased_week'],
        purchased_month=stats['purchased_month'],
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_LIST_BUTTON', '📋 Список подписок'),
                callback_data='admin_subs_list',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_EXPIRING', '⏰ Истекающие'),
                callback_data='admin_subs_expiring',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_STATS_BUTTON', '📊 Статистика'),
                callback_data='admin_subs_stats',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_GEO_BUTTON', '🌍 География'),
                callback_data='admin_subs_countries',
            ),
        ],
        [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_panel')],
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
        text = texts.t('ADMIN_SUBSCRIPTIONS_LIST_EMPTY', '📱 <b>Список подписок</b>\n\n❌ Подписки не найдены.')
    else:
        text = texts.t('ADMIN_SUBSCRIPTIONS_LIST_TITLE', '📱 <b>Список подписок</b>\n\n')
        text += texts.t(
            'ADMIN_SERVERS_LIST_META',
            '📊 Всего: {total} | Страница: {page}/{pages}\n\n',
        ).format(total=total_count, page=page, pages=total_pages)

        for i, sub in enumerate(subscriptions, 1 + (page - 1) * 10):
            user_info = (
                (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
                if sub.user
                else texts.t('SUBSCRIPTION_STATUS_UNKNOWN', 'Неизвестно')
            )
            sub_type = '🎁' if sub.is_trial else '💎'
            status = (
                texts.t('ADMIN_USER_SUBSCRIPTION_STATUS_ACTIVE', '✅ Активна')
                if sub.is_active
                else texts.t('ADMIN_USER_SUBSCRIPTION_STATUS_INACTIVE', '❌ Неактивна')
            )

            text += f'{i}. {sub_type} {user_info}\n'
            text += texts.t(
                'ADMIN_SUBSCRIPTIONS_LIST_END_DATE',
                '   {status} | До: {end_date}\n',
            ).format(status=status, end_date=format_datetime(sub.end_date))
            if sub.device_limit > 0:
                text += texts.t(
                    'ADMIN_SUBSCRIPTIONS_LIST_DEVICE_LIMIT',
                    '   📱 Устройств: {count}\n',
                ).format(count=sub.device_limit)
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
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'),
                    callback_data='admin_subs_list',
                )
            ],
            [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_subscriptions')],
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

    text = texts.t(
        'ADMIN_SUBSCRIPTIONS_EXPIRING_TEXT',
        '⏰ <b>Истекающие подписки</b>\n\n'
        '📊 <b>Статистика:</b>\n'
        '- Истекают через 3 дня: {expiring_3d}\n'
        '- Истекают завтра: {expiring_1d}\n'
        '- Уже истекли: {expired}\n\n'
        '<b>Истекают через 3 дня:</b>\n',
    ).format(expiring_3d=len(expiring_3d), expiring_1d=len(expiring_1d), expired=len(expired))

    for sub in expiring_3d[:5]:
        user_info = (
            (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
            if sub.user
            else texts.t('SUBSCRIPTION_STATUS_UNKNOWN', 'Неизвестно')
        )
        sub_type = '🎁' if sub.is_trial else '💎'
        text += f'{sub_type} {user_info} - {format_datetime(sub.end_date)}\n'

    if len(expiring_3d) > 5:
        text += texts.t('ADMIN_STATS_REVENUE_MORE_DAYS', '... и еще {count} дней').format(
            count=len(expiring_3d) - 5
        ) + '\n'

    text += texts.t('ADMIN_SUBSCRIPTIONS_EXPIRING_TOMORROW', '\n<b>Истекают завтра:</b>\n')
    for sub in expiring_1d[:5]:
        user_info = (
            (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
            if sub.user
            else texts.t('SUBSCRIPTION_STATUS_UNKNOWN', 'Неизвестно')
        )
        sub_type = '🎁' if sub.is_trial else '💎'
        text += f'{sub_type} {user_info} - {format_datetime(sub.end_date)}\n'

    if len(expiring_1d) > 5:
        text += texts.t('ADMIN_STATS_REVENUE_MORE_DAYS', '... и еще {count} дней').format(
            count=len(expiring_1d) - 5
        ) + '\n'

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_SEND_REMINDERS', '📨 Отправить напоминания'),
                callback_data='admin_send_expiry_reminders',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'),
                callback_data='admin_subs_expiring',
            )
        ],
        [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_subscriptions')],
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

    text = texts.t(
        'ADMIN_SUBSCRIPTIONS_DETAILED_STATS_TEXT',
        '📊 <b>Детальная статистика подписок</b>\n\n'
        '<b>📱 Общая информация:</b>\n'
        '• Всего подписок: {total_subscriptions}\n'
        '• Активных: {active_subscriptions}\n'
        '• Неактивных: {inactive_subscriptions}\n\n'
        '<b>💎 По типам:</b>\n'
        '• Платных: {paid_subscriptions}\n'
        '• Триальных: {trial_subscriptions}\n\n'
        '<b>📈 Продажи:</b>\n'
        '• Сегодня: {purchased_today}\n'
        '• За неделю: {purchased_week}\n'
        '• За месяц: {purchased_month}\n\n'
        '<b>⏰ Истечение:</b>\n'
        '• Истекают через 3 дня: {expiring_3d}\n'
        '• Истекают через 7 дней: {expiring_7d}\n'
        '• Уже истекли: {expired}\n\n'
        '<b>💰 Конверсия:</b>\n'
        '• Из триала в платную: {conversion}%\n'
        '• Продлений: {renewals_count}',
    ).format(
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
        conversion=stats.get('trial_to_paid_conversion', 0),
        renewals_count=stats.get('renewals_count', 0),
    )

    keyboard = [
        # [
        #     types.InlineKeyboardButton(text="📊 Экспорт данных", callback_data="admin_subs_export"),
        #     types.InlineKeyboardButton(text="📈 Графики", callback_data="admin_subs_charts")
        # ],
        # [types.InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_subs_stats")],
        [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_subscriptions')]
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

        text = texts.t('ADMIN_SUBSCRIPTIONS_COUNTRIES_TEXT', '🌍 <b>Управление странами</b>\n\n')

        if nodes_data:
            text += texts.t('ADMIN_SUBSCRIPTIONS_COUNTRIES_AVAILABLE_SERVERS', '<b>Доступные серверы:</b>\n')
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
                text += texts.t(
                    'ADMIN_SUBSCRIPTIONS_COUNTRIES_SERVERS_LINE',
                    '{flag} {country}: {active}/{total} серверов\n',
                ).format(flag=country_flag, country=country, active=active_nodes, total=total_nodes)

                total_users_online = sum(n.get('users_online', 0) or 0 for n in nodes)
                if total_users_online > 0:
                    text += texts.t(
                        'ADMIN_SUBSCRIPTIONS_COUNTRIES_ONLINE_USERS_LINE',
                        '   👥 Пользователей онлайн: {count}\n',
                    ).format(count=total_users_online)
        else:
            text += texts.t(
                'ADMIN_SUBSCRIPTIONS_COUNTRIES_SERVERS_LOAD_ERROR',
                '❌ Не удалось загрузить данные о серверах\n',
            )

        if squads_data:
            text += texts.t(
                'ADMIN_SUBSCRIPTIONS_COUNTRIES_TOTAL_SQUADS',
                '\n<b>Всего сквадов:</b> {count}\n',
            ).format(count=len(squads_data))

            total_members = sum(squad.get('members_count', 0) for squad in squads_data)
            text += texts.t(
                'ADMIN_SUBSCRIPTIONS_COUNTRIES_SQUAD_MEMBERS',
                '<b>Участников в сквадах:</b> {count}\n',
            ).format(count=total_members)

            text += texts.t('ADMIN_SUBSCRIPTIONS_COUNTRIES_SQUADS_TITLE', '\n<b>Сквады:</b>\n')
            for squad in squads_data[:5]:
                name = squad.get('name', texts.t('SUBSCRIPTION_STATUS_UNKNOWN', 'Неизвестно'))
                members = squad.get('members_count', 0)
                inbounds = squad.get('inbounds_count', 0)
                text += texts.t(
                    'ADMIN_SUBSCRIPTIONS_COUNTRIES_SQUAD_LINE',
                    '• {name}: {members} участников, {inbounds} inbound(s)\n',
                ).format(name=name, members=members, inbounds=inbounds)

            if len(squads_data) > 5:
                text += texts.t(
                    'ADMIN_SUBSCRIPTIONS_COUNTRIES_MORE_SQUADS',
                    '... и еще {count} сквадов\n',
                ).format(count=len(squads_data) - 5)

        user_stats = await get_users_by_countries(db)
        if user_stats:
            text += texts.t('ADMIN_SUBSCRIPTIONS_COUNTRIES_USERS_BY_REGIONS', '\n<b>Пользователи по регионам:</b>\n')
            for country, count in user_stats.items():
                country_flag = get_country_flag(country)
                text += texts.t(
                    'ADMIN_SUBSCRIPTIONS_COUNTRIES_USERS_LINE',
                    '{flag} {country}: {count} пользователей\n',
                ).format(flag=country_flag, country=country, count=count)

    except Exception as e:
        logger.error('Ошибка получения данных о странах', error=e)
        text = texts.t(
            'ADMIN_SUBSCRIPTIONS_COUNTRIES_ERROR_TEXT',
            '🌍 <b>Управление странами</b>\n\n'
            '❌ <b>Ошибка загрузки данных</b>\n'
            'Не удалось получить информацию о серверах.\n\n'
            'Проверьте подключение к RemnaWave API.\n\n'
            '<b>Детали ошибки:</b> {error}',
        ).format(error=e)

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'),
                callback_data='admin_subs_countries',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_COUNTRIES_NODES_STATS_BUTTON', '📊 Статистика нод'),
                callback_data='admin_rw_nodes',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_COUNTRIES_SQUADS_BUTTON', '🔧 Сквады'),
                callback_data='admin_rw_squads',
            ),
        ],
        [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_subscriptions')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def send_expiry_reminders(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_SUBSCRIPTIONS_REMINDERS_SENDING',
            '📨 Отправка напоминаний...\n\nПодождите, это может занять время.',
        ),
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
                    logger.debug('Пропуск email-пользователя при отправке напоминания', user_id=user.id)
                    continue

                days_left = max(1, subscription.days_left)
                user_texts = get_texts(user.language or db_user.language)

                reminder_text = user_texts.t(
                    'ADMIN_SUBSCRIPTIONS_EXPIRY_REMINDER_TEXT',
                    '⚠️ <b>Подписка истекает!</b>\n\n'
                    'Ваша подписка истекает через {days} день(а).\n\n'
                    'Не забудьте продлить подписку, чтобы не потерять доступ к серверам.\n\n'
                    '💎 Продлить подписку можно в главном меню.',
                ).format(days=days_left)

                await callback.bot.send_message(chat_id=user.telegram_id, text=reminder_text)
                sent_count += 1

            except Exception as e:
                logger.error('Ошибка отправки напоминания пользователю', user_id=subscription.user_id, error=e)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_SUBSCRIPTIONS_REMINDERS_SENT',
            '✅ Напоминания отправлены: {sent_count} из {total}',
        ).format(sent_count=sent_count, total=len(expiring_subs)),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_subs_expiring')]
            ]
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
