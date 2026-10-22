"""Persistent handoff state, scoped to one business connection and customer chat."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ai_support_bot.app.db import database
from ai_support_bot.app.db.models import BusinessChatControl


AUTO_PAUSE_MINUTES = 30


async def get_chat(connection_id: str, chat_id: int, owner_id: int) -> BusinessChatControl:
    async with database.AsyncSessionLocal() as db:
        insert = sqlite_insert if db.bind.dialect.name == 'sqlite' else pg_insert
        await db.execute(
            insert(BusinessChatControl)
            .values(
                connection_id=connection_id,
                chat_id=chat_id,
                owner_id=owner_id,
                manual_pause=False,
                revision=0,
            )
            .on_conflict_do_nothing(index_elements=['connection_id', 'chat_id'])
        )
        await db.commit()
        return (
            await db.execute(
                select(BusinessChatControl).where(
                    BusinessChatControl.connection_id == connection_id,
                    BusinessChatControl.chat_id == chat_id,
                )
            )
        ).scalar_one()


async def get_by_id(control_id: int) -> BusinessChatControl | None:
    async with database.AsyncSessionLocal() as db:
        return await db.get(BusinessChatControl, control_id)


async def find_owned_chat(owner_id: int, chat_id: int) -> BusinessChatControl | None:
    async with database.AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(BusinessChatControl)
                .where(
                    BusinessChatControl.owner_id == owner_id,
                    BusinessChatControl.chat_id == chat_id,
                )
                .order_by(BusinessChatControl.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


def paused(chat: BusinessChatControl) -> bool:
    until = chat.pause_until
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return chat.manual_pause or (until is not None and until > datetime.now(UTC))


async def active(control_id: int, revision: int) -> bool:
    chat = await get_by_id(control_id)
    return chat is not None and chat.revision == revision and not paused(chat)


async def operator_message(control_id: int, sent_at: datetime) -> None:
    until = sent_at + timedelta(minutes=AUTO_PAUSE_MINUTES)
    if until <= datetime.now(UTC):
        return
    async with database.AsyncSessionLocal() as db:
        await db.execute(
            update(BusinessChatControl)
            .where(
                BusinessChatControl.id == control_id,
                (BusinessChatControl.pause_until.is_(None)) | (BusinessChatControl.pause_until < until),
            )
            .values(pause_until=until, revision=BusinessChatControl.revision + 1)
        )
        await db.commit()


async def set_manual(control_id: int, take: bool) -> None:
    async with database.AsyncSessionLocal() as db:
        await db.execute(
            update(BusinessChatControl)
            .where(BusinessChatControl.id == control_id)
            .values(
                manual_pause=take,
                pause_until=None,
                revision=BusinessChatControl.revision + 1,
            )
        )
        await db.commit()
