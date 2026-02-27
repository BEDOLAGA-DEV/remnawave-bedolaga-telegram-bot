import json
from pathlib import Path

import qrcode
import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_referral_keyboard
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.referral_withdrawal_service import referral_withdrawal_service
from app.states import ReferralWithdrawalStates
from app.utils.photo_message import edit_or_answer_photo
from app.utils.user_utils import (
    get_detailed_referral_list,
    get_effective_referral_commission_percent,
    get_referral_analytics,
    get_user_referral_summary,
)


logger = structlog.get_logger(__name__)


async def show_referral_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Проверяем, включена ли реферальная программа
    if not settings.is_referral_program_enabled():
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('REFERRAL_PROGRAM_DISABLED', 'Реферальная программа отключена'), show_alert=True)
        return

    texts = get_texts(db_user.language)

    summary = await get_user_referral_summary(db, db_user.id)

    bot_username = (await callback.bot.get_me()).username
    referral_link = f'https://t.me/{bot_username}?start={db_user.referral_code}'

    referral_text = (
        texts.t('REFERRAL_PROGRAM_TITLE', '👥 <b>Реферальная программа</b>')
        + '\n\n'
        + texts.t('REFERRAL_STATS_HEADER', '📊 <b>Ваша статистика:</b>')
        + '\n'
        + texts.t(
            'REFERRAL_STATS_INVITED',
            '• Приглашено пользователей: <b>{count}</b>',
        ).format(count=summary['invited_count'])
        + '\n'
        + texts.t(
            'REFERRAL_STATS_FIRST_TOPUPS',
            '• Сделали первое пополнение: <b>{count}</b>',
        ).format(count=summary['paid_referrals_count'])
        + '\n'
        + texts.t(
            'REFERRAL_STATS_ACTIVE',
            '• Активных рефералов: <b>{count}</b>',
        ).format(count=summary['active_referrals_count'])
        + '\n'
        + texts.t(
            'REFERRAL_STATS_CONVERSION',
            '• Конверсия: <b>{rate}%</b>',
        ).format(rate=summary['conversion_rate'])
        + '\n'
        + texts.t(
            'REFERRAL_STATS_TOTAL_EARNED',
            '• Заработано всего: <b>{amount}</b>',
        ).format(amount=texts.format_price(summary['total_earned_kopeks']))
        + '\n'
        + texts.t(
            'REFERRAL_STATS_MONTH_EARNED',
            '• За последний месяц: <b>{amount}</b>',
        ).format(amount=texts.format_price(summary['month_earned_kopeks']))
        + '\n\n'
        + texts.t('REFERRAL_REWARDS_HEADER', '🎁 <b>Как работают награды:</b>')
        + '\n'
        + texts.t(
            'REFERRAL_REWARD_NEW_USER',
            '• Новый пользователь получает: <b>{bonus}</b> при первом пополнении от <b>{minimum}</b>',
        ).format(
            bonus=texts.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
            minimum=texts.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
        )
        + '\n'
        + texts.t(
            'REFERRAL_REWARD_INVITER',
            '• Вы получаете при первом пополнении реферала: <b>{bonus}</b>',
        ).format(bonus=texts.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS))
        + '\n'
        + texts.t(
            'REFERRAL_REWARD_COMMISSION',
            '• Комиссия с каждого пополнения реферала: <b>{percent}%</b>',
        ).format(percent=get_effective_referral_commission_percent(db_user))
        + '\n\n'
        + texts.t('REFERRAL_LINK_TITLE', '🔗 <b>Ваша реферальная ссылка:</b>')
        + f'\n<code>{referral_link}</code>\n\n'
        + texts.t('REFERRAL_CODE_TITLE', '🆔 <b>Ваш код:</b> <code>{code}</code>').format(code=db_user.referral_code)
        + '\n\n'
    )

    if summary['recent_earnings']:
        meaningful_earnings = [earning for earning in summary['recent_earnings'][:5] if earning['amount_kopeks'] > 0]

        if meaningful_earnings:
            referral_text += (
                texts.t(
                    'REFERRAL_RECENT_EARNINGS_HEADER',
                    '💰 <b>Последние начисления:</b>',
                )
                + '\n'
            )
            for earning in meaningful_earnings[:3]:
                reason_text = {
                    'referral_first_topup': texts.t(
                        'REFERRAL_EARNING_REASON_FIRST_TOPUP',
                        '🎉 Первое пополнение',
                    ),
                    'referral_commission_topup': texts.t(
                        'REFERRAL_EARNING_REASON_COMMISSION_TOPUP',
                        '💰 Комиссия с пополнения',
                    ),
                    'referral_commission': texts.t(
                        'REFERRAL_EARNING_REASON_COMMISSION_PURCHASE',
                        '💰 Комиссия с покупки',
                    ),
                }.get(earning['reason'], earning['reason'])

                referral_text += (
                    texts.t(
                        'REFERRAL_RECENT_EARNINGS_ITEM',
                        '• {reason}: <b>{amount}</b> от {referral_name}',
                    ).format(
                        reason=reason_text,
                        amount=texts.format_price(earning['amount_kopeks']),
                        referral_name=earning['referral_name'],
                    )
                    + '\n'
                )
            referral_text += '\n'

    if summary['earnings_by_type']:
        referral_text += (
            texts.t(
                'REFERRAL_EARNINGS_BY_TYPE_HEADER',
                '📈 <b>Доходы по типам:</b>',
            )
            + '\n'
        )

        if 'referral_first_topup' in summary['earnings_by_type']:
            data = summary['earnings_by_type']['referral_first_topup']
            if data['total_amount_kopeks'] > 0:
                referral_text += (
                    texts.t(
                        'REFERRAL_EARNINGS_FIRST_TOPUPS',
                        '• Бонусы за первые пополнения: <b>{count}</b> ({amount})',
                    ).format(
                        count=data['count'],
                        amount=texts.format_price(data['total_amount_kopeks']),
                    )
                    + '\n'
                )

        if 'referral_commission_topup' in summary['earnings_by_type']:
            data = summary['earnings_by_type']['referral_commission_topup']
            if data['total_amount_kopeks'] > 0:
                referral_text += (
                    texts.t(
                        'REFERRAL_EARNINGS_TOPUPS',
                        '• Комиссии с пополнений: <b>{count}</b> ({amount})',
                    ).format(
                        count=data['count'],
                        amount=texts.format_price(data['total_amount_kopeks']),
                    )
                    + '\n'
                )

        if 'referral_commission' in summary['earnings_by_type']:
            data = summary['earnings_by_type']['referral_commission']
            if data['total_amount_kopeks'] > 0:
                referral_text += (
                    texts.t(
                        'REFERRAL_EARNINGS_PURCHASES',
                        '• Комиссии с покупок: <b>{count}</b> ({amount})',
                    ).format(
                        count=data['count'],
                        amount=texts.format_price(data['total_amount_kopeks']),
                    )
                    + '\n'
                )

        referral_text += '\n'

    referral_text += texts.t(
        'REFERRAL_INVITE_FOOTER',
        '📢 Приглашайте друзей и зарабатывайте!',
    )

    await edit_or_answer_photo(
        callback,
        referral_text,
        get_referral_keyboard(db_user.language),
    )
    await callback.answer()


