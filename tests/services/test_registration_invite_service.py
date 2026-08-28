from types import SimpleNamespace

import pytest

from app.database.models import GuestPurchaseStatus, UserStatus
from app.services.registration_access_service import (
    RegistrationInviteKind,
    VerifiedRegistrationIdentity,
)
from app.services.registration_invite_service import RegistrationInviteService


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class GiftScalars:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class GiftResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return GiftScalars(self.value)


class FakeDB:
    def __init__(self, gift=None):
        self.gift = gift
        self.flushed = 0

    async def execute(self, statement):
        return GiftResult(self.gift)

    async def flush(self):
        self.flushed += 1


@pytest.mark.parametrize('status', [UserStatus.BLOCKED.value, UserStatus.DELETED.value])
async def test_inactive_referrer_is_not_an_invitation(monkeypatch, status):
    referrer = SimpleNamespace(id=1, status=status, telegram_id=777, email=None)

    async def get_referrer(db, code):
        return referrer

    monkeypatch.setattr('app.services.registration_invite_service.get_user_by_referral_code', get_referrer)

    async def no_campaign(*args, **kwargs):
        return None

    monkeypatch.setattr('app.services.registration_invite_service.get_campaign_by_start_parameter', no_campaign)
    service = RegistrationInviteService()

    evidence = await service.validate(
        object(),
        start_parameter='ref-code',
        identity=VerifiedRegistrationIdentity(telegram_id=123),
        allow_gift=True,
        lock_limited=True,
    )

    assert evidence is None


async def test_active_referrer_grants_invitation(monkeypatch):
    referrer = SimpleNamespace(id=1, status=UserStatus.ACTIVE.value, telegram_id=777, email=None)

    async def no_campaign(*args, **kwargs):
        return None

    async def get_referrer(*args, **kwargs):
        return referrer

    monkeypatch.setattr('app.services.registration_invite_service.get_campaign_by_start_parameter', no_campaign)
    monkeypatch.setattr('app.services.registration_invite_service.get_user_by_referral_code', get_referrer)

    evidence = await RegistrationInviteService().validate(
        object(),
        start_parameter='ref-code',
        identity=VerifiedRegistrationIdentity(telegram_id=123),
        allow_gift=True,
        lock_limited=True,
    )

    assert evidence.kind is RegistrationInviteKind.REFERRAL
    assert evidence.referrer_id == 1


async def test_active_campaign_grants_invitation(monkeypatch):
    campaign = SimpleNamespace(id=2, start_parameter='campaign', is_active=True, partner_user_id=None)

    async def get_campaign(*args, **kwargs):
        return campaign

    monkeypatch.setattr('app.services.registration_invite_service.get_campaign_by_start_parameter', get_campaign)

    evidence = await RegistrationInviteService().validate(
        object(),
        start_parameter='campaign',
        identity=VerifiedRegistrationIdentity(telegram_id=123),
        allow_gift=True,
        lock_limited=True,
    )

    assert evidence.kind is RegistrationInviteKind.CAMPAIGN
    assert evidence.campaign_id == 2


async def test_gift_is_not_accepted_when_gift_invites_are_disabled():
    db = FakeDB(gift=object())

    evidence = await RegistrationInviteService().validate(
        db,
        start_parameter='GIFT_' + 'a' * 48,
        identity=VerifiedRegistrationIdentity(telegram_id=123),
        allow_gift=False,
        lock_limited=True,
    )

    assert evidence is None


async def test_claimable_gift_is_returned_and_bound_without_commit(monkeypatch):
    gift = SimpleNamespace(
        id=5,
        token='x' * 64,
        buyer_user_id=8,
        user_id=None,
        status=GuestPurchaseStatus.PAID.value,
        is_gift=True,
    )
    db = FakeDB(gift)
    service = RegistrationInviteService()

    evidence = await service.validate(
        db,
        start_parameter='GIFT_' + gift.token[:48],
        identity=VerifiedRegistrationIdentity(user_id=11, telegram_id=123),
        allow_gift=True,
        lock_limited=True,
    )
    user = SimpleNamespace(id=11)
    await service.bind_locked_gift(db, evidence=evidence, user=user)

    assert evidence.kind is RegistrationInviteKind.GIFT
    assert gift.user_id == 11
    assert gift.status == GuestPurchaseStatus.PENDING_ACTIVATION.value
    assert db.flushed == 1


async def test_self_gift_does_not_grant_invitation():
    gift = SimpleNamespace(
        id=5,
        token='x' * 64,
        buyer_user_id=11,
        user_id=None,
        status=GuestPurchaseStatus.PAID.value,
        is_gift=True,
    )

    evidence = await RegistrationInviteService().validate(
        FakeDB(gift),
        start_parameter='GIFT_' + gift.token[:48],
        identity=VerifiedRegistrationIdentity(user_id=11, telegram_id=123),
        allow_gift=True,
        lock_limited=True,
    )

    assert evidence is None


async def test_early_gift_validation_does_not_lock_row(monkeypatch):
    calls = []
    gift = SimpleNamespace(
        id=5,
        token='x' * 64,
        buyer_user_id=8,
        user_id=None,
        status=GuestPurchaseStatus.PAID.value,
        is_gift=True,
    )

    async def get_gift(db, token, *, for_update):
        calls.append(for_update)
        return gift

    monkeypatch.setattr('app.services.registration_invite_service.get_claimable_gift', get_gift)

    evidence = await RegistrationInviteService().validate(
        object(),
        start_parameter='GIFT_' + gift.token[:48],
        identity=VerifiedRegistrationIdentity(telegram_id=123),
        allow_gift=True,
        lock_limited=False,
    )

    assert evidence.kind is RegistrationInviteKind.GIFT
    assert calls == [False]
