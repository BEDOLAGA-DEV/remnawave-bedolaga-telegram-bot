import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import settings
import app.handlers.subscription.common as common
import app.handlers.subscription.protocols as protocols


@pytest.fixture(autouse=True)
def _enable_protocols(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_protocols_enabled', lambda self: True, raising=False)


def _cb(data):
    msg = SimpleNamespace(
        edit_text=AsyncMock(), edit_reply_markup=AsyncMock(), answer=AsyncMock()
    )
    return SimpleNamespace(data=data, message=msg, answer=AsyncMock(), bot=None)


def _user():
    return SimpleNamespace(id=1, language='ru', promo_group_id=None)


class _State:
    def __init__(self, data):
        self._d = dict(data)

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)


def test_validate_protocol_selection():
    assert protocols.validate_protocol_selection(['a']) is True
    assert protocols.validate_protocol_selection([]) is False
    assert protocols.validate_protocol_selection(['', None]) is False


def _patch_available(monkeypatch, squads):
    async def fake_avail(db, promo_group_id=None):
        return squads

    monkeypatch.setattr(
        'app.database.crud.server_squad.get_available_server_squads', fake_avail
    )


@pytest.mark.asyncio
async def test_apply_writes_and_pushes(monkeypatch):
    sub = SimpleNamespace(id=5, user_id=1, connected_squads=['a'], updated_at=None)

    async def fake_resolve(cb, u, db, state=None):
        return sub, 5

    monkeypatch.setattr(common, 'resolve_subscription_from_context', fake_resolve)
    _patch_available(
        monkeypatch,
        [
            SimpleNamespace(squad_uuid='a', display_name='Main'),
            SimpleNamespace(squad_uuid='b', display_name='Extra'),
        ],
    )

    push = AsyncMock()
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService.update_remnawave_user', push
    )

    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    state = _State({'protocols': ['b']})
    cb = _cb('nz!_protocols_apply')

    await protocols.apply_protocols_changes(cb, _user(), db, state)

    assert sub.connected_squads == ['b']
    db.commit.assert_awaited_once()
    push.assert_awaited()


@pytest.mark.asyncio
async def test_apply_blocks_empty_selection(monkeypatch):
    sub = SimpleNamespace(id=5, user_id=1, connected_squads=['a'], updated_at=None)

    async def fake_resolve(cb, u, db, state=None):
        return sub, 5

    monkeypatch.setattr(common, 'resolve_subscription_from_context', fake_resolve)
    _patch_available(monkeypatch, [SimpleNamespace(squad_uuid='a', display_name='Main')])

    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    state = _State({'protocols': []})
    cb = _cb('nz!_protocols_apply')

    await protocols.apply_protocols_changes(cb, _user(), db, state)

    cb.answer.assert_awaited()
    db.commit.assert_not_awaited()
    assert sub.connected_squads == ['a']


@pytest.mark.asyncio
async def test_apply_enqueues_retry_when_push_fails(monkeypatch):
    sub = SimpleNamespace(id=5, user_id=1, connected_squads=['a'], updated_at=None)

    async def fake_resolve(cb, u, db, state=None):
        return sub, 5

    monkeypatch.setattr(common, 'resolve_subscription_from_context', fake_resolve)
    _patch_available(
        monkeypatch,
        [
            SimpleNamespace(squad_uuid='a', display_name='Main'),
            SimpleNamespace(squad_uuid='b', display_name='Extra'),
        ],
    )

    async def fake_push(self, db, subscription, *, sync_squads=False):
        return None  # simulate swallowed failure

    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService.update_remnawave_user', fake_push
    )

    enqueue_calls = []
    monkeypatch.setattr(
        'app.services.remnawave_retry_queue.remnawave_retry_queue.enqueue',
        lambda **kw: enqueue_calls.append(kw),
    )

    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    state = _State({'protocols': ['b']})
    cb = _cb('nz!_protocols_apply')

    await protocols.apply_protocols_changes(cb, _user(), db, state)

    assert sub.connected_squads == ['b']
    assert len(enqueue_calls) == 1
    assert enqueue_calls[0]['subscription_id'] == 5
