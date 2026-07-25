import asyncio
import logging

import structlog

from ai_support_bot.app.bot.bot import run_bot
from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db.database import init_db
from ai_support_bot.app.services import settings_store


structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    processors=[
        structlog.processors.TimeStamper(fmt='iso'),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)

logger = structlog.get_logger(__name__)


async def main() -> None:
    await init_db()
    await settings_store.load()
    logger.info(
        'Starting AI support service',
        model=settings_store.get('MODEL'),
        main_db=settings.main_db_enabled,
        remnawave=settings.remnawave_enabled,
    )

    await run_bot()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info('AI support service stopped')
