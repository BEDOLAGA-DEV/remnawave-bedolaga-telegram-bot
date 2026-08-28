"""Referral program routes for cabinet."""

import math

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.referral import not_referee_directed
from app.database.crud.referral_reward_level import normalize_reward_preference
from app.database.models import (
    AdvertisingCampaign,
    ReferralEarning,
    Subscription,
    SubscriptionStatus,
    User,
    WithdrawalRequest,
    WithdrawalRequestStatus,
)

from ..dependencies import get_cabinet_db, get_current_cabinet_user, get_optional_cabinet_user
from ..schemas.referral import (
    ReferralDaysTargetOption,
    ReferralEarningResponse,
    ReferralEarningsListResponse,
    ReferralInfoResponse,
    ReferralItemResponse,
    ReferralListResponse,
    ReferralProgramLevel,
    ReferralRewardChoiceRequest,
    ReferralTermsResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/referral', tags=['Cabinet Referral'])


@router.get('', response_model=ReferralInfoResponse)
async def get_referral_info(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get referral program info for current user."""
    # Get total referrals count
    total_query = select(func.count()).select_from(User).where(User.referred_by_id == user.id)
    total_result = await db.execute(total_query)
    total_referrals = total_result.scalar() or 0

    # Get active referrals (with active subscription right now)
    active_query = (
        select(func.count(func.distinct(User.id)))
        .join(Subscription, User.id == Subscription.user_id)
        .where(
            User.referred_by_id == user.id,
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.end_date > func.now(),
        )
    )
    active_result = await db.execute(active_query)
    active_referrals = active_result.scalar() or 0

    # Get total earnings.
    #
    # Дни считаются отдельной суммой и НЕ входят в расчёт доступного к выводу
    # баланса ниже: дни нельзя вывести деньгами, и подмешать их в entitlement
    # означало бы разрешить вывод сумм, которых пользователь не зарабатывал.
    earnings_query = select(
        func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
        func.coalesce(func.sum(ReferralEarning.days_granted), 0),
    ).where(ReferralEarning.user_id == user.id, not_referee_directed())
    earnings_result = await db.execute(earnings_query)
    total_earnings, total_earning_days = earnings_result.one()

    # Get user's commission percent
    commission_percent = user.referral_commission_percent
    if commission_percent is None:
        commission_percent = settings.REFERRAL_COMMISSION_PERCENT

    # Get withdrawn amount (approved + completed withdrawal requests)
    withdrawn_query = select(func.coalesce(func.sum(WithdrawalRequest.amount_kopeks), 0)).where(
        WithdrawalRequest.user_id == user.id,
        WithdrawalRequest.status.in_([WithdrawalRequestStatus.APPROVED.value, WithdrawalRequestStatus.COMPLETED.value]),
    )
    withdrawn_result = await db.execute(withdrawn_query)
    withdrawn = withdrawn_result.scalar() or 0

    # Get pending withdrawal amount
    pending_query = select(func.coalesce(func.sum(WithdrawalRequest.amount_kopeks), 0)).where(
        WithdrawalRequest.user_id == user.id,
        WithdrawalRequest.status == WithdrawalRequestStatus.PENDING.value,
    )
    pending_result = await db.execute(pending_query)
    pending = pending_result.scalar() or 0

    # Доступный баланс: мин(кошелёк, заработано - выведено - в ожидании)
    referral_entitlement = max(0, total_earnings - withdrawn - pending)
    available_balance = min(user.balance_kopeks, referral_entitlement)

    # Build referral links
    referral_link = (settings.get_cabinet_referral_link(user.referral_code) or '') if user.referral_code else ''
    bot_referral_link = settings.get_bot_referral_link(user.referral_code) if user.referral_code else ''

    return ReferralInfoResponse(
        referral_code=user.referral_code or '',
        referral_link=referral_link,
        bot_referral_link=bot_referral_link,
        total_referrals=total_referrals,
        active_referrals=active_referrals,
        total_earnings_kopeks=total_earnings,
        total_earnings_rubles=total_earnings / 100,
        total_earnings_days=int(total_earning_days or 0),
        commission_percent=commission_percent,
        available_balance_kopeks=available_balance,
        available_balance_rubles=available_balance / 100,
        withdrawn_kopeks=withdrawn,
    )


@router.get('/list', response_model=ReferralListResponse)
async def get_referral_list(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of invited users."""
    # Base query with eager loading of subscription relationship
    query = (
        select(User)
        .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
        .where(User.referred_by_id == user.id)
    )

    # Get total count
    count_query = select(func.count()).select_from(User).where(User.referred_by_id == user.id)
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Paginate
    offset = (page - 1) * per_page
    query = query.order_by(desc(User.created_at)).offset(offset).limit(per_page)

    result = await db.execute(query)
    referrals = result.scalars().all()

    items = [
        ReferralItemResponse(
            id=r.id,
            username=r.username,
            first_name=r.first_name,
            created_at=r.created_at,
            has_subscription=bool(getattr(r, 'subscriptions', None)),
            has_paid=r.has_had_paid_subscription,
        )
        for r in referrals
    ]

    pages = math.ceil(total / per_page) if total > 0 else 1

    return ReferralListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get('/earnings', response_model=ReferralEarningsListResponse)
async def get_referral_earnings(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get referral earnings history."""
    # Base query
    query = select(ReferralEarning).where(ReferralEarning.user_id == user.id, not_referee_directed())

    # Get total count and sum
    count_query = (
        select(func.count())
        .select_from(ReferralEarning)
        .where(ReferralEarning.user_id == user.id, not_referee_directed())
    )
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Дни суммируются рядом с деньгами: награда днями пишется с amount_kopeks == 0,
    # и без своего итога история показывает «всего 0 ₽» при непустом списке.
    sum_query = select(
        func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
        func.coalesce(func.sum(ReferralEarning.days_granted), 0),
    ).where(ReferralEarning.user_id == user.id, not_referee_directed())
    sum_result = await db.execute(sum_query)
    total_amount, total_days = sum_result.one()

    # Paginate
    offset = (page - 1) * per_page
    query = query.order_by(desc(ReferralEarning.created_at)).offset(offset).limit(per_page)

    result = await db.execute(query)
    earnings = result.scalars().all()

    # Batch-fetch referral users to avoid N+1
    referral_ids = list({e.referral_id for e in earnings if e.referral_id})
    if referral_ids:
        referral_users_result = await db.execute(select(User).where(User.id.in_(referral_ids)))
        referral_users_map = {u.id: u for u in referral_users_result.scalars().all()}
    else:
        referral_users_map = {}

    # Тариф дневной награды хранится только идентификатором: без этого запроса
    # история не может сказать, в какую подписку легли дни.
    tariff_ids = list({e.tariff_id for e in earnings if e.tariff_id})
    if tariff_ids:
        from app.database.models import Tariff

        tariffs_result = await db.execute(select(Tariff.id, Tariff.name).where(Tariff.id.in_(tariff_ids)))
        tariff_names_map = {row.id: row.name for row in tariffs_result.all()}
    else:
        tariff_names_map = {}

    # Batch-fetch campaigns to avoid N+1
    campaign_ids = list({e.campaign_id for e in earnings if e.campaign_id})
    if campaign_ids:
        campaigns_result = await db.execute(select(AdvertisingCampaign).where(AdvertisingCampaign.id.in_(campaign_ids)))
        campaigns_map = {c.id: c for c in campaigns_result.scalars().all()}
    else:
        campaigns_map = {}

    items = []
    for e in earnings:
        referral_user = referral_users_map.get(e.referral_id) if e.referral_id else None
        campaign = campaigns_map.get(e.campaign_id) if e.campaign_id else None

        items.append(
            ReferralEarningResponse(
                id=e.id,
                amount_kopeks=e.amount_kopeks,
                amount_rubles=e.amount_kopeks / 100,
                reason=e.reason or 'Referral commission',
                reward_type=getattr(e, 'reward_type', 'money') or 'money',
                level=int(getattr(e, 'level', 1) or 1),
                days_granted=int(getattr(e, 'days_granted', 0) or 0),
                tariff_id=e.tariff_id,
                tariff_name=tariff_names_map.get(e.tariff_id) if e.tariff_id else None,
                referral_username=referral_user.username if referral_user else None,
                referral_first_name=referral_user.first_name if referral_user else None,
                campaign_name=campaign.name if campaign else None,
                created_at=e.created_at,
            )
        )

    pages = math.ceil(total / per_page) if total > 0 else 1

    return ReferralEarningsListResponse(
        items=items,
        total=total,
        total_amount_kopeks=total_amount,
        total_amount_rubles=total_amount / 100,
        total_days_granted=int(total_days or 0),
        page=page,
        per_page=per_page,
        pages=pages,
    )


async def _days_target_options(db: AsyncSession, user) -> list[ReferralDaysTargetOption]:
    """Подписки, между которыми есть смысл выбирать.

    Триальные не предлагаются: положить в них награду всё равно нельзя, а пункт
    в списке обещал бы обратное.
    """
    from app.database.crud.subscription import get_active_subscriptions_by_user_id
    from app.database.models import Tariff

    subscriptions = [sub for sub in await get_active_subscriptions_by_user_id(db, user.id) if not sub.is_trial]
    if not subscriptions:
        return []

    tariff_ids = {sub.tariff_id for sub in subscriptions if sub.tariff_id}
    names: dict[int, str] = {}
    if tariff_ids:
        rows = await db.execute(select(Tariff.id, Tariff.name).where(Tariff.id.in_(tariff_ids)))
        names = {row.id: row.name for row in rows.all()}

    return [
        ReferralDaysTargetOption(
            id=sub.id,
            tariff_name=names.get(sub.tariff_id),
            end_date=sub.end_date.isoformat() if sub.end_date else None,
        )
        for sub in subscriptions
    ]


@router.patch('/reward-choice', response_model=ReferralTermsResponse)
async def update_reward_choice(
    request: ReferralRewardChoiceRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    user=Depends(get_current_cabinet_user),
):
    """Сохранить, что получать и куда класть дни.

    Каждое поле пишется, только если админ его разрешил и только если экран
    прислал признак «поле трогали»: сам None здесь значимое значение — «как
    настроено правилом» и «подбирать автоматически», — и от «не присылали» он
    иначе неотличим.
    """
    if request.set_reward_preference:
        if not settings.is_referral_reward_kind_choice_enabled():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Reward kind choice is disabled')
        user.referral_reward_preference = normalize_reward_preference(request.reward_preference)

    if request.set_days_target:
        if not settings.is_referral_days_target_choice_enabled():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Days target choice is disabled')

        chosen = request.days_target_subscription_id
        if chosen is not None:
            # Принадлежность проверяется здесь, а не только при начислении:
            # чужому идентификатору не место в базе.
            allowed = {option.id for option in await _days_target_options(db, user)}
            if chosen not in allowed:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Subscription is not yours')
        user.referral_days_subscription_id = chosen

    await db.commit()
    logger.info(
        'Пользователь изменил настройки реферальных наград',
        user_id=user.id,
        preference=user.referral_reward_preference,
        subscription_id=user.referral_days_subscription_id,
    )
    return await get_referral_terms(db=db, user=user)


@router.get('/terms', response_model=ReferralTermsResponse)
async def get_referral_terms(
    db: AsyncSession = Depends(get_cabinet_db),
    user: User | None = Depends(get_optional_cabinet_user),
):
    """Get referral program terms.

    В многоуровневой схеме описание берётся из таблицы уровней — из того же
    источника, из которого считает движок наград. Иначе кабинет публикует одни
    условия, бот платит по другим, и расходятся они молча.

    Пользователь ОПЦИОНАЛЕН: эндпоинт условий публичный, и требовать здесь токен
    значило бы закрыть его для неавторизованных. Язык берётся, когда пользователь
    известен, иначе описание отдаётся на языке по умолчанию.
    """
    level_descriptions: list[str] = []
    referee_bonus: str | None = None
    tier_progress = None
    program_levels: list[ReferralProgramLevel] = []
    personal_percent: int | None = None
    days_target_options: list[ReferralDaysTargetOption] = []
    choice_money: str | None = None
    choice_days: str | None = None

    if settings.is_referral_levels_scheme():
        from app.database.models import Tariff
        from app.services.referral_reward_service import (
            ReferralRewardLevelService,
            build_level_views,
            describe_active_levels,
            describe_referee_bonus,
            describe_reward_choice_sides,
            resolve_tier_progress,
        )

        configs = await ReferralRewardLevelService.get_all(db)
        tariff_ids = {cfg.referrer_tariff_id for cfg in configs.values() if cfg.referrer_tariff_id}
        tariff_ids |= {cfg.referee_tariff_id for cfg in configs.values() if cfg.referee_tariff_id}
        tariff_names: dict[int, str] = {}
        if tariff_ids:
            tariffs_result = await db.execute(select(Tariff.id, Tariff.name).where(Tariff.id.in_(tariff_ids)))
            tariff_names = {row.id: row.name for row in tariffs_result.all()}

        language = user.language if user else None
        level_descriptions = await describe_active_levels(db, tariff_names=tariff_names, language=language, viewer=user)
        # Пользователь известен — он и есть потенциальный пригласивший, и в
        # режиме рангов бонус его приглашённых задаётся его рангом. Аноним
        # получает описание стартового ранга.
        referee_bonus = await describe_referee_bonus(db, tariff_names=tariff_names, language=language, referrer=user)
        # Разобранные ступени для карточек кабинета. Строятся тем же кодом, что и
        # строки выше, поэтому разойтись с ними не могут.
        views, _current, personal_percent = await build_level_views(
            db, tariff_names=tariff_names, language=language, viewer=user
        )
        program_levels = [
            ReferralProgramLevel(
                level=view.level,
                is_current=view.is_current,
                rewards=view.rewards,
                pays_referrer=view.pays_referrer,
                trigger=view.trigger,
                trigger_label=view.trigger_label,
                required_referrals=view.required_referrals,
                required_referrals_active_only=view.required_referrals_active_only,
                referee_reward=view.referee_reward,
            )
            for view in views
        ]

        # Уровень считается только для известного пользователя: эндпоинт
        # публичный, и «ваш уровень» без пользователя было бы чужим.
        if user is not None:
            tier_progress = await resolve_tier_progress(db, user)

        # Подписки для выбора запрашиваются только когда выбор разрешён: иначе
        # это лишние запросы на каждом открытии публичной страницы условий.
        if user is not None and settings.is_referral_days_target_choice_enabled():
            days_target_options = await _days_target_options(db, user)

        if user is not None:
            choice_money, choice_days = await describe_reward_choice_sides(
                db, user, tariff_names=tariff_names, language=language
            )

    return ReferralTermsResponse(
        scheme='levels' if settings.is_referral_levels_scheme() else 'legacy',
        level_descriptions=level_descriptions,
        referee_bonus_description=referee_bonus,
        # В режиме рангов цепочки нет: получатель ровно один, прямой пригласивший.
        # Отдать сюда настроенную глубину значило бы пообещать клиенту выплаты
        # тем, кто выше, — их в этом режиме не бывает.
        max_level_depth=(
            settings.get_referral_max_level_depth()
            if settings.is_referral_levels_scheme() and not settings.is_referral_tier_levels()
            else 1
        ),
        levels_mode=settings.get_referral_levels_mode() if settings.is_referral_levels_scheme() else 'chain',
        tier_current_level=tier_progress.current_level if tier_progress else None,
        tier_next_level=tier_progress.next_level if tier_progress else None,
        tier_next_remaining=tier_progress.next_remaining if tier_progress else 0,
        tier_referrals_any=tier_progress.referrals_any if tier_progress else 0,
        tier_referrals_active=tier_progress.referrals_active if tier_progress else 0,
        levels=program_levels,
        personal_percent=personal_percent,
        allow_reward_kind_choice=settings.is_referral_reward_kind_choice_enabled(),
        allow_days_target_choice=settings.is_referral_days_target_choice_enabled(),
        reward_preference=(normalize_reward_preference(user.referral_reward_preference) if user else None),
        days_target_subscription_id=(user.referral_days_subscription_id if user else None),
        days_target_options=days_target_options,
        reward_choice_money=choice_money,
        reward_choice_days=choice_days,
        is_enabled=settings.is_referral_program_enabled(),
        commission_percent=settings.REFERRAL_COMMISSION_PERCENT,
        first_payment_commission_percent=settings.REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT,
        recurring_commission_tiers=settings.REFERRAL_RECURRING_COMMISSION_TIERS,
        minimum_topup_kopeks=settings.REFERRAL_MINIMUM_TOPUP_KOPEKS,
        minimum_topup_rubles=settings.REFERRAL_MINIMUM_TOPUP_KOPEKS / 100,
        first_topup_bonus_kopeks=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
        first_topup_bonus_rubles=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS / 100,
        inviter_bonus_kopeks=settings.REFERRAL_INVITER_BONUS_KOPEKS,
        inviter_bonus_rubles=settings.REFERRAL_INVITER_BONUS_KOPEKS / 100,
        max_commission_payments=settings.REFERRAL_MAX_COMMISSION_PAYMENTS,
        partner_section_visible=settings.REFERRAL_PARTNER_SECTION_VISIBLE,
    )
