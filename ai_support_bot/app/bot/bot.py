import base64
import io

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db.database import AsyncSessionLocal
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.agent import support_agent
from ai_support_bot.app.services.openai_client import OpenAIError


import asyncio
from contextlib import asynccontextmanager

from aiogram.enums import ChatAction

logger = structlog.get_logger(__name__)

_WELCOME = (
    '🤖 <b>ИИ-поддержка</b>\n\n'
    'Опишите ваш вопрос или пришлите скриншот проблемы — я постараюсь помочь '
)


@asynccontextmanager
async def typing_action(bot: Bot, chat_id: int):
    async def loop():
        try:
            while True:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _download_image_data_url(bot: Bot, file_id: str) -> str | None:
    try:
        file = await bot.get_file(file_id)
        if not file.file_path:
            return None
        buffer = await bot.download_file(file.file_path)
        raw = buffer.getvalue() if isinstance(buffer, io.BytesIO) else buffer
        encoded = base64.b64encode(raw).decode('ascii')
        return f'data:image/jpeg;base64,{encoded}'
    except Exception as error:
        logger.warning('Failed to download image', error=str(error))
        return None


async def cmd_start(message: Message) -> None:
    await message.answer(_WELCOME)


async def handle_message(message: Message) -> None:
    bot = message.bot
    telegram_id = message.from_user.id

    question = (message.text or message.caption or '').strip()
    image_url: str | None = None

    if message.photo and settings_store.get_bool('VISION_ENABLED'):
        image_url = await _download_image_data_url(bot, message.photo[-1].file_id)

    if not question and not image_url:
        await message.answer('Пожалуйста, опишите вопрос текстом или пришлите скриншот.')
        return

    try:
        async with typing_action(bot, message.chat.id):
            async with AsyncSessionLocal() as db:
                result = await support_agent.generate_answer(db, telegram_id, question, image_url=image_url)
    except OpenAIError as error:
        logger.error('Generation failed', error=str(error), telegram_id=telegram_id)
        await message.answer('⚠️ Не удалось получить ответ. Попробуйте позже.')
        return
    except Exception as error:
        logger.error('Unexpected error', error=str(error), telegram_id=telegram_id)
        await message.answer('⚠️ Произошла ошибка. Попробуйте позже.')
        return

    answer = result['answer'] or 'Не удалось сформировать ответ.'

    if result['escalate']:
        logger.info('Escalating question to admins', telegram_id=telegram_id)
        user_name = message.from_user.full_name or 'Пользователь'
        username_str = f" (@{message.from_user.username})" if message.from_user.username else ""
        notify_text = (
            "⚠️ <b>Внимание: Обращение требует внимания оператора!</b>\n\n"
            f"<b>Пользователь:</b> <a href='tg://user?id={telegram_id}'>{user_name}</a>{username_str} (ID: <code>{telegram_id}</code>)\n"
            f"<b>Вопрос:</b> {question or '[изображение]'}\n\n"
            f"<b>Сформированный проект ответа ИИ:</b>\n{answer}"
        )
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, notify_text, parse_mode='HTML')
            except Exception as err:
                logger.warning('Failed to notify admin on escalation', admin_id=admin_id, error=str(err))
        return

    try:
        await message.answer(answer, parse_mode='HTML')
    except Exception:
        await message.answer(answer.replace('<', '&lt;').replace('>', '&gt;'))


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.register(cmd_start, Command('start'))
    dp.message.register(handle_message, F.text | F.photo | F.caption)
    return dp


def build_bot() -> Bot:
    return Bot(token=settings.effective_bot_token, default=DefaultBotProperties(parse_mode='HTML'))


async def run_bot() -> None:
    bot = build_bot()
    dp = build_dispatcher()
    logger.info('AI support bot started')
    await dp.start_polling(bot)
