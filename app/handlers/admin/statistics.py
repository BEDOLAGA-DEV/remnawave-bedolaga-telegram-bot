from datetime import UTC, datetime, timedelta

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


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_statistics_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    text = """
📊 <b>Статистика системы</b>

Выберите раздел для просмотра статистики:
"""

    await callback.message.edit_text(text, reply_markup=get_admin_statistics_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)

    total_users = stats['total_users']
    active_rate = format_percentage(stats['active_users'] / total_users * 100 if total_users > 0 else 0)

    current_time = format_datetime(datetime.now(UTC))

    text = f"""
👥 <b>Статистика пользователей</b>

<b>Общие показатели:</b>
- Всего зарегистрировано: {stats['total_users']}
- Активных: {stats['active_users']} ({active_rate})
- Заблокированных: {stats['blocked_users']}

<b>Новые регистрации:</b>
- Сегодня: {stats['new_today']}
- За неделю: {stats['new_week']}
- За месяц: {stats['new_month']}

<b>Активность:</b>
- Коэффициент активности: {active_rate}
- Рост за месяц: +{stats['new_month']} ({format_percentage(stats['new_month'] / total_users * 100 if total_users > 0 else 0)})

<b>Обновлено:</b> {current_time}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_stats_users')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer('📊 Данные актуальны', show_alert=False)
        else:
            logger.error('Ошибка обновления статистики пользователей', error=e)
            await callback.answer('❌ Ошибка обновления данных', show_alert=True)
            return

    await callback.answer('✅ Статистика обновлена')


@admin_required
@error_handler
async def show_subscriptions_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_subscriptions_statistics(db)

    total_subs = stats['total_subscriptions']
    conversion_rate = format_percentage(stats['paid_subscriptions'] / total_subs * 100 if total_subs > 0 else 0)
    current_time = format_datetime(datetime.now(UTC))

    text = f"""
📱 <b>Статистика подписок</b>

<b>Общие показатели:</b>
- Всего подписок: {stats['total_subscriptions']}
- Активных: {stats['active_subscriptions']}
- Платных: {stats['paid_subscriptions']}
- Триальных: {stats['trial_subscriptions']}

<b>Конверсия:</b>
- Из триала в платную: {conversion_rate}
- Активных платных: {stats['paid_subscriptions']}

<b>Продажи:</b>
- Сегодня: {stats['purchased_today']}
- За неделю: {stats['purchased_week']}
- За месяц: {stats['purchased_month']}

<b>Обновлено:</b> {current_time}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_stats_subs')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer('✅ Статистика обновлена')
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer('📊 Данные актуальны', show_alert=False)
        else:
            logger.error('Ошибка обновления статистики подписок', error=e)
            await callback.answer('❌ Ошибка обновления данных', show_alert=True)


