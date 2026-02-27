"""Admin routes for payment verification in cabinet."""

import math
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PaymentMethod, User
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.services.payment_verification_service import (
    SUPPORTED_MANUAL_CHECK_METHODS,
    PendingPayment,
    get_payment_record,
    list_recent_pending_payments,
    method_display_name,
    run_manual_check,
)

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/payments', tags=['Cabinet Admin Payments'])


# ============ Schemas ============


class PendingPaymentResponse(BaseModel):
    """Pending payment details."""

    id: int
    method: str
    method_display: str
    identifier: str
    amount_kopeks: int
    amount_rubles: float
    status: str
    status_emoji: str
    status_text: str
    is_paid: bool
    is_checkable: bool
    created_at: datetime
    expires_at: datetime | None = None
    payment_url: str | None = None
    user_id: int | None = None
    user_telegram_id: int | None = None
    user_username: str | None = None

    class Config:
        from_attributes = True


class PendingPaymentListResponse(BaseModel):
    """Paginated list of pending payments."""

    items: list[PendingPaymentResponse]
    total: int
    page: int
    per_page: int
    pages: int


class ManualCheckResponse(BaseModel):
    """Response after manual payment status check."""

    success: bool
    message: str
    payment: PendingPaymentResponse | None = None
    status_changed: bool = False
    old_status: str | None = None
    new_status: str | None = None


class PaymentsStatsResponse(BaseModel):
    """Statistics about pending payments."""

    total_pending: int
    by_method: dict


# ============ Helper functions ============


def _get_admin_texts(admin: User | None):
    language = getattr(admin, 'language', None)
    return get_texts(language)


def _get_status_info(record: PendingPayment, texts) -> tuple[str, str]:
    """Get status emoji and text for a pending payment."""
    status_str = (record.status or '').lower()
    status_paid = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_PAID', 'Оплачено')
    status_pending = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_PENDING_PAYMENT', 'Ожидает оплаты')
    status_processing = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_PROCESSING', 'Обрабатывается')
    status_error = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_ERROR', 'Ошибка')
    status_canceled = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_CANCELED', 'Отменено')
    status_unknown = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_UNKNOWN', 'Неизвестно')
    status_hold = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_HOLD', 'На удержании')
    status_declined = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_DECLINED', 'Отклонено')
    status_expired = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_EXPIRED', 'Истёк')
    status_authorized = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_AUTHORIZED', 'Авторизовано')

    if record.is_paid:
        return '✅', status_paid

    if record.method == PaymentMethod.PAL24:
        mapping = {
            'new': ('⏳', status_pending),
            'process': ('⌛', status_processing),
            'success': ('✅', status_paid),
            'fail': ('❌', status_error),
            'canceled': ('❌', status_canceled),
        }
        return mapping.get(status_str, ('❓', status_unknown))

    if record.method == PaymentMethod.MULENPAY:
        mapping = {
            'created': ('⏳', status_pending),
            'processing': ('⌛', status_processing),
            'hold': ('🔒', status_hold),
            'success': ('✅', status_paid),
            'canceled': ('❌', status_canceled),
            'error': ('❌', status_error),
        }
        return mapping.get(status_str, ('❓', status_unknown))

    if record.method == PaymentMethod.WATA:
        mapping = {
            'opened': ('⏳', status_pending),
            'pending': ('⏳', status_pending),
            'processing': ('⌛', status_processing),
            'paid': ('✅', status_paid),
            'closed': ('✅', status_paid),
            'declined': ('❌', status_declined),
            'canceled': ('❌', status_canceled),
            'expired': ('⌛', status_expired),
        }
        return mapping.get(status_str, ('❓', status_unknown))

    if record.method == PaymentMethod.PLATEGA:
        mapping = {
            'pending': ('⏳', status_pending),
            'inprogress': ('⌛', status_processing),
            'confirmed': ('✅', status_paid),
            'failed': ('❌', status_error),
            'canceled': ('❌', status_canceled),
            'expired': ('⌛', status_expired),
        }
        return mapping.get(status_str, ('❓', status_unknown))

    if record.method == PaymentMethod.HELEKET:
        if status_str in {'pending', 'created', 'waiting', 'check', 'processing'}:
            return '⏳', status_pending
        if status_str in {'paid', 'paid_over'}:
            return '✅', status_paid
        if status_str in {'cancel', 'canceled', 'fail', 'failed', 'expired'}:
            return '❌', status_canceled
        return '❓', status_unknown

    if record.method == PaymentMethod.YOOKASSA:
        mapping = {
            'pending': ('⏳', status_pending),
            'waiting_for_capture': ('⌛', status_processing),
            'succeeded': ('✅', status_paid),
            'canceled': ('❌', status_canceled),
        }
        return mapping.get(status_str, ('❓', status_unknown))

    if record.method == PaymentMethod.CRYPTOBOT:
        mapping = {
            'active': ('⏳', status_pending),
            'paid': ('✅', status_paid),
            'expired': ('⌛', status_expired),
        }
        return mapping.get(status_str, ('❓', status_unknown))

    if record.method == PaymentMethod.CLOUDPAYMENTS:
        mapping = {
            'pending': ('⏳', status_pending),
            'authorized': ('⌛', status_authorized),
            'completed': ('✅', status_paid),
            'failed': ('❌', status_error),
        }
        return mapping.get(status_str, ('❓', status_unknown))

    if record.method == PaymentMethod.FREEKASSA:
        mapping = {
            'pending': ('⏳', status_pending),
            'success': ('✅', status_paid),
            'paid': ('✅', status_paid),
            'canceled': ('❌', status_canceled),
            'error': ('❌', status_error),
        }
        return mapping.get(status_str, ('❓', status_unknown))

    return '❓', status_unknown


