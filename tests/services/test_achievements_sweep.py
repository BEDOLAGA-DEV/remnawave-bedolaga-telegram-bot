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
