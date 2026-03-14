"""Service for guest (unauthenticated) purchases via landing pages."""

import asyncio
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth.jwt_handler import create_auto_login_token
from app.cabinet.auth.password_utils import hash_password
from app.config import settings
from app.database.crud.landing import create_guest_purchase
from app.database.crud.subscription import create_paid_subscription, get_subscription_by_user_id, replace_subscription
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.transaction import create_transaction
from app.database.crud.user import _get_or_create_default_promo_group
from app.database.models import (
    GuestPurchase,
    GuestPurchaseStatus,
    LandingPage,
    PaymentMethod,
    Tariff,
    TransactionType,
    User,
)
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

_TELEGRAM_USERNAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{4,31}$')


async def _send_admin_notification(
    purchase: GuestPurchase,
    tariff_name: str,
    *,
    is_pending_activation: bool = False,
) -> None:
    """Send admin topic notification about a guest purchase (best-effort)."""
    if not getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) or not settings.BOT_TOKEN:
        return
    try:
        from aiogram import Bot

        from app.services.admin_notification_service import AdminNotificationService

        async with Bot(token=settings.BOT_TOKEN) as bot:
            service = AdminNotificationService(bot)
            await service.send_guest_purchase_notification(
                purchase,
                tariff_name,
                is_pending_activation=is_pending_activation,
            )
    except Exception:
        logger.warning('Failed to send admin notification for guest purchase', purchase_id=purchase.id, exc_info=True)


