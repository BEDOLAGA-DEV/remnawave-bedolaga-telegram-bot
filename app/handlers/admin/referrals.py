import datetime
import json

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import (
    get_referral_statistics,
    get_top_referrers_by_period,
)
from app.database.crud.user import get_user_by_id, get_user_by_telegram_id
from app.database.models import ReferralEarning, User, WithdrawalRequest, WithdrawalRequestStatus
from app.localization.texts import get_texts
from app.services.referral_withdrawal_service import referral_withdrawal_service
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_referral_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        stats = await get_referral_statistics(db)

        avg_per_referrer = 0
        if stats.get('active_referrers', 0) > 0:
            avg_per_referrer = stats.get('total_paid_kopeks', 0) / stats['active_referrers']

        current_time = datetime.datetime.now().strftime('%H:%M:%S')

        notifications_text = (
            texts.t('ADMIN_REFERRALS_NOTIFICATIONS_ENABLED', '✅ Включены')
            if settings.REFERRAL_NOTIFICATIONS_ENABLED
            else texts.t('ADMIN_REFERRALS_NOTIFICATIONS_DISABLED', '❌ Отключены')
        )
        text = texts.t(
            'ADMIN_REFERRALS_STATS_TEXT',
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
            '- На одного реферера: {avg_per_referrer}\n\n'
            '<b>Топ-5 рефереров:</b>',
        ).format(
            users_with_referrals=stats.get('users_with_referrals', 0),
            active_referrers=stats.get('active_referrers', 0),
            total_paid=settings.format_price(stats.get('total_paid_kopeks', 0)),
            today_earnings=settings.format_price(stats.get('today_earnings_kopeks', 0)),
            week_earnings=settings.format_price(stats.get('week_earnings_kopeks', 0)),
            month_earnings=settings.format_price(stats.get('month_earnings_kopeks', 0)),
            avg_per_referrer=settings.format_price(int(avg_per_referrer)),
        )

        top_referrers = stats.get('top_referrers', [])
        if top_referrers:
            for i, referrer in enumerate(top_referrers[:5], 1):
                earned = referrer.get('total_earned_kopeks', 0)
                count = referrer.get('referrals_count', 0)
                user_id = referrer.get('user_id', 'N/A')

                if count > 0:
                    text += (
                        '\n'
                        + texts.t(
                            'ADMIN_REFERRALS_STATS_TOP_ITEM',
                            '{index}. ID {user_id}: {earned} ({count} реф.)',
                        ).format(index=i, user_id=user_id, earned=settings.format_price(earned), count=count)
                    )
                else:
                    logger.warning('Реферер имеет рефералов, но есть в топе', user_id=user_id, count=count)
        else:
            text += '\n' + texts.t('ADMIN_REFERRALS_STATS_NO_DATA', 'Нет данных')

        text += '\n\n' + texts.t(
            'ADMIN_REFERRALS_STATS_SETTINGS_BLOCK',
            '<b>Настройки реферальной системы:</b>\n'
            '- Минимальное пополнение: {minimum_topup}\n'
            '- Бонус за первое пополнение: {first_topup_bonus}\n'
            '- Бонус пригласившему: {inviter_bonus}\n'
            '- Комиссия с покупок: {commission_percent}%\n'
            '- Уведомления: {notifications}\n\n'
            '<i>🕐 Обновлено: {current_time}</i>',
        ).format(
            minimum_topup=settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
            first_topup_bonus=settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
            inviter_bonus=settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS),
            commission_percent=settings.REFERRAL_COMMISSION_PERCENT,
            notifications=notifications_text,
            current_time=current_time,
        )

        keyboard_rows = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_BUTTON_REFRESH', '🔄 Обновить'), callback_data='admin_referrals'
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_BUTTON_TOP', '👥 Топ рефереров'),
                    callback_data='admin_referrals_top',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_BUTTON_DIAGNOSTICS', '🔍 Диагностика логов'),
                    callback_data='admin_referral_diagnostics',
                )
            ],
        ]

        # Кнопка заявок на вывод (если функция включена)
        if settings.is_referral_withdrawal_enabled():
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_BUTTON_WITHDRAWALS', '💸 Заявки на вывод'),
                        callback_data='admin_withdrawal_requests',
                    )
                ]
            )

        keyboard_rows.extend(
            [
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_BUTTON_SETTINGS', '⚙️ Настройки'),
                        callback_data='admin_referrals_settings',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer(texts.t('ADMIN_REFERRALS_UPDATED', 'Обновлено'))
        except Exception as edit_error:
            if 'message is not modified' in str(edit_error):
                await callback.answer(texts.t('ADMIN_REFERRALS_DATA_ACTUAL', 'Данные актуальны'))
            else:
                logger.error('Ошибка редактирования сообщения', edit_error=edit_error)
                await callback.answer(texts.t('ADMIN_REFERRALS_UPDATE_ERROR', 'Ошибка обновления'))

    except Exception as e:
        logger.error('Ошибка в show_referral_statistics', error=e, exc_info=True)

        current_time = datetime.datetime.now().strftime('%H:%M:%S')
        text = texts.t(
            'ADMIN_REFERRALS_STATS_LOAD_ERROR_TEXT',
            '🤝 <b>Реферальная статистика</b>\n\n'
            '❌ <b>Ошибка загрузки данных</b>\n\n'
            '<b>Текущие настройки:</b>\n'
            '- Минимальное пополнение: {minimum_topup}\n'
            '- Бонус за первое пополнение: {first_topup_bonus}\n'
            '- Бонус пригласившему: {inviter_bonus}\n'
            '- Комиссия с покупок: {commission_percent}%\n\n'
            '<i>🕐 Время: {current_time}</i>',
        ).format(
            minimum_topup=settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
            first_topup_bonus=settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
            inviter_bonus=settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS),
            commission_percent=settings.REFERRAL_COMMISSION_PERCENT,
            current_time=current_time,
        )

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('ADMIN_REFERRALS_BUTTON_RETRY', '🔄 Повторить'), callback_data='admin_referrals')],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
            ]
        )

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except:
            pass
        await callback.answer(texts.t('ADMIN_REFERRALS_LOAD_ERROR', 'Произошла ошибка при загрузке статистики'))


def _get_top_keyboard(period: str, sort_by: str, texts) -> types.InlineKeyboardMarkup:
    """Создаёт клавиатуру для выбора периода и сортировки."""
    period_week = (
        texts.t('ADMIN_REFERRALS_TOP_PERIOD_WEEK_ACTIVE', '✅ Неделя')
        if period == 'week'
        else texts.t('ADMIN_REFERRALS_TOP_PERIOD_WEEK', 'Неделя')
    )
    period_month = (
        texts.t('ADMIN_REFERRALS_TOP_PERIOD_MONTH_ACTIVE', '✅ Месяц')
        if period == 'month'
        else texts.t('ADMIN_REFERRALS_TOP_PERIOD_MONTH', 'Месяц')
    )
    sort_earnings = (
        texts.t('ADMIN_REFERRALS_TOP_SORT_EARNINGS_ACTIVE', '✅ По заработку')
        if sort_by == 'earnings'
        else texts.t('ADMIN_REFERRALS_TOP_SORT_EARNINGS', 'По заработку')
    )
    sort_invited = (
        texts.t('ADMIN_REFERRALS_TOP_SORT_INVITED_ACTIVE', '✅ По приглашённым')
        if sort_by == 'invited'
        else texts.t('ADMIN_REFERRALS_TOP_SORT_INVITED', 'По приглашённым')
    )

    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text=period_week, callback_data=f'admin_top_ref:week:{sort_by}'),
                types.InlineKeyboardButton(text=period_month, callback_data=f'admin_top_ref:month:{sort_by}'),
            ],
            [
                types.InlineKeyboardButton(text=sort_earnings, callback_data=f'admin_top_ref:{period}:earnings'),
                types.InlineKeyboardButton(text=sort_invited, callback_data=f'admin_top_ref:{period}:invited'),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_BUTTON_REFRESH', '🔄 Обновить'),
                    callback_data=f'admin_top_ref:{period}:{sort_by}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_TOP_BACK_TO_STATS', '⬅️ К статистике'),
                    callback_data='admin_referrals',
                )
            ],
        ]
    )


