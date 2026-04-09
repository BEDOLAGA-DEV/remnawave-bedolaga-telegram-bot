import html
from datetime import UTC, datetime

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_active_subscriptions_by_user_id
from app.database.crud.user import add_user_balance
from app.database.crud.user_review import create_review, get_review_by_user
from app.database.models import TransactionType, User
from app.localization.texts import get_texts
from app.states import ReviewStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


def _stars(n: int) -> str:
    return '\u2b50' * n


def _get_rating_keyboard(language: str) -> types.InlineKeyboardMarkup:
    buttons = []
    for i in range(1, 6):
        buttons.append([
            types.InlineKeyboardButton(
                text=_stars(i),
                callback_data=f'nz!_review_rate_{i}',
            )
        ])
    texts = get_texts(language)
    buttons.append([
        types.InlineKeyboardButton(
            text=texts.BACK, callback_data='nz!_back_to_menu',
        )
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def _check_eligibility(
    db: AsyncSession, db_user: User
) -> tuple[bool, str | None]:
    """Check if user can leave a review. Returns (eligible, error_message)."""
    texts = get_texts(db_user.language)

    # Already reviewed
    existing = await get_review_by_user(db, db_user.id)
    if existing:
        return False, texts.t(
            'REVIEW_ALREADY_LEFT',
            'Вы уже оставили отзыв. Спасибо!',
        )

    # Must have subscription for REVIEW_MIN_DAYS
    subscriptions = await get_active_subscriptions_by_user_id(db, db_user.id)
    if not subscriptions:
        return False, texts.t(
            'REVIEW_NO_SUBSCRIPTION',
            'Для написания отзыва необходимо иметь активную подписку.',
        )

    now = datetime.now(UTC)
    min_days = settings.REVIEW_MIN_DAYS
    has_old_enough = any(
        sub.start_date and (now - sub.start_date).days >= min_days
        for sub in subscriptions
    )
    if not has_old_enough:
        return False, texts.t(
            'REVIEW_SUB_TOO_NEW',
            'Для написания отзыва необходимо пользоваться подпиской не менее {days} дней.',
        ).format(days=min_days)

    return True, None


@error_handler
async def start_review(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    eligible, error_msg = await _check_eligibility(db, db_user)
    if not eligible:
        await callback.answer(error_msg, show_alert=True)
        return

    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'REVIEW_SELECT_RATING',
            'Оцените наш сервис от 1 до 5 звезд:',
        ),
        reply_markup=_get_rating_keyboard(db_user.language),
    )
    await state.set_state(ReviewStates.rating)
    await callback.answer()


@error_handler
async def start_review_command(
    message: types.Message,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    eligible, error_msg = await _check_eligibility(db, db_user)
    if not eligible:
        await message.answer(error_msg)
        return

    texts = get_texts(db_user.language)
    await message.answer(
        texts.t(
            'REVIEW_SELECT_RATING',
            'Оцените наш сервис от 1 до 5 звезд:',
        ),
        reply_markup=_get_rating_keyboard(db_user.language),
    )
    await state.set_state(ReviewStates.rating)


@error_handler
async def on_rating_selected(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
):
    rating = int(callback.data.split('_')[-1])
    await state.update_data(rating=rating)
    await state.set_state(ReviewStates.text)

    texts = get_texts(db_user.language)
    cancel_kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text=texts.BACK, callback_data='nz!_back_to_menu',
        )]
    ])
    await callback.message.edit_text(
        texts.t(
            'REVIEW_ENTER_TEXT',
            'Вы выбрали {stars} ({rating}/5)\n\nНапишите ваш отзыв текстом:',
        ).format(stars=_stars(rating), rating=rating),
        reply_markup=cancel_kb,
    )
    await callback.answer()


@error_handler
async def on_review_text(
    message: types.Message,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
    bot: Bot,
):
    data = await state.get_data()
    rating = data.get('rating', 5)
    review_text = message.text.strip()

    if not review_text or len(review_text) < 10:
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('REVIEW_TEXT_TOO_SHORT', 'Отзыв слишком короткий. Напишите хотя бы 10 символов.'),
        )
        return

    if len(review_text) > 1000:
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('REVIEW_TEXT_TOO_LONG', 'Отзыв слишком длинный. Максимум 1000 символов.'),
        )
        return

    bonus = settings.REVIEW_BONUS_KOPEKS

    # Create review
    await create_review(
        db=db,
        user_id=db_user.id,
        rating=rating,
        text=review_text,
        bonus_kopeks=bonus,
    )

    # Credit balance
    if bonus > 0:
        await add_user_balance(
            db=db,
            user=db_user,
            amount_kopeks=bonus,
            description='Бонус за отзыв',
            transaction_type=TransactionType.DEPOSIT,
        )

    texts = get_texts(db_user.language)
    bonus_str = settings.format_price(bonus) if bonus > 0 else ''
    if bonus > 0:
        msg = texts.t(
            'REVIEW_CREATED_WITH_BONUS',
            'Спасибо за отзыв! {stars} ({rating}/5)\n\nВам начислен бонус: {bonus}',
        ).format(stars=_stars(rating), rating=rating, bonus=bonus_str)
    else:
        msg = texts.t(
            'REVIEW_CREATED',
            'Спасибо за отзыв! {stars} ({rating}/5)\n\nОтзыв отправлен на модерацию.',
        ).format(stars=_stars(rating), rating=rating)

    back_kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text=texts.BACK, callback_data='nz!_back_to_menu',
        )]
    ])
    await message.answer(msg, reply_markup=back_kb)
    await state.clear()

    logger.info(
        'Пользователь оставил отзыв',
        user_id=db_user.id,
        rating=rating,
        bonus_kopeks=bonus,
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(start_review, F.data == 'nz!_review')
    dp.message.register(start_review_command, F.text == '/review')
    dp.callback_query.register(
        on_rating_selected,
        F.data.startswith('nz!_review_rate_'),
        ReviewStates.rating,
    )
    dp.message.register(on_review_text, ReviewStates.text)
