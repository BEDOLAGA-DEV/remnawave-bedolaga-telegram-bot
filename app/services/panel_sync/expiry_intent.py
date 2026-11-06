"""Acknowledge a durable local expiry only after the panel confirms it."""

from datetime import datetime, timedelta

from sqlalchemy import update

from app.database.models import Subscription
from app.utils.timezone import panel_datetime_to_utc


async def acknowledge_pending_expiry(db, subscription, panel_user) -> None:
    pending = getattr(subscription, 'pending_panel_expire_at', None)
    confirmed = getattr(panel_user, 'expire_at', None)
    if db is None or not isinstance(pending, datetime) or not isinstance(confirmed, datetime):
        return
    if panel_datetime_to_utc(confirmed) < panel_datetime_to_utc(pending) - timedelta(seconds=60):
        return
    await db.flush()
    # Another renewal may have committed while this HTTP request was in flight.
    # A successful old response must not acknowledge that newer expiry.
    await db.execute(
        update(Subscription)
        .where(
            Subscription.id == subscription.id,
            Subscription.pending_panel_expire_at == pending,
            Subscription.end_date == pending,
        )
        .values(pending_panel_expire_at=None)
        .execution_options(synchronize_session=False)
    )
    await db.refresh(subscription, ['pending_panel_expire_at'])