@admin_required
@error_handler
async def show_top_referrers(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает топ рефереров (по умолчанию: неделя, по заработку)."""
    texts = get_texts(db_user.language)
    await _show_top_referrers_filtered(callback, db, period='week', sort_by='earnings', texts=texts)


@admin_required
@error_handler
async def show_top_referrers_filtered(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Обрабатывает выбор периода и сортировки."""
    texts = get_texts(db_user.language)
    # Парсим callback_data: admin_top_ref:period:sort_by
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer(texts.t('ADMIN_REFERRALS_PARAMS_ERROR', 'Ошибка параметров'))
        return

    period = parts[1]  # week или month
    sort_by = parts[2]  # earnings или invited

    if period not in ('week', 'month'):
        period = 'week'
    if sort_by not in ('earnings', 'invited'):
        sort_by = 'earnings'

    await _show_top_referrers_filtered(callback, db, period, sort_by, texts)


async def _show_top_referrers_filtered(
    callback: types.CallbackQuery, db: AsyncSession, period: str, sort_by: str, texts
):
    """Внутренняя функция отображения топа с фильтрами."""
    try:
        top_referrers = await get_top_referrers_by_period(db, period=period, sort_by=sort_by)

        period_text = (
            texts.t('ADMIN_REFERRALS_TOP_PERIOD_TEXT_WEEK', 'за неделю')
            if period == 'week'
            else texts.t('ADMIN_REFERRALS_TOP_PERIOD_TEXT_MONTH', 'за месяц')
        )
        sort_text = (
            texts.t('ADMIN_REFERRALS_TOP_SORT_TEXT_EARNINGS', 'по заработку')
            if sort_by == 'earnings'
            else texts.t('ADMIN_REFERRALS_TOP_SORT_TEXT_INVITED', 'по приглашённым')
        )

        text = texts.t('ADMIN_REFERRALS_TOP_TITLE', '🏆 <b>Топ рефереров {period}</b>').format(period=period_text) + '\n'
        text += texts.t('ADMIN_REFERRALS_TOP_SORT_LINE', '<i>Сортировка: {sort}</i>').format(sort=sort_text) + '\n\n'

        if top_referrers:
            for i, referrer in enumerate(top_referrers[:20], 1):
                earned = referrer.get('earnings_kopeks', 0)
                count = referrer.get('invited_count', 0)
                display_name = referrer.get('display_name', 'N/A')
                username = referrer.get('username', '')
                telegram_id = referrer.get('telegram_id')
                user_email = referrer.get('email', '')
                user_id = referrer.get('user_id', '')
                id_display = telegram_id or user_email or f'#{user_id}' if user_id else 'N/A'

                if username:
                    display_text = f'@{username} (ID{id_display})'
                elif display_name and display_name != f'ID{id_display}':
                    display_text = f'{display_name} (ID{id_display})'
                else:
                    display_text = f'ID{id_display}'

                emoji = ''
                if i == 1:
                    emoji = '🥇 '
                elif i == 2:
                    emoji = '🥈 '
                elif i == 3:
                    emoji = '🥉 '

                # Выделяем основную метрику в зависимости от сортировки
                if sort_by == 'invited':
                    text += f'{emoji}{i}. {display_text}\n'
                    text += (
                        texts.t(
                            'ADMIN_REFERRALS_TOP_INVITED_ITEM',
                            '   👥 <b>{count} приглашённых</b> | 💰 {earned}',
                        ).format(count=count, earned=settings.format_price(earned))
                        + '\n\n'
                    )
                else:
                    text += f'{emoji}{i}. {display_text}\n'
                    text += (
                        texts.t(
                            'ADMIN_REFERRALS_TOP_EARNINGS_ITEM',
                            '   💰 <b>{earned}</b> | 👥 {count} приглашённых',
                        ).format(earned=settings.format_price(earned), count=count)
                        + '\n\n'
                    )
        else:
            text += texts.t('ADMIN_REFERRALS_TOP_NO_DATA', 'Нет данных за выбранный период') + '\n'

        keyboard = _get_top_keyboard(period, sort_by, texts)

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer()
        except Exception as edit_error:
            if 'message is not modified' in str(edit_error):
                await callback.answer(texts.t('ADMIN_REFERRALS_DATA_ACTUAL', 'Данные актуальны'))
            else:
                raise

    except Exception as e:
        logger.error('Ошибка в show_top_referrers_filtered', error=e, exc_info=True)
        await callback.answer(texts.t('ADMIN_REFERRALS_TOP_LOAD_ERROR', 'Ошибка загрузки топа рефереров'))


@admin_required
@error_handler
async def show_referral_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    notifications_status = (
        texts.t('ADMIN_REFERRALS_NOTIFICATIONS_ENABLED', '✅ Включены')
        if settings.REFERRAL_NOTIFICATIONS_ENABLED
        else texts.t('ADMIN_REFERRALS_NOTIFICATIONS_DISABLED', '❌ Отключены')
    )
    text = texts.t(
        'ADMIN_REFERRALS_SETTINGS_TEXT',
        '⚙️ <b>Настройки реферальной системы</b>\n\n'
        '<b>Бонусы и награды:</b>\n'
        '• Минимальная сумма пополнения для участия: {minimum_topup}\n'
        '• Бонус за первое пополнение реферала: {first_topup_bonus}\n'
        '• Бонус пригласившему за первое пополнение: {inviter_bonus}\n\n'
        '<b>Комиссионные:</b>\n'
        '• Процент с каждой покупки реферала: {commission_percent}%\n\n'
        '<b>Уведомления:</b>\n'
        '• Статус: {notifications_status}\n'
        '• Попытки отправки: {retry_attempts}\n\n'
        '<i>💡 Для изменения настроек отредактируйте файл .env и перезапустите бота</i>',
    ).format(
        minimum_topup=settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
        first_topup_bonus=settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
        inviter_bonus=settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS),
        commission_percent=settings.REFERRAL_COMMISSION_PERCENT,
        notifications_status=notifications_status,
        retry_attempts=getattr(settings, 'REFERRAL_NOTIFICATION_RETRY_ATTEMPTS', 3),
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_TOP_BACK_TO_STATS', '⬅️ К статистике'),
                    callback_data='admin_referrals',
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_pending_withdrawal_requests(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает список ожидающих заявок на вывод."""
    texts = get_texts(db_user.language)
    requests = await referral_withdrawal_service.get_pending_requests(db)

    if not requests:
        text = texts.t(
            'ADMIN_REFERRALS_WITHDRAWAL_PENDING_EMPTY',
            '📋 <b>Заявки на вывод</b>\n\nНет ожидающих заявок.',
        )

        keyboard_rows = []
        # Кнопка тестового начисления (только в тестовом режиме)
        if settings.REFERRAL_WITHDRAWAL_TEST_MODE:
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_BUTTON_TEST_EARNING', '🧪 Тестовое начисление'),
                        callback_data='admin_test_referral_earning',
                    )
                ]
            )
        keyboard_rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_referrals')])

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
        await callback.answer()
        return

    text = texts.t('ADMIN_REFERRALS_WITHDRAWAL_PENDING_TITLE', '📋 <b>Заявки на вывод ({count})</b>').format(
        count=len(requests)
    ) + '\n\n'

    for req in requests[:10]:
        user = await get_user_by_id(db, req.user_id)
        user_name = user.full_name if user else texts.t('ADMIN_REFERRALS_UNKNOWN_USER', 'Неизвестно')
        user_tg_id = user.telegram_id if user else 'N/A'

        risk_emoji = (
            '🟢' if req.risk_score < 30 else '🟡' if req.risk_score < 50 else '🟠' if req.risk_score < 70 else '🔴'
        )

        text += texts.t(
            'ADMIN_REFERRALS_WITHDRAWAL_PENDING_ITEM',
            '<b>#{request_id}</b> — {user_name} (ID{user_tg_id})\n'
            '💰 {amount} | {risk_emoji} Риск: {risk_score}/100\n'
            '📅 {created_at}',
        ).format(
            request_id=req.id,
            user_name=user_name,
            user_tg_id=user_tg_id,
            amount=f'{req.amount_kopeks / 100:.0f}₽',
            risk_emoji=risk_emoji,
            risk_score=req.risk_score,
            created_at=req.created_at.strftime('%d.%m.%Y %H:%M'),
        ) + '\n\n'

    keyboard_rows = []
    for req in requests[:5]:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_WITHDRAWAL_PENDING_BUTTON', '#{request_id} — {amount}').format(
                        request_id=req.id, amount=f'{req.amount_kopeks / 100:.0f}₽'
                    ),
                    callback_data=f'admin_withdrawal_view_{req.id}',
                )
            ]
        )

    # Кнопка тестового начисления (только в тестовом режиме)
    if settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_BUTTON_TEST_EARNING', '🧪 Тестовое начисление'),
                    callback_data='admin_test_referral_earning',
                )
            ]
        )

    keyboard_rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_referrals')])

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
    await callback.answer()


@admin_required
@error_handler
async def view_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает детали заявки на вывод."""
    texts = get_texts(db_user.language)
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer(texts.t('ADMIN_REFERRALS_WITHDRAWAL_NOT_FOUND', 'Заявка не найдена'), show_alert=True)
        return

    user = await get_user_by_id(db, request.user_id)
    user_name = user.full_name if user else texts.t('ADMIN_REFERRALS_UNKNOWN_USER', 'Неизвестно')
    user_tg_id = (user.telegram_id or user.email or f'#{user.id}') if user else 'N/A'

    analysis = json.loads(request.risk_analysis) if request.risk_analysis else {}

    status_text = {
        WithdrawalRequestStatus.PENDING.value: texts.t('ADMIN_REFERRALS_WITHDRAWAL_STATUS_PENDING', '⏳ Ожидает'),
        WithdrawalRequestStatus.APPROVED.value: texts.t('ADMIN_REFERRALS_WITHDRAWAL_STATUS_APPROVED', '✅ Одобрена'),
        WithdrawalRequestStatus.REJECTED.value: texts.t('ADMIN_REFERRALS_WITHDRAWAL_STATUS_REJECTED', '❌ Отклонена'),
        WithdrawalRequestStatus.COMPLETED.value: texts.t('ADMIN_REFERRALS_WITHDRAWAL_STATUS_COMPLETED', '✅ Выполнена'),
        WithdrawalRequestStatus.CANCELLED.value: texts.t('ADMIN_REFERRALS_WITHDRAWAL_STATUS_CANCELLED', '🚫 Отменена'),
    }.get(request.status, request.status)

    text = texts.t(
        'ADMIN_REFERRALS_WITHDRAWAL_DETAILS_TEXT',
        '📋 <b>Заявка #{request_id}</b>\n\n'
        '👤 Пользователь: {user_name}\n'
        '🆔 ID: <code>{user_tg_id}</code>\n'
        '💰 Сумма: <b>{amount}</b>\n'
        '📊 Статус: {status}\n\n'
        '💳 <b>Реквизиты:</b>\n'
        '<code>{payment_details}</code>\n\n'
        '📅 Создана: {created_at}\n\n'
        '{analysis_text}',
    ).format(
        request_id=request.id,
        user_name=user_name,
        user_tg_id=user_tg_id,
        amount=f'{request.amount_kopeks / 100:.0f}₽',
        status=status_text,
        payment_details=request.payment_details,
        created_at=request.created_at.strftime('%d.%m.%Y %H:%M'),
        analysis_text=referral_withdrawal_service.format_analysis_for_admin(analysis),
    )

    keyboard = []

    if request.status == WithdrawalRequestStatus.PENDING.value:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_WITHDRAWAL_BUTTON_APPROVE', '✅ Одобрить'),
                    callback_data=f'admin_withdrawal_approve_{request.id}',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_WITHDRAWAL_BUTTON_REJECT', '❌ Отклонить'),
                    callback_data=f'admin_withdrawal_reject_{request.id}',
                ),
            ]
        )

    if request.status == WithdrawalRequestStatus.APPROVED.value:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_WITHDRAWAL_BUTTON_COMPLETE', '✅ Деньги переведены'),
                    callback_data=f'admin_withdrawal_complete_{request.id}',
                )
            ]
        )

    if user:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_WITHDRAWAL_BUTTON_USER_PROFILE', '👤 Профиль пользователя'),
                    callback_data=f'admin_user_manage_{user.id}',
                )
            ]
        )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_REFERRALS_WITHDRAWAL_BUTTON_BACK_TO_LIST', '⬅️ К списку'),
                callback_data='admin_withdrawal_requests',
            )
        ]
    )

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def approve_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Одобряет заявку на вывод."""
    texts = get_texts(db_user.language)
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer(texts.t('ADMIN_REFERRALS_WITHDRAWAL_NOT_FOUND', 'Заявка не найдена'), show_alert=True)
        return

    success, error = await referral_withdrawal_service.approve_request(db, request_id, db_user.id)

    if success:
        # Уведомляем пользователя (только если есть telegram_id)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                user_texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    user_texts.t(
                        'REFERRAL_WITHDRAWAL_APPROVED',
                        '✅ <b>Заявка на вывод #{id} одобрена!</b>\n\n'
                        'Сумма: <b>{amount}</b>\n'
                        'Средства списаны с баланса.\n\n'
                        'Ожидайте перевод на указанные реквизиты.',
                    ).format(id=request.id, amount=user_texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error('Ошибка отправки уведомления пользователю', error=e)

        await callback.answer(
            texts.t(
                'ADMIN_REFERRALS_WITHDRAWAL_APPROVED_ALERT',
                '✅ Заявка одобрена, средства списаны с баланса',
            )
        )

        # Обновляем отображение
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer(
            texts.t('ADMIN_REFERRALS_WITHDRAWAL_APPROVE_ERROR', '❌ {error}').format(error=error),
            show_alert=True,
        )


@admin_required
@error_handler
async def reject_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Отклоняет заявку на вывод."""
    texts = get_texts(db_user.language)
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer(texts.t('ADMIN_REFERRALS_WITHDRAWAL_NOT_FOUND', 'Заявка не найдена'), show_alert=True)
        return

    success = await referral_withdrawal_service.reject_request(
        db,
        request_id,
        db_user.id,
        texts.t('ADMIN_REFERRALS_WITHDRAWAL_REASON_REJECTED_BY_ADMIN', 'Отклонено администратором'),
    )

    if success:
        # Уведомляем пользователя (только если есть telegram_id)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                user_texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    user_texts.t(
                        'REFERRAL_WITHDRAWAL_REJECTED',
                        '❌ <b>Заявка на вывод #{id} отклонена</b>\n\n'
                        'Сумма: <b>{amount}</b>\n\n'
                        'Если у вас есть вопросы, обратитесь в поддержку.',
                    ).format(id=request.id, amount=user_texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error('Ошибка отправки уведомления пользователю', error=e)

        await callback.answer(texts.t('ADMIN_REFERRALS_WITHDRAWAL_REJECTED_ALERT', '❌ Заявка отклонена'))

        # Обновляем отображение
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer(texts.t('ADMIN_REFERRALS_WITHDRAWAL_REJECT_ERROR', '❌ Ошибка отклонения'), show_alert=True)


@admin_required
@error_handler
async def complete_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Отмечает заявку как выполненную (деньги переведены)."""
    texts = get_texts(db_user.language)
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer(texts.t('ADMIN_REFERRALS_WITHDRAWAL_NOT_FOUND', 'Заявка не найдена'), show_alert=True)
        return

    success = await referral_withdrawal_service.complete_request(
        db,
        request_id,
        db_user.id,
        texts.t('ADMIN_REFERRALS_WITHDRAWAL_REASON_TRANSFER_COMPLETED', 'Перевод выполнен'),
    )

    if success:
        # Уведомляем пользователя (только если есть telegram_id)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                user_texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    user_texts.t(
                        'REFERRAL_WITHDRAWAL_COMPLETED',
                        '💸 <b>Выплата по заявке #{id} выполнена!</b>\n\n'
                        'Сумма: <b>{amount}</b>\n\n'
                        'Деньги отправлены на указанные реквизиты.',
                    ).format(id=request.id, amount=user_texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error('Ошибка отправки уведомления пользователю', error=e)

        await callback.answer(texts.t('ADMIN_REFERRALS_WITHDRAWAL_COMPLETED_ALERT', '✅ Заявка выполнена'))

        # Обновляем отображение
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer(
            texts.t('ADMIN_REFERRALS_WITHDRAWAL_COMPLETE_ERROR', '❌ Ошибка выполнения'),
            show_alert=True,
        )


@admin_required
@error_handler
async def start_test_referral_earning(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Начинает процесс тестового начисления реферального дохода."""
    texts = get_texts(db_user.language)
    if not settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        await callback.answer(texts.t('ADMIN_REFERRALS_TEST_MODE_DISABLED', 'Тестовый режим отключён'), show_alert=True)
        return

    await state.set_state(AdminStates.test_referral_earning_input)

    text = texts.t(
        'ADMIN_REFERRALS_TEST_EARNING_PROMPT',
        '🧪 <b>Тестовое начисление реферального дохода</b>\n\n'
        'Введите данные в формате:\n'
        '<code>telegram_id сумма_в_рублях</code>\n\n'
        'Примеры:\n'
        '• <code>123456789 500</code> — начислит 500₽ пользователю 123456789\n'
        '• <code>987654321 1000</code> — начислит 1000₽ пользователю 987654321\n\n'
        '⚠️ Это создаст реальную запись ReferralEarning, как будто пользователь заработал с реферала.',
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_withdrawal_requests')]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def process_test_referral_earning(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Обрабатывает ввод тестового начисления."""
    texts = get_texts(db_user.language)
    if not settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        await message.answer(texts.t('ADMIN_REFERRALS_TEST_MODE_DISABLED_MSG', '❌ Тестовый режим отключён'))
        await state.clear()
        return

    text_input = message.text.strip()
    parts = text_input.split()

    if len(parts) != 2:
        await message.answer(
            texts.t(
                'ADMIN_REFERRALS_TEST_EARNING_FORMAT_ERROR',
                '❌ Неверный формат. Введите: <code>telegram_id сумма</code>\n\n'
                'Например: <code>123456789 500</code>',
            )
        )
        return

    try:
        target_telegram_id = int(parts[0])
        amount_rubles = float(parts[1].replace(',', '.'))
        amount_kopeks = int(amount_rubles * 100)

        if amount_kopeks <= 0:
            await message.answer(texts.t('ADMIN_REFERRALS_TEST_EARNING_AMOUNT_POSITIVE', '❌ Сумма должна быть положительной'))
            return

        if amount_kopeks > 10000000:  # Лимит 100 000₽
            await message.answer(
                texts.t(
                    'ADMIN_REFERRALS_TEST_EARNING_AMOUNT_LIMIT',
                    '❌ Максимальная сумма тестового начисления: 100 000₽',
                )
            )
            return

    except ValueError:
        await message.answer(
            texts.t(
                'ADMIN_REFERRALS_TEST_EARNING_NUMBERS_ERROR',
                '❌ Неверный формат чисел. Введите: <code>telegram_id сумма</code>\n\n'
                'Например: <code>123456789 500</code>',
            )
        )
        return

    # Ищем целевого пользователя
    target_user = await get_user_by_telegram_id(db, target_telegram_id)
    if not target_user:
        await message.answer(
            texts.t('ADMIN_REFERRALS_TEST_EARNING_USER_NOT_FOUND', '❌ Пользователь с ID {telegram_id} не найден в базе').format(
                telegram_id=target_telegram_id
            )
        )
        return

    # Создаём тестовое начисление
    earning = ReferralEarning(
        user_id=target_user.id,
        referral_id=target_user.id,  # Сам на себя (тестовое)
        amount_kopeks=amount_kopeks,
        reason='test_earning',
    )
    db.add(earning)

    # Добавляем на баланс пользователя
    target_user.balance_kopeks += amount_kopeks

    await db.commit()
    await state.clear()

    await message.answer(
        texts.t(
            'ADMIN_REFERRALS_TEST_EARNING_SUCCESS',
            '✅ <b>Тестовое начисление создано!</b>\n\n'
            '👤 Пользователь: {user_name}\n'
            '🆔 ID: <code>{telegram_id}</code>\n'
            '💰 Сумма: <b>{amount}</b>\n'
            '💳 Новый баланс: <b>{balance}</b>\n\n'
            'Начисление добавлено как реферальный доход.',
        ).format(
            user_name=target_user.full_name or texts.t('ADMIN_REFERRALS_NO_NAME', 'Без имени'),
            telegram_id=target_telegram_id,
            amount=f'{amount_rubles:.0f}₽',
            balance=f'{target_user.balance_kopeks / 100:.0f}₽',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_TEST_EARNING_BACK_TO_REQUESTS', '📋 К заявкам'),
                        callback_data='admin_withdrawal_requests',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_TEST_EARNING_PROFILE', '👤 Профиль'),
                        callback_data=f'admin_user_manage_{target_user.id}',
                    )
                ],
            ]
        ),
    )

    logger.info(
        'Тестовое начисление: админ начислил ₽ пользователю',
        telegram_id=db_user.telegram_id,
        amount_rubles=amount_rubles,
        target_telegram_id=target_telegram_id,
    )


