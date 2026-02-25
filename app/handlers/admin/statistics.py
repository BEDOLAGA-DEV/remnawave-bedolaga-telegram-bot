from datetime import datetime, timedelta

import structlog
from aiogram import Dispatcher, F, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import get_referral_statistics
from app.database.crud.subscription import get_subscriptions_statistics
from app.database.crud.transaction import get_revenue_by_period, get_transactions_statistics
from app.database.models import User
from app.keyboards.admin import get_admin_statistics_keyboard
from app.services.user_service import UserService
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime, format_percentage
from app.localization.texts import get_texts

logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_statistics_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = texts.t(
        'ADMIN_STATS_MENU_TEXT',
        '📊 <b>Статистика системы</b>\n\nВыберите раздел для просмотра статистики:',
    )

    await callback.message.edit_text(text, reply_markup=get_admin_statistics_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)

    total_users = stats['total_users']
    active_rate = format_percentage(stats['active_users'] / total_users * 100 if total_users > 0 else 0)
    growth_rate = format_percentage(stats['new_month'] / total_users * 100 if total_users > 0 else 0)

    current_time = format_datetime(datetime.utcnow())

    text = texts.t(
        'ADMIN_STATS_USERS_TEXT',
        '👥 <b>Статистика пользователей</b>\n\n'
        '<b>Общие показатели:</b>\n'
        '- Всего зарегистрировано: {total_users}\n'
        '- Активных: {active_users} ({active_rate})\n'
        '- Заблокированных: {blocked_users}\n\n'
        '<b>Новые регистрации:</b>\n'
        '- Сегодня: {new_today}\n'
        '- За неделю: {new_week}\n'
        '- За месяц: {new_month}\n\n'
        '<b>Активность:</b>\n'
        '- Коэффициент активности: {active_rate}\n'
        '- Рост за месяц: +{new_month} ({growth_rate})\n\n'
        '<b>Обновлено:</b> {current_time}',
    ).format(
        total_users=stats['total_users'],
        active_users=stats['active_users'],
        active_rate=active_rate,
        blocked_users=stats['blocked_users'],
        new_today=stats['new_today'],
        new_week=stats['new_week'],
        new_month=stats['new_month'],
        growth_rate=growth_rate,
        current_time=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'),
                    callback_data='admin_stats_users',
                )
            ],
            [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_DATA_ACTUAL', '📊 Данные актуальны'), show_alert=False)
        else:
            logger.error('Ошибка обновления статистики пользователей', error=e)
            await callback.answer(texts.t('ADMIN_STATS_UPDATE_ERROR', '❌ Ошибка обновления данных'), show_alert=True)
            return

    await callback.answer(texts.t('ADMIN_STATS_UPDATED', '✅ Статистика обновлена'))


@admin_required
@error_handler
async def show_subscriptions_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_subscriptions_statistics(db)

    total_subs = stats['total_subscriptions']
    conversion_rate = format_percentage(stats['paid_subscriptions'] / total_subs * 100 if total_subs > 0 else 0)
    current_time = format_datetime(datetime.utcnow())

    text = texts.t(
        'ADMIN_STATS_SUBSCRIPTIONS_TEXT',
        '📱 <b>Статистика подписок</b>\n\n'
        '<b>Общие показатели:</b>\n'
        '- Всего подписок: {total_subscriptions}\n'
        '- Активных: {active_subscriptions}\n'
        '- Платных: {paid_subscriptions}\n'
        '- Триальных: {trial_subscriptions}\n\n'
        '<b>Конверсия:</b>\n'
        '- Из триала в платную: {conversion_rate}\n'
        '- Активных платных: {paid_subscriptions}\n\n'
        '<b>Продажи:</b>\n'
        '- Сегодня: {purchased_today}\n'
        '- За неделю: {purchased_week}\n'
        '- За месяц: {purchased_month}\n\n'
        '<b>Обновлено:</b> {current_time}',
    ).format(
        total_subscriptions=stats['total_subscriptions'],
        active_subscriptions=stats['active_subscriptions'],
        paid_subscriptions=stats['paid_subscriptions'],
        trial_subscriptions=stats['trial_subscriptions'],
        conversion_rate=conversion_rate,
        purchased_today=stats['purchased_today'],
        purchased_week=stats['purchased_week'],
        purchased_month=stats['purchased_month'],
        current_time=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'),
                    callback_data='admin_stats_subs',
                )
            ],
            [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED', '✅ Статистика обновлена'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_DATA_ACTUAL', '📊 Данные актуальны'), show_alert=False)
        else:
            logger.error('Ошибка обновления статистики подписок', error=e)
            await callback.answer(texts.t('ADMIN_STATS_UPDATE_ERROR', '❌ Ошибка обновления данных'), show_alert=True)


