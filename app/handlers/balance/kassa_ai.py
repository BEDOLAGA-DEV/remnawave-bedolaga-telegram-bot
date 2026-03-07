"""Handler for KassaAI balance top-up."""

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


KASSA_AI_SUB_METHODS = {
    'kassa_ai_sbp': {'payment_system_id': 44, 'get_name': lambda: settings.get_kassa_ai_sbp_display_name()},
}

KASSA_AI_PAYMENT_METHODS = {'kassa_ai', 'kassa_ai_sbp'}


def _resolve_kassa_ai_params(
    payment_method: str | None,
) -> tuple[int | None, str]:
    """Return (payment_system_id, display_name) for a kassa_ai sub-method key."""
    if payment_method and payment_method in KASSA_AI_SUB_METHODS:
        meta = KASSA_AI_SUB_METHODS[payment_method]
        return meta['payment_system_id'], meta['get_name']()
    return settings.KASSA_AI_PAYMENT_SYSTEM_ID, settings.get_kassa_ai_display_name()


async def _create_kassa_ai_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
    payment_method: str | None = None,
):
    """
    Common logic for creating KassaAI payment and sending response.

    Args:
        message_or_callback: Either a Message or CallbackQuery object
        db_user: User object
        db: Database session
        amount_kopeks: Amount in kopeks
        edit_message: Whether to edit existing message or send new one
    """
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100

    ps_id, display_name = _resolve_kassa_ai_params(payment_method)

    # Create payment
    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    result = await payment_service.create_kassa_ai_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        email=getattr(db_user, 'email', None),
        language=db_user.language,
        payment_system_id=ps_id,
    )

    if not result:
        error_text = texts.t(
            'PAYMENT_CREATE_ERROR',
            'Не удалось создать платёж. Попробуйте позже.',
        )
        if edit_message:
            await message_or_callback.edit_text(
                error_text,
                reply_markup=get_back_keyboard(db_user.language),
                parse_mode='HTML',
            )
        else:
            await message_or_callback.answer(
                error_text,
                parse_mode='HTML',
            )
        return

    payment_url = result.get('payment_url')

    # Create keyboard with payment button
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'PAY_BUTTON',
                        '💳 Оплатить {amount}₽',
                    ).format(amount=f'{amount_rub:.0f}'),
                    url=payment_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_BUTTON', '◀️ Назад'),
                    callback_data='menu_balance',
                )
            ],
        ]
    )

    response_text = texts.t(
        'KASSA_AI_PAYMENT_CREATED',
        '💳 <b>Оплата через {name}</b>\n\n'
        'Сумма: <b>{amount}₽</b>\n\n'
        'Нажмите кнопку ниже для оплаты.\n'
        'После успешной оплаты баланс будет пополнен автоматически.',
    ).format(name=display_name, amount=f'{amount_rub:.2f}')

    if edit_message:
        await message_or_callback.edit_text(
            response_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    else:
        await message_or_callback.answer(
            response_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )

    logger.info('KassaAI payment created: user amount=₽', telegram_id=db_user.telegram_id, amount_rub=amount_rub)


@error_handler
async def process_kassa_ai_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
    payment_method: str | None = None,
):
    """
    Process payment amount directly (called from quick_amount handlers).
    """
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await message.answer(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await state.clear()
        return

    # Validate amount
    min_amount = settings.KASSA_AI_MIN_AMOUNT_KOPEKS
    max_amount = settings.KASSA_AI_MAX_AMOUNT_KOPEKS

    if amount_kopeks < min_amount:
        await message.answer(
            texts.t(
                'PAYMENT_AMOUNT_TOO_LOW',
                'Минимальная сумма пополнения: {min_amount}₽',
            ).format(min_amount=min_amount // 100),
            parse_mode='HTML',
        )
        return

    if amount_kopeks > max_amount:
        await message.answer(
            texts.t(
                'PAYMENT_AMOUNT_TOO_HIGH',
                'Максимальная сумма пополнения: {max_amount}₽',
            ).format(max_amount=max_amount // 100),
            parse_mode='HTML',
        )
        return

    await state.clear()

    await _create_kassa_ai_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
        payment_method=payment_method,
    )


@error_handler
async def start_kassa_ai_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """
    Start KassaAI top-up process - ask for amount.
    """
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='kassa_ai')

    min_amount = settings.KASSA_AI_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.KASSA_AI_MAX_AMOUNT_KOPEKS // 100
    display_name = settings.get_kassa_ai_display_name()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_BUTTON', '◀️ Назад'),
                    callback_data='menu_balance',
                )
            ]
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'KASSA_AI_ENTER_AMOUNT',
            '💳 <b>Пополнение через {name}</b>\n\n'
            'Введите сумму пополнения в рублях.\n\n'
            'Минимум: {min_amount}₽\n'
            'Максимум: {max_amount}₽',
        ).format(
            name=display_name,
            min_amount=min_amount,
            max_amount=f'{max_amount:,}'.replace(',', ' '),
        ),
        parse_mode='HTML',
        reply_markup=keyboard,
    )


