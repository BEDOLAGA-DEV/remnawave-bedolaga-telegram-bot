"""Общие инструменты платёжного сервиса.

В этом модуле собраны методы, которые нужны всем платёжным каналам:
построение клавиатур, базовые уведомления и стандартная обработка
успешных платежей.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal, get_db
from app.database.models import Subscription
from app.localization.texts import get_texts
from app.services.subscription_checkout_service import (
    has_subscription_checkout_draft,
    should_offer_checkout_resume,
)
from app.services.user_cart_service import user_cart_service
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.utils.payment_logger import payment_logger as logger


class PaymentCommonMixin:
    """Mixin с базовой логикой, которую используют остальные платёжные блоки."""

    async def build_topup_success_keyboard(self, user: Any) -> InlineKeyboardMarkup:
        """Формирует клавиатуру по завершении платежа, подстраиваясь под пользователя."""
        # Загружаем нужные тексты с учётом выбранного языка пользователя.
        texts = get_texts(user.language if user else 'ru')

        # Определяем статус подписки, чтобы показать подходящую кнопку.
        has_active_subscription = False
        subscription = None
        if user:
            try:
                subs = getattr(user, 'subscriptions', None) or []
                subscription = next(
                    (s for s in subs if getattr(s, 'is_active', False)),
                    None,
                )
                has_active_subscription = bool(
                    subscription
                    and not getattr(subscription, 'is_trial', False)
                    and getattr(subscription, 'is_active', False)
                )
            except MissingGreenlet:
                # user вне сессии — загружаем подписку отдельным запросом
                try:
                    async with AsyncSessionLocal() as session:
                        result = await session.execute(
                            select(Subscription.status, Subscription.is_trial, Subscription.end_date)
                            .where(Subscription.user_id == user.id)
                            .where(Subscription.status.in_(['active', 'trial']))
                            .order_by(Subscription.created_at.desc())
                        )
                        rows = result.all()
                        for row in rows:
                            end_date = row.end_date
                            if end_date is not None and end_date.tzinfo is None:
                                end_date = end_date.replace(tzinfo=UTC)
                            is_active = row.status == 'active' and end_date is not None and end_date > datetime.now(UTC)
                            if is_active and not row.is_trial:
                                has_active_subscription = True
                                break
                except Exception as db_error:
                    logger.warning(
                        'Не удалось загрузить подписку пользователя из БД',
                        getattr=getattr(user, 'id', None),
                        db_error=db_error,
                    )
            except Exception as error:  # pragma: no cover - защитный код
                logger.error(
                    'Ошибка загрузки подписки пользователя при построении клавиатуры после пополнения',
                    getattr=getattr(user, 'id', None),
                    error=error,
                )

        # Создаем основную кнопку: если есть активная подписка - продлить, иначе купить
        first_button = build_miniapp_or_callback_button(
            text=(texts.MENU_EXTEND_SUBSCRIPTION if has_active_subscription else texts.MENU_BUY_SUBSCRIPTION),
            callback_data=('subscription_extend' if has_active_subscription else 'menu_buy'),
        )

        keyboard_rows: list[list[InlineKeyboardButton]] = [
            [first_button],
        ]

        # Если для пользователя есть незавершённый checkout, предлагаем вернуться к нему.
        if user:
            try:
                has_saved_cart = await user_cart_service.has_user_cart(user.id)
            except Exception as cart_error:
                logger.warning(
                    'Не удалось проверить наличие сохраненной корзины у пользователя',
                    user_id=user.id,
                    cart_error=cart_error,
                )
                has_saved_cart = False

            if has_saved_cart:
                keyboard_rows.append(
                    [
                        build_miniapp_or_callback_button(
                            text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                            callback_data='return_to_saved_cart',
                        )
                    ]
                )
            else:
                draft_exists = await has_subscription_checkout_draft(user.id)
                if should_offer_checkout_resume(user, draft_exists, subscription=subscription):
                    keyboard_rows.append(
                        [
                            build_miniapp_or_callback_button(
                                text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                                callback_data='subscription_resume_checkout',
                            )
                        ]
                    )

        # Стандартные кнопки быстрого доступа к балансу и главному меню.
        keyboard_rows.append(
            [
                build_miniapp_or_callback_button(
                    text='💰 Мой баланс',
                    callback_data='menu_balance',
                )
            ]
        )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text='🏠 Главное меню',
                    callback_data='back_to_menu',
                )
            ]
        )

        return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    async def _send_payment_success_notification(
        self,
        telegram_id: int | None,
        amount_kopeks: int,
        user: Any | None = None,
        *,
        db: AsyncSession | None = None,
        payment_method_title: str | None = None,
    ) -> None:
        """Отправляет пользователю уведомление об успешном платеже."""
        # Lazy import to avoid circular dependency
        from app.cabinet.routes.websocket import notify_user_balance_topup

        # Send WebSocket notification to cabinet frontend (works for both Telegram and email-only users)
        user_id = getattr(user, 'id', None) if user else None
        if user_id:
            try:
                # Get new balance from user
                new_balance = getattr(user, 'balance_kopeks', 0)
                await notify_user_balance_topup(
                    user_id=user_id,
                    amount_kopeks=amount_kopeks,
                    new_balance_kopeks=new_balance,
                    description=payment_method_title or '',
                )
            except Exception as ws_error:
                logger.warning(
                    'Не удалось отправить WS уведомление о пополнении баланса для user_id',
                    user_id=user_id,
                    ws_error=ws_error,
                )

        if not getattr(self, 'bot', None):
            # Если бот не передан (например, внутри фоновых задач), уведомление пропускаем.
            return

        # Skip email-only users (no telegram_id)
        if not telegram_id:
            return

        user_snapshot = await self._ensure_user_snapshot(
            telegram_id,
            user,
            db=db,
        )

        try:
            payment_method = payment_method_title or 'Банковская карта (YooKassa)'

            # Стандартное сообщение с полной клавиатурой
            keyboard = await self.build_topup_success_keyboard(user_snapshot)
            message = (
                '✅ <b>Платеж успешно завершен!</b>\n\n'
                f'💰 Сумма: {settings.format_price(amount_kopeks)}\n'
                f'💳 Способ: {payment_method}\n\n'
                'Средства зачислены на ваш баланс!'
            )

            await self.bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        except Exception as error:
            logger.error('Ошибка отправки уведомления пользователю', telegram_id=telegram_id, error=error)

    async def _ensure_user_snapshot(
        self,
        telegram_id: int | None,
        user: Any | None,
        *,
        db: AsyncSession | None = None,
    ) -> Any | None:
        """Гарантирует, что данные пользователя пригодны для построения клавиатуры."""

        def _build_snapshot(source: Any | None) -> SimpleNamespace | None:
            if source is None:
                return None

            subs = getattr(source, 'subscriptions', None) or []
            active_sub = next(
                (s for s in subs if getattr(s, 'is_active', False)),
                None,
            )
            subscription_snapshot = None

            if active_sub is not None:
                subscription_snapshot = SimpleNamespace(
                    is_trial=getattr(active_sub, 'is_trial', False),
                    is_active=getattr(active_sub, 'is_active', False),
                    actual_status=getattr(active_sub, 'actual_status', None),
                )

            return SimpleNamespace(
                id=getattr(source, 'id', None),
                telegram_id=getattr(source, 'telegram_id', None),
                language=getattr(source, 'language', 'ru'),
                subscription=subscription_snapshot,
            )

        try:
            snapshot = _build_snapshot(user)
        except MissingGreenlet:
            snapshot = None

        if snapshot is not None:
            return snapshot

        fetch_session = db

        if fetch_session is not None:
            try:
                fetched_user = await get_user_by_telegram_id(fetch_session, telegram_id)
                return _build_snapshot(fetched_user)
            except Exception as fetch_error:
                logger.warning(
                    'Не удалось обновить пользователя из переданной сессии',
                    telegram_id=telegram_id,
                    fetch_error=fetch_error,
                )

        try:
            async for db_session in get_db():
                fetched_user = await get_user_by_telegram_id(db_session, telegram_id)
                return _build_snapshot(fetched_user)
        except Exception as fetch_error:
            logger.warning(
                'Не удалось получить пользователя для уведомления', telegram_id=telegram_id, fetch_error=fetch_error
            )

        return None

    async def process_successful_payment(
        self,
        payment_id: str,
        amount_kopeks: int,
        user_id: int,
        payment_method: str,
    ) -> bool:
        """Общая точка учёта успешных платежей (используется провайдерами при необходимости)."""
        try:
            logger.info(
                'Обработан успешный платеж ₽, пользователь , метод',
                payment_id=payment_id,
                amount_kopeks=amount_kopeks / 100,
                user_id=user_id,
                payment_method=payment_method,
            )
            return True
        except Exception as error:
            logger.error('Ошибка обработки платежа', payment_id=payment_id, error=error)
            return False


async def send_cart_notification_after_topup(
    user: Any,
    amount_kopeks: int,
    db: AsyncSession,
    bot: Any | None,
) -> bool:
    """Handle saved cart after balance top-up: try auto-purchase, then send notification.

    Returns True if a cart notification was sent.
    """
    from aiogram import types

    from app.database.crud.user import get_user_by_id
    from app.services.subscription_auto_purchase_service import (
        auto_purchase_saved_cart_after_topup,
        try_auto_extend_expired_after_topup,
        try_resume_disabled_daily_after_topup,
    )

    # Try to resume DISABLED daily subscription immediately (highest priority)
    try:
        daily_resumed = await try_resume_disabled_daily_after_topup(db, user, bot=bot)
        if daily_resumed:
            return False
    except Exception as daily_error:
        logger.error(
            'Ошибка авто-возобновления суточной подписки после пополнения',
            user_id=user.id,
            error=daily_error,
            exc_info=True,
        )

    cart_data = await user_cart_service.get_user_cart(user.id)
    # В приоритете всегда сохраненная корзина: она отражает явный выбор пользователя
    # (период/тариф/сумма). Автопродление expired — только когда корзины нет.
    if cart_data:
        cart_total = cart_data.get('total_price', 0)
        if not cart_total:
            logger.warning(
                'Сохраненная корзина найдена, но total_price отсутствует или некорректен',
                user_id=user.id,
                cart_total=cart_total,
            )
            return False

        # Try auto-purchase first
        auto_purchase_success = False
        try:
            auto_purchase_success = await auto_purchase_saved_cart_after_topup(db, user, bot=bot)
        except Exception as auto_error:
            logger.error(
                'Ошибка автоматической покупки подписки для пользователя',
                user_id=user.id,
                auto_error=auto_error,
                exc_info=True,
            )

        if auto_purchase_success:
            return False

        if not bot or not getattr(user, 'telegram_id', None):
            return False

        # Refresh balance from DB to account for any changes during auto-purchase attempt
        refreshed_user = await get_user_by_id(db, user.id)
        balance = getattr(refreshed_user or user, 'balance_kopeks', 0)

        texts = get_texts(getattr(user, 'language', 'ru'))

        # Build message based on whether balance is sufficient
        fmt = settings.format_price
        cart_total_formatted = fmt(cart_total)
        if balance >= cart_total:
            template = texts.get('BALANCE_TOPPED_UP_CART_SUFFICIENT', '')
            message_text = template.format(
                amount=fmt(amount_kopeks),
                balance=fmt(balance),
                cart_total=cart_total_formatted,
                total_amount=cart_total_formatted,
            )
        else:
            missing = cart_total - balance
            template = texts.get('BALANCE_TOPPED_UP_CART_INSUFFICIENT', '')
            message_text = template.format(
                amount=fmt(amount_kopeks),
                balance=fmt(balance),
                cart_total=cart_total_formatted,
                total_amount=cart_total_formatted,
                missing=fmt(missing),
            )

        if not message_text:
            logger.warning('Missing cart notification template', language=getattr(user, 'language', 'ru'))
            return False

        sent = False
        try:
            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.get('RETURN_TO_SUBSCRIPTION_CHECKOUT', '⬅️ Checkout'),
                            callback_data='return_to_saved_cart',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.get('MY_BALANCE_BUTTON', '💰 Balance'),
                            callback_data='menu_balance',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.get('MAIN_MENU_BUTTON', '🏠 Menu'),
                            callback_data='back_to_menu',
                        )
                    ],
                ]
            )
            await bot.send_message(
                chat_id=user.telegram_id,
                text=message_text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
            sent = True
            logger.info('Sent cart notification to user', user_id=user.id)
        except Exception as send_error:
            logger.error(
                'Failed to send cart notification to user',
                user_id=user.id,
                error=send_error,
            )

        return sent

    # Try to auto-extend expired subscription only when there is no saved cart.
    try:
        auto_extended = await try_auto_extend_expired_after_topup(db, user, bot=bot)
        if auto_extended:
            return False
    except Exception as extend_error:
        logger.error(
            'Ошибка автопродления истёкшей подписки после пополнения',
            user_id=user.id,
            error=extend_error,
            exc_info=True,
        )

    return False


# ---------------------------------------------------------------------------
# Guest purchase fulfillment (shared across all payment providers)
# ---------------------------------------------------------------------------


def _extract_guest_purchase_token(metadata: dict[str, Any] | None) -> str | None:
    """Return the purchase_token if the payment belongs to a guest purchase, else None."""
    if not isinstance(metadata, dict):
        return None
    if metadata.get('purpose') != 'guest_purchase':
        return None
    return metadata.get('purchase_token') or None


async def try_fulfill_guest_purchase(
    db: AsyncSession,
    *,
    metadata: dict[str, Any] | None,
    payment_amount_kopeks: int,
    provider_payment_id: str,
    provider_name: str,
    skip_amount_check: bool = False,
) -> bool | None:
    """Attempt to fulfill a guest purchase detected in payment metadata.

    Args:
        skip_amount_check: If True, skip the webhook/purchase amount comparison.
            Useful for providers like CryptoBot where currency conversion
            introduces imprecision.

    Returns:
        ``True``  -- guest purchase was detected and consumed (fulfilled or queued for retry).
        ``None``  -- this is NOT a guest purchase (caller should proceed normally).
    """
    purchase_token = _extract_guest_purchase_token(metadata)
    if purchase_token is None:
        return None

    from app.database.crud.landing import update_purchase_status
    from app.database.models import GuestPurchase, GuestPurchaseStatus
    from app.services.guest_purchase_service import fulfill_purchase

    try:
        # FOR UPDATE prevents concurrent webhooks from double-processing the same purchase
        result = await db.execute(select(GuestPurchase).where(GuestPurchase.token == purchase_token).with_for_update())
        existing = result.scalars().first()

        # Verify amount (skip for providers with currency conversion imprecision)
        if existing and not skip_amount_check and payment_amount_kopeks != existing.amount_kopeks:
            logger.error(
                'Webhook amount does not match guest purchase amount',
                webhook_kopeks=payment_amount_kopeks,
                purchase_kopeks=existing.amount_kopeks,
                purchase_token_prefix=purchase_token[:5],
                provider=provider_name,
            )
            await update_purchase_status(db, purchase_token, GuestPurchaseStatus.FAILED)
            return True  # consumed, even though failed

        # Idempotency: skip terminal states (and code-only gifts already in PAID)
        if (
            existing
            and existing.status
            in (
                GuestPurchaseStatus.DELIVERED.value,
                GuestPurchaseStatus.PENDING_ACTIVATION.value,
                GuestPurchaseStatus.FAILED.value,
            )
        ) or (
            existing
            and existing.status == GuestPurchaseStatus.PAID.value
            and existing.is_gift
            and not existing.gift_recipient_type
        ):
            logger.info(
                'Guest purchase already in terminal state, skipping',
                purchase_token_prefix=purchase_token[:5],
                status=existing.status,
                provider=provider_name,
            )
            await db.commit()
            return True

        # Mark as PAID (no commit -- let fulfill_purchase do atomic commit)
        await update_purchase_status(
            db,
            purchase_token,
            GuestPurchaseStatus.PAID,
            commit=False,
            payment_id=provider_payment_id,
            paid_at=datetime.now(UTC),
        )

        # Code-only gifts (is_gift=True, no recipient) stay in PAID status
        # — buyer shares the code manually, recipient activates via cabinet/bot
        if existing and existing.is_gift and not existing.gift_recipient_type:
            await db.commit()
            logger.info(
                'Code-only gift marked as PAID, skipping fulfillment',
                purchase_token_prefix=purchase_token[:5],
                provider=provider_name,
            )
            # NaloGO receipt: payment received, fulfillment deferred until code activation
            try:
                await db.refresh(existing)
                if existing.buyer:
                    from app.services.guest_purchase_service import _create_nalogo_receipt_for_purchase

                    await _create_nalogo_receipt_for_purchase(db, existing, existing.buyer)
                else:
                    logger.warning(
                        'Code-only gift has no buyer, skipping NaloGO receipt',
                        purchase_token_prefix=purchase_token[:5],
                        buyer_user_id=existing.buyer_user_id,
                    )
            except Exception:
                logger.exception(
                    'Failed to create NaloGO receipt for code-only gift',
                    purchase_token_prefix=purchase_token[:5],
                )
            return True

        # Fulfill: create user, subscription, deliver (commits on success)
        await fulfill_purchase(db, purchase_token)

        logger.info(
            'Guest purchase fulfilled',
            provider_payment_id=provider_payment_id,
            purchase_token_prefix=purchase_token[:5],
            provider=provider_name,
        )
        return True

    except Exception as guest_error:
        await db.rollback()
        logger.exception(
            'Error fulfilling guest purchase from webhook',
            provider_payment_id=provider_payment_id,
            provider=provider_name,
            error=guest_error,
        )
        # Mark as PAID (not FAILED) so retry_stuck_paid_purchases can pick it up.
        # Use a fresh session to avoid tainted-session issues after rollback.
        # The monitoring service retries PAID purchases every 5 minutes for up to 24 hours.
        try:
            from app.database.database import AsyncSessionLocal

            async with AsyncSessionLocal() as recovery_db:
                # Use FOR UPDATE to prevent TOCTOU race with concurrent webhook.
                row = await recovery_db.execute(
                    select(GuestPurchase).where(GuestPurchase.token == purchase_token).with_for_update()
                )
                current = row.scalars().first()
                if current and current.status in (
                    GuestPurchaseStatus.PENDING.value,
                    GuestPurchaseStatus.PAID.value,
                ):
                    current.status = GuestPurchaseStatus.PAID.value
                    current.payment_id = provider_payment_id
                    current.paid_at = datetime.now(UTC)
                    await recovery_db.commit()
        except Exception:
            logger.exception('Failed to mark guest purchase as PAID for retry')
        return True
