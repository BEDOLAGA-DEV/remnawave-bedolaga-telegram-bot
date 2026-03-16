import secrets
import string
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, case, func, nullslast, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.discount_offer import get_latest_claimed_offer_for_user
from app.database.crud.promo_group import get_default_promo_group
from app.database.crud.promo_offer_log import log_promo_offer_action
from app.database.models import (
    PaymentMethod,
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
    UserPromoGroup,
    UserStatus,
)
from app.utils.validators import sanitize_telegram_name


logger = structlog.get_logger(__name__)


def _normalize_language_code(language: str | None, fallback: str = 'ru') -> str:
    normalized = (language or '').strip().lower()
    if '-' in normalized:
        normalized = normalized.split('-', 1)[0]
    return normalized or fallback


def _build_spending_stats_select():
    """
    Возвращает базовый SELECT для статистики трат пользователей.

    Используется в:
    - get_users_list() для сортировки по тратам/покупкам
    - get_users_spending_stats() для получения статистики

    Returns:
        Tuple колонок (user_id, total_spent, purchase_count)
    """

    return (
        Transaction.user_id.label('user_id'),
        func.coalesce(
            func.sum(
                case(
                    (
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        func.abs(Transaction.amount_kopeks),
                    ),
                    else_=0,
                )
            ),
            0,
        ).label('total_spent'),
        func.coalesce(
            func.sum(
                case(
                    (
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label('purchase_count'),
    )


def generate_referral_code() -> str:
    alphabet = string.ascii_letters + string.digits
    code_suffix = ''.join(secrets.choice(alphabet) for _ in range(8))
    return f'ref{code_suffix}'


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription).selectinload(Subscription.tariff),
            selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            selectinload(User.referrer),
            selectinload(User.promo_group),
        )
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()

    if user and user.subscription:
        # Загружаем дополнительные зависимости для subscription
        _ = user.subscription.is_active

    return user


async def get_user_by_telegram_id(db: AsyncSession, telegram_id: int) -> User | None:
    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription).selectinload(Subscription.tariff),
            selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            selectinload(User.referrer),
            selectinload(User.promo_group),
        )
        .where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()

    if user and user.subscription:
        # Загружаем дополнительные зависимости для subscription
        _ = user.subscription.is_active

    return user


async def find_phantom_user_by_username(db: AsyncSession, username: str) -> User | None:
    """Find a phantom user created by guest purchase (no telegram_id, auth_type=telegram).

    Used during /start to reconcile phantom users with real Telegram accounts.
    """
    if not username:
        return None

    normalized = username.lower()
    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription).selectinload(Subscription.tariff),
        )
        .where(
            User.telegram_id.is_(None),
            User.auth_type == 'telegram',
            func.lower(User.username) == normalized,
        )
        .with_for_update()
    )
    return result.scalars().first()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    if not username:
        return None

    normalized = username.lower()

    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription).selectinload(Subscription.tariff),
            selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            selectinload(User.referrer),
            selectinload(User.promo_group),
        )
        .where(func.lower(User.username) == normalized)
    )

    user = result.scalar_one_or_none()

    if user and user.subscription:
        # Загружаем дополнительные зависимости для subscription
        _ = user.subscription.is_active

    return user


async def get_user_by_referral_code(db: AsyncSession, referral_code: str) -> User | None:
    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription).selectinload(Subscription.tariff),
            selectinload(User.promo_group),
            selectinload(User.referrer),
        )
        .where(User.referral_code == referral_code)
    )
    user = result.scalar_one_or_none()

    if user and user.subscription:
        # Загружаем дополнительные зависимости для subscription
        _ = user.subscription.is_active

    return user


async def get_user_by_remnawave_uuid(db: AsyncSession, remnawave_uuid: str) -> User | None:
    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription).selectinload(Subscription.tariff),
            selectinload(User.promo_group),
            selectinload(User.referrer),
        )
        .where(User.remnawave_uuid == remnawave_uuid)
    )
    user = result.scalar_one_or_none()

    if user and user.subscription:
        # Загружаем дополнительные зависимости для subscription
        _ = user.subscription.is_active

    return user


async def create_unique_referral_code(db: AsyncSession) -> str:
    max_attempts = 10

    for _ in range(max_attempts):
        code = generate_referral_code()
        existing_user = await get_user_by_referral_code(db, code)
        if not existing_user:
            return code

    timestamp = str(int(datetime.now(UTC).timestamp()))[-6:]
    return f'ref{timestamp}'


async def _sync_users_sequence(db: AsyncSession) -> None:
    """Ensure the users.id sequence matches the current max ID."""
    await db.execute(text("SELECT setval('users_id_seq', COALESCE((SELECT MAX(id) FROM users), 0) + 1, false)"))
    await db.commit()
    logger.warning('🔄 Последовательность users_id_seq была синхронизирована с текущим максимумом id')


async def _get_or_create_default_promo_group(db: AsyncSession) -> PromoGroup:
    default_group = await get_default_promo_group(db)
    if default_group:
        return default_group

    default_group = PromoGroup(
        name='Базовый юзер',
        server_discount_percent=0,
        traffic_discount_percent=0,
        device_discount_percent=0,
        is_default=True,
    )
    db.add(default_group)
    await db.flush()
    return default_group


