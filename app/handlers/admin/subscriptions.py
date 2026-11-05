import structlog
from aiogram import Dispatcher, F, types
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    get_all_subscriptions,
    get_expired_subscriptions,
    get_expiring_subscriptions,
    get_subscriptions_statistics,
)
from app.database.models import (
    ServerSquad,
    Subscription,
    SubscriptionServer,
    SubscriptionStatus,
    User,
)
from app.localization.texts import Texts, get_texts
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime
from app.utils.subscription_time import local_days_until


logger = structlog.get_logger(__name__)


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
    """Распределение активных пользователей по странам подключённых серверов.

    Один user может быть посчитан в нескольких странах, если у активной подписки
    подключены squad'ы из разных регионов — это ожидаемое поведение для географии.
    """
    try:
        result = await db.execute(
            select(
                ServerSquad.country_code,
                func.count(distinct(Subscription.user_id)),
            )
            .select_from(Subscription)
            .join(SubscriptionServer, SubscriptionServer.subscription_id == Subscription.id)
            .join(ServerSquad, SubscriptionServer.server_squad_id == ServerSquad.id)
            .where(Subscription.status == SubscriptionStatus.ACTIVE.value)
            .where(ServerSquad.country_code.isnot(None))
            .group_by(ServerSquad.country_code)
        )

        stats = {}
        for country_code, count in result.fetchall():
            if country_code:
                stats[country_code] = count

        return stats
    except Exception as error:
        logger.error('Ошибка получения статистики по странам', error=str(error), exc_info=True)
        return {}


