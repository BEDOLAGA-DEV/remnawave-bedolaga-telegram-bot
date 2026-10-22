import asyncio
import base64
import io
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from html import escape

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db import crud
from ai_support_bot.app.db.database import AsyncSessionLocal
from ai_support_bot.app.services import alerting, business_control, settings_store
from ai_support_bot.app.services.agent import support_agent
from ai_support_bot.app.services.openai_client import OpenAIError


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

_processed_updates: dict[tuple[str | None, int, int], float] = {}
_PROCESSED_TTL = 300
_business_turn: ContextVar[tuple[int, int] | None] = ContextVar('business_turn', default=None)


async def _turn_active() -> bool:
    turn = _business_turn.get()
    return turn is None or await business_control.active(*turn)


def _control_buttons(control_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Взять в работу', callback_data=f'aisup:take:{control_id}'),
        InlineKeyboardButton(text='Вернуть ИИ', callback_data=f'aisup:resume:{control_id}'),
    ]])


def _is_duplicate_update(message: Message) -> bool:
    message_key = getattr(message, 'message_id', None)
    if message_key is None:
        return False
    key = (getattr(message, 'business_connection_id', None), message.chat.id, message_key)
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
async def typing_action(bot: Bot, chat_id: int, business_connection_id: str | None = None):
    async def loop():
        try:
            while True:
                if not await _turn_active():
                    return
                options = {'business_connection_id': business_connection_id} if business_connection_id else {}
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, **options)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except TelegramAPIError as error:
            logger.warning('Failed to send typing action', error=str(error))

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
    parts = (message.text or '').split(maxsplit=1)
    payload = parts[1] if len(parts) == 2 else ''
    if payload.startswith('bizChat') and payload[7:].isdigit() and message.from_user is not None:
        control = await business_control.find_owned_chat(message.from_user.id, int(payload[7:]))
        if control is None:
            await message.answer('Чат пока не найден. Управление станет доступно после первого сообщения боту в этом чате.')
            return
        status = 'приостановлен' if business_control.paused(control) else 'включён'
        await message.answer(
            f'ИИ {status} в чате <code>{control.chat_id}</code>.\n'
            '«Взять в работу» приостанавливает ИИ до нажатия «Вернуть ИИ».',
            reply_markup=_control_buttons(control.id),
        )
        return
    await message.answer(_WELCOME)


def _user_label(message: Message) -> tuple[str, str]:
    user_name = escape(message.from_user.full_name or 'Пользователь')
    username_str = f' (@{escape(message.from_user.username)})' if message.from_user.username else ''
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


async def _notify_admins(bot: Bot, text: str, exclude_telegram_id: int | None = None, photo: str | None = None) -> None:
    recipients = set(_admin_recipients(exclude_telegram_id))
    options = {}
    turn = _business_turn.get()
    if turn is not None:
        control = await business_control.get_by_id(turn[0])
        if control is not None:
            options['reply_markup'] = _control_buttons(control.id)
            if control.owner_id != exclude_telegram_id:
                recipients.add(control.owner_id)
            text += f'\n\n<b>Чат обращения:</b> <code>{control.chat_id}</code>'
    for admin_id in sorted(recipients):
        try:
            notification = await bot.send_message(admin_id, text, parse_mode='HTML', **options)
            if photo:
                await bot.send_photo(
                    admin_id, photo, caption=f'Вложение к обращению пользователя {exclude_telegram_id}',
                    reply_parameters=ReplyParameters(message_id=notification.message_id),
                )
        except Exception as err:
            logger.warning('Failed to notify admin', admin_id=admin_id, error=str(err))


async def _answer_safely(message: Message, text: str) -> bool:
    if not await _turn_active():
        return False
    try:
        await message.answer(text, parse_mode='HTML')
    except TelegramBadRequest as error:
        if 'parse entities' not in str(error).lower():
            raise
        if not await _turn_active():
            return False
        await message.answer(text.replace('<', '&lt;').replace('>', '&gt;'))
    return True


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
        f'<b>Вопрос:</b> {escape(question or "[изображение]")}'
    )
    await _notify_admins(bot, notify_text, exclude_telegram_id=telegram_id)
    logger.info('Daily AI support limit reached', telegram_id=telegram_id, used=used_today, limit=limit)


