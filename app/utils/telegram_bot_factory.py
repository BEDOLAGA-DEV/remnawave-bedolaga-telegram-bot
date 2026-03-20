from __future__ import annotations

from typing import Any, Optional

from aiogram import Bot as AiogramBot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import TelegramAPIServer

from app.config import settings


def Bot(  # noqa: N802 - хотим сохранить имя для минимальных правок в проекте
    token: str,
    session: Optional[Any] = None,
    default: Optional[Any] = None,
    **kwargs: Any,
) -> AiogramBot:
    """
    aiogram Bot с кастомным Telegram Bot API URL (под прокси).

    Важно: в aiogram v3 Bot по умолчанию использует TelegramAPIServer(PRODUCTION),
    поэтому нужно явно задавать `session` с TelegramAPIServer.
    """

    if session is None:
        api_base = (getattr(settings, 'TELEGRAM_BOT_API_BASE_URL', None) or 'https://api.telegram.org').rstrip('/')
        file_base = (getattr(settings, 'TELEGRAM_BOT_FILE_BASE_URL', None) or api_base).rstrip('/')

        # Если в конфиге уже указан полный паттерн TelegramAPIServer — используем его как есть.
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
            is_local=False,
        )
        session = AiohttpSession(api=api)

    return AiogramBot(token=token, session=session, default=default, **kwargs)

