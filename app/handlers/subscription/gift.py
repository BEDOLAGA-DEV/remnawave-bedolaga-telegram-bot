"""Native Telegram handlers for subscription gifting catalog, period selection, and navigation."""

from __future__ import annotations

import hashlib
import html
import uuid
from pathlib import Path

import qrcode
import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import GuestPurchase, GuestPurchaseStatus, User
from app.handlers.subscription.purchase import show_subscription_info
from app.keyboards.inline import get_insufficient_balance_keyboard
from app.localization.texts import get_texts
from app.services.gift_claim_service import (
    GiftClaimAlreadyOwnedError,
    GiftClaimNotActivatableError,
    GiftClaimNotFoundError,
    GiftClaimSelfActivationError,
    claim_gift_for_user,
)
from app.services.gift_history_service import (
    GiftHistoryItem,
    get_sender_gift,
    has_sender_gifts,
    list_sender_gifts,
)
from app.services.gift_notification_service import (
    build_gift_history_detail_presentation,
    build_gift_result_presentation,
    resolve_gift_claim_channel,
)
from app.services.gift_purchase_service import (
    GiftError,
    GiftFeatureDisabledError,
    GiftInsufficientBalanceError,
    GiftPeriodUnavailableError,
    GiftPriceChangedError,
    GiftPurchaseRestrictedError,
    GiftQuote,
    GiftTariffOffer,
    GiftTariffUnavailableError,
    is_gift_enabled,
    list_gift_offers,
    purchase_gift_from_balance,
    quote_gift_purchase,
)
from app.services.guest_purchase_service import GuestPurchaseError
from app.services.user_cart_service import user_cart_service
from app.states import GiftActivationStates, GiftPurchaseStates
from app.utils.gift_links import build_gift_claim_artifacts


logger = structlog.get_logger(__name__)


# ── Render Helpers ──────────────────────────────────────────────────────────


GIFT_HISTORY_PAGE_SIZE = 5


