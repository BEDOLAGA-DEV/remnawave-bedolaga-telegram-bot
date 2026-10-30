"""Ранние выходы автовыдачи промогруппы не должны оставлять FOR UPDATE на строке пользователя.

Вызывается после коммита зачисления в сессии вебхука; висящая блокировка заставляла
чужие сессии (админ-уведомление) ждать, пока вебхук закончится.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import promo_group_assignment


class _Session:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.user = SimpleNamespace(id=1, auto_promo_group_threshold_kopeks=0, get_primary_promo_group=lambda: None)

    async def get(self, model, user_id):
        return self.user

    async def commit(self) -> None:
        self.events.append('commit')

    async def rollback(self) -> None:  # pragma: no cover - не должен вызываться
        self.events.append('rollback')


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> _Session:
    db = _Session()

    async def fake_lock(session, user):
        db.events.append('lock')
        return user

    monkeypatch.setattr(promo_group_assignment, 'lock_user_for_update', fake_lock)
    return db


@pytest.mark.anyio('asyncio')
async def test_lock_released_when_nothing_spent(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    monkeypatch.setattr(promo_group_assignment, 'get_user_total_spent_kopeks', AsyncMock(return_value=0))

    result = await promo_group_assignment.maybe_assign_promo_group_by_total_spent(session, 1)

    assert result is None
    assert session.events == ['lock', 'commit']


@pytest.mark.anyio('asyncio')
async def test_lock_released_when_no_group_matches(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    monkeypatch.setattr(promo_group_assignment, 'get_user_total_spent_kopeks', AsyncMock(return_value=1000))
    monkeypatch.setattr(promo_group_assignment, '_get_best_group_for_spending', AsyncMock(return_value=None))

    result = await promo_group_assignment.maybe_assign_promo_group_by_total_spent(session, 1)

    assert result is None
    assert session.events == ['lock', 'commit']


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'
