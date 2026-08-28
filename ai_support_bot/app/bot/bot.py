import asyncio
import base64
import io
import random
import re
import time
from contextlib import asynccontextmanager

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import Message

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db import crud
from ai_support_bot.app.db.database import AsyncSessionLocal
from ai_support_bot.app.services import alerting, settings_store
from ai_support_bot.app.services.agent import _is_smalltalk, support_agent
from ai_support_bot.app.services.openai_client import OpenAIError


_GREETING_PATTERNS = [r'привет\w*', r'здравств\w*', r'хай', r'hi', r'hello', r'добр(ое|ый|ой)', r'утро', r'день', r'вечер']
_THANKS_PATTERNS = [r'спасибо\w*', r'благодар\w*', r'спс', r'пасиб\w*', r'сяб', r'мерси']
_ACK_PATTERNS = [r'понял\w*', r'ясно', r'хорошо', r'ок\w*', r'окей', r'ладно', r'отлично', r'супер', r'класс', r'круто', r'ага', r'угу', r'помогло', r'заработало', r'принято']

_GREETING_REPLIES = [
    'Здравствуйте! Чем могу помочь по сервису? 🙂',
    'Приветствую! Опишите ваш вопрос, постараюсь помочь. 🙌',
    'Здравствуйте! Задавайте вопрос, я на связи. 🙂',
]

_THANKS_REPLIES = [
    'Пожалуйста! Рад был помочь. 😊',
    'Всегда пожалуйста! Если возникнут вопросы — обращайтесь. 🤝',
    'Пожалуйста! Рад, что всё получилось. ✨',
]

_ACK_REPLIES = [
    'Отлично! Обращайтесь, если появятся вопросы. 👍',
    'Хорошо! Рад был помочь. 🤝',
    'Понял вас! Всегда на связи, если что-то понадобится. 🙂',
]

_GENERAL_SMALLTALK_REPLIES = [
    'Всё отлично, спасибо! Готов помочь с вопросами по VPN. 🙂',
    'Всё хорошо! Чем могу помочь?',
]


def get_smalltalk_reply(question: str) -> str:
    q = (question or '').lower().strip()

    if any(re.search(p, q, re.IGNORECASE) for p in _THANKS_PATTERNS):
        return random.choice(_THANKS_REPLIES)

    if any(re.search(p, q, re.IGNORECASE) for p in _ACK_PATTERNS):
        return random.choice(_ACK_REPLIES)

    if any(re.search(p, q, re.IGNORECASE) for p in _GREETING_PATTERNS):
        return random.choice(_GREETING_REPLIES)

    return random.choice(_GENERAL_SMALLTALK_REPLIES)


logger = structlog.get_logger(__name__)

_WELCOME = (
    '🤖 <b>ИИ-поддержка</b>\n\n'
    'Я ИИ-ассистент поддержки: отвечаю на основе базы знаний и данных вашего аккаунта. '
    'Опишите вопрос или пришлите скриншот проблемы — если не смогу помочь, передам вопрос оператору.'
)

# (telegram_id, YYYY-MM-DD) — уже отправили пользователю ответ про лимит сегодня
_limit_user_notified: set[tuple[int, str]] = set()

_last_message_at: dict[int, float] = {}
_throttle_warned: set[int] = set()

_processed_updates: dict[int, float] = {}
_PROCESSED_TTL = 300


def _is_duplicate_update(message: Message) -> bool:
    message_key = getattr(message, 'message_id', None)
    if message_key is None:
        return False
    key = (message.chat.id << 24) + message_key
    now = time.time()
    if len(_processed_updates) > 5000:
        for stale in [k for k, ts in _processed_updates.items() if now - ts > _PROCESSED_TTL]:
            _processed_updates.pop(stale, None)
    if key in _processed_updates and now - _processed_updates[key] < _PROCESSED_TTL:
        return True
    _processed_updates[key] = now
    return False


def _throttle_hit(telegram_id: int) -> bool:
    window = settings_store.get_int('THROTTLE_SECONDS')
    if window <= 0:
        return False
    now = time.time()
    last = _last_message_at.get(telegram_id, 0.0)
    if now - last < window:
        return True
    _last_message_at[telegram_id] = now
    _throttle_warned.discard(telegram_id)
    if len(_last_message_at) > 10000:
        for stale in [k for k, ts in _last_message_at.items() if now - ts > 3600]:
            _last_message_at.pop(stale, None)
            _throttle_warned.discard(stale)
    return False


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


def _user_label(message: Message) -> tuple[str, str]:
    user_name = message.from_user.full_name or 'Пользователь'
    username_str = f' (@{message.from_user.username})' if message.from_user.username else ''
    return user_name, username_str