async def create_user_no_commit(
    db: AsyncSession,
    telegram_id: int,
    username: str = None,
    first_name: str = None,
    last_name: str = None,
    language: str = 'ru',
    referred_by_id: int = None,
    referral_code: str = None,
) -> User:
    """
    Создает пользователя без немедленного коммита для пакетной обработки
    """

    if not referral_code:
        referral_code = await create_unique_referral_code(db)
    normalized_language = _normalize_language_code(language)

    default_group = await _get_or_create_default_promo_group(db)
    promo_group_id = default_group.id

    safe_first = sanitize_telegram_name(first_name)
    safe_last = sanitize_telegram_name(last_name)
    user = User(
        telegram_id=telegram_id,
        username=username,
        first_name=safe_first,
        last_name=safe_last,
        language=normalized_language,
        referred_by_id=referred_by_id,
        referral_code=referral_code,
        balance_kopeks=0,
        has_had_paid_subscription=False,
        has_made_first_topup=False,
        promo_group_id=promo_group_id,
    )

    db.add(user)

    # Обязательно выполняем flush, чтобы получить присвоенный первичный ключ
    await db.flush()

    # Сохраняем ссылку на группу, чтобы дальнейшие операции могли её использовать
    user.promo_group = default_group

    # Не коммитим сразу, оставляем для пакетной обработки
    logger.info(
        '✅ Подготовлен пользователь с реферальным кодом (ожидает коммита)',
        telegram_id=telegram_id,
        referral_code=referral_code,
    )
    return user


async def create_user(
    db: AsyncSession,
    telegram_id: int,
    username: str = None,
    first_name: str = None,
    last_name: str = None,
    language: str = 'ru',
    referred_by_id: int = None,
    referral_code: str = None,
) -> User:
    if not referral_code:
        referral_code = await create_unique_referral_code(db)
    normalized_language = _normalize_language_code(language)

    attempts = 3

    for attempt in range(1, attempts + 1):
        default_group = await _get_or_create_default_promo_group(db)
        promo_group_id = default_group.id

        safe_first = sanitize_telegram_name(first_name)
        safe_last = sanitize_telegram_name(last_name)
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=safe_first,
            last_name=safe_last,
            language=normalized_language,
            referred_by_id=referred_by_id,
            referral_code=referral_code,
            balance_kopeks=0,
            has_had_paid_subscription=False,
            has_made_first_topup=False,
            promo_group_id=promo_group_id,
        )

        db.add(user)

        try:
            await db.commit()
            await db.refresh(user)

            user.promo_group = default_group
            logger.info(
                '✅ Создан пользователь с реферальным кодом', telegram_id=telegram_id, referral_code=referral_code
            )

            # Отправляем событие о создании пользователя
            try:
                from app.services.event_emitter import event_emitter

                await event_emitter.emit(
                    'user.created',
                    {
                        'user_id': user.id,
                        'telegram_id': user.telegram_id,
                        'username': user.username,
                        'first_name': user.first_name,
                        'last_name': user.last_name,
                        'referral_code': user.referral_code,
                        'referred_by_id': user.referred_by_id,
                    },
                    db=db,
                )
            except Exception as error:
                logger.warning('Failed to emit user.created event', error=error)

            return user

        except IntegrityError as exc:
            await db.rollback()

            if (
                isinstance(getattr(exc, 'orig', None), Exception)
                and 'users_pkey' in str(exc.orig)
                and attempt < attempts
            ):
                logger.warning(
                    '⚠️ Обнаружено несоответствие последовательности users_id_seq при создании пользователя . Выполняем повторную синхронизацию (попытка /)',
                    telegram_id=telegram_id,
                    attempt=attempt,
                    attempts=attempts,
                )
                await _sync_users_sequence(db)
                continue

            raise

    raise RuntimeError('Не удалось создать пользователя после синхронизации последовательности')


async def update_user(db: AsyncSession, user: User, **kwargs) -> User:
    from app.utils.validators import sanitize_telegram_name

    for field, value in kwargs.items():
        if field in ('first_name', 'last_name'):
            value = sanitize_telegram_name(value)
        if field == 'language':
            value = _normalize_language_code(value)
        if hasattr(user, field):
            setattr(user, field, value)

    user.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(user)

    return user


