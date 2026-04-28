"""Regression: expired-subscription day-1 notification must compute the
renewal price via PricingEngine, not from the legacy global PRICE_30_DAYS.

Before fix: the message hardcoded `settings.PRICE_30_DAYS`, which under
SALES_MODE=tariffs is a misleading classic-mode placeholder. Users with a
real cheaper tariff (40₽ minimum, 33₽ with promo-group discount) saw the
notification claim 100₽.
"""
import inspect
from app.services import monitoring_service


def test_send_expired_day1_notification_uses_pricing_engine():
    src = inspect.getsource(monitoring_service.MonitoringService._send_expired_day1_notification)

    # Post-fix: pricing_engine.calculate_renewal_price must be called.
    assert 'pricing_engine.calculate_renewal_price' in src, (
        'Expected pricing_engine to be called for renewal price calc'
    )

    # Per-tariff shortest-period lookup must be present.
    assert 'tariff.get_shortest_period()' in src, (
        'Expected per-tariff shortest-period lookup'
    )

    # The function must take a `db` parameter so the engine has a session.
    sig = inspect.signature(monitoring_service.MonitoringService._send_expired_day1_notification)
    assert 'db' in sig.parameters, (
        'Function signature must include `db` for PricingEngine access'
    )