@admin_required
@error_handler
async def show_revenue_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    month_stats = await get_transactions_statistics(db, month_start, now)
    all_time_stats = await get_transactions_statistics(db)
    current_time = format_datetime(datetime.utcnow())

    payment_methods = []
    for method, data in month_stats['by_payment_method'].items():
        if method and data['count'] > 0:
            payment_methods.append(
                texts.t(
                    'ADMIN_STATS_REVENUE_PAYMENT_METHOD_LINE',
                    '• {method}: {count} ({amount})',
                ).format(
                    method=method,
                    count=data['count'],
                    amount=settings.format_price(data['amount']),
                )
            )

    text = texts.t(
        'ADMIN_STATS_REVENUE_TEXT',
        '💰 <b>Статистика доходов</b>\n\n'
        '<b>За текущий месяц:</b>\n'
        '- Доходы: {month_income}\n'
        '- Расходы: {month_expenses}\n'
        '- Прибыль: {month_profit}\n'
        '- От подписок: {month_subscription_income}\n\n'
        '<b>Сегодня:</b>\n'
        '- Транзакций: {today_transactions}\n'
        '- Доходы: {today_income}\n\n'
        '<b>За все время:</b>\n'
        '- Общий доход: {all_income}\n'
        '- Общая прибыль: {all_profit}\n\n'
        '<b>Способы оплаты:</b>\n'
        '{payment_methods}\n\n'
        '<b>Обновлено:</b> {current_time}',
    ).format(
        month_income=settings.format_price(month_stats['totals']['income_kopeks']),
        month_expenses=settings.format_price(month_stats['totals']['expenses_kopeks']),
        month_profit=settings.format_price(month_stats['totals']['profit_kopeks']),
        month_subscription_income=settings.format_price(month_stats['totals']['subscription_income_kopeks']),
        today_transactions=month_stats['today']['transactions_count'],
        today_income=settings.format_price(month_stats['today']['income_kopeks']),
        all_income=settings.format_price(all_time_stats['totals']['income_kopeks']),
        all_profit=settings.format_price(all_time_stats['totals']['profit_kopeks']),
        payment_methods='\n'.join(payment_methods),
        current_time=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            # [types.InlineKeyboardButton(text="📈 Период", callback_data="admin_revenue_period")],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'),
                    callback_data='admin_stats_revenue',
                )
            ],
            [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED', '✅ Статистика обновлена'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_DATA_ACTUAL', '📊 Данные актуальны'), show_alert=False)
        else:
            logger.error('Ошибка обновления статистики доходов', error=e)
            await callback.answer(texts.t('ADMIN_STATS_UPDATE_ERROR', '❌ Ошибка обновления данных'), show_alert=True)


