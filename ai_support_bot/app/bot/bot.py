import asyncio
import base64
import io
import re
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from html import escape, unescape

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ChatType
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

from ai_support_bot.app.bot.message_queue import MessageQueue
from ai_support_bot.app.core.config import settings
from ai_support_bot.app.db import crud
from ai_support_bot.app.db.database import AsyncSessionLocal
from ai_support_bot.app.services import alerting, business_control, settings_store
from ai_support_bot.app.services.agent import support_agent
from ai_support_bot.app.services.emoji_style import unwrap
from ai_support_bot.app.services.openai_client import OpenAIError


logger = structlog.get_logger(__name__)

_WELCOME = (
    '🤖 <b>ИИ-поддержка</b>\n\n'
    'Я ИИ-ассистент поддержки: отвечаю на основе базы знаний и данных вашего аккаунта. '
    'Опишите вопрос или пришлите скриншот проблемы — если не смогу помочь, передам вопрос оператору.'
)

# (telegram_id, YYYY-MM-DD) — уже отправили пользователю ответ про лимит сегодня
_limit_user_notified: set[tuple[int, str]] = set()

_processed_updates: dict[tuple[str | None, int, int], float] = {}
_PROCESSED_TTL = 300
_business_turn: ContextVar[tuple[int, int] | None] = ContextVar('business_turn', default=None)
_message_queue = MessageQueue()

# Telegram rejects longer messages; counted in UTF-16 units of the raw HTML, which is never shorter
# than the parsed text, so every chunk is accepted.
TELEGRAM_TEXT_LIMIT = 4096
_HTML_TOKEN = re.compile(r'<[^<>]*>|&#?\w{1,32};|[^<&]+|[<&]')
_HTML_TAG = re.compile(r'<\s*(/?)\s*([a-zA-Z][\w-]*)')


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


