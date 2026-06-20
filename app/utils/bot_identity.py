"""Token-authoritative bot identity helpers."""

from __future__ import annotations

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


async def sync_bot_username(bot) -> None:
    """Sync settings.BOT_USERNAME from the live bot (token-authoritative).

    Telegram derives the username from BOT_TOKEN, so get_me() is the source of truth. A
    manually-set BOT_USERNAME env survives a token/bot swap and goes stale — which made
    gift-claim links keep pointing at the OLD bot (Telegram bug #650370) while referral
    links (which call get_me() live) were already correct. The bot and the cabinet run in
    one process sharing this settings instance, so syncing here fixes every
    get_bot_username() caller. Best-effort: a network blip keeps the configured value.
    """
    try:
        me = await bot.get_me()
    except Exception as e:
        logger.warning('Не удалось получить username бота через get_me(); используем BOT_USERNAME из конфига', error=e)
        return
    if me.username and me.username != settings.BOT_USERNAME:
        logger.info('Синхронизирован BOT_USERNAME из get_me()', old=settings.BOT_USERNAME, new=me.username)
        settings.BOT_USERNAME = me.username
