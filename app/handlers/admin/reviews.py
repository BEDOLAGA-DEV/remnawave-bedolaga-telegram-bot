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
from app.utils.user_utils import format_user_public_display


logger = structlog.get_logger(__name__)


def _format_review_text_for_channel(review) -> str:
    """The user's review text as it should appear in the channel."""
    return html.escape(review.text)


async def _format_review_meta_for_channel(review, db: AsyncSession) -> str:
    """Rating + short user info posted as a reply to the review text."""
    stars = '\u2b50' * review.rating

    days = 0
    if review.user and review.user.created_at:
        days = (datetime.now(UTC) - review.user.created_at).days

    # Site-only users have no @username / first_name; helper falls back to
    # anonymized email or `Пользователь #ID` so the channel post always
    # carries some identifier.
    user_display = format_user_public_display(getattr(review, 'user', None))

    lines = [
        f'{stars} ({review.rating}/5)',
        f'<b>Автор:</b> {html.escape(user_display)}',
        f'<b>С нами:</b> {days} дн.',
    ]

    try:
        from app.database.crud.subscription import get_subscription_by_user_id
        from app.database.models import SubscriptionStatus

        sub = await get_subscription_by_user_id(db, review.user_id)
        if sub:
            tariff_name = (
                sub.tariff.name if getattr(sub, 'tariff', None) and sub.tariff.name else None
            )
            if tariff_name:
                lines.append(f'<b>Тариф:</b> {html.escape(tariff_name)}')

            status_map = {
                SubscriptionStatus.ACTIVE.value: '✅ активна',
                SubscriptionStatus.TRIAL.value: '🎁 триал',
                SubscriptionStatus.EXPIRED.value: '⌛ истекла',
                SubscriptionStatus.DISABLED.value: '🚫 отключена',
            }
            status_label = status_map.get(sub.status, sub.status)
            lines.append(f'<b>Подписка:</b> {status_label}')
    except Exception as e:
        logger.debug('Не удалось получить подписку для мета-отзыва', error=e, review_id=review.id)

    return '\n'.join(lines)


def _format_review_for_admin(review) -> str:
    """Format a review for admin moderation view."""
    stars = '\u2b50' * review.rating
    user_display = format_user_public_display(getattr(review, 'user', None))

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

    # Credit bonus to user
    bonus = review.bonus_kopeks or 0
    if bonus > 0:
        try:
            from app.database.crud.user import add_user_balance, get_user_by_id
            from app.database.models import TransactionType

            review_user = await get_user_by_id(db, review.user_id)
            if review_user:
                await add_user_balance(
                    db=db,
                    user=review_user,
                    amount_kopeks=bonus,
                    description='Бонус за одобренный отзыв',
                    transaction_type=TransactionType.DEPOSIT,
                )
                # Notify user
                try:
                    await bot.send_message(
                        chat_id=review_user.telegram_id,
                        text=f'Ваш отзыв одобрен! Бонус {settings.format_price(bonus)} начислен на баланс.',
                    )
                except Exception:
                    pass  # User might have blocked the bot
        except Exception as bonus_err:
            logger.error('Не удалось начислить бонус за отзыв', error=bonus_err, review_id=review.id)

    # Post to channel: forward user's original message, then rating+meta as reply
    channel_published = False
    channel_id = settings.REVIEW_CHANNEL_ID
    if channel_id:
        try:
            text_msg = None
            if review.source_chat_id and review.source_message_id:
                try:
                    text_msg = await bot.forward_message(
                        chat_id=channel_id,
                        from_chat_id=review.source_chat_id,
                        message_id=review.source_message_id,
                    )
                except Exception as fwd_err:
                    logger.warning(
                        'Не удалось переслать оригинальное сообщение отзыва, отправляю текстом',
                        error=fwd_err,
                        review_id=review.id,
                    )
            if text_msg is None:
                text_msg = await bot.send_message(
                    chat_id=channel_id,
                    text=_format_review_text_for_channel(review),
                    parse_mode='HTML',
                )
            try:
                meta_text = await _format_review_meta_for_channel(review, db)
                await bot.send_message(
                    chat_id=channel_id,
                    text=meta_text,
                    parse_mode='HTML',
                    reply_to_message_id=text_msg.message_id,
                )
            except Exception as meta_err:
                logger.error(
                    'Не удалось отправить мета-сообщение к отзыву',
                    error=meta_err,
                    review_id=review.id,
                )
            await set_channel_message_id(db, review.id, text_msg.message_id)
            channel_published = True
            logger.info(
                'Отзыв опубликован в канале',
                review_id=review.id,
                channel_message_id=text_msg.message_id,
            )
        except Exception as e:
            logger.error('Не удалось опубликовать отзыв в канале', error=e, review_id=review.id)

    if channel_published:
        await callback.answer('Отзыв одобрен и опубликован!', show_alert=True)
    elif channel_id:
        await callback.answer(
            'Отзыв одобрен, но не удалось опубликовать в канале.\n'
            'Проверьте: бот добавлен в канал как админ?',
            show_alert=True,
        )
    else:
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
