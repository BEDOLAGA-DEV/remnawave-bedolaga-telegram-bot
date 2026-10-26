"""Anore operational paths must preserve payment identity and sandbox isolation."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from app.database.models import AnorePayment, PaymentMethod
from app.services import payment_search_service as search, payment_verification_service as verification
from app.services.guest_purchase_service import _find_succeeded_provider_payment


def _sql(statement):
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))


def _db_result(rows):
    result = Mock()
    result.scalars.return_value.all.return_value = rows
    result.scalars.return_value.first.return_value = rows[0] if rows else None
    return SimpleNamespace(execute=AsyncMock(return_value=result))


@pytest.mark.asyncio
async def test_guest_recovery_requires_live_payment_and_matching_purchase():
    db = _db_result([])
    assert await _find_succeeded_provider_payment(db, 'anore', 'masked-token') is None
    query = _sql(db.execute.call_args.args[0])
    assert 'anore_payments.is_test IS false' in query
    assert 'anore_payments.is_paid IS true' in query
    assert 'anore_payments.anore_payment_id IS NOT NULL' in query
    assert 'purchase_token' in query and 'masked-token' in query


@pytest.mark.asyncio
async def test_guest_recovery_preserves_canonical_order_id_and_exact_kopeks():
    db = _db_result([SimpleNamespace(order_id='masked-order', anore_payment_id='masked-invoice', amount_kopeks=12345)])
    assert await _find_succeeded_provider_payment(db, 'anore', 'masked-token') == ('masked-order', 12345)


@pytest.mark.parametrize(
    ('status', 'is_paid', 'is_test', 'expected'),
    [
        ('new', False, False, True),
        ('expired', False, False, False),
        ('paid', True, False, False),
        ('new', False, True, False),
    ],
)
def test_auto_check_skips_terminal_and_sandbox(status, is_paid, is_test, expected):
    assert verification._is_anore_pending(SimpleNamespace(status=status, is_paid=is_paid, is_test=is_test)) is expected


def test_disabled_provider_still_checks_existing_payments(monkeypatch):
    monkeypatch.setattr(type(verification.settings), 'is_anore_configured', lambda self: True)
    monkeypatch.setattr(type(verification.settings), 'is_anore_enabled', lambda self: False)
    assert PaymentMethod.ANORE in verification.get_enabled_auto_methods()


@pytest.mark.asyncio
async def test_manual_check_uses_local_order_identity(monkeypatch):
    payment = SimpleNamespace(order_id='masked-order')
    db = SimpleNamespace(get=AsyncMock(return_value=payment))
    service = SimpleNamespace(check_anore_payment_status=AsyncMock(return_value={'payment': payment}))
    record = object()
    monkeypatch.setattr(verification, 'get_payment_record', AsyncMock(return_value=record))
    assert await verification.run_manual_check(db, PaymentMethod.ANORE, 17, service) is record
    db.get.assert_awaited_once_with(AnorePayment, 17)
    service.check_anore_payment_status.assert_awaited_once_with(db, 'masked-order')


@pytest.mark.asyncio
async def test_search_uses_both_order_and_provider_ids():
    db = _db_result([])
    params = search.SearchParams(search='masked-invoice', date_from=datetime.now(UTC) - timedelta(days=1))
    await search._search_anore(db, params)
    query = _sql(db.execute.call_args.args[0])
    assert 'anore_payments.order_id ILIKE' in query
    assert 'anore_payments.anore_payment_id ILIKE' in query
    assert 'masked-invoice' in query


def test_admin_surfaces_label_sandbox_without_claiming_real_credit():
    from app.cabinet.routes.admin_payments import _get_status_info
    from app.handlers.admin.payments import _status_info

    record = SimpleNamespace(
        method=PaymentMethod.ANORE, status='paid', is_paid=True, payment=SimpleNamespace(is_test=True)
    )
    for label in (
        _get_status_info(record),
        _status_info(record, texts=SimpleNamespace(t=lambda key, fallback: fallback)),
    ):
        assert label[0] == '🧪'
        assert 'баланс не пополнен' in label[1]