def _utf16_len(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def _fitting_prefix(text: str, budget: int, hard_cut: bool = True) -> int:
    """Length of the longest prefix within the UTF-16 budget, cut after a line or word break when possible."""
    used, end = 0, len(text)
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > budget:
            end = index
            break
    if end == len(text):
        return end
    for separator in ('\n', ' '):
        position = text.rfind(separator, 0, end)
        if position > 0 and position >= end // 2:
            return position + 1
    return end if hard_cut else 0


def _split_plain(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    chunks = []
    while text:
        cut = _fitting_prefix(text, limit) or 1
        chunk, text = text[:cut], text[cut:]
        if chunk.strip():
            chunks.append(chunk)
    return chunks


def _split_html(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str] | None:
    """Split Telegram HTML into messages within the limit without cutting a tag or an entity.

    Tags open at a cut are closed at the end of one chunk and reopened at the start of the next.
    Returns None when the markup cannot be split this way; the caller then sends plain text.
    """
    if _utf16_len(text) <= limit:
        return [text]
    chunks: list[str] = []
    stack: list[tuple[str, str]] = []
    current, fresh = '', True

    def closing(extra: str = '') -> int:
        return _utf16_len(extra + ''.join(f'</{name}>' for name, _ in stack))

    def flush() -> None:
        nonlocal current, fresh
        chunk = current + ''.join(f'</{name}>' for name, _ in reversed(stack))
        if re.sub(r'<[^<>]*>', '', chunk).strip():
            chunks.append(chunk)
        current, fresh = ''.join(tag for _, tag in stack), True

    for token in _HTML_TOKEN.findall(text):
        tag = _HTML_TAG.match(token) if token.startswith('<') and token.endswith('>') else None
        if tag is not None and tag.group(1):
            name = tag.group(2).lower()
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == name:
                    del stack[index]
                    break
            current += token  # its length was already reserved as part of the closing tags
            continue
        if tag is None and not token.startswith(('&', '<')):
            while token:
                cut = _fitting_prefix(token, limit - _utf16_len(current) - closing(), hard_cut=fresh)
                if cut <= 0:
                    if fresh:
                        return None
                    flush()
                    continue
                current, token, fresh = current + token[:cut], token[cut:], False
                if token:
                    flush()
            continue
        opening = tag is not None and not token.endswith('/>')
        extra = f'</{tag.group(2).lower()}>' if opening else ''
        if _utf16_len(current + token) + closing(extra) > limit:
            if fresh:
                return None
            flush()
            if _utf16_len(current + token) + closing(extra) > limit:
                return None
        current += token
        fresh = False
        if opening:
            stack.append((tag.group(2).lower(), token))
    if not fresh:
        flush()
    return chunks


def _html_to_plain(text: str) -> str:
    return unescape(re.sub(r'<[^<>]*>', '', text))


async def _send_notification(bot: Bot, chat_id: int, text: str, options: dict) -> int:
    """Send a possibly long HTML notification in parts; buttons go with the last part.

    Returns the id of the first part, which attachments reply to. Raises when a part is not delivered.
    """
    pending = _split_html(text)
    parse_mode = 'HTML'
    if pending is None:
        pending, parse_mode = _split_plain(_html_to_plain(text)), None
    first_id = None
    while pending:
        chunk = pending.pop(0)
        extra = options if not pending else {}
        try:
            sent = await bot.send_message(chat_id, chunk, parse_mode=parse_mode, **extra)
        except TelegramBadRequest as error:
            if parse_mode is None or 'parse entities' not in str(error).lower():
                raise
            pending, parse_mode = _split_plain(_html_to_plain(''.join([chunk, *pending]))), None
            continue
        if first_id is None:
            first_id = sent.message_id
    return first_id


async def _alert(key: str, title: str, details: str, hint: str) -> None:
    try:
        await alerting.alert_admins(key, title, details, hint=hint)
    except Exception as error:
        logger.warning('Failed to raise admin alert', alert_key=key, error=str(error))


async def _notify_admins(bot: Bot, text: str, exclude_telegram_id: int | None = None, photo: str | None = None,
                         photos: list[str] | None = None) -> bool:
    """Deliver a service notification; True when at least one recipient received all of it."""
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
    delivered = 0
    failure: Exception | None = None
    for admin_id in sorted(recipients):
        try:
            notification_id = await _send_notification(bot, admin_id, text, options)
        except Exception as err:
            failure = err
            logger.error('Failed to notify admin', admin_id=admin_id, error=str(err), length=len(text))
            continue
        delivered += 1
        for attachment in photos or ([photo] if photo else []):
            try:
                await bot.send_photo(
                    admin_id, attachment, caption=f'Вложение к обращению пользователя {exclude_telegram_id}',
                    reply_parameters=ReplyParameters(message_id=notification_id),
                )
            except Exception as err:
                logger.warning('Failed to send attachment to admin', admin_id=admin_id, error=str(err))
    if not delivered:
        logger.error('Admin notification was not delivered to anyone', recipients=len(recipients))
        if failure is not None:
            description = getattr(failure, 'message', '') if isinstance(failure, TelegramAPIError) else ''
            await _alert(
                'notification_delivery', 'уведомление команде не доставлено',
                f'Получателей: {len(recipients)}, доставлено: 0. Ошибка: {type(failure).__name__} {description}'[:300],
                'Обращения клиентов не доходят до команды — проверьте логи бота поддержки и AISUP_ADMIN_IDS.',
            )
    return delivered > 0


async def _answer_safely(message: Message, text: str, buttons: list[list[dict]] | None = None) -> bool:
    if not await _turn_active():
        return False
    # aiogram passes the unknown «style» field (button colour) through to Telegram as is.
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(**button) for button in row] for row in buttons
    ]) if buttons else None
    try:
        await message.answer(text, parse_mode='HTML', **({'reply_markup': markup} if markup else {}))
    except TelegramBadRequest as error:
        # Premium emoji and coloured buttons are not allowed everywhere; on a refusal the
        # answer goes with plain emoji and without buttons rather than not at all.
        if ('<tg-emoji' in text or markup) and 'parse entities' not in str(error).lower():
            if not await _turn_active():
                return False
            await message.answer(unwrap(text), parse_mode='HTML')
            return True
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
    photos: list[str] | None = None,
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
            has_media=bool(photos or message.photo),
            media_type='photo' if photos or message.photo else None,
            media_file_id=photos[0] if photos else None,
            media_file_ids=photos,
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
    await _notify_admins(bot, notify_text, exclude_telegram_id=telegram_id, photos=photos)
    logger.info('Daily AI support limit reached', telegram_id=telegram_id, used=used_today, limit=limit)


