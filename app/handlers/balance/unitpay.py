"""Handler for UnitPay balance top-up."""

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
from app.services.unitpay_service import UNITPAY_SUB_METHODS
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


# --- Enabled check + display name lookup by payment method ---

_UNITPAY_METHOD_CONFIG = {
    'unitpay': {
        'is_enabled': settings.is_unitpay_enabled,
        'display_name': lambda: settings.UNITPAY_DISPLAY_NAME,
        'unavailable_text': 'UnitPay временно недоступен',
    },
    'unitpay_sbp': {
        'is_enabled': settings.is_unitpay_sbp_enabled,
        'display_name': settings.get_unitpay_sbp_display_name,
        'unavailable_text': 'UnitPay СБП временно недоступен',
    },
    'unitpay_card': {
        'is_enabled': settings.is_unitpay_card_enabled,
        'display_name': settings.get_unitpay_card_display_name,
        'unavailable_text': 'UnitPay Карта временно недоступна',
    },
}


async def _check_topup_restriction(callback: types.CallbackQuery, db_user: User) -> bool:
    """Check if user has topup restriction. Returns True if restricted."""
    if not getattr(db_user, 'restriction_topup', False):
        return False
    texts = get_texts(db_user.language)
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
    return True


async def _create_unitpay_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
    payment_method: str = 'unitpay',
):
    """Common logic for creating UnitPay payment and sending response."""
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100

    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    sub = UNITPAY_SUB_METHODS.get(payment_method)
    payment_type = sub['payment_type'] if sub else settings.UNITPAY_PAYMENT_TYPE

    result = await payment_service.create_unitpay_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        email=getattr(db_user, 'email', None),
        language=db_user.language,
        payment_type=payment_type,
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
    cfg = _UNITPAY_METHOD_CONFIG.get(payment_method, _UNITPAY_METHOD_CONFIG['unitpay'])
    display_name = cfg['display_name']()

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
        'UNITPAY_PAYMENT_CREATED',
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

    logger.info('UnitPay payment created', telegram_id=db_user.telegram_id, amount_rub=amount_rub)


@error_handler
async def process_unitpay_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
    payment_method: str = 'unitpay',
):
    """Process payment amount directly (called from custom_amount handlers)."""
    texts = get_texts(db_user.language)

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

    min_amount = settings.UNITPAY_MIN_AMOUNT_KOPEKS
    max_amount = settings.UNITPAY_MAX_AMOUNT_KOPEKS

    if amount_kopeks < min_amount:
        await message.answer(
            texts.t(
                'PAYMENT_AMOUNT_TOO_LOW',
                'Минимальная сумма пополнения: {min_amount}₽',
            ).format(min_amount=min_amount // 100),
            reply_markup=get_back_keyboard(db_user.language),
            parse_mode='HTML',
        )
        return

    if amount_kopeks > max_amount:
        await message.answer(
            texts.t(
                'PAYMENT_AMOUNT_TOO_HIGH',
                'Максимальная сумма пополнения: {max_amount}₽',
            ).format(max_amount=max_amount // 100),
            reply_markup=get_back_keyboard(db_user.language),
            parse_mode='HTML',
        )
        return

    await state.clear()

    await _create_unitpay_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
        payment_method=payment_method,
    )


# --- Generic start handler ---


async def _start_unitpay_sub_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    payment_method: str,
):
    """Generic start topup handler for any UnitPay sub-method."""
    cfg = _UNITPAY_METHOD_CONFIG[payment_method]
    texts = get_texts(db_user.language)

    if not cfg['is_enabled']():
        await callback.answer(texts.t('UNITPAY_NOT_AVAILABLE', cfg['unavailable_text']), show_alert=True)
        return

    if await _check_topup_restriction(callback, db_user):
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method=payment_method)

    min_amount = settings.UNITPAY_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.UNITPAY_MAX_AMOUNT_KOPEKS // 100
    display_name = cfg['display_name']()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.t('BACK_BUTTON', '◀️ Назад'), callback_data='menu_balance')]]
    )

    await callback.message.edit_text(
        texts.t(
            'UNITPAY_ENTER_AMOUNT',
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


# --- Public handler functions ---


@error_handler
async def start_unitpay_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start UnitPay default top-up process."""
    await _start_unitpay_sub_topup(callback, db_user, db, state, 'unitpay')


@error_handler
async def process_unitpay_custom_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Process custom amount input for UnitPay payment."""
    data = await state.get_data()
    pm = data.get('payment_method', '')
    if pm not in _UNITPAY_METHOD_CONFIG:
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

    await process_unitpay_payment_amount(
        message=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        state=state,
        payment_method=pm,
    )


@error_handler
async def start_unitpay_sbp_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start UnitPay SBP top-up process."""
    await _start_unitpay_sub_topup(callback, db_user, db, state, 'unitpay_sbp')


@error_handler
async def start_unitpay_card_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start UnitPay Card top-up process."""
    await _start_unitpay_sub_topup(callback, db_user, db, state, 'unitpay_card')
