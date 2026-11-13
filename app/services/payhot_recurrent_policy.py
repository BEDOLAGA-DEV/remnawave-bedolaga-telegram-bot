"""Shared authenticated HOT Pay recurring eligibility, independent of transport."""

from app.config import settings
from app.services.payment_method_config_service import get_payhot_checkout_policy


async def get_payhot_recurrent_policy(db, *, user, channel: str) -> dict:
    if channel not in ('telegram', 'cabinet'):
        raise ValueError('invalid_checkout_channel')
    base = {'enabled': False, 'reason': 'disabled', 'methods': []}
    if not getattr(settings, 'PAYHOT_RECURRENT_ENABLED', False):
        return base
    if user is None or user.status != 'active' or user.restriction_topup or user.restriction_subscription:
        return {**base, 'reason': 'user_restricted'}
    policy = await get_payhot_checkout_policy(db, user)
    methods = [option['id'] for option in policy['options'] if option['id'] in ('card', 'sbp')] if policy else []
    if not methods:
        return {**base, 'reason': 'payment_method_unavailable'}
    return {**base, 'enabled': True, 'reason': None, 'methods': methods, 'checkout_policy': policy}