async def queue_message(message: Message, business_connection: BusinessConnection | None = None) -> None:
    # Answers contain balance and personal subscription links, so groups and channels never get them.
    if getattr(message.chat, 'type', ChatType.PRIVATE) != ChatType.PRIVATE:
        return
    if message.from_user is None or _is_duplicate_update(message):
        return
    await settings_store.load()
    key = (getattr(message.bot, 'id', 0), getattr(message, 'business_connection_id', None),
           message.chat.id, message.from_user.id, _business_turn.get())

    async def process(batch):
        if await _turn_active():
            ordered = sorted(batch, key=lambda item: item.message_id)
            await handle_message(ordered[-1], business_connection, message_batch=ordered)

    try:
        await _message_queue.submit(
            key, message, process, interval=settings_store.get_int('THROTTLE_SECONDS'),
            min_delay=settings_store.get_int('BUSINESS_REPLY_DELAY_SECONDS') if business_connection else 0,
        )
    except OverflowError:
        await _answer_safely(message, 'Слишком много сообщений подряд. Дождитесь ответа на уже отправленные.')


def _failure_reason(error: Exception) -> str:
    """Short reason for admins: exception type, HTTP status and API code; never the message, keys or URLs."""
    parts = [type(error).__name__]
    if isinstance(error, OpenAIError):
        if error.status:
            parts.append(f'HTTP {error.status}')
        if error.code:
            parts.append(f'код {error.code}')
    return ', '.join(parts)


async def _report_generation_failure(message: Message, error: Exception, business: bool) -> None:
    model_error = isinstance(error, OpenAIError)
    reason = _failure_reason(error)
    mode = 'секретарь, клиенту ничего не отправлено' if business else 'бот поддержки, клиент получил «Попробуйте позже»'
    await _alert(
        f'generation:{reason}',
        'модель не ответила' if model_error else 'ошибка при подготовке ответа',
        f'{reason}\nРежим: {mode}',
        'Клиенты не получают ответы ИИ — проверьте ключ, баланс и доступность OpenAI.' if model_error
        else 'Клиенты не получают ответы ИИ — проверьте логи бота поддержки.',
    )
    # In secretary mode the reply would come from the owner's own account: a technical error there
    # looks broken, so the customer gets nothing and the owner answers manually.
    if not business:
        notice = '⚠️ Не удалось получить ответ.' if model_error else '⚠️ Произошла ошибка.'
        await _answer_safely(message, f'{notice} Попробуйте позже.')


