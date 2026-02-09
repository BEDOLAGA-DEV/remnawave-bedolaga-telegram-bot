"""Cabinet API routes."""

from fastapi import APIRouter

from .admin_apps import router as admin_apps_router
from .admin_ban_system import router as admin_ban_system_router
from .admin_broadcasts import router as admin_broadcasts_router
from .admin_campaigns import router as admin_campaigns_router
from .admin_email_templates import router as admin_email_templates_router
from .admin_payment_methods import router as admin_payment_methods_router
from .admin_payments import router as admin_payments_router
from .admin_promo_offers import router as admin_promo_offers_router
from .admin_promocodes import promo_groups_router as admin_promo_groups_router, router as admin_promocodes_router
from .admin_remnawave import router as admin_remnawave_router
from .admin_servers import router as admin_servers_router
from .admin_settings import router as admin_settings_router
from .admin_stats import router as admin_stats_router
from .admin_tariffs import router as admin_tariffs_router
from .admin_tickets import router as admin_tickets_router
from .admin_traffic import router as admin_traffic_router
from .admin_updates import router as admin_updates_router
from .admin_users import router as admin_users_router
from .admin_wheel import router as admin_wheel_router
from .auth import router as auth_router
from .balance import router as balance_router
from .branding import router as branding_router
from .contests import router as contests_router
from .info import router as info_router
from .media import router as media_router
from .notifications import router as notifications_router
from .oauth import router as oauth_router
from .polls import router as polls_router
from .promo import router as promo_router
from .promocode import router as promocode_router
from .referral import router as referral_router
from .subscription import router as subscription_router
from .ticket_notifications import (
    admin_router as admin_ticket_notifications_router,
    router as ticket_notifications_router,
)
from .tickets import router as tickets_router
from .websocket import router as websocket_router
from .wheel import router as wheel_router


# Main cabinet router
router = APIRouter(prefix='/cabinet', tags=['Cabinet'])

# Include all sub-routers
router.include_router(auth_router)
router.include_router(oauth_router)
router.include_router(subscription_router)
router.include_router(balance_router)
router.include_router(referral_router)
# Notifications router MUST be before tickets router to avoid route conflict
router.include_router(ticket_notifications_router)
router.include_router(tickets_router)
router.include_router(promocode_router)
router.include_router(contests_router)
router.include_router(polls_router)
router.include_router(promo_router)
router.include_router(notifications_router)
router.include_router(info_router)
router.include_router(branding_router)
router.include_router(media_router)

# Wheel routes
router.include_router(wheel_router)

# Admin routes (notifications router MUST be before tickets router to avoid route conflict)
router.include_router(admin_ticket_notifications_router)
router.include_router(admin_tickets_router)
router.include_router(admin_settings_router)
router.include_router(admin_apps_router)
router.include_router(admin_wheel_router)
router.include_router(admin_tariffs_router)
router.include_router(admin_servers_router)
router.include_router(admin_stats_router)
router.include_router(admin_ban_system_router)
router.include_router(admin_broadcasts_router)
router.include_router(admin_promocodes_router)
router.include_router(admin_promo_groups_router)
router.include_router(admin_campaigns_router)
router.include_router(admin_users_router)
router.include_router(admin_payment_methods_router)
router.include_router(admin_payments_router)
router.include_router(admin_promo_offers_router)
router.include_router(admin_remnawave_router)
router.include_router(admin_email_templates_router)
router.include_router(admin_updates_router)
router.include_router(admin_traffic_router)

# WebSocket route
router.include_router(websocket_router)

__all__ = ['router']
