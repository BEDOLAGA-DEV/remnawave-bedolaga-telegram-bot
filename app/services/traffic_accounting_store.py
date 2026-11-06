"""Transactional storage for finite traffic accounting, without panel side effects.

Caller owns commit/rollback. Subscription locks serialize with purchase/renewal
and grace; revision checks also reject a calculation made before that lock.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subscription, TrafficAccountingState, TrafficAllowance
from app.services.traffic_accounting import AccountingConflict, Allowance, TrafficAccount


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def _lock_subscription(db: AsyncSession, subscription_id: int) -> None:
    found = await db.scalar(select(Subscription.id).where(Subscription.id == subscription_id).with_for_update())
    if found is None:
        raise AccountingConflict('Subscription does not exist')


async def load_account(db: AsyncSession, subscription_id: int) -> tuple[TrafficAccount, TrafficAccountingState] | None:
    await _lock_subscription(db, subscription_id)
    state = await db.scalar(
        select(TrafficAccountingState)
        .where(TrafficAccountingState.subscription_id == subscription_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is None:
        return None
    rows = (
        await db.scalars(
            select(TrafficAllowance)
            .where(TrafficAllowance.subscription_id == subscription_id)
            .order_by(TrafficAllowance.operation_key)
            .execution_options(populate_existing=True)
        )
    ).all()
    return TrafficAccount(
        state.panel_identity,
        state.lifetime_bytes,
        state.used_bytes,
        tuple(
            Allowance(row.operation_key, row.remaining_bytes, _aware(row.expires_at), row.granted_bytes) for row in rows
        ),
    ), state


async def initialize_account(
    db: AsyncSession,
    subscription_id: int,
    account: TrafficAccount,
    *,
    strategy: str,
    timezone: str,
    anchor: datetime,
    next_refresh_at: datetime | None,
) -> TrafficAccountingState:
    existing = await load_account(db, subscription_id)
    if existing is not None:
        if existing[0].panel_identity != account.panel_identity:
            raise AccountingConflict('Cannot replace accounting identity during bootstrap replay')
        return existing[1]
    ZoneInfo(timezone)
    if strategy not in {'NO_RESET', 'DAY', 'WEEK', 'MONTH', 'MONTH_ROLLING'} or anchor.tzinfo is None:
        raise AccountingConflict('Invalid accounting calendar')
    state = TrafficAccountingState(
        subscription_id=subscription_id,
        panel_identity=account.panel_identity,
        lifetime_bytes=account.lifetime_bytes,
        used_bytes=account.used_bytes,
        revision=0,
        phase='prepared',
        business_strategy=strategy,
        calendar_timezone=timezone,
        calendar_anchor=anchor,
        next_refresh_at=next_refresh_at,
    )
    db.add(state)
    await db.flush()
    for item in account.allowances:
        db.add(
            TrafficAllowance(
                subscription_id=subscription_id,
                operation_key=item.key,
                granted_bytes=item.granted_bytes,
                remaining_bytes=item.remaining_bytes,
                expires_at=item.expires_at,
            )
        )
    await db.flush()
    return state


async def save_account(
    db: AsyncSession,
    subscription_id: int,
    account: TrafficAccount,
    *,
    expected_revision: int,
) -> int:
    loaded = await load_account(db, subscription_id)
    if loaded is None:
        raise AccountingConflict('Accounting state does not exist')
    previous, state = loaded
    if state.revision != expected_revision or previous.panel_identity != account.panel_identity:
        raise AccountingConflict('Stale accounting revision or identity')
    if account.lifetime_bytes < previous.lifetime_bytes:
        raise AccountingConflict('Cumulative usage cannot move backwards')
    if account.used_bytes > previous.used_bytes + account.lifetime_bytes - previous.lifetime_bytes:
        raise AccountingConflict('Resettable counter increased beyond cumulative usage')
    current = {item.key: item for item in account.allowances}
    for old in previous.allowances:
        new = current.get(old.key)
        if new is None or (new.granted_bytes, new.expires_at) != (old.granted_bytes, old.expires_at):
            raise AccountingConflict('Grant history cannot be removed or rewritten')
        if new.remaining_bytes > old.remaining_bytes:
            raise AccountingConflict('Spent grant cannot be restored; use a new operation key')
    changed = await db.execute(
        update(TrafficAccountingState)
        .where(
            TrafficAccountingState.subscription_id == subscription_id,
            TrafficAccountingState.revision == expected_revision,
        )
        .values(
            lifetime_bytes=account.lifetime_bytes,
            used_bytes=account.used_bytes,
            revision=expected_revision + 1,
            next_delivery_at=None,
        )
    )
    if changed.rowcount != 1:
        raise AccountingConflict('Concurrent accounting update')
    old_keys = {item.key for item in previous.allowances}
    for item in account.allowances:
        if item.key in old_keys:
            await db.execute(
                update(TrafficAllowance)
                .where(
                    TrafficAllowance.subscription_id == subscription_id,
                    TrafficAllowance.operation_key == item.key,
                )
                .values(remaining_bytes=item.remaining_bytes)
            )
        else:
            db.add(
                TrafficAllowance(
                    subscription_id=subscription_id,
                    operation_key=item.key,
                    granted_bytes=item.granted_bytes,
                    remaining_bytes=item.remaining_bytes,
                    expires_at=item.expires_at,
                )
            )
    await db.flush()
    return expected_revision + 1