async def show_referral_qr(
    callback: types.CallbackQuery,
    db_user: User,
):
    await callback.answer()

    texts = get_texts(db_user.language)

    bot_username = (await callback.bot.get_me()).username
    referral_link = f'https://t.me/{bot_username}?start={db_user.referral_code}'

    qr_dir = Path('data') / 'referral_qr'
    qr_dir.mkdir(parents=True, exist_ok=True)

    file_path = qr_dir / f'{db_user.id}.png'
    if not file_path.exists():
        img = qrcode.make(referral_link)
        img.save(file_path)

    photo = FSInputFile(file_path)
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
    )

    try:
        await callback.message.edit_media(
            types.InputMediaPhoto(
                media=photo,
                caption=texts.t(
                    'REFERRAL_LINK_CAPTION',
                    '🔗 Ваша реферальная ссылка:\n{link}',
                ).format(link=referral_link),
            ),
            reply_markup=keyboard,
        )
    except TelegramBadRequest:
        await callback.message.delete()
        await callback.message.answer_photo(
            photo,
            caption=texts.t(
                'REFERRAL_LINK_CAPTION',
                '🔗 Ваша реферальная ссылка:\n{link}',
            ).format(link=referral_link),
            reply_markup=keyboard,
        )


async def show_detailed_referral_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    texts = get_texts(db_user.language)

    referrals_data = await get_detailed_referral_list(db, db_user.id, limit=10, offset=(page - 1) * 10)

    if not referrals_data['referrals']:
        await edit_or_answer_photo(
            callback,
            texts.t(
                'REFERRAL_LIST_EMPTY',
                '📋 У вас пока нет рефералов.\n\nПоделитесь своей реферальной ссылкой, чтобы начать зарабатывать!',
            ),
            types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
            ),
            parse_mode=None,
        )
        await callback.answer()
        return

    text = (
        texts.t(
            'REFERRAL_LIST_HEADER',
            '👥 <b>Ваши рефералы</b> (стр. {current}/{total})',
        ).format(
            current=referrals_data['current_page'],
            total=referrals_data['total_pages'],
        )
        + '\n\n'
    )

    for i, referral in enumerate(referrals_data['referrals'], 1):
        status_emoji = '🟢' if referral['status'] == 'active' else '🔴'

        topup_emoji = '💰' if referral['has_made_first_topup'] else '⏳'

        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_HEADER',
                '{index}. {status} <b>{name}</b>',
            ).format(index=i, status=status_emoji, name=referral['full_name'])
            + '\n'
        )
        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_TOPUPS',
                '   {emoji} Пополнений: {count}',
            ).format(emoji=topup_emoji, count=referral['topups_count'])
            + '\n'
        )
        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_EARNED',
                '   💎 Заработано с него: {amount}',
            ).format(amount=texts.format_price(referral['total_earned_kopeks']))
            + '\n'
        )
        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_REGISTERED',
                '   📅 Регистрация: {days} дн. назад',
            ).format(days=referral['days_since_registration'])
            + '\n'
        )

        if referral['days_since_activity'] is not None:
            text += (
                texts.t(
                    'REFERRAL_LIST_ITEM_ACTIVITY',
                    '   🕐 Активность: {days} дн. назад',
                ).format(days=referral['days_since_activity'])
                + '\n'
            )
        else:
            text += (
                texts.t(
                    'REFERRAL_LIST_ITEM_ACTIVITY_LONG_AGO',
                    '   🕐 Активность: давно',
                )
                + '\n'
            )

        text += '\n'

    keyboard = []
    nav_buttons = []

    if referrals_data['has_prev']:
        nav_buttons.append(
            types.InlineKeyboardButton(
                text=texts.t('REFERRAL_LIST_PREV_PAGE', '⬅️ Назад'), callback_data=f'referral_list_page_{page - 1}'
            )
        )

    if referrals_data['has_next']:
        nav_buttons.append(
            types.InlineKeyboardButton(
                text=texts.t('REFERRAL_LIST_NEXT_PAGE', 'Вперед ➡️'), callback_data=f'referral_list_page_{page + 1}'
            )
        )

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')])

    await edit_or_answer_photo(
        callback,
        text,
        types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


async def show_referral_analytics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    analytics = await get_referral_analytics(db, db_user.id)

    text = texts.t('REFERRAL_ANALYTICS_TITLE', '📊 <b>Аналитика рефералов</b>') + '\n\n'

    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_HEADER',
            '💰 <b>Доходы по периодам:</b>',
        )
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_TODAY',
            '• Сегодня: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['today']))
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_WEEK',
            '• За неделю: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['week']))
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_MONTH',
            '• За месяц: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['month']))
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_QUARTER',
            '• За квартал: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['quarter']))
        + '\n\n'
    )

    if analytics['top_referrals']:
        text += (
            texts.t(
                'REFERRAL_ANALYTICS_TOP_TITLE',
                '🏆 <b>Топ-{count} рефералов:</b>',
            ).format(count=len(analytics['top_referrals']))
            + '\n'
        )
        for i, ref in enumerate(analytics['top_referrals'], 1):
            text += (
                texts.t(
                    'REFERRAL_ANALYTICS_TOP_ITEM',
                    '{index}. {name}: {amount} ({count} начислений)',
                ).format(
                    index=i,
                    name=ref['referral_name'],
                    amount=texts.format_price(ref['total_earned_kopeks']),
                    count=ref['earnings_count'],
                )
                + '\n'
            )
        text += '\n'

    text += texts.t(
        'REFERRAL_ANALYTICS_FOOTER',
        '📈 Продолжайте развивать свою реферальную сеть!',
    )

    await edit_or_answer_photo(
        callback,
        text,
        types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
        ),
    )
    await callback.answer()