async def lock_user_for_update(db: AsyncSession, user: User) -> User:
    """Lock user row with SELECT FOR UPDATE to prevent concurrent balance modifications.

    Returns the refreshed user object with current DB values.
    Must be called within an active transaction before modifying balance_kopeks.
    Eagerly loads key relationships to avoid MissingGreenlet in async context.
    """
    result = await db.execute(
        select(User)
        .where(User.id == user.id)
        .options(
            selectinload(User.subscription),
            selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            selectinload(User.promo_group),
            selectinload(User.referrer),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def add_user_balance(
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    description: str = 'Пополнение баланса',
    create_transaction: bool = True,
    transaction_type: TransactionType = TransactionType.DEPOSIT,
    bot=None,
    payment_method: PaymentMethod | None = None,
) -> bool:
    try:
        # Lock the user row to prevent concurrent balance race conditions
        # Eagerly load key relationships to avoid MissingGreenlet in async context
        locked_result = await db.execute(
            select(User)
            .where(User.id == user.id)
            .options(
                selectinload(User.subscription),
                selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
                selectinload(User.promo_group),
                selectinload(User.referrer),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        user = locked_result.scalar_one()

        old_balance = user.balance_kopeks
        user.balance_kopeks += amount_kopeks
        user.updated_at = datetime.now(UTC)

        if create_transaction:
            from app.database.crud.transaction import create_transaction as create_trans

            await create_trans(
                db=db,
                user_id=user.id,
                type=transaction_type,
                amount_kopeks=amount_kopeks,
                description=description,
                payment_method=payment_method,
            )

        await db.commit()
        await db.refresh(user)

        user_id_display = user.telegram_id or user.email or f'#{user.id}'
        logger.info(
            '💰 Баланс пользователя изменен: → (изменение: +)',
            user_id_display=user_id_display,
            old_balance=old_balance,
            balance_kopeks=user.balance_kopeks,
            amount_kopeks=amount_kopeks,
        )

        # Авто-возобновление суточной подписки НЕ делаем здесь —
        # это обязанность try_resume_disabled_daily_after_topup (через send_cart_notification_after_topup)
        # и DailySubscriptionService.process_auto_resume (30-минутный цикл).
        # Они корректно списывают суточную плату при возобновлении.

        return True

    except Exception as e:
        logger.error('Ошибка изменения баланса пользователя', user_id=user.id, error=e)
        await db.rollback()
        return False


async def add_user_balance_by_id(
    db: AsyncSession,
    telegram_id: int,
    amount_kopeks: int,
    description: str = 'Пополнение баланса',
    transaction_type: TransactionType = TransactionType.DEPOSIT,
    payment_method: PaymentMethod | None = None,
) -> bool:
    try:
        user = await get_user_by_telegram_id(db, telegram_id)
        if not user:
            logger.error('Пользователь с telegram_id не найден', telegram_id=telegram_id)
            return False

        return await add_user_balance(
            db,
            user,
            amount_kopeks,
            description,
            transaction_type=transaction_type,
            payment_method=payment_method,
        )

    except Exception as e:
        logger.error('Ошибка пополнения баланса пользователя', telegram_id=telegram_id, error=e)
        return False


async def subtract_user_balance(
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    description: str,
    create_transaction: bool = False,
    payment_method: PaymentMethod | None = None,
    *,
    transaction_type: TransactionType = TransactionType.WITHDRAWAL,
    consume_promo_offer: bool = False,
    mark_as_paid_subscription: bool = False,
    commit: bool = True,
) -> bool:
    if amount_kopeks < 0:
        logger.error('subtract_user_balance called with negative amount', amount_kopeks=amount_kopeks, user_id=user.id)
        return False

    logger.debug(
        'subtract_user_balance called',
        user_id=user.id,
        balance_kopeks=user.balance_kopeks,
        amount_kopeks=amount_kopeks,
        description=description,
    )

    # Lock the user row to prevent concurrent balance race conditions
    # Eagerly load key relationships to avoid MissingGreenlet in async context
    locked_result = await db.execute(
        select(User)
        .where(User.id == user.id)
        .options(
            selectinload(User.subscription),
            selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            selectinload(User.promo_group),
            selectinload(User.referrer),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    user = locked_result.scalar_one()

    log_context: dict[str, object] | None = None
    if consume_promo_offer:
        try:
            current_percent = int(getattr(user, 'promo_offer_discount_percent', 0) or 0)
        except (TypeError, ValueError):
            current_percent = 0

        if current_percent > 0:
            source = getattr(user, 'promo_offer_discount_source', None)
            log_context = {
                'offer_id': None,
                'percent': current_percent,
                'source': source,
                'effect_type': None,
                'details': {
                    'reason': 'manual_charge',
                    'description': description,
                    'amount_kopeks': amount_kopeks,
                },
            }
            try:
                offer = await get_latest_claimed_offer_for_user(db, user.id, source)
            except Exception as lookup_error:  # pragma: no cover - defensive logging
                logger.warning(
                    'Failed to fetch latest claimed promo offer for user', user_id=user.id, lookup_error=lookup_error
                )
                offer = None

            if offer:
                log_context['offer_id'] = offer.id
                log_context['effect_type'] = offer.effect_type
                if not log_context['percent'] and offer.discount_percent:
                    log_context['percent'] = offer.discount_percent

    if user.balance_kopeks < amount_kopeks:
        logger.error('   ❌ НЕДОСТАТОЧНО СРЕДСТВ!')
        return False

    try:
        old_balance = user.balance_kopeks
        user.balance_kopeks -= amount_kopeks

        if consume_promo_offer and getattr(user, 'promo_offer_discount_percent', 0):
            user.promo_offer_discount_percent = 0
            user.promo_offer_discount_source = None
            user.promo_offer_discount_expires_at = None

        if mark_as_paid_subscription:
            user.has_had_paid_subscription = True

        user.updated_at = datetime.now(UTC)

        if create_transaction:
            from app.database.crud.transaction import (
                create_transaction as create_trans,
            )

            await create_trans(
                db=db,
                user_id=user.id,
                type=transaction_type,
                amount_kopeks=amount_kopeks,
                description=description,
                payment_method=payment_method,
                commit=commit,
            )
        elif commit:
            await db.commit()
        else:
            await db.flush()

        if commit:
            await db.refresh(user)

        if consume_promo_offer and log_context:
            try:
                await log_promo_offer_action(
                    db,
                    user_id=user.id,
                    offer_id=log_context.get('offer_id'),
                    action='consumed',
                    source=log_context.get('source'),
                    percent=log_context.get('percent'),
                    effect_type=log_context.get('effect_type'),
                    details=log_context.get('details'),
                    commit=commit,
                )
            except Exception as log_error:  # pragma: no cover - defensive logging
                logger.warning(
                    'Failed to record promo offer consumption log for user', user_id=user.id, log_error=log_error
                )
                if commit:
                    try:
                        await db.rollback()
                    except Exception as rollback_error:  # pragma: no cover - defensive logging
                        logger.warning(
                            'Failed to rollback session after promo offer consumption log failure',
                            rollback_error=rollback_error,
                        )

        logger.info('✅ Средства списаны: →', old_balance=old_balance, balance_kopeks=user.balance_kopeks)
        return True

    except Exception as e:
        logger.error('❌ ОШИБКА СПИСАНИЯ', error=e)
        if commit:
            await db.rollback()
            return False
        raise


async def cleanup_expired_promo_offer_discounts(db: AsyncSession) -> int:
    now = datetime.now(UTC)
    result = await db.execute(
        select(User).where(
            User.promo_offer_discount_percent > 0,
            User.promo_offer_discount_expires_at.isnot(None),
            User.promo_offer_discount_expires_at <= now,
        )
    )
    users = result.scalars().all()
    if not users:
        return 0

    log_payloads: list[dict[str, object]] = []

    for user in users:
        try:
            percent = int(getattr(user, 'promo_offer_discount_percent', 0) or 0)
        except (TypeError, ValueError):
            percent = 0

        source = getattr(user, 'promo_offer_discount_source', None)
        offer_id = None
        effect_type = None

        if source:
            try:
                offer = await get_latest_claimed_offer_for_user(db, user.id, source)
            except Exception as lookup_error:  # pragma: no cover - defensive logging
                logger.warning(
                    'Failed to fetch latest claimed promo offer for user during expiration cleanup',
                    user_id=user.id,
                    lookup_error=lookup_error,
                )
                offer = None

            if offer:
                offer_id = offer.id
                effect_type = offer.effect_type
                if not percent and offer.discount_percent:
                    percent = offer.discount_percent

        log_payloads.append(
            {
                'user_id': user.id,
                'offer_id': offer_id,
                'source': source,
                'percent': percent,
                'effect_type': effect_type,
            }
        )

        user.promo_offer_discount_percent = 0
        user.promo_offer_discount_source = None
        user.promo_offer_discount_expires_at = None
        user.updated_at = now

    await db.commit()

    for payload in log_payloads:
        user_id = payload.get('user_id')
        if not user_id:
            continue
        try:
            await log_promo_offer_action(
                db,
                user_id=user_id,
                offer_id=payload.get('offer_id'),
                action='disabled',
                source=payload.get('source'),
                percent=payload.get('percent'),
                effect_type=payload.get('effect_type'),
                details={'reason': 'offer_expired'},
            )
        except Exception as log_error:  # pragma: no cover - defensive logging
            logger.warning('Failed to log promo offer expiration for user', user_id=user_id, log_error=log_error)
            try:
                await db.rollback()
            except Exception as rollback_error:  # pragma: no cover - defensive logging
                logger.warning(
                    'Failed to rollback session after promo offer expiration log failure', rollback_error=rollback_error
                )

    return len(users)


async def get_users_list(
    db: AsyncSession,
    offset: int = 0,
    limit: int = 50,
    search: str | None = None,
    email: str | None = None,
    status: UserStatus | None = None,
    order_by_balance: bool = False,
    order_by_traffic: bool = False,
    order_by_last_activity: bool = False,
    order_by_total_spent: bool = False,
    order_by_purchase_count: bool = False,
) -> list[User]:
    query = select(User).options(
        selectinload(User.subscription).selectinload(Subscription.tariff),
        selectinload(User.promo_group),
        selectinload(User.referrer),
    )

    if status:
        query = query.where(User.status == status.value)

    if search:
        search_term = f'%{search}%'
        conditions = [
            User.first_name.ilike(search_term),
            User.last_name.ilike(search_term),
            User.username.ilike(search_term),
        ]

        if search.isdigit():
            try:
                search_int = int(search)
                # Добавляем условие поиска по telegram_id, который является BigInteger
                # и может содержать большие значения, в отличие от User.id (INTEGER)
                conditions.append(User.telegram_id == search_int)
            except ValueError:
                # Если не удалось преобразовать в int, просто ищем по текстовым полям
                pass

        query = query.where(or_(*conditions))

    if email:
        query = query.where(User.email.ilike(f'%{email}%'))

    sort_flags = [
        order_by_balance,
        order_by_traffic,
        order_by_last_activity,
        order_by_total_spent,
        order_by_purchase_count,
    ]
    if sum(int(flag) for flag in sort_flags) > 1:
        logger.debug(
            'Выбрано несколько сортировок пользователей — применяется приоритет: трафик > траты > покупки > баланс > активность'
        )

    transactions_stats = None
    if order_by_total_spent or order_by_purchase_count:
        from app.database.models import Transaction

        transactions_stats = (
            select(*_build_spending_stats_select())
            .where(Transaction.is_completed.is_(True))
            .group_by(Transaction.user_id)
            .subquery()
        )
        query = query.outerjoin(transactions_stats, transactions_stats.c.user_id == User.id)

    if order_by_traffic:
        traffic_sort = func.coalesce(Subscription.traffic_used_gb, 0.0)
        query = query.outerjoin(Subscription, Subscription.user_id == User.id)
        query = query.order_by(traffic_sort.desc(), User.created_at.desc())
    elif order_by_total_spent:
        order_column = func.coalesce(transactions_stats.c.total_spent, 0)
        query = query.order_by(order_column.desc(), User.created_at.desc())
    elif order_by_purchase_count:
        order_column = func.coalesce(transactions_stats.c.purchase_count, 0)
        query = query.order_by(order_column.desc(), User.created_at.desc())
    elif order_by_balance:
        query = query.order_by(User.balance_kopeks.desc(), User.created_at.desc())
    elif order_by_last_activity:
        query = query.order_by(nullslast(User.last_activity.desc()), User.created_at.desc())
    else:
        query = query.order_by(User.created_at.desc())

    query = query.offset(offset).limit(limit)

    result = await db.execute(query)
    users = result.scalars().all()

    # Загружаем дополнительные зависимости для всех пользователей
    for user in users:
        if user and user.subscription:
            # Загружаем дополнительные зависимости для subscription
            _ = user.subscription.is_active

    return users


async def get_users_count(
    db: AsyncSession, status: UserStatus | None = None, search: str | None = None, email: str | None = None
) -> int:
    query = select(func.count(User.id))

    if status:
        query = query.where(User.status == status.value)

    if search:
        search_term = f'%{search}%'
        conditions = [
            User.first_name.ilike(search_term),
            User.last_name.ilike(search_term),
            User.username.ilike(search_term),
        ]

        if search.isdigit():
            try:
                search_int = int(search)
                # Добавляем условие поиска по telegram_id, который является BigInteger
                # и может содержать большие значения, в отличие от User.id (INTEGER)
                conditions.append(User.telegram_id == search_int)
            except ValueError:
                # Если не удалось преобразовать в int, просто ищем по текстовым полям
                pass

        query = query.where(or_(*conditions))

    if email:
        query = query.where(User.email.ilike(f'%{email}%'))

    result = await db.execute(query)
    return result.scalar()


async def get_users_spending_stats(db: AsyncSession, user_ids: list[int]) -> dict[int, dict[str, int]]:
    """
    Получает статистику трат для списка пользователей.

    Args:
        db: Сессия базы данных
        user_ids: Список ID пользователей

    Returns:
        Словарь {user_id: {"total_spent": int, "purchase_count": int}}
    """
    if not user_ids:
        return {}

    stats_query = (
        select(*_build_spending_stats_select())
        .where(
            Transaction.user_id.in_(user_ids),
            Transaction.is_completed.is_(True),
        )
        .group_by(Transaction.user_id)
    )

    result = await db.execute(stats_query)
    rows = result.all()

    return {
        row.user_id: {
            'total_spent': int(row.total_spent or 0),
            'purchase_count': int(row.purchase_count or 0),
        }
        for row in rows
    }


async def get_referrers_with_counts(
    db: AsyncSession,
    limit: int = 500,
    days: int | None = None,
    sort_by: str = 'referrals',
) -> tuple[list[User], dict[int, int], dict[int, int]]:
    """Return (users sorted by sort_by, user_id -> referrals_count, user_id -> total_earnings_kopeks).

    days: 7, 30, 60 or None for all time (filters both referral count and earnings by period).
    sort_by: 'referrals' (by count) or 'earnings'.
    """
    from app.database.models import ReferralEarning

    since = (datetime.now(UTC) - timedelta(days=days)) if days else None

    count_stmt = select(User.referred_by_id.label('referrer_id'), func.count(User.id).label('cnt')).where(
        User.referred_by_id.isnot(None)
    )
    if since is not None:
        count_stmt = count_stmt.where(User.created_at >= since)
    count_stmt = count_stmt.group_by(User.referred_by_id)
    ref_result = await db.execute(count_stmt)
    ref_rows = ref_result.all()
    if not ref_rows:
        return [], {}, {}
    referrals_count_by_id = {row.referrer_id: row.cnt for row in ref_rows}
    referrer_ids = list(referrals_count_by_id.keys())

    earnings_stmt = select(
        ReferralEarning.user_id.label('referrer_id'),
        func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0).label('total'),
    ).where(ReferralEarning.user_id.in_(referrer_ids))
    if since is not None:
        earnings_stmt = earnings_stmt.where(ReferralEarning.created_at >= since)
    earnings_stmt = earnings_stmt.group_by(ReferralEarning.user_id)
    earn_result = await db.execute(earnings_stmt)
    earnings_by_id = {row.referrer_id: int(row.total) for row in earn_result.all()}

    if sort_by == 'earnings':
        sorted_ids = sorted(
            referrer_ids,
            key=lambda uid: (earnings_by_id.get(uid, 0), referrals_count_by_id.get(uid, 0)),
            reverse=True,
        )[:limit]
    else:
        sorted_ids = sorted(
            referrer_ids,
            key=lambda uid: (referrals_count_by_id.get(uid, 0), earnings_by_id.get(uid, 0)),
            reverse=True,
        )[:limit]
    if not sorted_ids:
        return [], {}, {}

    users_result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription).selectinload(Subscription.tariff),
            selectinload(User.promo_group),
        )
        .where(User.id.in_(sorted_ids))
    )
    users_by_id = {u.id: u for u in users_result.scalars().all()}
    users = [users_by_id[uid] for uid in sorted_ids if uid in users_by_id]
    return users, referrals_count_by_id, earnings_by_id


async def get_referrals(db: AsyncSession, user_id: int) -> list[User]:
    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription).selectinload(Subscription.tariff),
            selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            selectinload(User.referrer),
            selectinload(User.promo_group),
        )
        .where(User.referred_by_id == user_id)
        .order_by(User.created_at.desc())
    )
    users = result.scalars().all()

    # Загружаем дополнительные зависимости для всех пользователей
    for user in users:
        if user and user.subscription:
            # Загружаем дополнительные зависимости для subscription
            _ = user.subscription.is_active

    return users