@admin_required
@error_handler
async def show_referral_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_referral_statistics(db)
    current_time = format_datetime(datetime.utcnow())

    avg_per_referrer = 0
    if stats['active_referrers'] > 0:
        avg_per_referrer = stats['total_paid_kopeks'] / stats['active_referrers']

    text = texts.t(
        'ADMIN_STATS_REFERRALS_TEXT',
        '🤝 <b>Реферальная статистика</b>\n\n'
        '<b>Общие показатели:</b>\n'
        '- Пользователей с рефералами: {users_with_referrals}\n'
        '- Активных рефереров: {active_referrers}\n'
        '- Выплачено всего: {total_paid}\n\n'
        '<b>За период:</b>\n'
        '- Сегодня: {today_earnings}\n'
        '- За неделю: {week_earnings}\n'
        '- За месяц: {month_earnings}\n\n'
        '<b>Средние показатели:</b>\n'
        '- На одного рефререра: {avg_per_referrer}\n\n'
        '<b>Топ рефереры:</b>\n',
    ).format(
        users_with_referrals=stats['users_with_referrals'],
        active_referrers=stats['active_referrers'],
        total_paid=settings.format_price(stats['total_paid_kopeks']),
        today_earnings=settings.format_price(stats['today_earnings_kopeks']),
        week_earnings=settings.format_price(stats['week_earnings_kopeks']),
        month_earnings=settings.format_price(stats['month_earnings_kopeks']),
        avg_per_referrer=settings.format_price(int(avg_per_referrer)),
    )

    if stats['top_referrers']:
        for i, referrer in enumerate(stats['top_referrers'][:5], 1):
            name = referrer['display_name']
            earned = settings.format_price(referrer['total_earned_kopeks'])
            count = referrer['referrals_count']
            text += texts.t(
                'ADMIN_STATS_REFERRALS_TOP_ITEM',
                '{index}. {name}: {earned} ({count} реф.)\n',
            ).format(index=i, name=name, earned=earned, count=count)
    else:
        text += texts.t('ADMIN_STATS_REFERRALS_NO_ACTIVE', 'Пока нет активных рефереров')

    text += texts.t('ADMIN_STATS_UPDATED_AT_LINE', '\n<b>Обновлено:</b> {current_time}').format(
        current_time=current_time
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'),
                    callback_data='admin_stats_referrals',
                )
            ],
            [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED', '✅ Статистика обновлена'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_DATA_ACTUAL', '📊 Данные актуальны'), show_alert=False)
        else:
            logger.error('Ошибка обновления реферальной статистики', error=e)
            await callback.answer(texts.t('ADMIN_STATS_UPDATE_ERROR', '❌ Ошибка обновления данных'), show_alert=True)


