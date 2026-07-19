import html
import math

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.keyboards.topup_amounts import get_topup_amount_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

OVERPAY_PAYMENT_METHODS = {'overpay', 'overpay_fps', 'overpay_card', 'overpay_int'}

OVERPAY_OPTION_MAP: dict[str, str | None] = {
    'overpay': None,
    'overpay_fps': 'fps',
    'overpay_card': 'card',
    'overpay_int': 'int',
}


def _extract_option(payment_method: str) -> str | None:
    return OVERPAY_OPTION_MAP.get(payment_method)


def _available_options(texts) -> list[tuple[str, str]]:
    options = [
        ('overpay_fps', texts.t('OVERPAY_OPTION_SBP', '\U0001f4f1 СБП')),
        ('overpay_card', texts.t('OVERPAY_OPTION_CARD', '\U0001f4b3 Карта')),
    ]
    if settings.is_overpay_int_enabled():
        options.append(('overpay_int', texts.t('OVERPAY_OPTION_INT', '\U0001f30d Международная карта (EUR)')))
    return options


def _check_topup_restriction(db_user: User, texts) -> InlineKeyboardMarkup | None:
    if not getattr(db_user, 'restriction_topup', False):
        return None

    keyboard = []
    support_url = settings.get_support_contact_url()
    if support_url:
        keyboard.append([InlineKeyboardButton(text='\U0001f198 Обжаловать', url=support_url)])
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _create_overpay_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
    option: str | None = None,
):
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100

    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    result = await payment_service.create_overpay_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        email=getattr(db_user, 'email', None),
        language=db_user.language,
        option=option,
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
    display_name = settings.get_overpay_display_name()
    amount_eur = result.get('amount_eur')

    if amount_eur:
        pay_button_text = texts.t(
            'PAY_BUTTON_EUR',
            '\U0001f4b3 Оплатить {amount}€',
        ).format(amount=f'{amount_eur:.2f}')
        response_text = texts.t(
            'OVERPAY_INT_PAYMENT_CREATED',
            '\U0001f30d <b>Оплата через {name}</b>\n\n'
            'Сумма: <b>{amount}₽</b> (≈ <b>{amount_eur}€</b>)\n\n'
            'Оплата проходит в евро, баланс будет пополнен в рублях.\n'
            'Нажмите кнопку ниже для оплаты.',
        ).format(name=display_name, amount=f'{amount_rub:.2f}', amount_eur=f'{amount_eur:.2f}')
    else:
        pay_button_text = texts.t(
            'PAY_BUTTON',
            '\U0001f4b3 Оплатить {amount}₽',
        ).format(amount=f'{amount_rub:.0f}')
        response_text = texts.t(
            'OVERPAY_PAYMENT_CREATED',
            '\U0001f4b3 <b>Оплата через {name}</b>\n\n'
            'Сумма: <b>{amount}₽</b>\n\n'
            'Нажмите кнопку ниже для оплаты.\n'
            'После успешной оплаты баланс будет пополнен автоматически.',
        ).format(name=display_name, amount=f'{amount_rub:.2f}')

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=pay_button_text, url=payment_url)],
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_BUTTON', '◀️ Назад'),
                    callback_data='menu_balance',
                )
            ],
        ]
    )

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

    logger.info('Overpay payment created', telegram_id=db_user.telegram_id, amount_rub=amount_rub, option=option)


