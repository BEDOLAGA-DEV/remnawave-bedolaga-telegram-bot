"""Handlers for Cashera balance interactions (по образцу Platega: выбор метода → сумма → счёт)."""

import html

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
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

METHOD_CALLBACK_PREFIX = 'cashera_method_'
DIRECT_CALLBACK_PREFIX = 'topup_cashera_m_'


def _get_active_methods() -> list[str]:
    return settings.get_cashera_active_methods()


def _restriction_keyboard(texts) -> types.InlineKeyboardMarkup:
    keyboard = []
    support_url = settings.get_support_contact_url()
    if support_url:
        keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _restriction_text(db_user: User) -> str:
    reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
    return f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.'


async def _prompt_amount(message: types.Message, db_user: User, state: FSMContext, method_code: str) -> None:
    texts = get_texts(db_user.language)
    method_name = settings.get_cashera_method_display_title(method_code)

    await state.update_data(payment_method='cashera', cashera_method=method_code)

    data = await state.get_data()
    pending_amount = int(data.get('cashera_pending_amount') or 0)
    if pending_amount > 0:
        # Сумма уже выбрана быстрой кнопкой — сразу создаём платёж.
        await state.update_data(cashera_pending_amount=None)
        await state.set_state(BalanceStates.waiting_for_amount)

        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await process_cashera_payment_amount(message, db_user, db, pending_amount, state)
        return

    min_amount_label = settings.format_price(settings.CASHERA_MIN_AMOUNT_KOPEKS)
    max_amount_label = settings.format_price(settings.CASHERA_MAX_AMOUNT_KOPEKS)
    keyboard = await get_topup_amount_keyboard('cashera', db_user.language, back_callback='back_to_menu')

    await message.edit_text(
        texts.t(
            'CASHERA_ENTER_AMOUNT',
            '💳 <b>Оплата через {name} ({method})</b>\n\nВведите сумму для пополнения от {min_amount} до {max_amount}.',
        ).format(
            name=settings.get_cashera_display_name_html(),
            method=method_name,
            min_amount=min_amount_label,
            max_amount=max_amount_label,
        ),
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(cashera_prompt_message_id=message.message_id, cashera_prompt_chat_id=message.chat.id)


async def _guard(callback: types.CallbackQuery, db_user: User) -> bool:
    """Общие проверки перед выбором метода: ограничение пополнения и включённость шлюза."""
    texts = get_texts(db_user.language)
    if getattr(db_user, 'restriction_topup', False):
        await callback.message.edit_text(_restriction_text(db_user), reply_markup=_restriction_keyboard(texts))
        await callback.answer()
        return False
    if not settings.is_cashera_enabled():
        await callback.answer(
            texts.t('CASHERA_TEMPORARILY_UNAVAILABLE', '❌ Оплата через Cashera временно недоступна'),
            show_alert=True,
        )
        return False
    return True


@error_handler
async def start_cashera_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    if not await _guard(callback, db_user):
        return

    texts = get_texts(db_user.language)
    active_methods = _get_active_methods()

    await state.update_data(payment_method='cashera')
    data = await state.get_data()
    has_pending_amount = bool(int(data.get('cashera_pending_amount') or 0))

    if len(active_methods) == 1:
        await _prompt_amount(callback.message, db_user, state, active_methods[0])
        await callback.answer()
        return

    buttons = [
        [
            types.InlineKeyboardButton(
                text=settings.get_cashera_method_display_title(code),
                callback_data=f'{METHOD_CALLBACK_PREFIX}{code}',
            )
        ]
        for code in active_methods
    ]
    buttons.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')])

    await callback.message.edit_text(
        texts.t('CASHERA_SELECT_PAYMENT_METHOD', 'Выберите способ оплаты {name}:').format(
            name=settings.get_cashera_display_name_html()
        ),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )
    if not has_pending_amount:
        await state.set_state(BalanceStates.waiting_for_cashera_method)
    await callback.answer()


@error_handler
async def handle_cashera_method_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    method_code = callback.data.removeprefix(METHOD_CALLBACK_PREFIX)
    if method_code not in _get_active_methods():
        await callback.answer('⚠️ Этот способ сейчас недоступен', show_alert=True)
        return
    await _prompt_amount(callback.message, db_user, state, method_code)
    await callback.answer()


@error_handler
async def start_cashera_direct_method(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """Метод выбран прямо на экране способов пополнения (CASHERA_INLINE_METHODS)."""
    method_code = callback.data.removeprefix(DIRECT_CALLBACK_PREFIX)
    if not await _guard(callback, db_user):
        return
    if method_code not in _get_active_methods():
        await callback.answer('⚠️ Этот способ сейчас недоступен', show_alert=True)
        return
    await _prompt_amount(callback.message, db_user, state, method_code)
    await callback.answer()


@error_handler
async def process_cashera_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if getattr(db_user, 'restriction_topup', False):
        await message.answer(
            _restriction_text(db_user),
            reply_markup=_restriction_keyboard(texts),
            parse_mode='HTML',
        )
        await state.clear()
        return

    if not settings.is_cashera_enabled():
        await message.answer(texts.t('CASHERA_TEMPORARILY_UNAVAILABLE', '❌ Оплата через Cashera временно недоступна'))
        return

    data = await state.get_data()
    method_code = str(data.get('cashera_method') or '')
    if method_code not in _get_active_methods():
        await message.answer(
            texts.t('CASHERA_METHOD_SELECTION_REQUIRED', '⚠️ Выберите способ оплаты перед вводом суммы')
        )
        await state.set_state(BalanceStates.waiting_for_cashera_method)
        return

    if amount_kopeks < settings.CASHERA_MIN_AMOUNT_KOPEKS:
        await message.answer(
            texts.t('PAYMENT_AMOUNT_TOO_LOW', 'Минимальная сумма пополнения: {min_amount}₽').format(
                min_amount=settings.CASHERA_MIN_AMOUNT_KOPEKS // 100
            ),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
        )
        await state.set_state(BalanceStates.waiting_for_amount)
        return

    if amount_kopeks > settings.CASHERA_MAX_AMOUNT_KOPEKS:
        await message.answer(
            texts.t('PAYMENT_AMOUNT_TOO_HIGH', 'Максимальная сумма пополнения: {max_amount}₽').format(
                max_amount=settings.CASHERA_MAX_AMOUNT_KOPEKS // 100
            ),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
        )
        await state.set_state(BalanceStates.waiting_for_amount)
        return

    try:
        payment_service = PaymentService(message.bot)
        result = await payment_service.create_cashera_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),
            language=db_user.language,
            payment_method_code=method_code,
        )
    except Exception as error:
        logger.exception('Ошибка создания платежа Cashera', error=error)
        result = None

    if not result or not result.get('payment_url'):
        await message.answer(
            texts.t(
                'CASHERA_PAYMENT_ERROR',
                '❌ Ошибка создания платежа Cashera. Попробуйте позже или обратитесь в поддержку.',
            )
        )
        await state.clear()
        return

    method_title = settings.get_cashera_method_display_title(method_code)
    local_payment_id = result.get('local_payment_id')
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('CASHERA_PAY_BUTTON', '💳 Оплатить через {method}').format(method=method_title),
                    url=result['payment_url'],
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_cashera_{local_payment_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
        ]
    )

    prompt_message_id = data.get('cashera_prompt_message_id')
    prompt_chat_id = data.get('cashera_prompt_chat_id', message.chat.id)
    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover - зависит от прав бота
        logger.warning('Не удалось удалить сообщение с суммой Cashera', delete_error=delete_error)
    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover - диагностический лог
            logger.warning('Не удалось удалить сообщение с запросом суммы Cashera', delete_error=delete_error)

    caption = texts.t(
        'CASHERA_PAYMENT_CREATED',
        '💳 <b>Оплата через {name} ({method})</b>\n\n'
        'Сумма: <b>{amount}</b>\n\n'
        'Нажмите кнопку ниже для оплаты.\n'
        'После успешной оплаты баланс будет пополнен автоматически.',
    ).format(
        name=settings.get_cashera_display_name_html(),
        method=method_title,
        amount=settings.format_price(amount_kopeks),
    )

    # Свой экран оплаты: QR прямо в чате. Без реквизитов — обычная ссылка ниже.
    h2h = await PaymentService(message.bot).get_cashera_h2h(result.get('payment_id'), method_code)
    qr_photo = _render_qr(h2h['qr']) if h2h else None
    if qr_photo is not None:
        await message.answer_photo(
            qr_photo,
            caption=caption
            + '\n\n'
            + texts.t('CASHERA_H2H_HINT', 'Отсканируйте QR-код в приложении банка или нажмите «Оплатить».'),
            reply_markup=keyboard,
            parse_mode='HTML',
        )
        await state.clear()
        return

    await message.answer(
        texts.t(
            'CASHERA_PAYMENT_CREATED',
            '💳 <b>Оплата через {name} ({method})</b>\n\n'
            'Сумма: <b>{amount}</b>\n\n'
            'Нажмите кнопку ниже для оплаты.\n'
            'После успешной оплаты баланс будет пополнен автоматически.',
        ).format(
            name=settings.get_cashera_display_name_html(),
            method=method_title,
            amount=settings.format_price(amount_kopeks),
        ),
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await state.clear()


def _render_qr(payload: str):
    """PNG с QR для строки СБП/ссылки; None — если не вышло (тогда покажем ссылку)."""
    try:
        from io import BytesIO

        import qrcode
        from aiogram.types import BufferedInputFile

        qr = qrcode.QRCode(version=None, box_size=10, border=4)
        qr.add_data(payload)
        qr.make(fit=True)
        buffer = BytesIO()
        qr.make_image(fill_color='black', back_color='white').save(buffer, format='PNG')
        return BufferedInputFile(buffer.getvalue(), filename='cashera_qr.png')
    except Exception as error:  # pragma: no cover - зависит от окружения
        logger.warning('Не удалось отрисовать QR Cashera', error=str(error))
        return None


@error_handler
async def check_cashera_payment_status(callback: types.CallbackQuery, db: AsyncSession):
    try:
        local_payment_id = int(callback.data.rsplit('_', 1)[-1])
    except ValueError:
        await callback.answer('❌ Некорректный идентификатор платежа', show_alert=True)
        return

    try:
        status_info = await PaymentService(callback.bot).get_cashera_payment_status(db, local_payment_id)
    except Exception as error:
        logger.exception('Ошибка проверки статуса Cashera', error=error)
        await callback.answer('⚠️ Ошибка проверки статуса', show_alert=True)
        return

    if not status_info:
        await callback.answer('⚠️ Платёж не найден', show_alert=True)
        return

    payment = status_info.get('payment')
    user = getattr(payment, 'user', None)
    texts = get_texts(getattr(user, 'language', None) or 'ru')

    if status_info.get('is_paid'):
        await callback.answer(texts.t('CASHERA_PAYMENT_ALREADY_CONFIRMED', '✅ Платёж уже зачислен'), show_alert=True)
        return

    status_labels = {
        'pending': texts.t('CASHERA_STATUS_PENDING', '⏳ Ожидает оплаты'),
        'failed': texts.t('CASHERA_STATUS_FAILED', '❌ Оплата не прошла'),
        'expired': texts.t('CASHERA_STATUS_EXPIRED', '⌛ Срок оплаты истёк'),
        'refunded': texts.t('CASHERA_STATUS_REFUNDED', '↩️ Возврат'),
        'chargeback': texts.t('CASHERA_STATUS_REFUNDED', '↩️ Возврат'),
    }
    status = status_info.get('status') or 'pending'
    await callback.answer(status_labels.get(status, status), show_alert=True)
