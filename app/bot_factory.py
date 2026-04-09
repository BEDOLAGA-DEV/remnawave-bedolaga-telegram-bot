"""Factory for creating Bot instances with proxy support."""

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import settings


def create_bot(token: str | None = None, **kwargs) -> Bot:
    """Create a Bot instance with SOCKS5 proxy session if PROXY_URL is configured."""
    proxy_url = settings.get_proxy_url()
    session = None
    if proxy_url:
        from aiogram.client.session.aiohttp import AiohttpSession

        session = AiohttpSession(proxy=proxy_url)

    kwargs.setdefault('default', DefaultBotProperties(parse_mode=ParseMode.HTML))
    return Bot(token=token or settings.BOT_TOKEN, session=session, **kwargs)