async def get_users_for_promo_segment(db: AsyncSession, segment: str) -> list[User]:
    now = datetime.now(UTC)

    base_query = (
        select(User)
        .options(
            selectinload(User.subscription).selectinload(Subscription.tariff),
            selectinload(User.promo_group),
            selectinload(User.referrer),
        )
        .where(User.status == UserStatus.ACTIVE.value)
    )

    if segment == 'no_subscription':
        query = base_query.outerjoin(Subscription, Subscription.user_id == User.id).where(Subscription.id.is_(None))
    else:
        query = base_query.join(Subscription)

        if segment == 'paid_active':
            query = query.where(
                Subscription.is_trial == False,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date > now,
            )
        elif segment == 'paid_expired':
            query = query.where(
                Subscription.is_trial == False,
                or_(
                    Subscription.status == SubscriptionStatus.EXPIRED.value,
                    Subscription.end_date <= now,
                ),
            )
        elif segment == 'trial_active':
            query = query.where(
                Subscription.is_trial == True,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date > now,
            )
        elif segment == 'trial_expired':
            query = query.where(
                Subscription.is_trial == True,
                or_(
                    Subscription.status == SubscriptionStatus.EXPIRED.value,
                    Subscription.end_date <= now,
                ),
            )
        else:
            logger.warning('Неизвестный сегмент для промо', segment=segment)
            return []

    result = await db.execute(query.order_by(User.id))
    users = result.scalars().unique().all()

    # Загружаем дополнительные зависимости для всех пользователей
    for user in users:
        if user and user.subscription:
            # Загружаем дополнительные зависимости для subscription
            _ = user.subscription.is_active

    return users


