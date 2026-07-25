from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ai_support_bot.app.db.models import Conversation, KnowledgeChunk, KnowledgeSource, Message


async def get_source_by_hash(db: AsyncSession, content_hash: str) -> KnowledgeSource | None:
    result = await db.execute(select(KnowledgeSource).where(KnowledgeSource.content_hash == content_hash))
    return result.scalar_one_or_none()


async def list_sources(db: AsyncSession) -> list[KnowledgeSource]:
    result = await db.execute(select(KnowledgeSource).order_by(KnowledgeSource.created_at.desc()))
    return list(result.scalars().all())


async def get_source(db: AsyncSession, source_id: int) -> KnowledgeSource | None:
    return await db.get(KnowledgeSource, source_id)


async def create_source(db: AsyncSession, filename: str, content_hash: str, title: str | None) -> KnowledgeSource:
    source = KnowledgeSource(filename=filename, content_hash=content_hash, title=title)
    db.add(source)
    await db.flush()
    return source


async def delete_source(db: AsyncSession, source_id: int) -> bool:
    source = await db.get(KnowledgeSource, source_id)
    if not source:
        return False
    await db.delete(source)
    await db.commit()
    return True


async def set_source_active(db: AsyncSession, source_id: int, is_active: bool) -> bool:
    source = await db.get(KnowledgeSource, source_id)
    if not source:
        return False
    source.is_active = is_active
    await db.commit()
    return True


async def get_existing_chunk_hashes(db: AsyncSession) -> set[str]:
    result = await db.execute(select(KnowledgeChunk.chunk_hash))
    return {row[0] for row in result.all()}


async def add_chunks(db: AsyncSession, chunks: list[KnowledgeChunk]) -> int:
    if not chunks:
        return 0
    db.add_all(chunks)
    await db.flush()
    return len(chunks)


async def update_source_counts(db: AsyncSession, source_id: int, chunk_count: int, message_count: int) -> None:
    source = await db.get(KnowledgeSource, source_id)
    if not source:
        return
    source.chunk_count = chunk_count
    source.message_count = message_count


async def get_active_chunks(db: AsyncSession) -> list[KnowledgeChunk]:
    result = await db.execute(
        select(KnowledgeChunk)
        .join(KnowledgeSource)
        .where(KnowledgeSource.is_active.is_(True), KnowledgeChunk.embedding.isnot(None))
    )
    return list(result.scalars().all())


async def count_chunks(db: AsyncSession) -> int:
    result = await db.execute(select(func.count(KnowledgeChunk.id)))
    return int(result.scalar() or 0)


async def get_or_create_conversation(db: AsyncSession, telegram_id: int) -> Conversation:
    result = await db.execute(
        select(Conversation)
        .where(Conversation.telegram_id == telegram_id)
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    conversation = result.scalar_one_or_none()
    if conversation:
        return conversation
    conversation = Conversation(telegram_id=telegram_id)
    db.add(conversation)
    await db.flush()
    return conversation


async def add_message(
    db: AsyncSession,
    conversation_id: int,
    telegram_id: int,
    role: str,
    content: str,
    has_media: bool = False,
    media_type: str | None = None,
    media_file_id: str | None = None,
    model: str | None = None,
    tokens_prompt: int | None = None,
    tokens_completion: int | None = None,
    used_context: list | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation_id,
        telegram_id=telegram_id,
        role=role,
        content=content,
        has_media=has_media,
        media_type=media_type,
        media_file_id=media_file_id,
        model=model,
        tokens_prompt=tokens_prompt,
        tokens_completion=tokens_completion,
        used_context=used_context,
    )
    db.add(message)
    await db.flush()
    return message


async def get_conversation_context(db: AsyncSession, conversation_id: int, limit: int) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    messages = list(result.scalars().all())
    messages.reverse()
    return messages


async def prune_messages(db: AsyncSession, keep_last: int) -> int:
    result = await db.execute(
        select(Message.id).order_by(Message.created_at.desc()).offset(keep_last)
    )
    stale_ids = [row[0] for row in result.all()]
    if not stale_ids:
        return 0
    await db.execute(delete(Message).where(Message.id.in_(stale_ids)))
    return len(stale_ids)


async def mark_escalated(db: AsyncSession, conversation_id: int) -> None:
    conversation = await db.get(Conversation, conversation_id)
    if conversation:
        conversation.escalated = True
        conversation.updated_at = datetime.now(timezone.utc)


async def list_recent_messages(db: AsyncSession, limit: int = 100, offset: int = 0) -> list[Message]:
    result = await db.execute(
        select(Message).order_by(Message.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def count_messages(db: AsyncSession) -> int:
    result = await db.execute(select(func.count(Message.id)))
    return int(result.scalar() or 0)
