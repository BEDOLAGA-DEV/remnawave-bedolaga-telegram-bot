"""Cabinet authentication module."""

from .jwt_handler import (
    create_access_token,
    create_auto_login_token,
    create_refresh_token,
    decode_token,
    get_token_payload,
)
from .password_utils import hash_password, verify_password
from .telegram_auth import (
    exchange_authorization_code,
    extract_telegram_user_from_init_data,
    generate_oidc_nonce,
    generate_pkce_pair,
    validate_telegram_init_data,
    validate_telegram_login_widget,
    validate_telegram_oidc_token,
)
from .telegram_link import create_link_token, decode_link_token


__all__ = [
    'create_access_token',
    'create_auto_login_token',
    'create_refresh_token',
    'decode_token',
    'exchange_authorization_code',
    'generate_oidc_nonce',
    'generate_pkce_pair',
    'get_token_payload',
    'hash_password',
    'validate_telegram_init_data',
    'validate_telegram_login_widget',
    'validate_telegram_oidc_token',
    'create_link_token',
    'decode_link_token',
    'verify_password',
]
