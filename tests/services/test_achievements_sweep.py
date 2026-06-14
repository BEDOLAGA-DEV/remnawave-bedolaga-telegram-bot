import pytest

from app.database.crud.achievement import get_achievement_sweep_user_ids


class _FakeResult:
    def __init__(self, ids):
        self._ids = ids

    def scalars(self):
        return self

    def all(self):
        return self._ids


class _FakeDB:
    """Returns canned id lists for sequential execute() calls."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def execute(self, *args, **kwargs):
        res = _FakeResult(self._results[self.calls])
        self.calls += 1
        return res


@pytest.mark.asyncio
async def test_sweep_user_ids_unions_and_dedupes():
    db = _FakeDB([[1, 2, 3], [3, 4]])  # active-sub ids, then recent-active ids
    ids = await get_achievement_sweep_user_ids(db, active_days=7)
    assert sorted(ids) == [1, 2, 3, 4]
    assert db.calls == 2  # one query per source


from unittest.mock import AsyncMock  # noqa: E402

import app.database.crud.achievement as ach_crud  # noqa: E402
from app.services.monitoring_service import MonitoringService  # noqa: E402


@pytest.mark.asyncio
async def test_sweep_skipped_when_disabled(monkeypatch):
    import app.services.monitoring_service as ms

    monkeypatch.setattr(ms.settings, 'ACHIEVEMENTS_ENABLED', True, raising=False)
    monkeypatch.setattr(ms.settings, 'ACHIEVEMENTS_AUTO_CHECK_ENABLED', False, raising=False)
    called = AsyncMock()
    monkeypatch.setattr(ach_crud, 'check_and_unlock_all', called)

    svc = MonitoringService(bot=AsyncMock())
    await svc._check_achievements(db=None)

    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_dispatches_per_candidate(monkeypatch):
    import app.services.monitoring_service as ms

    monkeypatch.setattr(ms.settings, 'ACHIEVEMENTS_ENABLED', True, raising=False)
    monkeypatch.setattr(ms.settings, 'ACHIEVEMENTS_AUTO_CHECK_ENABLED', True, raising=False)

    async def fake_ids(db, active_days):
        return [101, 202, 303]

    monkeypatch.setattr(ach_crud, 'get_achievement_sweep_user_ids', fake_ids)
    unlock = AsyncMock(return_value=[])
    monkeypatch.setattr(ach_crud, 'check_and_unlock_all', unlock)

    class _DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

    monkeypatch.setattr(ms, 'AsyncSessionLocal', lambda: _DummySession())

    svc = MonitoringService(bot=AsyncMock())
    await svc._check_achievements(db=None)

    assert unlock.await_count == 3
    assert {c.args[1] for c in unlock.await_args_list} == {101, 202, 303}


def test_achievement_unlock_notification_is_silent():
    # Badge unlocks are low-urgency; the background sweep must not buzz users.
    import inspect

    src = inspect.getsource(ach_crud.check_and_unlock_all)
    assert 'disable_notification=True' in src, 'achievement unlock notification must be silent'
