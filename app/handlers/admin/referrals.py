import datetime
import json
import logging

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


logger = logging.getLogger(__name__)


@admin_required
@error_handler
async def show_referral_statistics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        stats = await get_referral_statistics(db)

        avg_per_referrer = 0
        if stats.get('active_referrers', 0) > 0:
            avg_per_referrer = stats.get('total_paid_kopeks', 0) / stats['active_referrers']

        current_time = datetime.datetime.now().strftime('%H:%M:%S')

        text = f"""
🤝 <b>Реферальная статистика</b>

<b>Общие показатели:</b>
- Пользователей с рефералами: {stats.get('users_with_referrals', 0)}
- Активных рефереров: {stats.get('active_referrers', 0)}
- Выплачено всего: {settings.format_price(stats.get('total_paid_kopeks', 0))}

<b>За период:</b>
- Сегодня: {settings.format_price(stats.get('today_earnings_kopeks', 0))}
- За неделю: {settings.format_price(stats.get('week_earnings_kopeks', 0))}
- За месяц: {settings.format_price(stats.get('month_earnings_kopeks', 0))}

<b>Средние показатели:</b>
- На одного реферера: {settings.format_price(int(avg_per_referrer))}

<b>Топ-5 рефереров:</b>
"""

        top_referrers = stats.get('top_referrers', [])
        if top_referrers:
            for i, referrer in enumerate(top_referrers[:5], 1):
                earned = referrer.get('total_earned_kopeks', 0)
                count = referrer.get('referrals_count', 0)
                user_id = referrer.get('user_id', 'N/A')

                if count > 0:
                    text += f'{i}. ID {user_id}: {settings.format_price(earned)} ({count} реф.)\n'
                else:
                    logger.warning(f'Реферер {user_id} имеет {count} рефералов, но есть в топе')
        else:
            text += 'Нет данных\n'

        text += f"""

<b>Настройки реферальной системы:</b>
- Минимальное пополнение: {settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)}
- Бонус за первое пополнение: {settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}
- Бонус пригласившему: {settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)}
- Комиссия с покупок: {settings.REFERRAL_COMMISSION_PERCENT}%
- Уведомления: {'✅ Включены' if settings.REFERRAL_NOTIFICATIONS_ENABLED else '❌ Отключены'}

<i>🕐 Обновлено: {current_time}</i>
"""

        keyboard_rows = [
            [types.InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_referrals')],
            [types.InlineKeyboardButton(text='👥 Топ рефереров', callback_data='admin_referrals_top')],
            [types.InlineKeyboardButton(text='🔍 Диагностика логов', callback_data='admin_referral_diagnostics')],
        ]

        # Кнопка заявок на вывод (если функция включена)
        if settings.is_referral_withdrawal_enabled():
            keyboard_rows.append(
                [types.InlineKeyboardButton(text='💸 Заявки на вывод', callback_data='admin_withdrawal_requests')]
            )

        keyboard_rows.extend(
            [
                [types.InlineKeyboardButton(text='⚙️ Настройки', callback_data='admin_referrals_settings')],
                [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_panel')],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer('Обновлено')
        except Exception as edit_error:
            if 'message is not modified' in str(edit_error):
                await callback.answer('Данные актуальны')
            else:
                logger.error(f'Ошибка редактирования сообщения: {edit_error}')
                await callback.answer('Ошибка обновления')

    except Exception as e:
        logger.error(f'Ошибка в show_referral_statistics: {e}', exc_info=True)

        current_time = datetime.datetime.now().strftime('%H:%M:%S')
        text = f"""
🤝 <b>Реферальная статистика</b>

❌ <b>Ошибка загрузки данных</b>

<b>Текущие настройки:</b>
- Минимальное пополнение: {settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)}
- Бонус за первое пополнение: {settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}
- Бонус пригласившему: {settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)}
- Комиссия с покупок: {settings.REFERRAL_COMMISSION_PERCENT}%

<i>🕐 Время: {current_time}</i>
"""

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 Повторить', callback_data='admin_referrals')],
                [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_panel')],
            ]
        )

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except:
            pass
        await callback.answer('Произошла ошибка при загрузке статистики')


def _get_top_keyboard(period: str, sort_by: str) -> types.InlineKeyboardMarkup:
    """Создаёт клавиатуру для выбора периода и сортировки."""
    period_week = '✅ Неделя' if period == 'week' else 'Неделя'
    period_month = '✅ Месяц' if period == 'month' else 'Месяц'
    sort_earnings = '✅ По заработку' if sort_by == 'earnings' else 'По заработку'
    sort_invited = '✅ По приглашённым' if sort_by == 'invited' else 'По приглашённым'

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
            [types.InlineKeyboardButton(text='🔄 Обновить', callback_data=f'admin_top_ref:{period}:{sort_by}')],
            [types.InlineKeyboardButton(text='⬅️ К статистике', callback_data='admin_referrals')],
        ]
    )


@admin_required
@error_handler
async def show_top_referrers(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает топ рефереров (по умолчанию: неделя, по заработку)."""
    await _show_top_referrers_filtered(callback, db, period='week', sort_by='earnings')


@admin_required
@error_handler
async def show_top_referrers_filtered(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Обрабатывает выбор периода и сортировки."""
    # Парсим callback_data: admin_top_ref:period:sort_by
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer('Ошибка параметров')
        return

    period = parts[1]  # week или month
    sort_by = parts[2]  # earnings или invited

    if period not in ('week', 'month'):
        period = 'week'
    if sort_by not in ('earnings', 'invited'):
        sort_by = 'earnings'

    await _show_top_referrers_filtered(callback, db, period, sort_by)


async def _show_top_referrers_filtered(callback: types.CallbackQuery, db: AsyncSession, period: str, sort_by: str):
    """Внутренняя функция отображения топа с фильтрами."""
    try:
        top_referrers = await get_top_referrers_by_period(db, period=period, sort_by=sort_by)

        period_text = 'за неделю' if period == 'week' else 'за месяц'
        sort_text = 'по заработку' if sort_by == 'earnings' else 'по приглашённым'

        text = f'🏆 <b>Топ рефереров {period_text}</b>\n'
        text += f'<i>Сортировка: {sort_text}</i>\n\n'

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
                    text += f'   👥 <b>{count} приглашённых</b> | 💰 {settings.format_price(earned)}\n\n'
                else:
                    text += f'{emoji}{i}. {display_text}\n'
                    text += f'   💰 <b>{settings.format_price(earned)}</b> | 👥 {count} приглашённых\n\n'
        else:
            text += 'Нет данных за выбранный период\n'

        keyboard = _get_top_keyboard(period, sort_by)

        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer()
        except Exception as edit_error:
            if 'message is not modified' in str(edit_error):
                await callback.answer('Данные актуальны')
            else:
                raise

    except Exception as e:
        logger.error(f'Ошибка в show_top_referrers_filtered: {e}', exc_info=True)
        await callback.answer('Ошибка загрузки топа рефереров')


@admin_required
@error_handler
async def show_referral_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    text = f"""
⚙️ <b>Настройки реферальной системы</b>

<b>Бонусы и награды:</b>
• Минимальная сумма пополнения для участия: {settings.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS)}
• Бонус за первое пополнение реферала: {settings.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}
• Бонус пригласившему за первое пополнение: {settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)}

<b>Комиссионные:</b>
• Процент с каждой покупки реферала: {settings.REFERRAL_COMMISSION_PERCENT}%

<b>Уведомления:</b>
• Статус: {'✅ Включены' if settings.REFERRAL_NOTIFICATIONS_ENABLED else '❌ Отключены'}
• Попытки отправки: {getattr(settings, 'REFERRAL_NOTIFICATION_RETRY_ATTEMPTS', 3)}

<i>💡 Для изменения настроек отредактируйте файл .env и перезапустите бота</i>
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ К статистике', callback_data='admin_referrals')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_pending_withdrawal_requests(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает список ожидающих заявок на вывод."""
    requests = await referral_withdrawal_service.get_pending_requests(db)

    if not requests:
        text = '📋 <b>Заявки на вывод</b>\n\nНет ожидающих заявок.'

        keyboard_rows = []
        # Кнопка тестового начисления (только в тестовом режиме)
        if settings.REFERRAL_WITHDRAWAL_TEST_MODE:
            keyboard_rows.append(
                [types.InlineKeyboardButton(text='🧪 Тестовое начисление', callback_data='admin_test_referral_earning')]
            )
        keyboard_rows.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_referrals')])

        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
        await callback.answer()
        return

    text = f'📋 <b>Заявки на вывод ({len(requests)})</b>\n\n'

    for req in requests[:10]:
        user = await get_user_by_id(db, req.user_id)
        user_name = user.full_name if user else 'Неизвестно'
        user_tg_id = user.telegram_id if user else 'N/A'

        risk_emoji = (
            '🟢' if req.risk_score < 30 else '🟡' if req.risk_score < 50 else '🟠' if req.risk_score < 70 else '🔴'
        )

        text += f'<b>#{req.id}</b> — {user_name} (ID{user_tg_id})\n'
        text += f'💰 {req.amount_kopeks / 100:.0f}₽ | {risk_emoji} Риск: {req.risk_score}/100\n'
        text += f'📅 {req.created_at.strftime("%d.%m.%Y %H:%M")}\n\n'

    keyboard_rows = []
    for req in requests[:5]:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'#{req.id} — {req.amount_kopeks / 100:.0f}₽', callback_data=f'admin_withdrawal_view_{req.id}'
                )
            ]
        )

    # Кнопка тестового начисления (только в тестовом режиме)
    if settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        keyboard_rows.append(
            [types.InlineKeyboardButton(text='🧪 Тестовое начисление', callback_data='admin_test_referral_earning')]
        )

    keyboard_rows.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_referrals')])

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
    await callback.answer()


