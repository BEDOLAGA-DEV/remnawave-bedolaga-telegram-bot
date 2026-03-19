"""Subscription management routes for cabinet.

This module is a thin aggregator that includes all subscription sub-routers
from subscription_modules/. Each domain (status, purchase, traffic, devices, etc.)
lives in its own module for maintainability.

The router exported here preserves the original prefix='/subscription' and tags,
so all existing API paths remain unchanged.
"""

from fastapi import APIRouter

from .subscription_modules import (
    autopay_router,
    daily_router,
    devices_router,
    purchase_router,
    renewal_router,
    servers_router,
    status_router,
    tariff_switch_router,
    traffic_router,
)


router = APIRouter(prefix='/subscription', tags=['Cabinet Subscription'], redirect_slashes=False)

# Include all sub-routers (no prefix — each module defines paths relative to /subscription)
router.include_router(status_router)
router.include_router(renewal_router)
router.include_router(purchase_router)
router.include_router(traffic_router)
router.include_router(devices_router)
router.include_router(servers_router)
router.include_router(autopay_router)
router.include_router(daily_router)
router.include_router(tariff_switch_router)
