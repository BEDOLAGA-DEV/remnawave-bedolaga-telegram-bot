"""Regression: subscription_period_days achievement must consider both
SubscriptionConversion.first_paid_period_days AND non-trial subscription
span (end_date - start_date), not only the conversion-table value.
"""
import inspect
from app.database.crud import achievement as ach_crud


def test_subscription_period_days_uses_two_sources():
    src = inspect.getsource(ach_crud._get_user_stat)

    # Conversion source.
    assert 'SubscriptionConversion.first_paid_period_days' in src, (
        'Conversion-row branch missing.'
    )
    # Direct-sub span source.
    assert 'Subscription.end_date' in src and 'Subscription.start_date' in src, (
        'Subscription-span fallback branch missing.'
    )
    # max() of the two sources.
    assert 'max(from_conversion, from_span)' in src, (
        'Final max() over both sources missing — fallback may have been removed.'
    )