@admin_required
@error_handler
async def view_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает детали заявки на вывод."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer('Заявка не найдена', show_alert=True)
        return

    user = await get_user_by_id(db, request.user_id)
    user_name = user.full_name if user else 'Неизвестно'
    user_tg_id = (user.telegram_id or user.email or f'#{user.id}') if user else 'N/A'

    analysis = json.loads(request.risk_analysis) if request.risk_analysis else {}

    status_text = {
        WithdrawalRequestStatus.PENDING.value: '⏳ Ожидает',
        WithdrawalRequestStatus.APPROVED.value: '✅ Одобрена',
        WithdrawalRequestStatus.REJECTED.value: '❌ Отклонена',
        WithdrawalRequestStatus.COMPLETED.value: '✅ Выполнена',
        WithdrawalRequestStatus.CANCELLED.value: '🚫 Отменена',
    }.get(request.status, request.status)

    text = f"""
📋 <b>Заявка #{request.id}</b>

👤 Пользователь: {user_name}
🆔 ID: <code>{user_tg_id}</code>
💰 Сумма: <b>{request.amount_kopeks / 100:.0f}₽</b>
📊 Статус: {status_text}

💳 <b>Реквизиты:</b>
<code>{request.payment_details}</code>

📅 Создана: {request.created_at.strftime('%d.%m.%Y %H:%M')}

{referral_withdrawal_service.format_analysis_for_admin(analysis)}
"""

    keyboard = []

    if request.status == WithdrawalRequestStatus.PENDING.value:
        keyboard.append(
            [
                types.InlineKeyboardButton(text='✅ Одобрить', callback_data=f'admin_withdrawal_approve_{request.id}'),
                types.InlineKeyboardButton(text='❌ Отклонить', callback_data=f'admin_withdrawal_reject_{request.id}'),
            ]
        )

    if request.status == WithdrawalRequestStatus.APPROVED.value:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text='✅ Деньги переведены', callback_data=f'admin_withdrawal_complete_{request.id}'
                )
            ]
        )

    if user:
        keyboard.append(
            [types.InlineKeyboardButton(text='👤 Профиль пользователя', callback_data=f'admin_user_manage_{user.id}')]
        )
    keyboard.append([types.InlineKeyboardButton(text='⬅️ К списку', callback_data='admin_withdrawal_requests')])

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def approve_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Одобряет заявку на вывод."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer('Заявка не найдена', show_alert=True)
        return

    success, error = await referral_withdrawal_service.approve_request(db, request_id, db_user.id)

    if success:
        # Уведомляем пользователя (только если есть telegram_id)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    texts.t(
                        'REFERRAL_WITHDRAWAL_APPROVED',
                        '✅ <b>Заявка на вывод #{id} одобрена!</b>\n\n'
                        'Сумма: <b>{amount}</b>\n'
                        'Средства списаны с баланса.\n\n'
                        'Ожидайте перевод на указанные реквизиты.',
                    ).format(id=request.id, amount=texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error(f'Ошибка отправки уведомления пользователю: {e}')

        await callback.answer('✅ Заявка одобрена, средства списаны с баланса')

        # Обновляем отображение
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer(f'❌ {error}', show_alert=True)


@admin_required
@error_handler
async def reject_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Отклоняет заявку на вывод."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer('Заявка не найдена', show_alert=True)
        return

    success = await referral_withdrawal_service.reject_request(db, request_id, db_user.id, 'Отклонено администратором')

    if success:
        # Уведомляем пользователя (только если есть telegram_id)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    texts.t(
                        'REFERRAL_WITHDRAWAL_REJECTED',
                        '❌ <b>Заявка на вывод #{id} отклонена</b>\n\n'
                        'Сумма: <b>{amount}</b>\n\n'
                        'Если у вас есть вопросы, обратитесь в поддержку.',
                    ).format(id=request.id, amount=texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error(f'Ошибка отправки уведомления пользователю: {e}')

        await callback.answer('❌ Заявка отклонена')

        # Обновляем отображение
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer('❌ Ошибка отклонения', show_alert=True)


@admin_required
@error_handler
async def complete_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Отмечает заявку как выполненную (деньги переведены)."""
    request_id = int(callback.data.split('_')[-1])

    result = await db.execute(select(WithdrawalRequest).where(WithdrawalRequest.id == request_id))
    request = result.scalar_one_or_none()

    if not request:
        await callback.answer('Заявка не найдена', show_alert=True)
        return

    success = await referral_withdrawal_service.complete_request(db, request_id, db_user.id, 'Перевод выполнен')

    if success:
        # Уведомляем пользователя (только если есть telegram_id)
        user = await get_user_by_id(db, request.user_id)
        if user and user.telegram_id:
            try:
                texts = get_texts(user.language)
                await callback.bot.send_message(
                    user.telegram_id,
                    texts.t(
                        'REFERRAL_WITHDRAWAL_COMPLETED',
                        '💸 <b>Выплата по заявке #{id} выполнена!</b>\n\n'
                        'Сумма: <b>{amount}</b>\n\n'
                        'Деньги отправлены на указанные реквизиты.',
                    ).format(id=request.id, amount=texts.format_price(request.amount_kopeks)),
                )
            except Exception as e:
                logger.error(f'Ошибка отправки уведомления пользователю: {e}')

        await callback.answer('✅ Заявка выполнена')

        # Обновляем отображение
        await view_withdrawal_request(callback, db_user, db)
    else:
        await callback.answer('❌ Ошибка выполнения', show_alert=True)


@admin_required
@error_handler
async def start_test_referral_earning(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Начинает процесс тестового начисления реферального дохода."""
    if not settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        await callback.answer('Тестовый режим отключён', show_alert=True)
        return

    await state.set_state(AdminStates.test_referral_earning_input)

    text = """
🧪 <b>Тестовое начисление реферального дохода</b>

Введите данные в формате:
<code>telegram_id сумма_в_рублях</code>

Примеры:
• <code>123456789 500</code> — начислит 500₽ пользователю 123456789
• <code>987654321 1000</code> — начислит 1000₽ пользователю 987654321

⚠️ Это создаст реальную запись ReferralEarning, как будто пользователь заработал с реферала.
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_withdrawal_requests')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def process_test_referral_earning(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Обрабатывает ввод тестового начисления."""
    if not settings.REFERRAL_WITHDRAWAL_TEST_MODE:
        await message.answer('❌ Тестовый режим отключён')
        await state.clear()
        return

    text_input = message.text.strip()
    parts = text_input.split()

    if len(parts) != 2:
        await message.answer(
            '❌ Неверный формат. Введите: <code>telegram_id сумма</code>\n\nНапример: <code>123456789 500</code>'
        )
        return

    try:
        target_telegram_id = int(parts[0])
        amount_rubles = float(parts[1].replace(',', '.'))
        amount_kopeks = int(amount_rubles * 100)

        if amount_kopeks <= 0:
            await message.answer('❌ Сумма должна быть положительной')
            return

        if amount_kopeks > 10000000:  # Лимит 100 000₽
            await message.answer('❌ Максимальная сумма тестового начисления: 100 000₽')
            return

    except ValueError:
        await message.answer(
            '❌ Неверный формат чисел. Введите: <code>telegram_id сумма</code>\n\nНапример: <code>123456789 500</code>'
        )
        return

    # Ищем целевого пользователя
    target_user = await get_user_by_telegram_id(db, target_telegram_id)
    if not target_user:
        await message.answer(f'❌ Пользователь с ID {target_telegram_id} не найден в базе')
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
        f'✅ <b>Тестовое начисление создано!</b>\n\n'
        f'👤 Пользователь: {target_user.full_name or "Без имени"}\n'
        f'🆔 ID: <code>{target_telegram_id}</code>\n'
        f'💰 Сумма: <b>{amount_rubles:.0f}₽</b>\n'
        f'💳 Новый баланс: <b>{target_user.balance_kopeks / 100:.0f}₽</b>\n\n'
        f'Начисление добавлено как реферальный доход.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='📋 К заявкам', callback_data='admin_withdrawal_requests')],
                [types.InlineKeyboardButton(text='👤 Профиль', callback_data=f'admin_user_manage_{target_user.id}')],
            ]
        ),
    )

    logger.info(
        f'Тестовое начисление: админ {db_user.telegram_id} начислил {amount_rubles}₽ пользователю {target_telegram_id}'
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


def _get_period_display_name(period: str) -> str:
    """Возвращает человекочитаемое название периода."""
    names = {'today': 'сегодня', 'yesterday': 'вчера', 'week': '7 дней', 'month': '30 дней'}
    return names.get(period, 'сегодня')


async def _show_diagnostics_for_period(
    callback: types.CallbackQuery, db: AsyncSession, state: FSMContext, period: str
):
    """Внутренняя функция для отображения диагностики за указанный период."""
    try:
        await callback.answer('Анализирую логи...')

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
        period_display = _get_period_display_name(period)

        text = f"""
🔍 <b>Диагностика рефералов — {period_display}</b>

<b>📊 Статистика переходов:</b>
• Всего кликов по реф-ссылкам: {report.total_ref_clicks}
• Уникальных пользователей: {report.unique_users_clicked}
• Потерянных рефералов: {len(report.lost_referrals)}
"""

        if report.lost_referrals:
            text += '\n<b>❌ Потерянные рефералы:</b>\n'
            text += '<i>(пришли по ссылке, но реферер не засчитался)</i>\n\n'

            for i, lost in enumerate(report.lost_referrals[:15], 1):
                # Статус пользователя
                if not lost.registered:
                    status = '⚠️ Не в БД'
                elif not lost.has_referrer:
                    status = '❌ Без реферера'
                else:
                    status = f'⚡ Другой реферер (ID{lost.current_referrer_id})'

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
                text += f'\n<i>... и ещё {len(report.lost_referrals) - 15}</i>\n'
        else:
            text += '\n✅ <b>Все рефералы засчитаны!</b>\n'

        # Информация о логах
        log_path = referral_diagnostics_service.log_path
        log_exists = log_path.exists()
        log_size = log_path.stat().st_size if log_exists else 0

        text += f'\n<i>📂 {log_path.name}'
        if log_exists:
            text += f' ({log_size / 1024:.0f} KB)'
            text += f' | Строк: {report.lines_in_period}'
        else:
            text += ' (не найден!)'
        text += '</i>'

        # Кнопки: только "Сегодня" (текущий лог) и "Загрузить файл" (старые логи)
        keyboard_rows = [
            [
                types.InlineKeyboardButton(
                    text='📅 Сегодня (текущий лог)', callback_data='admin_ref_diag:today'
                ),
            ],
            [
                types.InlineKeyboardButton(text='📤 Загрузить лог-файл', callback_data='admin_ref_diag_upload')
            ],
            [
                types.InlineKeyboardButton(text='🔍 Проверить бонусы (по БД)', callback_data='admin_ref_check_bonuses')
            ],
            [
                types.InlineKeyboardButton(text='🏆 Синхронизировать с конкурсом', callback_data='admin_ref_sync_contest')
            ],
        ]

        # Кнопки действий (только если есть потерянные рефералы)
        if report.lost_referrals:
            keyboard_rows.append(
                [types.InlineKeyboardButton(text='📋 Предпросмотр исправлений', callback_data='admin_ref_fix_preview')]
            )

        keyboard_rows.extend(
            [
                [types.InlineKeyboardButton(text='🔄 Обновить', callback_data=f'admin_ref_diag:{period}')],
                [types.InlineKeyboardButton(text='⬅️ К статистике', callback_data='admin_referrals')],
            ]
        )

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка в _show_diagnostics_for_period: {e}', exc_info=True)
        await callback.answer('Ошибка при анализе логов', show_alert=True)


@admin_required
@error_handler
async def show_referral_diagnostics(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Показывает диагностику реферальной системы по логам."""
    # Определяем период из callback_data или используем "today" по умолчанию
    if ':' in callback.data:
        period = callback.data.split(':')[1]
    else:
        period = 'today'

    await _show_diagnostics_for_period(callback, db, state, period)


@admin_required
@error_handler
async def preview_referral_fixes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Показывает предпросмотр исправлений потерянных рефералов."""
    try:
        await callback.answer('Анализирую...')

        # Получаем период из state
        state_data = await state.get_data()
        period = state_data.get('diagnostics_period', 'today')

        from app.services.referral_diagnostics_service import DiagnosticReport, referral_diagnostics_service

        # Проверяем, работаем ли с загруженным файлом
        if period == 'uploaded_file':
            # Используем сохранённый отчёт из загруженного файла (десериализуем)
            report_data = state_data.get('uploaded_file_report')
            if not report_data:
                await callback.answer('Отчёт загруженного файла не найден', show_alert=True)
                return
            report = DiagnosticReport.from_dict(report_data)
            period_display = 'загруженный файл'
        else:
            # Получаем даты периода
            start_date, end_date = _get_period_dates(period)

            # Анализируем логи
            report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)
            period_display = _get_period_display_name(period)

        if not report.lost_referrals:
            await callback.answer('Нет потерянных рефералов для исправления', show_alert=True)
            return

        # Запускаем предпросмотр исправлений
        fix_report = await referral_diagnostics_service.fix_lost_referrals(db, report.lost_referrals, apply=False)

        # Формируем отчёт
        text = f"""
📋 <b>Предпросмотр исправлений — {period_display}</b>

<b>📊 Что будет сделано:</b>
• Исправлено рефералов: {fix_report.users_fixed}
• Бонусов рефералам: {settings.format_price(fix_report.bonuses_to_referrals)}
• Бонусов рефереам: {settings.format_price(fix_report.bonuses_to_referrers)}
• Ошибок: {fix_report.errors}

<b>🔍 Детали:</b>
"""

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
                    text += f'   • Реферер: {detail.referrer_name or f"ID{detail.referrer_id}"}\n'
                if detail.had_first_topup:
                    text += f'   • Первое пополнение: {settings.format_price(detail.topup_amount_kopeks)}\n'
                if detail.bonus_to_referral_kopeks > 0:
                    text += f'   • Бонус рефералу: {settings.format_price(detail.bonus_to_referral_kopeks)}\n'
                if detail.bonus_to_referrer_kopeks > 0:
                    text += f'   • Бонус рефереру: {settings.format_price(detail.bonus_to_referrer_kopeks)}\n'

        if len(fix_report.details) > 10:
            text += f'\n<i>... и ещё {len(fix_report.details) - 10}</i>\n'

        text += '\n⚠️ <b>Внимание!</b> Это только предпросмотр. Нажмите "Применить", чтобы выполнить исправления.'

        # Кнопка назад зависит от источника
        back_button_text = '⬅️ К диагностике'
        back_button_callback = f'admin_ref_diag:{period}' if period != 'uploaded_file' else 'admin_referral_diagnostics'

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='✅ Применить исправления', callback_data='admin_ref_fix_apply')],
                [types.InlineKeyboardButton(text=back_button_text, callback_data=back_button_callback)],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка в preview_referral_fixes: {e}', exc_info=True)
        await callback.answer('Ошибка при создании предпросмотра', show_alert=True)