async def create_invite_message(callback: types.CallbackQuery, db_user: User):
    texts = get_texts(db_user.language)

    bot_username = (await callback.bot.get_me()).username
    referral_link = f'https://t.me/{bot_username}?start={db_user.referral_code}'

    invite_text = (
        texts.t('REFERRAL_INVITE_TITLE', '🎉 Присоединяйся к VPN сервису!')
        + '\n\n'
        + texts.t(
            'REFERRAL_INVITE_BONUS',
            '💎 При первом пополнении от {minimum} ты получишь {bonus} бонусом на баланс!',
        ).format(
            minimum=texts.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
            bonus=texts.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
        )
        + '\n\n'
        + texts.t('REFERRAL_INVITE_FEATURE_FAST', '🚀 Быстрое подключение')
        + '\n'
        + texts.t('REFERRAL_INVITE_FEATURE_SERVERS', '🌍 Серверы по всему миру')
        + '\n'
        + texts.t('REFERRAL_INVITE_FEATURE_SECURE', '🔒 Надежная защита')
        + '\n\n'
        + texts.t('REFERRAL_INVITE_LINK_PROMPT', '👇 Переходи по ссылке:')
        + f'\n{referral_link}'
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_SHARE_BUTTON', '📤 Поделиться'), switch_inline_query=invite_text
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')],
        ]
    )

    await edit_or_answer_photo(
        callback,
        (
            texts.t('REFERRAL_INVITE_CREATED_TITLE', '📝 <b>Приглашение создано!</b>')
            + '\n\n'
            + texts.t(
                'REFERRAL_INVITE_CREATED_INSTRUCTION',
                'Нажмите кнопку «📤 Поделиться» чтобы отправить приглашение в любой чат, или скопируйте текст ниже:',
            )
            + '\n\n'
            f'<code>{invite_text}</code>'
        ),
        keyboard,
    )
    await callback.answer()


