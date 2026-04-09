"""Contest handlers for daily games."""

from datetime import UTC, datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.contest import get_active_rounds, get_attempt
from app.database.crud.subscription import get_active_subscriptions_by_user_id
from app.database.database import AsyncSessionLocal
from app.database.models import SubscriptionStatus
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.contests import (
    ContestAttemptService,
    get_game_strategy,
)
from app.states import ContestStates
from app.utils.decorators import auth_required, error_handler


logger = structlog.get_logger(__name__)

# Rate limiting storage
_rate_limits: dict = {}

# Service instance
_attempt_service = ContestAttemptService()


def _check_rate_limit(user_id: int, action: str, limit: int = 1, window_seconds: int = 5) -> bool:
    """Check if user exceeds rate limit for contest actions."""
    key = f'{user_id}_{action}'
    now = datetime.now(UTC).timestamp()

    if key not in _rate_limits:
        _rate_limits[key] = []

    # Clean old entries
    _rate_limits[key] = [t for t in _rate_limits[key] if now - t < window_seconds]

    if len(_rate_limits[key]) >= limit:
        return False

    _rate_limits[key].append(now)
    return True


def _validate_callback_data(data: str) -> list | None:
    """Validate and parse callback data safely."""
    if not data or not isinstance(data, str):
        return None

    parts = data.split('_')
    if len(parts) < 2 or parts[0] != 'contest':
        return None

    for part in parts:
        if not part or len(part) > 50:
            return None

    return parts


def _user_allowed(subscription) -> bool:
    """Check if user has active or trial subscription."""
    if not subscription:
        return False
    return subscription.status in {
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.TRIAL.value,
    }


async def _reply_not_eligible(callback: types.CallbackQuery, language: str):
    """Reply that user is not eligible to play."""
    texts = get_texts(language)
    await callback.answer(
        texts.t('CONTEST_NOT_ELIGIBLE', 'Игры доступны только с активной или триальной подпиской.'),
        show_alert=True,
    )


# ---------- Handlers ----------


