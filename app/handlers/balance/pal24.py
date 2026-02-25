import html
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


def _get_available_pal24_methods() -> list[str]:
    methods: list[str] = []
    if settings.is_pal24_sbp_button_visible():
        methods.append('sbp')
    if settings.is_pal24_card_button_visible():
        methods.append('card')
    if not methods:
        methods.append('sbp')
    return methods


async def _send_pal24_payment_message(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    payment_method: str,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)

    try:
        payment_service = PaymentService(message.bot)
        payment_result = await payment_service.create_pal24_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),
            language=db_user.language,
            payment_method=payment_method,
        )

        if not payment_result:
            await message.answer(
                texts.t(
                    'PAL24_PAYMENT_ERROR',
                    '❌ Ошибка создания платежа PayPalych. Попробуйте позже или обратитесь в поддержку.',
                )
            )
            await state.clear()
            return

        sbp_url = payment_result.get('sbp_url') or payment_result.get('transfer_url')
        card_url = payment_result.get('card_url')
        fallback_url = payment_result.get('link_page_url') or payment_result.get('link_url')

        if not (sbp_url or card_url or fallback_url):
            await message.answer(
                texts.t(
                    'PAL24_PAYMENT_ERROR',
                    '❌ Ошибка создания платежа PayPalych. Попробуйте позже или обратитесь в поддержку.',
                )
            )
            await state.clear()
            return

        if not sbp_url:
            sbp_url = fallback_url

        bill_id = payment_result.get('bill_id')
        local_payment_id = payment_result.get('local_payment_id')

        pay_buttons: list[list[types.InlineKeyboardButton]] = []
        steps: list[str] = []
        step_counter = 1

        default_sbp_text = texts.t(
            'PAL24_SBP_PAY_BUTTON',
            '🏦 Оплатить через PayPalych (СБП)',
        )
        sbp_button_text = settings.get_pal24_sbp_button_text(default_sbp_text)

        if sbp_url and settings.is_pal24_sbp_button_visible():
            pay_buttons.append(
                [
                    types.InlineKeyboardButton(
                        text=sbp_button_text,
                        url=sbp_url,
                    )
                ]
            )
            steps.append(
                texts.t(
                    'PAL24_INSTRUCTION_BUTTON',
                    '{step}. Нажмите кнопку «{button}»',
                ).format(step=step_counter, button=html.escape(sbp_button_text))
            )
            step_counter += 1

        default_card_text = texts.t(
            'PAL24_CARD_PAY_BUTTON',
            '💳 Оплатить банковской картой (PayPalych)',
        )
        card_button_text = settings.get_pal24_card_button_text(default_card_text)

        if card_url and card_url != sbp_url and settings.is_pal24_card_button_visible():
            pay_buttons.append(
                [
                    types.InlineKeyboardButton(
                        text=card_button_text,
                        url=card_url,
                    )
                ]
            )
            steps.append(
                texts.t(
                    'PAL24_INSTRUCTION_BUTTON',
                    '{step}. Нажмите кнопку «{button}»',
                ).format(step=step_counter, button=html.escape(card_button_text))
            )
            step_counter += 1

        if not pay_buttons and fallback_url and settings.is_pal24_sbp_button_visible():
            pay_buttons.append(
                [
                    types.InlineKeyboardButton(
                        text=sbp_button_text,
                        url=fallback_url,
                    )
                ]
            )
            steps.append(
                texts.t(
                    'PAL24_INSTRUCTION_BUTTON',
                    '{step}. Нажмите кнопку «{button}»',
                ).format(step=step_counter, button=html.escape(sbp_button_text))
            )
            step_counter += 1

        follow_template = texts.t(
            'PAL24_INSTRUCTION_FOLLOW',
            '{step}. Следуйте подсказкам платёжной системы',
        )
        steps.append(follow_template.format(step=step_counter))
        step_counter += 1

        confirm_template = texts.t(
            'PAL24_INSTRUCTION_CONFIRM',
            '{step}. Подтвердите перевод',
        )
        steps.append(confirm_template.format(step=step_counter))
        step_counter += 1

        success_template = texts.t(
            'PAL24_INSTRUCTION_COMPLETE',
            '{step}. Средства зачислятся автоматически',
        )
        steps.append(success_template.format(step=step_counter))

        message_template = texts.t(
            'PAL24_PAYMENT_INSTRUCTIONS',
            (
                '🏦 <b>Оплата через PayPalych</b>\n\n'
                '💰 Сумма: {amount}\n'
                '🆔 ID счета: {bill_id}\n\n'
                '📱 <b>Инструкция:</b>\n{steps}\n\n'
                '❓ Если возникнут проблемы, обратитесь в {support}'
            ),
        )

        keyboard_rows = pay_buttons + [
            [
                types.InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_pal24_{local_payment_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
        ]

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        message_text = message_template.format(
            amount=settings.format_price(amount_kopeks),
            bill_id=bill_id,
            steps='\n'.join(steps),
            support=settings.get_support_contact_display_html(),
        )

        invoice_message = await message.answer(
            message_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        try:
            from app.services import payment_service as payment_module

            payment = await payment_module.get_pal24_payment_by_id(db, local_payment_id)
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
            logger.warning('Не удалось сохранить сообщение PayPalych', error=error)

        await state.update_data(
            pal24_invoice_message_id=invoice_message.message_id,
            pal24_invoice_chat_id=invoice_message.chat.id,
        )

        await state.clear()

        logger.info(
            'Создан PayPalych счет для пользователя : ₽, ID метод',
            telegram_id=db_user.telegram_id,
            amount_kopeks=amount_kopeks / 100,
            bill_id=bill_id,
            payment_method=payment_method,
        )

    except Exception as error:
        logger.error('Ошибка создания PayPalych платежа', error=error)
        await message.answer(
            texts.t(
                'PAL24_PAYMENT_ERROR',
                '❌ Ошибка создания платежа PayPalych. Попробуйте позже или обратитесь в поддержку.',
            )
        )
        await state.clear()


@error_handler
async def start_pal24_payment(
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

    if not settings.is_pal24_enabled():
        await callback.answer(
            texts.t('PAL24_NOT_AVAILABLE', '❌ Оплата через PayPalych временно недоступна'),
            show_alert=True,
        )
        return

    # Формируем текст сообщения в зависимости от доступных способов оплаты
    if settings.is_pal24_sbp_button_visible() and settings.is_pal24_card_button_visible():
        payment_methods_text = texts.t('PAL24_TOPUP_METHODS_SBP_AND_CARD', 'СБП и банковской картой')
    elif settings.is_pal24_sbp_button_visible():
        payment_methods_text = texts.t('PAL24_TOPUP_METHODS_SBP', 'СБП')
    elif settings.is_pal24_card_button_visible():
        payment_methods_text = texts.t('PAL24_TOPUP_METHODS_CARD', 'банковской картой')
    else:
        # Если обе кнопки отключены, используем общий текст
        payment_methods_text = texts.t('PAL24_TOPUP_METHODS_FALLBACK', 'доступными способами')

    message_text = texts.t(
        'PAL24_TOPUP_PROMPT',
        (
            '🏦 <b>Оплата через PayPalych ({payment_methods_text})</b>\n\n'
            'Введите сумму для пополнения от 100 до 1 000 000 ₽.\n'
            'Оплата проходит через PayPalych ({payment_methods_text}).'
        ),
    ).format(payment_methods_text=payment_methods_text)

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
        payment_method='pal24',
        pal24_prompt_message_id=callback.message.message_id,
        pal24_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_pal24_payment_amount(
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

    if not settings.is_pal24_enabled():
        await message.answer(texts.t('PAL24_NOT_AVAILABLE', '❌ Оплата через PayPalych временно недоступна'))
        return

    if amount_kopeks < settings.PAL24_MIN_AMOUNT_KOPEKS:
        min_rubles = settings.PAL24_MIN_AMOUNT_KOPEKS / 100
        await message.answer(
            texts.t(
                'PAL24_MIN_AMOUNT_ERROR',
                '❌ Минимальная сумма для оплаты через PayPalych: {min_amount} ₽',
            ).format(min_amount=f'{min_rubles:.0f}')
        )
        return

    if amount_kopeks > settings.PAL24_MAX_AMOUNT_KOPEKS:
        max_rubles = settings.PAL24_MAX_AMOUNT_KOPEKS / 100
        await message.answer(
            texts.t(
                'PAL24_MAX_AMOUNT_ERROR',
                '❌ Максимальная сумма для оплаты через PayPalych: {max_amount} ₽',
            ).format(max_amount=f'{max_rubles:,.0f}'.replace(',', ' '))
        )
        return

    available_methods = _get_available_pal24_methods()

    state_data = await state.get_data()
    prompt_message_id = state_data.get('pal24_prompt_message_id')
    prompt_chat_id = state_data.get('pal24_prompt_chat_id', message.chat.id)

    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover - depends on bot rights
        logger.warning('Не удалось удалить сообщение с суммой PayPalych', delete_error=delete_error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover - diagnostic
            logger.warning('Не удалось удалить сообщение с запросом суммы PayPalych', delete_error=delete_error)

    if len(available_methods) == 1:
        await _send_pal24_payment_message(
            message,
            db_user,
            db,
            amount_kopeks,
            available_methods[0],
            state,
        )
        return

    await state.update_data(pal24_amount_kopeks=amount_kopeks)
    await state.set_state(BalanceStates.waiting_for_pal24_method)

    method_buttons: list[list[types.InlineKeyboardButton]] = []
    if 'sbp' in available_methods:
        method_buttons.append(
            [
                types.InlineKeyboardButton(
                    text=settings.get_pal24_sbp_button_text(
                        texts.t('PAL24_SBP_PAY_BUTTON', '🏦 Оплатить через PayPalych (СБП)')
                    ),
                    callback_data='pal24_method_sbp',
                )
            ]
        )
    if 'card' in available_methods:
        method_buttons.append(
            [
                types.InlineKeyboardButton(
                    text=settings.get_pal24_card_button_text(
                        texts.t('PAL24_CARD_PAY_BUTTON', '💳 Оплатить банковской картой (PayPalych)')
                    ),
                    callback_data='pal24_method_card',
                )
            ]
        )

    method_buttons.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')])

    await message.answer(
        texts.t(
            'PAL24_SELECT_PAYMENT_METHOD',
            'Выберите способ оплаты PayPalych:',
        ),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=method_buttons),
    )


@error_handler
async def handle_pal24_method_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    data = await state.get_data()
    amount_kopeks = data.get('pal24_amount_kopeks')
    if not amount_kopeks:
        texts = get_texts(db_user.language)
        await callback.answer(
            texts.t(
                'PAL24_PAYMENT_ERROR',
                '❌ Ошибка создания платежа PayPalych. Попробуйте позже или обратитесь в поддержку.',
            ),
            show_alert=True,
        )
        await state.clear()
        return

    method = 'sbp' if callback.data.endswith('_sbp') else 'card'

    await callback.answer()

    async with AsyncSessionLocal() as db:
        await _send_pal24_payment_message(
            callback.message,
            db_user,
            db,
            int(amount_kopeks),
            method,
            state,
        )


@error_handler
async def check_pal24_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
):
    texts = get_texts('ru')
    try:
        db_user = getattr(callback, 'db_user', None)
        texts = get_texts(db_user.language if db_user else 'ru') if db_user else get_texts('ru')

        local_payment_id = int(callback.data.split('_')[-1])
        payment_service = PaymentService(callback.bot)
        status_info = await payment_service.get_pal24_payment_status(db, local_payment_id)

        if not status_info:
            await callback.answer(
                texts.t('PAL24_PAYMENT_NOT_FOUND_ALERT', '❌ Платеж не найден'),
                show_alert=True,
            )
            return

        payment = status_info['payment']

        status_labels = {
            'NEW': ('⏳', texts.t('PAL24_STATUS_NEW', 'Ожидает оплаты')),
            'PROCESS': ('⌛', texts.t('PAL24_STATUS_PROCESS', 'Обрабатывается')),
            'SUCCESS': ('✅', texts.t('PAL24_STATUS_SUCCESS', 'Оплачен')),
            'FAIL': ('❌', texts.t('PAL24_STATUS_FAIL', 'Отменен')),
            'UNDERPAID': ('⚠️', texts.t('PAL24_STATUS_UNDERPAID', 'Недоплата')),
            'OVERPAID': ('⚠️', texts.t('PAL24_STATUS_OVERPAID', 'Переплата')),
        }

        emoji, status_text = status_labels.get(payment.status, ('❓', texts.t('PAL24_STATUS_UNKNOWN', 'Неизвестно')))

        metadata = payment.metadata_json or {}
        links_meta = metadata.get('links') if isinstance(metadata, dict) else None
        if not isinstance(links_meta, dict):
            links_meta = {}

        links_info = status_info.get('links') or {}

        def _extract_link(source: Any, keys: tuple[str, ...]) -> str | None:
            stack: list[Any] = [source]
            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    for key in keys:
                        value = current.get(key)
                        if value:
                            return str(value)
                    stack.extend(current.values())
                elif isinstance(current, list):
                    stack.extend(current)
            return None

        raw_response = metadata.get('raw_response') if isinstance(metadata, dict) else None
        remote_data = status_info.get('remote_data')
        transfer_keys = (
            'transfer_url',
            'transferUrl',
            'transfer_link',
            'transferLink',
            'transfer',
            'sbp_url',
            'sbpUrl',
            'sbp_link',
            'sbpLink',
        )
        card_keys = (
            'link_url',
            'linkUrl',
            'link',
            'card_url',
            'cardUrl',
            'card_link',
            'cardLink',
            'payment_url',
            'paymentUrl',
            'url',
        )

        extra_sbp_link = _extract_link(raw_response, transfer_keys) if raw_response else None
        if not extra_sbp_link and remote_data:
            extra_sbp_link = _extract_link(remote_data, transfer_keys)

        extra_card_link = _extract_link(raw_response, card_keys) if raw_response else None
        if not extra_card_link and remote_data:
            extra_card_link = _extract_link(remote_data, card_keys)

        sbp_link = (
            links_info.get('sbp')
            or links_meta.get('sbp')
            or status_info.get('sbp_url')
            or extra_sbp_link
            or payment.link_url
        )
        card_link = links_info.get('card') or links_meta.get('card') or status_info.get('card_url') or extra_card_link

        if not card_link and payment.link_page_url and payment.link_page_url != sbp_link:
            card_link = payment.link_page_url

        message_lines = [
            texts.t('PAL24_STATUS_MESSAGE_TITLE', '🏦 Статус платежа PayPalych:'),
            '',
            texts.t('PAL24_STATUS_BILL_ID_LINE', '🆔 ID счета: {bill_id}').format(bill_id=payment.bill_id),
            texts.t('PAL24_STATUS_AMOUNT_LINE', '💰 Сумма: {amount}').format(
                amount=settings.format_price(payment.amount_kopeks)
            ),
            texts.t('PAL24_STATUS_STATE_LINE', '📊 Статус: {emoji} {status_text}').format(
                emoji=emoji,
                status_text=status_text,
            ),
            texts.t('PAL24_STATUS_CREATED_AT_LINE', '📅 Создан: {created_at}').format(
                created_at=payment.created_at.strftime('%d.%m.%Y %H:%M')
            ),
        ]

        if payment.is_paid:
            message_lines.append('')
            message_lines.append(
                texts.t(
                    'PAL24_STATUS_PAID_NOTE',
                    '✅ Платеж успешно завершен! Средства уже на балансе.',
                )
            )
        elif payment.status in {'NEW', 'PROCESS'}:
            message_lines.append('')
            message_lines.append(
                texts.t(
                    'PAL24_STATUS_PENDING_NOTE',
                    '⏳ Платеж еще не завершен. Оплатите счет и проверьте статус позже.',
                )
            )
            if sbp_link:
                message_lines.append('')
                message_lines.append(texts.t('PAL24_STATUS_SBP_LINK_LINE', '🏦 СБП: {sbp_link}').format(sbp_link=sbp_link))
            if card_link and card_link != sbp_link:
                message_lines.append(
                    texts.t(
                        'PAL24_STATUS_CARD_LINK_LINE',
                        '💳 Банковская карта: {card_link}',
                    ).format(card_link=card_link)
                )
        elif payment.status in {'FAIL', 'UNDERPAID', 'OVERPAID'}:
            message_lines.append('')
            message_lines.append(
                texts.t(
                    'PAL24_STATUS_FAILED_NOTE',
                    '❌ Платеж не завершен корректно. Обратитесь в {support}',
                ).format(support=settings.get_support_contact_display())
            )

        pay_rows: list[list[types.InlineKeyboardButton]] = []

        if not payment.is_paid and payment.status in {'NEW', 'PROCESS'}:
            default_sbp_text = texts.t(
                'PAL24_SBP_PAY_BUTTON',
                '🏦 Оплатить через PayPalych (СБП)',
            )
            sbp_button_text = settings.get_pal24_sbp_button_text(default_sbp_text)

            if sbp_link and settings.is_pal24_sbp_button_visible():
                pay_rows.append(
                    [
                        types.InlineKeyboardButton(
                            text=sbp_button_text,
                            url=sbp_link,
                        )
                    ]
                )

            default_card_text = texts.t(
                'PAL24_CARD_PAY_BUTTON',
                '💳 Оплатить банковской картой (PayPalych)',
            )
            card_button_text = settings.get_pal24_card_button_text(default_card_text)

            if card_link and settings.is_pal24_card_button_visible():
                if not pay_rows or pay_rows[-1][0].url != card_link:
                    pay_rows.append(
                        [
                            types.InlineKeyboardButton(
                                text=card_button_text,
                                url=card_link,
                            )
                        ]
                    )

        keyboard_rows = pay_rows + [
            [
                types.InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_pal24_{local_payment_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
        ]

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        await callback.answer()
        try:
            await callback.message.edit_text(
                '\n'.join(message_lines),
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as error:
            if 'message is not modified' in str(error).lower():
                await callback.answer(texts.t('CHECK_STATUS_NO_CHANGES', 'Статус не изменился'))
            else:
                raise

    except Exception as e:
        logger.error('Ошибка проверки статуса PayPalych', error=e)
        await callback.answer(
            texts.t('PAL24_STATUS_CHECK_ERROR', '❌ Ошибка проверки статуса'),
            show_alert=True,
        )
