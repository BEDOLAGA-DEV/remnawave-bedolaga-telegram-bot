import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import Subscription
from app.services.traffic_accounting import Allowance, TrafficAccount
from app.services.traffic_accounting_store import initialize_account, load_account
from tests.services.test_traffic_accounting_service import END, NOW, snapshot
from tests.services.test_traffic_accounting_store import database


class ReplacementPanel:
    def __init__(self):
        self.created = None
        self.creates = 0
        self.lose_response = False

    async def get_user_by_id(self, panel_id):
        return self.created if self.created is not None and self.created.id == panel_id else None

    async def get_user_by_username(self, username):
        return self.created if self.created is not None and self.created.username == username else None

    async def create_user(self, **kwargs):
        self.creates += 1
        assert kwargs['status'].value == 'DISABLED'
        assert kwargs['traffic_limit_bytes'] == 70
        self.created = snapshot(used=0, lifetime=0, strategy='NO_RESET')
        self.created.id = 456
        self.created.created_at = NOW
        self.created.username = kwargs['username']
        self.created.status = 'DISABLED'
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError('lost create response')
        return self.created


@pytest.mark.parametrize('initial_phase', ['ready', 'suspended'])
async def test_recreation_lost_response_reuses_intent_and_never_regrants(monkeypatch, initial_phase):
    from app.services.traffic_accounting_recreation import ensure_account_recreated

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                '123@2025-11-16T00:00:00+00:00',
                100,
                30,
                (Allowance('purchase:1', 70, END, 100), Allowance('purchase:old', 0, END, 50)),
            ),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = initial_phase
        state.recreation_previous_phase = 'ready'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.remnawave_id = 123
        await db.commit()
        maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        panel = ReplacementPanel()
        panel.lose_response = True
        with pytest.raises(TimeoutError):
            await ensure_account_recreated(maker, panel, 1, now=NOW, allow_suspended=True)
        await ensure_account_recreated(maker, panel, 1, now=NOW, allow_suspended=True)
        await db.rollback()
        account, state = await load_account(db, 1)
        assert state.phase == 'ready'
        assert account.panel_identity.startswith('456@')
        assert account.remaining_bytes(NOW) == 70
        assert len(account.allowances) == 2
        assert panel.creates == 1
        assert (await db.get(Subscription, 1, populate_existing=True)).remnawave_id == 456


async def test_stale_worker_cannot_recreate_intentionally_deleted_account(monkeypatch):
    from app.services.traffic_accounting import AccountingConflict
    from app.services.traffic_accounting_recreation import ensure_account_recreated
    from tests.services.test_traffic_accounting_lifecycle import seed

    async with database(monkeypatch) as db:
        await seed(db, remaining=70)
        _, state = await load_account(db, 1)
        state.phase = 'suspended'
        state.recreation_previous_phase = 'ready'
        await db.commit()
        panel = ReplacementPanel()
        maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        with pytest.raises(AccountingConflict, match='suspended'):
            await ensure_account_recreated(maker, panel, 1, now=NOW)
        assert panel.creates == 0
