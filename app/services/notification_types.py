"""Виды уведомлений — отдельным модулем, без зависимостей.

Раньше перечисление жило в ``notification_delivery_service``, а тот тянет
шаблоны писем, которые сами обращаются к перечислению. Кольцо обходили импортом
внутри функции — так и жила скрытая связь, которую видел только CodeQL
(py/cyclic-import). Здесь у модуля нет ни одного импорта из проекта, поэтому
кольцу неоткуда взяться.
"""

from enum import Enum


class NotificationType(Enum):
    """Types of notifications that can be sent to users."""

    # Balance notifications
    BALANCE_TOPUP = 'balance_topup'
    BALANCE_CHANGE = 'balance_change'
    BALANCE_LOW = 'balance_low'

    # Subscription notifications
    SUBSCRIPTION_ACTIVATED = 'subscription_activated'
    SUBSCRIPTION_EXPIRING = 'subscription_expiring'
    SUBSCRIPTION_EXPIRED = 'subscription_expired'
    SUBSCRIPTION_RENEWED = 'subscription_renewed'
    WINBACK_EXPIRED_1D = 'winback_expired_1d'
    WINBACK_DISCOUNT = 'winback_discount'
    WINBACK_TRIAL_ENDING = 'winback_trial_ending'

    # Autopay notifications
    AUTOPAY_SUCCESS = 'autopay_success'
    AUTOPAY_FAILED = 'autopay_failed'
    AUTOPAY_INSUFFICIENT_FUNDS = 'autopay_insufficient_funds'

    # Daily subscription notifications
    DAILY_DEBIT = 'daily_debit'
    DAILY_INSUFFICIENT_FUNDS = 'daily_insufficient_funds'
    TRAFFIC_RESET = 'traffic_reset'

    # Account notifications
    BAN_NOTIFICATION = 'ban_notification'
    UNBAN_NOTIFICATION = 'unban_notification'
    WARNING_NOTIFICATION = 'warning_notification'

    # Referral notifications
    REFERRAL_BONUS = 'referral_bonus'
    REFERRAL_REGISTERED = 'referral_registered'
    REFERRAL_WELCOME = 'referral_welcome'

    # Partner notifications
    PARTNER_APPLICATION_APPROVED = 'partner_application_approved'
    PARTNER_APPLICATION_REJECTED = 'partner_application_rejected'

    # Withdrawal notifications
    WITHDRAWAL_APPROVED = 'withdrawal_approved'
    WITHDRAWAL_REJECTED = 'withdrawal_rejected'

    # Auth emails
    EMAIL_VERIFICATION = 'email_verification'
    PASSWORD_RESET = 'password_reset'
    EMAIL_CHANGE_CODE = 'email_change_code'

    # Webhook subscription events
    WEBHOOK_SUB_EXPIRED = 'webhook_sub_expired'
    WEBHOOK_SUB_DISABLED = 'webhook_sub_disabled'
    WEBHOOK_SUB_ENABLED = 'webhook_sub_enabled'
    WEBHOOK_SUB_LIMITED = 'webhook_sub_limited'
    WEBHOOK_SUB_TRAFFIC_RESET = 'webhook_sub_traffic_reset'
    WEBHOOK_SUB_DELETED = 'webhook_sub_deleted'
    WEBHOOK_SUB_REVOKED = 'webhook_sub_revoked'
    WEBHOOK_SUB_EXPIRING = 'webhook_sub_expiring'
    WEBHOOK_SUB_FIRST_CONNECTED = 'webhook_sub_first_connected'
    WEBHOOK_SUB_BANDWIDTH_THRESHOLD = 'webhook_sub_bandwidth_threshold'
    WEBHOOK_USER_NOT_CONNECTED = 'webhook_user_not_connected'
    WEBHOOK_DEVICE_ADDED = 'webhook_device_added'
    WEBHOOK_DEVICE_DELETED = 'webhook_device_deleted'
    WEBHOOK_TORRENT_DETECTED = 'webhook_torrent_detected'

    # Support tickets
    TICKET_REPLY = 'ticket_reply'

    # Other
    BROADCAST = 'broadcast'
    PAYMENT_RECEIVED = 'payment_received'
    NALOGO_RECEIPT = 'nalogo_receipt'
    PROMO_OFFER = 'promo_offer'

    # Guest purchase notifications
    GUEST_SUBSCRIPTION_DELIVERED = 'guest_subscription_delivered'
    GUEST_ACTIVATION_REQUIRED = 'guest_activation_required'
    GUEST_GIFT_RECEIVED = 'guest_gift_received'
    GUEST_CABINET_CREDENTIALS = 'guest_cabinet_credentials'
    GUEST_GIFT_LINK_BUYER = 'guest_gift_link_buyer'


# Письма, которые почтовые провайдеры считают массовой рассылкой: только они
# получают List-Unsubscribe и уважают отписку. Уведомления по действующей
# подписке (оплата, истечение, блокировка) сюда не входят — это транзакционная
# переписка, отписывать от неё нельзя.
MARKETING_NOTIFICATION_TYPES = frozenset(
    {
        NotificationType.PROMO_OFFER,
        NotificationType.WINBACK_EXPIRED_1D,
        NotificationType.WINBACK_DISCOUNT,
        NotificationType.WINBACK_TRIAL_ENDING,
    }
)
