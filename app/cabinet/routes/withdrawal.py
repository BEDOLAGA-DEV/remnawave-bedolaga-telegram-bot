"""User-facing withdrawal routes for cabinet."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User, WithdrawalRequest, WithdrawalRequestStatus
from app.localization.texts import get_texts
from app.services.referral_withdrawal_service import referral_withdrawal_service

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.withdrawals import (
    WithdrawalBalanceResponse,
    WithdrawalCreateRequest,
    WithdrawalCreateResponse,
    WithdrawalItemResponse,
    WithdrawalListResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/referral/withdrawal', tags=['Cabinet Withdrawal'])


@router.get('/balance', response_model=WithdrawalBalanceResponse)
async def get_withdrawal_balance(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get withdrawal balance stats for current user."""
    can_request, reason, stats = await referral_withdrawal_service.can_request_withdrawal(db, user.id)

    return WithdrawalBalanceResponse(
        total_earned=stats['total_earned'],
        referral_spent=stats['referral_spent'],
        withdrawn=stats['withdrawn'],
        pending=stats['pending'],
        available_referral=stats['available_referral'],
        available_total=stats['available_total'],
        only_referral_mode=stats['only_referral_mode'],
        min_amount_kopeks=settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS,
        is_withdrawal_enabled=settings.is_referral_withdrawal_enabled(),
        can_request=can_request,
        cannot_request_reason=reason if not can_request else None,
        requisites_text=settings.REFERRAL_WITHDRAWAL_REQUISITES_TEXT,
    )


@router.post('/create', response_model=WithdrawalCreateResponse)
async def create_withdrawal(
    request: WithdrawalCreateRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a withdrawal request."""
    withdrawal, error = await referral_withdrawal_service.create_withdrawal_request(
        db,
        user_id=user.id,
        amount_kopeks=request.amount_kopeks,
        payment_details=request.payment_details,
    )

    if not withdrawal:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    # Уведомляем админов о запросе на вывод
    try:
        from aiogram import Bot

        from app.services.admin_notification_service import AdminNotificationService

        if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
            bot = Bot(token=settings.BOT_TOKEN)
            try:
                notification_service = AdminNotificationService(bot)
                await notification_service.send_withdrawal_request_notification(
                    user=user,
                    amount_kopeks=request.amount_kopeks,
                    payment_details=request.payment_details,
                )
            finally:
                await bot.session.close()
    except Exception as e:
        logger.error('Failed to send admin notification for withdrawal request', error=e)

    return WithdrawalCreateResponse(
        id=withdrawal.id,
        amount_kopeks=withdrawal.amount_kopeks,
        status=withdrawal.status,
    )


@router.get('/history', response_model=WithdrawalListResponse)
async def get_withdrawal_history(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get user's withdrawal request history."""
    count_result = await db.execute(
        select(func.count()).select_from(WithdrawalRequest).where(WithdrawalRequest.user_id == user.id)
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        select(WithdrawalRequest)
        .where(WithdrawalRequest.user_id == user.id)
        .order_by(desc(WithdrawalRequest.created_at))
        .limit(50)
    )
    requests = result.scalars().all()

    items = [
        WithdrawalItemResponse(
            id=r.id,
            amount_kopeks=r.amount_kopeks,
            amount_rubles=r.amount_kopeks / 100,
            status=r.status,
            payment_details=r.payment_details,
            admin_comment=r.admin_comment,
            created_at=r.created_at,
            processed_at=r.processed_at,
        )
        for r in requests
    ]

    return WithdrawalListResponse(items=items, total=total)


@router.post('/{request_id}/cancel')
async def cancel_withdrawal(
    request_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Cancel a pending withdrawal request."""
    texts = get_texts(getattr(user, 'language', None))
    result = await db.execute(
        select(WithdrawalRequest)
        .where(
            WithdrawalRequest.id == request_id,
            WithdrawalRequest.user_id == user.id,
        )
        .with_for_update()
    )
    withdrawal = result.scalar_one_or_none()

    if not withdrawal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=texts.t('CABINET_WITHDRAWAL_REQUEST_NOT_FOUND', 'Заявка не найдена'),
        )

    if withdrawal.status != WithdrawalRequestStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=texts.t('CABINET_WITHDRAWAL_CANCEL_ONLY_PENDING', 'Можно отменить только заявку в ожидании'),
        )

    withdrawal.status = WithdrawalRequestStatus.CANCELLED.value
    await db.commit()

    return {'success': True}
