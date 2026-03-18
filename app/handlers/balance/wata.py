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
from app.services.payment_service import PaymentService, get_user_by_id as fetch_user_by_id
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def start_wata_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    if not settings.is_wata_enabled():
        await callback.answer('❌ Оплата через WATA временно недоступна', show_alert=True)
        return

    message_text = texts.t(
        'WATA_TOPUP_PROMPT',
        (
            '💳 <b>Оплата через WATA</b>\n\n'
            'Введите сумму пополнения. Минимальная сумма — {min_amount}, максимальная — {max_amount}.\n'
            'Оплата происходит через защищенную форму WATA.'
        ),
    ).format(
        min_amount=settings.format_price(settings.WATA_MIN_AMOUNT_KOPEKS),
        max_amount=settings.format_price(settings.WATA_MAX_AMOUNT_KOPEKS),
    )

    keyboard = get_back_keyboard(db_user.language)

    await callback.message.edit_text(
        message_text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method='wata',
        wata_prompt_message_id=callback.message.message_id,
        wata_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_wata_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await message.answer(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await state.clear()
        return

    if not settings.is_wata_enabled():
        await message.answer('❌ Оплата через WATA временно недоступна')
        return

    if amount_kopeks < settings.WATA_MIN_AMOUNT_KOPEKS:
        await message.answer(
            texts.t(
                'WATA_AMOUNT_TOO_LOW',
                'Минимальная сумма пополнения: {amount}',
            ).format(amount=settings.format_price(settings.WATA_MIN_AMOUNT_KOPEKS)),
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    if amount_kopeks > settings.WATA_MAX_AMOUNT_KOPEKS:
        await message.answer(
            texts.t(
                'WATA_AMOUNT_TOO_HIGH',
                'Максимальная сумма пополнения: {amount}',
            ).format(amount=settings.format_price(settings.WATA_MAX_AMOUNT_KOPEKS)),
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    payment_service = PaymentService(message.bot)

    try:
        result = await payment_service.create_wata_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),
            language=db_user.language,
        )
    except Exception as error:  # pragma: no cover - handled by decorator logs
        logger.exception('Ошибка создания WATA платежа', error=error)
        result = None

    if not result or not result.get('payment_url'):
        await message.answer(
            texts.t(
                'WATA_PAYMENT_ERROR',
                '❌ Ошибка создания платежа WATA. Попробуйте позже или обратитесь в поддержку.',
            )
        )
        await state.clear()
        return

    payment_url = result['payment_url']
    payment_link_id = result['payment_link_id']
    local_payment_id = result['local_payment_id']

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('WATA_PAY_BUTTON', '💳 Оплатить через WATA'),
                    url=payment_url,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_wata_{local_payment_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
        ]
    )

    message_template = texts.t(
        'WATA_PAYMENT_INSTRUCTIONS',
        (
            '💳 <b>Оплата через WATA</b>\n\n'
            '💰 Сумма: {amount}\n'
            '🆔 ID платежа: {payment_id}\n\n'
            '📱 <b>Инструкция:</b>\n'
            "1. Нажмите кнопку 'Оплатить через WATA'\n"
            '2. Следуйте подсказкам платежной системы\n'
            '3. Подтвердите перевод\n'
            '4. Средства зачислятся автоматически\n\n'
            '❓ Если возникнут проблемы, обратитесь в {support}'
        ),
    )

    message_text = message_template.format(
        amount=settings.format_price(amount_kopeks),
        payment_id=payment_link_id,
        support=settings.get_support_contact_display_html(),
    )

    state_data = await state.get_data()
    prompt_message_id = state_data.get('wata_prompt_message_id')
    prompt_chat_id = state_data.get('wata_prompt_chat_id', message.chat.id)

    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover - depends on bot rights
        logger.warning('Не удалось удалить сообщение с суммой WATA', delete_error=delete_error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover - diagnostic
            logger.warning('Не удалось удалить сообщение с запросом суммы WATA', delete_error=delete_error)

    invoice_message = await message.answer(
        message_text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    try:
        from app.services import payment_service as payment_module

        payment = await payment_module.get_wata_payment_by_local_id(db, local_payment_id)
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
        logger.warning('Не удалось сохранить сообщение WATA', error=error)

    await state.update_data(
        wata_invoice_message_id=invoice_message.message_id,
        wata_invoice_chat_id=invoice_message.chat.id,
    )

    await state.clear()

    logger.info(
        'Создан WATA платеж для пользователя : ₽, ссылка',
        telegram_id=db_user.telegram_id,
        amount_kopeks=amount_kopeks / 100,
        payment_link_id=payment_link_id,
    )


@error_handler
async def check_wata_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
):
    try:
        local_payment_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Некорректный идентификатор платежа', show_alert=True)
        return

    payment_service = PaymentService(callback.bot)
    status_info = await payment_service.get_wata_payment_status(db, local_payment_id)

    if not status_info:
        await callback.answer('❌ Платеж не найден', show_alert=True)
        return

    payment = status_info['payment']

    user_language = 'ru'
    try:
        user = await fetch_user_by_id(db, payment.user_id)
        if user and getattr(user, 'language', None):
            user_language = user.language
    except Exception as error:
        logger.debug('Не удалось получить пользователя для WATA статуса', error=error)

    texts = get_texts(user_language)

    status_labels: dict[str, dict[str, str]] = {
        'Opened': {'emoji': '⏳', 'label': texts.t('WATA_STATUS_OPENED', 'Ожидает оплаты')},
        'Closed': {'emoji': '⌛', 'label': texts.t('WATA_STATUS_CLOSED', 'Обрабатывается')},
        'Paid': {'emoji': '✅', 'label': texts.t('WATA_STATUS_PAID', 'Оплачен')},
        'Declined': {'emoji': '❌', 'label': texts.t('WATA_STATUS_DECLINED', 'Отклонен')},
    }

    label_info = status_labels.get(
        payment.status, {'emoji': '❓', 'label': texts.t('WATA_STATUS_UNKNOWN', 'Неизвестно')}
    )

    message_lines = [
        texts.t('WATA_STATUS_TITLE', '💳 <b>Статус платежа WATA</b>'),
        '',
        f'🆔 ID: {payment.payment_link_id}',
        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}',
        f'📊 Статус: {label_info["emoji"]} {label_info["label"]}',
        f'📅 Создан: {payment.created_at.strftime("%d.%m.%Y %H:%M") if payment.created_at else "—"}',
    ]

    if payment.is_paid:
        message_lines.append('\n✅ Платеж успешно завершен! Средства уже на балансе.')
    elif payment.status in {'Opened', 'Closed'}:
        message_lines.append('\n⏳ Платеж еще не завершен. Завершите оплату по ссылке и проверьте статус позже.')

    await callback.message.answer('\n'.join(message_lines), parse_mode='HTML')
    await callback.answer()