def _is_checkable(record: PendingPayment) -> bool:
    """Check if payment can be manually checked."""
    if record.method not in SUPPORTED_MANUAL_CHECK_METHODS:
        return False
    if not record.is_recent():
        return False
    status_str = (record.status or '').lower()
    if record.method == PaymentMethod.PAL24:
        return status_str in {'new', 'process'}
    if record.method == PaymentMethod.MULENPAY:
        return status_str in {'created', 'processing', 'hold'}
    if record.method == PaymentMethod.WATA:
        return status_str in {'opened', 'pending', 'processing', 'inprogress', 'in_progress'}
    if record.method == PaymentMethod.PLATEGA:
        return status_str in {'pending', 'inprogress', 'in_progress'}
    if record.method == PaymentMethod.HELEKET:
        return status_str not in {'paid', 'paid_over', 'cancel', 'canceled', 'fail', 'failed', 'expired'}
    if record.method == PaymentMethod.YOOKASSA:
        return status_str in {'pending', 'waiting_for_capture'}
    if record.method == PaymentMethod.CRYPTOBOT:
        return status_str in {'active'}
    if record.method == PaymentMethod.CLOUDPAYMENTS:
        return status_str in {'pending', 'authorized'}
    if record.method == PaymentMethod.FREEKASSA:
        return status_str in {'pending', 'created', 'processing'}
    return False


def _get_payment_url(record: PendingPayment) -> str | None:
    """Extract payment URL from record."""
    payment = record.payment
    payment_url = getattr(payment, 'payment_url', None)

    if record.method == PaymentMethod.PAL24:
        payment_url = getattr(payment, 'link_url', None) or getattr(payment, 'link_page_url', None) or payment_url
    elif record.method == PaymentMethod.WATA:
        payment_url = getattr(payment, 'url', None) or payment_url
    elif record.method == PaymentMethod.YOOKASSA:
        payment_url = getattr(payment, 'confirmation_url', None) or payment_url
    elif record.method == PaymentMethod.CRYPTOBOT:
        payment_url = (
            getattr(payment, 'bot_invoice_url', None)
            or getattr(payment, 'mini_app_invoice_url', None)
            or getattr(payment, 'web_app_invoice_url', None)
            or payment_url
        )
    elif record.method == PaymentMethod.PLATEGA:
        payment_url = getattr(payment, 'redirect_url', None) or payment_url
    elif record.method == PaymentMethod.CLOUDPAYMENTS or record.method == PaymentMethod.FREEKASSA:
        payment_url = getattr(payment, 'payment_url', None) or payment_url

    return payment_url


def _record_to_response(record: PendingPayment, texts) -> PendingPaymentResponse:
    """Convert PendingPayment to API response."""
    status_emoji, status_text = _get_status_info(record, texts)
    return PendingPaymentResponse(
        id=record.local_id,
        method=record.method.value,
        method_display=method_display_name(record.method),
        identifier=record.identifier,
        amount_kopeks=record.amount_kopeks,
        amount_rubles=record.amount_kopeks / 100,
        status=record.status or '',
        status_emoji=status_emoji,
        status_text=status_text,
        is_paid=record.is_paid,
        is_checkable=_is_checkable(record),
        created_at=record.created_at,
        expires_at=record.expires_at,
        payment_url=_get_payment_url(record),
        user_id=record.user.id if record.user else None,
        user_telegram_id=record.user.telegram_id if record.user else None,
        user_username=record.user.username if record.user else None,
    )