def _get_period_dates(period: str) -> tuple[datetime.datetime, datetime.datetime]:
    """Возвращает начальную и конечную даты для заданного периода."""
    now = datetime.datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == 'today':
        start_date = today
        end_date = today + datetime.timedelta(days=1)
    elif period == 'yesterday':
        start_date = today - datetime.timedelta(days=1)
        end_date = today
    elif period == 'week':
        start_date = today - datetime.timedelta(days=7)
        end_date = today + datetime.timedelta(days=1)
    elif period == 'month':
        start_date = today - datetime.timedelta(days=30)
        end_date = today + datetime.timedelta(days=1)
    else:
        # По умолчанию — сегодня
        start_date = today
        end_date = today + datetime.timedelta(days=1)

    return start_date, end_date


def _get_period_display_name(period: str, texts) -> str:
    """Возвращает человекочитаемое название периода."""
    names = {
        'today': texts.t('ADMIN_REFERRALS_PERIOD_TODAY', 'сегодня'),
        'yesterday': texts.t('ADMIN_REFERRALS_PERIOD_YESTERDAY', 'вчера'),
        'week': texts.t('ADMIN_REFERRALS_PERIOD_WEEK', '7 дней'),
        'month': texts.t('ADMIN_REFERRALS_PERIOD_MONTH', '30 дней'),
    }
    return names.get(period, texts.t('ADMIN_REFERRALS_PERIOD_TODAY', 'сегодня'))


