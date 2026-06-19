"""Central Yandex.Metrika purchase-fire hook in `create_transaction` (PR #2982).

Background
----------
The per-endpoint `store_cid_and_fire_purchase` helper was replaced by a single
central chokepoint in `app.database.crud.transaction`. Every completed
SUBSCRIPTION_PAYMENT — from cabinet, bot handlers, guest purchase, stars,
trial→paid conversion, autopay/recurring, IAP and webhooks — flows through
`create_transaction` (commit=True path) or, for callers that defer their own
commit, through `emit_transaction_side_effects` (commit=False path). The
purchase event must therefore fire **exactly once** per paid purchase and
**never** for deposits, gifts, refunds or not-yet-completed transactions.

These tests pin that contract:
  1. completed SUBSCRIPTION_PAYMENT → fires once with (user_id, abs(amount)),
  2. DEPOSIT / GIFT_PAYMENT / other types → never fires,
  3. is_completed=False → never fires inline,
  4. deferred path (emit_transaction_side_effects) → fires once,
  5. the two paths are mutually exclusive (commit flag), so a single
     transaction never double-fires,
  6. the amount is always the positive abs() value even though
     SUBSCRIPTION_PAYMENT is stored as a negative (debit) amount.

The Yandex service (`spawn_bg` / `fire_purchase_bg`) and every other lazy
side-effect import (`event_emitter`, promo-group assignment, referral contest)
are mocked so no real network or DB I/O happens — assertions are on the mocks.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud import transaction as tx_crud
from app.database.models import TransactionType


def _stub_db() -> SimpleNamespace:
    """Minimal AsyncSession double: records add, satisfies commit/flush/refresh."""
    db = SimpleNamespace()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture
def yandex_spy(monkeypatch):
    """Patch the Yandex service hooks plus the other lazy side-effects.

    Returns the (spawn_bg, fire_purchase_bg) MagicMocks. `fire_purchase_bg` is a
    plain MagicMock (not AsyncMock): in production it returns a coroutine that
    `spawn_bg` schedules; here it returns a MagicMock that `spawn_bg` (also
    mocked) merely records — so nothing is left un-awaited.
    """
    spawn_mock = MagicMock()
    fire_mock = MagicMock()
    monkeypatch.setattr('app.services.yandex_offline_conv_service.spawn_bg', spawn_mock)
    monkeypatch.setattr('app.services.yandex_offline_conv_service.fire_purchase_bg', fire_mock)

    # Neutralise the other lazy-imported side-effects so they don't touch the
    # stub DB / event bus. They're wrapped in try/except in the SUT, but we keep
    # the test focused on the Yandex contract.
    monkeypatch.setattr(
        'app.services.event_emitter.event_emitter.emit',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        'app.services.promo_group_assignment.maybe_assign_promo_group_by_total_spent',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        'app.services.referral_contest_service.referral_contest_service.on_subscription_payment',
        AsyncMock(return_value=None),
    )
    return spawn_mock, fire_mock


# ── create_transaction (commit=True, inline path) ──────────────────────────────


async def test_completed_subscription_payment_fires_once(yandex_spy):
    """Completed SUBSCRIPTION_PAYMENT → fire_purchase_bg(user_id, abs(amount)) once."""
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()

    await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=29900,
        description='Подписка 30 дней',
        is_completed=True,
        commit=True,
    )

    fire_mock.assert_called_once_with(42, 29900)
    spawn_mock.assert_called_once()


async def test_deposit_does_not_fire(yandex_spy):
    """DEPOSIT is a balance top-up, not a purchase → no purchase event."""
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()

    await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.DEPOSIT,
        amount_kopeks=50000,
        description='Пополнение баланса',
        is_completed=True,
        commit=True,
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


async def test_gift_payment_does_not_fire(yandex_spy):
    """GIFT_PAYMENT is not a self-purchase → no purchase event."""
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()

    await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.GIFT_PAYMENT,
        amount_kopeks=29900,
        description='Подарок другу',
        is_completed=True,
        commit=True,
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


async def test_refund_does_not_fire(yandex_spy):
    """REFUND must never count as a purchase conversion."""
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()

    await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.REFUND,
        amount_kopeks=29900,
        description='Возврат',
        is_completed=True,
        commit=True,
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


async def test_not_completed_subscription_payment_does_not_fire_inline(yandex_spy):
    """A pending (is_completed=False) SUBSCRIPTION_PAYMENT must not fire inline."""
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()

    await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=29900,
        description='Подписка (ожидает оплаты)',
        is_completed=False,
        commit=True,
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


async def test_commit_false_does_not_fire_inline(yandex_spy):
    """commit=False defers all side-effects → nothing fires from create_transaction.

    This is the other half of "no double-fire": the inline hook is gated behind
    `commit=True`, so deferred callers don't fire here (they fire later via
    emit_transaction_side_effects).
    """
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()

    await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=29900,
        description='Подписка 30 дней',
        is_completed=True,
        commit=False,
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


async def test_negative_stored_amount_fires_positive_abs(yandex_spy):
    """SUBSCRIPTION_PAYMENT is stored as a negative debit; the conversion event
    must receive the positive abs() amount, not the negative stored value."""
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()

    # create_transaction itself negates positive subscription amounts for storage;
    # the Yandex hook must still report the positive purchase value.
    await tx_crud.create_transaction(
        db,
        user_id=7,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=79900,
        description='Подписка 90 дней',
        is_completed=True,
        commit=True,
    )

    fire_mock.assert_called_once_with(7, 79900)
    assert fire_mock.call_args.args[1] > 0


# ── emit_transaction_side_effects (commit=False deferred path) ─────────────────


async def test_deferred_subscription_payment_fires_once(yandex_spy):
    """emit_transaction_side_effects on a completed SUBSCRIPTION_PAYMENT → fires once."""
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()
    fake_tx = SimpleNamespace(id=123)

    await tx_crud.emit_transaction_side_effects(
        db,
        fake_tx,
        amount_kopeks=29900,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        is_completed=True,
        description='Подписка 30 дней',
    )

    fire_mock.assert_called_once_with(42, 29900)
    spawn_mock.assert_called_once()


async def test_deferred_deposit_does_not_fire(yandex_spy):
    """Deferred DEPOSIT side-effects must not fire a purchase event."""
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()
    fake_tx = SimpleNamespace(id=124)

    await tx_crud.emit_transaction_side_effects(
        db,
        fake_tx,
        amount_kopeks=50000,
        user_id=42,
        type=TransactionType.DEPOSIT,
        is_completed=True,
        description='Пополнение',
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


async def test_deferred_not_completed_does_not_fire(yandex_spy):
    """Deferred SUBSCRIPTION_PAYMENT that isn't completed must not fire."""
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()
    fake_tx = SimpleNamespace(id=125)

    await tx_crud.emit_transaction_side_effects(
        db,
        fake_tx,
        amount_kopeks=29900,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        is_completed=False,
        description='Подписка (ожидает оплаты)',
    )

    fire_mock.assert_not_called()
    spawn_mock.assert_not_called()


