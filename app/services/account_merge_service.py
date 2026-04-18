from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import OAUTH_PROVIDER_COLUMNS, get_user_by_id
from app.database.models import (
    AccessPolicy,
    AdminAuditLog,
    AdminRole,
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    BroadcastHistory,
    ButtonClickLog,
    CabinetRefreshToken,
    CloudPaymentsPayment,
    ContestAttempt,
    CryptoBotPayment,
    DiscountOffer,
    FreekassaPayment,
    GuestPurchase,
    HeleketPayment,
    KassaAiPayment,
    MulenPayPayment,
    NewsArticle,
    Pal24Payment,
    PartnerApplication,
    PartnerStatus,
    PinnedMessage,
    PlategaPayment,
    Poll,
    PollResponse,
    PromoCode,
    PromoCodeUse,
    PromoOfferLog,
    PromoOfferTemplate,
    ReferralContest,
    ReferralContestEvent,
    ReferralEarning,
    RioPayPayment,
    SavedPaymentMethod,
    SentNotification,
    SeverPayPayment,
    Subscription,
    SubscriptionConversion,
    SubscriptionEvent,
    SubscriptionServer,
    SupportAuditLog,
    Ticket,
    TicketMessage,
    TicketNotification,
    Transaction,
    User,
    UserMessage,
    UserPromoGroup,
    UserRole,
    UserStatus,
    WataPayment,
    WelcomeText,
    WheelSpin,
    WithdrawalRequest,
    YooKassaPayment,
)
from app.external.remnawave_api import RemnaWaveAPI


logger = structlog.get_logger(__name__)

# OAuth-поля, которые можно перенести между аккаунтами (источник — OAUTH_PROVIDER_COLUMNS)
_OAUTH_FIELDS: tuple[str, ...] = tuple(OAUTH_PROVIDER_COLUMNS.values())

# Все платёжные таблицы с колонкой user_id
_PAYMENT_MODELS: tuple[type, ...] = (
    CloudPaymentsPayment,
    CryptoBotPayment,
    FreekassaPayment,
    HeleketPayment,
    KassaAiPayment,
    MulenPayPayment,
    Pal24Payment,
    PlategaPayment,
    RioPayPayment,
    SeverPayPayment,
    WataPayment,
    YooKassaPayment,
)

# Приоритет партнёрских статусов (чем выше число — тем приоритетнее)
_PARTNER_STATUS_PRIORITY: dict[str, int] = {
    PartnerStatus.NONE.value: 0,
    PartnerStatus.REJECTED.value: 1,
    PartnerStatus.PENDING.value: 2,
    PartnerStatus.APPROVED.value: 3,
}


def compute_auth_methods(user: User) -> list[str]:
    """Вычисляет список методов авторизации пользователя."""
    methods: list[str] = []
    if user.telegram_id:
        methods.append('telegram')
    if user.email and user.password_hash:
        methods.append('email')
    for provider, column in OAUTH_PROVIDER_COLUMNS.items():
        if getattr(user, column, None):
            methods.append(provider)
    return methods


def _build_subscription_preview(sub: Subscription | None) -> dict[str, Any] | None:
    """Формирует превью данных подписки."""
    if sub is None:
        return None
    tariff_name: str | None = None
    if sub.tariff:
        tariff_name = sub.tariff.name
    return {
        'status': sub.status,
        'is_trial': sub.is_trial,
        'end_date': sub.end_date,
        'traffic_limit_gb': sub.traffic_limit_gb,
        'traffic_used_gb': sub.traffic_used_gb,
        'device_limit': sub.device_limit,
        'tariff_name': tariff_name,
        'autopay_enabled': sub.autopay_enabled,
    }


def _build_user_preview(user: User) -> dict[str, Any]:
    """Формирует превью данных пользователя для предварительного просмотра мержа."""
    subs = getattr(user, 'subscriptions', None) or []
    return {
        'id': user.id,
        'username': user.username,
        'first_name': user.first_name,
        'email': user.email,
        'auth_methods': compute_auth_methods(user),
        'balance_kopeks': user.balance_kopeks,
        'subscription': _build_subscription_preview(subs[0] if subs else None),
        'subscriptions_count': len(subs),
        'created_at': user.created_at,
    }


