"""Handler for PayHot balance top-up (pay.hot)."""

import html

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
from app.services.payment_method_config_service import get_payhot_checkout_policy
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler
from app.utils.topup_retry import format_topup_amount_retry


logger = structlog.get_logger(__name__)


PAYHOT_SERVICE_MAP = {
    'payhot': None,
    **{f'payhot_{method}': method for method in ('card', 'sbp', 'sberpay', 'crypto', 'applepay', 'googlepay')},
}
PAYHOT_PAYMENT_METHODS = set(PAYHOT_SERVICE_MAP)


def _extract_service_type(payment_method: str) -> str | None:
    return PAYHOT_SERVICE_MAP.get(payment_method)


def _check_topup_restriction(db_user: User, texts) -> InlineKeyboardMarkup | None:
    """Проверяет ограничение на пополнение."""
    if not getattr(db_user, 'restriction_topup', False):
        return None

    keyboard = []
    support_url = settings.get_support_contact_url()
    if support_url:
        keyboard.append(
            [InlineKeyboardButton(text=texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать'), url=support_url)]
        )
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _display_name_for_method(payment_method: str, texts) -> str:
    label = {
        'sbp': texts.t('PAYMENT_UI_SBP_LABEL', 'СБП'),
        'card': texts.t('PAYMENT_UI_CARD_LABEL', 'Карта'),
        'sberpay': 'SberPay',
        'applepay': 'Apple Pay',
        'googlepay': 'Google Pay',
        'crypto': texts.t('PAYMENT_UI_CRYPTO_LABEL', 'Криптовалюта'),
    }.get(_extract_service_type(payment_method))
    return f'{settings.get_payhot_display_name()} — {label}' if label else settings.get_payhot_display_name()


async def _create_payhot_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
    payment_method_type: str | None = None,
    payment_phone: str | None = None,
):
    """Создаёт платёж PayHot и отправляет покупателю ссылку на страницу оплаты."""
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100

    payment_service = PaymentService()
    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    result = await payment_service.create_payhot_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        email=getattr(db_user, 'email', None),
        language=db_user.language,
        payment_method_type=payment_method_type,
        payment_phone=payment_phone,
    )

    if not result or not result.get('payment_url'):
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

    payment_url = result['payment_url']
    display_name = settings.get_payhot_display_name_html()

    pay_button_text = texts.t('PAYHOT_CHECKOUT_BUTTON')

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

    response_text = texts.t(
        'PAYHOT_PAYMENT_CREATED',
    ).format(name=display_name, amount=f'{amount_rub:.2f}')

    if edit_message:
        await message_or_callback.edit_text(response_text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await message_or_callback.answer(response_text, reply_markup=keyboard, parse_mode='HTML')

    logger.info('PayHot payment created', telegram_id=db_user.telegram_id, amount_rub=amount_rub)


@error_handler
async def process_payhot_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    """Обрабатывает сумму, введённую пользователем для PayHot."""
    texts = get_texts(db_user.language)

    restriction_kb = _check_topup_restriction(db_user, texts)
    if restriction_kb:
        reason = html.escape(
            getattr(db_user, 'restriction_reason', None)
            or texts.t('USER_RESTRICTION_DEFAULT_REASON', 'Действие ограничено администратором')
        )
        await message.answer(
            texts.t('USER_RESTRICTION_TOPUP_BLOCKED_SHORT', '🚫 <b>Пополнение ограничено</b>\n\n{reason}').format(
                reason=reason
            ),
            parse_mode='HTML',
            reply_markup=restriction_kb,
        )
        await state.clear()
        return

    policy = await get_payhot_checkout_policy(db, db_user)
    data = await state.get_data()
    payment_method = data.get('payment_method', 'payhot')
    option = _extract_service_type(payment_method)
    if policy is None or (option and option not in {item['id'] for item in policy['options']}):
        await message.answer(texts.t('PAYMENT_UNAVAILABLE', 'Способ оплаты недоступен'))
        await state.clear()
        return
    min_amount = policy['min_amount_kopeks']
    max_amount = policy['max_amount_kopeks']

    if amount_kopeks < min_amount:
        await message.answer(
            format_topup_amount_retry(db_user.language, min_amount, max_amount, too_low=True),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
        )
        return

    if amount_kopeks > max_amount:
        await message.answer(
            format_topup_amount_retry(db_user.language, min_amount, max_amount, too_low=False),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
        )
        return

    data = await state.get_data()
    payment_method = data.get('payment_method', 'payhot')
    payment_method_type = _extract_service_type(payment_method)

    payment_method_type = payment_method_type or policy['options'][0]['id']
    if payment_method_type == 'sberpay':
        await state.update_data(payhot_amount_kopeks=amount_kopeks, payment_method='payhot_sberpay')
        await state.set_state(BalanceStates.waiting_for_payhot_phone)
        await message.answer(
            texts.t('PAYHOT_ENTER_PHONE'),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
        )
        return
    await state.clear()

    await _create_payhot_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
        payment_method_type=payment_method_type,
    )


