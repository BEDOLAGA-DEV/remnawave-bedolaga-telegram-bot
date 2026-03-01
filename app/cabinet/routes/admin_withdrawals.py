"""Admin routes for managing withdrawal requests in cabinet."""

import json
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    ReferralEarning,
    User,
    WithdrawalRequest,
    WithdrawalRequestStatus,
)
from app.services.referral_withdrawal_service import referral_withdrawal_service

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.withdrawals import (
    AdminApproveWithdrawalRequest,
    AdminRejectWithdrawalRequest,
    AdminWithdrawalDetailResponse,
    AdminWithdrawalItem,
    AdminWithdrawalListResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/withdrawals', tags=['Cabinet Admin Withdrawals'])


def _get_risk_level(risk_score: int) -> str:
    """Get risk level from score."""
    if risk_score >= 70:
        return 'critical'
    if risk_score >= 50:
        return 'high'
    if risk_score >= 30:
        return 'medium'
    return 'low'


@router.get('', response_model=AdminWithdrawalListResponse)
async def list_withdrawals(
    withdrawal_status: Literal['pending', 'approved', 'rejected', 'completed', 'cancelled'] | None = Query(
        None, alias='status'
    ),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('withdrawals:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List all withdrawal requests."""
    query = select(WithdrawalRequest)
    count_query = select(func.count()).select_from(WithdrawalRequest)

    if withdrawal_status:
        query = query.where(WithdrawalRequest.status == withdrawal_status)
        count_query = count_query.where(WithdrawalRequest.status == withdrawal_status)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Pending stats
    pending_count_result = await db.execute(
        select(func.count())
        .select_from(WithdrawalRequest)
        .where(WithdrawalRequest.status == WithdrawalRequestStatus.PENDING.value)
    )
    pending_count = pending_count_result.scalar() or 0

    pending_total_result = await db.execute(
        select(func.coalesce(func.sum(WithdrawalRequest.amount_kopeks), 0)).where(
            WithdrawalRequest.status == WithdrawalRequestStatus.PENDING.value
        )
    )
    pending_total = pending_total_result.scalar() or 0

    query = query.order_by(desc(WithdrawalRequest.created_at)).offset(offset).limit(limit)
    result = await db.execute(query)
    withdrawals = result.scalars().all()

    # Batch-fetch users to avoid N+1
    user_ids = list({w.user_id for w in withdrawals})
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {u.id: u for u in users_result.scalars().all()}
    else:
        users_map = {}

    items = []
    for w in withdrawals:
        user = users_map.get(w.user_id)
        items.append(
            AdminWithdrawalItem(
                id=w.id,
                user_id=w.user_id,
                username=user.username if user else None,
                first_name=user.first_name if user else None,
                telegram_id=user.telegram_id if user else None,
                amount_kopeks=w.amount_kopeks,
                amount_rubles=w.amount_kopeks / 100,
                status=w.status,
                risk_score=w.risk_score or 0,
                risk_level=_get_risk_level(w.risk_score or 0),
                payment_details=w.payment_details,
                admin_comment=w.admin_comment,
                created_at=w.created_at,
                processed_at=w.processed_at,
            )
        )

    return AdminWithdrawalListResponse(
        items=items,
        total=total,
        pending_count=pending_count,
        pending_total_kopeks=pending_total,
    )


@router.get('/{withdrawal_id}', response_model=AdminWithdrawalDetailResponse)
async def get_withdrawal_detail(
    withdrawal_id: int,
    admin: User = Depends(require_permission('withdrawals:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed withdrawal request with risk analysis."""
    withdrawal = await db.get(WithdrawalRequest, withdrawal_id)
    if not withdrawal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Заявка не найдена',
        )

    user = await db.get(User, withdrawal.user_id)

    # Parse risk analysis
    risk_analysis = None
    if withdrawal.risk_analysis:
        try:
            risk_analysis = json.loads(withdrawal.risk_analysis)
        except (json.JSONDecodeError, TypeError):
            pass

    # Get referral stats
    referral_count = await db.execute(
        select(func.count()).select_from(User).where(User.referred_by_id == withdrawal.user_id)
    )
    total_earnings = await db.execute(
        select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
            ReferralEarning.user_id == withdrawal.user_id
        )
    )

    return AdminWithdrawalDetailResponse(
        id=withdrawal.id,
        user_id=withdrawal.user_id,
        username=user.username if user else None,
        first_name=user.first_name if user else None,
        telegram_id=user.telegram_id if user else None,
        amount_kopeks=withdrawal.amount_kopeks,
        amount_rubles=withdrawal.amount_kopeks / 100,
        status=withdrawal.status,
        risk_score=withdrawal.risk_score or 0,
        risk_level=_get_risk_level(withdrawal.risk_score or 0),
        risk_analysis=risk_analysis,
        payment_details=withdrawal.payment_details,
        admin_comment=withdrawal.admin_comment,
        balance_kopeks=user.balance_kopeks if user else 0,
        total_referrals=referral_count.scalar() or 0,
        total_earnings_kopeks=total_earnings.scalar() or 0,
        created_at=withdrawal.created_at,
        processed_at=withdrawal.processed_at,
    )


@router.post('/{withdrawal_id}/approve')
async def approve_withdrawal(
    withdrawal_id: int,
    request: AdminApproveWithdrawalRequest,
    admin: User = Depends(require_permission('withdrawals:approve')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Approve a withdrawal request."""
    success, error = await referral_withdrawal_service.approve_request(
        db,
        request_id=withdrawal_id,
        admin_id=admin.id,
        comment=request.comment,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    # Notify user about approval
    try:
        from aiogram import Bot

        from app.config import settings
        from app.services.notification_delivery_service import notification_delivery_service

        if settings.BOT_TOKEN:
            withdrawal = await db.get(WithdrawalRequest, withdrawal_id)
            user = await db.get(User, withdrawal.user_id) if withdrawal else None
            if user and withdrawal:
                formatted_amount = settings.format_price(withdrawal.amount_kopeks)
                comment_text = f'\n{request.comment}' if request.comment else ''
                tg_message = f'✅ Ваш запрос на вывод {formatted_amount} одобрен.{comment_text}'
                bot = Bot(token=settings.BOT_TOKEN)
                try:
                    await notification_delivery_service.notify_withdrawal_approved(
                        user=user,
                        amount_kopeks=withdrawal.amount_kopeks,
                        comment=request.comment,
                        bot=bot,
                        telegram_message=tg_message,
                    )
                finally:
                    await bot.session.close()
    except Exception as e:
        logger.error('Failed to send withdrawal approval notification', error=e)

    return {'success': True}


@router.post('/{withdrawal_id}/reject')
async def reject_withdrawal(
    withdrawal_id: int,
    request: AdminRejectWithdrawalRequest,
    admin: User = Depends(require_permission('withdrawals:reject')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reject a withdrawal request."""
    success, error = await referral_withdrawal_service.reject_request(
        db,
        request_id=withdrawal_id,
        admin_id=admin.id,
        comment=request.comment,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error or 'Не удалось отклонить заявку',
        )

    # Notify user about rejection
    try:
        from aiogram import Bot

        from app.config import settings
        from app.services.notification_delivery_service import notification_delivery_service

        if settings.BOT_TOKEN:
            withdrawal = await db.get(WithdrawalRequest, withdrawal_id)
            user = await db.get(User, withdrawal.user_id) if withdrawal else None
            if user and withdrawal:
                formatted_amount = settings.format_price(withdrawal.amount_kopeks)
                comment_text = f'\nПричина: {request.comment}' if request.comment else ''
                tg_message = f'❌ Ваш запрос на вывод {formatted_amount} отклонён.{comment_text}'
                bot = Bot(token=settings.BOT_TOKEN)
                try:
                    await notification_delivery_service.notify_withdrawal_rejected(
                        user=user,
                        amount_kopeks=withdrawal.amount_kopeks,
                        comment=request.comment,
                        bot=bot,
                        telegram_message=tg_message,
                    )
                finally:
                    await bot.session.close()
    except Exception as e:
        logger.error('Failed to send withdrawal rejection notification', error=e)

    return {'success': True}


@router.post('/{withdrawal_id}/complete')
async def complete_withdrawal(
    withdrawal_id: int,
    admin: User = Depends(require_permission('withdrawals:approve')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mark a withdrawal as completed (money transferred)."""
    success, error = await referral_withdrawal_service.complete_request(
        db,
        request_id=withdrawal_id,
        admin_id=admin.id,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error or 'Не удалось завершить заявку',
        )

    return {'success': True}