@admin_required
@error_handler
async def show_subscriptions_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_subscriptions_statistics(db)

    text = texts.t(
        'ADMIN_SUBSCRIPTIONS_UI_000',
        '\n📱 <b>Управление подписками</b>\n\n📊 <b>Статистика:</b>\n- Всего: {value1}\n- Активных: {value2}\n- Платных: {value3}\n- Триальных: {value4}\n\n📈 <b>Продажи:</b>\n- Сегодня: {value5}\n- За неделю: {value6}\n- За месяц: {value7}\n\nВыберите действие:\n',
    ).format(
        value1=stats['total_subscriptions'],
        value2=stats['active_subscriptions'],
        value3=stats['paid_subscriptions'],
        value4=stats['trial_subscriptions'],
        value5=stats['purchased_today'],
        value6=stats['purchased_week'],
        value7=stats['purchased_month'],
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_028', '📋 Список подписок'), callback_data='admin_subs_list'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_029', '⏰ Истекающие'), callback_data='admin_subs_expiring'
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_030', '📊 Статистика'), callback_data='admin_subs_stats'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_031', '🌍 География'), callback_data='admin_subs_countries'
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_032', '⬅️ Назад'), callback_data='admin_panel'
            )
        ],
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
        text = texts.t('ADMIN_SUBSCRIPTIONS_UI_004', '📱 <b>Список подписок</b>\n\n❌ Подписки не найдены.')
    else:
        text = texts.t('ADMIN_SUBSCRIPTIONS_UI_005', '📱 <b>Список подписок</b>\n\n')
        text += texts.t('ADMIN_SUBSCRIPTIONS_UI_006', '📊 Всего: {value1} | Страница: {value2}/{value3}\n\n').format(
            value1=total_count, value2=page, value3=total_pages
        )

        for i, sub in enumerate(subscriptions, 1 + (page - 1) * 10):
            user_info = (
                (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
                if sub.user
                else texts.t('ADMIN_SUBSCRIPTIONS_UI_010', 'Неизвестно')
            )
            sub_type = '🎁' if sub.is_trial else '💎'
            status = (
                texts.t('ADMIN_SUBSCRIPTIONS_UI_020', '✅ Активна')
                if sub.is_active
                else texts.t('ADMIN_SUBSCRIPTIONS_UI_021', '❌ Неактивна')
            )

            text += f'{i}. {sub_type} {user_info}\n'
            text += texts.t('ADMIN_SUBSCRIPTIONS_UI_009', '   {value1} | До: {value2}\n').format(
                value1=status, value2=format_datetime(sub.end_date)
            )
            if sub.device_limit is not None:
                text += texts.t('ADMIN_SUBSCRIPTIONS_UI_022', '   📱 Устройств: {value1}\n').format(
                    value1=Texts.format_device_limit(sub.device_limit)
                )
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
                    text=texts.t('ADMIN_SUBSCRIPTIONS_UI_034', '🔄 Обновить'), callback_data='admin_subs_list'
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_SUBSCRIPTIONS_UI_032', '⬅️ Назад'), callback_data='admin_subscriptions'
                )
            ],
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
        'ADMIN_SUBSCRIPTIONS_UI_001',
        '\n⏰ <b>Истекающие подписки</b>\n\n📊 <b>Статистика:</b>\n- Истекают через 3 дня: {value1}\n- Истекают завтра: {value2}\n- Уже истекли: {value3}\n\n<b>Истекают через 3 дня:</b>\n',
    ).format(value1=len(expiring_3d), value2=len(expiring_1d), value3=len(expired))

    for sub in expiring_3d[:5]:
        user_info = (
            (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
            if sub.user
            else texts.t('ADMIN_SUBSCRIPTIONS_UI_010', 'Неизвестно')
        )
        sub_type = '🎁' if sub.is_trial else '💎'
        text += f'{sub_type} {user_info} - {format_datetime(sub.end_date)}\n'

    if len(expiring_3d) > 5:
        text += texts.t('ADMIN_SUBSCRIPTIONS_UI_007', '... и еще {value1}\n').format(value1=len(expiring_3d) - 5)

    text += texts.t('ADMIN_SUBSCRIPTIONS_UI_002', '\n<b>Истекают завтра:</b>\n')
    for sub in expiring_1d[:5]:
        user_info = (
            (f'ID{sub.user.telegram_id}' if sub.user.telegram_id else sub.user.email or f'#{sub.user.id}')
            if sub.user
            else texts.t('ADMIN_SUBSCRIPTIONS_UI_010', 'Неизвестно')
        )
        sub_type = '🎁' if sub.is_trial else '💎'
        text += f'{sub_type} {user_info} - {format_datetime(sub.end_date)}\n'

    if len(expiring_1d) > 5:
        text += texts.t('ADMIN_SUBSCRIPTIONS_UI_007', '... и еще {value1}\n').format(value1=len(expiring_1d) - 5)

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_033', '📨 Отправить напоминания'),
                callback_data='admin_send_expiry_reminders',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_034', '🔄 Обновить'), callback_data='admin_subs_expiring'
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_032', '⬅️ Назад'), callback_data='admin_subscriptions'
            )
        ],
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
        'ADMIN_SUBSCRIPTIONS_UI_003',
        '\n📊 <b>Детальная статистика подписок</b>\n\n<b>📱 Общая информация:</b>\n• Всего подписок: {value1}\n• Активных: {value2}\n• Неактивных: {value3}\n\n<b>💎 По типам:</b>\n• Платных: {value4}\n• Триальных: {value5}\n\n<b>📈 Продажи:</b>\n• Сегодня: {value6}\n• За неделю: {value7}\n• За месяц: {value8}\n\n<b>⏰ Истечение:</b>\n• Истекают через 3 дня: {value9}\n• Истекают через 7 дней: {value10}\n• Уже истекли: {value11}\n\n<b>💰 Конверсия:</b>\n• Из триала в платную: {value12}%\n• Продлений: {value13}\n',
    ).format(
        value1=stats['total_subscriptions'],
        value2=stats['active_subscriptions'],
        value3=stats['total_subscriptions'] - stats['active_subscriptions'],
        value4=stats['paid_subscriptions'],
        value5=stats['trial_subscriptions'],
        value6=stats['purchased_today'],
        value7=stats['purchased_week'],
        value8=stats['purchased_month'],
        value9=len(expiring_3d),
        value10=len(expiring_7d),
        value11=len(expired),
        value12=stats.get('trial_to_paid_conversion', 0),
        value13=stats.get('renewals_count', 0),
    )

    keyboard = [
        # [
        #     types.InlineKeyboardButton(text="📊 Экспорт данных", callback_data="admin_subs_export"),
        #     types.InlineKeyboardButton(text="📈 Графики", callback_data="admin_subs_charts")
        # ],
        # [types.InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_subs_stats")],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_032', '⬅️ Назад'), callback_data='admin_subscriptions'
            )
        ]
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

        text = texts.t('ADMIN_SUBSCRIPTIONS_UI_008', '🌍 <b>Управление странами</b>\n\n')

        if nodes_data:
            text += texts.t('ADMIN_SUBSCRIPTIONS_UI_011', '<b>Доступные серверы:</b>\n')
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
                text += texts.t('ADMIN_SUBSCRIPTIONS_UI_023', '{value1} {value2}: {value3}/{value4} серверов\n').format(
                    value1=country_flag, value2=country, value3=active_nodes, value4=total_nodes
                )

                total_users_online = sum(n.get('users_online', 0) or 0 for n in nodes)
                if total_users_online > 0:
                    text += texts.t('ADMIN_SUBSCRIPTIONS_UI_035', '   👥 Пользователей онлайн: {value1}\n').format(
                        value1=total_users_online
                    )
        else:
            text += texts.t('ADMIN_SUBSCRIPTIONS_UI_012', '❌ Не удалось загрузить данные о серверах\n')

        if squads_data:
            text += texts.t('ADMIN_SUBSCRIPTIONS_UI_013', '\n<b>Всего сквадов:</b> {value1}\n').format(
                value1=len(squads_data)
            )

            total_members = sum(squad.get('members_count', 0) for squad in squads_data)
            text += texts.t('ADMIN_SUBSCRIPTIONS_UI_014', '<b>Участников в сквадах:</b> {value1}\n').format(
                value1=total_members
            )

            text += texts.t('ADMIN_SUBSCRIPTIONS_UI_015', '\n<b>Сквады:</b>\n')
            for squad in squads_data[:5]:
                name = squad.get('name', texts.t('ADMIN_SUBSCRIPTIONS_UI_010', 'Неизвестно'))
                members = squad.get('members_count', 0)
                inbounds = squad.get('inbounds_count', 0)
                text += texts.t(
                    'ADMIN_SUBSCRIPTIONS_UI_024', '• {value1}: {value2} участников, {value3} inbound(s)\n'
                ).format(value1=name, value2=members, value3=inbounds)

            if len(squads_data) > 5:
                text += texts.t('ADMIN_SUBSCRIPTIONS_UI_025', '... и еще {value1} сквадов\n').format(
                    value1=len(squads_data) - 5
                )

        user_stats = await get_users_by_countries(db)
        if user_stats:
            text += texts.t('ADMIN_SUBSCRIPTIONS_UI_016', '\n<b>Пользователи по регионам:</b>\n')
            for country, count in user_stats.items():
                country_flag = get_country_flag(country)
                text += texts.t('ADMIN_SUBSCRIPTIONS_UI_026', '{value1} {value2}: {value3} пользователей\n').format(
                    value1=country_flag, value2=country, value3=count
                )

    except Exception as e:
        logger.error('Ошибка получения данных о странах', error=e)
        text = texts.t(
            'ADMIN_SUBSCRIPTIONS_UI_017',
            '\n🌍 <b>Управление странами</b>\n\n❌ <b>Ошибка загрузки данных</b>\nНе удалось получить информацию о серверах.\n\nПроверьте подключение к RemnaWave API.\n\n<b>Детали ошибки:</b> {value1!s}\n',
        ).format(value1=e)

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_034', '🔄 Обновить'), callback_data='admin_subs_countries'
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_036', '📊 Статистика нод'), callback_data='admin_rw_nodes'
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_037', '🔧 Сквады'), callback_data='admin_rw_squads'
            ),
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUBSCRIPTIONS_UI_032', '⬅️ Назад'), callback_data='admin_subscriptions'
            )
        ],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def send_expiry_reminders(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_SUBSCRIPTIONS_UI_018', '📨 Отправка напоминаний...\n\nПодождите, это может занять время.'),
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

                from app.utils.notification_prefs import is_subscription_expiry_enabled

                if not settings.is_notifications_enabled() or not is_subscription_expiry_enabled(user):
                    continue

                recipient_texts = get_texts(getattr(user, 'language', None))
                days_left = local_days_until(subscription.end_date)
                expires_when = (
                    recipient_texts.t('ADMIN_SUBSCRIPTIONS_UI_038', 'сегодня')
                    if days_left == 0
                    else recipient_texts.t('ADMIN_SUBSCRIPTIONS_UI_039', 'через {value1} день(а)').format(
                        value1=days_left
                    )
                )

                tariff_label = ''
                if settings.is_multi_tariff_enabled() and hasattr(subscription, 'tariff') and subscription.tariff:
                    tariff_label = f' «{subscription.tariff.name}»'
                reminder_text = recipient_texts.t(
                    'ADMIN_SUBSCRIPTIONS_UI_027',
                    '\n⚠️ <b>Подписка{value1} истекает!</b>\n\nВаша подписка истекает {value2}.\n\nНе забудьте продлить подписку, чтобы не потерять доступ к серверам.\n\n💎 Продлить подписку можно в главном меню.\n',
                ).format(value1=tariff_label, value2=expires_when)

                await callback.bot.send_message(chat_id=user.telegram_id, text=reminder_text)
                sent_count += 1

            except Exception as e:
                logger.error('Ошибка отправки напоминания пользователю', user_id=subscription.user_id, error=e)

    await callback.message.edit_text(
        texts.t('ADMIN_SUBSCRIPTIONS_UI_019', '✅ Напоминания отправлены: {value1} из {value2}').format(
            value1=sent_count, value2=len(expiring_subs)
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_SUBSCRIPTIONS_UI_032', '⬅️ Назад'), callback_data='admin_subs_expiring'
                    )
                ]
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