def _admin_recipients(exclude_telegram_id: int | None = None) -> list[int]:
    """Service notifications must never land in the user's own chat."""
    recipients = [admin_id for admin_id in settings.admin_ids if admin_id != exclude_telegram_id]
    if exclude_telegram_id is not None and exclude_telegram_id in settings.admin_ids:
        logger.warning(
            'Admin notification suppressed for own chat: user is listed in AISUP_ADMIN_IDS',
            telegram_id=exclude_telegram_id,
        )
    return recipients


async def _notify_admins(bot: Bot, text: str, exclude_telegram_id: int | None = None) -> None:
    for admin_id in _admin_recipients(exclude_telegram_id):
        try:
            await bot.send_message(admin_id, text, parse_mode='HTML')
        except Exception as err:
            logger.warning('Failed to notify admin', admin_id=admin_id, error=str(err))


async def _answer_safely(message: Message, text: str) -> None:
    try:
        await message.answer(text, parse_mode='HTML')
    except Exception:
        await message.answer(text.replace('<', '&lt;').replace('>', '&gt;'))


async def _handle_daily_limit(
    message: Message,
    telegram_id: int,
    question: str,
    used_today: int,
    limit: int,
) -> None:
    """Save the message and notify admins quietly without answering the user."""
    bot = message.bot

    async with AsyncSessionLocal() as db:
        conversation = await crud.get_or_create_conversation(db, telegram_id)
        await crud.add_message(
            db,
            conversation_id=conversation.id,
            telegram_id=telegram_id,
            role='user',
            content=question or '[изображение]',
            has_media=bool(message.photo),
            media_type='photo' if message.photo else None,
        )
        await db.commit()

    user_name, username_str = _user_label(message)
    notify_text = (
        '🚫 <b>Дневной лимит ИИ-поддержки исчерпан</b>\n\n'
        f'<b>Пользователь:</b> <a href="tg://user?id={telegram_id}">{user_name}</a>{username_str} '
        f'(ID: <code>{telegram_id}</code>)\n'
        f'<b>Сообщений сегодня:</b> {used_today + 1} / {limit}\n'
        f'<b>Вопрос:</b> {question or "[изображение]"}'
    )
    await _notify_admins(bot, notify_text, exclude_telegram_id=telegram_id)
    logger.info('Daily AI support limit reached', telegram_id=telegram_id, used=used_today, limit=limit)


async def handle_message(message: Message) -> None:
    bot = message.bot
    telegram_id = message.from_user.id

    if _is_duplicate_update(message):
        logger.info('Duplicate update skipped', telegram_id=telegram_id)
        return

    question = (message.text or message.caption or '').strip()
    image_url: str | None = None

    if message.photo and settings_store.get_bool('VISION_ENABLED'):
        image_url = await _download_image_data_url(bot, message.photo[-1].file_id)

    if not question and not image_url:
        await message.answer('Пожалуйста, опишите вопрос текстом или пришлите скриншот.')
        return

    # Fast-path: smalltalk ("Как дела?", "Привет", etc.) — respond locally,
    # never call OpenAI so the LLM can't hallucinate VPN instructions.
    if _is_smalltalk(question) and not image_url:
        await message.answer(get_smalltalk_reply(question))
        return

    await settings_store.load()

    if _throttle_hit(telegram_id):
        if telegram_id not in _throttle_warned:
            _throttle_warned.add(telegram_id)
            await message.answer('Секунду, я ещё обрабатываю предыдущее сообщение — напишите чуть медленнее. 🙂')
        logger.info('Throttled message', telegram_id=telegram_id)
        return

    daily_limit = settings_store.get_int('DAILY_MESSAGE_LIMIT')
    if daily_limit > 0:
        async with AsyncSessionLocal() as db:
            used_today = await crud.count_user_messages_today(db, telegram_id)
        if used_today >= daily_limit:
            await _handle_daily_limit(message, telegram_id, question, used_today, daily_limit)
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

    await _answer_safely(message, answer)

    if result['escalate']:
        logger.info(
            'Escalating question to admins',
            telegram_id=telegram_id,
            hedged=bool(result.get('hedged')),
        )
        user_name, username_str = _user_label(message)
        reason = 'ИИ не уверен в ответе (нет данных в базе знаний)' if result.get('hedged') else 'запрошено моделью'
        notify_text = (
            '⚠️ <b>Внимание: Обращение требует внимания оператора!</b>\n\n'
            f'<b>Пользователь:</b> <a href="tg://user?id={telegram_id}">{user_name}</a>{username_str} '
            f'(ID: <code>{telegram_id}</code>)\n'
            f'<b>Причина:</b> {reason}\n'
            f'<b>Вопрос:</b> {question or "[изображение]"}\n\n'
            f'<b>Ответ, отправленный пользователю:</b>\n{answer}'
        )
        await _notify_admins(bot, notify_text, exclude_telegram_id=telegram_id)


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
    alerting.register_bot(bot)
    logger.info('AI support bot started')
    await dp.start_polling(bot)