def _render_tariff_catalog(db_user: User, offers: list[GiftTariffOffer]) -> tuple[str, InlineKeyboardMarkup]:
    """Render gift tariff catalog message and keyboard."""
    texts = get_texts(db_user.language)
    text = texts.t(
        'GIFT_CATALOG_TITLE',
        '🎁 <b>Подарить подписку</b>\n\nВыберите тариф для подарка:',
    )

    buttons: list[list[InlineKeyboardButton]] = []
    for offer in offers:
        tariff_label = texts.t('GIFT_TARIFF_CHOICE_BUTTON', '{tariff_name}').format(tariff_name=offer.tariff_name)
        buttons.append([InlineKeyboardButton(text=tariff_label, callback_data=f'gift_tariff:{offer.tariff_id}')])

    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('GIFT_MY_BUTTON', '🎁 Мои подарки'),
                callback_data='gift_my',
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('GIFT_ENTER_CODE_BUTTON', '🎁 Активировать код'),
                callback_data='gift_enter_code',
            )
        ]
    )
    buttons.append([InlineKeyboardButton(text=texts.t('GIFT_CANCEL_BUTTON', '❌ Отмена'), callback_data='gift_cancel')])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def _render_history_list(
    db_user: User,
    items: list[GiftHistoryItem],
    page: int,
    total_count: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render localized paginated gift history list and keyboard."""
    texts = get_texts(db_user.language)
    if not items or total_count == 0:
        text = texts.t(
            'GIFT_MY_EMPTY_TEXT',
            '🎁 <b>Мои подарки</b>\n\nУ вас пока нет оформленных подарков.',
        )
        buttons = [
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_ENTER_CODE_BUTTON', '🎁 Активировать код'),
                    callback_data='gift_enter_code',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_MY_BACK_TO_CATALOG_BUTTON', '◀️ Назад'),
                    callback_data='gift_back_tariffs',
                )
            ],
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=buttons)

    total_pages = max(1, (total_count + GIFT_HISTORY_PAGE_SIZE - 1) // GIFT_HISTORY_PAGE_SIZE)
    if total_pages > 1:
        text = texts.t(
            'GIFT_MY_TITLE_PAGED',
            '🎁 <b>Мои подарки</b> (стр. {page}/{total_pages})\n\nВыберите подарок для просмотра деталей:',
        ).format(page=page, total_pages=total_pages)
    else:
        text = texts.t(
            'GIFT_MY_TITLE',
            '🎁 <b>Мои подарки</b>\n\nВыберите подарок для просмотра деталей:',
        )

    buttons: list[list[InlineKeyboardButton]] = []
    for item in items:
        status_emoji = '✅' if item.is_delivered else '⏳'
        raw_name = item.tariff_name or texts.t('GIFT_TARIFF_DELETED', 'Архивный тариф')
        item_label = texts.t(
            'GIFT_MY_ITEM_BUTTON',
            '{status_emoji} {tariff_name} — {period_days} дн.',
        ).format(
            status_emoji=status_emoji,
            tariff_name=raw_name,
            period_days=item.period_days,
        )
        buttons.append([InlineKeyboardButton(text=item_label, callback_data=f'gift_my_open:{item.purchase_id}')])

    nav_row: list[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(
            InlineKeyboardButton(
                text=texts.t('GIFT_MY_PREV_PAGE_BUTTON', '⬅️ Предыдущая'),
                callback_data=f'gift_my_page:{page - 1}',
            )
        )
    if page < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text=texts.t('GIFT_MY_NEXT_PAGE_BUTTON', 'Следующая ➡️'),
                callback_data=f'gift_my_page:{page + 1}',
            )
        )
    if nav_row:
        buttons.append(nav_row)

    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('GIFT_MY_BACK_TO_CATALOG_BUTTON', '◀️ Назад'),
                callback_data='gift_back_tariffs',
            )
        ]
    )

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def _render_period_selection(db_user: User, offer: GiftTariffOffer) -> tuple[str, InlineKeyboardMarkup]:
    """Render gift period selection message and keyboard with HTML escaping."""
    texts = get_texts(db_user.language)
    escaped_name = html.escape(offer.tariff_name)
    escaped_desc = html.escape(offer.tariff_description) + '\n\n' if offer.tariff_description else ''
    traffic_str = (
        texts.format_traffic(offer.traffic_limit_gb)
        if offer.traffic_limit_gb is not None
        else texts.t('GIFT_TRAFFIC_UNLIMITED', '∞ (безлимит)')
    )
    devices_str = texts.format_device_limit(offer.device_limit)

    text = texts.t(
        'GIFT_SELECT_PERIOD_TITLE',
        '🎁 <b>Подарок: {tariff_name}</b>\n\n{description}📊 Трафик: <b>{traffic}</b>\n📱 Устройства: <b>{devices}</b>\n\nВыберите период:',
    ).format(
        tariff_name=escaped_name,
        description=escaped_desc,
        traffic=traffic_str,
        devices=devices_str,
    )

    buttons: list[list[InlineKeyboardButton]] = []
    for quote in offer.quotes:
        price_str = texts.format_price(quote.final_price_kopeks)
        if quote.discount_percent > 0:
            btn_text = texts.t('GIFT_PERIOD_DISCOUNT_BUTTON', '{days} дн. — {price} (-{discount}%)').format(
                days=quote.period_days, price=price_str, discount=quote.discount_percent
            )
        else:
            btn_text = texts.t('GIFT_PERIOD_CHOICE_BUTTON', '{days} дн. — {price}').format(
                days=quote.period_days, price=price_str
            )

        buttons.append(
            [InlineKeyboardButton(text=btn_text, callback_data=f'gift_period:{offer.tariff_id}:{quote.period_days}')]
        )

    nav_row = [
        InlineKeyboardButton(
            text=texts.t('GIFT_BACK_TO_TARIFFS_BUTTON', '◀️ К тарифам'),
            callback_data='gift_back_tariffs',
        ),
        InlineKeyboardButton(
            text=texts.t('GIFT_CANCEL_BUTTON', '❌ Отмена'),
            callback_data='gift_cancel',
        ),
    ]
    buttons.append(nav_row)

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def _render_confirmation_summary(db_user: User, quote: GiftQuote) -> tuple[str, InlineKeyboardMarkup]:
    """Render gift purchase confirmation summary message and keyboard with HTML escaping."""
    texts = get_texts(db_user.language)
    escaped_name = html.escape(quote.tariff_name)
    traffic_str = (
        texts.format_traffic(quote.traffic_limit_gb)
        if quote.traffic_limit_gb is not None
        else texts.t('GIFT_TRAFFIC_UNLIMITED', '∞ (безлимит)')
    )
    devices_str = texts.format_device_limit(quote.device_limit)
    balance_str = texts.format_price(db_user.balance_kopeks)
    final_price_str = texts.format_price(quote.final_price_kopeks)

    price_details = ''
    if quote.total_discount_kopeks > 0:
        orig_price_str = texts.format_price(quote.original_price_kopeks)
        disc_str = texts.format_price(quote.total_discount_kopeks)
        price_details += texts.t('GIFT_PRICE_ORIGINAL_LINE', '💵 Исходная цена: <s>{original_price}</s>\n').format(
            original_price=orig_price_str
        )
        price_details += texts.t('GIFT_PRICE_DISCOUNT_LINE', '🏷 Скидка: <b>-{discount}</b>\n').format(discount=disc_str)
    price_details += texts.t('GIFT_PRICE_FINAL_LINE', '💰 Итого к оплате: <b>{final_price}</b>\n').format(
        final_price=final_price_str
    )

    text = texts.t(
        'GIFT_SUMMARY_TITLE',
        '🎁 <b>Подтверждение подарка</b>\n\n📦 Тариф: <b>{tariff_name}</b>\n📅 Период: <b>{period_days} дн.</b>\n📊 Трафик: <b>{traffic}</b>\n📱 Устройства: <b>{devices}</b>\n\n{price_details}💳 Ваш баланс: <b>{balance}</b>\n\nПосле подтверждения с вашего баланса будет списана указанная сумма и создана ссылка на подарок.',
    ).format(
        tariff_name=escaped_name,
        period_days=quote.period_days,
        traffic=traffic_str,
        devices=devices_str,
        price_details=price_details,
        balance=balance_str,
    )

    buttons = [
        [
            InlineKeyboardButton(
                text=texts.t('GIFT_CONFIRM_PURCHASE_BUTTON', '✅ Подтвердить покупку'),
                callback_data='gift_confirm',
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t('GIFT_BACK_TO_PERIODS_BUTTON', '◀️ К периодам'),
                callback_data='gift_back_periods',
            ),
            InlineKeyboardButton(
                text=texts.t('GIFT_CANCEL_BUTTON', '❌ Отмена'),
                callback_data='gift_cancel',
            ),
        ],
    ]

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Callback Handlers ───────────────────────────────────────────────────────


async def handle_gift_catalog(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Entry point for native gift catalog and history hub."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    sales_enabled = await is_gift_enabled(db)
    has_history = await has_sender_gifts(db, db_user.id)

    if not sales_enabled:
        if has_history:
            text = texts.t(
                'GIFT_FEATURE_DISABLED_WITH_HISTORY',
                '🎁 <b>Подарки</b>\n\nПокупка новых подарков временно недоступна, но вы можете просмотреть свои подарки или активировать код.',
            )
            back_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.t('GIFT_MY_BUTTON', '🎁 Мои подарки'),
                            callback_data='gift_my',
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=texts.t('GIFT_ENTER_CODE_BUTTON', '🎁 Активировать код'),
                            callback_data='gift_enter_code',
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=texts.t('GIFT_CANCEL_BUTTON', '❌ Отмена'),
                            callback_data='gift_cancel',
                        )
                    ],
                ]
            )
            await callback.message.edit_text(text, reply_markup=back_kb, parse_mode='HTML')
            await callback.answer()
            return

        text = texts.t(
            'GIFT_FEATURE_DISABLED_NO_HISTORY',
            '🎁 <b>Подарки</b>\n\nПокупка новых подарков временно недоступна, но вы можете активировать полученный подарочный код.',
        )
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_ENTER_CODE_BUTTON', '🎁 Активировать код'),
                        callback_data='gift_enter_code',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_CANCEL_BUTTON', '❌ Отмена'),
                        callback_data='gift_cancel',
                    )
                ],
            ]
        )
        await callback.message.edit_text(text, reply_markup=back_kb, parse_mode='HTML')
        await callback.answer()
        return

    offers = await list_gift_offers(db, buyer=db_user)
    if not offers:
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_MY_BUTTON', '🎁 Мои подарки'),
                        callback_data='gift_my',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_ENTER_CODE_BUTTON', '🎁 Активировать код'),
                        callback_data='gift_enter_code',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_CANCEL_BUTTON', '❌ Отмена'),
                        callback_data='gift_cancel',
                    )
                ],
            ]
        )
        await callback.message.edit_text(
            texts.t('GIFT_NO_TARIFFS_AVAILABLE', 'В данный момент нет доступных тарифов для подарка.'),
            reply_markup=back_kb,
            parse_mode='HTML',
        )
        await callback.answer()
        return

    data = await state.get_data()
    checkout_id = data.get('gift_checkout_id') or uuid.uuid4().hex
    origin = data.get('gift_origin_callback') or (
        callback.data if callback.data != 'subscription_gift' else 'menu_subscription'
    )

    await state.set_state(GiftPurchaseStates.selecting_tariff)
    await state.update_data(
        gift_checkout_id=checkout_id,
        gift_origin_callback=origin,
    )

    text, keyboard = _render_tariff_catalog(db_user, offers)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def _render_and_show_periods(
    callback: types.CallbackQuery,
    tariff_id: int,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Render and display periods for a selected tariff without mutating callback data."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    if not await is_gift_enabled(db):
        await callback.answer(
            texts.t('GIFT_FEATURE_DISABLED', 'Покупка подарков временно недоступна.'),
            show_alert=True,
        )
        return

    offers = await list_gift_offers(db, buyer=db_user)
    offer = next((o for o in offers if o.tariff_id == tariff_id), None)
    if offer is None or not offer.quotes:
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_BACK_TO_TARIFFS_BUTTON', '◀️ К тарифам'),
                        callback_data='gift_back_tariffs',
                    )
                ]
            ]
        )
        await callback.message.edit_text(
            texts.t('GIFT_TARIFF_UNAVAILABLE', 'Выбранный тариф недоступен для подарка.'),
            reply_markup=back_kb,
        )
        await callback.answer()
        return

    data = await state.get_data()
    checkout_id = data.get('gift_checkout_id') or uuid.uuid4().hex
    await state.set_state(GiftPurchaseStates.selecting_period)
    await state.update_data(
        gift_checkout_id=checkout_id,
        gift_tariff_id=tariff_id,
    )

    text, keyboard = _render_period_selection(db_user, offer)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def handle_gift_tariff_select(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Handle tariff selection in gift flow and render periods."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    if not callback.data or not callback.data.startswith('gift_tariff:'):
        await callback.answer(texts.t('GIFT_INVALID_SELECTION', 'Некорректные параметры выбора.'), show_alert=True)
        return

    parts = callback.data.split(':', 1)
    if len(parts) != 2:
        await callback.answer(texts.t('GIFT_INVALID_SELECTION', 'Некорректные параметры выбора.'), show_alert=True)
        return

    try:
        tariff_id = int(parts[1])
    except ValueError:
        await callback.answer(texts.t('GIFT_INVALID_SELECTION', 'Некорректные параметры выбора.'), show_alert=True)
        return

    await _render_and_show_periods(callback, tariff_id, db_user, db, state)


async def handle_gift_period_select(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Handle period selection in gift flow and render confirmation summary."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    if not callback.data or not callback.data.startswith('gift_period:'):
        await callback.answer(texts.t('GIFT_INVALID_SELECTION', 'Некорректные параметры выбора.'), show_alert=True)
        return

    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer(texts.t('GIFT_INVALID_SELECTION', 'Некорректные параметры выбора.'), show_alert=True)
        return

    try:
        tariff_id = int(parts[1])
        period_days = int(parts[2])
    except ValueError:
        await callback.answer(texts.t('GIFT_INVALID_SELECTION', 'Некорректные параметры выбора.'), show_alert=True)
        return

    try:
        quote = await quote_gift_purchase(db, buyer=db_user, tariff_id=tariff_id, period_days=period_days)
    except GiftFeatureDisabledError:
        await callback.answer(
            texts.t('GIFT_FEATURE_DISABLED', 'Покупка подарков временно недоступна.'), show_alert=True
        )
        return
    except GiftTariffUnavailableError:
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_BACK_TO_TARIFFS_BUTTON', '◀️ К тарифам'),
                        callback_data='gift_back_tariffs',
                    )
                ]
            ]
        )
        await callback.message.edit_text(
            texts.t('GIFT_TARIFF_UNAVAILABLE', 'Выбранный тариф недоступен для подарка.'),
            reply_markup=back_kb,
        )
        await callback.answer()
        return
    except GiftPeriodUnavailableError:
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_BACK_TO_PERIODS_BUTTON', '◀️ К периодам'),
                        callback_data='gift_back_periods',
                    )
                ]
            ]
        )
        await callback.message.edit_text(
            texts.t('GIFT_PERIOD_UNAVAILABLE', 'Выбранный период недоступен.'),
            reply_markup=back_kb,
        )
        await callback.answer()
        return
    except GiftError as err:
        logger.warning('Gift quote calculation failed', error=str(err), tariff_id=tariff_id, period_days=period_days)
        await callback.answer(
            texts.t('GIFT_GENERIC_ERROR', 'Произошла ошибка при оформлении подарка. Попробуйте позже.'),
            show_alert=True,
        )
        return

    data = await state.get_data()
    checkout_id = data.get('gift_checkout_id') or uuid.uuid4().hex

    await state.set_state(GiftPurchaseStates.confirming_purchase)
    await state.update_data(
        gift_checkout_id=checkout_id,
        gift_tariff_id=tariff_id,
        gift_period_days=period_days,
        gift_expected_price_kopeks=quote.final_price_kopeks,
    )

    text, keyboard = _render_confirmation_summary(db_user, quote)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def handle_gift_back_tariffs(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Navigate back to tariff catalog."""
    await handle_gift_catalog(callback, db_user, db, state)


async def handle_gift_back_periods(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Navigate back to period selection for current tariff."""
    data = await state.get_data()
    tariff_id = data.get('gift_tariff_id')
    if tariff_id is None:
        await handle_gift_catalog(callback, db_user, db, state)
        return

    await _render_and_show_periods(callback, tariff_id, db_user, db, state)


async def handle_gift_cancel(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Cancel gift checkout, clean up saved gift cart, and return to origin subscription view."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    # If user has a saved gift cart, clear it and top-up intent
    cart_data = await user_cart_service.get_user_cart(db_user.id)
    if cart_data and cart_data.get('cart_mode') == 'gift_purchase':
        await user_cart_service.delete_user_cart(db_user.id)
        await user_cart_service.clear_topup_intent(db_user.id)

    data = await state.get_data()
    origin = data.get('gift_origin_callback', 'menu_subscription')
    await state.clear()

    if origin == 'my_subscriptions' and settings.is_multi_tariff_enabled():
        from app.handlers.subscription.my_subscriptions import show_my_subscriptions

        await show_my_subscriptions(callback, db_user, db, state)
    else:
        await show_subscription_info(callback, db_user, db)


async def handle_gift_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Confirmation handler: validates selection, preflights channels, purchases from balance, and renders result."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    data = await state.get_data()
    tariff_id = data.get('gift_tariff_id')
    period_days = data.get('gift_period_days')
    expected_price_kopeks = data.get('gift_expected_price_kopeks')

    if not tariff_id or not period_days or expected_price_kopeks is None:
        await callback.answer(
            texts.t('GIFT_INVALID_SELECTION', 'Некорректные параметры выбора.'),
            show_alert=True,
        )
        return

    checkout_id = data.get('gift_checkout_id')
    if not checkout_id:
        checkout_id = uuid.uuid4().hex
        await state.update_data(gift_checkout_id=checkout_id)

    # Preflight claim channels before debiting
    bot_username, cabinet_url = await resolve_gift_claim_channel(bot=callback.bot)
    if not bot_username and not cabinet_url:
        await callback.answer(
            texts.t(
                'GIFT_NO_CLAIM_CHANNEL_ERROR',
                '❌ Сервис подарков временно недоступен: не настроен канал выдачи ссылки.',
            ),
            show_alert=True,
        )
        return

    try:
        result = await purchase_gift_from_balance(
            db=db,
            buyer_id=db_user.id,
            tariff_id=tariff_id,
            period_days=period_days,
            expected_price_kopeks=expected_price_kopeks,
            idempotency_key=checkout_id,
            source='bot',
        )
    except GiftPriceChangedError as err:
        # Update FSM with fresh price, retain checkout for re-confirmation
        await state.update_data(
            gift_expected_price_kopeks=err.fresh_quote.final_price_kopeks,
        )
        text, keyboard = _render_confirmation_summary(db_user, err.fresh_quote)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
        await callback.answer(
            texts.t(
                'GIFT_PRICE_CHANGED_ERROR',
                '⚠️ Цена на выбранный тариф изменилась. Пожалуйста, подтвердите покупку заново.',
            ),
            show_alert=True,
        )
        return
    except GiftInsufficientBalanceError as err:
        missing_amount = err.required_kopeks - err.available_kopeks
        cart_data = {
            'cart_mode': 'gift_purchase',
            'gift_checkout_id': checkout_id,
            'tariff_id': tariff_id,
            'period_days': period_days,
            'total_price': expected_price_kopeks,
            'missing_amount': missing_amount,
            'saved_cart': True,
            'return_to_cart': True,
            'user_id': db_user.id,
        }
        saved = await user_cart_service.save_user_cart(db_user.id, cart_data)
        if not saved:
            req_str = texts.format_price(err.required_kopeks)
            avail_str = texts.format_price(err.available_kopeks)
            msg = texts.t(
                'GIFT_INSUFFICIENT_BALANCE_ERROR',
                '❌ Недостаточно средств на балансе. Требуется: {required}, доступно: {available}.',
            ).format(required=req_str, available=avail_str)
            await callback.answer(msg, show_alert=True)
            return

        req_str = texts.format_price(err.required_kopeks)
        avail_str = texts.format_price(err.available_kopeks)
        missing_str = texts.format_price(missing_amount)

        text = texts.t(
            'GIFT_INSUFFICIENT_BALANCE_TITLE',
            '💰 <b>Недостаточно средств для оформления подарка</b>\n\n'
            'Требуется: <b>{required}</b>\n'
            'У вас: <b>{balance}</b>\n'
            'Не хватает: <b>{missing}</b>\n\n'
            '🛒 <i>Ваша корзина сохранена! После пополнения баланса вы сможете вернуться к оформлению подарка.</i>\n\n'
            'Выберите способ пополнения:',
        ).format(
            required=req_str,
            balance=avail_str,
            missing=missing_str,
        )
        reply_markup = get_insufficient_balance_keyboard(
            language=db_user.language,
            amount_kopeks=missing_amount,
            resume_callback='return_to_gift_cart',
            has_saved_cart=True,
            resume_text=texts.t('GIFT_RETURN_TO_CART_BUTTON', '🎁 Вернуться к подарку'),
        )
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer()
        return
    except GiftPurchaseRestrictedError:
        await state.clear()
        await callback.answer(
            texts.t('GIFT_PURCHASE_RESTRICTED_ERROR', '❌ Покупка подписок недоступна для вашего аккаунта.'),
            show_alert=True,
        )
        return
    except GiftFeatureDisabledError:
        await state.clear()
        await callback.answer(
            texts.t('GIFT_FEATURE_DISABLED', 'Покупка подарков временно недоступна.'),
            show_alert=True,
        )
        return
    except GiftTariffUnavailableError:
        await state.clear()
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_CANCEL_BUTTON', '❌ Отмена'),
                        callback_data='gift_cancel',
                    )
                ]
            ]
        )
        await callback.message.edit_text(
            texts.t('GIFT_TARIFF_UNAVAILABLE', 'Выбранный тариф недоступен для подарка.'),
            reply_markup=back_kb,
        )
        await callback.answer()
        return
    except GiftPeriodUnavailableError:
        await state.clear()
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_BACK_TO_TARIFFS_BUTTON', '◀️ К тарифам'),
                        callback_data='gift_back_tariffs',
                    )
                ]
            ]
        )
        await callback.message.edit_text(
            texts.t('GIFT_PERIOD_UNAVAILABLE', 'Выбранный период недоступен.'),
            reply_markup=back_kb,
        )
        await callback.answer()
        return
    except GiftError as err:
        logger.error('Unexpected gift domain failure', buyer_id=db_user.id, error=str(err))
        await callback.answer(
            texts.t('GIFT_GENERIC_ERROR', 'Произошла ошибка при оформлении подарка. Попробуйте позже.'),
            show_alert=True,
        )
        return
    except Exception as err:
        logger.error('Unhandled error in gift confirmation', buyer_id=db_user.id, error=str(err), exc_info=True)
        await callback.answer(
            texts.t('GIFT_GENERIC_ERROR', 'Произошла ошибка при оформлении подарка. Попробуйте позже.'),
            show_alert=True,
        )
        return

    # Clear gift cart and intent if present
    cart_data = await user_cart_service.get_user_cart(db_user.id)
    if cart_data and cart_data.get('cart_mode') == 'gift_purchase':
        await user_cart_service.delete_user_cart(db_user.id)
        await user_cart_service.clear_topup_intent(db_user.id)

    text, keyboard = build_gift_result_presentation(
        language=db_user.language,
        purchase_result=result,
        bot_username=bot_username,
        cabinet_url=cabinet_url,
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()
    await state.clear()


async def handle_return_to_gift_cart(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Resume gift cart after balance top-up (Task 6)."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    cart_data = await user_cart_service.get_user_cart(db_user.id)
    if not cart_data:
        await callback.answer(
            texts.t('GIFT_CART_EXPIRED', '❌ Срок действия сохраненной корзины подарка истек.'),
            show_alert=True,
        )
        return

    if cart_data.get('cart_mode') != 'gift_purchase' or cart_data.get('user_id') != db_user.id:
        await user_cart_service.delete_user_cart(db_user.id)
        await user_cart_service.clear_topup_intent(db_user.id)
        await callback.answer(
            texts.t('GIFT_CART_INVALID', '❌ Сохраненный подарок недоступен или некорректен.'),
            show_alert=True,
        )
        return

    tariff_id = cart_data.get('tariff_id')
    period_days = cart_data.get('period_days')
    checkout_id = cart_data.get('gift_checkout_id') or uuid.uuid4().hex
    saved_total_price = cart_data.get('total_price')

    if not tariff_id or not period_days:
        await user_cart_service.delete_user_cart(db_user.id)
        await user_cart_service.clear_topup_intent(db_user.id)
        await callback.answer(
            texts.t('GIFT_CART_INVALID', '❌ Сохраненный подарок недоступен или некорректен.'),
            show_alert=True,
        )
        return

    # Check if this gift purchase was already completed (e.g. background auto-purchase succeeded but message delivery failed)
    existing_stmt = (
        select(GuestPurchase)
        .options(selectinload(GuestPurchase.tariff))
        .where(GuestPurchase.idempotency_key == checkout_id)
    )
    existing_res = await db.execute(existing_stmt)
    existing_purchase = existing_res.scalars().first()
    if existing_purchase is not None and existing_purchase.status in (
        GuestPurchaseStatus.PAID,
        GuestPurchaseStatus.DELIVERED,
    ):
        purchase_result = await purchase_gift_from_balance(
            db=db,
            buyer_id=db_user.id,
            tariff_id=existing_purchase.tariff_id,
            period_days=existing_purchase.period_days,
            expected_price_kopeks=existing_purchase.amount_kopeks,
            idempotency_key=checkout_id,
            source='bot',
        )
        bot_username, cabinet_url = await resolve_gift_claim_channel(bot=callback.bot)
        text, keyboard = build_gift_result_presentation(
            language=db_user.language,
            purchase_result=purchase_result,
            bot_username=bot_username,
            cabinet_url=cabinet_url,
        )
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
        await user_cart_service.delete_user_cart(db_user.id)
        await user_cart_service.clear_topup_intent(db_user.id)
        await state.clear()
        await callback.answer()
        return

    try:
        quote = await quote_gift_purchase(db, buyer=db_user, tariff_id=tariff_id, period_days=period_days)
    except GiftFeatureDisabledError:
        await user_cart_service.delete_user_cart(db_user.id)
        await user_cart_service.clear_topup_intent(db_user.id)
        await state.clear()
        await callback.answer(
            texts.t('GIFT_FEATURE_DISABLED', 'Покупка подарков временно недоступна.'),
            show_alert=True,
        )
        return
    except GiftTariffUnavailableError:
        await user_cart_service.delete_user_cart(db_user.id)
        await user_cart_service.clear_topup_intent(db_user.id)
        await state.clear()
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_CANCEL_BUTTON', '❌ Отмена'),
                        callback_data='gift_cancel',
                    )
                ]
            ]
        )
        await callback.message.edit_text(
            texts.t('GIFT_TARIFF_UNAVAILABLE', 'Выбранный тариф недоступен для подарка.'),
            reply_markup=back_kb,
        )
        await callback.answer()
        return
    except GiftPeriodUnavailableError:
        await user_cart_service.delete_user_cart(db_user.id)
        await user_cart_service.clear_topup_intent(db_user.id)
        await state.clear()
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_BACK_TO_TARIFFS_BUTTON', '◀️ К тарифам'),
                        callback_data='gift_back_tariffs',
                    )
                ]
            ]
        )
        await callback.message.edit_text(
            texts.t('GIFT_PERIOD_UNAVAILABLE', 'Выбранный период недоступен.'),
            reply_markup=back_kb,
        )
        await callback.answer()
        return
    except GiftPurchaseRestrictedError:
        await user_cart_service.delete_user_cart(db_user.id)
        await user_cart_service.clear_topup_intent(db_user.id)
        await state.clear()
        await callback.answer(
            texts.t('GIFT_PURCHASE_RESTRICTED_ERROR', '❌ Покупка подписок недоступна для вашего аккаунта.'),
            show_alert=True,
        )
        return
    except GiftError as err:
        logger.warning('Failed to requote gift on resume', error=str(err), tariff_id=tariff_id, period_days=period_days)
        await callback.answer(
            texts.t('GIFT_GENERIC_ERROR', 'Произошла ошибка при оформлении подарка. Попробуйте позже.'),
            show_alert=True,
        )
        return

    # Check if balance is still insufficient
    if db_user.balance_kopeks < quote.final_price_kopeks:
        new_missing = quote.final_price_kopeks - db_user.balance_kopeks
        cart_data['total_price'] = quote.final_price_kopeks
        cart_data['missing_amount'] = new_missing
        await user_cart_service.save_user_cart(db_user.id, cart_data)

        await state.set_state(GiftPurchaseStates.confirming_purchase)
        await state.update_data(
            gift_checkout_id=checkout_id,
            gift_tariff_id=tariff_id,
            gift_period_days=period_days,
            gift_expected_price_kopeks=quote.final_price_kopeks,
        )

        req_str = texts.format_price(quote.final_price_kopeks)
        bal_str = texts.format_price(db_user.balance_kopeks)
        missing_str = texts.format_price(new_missing)

        text = texts.t(
            'GIFT_INSUFFICIENT_BALANCE_TITLE',
            '💰 <b>Недостаточно средств для оформления подарка</b>\n\n'
            'Требуется: <b>{required}</b>\n'
            'У вас: <b>{balance}</b>\n'
            'Не хватает: <b>{missing}</b>\n\n'
            '🛒 <i>Ваша корзина сохранена! После пополнения баланса вы сможете вернуться к оформлению подарка.</i>\n\n'
            'Выберите способ пополнения:',
        ).format(
            required=req_str,
            balance=bal_str,
            missing=missing_str,
        )
        reply_markup = get_insufficient_balance_keyboard(
            language=db_user.language,
            amount_kopeks=new_missing,
            resume_callback='return_to_gift_cart',
            has_saved_cart=True,
            resume_text=texts.t('GIFT_RETURN_TO_CART_BUTTON', '🎁 Вернуться к подарку'),
        )
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
        await callback.answer()
        return

    # Balance is sufficient -> render confirmation summary
    await state.set_state(GiftPurchaseStates.confirming_purchase)
    await state.update_data(
        gift_checkout_id=checkout_id,
        gift_tariff_id=tariff_id,
        gift_period_days=period_days,
        gift_expected_price_kopeks=quote.final_price_kopeks,
    )

    text, keyboard = _render_confirmation_summary(db_user, quote)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')

    if saved_total_price is not None and quote.final_price_kopeks != saved_total_price:
        await callback.answer(
            texts.t(
                'GIFT_PRICE_CHANGED_ERROR',
                '⚠️ Цена на выбранный тариф изменилась. Пожалуйста, подтвердите покупку заново.',
            ),
            show_alert=True,
        )
    else:
        await callback.answer()


async def handle_gift_enter_code(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Prompt user to manually enter gift code or link."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    await state.set_state(GiftActivationStates.waiting_for_code)

    text = texts.t(
        'GIFT_ENTER_CODE_PROMPT',
        '🎁 <b>Активация подарка</b>\n\nОтправьте код подарка или полученную ссылку:',
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_ACTIVATION_CANCEL_BUTTON', '❌ Отмена'),
                    callback_data='gift_activation_cancel',
                )
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def handle_gift_activation_cancel(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Cancel manual code entry and return to gift catalog view."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    await state.set_state(None)
    await handle_gift_catalog(callback, db_user, db, state)


async def handle_gift_code_input(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Handle manual gift code or link input in GiftActivationStates.waiting_for_code."""
    texts = get_texts(db_user.language)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_ACTIVATION_CANCEL_BUTTON', '❌ Отмена'),
                    callback_data='gift_activation_cancel',
                )
            ]
        ]
    )

    if not message.text:
        await message.answer(
            texts.t(
                'GIFT_ACTIVATION_NON_TEXT_ERROR',
                '⚠️ Пожалуйста, отправьте текстовый код или ссылку на подарок.',
            ),
            reply_markup=cancel_kb,
            parse_mode='HTML',
        )
        return

    raw_input = message.text.strip()

    try:
        purchase = await claim_gift_for_user(
            db,
            claimant_user_id=db_user.id,
            claim_input=raw_input,
            allow_legacy_short=False,
        )
    except GiftClaimNotFoundError:
        await message.answer(
            texts.t(
                'GIFT_ACTIVATION_NOT_FOUND',
                'Подарок не найден или недоступен.',
            ),
            reply_markup=cancel_kb,
            parse_mode='HTML',
        )
        return
    except GiftClaimSelfActivationError:
        await message.answer(
            texts.t(
                'GIFT_ACTIVATION_SELF_CLAIM_ERROR',
                '⚠️ Нельзя активировать свой собственный подарок.\nОтправьте код другу!',
            ),
            reply_markup=cancel_kb,
            parse_mode='HTML',
        )
        return
    except GiftClaimAlreadyOwnedError:
        await message.answer(
            texts.t(
                'GIFT_ACTIVATION_ALREADY_OWNED_ERROR',
                'ℹ️ Этот подарок уже был активирован.',
            ),
            reply_markup=cancel_kb,
            parse_mode='HTML',
        )
        return
    except GiftClaimNotActivatableError:
        await message.answer(
            texts.t(
                'GIFT_ACTIVATION_NOT_ACTIVATABLE_ERROR',
                '❌ Этот подарок невозможно активировать.',
            ),
            reply_markup=cancel_kb,
            parse_mode='HTML',
        )
        return
    except GuestPurchaseError as exc:
        logger.warning(
            'Gift code activation failed with guest purchase error',
            claimant_user_id=db_user.id,
            error=exc.message,
        )
        if exc.status_code >= 500:
            msg_text = texts.t(
                'GIFT_ACTIVATION_GENERIC_ERROR',
                '❌ Произошла ошибка при активации подарка. Попробуйте активировать через личный кабинет.',
            )
        else:
            msg_text = texts.t(
                'GIFT_ACTIVATION_FAILED_PREFIX',
                'Не удалось активировать подарок: {error}',
            ).format(error=html.escape(exc.message))
        await message.answer(msg_text, reply_markup=cancel_kb, parse_mode='HTML')
        return
    except Exception:
        logger.exception(
            'Unexpected error during gift code activation',
            claimant_user_id=db_user.id,
        )
        await message.answer(
            texts.t(
                'GIFT_ACTIVATION_GENERIC_ERROR',
                '❌ Произошла ошибка при активации подарка. Попробуйте активировать через личный кабинет.',
            ),
            reply_markup=cancel_kb,
            parse_mode='HTML',
        )
        return

    await state.clear()

    tariff_name = html.escape(purchase.tariff.name) if purchase.tariff and purchase.tariff.name else ''
    period_days = purchase.period_days or 0
    success_text = texts.t(
        'GIFT_ACTIVATION_SUCCESS_TEXT',
        '🎁 <b>Подарок активирован!</b>\n{tariff_name} — {period_days} дн.\n\nВаша подписка обновлена.',
    ).format(tariff_name=tariff_name, period_days=period_days)

    success_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_BACK_TO_SUBSCRIPTION_BUTTON', '◀️ К подписке'),
                    callback_data='menu_subscription',
                ),
                InlineKeyboardButton(
                    text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                    callback_data='back_to_menu',
                ),
            ]
        ]
    )
    await message.answer(success_text, reply_markup=success_kb, parse_mode='HTML')


