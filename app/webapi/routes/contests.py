from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.config import settings
from app.localization.texts import get_texts
from app.database.crud.contest import (
    create_round,
    finish_round,
    get_active_round_by_template,
    get_active_rounds,
    get_template_by_id,
    list_templates,
    update_template_fields,
)
from app.database.crud.referral_contest import (
    create_referral_contest,
    delete_referral_contest,
    get_contest_events_count,
    get_contest_leaderboard,
    get_referral_contest,
    get_referral_contests_count,
    list_referral_contests,
    toggle_referral_contest,
    update_referral_contest,
)
from app.database.models import (
    ContestAttempt,
    ContestRound,
    ContestTemplate,
    ReferralContest,
    ReferralContestEvent,
    User,
)
from app.services.contest_rotation_service import contest_rotation_service
from app.webapi.dependencies import get_db_session, require_api_token
from app.webapi.schemas.contests import (
    ContestAttemptListResponse,
    ContestAttemptResponse,
    ContestAttemptUser,
    ContestRoundListResponse,
    ContestRoundResponse,
    ContestTemplateListResponse,
    ContestTemplateResponse,
    ContestTemplateUpdateRequest,
    ReferralContestCreateRequest,
    ReferralContestDetailedStatsResponse,
    ReferralContestDetailResponse,
    ReferralContestEventListResponse,
    ReferralContestEventResponse,
    ReferralContestEventUser,
    ReferralContestLeaderboardItem,
    ReferralContestListResponse,
    ReferralContestResponse,
    ReferralContestUpdateRequest,
    StartRoundRequest,
)


router = APIRouter()


# --------- Helpers ----------


def _to_utc_naive(dt: datetime, tz_name: str | None = None) -> datetime:
    tz = ZoneInfo(tz_name or settings.TIMEZONE)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def _serialize_template(tpl: ContestTemplate) -> ContestTemplateResponse:
    return ContestTemplateResponse(
        id=tpl.id,
        name=tpl.name,
        slug=tpl.slug,
        description=tpl.description,
        prize_type=tpl.prize_type,
        prize_value=tpl.prize_value,
        max_winners=tpl.max_winners,
        attempts_per_user=tpl.attempts_per_user,
        times_per_day=tpl.times_per_day,
        schedule_times=tpl.schedule_times,
        cooldown_hours=tpl.cooldown_hours,
        payload=tpl.payload or {},
        is_enabled=tpl.is_enabled,
        created_at=tpl.created_at,
        updated_at=tpl.updated_at,
    )


def _serialize_round(round_obj: ContestRound) -> ContestRoundResponse:
    tpl = round_obj.template
    return ContestRoundResponse(
        id=round_obj.id,
        template_id=round_obj.template_id,
        template_slug=tpl.slug if tpl else '',
        template_name=tpl.name if tpl else None,
        starts_at=round_obj.starts_at,
        ends_at=round_obj.ends_at,
        status=round_obj.status,
        payload=round_obj.payload or {},
        winners_count=round_obj.winners_count,
        max_winners=round_obj.max_winners,
        attempts_per_user=round_obj.attempts_per_user,
        created_at=round_obj.created_at,
        updated_at=round_obj.updated_at,
    )


def _serialize_attempt(attempt: ContestAttempt, user: User) -> ContestAttemptResponse:
    return ContestAttemptResponse(
        id=attempt.id,
        round_id=attempt.round_id,
        user=ContestAttemptUser(
            id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            full_name=user.full_name,
        ),
        answer=attempt.answer,
        is_winner=attempt.is_winner,
        created_at=attempt.created_at,
    )


def _serialize_referral_contest(contest: ReferralContest) -> ReferralContestResponse:
    return ReferralContestResponse(
        id=contest.id,
        title=contest.title,
        description=contest.description,
        prize_text=contest.prize_text,
        contest_type=contest.contest_type,
        start_at=contest.start_at,
        end_at=contest.end_at,
        daily_summary_time=contest.daily_summary_time,
        daily_summary_times=contest.daily_summary_times,
        timezone=contest.timezone,
        is_active=contest.is_active,
        last_daily_summary_date=contest.last_daily_summary_date,
        last_daily_summary_at=contest.last_daily_summary_at,
        final_summary_sent=contest.final_summary_sent,
        created_by=contest.created_by,
        created_at=contest.created_at,
        updated_at=contest.updated_at,
    )


def _parse_times_str(times_str: str | None) -> list[time]:
    if not times_str:
        return []
    parsed: list[time] = []
    for part in times_str.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            parsed.append(datetime.strptime(part, '%H:%M').time())
        except Exception:
            continue
    return parsed


def _primary_time(times_str: str | None, fallback: time | None) -> time:
    parsed = _parse_times_str(times_str)
    if parsed:
        return parsed[0]
    return fallback or time(hour=12)


