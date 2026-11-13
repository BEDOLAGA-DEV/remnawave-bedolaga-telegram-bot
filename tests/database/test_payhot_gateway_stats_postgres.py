"""The shared payment dashboard counts production-only HOT Pay receipts."""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud.payment_gateway_stats import get_gateway_success_rates
from app.database.models import AnorePayment, Base, PayHotPayment
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


async def test_payhot_dashboard_counts_paid_and_pending_without_sandbox_column(postgres_database):
    now = datetime.now(UTC)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        db.add_all(
            [
                PayHotPayment(order_id='stats-paid', amount_kopeks=10000, is_paid=True, status='succeeded'),
                PayHotPayment(order_id='stats-pending', amount_kopeks=10000, is_paid=False, status='processing'),
                PayHotPayment(
                    order_id='stats-old',
                    amount_kopeks=10000,
                    is_paid=True,
                    status='succeeded',
                    created_at=now - timedelta(days=3),
                ),
                AnorePayment(order_id='stats-anore-test', amount_kopeks=10000, is_paid=True, is_test=True),
                AnorePayment(order_id='stats-anore-live', amount_kopeks=10000, is_paid=True, is_test=False),
            ]
        )
        await db.commit()
        rows = await get_gateway_success_rates(db, now - timedelta(days=1), now + timedelta(days=1))
        assert rows == [
            {'method': 'payhot', 'total': 2, 'paid': 1, 'success_rate': 50.0},
            {'method': 'anore', 'total': 1, 'paid': 1, 'success_rate': 100.0},
        ]
