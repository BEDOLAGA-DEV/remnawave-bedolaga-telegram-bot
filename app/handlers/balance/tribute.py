import html

import structlog
from aiogram import types

from app.config import settings
from app.database.models import User
from app.external.tribute import get_product_links
from app.localization.texts import get_texts
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

TRIBUTE_PRODUCT_CALLBACK_PREFIX = 'tribute_pay_'


async def _reject_if_restricted_or_disabled(callback: types.CallbackQuery, db_user: User, texts) -> bool:
    """Ограничение пополнения и выключенный Tribute. True — ответ уже отправлен, дальше идти нельзя."""
    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(
            getattr(db_user, 'restriction_reason', None)
            or texts.t('USER_RESTRICTION_DEFAULT_REASON', 'Действие ограничено администратором')
        )
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать'), url=support_url
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
        return True

    if not settings.TRIBUTE_ENABLED:
        await callback.answer(
            texts.t('PAYMENT_UI_CARD_UNAVAILABLE', '❌ Оплата картой временно недоступна'), show_alert=True
        )
        return True

    return False


async def _show_product_choice(callback: types.CallbackQuery, texts, products: dict[int, str]) -> None:
    """Цифровой товар Tribute: цена фиксирована, показываем кнопки с суммами."""
    amount_buttons = [
        types.InlineKeyboardButton(
            text=f'{kopeks // 100} ₽', callback_data=f'{TRIBUTE_PRODUCT_CALLBACK_PREFIX}{kopeks}'
        )
        for kopeks in products
    ]
    keyboard = [amount_buttons[index : index + 2] for index in range(0, len(amount_buttons), 2)]
    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')])

    await callback.message.edit_text(
        texts.t(
            'PAYMENT_UI_TRIBUTE_PRODUCTS_INSTRUCTIONS',
            '💳 <b>Пополнение банковской картой</b>\n\n• Выберите сумму пополнения\n• Безопасная оплата через Tribute\n• Зачисление на баланс после оплаты\n\n• 🚨 Оплачивайте с этого же Telegram-аккаунта: деньги зачисляются тому, с кого платили.',
        ),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode='HTML',
    )


@error_handler
async def start_tribute_payment(
    callback: types.CallbackQuery,
    db_user: User,
):
    texts = get_texts(db_user.language)

    if await _reject_if_restricted_or_disabled(callback, db_user, texts):
        return

    products = get_product_links()
    if products:
        await _show_product_choice(callback, texts, products)
        await callback.answer()
        return

    try:
        from app.services.tribute_service import TributeService

        tribute_service = TributeService(callback.bot)
        payment_url = await tribute_service.create_payment_link(
            user_id=db_user.telegram_id,
            amount_kopeks=0,
            description='Пополнение баланса VPN',
        )

        if not payment_url:
            await callback.answer(
                texts.t('SUBSCRIPTION_UI_PAYMENT_CREATION_FAILED', '❌ Ошибка создания платежа'), show_alert=True
            )
            return

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('PAYMENT_UI_OPEN_CARD_PAYMENT', '💳 Перейти к оплате'), url=payment_url
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
            ]
        )

        message_text = texts.t(
            'PAYMENT_UI_TRIBUTE_INSTRUCTIONS',
            '💳 <b>Пополнение банковской картой</b>\n\n• Введите любую сумму от 100₽\n• Безопасная оплата через Tribute\n• Мгновенное зачисление на баланс\n• Принимаем карты Visa, MasterCard, МИР\n\n• 🚨 НЕ ОТПРАВЛЯТЬ ПЛАТЕЖ АНОНИМНО!\n\nНажмите кнопку для перехода к оплате:',
        )

        await callback.message.edit_text(
            message_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        TributeService.remember_invoice_message(
            db_user.telegram_id,
            callback.message.chat.id,
            callback.message.message_id,
        )

    except Exception as e:
        logger.error('Ошибка создания Tribute платежа', error=e)
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_PAYMENT_CREATION_FAILED', '❌ Ошибка создания платежа'), show_alert=True
        )

    await callback.answer()


@error_handler
async def start_tribute_product_payment(
    callback: types.CallbackQuery,
    db_user: User,
):
    """Выбрана сумма цифрового товара (`tribute_pay_<копейки>`): показываем ссылку на оплату этого товара."""
    texts = get_texts(db_user.language)

    if await _reject_if_restricted_or_disabled(callback, db_user, texts):
        return

    try:
        amount_kopeks = int((callback.data or '').removeprefix(TRIBUTE_PRODUCT_CALLBACK_PREFIX))
    except ValueError:
        amount_kopeks = 0

    products = get_product_links()
    if not products or amount_kopeks not in products:
        # Кнопка устарела (список сумм поменяли) — возвращаем к актуальному выбору
        await callback.answer(
            texts.t('PAYMENT_UI_TRIBUTE_AMOUNT_UNAVAILABLE', '❌ Эта сумма недоступна, выберите другую'),
            show_alert=True,
        )
        if products:
            await _show_product_choice(callback, texts, products)
        return

    try:
        from app.services.tribute_service import TributeService

        tribute_service = TributeService(callback.bot)
        payment_url = await tribute_service.create_payment_link(
            user_id=db_user.telegram_id,
            amount_kopeks=amount_kopeks,
            description='Пополнение баланса VPN',
        )

        if not payment_url:
            await callback.answer(
                texts.t('SUBSCRIPTION_UI_PAYMENT_CREATION_FAILED', '❌ Ошибка создания платежа'), show_alert=True
            )
            return

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('PAYMENT_UI_OPEN_CARD_PAYMENT', '💳 Перейти к оплате'), url=payment_url
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='topup_tribute')],
            ]
        )

        await callback.message.edit_text(
            texts.t(
                'PAYMENT_UI_TRIBUTE_PRODUCT_PAY',
                '💳 <b>Пополнение на {amount} ₽</b>\n\nНажмите кнопку для перехода к оплате.\n\n• 🚨 Оплачивайте с этого же Telegram-аккаунта: деньги зачисляются тому, с кого платили.',
            ).format(amount=amount_kopeks // 100),
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        TributeService.remember_invoice_message(
            db_user.telegram_id,
            callback.message.chat.id,
            callback.message.message_id,
        )

    except Exception as e:
        logger.error('Ошибка создания Tribute платежа', error=e)
        await callback.answer(
            texts.t('SUBSCRIPTION_UI_PAYMENT_CREATION_FAILED', '❌ Ошибка создания платежа'), show_alert=True
        )
        return

    await callback.answer()
