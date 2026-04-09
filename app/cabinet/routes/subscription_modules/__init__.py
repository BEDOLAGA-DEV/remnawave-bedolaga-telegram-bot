"""Subscription sub-modules for cabinet API.

Each module contains a subset of endpoints from the original monolithic subscription.py.
The main subscription.py includes all sub-routers for backward compatibility.
"""

from .autopay import router as autopay_router
from .daily import router as daily_router
from .devices import router as devices_router
from .multi_tariff import router as multi_tariff_router
from .purchase import router as purchase_router
from .renewal import router as renewal_router
from .servers import router as servers_router
from .status import router as status_router
from .tariff_switch import router as tariff_switch_router
from .traffic import router as traffic_router


__all__ = [
    'autopay_router',
    'daily_router',
    'devices_router',
    'multi_tariff_router',
    'purchase_router',
    'renewal_router',
    'servers_router',
    'status_router',
    'tariff_switch_router',
    'traffic_router',
]
