from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db.models import Base


engine = create_async_engine(settings.effective_database_url, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

main_engine = None
MainSessionLocal = None
if settings.main_db_enabled:
    main_engine = create_async_engine(settings.effective_main_database_url, pool_pre_ping=True)
    MainSessionLocal = async_sessionmaker(main_engine, class_=AsyncSession, expire_on_commit=False)


_CONVERSATION_COLUMNS = (
    ('summary', 'TEXT'),
    ('summarized_message_count', 'INTEGER NOT NULL DEFAULT 0'),
    ('summarized_up_to_id', 'INTEGER NOT NULL DEFAULT 0'),
    ('user_turns_since_summary', 'INTEGER NOT NULL DEFAULT 0'),
    ('summary_updated_at', 'TIMESTAMP'),
)


async def _ensure_conversation_columns(conn) -> None:
    for name, definition in _CONVERSATION_COLUMNS:
        try:
            await conn.execute(text(f'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS {name} {definition}'))
        except Exception:
            try:
                await conn.execute(text(f'ALTER TABLE conversations ADD COLUMN {name} {definition}'))
            except Exception:
                pass


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_conversation_columns(conn)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


async def get_main_session() -> AsyncSession | None:
    if MainSessionLocal is None:
        return None
    return MainSessionLocal()
