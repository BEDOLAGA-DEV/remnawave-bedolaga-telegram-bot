"""Search service for querying payments across all provider tables."""

from __future__ import annotations

import enum
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import cast, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.types import String as SAString

from app.database.models import (
    CloudPaymentsPayment,
    CryptoBotPayment,
    FreekassaPayment,
    HeleketPayment,
    KassaAiPayment,
    MulenPayPayment,
    Pal24Payment,
    PaymentMethod,
    PlategaPayment,
    RioPayPayment,
    SeverPayPayment,
    Transaction,
    TransactionType,
    User,
    WataPayment,
    YooKassaPayment,
)
from app.services.payment_verification_service import (
    PendingPayment,
    _build_record,
    _metadata_is_balance,
    _parse_cryptobot_amount_kopeks,
)


logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ALL_TIME_DAYS: int = 365
"""Safety limit for 'all time' queries to prevent unbounded scans."""

MAX_RECORDS_PER_PROVIDER: int = 5000
"""Hard limit on rows fetched from each provider table to prevent memory exhaustion."""

DEFAULT_PER_PAGE: int = 20
MAX_PER_PAGE: int = 100


def _escape_like(value: str) -> str:
    """Escape LIKE/ILIKE wildcard characters to prevent pattern injection."""
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


class StatusFilter(str, enum.Enum):
    """Supported status filter values."""

    ALL = 'all'
    PENDING = 'pending'
    PAID = 'paid'
    CANCELLED = 'cancelled'


class PeriodPreset(str, enum.Enum):
    """Predefined period presets."""

    TODAY = 'today'
    H24 = '24h'
    D7 = '7d'
    D30 = '30d'
    ALL = 'all'


_PERIOD_DELTAS: dict[PeriodPreset, timedelta] = {
    PeriodPreset.TODAY: timedelta(hours=0),  # special: start of today
    PeriodPreset.H24: timedelta(hours=24),
    PeriodPreset.D7: timedelta(days=7),
    PeriodPreset.D30: timedelta(days=30),
    PeriodPreset.ALL: timedelta(days=MAX_ALL_TIME_DAYS),
}


# Sets of provider-specific statuses used for classification.
_CANCELLED_STATUSES: frozenset[str] = frozenset(
    {
        'cancel',
        'canceled',
        'cancelled',
        'declined',
        'error',
        'expired',
        'fail',
        'failed',
        'amount_mismatch',
    }
)


# ---------------------------------------------------------------------------
# Search params
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SearchParams:
    """Encapsulates validated search parameters."""

    search: str | None = None
    status_filter: StatusFilter = StatusFilter.ALL
    method_filter: PaymentMethod | None = None
    period: PeriodPreset = PeriodPreset.H24
    date_from: datetime | None = None
    date_to: datetime | None = None
    tz: str | None = None
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    @property
    def cutoff(self) -> datetime:
        """Calculate the earliest datetime to consider."""
        if self.date_from is not None:
            return self.date_from
        if self.period == PeriodPreset.TODAY:
            try:
                user_tz = ZoneInfo(self.tz) if self.tz else UTC
            except (KeyError, ValueError):
                user_tz = UTC
            now_local = datetime.now(user_tz)
            midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            return midnight_local.astimezone(UTC)
        return datetime.now(UTC) - _PERIOD_DELTAS.get(self.period, _PERIOD_DELTAS[PeriodPreset.H24])

    @property
    def upper_bound(self) -> datetime | None:
        """Upper datetime bound (only set for custom ranges)."""
        return self.date_to


@dataclass(slots=True)
class SearchStats:
    """Aggregated search statistics."""

    total: int = 0
    pending: int = 0
    paid: int = 0
    cancelled: int = 0
    by_method: dict[str, int] | None = None


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------


_PAID_STATUSES: frozenset[str] = frozenset(
    {
        'completed',
        'confirmed',
        'paid',
        'paid_over',
        'succeeded',
        'success',
    }
)


def _classify_status(record: PendingPayment) -> StatusFilter:
    """Classify a payment record into one of the three buckets."""
    if record.is_paid:
        return StatusFilter.PAID
    status_lower = (record.status or '').lower()
    if status_lower in _PAID_STATUSES:
        return StatusFilter.PAID
    if status_lower in _CANCELLED_STATUSES:
        return StatusFilter.CANCELLED
    return StatusFilter.PENDING


