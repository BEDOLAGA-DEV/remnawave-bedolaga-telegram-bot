"""Device selection for new tariff purchases, shared by all checkout channels.

The caller must resolve the purchase target and check ownership first. A catalog
purchase can resolve to an existing paid subscription: it is not a new purchase.
"""

from dataclasses import dataclass

from app.config import settings
from app.utils.subscription_utils import resolve_tariff_purchase_device_limit


MAX_DEVICE_LIMIT = 2_147_483_647


class DeviceSelectionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PurchaseDeviceSelection:
    effective_limit: int
    included_limit: int
    selectable: bool
    min_limit: int | None
    max_limit: int | None
    reason: str | None


def resolve_purchase_device_selection(
    tariff, subscription, selected_device_limit: int | None
) -> PurchaseDeviceSelection:
    """Preserve legacy defaults; never reinterpret renewal as device purchase."""
    included = tariff.device_limit if tariff.device_limit is not None else 1
    effective = resolve_tariff_purchase_device_limit(tariff, subscription)
    configured_max = tariff.max_device_limit or settings.MAX_DEVICES_LIMIT
    maximum = min(configured_max, MAX_DEVICE_LIMIT) if configured_max > 0 else MAX_DEVICE_LIMIT
    reason = None
    if (
        (tariff.max_device_limit is not None and tariff.max_device_limit < 0)
        or included < 0
        or included > MAX_DEVICE_LIMIT
        or maximum < included
    ):
        reason = 'invalid_device_configuration'
    elif included == 0:
        reason = 'unlimited_devices'
    elif subscription is not None and not subscription.is_trial:
        reason = 'existing_paid_subscription'
    elif tariff.device_price_kopeks is None or tariff.device_price_kopeks <= 0:
        reason = 'extra_devices_not_available'
    elif maximum == included:
        reason = 'device_limit_reached'

    if selected_device_limit is not None:
        if type(selected_device_limit) is not int or not 1 <= selected_device_limit <= MAX_DEVICE_LIMIT:
            raise DeviceSelectionError('invalid_device_limit')
        if reason is not None:
            raise DeviceSelectionError('device_selection_not_available')
        if not included <= selected_device_limit <= maximum:
            raise DeviceSelectionError('invalid_device_limit')
        effective = selected_device_limit

    return PurchaseDeviceSelection(
        effective_limit=effective,
        included_limit=included,
        selectable=reason is None,
        min_limit=included if reason is None else None,
        max_limit=maximum if reason is None else None,
        reason=reason,
    )