# ============ Routes ============


@router.get('', response_model=PendingPaymentListResponse)
async def get_all_pending_payments(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    method_filter: str | None = Query(None, description='Filter by payment method'),
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get all pending payments for admin verification."""
    texts = _get_admin_texts(admin)
    all_pending = await list_recent_pending_payments(db)

    # Apply method filter if specified
    if method_filter:
        try:
            filter_method = PaymentMethod(method_filter)
            all_pending = [p for p in all_pending if p.method == filter_method]
        except ValueError:
            pass

    total = len(all_pending)
    pages = math.ceil(total / per_page) if total > 0 else 1

    # Paginate
    start_idx = (page - 1) * per_page
    page_payments = all_pending[start_idx : start_idx + per_page]

    items = [_record_to_response(p, texts) for p in page_payments]

    return PendingPaymentListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get('/stats', response_model=PaymentsStatsResponse)
async def get_payments_stats(
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get statistics about pending payments."""
    all_pending = await list_recent_pending_payments(db)

    by_method = {}
    for p in all_pending:
        method_name = method_display_name(p.method)
        if method_name not in by_method:
            by_method[method_name] = 0
        by_method[method_name] += 1

    return PaymentsStatsResponse(
        total_pending=len(all_pending),
        by_method=by_method,
    )


@router.get('/{method}/{payment_id}', response_model=PendingPaymentResponse)
async def get_pending_payment_details(
    method: str,
    payment_id: int,
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get details of a specific pending payment."""
    texts = _get_admin_texts(admin)
    try:
        payment_method = PaymentMethod(method)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=texts.t(
                'CABINET_ADMIN_PAYMENTS_INVALID_METHOD',
                'Invalid payment method: {method}',
            ).format(method=method),
        )

    record = await get_payment_record(db, payment_method, payment_id)

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=texts.t('CABINET_ADMIN_PAYMENTS_NOT_FOUND', 'Payment not found'),
        )

    return _record_to_response(record, texts)


@router.post('/{method}/{payment_id}/check', response_model=ManualCheckResponse)
async def check_payment_status(
    method: str,
    payment_id: int,
    admin: User = Depends(require_permission('payments:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Manually check and update payment status."""
    texts = _get_admin_texts(admin)
    try:
        payment_method = PaymentMethod(method)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=texts.t(
                'CABINET_ADMIN_PAYMENTS_INVALID_METHOD',
                'Invalid payment method: {method}',
            ).format(method=method),
        )

    # Get current record
    record = await get_payment_record(db, payment_method, payment_id)

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=texts.t('CABINET_ADMIN_PAYMENTS_NOT_FOUND', 'Payment not found'),
        )

    # Check if manual check is available
    if not _is_checkable(record):
        return ManualCheckResponse(
            success=False,
            message=texts.t(
                'CABINET_ADMIN_PAYMENTS_MANUAL_CHECK_NOT_AVAILABLE',
                'Ручная проверка недоступна для этого платежа',
            ),
            payment=_record_to_response(record, texts),
            status_changed=False,
        )

    old_status = record.status
    old_is_paid = record.is_paid

    # Run manual check
    payment_service = PaymentService()
    updated = await run_manual_check(db, payment_method, payment_id, payment_service)

    if not updated:
        return ManualCheckResponse(
            success=False,
            message=texts.t(
                'CABINET_ADMIN_PAYMENTS_MANUAL_CHECK_FAILED',
                'Не удалось проверить статус платежа',
            ),
            payment=_record_to_response(record, texts),
            status_changed=False,
        )

    status_changed = updated.status != old_status or updated.is_paid != old_is_paid

    if status_changed:
        _, new_status_text = _get_status_info(updated, texts)
        message = texts.t(
            'CABINET_ADMIN_PAYMENTS_STATUS_UPDATED',
            'Статус обновлён: {status_text}',
        ).format(status_text=new_status_text)
        logger.info(
            'Admin checked payment /',
            admin_id=admin.id,
            method=method,
            payment_id=payment_id,
            old_status=old_status,
            status=updated.status,
        )
    else:
        message = texts.t('CABINET_ADMIN_PAYMENTS_STATUS_UNCHANGED', 'Статус не изменился')

    return ManualCheckResponse(
        success=True,
        message=message,
        payment=_record_to_response(updated, texts),
        status_changed=status_changed,
        old_status=old_status,
        new_status=updated.status,
    )
