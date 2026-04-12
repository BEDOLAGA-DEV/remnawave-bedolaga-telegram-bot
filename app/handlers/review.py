import html
from datetime import UTC, datetime

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_active_subscriptions_by_user_id
from app.database.crud.user import add_user_balance
from app.database.crud.user_review import create_review, get_approved_reviews, get_review_by_user
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

    # Проверяем по дате регистрации пользователя (а не sub.start_date,
    # т.к. start_date сбрасывается при каждом продлении/переключении).
    user_created = getattr(db_user, 'created_at', None)
    if user_created:
        # Ensure timezone-aware comparison
        if user_created.tzinfo is None:
            user_created = user_created.replace(tzinfo=UTC)
        member_days = (now - user_created).days
    else:
        # Fallback: oldest subscription start_date
        member_days = max(
            ((now - sub.start_date).days if sub.start_date else 0)
            for sub in subscriptions
        )

    if member_days < min_days:
        remaining = min_days - member_days
        return False, texts.t(
            'REVIEW_SUB_TOO_NEW',
            'Для написания отзыва необходимо пользоваться сервисом не менее {days} дней.\n'
            'Осталось: {remaining} дн.',
        ).format(days=min_days, remaining=remaining)

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

    # Create review (is_approved=False by default — awaits moderation)
    await create_review(
        db=db,
        user_id=db_user.id,
        rating=rating,
        text=review_text,
        bonus_kopeks=bonus,
    )

    # Бонус НЕ начисляется здесь — только после одобрения админом
    # (app/handlers/admin/reviews.py:on_approve_review)

    texts = get_texts(db_user.language)
    bonus_str = settings.format_price(bonus) if bonus > 0 else ''
    msg = texts.t(
        'REVIEW_CREATED',
        'Спасибо за отзыв! {stars} ({rating}/5)\n\n'
        'Отзыв отправлен на модерацию.\n'
        'После одобрения вам будет начислен бонус: {bonus}',
    ).format(stars=_stars(rating), rating=rating, bonus=bonus_str)

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


@error_handler
async def show_review_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Show review entry screen: eligibility info, leave-review button, channel link."""
    texts = get_texts(db_user.language)

    existing = await get_review_by_user(db, db_user.id)
    buttons: list[list[types.InlineKeyboardButton]] = []

    if existing:
        status_label = (
            texts.t('REVIEW_STATUS_APPROVED', '✅ Опубликован')
            if existing.is_approved
            else texts.t('REVIEW_STATUS_PENDING', '⏳ На модерации')
        )
        msg = texts.t(
            'REVIEW_ALREADY_LEFT_DETAILS',
            '⭐ <b>Ваш отзыв</b>\n\n'
            'Оценка: {stars} ({rating}/5)\n'
            'Статус: {status}\n\n'
            '«{text}»\n\n'
            'Бонус: {bonus}',
        ).format(
            stars=_stars(existing.rating),
            rating=existing.rating,
            status=status_label,
            text=html.escape(existing.text[:200]),
            bonus=texts.format_price(existing.bonus_kopeks) if existing.bonus_kopeks else '—',
        )
    else:
        eligible, error_msg = await _check_eligibility(db, db_user)
        if eligible:
            msg = texts.t(
                'REVIEW_MENU_ELIGIBLE',
                '⭐ <b>Отзывы</b>\n\n'
                'Оставьте отзыв о нашем сервисе и получите бонус {bonus} на баланс!\n\n'
                'Ваш отзыв будет проверен модератором.',
            ).format(bonus=texts.format_price(settings.REVIEW_BONUS_KOPEKS))
            buttons.append([
                types.InlineKeyboardButton(
                    text=texts.t('REVIEW_WRITE_BUTTON', '✍️ Оставить отзыв'),
                    callback_data='nz!_review',
                )
            ])
        else:
            msg = texts.t(
                'REVIEW_MENU_INELIGIBLE',
                '⭐ <b>Отзывы</b>\n\n{reason}\n\n'
                'Бонус за отзыв: {bonus}',
            ).format(reason=error_msg, bonus=texts.format_price(settings.REVIEW_BONUS_KOPEKS))

    # Кнопка просмотра всех одобренных отзывов (всегда видна)
    buttons.append([
        types.InlineKeyboardButton(
            text=texts.t('REVIEW_VIEW_ALL_BUTTON', '📋 Все отзывы'),
            callback_data='nz!_review_all',
        )
    ])

    # Дополнительная URL-кнопка на публичный канал (если настроен)
    channel_url = (settings.REVIEWS_CHANNEL_URL or '').strip()
    if channel_url:
        buttons.append([
            types.InlineKeyboardButton(
                text=texts.t('REVIEW_CHANNEL_BUTTON', '📺 Канал отзывов'),
                url=channel_url,
            )
        ])

    buttons.append([
        types.InlineKeyboardButton(text=texts.BACK, callback_data='nz!_back_to_menu')
    ])

    await callback.message.edit_text(
        msg,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def show_all_reviews(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    """Show last 10 approved reviews from other users."""
    texts = get_texts(db_user.language)
    reviews = await get_approved_reviews(db, limit=10)

    if not reviews:
        msg = texts.t(
            'REVIEW_ALL_EMPTY',
            '📋 <b>Отзывы</b>\n\nПока нет опубликованных отзывов.',
        )
    else:
        lines = [texts.t('REVIEW_ALL_HEADER', '📋 <b>Последние отзывы</b>\n')]
        for r in reviews:
            name = '—'
            if r.user:
                name = r.user.first_name or r.user.username or f'#{r.user.telegram_id}'
                name = html.escape(name)
            stars = _stars(r.rating)
            short_text = html.escape(r.text[:120])
            if len(r.text) > 120:
                short_text += '...'
            lines.append(f'{stars} <b>{name}</b>\n{short_text}\n')
        msg = '\n'.join(lines)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='nz!_review_menu')],
        ]
    )

    await callback.message.edit_text(msg, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_review_menu, F.data == 'nz!_review_menu')
    dp.callback_query.register(show_all_reviews, F.data == 'nz!_review_all')
    dp.callback_query.register(start_review, F.data == 'nz!_review')
    dp.message.register(start_review_command, F.text == '/review')
    dp.callback_query.register(
        on_rating_selected,
        F.data.startswith('nz!_review_rate_'),
        ReviewStates.rating,
    )
    dp.message.register(on_review_text, ReviewStates.text)