# ── Gift History Handlers ───────────────────────────────────────────────────


async def _show_history_page(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    page: int = 1,
) -> None:
    """Display paginated list of sender gifts."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    page = max(page, 1)

    items, total_count = await list_sender_gifts(
        db,
        buyer_id=db_user.id,
        offset=(page - 1) * GIFT_HISTORY_PAGE_SIZE,
        limit=GIFT_HISTORY_PAGE_SIZE,
    )

    total_pages = max(1, (total_count + GIFT_HISTORY_PAGE_SIZE - 1) // GIFT_HISTORY_PAGE_SIZE) if total_count > 0 else 1
    if page > total_pages:
        page = total_pages
        items, total_count = await list_sender_gifts(
            db,
            buyer_id=db_user.id,
            offset=(page - 1) * GIFT_HISTORY_PAGE_SIZE,
            limit=GIFT_HISTORY_PAGE_SIZE,
        )

    text, keyboard = _render_history_list(
        db_user=db_user,
        items=items,
        page=page,
        total_count=total_count,
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def handle_gift_my(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Entry handler for 'My gifts' history list (Page 1)."""
    await _show_history_page(callback, db_user, db, page=1)


async def handle_gift_my_page(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Handle pagination page change in gift history."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    page = 1
    if callback.data and callback.data.startswith('gift_my_page:'):
        parts = callback.data.split(':', 1)
        if len(parts) == 2:
            try:
                page = int(parts[1])
            except ValueError:
                page = 1

    await _show_history_page(callback, db_user, db, page=page)


async def handle_gift_my_open(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Open detail card for a specific gift owned by the sender (IDOR protected)."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    if not callback.data or not callback.data.startswith('gift_my_open:'):
        await callback.answer(texts.t('GIFT_MY_ITEM_NOT_FOUND', 'Подарок не найден или недоступен.'), show_alert=True)
        return

    parts = callback.data.split(':', 1)
    if len(parts) != 2:
        await callback.answer(texts.t('GIFT_MY_ITEM_NOT_FOUND', 'Подарок не найден или недоступен.'), show_alert=True)
        return

    try:
        purchase_id = int(parts[1])
    except ValueError:
        await callback.answer(texts.t('GIFT_MY_ITEM_NOT_FOUND', 'Подарок не найден или недоступен.'), show_alert=True)
        return

    item = await get_sender_gift(db, buyer_id=db_user.id, purchase_id=purchase_id)
    if item is None:
        await callback.answer(texts.t('GIFT_MY_ITEM_NOT_FOUND', 'Подарок не найден или недоступен.'), show_alert=True)
        return

    bot_username, cabinet_url = await resolve_gift_claim_channel(bot=callback.bot)
    try:
        text, keyboard = build_gift_history_detail_presentation(
            language=db_user.language,
            item=item,
            bot_username=bot_username,
            cabinet_url=cabinet_url,
        )
    except Exception as err:
        logger.error(
            'Failed to build gift history detail presentation',
            buyer_id=db_user.id,
            purchase_id=purchase_id,
            error=str(err),
        )
        await callback.answer(
            texts.t('GIFT_GENERIC_ERROR', 'Произошла ошибка при оформлении подарка. Попробуйте позже.'),
            show_alert=True,
        )
        return

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def _load_shareable_gift(callback: types.CallbackQuery, db_user: User, db: AsyncSession, prefix: str):
    """Подарок отправителя вместе с его ссылками. ``None`` — показывать нечего.

    Один разбор на оба экрана: и QR, и текст для отправки нуждаются в одном и том
    же — своём подарке, который ещё не активирован, и в его ссылке. Дублировать
    проверку владельца в двух местах значило бы однажды забыть её в одном.
    """
    texts = get_texts(db_user.language)
    not_found = texts.t('GIFT_MY_ITEM_NOT_FOUND', 'Подарок не найден или недоступен.')

    raw = (callback.data or '').split(':', 1)
    if len(raw) != 2:
        await callback.answer(not_found, show_alert=True)
        return None

    try:
        purchase_id = int(raw[1])
    except ValueError:
        await callback.answer(not_found, show_alert=True)
        return None

    # Владелец проверяется запросом: идентификатор приходит из callback'а, и без
    # привязки к покупателю чужой подарок открывался бы по номеру.
    item = await get_sender_gift(db, buyer_id=db_user.id, purchase_id=purchase_id)
    if item is None:
        await callback.answer(not_found, show_alert=True)
        return None

    if not item.is_claimable:
        # Активированный подарок делиться нечем, а ссылка на него уже недействительна.
        await callback.answer(
            texts.t('GIFT_ITEM_NOT_CLAIMABLE', 'Этот подарок уже активирован — делиться нечем.'),
            show_alert=True,
        )
        return None

    bot_username, cabinet_url = await resolve_gift_claim_channel(bot=callback.bot)
    artifacts = build_gift_claim_artifacts(token=item.token, bot_username=bot_username, cabinet_url=cabinet_url)
    claim_link = artifacts.bot_claim_url or artifacts.cabinet_claim_url
    if not claim_link:
        logger.error('Gift claim channel is not configured', purchase_id=purchase_id)
        await callback.answer(not_found, show_alert=True)
        return None

    return item, artifacts, claim_link


def _gift_back_keyboard(texts, purchase_id: int) -> InlineKeyboardMarkup:
    """Возврат к карточке подарка, а не к списку: пользователь пришёл именно с неё."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_MY_BACK_TO_CARD', '◀️ К подарку'),
                    callback_data=f'gift_my_open:{purchase_id}',
                )
            ]
        ]
    )


async def handle_gift_my_qr(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Показать QR со ссылкой на активацию подарка.

    Нужен там, где переслать сообщение нельзя: подарок вручают вживую, и получатель
    наводит камеру.
    """
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    loaded = await _load_shareable_gift(callback, db_user, db, 'gift_my_qr')
    if loaded is None:
        return

    item, artifacts, claim_link = loaded
    texts = get_texts(db_user.language)
    await callback.answer()

    qr_dir = Path('data') / 'gift_qr'
    qr_dir.mkdir(parents=True, exist_ok=True)

    # Имя файла — от ССЫЛКИ, а не от номера подарка: сменится канал выдачи, и
    # закэшированный QR вёл бы на старый адрес.
    link_hash = hashlib.md5(claim_link.encode()).hexdigest()[:8]
    file_path = qr_dir / f'{item.purchase_id}_{link_hash}.png'
    if not file_path.exists():
        qrcode.make(claim_link).save(file_path)

    caption = texts.t(
        'GIFT_QR_CAPTION',
        '📱 <b>QR-код подарка</b>\n\nПокажите его получателю — камера откроет активацию.\n\n🔑 Код: <code>{public_code}</code>',
    ).format(public_code=html.escape(artifacts.public_code))
    keyboard = _gift_back_keyboard(texts, item.purchase_id)
    photo = FSInputFile(file_path)

    try:
        await callback.message.edit_media(
            types.InputMediaPhoto(media=photo, caption=caption, parse_mode='HTML'),
            reply_markup=keyboard,
        )
    except TelegramBadRequest:
        # Текстовое сообщение нельзя заменить фотографией — отправляем новым.
        await callback.message.delete()
        await callback.message.answer_photo(photo, caption=caption, reply_markup=keyboard, parse_mode='HTML')


async def handle_gift_my_text(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Готовое сообщение получателю, скопировать одним нажатием.

    Карточка показывает ссылку и код по отдельности, и переслать их приходится
    вручную, собирая фразу заново. Здесь текст уже собран и лежит в блоке кода —
    Telegram копирует такой блок целиком по нажатию.
    """
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    loaded = await _load_shareable_gift(callback, db_user, db, 'gift_my_text')
    if loaded is None:
        return

    item, artifacts, claim_link = loaded
    texts = get_texts(db_user.language)

    body = texts.t(
        'GIFT_COPY_TEXT_BODY',
        '🎁 Дарю тебе подписку {tariff_name} на {period_days} дн.\n\n'
        'Активировать: {claim_link}\n\n'
        'Если ссылка не открывается, введи код в боте: {public_code}',
    ).format(
        tariff_name=item.tariff_name or texts.t('GIFT_TARIFF_DEFAULT_NAME', 'VPN'),
        period_days=item.period_days,
        claim_link=claim_link,
        public_code=artifacts.public_code,
    )

    text = (
        f'{texts.t("GIFT_COPY_TEXT_TITLE", "📋 <b>Текст для отправки</b>")}\n\n'
        f'{texts.t("GIFT_COPY_TEXT_HINT", "Нажмите на текст ниже — он скопируется целиком.")}\n\n'
        # Экранируется ВСЁ содержимое: имя тарифа задаёт человек, и угловая
        # скобка в нём иначе оборвала бы разметку сообщения.
        f'<pre>{html.escape(body)}</pre>'
    )

    await callback.message.edit_text(text, reply_markup=_gift_back_keyboard(texts, item.purchase_id), parse_mode='HTML')
    await callback.answer()


async def handle_gift_my_back(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Return from gift detail card to history list."""
    await _show_history_page(callback, db_user, db, page=1)


# ── Handler Registration ───────────────────────────────────────────────────


def register_gift_handlers(dp: Dispatcher) -> None:
    """Register all gift purchase, navigation, and code activation handlers."""
    dp.callback_query.register(handle_gift_catalog, F.data == 'subscription_gift')
    dp.callback_query.register(handle_gift_my, F.data == 'gift_my')
    dp.callback_query.register(handle_gift_my_page, F.data.startswith('gift_my_page:'))
    dp.callback_query.register(handle_gift_my_open, F.data.startswith('gift_my_open:'))
    dp.callback_query.register(handle_gift_my_back, F.data == 'gift_my_back')
    dp.callback_query.register(handle_gift_my_qr, F.data.startswith('gift_my_qr:'))
    dp.callback_query.register(handle_gift_my_text, F.data.startswith('gift_my_text:'))
    dp.callback_query.register(handle_gift_enter_code, F.data == 'gift_enter_code')
    dp.callback_query.register(handle_gift_activation_cancel, F.data == 'gift_activation_cancel')
    dp.callback_query.register(handle_gift_tariff_select, F.data.startswith('gift_tariff:'))
    dp.callback_query.register(handle_gift_period_select, F.data.startswith('gift_period:'))
    dp.callback_query.register(handle_gift_back_tariffs, F.data == 'gift_back_tariffs')
    dp.callback_query.register(handle_gift_back_periods, F.data == 'gift_back_periods')
    dp.callback_query.register(handle_gift_cancel, F.data == 'gift_cancel')
    dp.callback_query.register(handle_gift_confirm, F.data == 'gift_confirm')
    dp.callback_query.register(handle_return_to_gift_cart, F.data == 'return_to_gift_cart')
    dp.message.register(handle_gift_code_input, GiftActivationStates.waiting_for_code)
