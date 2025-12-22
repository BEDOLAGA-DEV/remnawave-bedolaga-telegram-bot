import asyncio
import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user import get_users_list
from app.database.models import PinnedMessage, User, UserStatus
from app.utils.validators import sanitize_html, validate_html_tags

logger = logging.getLogger(__name__)


async def get_active_pinned_message(db: AsyncSession) -> Optional[PinnedMessage]:
    result = await db.execute(
        select(PinnedMessage)
        .where(PinnedMessage.is_active.is_(True))
        .order_by(PinnedMessage.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def set_active_pinned_message(
    db: AsyncSession,
    content: str,
    created_by: Optional[int] = None,
    media_type: Optional[str] = None,
    media_file_id: Optional[str] = None,
) -> PinnedMessage:
    sanitized_content = sanitize_html(content or "")
    is_valid, error_message = validate_html_tags(sanitized_content)
    if not is_valid:
        raise ValueError(error_message)

    if media_type not in {None, "photo", "video"}:
        raise ValueError("Поддерживаются только фото или видео в закрепленном сообщении")

    if created_by is not None:
        creator_id = await db.scalar(select(User.id).where(User.id == created_by))
    else:
        creator_id = None

    await db.execute(
        update(PinnedMessage)
        .where(PinnedMessage.is_active.is_(True))
        .values(is_active=False)
    )

    pinned_message = PinnedMessage(
        content=sanitized_content,
        media_type=media_type,
        media_file_id=media_file_id,
        is_active=True,
        created_by=creator_id,
    )

    db.add(pinned_message)
    await db.commit()
    await db.refresh(pinned_message)

    logger.info("Создано новое закрепленное сообщение #%s", pinned_message.id)
    return pinned_message


async def deliver_pinned_message_to_user(
    bot: Bot,
    db: AsyncSession,
    user: User,
) -> bool:
    pinned_message = await get_active_pinned_message(db)
    if not pinned_message:
        return False

    return await _send_and_pin_message(bot, user.telegram_id, pinned_message)


async def broadcast_pinned_message(
    bot: Bot,
    db: AsyncSession,
    pinned_message: PinnedMessage,
) -> tuple[int, int]:
    users: list[User] = []
    offset = 0
    batch_size = 5000

    while True:
        batch = await get_users_list(
            db,
            offset=offset,
            limit=batch_size,
            status=UserStatus.ACTIVE,
        )

        if not batch:
            break

        users.extend(batch)
        offset += batch_size

    sent_count = 0
    failed_count = 0
    semaphore = asyncio.Semaphore(5)

    async def send_to_user(user: User) -> None:
        nonlocal sent_count, failed_count
        async with semaphore:
            for attempt in range(3):
                try:
                    success = await _send_and_pin_message(
                        bot,
                        user.telegram_id,
                        pinned_message,
                    )
                    if success:
                        sent_count += 1
                    else:
                        failed_count += 1
                    break
                except TelegramRetryAfter as retry_error:
                    delay = min(retry_error.retry_after + 1, 30)
                    logger.warning(
                        "RetryAfter for user %s, waiting %s seconds",
                        user.telegram_id,
                        delay,
                    )
                    await asyncio.sleep(delay)
                except Exception as send_error:  # noqa: BLE001
                    logger.error(
                        "Ошибка отправки закрепленного сообщения пользователю %s: %s",
                        user.telegram_id,
                        send_error,
                    )
                    failed_count += 1
                    break

    for i in range(0, len(users), 50):
        batch = users[i : i + 50]
        tasks = [send_to_user(user) for user in batch]
        await asyncio.gather(*tasks)

    return sent_count, failed_count


async def _send_and_pin_message(bot: Bot, chat_id: int, pinned_message: PinnedMessage) -> bool:
    try:
        await bot.unpin_all_chat_messages(chat_id=chat_id)
    except TelegramBadRequest:
        pass
    except TelegramForbiddenError:
        return False

    try:
        if pinned_message.media_type == "photo" and pinned_message.media_file_id:
            sent_message = await bot.send_photo(
                chat_id=chat_id,
                photo=pinned_message.media_file_id,
                caption=pinned_message.content or None,
                parse_mode="HTML" if pinned_message.content else None,
                disable_notification=True,
            )
        elif pinned_message.media_type == "video" and pinned_message.media_file_id:
            sent_message = await bot.send_video(
                chat_id=chat_id,
                video=pinned_message.media_file_id,
                caption=pinned_message.content or None,
                parse_mode="HTML" if pinned_message.content else None,
                disable_notification=True,
            )
        else:
            sent_message = await bot.send_message(
                chat_id=chat_id,
                text=pinned_message.content,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=sent_message.message_id,
            disable_notification=True,
        )
        return True
    except TelegramForbiddenError:
        return False
    except TelegramBadRequest as error:
        logger.warning(
            "Некорректный запрос при отправке закрепленного сообщения в чат %s: %s",
            chat_id,
            error,
        )
    except Exception as error:  # noqa: BLE001
        logger.error(
            "Не удалось отправить закрепленное сообщение пользователю %s: %s",
            chat_id,
            error,
        )

    return False
