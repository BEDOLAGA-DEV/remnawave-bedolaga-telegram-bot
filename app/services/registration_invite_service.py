from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.campaign import get_campaign_by_start_parameter
from app.database.crud.user import get_user_by_referral_code
from app.database.models import GuestPurchaseStatus, User, UserStatus
from app.services.guest_purchase_service import get_claimable_gift
from app.services.registration_access_service import (
    RegistrationInviteEvidence,
    RegistrationInviteKind,
    VerifiedRegistrationIdentity,
)


class RegistrationInviteConflict(RuntimeError):
    pass


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]


class RegistrationInviteService:
    async def validate(
        self,
        db: AsyncSession,
        *,
        start_parameter: str | None,
        identity: VerifiedRegistrationIdentity,
        allow_gift: bool,
        lock_limited: bool,
    ) -> RegistrationInviteEvidence | None:
        value = (start_parameter or '').strip()
        if not value:
            return None

        if value.startswith(('GIFT_', 'giftclaim_')):
            if not allow_gift:
                return None
            token = value.removeprefix('GIFT_').removeprefix('giftclaim_')
            gift = await get_claimable_gift(db, token, for_update=lock_limited)
            if gift is None:
                return None
            if identity.user_id is not None and gift.buyer_user_id == identity.user_id:
                return None
            if gift.user_id is not None and gift.user_id != identity.user_id:
                return None
            return RegistrationInviteEvidence(
                kind=RegistrationInviteKind.GIFT,
                locked_gift=gift,
                fingerprint=_fingerprint(token),
            )

        campaign = await get_campaign_by_start_parameter(db, value, only_active=True)
        if campaign is not None:
            if identity.user_id is not None and campaign.partner_user_id == identity.user_id:
                return None
            return RegistrationInviteEvidence(
                kind=RegistrationInviteKind.CAMPAIGN,
                campaign_id=campaign.id,
                campaign_slug=campaign.start_parameter,
                fingerprint=_fingerprint(value),
            )

        referrer = await get_user_by_referral_code(db, value)
        if referrer is None or referrer.status != UserStatus.ACTIVE.value:
            return None
        if identity.user_id is not None and referrer.id == identity.user_id:
            return None
        if identity.telegram_id is not None and referrer.telegram_id == identity.telegram_id:
            return None
        if identity.email and referrer.email and referrer.email.lower() == identity.email.lower():
            return None
        return RegistrationInviteEvidence(
            kind=RegistrationInviteKind.REFERRAL,
            referrer_id=referrer.id,
            fingerprint=_fingerprint(value),
        )

    async def bind_locked_gift(
        self,
        db: AsyncSession,
        *,
        evidence: RegistrationInviteEvidence | None,
        user: User,
    ) -> None:
        if evidence is None or evidence.kind is not RegistrationInviteKind.GIFT:
            return
        gift = evidence.locked_gift
        if gift is None:
            raise RegistrationInviteConflict('gift evidence has no locked row')
        if gift.buyer_user_id == user.id:
            raise RegistrationInviteConflict('buyer cannot claim own gift')
        if gift.user_id not in (None, user.id):
            raise RegistrationInviteConflict('gift is already bound to another user')
        if gift.status not in {
            GuestPurchaseStatus.PAID.value,
            GuestPurchaseStatus.PENDING_ACTIVATION.value,
        }:
            raise RegistrationInviteConflict('gift is not claimable')
        gift.user_id = user.id
        if gift.status == GuestPurchaseStatus.PAID.value:
            gift.status = GuestPurchaseStatus.PENDING_ACTIVATION.value
        await db.flush()