async def _show_diagnostics_for_period(
    callback: types.CallbackQuery, db: AsyncSession, state: FSMContext, period: str, texts
):
    """Внутренняя функция для отображения диагностики за указанный период."""
    try:
        await callback.answer(texts.t('ADMIN_REFERRALS_DIAG_ANALYZING_LOGS', 'Анализирую логи...'))

        from app.services.referral_diagnostics_service import referral_diagnostics_service

        # Сохраняем период в state
        await state.update_data(diagnostics_period=period)
        from app.states import AdminStates

        await state.set_state(AdminStates.referral_diagnostics_period)

        # Получаем даты периода
        start_date, end_date = _get_period_dates(period)

        # Анализируем логи
        report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)

        # Формируем отчёт
        period_display = _get_period_display_name(period, texts)

        text = texts.t(
            'ADMIN_REFERRALS_DIAG_TEXT',
            '🔍 <b>Диагностика рефералов — {period}</b>\n\n'
            '<b>📊 Статистика переходов:</b>\n'
            '• Всего кликов по реф-ссылкам: {total_ref_clicks}\n'
            '• Уникальных пользователей: {unique_users_clicked}\n'
            '• Потерянных рефералов: {lost_count}',
        ).format(
            period=period_display,
            total_ref_clicks=report.total_ref_clicks,
            unique_users_clicked=report.unique_users_clicked,
            lost_count=len(report.lost_referrals),
        )

        if report.lost_referrals:
            text += '\n' + texts.t('ADMIN_REFERRALS_DIAG_LOST_HEADER', '<b>❌ Потерянные рефералы:</b>') + '\n'
            text += texts.t(
                'ADMIN_REFERRALS_DIAG_LOST_HINT',
                '<i>(пришли по ссылке, но реферер не засчитался)</i>',
            ) + '\n\n'

            for i, lost in enumerate(report.lost_referrals[:15], 1):
                # Статус пользователя
                if not lost.registered:
                    status = texts.t('ADMIN_REFERRALS_DIAG_STATUS_NOT_IN_DB', '⚠️ Не в БД')
                elif not lost.has_referrer:
                    status = texts.t('ADMIN_REFERRALS_DIAG_STATUS_NO_REFERRER', '❌ Без реферера')
                else:
                    status = texts.t(
                        'ADMIN_REFERRALS_DIAG_STATUS_OTHER_REFERRER',
                        '⚡ Другой реферер (ID{referrer_id})',
                    ).format(referrer_id=lost.current_referrer_id)

                # Имя или ID
                user_name = lost.username or lost.full_name or f'ID{lost.telegram_id}'
                if lost.username:
                    user_name = f'@{lost.username}'

                # Ожидаемый реферер
                referrer_info = ''
                if lost.expected_referrer_name:
                    referrer_info = f' → {lost.expected_referrer_name}'
                elif lost.expected_referrer_id:
                    referrer_info = f' → ID{lost.expected_referrer_id}'

                # Время
                time_str = lost.click_time.strftime('%H:%M')

                text += f'{i}. {user_name} — {status}\n'
                text += f'   <code>{lost.referral_code}</code>{referrer_info} ({time_str})\n'

            if len(report.lost_referrals) > 15:
                text += (
                    '\n'
                    + texts.t('ADMIN_REFERRALS_DIAG_AND_MORE', '<i>... и ещё {count}</i>').format(
                        count=len(report.lost_referrals) - 15
                    )
                    + '\n'
                )
        else:
            text += '\n' + texts.t('ADMIN_REFERRALS_DIAG_ALL_ACCOUNTED', '✅ <b>Все рефералы засчитаны!</b>') + '\n'

        # Информация о логах
        log_path = referral_diagnostics_service.log_path
        log_exists = log_path.exists()
        log_size = log_path.stat().st_size if log_exists else 0

        text += '\n' + texts.t('ADMIN_REFERRALS_DIAG_LOG_FILE_PREFIX', '<i>📂 {file_name}').format(file_name=log_path.name)
        if log_exists:
            text += f' ({log_size / 1024:.0f} KB)'
            text += texts.t('ADMIN_REFERRALS_DIAG_LOG_LINES', ' | Строк: {lines}').format(lines=report.lines_in_period)
        else:
            text += texts.t('ADMIN_REFERRALS_DIAG_LOG_NOT_FOUND', ' (не найден!)')
        text += '</i>'

        # Кнопки: только "Сегодня" (текущий лог) и "Загрузить файл" (старые логи)
        keyboard_rows = [
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_DIAG_BUTTON_TODAY_LOG', '📅 Сегодня (текущий лог)'),
                    callback_data='admin_ref_diag:today',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_DIAG_BUTTON_UPLOAD_LOG', '📤 Загрузить лог-файл'),
                    callback_data='admin_ref_diag_upload',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_DIAG_BUTTON_CHECK_BONUSES', '🔍 Проверить бонусы (по БД)'),
                    callback_data='admin_ref_check_bonuses',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_DIAG_BUTTON_SYNC_CONTEST', '🏆 Синхронизировать с конкурсом'),
                    callback_data='admin_ref_sync_contest',
                )
            ],
        ]

        # Кнопки действий (только если есть потерянные рефералы)
        if report.lost_referrals:
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_DIAG_BUTTON_PREVIEW_FIXES', '📋 Предпросмотр исправлений'),
                        callback_data='admin_ref_fix_preview',
                    )
                ]
            )

        keyboard_rows.extend(
            [
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_BUTTON_REFRESH', '🔄 Обновить'),
                        callback_data=f'admin_ref_diag:{period}',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_TOP_BACK_TO_STATS', '⬅️ К статистике'),
                        callback_data='admin_referrals',
                    )
                ],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в _show_diagnostics_for_period', error=e, exc_info=True)
        await callback.answer(texts.t('ADMIN_REFERRALS_DIAG_ANALYZE_ERROR', 'Ошибка при анализе логов'), show_alert=True)


