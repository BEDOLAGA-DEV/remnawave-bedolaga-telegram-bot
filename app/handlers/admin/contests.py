import logging
import math
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral_contest import (
    add_virtual_participant,
    create_referral_contest,
    delete_referral_contest,
    delete_virtual_participant,
    get_contest_events_count,
    get_contest_leaderboard_with_virtual,
    get_referral_contest,
    get_referral_contests_count,
    list_referral_contests,
    list_virtual_participants,
    toggle_referral_contest,
    update_referral_contest,
    update_virtual_participant_count,
)
from app.keyboards.admin import (
    get_admin_contests_keyboard,
    get_admin_contests_root_keyboard,
    get_admin_pagination_keyboard,
    get_contest_mode_keyboard,
    get_referral_contest_manage_keyboard,
)
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = logging.getLogger(__name__)

PAGE_SIZE = 5


def _ensure_timezone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning('Не удалось загрузить TZ %s, используем UTC', tz_name)
        return ZoneInfo('UTC')


def _format_contest_summary(contest, texts, tz: ZoneInfo) -> str:
    start_local = contest.start_at if contest.start_at.tzinfo else contest.start_at.replace(tzinfo=UTC)
    end_local = contest.end_at if contest.end_at.tzinfo else contest.end_at.replace(tzinfo=UTC)
    start_local = start_local.astimezone(tz)
    end_local = end_local.astimezone(tz)

    status = texts.t('ADMIN_CONTEST_STATUS_ACTIVE') if contest.is_active else texts.t('ADMIN_CONTEST_STATUS_INACTIVE')

    period = f'{start_local.strftime("%d.%m %H:%M")} — {end_local.strftime("%d.%m %H:%M")} ({tz.key})'

    summary_time = contest.daily_summary_time.strftime('%H:%M') if contest.daily_summary_time else '12:00'
    summary_times = contest.daily_summary_times or summary_time
    parts = [
        f'{status}',
        texts.t('ADMIN_CONTEST_PERIOD').format(period=period),
        texts.t('ADMIN_CONTEST_DAILY_SUMMARY').format(summary_times=summary_times),
    ]
    if contest.prize_text:
        parts.append(texts.t('ADMIN_CONTEST_PRIZE').format(prize=contest.prize_text))
    if contest.last_daily_summary_date:
        parts.append(texts.t('ADMIN_CONTEST_LAST_DAILY').format(date=contest.last_daily_summary_date.strftime('%d.%m')))
    return '\n'.join(parts)


def _parse_local_datetime(value: str, tz: ZoneInfo) -> datetime | None:
    try:
        dt = datetime.strptime(value.strip(), '%d.%m.%Y %H:%M')
    except ValueError:
        return None
    return dt.replace(tzinfo=tz)


def _parse_time(value: str):
    try:
        return datetime.strptime(value.strip(), '%H:%M').time()
    except ValueError:
        return None


def _parse_times(value: str) -> list[time]:
    times: list[time] = []
    for part in value.split(','):
        part = part.strip()
        if not part:
            continue
        parsed = _parse_time(part)
        if parsed:
            times.append(parsed)
    return times


def _is_skip_value(value: str, texts) -> bool:
    normalized = (value or '').strip().lower()
    return normalized in {'-', 'skip', texts.t('ADMIN_CONTEST_SKIP_TOKEN').strip().lower()}


def _build_virtual_participants_lines(contest_title: str, participants, texts) -> list[str]:
    lines = [texts.t('ADMIN_CONTEST_VP_TITLE').format(title=contest_title), '']
    if participants:
        for participant in participants:
            lines.append(
                texts.t('ADMIN_CONTEST_VP_ROW').format(
                    name=participant.display_name,
                    referral_count=participant.referral_count,
                )
            )
    else:
        lines.append(texts.t('ADMIN_CONTEST_VP_EMPTY'))
    return lines


def _build_virtual_participants_keyboard(contest_id: int, participants, texts) -> types.InlineKeyboardMarkup:
    rows = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_CONTEST_VP_ADD_BUTTON'),
                callback_data=f'admin_contest_vp_add_{contest_id}',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_CONTEST_VP_MASS_BUTTON'),
                callback_data=f'admin_contest_vp_mass_{contest_id}',
            ),
        ],
    ]

    if participants:
        for participant in participants:
            rows.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_CONTEST_VP_EDIT_BUTTON').format(name=participant.display_name),
                        callback_data=f'admin_contest_vp_edit_{participant.id}',
                    ),
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_CONTEST_VP_DELETE_BUTTON'),
                        callback_data=f'admin_contest_vp_del_{participant.id}',
                    ),
                ]
            )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.BACK,
                callback_data=f'admin_contest_view_{contest_id}',
            )
        ]
    )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@admin_required