@admin_required
@error_handler
async def show_summary_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    user_service = UserService()
    user_stats = await user_service.get_user_statistics(db)
    sub_stats = await get_subscriptions_statistics(db)

    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue_stats = await get_transactions_statistics(db, month_start, now)
    current_time = format_datetime(datetime.utcnow())

    conversion_rate = 0
    if user_stats['total_users'] > 0:
        conversion_rate = sub_stats['paid_subscriptions'] / user_stats['total_users'] * 100

    arpu = 0
    if user_stats['active_users'] > 0:
        arpu = revenue_stats['totals']['income_kopeks'] / user_stats['active_users']

    text = texts.t(
        'ADMIN_STATS_SUMMARY_TEXT',
        '📊 <b>Общая сводка системы</b>\n\n'
        '<b>Пользователи:</b>\n'
        '- Всего: {total_users}\n'
        '- Активных: {active_users}\n'
        '- Новых за месяц: {new_month_users}\n\n'
        '<b>Подписки:</b>\n'
        '- Активных: {active_subscriptions}\n'
        '- Платных: {paid_subscriptions}\n'
        '- Конверсия: {conversion_rate}\n\n'
        '<b>Финансы (месяц):</b>\n'
        '- Доходы: {income}\n'
        '- ARPU: {arpu}\n'
        '- Транзакций: {transactions_count}\n\n'
        '<b>Рост:</b>\n'
        '- Пользователи: +{new_month_users} за месяц\n'
        '- Продажи: +{purchased_month} за месяц\n\n'
        '<b>Обновлено:</b> {current_time}',
    ).format(
        total_users=user_stats['total_users'],
        active_users=user_stats['active_users'],
        new_month_users=user_stats['new_month'],
        active_subscriptions=sub_stats['active_subscriptions'],
        paid_subscriptions=sub_stats['paid_subscriptions'],
        conversion_rate=format_percentage(conversion_rate),
        income=settings.format_price(revenue_stats['totals']['income_kopeks']),
        arpu=settings.format_price(int(arpu)),
        transactions_count=sum(data['count'] for data in revenue_stats['by_type'].values()),
        purchased_month=sub_stats['purchased_month'],
        current_time=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_HISTORY_REFRESH', '🔄 Обновить'),
                    callback_data='admin_stats_summary',
                )
            ],
            [types.InlineKeyboardButton(text=texts.t('BACK', '⬅️ Назад'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED', '✅ Статистика обновлена'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_DATA_ACTUAL', '📊 Данные актуальны'), show_alert=False)
        else:
            logger.error('Ошибка обновления общей статистики', error=e)
            await callback.answer(texts.t('ADMIN_STATS_UPDATE_ERROR', '❌ Ошибка обновления данных'), show_alert=True)


@admin_required
@error_handler
async def show_revenue_by_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    period = callback.data.split('_')[-1]

    period_map = {'today': 1, 'yesterday': 1, 'week': 7, 'month': 30, 'all': 365}

    days = period_map.get(period, 30)
    revenue_data = await get_revenue_by_period(db, days)

    if period == 'yesterday':
        yesterday = datetime.utcnow().date() - timedelta(days=1)
        revenue_data = [r for r in revenue_data if r['date'] == yesterday]
    elif period == 'today':
        today = datetime.utcnow().date()
        revenue_data = [r for r in revenue_data if r['date'] == today]

    total_revenue = sum(r['amount_kopeks'] for r in revenue_data)
    avg_daily = total_revenue / len(revenue_data) if revenue_data else 0

    period_labels = {
        'today': texts.t('ADMIN_REFERRALS_PERIOD_TODAY', 'сегодня'),
        'yesterday': texts.t('ADMIN_REFERRALS_PERIOD_YESTERDAY', 'вчера'),
        'week': texts.t('ADMIN_STATS_PERIOD_WEEK', 'неделя'),
        'month': texts.t('ADMIN_STATS_PERIOD_MONTH', 'месяц'),
        'all': texts.t('ADMIN_STATS_PERIOD_ALL', 'все время'),
    }
    period_label = period_labels.get(period, period)

    text = texts.t(
        'ADMIN_STATS_REVENUE_PERIOD_TEXT',
        '📈 <b>Доходы за период: {period}</b>\n\n'
        '<b>Сводка:</b>\n'
        '- Общий доход: {total_revenue}\n'
        '- Дней с данными: {days_count}\n'
        '- Средний доход в день: {avg_daily}\n\n'
        '<b>По дням:</b>\n',
    ).format(
        period=period_label,
        total_revenue=settings.format_price(total_revenue),
        days_count=len(revenue_data),
        avg_daily=settings.format_price(int(avg_daily)),
    )

    for revenue in revenue_data[-10:]:
        text += texts.t(
            'ADMIN_STATS_REVENUE_DAY_LINE',
            '• {date}: {amount}\n',
        ).format(
            date=revenue['date'].strftime('%d.%m'),
            amount=settings.format_price(revenue['amount_kopeks']),
        )

    if len(revenue_data) > 10:
        text += texts.t('ADMIN_STATS_REVENUE_MORE_DAYS', '... и еще {count} дней').format(
            count=len(revenue_data) - 10
        )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_STATS_OTHER_PERIOD', '📊 Другой период'),
                        callback_data='admin_revenue_period',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_STATS_BACK_TO_REVENUE', '⬅️ К доходам'),
                        callback_data='admin_stats_revenue',
                    )
                ],
            ]
        ),
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_statistics_menu, F.data == 'admin_statistics')
    dp.callback_query.register(show_users_statistics, F.data == 'admin_stats_users')
    dp.callback_query.register(show_subscriptions_statistics, F.data == 'admin_stats_subs')
    dp.callback_query.register(show_revenue_statistics, F.data == 'admin_stats_revenue')
    dp.callback_query.register(show_referral_statistics, F.data == 'admin_stats_referrals')
    dp.callback_query.register(show_summary_statistics, F.data == 'admin_stats_summary')
    dp.callback_query.register(show_revenue_by_period, F.data.startswith('period_'))

    periods = ['today', 'yesterday', 'week', 'month', 'all']
    for period in periods:
        dp.callback_query.register(show_revenue_by_period, F.data == f'period_{period}')