async def get_inactive_users(db: AsyncSession, months: int = 3) -> list[User]:
    threshold_date = datetime.now(UTC) - timedelta(days=months * 30)

    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription).selectinload(Subscription.tariff),
            selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            selectinload(User.referrer),
            selectinload(User.promo_group),
        )
        .where(and_(User.last_activity < threshold_date, User.status == UserStatus.ACTIVE.value))
    )
    users = result.scalars().all()

    # Загружаем дополнительные зависимости для всех пользователей
    for user in users:
        if user and user.subscription:
            # Загружаем дополнительные зависимости для subscription
            _ = user.subscription.is_active

    return users


async def delete_user(db: AsyncSession, user: User) -> bool:
    user.status = UserStatus.DELETED.value
    user.updated_at = datetime.now(UTC)

    await db.commit()
    user_id_display = user.telegram_id or user.email or f'#{user.id}'
    logger.info('🗑️ Пользователь помечен как удаленный', user_id_display=user_id_display)
    return True


async def get_users_statistics(db: AsyncSession) -> dict:
    total_result = await db.execute(select(func.count(User.id)))
    total_users = total_result.scalar()

    active_result = await db.execute(select(func.count(User.id)).where(User.status == UserStatus.ACTIVE.value))
    active_users = active_result.scalar()

    today = datetime.now(UTC).date()
    today_result = await db.execute(
        select(func.count(User.id)).where(and_(User.created_at >= today, User.status == UserStatus.ACTIVE.value))
    )
    new_today = today_result.scalar()

    week_ago = datetime.now(UTC) - timedelta(days=7)
    week_result = await db.execute(
        select(func.count(User.id)).where(and_(User.created_at >= week_ago, User.status == UserStatus.ACTIVE.value))
    )
    new_week = week_result.scalar()

    month_ago = datetime.now(UTC) - timedelta(days=30)
    month_result = await db.execute(
        select(func.count(User.id)).where(and_(User.created_at >= month_ago, User.status == UserStatus.ACTIVE.value))
    )
    new_month = month_result.scalar()

    return {
        'total_users': total_users,
        'active_users': active_users,
        'blocked_users': total_users - active_users,
        'new_today': new_today,
        'new_week': new_week,
        'new_month': new_month,
    }


