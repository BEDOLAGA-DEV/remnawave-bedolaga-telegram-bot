import html
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
from app.localization.texts import get_texts
from app.services.referral_reward_service import format_reward_total
from app.services.user_service import UserService
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime, format_percentage
from app.utils.timezone import local_date, local_month_start


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_statistics_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', 'ru'))
    text = texts.t('ADMIN_STATISTICS_000')

    await callback.message.edit_text(text, reply_markup=get_admin_statistics_keyboard(db_user.language))
    await callback.answer()


@admin_required
@error_handler
async def show_users_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', 'ru'))
    user_service = UserService()
    stats = await user_service.get_user_statistics(db)

    total_users = stats['total_users']
    active_rate = format_percentage(stats['active_users'] / total_users * 100 if total_users > 0 else 0)

    current_time = format_datetime(datetime.now(UTC))

    text = texts.t('ADMIN_STATISTICS_001').format(
        value1=stats['total_users'],
        value2=stats['active_users'],
        value3=active_rate,
        value4=stats['blocked_users'],
        value5=stats['deleted_users'],
        value6=stats['new_today'],
        value7=stats['new_week'],
        value8=stats['new_month'],
        value9=active_rate,
        value10=stats['new_month'],
        value11=format_percentage(stats['new_month'] / total_users * 100 if total_users > 0 else 0),
        value12=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_017'), callback_data='admin_stats_users')],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_018'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATISTICS_015'), show_alert=False)
        else:
            logger.error('Ошибка обновления статистики пользователей', error=e)
            await callback.answer(texts.t('ADMIN_STATISTICS_016'), show_alert=True)
            return

    await callback.answer(texts.t('ADMIN_STATISTICS_012'))


@admin_required
@error_handler
async def show_subscriptions_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', 'ru'))
    stats = await get_subscriptions_statistics(db)

    total_subs = stats['total_subscriptions']
    conversion_rate = format_percentage(stats['paid_subscriptions'] / total_subs * 100 if total_subs > 0 else 0)
    current_time = format_datetime(datetime.now(UTC))

    text = texts.t('ADMIN_STATISTICS_002').format(
        value1=stats['total_subscriptions'],
        value2=stats['active_subscriptions'],
        value3=stats['paid_subscriptions'],
        value4=stats['trial_subscriptions'],
        value5=conversion_rate,
        value6=stats['paid_subscriptions'],
        value7=stats['purchased_today'],
        value8=stats['purchased_week'],
        value9=stats['purchased_month'],
        value10=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_017'), callback_data='admin_stats_subs')],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_018'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATISTICS_012'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATISTICS_015'), show_alert=False)
        else:
            logger.error('Ошибка обновления статистики подписок', error=e)
            await callback.answer(texts.t('ADMIN_STATISTICS_016'), show_alert=True)


@admin_required
@error_handler
async def show_revenue_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', 'ru'))
    now = datetime.now(UTC)
    month_start = local_month_start(now)

    month_stats = await get_transactions_statistics(db, month_start, now)
    all_time_stats = await get_transactions_statistics(db, start_date=datetime(2020, 1, 1, tzinfo=UTC), end_date=now)
    current_time = format_datetime(datetime.now(UTC))

    text = texts.t('ADMIN_STATISTICS_003').format(
        value1=settings.format_price(month_stats['totals']['income_kopeks']),
        value2=settings.format_price(month_stats['totals']['expenses_kopeks']),
        value3=settings.format_price(month_stats['totals']['profit_kopeks']),
        value4=settings.format_price(abs(month_stats['totals']['subscription_income_kopeks'])),
        value5=month_stats['today']['transactions_count'],
        value6=settings.format_price(month_stats['today']['income_kopeks']),
        value7=settings.format_price(all_time_stats['totals']['income_kopeks']),
        value8=settings.format_price(all_time_stats['totals']['profit_kopeks']),
    )

    for method, data in month_stats['by_payment_method'].items():
        if method and data['count'] > 0:
            text += f'• {method}: {data["count"]} ({settings.format_price(data["amount"])})\n'

    text += texts.t('ADMIN_STATISTICS_004').format(value1=current_time)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            # [types.InlineKeyboardButton(text="📈 Период", callback_data="admin_revenue_period")],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_017'), callback_data='admin_stats_revenue')],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_018'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATISTICS_012'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATISTICS_015'), show_alert=False)
        else:
            logger.error('Ошибка обновления статистики доходов', error=e)
            await callback.answer(texts.t('ADMIN_STATISTICS_016'), show_alert=True)