@error_handler
async def process_overpay_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
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

    min_amount = settings.OVERPAY_MIN_AMOUNT_KOPEKS
    max_amount = settings.OVERPAY_MAX_AMOUNT_KOPEKS

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
    payment_method = data.get('payment_method', 'overpay')
    option = _extract_option(payment_method)

    if option == 'int':
        if not settings.is_overpay_int_enabled():
            await message.answer(
                texts.t('OVERPAY_OPTION_UNAVAILABLE', 'Способ оплаты недоступен.'),
                reply_markup=get_back_keyboard(db_user.language),
                parse_mode='HTML',
            )
            await state.clear()
            return

        amount_eur = round(amount_kopeks / 100 / settings.OVERPAY_RUB_PER_EUR, 2)
        if amount_eur < settings.OVERPAY_INT_MIN_EUR:
            min_rub = math.ceil(settings.OVERPAY_INT_MIN_EUR * settings.OVERPAY_RUB_PER_EUR)
            await message.answer(
                texts.t(
                    'OVERPAY_INT_AMOUNT_TOO_LOW',
                    'Минимальная сумма для оплаты в евро: {min_eur}€ (≈ {min_rub}₽)',
                ).format(min_eur=f'{settings.OVERPAY_INT_MIN_EUR:g}', min_rub=min_rub),
                reply_markup=get_back_keyboard(db_user.language),
                parse_mode='HTML',
            )
            return

    await state.clear()

    await _create_overpay_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
        option=option,
    )


@error_handler
async def start_overpay_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
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

    await state.clear()

    options = _available_options(texts)
    display_name = settings.get_overpay_display_name()

    keyboard_rows = [[InlineKeyboardButton(text=label, callback_data=f'topup_{method}')] for method, label in options]
    keyboard_rows.append(
        [
            InlineKeyboardButton(
                text=texts.t('BACK_BUTTON', '◀️ Назад'),
                callback_data='menu_balance',
            )
        ]
    )

    await callback.message.edit_text(
        texts.t(
            'OVERPAY_SELECT_OPTION',
            '\U0001f4b3 <b>Пополнение через {name}</b>\n\nВыберите способ оплаты:',
        ).format(name=display_name),
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


async def _start_overpay_option_topup_impl(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    payment_method: str,
):
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

    option = _extract_option(payment_method)
    if option == 'int' and not settings.is_overpay_int_enabled():
        await callback.answer(texts.t('OVERPAY_OPTION_UNAVAILABLE', 'Способ оплаты недоступен.'), show_alert=True)
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method=payment_method)

    min_amount = settings.OVERPAY_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.OVERPAY_MAX_AMOUNT_KOPEKS // 100
    display_name = settings.get_overpay_display_name()

    min_int_kopeks = None
    if option == 'int':
        min_int_kopeks = math.ceil(settings.OVERPAY_INT_MIN_EUR * settings.OVERPAY_RUB_PER_EUR) * 100

    keyboard = await get_topup_amount_keyboard(
        payment_method,
        db_user.language,
        back_callback='topup_overpay',
        min_amount_kopeks=min_int_kopeks,
    )

    if option == 'int':
        min_eur_rub = math.ceil(settings.OVERPAY_INT_MIN_EUR * settings.OVERPAY_RUB_PER_EUR)
        text = texts.t(
            'OVERPAY_INT_ENTER_AMOUNT',
            '\U0001f30d <b>Пополнение через {name} (EUR)</b>\n\n'
            'Введите сумму пополнения в рублях.\n'
            'Оплата проходит в евро по курсу {rate}₽ за 1€, баланс пополняется в рублях.\n\n'
            'Минимум: {min_amount}₽\n'
            'Максимум: {max_amount}₽',
        ).format(
            name=display_name,
            rate=f'{settings.OVERPAY_RUB_PER_EUR:.2f}',
            min_amount=max(min_amount, min_eur_rub),
            max_amount=f'{max_amount:,}'.replace(',', ' '),
        )
    else:
        text = texts.t(
            'OVERPAY_ENTER_AMOUNT',
            '\U0001f4b3 <b>Пополнение через {name}</b>\n\n'
            'Введите сумму пополнения в рублях.\n\n'
            'Минимум: {min_amount}₽\n'
            'Максимум: {max_amount}₽',
        ).format(
            name=display_name,
            min_amount=min_amount,
            max_amount=f'{max_amount:,}'.replace(',', ' '),
        )

    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)


@error_handler
async def start_overpay_fps_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await _start_overpay_option_topup_impl(callback, db_user, state, 'overpay_fps')


@error_handler
async def start_overpay_card_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await _start_overpay_option_topup_impl(callback, db_user, state, 'overpay_card')


@error_handler
async def start_overpay_int_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await _start_overpay_option_topup_impl(callback, db_user, state, 'overpay_int')
