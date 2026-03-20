"""Admin routes for referral network graph visualization."""

import re
from collections import defaultdict

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    PartnerStatus,
    ReferralEarning,
    Subscription,
    Tariff,
    Transaction,
    TransactionType,
    User,
)
from app.utils.cache import RateLimitCache

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/referral-network', tags=['Cabinet Admin Referral Network'])

# ============ Constants ============

SPENT_TRANSACTION_TYPES: tuple[str, ...] = (TransactionType.SUBSCRIPTION_PAYMENT.value,)

EDGE_TYPE_REFERRAL = 'referral'
EDGE_TYPE_CAMPAIGN = 'campaign'
EDGE_TYPE_PARTNER_CAMPAIGN = 'partner_campaign'

NODE_PREFIX_USER = 'user_'
NODE_PREFIX_CAMPAIGN = 'campaign_'

TOP_REFERRERS_LIMIT = 5
SEARCH_RESULTS_LIMIT = 20
GRAPH_MAX_NODES = 5000

# Rate limits (per admin user, per window)
GRAPH_RATE_LIMIT = 10
GRAPH_RATE_WINDOW = 60
DETAIL_RATE_LIMIT = 30
DETAIL_RATE_WINDOW = 60
SEARCH_RATE_LIMIT = 30
SEARCH_RATE_WINDOW = 60

MAX_REFERRAL_DEPTH = 50

# Regex to escape LIKE wildcards
_LIKE_ESCAPE_RE = re.compile(r'([%_\\])')


# ============ Schemas ============


class NetworkUserNode(BaseModel):
    id: int
    tg_id: int | None
    username: str | None
    email: str | None
    display_name: str
    is_partner: bool
    referrer_id: int | None
    campaign_id: int | None
    direct_referrals: int
    total_branch_users: int
    branch_revenue_kopeks: int
    personal_revenue_kopeks: int
    personal_spent_kopeks: int
    subscription_name: str | None
    subscription_end: str | None
    registered_at: str | None


class TopReferrer(BaseModel):
    user_id: int
    username: str | None
    referral_count: int


class NetworkCampaignNode(BaseModel):
    id: int
    name: str
    start_parameter: str
    is_active: bool
    direct_users: int
    total_network_users: int
    total_revenue_kopeks: int
    conversion_rate: float
    avg_check_kopeks: int
    top_referrers: list[TopReferrer]


class NetworkEdge(BaseModel):
    source: str
    target: str
    type: str


class NetworkGraphResponse(BaseModel):
    users: list[NetworkUserNode]
    campaigns: list[NetworkCampaignNode]
    edges: list[NetworkEdge]
    total_users: int
    total_referrers: int
    total_campaigns: int
    total_earnings_kopeks: int


class NetworkUserDetail(BaseModel):
    id: int
    tg_id: int | None
    username: str | None
    email: str | None
    display_name: str
    is_partner: bool
    referrer_id: int | None
    referrer_display_name: str | None
    campaign_id: int | None
    campaign_name: str | None
    direct_referrals: int
    total_branch_users: int
    branch_revenue_kopeks: int
    personal_revenue_kopeks: int
    personal_spent_kopeks: int
    subscription_name: str | None
    subscription_end: str | None
    registered_at: str | None


class NetworkCampaignDetail(BaseModel):
    id: int
    name: str
    start_parameter: str
    is_active: bool
    direct_users: int
    total_network_users: int
    total_revenue_kopeks: int
    conversion_rate: float
    avg_check_kopeks: int
    top_referrers: list[TopReferrer]


class NetworkSearchResult(BaseModel):
    users: list[NetworkUserNode]
    campaigns: list[NetworkCampaignNode]


class CampaignOption(BaseModel):
    id: int
    name: str
    start_parameter: str
    is_active: bool
    direct_users: int


class PartnerOption(BaseModel):
    id: int
    display_name: str
    username: str | None
    campaign_count: int


class ScopeOptionsResponse(BaseModel):
    campaigns: list[CampaignOption]
    partners: list[PartnerOption]


# ============ Helpers ============


def _user_display_name(user: User) -> str:
    """Build display name from User model."""
    parts = [user.first_name, user.last_name]
    name = ' '.join(filter(None, parts))
    if name:
        return name
    if user.username:
        return user.username
    if user.telegram_id:
        return f'ID{user.telegram_id}'
    if user.email:
        return user.email.split('@')[0]
    return f'User{user.id}'


