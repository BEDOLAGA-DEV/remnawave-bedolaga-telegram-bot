import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.remnawave_service import RemnaWaveService


def _create_service() -> RemnaWaveService:
    service = RemnaWaveService.__new__(RemnaWaveService)
    service._panel_timezone = ZoneInfo('UTC')
    service._utc_timezone = ZoneInfo('UTC')
    return service


class _SavepointCM:
    """Async context manager standing in for ``db.begin_nested()`` (a SAVEPOINT).

    Production wraps ``create_user_no_commit`` in ``async with db.begin_nested():``
    so that an IntegrityError rolls back only the nested transaction. The
    SAVEPOINT swallows nothing — any exception raised in the body propagates,
    which is what the IntegrityError test relies on. We record enter/exit so the
    test can assert the savepoint was actually used.
    """

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited += 1
        return False  # never suppress — let IntegrityError reach the handler


def _make_db_with_savepoint() -> tuple[AsyncMock, _SavepointCM]:
    """AsyncMock session whose ``begin_nested()`` yields a real SAVEPOINT CM."""
    db = AsyncMock()
    savepoint = _SavepointCM()
    # begin_nested() is called synchronously as a context-manager factory, so it
    # must return the CM object directly (not a coroutine).
    db.begin_nested = MagicMock(return_value=savepoint)
    return db, savepoint


def _make_panel_user(telegram_id: int, expire_at: str, status: str = 'ACTIVE') -> dict:
    return {
        'telegramId': telegram_id,
        'expireAt': expire_at,
        'status': status,
    }


def test_deduplicate_prefers_latest_expire_date():
    service = _create_service()

    telegram_id = 100
    older = _make_panel_user(telegram_id, datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC).isoformat())
    newer = _make_panel_user(telegram_id, datetime(2025, 2, 1, 0, 0, 0, tzinfo=UTC).isoformat())

    deduplicated = service._deduplicate_panel_users_by_telegram_id([older, newer])

    assert deduplicated[telegram_id] is newer


def test_deduplicate_prefers_active_status_on_same_expire():
    service = _create_service()

    telegram_id = 200
    expire = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC).isoformat()
    disabled = _make_panel_user(telegram_id, expire, status='DISABLED')
    active = _make_panel_user(telegram_id, expire, status='ACTIVE')

    deduplicated = service._deduplicate_panel_users_by_telegram_id([disabled, active])

    assert deduplicated[telegram_id] is active


def test_deduplicate_ignores_records_without_expire_date():
    service = _create_service()

    telegram_id = 300
    missing_expire = _make_panel_user(telegram_id, '')
    valid = _make_panel_user(telegram_id, datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC).isoformat())

    deduplicated = service._deduplicate_panel_users_by_telegram_id([missing_expire, valid])

    assert deduplicated[telegram_id] is valid


async def test_get_or_create_user_handles_unique_violation(monkeypatch):
    service = _create_service()
    # Production now wraps create_user_no_commit in `async with db.begin_nested()`
    # (a SAVEPOINT), so the session mock must expose a working async CM.
    db, savepoint = _make_db_with_savepoint()

    panel_user = {'telegramId': 555, 'username': 'existing'}
    existing_user = object()

    create_user_mock = AsyncMock(side_effect=IntegrityError('stmt', 'params', Exception('unique')))
    get_user_mock = AsyncMock(return_value=existing_user)

    monkeypatch.setattr('app.services.remnawave_service.create_user_no_commit', create_user_mock)
    monkeypatch.setattr(
        'app.services.remnawave_service.get_user_by_telegram_id',
        get_user_mock,
    )

    user, created = await service._get_or_create_bot_user_from_panel(db, panel_user)

    assert user is existing_user
    assert created is False
    create_user_mock.assert_awaited_once()
    get_user_mock.assert_awaited_once_with(db, 555)
    # The SAVEPOINT is opened once and rolled back via __aexit__ on the
    # IntegrityError — no full db.rollback() is issued anymore (that would
    # expire every ORM object in the session).
    db.begin_nested.assert_called_once()
    assert savepoint.entered == 1
    assert savepoint.exited == 1
    db.rollback.assert_not_awaited()


async def test_get_or_create_user_creates_new(monkeypatch):
    service = _create_service()
    # create_user_no_commit now runs inside `async with db.begin_nested()`.
    db, savepoint = _make_db_with_savepoint()

    panel_user = {'telegramId': 777, 'username': 'new_user'}
    new_user = object()

    create_user_mock = AsyncMock(return_value=new_user)

    monkeypatch.setattr('app.services.remnawave_service.create_user_no_commit', create_user_mock)

    user, created = await service._get_or_create_bot_user_from_panel(db, panel_user)

    assert user is new_user
    assert created is True
    create_user_mock.assert_awaited_once_with(
        db=db,
        telegram_id=777,
        username='new_user',
        first_name='User 777',
        last_name=None,
        language='ru',
    )
    # The user is created inside a single SAVEPOINT that commits cleanly.
    db.begin_nested.assert_called_once()
    assert savepoint.entered == 1
    assert savepoint.exited == 1
