import asyncio
import html
from datetime import UTC, datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.poll import (
    get_poll_response_by_id,
    record_poll_answer,
)
from app.database.models import PollQuestion, User
from app.localization.texts import get_texts
from app.services.poll_service import get_next_question, get_question_option, reward_user_for_poll


logger = structlog.get_logger(__name__)


async def _delete_message_later(bot, chat_id: int, message_id: int, delay: int = 10) -> None:
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id, message_id)
    except Exception as error:  # pragma: no cover - cleanup best effort
        logger.debug('Не удалось удалить сообщение опроса', message_id=message_id, error=error)


async def _render_question_text(
    poll_title: str,
    question: PollQuestion,
    current_index: int,
    total: int,
    language: str,
) -> str:
    texts = get_texts(language)
    header = texts.t('POLL_QUESTION_HEADER', '<b>Вопрос {current}/{total}</b>').format(
        current=current_index,
        total=total,
    )
    lines = [f'🗳️ <b>{html.escape(poll_title)}</b>', '', header, '', html.escape(question.text)]
    return '\n'.join(lines)


async def _update_poll_message(
    message: types.Message,
    text: str,
    *,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    parse_mode: str | None = 'HTML',
) -> bool:
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return True
    except TelegramBadRequest as error:
        error_text = str(error).lower()
        if 'message is not modified' in error_text:
            logger.debug('Опросное сообщение уже актуально, пропускаем обновление', error=error)
            return True

        logger.warning('Не удалось обновить сообщение опроса', message_id=message.message_id, error=error)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.exception(
            'Непредвиденная ошибка при обновлении сообщения опроса', message_id=message.message_id, error=error
        )

    return False


def _build_options_keyboard(response_id: int, question: PollQuestion) -> types.InlineKeyboardMarkup:
    buttons: list[list[types.InlineKeyboardButton]] = []
    for option in sorted(question.options, key=lambda o: o.order):
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=option.text,
                    callback_data=f'poll_answer:{response_id}:{question.id}:{option.id}',
                )
            ]
        )
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def handle_poll_start(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    try:
        response_id = int(callback.data.split(':')[1])
    except (IndexError, ValueError):
        await callback.answer('❌ Опрос не найден', show_alert=True)
        return

    response = await get_poll_response_by_id(db, response_id)
    if not response or response.user_id != db_user.id:
        await callback.answer('❌ Опрос не найден', show_alert=True)
        return

    texts = get_texts(db_user.language)

    if response.completed_at:
        await callback.answer(texts.t('POLL_ALREADY_COMPLETED', 'Вы уже прошли этот опрос.'), show_alert=True)
        return

    if not response.poll or not response.poll.questions:
        await callback.answer(texts.t('POLL_EMPTY', 'Опрос пока недоступен.'), show_alert=True)
        return

    if not response.started_at:
        response.started_at = datetime.now(UTC)
        await db.commit()

    index, question = await get_next_question(response)
    if not question:
        await callback.answer(texts.t('POLL_ERROR', 'Не удалось загрузить вопросы.'), show_alert=True)
        return

    question_text = await _render_question_text(
        response.poll.title,
        question,
        index,
        len(response.poll.questions),
        db_user.language,
    )

    if not await _update_poll_message(
        callback.message,
        question_text,
        reply_markup=_build_options_keyboard(response.id, question),
    ):
        await callback.answer(texts.t('POLL_ERROR', 'Не удалось показать вопрос.'), show_alert=True)
        return
    await callback.answer()


async def handle_poll_answer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    try:
        _, response_id, question_id, option_id = callback.data.split(':', 3)
        response_id = int(response_id)
        question_id = int(question_id)
        option_id = int(option_id)
    except (ValueError, IndexError):
        await callback.answer('❌ Некорректные данные', show_alert=True)
        return

    response = await get_poll_response_by_id(db, response_id)
    texts = get_texts(db_user.language)

    if not response or response.user_id != db_user.id:
        await callback.answer('❌ Опрос не найден', show_alert=True)
        return

    if not response.poll:
        await callback.answer(texts.t('POLL_ERROR', 'Опрос недоступен.'), show_alert=True)
        return

    if response.completed_at:
        await callback.answer(texts.t('POLL_ALREADY_COMPLETED', 'Вы уже прошли этот опрос.'), show_alert=True)
        return

    question = next((q for q in response.poll.questions if q.id == question_id), None)
    if not question:
        await callback.answer(texts.t('POLL_ERROR', 'Вопрос не найден.'), show_alert=True)
        return

    option = await get_question_option(question, option_id)
    if not option:
        await callback.answer(texts.t('POLL_ERROR', 'Вариант ответа не найден.'), show_alert=True)
        return

    await record_poll_answer(
        db,
        response_id=response.id,
        question_id=question.id,
        option_id=option.id,
    )

    try:
        await db.refresh(response, attribute_names=['answers'])
    except Exception as error:  # pragma: no cover - defensive cache busting
        logger.debug('Не удалось обновить локальные ответы опроса', response_id=response.id, error=error)
        response = await get_poll_response_by_id(db, response.id)
        if not response:
            await callback.answer(texts.t('POLL_ERROR', 'Опрос недоступен.'), show_alert=True)
            return
    index, next_question = await get_next_question(response)

    if next_question:
        question_text = await _render_question_text(
            response.poll.title,
            next_question,
            index,
            len(response.poll.questions),
            db_user.language,
        )
        if not await _update_poll_message(
            callback.message,
            question_text,
            reply_markup=_build_options_keyboard(response.id, next_question),
        ):
            await callback.answer(texts.t('POLL_ERROR', 'Не удалось показать вопрос.'), show_alert=True)
            return
        await callback.answer()
        return

    response.completed_at = datetime.now(UTC)
    await db.commit()

    reward_amount = await reward_user_for_poll(db, response)

    thanks_lines = [texts.t('POLL_COMPLETED', '🙏 Спасибо за участие в опросе!')]
    if reward_amount:
        thanks_lines.append(
            texts.t(
                'POLL_REWARD_GRANTED',
                'Награда {amount} зачислена на ваш баланс.',
            ).format(amount=settings.format_price(reward_amount))
        )

    if not await _update_poll_message(
        callback.message,
        '\n\n'.join(thanks_lines),
    ):
        await callback.answer(texts.t('POLL_COMPLETED', '🙏 Спасибо за участие в опросе!'))
        return
    asyncio.create_task(_delete_message_later(callback.bot, callback.message.chat.id, callback.message.message_id))
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(handle_poll_start, F.data.startswith('poll_start:'))
    dp.callback_query.register(handle_poll_answer, F.data.startswith('poll_answer:'))
