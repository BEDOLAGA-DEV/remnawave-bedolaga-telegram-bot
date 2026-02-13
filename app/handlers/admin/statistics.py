import logging
from datetime import datetime, timedelta

from aiogram import Dispatcher, F, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import get_referral_statistics
from app.database.crud.subscription import get_subscriptions_statistics
from app.database.crud.transaction import get_revenue_by_period, get_transactions_statistics
from app.database.models import User
from app.keyboards.admin import get_admin_statistics_keyboard
from app.localization.texts import get_texts
from app.services.user_service import UserService
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime, format_percentage


logger = logging.getLogger(__name__)


@admin_required
@error_handler
async def show_statistics_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = texts.t('ADMIN_STATS_MENU_TEXT')

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

    current_time = format_datetime(datetime.utcnow())

    text = texts.t('ADMIN_STATS_USERS_TEXT').format(
        total_users=stats['total_users'],
        active_users=stats['active_users'],
        active_rate=active_rate,
        blocked_users=stats['blocked_users'],
        new_today=stats['new_today'],
        new_week=stats['new_week'],
        new_month=stats['new_month'],
        month_growth_percent=format_percentage(stats['new_month'] / total_users * 100 if total_users > 0 else 0),
        updated=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('ADMIN_REFRESH'), callback_data='admin_stats_users')],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATS_BACK_BUTTON'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_DATA_ACTUAL'), show_alert=False)
        else:
            logger.error(f'Ошибка обновления статистики пользователей: {e}')
            await callback.answer(texts.t('ADMIN_STATS_UPDATE_ERROR'), show_alert=True)
            return

    await callback.answer(texts.t('ADMIN_STATS_UPDATED_ALERT'))


@admin_required
@error_handler
async def show_subscriptions_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_subscriptions_statistics(db)

    total_subs = stats['total_subscriptions']
    conversion_rate = format_percentage(stats['paid_subscriptions'] / total_subs * 100 if total_subs > 0 else 0)
    current_time = format_datetime(datetime.utcnow())

    text = texts.t('ADMIN_STATS_SUBSCRIPTIONS_TEXT').format(
        total_subscriptions=stats['total_subscriptions'],
        active_subscriptions=stats['active_subscriptions'],
        paid_subscriptions=stats['paid_subscriptions'],
        trial_subscriptions=stats['trial_subscriptions'],
        conversion_rate=conversion_rate,
        purchased_today=stats['purchased_today'],
        purchased_week=stats['purchased_week'],
        purchased_month=stats['purchased_month'],
        updated=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('ADMIN_REFRESH'), callback_data='admin_stats_subs')],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATS_BACK_BUTTON'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED_ALERT'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_DATA_ACTUAL'), show_alert=False)
        else:
            logger.error(f'Ошибка обновления статистики подписок: {e}')
            await callback.answer(texts.t('ADMIN_STATS_UPDATE_ERROR'), show_alert=True)


@admin_required
@error_handler
async def show_revenue_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    month_stats = await get_transactions_statistics(db, month_start, now)
    all_time_stats = await get_transactions_statistics(db)
    current_time = format_datetime(datetime.utcnow())

    payment_methods_block = ''

    for method, data in month_stats['by_payment_method'].items():
        if method and data['count'] > 0:
            payment_methods_block += texts.t('ADMIN_STATS_REVENUE_PAYMENT_METHOD_LINE').format(
                method=method,
                count=data['count'],
                amount=settings.format_price(data['amount']),
            )

    if not payment_methods_block:
        payment_methods_block = texts.t('ADMIN_STATS_REVENUE_NO_PAYMENT_METHODS')

    text = texts.t('ADMIN_STATS_REVENUE_TEXT').format(
        month_income=settings.format_price(month_stats['totals']['income_kopeks']),
        month_expenses=settings.format_price(month_stats['totals']['expenses_kopeks']),
        month_profit=settings.format_price(month_stats['totals']['profit_kopeks']),
        month_subscription_income=settings.format_price(month_stats['totals']['subscription_income_kopeks']),
        today_transactions=month_stats['today']['transactions_count'],
        today_income=settings.format_price(month_stats['today']['income_kopeks']),
        all_time_income=settings.format_price(all_time_stats['totals']['income_kopeks']),
        all_time_profit=settings.format_price(all_time_stats['totals']['profit_kopeks']),
        payment_methods=payment_methods_block,
        updated=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('ADMIN_REFRESH'), callback_data='admin_stats_revenue')],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATS_BACK_BUTTON'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED_ALERT'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_DATA_ACTUAL'), show_alert=False)
        else:
            logger.error(f'Ошибка обновления статистики доходов: {e}')
            await callback.answer(texts.t('ADMIN_STATS_UPDATE_ERROR'), show_alert=True)