@admin_required
@error_handler
async def apply_referral_fixes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Применяет исправления потерянных рефералов."""
    try:
        await callback.answer('Применяю исправления...')

        # Получаем период из state
        state_data = await state.get_data()
        period = state_data.get('diagnostics_period', 'today')

        from app.services.referral_diagnostics_service import DiagnosticReport, referral_diagnostics_service

        # Проверяем, работаем ли с загруженным файлом
        if period == 'uploaded_file':
            # Используем сохранённый отчёт из загруженного файла (десериализуем)
            report_data = state_data.get('uploaded_file_report')
            if not report_data:
                await callback.answer('Отчёт загруженного файла не найден', show_alert=True)
                return
            report = DiagnosticReport.from_dict(report_data)
            period_display = 'загруженный файл'
        else:
            # Получаем даты периода
            start_date, end_date = _get_period_dates(period)

            # Анализируем логи
            report = await referral_diagnostics_service.analyze_period(db, start_date, end_date)
            period_display = _get_period_display_name(period)

        if not report.lost_referrals:
            await callback.answer('Нет потерянных рефералов для исправления', show_alert=True)
            return

        # Применяем исправления
        fix_report = await referral_diagnostics_service.fix_lost_referrals(db, report.lost_referrals, apply=True)

        # Формируем отчёт
        text = f"""
