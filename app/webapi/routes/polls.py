from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    Security,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.localization.texts import get_texts
from app.database.crud.poll import (
    create_poll,
    delete_poll as delete_poll_record,
    get_poll_by_id,
    get_poll_responses_with_answers,
    get_poll_statistics,
)
from app.database.models import Poll, PollAnswer, PollOption, PollQuestion, PollResponse
from app.handlers.admin.messages import get_custom_users, get_target_users
from app.services.poll_service import send_poll_to_users

from ..dependencies import get_db_session, require_api_token
from ..schemas.polls import (
    PollAnswerResponse,
    PollCreateRequest,
    PollDetailResponse,
    PollListResponse,
    PollOptionStats,
    PollQuestionOptionResponse,
    PollQuestionResponse,
    PollQuestionStats,
    PollResponsesListResponse,
    PollSendRequest,
    PollSendResponse,
    PollStatisticsResponse,
    PollSummaryResponse,
    PollUserResponse,
)


router = APIRouter()


def _format_price(kopeks: int) -> float:
    return round(kopeks / 100, 2)


def _serialize_option(option: PollOption) -> PollQuestionOptionResponse:
    return PollQuestionOptionResponse(
        id=option.id,
        text=option.text,
        order=option.order,
    )


def _serialize_question(question: PollQuestion) -> PollQuestionResponse:
    options = [_serialize_option(option) for option in sorted(question.options, key=lambda item: item.order)]
    return PollQuestionResponse(
        id=question.id,
        text=question.text,
        order=question.order,
        options=options,
    )


def _serialize_poll_summary(
    poll: Poll,
    responses_count: int | None = None,
) -> PollSummaryResponse:
    questions = getattr(poll, 'questions', [])
    if responses_count is None:
        responses = getattr(poll, 'responses', [])
        responses_count = len(responses)
    return PollSummaryResponse(
        id=poll.id,
        title=poll.title,
        description=poll.description,
        reward_enabled=poll.reward_enabled,
        reward_amount_kopeks=poll.reward_amount_kopeks,
        reward_amount_rubles=_format_price(poll.reward_amount_kopeks),
        questions_count=len(questions),
        responses_count=responses_count,
        created_at=poll.created_at,
        updated_at=poll.updated_at,
    )


def _serialize_poll_detail(poll: Poll) -> PollDetailResponse:
    questions = [_serialize_question(question) for question in sorted(poll.questions, key=lambda item: item.order)]
    return PollDetailResponse(
        id=poll.id,
        title=poll.title,
        description=poll.description,
        reward_enabled=poll.reward_enabled,
        reward_amount_kopeks=poll.reward_amount_kopeks,
        reward_amount_rubles=_format_price(poll.reward_amount_kopeks),
        questions=questions,
        created_at=poll.created_at,
        updated_at=poll.updated_at,
    )


def _serialize_answer(answer: PollAnswer) -> PollAnswerResponse:
    question = getattr(answer, 'question', None)
    option = getattr(answer, 'option', None)
    return PollAnswerResponse(
        question_id=question.id if question else answer.question_id,
        question_text=question.text if question else None,
        option_id=option.id if option else answer.option_id,
        option_text=option.text if option else None,
        created_at=answer.created_at,
    )


def _serialize_user_response(response: PollResponse) -> PollUserResponse:
    user = getattr(response, 'user', None)
    answers = [_serialize_answer(answer) for answer in sorted(response.answers, key=lambda item: item.created_at)]
    return PollUserResponse(
        id=response.id,
        user_id=getattr(user, 'id', None),
        user_telegram_id=getattr(user, 'telegram_id', None),
        user_username=getattr(user, 'username', None),
        sent_at=response.sent_at,
        started_at=response.started_at,
        completed_at=response.completed_at,
        reward_given=response.reward_given,
        reward_amount_kopeks=response.reward_amount_kopeks,
        reward_amount_rubles=_format_price(response.reward_amount_kopeks),
        answers=answers,
    )