@admin_required
@error_handler
async def show_referral_diagnostics(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Показывает диагностику реферальной системы по логам."""
    texts = get_texts(db_user.language)
    # Определяем период из callback_data или используем "today" по умолчанию
    if ':' in callback.data:
        period = callback.data.split(':')[1]
    else:
        period = 'today'

    await _show_diagnostics_for_period(callback, db, state, period, texts)


@admin_required
@error_handler
async def preview_referral_fixes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Показывает предпросмотр исправлений потерянных рефералов."""
    texts = get_texts(db_user.language)
    try:
        await callback.answer(texts.t('ADMIN_REFERRALS_DIAG_ANALYZING_SHORT', 'Анализирую...'))

        # Получаем период из state
        state_data = await state.get_data()
        period = state_data.get('diagnostics_period', 'today')

        from app.services.referral_diagnostics_service import DiagnosticReport, referral_diagnostics_service

        # Проверяем, работаем ли с загруженным файлом
        if period == 'uploaded_file':
            # Используем сохранённый отчёт из загруженного файла (десериализуем)
            report_data = state_data.get('uploaded_file_report')
            if not report_data:
                await callback.answer(
                    texts.t('ADMIN_REFERRALS_UPLOADED_REPORT_NOT_FOUND', 'Отчёт загруженного файла не найден'),
                    show_alert=True,
                )
                return
            report = DiagnosticReport.from_dict(report_data)
            period_display = texts.t('ADMIN_REFERRALS_PERIOD_UPLOADED_FILE', 'загруженный файл')
        else:
            # Получаем даты периода
            start_date, end_date = _get_period_dates(period)

            # Анализируем логи
            report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)
            period_display = _get_period_display_name(period, texts)

        if not report.lost_referrals:
            await callback.answer(
                texts.t('ADMIN_REFERRALS_NO_LOST_FOR_FIX', 'Нет потерянных рефералов для исправления'),
                show_alert=True,
            )
            return

        # Запускаем предпросмотр исправлений
        fix_report = await referral_diagnostics_service.fix_lost_referrals(db, report.lost_referrals, apply=False)

        # Формируем отчёт
        text = texts.t(
            'ADMIN_REFERRALS_FIX_PREVIEW_TEXT',
            '📋 <b>Предпросмотр исправлений — {period}</b>\n\n'
            '<b>📊 Что будет сделано:</b>\n'
            '• Исправлено рефералов: {users_fixed}\n'
            '• Бонусов рефералам: {referral_bonuses}\n'
            '• Бонусов рефереам: {referrer_bonuses}\n'
            '• Ошибок: {errors}\n\n'
            '<b>🔍 Детали:</b>',
        ).format(
            period=period_display,
            users_fixed=fix_report.users_fixed,
            referral_bonuses=settings.format_price(fix_report.bonuses_to_referrals),
            referrer_bonuses=settings.format_price(fix_report.bonuses_to_referrers),
            errors=fix_report.errors,
        )

        # Показываем первые 10 деталей
        for i, detail in enumerate(fix_report.details[:10], 1):
            user_name = detail.username or detail.full_name or f'ID{detail.telegram_id}'
            if detail.username:
                user_name = f'@{detail.username}'

            if detail.error:
                text += f'{i}. {user_name} — ❌ {detail.error}\n'
            else:
                text += f'{i}. {user_name}\n'
                if detail.referred_by_set:
                    text += texts.t(
                        'ADMIN_REFERRALS_FIX_DETAIL_REFERRER',
                        '   • Реферер: {referrer}',
                    ).format(referrer=detail.referrer_name or f'ID{detail.referrer_id}') + '\n'
                if detail.had_first_topup:
                    text += texts.t(
                        'ADMIN_REFERRALS_FIX_DETAIL_FIRST_TOPUP',
                        '   • Первое пополнение: {amount}',
                    ).format(amount=settings.format_price(detail.topup_amount_kopeks)) + '\n'
                if detail.bonus_to_referral_kopeks > 0:
                    text += texts.t(
                        'ADMIN_REFERRALS_FIX_DETAIL_BONUS_REFERRAL',
                        '   • Бонус рефералу: {amount}',
                    ).format(amount=settings.format_price(detail.bonus_to_referral_kopeks)) + '\n'
                if detail.bonus_to_referrer_kopeks > 0:
                    text += texts.t(
                        'ADMIN_REFERRALS_FIX_DETAIL_BONUS_REFERRER',
                        '   • Бонус рефереру: {amount}',
                    ).format(amount=settings.format_price(detail.bonus_to_referrer_kopeks)) + '\n'

        if len(fix_report.details) > 10:
            text += (
                '\n'
                + texts.t('ADMIN_REFERRALS_DIAG_AND_MORE', '<i>... и ещё {count}</i>').format(
                    count=len(fix_report.details) - 10
                )
                + '\n'
            )

        text += '\n' + texts.t(
            'ADMIN_REFERRALS_FIX_PREVIEW_WARNING',
            '⚠️ <b>Внимание!</b> Это только предпросмотр. Нажмите "Применить", чтобы выполнить исправления.',
        )

        # Кнопка назад зависит от источника
        back_button_text = texts.t('ADMIN_REFERRALS_BUTTON_BACK_TO_DIAGNOSTICS', '⬅️ К диагностике')
        back_button_callback = f'admin_ref_diag:{period}' if period != 'uploaded_file' else 'admin_referral_diagnostics'

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_FIX_BUTTON_APPLY', '✅ Применить исправления'),
                        callback_data='admin_ref_fix_apply',
                    )
                ],
                [types.InlineKeyboardButton(text=back_button_text, callback_data=back_button_callback)],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в preview_referral_fixes', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_REFERRALS_FIX_PREVIEW_ERROR', 'Ошибка при создании предпросмотра'),
            show_alert=True,
        )


