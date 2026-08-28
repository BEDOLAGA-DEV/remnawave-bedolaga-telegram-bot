"""Shared domain service for gift subscription catalog, quotes, and balance purchases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.system_setting import get_setting_value
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
from app.database.crud.user import lock_user_for_pricing, subtract_user_balance
from app.database.models import (
    GuestPurchase,
    GuestPurchaseStatus,
    PaymentMethod,
    Tariff,
    Transaction,
    TransactionType,
    User,
)
from app.services.guest_purchase_service import create_purchase
from app.services.pricing_engine import RenewalPricing, pricing_engine


logger = structlog.get_logger(__name__)

GIFT_ENABLED_KEY = 'CABINET_GIFT_ENABLED'


# ── Read Models ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GiftQuote:
    """Immutable quote for a gift tariff and period."""

    tariff_id: int
    tariff_name: str
    period_days: int
    traffic_limit_gb: int | None
    device_limit: int
    original_price_kopeks: int
    final_price_kopeks: int
    promo_group_discount_kopeks: int
    promo_offer_discount_kopeks: int
    consumes_promo_offer: bool

    @property
    def total_discount_kopeks(self) -> int:
        return self.promo_group_discount_kopeks + self.promo_offer_discount_kopeks

    @property
    def discount_percent(self) -> int:
        if self.original_price_kopeks <= 0:
            return 0
        diff = self.original_price_kopeks - self.final_price_kopeks
        if diff <= 0:
            return 0
        return int(round(diff * 100 / self.original_price_kopeks))


@dataclass(frozen=True)
class GiftTariffOffer:
    """Immutable catalog offer for an eligible gift tariff."""

    tariff_id: int
    tariff_name: str
    tariff_description: str | None
    traffic_limit_gb: int | None
    device_limit: int
    display_order: int
    quotes: tuple[GiftQuote, ...]


@dataclass(frozen=True)
class GiftRecipient:
    """Cabinet-compatible optional recipient details."""

    recipient_type: str | None = None
    recipient_value: str | None = None
    gift_message: str | None = None


@dataclass(frozen=True)
class GiftPurchaseResult:
    """Result of a balance gift purchase or idempotent replay."""

    purchase: GuestPurchase
    transaction: Transaction
    quote: GiftQuote
    remaining_balance_kopeks: int
    is_idempotent_replay: bool


# ── Typed Failures ──────────────────────────────────────────────────────────


class GiftError(Exception):
    """Base domain exception for gift operations."""


class GiftFeatureDisabledError(GiftError):
    """Raised when gift feature is disabled globally."""


class GiftTariffUnavailableError(GiftError):
    """Raised when selected tariff is not found, inactive, or not enabled for gifts."""


class GiftPeriodUnavailableError(GiftError):
    """Raised when selected period has no configured price for the tariff."""


class GiftPurchaseRestrictedError(GiftError):
    """Raised when the buyer is restricted from purchasing subscriptions."""


class GiftInsufficientBalanceError(GiftError):
    """Raised when buyer balance is insufficient to complete the gift debit."""

    def __init__(self, required_kopeks: int, available_kopeks: int) -> None:
        super().__init__(
            f'Insufficient balance: required {required_kopeks} kopeks, available {available_kopeks} kopeks'
        )
        self.required_kopeks = required_kopeks
        self.available_kopeks = available_kopeks


class GiftPriceChangedError(GiftError):
    """Raised when expected price does not match fresh quote at confirmation time."""

    def __init__(self, expected_price_kopeks: int, fresh_quote: GiftQuote) -> None:
        super().__init__(f'Gift price changed from {expected_price_kopeks} to {fresh_quote.final_price_kopeks} kopeks')
        self.expected_price_kopeks = expected_price_kopeks
        self.fresh_quote = fresh_quote


class GiftIdempotencyConflictError(GiftError):
    """Raised when an idempotency key is reused with different buyer, tariff, or period parameters."""


# ── Helpers ─────────────────────────────────────────────────────────────────


def _derive_buyer_contact(user: User) -> tuple[str, str]:
    """Derive contact_type and contact_value for buyer."""
    if user.email:
        return 'email', user.email
    if user.username:
        return 'telegram', f'@{user.username}'
    return 'telegram', f'id:{user.telegram_id or user.id}'


def _build_gift_transaction_external_id(source: str, idempotency_key: str) -> str:
    """Build canonical transaction external_id for idempotency replay."""
    return f'gift_{source}_{idempotency_key}'


async def _find_gift_transaction(
    db: AsyncSession,
    purchase: GuestPurchase,
    idempotency_key: str,
    fallback_source: str,
) -> Transaction | None:
    """Найти списание, которым оплачен именно этот подарок.

    Ключ собирается из ``purchase.source``: повтор может прийти из другого канала,
    чем исходная покупка, и тогда аргумент ``source`` вызывающего дал бы чужой
    external_id. Если ничего не нашлось — возвращаем ``None``: подставить чужую
    транзакцию хуже, чем не показать никакой.
    """
    candidates = {
        _build_gift_transaction_external_id(purchase.source or fallback_source, idempotency_key),
        _build_gift_transaction_external_id(fallback_source, idempotency_key),
    }
    result = await db.execute(
        select(Transaction).where(
            Transaction.external_id.in_(candidates),
            Transaction.payment_method == PaymentMethod.BALANCE.value,
        )
    )
    return result.scalars().first()


def _build_quote_from_pricing(
    tariff: Tariff,
    period_days: int,
    base_price: int,
    pricing_result: RenewalPricing,
) -> GiftQuote:
    """Construct GiftQuote from Tariff and RenewalPricing result."""
    final_price = max(1, pricing_result.final_total)
    return GiftQuote(
        tariff_id=tariff.id,
        tariff_name=tariff.name,
        period_days=period_days,
        traffic_limit_gb=tariff.traffic_limit_gb,
        device_limit=tariff.device_limit if tariff.device_limit is not None else 1,
        original_price_kopeks=base_price,
        final_price_kopeks=final_price,
        promo_group_discount_kopeks=pricing_result.promo_group_discount,
        promo_offer_discount_kopeks=pricing_result.promo_offer_discount,
        consumes_promo_offer=pricing_result.promo_offer_discount > 0,
    )


def _build_quote_from_purchase(purchase: GuestPurchase) -> GiftQuote:
    """Construct GiftQuote from an existing persisted purchase for idempotent replay."""
    tariff = purchase.tariff
    tariff_name = tariff.name if tariff else ''
    traffic_limit = tariff.traffic_limit_gb if tariff else None
    device_limit = tariff.device_limit if tariff and tariff.device_limit is not None else 1
    base_price = tariff.get_price_for_period(purchase.period_days) if tariff else purchase.amount_kopeks
    original_price = base_price if base_price is not None else purchase.amount_kopeks
    final_price = purchase.amount_kopeks
    diff = max(0, original_price - final_price)
    return GiftQuote(
        tariff_id=purchase.tariff_id or 0,
        tariff_name=tariff_name,
        period_days=purchase.period_days,
        traffic_limit_gb=traffic_limit,
        device_limit=device_limit,
        original_price_kopeks=original_price,
        final_price_kopeks=final_price,
        promo_group_discount_kopeks=0,
        promo_offer_discount_kopeks=diff,
        consumes_promo_offer=diff > 0,
    )


# ── Domain Service Operations ───────────────────────────────────────────────


async def is_gift_enabled(db: AsyncSession) -> bool:
    """Check if the gift feature is enabled via system settings."""
    value = await get_setting_value(db, GIFT_ENABLED_KEY)
    if value is not None:
        return value.lower() == 'true'
    return False


async def list_gift_offers(db: AsyncSession, buyer: User | None = None) -> list[GiftTariffOffer]:
    """List eligible tariffs and their personalized quotes for gift purchase."""
    if not await is_gift_enabled(db):
        return []

    result = await db.execute(
        select(Tariff)
        .where(Tariff.is_active.is_(True), Tariff.show_in_gift.is_(True))
        .order_by(Tariff.display_order.asc(), Tariff.id.asc())
    )
    tariffs = result.scalars().all()

    offers: list[GiftTariffOffer] = []
    for tariff in tariffs:
        period_days_list = tariff.get_available_periods()
        quotes: list[GiftQuote] = []
        for days in period_days_list:
            base_price = tariff.get_price_for_period(days)
            if base_price is None:
                continue

            pricing_result = await pricing_engine.calculate_tariff_purchase_price(
                tariff,
                days,
                device_limit=tariff.device_limit,
                user=buyer,
            )
            quotes.append(_build_quote_from_pricing(tariff, days, base_price, pricing_result))

        if not quotes:
            continue

        offers.append(
            GiftTariffOffer(
                tariff_id=tariff.id,
                tariff_name=tariff.name,
                tariff_description=tariff.description,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit if tariff.device_limit is not None else 1,
                display_order=tariff.display_order or 0,
                quotes=tuple(quotes),
            )
        )

    return offers


async def quote_gift_purchase(
    db: AsyncSession,
    buyer: User | None,
    tariff_id: int,
    period_days: int,
) -> GiftQuote:
    """Calculate personalized quote for a specific tariff and period."""
    if not await is_gift_enabled(db):
        raise GiftFeatureDisabledError('Gift feature is not enabled')

    tariff = await get_tariff_by_id(db, tariff_id)
    if tariff is None or not tariff.is_active or not tariff.show_in_gift:
        raise GiftTariffUnavailableError('Tariff not found or inactive')

    base_price = tariff.get_price_for_period(period_days)
    if base_price is None:
        raise GiftPeriodUnavailableError(f'Price is not configured for period {period_days} days')

    pricing_result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period_days,
        device_limit=tariff.device_limit,
        user=buyer,
    )
    return _build_quote_from_pricing(tariff, period_days, base_price, pricing_result)


async def purchase_gift_from_balance(
    db: AsyncSession,
    buyer_id: int,
    tariff_id: int,
    period_days: int,
    expected_price_kopeks: int,
    idempotency_key: str,
    source: str = 'bot',
    recipient: GiftRecipient | None = None,
) -> GiftPurchaseResult:
    """Atomically purchase a gift subscription from user balance with database idempotency."""
    if not idempotency_key or not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError('A valid non-empty idempotency_key is required for gift balance purchases')

    external_id = _build_gift_transaction_external_id(source, idempotency_key)

    # 1. Fast-path: check if this idempotency key was already committed
    existing_stmt = (
        select(GuestPurchase)
        .options(selectinload(GuestPurchase.tariff))
        .where(GuestPurchase.idempotency_key == idempotency_key)
    )
    existing_res = await db.execute(existing_stmt)
    existing_purchase = existing_res.scalars().first()

    if existing_purchase is not None:
        # Validate that parameters match the original purchase
        if (
            existing_purchase.buyer_user_id != buyer_id
            or existing_purchase.tariff_id != tariff_id
            or existing_purchase.period_days != period_days
        ):
            raise GiftIdempotencyConflictError(
                f"Idempotency key '{idempotency_key}' was already used with different purchase parameters"
            )

        # Транзакция ищется по external_id, собранному из source САМОЙ покупки:
        # повтор может прийти из другого канала (бот против кабинета), и тогда
        # аргумент source дал бы чужой ключ. Промахнуться тут нельзя — раньше
        # фоллбек брал последнюю GIFT_PAYMENT покупателя и при нескольких
        # подарках подсовывал транзакцию от другого.
        tx = await _find_gift_transaction(db, existing_purchase, idempotency_key, source)

        buyer = await db.get(User, buyer_id)
        remaining_balance = buyer.balance_kopeks if buyer else 0
        quote = _build_quote_from_purchase(existing_purchase)

        return GiftPurchaseResult(
            purchase=existing_purchase,
            transaction=tx,
            quote=quote,
            remaining_balance_kopeks=remaining_balance,
            is_idempotent_replay=True,
        )

    # 2. Check feature switch
    if not await is_gift_enabled(db):
        raise GiftFeatureDisabledError('Gift feature is not enabled')

    # 3. Lock buyer row for pricing and balance update
    buyer = await lock_user_for_pricing(db, buyer_id)
    if buyer is None:
        raise GiftPurchaseRestrictedError(f'User {buyer_id} not found')

    if getattr(buyer, 'restriction_subscription', False):
        raise GiftPurchaseRestrictedError('Purchases are restricted for this account')

    # 4. Reload tariff and validate period
    tariff = await get_tariff_by_id(db, tariff_id)
    if tariff is None or not tariff.is_active or not tariff.show_in_gift:
        raise GiftTariffUnavailableError('Tariff not found or inactive')

    base_price = tariff.get_price_for_period(period_days)
    if base_price is None:
        raise GiftPeriodUnavailableError(f'Price is not configured for period {period_days} days')

    # 5. Requote under sender lock
    pricing_result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period_days,
        device_limit=tariff.device_limit,
        user=buyer,
    )
    fresh_quote = _build_quote_from_pricing(tariff, period_days, base_price, pricing_result)
    fresh_price = fresh_quote.final_price_kopeks

    if fresh_price != expected_price_kopeks:
        raise GiftPriceChangedError(expected_price_kopeks=expected_price_kopeks, fresh_quote=fresh_quote)

    if buyer.balance_kopeks < fresh_price:
        raise GiftInsufficientBalanceError(required_kopeks=fresh_price, available_kopeks=buyer.balance_kopeks)

    # 6. Prepare contact & recipient info
    contact_type, contact_value = _derive_buyer_contact(buyer)
    gift_recipient_type = recipient.recipient_type if recipient else None
    gift_recipient_value = recipient.recipient_value if recipient else None
    gift_message = recipient.gift_message if recipient else None

    tx_description = f'Gift: {tariff.name} ({period_days}d)'
    if gift_recipient_value:
        tx_description += f' -> {gift_recipient_value}'

    consume_promo = fresh_quote.consumes_promo_offer

    # 7. Atomically create purchase, debit balance, create transaction, mark PAID
    try:
        purchase = await create_purchase(
            db,
            landing=None,
            tariff=tariff,
            period_days=period_days,
            amount_kopeks=fresh_price,
            contact_type=contact_type,
            contact_value=contact_value,
            payment_method=PaymentMethod.BALANCE.value,
            is_gift=True,
            gift_recipient_type=gift_recipient_type,
            gift_recipient_value=gift_recipient_value,
            gift_message=gift_message,
            source=source,
            buyer_user_id=buyer.id,
            idempotency_key=idempotency_key,
            commit=False,
        )

        balance_ok = await subtract_user_balance(
            db,
            buyer,
            fresh_price,
            description=tx_description,
            create_transaction=False,
            consume_promo_offer=consume_promo,
            commit=False,
        )
        if not balance_ok:
            await db.rollback()
            raise GiftInsufficientBalanceError(
                required_kopeks=fresh_price,
                available_kopeks=buyer.balance_kopeks,
            )

        transaction = await create_transaction(
            db,
            user_id=buyer.id,
            type=TransactionType.GIFT_PAYMENT,
            amount_kopeks=fresh_price,
            description=tx_description,
            payment_method=PaymentMethod.BALANCE,
            external_id=external_id,
            commit=False,
        )

        purchase.status = GuestPurchaseStatus.PAID.value
        purchase.paid_at = datetime.now(UTC)

        await db.commit()
        await db.refresh(purchase)
        await db.refresh(transaction)
        await db.refresh(buyer)
    except IntegrityError:
        await db.rollback()
        # Concurrent winner/loser handling: reload winning purchase
        res_retry = await db.execute(
            select(GuestPurchase)
            .options(selectinload(GuestPurchase.tariff))
            .where(GuestPurchase.idempotency_key == idempotency_key)
        )
        winning_purchase = res_retry.scalars().first()
        if winning_purchase is not None:
            if (
                winning_purchase.buyer_user_id != buyer_id
                or winning_purchase.tariff_id != tariff_id
                or winning_purchase.period_days != period_days
            ):
                raise GiftIdempotencyConflictError(
                    f"Idempotency key '{idempotency_key}' was already used with different purchase parameters"
                )

            tx = await _find_gift_transaction(db, winning_purchase, idempotency_key, source)
            buyer = await db.get(User, buyer_id)
            remaining_balance = buyer.balance_kopeks if buyer else 0
            quote = _build_quote_from_purchase(winning_purchase)

            return GiftPurchaseResult(
                purchase=winning_purchase,
                transaction=tx,
                quote=quote,
                remaining_balance_kopeks=remaining_balance,
                is_idempotent_replay=True,
            )
        raise

    # 8. Emit deferred side-effects after commit
    await emit_transaction_side_effects(
        db,
        transaction,
        amount_kopeks=fresh_price,
        user_id=buyer.id,
        type=TransactionType.GIFT_PAYMENT,
        payment_method=PaymentMethod.BALANCE,
        external_id=external_id,
        description=tx_description,
    )

    return GiftPurchaseResult(
        purchase=purchase,
        transaction=transaction,
        quote=fresh_quote,
        remaining_balance_kopeks=buyer.balance_kopeks,
        is_idempotent_replay=False,
    )