@router.get('', response_model=PollListResponse)
async def list_polls(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PollListResponse:
    total_result = await db.execute(select(func.count()).select_from(Poll))
    total = int(total_result.scalar_one() or 0)

    if total == 0:
        return PollListResponse(items=[], total=0, limit=limit, offset=offset)

    result = await db.execute(
        select(Poll).options(selectinload(Poll.questions)).order_by(Poll.created_at.desc()).offset(offset).limit(limit)
    )
    polls = result.scalars().unique().all()

    if not polls:
        return PollListResponse(items=[], total=total, limit=limit, offset=offset)

    poll_ids = [poll.id for poll in polls]
    counts_result = await db.execute(
        select(PollResponse.poll_id, func.count(PollResponse.id))
        .where(PollResponse.poll_id.in_(poll_ids))
        .group_by(PollResponse.poll_id)
    )
    responses_counts = {poll_id: count for poll_id, count in counts_result.all()}

    return PollListResponse(
        items=[_serialize_poll_summary(poll, responses_counts.get(poll.id, 0)) for poll in polls],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get('/{poll_id}', response_model=PollDetailResponse)
async def get_poll(
    poll_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PollDetailResponse:
    poll = await get_poll_by_id(db, poll_id)
    if not poll:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('POLL_NOT_FOUND', 'Poll not found'),
        )

    return _serialize_poll_detail(poll)


@router.post('', response_model=PollDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_poll_endpoint(
    payload: PollCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PollDetailResponse:
    poll = await create_poll(
        db,
        title=payload.title,
        description=payload.description,
        reward_enabled=payload.reward_enabled,
        reward_amount_kopeks=payload.reward_amount_kopeks,
        created_by=None,
        questions=[
            {
                'text': question.text,
                'options': [option.text for option in question.options],
            }
            for question in payload.questions
        ],
    )

    poll = await get_poll_by_id(db, poll.id)
    return _serialize_poll_detail(poll)


@router.delete('/{poll_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_poll(
    poll_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    success = await delete_poll_record(db, poll_id)
    if not success:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('POLL_NOT_FOUND', 'Poll not found'),
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get('/{poll_id}/stats', response_model=PollStatisticsResponse)
async def get_poll_stats(
    poll_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PollStatisticsResponse:
    poll = await db.get(Poll, poll_id)
    if not poll:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('POLL_NOT_FOUND', 'Poll not found'),
        )

    stats = await get_poll_statistics(db, poll_id)

    formatted_questions = [
        PollQuestionStats(
            id=question_data['id'],
            text=question_data['text'],
            order=question_data['order'],
            options=[
                PollOptionStats(
                    id=option_data['id'],
                    text=option_data['text'],
                    count=option_data['count'],
                )
                for option_data in question_data.get('options', [])
            ],
        )
        for question_data in stats.get('questions', [])
    ]

    return PollStatisticsResponse(
        poll_id=poll.id,
        poll_title=poll.title,
        total_responses=stats.get('total_responses', 0),
        completed_responses=stats.get('completed_responses', 0),
        reward_sum_kopeks=stats.get('reward_sum_kopeks', 0),
        reward_sum_rubles=_format_price(stats.get('reward_sum_kopeks', 0)),
        questions=formatted_questions,
    )


@router.get('/{poll_id}/responses', response_model=PollResponsesListResponse)
async def get_poll_responses(
    poll_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PollResponsesListResponse:
    poll_exists = await db.get(Poll, poll_id)
    if not poll_exists:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('POLL_NOT_FOUND', 'Poll not found'),
        )

    responses, total = await get_poll_responses_with_answers(
        db,
        poll_id,
        limit=limit,
        offset=offset,
    )

    items = [_serialize_user_response(response) for response in responses]

    return PollResponsesListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post('/{poll_id}/send', response_model=PollSendResponse)
async def send_poll(
    poll_id: int,
    payload: PollSendRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PollSendResponse:
    poll = await get_poll_by_id(db, poll_id)
    if not poll:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('POLL_NOT_FOUND', 'Poll not found'),
        )

    target = payload.target.strip()
    if not target:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_POLLS_TARGET_MUST_NOT_BE_EMPTY',
                'Target must not be empty',
            ),
        )

    if target.startswith('custom_'):
        users = await get_custom_users(db, target.replace('custom_', ''))
    else:
        users = await get_target_users(db, target)

    if not users:
        return PollSendResponse(
            poll_id=poll_id,
            target=target,
            sent=0,
            failed=0,
            skipped=0,
            total=0,
        )

    from app.config import settings

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        result = await send_poll_to_users(bot, db, poll, users)
    finally:
        await bot.session.close()

    return PollSendResponse(
        poll_id=poll_id,
        target=target,
        sent=result.get('sent', 0),
        failed=result.get('failed', 0),
        skipped=result.get('skipped', 0),
        total=result.get('total', 0),
    )
