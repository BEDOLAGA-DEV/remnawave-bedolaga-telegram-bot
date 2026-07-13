"""Tests for the wholesale coupon service (batches of one-time links).

Coupons are the fourth "grant subscription days" mechanism (after promocodes,
gifts and campaigns): the admin batch-generates one-time tokens, a user
redeems one via the ``/start coupon_<token>`` deep link. These tests pin the
redemption contract:

- token format is validated before any DB access;
- responses are uniform for unknown / revoked / redeemed-by-someone-else
  coupons (no oracle for enumeration), while redeemed-by-you is friendly;
- validation runs on an unlocked read; the claim re-reads the row FOR UPDATE
  and re-checks it, and the REDEEMED flip happens BEFORE the Remnawave sync
  (which commits the session internally), so claim and grant land in one
  transaction;
- a failed Remnawave sync (the method swallows errors and returns None)
  aborts the redemption: rollback, ``internal`` error, coupon stays ACTIVE
  and the link retryable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.database.crud.coupon import generate_coupon_token
from app.database.models import CouponStatus
from app.services.coupon_service import (
    COUPON_DEEP_LINK_PREFIX,
    CouponRedemptionError,
    _grant_subscription_days,
    is_coupon_token,
    redeem_coupon,
)


VALID_TOKEN = 'a1' * 16  # 32 hex chars


def _user(user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(id=user_id)


def _tariff() -> SimpleNamespace:
    return SimpleNamespace(id=3, name='Basic', allowed_squads=['sq-1'], traffic_limit_gb=100, device_limit=2)


def _batch(**overrides) -> SimpleNamespace:
    base = {'id': 1, 'tariff_id': 3, 'period_days': 30, 'is_expired': False, 'tariff': _tariff()}
    base.update(overrides)
    return SimpleNamespace(**base)


def _coupon(status: str = CouponStatus.ACTIVE.value, redeemed_by: int | None = None, batch=None) -> SimpleNamespace:
    if batch is None:
        batch = _batch()
    return SimpleNamespace(
        id=11, batch_id=batch.id, status=status, redeemed_by=redeemed_by, redeemed_at=None, batch=batch
    )


# --- Token format ---------------------------------------------------------


def test_generated_token_matches_format_and_fits_start_param() -> None:
    for _ in range(100):
        token = generate_coupon_token()
        assert is_coupon_token(token)
        # Telegram truncates start params longer than 64 chars; the coupon flow
        # relies on exact-match lookups, so the payload must never be truncated.
        assert len(COUPON_DEEP_LINK_PREFIX + token) <= 64


def test_generated_tokens_are_unique() -> None:
    tokens = {generate_coupon_token() for _ in range(1000)}
    assert len(tokens) == 1000


def test_is_coupon_token_rejects_wrong_shapes() -> None:
    assert not is_coupon_token('')
    assert not is_coupon_token('abc')
    assert not is_coupon_token('g' * 32)  # non-hex
    assert not is_coupon_token('a' * 31)
    assert not is_coupon_token('a' * 33)
    # redeem_coupon lowercases before validating, the matcher itself is strict
    assert not is_coupon_token('A' * 32)


# --- Redemption: validation and status handling ---------------------------


@pytest.mark.asyncio
async def test_invalid_format_never_touches_db() -> None:
    db = AsyncMock()
    with pytest.raises(CouponRedemptionError) as err:
        await redeem_coupon(db, 'not-a-token', _user())
    assert err.value.code == 'invalid'
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_token_is_invalid() -> None:
    db = AsyncMock()
    lookup = AsyncMock(return_value=None)
    with patch('app.services.coupon_service.get_coupon_by_token', lookup):
        with pytest.raises(CouponRedemptionError) as err:
            await redeem_coupon(db, VALID_TOKEN, _user())
    assert err.value.code == 'invalid'
    lookup.assert_awaited_once_with(db, VALID_TOKEN)


@pytest.mark.asyncio
async def test_token_is_normalized_before_lookup() -> None:
    db = AsyncMock()
    lookup = AsyncMock(return_value=None)
    with patch('app.services.coupon_service.get_coupon_by_token', lookup):
        with pytest.raises(CouponRedemptionError):
            await redeem_coupon(db, f'  {VALID_TOKEN.upper()} ', _user())
    lookup.assert_awaited_once_with(db, VALID_TOKEN)


@pytest.mark.asyncio
async def test_rejection_never_takes_the_row_lock() -> None:
    """Failed links must not hold FOR UPDATE for the rest of the /start handler."""
    db = AsyncMock()
    coupon = _coupon(status=CouponStatus.REVOKED.value)
    with patch('app.services.coupon_service.get_coupon_by_token', AsyncMock(return_value=coupon)):
        with pytest.raises(CouponRedemptionError):
            await redeem_coupon(db, VALID_TOKEN, _user())
    db.refresh.assert_not_called()


@pytest.mark.asyncio
async def test_redeemed_by_same_user_is_distinguishable() -> None:
    coupon = _coupon(status=CouponStatus.REDEEMED.value, redeemed_by=7)
    with patch('app.services.coupon_service.get_coupon_by_token', AsyncMock(return_value=coupon)):
        with pytest.raises(CouponRedemptionError) as err:
            await redeem_coupon(AsyncMock(), VALID_TOKEN, _user(7))
    assert err.value.code == 'already_redeemed_by_you'


@pytest.mark.asyncio
async def test_redeemed_by_other_user_is_uniform_invalid() -> None:
    coupon = _coupon(status=CouponStatus.REDEEMED.value, redeemed_by=8)
    with patch('app.services.coupon_service.get_coupon_by_token', AsyncMock(return_value=coupon)):
        with pytest.raises(CouponRedemptionError) as err:
            await redeem_coupon(AsyncMock(), VALID_TOKEN, _user(7))
    assert err.value.code == 'invalid'


@pytest.mark.asyncio
async def test_revoked_coupon_is_uniform_invalid() -> None:
    coupon = _coupon(status=CouponStatus.REVOKED.value)
    with patch('app.services.coupon_service.get_coupon_by_token', AsyncMock(return_value=coupon)):
        with pytest.raises(CouponRedemptionError) as err:
            await redeem_coupon(AsyncMock(), VALID_TOKEN, _user())
    assert err.value.code == 'invalid'


@pytest.mark.asyncio
async def test_expired_batch_raises_expired() -> None:
    coupon = _coupon(batch=_batch(is_expired=True))
    with patch('app.services.coupon_service.get_coupon_by_token', AsyncMock(return_value=coupon)):
        with pytest.raises(CouponRedemptionError) as err:
            await redeem_coupon(AsyncMock(), VALID_TOKEN, _user())
    assert err.value.code == 'expired'
    assert coupon.status == CouponStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_missing_tariff_is_internal_error() -> None:
    coupon = _coupon(batch=_batch(tariff=None))
    with patch('app.services.coupon_service.get_coupon_by_token', AsyncMock(return_value=coupon)):
        with pytest.raises(CouponRedemptionError) as err:
            await redeem_coupon(AsyncMock(), VALID_TOKEN, _user())
    assert err.value.code == 'internal'
    assert coupon.status == CouponStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_concurrent_claim_lost_after_lock_is_rejected() -> None:
    """The locked re-read must re-check the status — a concurrent redemption may win."""
    coupon = _coupon()
    db = AsyncMock()

    async def refresh_reveals_concurrent_redeem(instance, **kwargs):
        instance.status = CouponStatus.REDEEMED.value
        instance.redeemed_by = 999

    db.refresh = AsyncMock(side_effect=refresh_reveals_concurrent_redeem)
    with patch('app.services.coupon_service.get_coupon_by_token', AsyncMock(return_value=coupon)):
        with pytest.raises(CouponRedemptionError) as err:
            await redeem_coupon(db, VALID_TOKEN, _user())
    assert err.value.code == 'invalid'
    db.commit.assert_not_called()


# --- Redemption: happy path and failure atomicity -------------------------


@pytest.mark.asyncio
async def test_success_claims_under_lock_and_flips_before_remnawave_sync() -> None:
    coupon = _coupon()
    user = _user()
    db = AsyncMock()
    end_date = datetime.now(UTC) + timedelta(days=30)
    subscription = SimpleNamespace(id=99, end_date=end_date, traffic_limit_gb=100, device_limit=2)

    status_at_sync: list[str] = []

    async def fake_sync(self, db_arg, sub):
        # create_remnawave_user() commits the session internally, so by this
        # point the coupon MUST already be claimed — otherwise the grant could
        # commit while the coupon is still ACTIVE (double-payout window).
        status_at_sync.append(coupon.status)
        return SimpleNamespace(uuid='rw-uuid')

    with (
        patch('app.services.coupon_service.get_coupon_by_token', AsyncMock(return_value=coupon)),
        patch('app.services.coupon_service._grant_subscription_days', AsyncMock(return_value=(subscription, True))),
        patch('app.services.coupon_service.SubscriptionService.create_remnawave_user', new=fake_sync),
    ):
        result = await redeem_coupon(db, VALID_TOKEN, user)

    db.refresh.assert_awaited_once_with(coupon, with_for_update=True)
    assert status_at_sync == [CouponStatus.REDEEMED.value]
    assert coupon.redeemed_by == user.id
    assert coupon.redeemed_at is not None
    db.commit.assert_awaited_once()
    db.rollback.assert_not_called()
    assert result.tariff_name == 'Basic'
    assert result.period_days == 30
    assert result.renewed is True
    assert result.end_date == end_date
    assert result.traffic_limit_gb == 100
    assert result.device_limit == 2


@pytest.mark.asyncio
async def test_failed_remnawave_sync_aborts_redemption() -> None:
    """create_remnawave_user swallows API errors and returns None WITHOUT
    committing — the redemption must roll back so the coupon is not burned
    while the user got no working panel account."""
    coupon = _coupon()
    db = AsyncMock()

    async def failing_sync(self, db_arg, sub):
        return None

    granted = (SimpleNamespace(id=99, end_date=None, traffic_limit_gb=0, device_limit=None), False)
    with (
        patch('app.services.coupon_service.get_coupon_by_token', AsyncMock(return_value=coupon)),
        patch('app.services.coupon_service._grant_subscription_days', AsyncMock(return_value=granted)),
        patch('app.services.coupon_service.SubscriptionService.create_remnawave_user', new=failing_sync),
    ):
        with pytest.raises(CouponRedemptionError) as err:
            await redeem_coupon(db, VALID_TOKEN, _user())

    assert err.value.code == 'internal'
    db.rollback.assert_awaited_once()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_grant_failure_rolls_back_and_raises_internal() -> None:
    coupon = _coupon()
    db = AsyncMock()
    with (
        patch('app.services.coupon_service.get_coupon_by_token', AsyncMock(return_value=coupon)),
        patch(
            'app.services.coupon_service._grant_subscription_days',
            AsyncMock(side_effect=RuntimeError('boom')),
        ),
    ):
        with pytest.raises(CouponRedemptionError) as err:
            await redeem_coupon(db, VALID_TOKEN, _user())
    assert err.value.code == 'internal'
    db.rollback.assert_awaited_once()
    db.commit.assert_not_called()
    assert coupon.status == CouponStatus.ACTIVE.value, 'failed redemption must not consume the coupon'


# --- Grant branches (single-tariff mode mirrors gift activation) ----------


@pytest.mark.asyncio
async def test_grant_extends_active_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    existing = SimpleNamespace(end_date=datetime.now(UTC) + timedelta(days=5), tariff_id=3)
    extend = AsyncMock(return_value='extended')
    with (
        patch('app.services.coupon_service.get_subscription_by_user_id', AsyncMock(return_value=existing)),
        patch('app.services.coupon_service.extend_subscription', extend),
    ):
        subscription, renewed = await _grant_subscription_days(AsyncMock(), _user(), _tariff(), 30)

    assert subscription == 'extended'
    assert renewed is True
    assert extend.await_args.args[2] == 30
    kwargs = extend.await_args.kwargs
    assert kwargs['commit'] is False, 'the caller owns the transaction'
    assert kwargs['tariff_id'] == 3


@pytest.mark.asyncio
async def test_grant_replaces_expired_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    existing = SimpleNamespace(end_date=datetime.now(UTC) - timedelta(days=1), tariff_id=99)
    replaced = SimpleNamespace(tariff_id=99)
    replace = AsyncMock(return_value=replaced)
    with (
        patch('app.services.coupon_service.get_subscription_by_user_id', AsyncMock(return_value=existing)),
        patch('app.services.coupon_service.replace_subscription', replace),
    ):
        subscription, renewed = await _grant_subscription_days(AsyncMock(), _user(), _tariff(), 30)

    kwargs = replace.await_args.kwargs
    assert kwargs['is_trial'] is False
    assert kwargs['commit'] is False
    assert kwargs['duration_days'] == 30
    assert renewed is False, 'replacing an expired subscription is an activation, not a renewal'
    assert subscription.tariff_id == 3, 'replaced subscription must be reassigned to the batch tariff'


@pytest.mark.asyncio
async def test_grant_creates_subscription_when_none_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    create = AsyncMock(return_value='created')
    with (
        patch('app.services.coupon_service.get_subscription_by_user_id', AsyncMock(return_value=None)),
        patch('app.services.coupon_service.create_paid_subscription', create),
    ):
        subscription, renewed = await _grant_subscription_days(AsyncMock(), _user(9), _tariff(), 30)

    assert subscription == 'created'
    assert renewed is False
    kwargs = create.await_args.kwargs
    assert kwargs['user_id'] == 9
    assert kwargs['duration_days'] == 30
    assert kwargs['tariff_id'] == 3
    assert kwargs['commit'] is False


@pytest.mark.asyncio
async def test_grant_multi_tariff_looks_up_by_tariff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    lookup = AsyncMock(return_value=None)
    create = AsyncMock(return_value='created')
    with (
        patch('app.database.crud.subscription.get_subscription_by_user_and_tariff', lookup),
        patch('app.services.coupon_service.create_paid_subscription', create),
    ):
        subscription, _renewed = await _grant_subscription_days(AsyncMock(), _user(9), _tariff(), 30)

    lookup.assert_awaited_once()
    assert lookup.await_args.args[1:] == (9, 3)
    assert subscription == 'created'
