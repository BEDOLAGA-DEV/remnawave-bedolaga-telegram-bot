import structlog
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.reply import get_receipt_email_keyboard, remove_keyboard
from app.localization.texts import get_texts
from app.utils.decorators import error_handler
from app.utils.validators import validate_email

logger = structlog.get_logger(__name__)


@error_handler
async def ask_receipt_email(message: types.Message, db_user: User, state: FSMContext):
    """Show email prompt for fiscal receipt before payment."""
    # Delete the user's amount message (cleanup from previous step)
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    # Also delete the "Enter amount" prompt message if stored in FSM
    state_data = await state.get_data()
    prompt_message_id = state_data.get('yookassa_prompt_message_id')
    prompt_chat_id = state_data.get('yookassa_prompt_chat_id', message.chat.id)
    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except TelegramBadRequest:
            pass

    await _send_receipt_prompt(message, db_user, state)


@error_handler
async def process_receipt_email(
    message: types.Message, db_user: User, db: AsyncSession, state: FSMContext
):
    """Handle email input for fiscal receipt."""
    texts = get_texts(db_user.language)
    if not message.text:
        await _replace_receipt_prompt(message, db_user, state, _get_prompt_text(db_user))
        return

    email = message.text.strip()

    if email == texts.CANCEL:
        await _cleanup_receipt_messages(message, state)
        await _return_to_previous_step(message, db_user, db, state)
        return

    # Validate email
    if not validate_email(email):
        invalid_text = texts.RECEIPT_INVALID_EMAIL.format(email=email[:30])
        await _replace_receipt_prompt(message, db_user, state, invalid_text)
        return

    normalized_email = email.lower()
    logger.info('Email для чека принят без сохранения в профиль', telegram_id=db_user.telegram_id, email=normalized_email)

    await _cleanup_receipt_messages(message, state)

    # Proceed to payment
    await _proceed_to_payment(message, db_user, db, state, receipt_email=normalized_email, skip_receipt=False)


async def _proceed_to_payment(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    receipt_email: str | None,
    skip_receipt: bool,
):
    """Resume payment flow after email collection (or skip)."""
    from app.config import settings
    from app.services.payment_service import PaymentService

    state_data = await state.get_data()
    payment_method = state_data.get('receipt_payment_method', 'yookassa')
    amount_kopeks = state_data.get('receipt_amount_kopeks', 0)
    texts = get_texts(db_user.language)

    if not amount_kopeks:
        logger.error('Нет суммы для платежа после ввода email', telegram_id=db_user.telegram_id)
        await message.answer(texts.RECEIPT_AMOUNT_MISSING)
        await state.clear()
        return

    await message.answer(texts.RECEIPT_PAYMENT_CREATING, reply_markup=remove_keyboard())

    if payment_method.startswith('simple_subscription_'):
        await _resume_simple_subscription_payment(
            message=message,
            db_user=db_user,
            db=db,
            state=state,
            payment_method=payment_method,
            amount_kopeks=amount_kopeks,
            receipt_email=receipt_email,
            skip_receipt=skip_receipt,
            texts=texts,
        )
        return

    if payment_method.startswith('trial_yookassa'):
        await _resume_trial_payment(
            message=message,
            db_user=db_user,
            db=db,
            state=state,
            payment_method=payment_method,
            amount_kopeks=amount_kopeks,
            receipt_email=receipt_email,
            skip_receipt=skip_receipt,
            texts=texts,
        )
        return

    try:
        payment_service = PaymentService(message.bot)

        metadata = {
            'user_telegram_id': str(db_user.telegram_id),
            'user_username': db_user.username or '',
            'purpose': f'balance_topup{"_sbp" if payment_method == "yookassa_sbp" else ""}',
        }

        if payment_method == 'yookassa_sbp':
            payment_result = await payment_service.create_yookassa_sbp_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=amount_kopeks,
                description=settings.get_balance_payment_description(
                    amount_kopeks, telegram_user_id=db_user.telegram_id
                ),
                receipt_email=receipt_email,
                receipt_phone=None,
                skip_receipt=skip_receipt,
                metadata=metadata,
            )
        else:
            payment_result = await payment_service.create_yookassa_payment(
                db=db,
                user_id=db_user.id,
                amount_kopeks=amount_kopeks,
                description=settings.get_balance_payment_description(
                    amount_kopeks, telegram_user_id=db_user.telegram_id
                ),
                receipt_email=receipt_email,
                receipt_phone=None,
                skip_receipt=skip_receipt,
                metadata=metadata,
            )

        if not payment_result:
            await message.answer(
                texts.RECEIPT_PAYMENT_ERROR,
                reply_markup=remove_keyboard(),
            )
            await state.clear()
            return

        if payment_method == 'yookassa_sbp':
            await _send_sbp_payment_message(message, db_user, db, state, payment_result, amount_kopeks, texts)
        else:
            await _send_card_payment_message(message, db_user, db, state, payment_result, amount_kopeks, texts)

    except Exception as e:
        logger.error('Ошибка создания YooKassa платежа после ввода email', error=e)
        await message.answer(
            texts.RECEIPT_PAYMENT_ERROR,
            reply_markup=remove_keyboard(),
        )
        await state.clear()


