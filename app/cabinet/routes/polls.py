"""Polls routes for cabinet - user participation in polls/surveys."""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.poll import (
    get_poll_response_by_id,
    record_poll_answer,
)
from app.database.models import Poll, PollQuestion, PollResponse, User
from app.services.poll_service import get_next_question, get_question_option, reward_user_for_poll

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/polls', tags=['Cabinet Polls'])


# ============ Schemas ============


class PollOptionResponse(BaseModel):
    """Poll option."""

    id: int
    text: str
    order: int


class PollQuestionResponse(BaseModel):
    """Poll question with options."""

    id: int
    text: str
    order: int
    options: list[PollOptionResponse]


class PollInfo(BaseModel):
    """Poll info for user."""

    id: int
    response_id: int
    title: str
    description: str | None = None
    total_questions: int
    answered_questions: int
    is_completed: bool
    reward_amount: int | None = None


class PollStartResponse(BaseModel):
    """Response when starting a poll."""

    response_id: int
    current_question_index: int
    total_questions: int
    question: PollQuestionResponse


class AnswerRequest(BaseModel):
    """Request to answer a poll question."""

    option_id: int


class AnswerResponse(BaseModel):
    """Response after answering."""

    success: bool
    is_completed: bool
    next_question: PollQuestionResponse | None = None
    current_question_index: int | None = None
    total_questions: int
    reward_granted: int | None = None
    message: str | None = None


# ============ Helpers ============


def _question_to_response(question: PollQuestion) -> PollQuestionResponse:
    """Convert question model to response."""
    options = [
        PollOptionResponse(
            id=opt.id,
            text=opt.text,
            order=opt.order,
        )
        for opt in sorted(question.options, key=lambda o: o.order)
    ]
    return PollQuestionResponse(
        id=question.id,
        text=question.text,
        order=question.order,
        options=options,
    )


# ============ Routes ============


class PollsCountResponse(BaseModel):
    """Count of available polls."""

    count: int


@router.get('/count', response_model=PollsCountResponse)
async def get_polls_count(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get count of polls available for the user."""
    result = await db.execute(
        select(PollResponse)
        .where(PollResponse.user_id == user.id)
        .where(PollResponse.completed_at.is_(None))  # Only incomplete polls
    )
    responses = result.scalars().all()
    return PollsCountResponse(count=len(responses))


@router.get('', response_model=list[PollInfo])
async def get_available_polls(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of polls available for the user."""
    # Get user's poll responses with eager loading of relationships
    result = await db.execute(
        select(PollResponse)
        .where(PollResponse.user_id == user.id)
        .options(
            selectinload(PollResponse.poll).selectinload(Poll.questions),
            selectinload(PollResponse.answers),
        )
        .order_by(PollResponse.sent_at.desc())
    )
    responses = result.scalars().all()

    polls = []
    for response in responses:
        if not response.poll:
            continue

        answered_count = len(response.answers) if response.answers else 0
        total_questions = len(response.poll.questions) if response.poll.questions else 0

        # Convert kopeks to rubles for display
        reward_amount = None
        if response.poll.reward_amount_kopeks:
            reward_amount = response.poll.reward_amount_kopeks // 100

        polls.append(
            PollInfo(
                id=response.poll.id,
                response_id=response.id,
                title=response.poll.title,
                description=response.poll.description,
                total_questions=total_questions,
                answered_questions=answered_count,
                is_completed=response.completed_at is not None,
                reward_amount=reward_amount,
            )
        )

    return polls


@router.get('/{response_id}', response_model=PollInfo)
async def get_poll_details(
    response_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get details of a specific poll response."""
    response = await get_poll_response_by_id(db, response_id)

    if not response or response.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Poll not found',
        )

    if not response.poll:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Poll data not available',
        )

    answered_count = len(response.answers) if response.answers else 0
    total_questions = len(response.poll.questions) if response.poll.questions else 0

    # Convert kopeks to rubles for display
    reward_amount = None
    if response.poll.reward_amount_kopeks:
        reward_amount = response.poll.reward_amount_kopeks // 100

    return PollInfo(
        id=response.poll.id,
        response_id=response.id,
        title=response.poll.title,
        description=response.poll.description,
        total_questions=total_questions,
        answered_questions=answered_count,
        is_completed=response.completed_at is not None,
        reward_amount=reward_amount,
    )


@router.post('/{response_id}/start', response_model=PollStartResponse)
async def start_poll(
    response_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Start or continue a poll."""
    response = await get_poll_response_by_id(db, response_id)

    if not response or response.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Poll not found',
        )

    if response.completed_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This poll has already been completed',
        )

    if not response.poll or not response.poll.questions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Poll is not available',
        )

    # Mark as started if not already
    if not response.started_at:
        response.started_at = datetime.now(UTC)
        await db.commit()

    # Get next unanswered question
    index, question = await get_next_question(response)

    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No questions available',
        )

    return PollStartResponse(
        response_id=response.id,
        current_question_index=index,
        total_questions=len(response.poll.questions),
        question=_question_to_response(question),
    )


@router.post('/{response_id}/questions/{question_id}/answer', response_model=AnswerResponse)
async def answer_question(
    response_id: int,
    question_id: int,
    request: AnswerRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Submit answer for a poll question."""
    response = await get_poll_response_by_id(db, response_id)

    if not response or response.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Poll not found',
        )

    if response.completed_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This poll has already been completed',
        )

    if not response.poll:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Poll is not available',
        )

    # Find the question
    question = next((q for q in response.poll.questions if q.id == question_id), None)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Question not found',
        )

    # Validate option
    option = await get_question_option(question, request.option_id)
    if not option:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid answer option',
        )

    # Record the answer
    await record_poll_answer(
        db,
        response_id=response.id,
        question_id=question.id,
        option_id=option.id,
    )

    # Refresh to get updated answers
    try:
        await db.refresh(response, attribute_names=['answers'])
    except Exception:
        response = await get_poll_response_by_id(db, response_id)
        if not response:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to process answer',
            )

    # Get next question
    index, next_question = await get_next_question(response)
    total_questions = len(response.poll.questions)

    if next_question:
        # More questions to answer
        return AnswerResponse(
            success=True,
            is_completed=False,
            next_question=_question_to_response(next_question),
            current_question_index=index,
            total_questions=total_questions,
        )

    # Poll completed
    response.completed_at = datetime.now(UTC)
    await db.commit()

    # Award reward if any
    reward_amount = await reward_user_for_poll(db, response)

    message = 'Thank you for completing the poll!'
    if reward_amount:
        message += f' Reward of {settings.format_price(reward_amount)} has been added to your balance.'

    return AnswerResponse(
        success=True,
        is_completed=True,
        total_questions=total_questions,
        reward_granted=reward_amount,
        message=message,
    )