async def handle_message(message: Message, business_connection: BusinessConnection | None = None) -> None:
    if message.from_user is None:
        return
    bot = message.bot
    telegram_id = message.from_user.id

    if _is_duplicate_update(message):
        logger.info('Duplicate update skipped', telegram_id=telegram_id)
        return

    await settings_store.load()

    question = (message.text or message.caption or '').strip()
    image_url: str | None = None

    if message.photo and settings_store.get_bool('VISION_ENABLED'):
        image_url = await _download_image_data_url(bot, message.photo[-1].file_id)

    if message.photo and not settings_store.get_bool('VISION_ENABLED') and not question:
        await _answer_safely(message, 'Анализ изображений сейчас отключён. Опишите вопрос текстом.')
        return

    if not question and not image_url:
        await _answer_safely(message, 'Пожалуйста, опишите вопрос текстом или пришлите скриншот.')
        return

    if _throttle_hit(telegram_id):
        if telegram_id not in _throttle_warned:
            _throttle_warned.add(telegram_id)
            await _answer_safely(message, 'Секунду, я ещё обрабатываю предыдущее сообщение — напишите чуть медленнее. 🙂')
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
        async with typing_action(bot, message.chat.id, getattr(message, 'business_connection_id', None)):
            async with AsyncSessionLocal() as db:
                options = {'media_file_id': message.photo[-1].file_id} if message.photo else {}
                result = await support_agent.generate_answer(db, telegram_id, question, image_url=image_url, **options)
    except OpenAIError as error:
        logger.error('Generation failed', error=str(error), telegram_id=telegram_id)
        await _answer_safely(message, '⚠️ Не удалось получить ответ. Попробуйте позже.')
        return
    except Exception as error:
        logger.error('Unexpected error', error=str(error), telegram_id=telegram_id)
        await _answer_safely(message, '⚠️ Произошла ошибка. Попробуйте позже.')
        return

    answer = result['answer'] or 'Не удалось сформировать ответ.'

    if await _answer_safely(message, answer) is False:
        logger.info('AI answer suppressed: operator took over', chat_id=message.chat.id)
        return

    if (
        not result['escalate']
        and result['answer']
        and business_connection is not None
        and business_connection.rights
        and business_connection.rights.can_read_messages
        and await _turn_active()
    ):
        try:
            await bot.read_business_message(
                business_connection_id=business_connection.id,
                chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except TelegramAPIError as error:
            logger.warning('Failed to mark business message as read', error=str(error), chat_id=message.chat.id)

    if result['escalate']:
        logger.info(
            'Escalating question to admins',
            telegram_id=telegram_id,
            hedged=bool(result.get('hedged')),
        )
        user_name, username_str = _user_label(message)
        reason = 'ИИ не уверен в ответе (нет данных в базе знаний)' if result.get('hedged') else (
            'передача по запросу пользователя или правилам поддержки' if result.get('answer_source') == 'rules' else 'запрошено моделью'
        )
        notify_text = (
            '⚠️ <b>Внимание: Обращение требует внимания оператора!</b>\n\n'
            f'<b>Пользователь:</b> <a href="tg://user?id={telegram_id}">{user_name}</a>{username_str} '
            f'(ID: <code>{telegram_id}</code>)\n'
            f'<b>Причина:</b> {reason}\n'
            f'<b>Вопрос:</b> {escape(question or "[изображение]")}\n\n'
            f'<b>Ответ, отправленный пользователю:</b>\n{answer}'
        )
        if result.get('support_context'):
            notify_text += '\n\n' + escape(result['support_context'])
        await _notify_admins(bot, notify_text, exclude_telegram_id=telegram_id, photo=result.get('support_photo'))


async def handle_business_connection(connection: BusinessConnection) -> None:
    logger.info(
        'AI support business connection updated',
        business_connection_id=connection.id,
        owner_id=connection.user.id,
        enabled=connection.is_enabled,
        can_reply=bool(connection.rights and connection.rights.can_reply),
    )


async def handle_business_message(message: Message) -> None:
    if (
        not message.business_connection_id
        or message.chat.type != 'private'
        or message.from_user is None
        or message.from_user.is_bot
        or message.sender_business_bot is not None
    ):
        return

    # Fetch current permissions even after a restart or a missed connection update.
    try:
        connection = await message.bot.get_business_connection(message.business_connection_id)
    except TelegramAPIError as error:
        logger.warning('Failed to get business connection', error=str(error))
        return

    control = await business_control.get_chat(connection.id, message.chat.id, connection.user.id)
    if message.from_user.id == connection.user.id:
        await business_control.operator_message(control.id, message.date)
        return

    if (
        not connection.is_enabled
        or not connection.rights
        or not connection.rights.can_reply
        or message.from_user.id == connection.user.id
        or message.chat.id == connection.user.id
    ):
        return

    # Message.answer() retains business_connection_id, including validation/error replies.
    if business_control.paused(control) or not (message.text or message.photo or message.caption):
        return
    token = _business_turn.set((control.id, control.revision))
    try:
        await handle_message(message, business_connection=connection)
    finally:
        _business_turn.reset(token)


async def handle_control_callback(query: CallbackQuery) -> None:
    parts = (query.data or '').split(':')
    if len(parts) != 3 or parts[1] not in {'take', 'resume'} or not parts[2].isdigit():
        await query.answer('Неизвестная команда', show_alert=True)
        return
    control = await business_control.get_by_id(int(parts[2]))
    if control is None or (query.from_user.id != control.owner_id and query.from_user.id not in settings.admin_ids):
        await query.answer('Нет доступа к обращению', show_alert=True)
        return
    take = parts[1] == 'take'
    await business_control.set_manual(control.id, take)
    await query.answer('ИИ приостановлен до нажатия «Вернуть ИИ»' if take else 'ИИ включён для новых сообщений', show_alert=True)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.register(cmd_start, Command('start'))
    dp.message.register(handle_message, F.text | F.photo | F.caption)
    dp.business_connection.register(handle_business_connection)
    dp.business_message.register(handle_business_message)
    dp.callback_query.register(handle_control_callback, F.data.startswith('aisup:'))
    return dp


def build_bot() -> Bot:
    return Bot(token=settings.effective_bot_token, default=DefaultBotProperties(parse_mode='HTML'))


async def run_bot() -> None:
    bot = build_bot()
    dp = build_dispatcher()
    alerting.register_bot(bot)
    logger.info('AI support bot started')
    await dp.start_polling(bot)
