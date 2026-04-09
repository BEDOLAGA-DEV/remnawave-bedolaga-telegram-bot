import html
from datetime import UTC, datetime

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user_review import (
    approve_review,
    get_pending_reviews,
    reject_review,
    set_channel_message_id,
)
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _format_review_for_channel(review) -> str:
    """Format a review for posting to the public channel."""
    stars = '\u2b50' * review.rating
    username = review.user.username if review.user and review.user.username else None
    user_display = f'@{username}' if username else (review.user.first_name or 'Пользователь')

    days = 0
    if review.user and review.user.created_at:
        days = (datetime.now(UTC) - review.user.created_at).days

    escaped_text = html.escape(review.text)

    return (
        f'{stars} ({review.rating}/5)\n'
        f'\n'
        f'"{escaped_text}"\n'
        f'\n'
        f'— {user_display}, пользователь {days} дней'
    )


def _format_review_for_admin(review) -> str:
    """Format a review for admin moderation view."""
    stars = '\u2b50' * review.rating
    username = review.user.username if review.user and review.user.username else None
    user_display = f'@{username}' if username else (review.user.first_name or 'ID: ' + str(review.user_id))

    escaped_text = html.escape(review.text)
    bonus_str = settings.format_price(review.bonus_kopeks) if review.bonus_kopeks else '0'

    return (
        f'{stars} ({review.rating}/5)\n'
        f'<b>Пользователь:</b> {user_display}\n'
        f'<b>Бонус:</b> {bonus_str}\n\n'
        f'"{escaped_text}"'
    )


@admin_required
@error_handler
async def show_pending_reviews(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    reviews = await get_pending_reviews(db)

    if not reviews:
        back_kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')]
        ])
        await callback.message.edit_text(
            texts.t('ADMIN_NO_PENDING_REVIEWS', 'Нет отзывов на модерации.'),
            reply_markup=back_kb,
        )
        await callback.answer()
        return

    # Show first pending review
    review = reviews[0]
    review_text = _format_review_for_admin(review)
    count_text = f'\n\n<i>Отзывов на модерации: {len(reviews)}</i>'

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text='✅ Одобрить',
                callback_data=f'admin_review_approve_{review.id}',
            ),
            types.InlineKeyboardButton(
                text='❌ Отклонить',
                callback_data=f'admin_review_reject_{review.id}',
            ),
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
    ])

    await callback.message.edit_text(
        review_text + count_text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def on_approve_review(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    bot: Bot,
):
    review_id = int(callback.data.split('_')[-1])
    review = await approve_review(db, review_id)

    if not review:
        await callback.answer('Отзыв не найден', show_alert=True)
        return

    # Post to channel
    channel_id = settings.REVIEW_CHANNEL_ID
    if channel_id:
        try:
            channel_text = _format_review_for_channel(review)
            sent_msg = await bot.send_message(
                chat_id=channel_id,
                text=channel_text,
                parse_mode='HTML',
            )
            await set_channel_message_id(db, review.id, sent_msg.message_id)
            logger.info(
                'Отзыв опубликован в канале',
                review_id=review.id,
                channel_message_id=sent_msg.message_id,
            )
        except Exception as e:
            logger.error('Не удалось опубликовать отзыв в канале', error=e, review_id=review.id)

    await callback.answer('Отзыв одобрен!', show_alert=True)

    # Refresh pending list
    await show_pending_reviews(callback, db_user=db_user, db=db)


@admin_required
@error_handler
async def on_reject_review(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    bot: Bot,
):
    review_id = int(callback.data.split('_')[-1])

    # Check if review has channel message to delete
    from app.database.crud.user_review import get_pending_reviews as _unused  # noqa: F401
    from sqlalchemy import select
    from app.database.models import UserReview

    result = await db.execute(
        select(UserReview).where(UserReview.id == review_id)
    )
    review_obj = result.scalar_one_or_none()

    if review_obj and review_obj.channel_message_id and settings.REVIEW_CHANNEL_ID:
        try:
            await bot.delete_message(
                chat_id=settings.REVIEW_CHANNEL_ID,
                message_id=review_obj.channel_message_id,
            )
        except TelegramBadRequest:
            logger.warning(
                'Не удалось удалить сообщение отзыва из канала',
                review_id=review_id,
            )

    deleted = await reject_review(db, review_id)
    if not deleted:
        await callback.answer('Отзыв не найден', show_alert=True)
        return

    await callback.answer('Отзыв отклонен и удален.', show_alert=True)

    # Refresh pending list
    await show_pending_reviews(callback, db_user=db_user, db=db)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_pending_reviews, F.data == 'admin_reviews')
    dp.callback_query.register(
        on_approve_review, F.data.startswith('admin_review_approve_')
    )
    dp.callback_query.register(
        on_reject_review, F.data.startswith('admin_review_reject_')
    )
