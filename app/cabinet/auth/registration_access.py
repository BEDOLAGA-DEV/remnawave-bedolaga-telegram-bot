from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.registration_access_service import (
    RegistrationAccessContext,
    RegistrationAccessDecision,
    RegistrationAccessReason,
    RegistrationAccessService,
    RegistrationChannel,
    VerifiedRegistrationIdentity,
)
from app.services.registration_invite_service import RegistrationInviteService


_registration_access_service = RegistrationAccessService(invite_validator=RegistrationInviteService())


async def evaluate_public_registration(
    db: AsyncSession,
    *,
    channel: RegistrationChannel,
    existing_user=None,
    telegram_id: int | None = None,
    email: str | None = None,
    email_verified: bool = False,
    verified_admin: bool = False,
) -> RegistrationAccessDecision:
    if not settings.INVITE_ONLY_ENABLED:
        return RegistrationAccessDecision(
            allowed=True,
            reason=RegistrationAccessReason.INVITE_ONLY_DISABLED,
        )

    return await _registration_access_service.evaluate(
        db,
        RegistrationAccessContext(
            channel=channel,
            identity=VerifiedRegistrationIdentity(
                user_id=getattr(existing_user, 'id', None),
                telegram_id=telegram_id,
                email=email,
                email_verified=email_verified,
                verified_admin=verified_admin,
            ),
            existing_user=existing_user,
            start_parameter=None,
            lock_limited_invite=False,
        ),
    )


def raise_for_registration_decision(decision: RegistrationAccessDecision) -> None:
    if decision.allowed:
        return
    if decision.reason is RegistrationAccessReason.CHECK_UNAVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={'code': 'registration_check_unavailable'},
        )
    if decision.reason is RegistrationAccessReason.BLOCKED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='User account is not active',
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={'code': 'registration_invite_required'},
    )