@error_handler
async def show_contests_menu(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    if not settings.is_contests_enabled():
        await callback.message.edit_text(
            texts.t('ADMIN_CONTESTS_DISABLED'),
            reply_markup=get_admin_contests_root_keyboard(db_user.language),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        texts.t('ADMIN_CONTESTS_TITLE'),
        reply_markup=get_admin_contests_root_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_referral_contests_menu(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('ADMIN_CONTESTS_TITLE'),
        reply_markup=get_admin_contests_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def list_contests(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_CONTESTS_DISABLED'),
            show_alert=True,
        )
        return

    page = 1
    if callback.data.startswith('admin_contests_list_page_'):
        try:
            page = int(callback.data.split('_')[-1])
        except Exception:
            page = 1

    total = await get_referral_contests_count(db)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PAGE_SIZE

    contests = await list_referral_contests(db, limit=PAGE_SIZE, offset=offset)
    texts = get_texts(db_user.language)

    lines = [texts.t('ADMIN_CONTESTS_LIST_HEADER')]

    if not contests:
        lines.append(texts.t('ADMIN_CONTESTS_EMPTY'))
    else:
        for contest in contests:
            lines.append(f'• <b>{contest.title}</b> (#{contest.id})')
            contest_tz = _ensure_timezone(contest.timezone or settings.TIMEZONE)
            lines.append(_format_contest_summary(contest, texts, contest_tz))
            lines.append('')

    keyboard_rows: list[list[types.InlineKeyboardButton]] = []
    for contest in contests:
        title = contest.title if len(contest.title) <= 25 else contest.title[:22] + '...'
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'🔎 {title}',
                    callback_data=f'admin_contest_view_{contest.id}',
                )
            ]
        )

    pagination = get_admin_pagination_keyboard(
        page,
        total_pages,
        'admin_contests_list',
        back_callback='admin_contests',
        language=db_user.language,
    )
    keyboard_rows.extend(pagination.inline_keyboard)

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )
    await callback.answer()


