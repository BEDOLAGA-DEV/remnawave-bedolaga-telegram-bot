"""Resolve paid renewal terms independently of trial limits and stale cart data."""

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.utils.subscription_utils import resolve_tariff_purchase_device_limit


async def paid_trial_renewal_terms(db, subscription, *, tariff_id=None):
    target_id = tariff_id if tariff_id is not None else subscription.tariff_id
    if target_id is None:
        terms = {'device_limit': max(subscription.device_limit or 0, settings.DEFAULT_DEVICE_LIMIT)}
        if settings.is_traffic_fixed():
            terms['traffic_limit_gb'] = settings.get_fixed_traffic_limit()
        return terms
    tariff = await get_tariff_by_id(db, target_id)
    if tariff is None or not tariff.is_active:
        raise ValueError('Paid tariff is unavailable for trial conversion')
    return {
        'tariff_id': tariff.id,
        'traffic_limit_gb': tariff.traffic_limit_gb,
        'device_limit': resolve_tariff_purchase_device_limit(tariff, subscription),
        'connected_squads': list(tariff.allowed_squads or []),
    }