# ---------------------------------------------------------------------------
# User search type detection
# ---------------------------------------------------------------------------


class _UserSearchKind(enum.Enum):
    USERNAME = 'username'
    TELEGRAM_ID = 'telegram_id'
    EMAIL = 'email'
    INVOICE = 'invoice'


def _detect_user_search_kind(query: str) -> _UserSearchKind:
    """Auto-detect the type of user search query."""
    stripped = query.strip()
    if stripped.startswith('@'):
        return _UserSearchKind.USERNAME
    if stripped.isdigit():
        return _UserSearchKind.TELEGRAM_ID
    if '@' in stripped:
        return _UserSearchKind.EMAIL
    return _UserSearchKind.INVOICE


# ---------------------------------------------------------------------------
# Per-provider search functions
# ---------------------------------------------------------------------------


def _apply_date_filter(
    stmt: Any,
    created_at_col: Any,
    cutoff: datetime,
    upper_bound: datetime | None,
) -> Any:
    """Apply date range filters to a select statement."""
    stmt = stmt.where(created_at_col >= cutoff)
    if upper_bound is not None:
        stmt = stmt.where(created_at_col <= upper_bound)
    return stmt


def _apply_user_join_filter(
    stmt: Any,
    model: type,
    search_kind: _UserSearchKind,
    search_value: str,
) -> Any:
    """Apply user-based search filters by joining the User table."""
    stmt = stmt.join(User, model.user_id == User.id)
    if search_kind == _UserSearchKind.USERNAME:
        username = search_value.lstrip('@')
        stmt = stmt.where(User.username.ilike(f'%{_escape_like(username)}%'))
    elif search_kind == _UserSearchKind.TELEGRAM_ID:
        stmt = stmt.where(User.telegram_id == int(search_value))
    elif search_kind == _UserSearchKind.EMAIL:
        stmt = stmt.where(User.email.ilike(f'%{_escape_like(search_value)}%'))
    return stmt


