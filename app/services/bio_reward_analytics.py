"""Bio-reward analytics: conversion cohorts + viral K-factor.

Computed by a daily scheduler tick and cached in
``bio_reward_analytics_snapshot``. The cache rows are upserted in-place so the
read side (admin panel) always sees a consistent snapshot per bucket.

Public surface:
    bucket_for_month(dt)            -> "YYYY-MM"
    bucket_for_week(dt)             -> "YYYY-Www"
    compute_conversion_cohorts(...) -> list of bucket dicts
    compute_viral_coefficient(...)  -> dict
    recompute_all(db)               -> orchestrator + upsert
    read_snapshots(db, type)        -> list of snapshot rows for UI
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    BioRewardAnalyticsSnapshot,
    BioRewardParticipant,
    BioRewardStatus,
    Subscription,
    Transaction,
    TransactionType,
    User,
)


logger = structlog.get_logger(__name__)


CONVERSION_MONTHLY = 'conversion_monthly'
CONVERSION_WEEKLY = 'conversion_weekly'
VIRAL = 'viral'

VIRAL_WINDOWS_DAYS = (7, 30, 90)


# ---------- Pure helpers (testable without DB) ----------


def bucket_for_month(dt: datetime) -> str:
    return dt.strftime('%Y-%m')


def bucket_for_week(dt: datetime) -> str:
    iso_year, iso_week, _ = dt.isocalendar()
    return f'{iso_year}-W{iso_week:02d}'


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ---------- Conversion cohorts ----------


async def compute_conversion_cohorts(
    db: AsyncSession, granularity: str
) -> list[dict[str, Any]]:
    """Return cohort metrics keyed by bucket. granularity: 'monthly' or 'weekly'."""
    if granularity not in ('monthly', 'weekly'):
        raise ValueError(f'unknown granularity: {granularity}')
    bucket_fn = bucket_for_month if granularity == 'monthly' else bucket_for_week

    parts = (
        await db.execute(
            select(
                BioRewardParticipant.id,
                BioRewardParticipant.user_id,
                BioRewardParticipant.opted_in_at,
                BioRewardParticipant.last_bio_seen_at,
            )
        )
    ).all()

    buckets: dict[str, dict[str, Any]] = {}
    for _, user_id, opted_in_at, last_bio_seen_at in parts:
        opted = _ensure_aware(opted_in_at) or datetime.now(UTC)
        key = bucket_fn(opted)
        b = buckets.setdefault(
            key,
            {
                'bucket': key,
                'user_ids': set(),
                'opted_in_at': {},
                'ever_active_user_ids': set(),
            },
        )
        b['user_ids'].add(user_id)
        b['opted_in_at'][user_id] = opted
        if last_bio_seen_at is not None:
            b['ever_active_user_ids'].add(user_id)

    if not buckets:
        return []

    all_user_ids = {uid for b in buckets.values() for uid in b['user_ids']}

    subs_rows = (
        await db.execute(
            select(Subscription.user_id, Subscription.created_at, Subscription.is_trial).where(
                Subscription.user_id.in_(all_user_ids)
            )
        )
    ).all()

    first_paid_at: dict[int, datetime] = {}
    for user_id, created_at, is_trial in subs_rows:
        if is_trial:
            continue
        created_at = _ensure_aware(created_at)
        if created_at is None:
            continue
        cur = first_paid_at.get(user_id)
        if cur is None or created_at < cur:
            first_paid_at[user_id] = created_at

    txn_rows = (
        await db.execute(
            select(Transaction.user_id, Transaction.amount_kopeks, Transaction.created_at).where(
                Transaction.user_id.in_(all_user_ids),
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            )
        )
    ).all()
    txns_by_user: dict[int, list[tuple[int, datetime]]] = {}
    for user_id, amt, created_at in txn_rows:
        created_at = _ensure_aware(created_at)
        if created_at is None:
            continue
        txns_by_user.setdefault(user_id, []).append((int(amt or 0), created_at))

    out: list[dict[str, Any]] = []
    for key, b in buckets.items():
        user_ids = b['user_ids']
        opted = b['opted_in_at']
        converters: list[int] = []
        days_to_convert: list[int] = []
        revenue_kopeks = 0

        for uid in user_ids:
            paid_at = first_paid_at.get(uid)
            if paid_at is not None and paid_at >= opted[uid]:
                converters.append(uid)
                days_to_convert.append(max(0, (paid_at - opted[uid]).days))
            for amt, ts in txns_by_user.get(uid, ()):
                if ts >= opted[uid]:
                    revenue_kopeks += amt

        total = len(user_ids)
        converted = len(converters)
        conv_pct = round((converted / total) * 100) if total else 0
        avg_days = round(sum(days_to_convert) / len(days_to_convert)) if days_to_convert else None

        out.append(
            {
                'bucket': key,
                'total_opted_in': total,
                'ever_active': len(b['ever_active_user_ids']),
                'converted_paid': converted,
                'conversion_pct': conv_pct,
                'total_paid_revenue_kopeks': revenue_kopeks,
                'avg_days_to_convert': avg_days,
            }
        )

    out.sort(key=lambda r: r['bucket'], reverse=True)
    return out


# ---------- Viral coefficient ----------


async def compute_viral_coefficient(
    db: AsyncSession, window_days: int, *, now: datetime | None = None
) -> dict[str, Any]:
    """K-factor for one rolling window."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)

    bio_active_rows = (
        await db.execute(
            select(BioRewardParticipant.user_id).where(
                (BioRewardParticipant.status == BioRewardStatus.ACTIVE.value)
                | (BioRewardParticipant.last_bio_seen_at >= cutoff)
            )
        )
    ).all()
    bio_active_user_ids = {row[0] for row in bio_active_rows}

    if not bio_active_user_ids:
        return {
            'window_days': window_days,
            'bio_active_users': 0,
            'attributed_referrals': 0,
            'paid_attributed_referrals': 0,
            'k_factor': 0.0,
        }

    referral_rows = (
        await db.execute(
            select(User.id).where(
                User.referred_by_id.in_(bio_active_user_ids),
                User.created_at >= cutoff,
            )
        )
    ).all()
    referral_user_ids = {row[0] for row in referral_rows}
    attributed = len(referral_user_ids)

    paid_attributed = 0
    if referral_user_ids:
        paid_rows = (
            await db.execute(
                select(Subscription.user_id).where(
                    Subscription.user_id.in_(referral_user_ids),
                    Subscription.is_trial.is_(False),
                )
            )
        ).all()
        paid_attributed = len({row[0] for row in paid_rows})

    k = attributed / len(bio_active_user_ids) if bio_active_user_ids else 0.0
    return {
        'window_days': window_days,
        'bio_active_users': len(bio_active_user_ids),
        'attributed_referrals': attributed,
        'paid_attributed_referrals': paid_attributed,
        'k_factor': round(k, 4),
    }