async def get_merge_preview(
    db: AsyncSession,
    primary_user_id: int,
    secondary_user_id: int,
) -> dict[str, Any]:
    """Возвращает превью данных обоих аккаунтов для подтверждения мержа.

    Args:
        db: Сессия БД.
        primary_user_id: ID основного аккаунта (останется).
        secondary_user_id: ID вторичного аккаунта (будет поглощён).

    Returns:
        Словарь с ключами 'primary' и 'secondary', содержащими превью данных.

    Raises:
        ValueError: Если один из пользователей не найден или совпадают.
    """
    if primary_user_id == secondary_user_id:
        raise ValueError('primary_user_id и secondary_user_id не могут совпадать')

    primary = await get_user_by_id(db, primary_user_id)
    secondary = await get_user_by_id(db, secondary_user_id)

    if not primary:
        raise ValueError(f'Основной пользователь (id={primary_user_id}) не найден')
    if not secondary:
        raise ValueError(f'Вторичный пользователь (id={secondary_user_id}) не найден')

    return {
        'primary': _build_user_preview(primary),
        'secondary': _build_user_preview(secondary),
    }


@asynccontextmanager
async def _get_remnawave_api() -> AsyncIterator[RemnaWaveAPI]:
    """Создаёт экземпляр RemnaWave API клиента (паттерн из RemnaWaveService)."""
    auth_params = settings.get_remnawave_auth_params()
    base_url = (auth_params.get('base_url') or '').strip()
    api_key = (auth_params.get('api_key') or '').strip()

    if not base_url or not api_key:
        raise RuntimeError('RemnaWave API не настроен (REMNAWAVE_API_URL / REMNAWAVE_API_KEY)')

    api = RemnaWaveAPI(
        base_url=base_url,
        api_key=api_key,
        secret_key=auth_params.get('secret_key'),
        username=auth_params.get('username'),
        password=auth_params.get('password'),
        caddy_token=auth_params.get('caddy_token'),
        auth_type=auth_params.get('auth_type') or 'api_key',
    )
    async with api:
        yield api


async def _delete_remnawave_user_with_fallback(remnawave_uuid: str) -> None:
    """Удаляет пользователя из RemnaWave. При неудаче — деактивирует как fallback."""
    try:
        async with _get_remnawave_api() as api:
            deleted = await api.delete_user(remnawave_uuid)
            if deleted:
                logger.info(
                    'RemnaWave пользователь удалён при мерже',
                    remnawave_uuid=remnawave_uuid,
                )
            else:
                logger.warning(
                    'RemnaWave delete_user вернул False, пробуем disable',
                    remnawave_uuid=remnawave_uuid,
                )
                await api.disable_user(remnawave_uuid)
                logger.info(
                    'RemnaWave пользователь деактивирован как fallback при мерже',
                    remnawave_uuid=remnawave_uuid,
                )
    except Exception:
        logger.warning(
            'Не удалось удалить RemnaWave пользователя, пробуем disable',
            remnawave_uuid=remnawave_uuid,
            exc_info=True,
        )
        try:
            async with _get_remnawave_api() as api:
                await api.disable_user(remnawave_uuid)
                logger.info(
                    'RemnaWave пользователь деактивирован как fallback при мерже',
                    remnawave_uuid=remnawave_uuid,
                )
        except Exception:
            logger.error(
                'Не удалось ни удалить, ни деактивировать RemnaWave пользователя',
                remnawave_uuid=remnawave_uuid,
                exc_info=True,
            )


async def _sync_transferred_subscriptions_to_panel(
    primary: User,
    transferred_subs: list[Subscription],
) -> None:
    """Updates RemnaWave panel description for subscriptions transferred to primary user.

    After account merge transfers subscriptions from secondary to primary,
    the panel still shows the old secondary user's telegramId/username in the
    description. This function patches each subscription in RemnaWave so admin
    views reflect the actual owner.

    Failures are logged per-subscription but never propagate — panel desync is
    non-fatal and can be fixed by a manual resync later.
    """
    subs_with_uuid = [s for s in transferred_subs if getattr(s, 'remnawave_uuid', None)]
    if not subs_with_uuid:
        return

    new_description = settings.format_remnawave_user_description(
        full_name=primary.full_name,
        username=primary.username,
        telegram_id=primary.telegram_id,
        email=getattr(primary, 'email', None),
        user_id=primary.id,
    )

    try:
        async with _get_remnawave_api() as api:
            for sub in subs_with_uuid:
                try:
                    await api.update_user(
                        uuid=sub.remnawave_uuid,
                        description=new_description,
                        telegram_id=primary.telegram_id,
                        email=getattr(primary, 'email', None),
                    )
                    logger.info(
                        'Synced transferred subscription description to panel',
                        subscription_id=sub.id,
                        remnawave_uuid=sub.remnawave_uuid,
                        primary_user_id=primary.id,
                    )
                except Exception:
                    logger.warning(
                        'Failed to sync transferred subscription to panel',
                        subscription_id=sub.id,
                        remnawave_uuid=sub.remnawave_uuid,
                        primary_user_id=primary.id,
                        exc_info=True,
                    )
    except Exception:
        logger.warning(
            'Failed to connect to RemnaWave API for post-merge sync',
            primary_user_id=primary.id,
            subscription_count=len(subs_with_uuid),
            exc_info=True,
        )


