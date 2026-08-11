from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.constants import POSTGRES_INT4_MAX, POSTGRES_INT4_MIN
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.subscription import (
    create_paid_subscription,
    create_trial_subscription,
    deactivate_subscription,
    get_subscription_by_user_id,
    replace_subscription,
)
from app.database.crud.user import (
    add_user_balance,
    create_user,
    get_user_by_id,
    get_user_by_referral_code,
    get_user_by_telegram_id,
    subtract_user_balance,
    update_user,
)
from app.database.models import PaymentMethod, PromoGroup, Subscription, User, UserStatus
from app.services.manual_topup_service import ManualTopupKeyConflict, credit_manual_topup
from app.services.subscription_service import SubscriptionService
from app.utils.text_search import contains_conditions

from ..dependencies import get_db_session, require_api_token
from ..schemas.users import (
    BalanceDepositRequest,
    BalanceDepositResponse,
    BalanceUpdateRequest,
    PromoGroupSummary,
    SubscriptionSummary,
    UserCreateRequest,
    UserListResponse,
    UserResponse,
    UserSubscriptionCreateRequest,
    UserUpdateRequest,
)
from ._subscription_state import (
    restore_subscription_state as _restore_subscription_state,
    snapshot_subscription_state as _snapshot_subscription_state,
)


router = APIRouter()
logger = structlog.get_logger(__name__)


def _serialize_promo_group(group: PromoGroup | None) -> PromoGroupSummary | None:
    if not group:
        return None
    return PromoGroupSummary(
        id=group.id,
        name=group.name,
        server_discount_percent=group.server_discount_percent,
        traffic_discount_percent=group.traffic_discount_percent,
        device_discount_percent=group.device_discount_percent,
        apply_discounts_to_addons=getattr(group, 'apply_discounts_to_addons', True),
    )


def _serialize_subscription(subscription: Subscription | None) -> SubscriptionSummary | None:
    if not subscription:
        return None

    tariff = getattr(subscription, 'tariff', None)
    return SubscriptionSummary(
        id=subscription.id,
        status=subscription.status,
        actual_status=subscription.actual_status,
        is_trial=subscription.is_trial,
        start_date=subscription.start_date,
        end_date=subscription.end_date,
        traffic_limit_gb=subscription.traffic_limit_gb,
        traffic_used_gb=subscription.traffic_used_gb,
        device_limit=subscription.device_limit,
        autopay_enabled=subscription.autopay_enabled,
        autopay_days_before=subscription.autopay_days_before,
        subscription_url=subscription.subscription_url,
        subscription_crypto_link=subscription.subscription_crypto_link,
        connected_squads=list(subscription.connected_squads or []),
        tariff_id=subscription.tariff_id,
        tariff_name=tariff.name if tariff is not None else None,
    )


def _serialize_user(user: User) -> UserResponse:
    subscription = getattr(user, 'subscription', None)
    promo_group = getattr(user, 'promo_group', None)
    all_subscriptions = getattr(user, 'subscriptions', None) or []

    return UserResponse(
        id=user.id,
        telegram_id=user.telegram_id,
        email=user.email,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        status=user.status,
        language=user.language,
        balance_kopeks=user.balance_kopeks,
        balance_rubles=round(user.balance_kopeks / 100, 2),
        referral_code=user.referral_code,
        referred_by_id=user.referred_by_id,
        has_had_paid_subscription=user.has_had_paid_subscription,
        has_made_first_topup=user.has_made_first_topup,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_activity=user.last_activity,
        promo_group=_serialize_promo_group(promo_group),
        subscription=_serialize_subscription(subscription),
        subscriptions=[_serialize_subscription(s) for s in all_subscriptions if s is not None],
    )


def _apply_search_filter(query, search: str):
    # lower() в SQL сворачивает регистр по локали базы: под `C` (наш docker-compose)
    # кириллица не сворачивается, и «поз» не находил «Позитив».
    # См. app/utils/text_search.py.
    conditions = contains_conditions(
        (User.username, User.first_name, User.last_name, User.referral_code),
        search,
    )

    if search.isdigit():
        numeric_search = int(search)
        conditions.append(User.telegram_id == numeric_search)
        if POSTGRES_INT4_MIN <= numeric_search <= POSTGRES_INT4_MAX:
            conditions.append(User.id == numeric_search)

    return query.where(or_(*conditions))


