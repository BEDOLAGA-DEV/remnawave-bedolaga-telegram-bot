"""Read paid landing purchases without manufacturing invoices or ledger entries."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import GuestPurchase, PaymentMethod, User


if TYPE_CHECKING:
    from app.services.payment_verification_service import PendingPayment


def _method(value: str | None) -> PaymentMethod | None:
    from app.services.guest_purchase_service import _resolve_payment_method

    method = _resolve_payment_method(value)
    if method in {PaymentMethod.BALANCE, PaymentMethod.MANUAL, PaymentMethod.PLATEGA_RECURRENT}:
        return None
    return method


def _record(purchase: GuestPurchase) -> PendingPayment | None:
    from app.services.payment_verification_service import PendingPayment

    method = _method(purchase.payment_method)
    if method is None or purchase.source != 'landing' or purchase.paid_at is None or purchase.amount_kopeks <= 0:
        return None
    return PendingPayment(
        method=method,
        local_id=purchase.id,
        identifier=purchase.payment_id or f'landing-{purchase.id}',
        amount_kopeks=purchase.amount_kopeks,
        status='paid',
        is_paid=True,
        created_at=purchase.paid_at,
        user=purchase.buyer or (purchase.user if not purchase.is_gift else None),
        payment=purchase,
        source='landing',
    )


def _query():
    return (
        select(GuestPurchase)
        .options(
            selectinload(GuestPurchase.buyer),
            selectinload(GuestPurchase.user),
        )
        .where(GuestPurchase.source == 'landing', GuestPurchase.paid_at.is_not(None), GuestPurchase.amount_kopeks > 0)
    )


async def get_landing_payment(db: AsyncSession, method: PaymentMethod, local_id: int) -> PendingPayment | None:
    purchase = (await db.execute(_query().where(GuestPurchase.id == local_id))).scalar_one_or_none()
    record = _record(purchase) if purchase else None
    return record if record and record.method == method else None


async def fetch_landing_payments(
    db: AsyncSession,
    cutoff: datetime,
    *,
    upper_bound: datetime | None = None,
    search: str | None = None,
    method: PaymentMethod | None = None,
    limit: int = 5000,
) -> list[PendingPayment]:
    stmt = _query().where(GuestPurchase.paid_at >= cutoff)
    if upper_bound is not None:
        stmt = stmt.where(GuestPurchase.paid_at <= upper_bound)
    if method is not None:
        # A selected base provider also includes its landing sub-options.
        escaped = method.value.replace('_', r'\_')
        stmt = stmt.where(
            or_(
                GuestPurchase.payment_method == method.value,
                GuestPurchase.payment_method.like(f'{escaped}\\_%', escape='\\'),
            )
        )
    if search:
        escaped = search.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')
        pattern = f'%{escaped}%'
        if search.startswith('@'):
            user_filter = User.username.ilike(f'%{escaped[1:]}%', escape='\\')
            contact_filter = and_(
                GuestPurchase.contact_type == 'telegram', GuestPurchase.contact_value.ilike(pattern, escape='\\')
            )
        elif search.isdigit():
            # Avoid int64 overflow for arbitrary numeric invoice/search input.
            user_filter = User.telegram_id == int(search) if len(search) <= 18 else User.id.is_(None)
            contact_filter = and_(GuestPurchase.contact_type == 'telegram', GuestPurchase.contact_value == search)
        elif '@' in search:
            user_filter = User.email.ilike(pattern, escape='\\')
            contact_filter = and_(
                GuestPurchase.contact_type == 'email', GuestPurchase.contact_value.ilike(pattern, escape='\\')
            )
        else:
            user_filter = None
            contact_filter = GuestPurchase.payment_id.ilike(pattern, escape='\\')
        if user_filter is not None:
            stmt = stmt.where(
                or_(
                    GuestPurchase.buyer.has(user_filter),
                    and_(
                        GuestPurchase.buyer_user_id.is_(None),
                        GuestPurchase.is_gift.is_(False),
                        GuestPurchase.user.has(user_filter),
                    ),
                    contact_filter,
                )
            )
        else:
            stmt = stmt.where(contact_filter)
    purchases = (await db.execute(stmt.order_by(GuestPurchase.paid_at.desc()).limit(limit))).scalars().all()
    return [record for purchase in purchases if (record := _record(purchase)) is not None]


async def exclude_landing_duplicates(db: AsyncSession, records: list[PendingPayment]) -> list[PendingPayment]:
    """Match by purchase token/provider, never by amount, user or numeric row id."""
    tokens = set()
    charge_ids = set()
    for record in records:
        token = _purchase_token(record)
        if token is not None:
            tokens.add(token)
        if record.method == PaymentMethod.PLATEGA_RECURRENT:
            charge_ids.add(record.identifier)
    if not tokens and not charge_ids:
        return records
    stmt = select(GuestPurchase.token, GuestPurchase.payment_method, GuestPurchase.payment_id).where(
        GuestPurchase.source == 'landing',
        GuestPurchase.paid_at.is_not(None),
        GuestPurchase.amount_kopeks > 0,
        or_(GuestPurchase.token.in_(tokens), GuestPurchase.payment_id.in_(charge_ids)),
    )
    purchases = (await db.execute(stmt)).all()
    token_methods = {(p.token, _method(p.payment_method)) for p in purchases if _method(p.payment_method)}
    platega_ids = {p.payment_id for p in purchases if _method(p.payment_method) == PaymentMethod.PLATEGA}
    kept = []
    for record in records:
        if (_purchase_token(record), record.method) in token_methods:
            continue
        if record.method == PaymentMethod.PLATEGA_RECURRENT and record.identifier in platega_ids:
            continue
        kept.append(record)
    return kept


def _purchase_token(record: PendingPayment) -> str | None:
    metadata = getattr(record.payment, 'metadata_json', None)
    if record.method == PaymentMethod.CRYPTOBOT:
        try:
            metadata = json.loads(getattr(record.payment, 'payload', '') or '')
        except (ValueError, TypeError):
            return None
    if isinstance(metadata, dict) and metadata.get('purpose') == 'guest_purchase':
        token = metadata.get('purchase_token')
        return token if isinstance(token, str) and token else None
    return None