async def _search_yookassa(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = (
        select(YooKassaPayment).options(selectinload(YooKassaPayment.user)).order_by(desc(YooKassaPayment.created_at))
    )
    stmt = _apply_date_filter(stmt, YooKassaPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            stmt = stmt.where(YooKassaPayment.yookassa_payment_id.ilike(f'%{_escape_like(params.search)}%'))
        else:
            stmt = _apply_user_join_filter(stmt, YooKassaPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        if not _metadata_is_balance(payment):
            continue
        record = _build_record(
            PaymentMethod.YOOKASSA,
            payment,
            identifier=payment.yookassa_payment_id,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(getattr(payment, 'is_paid', False)),
        )
        if record:
            records.append(record)
    return records


async def _search_cryptobot(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = (
        select(CryptoBotPayment)
        .options(selectinload(CryptoBotPayment.user))
        .order_by(desc(CryptoBotPayment.created_at))
    )
    stmt = _apply_date_filter(stmt, CryptoBotPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            stmt = stmt.where(CryptoBotPayment.invoice_id.ilike(f'%{_escape_like(params.search)}%'))
        else:
            stmt = _apply_user_join_filter(stmt, CryptoBotPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        amount_kopeks = _parse_cryptobot_amount_kopeks(payment)
        record = _build_record(
            PaymentMethod.CRYPTOBOT,
            payment,
            identifier=payment.invoice_id,
            amount_kopeks=amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
        )
        if record:
            records.append(record)
    return records


async def _search_heleket(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = select(HeleketPayment).options(selectinload(HeleketPayment.user)).order_by(desc(HeleketPayment.created_at))
    stmt = _apply_date_filter(stmt, HeleketPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            stmt = stmt.where(
                or_(
                    HeleketPayment.uuid.ilike(f'%{_escape_like(params.search)}%'),
                    HeleketPayment.order_id.ilike(f'%{_escape_like(params.search)}%'),
                )
            )
        else:
            stmt = _apply_user_join_filter(stmt, HeleketPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        record = _build_record(
            PaymentMethod.HELEKET,
            payment,
            identifier=payment.uuid,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
            expires_at=getattr(payment, 'expires_at', None),
        )
        if record:
            records.append(record)
    return records


async def _search_mulenpay(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = (
        select(MulenPayPayment).options(selectinload(MulenPayPayment.user)).order_by(desc(MulenPayPayment.created_at))
    )
    stmt = _apply_date_filter(stmt, MulenPayPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            conditions = [MulenPayPayment.uuid.ilike(f'%{_escape_like(params.search)}%')]
            # mulen_payment_id is Integer -- cast for ILIKE
            if params.search.isdigit():
                conditions.append(MulenPayPayment.mulen_payment_id == int(params.search))
            else:
                conditions.append(
                    cast(MulenPayPayment.mulen_payment_id, SAString).ilike(f'%{_escape_like(params.search)}%')
                )
            stmt = stmt.where(or_(*conditions))
        else:
            stmt = _apply_user_join_filter(stmt, MulenPayPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        record = _build_record(
            PaymentMethod.MULENPAY,
            payment,
            identifier=payment.uuid,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
        )
        if record:
            records.append(record)
    return records


async def _search_pal24(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = select(Pal24Payment).options(selectinload(Pal24Payment.user)).order_by(desc(Pal24Payment.created_at))
    stmt = _apply_date_filter(stmt, Pal24Payment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            conditions = [
                Pal24Payment.bill_id.ilike(f'%{_escape_like(params.search)}%'),
                Pal24Payment.order_id.ilike(f'%{_escape_like(params.search)}%'),
            ]
            stmt = stmt.where(or_(*conditions))
        else:
            stmt = _apply_user_join_filter(stmt, Pal24Payment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        record = _build_record(
            PaymentMethod.PAL24,
            payment,
            identifier=payment.bill_id,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
            expires_at=getattr(payment, 'expires_at', None),
        )
        if record:
            records.append(record)
    return records


async def _search_wata(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = select(WataPayment).options(selectinload(WataPayment.user)).order_by(desc(WataPayment.created_at))
    stmt = _apply_date_filter(stmt, WataPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            conditions = [
                WataPayment.payment_link_id.ilike(f'%{_escape_like(params.search)}%'),
                WataPayment.order_id.ilike(f'%{_escape_like(params.search)}%'),
            ]
            stmt = stmt.where(or_(*conditions))
        else:
            stmt = _apply_user_join_filter(stmt, WataPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        record = _build_record(
            PaymentMethod.WATA,
            payment,
            identifier=payment.payment_link_id,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
            expires_at=getattr(payment, 'expires_at', None),
        )
        if record:
            records.append(record)
    return records


async def _search_platega(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = select(PlategaPayment).options(selectinload(PlategaPayment.user)).order_by(desc(PlategaPayment.created_at))
    stmt = _apply_date_filter(stmt, PlategaPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            conditions = [
                PlategaPayment.correlation_id.ilike(f'%{_escape_like(params.search)}%'),
                PlategaPayment.platega_transaction_id.ilike(f'%{_escape_like(params.search)}%'),
            ]
            stmt = stmt.where(or_(*conditions))
        else:
            stmt = _apply_user_join_filter(stmt, PlategaPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        identifier = payment.platega_transaction_id or payment.correlation_id or str(payment.id)
        record = _build_record(
            PaymentMethod.PLATEGA,
            payment,
            identifier=identifier,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
            expires_at=getattr(payment, 'expires_at', None),
        )
        if record:
            records.append(record)
    return records


async def _search_cloudpayments(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = (
        select(CloudPaymentsPayment)
        .options(selectinload(CloudPaymentsPayment.user))
        .order_by(desc(CloudPaymentsPayment.created_at))
    )
    stmt = _apply_date_filter(stmt, CloudPaymentsPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            conditions = [CloudPaymentsPayment.invoice_id.ilike(f'%{_escape_like(params.search)}%')]
            # transaction_id_cp is BigInteger -- cast for ILIKE
            if params.search.isdigit():
                conditions.append(CloudPaymentsPayment.transaction_id_cp == int(params.search))
            else:
                conditions.append(
                    cast(CloudPaymentsPayment.transaction_id_cp, SAString).ilike(f'%{_escape_like(params.search)}%')
                )
            stmt = stmt.where(or_(*conditions))
        else:
            stmt = _apply_user_join_filter(stmt, CloudPaymentsPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        record = _build_record(
            PaymentMethod.CLOUDPAYMENTS,
            payment,
            identifier=payment.invoice_id,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
        )
        if record:
            records.append(record)
    return records


async def _search_freekassa(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = (
        select(FreekassaPayment)
        .options(selectinload(FreekassaPayment.user))
        .order_by(desc(FreekassaPayment.created_at))
    )
    stmt = _apply_date_filter(stmt, FreekassaPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            conditions = [
                FreekassaPayment.order_id.ilike(f'%{_escape_like(params.search)}%'),
                FreekassaPayment.freekassa_order_id.ilike(f'%{_escape_like(params.search)}%'),
            ]
            stmt = stmt.where(or_(*conditions))
        else:
            stmt = _apply_user_join_filter(stmt, FreekassaPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        record = _build_record(
            PaymentMethod.FREEKASSA,
            payment,
            identifier=payment.order_id,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
        )
        if record:
            records.append(record)
    return records


async def _search_kassa_ai(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = select(KassaAiPayment).options(selectinload(KassaAiPayment.user)).order_by(desc(KassaAiPayment.created_at))
    stmt = _apply_date_filter(stmt, KassaAiPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            conditions = [
                KassaAiPayment.order_id.ilike(f'%{_escape_like(params.search)}%'),
                KassaAiPayment.kassa_ai_order_id.ilike(f'%{_escape_like(params.search)}%'),
            ]
            stmt = stmt.where(or_(*conditions))
        else:
            stmt = _apply_user_join_filter(stmt, KassaAiPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        record = _build_record(
            PaymentMethod.KASSA_AI,
            payment,
            identifier=payment.order_id,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
        )
        if record:
            records.append(record)
    return records


async def _search_riopay(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = select(RioPayPayment).options(selectinload(RioPayPayment.user)).order_by(desc(RioPayPayment.created_at))
    stmt = _apply_date_filter(stmt, RioPayPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            conditions = [
                RioPayPayment.order_id.ilike(f'%{_escape_like(params.search)}%'),
                RioPayPayment.riopay_order_id.ilike(f'%{_escape_like(params.search)}%'),
            ]
            stmt = stmt.where(or_(*conditions))
        else:
            stmt = _apply_user_join_filter(stmt, RioPayPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        record = _build_record(
            PaymentMethod.RIOPAY,
            payment,
            identifier=payment.order_id,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
            expires_at=getattr(payment, 'expires_at', None),
        )
        if record:
            records.append(record)
    return records


async def _search_severpay(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = (
        select(SeverPayPayment).options(selectinload(SeverPayPayment.user)).order_by(desc(SeverPayPayment.created_at))
    )
    stmt = _apply_date_filter(stmt, SeverPayPayment.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            conditions = [
                SeverPayPayment.order_id.ilike(f'%{_escape_like(params.search)}%'),
                SeverPayPayment.severpay_id.ilike(f'%{_escape_like(params.search)}%'),
                SeverPayPayment.severpay_uid.ilike(f'%{_escape_like(params.search)}%'),
            ]
            stmt = stmt.where(or_(*conditions))
        else:
            stmt = _apply_user_join_filter(stmt, SeverPayPayment, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for payment in result.scalars().all():
        record = _build_record(
            PaymentMethod.SEVERPAY,
            payment,
            identifier=payment.order_id,
            amount_kopeks=payment.amount_kopeks,
            status=payment.status or '',
            is_paid=bool(payment.is_paid),
            expires_at=getattr(payment, 'expires_at', None),
        )
        if record:
            records.append(record)
    return records


async def _search_stars(db: AsyncSession, params: SearchParams) -> list[PendingPayment]:
    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.user))
        .where(
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.payment_method == PaymentMethod.TELEGRAM_STARS.value,
        )
        .order_by(desc(Transaction.created_at))
    )
    stmt = _apply_date_filter(stmt, Transaction.created_at, params.cutoff, params.upper_bound)

    if params.search:
        kind = _detect_user_search_kind(params.search)
        if kind == _UserSearchKind.INVOICE:
            stmt = stmt.where(Transaction.external_id.ilike(f'%{_escape_like(params.search)}%'))
        else:
            stmt = _apply_user_join_filter(stmt, Transaction, kind, params.search)

    stmt = stmt.limit(MAX_RECORDS_PER_PROVIDER)
    result = await db.execute(stmt)
    records: list[PendingPayment] = []
    for transaction in result.scalars().all():
        record = _build_record(
            PaymentMethod.TELEGRAM_STARS,
            transaction,
            identifier=transaction.external_id or str(transaction.id),
            amount_kopeks=transaction.amount_kopeks,
            status='paid' if transaction.is_completed else 'pending',
            is_paid=bool(transaction.is_completed),
        )
        if record:
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Provider -> search function mapping
# ---------------------------------------------------------------------------

_PROVIDER_SEARCH_MAP: dict[PaymentMethod, Any] = {
    PaymentMethod.YOOKASSA: _search_yookassa,
    PaymentMethod.CRYPTOBOT: _search_cryptobot,
    PaymentMethod.HELEKET: _search_heleket,
    PaymentMethod.MULENPAY: _search_mulenpay,
    PaymentMethod.PAL24: _search_pal24,
    PaymentMethod.WATA: _search_wata,
    PaymentMethod.PLATEGA: _search_platega,
    PaymentMethod.CLOUDPAYMENTS: _search_cloudpayments,
    PaymentMethod.FREEKASSA: _search_freekassa,
    PaymentMethod.KASSA_AI: _search_kassa_ai,
    PaymentMethod.RIOPAY: _search_riopay,
    PaymentMethod.SEVERPAY: _search_severpay,
    PaymentMethod.TELEGRAM_STARS: _search_stars,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def search_payments(
    db: AsyncSession,
    params: SearchParams,
) -> tuple[list[PendingPayment], int]:
    """Search payments across all (or filtered) providers.

    Returns:
        Tuple of ``(page_items, total_count)`` where *page_items* is
        a slice according to ``params.page`` / ``params.per_page``.
    """

    # Determine which providers to query
    if params.method_filter is not None:
        search_fn = _PROVIDER_SEARCH_MAP.get(params.method_filter)
        if search_fn is None:
            return [], 0
        provider_results: list[list[PendingPayment]] = [await search_fn(db, params)]
    else:
        provider_results = []
        for search_fn in _PROVIDER_SEARCH_MAP.values():
            provider_results.append(await search_fn(db, params))

    # Flatten
    all_records: list[PendingPayment] = []
    for batch in provider_results:
        all_records.extend(batch)

    # Apply status filter in Python (status classification depends on provider logic)
    if params.status_filter != StatusFilter.ALL:
        all_records = [r for r in all_records if _classify_status(r) == params.status_filter]

    # Sort globally by created_at desc
    all_records.sort(key=lambda r: r.created_at, reverse=True)

    total = len(all_records)

    # Paginate
    start_idx = (params.page - 1) * params.per_page
    page_items = all_records[start_idx : start_idx + params.per_page]

    return page_items, total


async def search_payments_stats(
    db: AsyncSession,
    params: SearchParams,
) -> SearchStats:
    """Compute aggregated statistics for the given search filters.

    Pagination params are ignored -- stats cover the full result set.
    """

    # Reuse the same search logic but force ALL statuses for counting
    stats_params = SearchParams(
        search=params.search,
        status_filter=StatusFilter.ALL,
        method_filter=params.method_filter,
        period=params.period,
        date_from=params.date_from,
        date_to=params.date_to,
        page=1,
        per_page=MAX_PER_PAGE,
    )

    # Query all providers
    if stats_params.method_filter is not None:
        search_fn = _PROVIDER_SEARCH_MAP.get(stats_params.method_filter)
        if search_fn is None:
            return SearchStats()
        all_records: list[PendingPayment] = await search_fn(db, stats_params)
    else:
        all_records = []
        for search_fn in _PROVIDER_SEARCH_MAP.values():
            all_records.extend(await search_fn(db, stats_params))

    # Classify
    pending_count = 0
    paid_count = 0
    cancelled_count = 0
    method_counter: Counter[str] = Counter()

    for record in all_records:
        status = _classify_status(record)
        if status == StatusFilter.PAID:
            paid_count += 1
        elif status == StatusFilter.CANCELLED:
            cancelled_count += 1
        else:
            pending_count += 1
        method_counter[record.method.value] += 1

    return SearchStats(
        total=len(all_records),
        pending=pending_count,
        paid=paid_count,
        cancelled=cancelled_count,
        by_method=dict(method_counter),
    )
