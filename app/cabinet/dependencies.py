"""FastAPI dependencies for cabinet module."""

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.services.blacklist_service import blacklist_service
from app.services.maintenance_service import maintenance_service

from .auth.jwt_handler import get_token_payload
from .auth.telegram_auth import validate_telegram_init_data


logger = structlog.get_logger(__name__)

security = HTTPBearer(auto_error=False)


async def get_cabinet_db() -> AsyncSession:
    """Get database session for cabinet operations."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_current_cabinet_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_cabinet_db),
) -> User:
    """
    Get current authenticated cabinet user from JWT token.

    Args:
        request: FastAPI request object (for reading X-Telegram-Init-Data header)
        credentials: HTTP Bearer credentials
        db: Database session

    Returns:
        Authenticated User object

    Raises:
        HTTPException: If token is invalid, expired, or user not found
    """
    # Check maintenance mode first (except for admins - checked later)
    if maintenance_service.is_maintenance_active():
        # We need to check token first to see if user is admin
        pass  # Will check after getting user

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required',
            headers={'WWW-Authenticate': 'Bearer'},
        )

    token = credentials.credentials
    payload = get_token_payload(token, expected_type='access')

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired token',
            headers={'WWW-Authenticate': 'Bearer'},
        )

    try:
        user_id = int(payload.get('sub'))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid token payload',
            headers={'WWW-Authenticate': 'Bearer'},
        )

    user = await get_user_by_id(db, user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    if user.status != 'active':
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='User account is not active',
        )

    # Defense in depth: cross-validate Telegram identity.
    # The frontend sends X-Telegram-Init-Data on every request.
    # If the header is present and cryptographically valid, verify that
    # the Telegram user ID matches the JWT user's telegram_id.
    # This prevents cross-account token reuse when Telegram WebView
    # shares localStorage across accounts on the same device.
    init_data_raw = request.headers.get('X-Telegram-Init-Data')
    if init_data_raw and user.telegram_id is not None:
        # Use generous max_age: Telegram Desktop caches initData
        tg_user = validate_telegram_init_data(init_data_raw, max_age_seconds=86400 * 30)
        if tg_user is None:
            logger.warning(
                'Telegram initData validation failed but header was present',
                jwt_user_id=user.id,
            )
        elif tg_user.get('id') != user.telegram_id:
            logger.warning(
                'Telegram identity mismatch: JWT belongs to different user than current Telegram account',
                jwt_user_id=user.id,
                jwt_telegram_id=user.telegram_id,
                init_data_telegram_id=tg_user.get('id'),
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail='Session belongs to a different Telegram account. Please restart the app.',
                headers={'WWW-Authenticate': 'Bearer'},
            )

    # Check blacklist
    if user.telegram_id is not None:
        is_blacklisted, reason = await blacklist_service.is_user_blacklisted(user.telegram_id, user.username)
        if is_blacklisted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    'code': 'blacklisted',
                    'message': reason or 'Доступ запрещен',
                },
            )

    # Check maintenance mode (allow admins to pass)
    if maintenance_service.is_maintenance_active():
        # Проверяем админа по telegram_id ИЛИ email
        is_admin = settings.is_admin(telegram_id=user.telegram_id, email=user.email if user.email_verified else None)
        if not is_admin:
            status_info = maintenance_service.get_status_info()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    'code': 'maintenance',
                    'message': maintenance_service.get_maintenance_message() or 'Service is under maintenance',
                    'reason': status_info.get('reason'),
                },
            )

    # Check required channel subscription - Telegram users only
    if settings.CHANNEL_IS_REQUIRED_SUB:
        # Skip for email-only users (no telegram_id)
        if user.telegram_id is not None:
            # Skip admin check
            is_admin = settings.is_admin(
                telegram_id=user.telegram_id, email=user.email if user.email_verified else None
            )
            if not is_admin:
                from app.services.channel_subscription_service import channel_subscription_service

                channels_with_status = await channel_subscription_service.get_channels_with_status(user.telegram_id)
                is_subscribed = (
                    all(ch['is_subscribed'] for ch in channels_with_status) if channels_with_status else True
                )

                if not is_subscribed:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            'code': 'channel_subscription_required',
                            'message': 'Please subscribe to the required channels to continue',
                            'channels': channels_with_status,
                        },
                    )

    return user


async def get_optional_cabinet_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_cabinet_db),
) -> User | None:
    """
    Optionally get current authenticated cabinet user.

    Returns None if no valid token is provided instead of raising an exception.
    """
    if not credentials:
        return None

    token = credentials.credentials
    payload = get_token_payload(token, expected_type='access')

    if not payload:
        return None

    try:
        user_id = int(payload.get('sub'))
    except (TypeError, ValueError):
        return None

    user = await get_user_by_id(db, user_id)

    if not user or user.status != 'active':
        return None

    # Cross-validate Telegram identity (same as get_current_cabinet_user)
    init_data_raw = request.headers.get('X-Telegram-Init-Data')
    if init_data_raw and user.telegram_id is not None:
        tg_user = validate_telegram_init_data(init_data_raw, max_age_seconds=86400 * 30)
        if tg_user and tg_user.get('id') != user.telegram_id:
            logger.warning(
                'Telegram identity mismatch in optional auth',
                jwt_user_id=user.id,
                jwt_telegram_id=user.telegram_id,
                init_data_telegram_id=tg_user.get('id'),
            )
            return None

    return user


async def get_current_admin_user(
    request: Request,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> User:
    """
    Get current authenticated admin user.

    Checks if the user is admin by legacy config (ADMIN_IDS / ADMIN_EMAILS)
    **or** by RBAC role assignment (any role with level > 0).

    Args:
        request: FastAPI request object
        user: Authenticated User object
        db: Database session

    Returns:
        Authenticated admin User object

    Raises:
        HTTPException: If user is not an admin by either mechanism
    """
    # Legacy check: config-based admin list
    is_legacy_admin = settings.is_admin(
        telegram_id=user.telegram_id,
        email=user.email if user.email_verified else None,
    )
    if is_legacy_admin:
        return user

    # RBAC check: user has any active role with level > 0
    from app.database.crud.rbac import UserRoleCRUD

    _permissions, _role_names, max_level = await UserRoleCRUD.get_user_permissions(db, user.id)
    if max_level > 0:
        return user

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail='Admin access required',
    )


def require_permission(*permissions: str):
    """
    FastAPI dependency factory for RBAC permission checks.

    Usage::

        @router.get("/users", dependencies=[Depends(require_permission("users:read"))])
        async def list_users(...): ...

        # Or inject the user:
        @router.get("/users")
        async def list_users(user: User = Depends(require_permission("users:read"))): ...
    """
    if not permissions:
        raise ValueError('require_permission() requires at least one permission argument')

    async def dependency(
        request: Request,
        user: User = Depends(get_current_cabinet_user),
        db: AsyncSession = Depends(get_cabinet_db),
    ) -> User:
        from app.services.permission_service import PermissionService

        ip_address = (
            request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or request.headers.get('X-Real-IP', '').strip()
            or (request.client.host if request.client else None)
        )
        user_agent = request.headers.get('user-agent', '')

        # Extract resource_type from the first permission (section before ':')
        resource_type = None
        if permissions:
            first_perm = permissions[0]
            if ':' in first_perm:
                resource_type = first_perm.split(':', maxsplit=1)[0]

        for perm in permissions:
            allowed, reason = await PermissionService.check_permission(
                db,
                user,
                perm,
                ip_address=ip_address,
            )
            if not allowed:
                await PermissionService.log_action(
                    db,
                    user_id=user.id,
                    action=perm,
                    resource_type=resource_type,
                    status='denied',
                    ip_address=ip_address,
                    user_agent=user_agent,
                    request_method=request.method,
                    request_path=str(request.url.path),
                    details={'reason': reason},
                )
                await db.commit()
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f'Permission denied: {reason}',
                )

        # Capture request details
        details: dict = {
            'method': request.method,
            'path': str(request.url.path),
        }
        query_params = dict(request.query_params)
        if query_params:
            details['query_params'] = query_params
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            try:
                body = await request.body()
                if body:
                    import json

                    details['request_body'] = json.loads(body)
            except Exception:
                pass

        # Log successful access with all requested permissions
        await PermissionService.log_action(
            db,
            user_id=user.id,
            action=','.join(permissions),
            resource_type=resource_type,
            status='success',
            ip_address=ip_address,
            user_agent=user_agent,
            request_method=request.method,
            request_path=str(request.url.path),
            details=details,
        )
        await db.commit()
        return user

    return dependency
