import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
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
async def start_mulenpay_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
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

    mulenpay_name = settings.get_mulenpay_display_name()
    mulenpay_name_html = settings.get_mulenpay_display_name_html()

    if not settings.is_mulenpay_enabled():
        await callback.answer(
            texts.t(
                'MULENPAY_NOT_AVAILABLE',
                '❌ Оплата через {mulenpay_name} временно недоступна',
            ).format(mulenpay_name=mulenpay_name),
            show_alert=True,
        )
        return

    message_template = texts.t(
        'MULENPAY_TOPUP_PROMPT',
        (
            '💳 <b>Оплата через {mulenpay_name_html}</b>\n\n'
            'Введите сумму для пополнения от 100 до 100 000 ₽.\n'
            'Оплата происходит через защищенную платформу {mulenpay_name}.'
        ),
    )
    message_text = message_template.format(
        mulenpay_name=mulenpay_name,
        mulenpay_name_html=mulenpay_name_html,
    )

    keyboard = get_back_keyboard(db_user.language)

    if settings.is_quick_amount_buttons_enabled():
        from .main import get_quick_amount_buttons

        quick_amount_buttons = await get_quick_amount_buttons(db_user.language, db_user)
        if quick_amount_buttons:
            keyboard.inline_keyboard = quick_amount_buttons + keyboard.inline_keyboard

    await callback.message.edit_text(
        message_text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method='mulenpay',
        mulenpay_prompt_message_id=callback.message.message_id,
        mulenpay_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_mulenpay_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
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

    mulenpay_name = settings.get_mulenpay_display_name()
    mulenpay_name_html = settings.get_mulenpay_display_name_html()

    if not settings.is_mulenpay_enabled():
        await message.answer(
            texts.t(
                'MULENPAY_NOT_AVAILABLE',
                '❌ Оплата через {mulenpay_name} временно недоступна',
            ).format(mulenpay_name=mulenpay_name)
        )
        return

    if amount_kopeks < settings.MULENPAY_MIN_AMOUNT_KOPEKS:
        await message.answer(
            texts.t(
                'PAYMENT_AMOUNT_TOO_LOW',
                'Минимальная сумма пополнения: {min_amount}₽',
            ).format(min_amount=settings.MULENPAY_MIN_AMOUNT_KOPEKS // 100)
        )
        return

    if amount_kopeks > settings.MULENPAY_MAX_AMOUNT_KOPEKS:
        await message.answer(
            texts.t(
                'PAYMENT_AMOUNT_TOO_HIGH',
                'Максимальная сумма пополнения: {max_amount}₽',
            ).format(max_amount=f'{settings.MULENPAY_MAX_AMOUNT_KOPEKS // 100:,}'.replace(',', ' '))
        )
        return

    amount_rubles = amount_kopeks / 100

    state_data = await state.get_data()
    prompt_message_id = state_data.get('mulenpay_prompt_message_id')
    prompt_chat_id = state_data.get('mulenpay_prompt_chat_id', message.chat.id)

    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover - depends on bot permissions
        logger.warning('Не удалось удалить сообщение с суммой MulenPay', delete_error=delete_error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover - diagnostic
            logger.warning('Не удалось удалить сообщение с запросом суммы MulenPay', delete_error=delete_error)

    try:
        payment_service = PaymentService(message.bot)
        payment_result = await payment_service.create_mulenpay_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),
            language=db_user.language,
        )

        if not payment_result or not payment_result.get('payment_url'):
            await message.answer(
                texts.t(
                    'MULENPAY_PAYMENT_ERROR',
                    '❌ Ошибка создания платежа {mulenpay_name}. Попробуйте позже или обратитесь в поддержку.',
                ).format(mulenpay_name=mulenpay_name)
            )
            await state.clear()
            return

        payment_url = payment_result.get('payment_url')
        mulen_payment_id = payment_result.get('mulen_payment_id')
        local_payment_id = payment_result.get('local_payment_id')

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'MULENPAY_PAY_BUTTON',
                            '💳 Оплатить через {mulenpay_name}',
                        ).format(mulenpay_name=mulenpay_name),
                        url=payment_url,
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                        callback_data=f'check_mulenpay_{local_payment_id}',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
            ]
        )

        payment_id_display = mulen_payment_id if mulen_payment_id is not None else local_payment_id

        message_template = texts.t(
            'MULENPAY_PAYMENT_INSTRUCTIONS',
            (
                '💳 <b>Оплата через {mulenpay_name_html}</b>\n\n'
                '💰 Сумма: {amount}\n'
                '🆔 ID платежа: {payment_id}\n\n'
                '📱 <b>Инструкция:</b>\n'
                "1. Нажмите кнопку 'Оплатить через {mulenpay_name}'\n"
                '2. Следуйте подсказкам платежной системы\n'
                '3. Подтвердите перевод\n'
                '4. Средства зачислятся автоматически\n\n'
                '❓ Если возникнут проблемы, обратитесь в {support}'
            ),
        )

        message_text = message_template.format(
            amount=settings.format_price(amount_kopeks),
            payment_id=payment_id_display,
            support=settings.get_support_contact_display_html(),
            mulenpay_name=mulenpay_name,
            mulenpay_name_html=mulenpay_name_html,
        )

        invoice_message = await message.answer(
            message_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        try:
            from app.services import payment_service as payment_module

            payment = await payment_module.get_mulenpay_payment_by_local_id(db, local_payment_id)
            if payment:
                payment_metadata = dict(getattr(payment, 'metadata_json', {}) or {})
                payment_metadata['invoice_message'] = {
                    'chat_id': invoice_message.chat.id,
                    'message_id': invoice_message.message_id,
                }
                await payment_module.update_mulenpay_payment_metadata(
                    db,
                    payment=payment,
                    metadata=payment_metadata,
                )
        except Exception as error:  # pragma: no cover - diagnostic logging only
            logger.warning('Не удалось сохранить данные сообщения MulenPay', error=error)

        await state.update_data(
            mulenpay_invoice_message_id=invoice_message.message_id,
            mulenpay_invoice_chat_id=invoice_message.chat.id,
        )

        await state.clear()

        logger.info(
            'Создан платеж для пользователя : ₽, ID',
            mulenpay_name=mulenpay_name,
            telegram_id=db_user.telegram_id,
            amount_rubles=amount_rubles,
            payment_id_display=payment_id_display,
        )

    except Exception as e:
        logger.error('Ошибка создания платежа', mulenpay_name=mulenpay_name, error=e)
        await message.answer(
            texts.t(
                'MULENPAY_PAYMENT_ERROR',
                '❌ Ошибка создания платежа {mulenpay_name}. Попробуйте позже или обратитесь в поддержку.',
            ).format(mulenpay_name=mulenpay_name)
        )
        await state.clear()


@error_handler
async def check_mulenpay_payment_status(callback: types.CallbackQuery, db: AsyncSession):
    language = (getattr(getattr(callback, 'from_user', None), 'language_code', None) or 'ru').split('-')[0]
    texts = get_texts(language)

    try:
        local_payment_id = int(callback.data.split('_')[-1])
        payment_service = PaymentService(callback.bot)
        status_info = await payment_service.get_mulenpay_payment_status(db, local_payment_id)

        if not status_info:
            await callback.answer(
                texts.t(
                    'MULENPAY_PAYMENT_NOT_FOUND_ALERT',
                    '❌ Платеж не найден',
                ),
                show_alert=True,
            )
            return

        payment = status_info['payment']

        status_labels = {
            'created': ('⏳', texts.t('MULENPAY_STATUS_CREATED', 'Ожидает оплаты')),
            'processing': ('⌛', texts.t('MULENPAY_STATUS_PROCESSING', 'Обрабатывается')),
            'success': ('✅', texts.t('MULENPAY_STATUS_SUCCESS', 'Оплачен')),
            'canceled': ('❌', texts.t('MULENPAY_STATUS_CANCELED', 'Отменен')),
            'error': ('⚠️', texts.t('MULENPAY_STATUS_ERROR', 'Ошибка')),
            'hold': ('🔒', texts.t('MULENPAY_STATUS_HOLD', 'Холд')),
            'unknown': ('❓', texts.t('MULENPAY_STATUS_UNKNOWN', 'Неизвестно')),
        }

        emoji, status_text = status_labels.get(
            payment.status,
            ('❓', texts.t('MULENPAY_STATUS_UNKNOWN', 'Неизвестно')),
        )

        mulenpay_name = settings.get_mulenpay_display_name()
        message_lines = [
            texts.t(
                'MULENPAY_STATUS_HEADER',
                '💳 Статус платежа {mulenpay_name}:\n\n',
            ).format(mulenpay_name=mulenpay_name),
            texts.t('MULENPAY_STATUS_ID_LINE', '🆔 ID: {payment_id}\n').format(
                payment_id=payment.mulen_payment_id or payment.id
            ),
            texts.t('MULENPAY_STATUS_AMOUNT_LINE', '💰 Сумма: {amount}\n').format(
                amount=settings.format_price(payment.amount_kopeks)
            ),
            texts.t('MULENPAY_STATUS_STATE_LINE', '📊 Статус: {emoji} {status_text}\n').format(
                emoji=emoji,
                status_text=status_text,
            ),
            texts.t('MULENPAY_STATUS_CREATED_AT_LINE', '📅 Создан: {created_at}\n').format(
                created_at=payment.created_at.strftime('%d.%m.%Y %H:%M')
            ),
        ]

        if payment.is_paid:
            message_lines.append(
                texts.t(
                    'MULENPAY_STATUS_PAID_NOTE',
                    '\n✅ Платеж успешно завершен! Средства уже на балансе.',
                )
            )
        elif payment.status in {'created', 'processing'}:
            message_lines.append(
                texts.t(
                    'MULENPAY_STATUS_PENDING_NOTE',
                    '\n⏳ Платеж еще не завершен. Завершите оплату по ссылке и проверьте статус позже.',
                )
            )
            if payment.payment_url:
                message_lines.append(
                    texts.t(
                        'MULENPAY_STATUS_PAYMENT_URL_LINE',
                        '\n🔗 Ссылка на оплату: {payment_url}',
                    ).format(payment_url=payment.payment_url)
                )
        elif payment.status in {'canceled', 'error'}:
            message_lines.append(
                texts.t(
                    'MULENPAY_STATUS_FAILED_NOTE',
                    '\n❌ Платеж не был завершен. Попробуйте создать новый платеж или обратитесь в {support}',
                ).format(support=settings.get_support_contact_display())
            )

        message_text = ''.join(message_lines)

        if len(message_text) > 190:
            await callback.message.answer(message_text)
            await callback.answer(
                texts.t('MULENPAY_STATUS_SENT_ALERT', 'ℹ️ Статус платежа отправлен в чат'),
                show_alert=True,
            )
        else:
            await callback.answer(message_text, show_alert=True)

    except Exception as e:
        logger.error('Ошибка проверки статуса', get_mulenpay_display_name=settings.get_mulenpay_display_name(), error=e)
        await callback.answer(
            texts.t('MULENPAY_STATUS_CHECK_ERROR', '❌ Ошибка проверки статуса'),
            show_alert=True,
        )
