"""Handler for Donut balance top-up (Donut P2P)."""

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


DONUT_PAYMENT_METHODS = {'donut', 'donut_card', 'donut_sbp', 'donut_sbp_qr'}

DONUT_SERVICE_MAP: dict[str, str | None] = {
    'donut': None,
    'donut_card': 'card',
    'donut_sbp': 'sbp',
    'donut_sbp_qr': 'sbp_qr',
}


def _extract_service_type(payment_method: str) -> str | None:
    return DONUT_SERVICE_MAP.get(payment_method)


def _check_topup_restriction(db_user: User, texts) -> InlineKeyboardMarkup | None:
    """Проверяет ограничение на пополнение."""
    if not getattr(db_user, 'restriction_topup', False):
        return None

    keyboard = []
    support_url = settings.get_support_contact_url()
    if support_url:
        keyboard.append([InlineKeyboardButton(text='\U0001f198 Обжаловать', url=support_url)])
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _get_display_name(payment_method: str) -> str:
    if payment_method == 'donut_card':
        return settings.get_donut_card_display_name()
    if payment_method == 'donut_sbp':
        return settings.get_donut_sbp_display_name()
    if payment_method == 'donut_sbp_qr':
        return settings.get_donut_sbp_qr_display_name()
    return settings.get_donut_display_name()


async def _create_donut_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
    payment_method_type: str | None = None,
    display_name: str | None = None,
):
    """Создаёт платёж Donut и отправляет ссылку пользователю."""
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100

    payment_service = PaymentService()
    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    result = await payment_service.create_donut_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        email=getattr(db_user, 'email', None),
        language=db_user.language,
        payment_method_type=payment_method_type,
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
            await message_or_callback.answer(error_text, parse_mode='HTML')
        return

    payment_url = result.get('payment_url')
    name = display_name or settings.get_donut_display_name()

    pay_button_text = texts.t('PAY_BUTTON', '\U0001f4b3 Оплатить {amount}₽').format(
        amount=f'{amount_rub:.0f}',
    )

    keyboard_buttons: list[list[InlineKeyboardButton]] = []
    if payment_url:
        keyboard_buttons.append([InlineKeyboardButton(text=pay_button_text, url=payment_url)])
    keyboard_buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('BACK_BUTTON', '◀️ Назад'),
                callback_data='menu_balance',
            )
        ]
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    if payment_url:
        response_text = texts.t(
            'DONUT_PAYMENT_CREATED',
            '\U0001f4b3 <b>Оплата через {name}</b>\n\n'
            'Сумма: <b>{amount}₽</b>\n\n'
            'Нажмите кнопку ниже для перехода к оплате.\n'
            'После подтверждения платежа баланс будет пополнен автоматически.',
        ).format(name=name, amount=f'{amount_rub:.2f}')
    else:
        response_text = texts.t(
            'DONUT_PAYMENT_PROCESSING',
            '\U0001f4b3 <b>Платёж создан через {name}</b>\n\n'
            'Сумма: <b>{amount}₽</b>\n\n'
            'Платёж в обработке. Реквизиты будут отправлены отдельным сообщением.',
        ).format(name=name, amount=f'{amount_rub:.2f}')

    if edit_message:
        await message_or_callback.edit_text(response_text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await message_or_callback.answer(response_text, reply_markup=keyboard, parse_mode='HTML')

    logger.info('Donut payment created', telegram_id=db_user.telegram_id, amount_rub=amount_rub)


@error_handler
async def process_donut_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    """Обрабатывает сумму, введённую пользователем для Donut."""
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

    min_amount = settings.DONUT_MIN_AMOUNT_KOPEKS
    max_amount = settings.DONUT_MAX_AMOUNT_KOPEKS

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

    data = await state.get_data()
    payment_method = data.get('payment_method', 'donut')
    payment_method_type = _extract_service_type(payment_method)
    display_name = _get_display_name(payment_method)

    await state.clear()

    await _create_donut_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
        payment_method_type=payment_method_type,
        display_name=display_name,
    )


async def _start_donut_topup_impl(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    payment_method: str,
):
    """Стартует FSM ввода суммы для Donut."""
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
    await state.update_data(payment_method=payment_method)

    min_amount = settings.DONUT_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.DONUT_MAX_AMOUNT_KOPEKS // 100

    display_name = _get_display_name(payment_method)

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
            'DONUT_ENTER_AMOUNT',
            '\U0001f4b3 <b>Пополнение через {name}</b>\n\n'
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
async def start_donut_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await _start_donut_topup_impl(callback, db_user, state, 'donut')


@error_handler
async def start_donut_card_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await _start_donut_topup_impl(callback, db_user, state, 'donut_card')


@error_handler
async def start_donut_sbp_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await _start_donut_topup_impl(callback, db_user, state, 'donut_sbp')


@error_handler
async def start_donut_sbp_qr_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await _start_donut_topup_impl(callback, db_user, state, 'donut_sbp_qr')
