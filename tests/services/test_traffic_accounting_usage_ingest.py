from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from app.database.models import Subscription
from app.services.subscription_service import SubscriptionService
from app.services.traffic_accounting import Allowance, TrafficAccount
from app.services.traffic_accounting_store import initialize_account, load_account
from tests.services.test_traffic_accounting_service import PanelDouble
from tests.services.test_traffic_accounting_store import database


async def test_sync_usage_settles_ledger_without_importing_physical_used(monkeypatch):
    now = datetime.now(UTC)
    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.traffic_limit_gb = 40
        sub.remnawave_id = 123
        sub.traffic_accounting_mode = 'managed'
        panel = PanelDouble()
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                '123@2025-11-16T00:00:00+00:00',
                100,
                30,
                (Allowance('purchase:1', 20 * 1024**3, now + timedelta(days=2)),),
            ),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=now,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        panel.user.user_traffic.lifetime_used_traffic_bytes = 100 + 5 * 1024**3
        panel.user.user_traffic.used_traffic_bytes = 30 + 5 * 1024**3
        await db.commit()

        @asynccontextmanager
        async def client(self):
            yield panel

        monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
        monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *a: None)
        assert await SubscriptionService().sync_subscription_usage(db, sub)
        account, _ = await load_account(db, 1)
        assert account.remaining_bytes(now) == 15 * 1024**3
        assert sub.traffic_used_gb == 25