@admin_required
@error_handler
async def apply_referral_fixes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Применяет исправления потерянных рефералов."""
    texts = get_texts(db_user.language)
    try:
        await callback.answer(texts.t('ADMIN_REFERRALS_FIX_APPLYING', 'Применяю исправления...'))

        # Получаем период из state
        state_data = await state.get_data()
        period = state_data.get('diagnostics_period', 'today')

        from app.services.referral_diagnostics_service import DiagnosticReport, referral_diagnostics_service

        # Проверяем, работаем ли с загруженным файлом
        if period == 'uploaded_file':
            # Используем сохранённый отчёт из загруженного файла (десериализуем)
            report_data = state_data.get('uploaded_file_report')
            if not report_data:
                await callback.answer(
                    texts.t('ADMIN_REFERRALS_UPLOADED_REPORT_NOT_FOUND', 'Отчёт загруженного файла не найден'),
                    show_alert=True,
                )
                return
            report = DiagnosticReport.from_dict(report_data)
            period_display = texts.t('ADMIN_REFERRALS_PERIOD_UPLOADED_FILE', 'загруженный файл')
        else:
            # Получаем даты периода
            start_date, end_date = _get_period_dates(period)

            # Анализируем логи
            report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)
            period_display = _get_period_display_name(period, texts)

        if not report.lost_referrals:
            await callback.answer(
                texts.t('ADMIN_REFERRALS_NO_LOST_FOR_FIX', 'Нет потерянных рефералов для исправления'),
                show_alert=True,
            )
            return

        # Применяем исправления
        fix_report = await referral_diagnostics_service.fix_lost_referrals(db, report.lost_referrals, apply=True)

        # Формируем отчёт
        text = texts.t(
            'ADMIN_REFERRALS_FIX_APPLIED_TEXT',
            '✅ <b>Исправления применены — {period}</b>\n\n'
            '<b>📊 Результаты:</b>\n'
            '• Исправлено рефералов: {users_fixed}\n'
            '• Бонусов рефералам: {referral_bonuses}\n'
            '• Бонусов рефереам: {referrer_bonuses}\n'
            '• Ошибок: {errors}\n\n'
            '<b>🔍 Детали:</b>',
        ).format(
            period=period_display,
            users_fixed=fix_report.users_fixed,
            referral_bonuses=settings.format_price(fix_report.bonuses_to_referrals),
            referrer_bonuses=settings.format_price(fix_report.bonuses_to_referrers),
            errors=fix_report.errors,
        )

        # Показываем первые 10 успешных деталей
        success_count = 0
        for detail in fix_report.details:
            if not detail.error and success_count < 10:
                success_count += 1
                user_name = detail.username or detail.full_name or f'ID{detail.telegram_id}'
                if detail.username:
                    user_name = f'@{user_name}'

                text += f'{success_count}. {user_name}\n'
                if detail.referred_by_set:
                    text += texts.t(
                        'ADMIN_REFERRALS_FIX_DETAIL_REFERRER',
                        '   • Реферер: {referrer}',
                    ).format(referrer=detail.referrer_name or f'ID{detail.referrer_id}') + '\n'
                if detail.bonus_to_referral_kopeks > 0:
                    text += texts.t(
                        'ADMIN_REFERRALS_FIX_DETAIL_BONUS_REFERRAL',
                        '   • Бонус рефералу: {amount}',
                    ).format(amount=settings.format_price(detail.bonus_to_referral_kopeks)) + '\n'
                if detail.bonus_to_referrer_kopeks > 0:
                    text += texts.t(
                        'ADMIN_REFERRALS_FIX_DETAIL_BONUS_REFERRER',
                        '   • Бонус рефереру: {amount}',
                    ).format(amount=settings.format_price(detail.bonus_to_referrer_kopeks)) + '\n'

        if fix_report.users_fixed > 10:
            text += (
                '\n'
                + texts.t('ADMIN_REFERRALS_FIX_AND_MORE_FIXED', '<i>... и ещё {count} исправлений</i>').format(
                    count=fix_report.users_fixed - 10
                )
                + '\n'
            )

        # Показываем ошибки
        if fix_report.errors > 0:
            text += '\n' + texts.t('ADMIN_REFERRALS_FIX_ERRORS_HEADER', '<b>❌ Ошибки:</b>') + '\n'
            error_count = 0
            for detail in fix_report.details:
                if detail.error and error_count < 5:
                    error_count += 1
                    user_name = detail.username or detail.full_name or f'ID{detail.telegram_id}'
                    text += f'• {user_name}: {detail.error}\n'
            if fix_report.errors > 5:
                text += texts.t('ADMIN_REFERRALS_FIX_AND_MORE_ERRORS', '<i>... и ещё {count} ошибок</i>').format(
                    count=fix_report.errors - 5
                ) + '\n'

        # Кнопки зависят от источника
        keyboard_rows = []
        if period != 'uploaded_file':
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_FIX_BUTTON_REFRESH_DIAG', '🔄 Обновить диагностику'),
                        callback_data=f'admin_ref_diag:{period}',
                    )
                ]
            )
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_TOP_BACK_TO_STATS', '⬅️ К статистике'),
                    callback_data='admin_referrals',
                )
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.message.edit_text(text, reply_markup=keyboard)

        # Очищаем сохранённый отчёт из state
        if period == 'uploaded_file':
            await state.update_data(uploaded_file_report=None)

    except Exception as e:
        logger.error('Ошибка в apply_referral_fixes', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_REFERRALS_FIX_APPLY_ERROR', 'Ошибка при применении исправлений'),
            show_alert=True,
        )


# =============================================================================
# Проверка бонусов по БД
# =============================================================================


@admin_required
@error_handler
async def check_missing_bonuses(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Проверяет по БД — всем ли рефералам начислены бонусы."""
    texts = get_texts(db_user.language)
    from app.services.referral_diagnostics_service import (
        referral_diagnostics_service,
    )

    await callback.answer(texts.t('ADMIN_REFERRALS_BONUS_CHECK_RUNNING', '🔍 Проверяю бонусы...'))

    try:
        report = await referral_diagnostics_service.check_missing_bonuses(db)

        # Сохраняем отчёт в state для последующего применения
        await state.update_data(missing_bonuses_report=report.to_dict())

        text = texts.t(
            'ADMIN_REFERRALS_BONUS_CHECK_TEXT',
            '🔍 <b>Проверка бонусов по БД</b>\n\n'
            '📊 <b>Статистика:</b>\n'
            '• Всего рефералов: {total_referrals}\n'
            '• С пополнением ≥ минимума: {with_topup}\n'
            '• <b>Без бонусов: {missing_count}</b>',
        ).format(
            total_referrals=report.total_referrals_checked,
            with_topup=report.referrals_with_topup,
            missing_count=len(report.missing_bonuses),
        )

        if report.missing_bonuses:
            text += '\n' + texts.t(
                'ADMIN_REFERRALS_BONUS_CHECK_TO_CREDIT',
                '💰 <b>Требуется начислить:</b>\n'
                '• Рефералам: {to_referrals}\n'
                '• Рефереерам: {to_referrers}\n'
                '• <b>Итого: {total}</b>\n\n'
                '👤 <b>Список ({count} чел.):</b>',
            ).format(
                to_referrals=f'{report.total_missing_to_referrals / 100:.0f}₽',
                to_referrers=f'{report.total_missing_to_referrers / 100:.0f}₽',
                total=f'{(report.total_missing_to_referrals + report.total_missing_to_referrers) / 100:.0f}₽',
                count=len(report.missing_bonuses),
            )
            for i, mb in enumerate(report.missing_bonuses[:15], 1):
                referral_name = mb.referral_full_name or mb.referral_username or str(mb.referral_telegram_id)
                referrer_name = mb.referrer_full_name or mb.referrer_username or str(mb.referrer_telegram_id)
                text += f'\n{i}. <b>{referral_name}</b>'
                text += '\n' + texts.t('ADMIN_REFERRALS_BONUS_CHECK_LIST_REFERRER', '   └ Пригласил: {name}').format(
                    name=referrer_name
                )
                text += '\n' + texts.t('ADMIN_REFERRALS_BONUS_CHECK_LIST_TOPUP', '   └ Пополнение: {amount}').format(
                    amount=f'{mb.first_topup_amount_kopeks / 100:.0f}₽'
                )
                text += '\n' + texts.t(
                    'ADMIN_REFERRALS_BONUS_CHECK_LIST_BONUSES',
                    '   └ Бонусы: {referral_bonus} + {referrer_bonus}',
                ).format(
                    referral_bonus=f'{mb.referral_bonus_amount / 100:.0f}₽',
                    referrer_bonus=f'{mb.referrer_bonus_amount / 100:.0f}₽',
                )

            if len(report.missing_bonuses) > 15:
                text += '\n\n' + texts.t('ADMIN_REFERRALS_BONUS_CHECK_AND_MORE', '<i>... и ещё {count} чел.</i>').format(
                    count=len(report.missing_bonuses) - 15
                )

            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_REFERRALS_BONUS_CHECK_BUTTON_APPLY_ALL', '✅ Начислить все бонусы'),
                            callback_data='admin_ref_bonus_apply',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_REFERRALS_BUTTON_REFRESH', '🔄 Обновить'),
                            callback_data='admin_ref_check_bonuses',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_REFERRALS_BUTTON_BACK_TO_DIAGNOSTICS', '⬅️ К диагностике'),
                            callback_data='admin_referral_diagnostics',
                        )
                    ],
                ]
            )
        else:
            text += '\n' + texts.t('ADMIN_REFERRALS_BONUS_CHECK_ALL_DONE', '✅ <b>Все бонусы начислены!</b>')
            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_REFERRALS_BUTTON_REFRESH', '🔄 Обновить'),
                            callback_data='admin_ref_check_bonuses',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_REFERRALS_BUTTON_BACK_TO_DIAGNOSTICS', '⬅️ К диагностике'),
                            callback_data='admin_referral_diagnostics',
                        )
                    ],
                ]
            )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в check_missing_bonuses', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_REFERRALS_BONUS_CHECK_ERROR', 'Ошибка при проверке бонусов'),
            show_alert=True,
        )