✅ <b>Исправления применены — {period_display}</b>

<b>📊 Результаты:</b>
• Исправлено рефералов: {fix_report.users_fixed}
• Бонусов рефералам: {settings.format_price(fix_report.bonuses_to_referrals)}
• Бонусов рефереам: {settings.format_price(fix_report.bonuses_to_referrers)}
• Ошибок: {fix_report.errors}

<b>🔍 Детали:</b>
"""

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
                    text += f'   • Реферер: {detail.referrer_name or f"ID{detail.referrer_id}"}\n'
                if detail.bonus_to_referral_kopeks > 0:
                    text += f'   • Бонус рефералу: {settings.format_price(detail.bonus_to_referral_kopeks)}\n'
                if detail.bonus_to_referrer_kopeks > 0:
                    text += f'   • Бонус рефереру: {settings.format_price(detail.bonus_to_referrer_kopeks)}\n'

        if fix_report.users_fixed > 10:
            text += f'\n<i>... и ещё {fix_report.users_fixed - 10} исправлений</i>\n'

        # Показываем ошибки
        if fix_report.errors > 0:
            text += '\n<b>❌ Ошибки:</b>\n'
            error_count = 0
            for detail in fix_report.details:
                if detail.error and error_count < 5:
                    error_count += 1
                    user_name = detail.username or detail.full_name or f'ID{detail.telegram_id}'
                    text += f'• {user_name}: {detail.error}\n'
            if fix_report.errors > 5:
                text += f'<i>... и ещё {fix_report.errors - 5} ошибок</i>\n'

        # Кнопки зависят от источника
        keyboard_rows = []
        if period != 'uploaded_file':
            keyboard_rows.append(
                [types.InlineKeyboardButton(text='🔄 Обновить диагностику', callback_data=f'admin_ref_diag:{period}')]
            )
        keyboard_rows.append([types.InlineKeyboardButton(text='⬅️ К статистике', callback_data='admin_referrals')])

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.message.edit_text(text, reply_markup=keyboard)

        # Очищаем сохранённый отчёт из state
        if period == 'uploaded_file':
            await state.update_data(uploaded_file_report=None)

    except Exception as e:
        logger.error(f'Ошибка в apply_referral_fixes: {e}', exc_info=True)
        await callback.answer('Ошибка при применении исправлений', show_alert=True)


# =============================================================================
# Проверка бонусов по БД
# =============================================================================


@admin_required
@error_handler
async def check_missing_bonuses(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Проверяет по БД — всем ли рефералам начислены бонусы."""
    from app.services.referral_diagnostics_service import (
        referral_diagnostics_service,
    )

    await callback.answer('🔍 Проверяю бонусы...')

    try:
        report = await referral_diagnostics_service.check_missing_bonuses(db)

        # Сохраняем отчёт в state для последующего применения
        await state.update_data(missing_bonuses_report=report.to_dict())

        text = f"""
🔍 <b>Проверка бонусов по БД</b>

📊 <b>Статистика:</b>
• Всего рефералов: {report.total_referrals_checked}
• С пополнением ≥ минимума: {report.referrals_with_topup}
• <b>Без бонусов: {len(report.missing_bonuses)}</b>
"""

        if report.missing_bonuses:
            text += f"""
💰 <b>Требуется начислить:</b>
• Рефералам: {report.total_missing_to_referrals / 100:.0f}₽
• Рефереерам: {report.total_missing_to_referrers / 100:.0f}₽
• <b>Итого: {(report.total_missing_to_referrals + report.total_missing_to_referrers) / 100:.0f}₽</b>

👤 <b>Список ({len(report.missing_bonuses)} чел.):</b>
"""
            for i, mb in enumerate(report.missing_bonuses[:15], 1):
                referral_name = mb.referral_full_name or mb.referral_username or str(mb.referral_telegram_id)
                referrer_name = mb.referrer_full_name or mb.referrer_username or str(mb.referrer_telegram_id)
                text += f'\n{i}. <b>{referral_name}</b>'
                text += f'\n   └ Пригласил: {referrer_name}'
                text += f'\n   └ Пополнение: {mb.first_topup_amount_kopeks / 100:.0f}₽'
                text += f'\n   └ Бонусы: {mb.referral_bonus_amount / 100:.0f}₽ + {mb.referrer_bonus_amount / 100:.0f}₽'

            if len(report.missing_bonuses) > 15:
                text += f'\n\n<i>... и ещё {len(report.missing_bonuses) - 15} чел.</i>'

            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='✅ Начислить все бонусы', callback_data='admin_ref_bonus_apply')],
                    [types.InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_ref_check_bonuses')],
                    [types.InlineKeyboardButton(text='⬅️ К диагностике', callback_data='admin_referral_diagnostics')],
                ]
            )
        else:
            text += '\n✅ <b>Все бонусы начислены!</b>'
            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 Обновить', callback_data='admin_ref_check_bonuses')],
                    [types.InlineKeyboardButton(text='⬅️ К диагностике', callback_data='admin_referral_diagnostics')],
                ]
            )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка в check_missing_bonuses: {e}', exc_info=True)
        await callback.answer('Ошибка при проверке бонусов', show_alert=True)