async def _start_payhot_topup_impl(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    payment_method: str,
    db: AsyncSession,
):
    """Стартует FSM ввода суммы для PayHot."""
    texts = get_texts(db_user.language)
    policy = await get_payhot_checkout_policy(db, db_user)
    option = _extract_service_type(payment_method)
    if policy is None or (option and option not in {item['id'] for item in policy['options']}):
        await callback.answer(texts.t('PAYMENT_UNAVAILABLE', 'Способ оплаты недоступен'), show_alert=True)
        return

    restriction_kb = _check_topup_restriction(db_user, texts)
    if restriction_kb:
        reason = html.escape(
            getattr(db_user, 'restriction_reason', None)
            or texts.t('USER_RESTRICTION_DEFAULT_REASON', 'Действие ограничено администратором')
        )
        await callback.message.edit_text(
            texts.t('USER_RESTRICTION_TOPUP_BLOCKED_SHORT', '🚫 <b>Пополнение ограничено</b>\n\n{reason}').format(
                reason=reason
            ),
            parse_mode='HTML',
            reply_markup=restriction_kb,
        )
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method=payment_method)

    min_amount = policy['min_amount_kopeks'] / 100
    max_amount = policy['max_amount_kopeks'] / 100

    display_name = html.escape(_display_name_for_method(payment_method, texts))

    keyboard = await get_topup_amount_keyboard(payment_method, db_user.language)

    await callback.message.edit_text(
        texts.t(
            'PAYHOT_ENTER_AMOUNT',
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
async def start_payhot_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await _start_payhot_topup_impl(callback, db_user, state, 'payhot', db)


@error_handler
async def start_payhot_card_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await _start_payhot_topup_impl(callback, db_user, state, 'payhot_card', db)


@error_handler
async def start_payhot_sbp_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    await _start_payhot_topup_impl(callback, db_user, state, 'payhot_sbp', db)


@error_handler
async def start_payhot_selected_topup(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    method = callback.data.removeprefix('topup_')
    await _start_payhot_topup_impl(callback, db_user, state, method, db)


@error_handler
async def process_payhot_phone(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    import re

    texts = get_texts(db_user.language)
    phone = (message.text or '').strip()
    if not re.fullmatch(r'\+7[0-9]{10}', phone):
        await message.answer(texts.t('PAYHOT_ENTER_PHONE'))
        return
    data = await state.get_data()
    amount = data.get('payhot_amount_kopeks')
    policy = await get_payhot_checkout_policy(db, db_user)
    if (
        policy is None
        or 'sberpay' not in {item['id'] for item in policy['options']}
        or type(amount) is not int
        or not policy['min_amount_kopeks'] <= amount <= policy['max_amount_kopeks']
    ):
        await state.clear()
        await message.answer(texts.t('PAYMENT_UNAVAILABLE'))
        return
    await state.clear()
    await _create_payhot_payment_and_respond(
        message, db_user, db, amount, payment_method_type='sberpay', payment_phone=phone
    )
