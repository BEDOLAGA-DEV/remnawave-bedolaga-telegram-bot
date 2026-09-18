"""Сегменты подписки — одно определение для выборок списка людей и для строки списка.

Раньше фильтр «Триал» искал строки со статусом ``trial``, а триалы (и классические,
и тарифные) создаются со статусом ``active`` и признаком ``is_trial``: сегмент был
пуст, «Активные» включали триалы и просроченных, «Истёкшие» не видели тех, у кого
срок прошёл, а монитор ещё не переставил статус. Здесь SQL-условие и его
python-зеркало для строки списка (чип «Триал N дн.» / «истекла») определены рядом,
чтобы фильтр, счётчики и подпись в строке никогда не расходились.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, or_
from sqlalchemy.sql import ColumnElement

from app.database.models import Subscription, SubscriptionStatus


LIVE_STATUSES: tuple[str, ...] = (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)

SEGMENT_TRIAL = 'trial'
SEGMENT_ACTIVE = 'active'
SEGMENT_EXPIRED = 'expired'
SEGMENT_LIMITED = 'limited'
SEGMENT_DISABLED = 'disabled'


def segment_condition(segment: str, now: datetime) -> ColumnElement[bool]:
    """SQL-условие «подписка относится к сегменту» (по строке ``subscriptions``)."""
    alive = and_(Subscription.status.in_(LIVE_STATUSES), Subscription.end_date > now)
    if segment == SEGMENT_TRIAL:
        return and_(alive, Subscription.is_trial.is_(True))
    if segment == SEGMENT_ACTIVE:
        # is_trial может быть NULL у старых строк — это не триал.
        return and_(alive, Subscription.is_trial.is_not(True))
    if segment == SEGMENT_EXPIRED:
        return or_(
            Subscription.status == SubscriptionStatus.EXPIRED.value,
            and_(Subscription.status.in_(LIVE_STATUSES), Subscription.end_date <= now),
        )
    if segment == SEGMENT_LIMITED:
        return Subscription.status == SubscriptionStatus.LIMITED.value
    if segment == SEGMENT_DISABLED:
        return Subscription.status == SubscriptionStatus.DISABLED.value
    return Subscription.status == segment


def subscription_segment(subscription: Subscription, now: datetime | None = None) -> str:
    """Python-зеркало ``segment_condition`` для одной подписки (строка списка, чип)."""
    now = now or datetime.now(UTC)
    status = subscription.status
    if status in (
        SubscriptionStatus.EXPIRED.value,
        SubscriptionStatus.DISABLED.value,
        SubscriptionStatus.LIMITED.value,
    ):
        return status
    if status in LIVE_STATUSES:
        end = subscription.end_date
        if end is not None and end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        if end is None or end <= now:
            return SEGMENT_EXPIRED
        return SEGMENT_TRIAL if subscription.is_trial else SEGMENT_ACTIVE
    return status