@admin_required
@error_handler
async def apply_missing_bonuses(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Применяет начисление пропущенных бонусов."""
    texts = get_texts(db_user.language)
    from app.services.referral_diagnostics_service import (
        MissingBonusReport,
        referral_diagnostics_service,
    )

    await callback.answer(texts.t('ADMIN_REFERRALS_BONUS_APPLY_RUNNING', '💰 Начисляю бонусы...'))

    try:
        # Получаем сохранённый отчёт
        data = await state.get_data()
        report_dict = data.get('missing_bonuses_report')

        if not report_dict:
            await callback.answer(
                texts.t('ADMIN_REFERRALS_BONUS_REPORT_NOT_FOUND', '❌ Отчёт не найден. Обновите проверку.'),
                show_alert=True,
            )
            return

        report = MissingBonusReport.from_dict(report_dict)

        if not report.missing_bonuses:
            await callback.answer(
                texts.t('ADMIN_REFERRALS_BONUS_NOTHING_TO_APPLY', '✅ Нет бонусов для начисления'),
                show_alert=True,
            )
            return

        # Применяем исправления
        fix_report = await referral_diagnostics_service.fix_missing_bonuses(db, report.missing_bonuses, apply=True)

        text = texts.t(
            'ADMIN_REFERRALS_BONUS_APPLY_RESULT_TEXT',
            '✅ <b>Бонусы начислены!</b>\n\n'
            '📊 <b>Результат:</b>\n'
            '• Обработано: {users_fixed} пользователей\n'
            '• Начислено рефералам: {to_referrals}\n'
            '• Начислено рефереерам: {to_referrers}\n'
            '• <b>Итого: {total}</b>',
        ).format(
            users_fixed=fix_report.users_fixed,
            to_referrals=f'{fix_report.bonuses_to_referrals / 100:.0f}₽',
            to_referrers=f'{fix_report.bonuses_to_referrers / 100:.0f}₽',
            total=f'{(fix_report.bonuses_to_referrals + fix_report.bonuses_to_referrers) / 100:.0f}₽',
        )

        if fix_report.errors > 0:
            text += '\n' + texts.t('ADMIN_REFERRALS_BONUS_APPLY_ERRORS', '⚠️ Ошибок: {count}').format(
                count=fix_report.errors
            )

        # Очищаем отчёт из state
        await state.update_data(missing_bonuses_report=None)

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_BONUS_APPLY_BUTTON_CHECK_AGAIN', '🔍 Проверить снова'),
                        callback_data='admin_ref_check_bonuses',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_BUTTON_BACK_TO_DIAGNOSTICS', '⬅️ К диагностике'),
                        callback_data='admin_referral_diagnostics',
                    )
                ],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в apply_missing_bonuses', error=e, exc_info=True)
        await callback.answer(
            texts.t('ADMIN_REFERRALS_BONUS_APPLY_ERROR', 'Ошибка при начислении бонусов'),
            show_alert=True,
        )


@admin_required
@error_handler
async def sync_referrals_with_contest(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Синхронизирует всех рефералов с активными конкурсами."""
    texts = get_texts(db_user.language)
    from app.database.crud.referral_contest import get_contests_for_events
    from app.services.referral_contest_service import referral_contest_service

    await callback.answer(texts.t('ADMIN_REFERRALS_SYNC_RUNNING', '🏆 Синхронизирую с конкурсами...'))

    try:
        from datetime import datetime

        now_utc = datetime.utcnow()

        # Получаем активные конкурсы
        paid_contests = await get_contests_for_events(db, now_utc, contest_types=['referral_paid'])
        reg_contests = await get_contests_for_events(db, now_utc, contest_types=['referral_registered'])

        all_contests = list(paid_contests) + list(reg_contests)

        if not all_contests:
            await callback.message.edit_text(
                texts.t(
                    'ADMIN_REFERRALS_SYNC_NO_ACTIVE_CONTESTS',
                    '❌ <b>Нет активных конкурсов рефералов</b>\n\n'
                    'Создайте конкурс в разделе "Конкурсы" для синхронизации.',
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_REFERRALS_BUTTON_BACK_TO_DIAGNOSTICS', '⬅️ К диагностике'),
                                callback_data='admin_referral_diagnostics',
                            )
                        ]
                    ]
                ),
            )
            return

        # Синхронизируем каждый конкурс
        total_created = 0
        total_updated = 0
        total_skipped = 0
        contest_results = []

        for contest in all_contests:
            stats = await referral_contest_service.sync_contest(db, contest.id)
            if 'error' not in stats:
                total_created += stats.get('created', 0)
                total_updated += stats.get('updated', 0)
                total_skipped += stats.get('skipped', 0)
                contest_results.append(
                    texts.t('ADMIN_REFERRALS_SYNC_CONTEST_LINE_SUCCESS', '• {title}: +{created} новых').format(
                        title=contest.title, created=stats.get('created', 0)
                    )
                )
            else:
                contest_results.append(
                    texts.t('ADMIN_REFERRALS_SYNC_CONTEST_LINE_ERROR', '• {title}: ошибка').format(title=contest.title)
                )

        text = texts.t(
            'ADMIN_REFERRALS_SYNC_RESULT_TEXT',
            '🏆 <b>Синхронизация с конкурсами завершена!</b>\n\n'
            '📊 <b>Результат:</b>\n'
            '• Конкурсов обработано: {contests_count}\n'
            '• Новых событий добавлено: {created}\n'
            '• Обновлено: {updated}\n'
            '• Пропущено (уже есть): {skipped}\n\n'
            '📋 <b>По конкурсам:</b>',
        ).format(
            contests_count=len(all_contests),
            created=total_created,
            updated=total_updated,
            skipped=total_skipped,
        )
        text += '\n'.join(contest_results)

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_SYNC_BUTTON_RETRY', '🔄 Синхронизировать снова'),
                        callback_data='admin_ref_sync_contest',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_BUTTON_BACK_TO_DIAGNOSTICS', '⬅️ К диагностике'),
                        callback_data='admin_referral_diagnostics',
                    )
                ],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error('Ошибка в sync_referrals_with_contest', error=e, exc_info=True)
        await callback.answer(texts.t('ADMIN_REFERRALS_SYNC_ERROR', 'Ошибка при синхронизации'), show_alert=True)