@admin_required
@error_handler
async def show_referral_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', 'ru'))
    stats = await get_referral_statistics(db)
    current_time = format_datetime(datetime.now(UTC))

    avg_per_referrer = 0
    if stats['active_referrers'] > 0:
        avg_per_referrer = stats['total_paid_kopeks'] / stats['active_referrers']

    # Дни — вторая валюта программы: без них экран показывает «выплачено 0 ₽»
    # на установке, где начисления идут днями подписки.
    text = texts.t('ADMIN_STATISTICS_005').format(
        value1=stats['users_with_referrals'],
        value2=stats['active_referrers'],
        value3=format_reward_total(
            stats['total_paid_kopeks'], stats.get('total_paid_days', 0), language=texts.language
        ),
        value4=format_reward_total(
            stats['today_earnings_kopeks'], stats.get('today_earnings_days', 0), language=texts.language
        ),
        value5=format_reward_total(
            stats['week_earnings_kopeks'], stats.get('week_earnings_days', 0), language=texts.language
        ),
        value6=format_reward_total(
            stats['month_earnings_kopeks'], stats.get('month_earnings_days', 0), language=texts.language
        ),
        value7=settings.format_price(int(avg_per_referrer)),
    )

    meaningful_levels = [row for row in (stats.get('by_level') or []) if row.get('money_kopeks') or row.get('days')]
    if len(meaningful_levels) > 1:
        text += texts.t('ADMIN_STATISTICS_009')
        for row in meaningful_levels:
            text += texts.t('ADMIN_STATISTICS_013').format(
                value1=row['level'],
                value2=format_reward_total(row.get('money_kopeks', 0), row.get('days', 0), language=texts.language),
            )

    text += texts.t('ADMIN_STATISTICS_006')

    if stats['top_referrers']:
        for i, referrer in enumerate(stats['top_referrers'][:5], 1):
            name = html.escape(referrer['display_name'])
            earned = format_reward_total(
                referrer['total_earned_kopeks'], referrer.get('total_earned_days', 0), language=texts.language
            )
            count = referrer['referrals_count']
            text += texts.t('ADMIN_STATISTICS_014').format(value1=i, value2=name, value3=earned, value4=count)
    else:
        text += texts.t('ADMIN_STATISTICS_010')

    text += texts.t('ADMIN_STATISTICS_004').format(value1=current_time)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_017'), callback_data='admin_stats_referrals')],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_018'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATISTICS_012'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATISTICS_015'), show_alert=False)
        else:
            logger.error('Ошибка обновления реферальной статистики', error=e)
            await callback.answer(texts.t('ADMIN_STATISTICS_016'), show_alert=True)


@admin_required
@error_handler
async def show_summary_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', 'ru'))
    user_service = UserService()
    user_stats = await user_service.get_user_statistics(db)
    sub_stats = await get_subscriptions_statistics(db)

    now = datetime.now(UTC)
    month_start = local_month_start(now)
    revenue_stats = await get_transactions_statistics(db, month_start, now)
    current_time = format_datetime(datetime.now(UTC))

    conversion_rate = 0
    if user_stats['total_users'] > 0:
        conversion_rate = sub_stats['paid_subscriptions'] / user_stats['total_users'] * 100

    arpu = 0
    if user_stats['active_users'] > 0:
        arpu = revenue_stats['totals']['income_kopeks'] / user_stats['active_users']

    text = texts.t('ADMIN_STATISTICS_007').format(
        value1=user_stats['total_users'],
        value2=user_stats['active_users'],
        value3=user_stats['new_month'],
        value4=sub_stats['active_subscriptions'],
        value5=sub_stats['paid_subscriptions'],
        value6=format_percentage(conversion_rate),
        value7=settings.format_price(revenue_stats['totals']['income_kopeks']),
        value8=settings.format_price(int(arpu)),
        value9=sum(data['count'] for data in revenue_stats['by_type'].values()),
        value10=user_stats['new_month'],
        value11=sub_stats['purchased_month'],
        value12=current_time,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_017'), callback_data='admin_stats_summary')],
            [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_018'), callback_data='admin_statistics')],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(texts.t('ADMIN_STATISTICS_012'))
    except Exception as e:
        if 'message is not modified' in str(e):
            await callback.answer(texts.t('ADMIN_STATISTICS_015'), show_alert=False)
        else:
            logger.error('Ошибка обновления общей статистики', error=e)
            await callback.answer(texts.t('ADMIN_STATISTICS_016'), show_alert=True)


@admin_required
@error_handler
async def show_revenue_by_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(getattr(db_user, 'language', 'ru'))
    period = callback.data.split('_')[-1]

    # Дни — календарные, в settings.TIMEZONE; «вчера» требует двух дней данных.
    period_map = {'today': 1, 'yesterday': 2, 'week': 7, 'month': 30, 'all': 365}

    days = period_map.get(period, 30)
    revenue_data = await get_revenue_by_period(db, days)

    if period == 'yesterday':
        yesterday = local_date() - timedelta(days=1)
        revenue_data = [r for r in revenue_data if r['date'] == yesterday]
    elif period == 'today':
        today = local_date()
        revenue_data = [r for r in revenue_data if r['date'] == today]

    total_revenue = sum(r['amount_kopeks'] for r in revenue_data)
    avg_daily = total_revenue / len(revenue_data) if revenue_data else 0

    text = texts.t('ADMIN_STATISTICS_008').format(
        value1=texts.t(f'ADMIN_STATISTICS_PERIOD_{period.upper()}', period),
        value2=settings.format_price(total_revenue),
        value3=len(revenue_data),
        value4=settings.format_price(int(avg_daily)),
    )

    for revenue in revenue_data[-10:]:
        text += f'• {revenue["date"].strftime("%d.%m")}: {settings.format_price(revenue["amount_kopeks"])}\n'

    if len(revenue_data) > 10:
        text += texts.t('ADMIN_STATISTICS_011').format(value1=len(revenue_data) - 10)

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_STATISTICS_019'), callback_data='admin_revenue_period'
                    )
                ],
                [types.InlineKeyboardButton(text=texts.t('ADMIN_STATISTICS_020'), callback_data='admin_stats_revenue')],
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