@router.get('', response_model=UserListResponse)
async def list_users(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: UserStatus | None = Query(default=None, alias='status'),
    promo_group_id: int | None = Query(default=None),
    search: str | None = Query(default=None),
) -> UserListResponse:
    base_query = select(User).options(
        selectinload(User.subscriptions).selectinload(Subscription.tariff),
        selectinload(User.promo_group),
    )

    if status_filter:
        base_query = base_query.where(User.status == status_filter.value)

    if promo_group_id:
        base_query = base_query.where(User.promo_group_id == promo_group_id)

    if search:
        base_query = _apply_search_filter(base_query, search)

    total_query = base_query.with_only_columns(func.count()).order_by(None)
    total = await db.scalar(total_query) or 0

    result = await db.execute(base_query.order_by(User.created_at.desc()).offset(offset).limit(limit))
    users = result.scalars().unique().all()

    return UserListResponse(
        items=[_serialize_user(user) for user in users],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get('/{user_id}', response_model=UserResponse)
async def get_user(
    user_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    # First check if the provided ID is a telegram_id
    user = await get_user_by_telegram_id(db, user_id)
    if user:
        return _serialize_user(user)

    # If not found as telegram_id, check as internal user ID
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')

    return _serialize_user(user)


@router.get('/by-telegram-id/{telegram_id}', response_model=UserResponse)
async def get_user_by_telegram_id_endpoint(
    telegram_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """
    Get user by Telegram ID
    """
    user = await get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')

    return _serialize_user(user)


@router.post('', response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user_endpoint(
    payload: UserCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    # Check for duplicate telegram_id only if provided (skip for email-only users)
    if payload.telegram_id is not None:
        existing = await get_user_by_telegram_id(db, payload.telegram_id)
        if existing:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'User with this telegram_id already exists')

    user = await create_user(
        db,
        telegram_id=payload.telegram_id,
        username=payload.username,
        first_name=payload.first_name,
        last_name=payload.last_name,
        language=payload.language,
        referred_by_id=payload.referred_by_id,
    )

    if payload.promo_group_id and payload.promo_group_id != user.promo_group_id:
        promo_group = await get_promo_group_by_id(db, payload.promo_group_id)
        if not promo_group:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Promo group not found')
        user = await update_user(db, user, promo_group_id=promo_group.id)

    user = await get_user_by_id(db, user.id)
    return _serialize_user(user)


@router.patch('/{user_id}', response_model=UserResponse)
async def update_user_endpoint(
    user_id: int,
    payload: UserUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    # First check if the provided ID is a telegram_id
    user = await get_user_by_telegram_id(db, user_id)
    if user:
        found_user = user
    else:
        # If not found as telegram_id, check as internal user ID
        found_user = await get_user_by_id(db, user_id)

    if not found_user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')

    updates: dict[str, Any] = {}

    if payload.username is not None:
        updates['username'] = payload.username
    if payload.first_name is not None:
        updates['first_name'] = payload.first_name
    if payload.last_name is not None:
        updates['last_name'] = payload.last_name
    if payload.language is not None:
        updates['language'] = payload.language
    if payload.has_had_paid_subscription is not None:
        updates['has_had_paid_subscription'] = payload.has_had_paid_subscription
    if payload.has_made_first_topup is not None:
        updates['has_made_first_topup'] = payload.has_made_first_topup

    if payload.status is not None:
        try:
            status_value = UserStatus(payload.status).value
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Invalid status') from error
        updates['status'] = status_value

    if payload.promo_group_id is not None:
        promo_group = await get_promo_group_by_id(db, payload.promo_group_id)
        if not promo_group:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Promo group not found')
        updates['promo_group_id'] = promo_group.id

    if payload.referral_code is not None and payload.referral_code != found_user.referral_code:
        existing_code_owner = await get_user_by_referral_code(db, payload.referral_code)
        if existing_code_owner and existing_code_owner.id != found_user.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Referral code already in use')
        updates['referral_code'] = payload.referral_code

    if not updates:
        return _serialize_user(found_user)

    found_user = await update_user(db, found_user, **updates)
    # Reload the user to ensure we have the latest data
    if found_user.telegram_id == user_id:
        found_user = await get_user_by_telegram_id(db, user_id)
    else:
        found_user = await get_user_by_id(db, found_user.id)

    return _serialize_user(found_user)


@router.post('/{user_id}/balance', response_model=UserResponse)
async def update_balance(
    user_id: int,
    payload: BalanceUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    if payload.amount_kopeks == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Amount must be non-zero')

    # First check if the provided ID is a telegram_id
    user = await get_user_by_telegram_id(db, user_id)
    if user:
        found_user = user
    else:
        # If not found as telegram_id, check as internal user ID
        found_user = await get_user_by_id(db, user_id)

    if not found_user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')

    if payload.amount_kopeks > 0:
        success = await add_user_balance(
            db,
            found_user,
            amount_kopeks=payload.amount_kopeks,
            description=payload.description or 'Корректировка через веб-API',
            create_transaction=payload.create_transaction,
            payment_method=PaymentMethod.MANUAL,
        )
    else:
        success = await subtract_user_balance(
            db,
            found_user,
            amount_kopeks=abs(payload.amount_kopeks),
            description=payload.description or 'Корректировка через веб-API',
            create_transaction=payload.create_transaction,
            payment_method=PaymentMethod.MANUAL,
        )

    if not success:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, 'Failed to update balance')

    # Reload the user to ensure we have the latest data
    if found_user.telegram_id == user_id:
        found_user = await get_user_by_telegram_id(db, user_id)
    else:
        found_user = await get_user_by_id(db, found_user.id)

    return _serialize_user(found_user)


def _open_bot() -> Any | None:
    """Бот для уведомлений. None — уведомления пропускаем, деньги всё равно зачисляем."""
    try:
        from app.bot_factory import create_bot

        return create_bot()
    except Exception as error:
        logger.error('Не удалось создать бота для уведомлений о пополнении', error=error)
        return None


@router.post('/{user_id}/deposit', response_model=BalanceDepositResponse)
async def deposit_balance(
    user_id: int,
    payload: BalanceDepositRequest,
    token: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> BalanceDepositResponse:
    """Ручное пополнение баланса — как настоящий платёж, но инициированное поддержкой.

    В отличие от `POST /users/{id}/balance` (низкоуровневая корректировка числа) здесь:

    * только зачисление, списать нельзя;
    * `idempotency_key` — повтор запроса не начислит деньги дважды;
    * запускается весь конвейер настоящего пополнения: реферальная комиссия,
      уведомление пользователю, возобновление приостановленной суточной подписки,
      автопокупка сохранённой корзины. Без этого «поддержка начислила вручную»
      оставляло аккаунт в состоянии «деньги есть, подписка не работает»;
    * сумма ограничена `WEB_API_MANUAL_DEPOSIT_MAX_KOPEKS`.

    `user_id` — внутренний ID либо Telegram ID (как и в остальных `/users` эндпоинтах).
    Кого именно пополнили, видно в ответе (`user_id` + `telegram_id`).
    """
    max_kopeks = settings.WEB_API_MANUAL_DEPOSIT_MAX_KOPEKS
    if max_kopeks and payload.amount_kopeks > max_kopeks:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f'Amount {payload.amount_kopeks} exceeds the manual deposit limit of {max_kopeks} kopeks '
            f'(raise WEB_API_MANUAL_DEPOSIT_MAX_KOPEKS or use POST /users/{{id}}/balance)',
        )

    found_user = await _get_user_by_id_or_telegram_id(db, user_id)
    # Снимаем идентификаторы ДО начисления: в ветке гонки по ключу идемпотентности
    # внутри делается rollback, а он экспирирует ORM-объект — обращение к атрибуту
    # после этого ушло бы в ленивую подгрузку и упало бы MissingGreenlet.
    resolved_id = found_user.id
    resolved_telegram_id = found_user.telegram_id

    bot = _open_bot()
    try:
        result = await credit_manual_topup(
            db,
            found_user,
            amount_kopeks=payload.amount_kopeks,
            description=payload.description or 'Ручное пополнение',
            idempotency_key=payload.idempotency_key,
            bot=bot,
            notify_user=payload.notify_user,
            apply_topup_bonuses=payload.apply_topup_bonuses,
        )
    except ManualTopupKeyConflict as conflict:
        # Тот же ключ, но другая сумма или другой пользователь — ошибка вызывающего.
        # Молчаливое «duplicate, ничего не начислено» агент прочитал бы как успех.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'Idempotency key already used by transaction {conflict.transaction.id} '
            f'(user {conflict.transaction.user_id}, {conflict.transaction.amount_kopeks} kopeks)',
        ) from conflict
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception as error:
                logger.warning('Не удалось закрыть сессию бота после пополнения', error=error)

    logger.info(
        'Ручное пополнение через API',
        token_id=getattr(token, 'id', None),
        token_name=getattr(token, 'name', None),
        user_id=resolved_id,
        telegram_id=resolved_telegram_id,
        amount_kopeks=payload.amount_kopeks,
        idempotency_key=payload.idempotency_key,
        duplicate=result.duplicate,
        transaction_id=result.transaction.id,
    )

    return BalanceDepositResponse(
        success=True,
        duplicate=result.duplicate,
        user_id=resolved_id,
        telegram_id=resolved_telegram_id,
        transaction_id=result.transaction.id,
        amount_kopeks=result.transaction.amount_kopeks,
        old_balance_kopeks=result.old_balance_kopeks,
        new_balance_kopeks=result.new_balance_kopeks,
        new_balance_rubles=round(result.new_balance_kopeks / 100, 2),
    )


async def _get_user_by_id_or_telegram_id(db: AsyncSession, user_id: int) -> User:
    """Helper function to get user by ID or telegram_id"""
    user = await get_user_by_telegram_id(db, user_id)
    if user:
        return user

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')
    return user


async def _delete_subscription_if_exists(db: AsyncSession, subscription_id: int) -> None:
    result = await db.execute(select(Subscription).where(Subscription.id == subscription_id))
    subscription = result.scalar_one_or_none()
    if not subscription:
        return
    await db.delete(subscription)
    await db.commit()


@router.post('/{user_id}/subscription', response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user_subscription(
    user_id: int,
    payload: UserSubscriptionCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """
    Создать или заменить подписку для пользователя.
    Поддерживает создание как триальных, так и платных подписок.
    """
    user = await _get_user_by_id_or_telegram_id(db, user_id)

    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        active_subs = await get_active_subscriptions_by_user_id(db, user.id)
        if payload.replace_existing and payload.subscription_id:
            from app.database.crud.subscription import get_subscription_by_id

            existing = await get_subscription_by_id(db, payload.subscription_id)
            if existing and existing.user_id != user.id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Subscription does not belong to this user')
        elif payload.replace_existing and active_subs:
            if len(active_subs) == 1:
                existing = active_subs[0]
            else:
                _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                _pool = _non_daily or active_subs
                existing = max(_pool, key=lambda s: s.days_left)
        else:
            existing = None
        if active_subs and not payload.replace_existing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                'User already has a subscription. Use replace_existing=true to replace it',
            )
    else:
        existing = await get_subscription_by_user_id(db, user.id)
        if existing and not payload.replace_existing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                'User already has a subscription. Use replace_existing=true to replace it',
            )
    previous_state = _snapshot_subscription_state(existing) if existing else None

    forced_devices = None
    if not settings.is_devices_selection_enabled():
        forced_devices = settings.get_disabled_mode_device_limit()

    subscription = None
    try:
        if payload.is_trial:
            trial_device_limit = payload.device_limit
            if trial_device_limit is None:
                trial_device_limit = forced_devices
            duration_days = payload.duration_days or settings.TRIAL_DURATION_DAYS
            traffic_limit_gb = payload.traffic_limit_gb or settings.TRIAL_TRAFFIC_LIMIT_GB

            if existing:
                # Сохраняем существующие сквады при замене
                connected_squads = list(existing.connected_squads or [])
                if payload.squad_uuid:
                    connected_squads = [payload.squad_uuid]
                elif payload.connected_squads:
                    connected_squads = payload.connected_squads

                subscription = await replace_subscription(
                    db,
                    existing,
                    duration_days=duration_days,
                    traffic_limit_gb=traffic_limit_gb,
                    device_limit=(
                        trial_device_limit if trial_device_limit is not None else settings.TRIAL_DEVICE_LIMIT
                    ),
                    connected_squads=connected_squads,
                    is_trial=True,
                    update_server_counters=True,
                )
            else:
                subscription = await create_trial_subscription(
                    db,
                    user_id=user.id,
                    duration_days=duration_days,
                    traffic_limit_gb=traffic_limit_gb,
                    device_limit=trial_device_limit,
                    squad_uuid=payload.squad_uuid,
                )
        else:
            if payload.duration_days is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, 'duration_days is required for paid subscriptions')
            device_limit = payload.device_limit
            if device_limit is None:
                if forced_devices is not None:
                    device_limit = forced_devices
                else:
                    device_limit = settings.DEFAULT_DEVICE_LIMIT

            if existing:
                subscription = await replace_subscription(
                    db,
                    existing,
                    duration_days=payload.duration_days,
                    traffic_limit_gb=payload.traffic_limit_gb or settings.DEFAULT_TRAFFIC_LIMIT_GB,
                    device_limit=device_limit,
                    connected_squads=payload.connected_squads or [],
                    is_trial=False,
                    update_server_counters=True,
                )
            else:
                subscription = await create_paid_subscription(
                    db,
                    user_id=user.id,
                    duration_days=payload.duration_days,
                    traffic_limit_gb=payload.traffic_limit_gb or settings.DEFAULT_TRAFFIC_LIMIT_GB,
                    device_limit=device_limit,
                    connected_squads=payload.connected_squads or [],
                    update_server_counters=True,
                )

        subscription_service = SubscriptionService()
        rem_user = await subscription_service.update_remnawave_user(db, subscription, reset_traffic=False)
        if not rem_user:
            rem_user = await subscription_service.create_remnawave_user(db, subscription, reset_traffic=False)
        if not rem_user:
            raise ValueError('Failed to create/update user in Remnawave')
    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to sync user subscription with Remnawave', user_id=user.id)
        try:
            if existing and previous_state is not None:
                await _restore_subscription_state(db, existing.id, previous_state)
            elif subscription is not None:
                await _delete_subscription_if_exists(db, subscription.id)
        except Exception:
            logger.exception('Failed to rollback user subscription mutation', user_id=user.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to sync with Remnawave',
        )

    # Перезагружаем пользователя с подпиской
    user = await get_user_by_id(db, user.id)
    return _serialize_user(user)


@router.patch('/{user_id}/subscription', response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def patch_user_subscription(
    user_id: int,
    payload: UserSubscriptionCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    return await create_user_subscription(user_id, payload, _, db)


@router.delete('/{user_id}/subscription', response_model=UserResponse)
async def delete_user_subscription(
    user_id: int,
    subscription_id: int | None = None,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """
    Деактивировать подписку пользователя.
    Подписка не удаляется физически, а помечается как DISABLED.
    In multi-tariff mode, subscription_id query param specifies which subscription to deactivate.
    """
    user = await _get_user_by_id_or_telegram_id(db, user_id)

    if settings.is_multi_tariff_enabled():
        if subscription_id:
            from app.database.crud.subscription import get_subscription_by_id

            subscription = await get_subscription_by_id(db, subscription_id)
            if subscription and subscription.user_id != user.id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Subscription does not belong to this user')
        else:
            from app.database.crud.subscription import get_active_subscriptions_by_user_id

            active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            if not active_subs:
                subscription = None
            elif len(active_subs) == 1:
                subscription = active_subs[0]
            else:
                _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                _pool = _non_daily or active_subs
                subscription = max(_pool, key=lambda s: s.days_left)
    else:
        subscription = await get_subscription_by_user_id(db, user.id)
    if not subscription:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User has no subscription')

    # Подписка деактивируется — СБП-автопродление Platega обязано быть отменено,
    # иначе следующий push-коллбек продлит и заново включит её, а банк продолжит списывать.
    from app.services.payment.lava import cancel_lava_recurring_for_subscription_safe
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    await cancel_platega_recurring_for_subscription_safe(db, subscription.id)

    await cancel_lava_recurring_for_subscription_safe(db, subscription.id)
    await deactivate_subscription(db, subscription)

    # Деактивируем пользователя в RemnaWave, если есть панельная идентичность
    panel_user_id = subscription.remnawave_id if settings.is_multi_tariff_enabled() else user.remnawave_id
    if panel_user_id:
        subscription_service = SubscriptionService()
        await subscription_service.disable_remnawave_user(panel_user_id)

    # Перезагружаем пользователя
    user = await get_user_by_id(db, user.id)
    return _serialize_user(user)