async def show_withdrawal_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Показывает информацию о выводе реферального баланса."""
    texts = get_texts(db_user.language)

    if not settings.is_referral_withdrawal_enabled():
        await callback.answer(texts.t('REFERRAL_WITHDRAWAL_DISABLED', 'Функция вывода отключена'), show_alert=True)
        return

    # Получаем детальную статистику баланса
    stats = await referral_withdrawal_service.get_referral_balance_stats(db, db_user.id)
    min_amount = settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS
    cooldown_days = settings.REFERRAL_WITHDRAWAL_COOLDOWN_DAYS

    # Проверяем возможность вывода
    can_request, reason, _stats = await referral_withdrawal_service.can_request_withdrawal(db, db_user.id)

    text = texts.t('REFERRAL_WITHDRAWAL_TITLE', '💸 <b>Вывод реферального баланса</b>') + '\n\n'

    # Показываем детальную статистику
    text += referral_withdrawal_service.format_balance_stats_for_user(stats, texts)
    text += '\n'

    text += (
        texts.t('REFERRAL_WITHDRAWAL_MIN_AMOUNT', '📊 Минимальная сумма: <b>{amount}</b>').format(
            amount=texts.format_price(min_amount)
        )
        + '\n'
    )
    text += (
        texts.t('REFERRAL_WITHDRAWAL_COOLDOWN', '⏱ Частота вывода: раз в <b>{days}</b> дней').format(days=cooldown_days)
        + '\n\n'
    )

    keyboard = []

    if can_request:
        text += texts.t('REFERRAL_WITHDRAWAL_READY', '✅ Вы можете запросить вывод средств') + '\n'
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_WITHDRAWAL_REQUEST_BUTTON', '📝 Оформить заявку'),
                    callback_data='referral_withdrawal_start',
                )
            ]
        )
    else:
        text += f'❌ {reason}\n'

    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')])

    await edit_or_answer_photo(callback, text, types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


async def start_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Начинает процесс оформления заявки на вывод."""
    texts = get_texts(db_user.language)

    # Повторная проверка
    can_request, reason, wd_stats = await referral_withdrawal_service.can_request_withdrawal(db, db_user.id)
    if not can_request:
        await callback.answer(reason, show_alert=True)
        return

    available = wd_stats.get('available_total', 0) if wd_stats else 0

    # Сохраняем доступный баланс в состоянии
    await state.update_data(available_balance=available)
    await state.set_state(ReferralWithdrawalStates.waiting_for_amount)

    text = texts.t(
        'REFERRAL_WITHDRAWAL_ENTER_AMOUNT', '💸 Введите сумму для вывода в рублях\n\nДоступно: <b>{amount}</b>'
    ).format(amount=texts.format_price(available))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_WITHDRAWAL_ALL', 'Вывести всё ({amount})').format(
                        amount=f'{available / 100:.0f}₽'
                    ),
                    callback_data=f'referral_withdrawal_amount_{available}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                )
            ],
        ]
    )

    await edit_or_answer_photo(callback, text, keyboard)
    await callback.answer()


