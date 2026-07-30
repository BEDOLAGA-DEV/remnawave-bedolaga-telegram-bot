"""Настройка нижней кнопки «Меню» Telegram на открытие веб-кабинета (WebApp).

Кнопка меню (setChatMenuButton без chat_id) — глобальная для всех личных чатов,
поэтому выставляется один раз при старте бота. Бот продолжает работать через
обычные сообщения и inline-кнопки; кнопка меню лишь даёт быстрый вход в кабинет.
"""

import structlog
from aiogram import Bot
from aiogram.types import MenuButtonWebApp, WebAppInfo

from app.config import settings


logger = structlog.get_logger(__name__)


def _resolve_menu_button_url() -> str | None:
    """URL веб-кабинета для WebApp-кнопки меню, либо None.

    None означает «не настраивать кнопку»: функция выключена, URL не задан или не
    https (Telegram принимает только https для WebApp).
    """
    if not settings.MENU_BUTTON_WEBAPP_ENABLED:
        return None
    url = (settings.MENU_BUTTON_WEBAPP_URL or '').strip() or (settings.MINIAPP_CUSTOM_URL or '').strip()
    if not url.lower().startswith('https://'):
        return None
    return url


async def configure_chat_menu_button(bot: Bot) -> bool:
    """Выставляет кнопку меню на открытие веб-кабинета. Возвращает True, если выставлена.

    Если опция выключена или URL кабинета не задан/не https — ничего не делает и
    возвращает False (существующую кнопку меню не трогаем). Ошибки Telegram API не
    роняют старт бота.
    """
    url = _resolve_menu_button_url()
    if not url:
        return False

    text = (settings.MENU_BUTTON_WEBAPP_TEXT or 'Кабинет').strip() or 'Кабинет'
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text=text, web_app=WebAppInfo(url=url)))
    except Exception as error:
        logger.error('Не удалось настроить кнопку меню Telegram (WebApp)', error=error)
        return False

    logger.info('Кнопка меню Telegram настроена на веб-кабинет', url=url, text=text)
    return True
