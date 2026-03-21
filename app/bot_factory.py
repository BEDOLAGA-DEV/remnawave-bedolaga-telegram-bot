"""Factory for creating Bot instances with proxy support."""
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import TelegramAPIServer
from aiogram.enums import ParseMode

from app.config import settings


if TYPE_CHECKING:
    from aiogram.client.session.aiohttp import AiohttpSession


def create_bot(
    token: str | None = None, 
    session: AiohttpSession | None = None,
    **kwargs
) -> Bot:
    """Create a Bot instance with SOCKS5 proxy session if PROXY_URL is configured.
    You should provide custom session.
    """
    proxy_url = settings.get_proxy_url()

    if session is None:
        from aiogram.client.session.aiohttp import AiohttpSession

        api_base = (settings.TELEGRAM_BOT_API_BASE_URL or 'https://api.telegram.org').rstrip('/')
        file_base = (settings.TELEGRAM_BOT_FILE_BASE_URL or api_base).rstrip('/')

        # If the API base pattern is already set, use it as is.
        if '{token}' in api_base and '{method}' in api_base:
            base_pattern = api_base
        else:
            base_pattern = f'{api_base}/bot{{token}}/{{method}}'

        if '{token}' in file_base and '{path}' in file_base:
            file_pattern = file_base
        else:
            file_pattern = f'{file_base}/file/bot{{token}}/{{path}}'

        api = TelegramAPIServer(
            base=base_pattern,
            file=file_pattern,
            is_local=settings.DEBUG,
        )
        session = AiohttpSession(api=api, proxy=proxy_url if proxy_url else None)


    kwargs.setdefault('default', DefaultBotProperties(parse_mode=ParseMode.HTML))
    return Bot(token=token or settings.BOT_TOKEN, session=session, **kwargs)
