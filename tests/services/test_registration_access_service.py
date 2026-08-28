from types import SimpleNamespace

import pytest

from app.database.models import UserStatus
from app.services.registration_access_service import (
    RegistrationAccessContext,
    RegistrationAccessReason,
    RegistrationAccessService,
    RegistrationChannel,
    RegistrationInviteEvidence,
    RegistrationInviteKind,
    RegistrationInviteValidator,
    VerifiedRegistrationIdentity,
)


class FakeValidator:
    def __init__(self, evidence=None, error=None):
        self.evidence = evidence
        self.error = error
        self.calls = []

    async def validate(self, db, *, start_parameter, identity, allow_gift, lock_limited):
        self.calls.append((db, start_parameter, identity, allow_gift, lock_limited))
        if self.error:
            raise self.error
        return self.evidence


def reader(enabled: bool, allow_gift: bool = True):
    async def _read(db, key, default):
        if key == 'INVITE_ONLY_ENABLED':
            return enabled
        if key == 'INVITE_ONLY_ALLOW_GIFT_LINKS':
            return allow_gift
        return default

    return _read


def context(status=None, *, admin=False, channel=RegistrationChannel.TELEGRAM_START, payload='code'):
    user = None if status is None else SimpleNamespace(id=10, status=status)
    return RegistrationAccessContext(
        channel=channel,
        identity=VerifiedRegistrationIdentity(telegram_id=123, verified_admin=admin),
        existing_user=user,
        start_parameter=payload,
    )


@pytest.mark.parametrize(
    ('enabled', 'status', 'admin', 'evidence', 'allowed', 'reason'),
    [
        (False, None, False, None, True, RegistrationAccessReason.INVITE_ONLY_DISABLED),
        (True, UserStatus.ACTIVE.value, False, None, True, RegistrationAccessReason.EXISTING_ACTIVE),
        (
            True,
            UserStatus.BLOCKED.value,
            False,
            RegistrationInviteEvidence(RegistrationInviteKind.REFERRAL),
            False,
            RegistrationAccessReason.BLOCKED,
        ),
        (True, UserStatus.DELETED.value, False, None, False, RegistrationAccessReason.INVITE_REQUIRED),
        (True, None, False, None, False, RegistrationAccessReason.INVITE_REQUIRED),
        # An env-configured admin outranks BLOCKED: blocking such an account is refused
        # at every write site, so the flag can only come from the broadcast auto-block
        # after the owner muted the bot — honouring it would lock the owner out for good.
        (True, UserStatus.BLOCKED.value, True, None, True, RegistrationAccessReason.VERIFIED_ADMIN),
        (False, UserStatus.BLOCKED.value, True, None, True, RegistrationAccessReason.VERIFIED_ADMIN),
        (True, UserStatus.DELETED.value, True, None, True, RegistrationAccessReason.VERIFIED_ADMIN),
        (
            True,
            None,
            False,
            RegistrationInviteEvidence(RegistrationInviteKind.CAMPAIGN),
            True,
            RegistrationAccessReason.INVITE_GRANTED,
        ),
    ],
)
async def test_access_matrix(enabled, status, admin, evidence, allowed, reason):
    validator = FakeValidator(evidence=evidence)
    service = RegistrationAccessService(invite_validator=validator, settings_reader=reader(enabled))

    decision = await service.evaluate(object(), context(status, admin=admin))

    assert decision.allowed is allowed
    assert decision.reason is reason


async def test_non_telegram_channel_cannot_create_or_revive_when_enabled():
    service = RegistrationAccessService(
        invite_validator=FakeValidator(RegistrationInviteEvidence(RegistrationInviteKind.REFERRAL)),
        settings_reader=reader(True),
    )

    decision = await service.evaluate(
        object(),
        context(None, channel=RegistrationChannel.CABINET_EMAIL),
    )

    assert decision.allowed is False
    assert decision.reason is RegistrationAccessReason.CHANNEL_NOT_ALLOWED


async def test_web_gift_claim_is_admitted_by_the_gift_token_it_carries():
    """The 64-char token the web claim requires is the same bearer invite the deep link wraps."""
    validator = FakeValidator(RegistrationInviteEvidence(RegistrationInviteKind.GIFT))
    service = RegistrationAccessService(invite_validator=validator, settings_reader=reader(True))

    decision = await service.evaluate(
        object(),
        context(None, channel=RegistrationChannel.LANDING_GIFT_CLAIM, payload='GIFT_' + 'T' * 64),
    )

    assert decision.allowed is True
    assert decision.reason is RegistrationAccessReason.INVITE_GRANTED
    assert validator.calls[0][1] == 'GIFT_' + 'T' * 64


async def test_web_gift_claim_without_resolvable_gift_stays_denied():
    validator = FakeValidator(evidence=None)
    service = RegistrationAccessService(invite_validator=validator, settings_reader=reader(True))

    decision = await service.evaluate(
        object(),
        context(None, channel=RegistrationChannel.LANDING_GIFT_CLAIM, payload='GIFT_' + 'T' * 64),
    )

    assert decision.allowed is False
    assert decision.reason is RegistrationAccessReason.INVITE_REQUIRED


async def test_web_gift_claim_respects_disabled_gift_invites():
    validator = FakeValidator(RegistrationInviteEvidence(RegistrationInviteKind.GIFT))
    service = RegistrationAccessService(
        invite_validator=validator,
        settings_reader=reader(True, allow_gift=False),
    )

    await service.evaluate(
        object(),
        context(None, channel=RegistrationChannel.LANDING_GIFT_CLAIM, payload='GIFT_' + 'T' * 64),
    )

    assert validator.calls[0][3] is False


async def test_active_user_does_not_touch_settings_or_validator():
    async def fail_reader(*args, **kwargs):
        raise AssertionError('settings must not be read for active user')

    validator = FakeValidator(error=AssertionError('validator must not run'))
    service = RegistrationAccessService(invite_validator=validator, settings_reader=fail_reader)

    decision = await service.evaluate(object(), context(UserStatus.ACTIVE.value))

    assert decision.allowed is True
    assert validator.calls == []


async def test_validator_error_is_fail_closed_for_new_user():
    service = RegistrationAccessService(
        invite_validator=FakeValidator(error=RuntimeError('db unavailable')),
        settings_reader=reader(True),
    )

    decision = await service.evaluate(object(), context(None))

    assert decision.allowed is False
    assert decision.reason is RegistrationAccessReason.CHECK_UNAVAILABLE


async def test_gift_flag_is_forwarded_to_validator():
    validator = FakeValidator()
    service = RegistrationAccessService(invite_validator=validator, settings_reader=reader(True, False))

    await service.evaluate(object(), context(None, payload='GIFT_secret'))

    assert validator.calls[0][3] is False


async def test_invite_validator_protocol_stub_is_not_executable():
    with pytest.raises(NotImplementedError):
        await RegistrationInviteValidator.validate(
            object(),
            object(),
            start_parameter=None,
            identity=VerifiedRegistrationIdentity(),
            allow_gift=False,
            lock_limited=False,
        )
