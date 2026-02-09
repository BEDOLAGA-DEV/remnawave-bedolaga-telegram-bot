import logging
import secrets
import string
from datetime import UTC, datetime, timedelta

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


logger = logging.getLogger(__name__)


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
                        Transaction.amount_kopeks,
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
            selectinload(User.subscription),
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
            selectinload(User.subscription),
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


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    if not username:
        return None

    normalized = username.lower()

    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription),
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
            selectinload(User.subscription),
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
            selectinload(User.subscription),
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

    timestamp = str(int(datetime.utcnow().timestamp()))[-6:]
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
    logger.info(f'✅ Подготовлен пользователь {telegram_id} с реферальным кодом {referral_code} (ожидает коммита)')
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
            logger.info(f'✅ Создан пользователь {telegram_id} с реферальным кодом {referral_code}')

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
                logger.warning('Failed to emit user.created event: %s', error)

            return user

        except IntegrityError as exc:
            await db.rollback()

            if (
                isinstance(getattr(exc, 'orig', None), Exception)
                and 'users_pkey' in str(exc.orig)
                and attempt < attempts
            ):
                logger.warning(
                    '⚠️ Обнаружено несоответствие последовательности users_id_seq при создании пользователя %s. '
                    'Выполняем повторную синхронизацию (попытка %s/%s)',
                    telegram_id,
                    attempt,
                    attempts,
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

    user.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(user)

    return user


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
        old_balance = user.balance_kopeks
        user.balance_kopeks += amount_kopeks
        user.updated_at = datetime.utcnow()

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
            f'💰 Баланс пользователя {user_id_display} изменен: {old_balance} → {user.balance_kopeks} (изменение: +{amount_kopeks})'
        )

        # Автоматическое возобновление приостановленной суточной подписки
        try:
            from app.database.crud.subscription import get_subscription_by_user_id, resume_daily_subscription
            from app.database.crud.tariff import get_tariff_by_id
            from app.database.models import SubscriptionStatus

            # Загружаем подписку явно, чтобы избежать lazy loading
            subscription = await get_subscription_by_user_id(db, user.id)
            if subscription and subscription.status == SubscriptionStatus.DISABLED.value:
                # Проверяем что это суточный тариф
                is_daily = getattr(subscription, 'is_daily_tariff', False)
                if is_daily and subscription.tariff_id:
                    # Загружаем тариф явно
                    tariff = await get_tariff_by_id(db, subscription.tariff_id)
                    if tariff:
                        daily_price = getattr(tariff, 'daily_price_kopeks', 0)
                        # Если баланс достаточный для суточной оплаты - возобновляем
                        if daily_price > 0 and user.balance_kopeks >= daily_price:
                            await resume_daily_subscription(db, subscription)
                            logger.info(
                                f'✅ Автоматически возобновлена суточная подписка {subscription.id} '
                                f'после пополнения баланса (user_id={user.id})'
                            )
                            # Синхронизируем с RemnaWave
                            try:
                                from app.services.subscription_service import SubscriptionService

                                subscription_service = SubscriptionService()
                                await subscription_service.update_remnawave_user(db, subscription)
                            except Exception as sync_err:
                                logger.warning(f'Не удалось синхронизировать с RemnaWave: {sync_err}')
        except Exception as resume_err:
            logger.warning(f'Ошибка при попытке возобновить суточную подписку: {resume_err}')

        return True

    except Exception as e:
        logger.error(f'Ошибка изменения баланса пользователя {user.id}: {e}')
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
            logger.error(f'Пользователь с telegram_id {telegram_id} не найден')
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
        logger.error(f'Ошибка пополнения баланса пользователя {telegram_id}: {e}')
        return False


async def subtract_user_balance(
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    description: str,
    create_transaction: bool = False,
    payment_method: PaymentMethod | None = None,
    *,
    consume_promo_offer: bool = False,
    auto_commit: bool = True,
) -> bool:
    user_id_display = user.telegram_id or user.email or f'#{user.id}'
    logger.info('💸 ОТЛАДКА subtract_user_balance:')
    logger.info(f'   👤 User ID: {user.id} (ID: {user_id_display})')
    logger.info(f'   💰 Баланс до списания: {user.balance_kopeks} копеек')
    logger.info(f'   💸 Сумма к списанию: {amount_kopeks} копеек')
    logger.info(f'   📝 Описание: {description}')

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
                    'Failed to fetch latest claimed promo offer for user %s: %s',
                    user.id,
                    lookup_error,
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

        user.updated_at = datetime.utcnow()

        if auto_commit:
            await db.commit()
            await db.refresh(user)

        if create_transaction:
            from app.database.crud.transaction import (
                create_transaction as create_trans,
            )

            await create_trans(
                db=db,
                user_id=user.id,
                type=TransactionType.WITHDRAWAL,
                amount_kopeks=amount_kopeks,
                description=description,
                payment_method=payment_method,
            )

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
                )
            except Exception as log_error:  # pragma: no cover - defensive logging
                logger.warning(
                    'Failed to record promo offer consumption log for user %s: %s',
                    user.id,
                    log_error,
                )
                try:
                    await db.rollback()
                except Exception as rollback_error:  # pragma: no cover - defensive logging
                    logger.warning(
                        'Failed to rollback session after promo offer consumption log failure: %s',
                        rollback_error,
                    )

        logger.info(f'   ✅ Средства списаны: {old_balance} → {user.balance_kopeks}')
        return True

    except Exception as e:
        logger.error(f'   ❌ ОШИБКА СПИСАНИЯ: {e}')
        await db.rollback()
        return False


