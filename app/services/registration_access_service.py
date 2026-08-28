from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ENV_OVERRIDE_KEYS, settings
from app.database.crud.system_setting import get_setting_value
from app.database.models import GuestPurchase, User, UserStatus
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)


class RegistrationChannel(StrEnum):
    TELEGRAM_START = 'telegram_start'
    CABINET_TELEGRAM_INIT_DATA = 'cabinet_telegram_init_data'
    CABINET_TELEGRAM_WIDGET = 'cabinet_telegram_widget'
    CABINET_TELEGRAM_OIDC = 'cabinet_telegram_oidc'
    CABINET_EMAIL = 'cabinet_email'
    CABINET_OAUTH = 'cabinet_oauth'
    CABINET_SESSION = 'cabinet_session'
    CABINET_SUPPORT_WS = 'cabinet_support_ws'
    LANDING_PURCHASE = 'landing_purchase'
    LANDING_GIFT_CLAIM = 'landing_gift_claim'


class RegistrationAccessReason(StrEnum):
    INVITE_ONLY_DISABLED = 'invite_only_disabled'
    EXISTING_ACTIVE = 'existing_active'
    VERIFIED_ADMIN = 'verified_admin'
    INVITE_GRANTED = 'invite_granted'
    INVITE_REQUIRED = 'invite_required'
    BLOCKED = 'blocked'
    CHECK_UNAVAILABLE = 'check_unavailable'
    CHANNEL_NOT_ALLOWED = 'channel_not_allowed'


# Channels that can carry invite evidence in the request itself. Telegram /start
# carries the start parameter; the web gift claim carries the full 64-char gift
# token, which is the same bearer secret the ``GIFT_`` deep link wraps. Every other
# channel offers no proof of an invitation and can never create or revive a user
# while invite-only is on.
INVITE_BEARING_CHANNELS = frozenset(
    {
        RegistrationChannel.TELEGRAM_START,
        RegistrationChannel.LANDING_GIFT_CLAIM,
    }
)


class RegistrationInviteKind(StrEnum):
    REFERRAL = 'referral'
    CAMPAIGN = 'campaign'
    GIFT = 'gift'


@dataclass(frozen=True, slots=True)
class VerifiedRegistrationIdentity:
    user_id: int | None = None
    telegram_id: int | None = None
    email: str | None = None
    email_verified: bool = False
    verified_admin: bool = False


@dataclass(slots=True)
class RegistrationInviteEvidence:
    kind: RegistrationInviteKind
    referrer_id: int | None = None
    campaign_id: int | None = None
    campaign_slug: str | None = None
    locked_gift: GuestPurchase | None = None
    fingerprint: str | None = None


class RegistrationInviteValidator(Protocol):
    async def validate(
        self,
        db: AsyncSession,
        *,
        start_parameter: str | None,
        identity: VerifiedRegistrationIdentity,
        allow_gift: bool,
        lock_limited: bool,
    ) -> RegistrationInviteEvidence | None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class RegistrationAccessContext:
    channel: RegistrationChannel
    identity: VerifiedRegistrationIdentity
    existing_user: User | None = None
    start_parameter: str | None = None
    lock_limited_invite: bool = True


@dataclass(frozen=True, slots=True)
class RegistrationAccessDecision:
    allowed: bool
    reason: RegistrationAccessReason
    evidence: RegistrationInviteEvidence | None = None


SettingsReader = Callable[[AsyncSession, str, bool], Awaitable[bool]]


async def _read_effective_bool(db: AsyncSession, key: str, default: bool) -> bool:
    if key in ENV_OVERRIDE_KEYS:
        return bool(getattr(settings, key))

    raw = await get_setting_value(db, key)
    if raw is None:
        return bool(getattr(settings, key, default))

    value = bot_configuration_service.deserialize_value(key, raw)
    if not isinstance(value, bool):
        raise ValueError(f'{key} did not deserialize to bool')
    return value


class RegistrationAccessService:
    def __init__(
        self,
        *,
        invite_validator: RegistrationInviteValidator | None = None,
        settings_reader: SettingsReader | None = None,
    ) -> None:
        self._invite_validator = invite_validator
        self._settings_reader = settings_reader or _read_effective_bool

    async def evaluate(
        self,
        db: AsyncSession,
        context: RegistrationAccessContext,
    ) -> RegistrationAccessDecision:
        user = context.existing_user
        status = getattr(user, 'status', None)

        # The env config is the root of trust and outranks BLOCKED. Blocking is refused
        # for these accounts at every write site (see is_protected_from_blocking), so a
        # BLOCKED admin can only be a row predating that guard — most likely written by
        # the broadcast auto-block after the owner muted the bot, never an intentional
        # ban. Recovering here is what keeps that from being a permanent lockout.
        if context.identity.verified_admin:
            return self._decision(True, RegistrationAccessReason.VERIFIED_ADMIN, context)

        if status == UserStatus.ACTIVE.value:
            return self._decision(True, RegistrationAccessReason.EXISTING_ACTIVE, context)

        if status == UserStatus.BLOCKED.value:
            return self._decision(False, RegistrationAccessReason.BLOCKED, context)

        try:
            enabled = await self._settings_reader(db, 'INVITE_ONLY_ENABLED', False)
        except Exception:
            logger.exception(
                'Failed to resolve invite-only setting',
                channel=context.channel.value,
                user_id=getattr(user, 'id', None),
                telegram_id=context.identity.telegram_id,
            )
            return self._decision(False, RegistrationAccessReason.CHECK_UNAVAILABLE, context)

        if not enabled:
            return self._decision(True, RegistrationAccessReason.INVITE_ONLY_DISABLED, context)

        if context.channel not in INVITE_BEARING_CHANNELS:
            return self._decision(False, RegistrationAccessReason.CHANNEL_NOT_ALLOWED, context)

        if self._invite_validator is None:
            return self._decision(False, RegistrationAccessReason.CHECK_UNAVAILABLE, context)

        try:
            allow_gift = await self._settings_reader(db, 'INVITE_ONLY_ALLOW_GIFT_LINKS', True)
            evidence = await self._invite_validator.validate(
                db,
                start_parameter=context.start_parameter,
                identity=context.identity,
                allow_gift=allow_gift,
                lock_limited=context.lock_limited_invite,
            )
        except Exception:
            logger.exception(
                'Invite validation failed',
                channel=context.channel.value,
                user_id=getattr(user, 'id', None),
                telegram_id=context.identity.telegram_id,
            )
            return self._decision(False, RegistrationAccessReason.CHECK_UNAVAILABLE, context)

        if evidence is None:
            return self._decision(False, RegistrationAccessReason.INVITE_REQUIRED, context)
        return self._decision(True, RegistrationAccessReason.INVITE_GRANTED, context, evidence)

    @staticmethod
    def _decision(
        allowed: bool,
        reason: RegistrationAccessReason,
        context: RegistrationAccessContext,
        evidence: RegistrationInviteEvidence | None = None,
    ) -> RegistrationAccessDecision:
        logger.info(
            'Registration access decision',
            allowed=allowed,
            reason=reason.value,
            channel=context.channel.value,
            user_id=getattr(context.existing_user, 'id', None),
            telegram_id=context.identity.telegram_id,
            invite_kind=evidence.kind.value if evidence else None,
            invite_fingerprint=evidence.fingerprint if evidence else None,
        )
        return RegistrationAccessDecision(allowed=allowed, reason=reason, evidence=evidence)
