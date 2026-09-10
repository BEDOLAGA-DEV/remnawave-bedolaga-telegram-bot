"""Возврат к сохранённой корзине докупки трафика или устройств.

Жалоба: докупил трафик, денег не хватило, пополнил баланс — «Трафик не
покупается сам», а кнопка «Вернуться к оформлению подписки» отвечает «Корзина
повреждена» и удаляет её; следующее нажатие — «Корзина не найдена».

Причины были две. Корзины докупки сохранялись без метки намерения
(``return_to_cart``), и тихая автопокупка после пополнения их пропускала. А
общий обработчик кнопки знал только подписочные корзины и требовал у корзины
``period_days`` — у докупки его нет и быть не может.
"""

from __future__ import annotations

from aiogram import types
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.inline import get_insufficient_balance_keyboard_with_cart
from app.localization.texts import get_texts
from app.services.subscription_auto_purchase_service import resume_addon_cart


def _price_kopeks(cart_data: dict) -> int:
    try:
        return int(cart_data.get('price_kopeks') or 0)
    except (TypeError, ValueError):
        return 0


async def resume_addon_cart_from_button(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    cart_data: dict,
) -> None:
    """Довести докупку до конца по явному нажатию; при нехватке — снова к пополнению."""
    texts = get_texts(db_user.language)
    price = _price_kopeks(cart_data)

    if price > 0 and db_user.balance_kopeks < price:
        missing = price - db_user.balance_kopeks
        text = texts.t(
            'ADDON_CART_STILL_INSUFFICIENT',
            ('❌ Все еще недостаточно средств\n\nТребуется: {required}\nУ вас: {balance}\nНе хватает: {missing}'),
        ).format(
            required=texts.format_price(price, round_kopeks=False),
            balance=texts.format_price(db_user.balance_kopeks, round_kopeks=False),
            missing=texts.format_price(missing, round_kopeks=False),
        )
        await callback.message.edit_text(
            text,
            reply_markup=get_insufficient_balance_keyboard_with_cart(db_user.language, missing),
        )
        await callback.answer()
        return

    # Сама покупка шлёт человеку итог («Трафик добавлен» / «Устройства добавлены»)
    # и чистит корзину; здесь — только короткий ответ на нажатие.
    succeeded = await resume_addon_cart(db, db_user, cart_data, bot=callback.bot)
    if succeeded:
        await callback.answer(texts.t('ADDON_CART_COMPLETED', '✅ Готово!'))
        return

    await callback.answer(
        texts.t(
            'ADDON_CART_FAILED',
            '❌ Не удалось завершить покупку. Попробуйте ещё раз или напишите в поддержку.',
        ),
        show_alert=True,
    )