def _serialize_leaderboard_item(row) -> ReferralContestLeaderboardItem:
    user, referrals_count, total_amount = row
    total_amount_kopeks = int(total_amount or 0)
    return ReferralContestLeaderboardItem(
        user_id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        full_name=user.full_name,
        referrals_count=int(referrals_count or 0),
        total_amount_kopeks=total_amount_kopeks,
        total_amount_rubles=round(total_amount_kopeks / 100, 2),
    )


def _serialize_event(
    event: ReferralContestEvent,
    referrer: User,
    referral: User,
) -> ReferralContestEventResponse:
    amount_kopeks = int(event.amount_kopeks or 0)
    return ReferralContestEventResponse(
        id=event.id,
        contest_id=event.contest_id,
        referrer=ReferralContestEventUser(
            id=referrer.id,
            telegram_id=referrer.telegram_id,
            username=referrer.username,
            full_name=referrer.full_name,
        ),
        referral=ReferralContestEventUser(
            id=referral.id,
            telegram_id=referral.telegram_id,
            username=referral.username,
            full_name=referral.full_name,
        ),
        event_type=event.event_type,
        amount_kopeks=amount_kopeks,
        amount_rubles=round(amount_kopeks / 100, 2),
        occurred_at=event.occurred_at,
    )


# --------- Daily contests (мини-игры) ----------


@router.get(
    '/daily/templates',
    response_model=ContestTemplateListResponse,
    tags=['contests'],
)
async def list_daily_templates(
    enabled_only: bool = Query(False, description='Показывать только включенные игры'),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ContestTemplateListResponse:
    templates = await list_templates(db, enabled_only=enabled_only)
    return ContestTemplateListResponse(items=[_serialize_template(tpl) for tpl in templates])


@router.get(
    '/daily/templates/{template_id}',
    response_model=ContestTemplateResponse,
    tags=['contests'],
)
async def get_daily_template(
    template_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ContestTemplateResponse:
    texts = get_texts('ru')
    tpl = await get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_TEMPLATE_NOT_FOUND', 'Template not found'),
        )
    return _serialize_template(tpl)


@router.patch(
    '/daily/templates/{template_id}',
    response_model=ContestTemplateResponse,
    tags=['contests'],
)
async def update_daily_template(
    template_id: int,
    payload: ContestTemplateUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ContestTemplateResponse:
    texts = get_texts('ru')
    tpl = await get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_TEMPLATE_NOT_FOUND', 'Template not found'),
        )

    update_fields = payload.model_dump(exclude_none=True)
    if not update_fields:
        return _serialize_template(tpl)

    tpl = await update_template_fields(db, tpl, **update_fields)
    return _serialize_template(tpl)


@router.post(
    '/daily/templates/{template_id}/start-round',
    response_model=ContestRoundResponse,
    status_code=status.HTTP_201_CREATED,
    tags=['contests'],
)
async def start_round_now(
    template_id: int,
    payload: StartRoundRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ContestRoundResponse:
    texts = get_texts('ru')
    tpl = await get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_TEMPLATE_NOT_FOUND', 'Template not found'),
        )

    if not tpl.is_enabled:
        tpl = await update_template_fields(db, tpl, is_enabled=True)

    existing = await get_active_round_by_template(db, tpl.id)
    if existing and not payload.force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            texts.t(
                'WEBAPI_CONTESTS_ACTIVE_ROUND_ALREADY_EXISTS_FORCE_HINT',
                'Active round already exists for this template. Set force=true to start a new one.',
            ),
        )
    if existing and payload.force:
        await finish_round(db, existing)

    starts_at = payload.starts_at or datetime.now(UTC)
    ends_at = payload.ends_at
    if payload.cooldown_hours and not ends_at:
        ends_at = starts_at + timedelta(hours=payload.cooldown_hours)
    elif not ends_at:
        ends_at = starts_at + timedelta(hours=tpl.cooldown_hours)

    starts_at = _to_utc_naive(starts_at, settings.TIMEZONE)
    ends_at = _to_utc_naive(ends_at, settings.TIMEZONE)

    round_payload: dict[str, Any] = payload.payload or contest_rotation_service._build_payload_for_template(tpl)  # type: ignore[attr-defined]

    round_obj = await create_round(
        db,
        template=tpl,
        starts_at=starts_at,
        ends_at=ends_at,
        payload=round_payload,
    )
    round_obj.template = tpl
    await contest_rotation_service._announce_round_start(  # type: ignore[attr-defined]
        tpl,
        starts_at,
        ends_at,
    )
    return _serialize_round(round_obj)