async def _handle_subscription_merge(
    db: AsyncSession,
    primary: User,
    secondary: User,
    keep_subscription_from: Literal['primary', 'secondary'],
) -> None:
    """Обрабатывает мерж подписок между двумя аккаунтами.

    Args:
        db: Сессия БД.
        primary: Основной пользователь.
        secondary: Вторичный пользователь.
        keep_subscription_from: 'primary' или 'secondary' — чью подписку оставить.
    """
    # Multi-tariff mode: transfer ALL subscriptions from secondary to primary.
    # Duplicate tariff_ids are allowed, so do not collapse or expire conflicts here.
    if settings.is_multi_tariff_enabled():
        secondary_subs = list(getattr(secondary, 'subscriptions', None) or [])
        secondary_legacy_uuid = secondary.remnawave_uuid

        transferred: list[Subscription] = []
        if secondary_subs:
            for sub in secondary_subs:
                sub_tariff_id = getattr(sub, 'tariff_id', None)
                sub_remnawave_uuid = getattr(sub, 'remnawave_uuid', None)

                sub.user_id = primary.id
                transferred.append(sub)
                logger.info(
                    'Transferred subscription during account merge',
                    subscription_id=sub.id,
                    tariff_id=sub_tariff_id,
                    from_user=secondary.id,
                    to_user=primary.id,
                    remnawave_uuid=sub_remnawave_uuid,
                )
                if sub_remnawave_uuid and secondary_legacy_uuid and sub_remnawave_uuid == secondary_legacy_uuid:
                    logger.warning(
                        'Transferred subscription remnawave_uuid matches secondary legacy uuid — manual panel review required',
                        subscription_id=sub.id,
                        remnawave_uuid=sub_remnawave_uuid,
                        secondary_user_id=secondary.id,
                        primary_user_id=primary.id,
                    )
            await db.flush()
            logger.info(
                'Мерж подписок (multi-tariff): перенесено подписок secondary на primary',
                count=len(transferred),
                primary_id=primary.id,
                secondary_id=secondary.id,
            )
            # Sync transferred subscriptions in RemnaWave panel so description
            # reflects the primary user (telegramId, username, email).
            await _sync_transferred_subscriptions_to_panel(primary, transferred)
        # Clean up legacy remnawave_uuid on secondary
        if secondary.remnawave_uuid:
            secondary.remnawave_uuid = None
        return

    # Legacy single-subscription mode
    primary_subs = getattr(primary, 'subscriptions', None) or []
    secondary_subs = getattr(secondary, 'subscriptions', None) or []
    primary_sub = primary_subs[0] if primary_subs else None
    secondary_sub = secondary_subs[0] if secondary_subs else None
    has_primary_sub = primary_sub is not None
    has_secondary_sub = secondary_sub is not None

    # Ни у кого нет подписки — ничего не делаем
    if not has_primary_sub and not has_secondary_sub:
        logger.info(
            'Мерж подписок: ни у кого нет подписки',
            primary_id=primary.id,
            secondary_id=secondary.id,
        )
        return

    # Подписка только у primary — удаляем RemnaWave юзера secondary (если есть)
    if has_primary_sub and not has_secondary_sub:
        if secondary.remnawave_uuid:
            await _delete_remnawave_user_with_fallback(secondary.remnawave_uuid)
            secondary.remnawave_uuid = None
        logger.info(
            'Мерж подписок: оставлена подписка primary, secondary не имел подписки',
            primary_id=primary.id,
            secondary_id=secondary.id,
        )
        return

    # Подписка только у secondary — переносим на primary
    if not has_primary_sub and has_secondary_sub:
        assert secondary_sub is not None
        secondary_sub.user_id = primary.id
        # Переносим remnawave_uuid (clear→flush→assign — unique constraint safety)
        if secondary.remnawave_uuid:
            uuid_to_transfer = secondary.remnawave_uuid
            secondary.remnawave_uuid = None
            await db.flush()
            primary.remnawave_uuid = uuid_to_transfer
        await db.flush()
        logger.info(
            'Мерж подписок: перенесена подписка secondary на primary',
            primary_id=primary.id,
            secondary_id=secondary.id,
        )
        return

    # Обе подписки есть — выбираем по keep_subscription_from
    assert primary_sub is not None
    assert secondary_sub is not None

    if keep_subscription_from == 'secondary':
        # Удаляем подписку primary из RemnaWave
        if primary.remnawave_uuid:
            await _delete_remnawave_user_with_fallback(primary.remnawave_uuid)
            primary.remnawave_uuid = None
        # Явно удаляем subscription_servers перед подпиской (CASCADE настроен, но делаем явно для ясности)
        await db.execute(delete(SubscriptionServer).where(SubscriptionServer.subscription_id == primary_sub.id))
        # Удаляем запись подписки primary
        await db.delete(primary_sub)
        await db.flush()
        # Переносим подписку secondary на primary
        secondary_sub.user_id = primary.id
        # Переносим remnawave_uuid (clear→flush→assign — unique constraint safety)
        if secondary.remnawave_uuid:
            uuid_to_transfer = secondary.remnawave_uuid
            secondary.remnawave_uuid = None
            await db.flush()
            primary.remnawave_uuid = uuid_to_transfer
        # Flush сразу — гарантируем, что DELETE предшествует UPDATE (unique constraint на subscription.user_id)
        await db.flush()
        logger.info(
            'Мерж подписок: оставлена подписка secondary, подписка primary удалена',
            primary_id=primary.id,
            secondary_id=secondary.id,
        )
    else:
        # keep_subscription_from == 'primary' (по умолчанию)
        # Удаляем подписку secondary из RemnaWave
        if secondary.remnawave_uuid:
            await _delete_remnawave_user_with_fallback(secondary.remnawave_uuid)
            secondary.remnawave_uuid = None
        # Явно удаляем subscription_servers перед подпиской (CASCADE настроен, но делаем явно для ясности)
        await db.execute(delete(SubscriptionServer).where(SubscriptionServer.subscription_id == secondary_sub.id))
        # Удаляем запись подписки secondary
        await db.delete(secondary_sub)
        await db.flush()
        logger.info(
            'Мерж подписок: оставлена подписка primary, подписка secondary удалена',
            primary_id=primary.id,
            secondary_id=secondary.id,
        )


