"""Handler for External Gateway balance top-up."""

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


async def _create_external_gateway_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
):
    """Создаёт платёж через внешний шлюз и отправляет ответ пользователю."""
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100
    display_name = settings.get_external_gateway_display_name()

    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    result = await payment_service.create_external_gateway_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
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

    redirect_url = result.get('redirect_url')
    local_payment_id = result.get('local_payment_id')

    # Клавиатура: кнопка оплаты + проверка статуса + назад
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'PAY_BUTTON',
                        '💳 Оплатить {amount}₽',
                    ).format(amount=f'{amount_rub:.0f}'),
                    url=redirect_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('CHECK_PAYMENT_STATUS', '🔄 Проверить оплату'),
                    callback_data=f'ext_gw_check|{local_payment_id}',
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
        'EXTERNAL_GATEWAY_PAYMENT_CREATED',
        '💳 <b>Оплата через {name}</b>\n\n'
        'Сумма: <b>{amount}₽</b>\n\n'
        'Нажмите кнопку ниже для оплаты.\n'
        'После успешной оплаты баланс будет пополнен автоматически.\n\n'
        'Если оплата не зачислилась — нажмите «Проверить оплату».',
    ).format(name=html.escape(display_name), amount=f'{amount_rub:.2f}')

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

    logger.info(
        'External Gateway payment created',
        telegram_id=db_user.telegram_id,
        amount_rub=amount_rub,
    )


@error_handler
async def process_external_gateway_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    """Обработка суммы платежа (вызывается из роутера или quick_amount)."""
    texts = get_texts(db_user.language)

    # Проверка ограничения
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

    # Валидация суммы
    min_amount = settings.EXTERNAL_GATEWAY_MIN_AMOUNT_KOPEKS
    max_amount = settings.EXTERNAL_GATEWAY_MAX_AMOUNT_KOPEKS

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

    await _create_external_gateway_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
    )


@error_handler
async def start_external_gateway_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Начало пополнения через внешний шлюз — запрос суммы."""
    texts = get_texts(db_user.language)

    if not settings.is_external_gateway_enabled():
        await callback.answer(
            texts.t('EXTERNAL_GATEWAY_NOT_AVAILABLE', 'Способ оплаты временно недоступен'),
            show_alert=True,
        )
        return

    # Проверка ограничения
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
    await state.update_data(payment_method='external_gateway')

    min_amount = settings.EXTERNAL_GATEWAY_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.EXTERNAL_GATEWAY_MAX_AMOUNT_KOPEKS // 100
    display_name = settings.get_external_gateway_display_name()

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
            'EXTERNAL_GATEWAY_ENTER_AMOUNT',
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
async def process_external_gateway_custom_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обработка произвольной суммы от пользователя."""
    data = await state.get_data()
    if data.get('payment_method') != 'external_gateway':
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

    await process_external_gateway_payment_amount(
        message=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        state=state,
    )


@error_handler
async def process_external_gateway_quick_amount(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Обработка быстрого выбора суммы."""
    texts = get_texts(db_user.language)

    if not settings.is_external_gateway_enabled():
        await callback.answer(
            texts.t('EXTERNAL_GATEWAY_NOT_AVAILABLE', 'Способ оплаты временно недоступен'),
            show_alert=True,
        )
        return

    # Извлекаем сумму из callback_data: topup_amount|external_gateway|{amount_kopeks}
    try:
        parts = callback.data.split('|')
        amount_kopeks = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer('Ошибка', show_alert=True)
        return

    await callback.answer()

    await _create_external_gateway_payment_and_respond(
        message_or_callback=callback.message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=True,
    )


@error_handler
async def check_external_gateway_status(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext | None = None,
):
    """Проверка статуса платежа по кнопке."""
    texts = get_texts(db_user.language)

    # Извлекаем local_payment_id из callback_data: ext_gw_check|{id}
    try:
        parts = callback.data.split('|')
        local_payment_id = int(parts[1])
    except (IndexError, ValueError):
        await callback.answer('Ошибка', show_alert=True)
        return

    payment_service = PaymentService()
    result = await payment_service.get_external_gateway_payment_status(db, local_payment_id)

    if not result:
        await callback.answer(
            texts.t('PAYMENT_NOT_FOUND', 'Платёж не найден'),
            show_alert=True,
        )
        return

    if result.get('is_paid'):
        await callback.answer(
            texts.t('PAYMENT_ALREADY_PROCESSED', '✅ Платёж уже обработан!'),
            show_alert=True,
        )
    else:
        status = result.get('status', 'pending')
        if status == 'pending':
            await callback.answer(
                texts.t('PAYMENT_STILL_PENDING', '⏳ Платёж ещё не оплачен. Попробуйте позже.'),
                show_alert=True,
            )
        elif status in ('failed', 'expired'):
            await callback.answer(
                texts.t('PAYMENT_FAILED', '❌ Платёж не удался или истёк.'),
                show_alert=True,
            )
        else:
            await callback.answer(
                texts.t('PAYMENT_PROCESSING', '⏳ Платёж обрабатывается...'),
                show_alert=True,
            )
