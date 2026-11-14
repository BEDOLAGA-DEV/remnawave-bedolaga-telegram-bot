from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, PayHotAgreement, PayHotChargeAttempt
from app.services.payhot_recurrent_guard import guard_payhot_credentials, has_conflicting_payhot, has_live_payhot
from tests.database.test_payhot_recurrent_journal_postgres import END, payload, seed
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('status', ['UNKNOWN', 'PROCESSING', 'REVIEW', 'REFUND_REVIEW', 'SUCCEEDED'])
async def test_cancelled_mandate_does_not_release_unsettled_money_guard(postgres_database, monkeypatch, status):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        agreement = await db.get(PayHotAgreement, aid)
        agreement.status = 'CANCELLED'
        db.add(
            PayHotChargeAttempt(
                agreement_id=aid,
                cycle_key='cycle-example',
                expected_end_date=END,
                request_payload=payload(),
                idempotency_key='synthetic-idempotency',
                merchant_reference='renew-example',
                amount_kopeks=10000,
                currency='RUB',
                status=status,
            )
        )
        await db.commit()
        assert await has_live_payhot(db, 1)
        assert await has_conflicting_payhot(db, user_id=1, subscription_id=1)
        assert not await has_conflicting_payhot(db, user_id=2, subscription_id=1)
        assert not await has_conflicting_payhot(db, user_id=1, subscription_id=2)
        with pytest.raises(ValueError):
            await guard_payhot_credentials(db, 'PAYHOT_API_KEY', 'new-synthetic-key')
        attempt = await db.scalar(select(PayHotChargeAttempt))
        attempt.status = 'SUCCEEDED'
        attempt.delivery_completed_at = datetime.now(UTC)
        await db.commit()
        assert not await has_live_payhot(db, 1)


async def test_cancelled_without_money_does_not_block(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        agreement = await db.get(PayHotAgreement, aid)
        agreement.status = 'CANCELLED'
        await db.commit()
        assert not await has_live_payhot(db, 1)
        assert not await has_conflicting_payhot(db, user_id=1, subscription_id=1)
