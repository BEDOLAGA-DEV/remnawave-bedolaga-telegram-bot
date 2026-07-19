"""Source-level pins for the coupon deep-link wiring.

The /start flow is huge and heavily branched, and the coupon feature relies on
a few ordering invariants that are easy to break silently during refactors:

- the coupon branch must swallow ``start_parameter`` ONLY for exact-format
  tokens that exist in the DB (a campaign start_parameter may legitimately
  begin with ``coupon_``, even ``coupon_<32 hex>``);
- every gift-activation call site must also redeem pending coupons;
- in the service, validation runs unlocked, the claim happens under a
  FOR UPDATE re-read, the REDEEMED flip precedes the Remnawave sync (which
  commits the session internally), and the sync's None error-return aborts
  the redemption;
- in the admin module, the ``*_revoke_confirm_`` handler must be registered
  before the ``*_revoke_`` one (the shorter prefix matches both), and the
  create-confirmation callback must be guarded against stale buttons and
  double taps.
"""

import inspect

import app.handlers.start as start_module
from app.handlers.admin import coupons as admin_coupons
from app.services import coupon_service
from app.states import AdminStates


START_SOURCE = inspect.getsource(start_module)


def test_cmd_start_parses_coupon_deep_link() -> None:
    assert 'startswith(COUPON_DEEP_LINK_PREFIX)' in START_SOURCE
    assert 'pending_coupon_token=coupon_token' in START_SOURCE


def test_coupon_redeem_called_at_every_gift_activation_site() -> None:
    gift_calls = START_SOURCE.count('await _activate_pending_gift_after_registration(')
    coupon_calls = START_SOURCE.count('await _redeem_pending_coupon(')
    assert gift_calls == 3, 'gift activation call sites moved — re-check coupon redemption wiring'
    assert coupon_calls == gift_calls, 'every gift activation site must also redeem pending coupons'


def test_coupon_branch_only_swallows_existing_coupons() -> None:
    """`start_parameter = None` must sit INSIDE the is_coupon_token() guard AND
    require a DB hit — otherwise a campaign whose start_parameter is
    'coupon_<32 hex>' silently loses its attribution."""
    source = inspect.getsource(start_module.cmd_start)
    branch_start = source.index('startswith(COUPON_DEEP_LINK_PREFIX)')
    branch_end = source.index('webauth_', branch_start)
    branch = source[branch_start:branch_end]

    guard_pos = branch.index('if is_coupon_token(')
    exists_pos = branch.index('get_coupon_by_token(db, coupon_token)')
    null_pos = branch.index('start_parameter = None')
    assert guard_pos < exists_pos < null_pos

    guard_indent = len(branch[:guard_pos].split('\n')[-1])
    null_indent = len(branch[:null_pos].split('\n')[-1])
    assert null_indent > guard_indent, (
        'start_parameter must be swallowed only for real coupon tokens, '
        'otherwise campaigns whose start_parameter begins with "coupon_" break'
    )


def test_redeem_flips_status_before_remnawave_sync() -> None:
    source = inspect.getsource(coupon_service.redeem_coupon)
    flip = source.index('coupon.status = CouponStatus.REDEEMED.value')
    sync = source.index('create_remnawave_user')
    commit = source.index('await db.commit()')
    assert flip < sync < commit, (
        'create_remnawave_user() commits the session internally: the claim must '
        'already be part of that transaction, or the coupon can pay out twice'
    )


def test_redeem_claims_under_row_lock_and_rechecks() -> None:
    source = inspect.getsource(coupon_service.redeem_coupon)
    assert 'with_for_update=True' in source
    assert source.count('_check_redeemable(coupon, user)') == 2, (
        'validation must run both unlocked (no lock held on rejection paths) '
        'and again after the FOR UPDATE re-read (concurrent claim may have won)'
    )


def test_redeem_aborts_when_remnawave_sync_returns_none() -> None:
    source = inspect.getsource(coupon_service.redeem_coupon)
    assert 'remnawave_user is None' in source, (
        'create_remnawave_user swallows errors and returns None without '
        'committing — ignoring that burns the coupon during any panel outage'
    )


def test_revoke_confirm_registered_before_revoke_prefix() -> None:
    source = inspect.getsource(admin_coupons.register_handlers)
    confirm = source.index("'admin_coupon_revoke_confirm_'")
    revoke = source.index("'admin_coupon_revoke_'")
    assert confirm < revoke, "startswith('admin_coupon_revoke_') also matches confirmation callbacks"


def test_create_confirm_is_guarded_against_stale_buttons_and_double_taps() -> None:
    source = inspect.getsource(admin_coupons.confirm_coupon_batch_creation)
    # Stale confirm buttons from old messages must be rejected by FSM state
    assert 'creating_coupon_batch_expiry.state' in source
    # The wizard state must be consumed BEFORE the slow batch insert
    clear = source.index('await state.clear()')
    create = source.index('await create_coupon_batch(')
    assert clear < create, 'a double-tap on the confirm button must not create the batch twice'
    # FSM check→clear is not atomic across awaits (aiogram dispatches updates
    # concurrently), so a synchronous set guard must close the double-tap window:
    # the membership check and .add() run with no await between them.
    assert isinstance(admin_coupons._batch_creation_in_progress, set)
    add = source.index('_batch_creation_in_progress.add(')
    check = source.index('in _batch_creation_in_progress')
    guarded_create = source.index('await create_coupon_batch(')
    assert check < add < guarded_create, 'the in-flight guard must be claimed before the batch insert'
    # And released in a finally so a failure can't wedge the admin out forever.
    assert '_batch_creation_in_progress.discard(' in source
    assert 'finally:' in source


def test_admin_states_for_coupon_creation_exist() -> None:
    for name in (
        'creating_coupon_batch_days',
        'creating_coupon_batch_count',
        'creating_coupon_batch_name',
        'creating_coupon_batch_price',
        'creating_coupon_batch_expiry',
    ):
        assert hasattr(AdminStates, name)