@router.get(
    '/daily/rounds',
    response_model=ContestRoundListResponse,
    tags=['contests'],
)
async def list_rounds(
    status_filter: str = Query('active', pattern='^(active|finished|any)$'),
    template_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ContestRoundListResponse:
    if status_filter == 'active':
        rounds = await get_active_rounds(db)
        if template_id:
            rounds = [r for r in rounds if r.template_id == template_id]
        total = len(rounds)
        items = rounds[offset : offset + limit]
        return ContestRoundListResponse(
            items=[_serialize_round(r) for r in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    query = select(ContestRound).options(selectinload(ContestRound.template)).order_by(ContestRound.starts_at.desc())
    count_query = select(func.count(ContestRound.id))
    if status_filter != 'any':
        query = query.where(ContestRound.status == status_filter)
        count_query = count_query.where(ContestRound.status == status_filter)
    if template_id:
        query = query.where(ContestRound.template_id == template_id)
        count_query = count_query.where(ContestRound.template_id == template_id)

    total = await db.scalar(count_query) or 0
    result = await db.execute(query.offset(offset).limit(limit))
    rounds = result.scalars().unique().all()

    return ContestRoundListResponse(
        items=[_serialize_round(r) for r in rounds],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get(
    '/daily/rounds/{round_id}',
    response_model=ContestRoundResponse,
    tags=['contests'],
)
async def get_round(
    round_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ContestRoundResponse:
    texts = get_texts('ru')
    result = await db.execute(
        select(ContestRound).options(selectinload(ContestRound.template)).where(ContestRound.id == round_id)
    )
    round_obj = result.scalar_one_or_none()
    if not round_obj:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_ROUND_NOT_FOUND', 'Round not found'),
        )
    return _serialize_round(round_obj)


@router.post(
    '/daily/rounds/{round_id}/finish',
    response_model=ContestRoundResponse,
    tags=['contests'],
)
async def finish_round_now(
    round_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ContestRoundResponse:
    texts = get_texts('ru')
    result = await db.execute(
        select(ContestRound).options(selectinload(ContestRound.template)).where(ContestRound.id == round_id)
    )
    round_obj = result.scalar_one_or_none()
    if not round_obj:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_ROUND_NOT_FOUND', 'Round not found'),
        )
    if round_obj.status != 'finished':
        round_obj = await finish_round(db, round_obj)
    return _serialize_round(round_obj)


@router.get(
    '/daily/rounds/{round_id}/attempts',
    response_model=ContestAttemptListResponse,
    tags=['contests'],
)
async def list_attempts(
    round_id: int,
    winners_only: bool = Query(False, description='Вернуть только победителей'),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ContestAttemptListResponse:
    conditions = [ContestAttempt.round_id == round_id]
    if winners_only:
        conditions.append(ContestAttempt.is_winner.is_(True))

    total = await db.scalar(select(func.count(ContestAttempt.id)).where(and_(*conditions))) or 0

    query = (
        select(ContestAttempt, User)
        .join(User, User.id == ContestAttempt.user_id)
        .where(and_(*conditions))
        .order_by(ContestAttempt.created_at)
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(query)
    rows = result.all()

    return ContestAttemptListResponse(
        items=[_serialize_attempt(attempt, user) for attempt, user in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


# --------- Referral contests ----------


@router.get(
    '/referral',
    response_model=ReferralContestListResponse,
    tags=['contests'],
)
async def list_referral(
    contest_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ReferralContestListResponse:
    contests = await list_referral_contests(
        db,
        limit=limit,
        offset=offset,
        contest_type=contest_type,
    )
    total = await get_referral_contests_count(db, contest_type=contest_type)
    return ReferralContestListResponse(
        items=[_serialize_referral_contest(c) for c in contests],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.post(
    '/contests/referral',
    response_model=ReferralContestResponse,
    status_code=status.HTTP_201_CREATED,
    tags=['contests'],
)
async def create_referral(
    payload: ReferralContestCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ReferralContestResponse:
    texts = get_texts('ru')
    start_at = _to_utc_naive(payload.start_at, payload.timezone)
    end_at = _to_utc_naive(payload.end_at, payload.timezone)
    if end_at <= start_at:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_CONTESTS_END_AT_MUST_BE_AFTER_START_AT', 'end_at must be after start_at'),
        )

    summary_time = _primary_time(payload.daily_summary_times, payload.daily_summary_time)

    contest = await create_referral_contest(
        db,
        title=payload.title,
        description=payload.description,
        prize_text=payload.prize_text,
        contest_type=payload.contest_type,
        start_at=start_at,
        end_at=end_at,
        daily_summary_time=summary_time,
        daily_summary_times=payload.daily_summary_times,
        timezone_name=payload.timezone,
        created_by=payload.created_by,
    )

    if payload.is_active is False:
        contest = await toggle_referral_contest(db, contest, False)

    return _serialize_referral_contest(contest)


@router.get(
    '/referral/{contest_id}',
    response_model=ReferralContestDetailResponse,
    tags=['contests'],
)
async def get_referral(
    contest_id: int,
    leaderboard_limit: int = Query(5, ge=1, le=50),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ReferralContestDetailResponse:
    texts = get_texts('ru')
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_CONTEST_NOT_FOUND', 'Contest not found'),
        )

    total_events = await get_contest_events_count(db, contest.id)
    leaderboard_rows = await get_contest_leaderboard(db, contest.id, limit=leaderboard_limit)
    leaderboard = [_serialize_leaderboard_item(row) for row in leaderboard_rows]

    return ReferralContestDetailResponse(
        **_serialize_referral_contest(contest).model_dump(),
        total_events=int(total_events),
        leaderboard=leaderboard,
    )


@router.patch(
    '/referral/{contest_id}',
    response_model=ReferralContestResponse,
    tags=['contests'],
)
async def update_referral(
    contest_id: int,
    payload: ReferralContestUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ReferralContestResponse:
    texts = get_texts('ru')
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_CONTEST_NOT_FOUND', 'Contest not found'),
        )

    fields = payload.model_dump(exclude_none=True)

    if 'start_at' in fields:
        fields['start_at'] = _to_utc_naive(fields['start_at'], fields.get('timezone') or contest.timezone)
    if 'end_at' in fields:
        fields['end_at'] = _to_utc_naive(fields['end_at'], fields.get('timezone') or contest.timezone)
    if 'daily_summary_times' in fields:
        fields['daily_summary_time'] = _primary_time(
            fields['daily_summary_times'], fields.get('daily_summary_time') or contest.daily_summary_time
        )
    elif 'daily_summary_time' in fields:
        # ensure type is time (pydantic provides time)
        pass

    new_start = fields.get('start_at', contest.start_at)
    new_end = fields.get('end_at', contest.end_at)
    if new_end <= new_start:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_CONTESTS_END_AT_MUST_BE_AFTER_START_AT', 'end_at must be after start_at'),
        )

    if fields:
        contest = await update_referral_contest(db, contest, **fields)

    return _serialize_referral_contest(contest)


@router.post(
    '/referral/{contest_id}/toggle',
    response_model=ReferralContestResponse,
    tags=['contests'],
)
async def toggle_referral(
    contest_id: int,
    is_active: bool = Query(..., description='Активировать или остановить конкурс'),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ReferralContestResponse:
    texts = get_texts('ru')
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_CONTEST_NOT_FOUND', 'Contest not found'),
        )
    contest = await toggle_referral_contest(db, contest, is_active)
    return _serialize_referral_contest(contest)


@router.delete(
    '/referral/{contest_id}',
    status_code=status.HTTP_200_OK,
    tags=['contests'],
)
async def delete_referral(
    contest_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    texts = get_texts('ru')
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_CONTEST_NOT_FOUND', 'Contest not found'),
        )
    now_utc = datetime.now(UTC)
    if contest.is_active or contest.end_at > now_utc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_CONTESTS_ONLY_FINISHED_CONTESTS_CAN_BE_DELETED',
                'Можно удалять только завершённые конкурсы',
            ),
        )
    await delete_referral_contest(db, contest)
    return {'status': 'deleted'}


@router.get(
    '/referral/{contest_id}/events',
    response_model=ReferralContestEventListResponse,
    tags=['contests'],
)
async def list_referral_events(
    contest_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ReferralContestEventListResponse:
    texts = get_texts('ru')
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_CONTEST_NOT_FOUND', 'Contest not found'),
        )

    referrer_user = aliased(User)
    referral_user = aliased(User)

    base_conditions = [ReferralContestEvent.contest_id == contest_id]
    total = await db.scalar(select(func.count(ReferralContestEvent.id)).where(and_(*base_conditions))) or 0

    query = (
        select(ReferralContestEvent, referrer_user, referral_user)
        .join(referrer_user, referrer_user.id == ReferralContestEvent.referrer_id)
        .join(referral_user, referral_user.id == ReferralContestEvent.referral_id)
        .where(and_(*base_conditions))
        .order_by(ReferralContestEvent.occurred_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(query)
    rows = result.all()

    return ReferralContestEventListResponse(
        items=[_serialize_event(event, referrer, referral) for event, referrer, referral in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get(
    '/referral/{contest_id}/detailed-stats',
    response_model=ReferralContestDetailedStatsResponse,
    tags=['contests'],
)
async def get_referral_detailed_stats(
    contest_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ReferralContestDetailedStatsResponse:
    texts = get_texts('ru')
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_CONTESTS_CONTEST_NOT_FOUND', 'Contest not found'),
        )

    from app.services.referral_contest_service import referral_contest_service

    stats = await referral_contest_service.get_detailed_contest_stats(db, contest_id)
    return ReferralContestDetailedStatsResponse(**stats)