class GuestPurchaseError(Exception):
    """Domain error for guest purchase operations."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def validate_and_calculate(
    db: AsyncSession,
    landing: LandingPage,
    tariff_id: int,
    period_days: int,
) -> tuple[Tariff, int]:
    """Validate tariff/period against landing config and return (tariff, price_kopeks).

    Raises:
        GuestPurchaseError: If tariff or period is not allowed or price is unavailable.
    """
    allowed_ids = landing.allowed_tariff_ids or []
    if tariff_id not in allowed_ids:
        raise GuestPurchaseError('Tariff is not available on this landing page')

    tariff = await get_tariff_by_id(db, tariff_id)
    if tariff is None or not tariff.is_active:
        raise GuestPurchaseError('Tariff not found or inactive')

    # Check period against landing-level override (if set)
    allowed_periods = landing.allowed_periods or {}
    if allowed_periods:
        tariff_periods_override = allowed_periods.get(str(tariff_id))
        if tariff_periods_override is not None:
            if period_days not in tariff_periods_override:
                raise GuestPurchaseError('Period is not available for this tariff on this landing page')
        else:
            # No override for this tariff -> all tariff periods allowed
            available = tariff.get_available_periods()
            if period_days not in available:
                raise GuestPurchaseError('Period is not available for this tariff')
    else:
        # No overrides at all -> use tariff's own periods
        available = tariff.get_available_periods()
        if period_days not in available:
            raise GuestPurchaseError('Period is not available for this tariff')

    price_kopeks = tariff.get_price_for_period(period_days)
    if price_kopeks is None:
        raise GuestPurchaseError('Price is not configured for this period')

    # Apply landing discount if active
    if landing.discount_percent and landing.discount_starts_at and landing.discount_ends_at:
        now = datetime.now(UTC)
        if landing.discount_starts_at <= now < landing.discount_ends_at:
            overrides = landing.discount_overrides or {}
            tariff_override = overrides.get(str(tariff_id))
            effective_discount = tariff_override if tariff_override is not None else landing.discount_percent
            price_kopeks = max(1, price_kopeks - (price_kopeks * effective_discount // 100))

    return tariff, price_kopeks


async def create_purchase(
    db: AsyncSession,
    landing: LandingPage | None,
    tariff: Tariff,
    period_days: int,
    amount_kopeks: int,
    contact_type: str,
    contact_value: str,
    payment_method: str,
    is_gift: bool = False,
    gift_recipient_type: str | None = None,
    gift_recipient_value: str | None = None,
    gift_message: str | None = None,
    source: str = 'landing',
    buyer_user_id: int | None = None,
    commit: bool = True,
) -> GuestPurchase:
    """Create a guest purchase record."""
    purchase = await create_guest_purchase(
        db,
        commit=commit,
        landing_id=landing.id if landing else None,
        tariff_id=tariff.id,
        period_days=period_days,
        amount_kopeks=amount_kopeks,
        contact_type=contact_type,
        contact_value=contact_value,
        payment_method=payment_method,
        is_gift=is_gift,
        gift_recipient_type=gift_recipient_type,
        gift_recipient_value=gift_recipient_value,
        gift_message=gift_message,
        source=source,
        buyer_user_id=buyer_user_id,
        status=GuestPurchaseStatus.PENDING.value,
    )

    logger.info(
        'Guest purchase created',
        purchase_id=purchase.id,
        token_prefix=purchase.token[:5],
        landing_slug=landing.slug if landing else None,
        tariff_id=tariff.id,
        period_days=period_days,
        amount_kopeks=amount_kopeks,
        is_gift=is_gift,
        source=source,
    )

    return purchase


async def fulfill_purchase(
    db: AsyncSession,
    purchase_token: str,
    pre_resolved_telegram_id: int | None = None,
) -> GuestPurchase | None:
    """After payment: find/create user, create subscription, send notification.

    Uses SELECT ... FOR UPDATE to prevent concurrent fulfillment of the same purchase.
    The PENDING_ACTIVATION path commits early and returns (terminal for this call).
    The DELIVERED path commits after subscription creation.
    Returns the updated purchase or None if not found.

    Args:
        pre_resolved_telegram_id: If caller already resolved the recipient's telegram_id
            via Bot API, pass it here to avoid a duplicate API call.
    """
    result = await db.execute(select(GuestPurchase).where(GuestPurchase.token == purchase_token).with_for_update())
    purchase = result.scalars().first()

    if purchase is None:
        logger.warning('Fulfill called for unknown purchase', token_prefix=purchase_token[:5])
        return None

    if purchase.status != GuestPurchaseStatus.PAID.value:
        logger.warning(
            'Fulfill called for purchase not in PAID status',
            token_prefix=purchase_token[:5],
            current_status=purchase.status,
        )
        return purchase

    try:
        # Determine recipient contact info
        recipient_type, recipient_value = _get_recipient_contact(purchase)

        # Find or create user for the recipient (no commit — stays within our transaction)
        user, is_new_account = await _find_or_create_user(
            db,
            recipient_type,
            recipient_value,
            purchase=purchase,
            pre_resolved_telegram_id=pre_resolved_telegram_id,
        )

        # Load tariff early — needed for both PENDING_ACTIVATION and DELIVERED paths
        tariff = await get_tariff_by_id(db, purchase.tariff_id)
        if tariff is None:
            logger.error('Tariff not found during fulfillment', tariff_id=purchase.tariff_id)
            raise GuestPurchaseError('Tariff not found', status_code=500)

        # Resolve notification params before any commit (avoids lazy-loading after commit)
        notification_tariff_name = tariff.name
        notification_language = user.language or 'ru'

        # Verify the tariff still has a price configured for this period.
        # We do NOT re-verify the exact amount because discounts, price changes,
        # or promo codes may have altered the price at purchase time. The amount
        # was validated server-side in validate_and_calculate() and the payment
        # provider confirmed the charged amount.
        expected_price = tariff.get_price_for_period(purchase.period_days)
        if expected_price is None:
            logger.error(
                'Price no longer configured for period — aborting fulfillment',
                purchase_id=purchase.id,
                tariff_id=tariff.id,
                period_days=purchase.period_days,
            )
            purchase.status = GuestPurchaseStatus.FAILED.value
            await db.commit()
            return purchase

        # Check if user already has a subscription
        existing_subscription = await get_subscription_by_user_id(db, user.id)
        if existing_subscription is not None and (existing_subscription.is_active or purchase.is_gift):
            # Active subscription or gift with any existing subscription — hold for manual activation
            purchase.status = GuestPurchaseStatus.PENDING_ACTIVATION.value
            purchase.user_id = user.id
            if recipient_type == 'email' and not purchase.is_gift and is_new_account:
                purchase.auto_login_token = create_auto_login_token(user.id)
            await db.commit()
            await db.refresh(purchase, attribute_names=['landing', 'user'])

            try:
                await send_guest_notification(
                    purchase,
                    is_pending_activation=True,
                    tariff_name=notification_tariff_name,
                    language=notification_language,
                    is_new_account=is_new_account,
                )
            except Exception:
                logger.exception('Failed to send pending_activation notification', purchase_id=purchase.id)

            await _send_admin_notification(purchase, notification_tariff_name, is_pending_activation=True)

            # Clear plaintext password after email delivery
            if purchase.cabinet_password:
                purchase.cabinet_password = None
                await db.commit()

            logger.info(
                'Guest purchase held for activation (existing subscription)',
                purchase_id=purchase.id,
                token_prefix=purchase_token[:5],
                user_id=user.id,
            )
            return purchase

        if existing_subscription is not None:
            # Expired/inactive subscription — replace it
            existing_subscription.tariff_id = tariff.id
            subscription = await replace_subscription(
                db,
                existing_subscription,
                duration_days=purchase.period_days,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=tariff.allowed_squads or [],
                is_trial=False,
                update_server_counters=True,
            )
        else:
            # No subscription at all — create new
            subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=purchase.period_days,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=tariff.allowed_squads or [],
                tariff_id=tariff.id,
                update_server_counters=True,
            )

        subscription_service = SubscriptionService()
        await subscription_service.create_remnawave_user(db, subscription)
        await db.refresh(subscription)

        purchase.subscription_url = subscription.subscription_url
        purchase.subscription_crypto_link = subscription.subscription_crypto_link

        # Update purchase directly (already locked — no need to re-fetch)
        purchase.status = GuestPurchaseStatus.DELIVERED.value
        purchase.user_id = user.id
        purchase.delivered_at = datetime.now(UTC)
        if recipient_type == 'email' and not purchase.is_gift and is_new_account:
            purchase.auto_login_token = create_auto_login_token(user.id)

        await db.commit()
        await db.refresh(purchase, attribute_names=['landing', 'user'])

        # Create transaction so promo group auto-assignment and contest tracking work
        try:
            payment_method_enum = _resolve_payment_method(purchase.payment_method)
            await create_transaction(
                db=db,
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=purchase.amount_kopeks,
                description=f'Покупка подписки через лендинг ({notification_tariff_name}, {purchase.period_days} дн.)',
                payment_method=payment_method_enum,
                external_id=purchase.payment_id,
                is_completed=True,
            )
        except Exception:
            logger.exception('Failed to create transaction for guest purchase', purchase_id=purchase.id)

        try:
            await send_guest_notification(
                purchase,
                is_pending_activation=False,
                tariff_name=notification_tariff_name,
                language=notification_language,
                is_new_account=is_new_account,
            )
        except Exception:
            logger.exception('Failed to send delivery notification', purchase_id=purchase.id)

        await _send_admin_notification(purchase, notification_tariff_name, is_pending_activation=False)

        # Clear plaintext password after email delivery — no longer needed in DB
        if purchase.cabinet_password:
            purchase.cabinet_password = None
            await db.commit()

        logger.info(
            'Guest purchase fulfilled',
            purchase_id=purchase.id,
            token_prefix=purchase_token[:5],
            user_id=user.id,
            recipient_type=recipient_type,
        )

    except GuestPurchaseError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        logger.exception(
            'Failed to fulfill purchase',
            token_prefix=purchase_token[:5],
            purchase_id=purchase.id,
        )
        raise GuestPurchaseError('Purchase fulfillment failed', status_code=500)

    return purchase


def _resolve_payment_method(method_str: str | None) -> PaymentMethod | None:
    """Convert payment method string from GuestPurchase to PaymentMethod enum."""
    if not method_str:
        return None
    # Try exact match first (handles 'telegram_stars', 'kassa_ai', 'yookassa', etc.)
    try:
        return PaymentMethod(method_str)
    except ValueError:
        pass
    # Strip sub-option suffix ('yookassa_sbp' → 'yookassa', 'platega_2' → 'platega')
    if '_' in method_str:
        base_method = method_str.split('_')[0]
        try:
            return PaymentMethod(base_method)
        except ValueError:
            pass
    logger.debug('Unknown payment method for transaction', method=method_str)
    return None


def _mask_email(email: str) -> str:
    """Mask email for logging: 'user@example.com' -> 'u***@e***.com'."""
    if not email:
        return '***'
    parts = email.split('@')
    if len(parts) != 2:
        return '***'
    local = parts[0][0] + '***' if parts[0] else '***'
    domain_parts = parts[1].split('.')
    if not domain_parts[0]:
        return f'{local}@***'
    domain = domain_parts[0][0] + '***'
    tld = domain_parts[-1] if len(domain_parts) > 1 else ''
    return f'{local}@{domain}.{tld}'


async def _find_or_create_user(
    db: AsyncSession,
    contact_type: Literal['email', 'telegram'],
    contact_value: str,
    purchase: GuestPurchase | None = None,
    pre_resolved_telegram_id: int | None = None,
) -> tuple[User, bool]:
    """Find user by email/telegram username or create a new one.

    For email contacts: creates a verified cabinet account with generated password.
    For telegram contacts: creates user without password (QR flow only).

    Returns (user, is_new_account) where is_new_account means a new password was generated.

    Args:
        pre_resolved_telegram_id: If caller already resolved the telegram_id via Bot API,
            pass it here to skip the redundant API call.

    NOTE: Does NOT commit — caller is responsible for committing the transaction.
    This preserves FOR UPDATE locks held by the caller.
    """
    if contact_type == 'email':
        result = await db.execute(select(User).where(User.email == contact_value))
        user = result.scalars().first()
        if user:
            is_new_account = False
            if not user.password_hash:
                # User without cabinet access — generate credentials
                plain_password = secrets.token_urlsafe(12)
                user.password_hash = hash_password(plain_password)
                if purchase:
                    purchase.cabinet_password = plain_password
                is_new_account = True
            # Fix email_verified for all existing guest users
            if not user.email_verified:
                user.email_verified = True
                user.email_verified_at = datetime.now(UTC)
            # Ensure default promo group
            if not user.promo_group_id:
                default_group = await _get_or_create_default_promo_group(db)
                user.promo_group_id = default_group.id
            return user, is_new_account

        # Create new email user with verified cabinet account
        plain_password = secrets.token_urlsafe(12)
        default_group = await _get_or_create_default_promo_group(db)
        user = User(
            auth_type='email',
            email=contact_value,
            email_verified=True,
            email_verified_at=datetime.now(UTC),
            password_hash=hash_password(plain_password),
            promo_group_id=default_group.id,
        )
        if purchase:
            purchase.cabinet_password = plain_password
        try:
            async with db.begin_nested():
                db.add(user)
                await db.flush()
        except IntegrityError:
            result = await db.execute(select(User).where(User.email == contact_value))
            user = result.scalars().first()
            if user:
                # Race condition — user was created concurrently
                if purchase:
                    purchase.cabinet_password = None
                is_new_account = False
                if not user.password_hash:
                    regen_password = secrets.token_urlsafe(12)
                    user.password_hash = hash_password(regen_password)
                    if purchase:
                        purchase.cabinet_password = regen_password
                    is_new_account = True
                if not user.email_verified:
                    user.email_verified = True
                    user.email_verified_at = datetime.now(UTC)
                if not user.promo_group_id:
                    default_group = await _get_or_create_default_promo_group(db)
                    user.promo_group_id = default_group.id
                return user, is_new_account
            raise
        logger.info(
            'Created new email user with cabinet account for guest purchase',
            user_id=user.id,
            email_masked=_mask_email(contact_value),
        )
        return user, True

    if contact_type != 'telegram':
        raise GuestPurchaseError(f'Unsupported contact type: {contact_type}', status_code=500)

    username = contact_value.lstrip('@')
    if not _TELEGRAM_USERNAME_RE.match(username):
        raise GuestPurchaseError('Invalid Telegram username format', status_code=400)
    normalized = username.lower()

    # Try to resolve telegram_id via Bot API (works if user has interacted with the bot)
    resolved_telegram_id: int | None = pre_resolved_telegram_id
    if resolved_telegram_id is None:
        try:
            from aiogram import Bot

            async with Bot(token=settings.BOT_TOKEN) as bot:
                chat = await asyncio.wait_for(
                    bot.get_chat(chat_id=f'@{username}'),
                    timeout=5.0,
                )
                resolved_telegram_id = chat.id
                # Use the canonical username from Telegram if available
                if chat.username:
                    username = chat.username
                    normalized = username.lower()
        except Exception as exc:
            logger.debug('Could not resolve telegram_id for username', username=username, error=str(exc))

    # Search by telegram_id first (most reliable), then by username (case-insensitive)
    user = None
    if resolved_telegram_id:
        result = await db.execute(
            select(User).where(User.telegram_id == resolved_telegram_id),
        )
        user = result.scalars().first()

    if not user:
        result = await db.execute(
            select(User).where(func.lower(User.username) == normalized),
        )
        user = result.scalars().first()

    if user:
        # Backfill telegram_id if we resolved it and user doesn't have it yet
        if resolved_telegram_id and not user.telegram_id:
            try:
                async with db.begin_nested():
                    user.telegram_id = resolved_telegram_id
                    await db.flush()
            except IntegrityError:
                logger.warning(
                    'Could not backfill telegram_id (unique constraint)',
                    user_id=user.id,
                    resolved_telegram_id=resolved_telegram_id,
                )
                await db.refresh(user)
        # Ensure default promo group
        if not user.promo_group_id:
            default_group = await _get_or_create_default_promo_group(db)
            user.promo_group_id = default_group.id
        return user, False

    # Create new telegram user
    default_group = await _get_or_create_default_promo_group(db)
    user = User(
        auth_type='telegram',
        username=username,
        telegram_id=resolved_telegram_id,
        promo_group_id=default_group.id,
    )
    try:
        async with db.begin_nested():
            db.add(user)
            await db.flush()
    except IntegrityError:
        if resolved_telegram_id:
            result = await db.execute(select(User).where(User.telegram_id == resolved_telegram_id))
            user = result.scalars().first()
            if user:
                if not user.promo_group_id:
                    default_group = await _get_or_create_default_promo_group(db)
                    user.promo_group_id = default_group.id
                return user, False
        result = await db.execute(select(User).where(func.lower(User.username) == normalized))
        user = result.scalars().first()
        if user:
            if not user.promo_group_id:
                default_group = await _get_or_create_default_promo_group(db)
                user.promo_group_id = default_group.id
            return user, False
        raise
    logger.info(
        'Created new telegram user for guest purchase',
        user_id=user.id,
        username=username,
        has_telegram_id=resolved_telegram_id is not None,
    )
    return user, False


def _get_recipient_contact(purchase: GuestPurchase) -> tuple[str, str]:
    """Return (contact_type, contact_value) for the purchase recipient."""
    if purchase.is_gift and purchase.gift_recipient_type and purchase.gift_recipient_value:
        return purchase.gift_recipient_type, purchase.gift_recipient_value
    return purchase.contact_type, purchase.contact_value


async def _send_telegram_gift_notification(
    purchase: GuestPurchase,
    *,
    is_pending_activation: bool = False,
    tariff_name: str = '',
) -> None:
    """Send Telegram bot message to gift recipient if they have a telegram_id."""
    if not settings.BOT_TOKEN:
        return
    user = purchase.user
    if not user or not user.telegram_id:
        return

    try:
        import html as html_mod

        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        gift_from = ''
        if purchase.contact_value:
            safe_name = html_mod.escape(purchase.contact_value)
            gift_from = f'\nОт: {safe_name}'

        gift_msg = ''
        if purchase.gift_message:
            safe_msg = html_mod.escape(purchase.gift_message)
            gift_msg = f'\n\n"{safe_msg}"'

        safe_tariff = html_mod.escape(tariff_name) if tariff_name else ''
        period_text = f'{purchase.period_days} дн.' if purchase.period_days else ''
        tariff_text = f'{safe_tariff} — {period_text}' if safe_tariff else period_text

        text = f'🎁 <b>Вам подарили VPN подписку!</b>\n{tariff_text}{gift_from}{gift_msg}'

        keyboard = None
        if is_pending_activation:
            text += '\n\nУ вас уже есть активная подписка. Нажмите кнопку ниже, чтобы активировать подарок (текущая подписка будет заменена).'
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text='Активировать подарок',
                            callback_data=f'gift_activate:{purchase.id}',
                        )
                    ]
                ]
            )

        async with Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        ) as bot:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=keyboard,
            )

        logger.info(
            'Telegram gift notification sent',
            purchase_id=purchase.id,
            recipient_telegram_id=user.telegram_id,
            is_pending_activation=is_pending_activation,
        )
    except Exception:
        logger.warning(
            'Failed to send Telegram gift notification',
            purchase_id=purchase.id,
            recipient_telegram_id=user.telegram_id if user else None,
            exc_info=True,
        )


async def send_guest_notification(
    purchase: GuestPurchase,
    *,
    is_pending_activation: bool = False,
    tariff_name: str = '',
    language: str = 'ru',
    is_new_account: bool = False,
) -> None:
    """Send notification for guest purchase delivery or activation requirement.

    For telegram gift contacts, sends a Telegram bot message to the recipient.
    For telegram non-gift contacts, no notification is sent (success page only).
    For email contacts, sends an email notification.
    For gifts, notification goes to the recipient, not the buyer.

    Args:
        purchase: The guest purchase record.
        is_pending_activation: Whether this is a pending activation notification.
        tariff_name: Pre-resolved tariff name (avoids lazy-loading after commit).
        language: User language for email template (avoids lazy-loading user relationship).
        is_new_account: Whether a new cabinet account / password was just created.
    """
    # Lazy imports to avoid circular dependencies (cabinet services -> services -> cabinet)
    from app.cabinet.services.email_service import email_service
    from app.cabinet.services.email_templates import EmailNotificationTemplates
    from app.services.notification_delivery_service import NotificationType

    recipient_type, recipient_value = _get_recipient_contact(purchase)

    if recipient_type == 'telegram':
        if purchase.is_gift:
            await _send_telegram_gift_notification(
                purchase, is_pending_activation=is_pending_activation, tariff_name=tariff_name
            )
        return

    recipient_email = recipient_value
    if recipient_type != 'email':
        return

    cabinet_base = (settings.CABINET_URL or '').rstrip('/')
    success_page_url = f'{cabinet_base}/buy/success/{purchase.token}'
    # For gift pending activation: add hint so the recipient's success page shows the activate button
    if is_pending_activation and purchase.is_gift:
        success_page_url += '?activate=1'

    context = {
        'tariff_name': tariff_name,
        'period_days': purchase.period_days,
        'success_page_url': success_page_url,
        'subscription_url': purchase.subscription_url or '',
        'is_gift': purchase.is_gift,
        'gift_message': purchase.gift_message,
        'is_existing_user': not is_new_account,
        'cabinet_url': cabinet_base,
        'cabinet_email': recipient_email,
        'cabinet_password': purchase.cabinet_password,
    }

    if is_pending_activation:
        notification_type = NotificationType.GUEST_ACTIVATION_REQUIRED
    elif purchase.is_gift:
        notification_type = NotificationType.GUEST_GIFT_RECEIVED
    else:
        notification_type = NotificationType.GUEST_SUBSCRIPTION_DELIVERED

    templates = EmailNotificationTemplates()

    # Check DB override first, then fall back to hardcoded template
    template = None
    try:
        from app.cabinet.services.email_template_overrides import get_rendered_override

        rendered = await get_rendered_override(notification_type.value, language, context)
        if rendered:
            subject, body_html = rendered
            template = {
                'subject': subject,
                'body_html': body_html,
            }
    except Exception as e:
        logger.debug('Failed to check template override', e=e)

    if not template:
        template = templates.get_template(notification_type, language, context)

    if not template:
        logger.warning('No email template found for guest notification', notification_type=notification_type.value)
        return

    result = await asyncio.to_thread(
        email_service.send_email,
        to_email=recipient_email,
        subject=template['subject'],
        body_html=template['body_html'],
    )

    if result:
        logger.info(
            'Guest purchase notification sent',
            purchase_id=purchase.id,
            notification_type=notification_type.value,
            recipient_masked=_mask_email(recipient_email),
        )
    else:
        logger.warning(
            'Failed to send guest purchase notification',
            purchase_id=purchase.id,
            notification_type=notification_type.value,
        )

    # Send separate credentials email for new accounts (self-purchases and gifts)
    if purchase.cabinet_password:
        cred_template = None
        try:
            from app.cabinet.services.email_template_overrides import get_rendered_override

            cred_rendered = await get_rendered_override(
                NotificationType.GUEST_CABINET_CREDENTIALS.value, language, context
            )
            if cred_rendered:
                cred_subject, cred_body = cred_rendered
                cred_template = {
                    'subject': cred_subject,
                    'body_html': cred_body,
                }
        except Exception as e:
            logger.debug('Failed to check credentials template override', e=e)
        if not cred_template:
            cred_template = templates.get_template(NotificationType.GUEST_CABINET_CREDENTIALS, language, context)
        if cred_template:
            cred_result = await asyncio.to_thread(
                email_service.send_email,
                to_email=recipient_email,
                subject=cred_template['subject'],
                body_html=cred_template['body_html'],
            )
            if cred_result:
                logger.info(
                    'Cabinet credentials email sent',
                    purchase_id=purchase.id,
                    recipient_masked=_mask_email(recipient_email),
                )
            else:
                logger.warning('Failed to send cabinet credentials email', purchase_id=purchase.id)


async def activate_purchase(db: AsyncSession, purchase_token: str, *, skip_notification: bool = False) -> GuestPurchase:
    """Activate a PENDING_ACTIVATION purchase by replacing or creating a subscription.

    Uses SELECT ... FOR UPDATE to prevent concurrent activation.
    Raises GuestPurchaseError on validation failures.
    Returns the updated purchase (status=DELIVERED).
    """
    result = await db.execute(select(GuestPurchase).where(GuestPurchase.token == purchase_token).with_for_update())
    purchase = result.scalars().first()

    if purchase is None:
        raise GuestPurchaseError('Purchase not found', status_code=404)

    # Idempotent: already delivered
    if purchase.status == GuestPurchaseStatus.DELIVERED.value:
        return purchase

    if purchase.status != GuestPurchaseStatus.PENDING_ACTIVATION.value:
        raise GuestPurchaseError('Purchase is not pending activation', status_code=400)

    tariff = await get_tariff_by_id(db, purchase.tariff_id)
    if tariff is None:
        raise GuestPurchaseError('Tariff not found', status_code=500)

    if not purchase.user_id:
        raise GuestPurchaseError('No user linked to purchase', status_code=500)

    user_result = await db.execute(select(User).where(User.id == purchase.user_id))
    user = user_result.scalars().first()
    if user is None:
        raise GuestPurchaseError('User not found', status_code=500)

    # Ensure email users have cabinet access
    is_new_account = False
    if user.auth_type == 'email' and not user.password_hash:
        plain_password = secrets.token_urlsafe(12)
        user.password_hash = hash_password(plain_password)
        purchase.cabinet_password = plain_password
        is_new_account = True
    if user.auth_type == 'email' and not user.email_verified:
        user.email_verified = True
        user.email_verified_at = datetime.now(UTC)

    # Resolve notification params before any commit (avoids lazy-loading after commit)
    notification_tariff_name = tariff.name
    notification_language = user.language or 'ru'

    try:
        existing_subscription = await get_subscription_by_user_id(db, user.id)
        subscription_service = SubscriptionService()

        if existing_subscription is not None:
            subscription = await replace_subscription(
                db,
                existing_subscription,
                duration_days=purchase.period_days,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=tariff.allowed_squads or [],
                is_trial=False,
                update_server_counters=True,
                commit=False,
            )
            subscription.tariff_id = tariff.id
        else:
            subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=purchase.period_days,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=tariff.allowed_squads or [],
                tariff_id=tariff.id,
                update_server_counters=True,
                commit=False,
            )

        await subscription_service.create_remnawave_user(db, subscription)
        await db.refresh(subscription)

        purchase.subscription_url = subscription.subscription_url
        purchase.subscription_crypto_link = subscription.subscription_crypto_link
        purchase.status = GuestPurchaseStatus.DELIVERED.value
        purchase.delivered_at = datetime.now(UTC)
        if user.auth_type == 'email' and not purchase.is_gift and is_new_account:
            purchase.auto_login_token = create_auto_login_token(user.id)

        # Single atomic commit: subscription + purchase status + user changes
        await db.commit()
        await db.refresh(purchase, attribute_names=['landing', 'user'])

        # Create transaction so promo group auto-assignment and contest tracking work
        try:
            payment_method_enum = _resolve_payment_method(purchase.payment_method)
            await create_transaction(
                db=db,
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=purchase.amount_kopeks,
                description=f'Покупка подписки через лендинг ({notification_tariff_name}, {purchase.period_days} дн.)',
                payment_method=payment_method_enum,
                external_id=purchase.payment_id,
                is_completed=True,
            )
        except Exception:
            logger.exception('Failed to create transaction for activated purchase', purchase_id=purchase.id)

        if not skip_notification:
            try:
                await send_guest_notification(
                    purchase,
                    is_pending_activation=False,
                    tariff_name=notification_tariff_name,
                    language=notification_language,
                    is_new_account=is_new_account,
                )
            except Exception:
                logger.exception('Failed to send delivery notification after activation', purchase_id=purchase.id)

        await _send_admin_notification(purchase, notification_tariff_name, is_pending_activation=False)

        # Clear plaintext password after email delivery
        if purchase.cabinet_password:
            purchase.cabinet_password = None
            await db.commit()

        logger.info(
            'Guest purchase activated',
            purchase_id=purchase.id,
            token_prefix=purchase_token[:5],
            user_id=user.id,
        )

    except GuestPurchaseError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        logger.exception('Failed to activate purchase', purchase_id=purchase.id)
        raise GuestPurchaseError('Activation failed, please try again', status_code=500)

    return purchase


async def retry_stuck_paid_purchases(
    db: AsyncSession,
    stale_minutes: int = 5,
    limit: int = 10,
    max_age_hours: int = 24,
) -> int:
    """Retry fulfillment for purchases stuck in PAID status.

    Finds purchases that have been in PAID status for longer than stale_minutes
    (but not older than max_age_hours) and attempts to fulfill them in isolated
    sessions. Returns the number of successfully retried purchases.

    Purchases older than max_age_hours are left for manual investigation.
    """
    from app.database.database import AsyncSessionLocal

    cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)
    max_age = datetime.now(UTC) - timedelta(hours=max_age_hours)

    # Collect tokens only — each retry gets its own session.
    # NULL paid_at is included via or_() as a safety net for data anomalies.
    result = await db.execute(
        select(GuestPurchase.token)
        .where(
            GuestPurchase.status == GuestPurchaseStatus.PAID.value,
            or_(GuestPurchase.paid_at < cutoff, GuestPurchase.paid_at.is_(None)),
            or_(GuestPurchase.paid_at > max_age, GuestPurchase.paid_at.is_(None)),
            # Exclude code-only gifts — they stay PAID intentionally until activated
            ~(GuestPurchase.is_gift.is_(True) & GuestPurchase.gift_recipient_type.is_(None)),
        )
        .order_by(GuestPurchase.paid_at.asc().nulls_first())
        .limit(limit)
    )
    tokens = result.scalars().all()

    if not tokens:
        return 0

    retried = 0
    for token in tokens:
        try:
            async with AsyncSessionLocal() as retry_db:
                await fulfill_purchase(retry_db, token)
                retried += 1
                logger.info('Retried stuck purchase successfully', token_prefix=token[:5])
        except Exception:
            logger.exception('Failed to retry stuck purchase', token_prefix=token[:5])

    return retried


async def retry_stuck_pending_activation(
    db: AsyncSession,
    stale_minutes: int = 10,
    limit: int = 10,
    max_age_hours: int = 24,
) -> int:
    """Retry activation for purchases stuck in PENDING_ACTIVATION status.

    This handles the case where activate_purchase() failed after the status
    was already transitioned to PENDING_ACTIVATION (e.g., Remnawave panel was
    temporarily down). Each retry runs in an isolated session.
    """
    from app.database.database import AsyncSessionLocal

    cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)
    max_age = datetime.now(UTC) - timedelta(hours=max_age_hours)

    result = await db.execute(
        select(GuestPurchase.token)
        .where(
            GuestPurchase.status == GuestPurchaseStatus.PENDING_ACTIVATION.value,
            or_(GuestPurchase.paid_at < cutoff, GuestPurchase.paid_at.is_(None)),
            or_(GuestPurchase.paid_at > max_age, GuestPurchase.paid_at.is_(None)),
            GuestPurchase.user_id.isnot(None),
        )
        .order_by(GuestPurchase.paid_at.asc().nulls_first())
        .limit(limit)
    )
    tokens = result.scalars().all()

    if not tokens:
        return 0

    retried = 0
    for token in tokens:
        try:
            async with AsyncSessionLocal() as retry_db:
                await activate_purchase(retry_db, token)
                retried += 1
                logger.info('Retried stuck pending_activation successfully', token_prefix=token[:5])
        except Exception:
            logger.exception('Failed to retry stuck pending_activation', token_prefix=token[:5])

    return retried
