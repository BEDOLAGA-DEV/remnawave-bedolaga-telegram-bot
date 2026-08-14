from collections.abc import AsyncGenerator

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db.models import Base


logger = structlog.get_logger(__name__)

engine = create_async_engine(settings.effective_database_url, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

main_engine = None
MainSessionLocal = None
if settings.main_db_enabled:
    main_engine = create_async_engine(settings.effective_main_database_url, pool_pre_ping=True)
    MainSessionLocal = async_sessionmaker(main_engine, class_=AsyncSession, expire_on_commit=False)

pgvector_ready = False
_initialized = False


_CONVERSATION_COLUMNS = (
    ('summary', 'TEXT'),
    ('summarized_message_count', 'INTEGER NOT NULL DEFAULT 0'),
    ('summarized_up_to_id', 'INTEGER NOT NULL DEFAULT 0'),
    ('user_turns_since_summary', 'INTEGER NOT NULL DEFAULT 0'),
    ('summary_updated_at', 'TIMESTAMP'),
)


def is_postgres() -> bool:
    return engine.dialect.name == 'postgresql'


async def _ensure_conversation_columns(conn) -> None:
    for name, definition in _CONVERSATION_COLUMNS:
        try:
            await conn.execute(text(f'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS {name} {definition}'))
        except Exception:
            try:
                await conn.execute(text(f'ALTER TABLE conversations ADD COLUMN {name} {definition}'))
            except Exception:
                pass


async def _ensure_pgvector(conn) -> bool:
    dim = settings.embedding_dim
    await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
    await conn.execute(
        text(f'ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding_vec vector({dim})')
    )
    await conn.execute(
        text(
            'CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding_vec '
            'ON knowledge_chunks USING hnsw (embedding_vec vector_cosine_ops)'
        )
    )
    await conn.execute(
        text(
            'UPDATE knowledge_chunks SET embedding_vec = CAST(embedding::text AS vector) '
            'WHERE embedding_vec IS NULL AND embedding IS NOT NULL '
            f'AND json_array_length(embedding::json) = {dim}'
        )
    )
    return True


async def init_db() -> None:
    global pgvector_ready, _initialized

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_conversation_columns(conn)

    _initialized = True

    if not (is_postgres() and settings.PGVECTOR_ENABLED):
        pgvector_ready = False
        return

    try:
        async with engine.begin() as conn:
            pgvector_ready = await _ensure_pgvector(conn)
        logger.info('pgvector search enabled', dim=settings.embedding_dim)
    except Exception as error:
        pgvector_ready = False
        logger.warning('pgvector unavailable, using in-memory vector search', error=str(error))


async def ensure_ready() -> None:
    """Idempotent init for processes that only touch the AI support DB (web admin API)."""
    if _initialized:
        return
    try:
        await init_db()
    except Exception as error:
        logger.warning('AI support DB init skipped', error=str(error))


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


async def get_main_session() -> AsyncSession | None:
    if MainSessionLocal is None:
        return None
    return MainSessionLocal()
