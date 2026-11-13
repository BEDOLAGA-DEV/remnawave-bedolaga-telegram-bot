"""Real PostgreSQL eligibility for durable post-payment fulfillment."""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import Base, PayHotPayment
from app.services.payment_verification_service import _fetch_payhot_fulfillment_recovery
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


async def test_old_paid_guest_recovery_excludes_finished_attention_unpaid(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        for name, paid, state in [
            ('old-guest', True, {'state': 'pending'}),
            ('missing-state', True, None),
            ('done', True, {'state': 'done'}),
            ('attention', True, {'state': 'attention'}),
            ('unpaid', False, {'state': 'pending'}),
        ]:
            db.add(
                PayHotPayment(
                    order_id=name,
                    amount_kopeks=10000,
                    currency='RUB',
                    is_paid=paid,
                    fulfillment_state=state,
                    created_at=datetime.now(UTC) - timedelta(days=3),
                )
            )
        await db.commit()
        selected = await _fetch_payhot_fulfillment_recovery(db)
        assert {record.identifier for record in selected} == {'old-guest', 'missing-state'}
        assert all(record.is_paid and record.user is None and not record.is_recent() for record in selected)


async def test_recurring_recovery_keeps_only_unfinished_delivery(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        for state in ('pending', 'completed', 'review'):
            db.add(
                PayHotPayment(
                    order_id=f'recurring-{state}',
                    amount_kopeks=10000,
                    currency='RUB',
                    is_paid=True,
                    metadata_json={'purpose': 'payhot_recurring'},
                    fulfillment_state={'kind': 'recurring', 'state': state},
                )
            )
        await db.commit()
        assert {row.identifier for row in await _fetch_payhot_fulfillment_recovery(db)} == {'recurring-pending'}