@auth_required
@error_handler
async def show_contests_menu(callback: types.CallbackQuery, db_user, db: AsyncSession):
    """Show menu with available contest games."""
    texts = get_texts(db_user.language)

    active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
    # For eligibility: pick best non-daily subscription (most days left)
    non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
    eligible = non_daily or active_subs
    subscription = max(eligible, key=lambda s: s.days_left) if eligible else None
    if not _user_allowed(subscription):
        await _reply_not_eligible(callback, db_user.language)
        return

    active_rounds = await get_active_rounds(db)

    # Group by template, take one round per template
    unique_templates = {}
    for rnd in active_rounds:
        if not rnd.template or not rnd.template.is_enabled:
            continue
        tpl_slug = rnd.template.slug if rnd.template else ''
        if tpl_slug not in unique_templates:
            unique_templates[tpl_slug] = rnd

    buttons = []
    for tpl_slug, rnd in unique_templates.items():
        title = rnd.template.name if rnd.template else tpl_slug
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=f'▶️ {title}',
                    callback_data=f'contest_play_{tpl_slug}_{rnd.id}',
                )
            ]
        )

    if not buttons:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('CONTEST_EMPTY', 'Сейчас игр нет'),
                    callback_data='noop',
                )
            ]
        )

    buttons.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])

    await callback.message.edit_text(
        texts.t('CONTEST_MENU_TITLE', '🎲 <b>Игры/Конкурсы</b>\nВыберите игру:'),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@auth_required
@error_handler
async def play_contest(callback: types.CallbackQuery, state: FSMContext, db_user, db: AsyncSession):
    """Start playing a specific contest."""
    texts = get_texts(db_user.language)

    active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
    # For eligibility: pick best non-daily subscription (most days left)
    non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
    eligible = non_daily or active_subs
    subscription = max(eligible, key=lambda s: s.days_left) if eligible else None
    if not _user_allowed(subscription):
        await _reply_not_eligible(callback, db_user.language)
        return

    # Rate limit check
    if not _check_rate_limit(db_user.id, 'contest_play', limit=2, window_seconds=10):
        await callback.answer(
            texts.t('CONTEST_TOO_FAST', 'Слишком быстро! Подождите.'),
            show_alert=True,
        )
        return

    # Validate callback data
    parts = _validate_callback_data(callback.data)
    if not parts or len(parts) < 4 or parts[1] != 'play':
        await callback.answer('Некорректные данные', show_alert=True)
        return

    round_id_str = parts[-1]
    try:
        round_id = int(round_id_str)
    except ValueError:
        await callback.answer('Некорректные данные', show_alert=True)
        return

    # Get round with template
    async with AsyncSessionLocal() as db2:
        active_rounds = await get_active_rounds(db2)
        round_obj = next((r for r in active_rounds if r.id == round_id), None)

        if not round_obj:
            await callback.answer(
                texts.t('CONTEST_ROUND_FINISHED', 'Раунд завершён или недоступен.'),
                show_alert=True,
            )
            return

        if not round_obj.template or not round_obj.template.is_enabled:
            await callback.answer(
                texts.t('CONTEST_DISABLED', 'Игра отключена.'),
                show_alert=True,
            )
            return

        # Check if user already played
        attempt = await get_attempt(db2, round_id, db_user.id)
        if attempt:
            await callback.answer(
                texts.t('CONTEST_ALREADY_PLAYED', 'У вас уже была попытка в этом раунде.'),
                show_alert=True,
            )
            return

        # Get game strategy and render
        tpl = round_obj.template
        strategy = get_game_strategy(tpl.slug)

        if not strategy:
            await callback.answer(
                texts.t('CONTEST_UNKNOWN', 'Тип конкурса не поддерживается.'),
                show_alert=True,
            )
            return

        render_result = strategy.render(
            round_id=round_obj.id,
            payload=round_obj.payload or {},
            language=db_user.language,
        )

        # For text input games, create pending attempt and set FSM state
        if render_result.requires_text_input:
            await _attempt_service.create_pending_attempt(db2, round_obj.id, db_user.id)
            await state.set_state(ContestStates.waiting_for_answer)
            await state.update_data(contest_round_id=round_obj.id)

        await callback.message.edit_text(
            render_result.text,
            reply_markup=render_result.keyboard,
        )
        await callback.answer()


@auth_required
@error_handler
async def handle_pick(callback: types.CallbackQuery, db_user, db: AsyncSession):
    """Handle button pick in contest games."""
    texts = get_texts(db_user.language)

    # Rate limit check
    if not _check_rate_limit(db_user.id, 'contest_pick', limit=1, window_seconds=3):
        await callback.answer(
            texts.t('CONTEST_TOO_FAST', 'Слишком быстро! Подождите.'),
            show_alert=True,
        )
        return

    # Validate callback data
    parts = _validate_callback_data(callback.data)
    if not parts or len(parts) < 4 or parts[1] != 'pick':
        await callback.answer('Некорректные данные', show_alert=True)
        return

    round_id_str = parts[2]
    pick = '_'.join(parts[3:])

    try:
        round_id = int(round_id_str)
    except ValueError:
        await callback.answer('Некорректные данные', show_alert=True)
        return

    # Re-check subscription
    active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
    # For eligibility: pick best non-daily subscription (most days left)
    non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
    eligible = non_daily or active_subs
    subscription = max(eligible, key=lambda s: s.days_left) if eligible else None
    if not _user_allowed(subscription):
        await callback.answer(
            texts.t('CONTEST_NOT_ELIGIBLE', 'Игра недоступна без активной подписки.'),
            show_alert=True,
        )
        return

    async with AsyncSessionLocal() as db2:
        active_rounds = await get_active_rounds(db2)
        round_obj = next((r for r in active_rounds if r.id == round_id), None)

        if not round_obj:
            await callback.answer(
                texts.t('CONTEST_ROUND_FINISHED', 'Раунд завершён.'),
                show_alert=True,
            )
            return

        # Process attempt using service
        result = await _attempt_service.process_button_attempt(
            db=db2,
            round_obj=round_obj,
            user_id=db_user.id,
            pick=pick,
            language=db_user.language,
        )

        await callback.answer(result.message, show_alert=True)


@auth_required
@error_handler
async def handle_text_answer(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    """Handle text answer in contest games."""
    texts = get_texts(db_user.language)

    data = await state.get_data()
    round_id = data.get('contest_round_id')
    if not round_id:
        await state.clear()
        return

    async with AsyncSessionLocal() as db2:
        active_rounds = await get_active_rounds(db2)
        round_obj = next((r for r in active_rounds if r.id == round_id), None)

        if not round_obj:
            await message.answer(
                texts.t('CONTEST_ROUND_FINISHED', 'Раунд завершён.'),
                reply_markup=get_back_keyboard(db_user.language),
            )
            await state.clear()
            return

        # Process attempt using service
        text_answer = (message.text or '').strip()
        result = await _attempt_service.process_text_attempt(
            db=db2,
            round_obj=round_obj,
            user_id=db_user.id,
            text_answer=text_answer,
            language=db_user.language,
        )

        await message.answer(
            result.message,
            reply_markup=get_back_keyboard(db_user.language),
        )

    await state.clear()


def register_handlers(dp: Dispatcher):
    """Register contest handlers."""
    dp.callback_query.register(show_contests_menu, F.data == 'contests_menu')
    dp.callback_query.register(play_contest, F.data.startswith('contest_play_'))
    dp.callback_query.register(handle_pick, F.data.startswith('contest_pick_'))
    dp.message.register(handle_text_answer, ContestStates.waiting_for_answer)
    dp.message.register(lambda message: None, Command('contests'))  # placeholder
