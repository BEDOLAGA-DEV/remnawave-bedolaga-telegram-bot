"""Admin routes for payment verification in cabinet."""

import math
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, User
from app.services.payment_search_service import (
    MAX_ALL_TIME_DAYS,
    PeriodPreset,
    SearchParams,
    StatusFilter,
    search_payments,
    search_payments_stats,
)
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
    user_email: str | None = None

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


class SearchStatsResponse(BaseModel):
    """Statistics for payment search results."""

    total: int
    pending: int
    paid: int
    cancelled: int
    by_method: dict


# ============ Helper functions ============


def _get_status_info(record: PendingPayment) -> tuple[str, str]:
    """Get status emoji and text for a pending payment."""
    status_str = (record.status or '').lower()

    if record.is_paid:
        return '✅', 'Оплачено'

    if record.method == PaymentMethod.PAL24:
        mapping = {
            'new': ('⏳', 'Ожидает оплаты'),
            'process': ('⌛', 'Обрабатывается'),
            'success': ('✅', 'Оплачено'),
            'fail': ('❌', 'Ошибка'),
            'canceled': ('❌', 'Отменено'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.MULENPAY:
        mapping = {
            'created': ('⏳', 'Ожидает оплаты'),
            'processing': ('⌛', 'Обрабатывается'),
            'hold': ('🔒', 'На удержании'),
            'success': ('✅', 'Оплачено'),
            'canceled': ('❌', 'Отменено'),
            'error': ('❌', 'Ошибка'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.WATA:
        mapping = {
            'opened': ('⏳', 'Ожидает оплаты'),
            'pending': ('⏳', 'Ожидает оплаты'),
            'processing': ('⌛', 'Обрабатывается'),
            'paid': ('✅', 'Оплачено'),
            'closed': ('✅', 'Оплачено'),
            'declined': ('❌', 'Отклонено'),
            'canceled': ('❌', 'Отменено'),
            'expired': ('⌛', 'Истёк'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.PLATEGA:
        mapping = {
            'pending': ('⏳', 'Ожидает оплаты'),
            'inprogress': ('⌛', 'Обрабатывается'),
            'confirmed': ('✅', 'Оплачено'),
            'failed': ('❌', 'Ошибка'),
            'canceled': ('❌', 'Отменено'),
            'expired': ('⌛', 'Истёк'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.HELEKET:
        if status_str in {'pending', 'created', 'waiting', 'check', 'processing'}:
            return '⏳', 'Ожидает оплаты'
        if status_str in {'paid', 'paid_over'}:
            return '✅', 'Оплачено'
        if status_str in {'cancel', 'canceled', 'fail', 'failed', 'expired'}:
            return '❌', 'Отменено'
        return '❓', 'Неизвестно'

    if record.method == PaymentMethod.YOOKASSA:
        mapping = {
            'pending': ('⏳', 'Ожидает оплаты'),
            'waiting_for_capture': ('⌛', 'Обрабатывается'),
            'succeeded': ('✅', 'Оплачено'),
            'canceled': ('❌', 'Отменено'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.CRYPTOBOT:
        mapping = {
            'active': ('⏳', 'Ожидает оплаты'),
            'paid': ('✅', 'Оплачено'),
            'expired': ('⌛', 'Истёк'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.CLOUDPAYMENTS:
        mapping = {
            'pending': ('⏳', 'Ожидает оплаты'),
            'authorized': ('⌛', 'Авторизовано'),
            'completed': ('✅', 'Оплачено'),
            'failed': ('❌', 'Ошибка'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.FREEKASSA:
        mapping = {
            'pending': ('⏳', 'Ожидает оплаты'),
            'success': ('✅', 'Оплачено'),
            'paid': ('✅', 'Оплачено'),
            'canceled': ('❌', 'Отменено'),
            'error': ('❌', 'Ошибка'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    return '❓', 'Неизвестно'


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
        return status_str == 'active'
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

    if payment_url and not payment_url.startswith(('https://', 'http://')):
        return None
    return payment_url


def _record_to_response(record: PendingPayment) -> PendingPaymentResponse:
    """Convert PendingPayment to API response."""
    status_emoji, status_text = _get_status_info(record)
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
        user_email=record.user.email if record.user else None,
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

    items = [_record_to_response(p) for p in page_payments]

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


@router.get('/search', response_model=PendingPaymentListResponse)
async def search_payments_endpoint(
    search: str | None = Query(
        None, max_length=256, description='Search query (invoice, @username, telegram_id, email)'
    ),
    status_filter: str = Query('all', description='Status filter: all, pending, paid, cancelled'),
    method_filter: str | None = Query(None, description='Filter by payment method'),
    period: str = Query('24h', description='Period preset: 24h, 7d, 30d, all'),
    date_from: datetime | None = Query(None, description='Custom range start (ISO 8601)'),
    date_to: datetime | None = Query(None, description='Custom range end (ISO 8601)'),
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Search payments across all providers with filters."""
    try:
        parsed_status = StatusFilter(status_filter)
    except ValueError:
        parsed_status = StatusFilter.ALL

    try:
        parsed_period = PeriodPreset(period)
    except ValueError:
        parsed_period = PeriodPreset.H24

    parsed_method: PaymentMethod | None = None
    if method_filter:
        try:
            parsed_method = PaymentMethod(method_filter)
        except ValueError:
            pass

    # Ensure custom dates are timezone-aware
    if date_from is not None and date_from.tzinfo is None:
        date_from = date_from.replace(tzinfo=UTC)
    if date_to is not None and date_to.tzinfo is None:
        date_to = date_to.replace(tzinfo=UTC)

    # Clamp custom dates to safety limit
    min_allowed = datetime.now(UTC) - timedelta(days=MAX_ALL_TIME_DAYS)
    if date_from is not None and date_from < min_allowed:
        date_from = min_allowed
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='date_from must be before date_to')

    params = SearchParams(
        search=search.strip() if search else None,
        status_filter=parsed_status,
        method_filter=parsed_method,
        period=parsed_period,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page,
    )

    page_items, total = await search_payments(db, params)
    pages = math.ceil(total / per_page) if total > 0 else 1
    items = [_record_to_response(p) for p in page_items]

    return PendingPaymentListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get('/search/stats', response_model=SearchStatsResponse)
async def search_payments_stats_endpoint(
    search: str | None = Query(
        None, max_length=256, description='Search query (invoice, @username, telegram_id, email)'
    ),
    status_filter: str = Query('all', description='Status filter: all, pending, paid, cancelled'),
    method_filter: str | None = Query(None, description='Filter by payment method'),
    period: str = Query('24h', description='Period preset: 24h, 7d, 30d, all'),
    date_from: datetime | None = Query(None, description='Custom range start (ISO 8601)'),
    date_to: datetime | None = Query(None, description='Custom range end (ISO 8601)'),
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get aggregated statistics for payment search results."""
    try:
        parsed_status = StatusFilter(status_filter)
    except ValueError:
        parsed_status = StatusFilter.ALL

    try:
        parsed_period = PeriodPreset(period)
    except ValueError:
        parsed_period = PeriodPreset.H24

    parsed_method: PaymentMethod | None = None
    if method_filter:
        try:
            parsed_method = PaymentMethod(method_filter)
        except ValueError:
            pass

    # Ensure custom dates are timezone-aware
    if date_from is not None and date_from.tzinfo is None:
        date_from = date_from.replace(tzinfo=UTC)
    if date_to is not None and date_to.tzinfo is None:
        date_to = date_to.replace(tzinfo=UTC)

    # Clamp custom dates to safety limit
    min_allowed = datetime.now(UTC) - timedelta(days=MAX_ALL_TIME_DAYS)
    if date_from is not None and date_from < min_allowed:
        date_from = min_allowed
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='date_from must be before date_to')

    params = SearchParams(
        search=search.strip() if search else None,
        status_filter=parsed_status,
        method_filter=parsed_method,
        period=parsed_period,
        date_from=date_from,
        date_to=date_to,
    )

    stats = await search_payments_stats(db, params)

    return SearchStatsResponse(
        total=stats.total,
        pending=stats.pending,
        paid=stats.paid,
        cancelled=stats.cancelled,
        by_method=stats.by_method or {},
    )


@router.get('/{method}/{payment_id}', response_model=PendingPaymentResponse)
async def get_pending_payment_details(
    method: str,
    payment_id: int,
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get details of a specific pending payment."""
    try:
        payment_method = PaymentMethod(method)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid payment method',
        )

    record = await get_payment_record(db, payment_method, payment_id)

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Payment not found',
        )

    return _record_to_response(record)


@router.post('/{method}/{payment_id}/check', response_model=ManualCheckResponse)
async def check_payment_status(
    method: str,
    payment_id: int,
    admin: User = Depends(require_permission('payments:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Manually check and update payment status."""
    try:
        payment_method = PaymentMethod(method)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid payment method',
        )

    # Get current record
    record = await get_payment_record(db, payment_method, payment_id)

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Payment not found',
        )

    # Check if manual check is available
    if not _is_checkable(record):
        return ManualCheckResponse(
            success=False,
            message='Ручная проверка недоступна для этого платежа',
            payment=_record_to_response(record),
            status_changed=False,
        )

    old_status = record.status
    old_is_paid = record.is_paid

    # Run manual check
    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        payment_service = PaymentService(bot=bot)
        updated = await run_manual_check(db, payment_method, payment_id, payment_service)
    finally:
        await bot.session.close()

    if not updated:
        return ManualCheckResponse(
            success=False,
            message='Не удалось проверить статус платежа',
            payment=_record_to_response(record),
            status_changed=False,
        )

    status_changed = updated.status != old_status or updated.is_paid != old_is_paid

    if status_changed:
        _, new_status_text = _get_status_info(updated)
        message = f'Статус обновлён: {new_status_text}'
        logger.info(
            'Admin checked payment /',
            admin_id=admin.id,
            method=method,
            payment_id=payment_id,
            old_status=old_status,
            status=updated.status,
        )
    else:
        message = 'Статус не изменился'

    return ManualCheckResponse(
        success=True,
        message=message,
        payment=_record_to_response(updated),
        status_changed=status_changed,
        old_status=old_status,
        new_status=updated.status,
    )