@admin_required
@error_handler
async def apply_missing_bonuses(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Применяет начисление пропущенных бонусов."""
    from app.services.referral_diagnostics_service import (
        MissingBonusReport,
        referral_diagnostics_service,
    )

    await callback.answer('💰 Начисляю бонусы...')

    try:
        # Получаем сохранённый отчёт
        data = await state.get_data()
        report_dict = data.get('missing_bonuses_report')

        if not report_dict:
            await callback.answer('❌ Отчёт не найден. Обновите проверку.', show_alert=True)
            return

        report = MissingBonusReport.from_dict(report_dict)

        if not report.missing_bonuses:
            await callback.answer('✅ Нет бонусов для начисления', show_alert=True)
            return

        # Применяем исправления
        fix_report = await referral_diagnostics_service.fix_missing_bonuses(
            db, report.missing_bonuses, apply=True
        )

        text = f"""
✅ <b>Бонусы начислены!</b>

📊 <b>Результат:</b>
• Обработано: {fix_report.users_fixed} пользователей
• Начислено рефералам: {fix_report.bonuses_to_referrals / 100:.0f}₽
• Начислено рефереерам: {fix_report.bonuses_to_referrers / 100:.0f}₽
• <b>Итого: {(fix_report.bonuses_to_referrals + fix_report.bonuses_to_referrers) / 100:.0f}₽</b>
"""

        if fix_report.errors > 0:
            text += f'\n⚠️ Ошибок: {fix_report.errors}'

        # Очищаем отчёт из state
        await state.update_data(missing_bonuses_report=None)

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔍 Проверить снова', callback_data='admin_ref_check_bonuses')],
                [types.InlineKeyboardButton(text='⬅️ К диагностике', callback_data='admin_referral_diagnostics')],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка в apply_missing_bonuses: {e}', exc_info=True)
        await callback.answer('Ошибка при начислении бонусов', show_alert=True)


@admin_required
@error_handler
async def sync_referrals_with_contest(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Синхронизирует всех рефералов с активными конкурсами."""
    from app.database.crud.referral_contest import get_contests_for_events
    from app.services.referral_contest_service import referral_contest_service

    await callback.answer('🏆 Синхронизирую с конкурсами...')

    try:
        from datetime import datetime

        now_utc = datetime.utcnow()

        # Получаем активные конкурсы
        paid_contests = await get_contests_for_events(db, now_utc, contest_types=['referral_paid'])
        reg_contests = await get_contests_for_events(db, now_utc, contest_types=['referral_registered'])

        all_contests = list(paid_contests) + list(reg_contests)

        if not all_contests:
            await callback.message.edit_text(
                '❌ <b>Нет активных конкурсов рефералов</b>\n\n'
                'Создайте конкурс в разделе "Конкурсы" для синхронизации.',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='⬅️ К диагностике', callback_data='admin_referral_diagnostics')]
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
                contest_results.append(f"• {contest.title}: +{stats.get('created', 0)} новых")
            else:
                contest_results.append(f'• {contest.title}: ошибка')

        text = f"""
🏆 <b>Синхронизация с конкурсами завершена!</b>

📊 <b>Результат:</b>
• Конкурсов обработано: {len(all_contests)}
• Новых событий добавлено: {total_created}
• Обновлено: {total_updated}
• Пропущено (уже есть): {total_skipped}

📋 <b>По конкурсам:</b>
"""
        text += '\n'.join(contest_results)

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 Синхронизировать снова', callback_data='admin_ref_sync_contest')],
                [types.InlineKeyboardButton(text='⬅️ К диагностике', callback_data='admin_referral_diagnostics')],
            ]
        )

        await callback.message.edit_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error(f'Ошибка в sync_referrals_with_contest: {e}', exc_info=True)
        await callback.answer('Ошибка при синхронизации', show_alert=True)