async def handle_message(message: Message, business_connection: BusinessConnection | None = None,
                         message_batch: list[Message] | None = None) -> None:
    if message.from_user is None:
        return
    bot = message.bot
    telegram_id = message.from_user.id

    if message_batch is None and _is_duplicate_update(message):
        logger.info('Duplicate update skipped', telegram_id=telegram_id)
        return

    await settings_store.load()

    batch = message_batch or [message]
    question = '\n'.join(text for item in batch if (text := (item.text or item.caption or '').strip()))
    photo_messages = [item for item in batch if item.photo]
    image_urls = []

    if settings_store.get_bool('VISION_ENABLED'):
        for item in photo_messages:
            url = await _download_image_data_url(bot, item.photo[-1].file_id)
            if url is None:
                await _answer_safely(message, 'Не удалось загрузить скриншот. Пришлите его ещё раз.')
                return
            image_urls.append(url)
    image_url = image_urls[0] if len(image_urls) == 1 else image_urls or None

    if photo_messages and not settings_store.get_bool('VISION_ENABLED') and not question:
        await _answer_safely(message, 'Анализ изображений сейчас отключён. Опишите вопрос текстом.')
        return

    if not question and not image_url:
        await _answer_safely(message, 'Пожалуйста, опишите вопрос текстом или пришлите скриншот.')
        return

    daily_limit = settings_store.get_int('DAILY_MESSAGE_LIMIT')
    if daily_limit > 0:
        async with AsyncSessionLocal() as db:
            used_today = await crud.count_user_messages_today(db, telegram_id)
        if used_today >= daily_limit:
            await _handle_daily_limit(message, telegram_id, question, used_today, daily_limit,
                                      photos=[item.photo[-1].file_id for item in photo_messages])
            return

    try:
        async with typing_action(bot, message.chat.id, getattr(message, 'business_connection_id', None)):
            async with AsyncSessionLocal() as db:
                file_ids = [item.photo[-1].file_id for item in photo_messages]
                options = {'media_file_id': file_ids[0] if len(file_ids) == 1 else file_ids} if file_ids else {}
                result = await support_agent.generate_answer(db, telegram_id, question, image_url=image_url, **options)
    except OpenAIError as error:
        logger.error('Generation failed', error=str(error), telegram_id=telegram_id)
        await _report_generation_failure(message, error, business_connection is not None)
        return
    except Exception as error:
        logger.error('Unexpected error', error=str(error), error_type=type(error).__name__, telegram_id=telegram_id)
        await _report_generation_failure(message, error, business_connection is not None)
        return

    answer = result['answer'] or 'Не удалось сформировать ответ.'

    if await _answer_safely(message, answer, result.get('buttons')) is False:
        logger.info('AI answer suppressed: operator took over', chat_id=message.chat.id)
        return
    if result.get('buttons'):
        logger.info('Answer buttons sent', telegram_id=telegram_id,
                    buttons=[button['text'] for row in result['buttons'] for button in row])

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
        reason = {
            'repeated_diagnostics': 'ИИ повторяет уже предложенную диагностику без нового решения',
            'subscription_restricted': 'сообщение об ограничении или блокировке подписки',
            'subscription_cancellation': 'запрос на отмену подписки или автоматических списаний',
            'unrecognized_charge': 'пользователь сообщает о незнакомых списаниях без подключения услуги',
            'unwanted_renewal': 'нежеланное автопродление или списание: проверка и, возможно, возврат',
            'price_mismatch': 'цена при оплате не совпадает с показанной (скидка не применилась)',
            'account_mismatch': 'клиент пользуется оплаченной подпиской, а в этом аккаунте подписок нет',
            'diagnostics_failed': 'диагностика не помогла, требуется ручная проверка',
            'wheel_reward_review': 'не удалось подтвердить доступный личный промокод из колеса',
            'payment_review': 'требуется ручная проверка оплаты и доступа по подписке',
            'repeated_clarification': 'ИИ повторяет один уточняющий вопрос без продвижения обращения',
            'repeated_answer': 'ИИ повторяет тот же ответ без продвижения обращения',
            'refund': 'запрос на возврат денег',
            'payment_dispute': 'спорное или повторное списание',
            'wrong_tariff': 'оплачен не тот тариф: нужна замена или возврат',
            'account_transfer': 'взлом или потеря аккаунта, перенос подписки на другой аккаунт',
        }.get(result.get('escalation_reason'), reason)
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
        delivered = await _notify_admins(bot, notify_text, exclude_telegram_id=telegram_id,
                                         photo=result.get('support_photo'), photos=result.get('support_photos'))
        turn = _business_turn.get() if business_connection is not None else None
        if turn is not None:
            # Pause only when someone got the handoff with the «Вернуть ИИ» button, otherwise the chat
            # would stay silent forever without anyone knowing.
            if delivered:
                await business_control.set_manual(turn[0], True)
            else:
                logger.error('Handoff not delivered, AI stays active in business chat', chat_id=message.chat.id)


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
        await queue_message(message, business_connection=connection)
    finally:
        _business_turn.reset(token)


async def handle_business_edit(message: Message) -> None:
    if not message.business_connection_id or message.chat.type != 'private' or not message.from_user or message.from_user.is_bot:
        return
    try:
        connection = await message.bot.get_business_connection(message.business_connection_id)
    except TelegramAPIError as error:
        logger.warning('Failed to get business connection for edit', error=str(error))
        return
    if message.from_user.id == connection.user.id:
        control = await business_control.get_chat(connection.id, message.chat.id, connection.user.id)
        sent_at = message.edit_date or message.date
        if isinstance(sent_at, (int, float)):
            sent_at = datetime.fromtimestamp(sent_at, tz=UTC)
        await business_control.operator_message(control.id, sent_at)


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
    # Only private chats: in a group the answer (balance, subscription link) would be visible to everyone.
    private = F.chat.type == ChatType.PRIVATE
    dp.message.register(cmd_start, private, Command('start'))
    dp.message.register(queue_message, private, F.text | F.photo | F.caption)
    dp.business_connection.register(handle_business_connection)
    dp.business_message.register(handle_business_message)
    dp.edited_business_message.register(handle_business_edit)
    dp.callback_query.register(handle_control_callback, F.data.startswith('aisup:'))
    dp.shutdown.register(_message_queue.close)
    return dp


def build_bot() -> Bot:
    return Bot(token=settings.effective_bot_token, default=DefaultBotProperties(parse_mode='HTML'))


async def run_bot() -> None:
    bot = build_bot()
    dp = build_dispatcher()
    alerting.register_bot(bot)
    logger.info('AI support bot started')
    await dp.start_polling(bot)
