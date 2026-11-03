"""CisPay recurring contract: exact day intervals and server-side eligibility."""

from app.config import settings


def resolve_cispay_period(period_days: int, is_daily: bool) -> int:
    if is_daily or type(period_days) is not int or not 7 <= period_days <= 360:
        raise ValueError('CisPay поддерживает автоплатежи с периодом от 7 до 360 дней')
    return period_days


async def get_cispay_recurrent_policy(db, *, user, channel: str) -> dict:
    from app.services.payment_method_config_service import get_cispay_checkout_policy

    result = {'enabled': False, 'reason': 'disabled', 'min_days': 7, 'max_days': 360}
    if channel not in ('telegram', 'cabinet'):
        raise ValueError('Invalid checkout channel')
    if not settings.CISPAY_RECURRENT_ENABLED:
        return result
    if user is None or user.status != 'active' or user.restriction_topup:
        return {**result, 'reason': 'user_restricted'}
    policy = await get_cispay_checkout_policy(db, user, 'SBP', telegram_checkout=channel == 'telegram')
    if policy is None:
        return {**result, 'reason': 'sbp_unavailable'}
    return {**result, 'enabled': True, 'reason': None, 'checkout_policy': policy}


def cispay_tariff_terms(tariff) -> dict:
    """Entitlement being bought, separate from the provider's immutable amount."""
    return {
        'traffic_limit_gb': tariff.traffic_limit_gb,
        'device_limit': tariff.device_limit,
        'allowed_squads': sorted(set(tariff.allowed_squads or [])),
        'traffic_reset_mode': str(tariff.traffic_reset_mode or 'NO_RESET').upper(),
    }


def cispay_tariff_terms_match(tariff, snapshot: dict) -> bool:
    # Missing consent details cannot authorize a different set of entitlements.
    return snapshot.get('tariff_terms') == cispay_tariff_terms(tariff) and tariff.has_configured_price_for_period(
        snapshot['period_days']
    )
