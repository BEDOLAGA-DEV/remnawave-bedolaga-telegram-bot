"""set_bot_role must not overwrite the original creator on update."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.database.crud.bot_role import BotRoleCRUD


def _db_returning(existing):
    """A fake AsyncSession whose execute().scalar_one_or_none() returns `existing`."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


async def test_update_preserves_original_created_by():
    existing = SimpleNamespace(user_id=5, permissions=['support'], created_by=999)
    db = _db_returning(existing)

    await BotRoleCRUD.set_bot_role(db, 5, ['users', 'payments'], created_by=222)

    assert existing.permissions == ['users', 'payments']
    assert existing.created_by == 999  # NOT overwritten by editor 222


async def test_create_sets_created_by():
    db = _db_returning(None)  # no existing row

    await BotRoleCRUD.set_bot_role(db, 7, ['support'], created_by=222)

    db.add.assert_called_once()
    created = db.add.call_args.args[0]
    assert created.created_by == 222
    assert created.permissions == ['support']