async def test_deferred_negative_amount_fires_positive_abs(yandex_spy):
    """Deferred path must also pass the positive abs() amount."""
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()
    fake_tx = SimpleNamespace(id=126)

    # Callers may pass the already-stored negative debit amount.
    await tx_crud.emit_transaction_side_effects(
        db,
        fake_tx,
        amount_kopeks=-29900,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        is_completed=True,
        description='Подписка 30 дней',
    )

    fire_mock.assert_called_once_with(42, 29900)
    assert fire_mock.call_args.args[1] > 0


# ── no double-fire across the two paths for a single transaction ───────────────


async def test_single_transaction_does_not_double_fire(yandex_spy):
    """One purchase = one fire. The inline (commit=True) path and the deferred
    (emit_transaction_side_effects) path are mutually exclusive by design:
    commit=True callers don't call emit_*, and commit=False callers rely on it.

    Simulate a commit=False caller: create_transaction must NOT fire, then the
    explicit emit_transaction_side_effects fires exactly once — total of one.
    """
    spawn_mock, fire_mock = yandex_spy
    db = _stub_db()

    tx = await tx_crud.create_transaction(
        db,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=29900,
        description='Подписка 30 дней',
        is_completed=True,
        commit=False,
    )
    # No inline fire on the deferred path.
    fire_mock.assert_not_called()

    await tx_crud.emit_transaction_side_effects(
        db,
        tx,
        amount_kopeks=29900,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        is_completed=True,
        description='Подписка 30 дней',
    )

    # Exactly one fire total for the single purchase.
    fire_mock.assert_called_once_with(42, 29900)
    spawn_mock.assert_called_once()