async def cleanup_expired_promo_offer_discounts(db: AsyncSession) -> int:
    now = datetime.utcnow()
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
                    'Failed to fetch latest claimed promo offer for user %s during expiration cleanup: %s',
                    user.id,
                    lookup_error,
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
            logger.warning(
                'Failed to log promo offer expiration for user %s: %s',
                user_id,
                log_error,
            )
            try:
                await db.rollback()
            except Exception as rollback_error:  # pragma: no cover - defensive logging
                logger.warning(
                    'Failed to rollback session after promo offer expiration log failure: %s',
                    rollback_error,
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
        selectinload(User.subscription),
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


async def get_referrals(db: AsyncSession, user_id: int) -> list[User]:
    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription),
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
    now = datetime.utcnow()

    base_query = (
        select(User)
        .options(
            selectinload(User.subscription),
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
            logger.warning('Неизвестный сегмент для промо: %s', segment)
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
    threshold_date = datetime.utcnow() - timedelta(days=months * 30)

    result = await db.execute(
        select(User)
        .options(
            selectinload(User.subscription),
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
    user.updated_at = datetime.utcnow()

    await db.commit()
    user_id_display = user.telegram_id or user.email or f'#{user.id}'
    logger.info(f'🗑️ Пользователь {user_id_display} помечен как удаленный')
    return True


async def get_users_statistics(db: AsyncSession) -> dict:
    total_result = await db.execute(select(func.count(User.id)))
    total_users = total_result.scalar()

    active_result = await db.execute(select(func.count(User.id)).where(User.status == UserStatus.ACTIVE.value))
    active_users = active_result.scalar()

    today = datetime.utcnow().date()
    today_result = await db.execute(
        select(func.count(User.id)).where(and_(User.created_at >= today, User.status == UserStatus.ACTIVE.value))
    )
    new_today = today_result.scalar()

    week_ago = datetime.utcnow() - timedelta(days=7)
    week_result = await db.execute(
        select(func.count(User.id)).where(and_(User.created_at >= week_ago, User.status == UserStatus.ACTIVE.value))
    )
    new_week = week_result.scalar()

    month_ago = datetime.utcnow() - timedelta(days=30)
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
    current_time = datetime.utcnow()

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
        .options(selectinload(User.subscription))
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
    logger.info(f'✅ Создан email-пользователь {email} с id={user.id}')

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
        logger.warning('Failed to emit user.created event: %s', error)

    return user


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Get user by email address."""
    result = await db.execute(select(User).where(User.email == email))
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
    query = select(User.id).where(User.email == email)
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
    user.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(user)

    logger.info(f'Email change pending for user {user.id}: {user.email} -> {new_email}')
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

    if user.email_change_expires and datetime.utcnow() > user.email_change_expires:
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
    user.email_verified_at = datetime.utcnow()
    user.email_change_new = None
    user.email_change_code = None
    user.email_change_expires = None
    user.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(user)

    logger.info(f'Email changed for user {user.id}: {old_email} -> {new_email}')
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
    user.updated_at = datetime.utcnow()

    await db.commit()
    logger.info(f'Email change cancelled for user {user.id}')


# --- OAuth provider functions ---

_OAUTH_PROVIDER_COLUMNS = {
    'google': 'google_id',
    'yandex': 'yandex_id',
    'discord': 'discord_id',
    'vk': 'vk_id',
}


async def get_user_by_oauth_provider(db: AsyncSession, provider: str, provider_id: str) -> User | None:
    """Find a user by OAuth provider ID."""
    column_name = _OAUTH_PROVIDER_COLUMNS.get(provider)
    if not column_name:
        return None
    column = getattr(User, column_name)
    # VK uses BigInteger, so convert
    value: str | int = int(provider_id) if provider == 'vk' else provider_id
    result = await db.execute(select(User).where(column == value))
    return result.scalar_one_or_none()


async def set_user_oauth_provider_id(db: AsyncSession, user: User, provider: str, provider_id: str) -> None:
    """Link an OAuth provider ID to an existing user."""
    column_name = _OAUTH_PROVIDER_COLUMNS.get(provider)
    if not column_name:
        return
    value: str | int = int(provider_id) if provider == 'vk' else provider_id
    setattr(user, column_name, value)
    user.updated_at = datetime.now(UTC).replace(tzinfo=None)
    logger.info(f'Linked {provider} (id={provider_id}) to user {user.id}')


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
) -> User:
    """Create a new user via OAuth provider."""
    referral_code = await create_unique_referral_code(db)
    normalized_language = _normalize_language_code(language)
    default_group = await _get_or_create_default_promo_group(db)

    column_name = _OAUTH_PROVIDER_COLUMNS.get(provider)
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
    logger.info(f'Created OAuth user via {provider} (provider_id={provider_id}) with id={user.id}')

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
        logger.warning('Failed to emit user.created event: %s', error)

    return user
