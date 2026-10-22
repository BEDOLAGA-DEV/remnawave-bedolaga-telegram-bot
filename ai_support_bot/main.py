import asyncio
import logging

import structlog

from ai_support_bot.app.bot.bot import run_bot
from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db import database
from ai_support_bot.app.db.database import init_db
from ai_support_bot.app.navigation import registry as navigation_registry
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


def _navigation_languages() -> list[str]:
    raw = settings_store.get('NAVIGATION_LANGUAGES') or navigation_registry.DEFAULT_LANGUAGE
    codes = [part.strip().lower() for part in raw.replace(';', ',').split(',') if part.strip()]
    return codes or [navigation_registry.DEFAULT_LANGUAGE]


async def main() -> None:
    settings.assert_secure()

    await init_db()
    await settings_store.load()

    if settings_store.get_bool('NAVIGATION_ENABLED'):
        await navigation_registry.warmup(_navigation_languages())

    logger.info(
        'Starting AI support service',
        model=settings_store.get('MODEL'),
        main_db=settings.main_db_enabled,
        remnawave=settings.remnawave_enabled,
        pgvector=database.pgvector_ready,
        navigation=navigation_registry.stats(),
    )

    await run_bot()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info('AI support service stopped')