@admin_required
@error_handler
async def request_log_file_upload(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Запрашивает загрузку лог-файла для анализа."""
    texts = get_texts(db_user.language)
    await state.set_state(AdminStates.waiting_for_log_file)

    text = texts.t(
        'ADMIN_REFERRALS_LOG_UPLOAD_PROMPT',
        '📤 <b>Загрузка лог-файла для анализа</b>\n\n'
        'Отправьте файл лога (расширение .log или .txt).\n\n'
        'Файл будет проанализирован на наличие потерянных рефералов за ВСЕ время, записанное в логе.\n\n'
        '⚠️ <b>Важно:</b>\n'
        '• Файл должен быть текстовым (.log, .txt)\n'
        '• Максимальный размер: 50 MB\n'
        '• После анализа файл будет автоматически удалён\n\n'
        'Если ротация логов удалила старые данные — загрузите резервную копию.',
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_referral_diagnostics')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def receive_log_file(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Получает и анализирует загруженный лог-файл."""
    import tempfile
    from pathlib import Path

    texts = get_texts(db_user.language)

    if not message.document:
        await message.answer(
            texts.t('ADMIN_REFERRALS_LOG_UPLOAD_SEND_AS_DOC', '❌ Пожалуйста, отправьте файл документом.'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_referral_diagnostics')]
                ]
            ),
        )
        return

    # Проверяем расширение файла
    file_name = message.document.file_name or 'unknown'
    file_ext = Path(file_name).suffix.lower()

    if file_ext not in ['.log', '.txt']:
        await message.answer(
            texts.t(
                'ADMIN_REFERRALS_LOG_UPLOAD_INVALID_EXT',
                '❌ Неверный формат файла: {ext}\n\nПоддерживаются только текстовые файлы (.log, .txt)',
            ).format(ext=file_ext),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_referral_diagnostics')]
                ]
            ),
        )
        return

    # Проверяем размер файла
    max_size = 50 * 1024 * 1024  # 50 MB
    if message.document.file_size > max_size:
        await message.answer(
            texts.t(
                'ADMIN_REFERRALS_LOG_UPLOAD_TOO_LARGE',
                '❌ Файл слишком большой: {size_mb:.1f} MB\n\nМаксимальный размер: 50 MB',
            ).format(size_mb=message.document.file_size / 1024 / 1024),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_referral_diagnostics')]
                ]
            ),
        )
        return

    # Информируем о начале загрузки
    status_message = await message.answer(
        texts.t(
            'ADMIN_REFERRALS_LOG_UPLOAD_DOWNLOADING',
            '📥 Загружаю файл {file_name} ({size_mb:.1f} MB)...',
        ).format(file_name=file_name, size_mb=message.document.file_size / 1024 / 1024)
    )

    temp_file_path = None

    try:
        # Скачиваем файл во временную директорию
        temp_dir = tempfile.gettempdir()
        temp_file_path = str(Path(temp_dir) / f'ref_diagnostics_{message.from_user.id}_{file_name}')

        # Скачиваем файл
        file = await message.bot.get_file(message.document.file_id)
        await message.bot.download_file(file.file_path, temp_file_path)

        logger.info('📥 Файл загружен: ( байт)', temp_file_path=temp_file_path, file_size=message.document.file_size)

        # Обновляем статус
        await status_message.edit_text(
            texts.t(
                'ADMIN_REFERRALS_LOG_UPLOAD_ANALYZING_STATUS',
                '🔍 Анализирую файл {file_name}...\n\nЭто может занять некоторое время.',
            ).format(file_name=file_name)
        )

        # Анализируем файл
        from app.services.referral_diagnostics_service import referral_diagnostics_service

        report = await referral_diagnostics_service.analyze_file(db, temp_file_path)

        # Формируем отчёт
        text = texts.t(
            'ADMIN_REFERRALS_LOG_ANALYSIS_TEXT',
            '🔍 <b>Анализ лог-файла: {file_name}</b>\n\n'
            '<b>📊 Статистика переходов:</b>\n'
            '• Всего кликов по реф-ссылкам: {total_ref_clicks}\n'
            '• Уникальных пользователей: {unique_users_clicked}\n'
            '• Потерянных рефералов: {lost_count}\n'
            '• Строк в файле: {lines_in_file}',
        ).format(
            file_name=file_name,
            total_ref_clicks=report.total_ref_clicks,
            unique_users_clicked=report.unique_users_clicked,
            lost_count=len(report.lost_referrals),
            lines_in_file=report.lines_in_period,
        )

        if report.lost_referrals:
            text += '\n' + texts.t('ADMIN_REFERRALS_DIAG_LOST_HEADER', '<b>❌ Потерянные рефералы:</b>') + '\n'
            text += texts.t(
                'ADMIN_REFERRALS_DIAG_LOST_HINT',
                '<i>(пришли по ссылке, но реферер не засчитался)</i>',
            ) + '\n\n'

            for i, lost in enumerate(report.lost_referrals[:15], 1):
                # Статус пользователя
                if not lost.registered:
                    status = texts.t('ADMIN_REFERRALS_DIAG_STATUS_NOT_IN_DB', '⚠️ Не в БД')
                elif not lost.has_referrer:
                    status = texts.t('ADMIN_REFERRALS_DIAG_STATUS_NO_REFERRER', '❌ Без реферера')
                else:
                    status = texts.t(
                        'ADMIN_REFERRALS_DIAG_STATUS_OTHER_REFERRER',
                        '⚡ Другой реферер (ID{referrer_id})',
                    ).format(referrer_id=lost.current_referrer_id)

                # Имя или ID
                user_name = lost.username or lost.full_name or f'ID{lost.telegram_id}'
                if lost.username:
                    user_name = f'@{lost.username}'

                # Ожидаемый реферер
                referrer_info = ''
                if lost.expected_referrer_name:
                    referrer_info = f' → {lost.expected_referrer_name}'
                elif lost.expected_referrer_id:
                    referrer_info = f' → ID{lost.expected_referrer_id}'

                # Время
                time_str = lost.click_time.strftime('%d.%m.%Y %H:%M')

                text += f'{i}. {user_name} — {status}\n'
                text += f'   <code>{lost.referral_code}</code>{referrer_info} ({time_str})\n'

            if len(report.lost_referrals) > 15:
                text += (
                    '\n'
                    + texts.t('ADMIN_REFERRALS_DIAG_AND_MORE', '<i>... и ещё {count}</i>').format(
                        count=len(report.lost_referrals) - 15
                    )
                    + '\n'
                )
        else:
            text += '\n' + texts.t('ADMIN_REFERRALS_DIAG_ALL_ACCOUNTED', '✅ <b>Все рефералы засчитаны!</b>') + '\n'

        # Сохраняем отчёт в state для дальнейшего использования (сериализуем в dict)
        await state.update_data(
            diagnostics_period='uploaded_file',
            uploaded_file_report=report.to_dict(),
        )

        # Кнопки действий
        keyboard_rows = []

        if report.lost_referrals:
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_DIAG_BUTTON_PREVIEW_FIXES', '📋 Предпросмотр исправлений'),
                        callback_data='admin_ref_fix_preview',
                    )
                ]
            )

        keyboard_rows.extend(
            [
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_BUTTON_BACK_TO_DIAGNOSTICS', '⬅️ К диагностике'),
                        callback_data='admin_referral_diagnostics',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_REFERRALS_TOP_BACK_TO_STATS', '⬅️ К статистике'),
                        callback_data='admin_referrals',
                    )
                ],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        # Удаляем статусное сообщение
        await status_message.delete()

        # Отправляем результат
        await message.answer(text, reply_markup=keyboard)

        # Очищаем состояние
        await state.set_state(AdminStates.referral_diagnostics_period)

    except Exception as e:
        logger.error('❌ Ошибка при обработке файла', error=e, exc_info=True)

        try:
            await status_message.edit_text(
                texts.t(
                    'ADMIN_REFERRALS_LOG_ANALYSIS_ERROR_STATUS',
                    '❌ <b>Ошибка при анализе файла</b>\n\n'
                    'Файл: {file_name}\n'
                    'Ошибка: {error}\n\n'
                    'Проверьте, что файл является текстовым логом бота.',
                ).format(file_name=file_name, error=e),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_REFERRALS_LOG_ANALYSIS_RETRY', '🔄 Попробовать снова'),
                                callback_data='admin_ref_diag_upload',
                            )
                        ],
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('ADMIN_REFERRALS_BUTTON_BACK_TO_DIAGNOSTICS', '⬅️ К диагностике'),
                                callback_data='admin_referral_diagnostics',
                            )
                        ],
                    ]
                ),
            )
        except:
            await message.answer(
                texts.t('ADMIN_REFERRALS_LOG_ANALYSIS_ERROR_MSG', '❌ Ошибка при анализе файла: {error}').format(
                    error=e
                ),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_referral_diagnostics')]
                    ]
                ),
            )

    finally:
        # Удаляем временный файл
        if temp_file_path and Path(temp_file_path).exists():
            try:
                Path(temp_file_path).unlink()
                logger.info('🗑️ Временный файл удалён', temp_file_path=temp_file_path)
            except Exception as e:
                logger.error('Ошибка удаления временного файла', error=e)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_referral_statistics, F.data == 'admin_referrals')
    dp.callback_query.register(show_top_referrers, F.data == 'admin_referrals_top')
    dp.callback_query.register(show_top_referrers_filtered, F.data.startswith('admin_top_ref:'))
    dp.callback_query.register(show_referral_settings, F.data == 'admin_referrals_settings')
    dp.callback_query.register(show_referral_diagnostics, F.data == 'admin_referral_diagnostics')
    dp.callback_query.register(show_referral_diagnostics, F.data.startswith('admin_ref_diag:'))
    dp.callback_query.register(preview_referral_fixes, F.data == 'admin_ref_fix_preview')
    dp.callback_query.register(apply_referral_fixes, F.data == 'admin_ref_fix_apply')

    # Загрузка лог-файла
    dp.callback_query.register(request_log_file_upload, F.data == 'admin_ref_diag_upload')
    dp.message.register(receive_log_file, AdminStates.waiting_for_log_file)

    # Проверка бонусов по БД
    dp.callback_query.register(check_missing_bonuses, F.data == 'admin_ref_check_bonuses')
    dp.callback_query.register(apply_missing_bonuses, F.data == 'admin_ref_bonus_apply')
    dp.callback_query.register(sync_referrals_with_contest, F.data == 'admin_ref_sync_contest')

    # Хендлеры заявок на вывод
    dp.callback_query.register(show_pending_withdrawal_requests, F.data == 'admin_withdrawal_requests')
    dp.callback_query.register(view_withdrawal_request, F.data.startswith('admin_withdrawal_view_'))
    dp.callback_query.register(approve_withdrawal_request, F.data.startswith('admin_withdrawal_approve_'))
    dp.callback_query.register(reject_withdrawal_request, F.data.startswith('admin_withdrawal_reject_'))
    dp.callback_query.register(complete_withdrawal_request, F.data.startswith('admin_withdrawal_complete_'))

    # Тестовое начисление
    dp.callback_query.register(start_test_referral_earning, F.data == 'admin_test_referral_earning')
    dp.message.register(process_test_referral_earning, AdminStates.test_referral_earning_input)