async def execute_merge(
    db: AsyncSession,
    primary_user_id: int,
    secondary_user_id: int,
    keep_subscription_from: Literal['primary', 'secondary'] = 'primary',
    provider: str | None = None,
    provider_id: str | None = None,
) -> User:
    """Выполняет атомарный мерж двух аккаунтов. Caller отвечает за commit/rollback.

    Переносит все данные с secondary на primary, помечает secondary как deleted.

    Args:
        db: Сессия БД (caller управляет транзакцией).
        primary_user_id: ID основного аккаунта.
        secondary_user_id: ID вторичного аккаунта.
        keep_subscription_from: 'primary' или 'secondary' — чью подписку оставить.
        provider: OAuth-провайдер, инициировавший мерж (для логирования).
        provider_id: ID провайдера (для логирования).

    Returns:
        Обновлённый объект primary User.

    Raises:
        ValueError: Если пользователь не найден, совпадают ID, или secondary уже удалён.
    """
    if keep_subscription_from not in ('primary', 'secondary'):
        raise ValueError("keep_subscription_from должен быть 'primary' или 'secondary'")

    if primary_user_id == secondary_user_id:
        raise ValueError('primary_user_id и secondary_user_id не могут совпадать')

    primary = await get_user_by_id(db, primary_user_id)
    secondary = await get_user_by_id(db, secondary_user_id)

    if not primary:
        raise ValueError(f'Основной пользователь (id={primary_user_id}) не найден')
    if primary.status == UserStatus.DELETED.value:
        raise ValueError(f'Основной пользователь (id={primary_user_id}) удалён')
    if not secondary:
        raise ValueError(f'Вторичный пользователь (id={secondary_user_id}) не найден')
    if secondary.status == UserStatus.DELETED.value:
        raise ValueError(f'Вторичный пользователь (id={secondary_user_id}) уже удалён')

    logger.info(
        'Начинаем мерж аккаунтов',
        primary_id=primary.id,
        secondary_id=secondary.id,
        keep_subscription_from=keep_subscription_from,
        provider=provider,
        provider_id=provider_id,
    )

    # 1. Перенос OAuth ID
    # Два прохода: сначала очищаем secondary (flush для освобождения unique constraint),
    # затем устанавливаем на primary. Без этого SQLAlchemy может отправить UPDATE primary
    # раньше UPDATE secondary, что вызовет UniqueViolation.
    oauth_transfers: list[tuple[str, object]] = []
    for field in _OAUTH_FIELDS:
        secondary_value = getattr(secondary, field)
        primary_value = getattr(primary, field)
        if secondary_value and not primary_value:
            oauth_transfers.append((field, secondary_value))
            setattr(secondary, field, None)

    if oauth_transfers:
        await db.flush()  # Освобождаем unique constraints перед переносом
        for field, value in oauth_transfers:
            setattr(primary, field, value)
            logger.info(
                'Перенесён OAuth ID',
                field=field,
                primary_id=primary.id,
                secondary_id=secondary.id,
            )

    # 2. Перенос telegram_id (unique constraint — тот же паттерн: очистка → flush → установка)
    if secondary.telegram_id and not primary.telegram_id:
        transferred_tg_id = secondary.telegram_id
        secondary.telegram_id = None
        await db.flush()
        primary.telegram_id = transferred_tg_id
        logger.info(
            'Перенесён telegram_id',
            primary_id=primary.id,
            secondary_id=secondary.id,
        )

    # 3. Перенос email + password (unique constraint на email — тот же паттерн)
    if not primary.email and secondary.email:
        transferred_email = secondary.email
        transferred_verified = secondary.email_verified
        transferred_verified_at = secondary.email_verified_at
        transferred_password_hash = secondary.password_hash
        # Очищаем на secondary и flush перед установкой на primary
        secondary.email = None
        secondary.email_verified = False
        secondary.email_verified_at = None
        secondary.password_hash = None
        await db.flush()
        primary.email = transferred_email
        primary.email_verified = transferred_verified
        primary.email_verified_at = transferred_verified_at
        primary.password_hash = transferred_password_hash
        logger.info(
            'Перенесены email и пароль',
            primary_id=primary.id,
            secondary_id=secondary.id,
        )

    # 4. Суммируем баланс (включая отрицательный — долг не должен исчезать)
    transferred_kopeks = secondary.balance_kopeks
    if transferred_kopeks != 0:
        from app.database.models import User as UserModel

        if isinstance(primary, UserModel):
            from app.database.crud.user import lock_user_for_update

            primary = await lock_user_for_update(db, primary)
            secondary = await lock_user_for_update(db, secondary)
            # Re-read after lock in case concurrent payment changed it
            transferred_kopeks = secondary.balance_kopeks
        primary.balance_kopeks += transferred_kopeks
        secondary.balance_kopeks = 0
        logger.info(
            'Перенесён баланс',
            primary_id=primary.id,
            secondary_id=secondary.id,
            transferred_kopeks=transferred_kopeks,
        )

    # 4a. Объединение булевых флагов (True побеждает — пользователь имел опыт)
    if secondary.has_had_paid_subscription and not primary.has_had_paid_subscription:
        primary.has_had_paid_subscription = True
    if secondary.has_made_first_topup and not primary.has_made_first_topup:
        primary.has_made_first_topup = True

    # 4b. Объединение ограничений (берём наиболее строгое)
    if secondary.restriction_topup and not primary.restriction_topup:
        primary.restriction_topup = True
    if secondary.restriction_subscription and not primary.restriction_subscription:
        primary.restriction_subscription = True
    if secondary.restriction_reason and not primary.restriction_reason:
        primary.restriction_reason = secondary.restriction_reason

    # 4c. Суммируем использованные промокоды
    if secondary.used_promocodes:
        primary.used_promocodes = (primary.used_promocodes or 0) + secondary.used_promocodes

    # 5. Мерж подписок
    await _handle_subscription_merge(db, primary, secondary, keep_subscription_from)

    # 6. Переназначение транзакций
    await db.execute(update(Transaction).where(Transaction.user_id == secondary.id).values(user_id=primary.id))

    # 7. Переназначение всех платёжных таблиц
    for payment_model in _PAYMENT_MODELS:
        await db.execute(update(payment_model).where(payment_model.user_id == secondary.id).values(user_id=primary.id))

    # 7b. Переназначение saved_payment_methods (FK без ondelete)
    await db.execute(
        update(SavedPaymentMethod).where(SavedPaymentMethod.user_id == secondary.id).values(user_id=primary.id)
    )

    # 8. Переназначение referral_earnings
    # 8a. Удаляем cross-referral записи между участниками мержа (иначе станут self-referral)
    await db.execute(
        delete(ReferralEarning).where(
            or_(
                and_(ReferralEarning.user_id == secondary.id, ReferralEarning.referral_id == primary.id),
                and_(ReferralEarning.user_id == primary.id, ReferralEarning.referral_id == secondary.id),
            )
        )
    )
    # 8b. Переназначение оставшихся записей
    await db.execute(update(ReferralEarning).where(ReferralEarning.user_id == secondary.id).values(user_id=primary.id))
    await db.execute(
        update(ReferralEarning).where(ReferralEarning.referral_id == secondary.id).values(referral_id=primary.id)
    )

    # 9. Переназначение реферальной цепочки (исключая self-referral)
    await db.execute(
        update(User).where(User.referred_by_id == secondary.id, User.id != primary.id).values(referred_by_id=primary.id)
    )
    # Если primary был приглашён secondary — очищаем (нельзя ссылаться на самого себя)
    if primary.referred_by_id == secondary.id:
        primary.referred_by_id = None

    # Переносим реферальную связь secondary → primary (если primary не имеет своей)
    if primary.referred_by_id is None and secondary.referred_by_id is not None:
        if secondary.referred_by_id != primary.id:
            primary.referred_by_id = secondary.referred_by_id

    # 10. Переназначение withdrawal_requests
    await db.execute(
        update(WithdrawalRequest).where(WithdrawalRequest.user_id == secondary.id).values(user_id=primary.id)
    )
    # processed_by — админский FK, обнуляем (не переносим на primary, чтобы не искажать аудит)
    await db.execute(
        update(WithdrawalRequest).where(WithdrawalRequest.processed_by == secondary.id).values(processed_by=None)
    )

    # 10a. Переназначение subscription_conversions, subscription_events, discount_offers
    await db.execute(
        update(SubscriptionConversion).where(SubscriptionConversion.user_id == secondary.id).values(user_id=primary.id)
    )
    await db.execute(
        update(SubscriptionEvent).where(SubscriptionEvent.user_id == secondary.id).values(user_id=primary.id)
    )
    await db.execute(update(DiscountOffer).where(DiscountOffer.user_id == secondary.id).values(user_id=primary.id))

    # 10b. Переназначение user_promo_groups (composite PK: user_id + promo_group_id)
    # Сначала удаляем дубликаты членства в группах, затем переназначаем оставшиеся
    primary_group_ids = select(UserPromoGroup.promo_group_id).where(UserPromoGroup.user_id == primary.id)
    await db.execute(
        delete(UserPromoGroup).where(
            UserPromoGroup.user_id == secondary.id,
            UserPromoGroup.promo_group_id.in_(primary_group_ids),
        )
    )
    await db.execute(update(UserPromoGroup).where(UserPromoGroup.user_id == secondary.id).values(user_id=primary.id))

    # 10c. Переназначение poll_responses (unique: poll_id + user_id)
    # Сначала удаляем дубликаты ответов на опросы, затем переназначаем оставшиеся
    primary_poll_ids = select(PollResponse.poll_id).where(PollResponse.user_id == primary.id)
    await db.execute(
        delete(PollResponse).where(
            PollResponse.user_id == secondary.id,
            PollResponse.poll_id.in_(primary_poll_ids),
        )
    )
    await db.execute(update(PollResponse).where(PollResponse.user_id == secondary.id).values(user_id=primary.id))

    # 10d. Переназначение promo_offer_logs (без unique constraint — простое переназначение)
    await db.execute(update(PromoOfferLog).where(PromoOfferLog.user_id == secondary.id).values(user_id=primary.id))

    # 10e. Переназначение advertising_campaign_registrations (unique: campaign_id + user_id)
    primary_campaign_ids = select(AdvertisingCampaignRegistration.campaign_id).where(
        AdvertisingCampaignRegistration.user_id == primary.id
    )
    await db.execute(
        delete(AdvertisingCampaignRegistration).where(
            AdvertisingCampaignRegistration.user_id == secondary.id,
            AdvertisingCampaignRegistration.campaign_id.in_(primary_campaign_ids),
        )
    )
    await db.execute(
        update(AdvertisingCampaignRegistration)
        .where(AdvertisingCampaignRegistration.user_id == secondary.id)
        .values(user_id=primary.id)
    )

    # 10f. Переназначение contest_attempts (unique: round_id + user_id)
    primary_round_ids = select(ContestAttempt.round_id).where(ContestAttempt.user_id == primary.id)
    await db.execute(
        delete(ContestAttempt).where(
            ContestAttempt.user_id == secondary.id,
            ContestAttempt.round_id.in_(primary_round_ids),
        )
    )
    await db.execute(update(ContestAttempt).where(ContestAttempt.user_id == secondary.id).values(user_id=primary.id))

    # 10g. Удаляем роли secondary (НЕ переносим — предотвращает эскалацию привилегий через мерж)
    await db.execute(delete(UserRole).where(UserRole.user_id == secondary.id))
    # assigned_by — админский FK, обнуляем (не переносим на primary, чтобы не искажать аудит)
    await db.execute(update(UserRole).where(UserRole.assigned_by == secondary.id).values(assigned_by=None))

    # 10h. Переназначение referral_contest_events (unique: contest_id + referral_id)
    # Удаляем cross-referral события между участниками мержа
    await db.execute(
        delete(ReferralContestEvent).where(
            or_(
                and_(ReferralContestEvent.referrer_id == secondary.id, ReferralContestEvent.referral_id == primary.id),
                and_(ReferralContestEvent.referrer_id == primary.id, ReferralContestEvent.referral_id == secondary.id),
            )
        )
    )
    # Дедупликация по (contest_id, referral_id) перед переназначением referral_id
    primary_referral_contest_ids = select(ReferralContestEvent.contest_id).where(
        ReferralContestEvent.referral_id == primary.id
    )
    await db.execute(
        delete(ReferralContestEvent).where(
            ReferralContestEvent.referral_id == secondary.id,
            ReferralContestEvent.contest_id.in_(primary_referral_contest_ids),
        )
    )
    await db.execute(
        update(ReferralContestEvent)
        .where(ReferralContestEvent.referral_id == secondary.id)
        .values(referral_id=primary.id)
    )
    await db.execute(
        update(ReferralContestEvent)
        .where(ReferralContestEvent.referrer_id == secondary.id)
        .values(referrer_id=primary.id)
    )

    # 10i. Переназначение promocode_uses (unique constraint: user_id + promocode_id)
    primary_promo_ids = select(PromoCodeUse.promocode_id).where(PromoCodeUse.user_id == primary.id)
    await db.execute(
        delete(PromoCodeUse).where(
            PromoCodeUse.user_id == secondary.id,
            PromoCodeUse.promocode_id.in_(primary_promo_ids),
        )
    )
    await db.execute(update(PromoCodeUse).where(PromoCodeUse.user_id == secondary.id).values(user_id=primary.id))

    # 10j. Переназначение partner_applications
    await db.execute(
        update(PartnerApplication).where(PartnerApplication.user_id == secondary.id).values(user_id=primary.id)
    )
    # processed_by — админский FK, обнуляем
    await db.execute(
        update(PartnerApplication).where(PartnerApplication.processed_by == secondary.id).values(processed_by=None)
    )

    # 10k. Переназначение tickets, ticket_messages, ticket_notifications
    await db.execute(update(Ticket).where(Ticket.user_id == secondary.id).values(user_id=primary.id))
    await db.execute(update(TicketMessage).where(TicketMessage.user_id == secondary.id).values(user_id=primary.id))
    await db.execute(
        update(TicketNotification).where(TicketNotification.user_id == secondary.id).values(user_id=primary.id)
    )

    # 10l. Переназначение wheel_spins
    await db.execute(update(WheelSpin).where(WheelSpin.user_id == secondary.id).values(user_id=primary.id))

    # 10m. Обновление FK ссылок в advertising_campaigns
    # partner_user_id — владение (переназначаем)
    await db.execute(
        update(AdvertisingCampaign)
        .where(AdvertisingCampaign.partner_user_id == secondary.id)
        .values(partner_user_id=primary.id)
    )
    # created_by — админский FK, обнуляем
    await db.execute(
        update(AdvertisingCampaign).where(AdvertisingCampaign.created_by == secondary.id).values(created_by=None)
    )

    # 10n. Переназначение sent_notifications
    await db.execute(
        update(SentNotification).where(SentNotification.user_id == secondary.id).values(user_id=primary.id)
    )

    # 10o. Переназначение button_click_logs
    await db.execute(update(ButtonClickLog).where(ButtonClickLog.user_id == secondary.id).values(user_id=primary.id))

    # 10p. Переназначение support_audit_logs
    # actor_user_id — кто действовал (админский FK), обнуляем
    await db.execute(
        update(SupportAuditLog).where(SupportAuditLog.actor_user_id == secondary.id).values(actor_user_id=None)
    )
    # target_user_id — над кем действовали (пользовательский FK), переназначаем
    await db.execute(
        update(SupportAuditLog).where(SupportAuditLog.target_user_id == secondary.id).values(target_user_id=primary.id)
    )

    # 10q. Переназначение admin_audit_log
    await db.execute(update(AdminAuditLog).where(AdminAuditLog.user_id == secondary.id).values(user_id=primary.id))

    # 10r. Обнуление created_by / admin_id FK ссылок в админских таблицах
    # (не переносим на primary — сохраняем целостность аудита; AdminAuditLog.user_id не nullable, переназначаем)
    await db.execute(update(PromoCode).where(PromoCode.created_by == secondary.id).values(created_by=None))
    await db.execute(update(ReferralContest).where(ReferralContest.created_by == secondary.id).values(created_by=None))
    await db.execute(
        update(PromoOfferTemplate).where(PromoOfferTemplate.created_by == secondary.id).values(created_by=None)
    )
    await db.execute(update(BroadcastHistory).where(BroadcastHistory.admin_id == secondary.id).values(admin_id=None))
    await db.execute(update(Poll).where(Poll.created_by == secondary.id).values(created_by=None))
    await db.execute(update(UserMessage).where(UserMessage.created_by == secondary.id).values(created_by=None))
    await db.execute(update(WelcomeText).where(WelcomeText.created_by == secondary.id).values(created_by=None))
    await db.execute(update(PinnedMessage).where(PinnedMessage.created_by == secondary.id).values(created_by=None))
    await db.execute(update(AdminRole).where(AdminRole.created_by == secondary.id).values(created_by=None))
    await db.execute(update(AccessPolicy).where(AccessPolicy.created_by == secondary.id).values(created_by=None))
    await db.execute(update(NewsArticle).where(NewsArticle.created_by == secondary.id).values(created_by=None))

    # 10s. Переназначение guest_purchases (оба FK — buyer_user_id и user_id)
    await db.execute(
        update(GuestPurchase).where(GuestPurchase.buyer_user_id == secondary.id).values(buyer_user_id=primary.id)
    )
    await db.execute(update(GuestPurchase).where(GuestPurchase.user_id == secondary.id).values(user_id=primary.id))

    # 11. Инвалидация refresh-токенов обоих пользователей (после мержа будет создан новый)
    now = datetime.now(UTC)
    await db.execute(
        update(CabinetRefreshToken)
        .where(
            CabinetRefreshToken.user_id.in_([primary.id, secondary.id]),
            CabinetRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )

    # 12. Перенос partner_status (оставляем более приоритетный)
    primary_priority = _PARTNER_STATUS_PRIORITY.get(primary.partner_status, 0)
    secondary_priority = _PARTNER_STATUS_PRIORITY.get(secondary.partner_status, 0)
    if secondary_priority > primary_priority:
        primary.partner_status = secondary.partner_status
        logger.info(
            'Перенесён partner_status',
            primary_id=primary.id,
            secondary_id=secondary.id,
            new_status=primary.partner_status,
        )

    # 13. Перенос referral_commission_percent
    if secondary.referral_commission_percent is not None and primary.referral_commission_percent is None:
        primary.referral_commission_percent = secondary.referral_commission_percent
        logger.info(
            'Перенесён referral_commission_percent',
            primary_id=primary.id,
            secondary_id=secondary.id,
            value=primary.referral_commission_percent,
        )

    # 14. Помечаем secondary как удалённый и очищаем ВСЕ unique constraint и FK поля
    # NOTE: In multi-tariff mode, all secondary subscriptions were already transferred to primary
    # in _handle_subscription_merge. Do NOT clear their remnawave_uuid — they are now primary's subs.
    secondary.status = UserStatus.DELETED.value
    secondary.referral_code = None
    secondary.remnawave_uuid = None
    secondary.referred_by_id = None
    secondary.email = None
    secondary.email_verified = False
    secondary.email_verified_at = None
    secondary.email_verification_token = None
    secondary.email_verification_expires = None
    secondary.email_change_new = None
    secondary.email_change_code = None
    secondary.email_change_expires = None
    secondary.password_hash = None
    secondary.password_reset_token = None
    secondary.password_reset_expires = None
    secondary.telegram_id = None
    for field in _OAUTH_FIELDS:
        if getattr(secondary, field) is not None:
            setattr(secondary, field, None)
    secondary.updated_at = now

    logger.info(
        'Мерж аккаунтов завершён',
        primary_id=primary.id,
        secondary_id=secondary.id,
        provider=provider,
    )

    # 15. flush (не commit — caller управляет транзакцией)
    await db.flush()

    return primary