async def get_users_with_active_subscriptions(db: AsyncSession) -> list[User]:
    """
    Получает список пользователей с активными подписками.
    Используется для мониторинга трафика.

    Returns:
        Список пользователей с активными подписками и remnawave_uuid
    """
    current_time = datetime.now(UTC)

    result = await db.execute(
        select(User)
        .join(Subscription, User.id == Subscription.user_id)
        .where(
            and_(
                User.remnawave_uuid.isnot(None),
                User.status == UserStatus.ACTIVE.value,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date > current_time,
            )
        )
        .options(selectinload(User.subscription).selectinload(Subscription.tariff))
    )

    return result.scalars().unique().all()


async def create_user_by_email(
    db: AsyncSession,
    email: str,
    password_hash: str,
    first_name: str | None = None,
    language: str = 'ru',
    referred_by_id: int | None = None,
) -> User:
    """
    Создать пользователя через email регистрацию (без Telegram).

    Args:
        db: Database session
        email: Email address (will be unverified initially)
        password_hash: Hashed password
        first_name: Optional first name
        language: User language
        referred_by_id: Referrer user ID

    Returns:
        Created User object
    """
    referral_code = await create_unique_referral_code(db)
    normalized_language = _normalize_language_code(language)
    default_group = await _get_or_create_default_promo_group(db)

    user = User(
        telegram_id=None,  # Email-only user
        auth_type='email',
        email=email,
        email_verified=False,
        password_hash=password_hash,
        username=None,
        first_name=sanitize_telegram_name(first_name) if first_name else None,
        last_name=None,
        language=normalized_language,
        referred_by_id=referred_by_id,
        referral_code=referral_code,
        balance_kopeks=0,
        has_had_paid_subscription=False,
        has_made_first_topup=False,
        promo_group_id=default_group.id,
    )

    db.add(user)
    await db.commit()
    await db.refresh(user)

    user.promo_group = default_group
    logger.info('✅ Создан email-пользователь с id', email=email, user_id=user.id)

    # Emit event
    try:
        from app.services.event_emitter import event_emitter

        await event_emitter.emit(
            'user.created',
            {
                'user_id': user.id,
                'email': user.email,
                'auth_type': 'email',
                'first_name': user.first_name,
                'referral_code': user.referral_code,
                'referred_by_id': user.referred_by_id,
            },
            db=db,
        )
    except Exception as error:
        logger.warning('Failed to emit user.created event', error=error)

    return user


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Get user by email address (case-insensitive)."""
    if not email or not email.strip():
        return None
    email_lower = email.strip().lower()
    result = await db.execute(select(User).where(func.lower(User.email) == email_lower))
    return result.scalar_one_or_none()


async def is_email_taken(db: AsyncSession, email: str, exclude_user_id: int | None = None) -> bool:
    """
    Check if email is already taken by another user.

    Args:
        db: Database session
        email: Email to check
        exclude_user_id: User ID to exclude from check (for current user)

    Returns:
        True if email is taken, False otherwise
    """
    if not email or not email.strip():
        return False
    email_lower = email.strip().lower()
    query = select(User.id).where(func.lower(User.email) == email_lower)
    if exclude_user_id:
        query = query.where(User.id != exclude_user_id)
    result = await db.execute(query)
    return result.scalar_one_or_none() is not None


async def set_email_change_pending(
    db: AsyncSession,
    user: User,
    new_email: str,
    code: str,
    expires_at: datetime,
) -> User:
    """
    Set pending email change for user.

    Args:
        db: Database session
        user: User object
        new_email: New email address
        code: 6-digit verification code
        expires_at: Code expiration datetime

    Returns:
        Updated User object
    """
    user.email_change_new = new_email
    user.email_change_code = code
    user.email_change_expires = expires_at
    user.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(user)

    logger.info('Email change pending for user', user_id=user.id, email=user.email, new_email=new_email)
    return user


async def verify_and_apply_email_change(db: AsyncSession, user: User, code: str) -> tuple[bool, str]:
    """
    Verify email change code and apply the change.

    Args:
        db: Database session
        user: User object
        code: Verification code from user

    Returns:
        Tuple of (success: bool, message: str)
    """
    if not user.email_change_new or not user.email_change_code:
        return False, 'No pending email change'

    if user.email_change_expires and datetime.now(UTC) > user.email_change_expires:
        # Clear expired data
        user.email_change_new = None
        user.email_change_code = None
        user.email_change_expires = None
        await db.commit()
        return False, 'Verification code has expired'

    if user.email_change_code != code:
        return False, 'Invalid verification code'

    # Check if new email is still available
    existing = await get_user_by_email(db, user.email_change_new)
    if existing and existing.id != user.id:
        user.email_change_new = None
        user.email_change_code = None
        user.email_change_expires = None
        await db.commit()
        return False, 'This email is already taken'

    old_email = user.email
    new_email = user.email_change_new

    # Apply the change
    user.email = new_email
    user.email_verified = True
    user.email_verified_at = datetime.now(UTC)
    user.email_change_new = None
    user.email_change_code = None
    user.email_change_expires = None
    user.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(user)

    logger.info('Email changed for user', user_id=user.id, old_email=old_email, new_email=new_email)
    return True, 'Email changed successfully'


async def clear_email_change_pending(db: AsyncSession, user: User) -> None:
    """
    Clear pending email change data.

    Args:
        db: Database session
        user: User object
    """
    user.email_change_new = None
    user.email_change_code = None
    user.email_change_expires = None
    user.updated_at = datetime.now(UTC)

    await db.commit()
    logger.info('Email change cancelled for user', user_id=user.id)


# --- OAuth provider functions ---

# Single source of truth: provider name → User model column name.
# Imported by account_linking.py and account_merge_service.py.
OAUTH_PROVIDER_COLUMNS: dict[str, str] = {
    'google': 'google_id',
    'yandex': 'yandex_id',
    'discord': 'discord_id',
    'vk': 'vk_id',
}


async def get_user_by_oauth_provider(db: AsyncSession, provider: str, provider_id: str) -> User | None:
    """Find a user by OAuth provider ID."""
    column_name = OAUTH_PROVIDER_COLUMNS.get(provider)
    if not column_name:
        logger.warning('Unknown OAuth provider in lookup', provider=provider)
        return None
    column = getattr(User, column_name)
    # VK uses BigInteger, so convert
    value: str | int = int(provider_id) if provider == 'vk' else provider_id
    result = await db.execute(select(User).where(column == value))
    return result.scalar_one_or_none()


async def set_user_oauth_provider_id(db: AsyncSession, user: User, provider: str, provider_id: str) -> None:
    """Link an OAuth provider ID to an existing user."""
    column_name = OAUTH_PROVIDER_COLUMNS.get(provider)
    if not column_name:
        logger.warning('Unknown OAuth provider in set', provider=provider, user_id=user.id)
        return
    value: str | int = int(provider_id) if provider == 'vk' else provider_id
    setattr(user, column_name, value)
    user.updated_at = datetime.now(UTC)
    logger.info('OAuth provider linked to user', provider=provider, provider_id=provider_id, user_id=user.id)


async def clear_user_oauth_provider_id(db: AsyncSession, user: User, provider: str) -> None:
    """Unlink an OAuth provider from an existing user (set column to None)."""
    column_name = OAUTH_PROVIDER_COLUMNS.get(provider)
    if not column_name:
        logger.warning('Unknown OAuth provider in clear', provider=provider, user_id=user.id)
        return
    setattr(user, column_name, None)
    user.updated_at = datetime.now(UTC)
    logger.info('Unlinked OAuth provider from user', provider=provider, user_id=user.id)


async def create_user_by_oauth(
    db: AsyncSession,
    provider: str,
    provider_id: str,
    email: str | None = None,
    email_verified: bool = False,
    first_name: str | None = None,
    last_name: str | None = None,
    username: str | None = None,
    language: str = 'ru',
    referred_by_id: int | None = None,
) -> User:
    """Create a new user via OAuth provider."""
    referral_code = await create_unique_referral_code(db)
    normalized_language = _normalize_language_code(language)
    default_group = await _get_or_create_default_promo_group(db)

    column_name = OAUTH_PROVIDER_COLUMNS.get(provider)
    provider_value: str | int = int(provider_id) if provider == 'vk' else provider_id

    user = User(
        telegram_id=None,
        auth_type=provider,
        email=email,
        email_verified=email_verified,
        password_hash=None,
        username=sanitize_telegram_name(username) if username else None,
        first_name=sanitize_telegram_name(first_name) if first_name else None,
        last_name=sanitize_telegram_name(last_name) if last_name else None,
        language=normalized_language,
        referred_by_id=referred_by_id,
        referral_code=referral_code,
        balance_kopeks=0,
        has_had_paid_subscription=False,
        has_made_first_topup=False,
        promo_group_id=default_group.id,
    )
    if column_name:
        setattr(user, column_name, provider_value)

    db.add(user)
    await db.flush()
    await db.refresh(user)

    user.promo_group = default_group
    logger.info(
        'Created OAuth user via (provider_id=) with id', provider=provider, provider_id=provider_id, user_id=user.id
    )

    try:
        from app.services.event_emitter import event_emitter

        await event_emitter.emit(
            'user.created',
            {
                'user_id': user.id,
                'email': user.email,
                'auth_type': provider,
                'first_name': user.first_name,
                'referral_code': user.referral_code,
            },
            db=db,
        )
    except Exception as error:
        logger.warning('Failed to emit user.created event', error=error)

    return user
