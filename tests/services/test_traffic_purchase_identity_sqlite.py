from datetime import timedelta

from sqlalchemy import delete

from app.database.models import TrafficPurchase
from app.services.traffic_accounting import Allowance, TrafficAccount
from app.services.traffic_accounting_service import grant_purchase_locked, lock_accounting_subscription
from app.services.traffic_accounting_store import initialize_account, load_account
from tests.services.test_traffic_accounting_service import END, NOW, snapshot
from tests.services.test_traffic_accounting_store import database


async def test_expired_deleted_purchase_id_is_not_reused_by_new_paid_grant(monkeypatch):
    async with database(monkeypatch) as db:
        old = TrafficPurchase(subscription_id=1, traffic_gb=1, expires_at=NOW - timedelta(days=1))
        db.add(old)
        await db.flush()
        old_id = old.id
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                '123@2025-11-16T00:00:00+00:00', 100, 30, (Allowance(f'purchase:{old_id}', 0, old.expires_at, 1024**3),)
            ),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        await db.execute(delete(TrafficPurchase).where(TrafficPurchase.id == old_id))
        await db.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        new = TrafficPurchase(subscription_id=1, traffic_gb=1, expires_at=END)
        db.add(new)
        await db.flush()
        assert new.id > old_id
        await grant_purchase_locked(db, sub, new, snapshot(), now=NOW)
        account, _ = await load_account(db, 1)
        assert account.remaining_bytes(NOW) == 1024**3
        assert len(account.allowances) == 2


async def test_deleted_transaction_id_cannot_deduplicate_a_new_paid_intent(monkeypatch):
    from sqlalchemy import select

    from app.database.models import Subscription, TrafficBaseGrantIntent, Transaction
    from app.services.traffic_base_grant_service import stage_paid_base_grant

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, ()),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.traffic_limit_gb = 100
        old = Transaction(user_id=1, type='subscription_payment', amount_kopeks=-100)
        db.add(old)
        await db.flush()
        old_id = old.id
        first = await stage_paid_base_grant(db, sub, operation_key=f'admin:buy:{old_id}')
        first.state = 'applied'
        await db.commit()
        await db.execute(delete(Transaction).where(Transaction.id == old_id))
        await db.commit()
        new = Transaction(user_id=1, type='subscription_payment', amount_kopeks=-100)
        db.add(new)
        await db.flush()
        assert new.id > old_id
        second = await stage_paid_base_grant(db, sub, operation_key=f'admin:buy:{new.id}')
        await db.commit()
        assert second.id != first.id
        assert second.state == 'pending'
        assert len(list(await db.scalars(select(TrafficBaseGrantIntent)))) == 2