async def process_withdrawal_amount(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Обрабатывает ввод суммы для вывода."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    available = data.get('available_balance', 0)

    try:
        # Парсим сумму (в рублях)
        amount_text = message.text.strip().replace(',', '.').replace('₽', '').replace(' ', '')
        amount_rubles = float(amount_text)
        amount_kopeks = int(amount_rubles * 100)

        if amount_kopeks <= 0:
            await message.answer(
                texts.t('REFERRAL_WITHDRAWAL_INVALID_AMOUNT_POSITIVE', '❌ Введите положительную сумму')
            )
            return

        min_amount = settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS
        if amount_kopeks < min_amount:
            await message.answer(
                texts.t('REFERRAL_WITHDRAWAL_MIN_ERROR', '❌ Минимальная сумма: {amount}').format(
                    amount=texts.format_price(min_amount)
                )
            )
            return

        if amount_kopeks > available:
            await message.answer(
                texts.t('REFERRAL_WITHDRAWAL_INSUFFICIENT', '❌ Недостаточно средств. Доступно: {amount}').format(
                    amount=texts.format_price(available)
                )
            )
            return

        # Сохраняем сумму и переходим к вводу реквизитов
        await state.update_data(withdrawal_amount=amount_kopeks)
        await state.set_state(ReferralWithdrawalStates.waiting_for_payment_details)

        text = texts.t(
            'REFERRAL_WITHDRAWAL_ENTER_DETAILS',
            '💳 Введите реквизиты для перевода:\n\nНапример:\n• СБП: +7 999 123-45-67 (Сбербанк)',
        )

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                    )
                ]
            ]
        )

        await message.answer(text, reply_markup=keyboard)

    except ValueError:
        await message.answer(texts.t('REFERRAL_WITHDRAWAL_INVALID_AMOUNT', '❌ Введите корректную сумму'))


