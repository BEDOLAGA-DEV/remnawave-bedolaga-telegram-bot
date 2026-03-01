"""Admin routes for broadcasts in cabinet."""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BroadcastHistory, Subscription, SubscriptionStatus, Tariff, User
from app.handlers.admin.messages import get_target_users_count
from app.keyboards.admin import BROADCAST_BUTTONS, DEFAULT_BROADCAST_BUTTONS
from app.services.broadcast_service import (
    BroadcastConfig,
    BroadcastMediaConfig,
    EmailBroadcastConfig,
    broadcast_service,
    email_broadcast_service,
)

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.broadcasts import (
    BroadcastButton,
    BroadcastButtonsResponse,
    BroadcastCreateRequest,
    BroadcastFilter,
    BroadcastFiltersResponse,
    BroadcastListResponse,
    BroadcastPreviewRequest,
    BroadcastPreviewResponse,
    BroadcastResponse,
    BroadcastTariffsResponse,
    CombinedBroadcastCreateRequest,
    EmailFilterItem,
    EmailFiltersResponse,
    EmailPreviewRequest,
    EmailPreviewResponse,
    TariffFilter,
    TariffForBroadcast,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/broadcasts', tags=['Cabinet Admin Broadcasts'])


# ============ Filter Labels ============

FILTER_LABELS = {
    'all': 'Все пользователи',
    'active': 'Активные подписки',
    'trial': 'Триальные',
    'no': 'Без подписки',
    'expiring': 'Истекают (3 дня)',
    'expired': 'Истекшие',
    'zero': 'Нулевой трафик',
    'active_zero': 'Активные с нулевым трафиком',
    'trial_zero': 'Триальные с нулевым трафиком',
}

FILTER_GROUPS = {
    'all': 'basic',
    'active': 'subscription',
    'trial': 'subscription',
    'no': 'subscription',
    'expiring': 'subscription',
    'expired': 'subscription',
    'zero': 'traffic',
    'active_zero': 'traffic',
    'trial_zero': 'traffic',
}

CUSTOM_FILTER_LABELS = {
    'custom_today': 'Регистрация сегодня',
    'custom_week': 'Регистрация за неделю',
    'custom_month': 'Регистрация за месяц',
    'custom_active_today': 'Активны сегодня',
    'custom_inactive_week': 'Неактивны 7+ дней',
    'custom_inactive_month': 'Неактивны 30+ дней',
    'custom_referrals': 'Пришли по рефералу',
    'custom_direct': 'Прямая регистрация',
}

CUSTOM_FILTER_GROUPS = {
    'custom_today': 'registration',
    'custom_week': 'registration',
    'custom_month': 'registration',
    'custom_active_today': 'activity',
    'custom_inactive_week': 'activity',
    'custom_inactive_month': 'activity',
    'custom_referrals': 'source',
    'custom_direct': 'source',
}


# ============ Email Filter Labels ============

EMAIL_FILTER_LABELS = {
    'all_email': 'Все с email',
    'email_only': 'Только email-регистрация',
    'telegram_with_email': 'Telegram с email',
    'active_email': 'С активной подпиской',
    'expired_email': 'С истекшей подпиской',
}

EMAIL_FILTER_GROUPS = {
    'all_email': 'basic',
    'email_only': 'auth_type',
    'telegram_with_email': 'auth_type',
    'active_email': 'subscription',
    'expired_email': 'subscription',
}


# ============ Helper Functions ============


def _serialize_broadcast(broadcast: BroadcastHistory) -> BroadcastResponse:
    """Serialize broadcast to response model."""
    blocked = broadcast.blocked_count or 0
    progress = 0.0
    if broadcast.total_count > 0:
        progress = round((broadcast.sent_count + broadcast.failed_count + blocked) / broadcast.total_count * 100, 1)

    return BroadcastResponse(
        id=broadcast.id,
        target_type=broadcast.target_type,
        message_text=broadcast.message_text,
        has_media=broadcast.has_media,
        media_type=broadcast.media_type,
        media_file_id=broadcast.media_file_id,
        media_caption=broadcast.media_caption,
        total_count=broadcast.total_count,
        sent_count=broadcast.sent_count,
        failed_count=broadcast.failed_count,
        blocked_count=blocked,
        status=broadcast.status,
        admin_id=broadcast.admin_id,
        admin_name=broadcast.admin_name,
        created_at=broadcast.created_at,
        completed_at=broadcast.completed_at,
        progress_percent=progress,
        channel=getattr(broadcast, 'channel', 'telegram') or 'telegram',
        email_subject=getattr(broadcast, 'email_subject', None),
        email_html_content=getattr(broadcast, 'email_html_content', None),
    )


async def _get_email_filter_count(db: AsyncSession, target: str) -> int:
    """Get count of email users matching the filter."""
    base_conditions = [
        User.email.isnot(None),
        User.email_verified == True,
        User.status == 'active',
    ]

    if target == 'all_email':
        query = select(func.count(User.id)).where(*base_conditions)

    elif target == 'email_only':
        query = select(func.count(User.id)).where(
            *base_conditions,
            User.auth_type == 'email',
        )

    elif target == 'telegram_with_email':
        query = select(func.count(User.id)).where(
            *base_conditions,
            User.auth_type == 'telegram',
            User.telegram_id.isnot(None),
        )

    elif target == 'active_email':
        query = (
            select(func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                *base_conditions,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
            )
        )

    elif target == 'expired_email':
        query = (
            select(func.count(distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                *base_conditions,
                Subscription.status.in_(
                    [
                        SubscriptionStatus.EXPIRED.value,
                        SubscriptionStatus.DISABLED.value,
                    ]
                ),
            )
        )

    else:
        return 0

    result = await db.execute(query)
    return result.scalar() or 0


def _validate_email_target(target: str) -> bool:
    """Validate email target filter."""
    return target in EMAIL_FILTER_LABELS


async def _get_tariff_user_counts(db: AsyncSession) -> dict:
    """Get count of active users per tariff."""
    result = await db.execute(
        select(Subscription.tariff_id, func.count(func.distinct(Subscription.user_id)).label('count'))
        .join(User, User.id == Subscription.user_id)
        .where(
            User.status == 'active',
            Subscription.status == SubscriptionStatus.ACTIVE.value,
        )
        .group_by(Subscription.tariff_id)
    )
    return {row.tariff_id: row.count for row in result.all()}


def _validate_target(target: str, tariff_ids: set) -> bool:
    """Validate target value."""
    if target in FILTER_LABELS:
        return True
    if target in CUSTOM_FILTER_LABELS:
        return True
    if target.startswith('tariff_'):
        try:
            tariff_id = int(target.split('_')[1])
            return tariff_id in tariff_ids
        except (ValueError, IndexError):
            return False
    return False


def _validate_buttons(buttons: list[str]) -> bool:
    """Validate button keys."""
    return all(button in BROADCAST_BUTTONS for button in buttons)


# ============ Endpoints ============


@router.get('/filters', response_model=BroadcastFiltersResponse)
async def get_filters(
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastFiltersResponse:
    """Get all available filters with user counts."""
    # Basic filters
    filters = []
    for key, label in FILTER_LABELS.items():
        try:
            count = await get_target_users_count(db, key)
        except Exception as e:
            logger.warning('Failed to get count for filter', key=key, error=e)
            count = 0
        filters.append(
            BroadcastFilter(
                key=key,
                label=label,
                count=count,
                group=FILTER_GROUPS.get(key),
            )
        )

    # Custom filters
    custom_filters = []
    for key, label in CUSTOM_FILTER_LABELS.items():
        try:
            count = await get_target_users_count(db, key)
        except Exception as e:
            logger.warning('Failed to get count for custom filter', key=key, error=e)
            count = 0
        custom_filters.append(
            BroadcastFilter(
                key=key,
                label=label,
                count=count,
                group=CUSTOM_FILTER_GROUPS.get(key),
            )
        )

    # Tariff filters
    tariff_counts = await _get_tariff_user_counts(db)
    result = await db.execute(select(Tariff).where(Tariff.is_active == True).order_by(Tariff.name))
    tariffs = result.scalars().all()

    tariff_filters = []
    for tariff in tariffs:
        tariff_filters.append(
            TariffFilter(
                key=f'tariff_{tariff.id}',
                label=tariff.name,
                tariff_id=tariff.id,
                count=tariff_counts.get(tariff.id, 0),
            )
        )

    return BroadcastFiltersResponse(
        filters=filters,
        tariff_filters=tariff_filters,
        custom_filters=custom_filters,
    )


@router.get('/tariffs', response_model=BroadcastTariffsResponse)
async def get_tariffs(
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastTariffsResponse:
    """Get tariffs for broadcast filtering."""
    tariff_counts = await _get_tariff_user_counts(db)
    result = await db.execute(select(Tariff).where(Tariff.is_active == True).order_by(Tariff.name))
    tariffs = result.scalars().all()

    return BroadcastTariffsResponse(
        tariffs=[
            TariffForBroadcast(
                id=t.id,
                name=t.name,
                filter_key=f'tariff_{t.id}',
                active_users_count=tariff_counts.get(t.id, 0),
            )
            for t in tariffs
        ]
    )


@router.get('/buttons', response_model=BroadcastButtonsResponse)
async def get_buttons(
    admin: User = Depends(require_permission('broadcasts:read')),
) -> BroadcastButtonsResponse:
    """Get available buttons for broadcasts."""
    default_buttons = set(DEFAULT_BROADCAST_BUTTONS)
    buttons = []
    for key, config in BROADCAST_BUTTONS.items():
        buttons.append(
            BroadcastButton(
                key=key,
                label=config.get('default_text', key),
                default=key in default_buttons,
            )
        )
    return BroadcastButtonsResponse(buttons=buttons)


@router.post('/preview', response_model=BroadcastPreviewResponse)
async def preview_broadcast(
    request: BroadcastPreviewRequest,
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastPreviewResponse:
    """Preview broadcast recipients count."""
    # Get tariff IDs for validation
    result = await db.execute(select(Tariff.id))
    tariff_ids = {row[0] for row in result.all()}

    if not _validate_target(request.target, tariff_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid target: {request.target}',
        )

    try:
        count = await get_target_users_count(db, request.target)
    except Exception as e:
        logger.error('Failed to get count for target', target=request.target, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to count recipients',
        )

    return BroadcastPreviewResponse(target=request.target, count=count)


@router.post('', response_model=BroadcastResponse, status_code=status.HTTP_201_CREATED)
async def create_broadcast(
    request: BroadcastCreateRequest,
    admin: User = Depends(require_permission('broadcasts:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastResponse:
    """Create and start a broadcast."""
    # Validate target
    result = await db.execute(select(Tariff.id))
    tariff_ids = {row[0] for row in result.all()}

    if not _validate_target(request.target, tariff_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid target: {request.target}',
        )

    # Validate buttons
    if not _validate_buttons(request.selected_buttons):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid button key',
        )

    message_text = request.message_text.strip()
    if not message_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Message text must not be empty',
        )

    media_payload = request.media

    # Create broadcast record
    broadcast = BroadcastHistory(
        target_type=request.target,
        message_text=message_text,
        has_media=media_payload is not None,
        media_type=media_payload.type if media_payload else None,
        media_file_id=media_payload.file_id if media_payload else None,
        media_caption=media_payload.caption if media_payload else None,
        total_count=0,
        sent_count=0,
        failed_count=0,
        status='queued',
        admin_id=admin.id,
        admin_name=admin.username or f'Admin #{admin.id}',
    )
    db.add(broadcast)
    await db.commit()
    await db.refresh(broadcast)

    # Prepare media config
    media_config = None
    if media_payload:
        media_config = BroadcastMediaConfig(
            type=media_payload.type,
            file_id=media_payload.file_id,
            caption=media_payload.caption or message_text,
        )

    # Create broadcast config
    config = BroadcastConfig(
        target=request.target,
        message_text=message_text,
        selected_buttons=request.selected_buttons,
        media=media_config,
        initiator_name=admin.username or f'Admin #{admin.id}',
    )

    # Start broadcast
    await broadcast_service.start_broadcast(broadcast.id, config)
    await db.refresh(broadcast)

    logger.info(
        'Admin created broadcast for target', admin_id=admin.id, broadcast_id=broadcast.id, target=request.target
    )

    return _serialize_broadcast(broadcast)


@router.get('', response_model=BroadcastListResponse)
async def list_broadcasts(
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> BroadcastListResponse:
    """Get list of broadcasts with pagination."""
    total = await db.scalar(select(func.count(BroadcastHistory.id))) or 0

    result = await db.execute(
        select(BroadcastHistory).order_by(BroadcastHistory.created_at.desc()).offset(offset).limit(limit)
    )
    broadcasts = result.scalars().all()

    return BroadcastListResponse(
        items=[_serialize_broadcast(b) for b in broadcasts],
        total=int(total),
        limit=limit,
        offset=offset,
    )


# ============ Email Broadcast Endpoints ============


@router.get('/email-filters', response_model=EmailFiltersResponse)
async def get_email_filters(
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailFiltersResponse:
    """Get all available email filters with user counts."""
    filters = []
    total_with_email = 0

    for key, label in EMAIL_FILTER_LABELS.items():
        try:
            count = await _get_email_filter_count(db, key)
        except Exception as e:
            logger.warning('Failed to get count for email filter', key=key, error=e)
            count = 0

        filters.append(
            EmailFilterItem(
                key=key,
                label=label,
                count=count,
                group=EMAIL_FILTER_GROUPS.get(key),
            )
        )

        # Track total with email (all_email filter)
        if key == 'all_email':
            total_with_email = count

    return EmailFiltersResponse(
        filters=filters,
        total_with_email=total_with_email,
    )


@router.post('/email-preview', response_model=EmailPreviewResponse)
async def preview_email_broadcast(
    request: EmailPreviewRequest,
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailPreviewResponse:
    """Preview email broadcast recipients count."""
    if not _validate_email_target(request.target):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid email target: {request.target}',
        )

    try:
        count = await _get_email_filter_count(db, request.target)
    except Exception as e:
        logger.error('Failed to get email count for target', target=request.target, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to count email recipients',
        )

    return EmailPreviewResponse(target=request.target, count=count)


@router.post('/send', response_model=BroadcastResponse, status_code=status.HTTP_201_CREATED)
async def create_combined_broadcast(
    request: CombinedBroadcastCreateRequest,
    admin: User = Depends(require_permission('broadcasts:send')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastResponse:
    """Create and start a combined broadcast (telegram/email/both)."""
    # Get tariff IDs for target validation
    result = await db.execute(select(Tariff.id))
    tariff_ids = {row[0] for row in result.all()}

    admin_name = admin.username or f'Admin #{admin.id}'

    # Validate based on channel
    if request.channel in ('telegram', 'both'):
        # Validate telegram target
        if not _validate_target(request.target, tariff_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Invalid target: {request.target}',
            )

        # Validate telegram message
        if not request.message_text or not request.message_text.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Message text is required for Telegram broadcast',
            )

        # Validate buttons
        if not _validate_buttons(request.selected_buttons):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid button key',
            )

    if request.channel in ('email', 'both'):
        # For email channel, target must be email filter or we use telegram target for 'both'
        if request.channel == 'email' and not _validate_email_target(request.target):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Invalid email target: {request.target}',
            )

        # Validate email fields
        if not request.email_subject or not request.email_subject.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Email subject is required for email broadcast',
            )

        if not request.email_html_content or not request.email_html_content.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Email HTML content is required for email broadcast',
            )

    media_payload = request.media

    # Create broadcast record
    broadcast = BroadcastHistory(
        target_type=request.target,
        message_text=request.message_text.strip() if request.message_text else None,
        has_media=media_payload is not None,
        media_type=media_payload.type if media_payload else None,
        media_file_id=media_payload.file_id if media_payload else None,
        media_caption=media_payload.caption if media_payload else None,
        total_count=0,
        sent_count=0,
        failed_count=0,
        status='queued',
        admin_id=admin.id,
        admin_name=admin_name,
        channel=request.channel,
        email_subject=request.email_subject.strip() if request.email_subject else None,
        email_html_content=request.email_html_content.strip() if request.email_html_content else None,
    )
    db.add(broadcast)
    await db.commit()
    await db.refresh(broadcast)

    # Start broadcasts based on channel
    if request.channel in ('telegram', 'both'):
        # Prepare media config
        media_config = None
        if media_payload:
            media_config = BroadcastMediaConfig(
                type=media_payload.type,
                file_id=media_payload.file_id,
                caption=media_payload.caption or request.message_text,
            )

        # Create telegram broadcast config
        telegram_config = BroadcastConfig(
            target=request.target,
            message_text=request.message_text.strip(),
            selected_buttons=request.selected_buttons,
            media=media_config,
            initiator_name=admin_name,
        )

        await broadcast_service.start_broadcast(broadcast.id, telegram_config)

    if request.channel in ('email', 'both'):
        # For 'both' channel, we use 'all_email' as default email target
        # since telegram target won't match email filters
        email_target = request.target if request.channel == 'email' else 'all_email'

        # Create email broadcast config
        email_config = EmailBroadcastConfig(
            target=email_target,
            email_subject=request.email_subject.strip(),
            email_html_content=request.email_html_content.strip(),
            initiator_name=admin_name,
        )

        await email_broadcast_service.start_broadcast(broadcast.id, email_config)

    await db.refresh(broadcast)

    logger.info(
        'Admin created broadcast for target',
        admin_id=admin.id,
        channel=request.channel,
        broadcast_id=broadcast.id,
        target=request.target,
    )

    return _serialize_broadcast(broadcast)


@router.get('/{broadcast_id}', response_model=BroadcastResponse)
async def get_broadcast(
    broadcast_id: int,
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastResponse:
    """Get broadcast details."""
    broadcast = await db.get(BroadcastHistory, broadcast_id)
    if not broadcast:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Broadcast not found',
        )
    return _serialize_broadcast(broadcast)


@router.post('/{broadcast_id}/stop', response_model=BroadcastResponse)
async def stop_broadcast(
    broadcast_id: int,
    admin: User = Depends(require_permission('broadcasts:send')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastResponse:
    """Stop a running broadcast (telegram or email)."""
    broadcast = await db.get(BroadcastHistory, broadcast_id)
    if not broadcast:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Broadcast not found',
        )

    if broadcast.status not in {'queued', 'in_progress', 'cancelling'}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Broadcast is not running',
        )

    # Try to stop both telegram and email broadcasts (one or both may be running)
    channel = getattr(broadcast, 'channel', 'telegram') or 'telegram'

    is_running = False
    if channel in ('telegram', 'both'):
        is_running = await broadcast_service.request_stop(broadcast_id) or is_running

    if channel in ('email', 'both'):
        is_running = await email_broadcast_service.request_stop(broadcast_id) or is_running

    if is_running:
        broadcast.status = 'cancelling'
    else:
        broadcast.status = 'cancelled'
        broadcast.completed_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(broadcast)

    logger.info('Admin stopped broadcast', admin_id=admin.id, broadcast_id=broadcast_id)

    return _serialize_broadcast(broadcast)