async def _cleanup_receipt_messages(message: types.Message, state: FSMContext):
    state_data = await state.get_data()
    prompt_msg_id = state_data.get('receipt_prompt_message_id')
    if prompt_msg_id:
        try:
            await message.bot.delete_message(message.chat.id, prompt_msg_id)
        except TelegramBadRequest:
            pass

    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def _return_to_previous_step(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    state_data = await state.get_data()
    payment_method = state_data.get('receipt_payment_method', 'yookassa')

    if payment_method in {'yookassa', 'yookassa_sbp'}:
        await _return_to_balance_amount_step(message, db_user, state, payment_method)
        return

    if payment_method.startswith('simple_subscription_'):
        from app.handlers.simple_subscription import _show_simple_subscription_payment_methods
        from app.states import SubscriptionStates

        await state.set_state(SubscriptionStates.waiting_for_simple_subscription_payment_method)
        await _show_simple_subscription_payment_methods(message, db_user, state, db, edit_message=False)
        return

    if payment_method.startswith('trial_yookassa'):
        from app.handlers.subscription.purchase import _show_paid_trial_payment_methods

        await _show_paid_trial_payment_methods(message, db_user, db, edit_message=False)
        await state.clear()
        return

    await state.clear()
    await message.answer(get_texts(db_user.language).CANCEL, reply_markup=remove_keyboard())


async def _return_to_balance_amount_step(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    payment_method: str,
):
    from app.config import settings
    from app.keyboards.inline import get_back_keyboard
    from app.states import BalanceStates

    texts = get_texts(db_user.language)
    min_amount_rub = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
    max_amount_rub = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100

    if payment_method == 'yookassa_sbp':
        message_text = (
            f'🏦 <b>Оплата через СБП</b>\n\n'
            f'Введите сумму для пополнения от {min_amount_rub:.0f} до {max_amount_rub:,.0f} рублей:'
        )
    else:
        message_text = (
            f'💳 <b>Оплата банковской картой</b>\n\n'
            f'Введите сумму для пополнения от {min_amount_rub:.0f} до {max_amount_rub:,.0f} рублей:'
        )

    prompt_message = await message.answer(
        message_text,
        reply_markup=get_back_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method=payment_method,
        yookassa_prompt_message_id=prompt_message.message_id,
        yookassa_prompt_chat_id=prompt_message.chat.id,
    )
    logger.info('Возврат на шаг ввода суммы YooKassa', telegram_id=db_user.telegram_id, payment_method=payment_method)


def _get_prompt_text(db_user: User) -> str:
    texts = get_texts(db_user.language)
    current_receipt_email = _get_current_receipt_email(db_user)
    if current_receipt_email:
        return texts.RECEIPT_EMAIL_PROMPT_WITH_CURRENT.format(email=current_receipt_email)
    return texts.RECEIPT_EMAIL_PROMPT


def _get_current_receipt_email(db_user: User) -> str | None:
    if db_user.email and db_user.email_verified:
        return db_user.email
    return None


async def _send_receipt_prompt(message: types.Message, db_user: User, state: FSMContext, prompt_text: str | None = None):
    prompt_msg = await message.answer(
        prompt_text or _get_prompt_text(db_user),
        reply_markup=get_receipt_email_keyboard(db_user.language, _get_current_receipt_email(db_user)),
    )
    await state.update_data(receipt_prompt_message_id=prompt_msg.message_id)


async def _replace_receipt_prompt(message: types.Message, db_user: User, state: FSMContext, prompt_text: str):
    state_data = await state.get_data()
    prompt_msg_id = state_data.get('receipt_prompt_message_id')
    if prompt_msg_id:
        try:
            await message.bot.delete_message(message.chat.id, prompt_msg_id)
        except TelegramBadRequest:
            pass

    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    await _send_receipt_prompt(message, db_user, state, prompt_text)


async def _send_card_payment_message(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    payment_result: dict,
    amount_kopeks: int,
    texts,
):
    """Send card payment message with confirmation URL."""
    from app.config import settings

    confirmation_url = payment_result.get('confirmation_url')
    if not confirmation_url:
        await message.answer(
            texts.RECEIPT_PAYMENT_LINK_ERROR,
            reply_markup=remove_keyboard(),
        )
        await state.clear()
        return

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.RECEIPT_PAY_CARD_BUTTON, url=confirmation_url)],
            [
                types.InlineKeyboardButton(
                    text=texts.CHECK_STATUS_BUTTON,
                    callback_data=f'check_yookassa_{payment_result["local_payment_id"]}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
        ]
    )

    invoice_message = await message.answer(
        texts.RECEIPT_CARD_INVOICE.format(
            amount=settings.format_price(amount_kopeks),
            payment_id=f'{payment_result["yookassa_payment_id"][:8]}...',
            support=settings.get_support_contact_display_html(),
        ),
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    # Save invoice message metadata
    await _save_payment_metadata(db, payment_result, invoice_message)

    await state.clear()
    logger.info(
        'Создан платеж YooKassa для пользователя (после ввода email)',
        telegram_id=db_user.telegram_id,
        value=amount_kopeks // 100,
        payment_id=payment_result['yookassa_payment_id'],
    )


async def _send_sbp_payment_message(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    payment_result: dict,
    amount_kopeks: int,
    texts,
):
    """Send SBP payment message with QR code and confirmation URL."""
    from app.config import settings

    confirmation_url = payment_result.get('confirmation_url')
    qr_confirmation_data = payment_result.get('qr_confirmation_data')

    if not confirmation_url and not qr_confirmation_data:
        await message.answer(
            texts.RECEIPT_SBP_DATA_ERROR,
            reply_markup=remove_keyboard(),
        )
        await state.clear()
        return

    # Generate QR code
    qr_photo = None
    qr_data = qr_confirmation_data or confirmation_url
    if qr_data:
        qr_photo = await _build_qr_photo(qr_data)

    # Build keyboard
    keyboard_buttons = []
    if confirmation_url:
        keyboard_buttons.append([types.InlineKeyboardButton(text=texts.RECEIPT_GO_TO_PAYMENT_BUTTON, url=confirmation_url)])

    keyboard_buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.CHECK_STATUS_BUTTON,
                callback_data=f'check_yookassa_{payment_result["local_payment_id"]}',
            )
        ]
    )
    keyboard_buttons.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')])
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    # Message text
    message_text = texts.RECEIPT_SBP_INVOICE.format(
        amount=settings.format_price(amount_kopeks),
        payment_id=f'{payment_result["yookassa_payment_id"][:8]}...',
        support=settings.get_support_contact_display_html(),
    )
    if not confirmation_url and not qr_photo:
        message_text = _append_sbp_manual_instructions(message_text, payment_result['yookassa_payment_id'])

    if qr_photo:
        invoice_message = await message.answer_photo(
            photo=qr_photo, caption=message_text, reply_markup=keyboard, parse_mode='HTML'
        )
    else:
        invoice_message = await message.answer(message_text, reply_markup=keyboard, parse_mode='HTML')

    # Save invoice message metadata
    await _save_payment_metadata(db, payment_result, invoice_message)

    await state.clear()
    logger.info(
        'Создан платеж YooKassa СБП для пользователя (после ввода email)',
        telegram_id=db_user.telegram_id,
        value=amount_kopeks // 100,
        payment_id=payment_result['yookassa_payment_id'],
    )


