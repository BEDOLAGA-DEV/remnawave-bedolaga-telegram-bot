from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Transaction, TransactionType, User, WlTrafficPurchase
from app.localization.texts import get_texts
from app.utils.decorators import admin_required, error_handler

logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_wl_analytics(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    texts = get_texts(db_user.language)

    # 1. Total purchases count and total GB sold
    totals_query = select(
        func.count(WlTrafficPurchase.id),
        func.coalesce(func.sum(WlTrafficPurchase.traffic_gb), 0),
    )
    totals_result = await db.execute(totals_query)
    total_count, total_gb = totals_result.one()

    # 2. Revenue from WL traffic transactions (description contains 'БС' or 'wl_traffic')
    revenue_query = select(
        func.count(Transaction.id),
        func.coalesce(func.sum(Transaction.amount_kopeks), 0),
    ).where(
        Transaction.is_completed.is_(True),
        Transaction.type == TransactionType.WITHDRAWAL.value,
        (Transaction.description.ilike('%БС%') | Transaction.description.ilike('%wl_traffic%')),
    )
    revenue_result = await db.execute(revenue_query)
    revenue_count, revenue_total_kopeks = revenue_result.one()

    # 3. Top 5 most popular package sizes
    popular_query = (
        select(
            WlTrafficPurchase.traffic_gb,
            func.count(WlTrafficPurchase.id).label('cnt'),
        )
        .group_by(WlTrafficPurchase.traffic_gb)
        .order_by(func.count(WlTrafficPurchase.id).desc())
        .limit(5)
    )
    popular_result = await db.execute(popular_query)
    popular_packages = popular_result.all()

    # Build message
    lines = [
        '<b>📊 WL-аналитика трафика</b>',
        '',
        f'📦 Всего покупок: <b>{total_count}</b>',
        f'📶 Всего продано: <b>{total_gb} ГБ</b>',
        '',
        f'💰 Транзакций (списание): <b>{revenue_count}</b>',
        f'💵 Сумма списаний: <b>{settings.format_price(abs(revenue_total_kopeks))}</b>',
    ]

    if popular_packages:
        lines.append('')
        lines.append('<b>🏆 Топ-5 популярных пакетов:</b>')
        for i, (traffic_gb, cnt) in enumerate(popular_packages, 1):
            lines.append(f'  {i}. {traffic_gb} ГБ — {cnt} покупок')

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )

    await callback.message.edit_text('\n'.join(lines), parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_wl_analytics, F.data == 'admin_wl_analytics')
