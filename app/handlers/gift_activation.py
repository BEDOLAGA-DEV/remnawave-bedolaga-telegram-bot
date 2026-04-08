"""Handler for gift subscription activation via inline callback button."""

import html as html_mod

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InaccessibleMessage
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database.database import AsyncSessionLocal
from app.database.models import GuestPurchase
from app.services.guest_purchase_service import GuestPurchaseError, activate_purchase


logger = structlog.get_logger(__name__)

_GIFT_NOT_FOUND = 'Подарок не найден или недоступен.'


async def handle_gift_activate(callback: types.CallbackQuery) -> None:
    """Handle gift_activate:{purchase_id} callback from Telegram notification."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer('Сообщение устарело. Попробуйте /start.', show_alert=True)
        return

    if not callback.data:
        return

    parts = callback.data.split(':', 1)
    if len(parts) != 2:
        await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
        return

    try:
        purchase_id = int(parts[1])
    except ValueError:
        await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text('⏳ Активируем подарок...', parse_mode=None)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GuestPurchase)
            .options(selectinload(GuestPurchase.user), selectinload(GuestPurchase.tariff))
            .where(GuestPurchase.id == purchase_id)
        )
        purchase = result.scalars().first()

        if not purchase or purchase.user_id is None or purchase.user is None:
            await callback.message.edit_text(_GIFT_NOT_FOUND, parse_mode=None)
            return

        # Verify the callback sender is the actual recipient
        if purchase.user.telegram_id != callback.from_user.id:
            await callback.message.edit_text(_GIFT_NOT_FOUND, parse_mode=None)
            return

        # Resolve tariff info inside session (selectin-loaded relationships)
        tariff_name = html_mod.escape(purchase.tariff.name) if purchase.tariff and purchase.tariff.name else ''
        period_days = purchase.period_days

        try:
            await activate_purchase(db, purchase.token, skip_notification=True)
        except GuestPurchaseError as exc:
            logger.warning(
                'Gift activation via callback failed',
                purchase_id=purchase_id,
                telegram_id=callback.from_user.id,
                error=exc.message,
            )
            if exc.status_code >= 500:
                await callback.message.edit_text('Произошла ошибка при активации. Попробуйте позже.', parse_mode=None)
            else:
                await callback.message.edit_text(
                    f'Не удалось активировать подарок: {html_mod.escape(exc.message)}',
                    parse_mode=None,
                )
            return
        except Exception:
            logger.exception(
                'Unexpected error during gift activation via callback',
                purchase_id=purchase_id,
                telegram_id=callback.from_user.id,
            )
            await callback.message.edit_text('Произошла ошибка при активации. Попробуйте позже.', parse_mode=None)
            return

    period_text = f'{period_days} дн.' if period_days else ''
    tariff_text = f'{tariff_name} — {period_text}' if tariff_name else period_text

    await callback.message.edit_text(
        f'✅ <b>Подарок активирован!</b>\n{tariff_text}\n\nВаша подписка обновлена.',
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_gift_activate, F.data.startswith('gift_activate:'))