async def _resume_simple_subscription_payment(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    payment_method: str,
    amount_kopeks: int,
    receipt_email: str | None,
    skip_receipt: bool,
    texts,
):
    from app.config import settings
    from app.services.payment_service import PaymentService

    state_data = await state.get_data()
    order_id = state_data.get('receipt_order_id')
    subscription_params = state_data.get('receipt_subscription_params') or {}

    if not order_id or not subscription_params:
        await message.answer(texts.RECEIPT_PAYMENT_ERROR, reply_markup=remove_keyboard())
        await state.clear()
        return

    payment_service = PaymentService(message.bot)
    description = f'Оплата подписки на {subscription_params["period_days"]} дней'
    metadata = {
        'user_telegram_id': str(db_user.telegram_id),
        'user_username': db_user.username or '',
        'order_id': str(order_id),
        'subscription_id': str(order_id),
        'subscription_period': str(subscription_params['period_days']),
        'payment_purpose': 'simple_subscription_purchase',
    }

    if payment_method.endswith('sbp'):
        payment_result = await payment_service.create_yookassa_sbp_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=description,
            receipt_email=receipt_email,
            receipt_phone=None,
            skip_receipt=skip_receipt,
            metadata=metadata,
        )
    else:
        payment_result = await payment_service.create_yookassa_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=description,
            receipt_email=receipt_email,
            receipt_phone=None,
            skip_receipt=skip_receipt,
            metadata=metadata,
        )

    if not payment_result:
        await message.answer(texts.RECEIPT_PAYMENT_ERROR, reply_markup=remove_keyboard())
        await state.clear()
        return

    confirmation_url = payment_result.get('confirmation_url')
    qr_confirmation_data = payment_result.get('qr_confirmation_data')
    if not confirmation_url and not qr_confirmation_data:
        await message.answer(texts.RECEIPT_SBP_DATA_ERROR, reply_markup=remove_keyboard())
        await state.clear()
        return

    qr_photo = None
    qr_data = qr_confirmation_data or confirmation_url
    if qr_data:
        qr_photo = await _build_qr_photo(qr_data)

    keyboard_buttons = []
    if confirmation_url:
        keyboard_buttons.append([types.InlineKeyboardButton(text=texts.RECEIPT_GO_TO_PAYMENT_BUTTON, url=confirmation_url)])
    keyboard_buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.CHECK_STATUS_BUTTON,
                callback_data=f'check_yookassa_{payment_result["local_payment_id"]}',
            )
        ]
    )
    keyboard_buttons.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_purchase')])
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    show_devices = settings.is_devices_selection_enabled()
    devices_line = ''
    if show_devices:
        devices_line = f'\n📱 {texts.t("DEVICES", "Устройства")}: {subscription_params["device_limit"]}'

    traffic_gb = subscription_params['traffic_limit_gb']
    traffic_label = texts.t('UNLIMITED', 'Безлимит') if traffic_gb == 0 else f'{traffic_gb} ГБ'
    message_text = texts.t(
        'SIMPLE_SUBSCRIPTION_YOOKASSA_INVOICE',
        '💳 <b>Оплата подписки через YooKassa</b>\n\n'
        '📅 Период: {period_days} дней{devices_line}\n'
        '📊 Трафик: {traffic}\n'
        '💰 Сумма: {amount}\n'
        '🆔 ID платежа: {payment_id}\n\n'
        '🔒 Оплата происходит через защищенную систему YooKassa\n'
        '✅ Принимаем карты: Visa, MasterCard, МИР\n\n'
        '❓ Если возникнут проблемы, обратитесь в {support}',
    ).format(
        period_days=subscription_params['period_days'],
        devices_line=devices_line,
        traffic=traffic_label,
        amount=settings.format_price(amount_kopeks),
        payment_id=f'{payment_result["yookassa_payment_id"][:8]}...',
        support=settings.get_support_contact_display_html(),
    )
    if payment_method.endswith('sbp') and not confirmation_url and not qr_photo:
        message_text = _append_sbp_manual_instructions(message_text, payment_result['yookassa_payment_id'])

    if qr_photo:
        invoice_message = await message.answer_photo(
            photo=qr_photo,
            caption=message_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    else:
        invoice_message = await message.answer(message_text, reply_markup=keyboard, parse_mode='HTML')

    await _save_payment_metadata(db, payment_result, invoice_message)
    await state.clear()


async def _resume_trial_payment(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    payment_method: str,
    amount_kopeks: int,
    receipt_email: str | None,
    skip_receipt: bool,
    texts,
):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.config import settings
    from app.services.payment_service import PaymentService

    state_data = await state.get_data()
    pending_subscription_id = state_data.get('receipt_pending_subscription_id')
    trial_duration = state_data.get('receipt_trial_duration')

    if not pending_subscription_id or not trial_duration:
        await message.answer(texts.RECEIPT_PAYMENT_ERROR, reply_markup=remove_keyboard())
        await state.clear()
        return

    payment_service = PaymentService(message.bot)
    description = texts.t('PAID_TRIAL_PAYMENT_DESC', 'Пробная подписка на {days} дней').format(days=trial_duration)
    metadata = {
        'type': 'trial',
        'subscription_id': pending_subscription_id,
        'user_id': db_user.id,
    }

    if payment_method.endswith('sbp'):
        payment_result = await payment_service.create_yookassa_sbp_payment(
            db=db,
            amount_kopeks=amount_kopeks,
            description=description,
            user_id=db_user.id,
            receipt_email=receipt_email,
            receipt_phone=None,
            skip_receipt=skip_receipt,
            metadata=metadata,
        )

        if not payment_result:
            await message.answer(texts.RECEIPT_PAYMENT_ERROR, reply_markup=remove_keyboard())
            await state.clear()
            return

        confirmation_url = payment_result.get('confirmation_url')
        qr_confirmation_data = payment_result.get('qr_confirmation_data')
        qr_photo = await _build_qr_photo(qr_confirmation_data or confirmation_url)

        keyboard_rows = []
        if confirmation_url:
            keyboard_rows.append([InlineKeyboardButton(text='💳 Оплатить', url=confirmation_url)])
        keyboard_rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='trial_activate')])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        message_text = texts.t(
            'PAID_TRIAL_YOOKASSA_SBP',
            '🏦 <b>Оплата через СБП</b>\n\nОтсканируйте QR-код или перейдите по ссылке для оплаты.\n\n💰 Сумма: {amount}',
        ).format(amount=settings.format_price(amount_kopeks))
        if not confirmation_url and not qr_photo:
            message_text = _append_sbp_manual_instructions(message_text, payment_result['yookassa_payment_id'])

        if qr_photo:
            await message.answer_photo(
                photo=qr_photo,
                caption=message_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        else:
            await message.answer(message_text, reply_markup=keyboard, parse_mode='HTML')
    else:
        payment_result = await payment_service.create_yookassa_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=description,
            receipt_email=receipt_email,
            receipt_phone=None,
            skip_receipt=skip_receipt,
            metadata=metadata,
        )

        if not payment_result or not payment_result.get('confirmation_url'):
            await message.answer(texts.RECEIPT_PAYMENT_ERROR, reply_markup=remove_keyboard())
            await state.clear()
            return

        await message.answer(
            texts.t(
                'PAID_TRIAL_YOOKASSA_CARD',
                '💳 <b>Оплата картой</b>\n\nНажмите кнопку ниже для перехода к оплате.\n\n💰 Сумма: {amount}',
            ).format(amount=settings.format_price(amount_kopeks)),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text='💳 Оплатить', url=payment_result['confirmation_url'])],
                    [InlineKeyboardButton(text=texts.BACK, callback_data='trial_activate')],
                ]
            ),
            parse_mode='HTML',
        )

    await state.clear()