# ---------- Cache upsert / read ----------


async def _upsert_snapshot(
    db: AsyncSession, snapshot_type: str, bucket_key: str, payload: dict[str, Any]
) -> None:
    existing = (
        await db.execute(
            select(BioRewardAnalyticsSnapshot).where(
                BioRewardAnalyticsSnapshot.snapshot_type == snapshot_type,
                BioRewardAnalyticsSnapshot.bucket_key == bucket_key,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if existing is None:
        db.add(
            BioRewardAnalyticsSnapshot(
                snapshot_type=snapshot_type,
                bucket_key=bucket_key,
                payload=payload,
                computed_at=now,
            )
        )
    else:
        existing.payload = payload
        existing.computed_at = now


async def recompute_all(db: AsyncSession) -> dict[str, int]:
    """Recompute every metric and replace the snapshot table contents."""
    monthly = await compute_conversion_cohorts(db, 'monthly')
    weekly = await compute_conversion_cohorts(db, 'weekly')

    await db.execute(
        delete(BioRewardAnalyticsSnapshot).where(
            BioRewardAnalyticsSnapshot.snapshot_type.in_(
                (CONVERSION_MONTHLY, CONVERSION_WEEKLY, VIRAL)
            )
        )
    )

    for row in monthly:
        await _upsert_snapshot(db, CONVERSION_MONTHLY, row['bucket'], row)
    for row in weekly:
        await _upsert_snapshot(db, CONVERSION_WEEKLY, row['bucket'], row)
    for window in VIRAL_WINDOWS_DAYS:
        viral = await compute_viral_coefficient(db, window)
        await _upsert_snapshot(db, VIRAL, f'{window}d', viral)

    await db.commit()
    return {
        'conversion_monthly': len(monthly),
        'conversion_weekly': len(weekly),
        'viral_windows': len(VIRAL_WINDOWS_DAYS),
    }


async def read_snapshots(
    db: AsyncSession, snapshot_type: str, *, limit: int = 12
) -> list[BioRewardAnalyticsSnapshot]:
    rows = (
        await db.execute(
            select(BioRewardAnalyticsSnapshot)
            .where(BioRewardAnalyticsSnapshot.snapshot_type == snapshot_type)
            .order_by(BioRewardAnalyticsSnapshot.bucket_key.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def last_computed_at(db: AsyncSession) -> datetime | None:
    row = (
        await db.execute(
            select(BioRewardAnalyticsSnapshot.computed_at)
            .order_by(BioRewardAnalyticsSnapshot.computed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _ensure_aware(row)