@admin_required
@error_handler
async def show_revenue_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    month_stats = await get_transactions_statistics(db, month_start, now)
    all_time_stats = await get_transactions_statistics(db, start_date=datetime(2020, 1, 1, tzinfo=UTC), end_date=now)
    current_time = format_datetime(datetime.now(UTC))

    text = f"""
💰 <b>Статистика доходов</b>

<b>За текущий месяц:</b>
- Доходы: {settings.format_price(month_stats['totals']['income_kopeks'])}
- Расходы: {settings.format_price(month_stats['totals']['expenses_kopeks'])}
- Прибыль: {settings.format_price(month_stats['totals']['profit_kopeks'])}
- От подписок: {settings.format_price(abs(month_stats['totals']['subscription_income_kopeks']))}

<b>Сегодня:</b>
- Транзакций: {month_stats['today']['transactions_count']}
- Доходы: {settings.format_price(month_stats['today']['income_kopeks'])}

<b>За все время:</b>
- Общий доход: {settings.format_price(all_time_stats['totals']['income_kopeks'])}
- Общая прибыль: {settings.format_price(all_time_stats['totals']['profit_kopeks'])}

<b>Способы оплаты:</b>
"""

    for method, data in month_stats['by_payment_method'].items():
        if method and data['count'] > 0:
            text += f'• {method}: {data["count"]} ({settings.format_price(data["amount"])})\n'

    text += f'\n<b>Обновлено:</b> {current_time}'

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            # [types.InlineKeyboardButton(text="📈 Период", callback_data="admin_revenue_period")],
            [types.InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_stats_revenue')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer('✅ Статистика обновлена')
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer('📊 Данные актуальны', show_alert=False)
        else:
            logger.error('Ошибка обновления статистики доходов', error=e)
            await callback.answer('❌ Ошибка обновления данных', show_alert=True)


@admin_required
@error_handler
async def show_referral_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    stats = await get_referral_statistics(db)
    current_time = format_datetime(datetime.now(UTC))

    avg_per_referrer = 0
    if stats['active_referrers'] > 0:
        avg_per_referrer = stats['total_paid_kopeks'] / stats['active_referrers']

    text = f"""
🤝 <b>Реферальная статистика</b>

<b>Общие показатели:</b>
- Пользователей с рефералами: {stats['users_with_referrals']}
- Активных рефереров: {stats['active_referrers']}
- Выплачено всего: {settings.format_price(stats['total_paid_kopeks'])}

<b>За период:</b>
- Сегодня: {settings.format_price(stats['today_earnings_kopeks'])}
- За неделю: {settings.format_price(stats['week_earnings_kopeks'])}
- За месяц: {settings.format_price(stats['month_earnings_kopeks'])}

<b>Средние показатели:</b>
- На одного рефререра: {settings.format_price(int(avg_per_referrer))}

<b>Топ рефереры:</b>
"""

    if stats['top_referrers']:
        for i, referrer in enumerate(stats['top_referrers'][:5], 1):
            name = referrer['display_name']
            earned = settings.format_price(referrer['total_earned_kopeks'])
            count = referrer['referrals_count']
            text += f'{i}. {name}: {earned} ({count} реф.)\n'
    else:
        text += 'Пока нет активных рефереров'

    text += f'\n<b>Обновлено:</b> {current_time}'

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_stats_referrals')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer('✅ Статистика обновлена')
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer('📊 Данные актуальны', show_alert=False)
        else:
            logger.error('Ошибка обновления реферальной статистики', error=e)
            await callback.answer('❌ Ошибка обновления данных', show_alert=True)


@admin_required
@error_handler
async def show_summary_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    user_service = UserService()
    user_stats = await user_service.get_user_statistics(db)
    sub_stats = await get_subscriptions_statistics(db)

    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue_stats = await get_transactions_statistics(db, month_start, now)
    current_time = format_datetime(datetime.now(UTC))

    conversion_rate = 0
    if user_stats['total_users'] > 0:
        conversion_rate = sub_stats['paid_subscriptions'] / user_stats['total_users'] * 100

    arpu = 0
    if user_stats['active_users'] > 0:
        arpu = revenue_stats['totals']['income_kopeks'] / user_stats['active_users']

    text = f"""
📊 <b>Общая сводка системы</b>

<b>Пользователи:</b>
- Всего: {user_stats['total_users']}
- Активных: {user_stats['active_users']}
- Новых за месяц: {user_stats['new_month']}

<b>Подписки:</b>
- Активных: {sub_stats['active_subscriptions']}
- Платных: {sub_stats['paid_subscriptions']}
- Конверсия: {format_percentage(conversion_rate)}

<b>Финансы (месяц):</b>
- Доходы: {settings.format_price(revenue_stats['totals']['income_kopeks'])}
- ARPU: {settings.format_price(int(arpu))}
- Транзакций: {sum(data['count'] for data in revenue_stats['by_type'].values())}

<b>Рост:</b>
- Пользователи: +{user_stats['new_month']} за месяц
- Продажи: +{sub_stats['purchased_month']} за месяц

<b>Обновлено:</b> {current_time}
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_stats_summary')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer('✅ Статистика обновлена')
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer('📊 Данные актуальны', show_alert=False)
        else:
            logger.error('Ошибка обновления общей статистики', error=e)
            await callback.answer('❌ Ошибка обновления данных', show_alert=True)


@admin_required
@error_handler
async def show_revenue_by_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    period = callback.data.split('_')[-1]

    period_map = {'today': 1, 'yesterday': 1, 'week': 7, 'month': 30, 'all': 365}

    days = period_map.get(period, 30)
    revenue_data = await get_revenue_by_period(db, days)

    if period == 'yesterday':
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        revenue_data = [r for r in revenue_data if r['date'] == yesterday]
    elif period == 'today':
        today = datetime.now(UTC).date()
        revenue_data = [r for r in revenue_data if r['date'] == today]

    total_revenue = sum(r['amount_kopeks'] for r in revenue_data)
    avg_daily = total_revenue / len(revenue_data) if revenue_data else 0

    text = f"""
📈 <b>Доходы за период: {period}</b>

<b>Сводка:</b>
- Общий доход: {settings.format_price(total_revenue)}
- Дней с данными: {len(revenue_data)}
- Средний доход в день: {settings.format_price(int(avg_daily))}

<b>По дням:</b>
"""

    for revenue in revenue_data[-10:]:
        text += f'• {revenue["date"].strftime("%d.%m")}: {settings.format_price(revenue["amount_kopeks"])}\n'

    if len(revenue_data) > 10:
        text += f'... и еще {len(revenue_data) - 10} дней'

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='📊 Другой период', callback_data='admin_revenue_period')],
                [types.InlineKeyboardButton(text='⬅️ К доходам', callback_data='admin_stats_revenue')],
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
