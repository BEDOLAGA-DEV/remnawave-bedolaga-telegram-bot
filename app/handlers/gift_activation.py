"""Handler for gift subscription activation via inline callback button."""

import html as html_mod

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.localization.texts import get_texts
from app.services.gift_claim_service import (
    GiftClaimAlreadyOwnedError,
    GiftClaimNotActivatableError,
    GiftClaimNotFoundError,
    GiftClaimSelfActivationError,
    claim_bound_gift_for_user,
)
from app.services.guest_purchase_service import GuestPurchaseError, TariffChoiceRequiredError
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

_GIFT_NOT_FOUND = 'Подарок не найден или недоступен.'
GIFT_NEW_COPY = 'n'


def gift_tariff_choice_text(texts) -> str:
    return texts.t(
        'GIFT_COPY_CHOICE_TEXT',
        '🎁 Тариф этого подарка у вас уже есть.\n\n'
        'Добавьте дни к имеющейся подписке или активируйте подарок отдельной подпиской — '
        'у неё будут свои устройства, трафик и ссылка подключения.',
    )


def gift_tariff_choice_markup(texts, choice: TariffChoiceRequiredError) -> InlineKeyboardMarkup:
    """Тариф подарка у получателя уже жив: в какую подписку дни или отдельной копией."""
    rows = []
    for subscription in choice.subscriptions:
        until = format_local_datetime(subscription.end_date, '%d.%m.%Y')
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_COPY_EXTEND_BUTTON', '🔄 Добавить к подписке: до {date}').format(date=until),
                    callback_data=f'gift_activate:{choice.purchase_id}:t{subscription.id}',
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=texts.t('GIFT_COPY_NEW_BUTTON', '➕ Отдельной подпиской'),
                callback_data=f'gift_activate:{choice.purchase_id}:{GIFT_NEW_COPY}',
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _parse_gift_choice(token: str | None) -> dict:
    if token == GIFT_NEW_COPY:
        return {'as_copy': True}
    if token and token.startswith('t') and token[1:].isdigit():
        return {'target_subscription_id': int(token[1:])}
    return {}


async def handle_gift_activate(callback: types.CallbackQuery, db_user: User | None = None) -> None:
    """Handle gift_activate:{purchase_id} callback from Telegram notification."""
    texts = get_texts(db_user.language if db_user else callback.from_user.language_code)
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer(texts.t('GIFT_ACTIVATION_OBSOLETE_MESSAGE'), show_alert=True)
        return

    if not callback.data:
        return

    parts = callback.data.split(':')
    if len(parts) not in (2, 3):
        await callback.answer(texts.t('GIFT_ACTIVATION_NOT_FOUND'), show_alert=True)
        return

    try:
        purchase_id = int(parts[1])
    except ValueError:
        await callback.answer(texts.t('GIFT_ACTIVATION_NOT_FOUND'), show_alert=True)
        return
    choice = _parse_gift_choice(parts[2] if len(parts) == 3 else None)

    await callback.answer()
    await callback.message.edit_text(texts.t('GIFT_ACTIVATION_IN_PROGRESS'), parse_mode=None)

    async with AsyncSessionLocal() as db:
        user = await get_user_by_telegram_id(db, callback.from_user.id)
        if not user:
            await callback.message.edit_text(texts.t('GIFT_ACTIVATION_NOT_FOUND'), parse_mode=None)
            return

        texts = get_texts(user.language)

        try:
            purchase = await claim_bound_gift_for_user(
                db,
                claimant_user_id=user.id,
                purchase_id=purchase_id,
                **choice,
            )
        except TariffChoiceRequiredError as required:
            await callback.message.edit_text(
                gift_tariff_choice_text(texts),
                reply_markup=gift_tariff_choice_markup(texts, required),
                parse_mode=None,
            )
            return
        except (GiftClaimNotFoundError, GiftClaimAlreadyOwnedError):
            await callback.message.edit_text(
                texts.t('GIFT_ACTIVATION_NOT_FOUND', _GIFT_NOT_FOUND),
                parse_mode=None,
            )
            return
        except GiftClaimSelfActivationError:
            await callback.message.edit_text(
                texts.t(
                    'GIFT_ACTIVATION_SELF_CLAIM_ERROR',
                    '⚠️ Нельзя активировать свой собственный подарок.\nОтправьте код другу!',
                ),
                parse_mode=None,
            )
            return
        except GiftClaimNotActivatableError:
            await callback.message.edit_text(
                texts.t('GIFT_ACTIVATION_NOT_ACTIVATABLE_ERROR', '❌ Этот подарок невозможно активировать.'),
                parse_mode=None,
            )
            return
        except GuestPurchaseError as exc:
            logger.warning(
                'Gift activation via callback failed',
                purchase_id=purchase_id,
                telegram_id=callback.from_user.id,
                error=exc.message,
            )
            if exc.status_code >= 500:
                await callback.message.edit_text(
                    texts.t(
                        'GIFT_ACTIVATION_GENERIC_ERROR',
                        'Произошла ошибка при активации. Попробуйте позже.',
                    ),
                    parse_mode=None,
                )
            else:
                await callback.message.edit_text(
                    texts.t(
                        'GIFT_ACTIVATION_FAILED_PREFIX',
                        'Не удалось активировать подарок: {error}',
                    ).format(error=html_mod.escape(exc.message)),
                    parse_mode=None,
                )
            return
        except Exception:
            logger.exception(
                'Unexpected error during gift activation via callback',
                purchase_id=purchase_id,
                telegram_id=callback.from_user.id,
            )
            await callback.message.edit_text(
                texts.t(
                    'GIFT_ACTIVATION_GENERIC_ERROR',
                    'Произошла ошибка при активации. Попробуйте позже.',
                ),
                parse_mode=None,
            )
            return

        tariff_name = html_mod.escape(purchase.tariff.name) if purchase.tariff and purchase.tariff.name else ''
        period_days = purchase.period_days
        period_text = f'{period_days} дн.' if period_days else ''
        tariff_text = f'{tariff_name} — {period_text}' if tariff_name else period_text

        await callback.message.edit_text(
            texts.t(
                'GIFT_ACTIVATION_CALLBACK_SUCCESS_TEXT',
                '✅ <b>Подарок активирован!</b>\n{tariff_text}\n\nВаша подписка обновлена.',
            ).format(
                tariff_text=tariff_text,
            ),
        )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_gift_activate, F.data.startswith('gift_activate:'))