@error_handler
async def process_kassa_ai_custom_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """
    Process custom amount input for KassaAI payment.
    """
    data = await state.get_data()
    if data.get('payment_method') not in KASSA_AI_PAYMENT_METHODS:
        return

    texts = get_texts(db_user.language)

    try:
        amount_text = message.text.replace(',', '.').replace(' ', '').strip()
        amount_rubles = float(amount_text)
        amount_kopeks = int(amount_rubles * 100)
    except (ValueError, TypeError):
        await message.answer(
            texts.t(
                'PAYMENT_INVALID_AMOUNT',
                'Введите корректную сумму числом.',
            ),
            parse_mode='HTML',
        )
        return

    await process_kassa_ai_payment_amount(
        message=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        state=state,
        payment_method=data.get('payment_method'),
    )


@error_handler
async def process_kassa_ai_quick_amount(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """
    Process quick amount selection for KassaAI payment.
    Called when user clicks a predefined amount button.
    """
    texts = get_texts(db_user.language)

    if not settings.is_kassa_ai_enabled():
        await callback.answer(
            texts.t('KASSA_AI_NOT_AVAILABLE', 'KassaAI временно недоступен'),
            show_alert=True,
        )
        return

    # Extract amount from callback data: topup_amount|kassa_ai|{amount_kopeks}
    try:
        parts = callback.data.split('|')
        if len(parts) >= 3:
            amount_kopeks = int(parts[2])
        else:
            await callback.answer('Invalid callback data', show_alert=True)
            return
    except (ValueError, IndexError):
        await callback.answer('Invalid amount', show_alert=True)
        return

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        return

    # Validate amount
    min_amount = settings.KASSA_AI_MIN_AMOUNT_KOPEKS
    max_amount = settings.KASSA_AI_MAX_AMOUNT_KOPEKS

    if amount_kopeks < min_amount:
        await callback.answer(
            texts.t('AMOUNT_TOO_LOW_SHORT', 'Сумма слишком мала'),
            show_alert=True,
        )
        return

    if amount_kopeks > max_amount:
        await callback.answer(
            texts.t('AMOUNT_TOO_HIGH_SHORT', 'Сумма слишком велика'),
            show_alert=True,
        )
        return

    await callback.answer()
    await state.clear()

    await _create_kassa_ai_payment_and_respond(
        message_or_callback=callback.message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=True,
        payment_method='kassa_ai',
    )


@error_handler
async def start_kassa_ai_sbp_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start KassaAI SBP top-up process - ask for amount."""
    texts = get_texts(db_user.language)

    if not settings.is_kassa_ai_sbp_enabled():
        await callback.answer(
            texts.t('KASSA_AI_NOT_AVAILABLE', 'KassaAI СБП временно недоступен'),
            show_alert=True,
        )
        return

    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='kassa_ai_sbp')

    min_amount = settings.KASSA_AI_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.KASSA_AI_MAX_AMOUNT_KOPEKS // 100
    display_name = settings.get_kassa_ai_sbp_display_name()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_BUTTON', '◀️ Назад'),
                    callback_data='menu_balance',
                )
            ]
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'KASSA_AI_ENTER_AMOUNT',
            '💳 <b>Пополнение через {name}</b>\n\n'
            'Введите сумму пополнения в рублях.\n\n'
            'Минимум: {min_amount}₽\n'
            'Максимум: {max_amount}₽',
        ).format(
            name=display_name,
            min_amount=min_amount,
            max_amount=f'{max_amount:,}'.replace(',', ' '),
        ),
        parse_mode='HTML',
        reply_markup=keyboard,
    )


@error_handler
async def process_kassa_ai_sbp_quick_amount(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Process quick amount selection for KassaAI SBP payment."""
    texts = get_texts(db_user.language)

    if not settings.is_kassa_ai_sbp_enabled():
        await callback.answer(
            texts.t('KASSA_AI_NOT_AVAILABLE', 'KassaAI СБП временно недоступен'),
            show_alert=True,
        )
        return

    try:
        parts = callback.data.split('|')
        if len(parts) >= 3:
            amount_kopeks = int(parts[2])
        else:
            await callback.answer('Invalid callback data', show_alert=True)
            return
    except (ValueError, IndexError):
        await callback.answer('Invalid amount', show_alert=True)
        return

    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        return

    min_amount = settings.KASSA_AI_MIN_AMOUNT_KOPEKS
    max_amount = settings.KASSA_AI_MAX_AMOUNT_KOPEKS

    if amount_kopeks < min_amount:
        await callback.answer(
            texts.t('AMOUNT_TOO_LOW_SHORT', 'Сумма слишком мала'),
            show_alert=True,
        )
        return

    if amount_kopeks > max_amount:
        await callback.answer(
            texts.t('AMOUNT_TOO_HIGH_SHORT', 'Сумма слишком велика'),
            show_alert=True,
        )
        return

    await callback.answer()
    await state.clear()

    await _create_kassa_ai_payment_and_respond(
        message_or_callback=callback.message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=True,
        payment_method='kassa_ai_sbp',
    )