@admin_required
@error_handler
async def request_log_file_upload(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Запрашивает загрузку лог-файла для анализа."""
    await state.set_state(AdminStates.waiting_for_log_file)

    text = """
📤 <b>Загрузка лог-файла для анализа</b>

Отправьте файл лога (расширение .log или .txt).

Файл будет проанализирован на наличие потерянных рефералов за ВСЕ время, записанное в логе.

⚠️ <b>Важно:</b>
• Файл должен быть текстовым (.log, .txt)
• Максимальный размер: 50 MB
• После анализа файл будет автоматически удалён

Если ротация логов удалила старые данные — загрузите резервную копию.
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_referral_diagnostics')]]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@admin_required
@error_handler
async def receive_log_file(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Получает и анализирует загруженный лог-файл."""
    import os
    import tempfile

    if not message.document:
        await message.answer(
            '❌ Пожалуйста, отправьте файл документом.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_referral_diagnostics')]
                ]
            ),
        )
        return

    # Проверяем расширение файла
    file_name = message.document.file_name or 'unknown'
    file_ext = os.path.splitext(file_name)[1].lower()

    if file_ext not in ['.log', '.txt']:
        await message.answer(
            f'❌ Неверный формат файла: {file_ext}\n\n'
            f'Поддерживаются только текстовые файлы (.log, .txt)',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_referral_diagnostics')]
                ]
            ),
        )
        return

    # Проверяем размер файла
    max_size = 50 * 1024 * 1024  # 50 MB
    if message.document.file_size > max_size:
        await message.answer(
            f'❌ Файл слишком большой: {message.document.file_size / 1024 / 1024:.1f} MB\n\n'
            f'Максимальный размер: 50 MB',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_referral_diagnostics')]
                ]
            ),
        )
        return

    # Информируем о начале загрузки
    status_message = await message.answer(
        f'📥 Загружаю файл {file_name} ({message.document.file_size / 1024 / 1024:.1f} MB)...'
    )

    temp_file_path = None

    try:
        # Скачиваем файл во временную директорию
        temp_dir = tempfile.gettempdir()
        temp_file_path = os.path.join(temp_dir, f'ref_diagnostics_{message.from_user.id}_{file_name}')

        # Скачиваем файл
        file = await message.bot.get_file(message.document.file_id)
        await message.bot.download_file(file.file_path, temp_file_path)

        logger.info(f'📥 Файл загружен: {temp_file_path} ({message.document.file_size} байт)')

        # Обновляем статус
        await status_message.edit_text(f'🔍 Анализирую файл {file_name}...\n\nЭто может занять некоторое время.')

        # Анализируем файл
        from app.services.referral_diagnostics_service import referral_diagnostics_service

        report = await referral_diagnostics_service.analyze_file(db, temp_file_path)

        # Формируем отчёт
        text = f"""
🔍 <b>Анализ лог-файла: {file_name}</b>

<b>📊 Статистика переходов:</b>
• Всего кликов по реф-ссылкам: {report.total_ref_clicks}
• Уникальных пользователей: {report.unique_users_clicked}
• Потерянных рефералов: {len(report.lost_referrals)}
• Строк в файле: {report.lines_in_period}
"""

        if report.lost_referrals:
            text += '\n<b>❌ Потерянные рефералы:</b>\n'
            text += '<i>(пришли по ссылке, но реферер не засчитался)</i>\n\n'

            for i, lost in enumerate(report.lost_referrals[:15], 1):
                # Статус пользователя
                if not lost.registered:
                    status = '⚠️ Не в БД'
                elif not lost.has_referrer:
                    status = '❌ Без реферера'
                else:
                    status = f'⚡ Другой реферер (ID{lost.current_referrer_id})'

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
                text += f'\n<i>... и ещё {len(report.lost_referrals) - 15}</i>\n'
        else:
            text += '\n✅ <b>Все рефералы засчитаны!</b>\n'

        # Сохраняем отчёт в state для дальнейшего использования (сериализуем в dict)
        await state.update_data(
            diagnostics_period='uploaded_file',
            uploaded_file_report=report.to_dict(),
        )

        # Кнопки действий
        keyboard_rows = []

        if report.lost_referrals:
            keyboard_rows.append(
                [types.InlineKeyboardButton(text='📋 Предпросмотр исправлений', callback_data='admin_ref_fix_preview')]
            )

        keyboard_rows.extend(
            [
                [types.InlineKeyboardButton(text='⬅️ К диагностике', callback_data='admin_referral_diagnostics')],
                [types.InlineKeyboardButton(text='⬅️ К статистике', callback_data='admin_referrals')],
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
        logger.error(f'❌ Ошибка при обработке файла: {e}', exc_info=True)

        try:
            await status_message.edit_text(
                f'❌ <b>Ошибка при анализе файла</b>\n\n'
                f'Файл: {file_name}\n'
                f'Ошибка: {e!s}\n\n'
                f'Проверьте, что файл является текстовым логом бота.',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='🔄 Попробовать снова', callback_data='admin_ref_diag_upload')],
                        [types.InlineKeyboardButton(text='⬅️ К диагностике', callback_data='admin_referral_diagnostics')],
                    ]
                ),
            )
        except:
            await message.answer(
                f'❌ Ошибка при анализе файла: {e!s}',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_referral_diagnostics')]
                    ]
                ),
            )

    finally:
        # Удаляем временный файл
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.info(f'🗑️ Временный файл удалён: {temp_file_path}')
            except Exception as e:
                logger.error(f'Ошибка удаления временного файла: {e}')


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