@admin_required
@error_handler
async def show_contest_details(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_CONTESTS_DISABLED'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    texts = get_texts(db_user.language)

    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND'), show_alert=True)
        return

    tz = _ensure_timezone(contest.timezone or settings.TIMEZONE)
    leaderboard = await get_contest_leaderboard_with_virtual(db, contest.id, limit=5)
    virtual_list = await list_virtual_participants(db, contest.id)
    virtual_count = sum(vp.referral_count for vp in virtual_list)
    total_events = await get_contest_events_count(db, contest.id) + virtual_count

    lines = [
        f'🏆 <b>{contest.title}</b>',
        _format_contest_summary(contest, texts, tz),
        texts.t('ADMIN_CONTEST_TOTAL_EVENTS').format(count=total_events),
    ]

    if contest.description:
        lines.append('')
        lines.append(contest.description)

    if leaderboard:
        lines.append('')
        lines.append(texts.t('ADMIN_CONTEST_LEADERBOARD_TITLE'))
        for idx, (name, score, _, is_virtual) in enumerate(leaderboard, start=1):
            virt_mark = ' 👻' if is_virtual else ''
            lines.append(f'{idx}. {name}{virt_mark} — {score}')

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=get_referral_contest_manage_keyboard(
            contest.id,
            is_active=contest.is_active,
            can_delete=(
                not contest.is_active
                and (contest.end_at.replace(tzinfo=UTC) if contest.end_at.tzinfo is None else contest.end_at)
                < datetime.now(UTC)
            ),
            language=db_user.language,
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_contest(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_CONTESTS_DISABLED'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    texts = get_texts(db_user.language)

    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND'), show_alert=True)
        return

    await toggle_referral_contest(db, contest, not contest.is_active)
    await show_contest_details(callback, db_user, db)


@admin_required
@error_handler
async def prompt_edit_summary_times(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND'), show_alert=True)
        return
    await state.set_state(AdminStates.editing_referral_contest_summary_times)
    await state.update_data(contest_id=contest_id)
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=f'admin_contest_view_{contest_id}',
                )
            ]
        ]
    )
    await callback.message.edit_text(
        texts.t('ADMIN_CONTEST_ENTER_DAILY_TIME'),
        reply_markup=kb,
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_summary_times(
    message: types.Message,
    state: FSMContext,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    contest_id = data.get('contest_id')
    if not contest_id:
        await message.answer(texts.ERROR)
        await state.clear()
        return

    times = _parse_times(message.text or '')
    summary_time = times[0] if times else _parse_time(message.text or '')
    if not summary_time:
        await message.answer(texts.t('ADMIN_CONTEST_INVALID_TIME'))
        await state.clear()
        return

    contest = await get_referral_contest(db, int(contest_id))
    if not contest:
        await message.answer(texts.t('ADMIN_CONTEST_NOT_FOUND'))
        await state.clear()
        return

    await update_referral_contest(
        db,
        contest,
        daily_summary_time=summary_time,
        daily_summary_times=','.join(t.strftime('%H:%M') for t in times) if times else None,
    )

    await message.answer(texts.t('ADMIN_UPDATED'))
    await state.clear()


@admin_required
@error_handler
async def delete_contest(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND'), show_alert=True)
        return

    now_utc = datetime.utcnow()
    if contest.is_active or contest.end_at > now_utc:
        await callback.answer(
            texts.t('ADMIN_CONTEST_DELETE_RESTRICT'),
            show_alert=True,
        )
        return

    await delete_referral_contest(db, contest)
    await callback.answer(texts.t('ADMIN_CONTEST_DELETED'), show_alert=True)
    await list_contests(callback, db_user, db)


@admin_required
@error_handler
async def show_leaderboard(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    if not settings.is_contests_enabled():
        await callback.answer(
            get_texts(db_user.language).t('ADMIN_CONTESTS_DISABLED'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    texts = get_texts(db_user.language)

    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND'), show_alert=True)
        return

    leaderboard = await get_contest_leaderboard_with_virtual(db, contest_id, limit=10)
    if not leaderboard:
        await callback.answer(texts.t('ADMIN_CONTEST_EMPTY_LEADERBOARD'), show_alert=True)
        return

    lines = [
        texts.t('ADMIN_CONTEST_LEADERBOARD_TITLE'),
    ]
    for idx, (name, score, _, is_virtual) in enumerate(leaderboard, start=1):
        virt_mark = ' 👻' if is_virtual else ''
        lines.append(f'{idx}. {name}{virt_mark} — {score}')

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=get_referral_contest_manage_keyboard(
            contest_id, is_active=contest.is_active, language=db_user.language
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_contest_creation(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    if not settings.is_contests_enabled():
        await callback.answer(
            texts.t('ADMIN_CONTESTS_DISABLED'),
            show_alert=True,
        )
        return

    await state.clear()
    await state.set_state(AdminStates.creating_referral_contest_mode)
    await callback.message.edit_text(
        texts.t('ADMIN_CONTEST_MODE_PROMPT'),
        reply_markup=get_contest_mode_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def select_contest_mode(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    mode = 'referral_paid' if callback.data == 'admin_contest_mode_paid' else 'referral_registered'
    await state.update_data(contest_type=mode)
    await state.set_state(AdminStates.creating_referral_contest_title)
    await callback.message.edit_text(
        texts.t('ADMIN_CONTEST_ENTER_TITLE'),
        reply_markup=None,
    )
    await callback.answer()


@admin_required
@error_handler
async def process_title(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    title = message.text.strip()
    texts = get_texts(db_user.language)

    await state.update_data(title=title)
    await state.set_state(AdminStates.creating_referral_contest_description)
    await message.answer(texts.t('ADMIN_CONTEST_ENTER_DESCRIPTION'))


@admin_required
@error_handler
async def process_description(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    description = message.text.strip()
    texts = get_texts(db_user.language)
    if _is_skip_value(description, texts):
        description = None

    await state.update_data(description=description)
    await state.set_state(AdminStates.creating_referral_contest_prize)
    await message.answer(texts.t('ADMIN_CONTEST_ENTER_PRIZE'))


@admin_required
@error_handler
async def process_prize(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    prize = message.text.strip()
    texts = get_texts(db_user.language)
    if _is_skip_value(prize, texts):
        prize = None

    await state.update_data(prize=prize)
    await state.set_state(AdminStates.creating_referral_contest_start)
    await message.answer(texts.t('ADMIN_CONTEST_ENTER_START'))


@admin_required
@error_handler
async def process_start_date(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    tz = _ensure_timezone(settings.TIMEZONE)
    start_dt = _parse_local_datetime(message.text, tz)
    texts = get_texts(db_user.language)

    if not start_dt:
        await message.answer(texts.t('ADMIN_CONTEST_INVALID_DATE'))
        return

    await state.update_data(start_at=start_dt.isoformat())
    await state.set_state(AdminStates.creating_referral_contest_end)
    await message.answer(texts.t('ADMIN_CONTEST_ENTER_END'))


@admin_required
@error_handler
async def process_end_date(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    tz = _ensure_timezone(settings.TIMEZONE)
    end_dt = _parse_local_datetime(message.text, tz)
    texts = get_texts(db_user.language)

    if not end_dt:
        await message.answer(texts.t('ADMIN_CONTEST_INVALID_DATE'))
        return

    data = await state.get_data()
    start_raw = data.get('start_at')
    start_dt = datetime.fromisoformat(start_raw) if start_raw else None
    if start_dt and end_dt <= start_dt:
        await message.answer(texts.t('ADMIN_CONTEST_END_BEFORE_START'))
        return

    await state.update_data(end_at=end_dt.isoformat())
    await state.set_state(AdminStates.creating_referral_contest_time)
    await message.answer(texts.t('ADMIN_CONTEST_ENTER_DAILY_TIME'))


@admin_required
@error_handler
async def finalize_contest_creation(message: types.Message, state: FSMContext, db_user, db: AsyncSession):
    times = _parse_times(message.text or '')
    summary_time = times[0] if times else _parse_time(message.text)
    texts = get_texts(db_user.language)

    if not summary_time:
        await message.answer(texts.t('ADMIN_CONTEST_INVALID_TIME'))
        return

    data = await state.get_data()
    tz = _ensure_timezone(settings.TIMEZONE)

    start_at_raw = data.get('start_at')
    end_at_raw = data.get('end_at')
    if not start_at_raw or not end_at_raw:
        await message.answer(texts.t('ADMIN_CONTEST_INVALID_DATE'))
        return

    start_at = datetime.fromisoformat(start_at_raw).astimezone(UTC).replace(tzinfo=None)
    end_at = datetime.fromisoformat(end_at_raw).astimezone(UTC).replace(tzinfo=None)

    contest_type = data.get('contest_type') or 'referral_paid'

    contest = await create_referral_contest(
        db,
        title=data.get('title'),
        description=data.get('description'),
        prize_text=data.get('prize'),
        contest_type=contest_type,
        start_at=start_at,
        end_at=end_at,
        daily_summary_time=summary_time,
        daily_summary_times=','.join(t.strftime('%H:%M') for t in times) if times else None,
        timezone_name=tz.key,
        created_by=db_user.id,
    )

    await state.clear()

    await message.answer(
        texts.t('ADMIN_CONTEST_CREATED'),
        reply_markup=get_referral_contest_manage_keyboard(
            contest.id,
            is_active=contest.is_active,
            language=db_user.language,
        ),
    )


@admin_required
@error_handler
async def show_detailed_stats(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    if not settings.is_contests_enabled():
        await callback.answer(
            texts.t('ADMIN_CONTESTS_DISABLED'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)

    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND'), show_alert=True)
        return

    from app.services.referral_contest_service import referral_contest_service

    stats = await referral_contest_service.get_detailed_contest_stats(db, contest_id)
    virtual = await list_virtual_participants(db, contest_id)
    virtual_count = len(virtual)
    virtual_referrals = sum(vp.referral_count for vp in virtual)

    general_lines = [
        texts.t('ADMIN_CONTEST_STATS_TITLE'),
        texts.t('ADMIN_CONTEST_STATS_CONTEST_NAME').format(title=contest.title),
        '',
        texts.t('ADMIN_CONTEST_STATS_PARTICIPANTS').format(count=stats['total_participants']),
        texts.t('ADMIN_CONTEST_STATS_INVITED').format(count=stats['total_invited']),
        '',
        texts.t('ADMIN_CONTEST_STATS_PAID').format(count=stats.get('paid_count', 0)),
        texts.t('ADMIN_CONTEST_STATS_UNPAID').format(count=stats.get('unpaid_count', 0)),
        '',
        texts.t('ADMIN_CONTEST_STATS_SUMS_TITLE'),
        texts.t('ADMIN_CONTEST_STATS_SUBSCRIPTIONS').format(amount=stats.get('subscription_total', 0) // 100),
        texts.t('ADMIN_CONTEST_STATS_DEPOSITS').format(amount=stats.get('deposit_total', 0) // 100),
    ]

    if virtual_count > 0:
        general_lines.append('')
        general_lines.append(
            texts.t('ADMIN_CONTEST_STATS_VIRTUAL').format(count=virtual_count, referrals=virtual_referrals)
        )

    await callback.message.edit_text(
        '\n'.join(general_lines),
        reply_markup=get_referral_contest_manage_keyboard(
            contest_id, is_active=contest.is_active, language=db_user.language
        ),
    )

    await callback.answer()


@admin_required
@error_handler
async def show_detailed_stats_page(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    contest_id: int = None,
    page: int = 1,
    stats: dict = None,
):
    texts = get_texts(db_user.language)
    if contest_id is None or stats is None:
        parts = callback.data.split('_')
        contest_id = int(parts[5])
        page = int(parts[7])

        from app.services.referral_contest_service import referral_contest_service

        stats = await referral_contest_service.get_detailed_contest_stats(db, contest_id)

    participants = stats['participants']
    total_participants = len(participants)
    PAGE_SIZE = 10
    total_pages = math.ceil(total_participants / PAGE_SIZE)

    page = max(1, min(page, total_pages))
    offset = (page - 1) * PAGE_SIZE
    page_participants = participants[offset : offset + PAGE_SIZE]

    lines = [texts.t('ADMIN_CONTEST_PARTICIPANTS_PAGE_TITLE').format(page=page, total_pages=total_pages)]
    for p in page_participants:
        lines.extend(
            [
                f'• <b>{p["full_name"]}</b>',
                texts.t('ADMIN_CONTEST_PARTICIPANTS_PAGE_INVITED').format(count=p['total_referrals']),
                texts.t('ADMIN_CONTEST_PARTICIPANTS_PAGE_PAID').format(count=p['paid_referrals']),
                texts.t('ADMIN_CONTEST_PARTICIPANTS_PAGE_UNPAID').format(count=p['unpaid_referrals']),
                texts.t('ADMIN_CONTEST_PARTICIPANTS_PAGE_AMOUNT').format(amount=p['total_paid_amount'] // 100),
                '',
            ]
        )

    pagination = get_admin_pagination_keyboard(
        page,
        total_pages,
        f'admin_contest_detailed_stats_page_{contest_id}',
        back_callback=f'admin_contest_view_{contest_id}',
        language=db_user.language,
    )

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=pagination,
    )

    await callback.answer()


@admin_required
@error_handler
async def sync_contest(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    if not settings.is_contests_enabled():
        await callback.answer(
            texts.t('ADMIN_CONTESTS_DISABLED'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)

    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND'), show_alert=True)
        return

    await callback.answer(texts.t('ADMIN_CONTEST_SYNC_STARTED'), show_alert=False)

    from app.services.referral_contest_service import referral_contest_service

    cleanup_stats = await referral_contest_service.cleanup_contest(db, contest_id)

    if 'error' in cleanup_stats:
        await callback.message.answer(
            texts.t('ADMIN_CONTEST_SYNC_CLEANUP_ERROR').format(error=cleanup_stats['error']),
        )
        return

    stats = await referral_contest_service.sync_contest(db, contest_id)

    if 'error' in stats:
        await callback.message.answer(
            texts.t('ADMIN_CONTEST_SYNC_ERROR').format(error=stats['error']),
        )
        return

    start_str = stats.get('contest_start', contest.start_at.isoformat())
    end_str = stats.get('contest_end', contest.end_at.isoformat())
    period_start = contest.start_at.strftime('%d.%m.%Y')
    period_end = contest.end_at.strftime('%d.%m.%Y')

    lines = [
        texts.t('ADMIN_CONTEST_SYNC_DONE'),
        '',
        texts.t('ADMIN_CONTEST_SYNC_CONTEST').format(title=contest.title),
        texts.t('ADMIN_CONTEST_SYNC_PERIOD').format(start=period_start, end=period_end),
        texts.t('ADMIN_CONTEST_SYNC_FILTER_TITLE'),
        f'   <code>{start_str}</code>',
        f'   <code>{end_str}</code>',
        '',
        texts.t('ADMIN_CONTEST_SYNC_CLEANUP_TITLE'),
        texts.t('ADMIN_CONTEST_SYNC_CLEANUP_DELETED').format(count=cleanup_stats.get('deleted', 0)),
        texts.t('ADMIN_CONTEST_SYNC_CLEANUP_REMAINING').format(count=cleanup_stats.get('remaining', 0)),
        texts.t('ADMIN_CONTEST_SYNC_CLEANUP_TOTAL_BEFORE').format(count=cleanup_stats.get('total_before', 0)),
        '',
        texts.t('ADMIN_CONTEST_SYNC_STATS_TITLE'),
        texts.t('ADMIN_CONTEST_SYNC_STATS_TOTAL_EVENTS').format(count=stats.get('total_events', 0)),
        texts.t('ADMIN_CONTEST_SYNC_STATS_FILTERED').format(count=stats.get('filtered_out_events', 0)),
        texts.t('ADMIN_CONTEST_SYNC_STATS_UPDATED').format(count=stats.get('updated', 0)),
        texts.t('ADMIN_CONTEST_SYNC_STATS_SKIPPED').format(count=stats.get('skipped', 0)),
        '',
        texts.t('ADMIN_CONTEST_STATS_PAID').format(count=stats.get('paid_count', 0)),
        texts.t('ADMIN_CONTEST_STATS_UNPAID').format(count=stats.get('unpaid_count', 0)),
        '',
        texts.t('ADMIN_CONTEST_STATS_SUMS_TITLE'),
        texts.t('ADMIN_CONTEST_STATS_SUBSCRIPTIONS').format(amount=stats.get('subscription_total', 0) // 100),
        texts.t('ADMIN_CONTEST_STATS_DEPOSITS').format(amount=stats.get('deposit_total', 0) // 100),
    ]

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    back_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_CONTEST_BACK_TO_CONTEST'), callback_data=f'admin_contest_view_{contest_id}'
                )
            ]
        ]
    )

    await callback.message.answer(
        '\n'.join(lines),
        parse_mode='HTML',
        reply_markup=back_keyboard,
    )

    detailed_stats = await referral_contest_service.get_detailed_contest_stats(db, contest_id)
    general_lines = [
        f'🏆 <b>{contest.title}</b>',
        texts.t('ADMIN_CONTEST_PERIOD_SHORT').format(start=period_start, end=period_end),
        '',
        texts.t('ADMIN_CONTEST_STATS_PARTICIPANTS').format(count=detailed_stats['total_participants']),
        texts.t('ADMIN_CONTEST_STATS_INVITED').format(count=detailed_stats['total_invited']),
        '',
        texts.t('ADMIN_CONTEST_STATS_PAID').format(count=detailed_stats.get('paid_count', 0)),
        texts.t('ADMIN_CONTEST_STATS_UNPAID').format(count=detailed_stats.get('unpaid_count', 0)),
        texts.t('ADMIN_CONTEST_STATS_SUBSCRIPTIONS').format(amount=detailed_stats['total_paid_amount'] // 100),
    ]

    await callback.message.edit_text(
        '\n'.join(general_lines),
        reply_markup=get_referral_contest_manage_keyboard(
            contest_id, is_active=contest.is_active, language=db_user.language
        ),
    )


@admin_required
@error_handler
async def debug_contest_transactions(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    if not settings.is_contests_enabled():
        await callback.answer(
            texts.t('ADMIN_CONTESTS_DISABLED'),
            show_alert=True,
        )
        return

    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)

    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND'), show_alert=True)
        return

    await callback.answer(texts.t('ADMIN_CONTEST_DEBUG_LOADING'), show_alert=False)

    from app.database.crud.referral_contest import debug_contest_transactions as debug_txs

    debug_data = await debug_txs(db, contest_id, limit=10)

    if 'error' in debug_data:
        await callback.message.answer(texts.t('ADMIN_CONTEST_DEBUG_ERROR').format(error=debug_data['error']))
        return

    deposit_total = debug_data.get('deposit_total_kopeks', 0) // 100
    subscription_total = debug_data.get('subscription_total_kopeks', 0) // 100

    lines = [
        texts.t('ADMIN_CONTEST_DEBUG_TITLE'),
        '',
        texts.t('ADMIN_CONTEST_SYNC_CONTEST').format(title=contest.title),
        texts.t('ADMIN_CONTEST_DEBUG_PERIOD_TITLE'),
        texts.t('ADMIN_CONTEST_DEBUG_PERIOD_START').format(value=debug_data.get('contest_start')),
        texts.t('ADMIN_CONTEST_DEBUG_PERIOD_END').format(value=debug_data.get('contest_end')),
        texts.t('ADMIN_CONTEST_DEBUG_REFERRALS_IN_PERIOD').format(count=debug_data.get('referral_count', 0)),
        texts.t('ADMIN_CONTEST_DEBUG_FILTERED_OUT').format(count=debug_data.get('filtered_out', 0)),
        texts.t('ADMIN_CONTEST_DEBUG_TOTAL_EVENTS').format(count=debug_data.get('total_all_events', 0)),
        '',
        texts.t('ADMIN_CONTEST_STATS_SUMS_TITLE'),
        texts.t('ADMIN_CONTEST_STATS_DEPOSITS').format(amount=deposit_total),
        texts.t('ADMIN_CONTEST_STATS_SUBSCRIPTIONS').format(amount=subscription_total),
        '',
    ]

    txs_in = debug_data.get('transactions_in_period', [])
    if txs_in:
        lines.append(texts.t('ADMIN_CONTEST_DEBUG_IN_PERIOD').format(count=len(txs_in)))
        for tx in txs_in[:5]:
            lines.append(
                f'  • {tx["created_at"][:10]} | {tx["type"]} | {tx["amount_kopeks"] // 100}₽ | user={tx["user_id"]}'
            )
        if len(txs_in) > 5:
            lines.append(texts.t('ADMIN_CONTEST_DEBUG_MORE').format(count=len(txs_in) - 5))
    else:
        lines.append(texts.t('ADMIN_CONTEST_DEBUG_IN_PERIOD_EMPTY'))

    lines.append('')

    txs_out = debug_data.get('transactions_outside_period', [])
    if txs_out:
        lines.append(texts.t('ADMIN_CONTEST_DEBUG_OUT_PERIOD').format(count=len(txs_out)))
        for tx in txs_out[:5]:
            lines.append(
                f'  • {tx["created_at"][:10]} | {tx["type"]} | {tx["amount_kopeks"] // 100}₽ | user={tx["user_id"]}'
            )
        if len(txs_out) > 5:
            lines.append(texts.t('ADMIN_CONTEST_DEBUG_MORE').format(count=len(txs_out) - 5))
    else:
        lines.append(texts.t('ADMIN_CONTEST_DEBUG_OUT_PERIOD_EMPTY'))

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    back_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_CONTEST_BACK_TO_CONTEST'), callback_data=f'admin_contest_view_{contest_id}'
                )
            ]
        ]
    )

    await callback.message.answer(
        '\n'.join(lines),
        parse_mode='HTML',
        reply_markup=back_keyboard,
    )


# ── Виртуальные участники ──────────────────────────────────────────────


@admin_required
@error_handler
async def show_virtual_participants(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    contest_id = int(callback.data.split('_')[-1])
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        await callback.answer(texts.t('ADMIN_CONTEST_NOT_FOUND'), show_alert=True)
        return

    vps = await list_virtual_participants(db, contest_id)

    await callback.message.edit_text(
        '\n'.join(_build_virtual_participants_lines(contest.title, vps, texts)),
        reply_markup=_build_virtual_participants_keyboard(contest_id, vps, texts),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_add_virtual_participant(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    contest_id = int(callback.data.split('_')[-1])
    await state.set_state(AdminStates.adding_virtual_participant_name)
    await state.update_data(vp_contest_id=contest_id)
    await callback.message.edit_text(
        texts.t('ADMIN_CONTEST_VP_ENTER_NAME'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_contest_vp_{contest_id}')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_virtual_participant_name(
    message: types.Message,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    name = message.text.strip()
    if not name or len(name) > 200:
        await message.answer(texts.t('ADMIN_CONTEST_VP_NAME_INVALID'))
        return
    await state.update_data(vp_name=name)
    await state.set_state(AdminStates.adding_virtual_participant_count)
    await message.answer(texts.t('ADMIN_CONTEST_VP_ENTER_REFERRALS').format(name=name))


@admin_required
@error_handler
async def process_virtual_participant_count(
    message: types.Message,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    try:
        count = int(message.text.strip())
        if count < 1:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer(texts.t('ADMIN_CONTEST_VP_POSITIVE_INTEGER'))
        return

    data = await state.get_data()
    contest_id = data['vp_contest_id']
    display_name = data['vp_name']
    await state.clear()

    vp = await add_virtual_participant(db, contest_id, display_name, count)
    await message.answer(
        texts.t('ADMIN_CONTEST_VP_ADDED').format(name=vp.display_name, referral_count=vp.referral_count),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_CONTEST_VP_TO_LIST'),
                        callback_data=f'admin_contest_vp_{contest_id}',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_CONTEST_VP_TO_CONTEST'),
                        callback_data=f'admin_contest_view_{contest_id}',
                    )
                ],
            ]
        ),
    )


@admin_required
@error_handler
async def delete_virtual_participant_handler(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    vp_id = int(callback.data.split('_')[-1])

    # Получим contest_id до удаления
    from sqlalchemy import select as sa_select

    from app.database.models import ReferralContestVirtualParticipant

    result = await db.execute(
        sa_select(ReferralContestVirtualParticipant).where(ReferralContestVirtualParticipant.id == vp_id)
    )
    vp = result.scalar_one_or_none()
    if not vp:
        await callback.answer(texts.t('ADMIN_CONTEST_VP_NOT_FOUND'), show_alert=True)
        return

    contest_id = vp.contest_id
    deleted = await delete_virtual_participant(db, vp_id)
    if deleted:
        await callback.answer(texts.t('ADMIN_CONTEST_VP_DELETED'), show_alert=False)
    else:
        await callback.answer(texts.t('ADMIN_CONTEST_VP_DELETE_FAILED'), show_alert=True)

    vps = await list_virtual_participants(db, contest_id)
    contest = await get_referral_contest(db, contest_id)

    await callback.message.edit_text(
        '\n'.join(_build_virtual_participants_lines(contest.title, vps, texts)),
        reply_markup=_build_virtual_participants_keyboard(contest_id, vps, texts),
    )


@admin_required
@error_handler
async def start_mass_virtual_participants(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    contest_id = int(callback.data.split('_')[-1])
    await state.set_state(AdminStates.adding_mass_virtual_count)
    await state.update_data(mass_vp_contest_id=contest_id)

    await callback.message.edit_text(
        texts.t('ADMIN_CONTEST_VP_MASS_HELP'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_contest_vp_{contest_id}')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_mass_virtual_count(
    message: types.Message,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    try:
        count = int(message.text.strip())
        if count < 1 or count > 50:
            await message.answer(
                texts.t('ADMIN_CONTEST_VP_MASS_COUNT_INVALID'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_contests_ref')],
                    ]
                ),
            )
            return
    except ValueError:
        await message.answer(
            texts.t('ADMIN_CONTEST_VP_MASS_COUNT_INVALID_FORMAT'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_contests_ref')],
                ]
            ),
        )
        return

    await state.update_data(mass_vp_count=count)
    await state.set_state(AdminStates.adding_mass_virtual_referrals)

    data = await state.get_data()
    contest_id = data.get('mass_vp_contest_id')

    await message.answer(
        texts.t('ADMIN_CONTEST_VP_MASS_COUNT_SET').format(count=count),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_contest_vp_{contest_id}')],
            ]
        ),
    )


@admin_required
@error_handler
async def process_mass_virtual_referrals(
    message: types.Message,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    import random
    import string

    try:
        referrals_count = int(message.text.strip())
        if referrals_count < 1 or referrals_count > 100:
            await message.answer(texts.t('ADMIN_CONTEST_VP_MASS_REFERRALS_INVALID'))
            return
    except ValueError:
        await message.answer(texts.t('ADMIN_CONTEST_VP_MASS_REFERRALS_INVALID_FORMAT'))
        return

    data = await state.get_data()
    contest_id = data.get('mass_vp_contest_id')
    ghost_count = data.get('mass_vp_count', 1)

    await state.clear()

    created = []
    for _ in range(ghost_count):
        name_length = random.randint(3, 5)
        name = ''.join(random.choices(string.ascii_letters + string.digits, k=name_length))

        vp = await add_virtual_participant(db, contest_id, name, referrals_count)
        created.append(vp)

    text = texts.t('ADMIN_CONTEST_VP_MASS_RESULT').format(
        created=len(created),
        referrals_count=referrals_count,
        total_referrals=len(created) * referrals_count,
    )
    for vp in created[:10]:
        text += f'{texts.t("ADMIN_CONTEST_VP_ROW").format(name=vp.display_name, referral_count=vp.referral_count)}\n'

    if len(created) > 10:
        text += f'{texts.t("ADMIN_CONTEST_VP_MASS_MORE").format(count=len(created) - 10)}\n'

    await message.answer(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_CONTEST_VP_TO_GHOSTS_LIST'),
                        callback_data=f'admin_contest_vp_{contest_id}',
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_CONTEST_VP_TO_CONTEST'),
                        callback_data=f'admin_contest_view_{contest_id}',
                    )
                ],
            ]
        ),
    )


@admin_required
@error_handler
async def start_edit_virtual_participant(
    callback: types.CallbackQuery,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    vp_id = int(callback.data.split('_')[-1])

    from sqlalchemy import select as sa_select

    from app.database.models import ReferralContestVirtualParticipant

    result = await db.execute(
        sa_select(ReferralContestVirtualParticipant).where(ReferralContestVirtualParticipant.id == vp_id)
    )
    vp = result.scalar_one_or_none()
    if not vp:
        await callback.answer(texts.t('ADMIN_CONTEST_VP_NOT_FOUND'), show_alert=True)
        return

    await state.set_state(AdminStates.editing_virtual_participant_count)
    await state.update_data(vp_edit_id=vp_id, vp_edit_contest_id=vp.contest_id)
    await callback.message.edit_text(
        texts.t('ADMIN_CONTEST_VP_EDIT_PROMPT').format(name=vp.display_name, referral_count=vp.referral_count),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_contest_vp_{vp.contest_id}')],
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_virtual_participant_count(
    message: types.Message,
    db_user,
    db: AsyncSession,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    try:
        count = int(message.text.strip())
        if count < 1:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer(texts.t('ADMIN_CONTEST_VP_POSITIVE_INTEGER'))
        return

    data = await state.get_data()
    vp_id = data['vp_edit_id']
    contest_id = data['vp_edit_contest_id']
    await state.clear()

    vp = await update_virtual_participant_count(db, vp_id, count)
    if vp:
        await message.answer(
            texts.t('ADMIN_CONTEST_VP_UPDATED').format(name=vp.display_name, referral_count=vp.referral_count),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_CONTEST_VP_TO_LIST'),
                            callback_data=f'admin_contest_vp_{contest_id}',
                        )
                    ],
                ]
            ),
        )
    else:
        await message.answer(texts.t('ADMIN_CONTEST_VP_NOT_FOUND'))


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_contests_menu, F.data == 'admin_contests')
    dp.callback_query.register(show_referral_contests_menu, F.data == 'admin_contests_referral')
    dp.callback_query.register(list_contests, F.data == 'admin_contests_list')
    dp.callback_query.register(list_contests, F.data.startswith('admin_contests_list_page_'))
    dp.callback_query.register(show_contest_details, F.data.startswith('admin_contest_view_'))
    dp.callback_query.register(toggle_contest, F.data.startswith('admin_contest_toggle_'))
    dp.callback_query.register(prompt_edit_summary_times, F.data.startswith('admin_contest_edit_times_'))
    dp.callback_query.register(delete_contest, F.data.startswith('admin_contest_delete_'))
    dp.callback_query.register(show_leaderboard, F.data.startswith('admin_contest_leaderboard_'))
    dp.callback_query.register(show_detailed_stats, F.data.startswith('admin_contest_detailed_stats_'))
    dp.callback_query.register(show_detailed_stats_page, F.data.startswith('admin_contest_detailed_stats_page_'))
    dp.callback_query.register(sync_contest, F.data.startswith('admin_contest_sync_'))
    dp.callback_query.register(debug_contest_transactions, F.data.startswith('admin_contest_debug_'))
    dp.callback_query.register(start_contest_creation, F.data == 'admin_contests_create')
    dp.callback_query.register(
        select_contest_mode, F.data.in_(['admin_contest_mode_paid', 'admin_contest_mode_registered'])
    )

    dp.message.register(process_title, AdminStates.creating_referral_contest_title)
    dp.message.register(process_description, AdminStates.creating_referral_contest_description)
    dp.message.register(process_prize, AdminStates.creating_referral_contest_prize)
    dp.message.register(process_start_date, AdminStates.creating_referral_contest_start)
    dp.message.register(process_end_date, AdminStates.creating_referral_contest_end)
    dp.message.register(finalize_contest_creation, AdminStates.creating_referral_contest_time)
    dp.message.register(process_edit_summary_times, AdminStates.editing_referral_contest_summary_times)

    dp.callback_query.register(start_add_virtual_participant, F.data.startswith('admin_contest_vp_add_'))
    dp.callback_query.register(delete_virtual_participant_handler, F.data.startswith('admin_contest_vp_del_'))
    dp.callback_query.register(start_edit_virtual_participant, F.data.startswith('admin_contest_vp_edit_'))
    dp.callback_query.register(start_mass_virtual_participants, F.data.startswith('admin_contest_vp_mass_'))
    dp.callback_query.register(show_virtual_participants, F.data.regexp(r'^admin_contest_vp_\d+$'))
    dp.message.register(process_virtual_participant_name, AdminStates.adding_virtual_participant_name)
    dp.message.register(process_virtual_participant_count, AdminStates.adding_virtual_participant_count)
    dp.message.register(process_edit_virtual_participant_count, AdminStates.editing_virtual_participant_count)
    dp.message.register(process_mass_virtual_count, AdminStates.adding_mass_virtual_count)
    dp.message.register(process_mass_virtual_referrals, AdminStates.adding_mass_virtual_referrals)