async def process_withdrawal_amount_callback(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Обрабатывает выбор суммы для вывода через кнопку."""
    texts = get_texts(db_user.language)

    # Получаем сумму из callback_data
    amount_kopeks = int(callback.data.split('_')[-1])

    # Сохраняем сумму и переходим к вводу реквизитов
    await state.update_data(withdrawal_amount=amount_kopeks)
    await state.set_state(ReferralWithdrawalStates.waiting_for_payment_details)

    text = texts.t(
        'REFERRAL_WITHDRAWAL_ENTER_DETAILS',
        '💳 Введите реквизиты для перевода:\n\nНапример:\n• СБП: +7 999 123-45-67 (Сбербанк)',
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                )
            ]
        ]
    )

    await edit_or_answer_photo(callback, text, keyboard)
    await callback.answer()


async def process_payment_details(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Обрабатывает ввод реквизитов и показывает подтверждение."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    amount_kopeks = data.get('withdrawal_amount', 0)
    payment_details = message.text.strip()

    if len(payment_details) < 10:
        await message.answer(texts.t('REFERRAL_WITHDRAWAL_DETAILS_TOO_SHORT', '❌ Реквизиты слишком короткие'))
        return

    # Сохраняем реквизиты
    await state.update_data(payment_details=payment_details)
    await state.set_state(ReferralWithdrawalStates.confirming)

    text = texts.t('REFERRAL_WITHDRAWAL_CONFIRM_TITLE', '📋 <b>Подтверждение заявки</b>') + '\n\n'
    text += (
        texts.t('REFERRAL_WITHDRAWAL_CONFIRM_AMOUNT', '💰 Сумма: <b>{amount}</b>').format(
            amount=texts.format_price(amount_kopeks)
        )
        + '\n\n'
    )
    text += (
        texts.t('REFERRAL_WITHDRAWAL_CONFIRM_DETAILS', '💳 Реквизиты:\n<code>{details}</code>').format(
            details=payment_details
        )
        + '\n\n'
    )
    text += texts.t('REFERRAL_WITHDRAWAL_CONFIRM_WARNING', '⚠️ После отправки заявка будет рассмотрена администрацией')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_WITHDRAWAL_CONFIRM_BUTTON', '✅ Подтвердить'),
                    callback_data='referral_withdrawal_confirm',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                )
            ],
        ]
    )

    await message.answer(text, reply_markup=keyboard)


async def confirm_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Подтверждает и создаёт заявку на вывод."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    amount_kopeks = data.get('withdrawal_amount', 0)
    payment_details = data.get('payment_details', '')

    await state.clear()

    # Создаём заявку
    request, error = await referral_withdrawal_service.create_withdrawal_request(
        db, db_user.id, amount_kopeks, payment_details
    )

    if error:
        await callback.answer(f'❌ {error}', show_alert=True)
        return

    # Отправляем уведомление админам
    analysis = json.loads(request.risk_analysis) if request.risk_analysis else {}

    user_id_display = db_user.telegram_id or db_user.email or f'#{db_user.id}'
    admin_text = texts.t(
        'ADMIN_REFERRALS_WITHDRAWAL_NEW_REQUEST_TEXT',
        '🔔 <b>Новая заявка на вывод #{request_id}</b>\n\n'
        '👤 Пользователь: {user_name}\n'
        '🆔 ID: <code>{user_id_display}</code>\n'
        '💰 Сумма: <b>{amount}</b>\n\n'
        '💳 Реквизиты:\n'
        '<code>{payment_details}</code>\n\n'
        '{analysis_text}',
    ).format(
        request_id=request.id,
        user_name=db_user.full_name or texts.t('ADMIN_REFERRALS_NO_NAME', 'Без имени'),
        user_id_display=user_id_display,
        amount=f'{amount_kopeks / 100:.0f}₽',
        payment_details=payment_details,
        analysis_text=referral_withdrawal_service.format_analysis_for_admin(analysis),
    )

    # Формируем клавиатуру - кнопка профиля только для Telegram-пользователей
    keyboard_rows = [
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
    ]
    if db_user.telegram_id:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_REFERRALS_WITHDRAWAL_BUTTON_USER_PROFILE', '👤 Профиль пользователя'),
                    callback_data=f'admin_user_{db_user.telegram_id}',
                )
            ]
        )
    admin_keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    try:
        notification_service = AdminNotificationService(callback.bot)
        await notification_service.send_admin_notification(admin_text, reply_markup=admin_keyboard)
    except Exception as e:
        logger.error('Ошибка отправки уведомления админам о заявке на вывод', error=e)

    # Уведомление в топик, если настроено
    topic_id = settings.REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID
    if topic_id and settings.ADMIN_NOTIFICATIONS_CHAT_ID:
        try:
            await callback.bot.send_message(
                chat_id=settings.ADMIN_NOTIFICATIONS_CHAT_ID,
                message_thread_id=topic_id,
                text=admin_text,
                reply_markup=admin_keyboard,
                parse_mode='HTML',
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления в топик о заявке на вывод', error=e)

    # Отвечаем пользователю
    text = texts.t(
        'REFERRAL_WITHDRAWAL_SUCCESS',
        '✅ <b>Заявка #{id} создана!</b>\n\n'
        'Сумма: <b>{amount}</b>\n\n'
        'Ваша заявка будет рассмотрена администрацией. '
        'Мы уведомим вас о результате.',
    ).format(id=request.id, amount=texts.format_price(amount_kopeks))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
    )

    await edit_or_answer_photo(callback, text, keyboard)
    await callback.answer()


async def cancel_withdrawal_request(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """Отменяет процесс создания заявки на вывод."""
    await state.clear()
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('CANCELLED', 'Отменено'))

    # Возвращаем в меню партнёрки
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
    )
    await edit_or_answer_photo(callback, texts.t('REFERRAL_WITHDRAWAL_CANCELLED', '❌ Заявка отменена'), keyboard)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_referral_info, F.data == 'menu_referrals')

    dp.callback_query.register(create_invite_message, F.data == 'referral_create_invite')

    dp.callback_query.register(show_referral_qr, F.data == 'referral_show_qr')

    dp.callback_query.register(show_detailed_referral_list, F.data == 'referral_list')

    dp.callback_query.register(show_referral_analytics, F.data == 'referral_analytics')

    async def handle_referral_list_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
        page = int(callback.data.split('_')[-1])
        await show_detailed_referral_list(callback, db_user, db, page)

    dp.callback_query.register(handle_referral_list_page, F.data.startswith('referral_list_page_'))

    # Хендлеры вывода реферального баланса
    dp.callback_query.register(show_withdrawal_info, F.data == 'referral_withdrawal')

    dp.callback_query.register(start_withdrawal_request, F.data == 'referral_withdrawal_start')

    dp.callback_query.register(process_withdrawal_amount_callback, F.data.startswith('referral_withdrawal_amount_'))

    dp.callback_query.register(confirm_withdrawal_request, F.data == 'referral_withdrawal_confirm')

    dp.callback_query.register(cancel_withdrawal_request, F.data == 'referral_withdrawal_cancel')

    # Обработка текстового ввода суммы
    dp.message.register(process_withdrawal_amount, ReferralWithdrawalStates.waiting_for_amount)

    # Обработка текстового ввода реквизитов
    dp.message.register(process_payment_details, ReferralWithdrawalStates.waiting_for_payment_details)
