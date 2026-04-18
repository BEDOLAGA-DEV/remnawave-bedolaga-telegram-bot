"""Handler for AuraPay balance top-up."""

import html

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


def _check_topup_restriction(db_user: User, texts) -> InlineKeyboardMarkup | None:
    """Проверяет ограничение на пополнение. Возвращает клавиатуру если ограничен, иначе None."""
    if not getattr(db_user, 'restriction_topup', False):
        return None

    keyboard = []
    support_url = settings.get_support_contact_url()
    if support_url:
        keyboard.append([InlineKeyboardButton(text='\U0001f198 Обжаловать', url=support_url)])
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _create_aurapay_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
):
    """
    Common logic for creating AuraPay payment and sending response.
    """
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100

    # Create payment
    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    result = await payment_service.create_aurapay_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        email=getattr(db_user, 'email', None),
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
    display_name = settings.get_aurapay_display_name()

    # Create keyboard with payment button
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'PAY_BUTTON',
                        '\U0001f4b3 Оплатить {amount}\u20bd',
                    ).format(amount=f'{amount_rub:.0f}'),
                    url=payment_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_BUTTON', '\u25c0\ufe0f Назад'),
                    callback_data='menu_balance',
                )
            ],
        ]
    )

    response_text = texts.t(
        'AURAPAY_PAYMENT_CREATED',
        '\U0001f4b3 <b>Оплата через {name}</b>\n\n'
        'Сумма: <b>{amount}\u20bd</b>\n\n'
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

    logger.info('AuraPay payment created', telegram_id=db_user.telegram_id, amount_rub=amount_rub)


@error_handler
async def process_aurapay_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    """
    Process payment amount directly.
    """
    texts = get_texts(db_user.language)

    restriction_kb = _check_topup_restriction(db_user, texts)
    if restriction_kb:
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        await message.answer(
            f'\U0001f6ab <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=restriction_kb,
        )
        await state.clear()
        return

    # Validate amount
    min_amount = settings.AURAPAY_MIN_AMOUNT_KOPEKS
    max_amount = settings.AURAPAY_MAX_AMOUNT_KOPEKS

    if amount_kopeks < min_amount:
        await message.answer(
            texts.t(
                'PAYMENT_AMOUNT_TOO_LOW',
                'Минимальная сумма пополнения: {min_amount}\u20bd',
            ).format(min_amount=min_amount // 100),
            reply_markup=get_back_keyboard(db_user.language),
            parse_mode='HTML',
        )
        return

    if amount_kopeks > max_amount:
        await message.answer(
            texts.t(
                'PAYMENT_AMOUNT_TOO_HIGH',
                'Максимальная сумма пополнения: {max_amount}\u20bd',
            ).format(max_amount=max_amount // 100),
            reply_markup=get_back_keyboard(db_user.language),
            parse_mode='HTML',
        )
        return

    await state.clear()

    await _create_aurapay_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
    )


@error_handler
async def start_aurapay_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """
    Start AuraPay top-up process - ask for amount.
    """
    texts = get_texts(db_user.language)

    restriction_kb = _check_topup_restriction(db_user, texts)
    if restriction_kb:
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        await callback.message.edit_text(
            f'\U0001f6ab <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=restriction_kb,
        )
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='aurapay')

    min_amount = settings.AURAPAY_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.AURAPAY_MAX_AMOUNT_KOPEKS // 100
    display_name = settings.get_aurapay_display_name()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_BUTTON', '\u25c0\ufe0f Назад'),
                    callback_data='menu_balance',
                )
            ]
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'AURAPAY_ENTER_AMOUNT',
            '\U0001f4b3 <b>Пополнение через {name}</b>\n\n'
            'Введите сумму пополнения в рублях.\n\n'
            'Минимум: {min_amount}\u20bd\n'
            'Максимум: {max_amount}\u20bd',
        ).format(
            name=display_name,
            min_amount=min_amount,
            max_amount=f'{max_amount:,}'.replace(',', ' '),
        ),
        parse_mode='HTML',
        reply_markup=keyboard,
    )