@admin_required
@error_handler
async def show_referral_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_referral_statistics(db)
    current_time = format_datetime(datetime.utcnow())

    avg_per_referrer = 0
    if stats['active_referrers'] > 0:
        avg_per_referrer = stats['total_paid_kopeks'] / stats['active_referrers']

    top_referrers_block = ''

    if stats['top_referrers']:
        for i, referrer in enumerate(stats['top_referrers'][:5], 1):
            name = referrer['display_name']
            earned = settings.format_price(referrer['total_earned_kopeks'])
            count = referrer['referrals_count']
            top_referrers_block += texts.t('ADMIN_STATS_REFERRAL_TOP_LINE').format(
                index=i,
                name=name,
                earned=earned,
                count=count,
            )
    else:
        top_referrers_block = texts.t('ADMIN_STATS_REFERRAL_NO_ACTIVE')

    text = texts.t('ADMIN_STATS_REFERRAL_TEXT').format(
        users_with_referrals=stats['users_with_referrals'],
        active_referrers=stats['active_referrers'],
        total_paid=settings.format_price(stats['total_paid_kopeks']),
        today_earnings=settings.format_price(stats['today_earnings_kopeks']),
        week_earnings=settings.format_price(stats['week_earnings_kopeks']),
        month_earnings=settings.format_price(stats['month_earnings_kopeks']),
        avg_per_referrer=settings.format_price(int(avg_per_referrer)),
        top_referrers=top_referrers_block,
        updated=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('ADMIN_REFRESH'), callback_data='admin_stats_referrals')],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATS_BACK_BUTTON'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED_ALERT'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_DATA_ACTUAL'), show_alert=False)
        else:
            logger.error(f'Ошибка обновления реферальной статистики: {e}')
            await callback.answer(texts.t('ADMIN_STATS_UPDATE_ERROR'), show_alert=True)


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

    text = texts.t('ADMIN_STATS_SUMMARY_TEXT').format(
        total_users=user_stats['total_users'],
        active_users=user_stats['active_users'],
        new_month_users=user_stats['new_month'],
        active_subscriptions=sub_stats['active_subscriptions'],
        paid_subscriptions=sub_stats['paid_subscriptions'],
        conversion_rate=format_percentage(conversion_rate),
        income_month=settings.format_price(revenue_stats['totals']['income_kopeks']),
        arpu=settings.format_price(int(arpu)),
        transactions_count=sum(data['count'] for data in revenue_stats['by_type'].values()),
        purchased_month=sub_stats['purchased_month'],
        updated=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('ADMIN_REFRESH'), callback_data='admin_stats_summary')],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATS_BACK_BUTTON'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATS_UPDATED_ALERT'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATS_DATA_ACTUAL'), show_alert=False)
        else:
            logger.error(f'Ошибка обновления общей статистики: {e}')
            await callback.answer(texts.t('ADMIN_STATS_UPDATE_ERROR'), show_alert=True)


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

    period_key = {
        'today': 'ADMIN_STATS_PERIOD_TODAY',
        'yesterday': 'ADMIN_STATS_PERIOD_YESTERDAY',
        'week': 'ADMIN_STATS_PERIOD_WEEK',
        'month': 'ADMIN_STATS_PERIOD_MONTH',
        'all': 'ADMIN_STATS_PERIOD_ALL',
    }.get(period, 'ADMIN_STATS_PERIOD_MONTH')
    period_label = texts.t(period_key)

    daily_rows = ''

    for revenue in revenue_data[-10:]:
        daily_rows += texts.t('ADMIN_STATS_REVENUE_PERIOD_DAY_LINE').format(
            day=revenue['date'].strftime('%d.%m'),
            amount=settings.format_price(revenue['amount_kopeks']),
        )

    extra_days_line = ''
    if len(revenue_data) > 10:
        extra_days_line = texts.t('ADMIN_STATS_REVENUE_PERIOD_EXTRA_DAYS').format(count=len(revenue_data) - 10)

    text = texts.t('ADMIN_STATS_REVENUE_PERIOD_TEXT').format(
        period=period_label,
        total_revenue=settings.format_price(total_revenue),
        days_count=len(revenue_data),
        avg_daily=settings.format_price(int(avg_daily)),
        daily_rows=daily_rows,
        extra_days_line=extra_days_line,
    )

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_STATS_OTHER_PERIOD_BUTTON'), callback_data='admin_revenue_period'
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_STATS_TO_REVENUE_BUTTON'), callback_data='admin_stats_revenue'
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
