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


logger = structlog.get_logger(__name__)

_WELCOME = (
    '🤖 <b>ИИ-поддержка</b>\n\n'
    'Опишите ваш вопрос или пришлите скриншот проблемы — я постараюсь помочь '
    'на основе данных вашего аккаунта и опыта поддержки.'
)


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

    thinking = await message.answer('🤖 Думаю над ответом...')

    try:
        async with AsyncSessionLocal() as db:
            result = await support_agent.generate_answer(db, telegram_id, question, image_url=image_url)
    except OpenAIError as error:
        logger.error('Generation failed', error=str(error), telegram_id=telegram_id)
        await thinking.edit_text('⚠️ Не удалось получить ответ. Попробуйте позже.')
        return
    except Exception as error:
        logger.error('Unexpected error', error=str(error), telegram_id=telegram_id)
        await thinking.edit_text('⚠️ Произошла ошибка. Попробуйте позже.')
        return

    answer = result['answer'] or 'Не удалось сформировать ответ.'
    if result['escalate']:
        answer += '\n\nℹ️ Похоже, вопрос требует внимания оператора. Обратитесь в основную поддержку.'

    try:
        await thinking.edit_text(answer)
    except Exception:
        await thinking.edit_text(answer.replace('<', '&lt;').replace('>', '&gt;'))


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
