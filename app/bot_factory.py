"""Factory for creating Bot instances with proxy and custom API server support."""

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import settings


def create_bot(token: str | None = None, **kwargs) -> Bot:
    """Create a Bot instance with SOCKS5 proxy and/or custom Telegram API server."""
    proxy_url = settings.get_proxy_url()
    telegram_api_url = settings.get_telegram_api_url()
    session = None
    if proxy_url or telegram_api_url:
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer

        session_kwargs: dict = {}
        if proxy_url:
            session_kwargs['proxy'] = proxy_url
        if telegram_api_url:
            session_kwargs['api'] = TelegramAPIServer.from_base(telegram_api_url)

        session = AiohttpSession(**session_kwargs)

    kwargs.setdefault('default', DefaultBotProperties(parse_mode=ParseMode.HTML))
    return Bot(token=token or settings.BOT_TOKEN, session=session, **kwargs)


# Patch InlineKeyboardButton to redirect payment handlers back to the payment methods screen
from aiogram.types import InlineKeyboardButton
import sys

_orig_init = InlineKeyboardButton.__init__

def _patched_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    if getattr(self, 'callback_data', None) in ('menu_balance', 'back_to_menu'):
        try:
            frame = sys._getframe(1)
            for _ in range(4):
                if not frame:
                    break
                filename = frame.f_code.co_filename
                co_name = frame.f_code.co_name
                if 'handlers/balance' in filename and not filename.endswith('main.py'):
                    is_prompt = (
                        co_name.startswith('start_') or
                        '_topup' in co_name or
                        '_prompt' in co_name or
                        'process_' in co_name or
                        co_name.endswith('_payment')
                    ) and not co_name.startswith('_create') and co_name != '_check_topup_restriction'
                    if is_prompt:
                        self.callback_data = 'balance_topup'
                        break
                frame = frame.f_back
        except Exception:
            pass

InlineKeyboardButton.__init__ = _patched_init
