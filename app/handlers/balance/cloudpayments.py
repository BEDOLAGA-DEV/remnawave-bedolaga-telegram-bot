"""Handler for CloudPayments balance top-up."""

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


async def _create_cloudpayments_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
):
    """
    Common logic for creating CloudPayments payment and sending response.

    Args:
        message_or_callback: Either a Message or CallbackQuery object
        db_user: User object
        db: Database session
        amount_kopeks: Amount in kopeks
        edit_message: Whether to edit existing message or send new one
    """
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100

    # Create payment
    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description=settings.CLOUDPAYMENTS_DESCRIPTION,
    )

    result = await payment_service.create_cloudpayments_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        telegram_id=db_user.telegram_id,
        language=db_user.language,
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
        'CLOUDPAYMENTS_PAYMENT_CREATED',
        '💳 <b>Оплата банковской картой</b>\n\n'
        'Сумма: <b>{amount}₽</b>\n\n'
        'Нажмите кнопку ниже для оплаты.\n'
        'После успешной оплаты баланс будет пополнен автоматически.',
    ).format(amount=f'{amount_rub:.2f}')

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

    logger.info('CloudPayments payment created: user amount=₽', telegram_id=db_user.telegram_id, amount_rub=amount_rub)


@error_handler
async def process_cloudpayments_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    """
    Process payment amount directly (called from quick_amount handlers).

    Similar to process_heleket_payment_amount and other payment handlers.
    """
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or texts.t(
            'USER_RESTRICTION_REASON_DEFAULT', 'Действие ограничено администратором'
        )
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать'),
                        url=support_url,
                    )
                ]
            )
        keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await message.answer(
            texts.t(
                'USER_RESTRICTION_TOPUP_BLOCKED',
                '🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.',
            ).format(reason=reason),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await state.clear()
        return

    if not settings.is_cloudpayments_enabled():
        await message.answer(
            texts.t('CLOUDPAYMENTS_NOT_AVAILABLE', 'CloudPayments временно недоступен'),
        )
        return

    # Validate amount
    if amount_kopeks < settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS:
        min_rub = settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS / 100
        await message.answer(
            texts.t(
                'AMOUNT_TOO_LOW',
                'Минимальная сумма пополнения: {min_amount:.0f}₽',
            ).format(min_amount=min_rub),
        )
        return

    if amount_kopeks > settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS:
        max_rub = settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS / 100
        await message.answer(
            texts.t(
                'AMOUNT_TOO_HIGH',
                'Максимальная сумма пополнения: {max_amount:,.0f}₽',
            ).format(max_amount=max_rub),
        )
        return

    # Clear state
    await state.clear()

    await _create_cloudpayments_payment_and_respond(message, db_user, db, amount_kopeks, edit_message=False)


@error_handler
async def start_cloudpayments_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    """
    Start CloudPayments payment flow.

    Shows amount input prompt or quick amount buttons.
    """
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or texts.t(
            'USER_RESTRICTION_REASON_DEFAULT', 'Действие ограничено администратором'
        )
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать'),
                        url=support_url,
                    )
                ]
            )
        keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            texts.t(
                'USER_RESTRICTION_TOPUP_BLOCKED',
                '🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.',
            ).format(reason=reason),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    if not settings.is_cloudpayments_enabled():
        await callback.answer(
            texts.t('CLOUDPAYMENTS_NOT_AVAILABLE', 'CloudPayments временно недоступен'),
            show_alert=True,
        )
        return

    min_amount_rub = settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS / 100
    max_amount_rub = settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS / 100

    message_text = texts.t(
        'CLOUDPAYMENTS_ENTER_AMOUNT',
        '💳 <b>Оплата банковской картой (CloudPayments)</b>\n\n'
        'Введите сумму для пополнения от {min_amount:.0f} до {max_amount:,.0f} рублей:',
    ).format(min_amount=min_amount_rub, max_amount=max_amount_rub)

    keyboard = get_back_keyboard(db_user.language)

    await callback.message.edit_text(
        message_text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='cloudpayments')
    await state.update_data(
        cloudpayments_prompt_message_id=callback.message.message_id,
        cloudpayments_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_cloudpayments_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """
    Process entered amount and create CloudPayments payment.

    Generates payment link and sends it to user.
    """
    texts = get_texts(db_user.language)

    # Get state data
    state_data = await state.get_data()
    payment_method = state_data.get('payment_method')

    if payment_method != 'cloudpayments':
        return  # Not our payment method

    # Parse amount
    try:
        amount_text = message.text.strip().replace(',', '.').replace(' ', '')
        amount_rub = float(amount_text)
        amount_kopeks = int(amount_rub * 100)
    except (ValueError, TypeError):
        await message.answer(
            texts.t('INVALID_AMOUNT', 'Введите корректную сумму числом'),
            parse_mode='HTML',
        )
        return

    # Validate amount
    if amount_kopeks < settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS:
        min_rub = settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS / 100
        await message.answer(
            texts.t(
                'AMOUNT_TOO_LOW',
                'Минимальная сумма пополнения: {min_amount:.0f}₽',
            ).format(min_amount=min_rub),
            parse_mode='HTML',
        )
        return

    if amount_kopeks > settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS:
        max_rub = settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS / 100
        await message.answer(
            texts.t(
                'AMOUNT_TOO_HIGH',
                'Максимальная сумма пополнения: {max_amount:,.0f}₽',
            ).format(max_amount=max_rub),
            parse_mode='HTML',
        )
        return

    # Clear state
    await state.clear()

    # Create payment
    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description=settings.CLOUDPAYMENTS_DESCRIPTION,
    )

    result = await payment_service.create_cloudpayments_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        telegram_id=db_user.telegram_id,
        language=db_user.language,
    )

    if not result:
        await message.answer(
            texts.t(
                'PAYMENT_CREATE_ERROR',
                'Не удалось создать платёж. Попробуйте позже.',
            ),
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

    await message.answer(
        texts.t(
            'CLOUDPAYMENTS_PAYMENT_CREATED',
            '💳 <b>Оплата банковской картой</b>\n\n'
            'Сумма: <b>{amount}₽</b>\n\n'
            'Нажмите кнопку ниже для оплаты.\n'
            'После успешной оплаты баланс будет пополнен автоматически.',
        ).format(amount=f'{amount_rub:.2f}'),
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    logger.info('CloudPayments payment created: user amount=₽', telegram_id=db_user.telegram_id, amount_rub=amount_rub)


@error_handler
async def handle_cloudpayments_quick_amount(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """
    Handle quick amount selection for CloudPayments.

    Called when user clicks a predefined amount button.
    """
    texts = get_texts(db_user.language)

    if not settings.is_cloudpayments_enabled():
        await callback.answer(
            texts.t('CLOUDPAYMENTS_NOT_AVAILABLE', 'CloudPayments временно недоступен'),
            show_alert=True,
        )
        return

    # Extract amount from callback data: topup_amount|cloudpayments|{amount_kopeks}
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

    amount_rub = amount_kopeks / 100

    # Validate amount
    if amount_kopeks < settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS:
        await callback.answer(
            texts.t('AMOUNT_TOO_LOW_SHORT', 'Сумма слишком мала'),
            show_alert=True,
        )
        return

    if amount_kopeks > settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS:
        await callback.answer(
            texts.t('AMOUNT_TOO_HIGH_SHORT', 'Сумма слишком велика'),
            show_alert=True,
        )
        return

    await callback.answer()

    # Create payment
    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description=settings.CLOUDPAYMENTS_DESCRIPTION,
    )

    result = await payment_service.create_cloudpayments_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        telegram_id=db_user.telegram_id,
        language=db_user.language,
    )

    if not result:
        await callback.message.edit_text(
            texts.t(
                'PAYMENT_CREATE_ERROR',
                'Не удалось создать платёж. Попробуйте позже.',
            ),
            reply_markup=get_back_keyboard(db_user.language),
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

    await callback.message.edit_text(
        texts.t(
            'CLOUDPAYMENTS_PAYMENT_CREATED',
            '💳 <b>Оплата банковской картой</b>\n\n'
            'Сумма: <b>{amount}₽</b>\n\n'
            'Нажмите кнопку ниже для оплаты.\n'
            'После успешной оплаты баланс будет пополнен автоматически.',
        ).format(amount=f'{amount_rub:.2f}'),
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    logger.info(
        'CloudPayments payment created (quick): user amount=₽', telegram_id=db_user.telegram_id, amount_rub=amount_rub
    )