def _format_datetime(dt: object) -> str | None:
    """Format datetime to ISO string, handle None."""
    if dt is None:
        return None
    return dt.isoformat()


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards (%, _, \\) to prevent injection."""
    return _LIKE_ESCAPE_RE.sub(r'\\\1', value)


def _build_user_node(
    user: User,
    *,
    direct_referral_count: int,
    personal_revenue: int,
    branch_revenue: int,
    personal_spent: int,
    campaign_id: int | None,
    subscription_name: str | None,
    subscription_end_str: str | None,
) -> NetworkUserNode:
    return NetworkUserNode(
        id=user.id,
        tg_id=user.telegram_id,
        username=user.username,
        email=user.email,
        display_name=_user_display_name(user),
        is_partner=user.partner_status == PartnerStatus.APPROVED.value,
        referrer_id=user.referred_by_id,
        campaign_id=campaign_id,
        direct_referrals=direct_referral_count,
        total_branch_users=direct_referral_count,
        branch_revenue_kopeks=branch_revenue,
        personal_revenue_kopeks=personal_revenue,
        personal_spent_kopeks=personal_spent,
        subscription_name=subscription_name,
        subscription_end=subscription_end_str,
        registered_at=_format_datetime(user.created_at),
    )


# ============ Data fetching ============


async def _fetch_network_user_ids(db: AsyncSession) -> set[int]:
    """Get IDs of all users that participate in the referral network.

    A user is in the network if they:
    - have a referrer (referred_by_id IS NOT NULL)
    - have at least one referral (someone's referred_by_id points to them)
    - have at least one campaign registration
    """
    # Users with referrer
    referred_q = select(User.id).where(User.referred_by_id.isnot(None))

    # Users who are referrers
    referrer_q = select(User.referred_by_id).where(User.referred_by_id.isnot(None)).distinct()

    # Users with campaign registration
    campaign_user_q = select(AdvertisingCampaignRegistration.user_id).distinct()

    result_referred = await db.execute(referred_q)
    result_referrers = await db.execute(referrer_q)
    result_campaign = await db.execute(campaign_user_q)

    user_ids: set[int] = set()
    user_ids.update(row[0] for row in result_referred)
    user_ids.update(row[0] for row in result_referrers if row[0] is not None)
    user_ids.update(row[0] for row in result_campaign)

    return user_ids


async def _fetch_direct_referral_counts(db: AsyncSession, user_ids: set[int] | None = None) -> dict[int, int]:
    """Return {user_id: count_of_direct_referrals}.

    When user_ids is provided, only counts referrals for those users.
    """
    stmt = select(User.referred_by_id, func.count(User.id)).where(User.referred_by_id.isnot(None))
    if user_ids is not None:
        stmt = stmt.where(User.referred_by_id.in_(user_ids))
    stmt = stmt.group_by(User.referred_by_id)
    result = await db.execute(stmt)
    return {row[0]: row[1] for row in result}


async def _fetch_personal_revenue(db: AsyncSession, user_ids: set[int]) -> dict[int, int]:
    """Return {user_id: total_referral_earnings_kopeks} for given users."""
    if not user_ids:
        return {}

    stmt = (
        select(ReferralEarning.user_id, func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0))
        .where(ReferralEarning.user_id.in_(user_ids))
        .group_by(ReferralEarning.user_id)
    )
    result = await db.execute(stmt)
    return {row[0]: row[1] for row in result}


async def _fetch_branch_revenue(db: AsyncSession, user_ids: set[int]) -> dict[int, int]:
    """Return {referrer_id: total_spent_by_direct_referrals}.

    This sums subscription payments by each user's direct referrals (one level deep).
    """
    if not user_ids:
        return {}

    referred_user = (
        select(User.id, User.referred_by_id)
        .where(and_(User.referred_by_id.isnot(None), User.referred_by_id.in_(user_ids)))
        .subquery()
    )

    stmt = (
        select(
            referred_user.c.referred_by_id,
            func.coalesce(func.sum(Transaction.amount_kopeks), 0),
        )
        .join(referred_user, Transaction.user_id == referred_user.c.id)
        .where(
            and_(
                Transaction.type.in_(SPENT_TRANSACTION_TYPES),
                Transaction.is_completed.is_(True),
            )
        )
        .group_by(referred_user.c.referred_by_id)
    )
    result = await db.execute(stmt)
    return {row[0]: row[1] for row in result}


async def _fetch_personal_spent(db: AsyncSession, user_ids: set[int]) -> dict[int, int]:
    """Return {user_id: total_spent_kopeks} for given users."""
    if not user_ids:
        return {}

    stmt = (
        select(Transaction.user_id, func.coalesce(func.sum(Transaction.amount_kopeks), 0))
        .where(
            and_(
                Transaction.user_id.in_(user_ids),
                Transaction.type.in_(SPENT_TRANSACTION_TYPES),
                Transaction.is_completed.is_(True),
            )
        )
        .group_by(Transaction.user_id)
    )
    result = await db.execute(stmt)
    return {row[0]: row[1] for row in result}


async def _fetch_campaign_registrations(db: AsyncSession, user_ids: set[int] | None = None) -> dict[int, int]:
    """Return {user_id: first_campaign_id} (the earliest registration per user).

    When user_ids is provided, only fetches registrations for those users.
    """
    # Use a window function to pick the first registration per user
    row_num = (
        func.row_number()
        .over(
            partition_by=AdvertisingCampaignRegistration.user_id,
            order_by=AdvertisingCampaignRegistration.created_at.asc(),
        )
        .label('rn')
    )

    inner = select(
        AdvertisingCampaignRegistration.user_id,
        AdvertisingCampaignRegistration.campaign_id,
        row_num,
    )
    if user_ids is not None:
        inner = inner.where(AdvertisingCampaignRegistration.user_id.in_(user_ids))
    subq = inner.subquery()

    stmt = select(subq.c.user_id, subq.c.campaign_id).where(subq.c.rn == 1)
    result = await db.execute(stmt)
    return {row[0]: row[1] for row in result}


async def _fetch_subscription_info(db: AsyncSession, user_ids: set[int]) -> dict[int, tuple[str | None, str | None]]:
    """Return {user_id: (tariff_name, end_date_iso)} for given users."""
    if not user_ids:
        return {}

    stmt = (
        select(Subscription.user_id, Tariff.name, Subscription.end_date)
        .outerjoin(Tariff, Subscription.tariff_id == Tariff.id)
        .where(Subscription.user_id.in_(user_ids))
    )
    result = await db.execute(stmt)
    return {row[0]: (row[1], _format_datetime(row[2]) if row[2] else None) for row in result}


async def _fetch_campaign_stats(
    db: AsyncSession,
    referral_counts: dict[int, int],
    campaign_ids: set[int] | None = None,
) -> list[NetworkCampaignNode]:
    """Build campaign nodes with aggregated stats. Optionally scoped to campaign_ids."""
    stmt = select(AdvertisingCampaign)
    if campaign_ids is not None:
        stmt = stmt.where(AdvertisingCampaign.id.in_(campaign_ids))
    result = await db.execute(stmt)
    campaigns = list(result.scalars().all())

    if not campaigns:
        return []

    fetched_campaign_ids = [c.id for c in campaigns]

    # Users per campaign (for computing registration counts, network users, and top referrers)
    user_campaign_stmt = select(
        AdvertisingCampaignRegistration.campaign_id,
        AdvertisingCampaignRegistration.user_id,
    ).where(AdvertisingCampaignRegistration.campaign_id.in_(fetched_campaign_ids))
    uc_result = await db.execute(user_campaign_stmt)
    campaign_user_ids: dict[int, list[int]] = defaultdict(list)
    for row in uc_result:
        campaign_user_ids[row[0]].append(row[1])

    # Registration counts derived from the same query
    reg_counts: dict[int, int] = {cid: len(uids) for cid, uids in campaign_user_ids.items()}

    # Total spending by users from each campaign (for revenue, conversion, avg check)
    all_campaign_users = set()
    for uids in campaign_user_ids.values():
        all_campaign_users.update(uids)

    user_spent: dict[int, int] = {}
    if all_campaign_users:
        spent_stmt = (
            select(Transaction.user_id, func.coalesce(func.sum(Transaction.amount_kopeks), 0))
            .where(
                and_(
                    Transaction.user_id.in_(all_campaign_users),
                    Transaction.type.in_(SPENT_TRANSACTION_TYPES),
                    Transaction.is_completed.is_(True),
                )
            )
            .group_by(Transaction.user_id)
        )
        spent_result = await db.execute(spent_stmt)
        user_spent = {row[0]: row[1] for row in spent_result}

    # Top referrers: users from campaign who have the most referrals
    # Also need usernames for those users
    top_referrer_user_ids: set[int] = set()
    campaign_top_referrers_raw: dict[int, list[tuple[int, int]]] = {}

    for cid, uids in campaign_user_ids.items():
        scored = [(uid, referral_counts.get(uid, 0)) for uid in uids if referral_counts.get(uid, 0) > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:TOP_REFERRERS_LIMIT]
        campaign_top_referrers_raw[cid] = top
        top_referrer_user_ids.update(uid for uid, _ in top)

    username_map: dict[int, str | None] = {}
    if top_referrer_user_ids:
        uname_stmt = select(User.id, User.username).where(User.id.in_(top_referrer_user_ids))
        uname_result = await db.execute(uname_stmt)
        username_map = {row[0]: row[1] for row in uname_result}

    campaign_nodes: list[NetworkCampaignNode] = []
    for campaign in campaigns:
        cid = campaign.id
        direct_users = reg_counts.get(cid, 0)
        c_user_ids = campaign_user_ids.get(cid, [])

        # Total network users: direct users + their referrals
        network_users = direct_users
        for uid in c_user_ids:
            network_users += referral_counts.get(uid, 0)

        # Conversion = users who spent > 0 / total registered
        paying_users = sum(1 for uid in c_user_ids if user_spent.get(uid, 0) > 0)
        conversion_rate = (paying_users / direct_users * 100) if direct_users > 0 else 0.0

        # Total revenue = sum of subscription payments by campaign users
        total_spent_by_campaign_users = sum(user_spent.get(uid, 0) for uid in c_user_ids)
        avg_check = (total_spent_by_campaign_users // paying_users) if paying_users > 0 else 0

        top_refs = [
            TopReferrer(
                user_id=uid,
                username=username_map.get(uid),
                referral_count=cnt,
            )
            for uid, cnt in campaign_top_referrers_raw.get(cid, [])
        ]

        campaign_nodes.append(
            NetworkCampaignNode(
                id=cid,
                name=campaign.name,
                start_parameter=campaign.start_parameter,
                is_active=campaign.is_active,
                direct_users=direct_users,
                total_network_users=network_users,
                total_revenue_kopeks=total_spent_by_campaign_users,
                conversion_rate=round(conversion_rate, 2),
                avg_check_kopeks=avg_check,
                top_referrers=top_refs,
            )
        )

    return campaign_nodes


# ============ Endpoints ============


@router.get('/', response_model=NetworkGraphResponse)
async def get_referral_network(
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NetworkGraphResponse:
    """Return full referral network graph data for visualization."""
    if await RateLimitCache.is_rate_limited(
        admin.id,
        'referral_graph',
        GRAPH_RATE_LIMIT,
        GRAPH_RATE_WINDOW,
        fail_closed=True,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': str(GRAPH_RATE_WINDOW)},
        )
    logger.info('Fetching referral network graph', admin_id=admin.id)

    # Gather all IDs of users in the network
    network_user_ids = await _fetch_network_user_ids(db)

    if not network_user_ids:
        return NetworkGraphResponse(
            users=[],
            campaigns=[],
            edges=[],
            total_users=0,
            total_referrers=0,
            total_campaigns=0,
            total_earnings_kopeks=0,
        )

    # Cap to prevent excessive response sizes (deterministic: keep lowest IDs for stability)
    if len(network_user_ids) > GRAPH_MAX_NODES:
        logger.warning(
            'Referral network exceeds node limit, truncating',
            total=len(network_user_ids),
            limit=GRAPH_MAX_NODES,
        )
        network_user_ids = set(sorted(network_user_ids)[:GRAPH_MAX_NODES])

    # Batch-fetch all aggregated data (scoped to network users)
    referral_counts = await _fetch_direct_referral_counts(db, network_user_ids)
    personal_revenue = await _fetch_personal_revenue(db, network_user_ids)
    branch_revenue = await _fetch_branch_revenue(db, network_user_ids)
    personal_spent = await _fetch_personal_spent(db, network_user_ids)
    campaign_regs = await _fetch_campaign_registrations(db, network_user_ids)
    sub_info = await _fetch_subscription_info(db, network_user_ids)

    # Fetch actual user rows
    users_stmt = select(User).where(User.id.in_(network_user_ids))
    users_result = await db.execute(users_stmt)
    users = list(users_result.scalars().all())

    # Build user nodes
    user_nodes: list[NetworkUserNode] = []
    for user in users:
        sub = sub_info.get(user.id, (None, None))
        user_nodes.append(
            _build_user_node(
                user,
                direct_referral_count=referral_counts.get(user.id, 0),
                personal_revenue=personal_revenue.get(user.id, 0),
                branch_revenue=branch_revenue.get(user.id, 0),
                personal_spent=personal_spent.get(user.id, 0),
                campaign_id=campaign_regs.get(user.id),
                subscription_name=sub[0],
                subscription_end_str=sub[1],
            )
        )

    # Build campaign nodes
    campaign_nodes = await _fetch_campaign_stats(db, referral_counts)

    # Build edges
    edges: list[NetworkEdge] = []

    # Referral edges (only emit when both endpoints exist in the node set)
    for user in users:
        if user.referred_by_id is not None and user.referred_by_id in network_user_ids:
            edges.append(
                NetworkEdge(
                    source=f'{NODE_PREFIX_USER}{user.referred_by_id}',
                    target=f'{NODE_PREFIX_USER}{user.id}',
                    type=EDGE_TYPE_REFERRAL,
                )
            )

    # Campaign edges
    for user_id, campaign_id in campaign_regs.items():
        if user_id in network_user_ids:
            edges.append(
                NetworkEdge(
                    source=f'{NODE_PREFIX_CAMPAIGN}{campaign_id}',
                    target=f'{NODE_PREFIX_USER}{user_id}',
                    type=EDGE_TYPE_CAMPAIGN,
                )
            )

    # Partner ↔ Campaign edges (partner owns campaign)
    partner_campaigns_stmt = select(
        AdvertisingCampaign.id,
        AdvertisingCampaign.partner_user_id,
    ).where(AdvertisingCampaign.partner_user_id.isnot(None))
    partner_campaigns_result = await db.execute(partner_campaigns_stmt)
    for campaign_id, partner_user_id in partner_campaigns_result:
        if partner_user_id in network_user_ids:
            edges.append(
                NetworkEdge(
                    source=f'{NODE_PREFIX_USER}{partner_user_id}',
                    target=f'{NODE_PREFIX_CAMPAIGN}{campaign_id}',
                    type=EDGE_TYPE_PARTNER_CAMPAIGN,
                )
            )

    # Summary stats
    total_referrers = len([u for u in user_nodes if u.direct_referrals > 0])

    total_earnings = sum(personal_revenue.values())

    return NetworkGraphResponse(
        users=user_nodes,
        campaigns=campaign_nodes,
        edges=edges,
        total_users=len(user_nodes),
        total_referrers=total_referrers,
        total_campaigns=len(campaign_nodes),
        total_earnings_kopeks=total_earnings,
    )


@router.get('/scope-options', response_model=ScopeOptionsResponse)
async def get_scope_options(
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ScopeOptionsResponse:
    """Return lightweight lists of campaigns and partners for the scope selector."""
    if await RateLimitCache.is_rate_limited(
        admin.id,
        'referral_scope_opts',
        DETAIL_RATE_LIMIT,
        DETAIL_RATE_WINDOW,
        fail_closed=True,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': str(DETAIL_RATE_WINDOW)},
        )

    # Campaigns with registration counts
    campaign_stmt = (
        select(
            AdvertisingCampaign.id,
            AdvertisingCampaign.name,
            AdvertisingCampaign.start_parameter,
            AdvertisingCampaign.is_active,
            func.count(AdvertisingCampaignRegistration.id).label('direct_users'),
        )
        .outerjoin(
            AdvertisingCampaignRegistration,
            AdvertisingCampaignRegistration.campaign_id == AdvertisingCampaign.id,
        )
        .group_by(AdvertisingCampaign.id)
        .order_by(AdvertisingCampaign.name)
    )
    campaign_result = await db.execute(campaign_stmt)
    campaign_options = [
        CampaignOption(
            id=row[0],
            name=row[1],
            start_parameter=row[2],
            is_active=row[3],
            direct_users=row[4],
        )
        for row in campaign_result
    ]

    # Partners with campaign counts
    partner_stmt = (
        select(
            User.id,
            User.username,
            User.first_name,
            User.last_name,
            User.telegram_id,
            User.email,
            func.count(AdvertisingCampaign.id).label('campaign_count'),
        )
        .outerjoin(AdvertisingCampaign, AdvertisingCampaign.partner_user_id == User.id)
        .where(User.partner_status == PartnerStatus.APPROVED.value)
        .group_by(User.id)
        .order_by(User.id)
    )
    partner_result = await db.execute(partner_stmt)
    partner_options = []
    for row in partner_result:
        user_obj = User(
            id=row[0],
            username=row[1],
            first_name=row[2],
            last_name=row[3],
            telegram_id=row[4],
            email=row[5],
        )
        partner_options.append(
            PartnerOption(
                id=row[0],
                display_name=_user_display_name(user_obj),
                username=row[1],
                campaign_count=row[6],
            )
        )

    return ScopeOptionsResponse(campaigns=campaign_options, partners=partner_options)


async def _get_descendant_user_ids(db: AsyncSession, root_ids: set[int]) -> set[int]:
    """Get all user IDs in the referral trees rooted at root_ids (inclusive)."""
    if not root_ids:
        return set()

    anchor = (
        select(User.id, literal(0).label('depth')).where(User.id.in_(root_ids)).cte(name='descendants', recursive=True)
    )
    rpart = (
        select(User.id, (anchor.c.depth + 1).label('depth'))
        .join(anchor, User.referred_by_id == anchor.c.id)
        .where(anchor.c.depth < MAX_REFERRAL_DEPTH)
    )
    descendants_cte = anchor.union_all(rpart)

    result = await db.execute(select(func.distinct(descendants_cte.c.id)))
    return {row[0] for row in result}


async def _get_ancestor_user_ids(db: AsyncSession, start_user_ids: set[int]) -> set[int]:
    """Walk up the referral chain from start_user_ids to root (inclusive)."""
    if not start_user_ids:
        return set()
    anchor = (
        select(User.id, User.referred_by_id, literal(0).label('depth'))
        .where(User.id.in_(start_user_ids))
        .cte(name='ancestors', recursive=True)
    )
    rpart = (
        select(User.id, User.referred_by_id, (anchor.c.depth + 1).label('depth'))
        .join(anchor, User.id == anchor.c.referred_by_id)
        .where(anchor.c.depth < MAX_REFERRAL_DEPTH)
    )
    ancestors_cte = anchor.union_all(rpart)

    result = await db.execute(select(func.distinct(ancestors_cte.c.id)))
    return {row[0] for row in result}


async def _build_scoped_graph(
    db: AsyncSession,
    scoped_user_ids: set[int],
    campaign_ids: set[int],
) -> NetworkGraphResponse:
    """Build graph response for a scoped set of users and campaigns."""
    if not scoped_user_ids:
        # Still show campaign nodes even with no users
        if campaign_ids:
            campaign_nodes = await _fetch_campaign_stats(db, {}, campaign_ids=campaign_ids)
            return NetworkGraphResponse(
                users=[],
                campaigns=campaign_nodes,
                edges=[],
                total_users=0,
                total_referrers=0,
                total_campaigns=len(campaign_nodes),
                total_earnings_kopeks=0,
            )
        return NetworkGraphResponse(
            users=[],
            campaigns=[],
            edges=[],
            total_users=0,
            total_referrers=0,
            total_campaigns=0,
            total_earnings_kopeks=0,
        )

    # Cap to prevent excessive response sizes
    if len(scoped_user_ids) > GRAPH_MAX_NODES:
        logger.warning(
            'Scoped referral network exceeds node limit, truncating',
            total=len(scoped_user_ids),
            limit=GRAPH_MAX_NODES,
        )
        scoped_user_ids = set(sorted(scoped_user_ids)[:GRAPH_MAX_NODES])

    referral_counts = await _fetch_direct_referral_counts(db, scoped_user_ids)
    personal_revenue = await _fetch_personal_revenue(db, scoped_user_ids)
    branch_revenue = await _fetch_branch_revenue(db, scoped_user_ids)
    personal_spent = await _fetch_personal_spent(db, scoped_user_ids)
    campaign_regs = await _fetch_campaign_registrations(db, scoped_user_ids)
    sub_info = await _fetch_subscription_info(db, scoped_user_ids)

    users_result = await db.execute(select(User).where(User.id.in_(scoped_user_ids)))
    users = list(users_result.scalars().all())

    user_nodes: list[NetworkUserNode] = []
    for user in users:
        sub = sub_info.get(user.id, (None, None))
        user_nodes.append(
            _build_user_node(
                user,
                direct_referral_count=referral_counts.get(user.id, 0),
                personal_revenue=personal_revenue.get(user.id, 0),
                branch_revenue=branch_revenue.get(user.id, 0),
                personal_spent=personal_spent.get(user.id, 0),
                campaign_id=campaign_regs.get(user.id),
                subscription_name=sub[0],
                subscription_end_str=sub[1],
            )
        )

    # Include campaigns from the scope + any campaigns users registered through
    all_campaign_ids = campaign_ids | set(campaign_regs.values())
    all_campaign_ids.discard(None)
    campaign_nodes = (
        await _fetch_campaign_stats(db, referral_counts, campaign_ids=all_campaign_ids) if all_campaign_ids else []
    )

    edges: list[NetworkEdge] = []

    for user in users:
        if user.referred_by_id is not None and user.referred_by_id in scoped_user_ids:
            edges.append(
                NetworkEdge(
                    source=f'{NODE_PREFIX_USER}{user.referred_by_id}',
                    target=f'{NODE_PREFIX_USER}{user.id}',
                    type=EDGE_TYPE_REFERRAL,
                )
            )

    for user_id, cid in campaign_regs.items():
        if user_id in scoped_user_ids and cid in all_campaign_ids:
            edges.append(
                NetworkEdge(
                    source=f'{NODE_PREFIX_CAMPAIGN}{cid}',
                    target=f'{NODE_PREFIX_USER}{user_id}',
                    type=EDGE_TYPE_CAMPAIGN,
                )
            )

    partner_stmt = select(
        AdvertisingCampaign.id,
        AdvertisingCampaign.partner_user_id,
    ).where(
        AdvertisingCampaign.partner_user_id.isnot(None),
        AdvertisingCampaign.id.in_(all_campaign_ids),
    )
    for cid, pid in await db.execute(partner_stmt):
        if pid in scoped_user_ids:
            edges.append(
                NetworkEdge(
                    source=f'{NODE_PREFIX_USER}{pid}',
                    target=f'{NODE_PREFIX_CAMPAIGN}{cid}',
                    type=EDGE_TYPE_PARTNER_CAMPAIGN,
                )
            )

    total_referrers = len([u for u in user_nodes if u.direct_referrals > 0])
    total_earnings = sum(personal_revenue.values())

    return NetworkGraphResponse(
        users=user_nodes,
        campaigns=campaign_nodes,
        edges=edges,
        total_users=len(user_nodes),
        total_referrers=total_referrers,
        total_campaigns=len(campaign_nodes),
        total_earnings_kopeks=total_earnings,
    )


MAX_SCOPE_ITEMS = 50


@router.get('/scoped', response_model=NetworkGraphResponse)
async def get_scoped_referral_network(
    campaign_ids: list[int] = Query(default=[], max_length=MAX_SCOPE_ITEMS),
    partner_ids: list[int] = Query(default=[], max_length=MAX_SCOPE_ITEMS),
    user_ids: list[int] = Query(default=[], max_length=MAX_SCOPE_ITEMS),
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NetworkGraphResponse:
    """Return scoped referral network graph for selected campaigns, partners, and/or users."""
    if await RateLimitCache.is_rate_limited(
        admin.id,
        'referral_scoped',
        GRAPH_RATE_LIMIT,
        GRAPH_RATE_WINDOW,
        fail_closed=True,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': str(GRAPH_RATE_WINDOW)},
        )

    unique_campaign_ids = set(campaign_ids)
    unique_partner_ids = set(partner_ids)
    unique_user_ids = set(user_ids)

    total_items = len(unique_campaign_ids) + len(unique_partner_ids) + len(unique_user_ids)
    if total_items == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='At least one campaign, partner, or user must be selected',
        )
    if total_items > MAX_SCOPE_ITEMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Too many items selected (max {MAX_SCOPE_ITEMS})',
        )

    all_scoped_user_ids: set[int] = set()
    all_campaign_ids: set[int] = set()

    # --- Campaigns ---
    if unique_campaign_ids:
        existing = await db.execute(
            select(AdvertisingCampaign.id).where(AdvertisingCampaign.id.in_(unique_campaign_ids))
        )
        valid_campaign_ids = {row[0] for row in existing}
        if valid_campaign_ids:
            all_campaign_ids |= valid_campaign_ids

            campaign_reg_result = await db.execute(
                select(AdvertisingCampaignRegistration.user_id).where(
                    AdvertisingCampaignRegistration.campaign_id.in_(valid_campaign_ids)
                )
            )
            campaign_registered_ids = {row[0] for row in campaign_reg_result}
            campaign_descendant_ids = await _get_descendant_user_ids(db, campaign_registered_ids)
            all_scoped_user_ids |= campaign_descendant_ids

    # --- Partners ---
    if unique_partner_ids:
        partner_result = await db.execute(
            select(User.id).where(
                User.id.in_(unique_partner_ids),
                User.partner_status == PartnerStatus.APPROVED.value,
            )
        )
        valid_partner_ids = {row[0] for row in partner_result}
        if valid_partner_ids:
            partner_campaigns_result = await db.execute(
                select(AdvertisingCampaign.id).where(AdvertisingCampaign.partner_user_id.in_(valid_partner_ids))
            )
            partner_campaign_set = {row[0] for row in partner_campaigns_result}
            all_campaign_ids |= partner_campaign_set

            partner_registered_ids: set[int] = set()
            if partner_campaign_set:
                partner_reg_result = await db.execute(
                    select(AdvertisingCampaignRegistration.user_id).where(
                        AdvertisingCampaignRegistration.campaign_id.in_(partner_campaign_set)
                    )
                )
                partner_registered_ids = {row[0] for row in partner_reg_result}

            partner_descendant_ids = await _get_descendant_user_ids(db, partner_registered_ids | valid_partner_ids)
            all_scoped_user_ids |= partner_descendant_ids

    # --- Users ---
    if unique_user_ids:
        user_result = await db.execute(select(User.id).where(User.id.in_(unique_user_ids)))
        valid_user_ids = {row[0] for row in user_result}
        if valid_user_ids:
            ancestor_ids = await _get_ancestor_user_ids(db, valid_user_ids)
            descendant_ids = await _get_descendant_user_ids(db, valid_user_ids)
            all_scoped_user_ids |= ancestor_ids | descendant_ids

    # Fail only if ALL provided IDs were invalid
    if not all_scoped_user_ids and not all_campaign_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No valid items found for the provided IDs',
        )

    # Discover campaigns that scoped users registered through (runs for all scopes)
    if all_scoped_user_ids:
        scope_campaign_regs = await _fetch_campaign_registrations(db, all_scoped_user_ids)
        relevant_campaigns = {cid for cid in scope_campaign_regs.values() if cid is not None}
        all_campaign_ids |= relevant_campaigns

    return await _build_scoped_graph(db, all_scoped_user_ids, all_campaign_ids)


@router.get('/user/{user_id}', response_model=NetworkUserDetail)
async def get_network_user_detail(
    user_id: int,
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NetworkUserDetail:
    """Return detailed info about a specific user in the referral network."""
    if await RateLimitCache.is_rate_limited(
        admin.id,
        'referral_user_detail',
        DETAIL_RATE_LIMIT,
        DETAIL_RATE_WINDOW,
        fail_closed=True,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': str(DETAIL_RATE_WINDOW)},
        )
    logger.info('Fetching network user detail', admin_id=admin.id, target_user_id=user_id)

    # Fetch user with subscription eagerly loaded
    stmt = (
        select(User)
        .options(selectinload(User.subscription).selectinload(Subscription.tariff))
        .where(User.id == user_id)
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    # Direct referral count
    ref_count_stmt = select(func.count(User.id)).where(User.referred_by_id == user_id)
    ref_count_result = await db.execute(ref_count_stmt)
    direct_referrals = ref_count_result.scalar() or 0

    # Personal revenue
    personal_rev_stmt = select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
        ReferralEarning.user_id == user_id
    )
    personal_rev_result = await db.execute(personal_rev_stmt)
    personal_revenue = personal_rev_result.scalar() or 0

    # Branch revenue: computed after branch CTE (see below)
    branch_revenue = 0

    # Personal spent
    spent_stmt = select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
        and_(
            Transaction.user_id == user_id,
            Transaction.type.in_(SPENT_TRANSACTION_TYPES),
            Transaction.is_completed.is_(True),
        )
    )
    spent_result = await db.execute(spent_stmt)
    personal_spent = spent_result.scalar() or 0

    # Campaign registration
    campaign_reg_stmt = (
        select(AdvertisingCampaignRegistration.campaign_id)
        .where(AdvertisingCampaignRegistration.user_id == user_id)
        .order_by(AdvertisingCampaignRegistration.created_at.asc())
        .limit(1)
    )
    campaign_reg_result = await db.execute(campaign_reg_stmt)
    campaign_id = campaign_reg_result.scalar_one_or_none()

    # Campaign name
    campaign_name: str | None = None
    if campaign_id is not None:
        camp_stmt = select(AdvertisingCampaign.name).where(AdvertisingCampaign.id == campaign_id)
        camp_result = await db.execute(camp_stmt)
        campaign_name = camp_result.scalar_one_or_none()

    # Total branch users via recursive CTE (with depth limit to prevent cycles)
    base = (
        select(User.id, literal(1).label('depth'))
        .where(User.referred_by_id == user_id)
        .cte(name='branch', recursive=True)
    )
    recursive_part = (
        select(User.id, (base.c.depth + 1).label('depth'))
        .join(base, User.referred_by_id == base.c.id)
        .where(base.c.depth < MAX_REFERRAL_DEPTH)
    )
    branch_cte = base.union_all(recursive_part)  # UNION ALL + count(distinct) is faster than UNION
    total_branch_stmt = select(func.count(func.distinct(branch_cte.c.id))).select_from(branch_cte)
    total_branch_result = await db.execute(total_branch_stmt)
    total_branch_users = total_branch_result.scalar() or 0

    # Branch revenue: total spent by all users in the branch
    branch_user_ids_stmt = select(branch_cte.c.id)
    branch_rev_stmt = select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
        and_(
            Transaction.user_id.in_(branch_user_ids_stmt),
            Transaction.type.in_(SPENT_TRANSACTION_TYPES),
            Transaction.is_completed.is_(True),
        )
    )
    branch_rev_result = await db.execute(branch_rev_stmt)
    branch_revenue = branch_rev_result.scalar() or 0

    # Referrer info
    referrer_display_name: str | None = None
    if user.referred_by_id is not None:
        referrer_stmt = select(User).where(User.id == user.referred_by_id)
        referrer_result = await db.execute(referrer_stmt)
        referrer = referrer_result.scalar_one_or_none()
        if referrer is not None:
            referrer_display_name = _user_display_name(referrer)

    # Subscription info
    subscription_name: str | None = None
    subscription_end: str | None = None
    if user.subscription is not None:
        if user.subscription.tariff is not None:
            subscription_name = user.subscription.tariff.name
        subscription_end = _format_datetime(user.subscription.end_date)

    return NetworkUserDetail(
        id=user.id,
        tg_id=user.telegram_id,
        username=user.username,
        email=user.email,
        display_name=_user_display_name(user),
        is_partner=user.partner_status == PartnerStatus.APPROVED.value,
        referrer_id=user.referred_by_id,
        referrer_display_name=referrer_display_name,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        direct_referrals=direct_referrals,
        total_branch_users=total_branch_users,
        branch_revenue_kopeks=branch_revenue,
        personal_revenue_kopeks=personal_revenue,
        personal_spent_kopeks=personal_spent,
        subscription_name=subscription_name,
        subscription_end=subscription_end,
        registered_at=_format_datetime(user.created_at),
    )


@router.get('/campaign/{campaign_id}', response_model=NetworkCampaignDetail)
async def get_network_campaign_detail(
    campaign_id: int,
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NetworkCampaignDetail:
    """Return detailed info about a specific advertising campaign."""
    if await RateLimitCache.is_rate_limited(
        admin.id,
        'referral_campaign_detail',
        DETAIL_RATE_LIMIT,
        DETAIL_RATE_WINDOW,
        fail_closed=True,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': str(DETAIL_RATE_WINDOW)},
        )
    logger.info('Fetching network campaign detail', admin_id=admin.id, campaign_id=campaign_id)

    # Fetch campaign
    stmt = select(AdvertisingCampaign).where(AdvertisingCampaign.id == campaign_id)
    result = await db.execute(stmt)
    campaign = result.scalar_one_or_none()

    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Campaign not found',
        )

    # Registration count
    reg_count_stmt = select(func.count(AdvertisingCampaignRegistration.id)).where(
        AdvertisingCampaignRegistration.campaign_id == campaign_id
    )
    reg_result = await db.execute(reg_count_stmt)
    direct_users = reg_result.scalar() or 0

    # User IDs from this campaign
    user_ids_stmt = select(AdvertisingCampaignRegistration.user_id).where(
        AdvertisingCampaignRegistration.campaign_id == campaign_id
    )
    user_ids_result = await db.execute(user_ids_stmt)
    campaign_user_ids = [row[0] for row in user_ids_result]

    # Referral counts for campaign users
    referral_counts: dict[int, int] = {}
    total_network_users = direct_users
    if campaign_user_ids:
        ref_stmt = (
            select(User.referred_by_id, func.count(User.id))
            .where(User.referred_by_id.in_(campaign_user_ids))
            .group_by(User.referred_by_id)
        )
        ref_result = await db.execute(ref_stmt)
        referral_counts = {row[0]: row[1] for row in ref_result}
        total_network_users += sum(referral_counts.values())

    # Spending by campaign users (for conversion, avg check, and total revenue)
    paying_users = 0
    total_spent = 0
    if campaign_user_ids:
        spent_stmt = (
            select(Transaction.user_id, func.coalesce(func.sum(Transaction.amount_kopeks), 0))
            .where(
                and_(
                    Transaction.user_id.in_(campaign_user_ids),
                    Transaction.type.in_(SPENT_TRANSACTION_TYPES),
                    Transaction.is_completed.is_(True),
                )
            )
            .group_by(Transaction.user_id)
        )
        spent_result = await db.execute(spent_stmt)
        for row in spent_result:
            if row[1] > 0:
                paying_users += 1
                total_spent += row[1]

    conversion_rate = (paying_users / direct_users * 100) if direct_users > 0 else 0.0
    avg_check = (total_spent // paying_users) if paying_users > 0 else 0

    # Top referrers from this campaign
    scored = [(uid, referral_counts.get(uid, 0)) for uid in campaign_user_ids if referral_counts.get(uid, 0) > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:TOP_REFERRERS_LIMIT]

    top_user_ids = [uid for uid, _ in top]
    username_map: dict[int, str | None] = {}
    if top_user_ids:
        uname_stmt = select(User.id, User.username).where(User.id.in_(top_user_ids))
        uname_result = await db.execute(uname_stmt)
        username_map = {row[0]: row[1] for row in uname_result}

    top_referrers = [
        TopReferrer(
            user_id=uid,
            username=username_map.get(uid),
            referral_count=cnt,
        )
        for uid, cnt in top
    ]

    return NetworkCampaignDetail(
        id=campaign.id,
        name=campaign.name,
        start_parameter=campaign.start_parameter,
        is_active=campaign.is_active,
        direct_users=direct_users,
        total_network_users=total_network_users,
        total_revenue_kopeks=total_spent,
        conversion_rate=round(conversion_rate, 2),
        avg_check_kopeks=avg_check,
        top_referrers=top_referrers,
    )


@router.get('/search', response_model=NetworkSearchResult)
async def search_referral_network(
    q: str = Query(..., min_length=1, max_length=200, description='Search query'),
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NetworkSearchResult:
    """Search users and campaigns in the referral network by telegram_id, username, email, or campaign name."""
    if await RateLimitCache.is_rate_limited(
        admin.id,
        'referral_search',
        SEARCH_RATE_LIMIT,
        SEARCH_RATE_WINDOW,
        fail_closed=True,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': str(SEARCH_RATE_WINDOW)},
        )
    logger.info('Searching referral network', admin_id=admin.id, query_len=len(q))

    query_stripped = q.strip()
    escaped_query = _escape_like(query_stripped)

    # Build user search conditions (with escaped LIKE wildcards)
    user_conditions = [
        User.username.ilike(f'%{escaped_query}%', escape='\\'),
        User.email.ilike(f'%{escaped_query}%', escape='\\'),
    ]

    # If query is numeric, also search by telegram_id and user id
    if query_stripped.isdigit():
        numeric_val = int(query_stripped)
        user_conditions.append(User.telegram_id == numeric_val)
        user_conditions.append(User.id == numeric_val)

    # Find matching users that are part of the referral network
    network_user_ids = await _fetch_network_user_ids(db)

    user_stmt = (
        select(User)
        .where(
            and_(
                User.id.in_(network_user_ids) if network_user_ids else literal(False),
                or_(*user_conditions),
            )
        )
        .limit(SEARCH_RESULTS_LIMIT)
    )
    user_result = await db.execute(user_stmt)
    matched_users = list(user_result.scalars().all())

    # Batch-fetch data for matched users
    matched_ids = {u.id for u in matched_users}
    user_nodes: list[NetworkUserNode] = []

    # Fetch referral counts scoped to matched users
    referral_counts = await _fetch_direct_referral_counts(db, matched_ids) if matched_ids else {}

    if matched_ids:
        personal_revenue = await _fetch_personal_revenue(db, matched_ids)
        branch_revenue = await _fetch_branch_revenue(db, matched_ids)
        personal_spent = await _fetch_personal_spent(db, matched_ids)
        campaign_regs = await _fetch_campaign_registrations(db, matched_ids)
        sub_info = await _fetch_subscription_info(db, matched_ids)

        for user in matched_users:
            sub = sub_info.get(user.id, (None, None))
            user_nodes.append(
                _build_user_node(
                    user,
                    direct_referral_count=referral_counts.get(user.id, 0),
                    personal_revenue=personal_revenue.get(user.id, 0),
                    branch_revenue=branch_revenue.get(user.id, 0),
                    personal_spent=personal_spent.get(user.id, 0),
                    campaign_id=campaign_regs.get(user.id),
                    subscription_name=sub[0],
                    subscription_end_str=sub[1],
                )
            )

    # Search campaigns (with escaped LIKE wildcards)
    campaign_stmt = (
        select(AdvertisingCampaign)
        .where(
            or_(
                AdvertisingCampaign.name.ilike(f'%{escaped_query}%', escape='\\'),
                AdvertisingCampaign.start_parameter.ilike(f'%{escaped_query}%', escape='\\'),
            )
        )
        .limit(SEARCH_RESULTS_LIMIT)
    )
    campaign_result = await db.execute(campaign_stmt)
    matched_campaigns = list(campaign_result.scalars().all())

    # Batch campaign stats instead of N+1 queries per campaign
    campaign_nodes: list[NetworkCampaignNode] = []
    if matched_campaigns:
        matched_campaign_ids = [c.id for c in matched_campaigns]

        # Batch: registration counts per campaign
        reg_count_stmt = (
            select(
                AdvertisingCampaignRegistration.campaign_id,
                func.count(AdvertisingCampaignRegistration.id),
            )
            .where(AdvertisingCampaignRegistration.campaign_id.in_(matched_campaign_ids))
            .group_by(AdvertisingCampaignRegistration.campaign_id)
        )
        reg_res = await db.execute(reg_count_stmt)
        reg_counts: dict[int, int] = {row[0]: row[1] for row in reg_res}

        # Batch: user IDs per campaign
        user_campaign_stmt = select(
            AdvertisingCampaignRegistration.campaign_id,
            AdvertisingCampaignRegistration.user_id,
        ).where(AdvertisingCampaignRegistration.campaign_id.in_(matched_campaign_ids))
        uc_res = await db.execute(user_campaign_stmt)
        campaign_user_map: dict[int, list[int]] = defaultdict(list)
        for row in uc_res:
            campaign_user_map[row[0]].append(row[1])

        # Fetch referral counts and spending scoped to campaign users
        all_campaign_user_ids: set[int] = set()
        for uids in campaign_user_map.values():
            all_campaign_user_ids.update(uids)

        # Batch: spending per user (for campaign revenue)
        campaign_user_spent: dict[int, int] = {}
        if all_campaign_user_ids:
            spent_stmt = (
                select(Transaction.user_id, func.coalesce(func.sum(Transaction.amount_kopeks), 0))
                .where(
                    and_(
                        Transaction.user_id.in_(all_campaign_user_ids),
                        Transaction.type.in_(SPENT_TRANSACTION_TYPES),
                        Transaction.is_completed.is_(True),
                    )
                )
                .group_by(Transaction.user_id)
            )
            spent_res = await db.execute(spent_stmt)
            campaign_user_spent = {row[0]: row[1] for row in spent_res}
        campaign_referral_counts = (
            await _fetch_direct_referral_counts(db, all_campaign_user_ids) if all_campaign_user_ids else {}
        )

        for campaign in matched_campaigns:
            cid = campaign.id
            direct_users = reg_counts.get(cid, 0)
            c_user_ids = campaign_user_map.get(cid, [])
            network_users = direct_users + sum(campaign_referral_counts.get(uid, 0) for uid in c_user_ids)
            total_revenue = sum(campaign_user_spent.get(uid, 0) for uid in c_user_ids)

            campaign_nodes.append(
                NetworkCampaignNode(
                    id=cid,
                    name=campaign.name,
                    start_parameter=campaign.start_parameter,
                    is_active=campaign.is_active,
                    direct_users=direct_users,
                    total_network_users=network_users,
                    total_revenue_kopeks=total_revenue,
                    conversion_rate=0.0,
                    avg_check_kopeks=0,
                    top_referrers=[],
                )
            )

    return NetworkSearchResult(
        users=user_nodes,
        campaigns=campaign_nodes,
    )
