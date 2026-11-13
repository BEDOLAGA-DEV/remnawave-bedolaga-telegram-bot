"""HOT Pay local conflict/cancellation guards; never commit or contact provider."""

from datetime import UTC, datetime

from sqlalchemy import and_, exists, or_, select, update

from app.config import settings
from app.database.models import PayHotAgreement, PayHotChargeAttempt, PayHotPayment, User


LIVE_STATUSES = ('CREATING', 'CREATION_UNKNOWN', 'PENDING', 'ACTIVE', 'PAUSED', 'CANCEL_PENDING')
CONFLICT_MESSAGE = 'Сначала отключите автоплатёж HOT Pay и дождитесь подтверждения отключения.'


def payhot_obligation_condition():
    """Revocation stops future debits, not reconciliation of a submitted charge."""
    unresolved = exists(
        select(PayHotChargeAttempt.id).where(
            PayHotChargeAttempt.agreement_id == PayHotAgreement.id,
            or_(
                PayHotChargeAttempt.status.in_(
                    ('RESERVED', 'SENDING', 'UNKNOWN', 'PROCESSING', 'REVIEW', 'REFUND_REVIEW')
                ),
                and_(PayHotChargeAttempt.status == 'SUCCEEDED', PayHotChargeAttempt.delivery_completed_at.is_(None)),
            ),
        )
    )
    return or_(PayHotAgreement.status.in_(LIVE_STATUSES), unresolved)


async def has_live_payhot(db, subscription_id: int) -> bool:
    return (
        await db.scalar(
            select(PayHotAgreement.id)
            .where(PayHotAgreement.subscription_id == subscription_id, payhot_obligation_condition())
            .limit(1)
        )
        is not None
    )


async def has_conflicting_payhot(db, *, user_id: int, subscription_id: int) -> bool:
    query = select(PayHotAgreement.id).where(PayHotAgreement.owner_id == user_id, payhot_obligation_condition())
    if settings.is_multi_tariff_enabled():
        query = query.where(PayHotAgreement.subscription_id == subscription_id)
    return await db.scalar(query.limit(1)) is not None


async def require_no_payhot(db, *, user_id: int, subscription_id: int):
    # Enrollment uses the same account lock. Feature flags never suppress old obligations.
    await db.scalar(select(User.id).where(User.id == user_id).with_for_update())
    if await has_conflicting_payhot(db, user_id=user_id, subscription_id=subscription_id):
        raise ValueError(CONFLICT_MESSAGE)


async def _stage(db, condition, *, reason):
    await db.execute(
        update(PayHotAgreement)
        .where(condition, PayHotAgreement.status.in_(LIVE_STATUSES), PayHotAgreement.cancel_requested_at.is_(None))
        .values(status='CANCEL_PENDING', cancel_requested_at=datetime.now(UTC), reason=reason[:64])
    )


async def stage_payhot_cancellation(db, subscription_id: int, *, reason: str):
    await _stage(db, PayHotAgreement.subscription_id == subscription_id, reason=reason)


async def stage_payhot_owner_cancellation(db, user_id: int, *, reason: str):
    """Also catches unresolved enrollment whose subscription has been removed."""
    await _stage(db, PayHotAgreement.owner_id == user_id, reason=reason)


async def guard_payhot_credentials(db, key: str, value):
    if key not in ('PAYHOT_API_KEY', 'PAYHOT_API_URL', 'PAYHOT_WEBHOOK_SECRET'):
        return
    current = str(getattr(settings, key, None) or '').strip()
    value = str(value or '').strip()
    if value == current or (key == 'PAYHOT_WEBHOOK_SECRET' and value):
        return
    if await db.scalar(select(PayHotAgreement.id).where(payhot_obligation_condition()).limit(1)) is not None:
        raise ValueError(
            'Сначала отключите действующие автоплатежи HOT Pay. Реквизиты нужны для их отмены и проверки платежей.'
        )


def ordinary_payhot_invoice_condition():
    """Retain immutable first-charge proof even after revocation or owner removal."""
    return PayHotPayment.id.not_in(
        select(PayHotAgreement.first_payment_id).where(PayHotAgreement.first_payment_id.is_not(None))
    )
