"""Cabinet Pydantic schemas."""

from .auth import (
    TelegramAuthRequest,
    TelegramWidgetAuthRequest,
    EmailRegisterRequest,
    EmailVerifyRequest,
    EmailLoginRequest,
    RefreshTokenRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    TokenResponse,
    UserResponse,
    AuthResponse,
)
from .email_resend import EmailResendRequest
from .subscription import (
    SubscriptionResponse,
    TrafficPurchaseInfo,
    RenewalOptionResponse,
    RenewalRequest,
    TrafficPackageResponse,
    TrafficPurchaseRequest,
    DevicePurchaseRequest,
    AutopayUpdateRequest,
)
from .balance import (
    BalanceResponse,
    TransactionResponse,
    TransactionListResponse,
    PaymentMethodResponse,
    TopUpRequest,
    TopUpResponse,
)
from .referral import (
    ReferralInfoResponse,
    ReferralListResponse,
    ReferralEarningResponse,
    ReferralTermsResponse,
)
from .tickets import (
    TicketResponse,
    TicketListResponse,
    TicketMessageResponse,
    TicketCreateRequest,
    TicketMessageCreateRequest,
)

__all__ = [
    # Auth
    "TelegramAuthRequest",
    "TelegramWidgetAuthRequest",
    "EmailRegisterRequest",
    "EmailVerifyRequest",
    "EmailLoginRequest",
    "RefreshTokenRequest",
    "PasswordForgotRequest",
    "PasswordResetRequest",
    "TokenResponse",
    "UserResponse",
    "AuthResponse",
    # Subscription
    "SubscriptionResponse",
    "TrafficPurchaseInfo",
    "RenewalOptionResponse",
    "RenewalRequest",
    "TrafficPackageResponse",
    "TrafficPurchaseRequest",
    "DevicePurchaseRequest",
    "AutopayUpdateRequest",
    # Balance
    "BalanceResponse",
    "TransactionResponse",
    "TransactionListResponse",
    "PaymentMethodResponse",
    "TopUpRequest",
    "TopUpResponse",
    # Referral
    "ReferralInfoResponse",
    "ReferralListResponse",
    "ReferralEarningResponse",
    "ReferralTermsResponse",
    # Tickets
    "TicketResponse",
    "TicketListResponse",
    "TicketMessageResponse",
    "TicketCreateRequest",
    "TicketMessageCreateRequest",
]
