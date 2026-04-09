import html
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
async def start_yookassa_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
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

    if not settings.is_yookassa_enabled():
        await callback.answer('❌ Оплата картой через YooKassa временно недоступна', show_alert=True)
        return

    min_amount_rub = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
    max_amount_rub = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100

    message_text = (
        f'💳 <b>Оплата банковской картой</b>\n\n'
        f'Введите сумму для пополнения от {min_amount_rub:.0f} до {max_amount_rub:,.0f} рублей:'
    )

    keyboard = get_back_keyboard(db_user.language)

    await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='yookassa')
    await state.update_data(
        yookassa_prompt_message_id=callback.message.message_id,
        yookassa_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def start_yookassa_sbp_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
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

    if not settings.is_yookassa_enabled() or not settings.YOOKASSA_SBP_ENABLED:
        await callback.answer('❌ Оплата через СБП временно недоступна', show_alert=True)
        return

    min_amount_rub = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
    max_amount_rub = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100

    message_text = (
        f'🏦 <b>Оплата через СБП</b>\n\n'
        f'Введите сумму для пополнения от {min_amount_rub:.0f} до {max_amount_rub:,.0f} рублей:'
    )

    keyboard = get_back_keyboard(db_user.language)

    await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='yookassa_sbp')
    await state.update_data(
        yookassa_prompt_message_id=callback.message.message_id,
        yookassa_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_yookassa_payment_amount(
    message: types.Message, db_user: User, db: AsyncSession, amount_kopeks: int, state: FSMContext
):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
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

    texts = get_texts(db_user.language)

    if not settings.is_yookassa_enabled():
        await message.answer('❌ Оплата через YooKassa временно недоступна')
        return

    if amount_kopeks < settings.YOOKASSA_MIN_AMOUNT_KOPEKS:
        min_rubles = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
        await message.answer(
            f'❌ Минимальная сумма для оплаты картой: {min_rubles:.0f} ₽',
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    if amount_kopeks > settings.YOOKASSA_MAX_AMOUNT_KOPEKS:
        max_rubles = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100
        await message.answer(
            f'❌ Максимальная сумма для оплаты картой: {max_rubles:,.0f} ₽'.replace(',', ' '),
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    try:
        payment_service = PaymentService(message.bot)

        payment_result = await payment_service.create_yookassa_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),
            receipt_email=None,
            receipt_phone=None,
            metadata={
                'user_telegram_id': str(db_user.telegram_id),
                'user_username': db_user.username or '',
                'purpose': 'balance_topup',
            },
        )

        if not payment_result:
            await message.answer('❌ Ошибка создания платежа. Попробуйте позже или обратитесь в поддержку.')
            await state.clear()
            return

        confirmation_url = payment_result.get('confirmation_url')
        if not confirmation_url:
            await message.answer('❌ Ошибка получения ссылки для оплаты. Обратитесь в поддержку.')
            await state.clear()
            return

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='💳 Оплатить картой', url=confirmation_url)],
                [
                    types.InlineKeyboardButton(
                        text='📊 Проверить статус', callback_data=f'check_yookassa_{payment_result["local_payment_id"]}'
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
            ]
        )

        state_data = await state.get_data()
        prompt_message_id = state_data.get('yookassa_prompt_message_id')
        prompt_chat_id = state_data.get('yookassa_prompt_chat_id', message.chat.id)

        try:
            await message.delete()
        except Exception as delete_error:  # pragma: no cover - зависит от прав бота
            logger.warning('Не удалось удалить сообщение с суммой YooKassa', delete_error=delete_error)

        if prompt_message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - диагностический лог
                logger.warning('Не удалось удалить сообщение с запросом суммы YooKassa', delete_error=delete_error)

        invoice_message = await message.answer(
            f'💳 <b>Оплата банковской картой</b>\n\n'
            f'💰 Сумма: {settings.format_price(amount_kopeks)}\n'
            f'🆔 ID платежа: {payment_result["yookassa_payment_id"][:8]}...\n\n'
            f'📱 <b>Инструкция:</b>\n'
            f"1. Нажмите кнопку 'Оплатить картой'\n"
            f'2. Введите данные вашей карты\n'
            f'3. Подтвердите платеж\n'
            f'4. Деньги поступят на баланс автоматически\n\n'
            f'🔒 Оплата происходит через защищенную систему YooKassa\n'
            f'✅ Принимаем карты: Visa, MasterCard, МИР\n\n'
            f'❓ Если возникнут проблемы, обратитесь в {settings.get_support_contact_display_html()}',
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        try:
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

        await state.update_data(
            yookassa_invoice_message_id=invoice_message.message_id,
            yookassa_invoice_chat_id=invoice_message.chat.id,
        )

        await state.clear()
        logger.info(
            'Создан платеж YooKassa для пользователя ₽, ID',
            telegram_id=db_user.telegram_id,
            value=amount_kopeks // 100,
            payment_result=payment_result['yookassa_payment_id'],
        )

    except Exception as e:
        logger.error('Ошибка создания YooKassa платежа', error=e)
        await message.answer('❌ Ошибка создания платежа. Попробуйте позже или обратитесь в поддержку.')
        await state.clear()


@error_handler
async def process_yookassa_sbp_payment_amount(
    message: types.Message, db_user: User, db: AsyncSession, amount_kopeks: int, state: FSMContext
):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
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

    texts = get_texts(db_user.language)

    if not settings.is_yookassa_enabled() or not settings.YOOKASSA_SBP_ENABLED:
        await message.answer('❌ Оплата через СБП временно недоступна')
        return

    if amount_kopeks < settings.YOOKASSA_MIN_AMOUNT_KOPEKS:
        min_rubles = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
        await message.answer(
            f'❌ Минимальная сумма для оплаты через СБП: {min_rubles:.0f} ₽',
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    if amount_kopeks > settings.YOOKASSA_MAX_AMOUNT_KOPEKS:
        max_rubles = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100
        await message.answer(
            f'❌ Максимальная сумма для оплаты через СБП: {max_rubles:,.0f} ₽'.replace(',', ' '),
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    try:
        payment_service = PaymentService(message.bot)

        payment_result = await payment_service.create_yookassa_sbp_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),
            receipt_email=None,
            receipt_phone=None,
            metadata={
                'user_telegram_id': str(db_user.telegram_id),
                'user_username': db_user.username or '',
                'purpose': 'balance_topup_sbp',
            },
        )

        if not payment_result:
            await message.answer('❌ Ошибка создания платежа через СБП. Попробуйте позже или обратитесь в поддержку.')
            await state.clear()
            return

        confirmation_url = payment_result.get('confirmation_url')
        qr_confirmation_data = payment_result.get('qr_confirmation_data')

        if not confirmation_url and not qr_confirmation_data:
            await message.answer('❌ Ошибка получения данных для оплаты через СБП. Обратитесь в поддержку.')
            await state.clear()
            return

        # Подготовим QR-код для вставки в основное сообщение
        qr_photo = None
        if qr_confirmation_data:
            try:
                # Импортируем необходимые модули для генерации QR-кода
                from io import BytesIO

                import qrcode
                from aiogram.types import BufferedInputFile

                # Создаем QR-код из полученных данных
                qr = qrcode.QRCode(version=1, box_size=10, border=5)
                qr.add_data(qr_confirmation_data)
                qr.make(fit=True)

                img = qr.make_image(fill_color='black', back_color='white')

                # Сохраняем изображение в байты
                img_bytes = BytesIO()
                img.save(img_bytes, format='PNG')
                img_bytes.seek(0)

                qr_photo = BufferedInputFile(img_bytes.getvalue(), filename='qrcode.png')
            except ImportError:
                logger.warning('qrcode библиотека не установлена, QR-код не будет сгенерирован')
            except Exception as e:
                logger.error('Ошибка генерации QR-кода', error=e)

        # Если нет QR-данных из YooKassa, но есть URL, генерируем QR-код из URL
        if not qr_photo and confirmation_url:
            try:
                # Импортируем необходимые модули для генерации QR-кода
                from io import BytesIO

                import qrcode
                from aiogram.types import BufferedInputFile

                # Создаем QR-код из URL
                qr = qrcode.QRCode(version=1, box_size=10, border=5)
                qr.add_data(confirmation_url)
                qr.make(fit=True)

                img = qr.make_image(fill_color='black', back_color='white')

                # Сохраняем изображение в байты
                img_bytes = BytesIO()
                img.save(img_bytes, format='PNG')
                img_bytes.seek(0)

                qr_photo = BufferedInputFile(img_bytes.getvalue(), filename='qrcode.png')
            except ImportError:
                logger.warning('qrcode библиотека не установлена, QR-код не будет сгенерирован')
            except Exception as e:
                logger.error('Ошибка генерации QR-кода из URL', error=e)

        # Создаем клавиатуру с кнопками для оплаты по ссылке и проверки статуса
        keyboard_buttons = []

        # Добавляем кнопку оплаты, если доступна ссылка
        if confirmation_url:
            keyboard_buttons.append([types.InlineKeyboardButton(text='🔗 Перейти к оплате', url=confirmation_url)])
        else:
            # Если ссылка недоступна, предлагаем оплатить через ID платежа в приложении банка
            keyboard_buttons.append(
                [types.InlineKeyboardButton(text='📱 Оплатить в приложении банка', callback_data='temp_disabled')]
            )

        # Добавляем общие кнопки
        keyboard_buttons.append(
            [
                types.InlineKeyboardButton(
                    text='📊 Проверить статус', callback_data=f'check_yookassa_{payment_result["local_payment_id"]}'
                )
            ]
        )
        keyboard_buttons.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')])

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        state_data = await state.get_data()
        prompt_message_id = state_data.get('yookassa_prompt_message_id')
        prompt_chat_id = state_data.get('yookassa_prompt_chat_id', message.chat.id)

        try:
            await message.delete()
        except Exception as delete_error:  # pragma: no cover - зависит от прав бота
            logger.warning('Не удалось удалить сообщение с суммой YooKassa (СБП)', delete_error=delete_error)

        if prompt_message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - диагностический лог
                logger.warning(
                    'Не удалось удалить сообщение с запросом суммы YooKassa (СБП)', delete_error=delete_error
                )

        # Подготавливаем текст сообщения
        message_text = (
            f'🔗 <b>Оплата через СБП</b>\n\n'
            f'💰 Сумма: {settings.format_price(amount_kopeks)}\n'
            f'🆔 ID платежа: {payment_result["yookassa_payment_id"][:8]}...\n\n'
        )

        # Добавляем инструкции в зависимости от доступных способов оплаты
        if not confirmation_url:
            message_text += (
                f'📱 <b>Инструкция по оплате:</b>\n'
                f'1. Откройте приложение вашего банка\n'
                f'2. Найдите функцию оплаты по реквизитам или перевод по СБП\n'
                f'3. Введите ID платежа: <code>{payment_result["yookassa_payment_id"]}</code>\n'
                f'4. Подтвердите платеж в приложении банка\n'
                f'5. Деньги поступят на баланс автоматически\n\n'
            )

        message_text += (
            f'🔒 Оплата происходит через защищенную систему YooKassa\n'
            f'✅ Принимаем СБП от всех банков-участников\n\n'
            f'❓ Если возникнут проблемы, обратитесь в {settings.get_support_contact_display_html()}'
        )

        # Отправляем сообщение с инструкциями и клавиатурой
        # Если есть QR-код, отправляем его как медиа-сообщение
        if qr_photo:
            # Используем метод отправки медиа-группы или фото с описанием
            invoice_message = await message.answer_photo(
                photo=qr_photo, caption=message_text, reply_markup=keyboard, parse_mode='HTML'
            )
        else:
            # Если QR-код недоступен, отправляем обычное текстовое сообщение
            invoice_message = await message.answer(message_text, reply_markup=keyboard, parse_mode='HTML')

        try:
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
            logger.warning('Не удалось сохранить сообщение YooKassa (СБП)', error=error)

        await state.update_data(
            yookassa_invoice_message_id=invoice_message.message_id,
            yookassa_invoice_chat_id=invoice_message.chat.id,
        )

        await state.clear()
        logger.info(
            'Создан платеж YooKassa СБП для пользователя ₽, ID',
            telegram_id=db_user.telegram_id,
            value=amount_kopeks // 100,
            payment_result=payment_result['yookassa_payment_id'],
        )

    except Exception as e:
        logger.error('Ошибка создания YooKassa СБП платежа', error=e)
        await message.answer('❌ Ошибка создания платежа через СБП. Попробуйте позже или обратитесь в поддержку.')
        await state.clear()


@error_handler
async def check_yookassa_payment_status(callback: types.CallbackQuery, db: AsyncSession):
    try:
        local_payment_id = int(callback.data.split('_')[-1])

        from app.database.crud.yookassa import get_yookassa_payment_by_local_id

        payment = await get_yookassa_payment_by_local_id(db, local_payment_id)

        if not payment:
            await callback.answer('❌ Платеж не найден', show_alert=True)
            return

        status_emoji = {
            'pending': '⏳',
            'waiting_for_capture': '⌛',
            'succeeded': '✅',
            'canceled': '❌',
            'failed': '❌',
        }

        status_text = {
            'pending': 'Ожидает оплаты',
            'waiting_for_capture': 'Ожидает подтверждения',
            'succeeded': 'Оплачен',
            'canceled': 'Отменен',
            'failed': 'Ошибка',
        }

        emoji = status_emoji.get(payment.status, '❓')
        status = status_text.get(payment.status, 'Неизвестно')

        message_text = (
            f'💳 Статус платежа:\n\n'
            f'🆔 ID: {payment.yookassa_payment_id[:8]}...\n'
            f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
            f'📊 Статус: {emoji} {status}\n'
            f'📅 Создан: {payment.created_at.strftime("%d.%m.%Y %H:%M")}\n'
        )

        if payment.is_succeeded:
            message_text += '\n✅ Платеж успешно завершен!\n\nСредства зачислены на баланс.'
        elif payment.is_pending:
            message_text += "\n⏳ Платеж ожидает оплаты. Нажмите кнопку 'Оплатить' выше."
        elif payment.is_failed:
            message_text += f'\n❌ Платеж не прошел. Обратитесь в {settings.get_support_contact_display()}'

        await callback.answer(message_text, show_alert=True)

    except Exception as e:
        logger.error('Ошибка проверки статуса платежа', error=e)
        await callback.answer('❌ Ошибка проверки статуса', show_alert=True)
