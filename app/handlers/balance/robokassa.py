import html

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


def _restriction_block_message(db_user: User):
    reason = html.escape(
        getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
    )
    support_url = settings.get_support_contact_url()
    keyboard = []
    if support_url:
        keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
    texts = get_texts(db_user.language)
    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return (
        f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
        'Если вы считаете это ошибкой, вы можете обжаловать решение.',
        types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


@error_handler
async def start_robokassa_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if getattr(db_user, 'restriction_topup', False):
        text, kb = _restriction_block_message(db_user)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
        return

    display_name = settings.get_robokassa_display_name()
    display_name_html = settings.get_robokassa_display_name_html()

    if not settings.is_robokassa_enabled():
        await callback.answer(
            f'❌ Оплата через {display_name} временно недоступна',
            show_alert=True,
        )
        return

    message_template = texts.t(
        'ROBOKASSA_TOPUP_PROMPT',
        (
            '💳 <b>Оплата через {display_name_html}</b>\n\n'
            'Введите сумму для пополнения от {min_amount} до {max_amount}.\n'
            'После ввода суммы вы получите ссылку на страницу {display_name}, где сможете '
            'выбрать удобный способ оплаты (карта, СБП, электронные кошельки и др.).'
        ),
    )
    message_text = message_template.format(
        display_name=display_name,
        display_name_html=display_name_html,
        min_amount=settings.format_price(settings.ROBOKASSA_MIN_AMOUNT_KOPEKS),
        max_amount=settings.format_price(settings.ROBOKASSA_MAX_AMOUNT_KOPEKS),
    )

    keyboard = get_back_keyboard(db_user.language)

    await callback.message.edit_text(
        message_text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method='robokassa',
        robokassa_prompt_message_id=callback.message.message_id,
        robokassa_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_robokassa_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if getattr(db_user, 'restriction_topup', False):
        text, kb = _restriction_block_message(db_user)
        await message.answer(text, reply_markup=kb, parse_mode='HTML')
        await state.clear()
        return

    display_name = settings.get_robokassa_display_name()
    display_name_html = settings.get_robokassa_display_name_html()

    if not settings.is_robokassa_enabled():
        await message.answer(f'❌ Оплата через {display_name} временно недоступна')
        return

    if amount_kopeks < settings.ROBOKASSA_MIN_AMOUNT_KOPEKS:
        await message.answer(
            f'Минимальная сумма пополнения: {settings.format_price(settings.ROBOKASSA_MIN_AMOUNT_KOPEKS)}',
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    if amount_kopeks > settings.ROBOKASSA_MAX_AMOUNT_KOPEKS:
        await message.answer(
            f'Максимальная сумма пополнения: {settings.format_price(settings.ROBOKASSA_MAX_AMOUNT_KOPEKS)}',
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    state_data = await state.get_data()
    prompt_message_id = state_data.get('robokassa_prompt_message_id')
    prompt_chat_id = state_data.get('robokassa_prompt_chat_id', message.chat.id)

    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover
        logger.warning('Не удалось удалить сообщение с суммой Robokassa', delete_error=delete_error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover
            logger.warning('Не удалось удалить сообщение с запросом суммы Robokassa', delete_error=delete_error)

    try:
        payment_service = PaymentService(message.bot)
        payment_result = await payment_service.create_robokassa_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(
                amount_kopeks, telegram_user_id=db_user.telegram_id
            ),
        )

        if not payment_result or not payment_result.get('payment_url'):
            await message.answer(
                texts.t(
                    'ROBOKASSA_PAYMENT_ERROR',
                    '❌ Ошибка создания платежа {display_name}. Попробуйте позже или обратитесь в поддержку.',
                ).format(display_name=display_name)
            )
            await state.clear()
            return

        payment_url = payment_result.get('payment_url')
        inv_id = payment_result.get('inv_id')
        local_payment_id = payment_result.get('local_payment_id')

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'ROBOKASSA_PAY_BUTTON',
                            '💳 Оплатить через {display_name}',
                        ).format(display_name=display_name),
                        url=payment_url,
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                        callback_data=f'check_robokassa_{local_payment_id}',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
            ]
        )

        message_template = texts.t(
            'ROBOKASSA_PAYMENT_INSTRUCTIONS',
            (
                '💳 <b>Оплата через {display_name_html}</b>\n\n'
                '💰 Сумма: {amount}\n'
                '🆔 ID платежа: {payment_id}\n\n'
                '📱 <b>Инструкция:</b>\n'
                "1. Нажмите кнопку 'Оплатить через {display_name}'\n"
                '2. Выберите способ оплаты на странице {display_name}\n'
                '3. Подтвердите перевод\n'
                '4. Средства зачислятся автоматически\n\n'
                '❓ Если возникнут проблемы, обратитесь в {support}'
            ),
        )

        message_text = message_template.format(
            amount=settings.format_price(amount_kopeks),
            payment_id=inv_id,
            support=settings.get_support_contact_display_html(),
            display_name=display_name,
            display_name_html=display_name_html,
        )

        invoice_message = await message.answer(
            message_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        try:
            from app.services import payment_service as payment_module

            payment = await payment_module.get_robokassa_payment_by_local_id(db, local_payment_id)
            if payment:
                payment_metadata = dict(getattr(payment, 'metadata_json', {}) or {})
                payment_metadata['invoice_message'] = {
                    'chat_id': invoice_message.chat.id,
                    'message_id': invoice_message.message_id,
                }
                await payment_module.update_robokassa_payment_metadata(
                    db,
                    payment=payment,
                    metadata=payment_metadata,
                )
        except Exception as error:  # pragma: no cover
            logger.warning('Не удалось сохранить данные сообщения Robokassa', error=error)

        await state.update_data(
            robokassa_invoice_message_id=invoice_message.message_id,
            robokassa_invoice_chat_id=invoice_message.chat.id,
        )

        await state.clear()

        logger.info(
            'Создан платеж Robokassa для пользователя',
            telegram_id=db_user.telegram_id,
            amount_kopeks=amount_kopeks,
            inv_id=inv_id,
        )

    except Exception as e:
        logger.error('Ошибка создания платежа Robokassa', error=e)
        await message.answer(
            texts.t(
                'ROBOKASSA_PAYMENT_ERROR',
                '❌ Ошибка создания платежа {display_name}. Попробуйте позже или обратитесь в поддержку.',
            ).format(display_name=display_name)
        )
        await state.clear()


@error_handler
async def check_robokassa_payment_status(callback: types.CallbackQuery, db: AsyncSession):
    try:
        local_payment_id = int(callback.data.split('_')[-1])
        payment_service = PaymentService(callback.bot)
        status_info = await payment_service.get_robokassa_payment_status(db, local_payment_id)

        if not status_info:
            await callback.answer('❌ Платеж не найден', show_alert=True)
            return

        payment = status_info['payment']

        status_labels = {
            'created': ('⏳', 'Ожидает оплаты'),
            'processing': ('⌛', 'Обрабатывается'),
            'success': ('✅', 'Оплачен'),
            'canceled': ('❌', 'Отменен'),
            'error': ('⚠️', 'Ошибка'),
            'unknown': ('❓', 'Неизвестно'),
        }

        emoji, status_text = status_labels.get(payment.status, ('❓', 'Неизвестно'))

        display_name = settings.get_robokassa_display_name()
        message_lines = [
            f'💳 Статус платежа {display_name}:\n\n',
            f'🆔 ID: {payment.inv_id}\n',
            f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n',
            f'📊 Статус: {emoji} {status_text}\n',
            f'📅 Создан: {payment.created_at.strftime("%d.%m.%Y %H:%M")}\n',
        ]

        if payment.is_paid:
            message_lines.append('\n✅ Платеж успешно завершен! Средства уже на балансе.')
        elif payment.status in {'created', 'processing'}:
            message_lines.append('\n⏳ Платеж ещё не завершен. Завершите оплату по ссылке и проверьте статус позже.')
            if payment.payment_url:
                message_lines.append(f'\n🔗 Ссылка на оплату: {payment.payment_url}')
        elif payment.status in {'canceled', 'error'}:
            message_lines.append(
                f'\n❌ Платеж не был завершен. Попробуйте создать новый платеж или обратитесь в {settings.get_support_contact_display()}'
            )

        message_text = ''.join(message_lines)

        if len(message_text) > 190:
            await callback.message.answer(message_text)
            await callback.answer('ℹ️ Статус платежа отправлен в чат', show_alert=True)
        else:
            await callback.answer(message_text, show_alert=True)

    except Exception as e:
        logger.error('Ошибка проверки статуса Robokassa', error=e)
        await callback.answer('❌ Ошибка проверки статуса', show_alert=True)
