from datetime import UTC, datetime

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def start_heleket_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
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
                    types.InlineKeyboardButton(
                        text=texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать'),
                        url=support_url,
                    )
                ]
            )
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            texts.t(
                'USER_RESTRICTION_TOPUP_BLOCKED',
                '🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.',
            ).format(reason=reason),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    if not settings.is_heleket_enabled():
        await callback.answer(
            texts.t('HELEKET_NOT_AVAILABLE', '❌ Оплата через Heleket недоступна'),
            show_alert=True,
        )
        return

    markup = settings.get_heleket_markup_percent()
    markup_text: str | None
    if markup > 0:
        label = texts.t('PAYMENT_HELEKET_MARKUP_LABEL', 'Наценка провайдера')
        markup_text = f'{label}: {markup:.0f}%'
    elif markup < 0:
        label = texts.t('PAYMENT_HELEKET_DISCOUNT_LABEL', 'Скидка провайдера')
        markup_text = f'{label}: {abs(markup):.0f}%'
    else:
        markup_text = None

    message_text = texts.t(
        'HELEKET_TOPUP_PROMPT',
        '🪙 <b>Пополнение через Heleket</b>\n\n'
        'Введите сумму пополнения от 100 до 100,000 ₽:\n\n'
        '⚡ Мгновенное зачисление\n'
        '🔒 Безопасная оплата{markup_line}',
    ).format(markup_line=(f'\n\n{markup_text}' if markup_text else ''))

    keyboard = get_back_keyboard(db_user.language)

    if settings.is_quick_amount_buttons_enabled():
        from .main import get_quick_amount_buttons

        quick_buttons = await get_quick_amount_buttons(db_user.language, db_user)
        if quick_buttons:
            keyboard.inline_keyboard = quick_buttons + keyboard.inline_keyboard

    await callback.message.edit_text(
        message_text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method='heleket',
        heleket_prompt_message_id=callback.message.message_id,
        heleket_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_heleket_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
) -> None:
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
                    types.InlineKeyboardButton(
                        text=texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать'),
                        url=support_url,
                    )
                ]
            )
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await message.answer(
            texts.t(
                'USER_RESTRICTION_TOPUP_BLOCKED',
                '🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.',
            ).format(reason=reason),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await state.clear()
        return

    if not settings.is_heleket_enabled():
        await message.answer(
            texts.t('HELEKET_NOT_AVAILABLE', '❌ Оплата через Heleket недоступна'),
        )
        return

    amount_rubles = amount_kopeks / 100

    if amount_rubles < 100:
        await message.answer(
            texts.t('AMOUNT_TOO_LOW', 'Минимальная сумма пополнения: {min_amount:.0f}₽').format(min_amount=100),
        )
        return

    if amount_rubles > 100000:
        await message.answer(
            texts.t('AMOUNT_TOO_HIGH', 'Максимальная сумма пополнения: {max_amount:,.0f}₽').format(max_amount=100000),
        )
        return

    payment_service = PaymentService(message.bot)

    result = await payment_service.create_heleket_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=texts.t(
            'HELEKET_INVOICE_DESCRIPTION_TOPUP',
            'Пополнение баланса на {amount_rub:.0f} ₽',
        ).format(amount_rub=amount_rubles),
        language=db_user.language,
    )

    if not result:
        await message.answer(
            texts.t(
                'HELEKET_CREATE_PAYMENT_ERROR',
                '❌ Не удалось создать счёт в Heleket. Попробуйте позже или обратитесь в поддержку.',
            ),
        )
        await state.clear()
        return

    payment_url = result.get('payment_url')
    if not payment_url:
        await message.answer(
            texts.t('HELEKET_PAYMENT_LINK_ERROR', '❌ Не удалось получить ссылку для оплаты Heleket'),
        )
        await state.clear()
        return

    payer_amount = result.get('payer_amount')
    payer_currency = result.get('payer_currency')
    result.get('exchange_rate')
    discount_percent = result.get('discount_percent')

    details = [
        texts.t('HELEKET_INVOICE_TITLE', '🪙 <b>Оплата через Heleket</b>'),
        '',
        texts.t(
            'HELEKET_INVOICE_TOPUP_AMOUNT',
            '💰 Сумма к зачислению: {amount_rub:.0f} ₽',
        ).format(amount_rub=amount_rubles),
    ]

    if payer_amount and payer_currency:
        details.append(
            texts.t(
                'HELEKET_INVOICE_PAYER_AMOUNT',
                '🪙 К оплате: {payer_amount} {payer_currency}',
            ).format(payer_amount=payer_amount, payer_currency=payer_currency)
        )

    markup_percent: float | None = None
    if discount_percent is not None:
        try:
            discount_int = int(discount_percent)
            markup_percent = -discount_int
        except (TypeError, ValueError):
            markup_percent = None

    if markup_percent:
        label_markup = texts.t('PAYMENT_HELEKET_MARKUP_LABEL', 'Наценка провайдера')
        label_discount = texts.t('PAYMENT_HELEKET_DISCOUNT_LABEL', 'Скидка провайдера')
        absolute = abs(markup_percent)
        if markup_percent > 0:
            details.append(
                texts.t(
                    'HELEKET_INVOICE_MARKUP_LINE',
                    '📈 {label}: +{value}%',
                ).format(label=label_markup, value=absolute)
            )
        else:
            details.append(
                texts.t(
                    'HELEKET_INVOICE_DISCOUNT_LINE',
                    '📉 {label}: {value}%',
                ).format(label=label_discount, value=absolute)
            )

    if payer_amount and payer_currency:
        try:
            payer_amount_float = float(payer_amount)
            if payer_amount_float > 0:
                rub_per_currency = amount_rubles / payer_amount_float
                details.append(
                    texts.t(
                        'HELEKET_INVOICE_RATE_LINE',
                        '💱 Курс: 1 {currency} ≈ {rate:.2f} ₽',
                    ).format(currency=payer_currency, rate=rub_per_currency)
                )
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    details.extend(
        [
            '',
            texts.t('HELEKET_INVOICE_INSTRUCTION_TITLE', '📱 Инструкция:'),
            texts.t('HELEKET_INVOICE_INSTRUCTION_STEP_1', "1. Нажмите кнопку 'Оплатить'"),
            texts.t('HELEKET_INVOICE_INSTRUCTION_STEP_2', '2. Перейдите на страницу Heleket'),
            texts.t('HELEKET_INVOICE_INSTRUCTION_STEP_3', '3. Оплатите указанную сумму'),
            texts.t('HELEKET_INVOICE_INSTRUCTION_STEP_4', '4. Баланс пополнится автоматически'),
        ]
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.t('PAY_WITH_COINS_BUTTON', '🪙 Оплатить'), url=payment_url)],
            [
                types.InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_heleket_{result["local_payment_id"]}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
        ]
    )

    state_data = await state.get_data()
    prompt_message_id = state_data.get('heleket_prompt_message_id')
    prompt_chat_id = state_data.get('heleket_prompt_chat_id', message.chat.id)

    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover - depends on bot rights
        logger.warning('Не удалось удалить сообщение с суммой Heleket', delete_error=delete_error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover - diagnostic
            logger.warning('Не удалось удалить сообщение с запросом суммы Heleket', delete_error=delete_error)

    invoice_message = await message.answer('\n'.join(details), parse_mode='HTML', reply_markup=keyboard)

    try:
        from app.services import payment_service as payment_module

        payment = await payment_module.get_heleket_payment_by_id(db, result['local_payment_id'])
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
    except Exception as error:  # pragma: no cover - diagnostics
        logger.warning('Не удалось сохранить сообщение Heleket', error=error)

    await state.update_data(
        heleket_invoice_message_id=invoice_message.message_id,
        heleket_invoice_chat_id=invoice_message.chat.id,
    )

    await state.clear()


@error_handler
async def check_heleket_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
) -> None:
    try:
        local_payment_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        user = callback.from_user
        language = getattr(user, 'language_code', 'ru') if user else 'ru'
        texts = get_texts(language)
        await callback.answer(
            texts.t('HELEKET_INVALID_PAYMENT_ID', 'Некорректный идентификатор платежа'),
            show_alert=True,
        )
        return

    from app.database.crud.heleket import get_heleket_payment_by_id

    payment = await get_heleket_payment_by_id(db, local_payment_id)
    if not payment:
        user = callback.from_user
        language = getattr(user, 'language_code', 'ru') if user else 'ru'
        texts = get_texts(language)
        await callback.answer(
            texts.t('ADMIN_PAYMENT_NOT_FOUND', 'Платёж не найден.'),
            show_alert=True,
        )
        return

    language = getattr(payment.user, 'language', None) or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)

    if payment.is_paid:
        message = texts.t('HELEKET_PAYMENT_ALREADY_PAID', '✅ Платёж уже зачислен')
        await callback.answer(message, show_alert=True)
        return

    payment_service = PaymentService(callback.bot)
    updated_payment = await payment_service.sync_heleket_payment_status(
        db,
        local_payment_id=local_payment_id,
    )

    if updated_payment:
        payment = updated_payment

    if payment.is_paid:
        message = texts.t('HELEKET_PAYMENT_SUCCESS', '✅ Платёж зачислен на баланс')
        await callback.answer(message, show_alert=True)
        return

    status_normalized = (payment.status or '').lower()
    status_messages = {
        'check': texts.t('HELEKET_STATUS_CHECK', '⏳ Ожидание оплаты'),
        'process': texts.t('HELEKET_STATUS_PROCESS', '⚙️ Платёж обрабатывается'),
        'confirm_check': texts.t('HELEKET_STATUS_CONFIRM_CHECK', '⛓ Ожидание подтверждений сети'),
        'wrong_amount': texts.t('HELEKET_STATUS_WRONG_AMOUNT', '❗️ Оплачена неверная сумма'),
        'wrong_amount_waiting': texts.t(
            'HELEKET_STATUS_WRONG_AMOUNT_WAITING',
            '❗️ Недостаточная сумма, ожидаем доплату',
        ),
        'paid_over': texts.t('HELEKET_STATUS_PAID_OVER', '✅ Платёж зачислен (с переплатой)'),
        'paid': texts.t('HELEKET_STATUS_PAID', '✅ Платёж зачислен'),
        'cancel': texts.t('HELEKET_STATUS_CANCEL', '🚫 Платёж отменён'),
        'fail': texts.t('HELEKET_STATUS_FAIL', '❌ Ошибка при оплате'),
        'system_fail': texts.t('HELEKET_STATUS_SYSTEM_FAIL', '❌ Системная ошибка Heleket'),
        'refund_process': texts.t('HELEKET_STATUS_REFUND_PROCESS', '↩️ Возврат обрабатывается'),
        'refund_fail': texts.t('HELEKET_STATUS_REFUND_FAIL', '⚠️ Ошибка возврата'),
        'refund_paid': texts.t('HELEKET_STATUS_REFUND_PAID', '✅ Возврат выполнен'),
        'locked': texts.t('HELEKET_STATUS_LOCKED', '🔒 Средства заблокированы'),
    }

    message = status_messages.get(status_normalized)
    if message is None:
        template = texts.t('HELEKET_STATUS_UNKNOWN', 'ℹ️ Статус платежа: {status}')
        status_value = payment.status or status_normalized or '—'
        try:
            message = template.format(status=status_value)
        except Exception:  # pragma: no cover - defensive formatting
            message = template.replace('{status}', str(status_value))

    await callback.answer(message, show_alert=True)
