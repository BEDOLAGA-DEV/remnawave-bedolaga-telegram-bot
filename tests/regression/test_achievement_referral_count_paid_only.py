"""Regression: referral_count condition must count only paid referrals.

Before fix: 25 fake unfunded sign-ups unlocked the Ambassador badge.
After fix: the count is gated by `User.id IN (paid deposit users)`.
"""
import inspect
from app.database.crud import achievement as ach_crud


def test_referral_count_filters_to_paid_users():
    src = inspect.getsource(ach_crud._get_user_stat)

    # Subquery for paid users must be present.
    assert 'paid_refs_subq' in src, 'paid-refs subquery missing'
    # The subquery filters by completed deposits.
    assert 'TransactionType.DEPOSIT.value' in src, (
        'paid-refs subquery should filter by DEPOSIT type'
    )
    assert 'is_completed.is_(True)' in src, (
        'paid-refs subquery should filter by completed transactions'
    )
    # The outer query AND-joins User.id with the subquery.
    assert 'User.id.in_(select(paid_refs_subq.c.user_id))' in src, (
        'Outer referral_count query must filter by paid-refs subquery'
    )
