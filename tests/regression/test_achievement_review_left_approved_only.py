"""Regression: review_left achievement must count only approved reviews.

Before fix: a pending or rejected review still unlocked the badge, which
let users farm by submitting throwaway reviews.
"""
import inspect
from app.database.crud import achievement as ach_crud


def test_review_left_filters_by_is_approved():
    src = inspect.getsource(ach_crud._get_user_stat)

    # The review_left branch must filter by UserReview.is_approved.is_(True).
    branch_start = src.index("condition_type == 'review_left'")
    branch = src[branch_start:branch_start + 600]
    assert 'UserReview.is_approved.is_(True)' in branch, (
        'review_left branch must filter by is_approved'
    )