async def _build_qr_photo(qr_data: str | None):
    if not qr_data:
        return None

    try:
        from io import BytesIO

        import qrcode
        from aiogram.types import BufferedInputFile

        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        img_bytes = BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        return BufferedInputFile(img_bytes.getvalue(), filename='qrcode.png')
    except ImportError:
        logger.warning('qrcode библиотека не установлена, QR-код не будет сгенерирован')
        return None
    except Exception as error:
        logger.error('Ошибка генерации QR-кода', error=error)
        return None


def _append_sbp_manual_instructions(message_text: str, payment_id: str) -> str:
    return (
        f'{message_text}\n\n'
        '📱 <b>Инструкция по оплате:</b>\n'
        '1. Откройте приложение вашего банка\n'
        '2. Выберите оплату по СБП или по реквизитам\n'
        f'3. Используйте ID платежа: <code>{payment_id}</code>\n'
        '4. Подтвердите оплату в банковском приложении'
    )


async def _save_payment_metadata(db: AsyncSession, payment_result: dict, invoice_message: types.Message):
    """Save invoice message metadata to payment record."""
    try:
        from datetime import UTC, datetime

        from sqlalchemy import update

        from app.services import payment_service as payment_module

        payment = await payment_module.get_yookassa_payment_by_local_id(db, payment_result['local_payment_id'])
        if payment:
            metadata = dict(getattr(payment, 'metadata_json', {}) or {})
            metadata['invoice_message'] = {
                'chat_id': invoice_message.chat.id,
                'message_id': invoice_message.message_id,
            }
            await db.execute(
                update(payment.__class__)
                .where(payment.__class__.id == payment.id)
                .values(metadata_json=metadata, updated_at=datetime.now(UTC))
            )
            await db.commit()
    except Exception as error:  # pragma: no cover - диагностический лог
        logger.warning('Не удалось сохранить сообщение YooKassa', error=error)
