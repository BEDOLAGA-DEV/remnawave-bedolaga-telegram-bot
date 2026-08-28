"""Canonical utilities for building Telegram and Cabinet gift claim and share links.

Security constraints:
- Telegram start parameters are limited to 64 characters (alphanumeric, underscore, hyphen).
- A gift start parameter must use the 'GIFT_' prefix (5 chars) and retain at least
  GIFT_TOKEN_MIN_PREFIX_LENGTH (48 chars) of entropy from the 64-char purchase token.
- Standalone gift tokens must never be placed directly in share text, logs, or user copy.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass


TELEGRAM_START_PARAM_MAX_LENGTH: int = 64
TELEGRAM_GIFT_START_PREFIX: str = 'GIFT_'
GIFT_TOKEN_MIN_PREFIX_LENGTH: int = 48
GIFT_TOKEN_BOT_PREFIX_LENGTH: int = TELEGRAM_START_PARAM_MAX_LENGTH - len(TELEGRAM_GIFT_START_PREFIX)  # 59
LEGACY_GIFT_CODE_MIN_LENGTH: int = 8

_TOKEN_RE = re.compile(r'^[a-zA-Z0-9_-]+$')
_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]+$')


class GiftLinkError(Exception):
    """Base exception for gift link formatting and validation errors."""


class InvalidGiftTokenError(GiftLinkError, ValueError):
    """Raised when a gift token is malformed, too short, or contains invalid characters."""


class InvalidBotUsernameError(GiftLinkError, ValueError):
    """Raised when a bot username is invalid or malformed."""


class MissingBotUsernameError(InvalidBotUsernameError):
    """Raised when a bot username is empty or missing."""


class InvalidCabinetUrlError(GiftLinkError, ValueError):
    """Raised when a cabinet base URL is invalid or malformed."""


class MissingCabinetUrlError(InvalidCabinetUrlError):
    """Raised when a cabinet base URL is empty or missing."""


class InvalidClaimLinkError(GiftLinkError, ValueError):
    """Raised when a claim link for sharing is invalid, empty, or malformed."""


class InvalidShareTextError(GiftLinkError, ValueError):
    """Raised when share text is invalid, empty, or malformed."""


@dataclass(frozen=True, slots=True)
class GiftClaimArtifacts:
    """Immutable bundle of canonical public gift code and channel claim URLs."""

    public_code: str
    bot_claim_url: str | None = None
    cabinet_claim_url: str | None = None
    telegram_share_url: str | None = None


def _validate_gift_token(token: str) -> str:
    """Validate that the token is a valid, secure URL-safe token."""
    if not isinstance(token, str) or not token:
        raise InvalidGiftTokenError('Gift token must be a non-empty string')

    if not _TOKEN_RE.match(token):
        raise InvalidGiftTokenError(f'Gift token contains invalid characters (length={len(token)})')

    if len(token) < GIFT_TOKEN_MIN_PREFIX_LENGTH:
        raise InvalidGiftTokenError(
            f'Gift token length ({len(token)}) is below security threshold of {GIFT_TOKEN_MIN_PREFIX_LENGTH}'
        )

    return token


def _normalize_bot_username(bot_username: str) -> str:
    """Normalize and validate a Telegram bot username."""
    if not isinstance(bot_username, str):
        raise MissingBotUsernameError('Bot username must be a string')

    cleaned = bot_username.strip()
    if not cleaned or cleaned == '@':
        raise MissingBotUsernameError('Bot username cannot be empty')

    cleaned = cleaned.lstrip('@')
    if not cleaned:
        raise MissingBotUsernameError('Bot username cannot be empty')

    if not _USERNAME_RE.match(cleaned):
        raise InvalidBotUsernameError(f'Invalid bot username: {bot_username!r}')

    return cleaned


def _normalize_cabinet_url(cabinet_url: str) -> str:
    """Normalize and validate a Cabinet base URL."""
    if not isinstance(cabinet_url, str):
        raise MissingCabinetUrlError('Cabinet URL must be a string')

    cleaned = cabinet_url.strip()
    if not cleaned:
        raise MissingCabinetUrlError('Cabinet URL cannot be empty')

    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise InvalidCabinetUrlError(f'Invalid cabinet URL: {cabinet_url!r}')

    return cleaned.rstrip('/')


def build_gift_public_code(token: str) -> str:
    """Build the canonical public gift code (``GIFT_<59_chars>``).

    The public code is source-neutral and identical for bot- and cabinet-origin
    purchases. It prefixes the first 59 characters of the validated token with
    ``GIFT_``, matching Telegram's 64-character start_param limit.

    Args:
        token: Full 64-character (or minimum 48-character) purchase token.

    Returns:
        Canonical public gift code, e.g. ``GIFT_<59_chars>``.

    Raises:
        InvalidGiftTokenError: If token is malformed, too short, or non-URL-safe.
    """
    valid_token = _validate_gift_token(token)
    token_prefix = valid_token[:GIFT_TOKEN_BOT_PREFIX_LENGTH]
    return f'{TELEGRAM_GIFT_START_PREFIX}{token_prefix}'


def parse_gift_claim_input(value: str, allow_legacy_short: bool = False) -> str:
    """Parse and normalize gift claim credentials from various user inputs.

    Supported input formats:
    - Canonical public code: ``GIFT_<prefix>``
    - Legacy dash code: ``GIFT-<prefix>``
    - Legacy alias: ``giftclaim_<prefix>`` / ``giftclaim-<prefix>``
    - Telegram deep link: ``https://t.me/<bot>?start=GIFT_<prefix>``, ``tg://...``, ``t.me/...``
    - Cabinet claim URL: ``https://<cabinet>/buy/gift/<token>``
    - Raw full token or valid token fragment

    Args:
        value: Raw user input string from message text, URL, or API body.
        allow_legacy_short: If True, allows legacy short codes (>= 8 chars) for
            backward-compatible cabinet activation. If False (default), strictly
            enforces GIFT_TOKEN_MIN_PREFIX_LENGTH (48 chars).

    Returns:
        Normalized token or token prefix string.

    Raises:
        InvalidGiftTokenError: If input is empty, malformed, contains invalid
            characters, or fails the length security threshold.
    """
    if not isinstance(value, str):
        raise InvalidGiftTokenError('Gift claim input must be a string')

    cleaned = value.strip()
    if not cleaned:
        raise InvalidGiftTokenError('Gift claim input cannot be empty')

    # Handle URLs (Telegram deep links or Cabinet claim URLs)
    url_candidate = cleaned
    if url_candidate.startswith(('t.me/', 'www.t.me/')):
        url_candidate = f'https://{url_candidate}'

    extracted = cleaned
    if url_candidate.startswith(('http://', 'https://', 'tg://')) or '/buy/gift/' in url_candidate:
        try:
            parsed = urllib.parse.urlparse(url_candidate)
        except Exception:
            raise InvalidGiftTokenError('Malformed URL in gift claim input') from None

        qs = urllib.parse.parse_qs(parsed.query)
        start_params = qs.get('start')
        if start_params:
            start_val = start_params[0].strip()
            if not start_val:
                raise InvalidGiftTokenError('Gift claim URL has empty start parameter')
            upper_val = start_val.upper()
            lower_val = start_val.lower()
            if (
                upper_val.startswith(('GIFT_', 'GIFT-'))
                or lower_val.startswith(('giftclaim_', 'giftclaim-'))
                or (len(start_val) == 64 and _TOKEN_RE.match(start_val))
            ):
                extracted = start_val
            else:
                raise InvalidGiftTokenError('Telegram deep link does not contain a gift start parameter')
        elif '/buy/gift/' in parsed.path:
            parts = parsed.path.rstrip('/').split('/buy/gift/')
            if len(parts) > 1 and parts[-1].strip():
                extracted = parts[-1].strip().split('/')[0].strip()
                if not extracted:
                    raise InvalidGiftTokenError('Gift claim URL has empty token in path')
            else:
                raise InvalidGiftTokenError('Malformed cabinet gift claim URL')
        elif parsed.scheme in ('http', 'https', 'tg') or parsed.netloc:
            raise InvalidGiftTokenError('URL does not contain a gift claim parameter or path')

    # Normalize known gift prefixes (case-insensitive)
    token = extracted
    upper = extracted.upper()
    lower = extracted.lower()
    if upper.startswith(('GIFT_', 'GIFT-')):
        token = extracted[5:]
    elif lower.startswith(('giftclaim_', 'giftclaim-')):
        token = extracted[10:]

    if not token or not _TOKEN_RE.match(token):
        raise InvalidGiftTokenError(f'Gift claim input contains invalid characters (length={len(token)})')

    min_length = LEGACY_GIFT_CODE_MIN_LENGTH if allow_legacy_short else GIFT_TOKEN_MIN_PREFIX_LENGTH
    if len(token) < min_length:
        raise InvalidGiftTokenError(
            f'Gift claim input length ({len(token)}) is below security threshold of {min_length}'
        )

    return token


def build_gift_claim_artifacts(
    token: str,
    bot_username: str | None = None,
    cabinet_url: str | None = None,
    share_text: str | None = None,
) -> GiftClaimArtifacts:
    """Build immutable gift claim artifacts with public code and available channel URLs.

    Public code is always derived from the token. Optional channels that cannot be
    constructed produce None without suppressing the canonical code.

    Args:
        token: Full 64-character (or minimum 48-character) purchase token.
        bot_username: Optional Telegram bot username.
        cabinet_url: Optional web cabinet base URL.
        share_text: Optional localized text for Telegram chat picker.

    Returns:
        GiftClaimArtifacts containing canonical public_code and optional valid URLs.

    Raises:
        InvalidGiftTokenError: If token is malformed, too short, or non-URL-safe.
    """
    public_code = build_gift_public_code(token)

    bot_claim_url: str | None = None
    if bot_username:
        try:
            bot_claim_url = build_bot_gift_claim_link(token, bot_username)
        except GiftLinkError:
            bot_claim_url = None

    cabinet_claim_url: str | None = None
    if cabinet_url:
        try:
            cabinet_claim_url = build_cabinet_gift_claim_link(token, cabinet_url)
        except GiftLinkError:
            cabinet_claim_url = None

    telegram_share_url: str | None = None
    if share_text:
        primary_claim_url = bot_claim_url or cabinet_claim_url
        if primary_claim_url:
            try:
                telegram_share_url = build_telegram_gift_share_url(primary_claim_url, share_text)
            except GiftLinkError:
                telegram_share_url = None

    return GiftClaimArtifacts(
        public_code=public_code,
        bot_claim_url=bot_claim_url,
        cabinet_claim_url=cabinet_claim_url,
        telegram_share_url=telegram_share_url,
    )


def build_bot_gift_claim_link(token: str, bot_username: str) -> str:
    """Build a canonical Telegram deep-link for claiming a gift subscription.

    The start parameter is formatted as ``GIFT_<token_prefix>``, truncated to
    fit exactly within Telegram's 64-character start_param limit while retaining
    59 characters of entropy (exceeding the 48-character security threshold).

    Args:
        token: Full 64-character (or minimum 48-character) URL-safe purchase token.
        bot_username: Telegram bot username (with or without leading '@').

    Returns:
        Canonical claim link, e.g. ``https://t.me/my_bot?start=GIFT_<59_chars>``.

    Raises:
        InvalidGiftTokenError: If token is malformed, too short, or non-URL-safe.
        MissingBotUsernameError: If bot username is missing or empty.
        InvalidBotUsernameError: If bot username contains invalid characters.
    """
    clean_username = _normalize_bot_username(bot_username)
    public_code = build_gift_public_code(token)
    return f'https://t.me/{clean_username}?start={public_code}'


def build_cabinet_gift_claim_link(token: str, cabinet_url: str) -> str:
    """Build a canonical web cabinet claim link containing the full bearer token.

    Args:
        token: Full 64-character purchase token.
        cabinet_url: Cabinet base URL (e.g. ``https://cabinet.example.com``).

    Returns:
        Canonical web claim URL, e.g. ``https://cabinet.example.com/buy/gift/<token>``.

    Raises:
        InvalidGiftTokenError: If token is malformed, too short, or non-URL-safe.
        MissingCabinetUrlError: If cabinet URL is missing or empty.
        InvalidCabinetUrlError: If cabinet URL has an invalid scheme or format.
    """
    valid_token = _validate_gift_token(token)
    clean_cabinet_base = _normalize_cabinet_url(cabinet_url)

    return f'{clean_cabinet_base}/buy/gift/{valid_token}'


def build_telegram_gift_share_url(claim_link: str, localized_share_text: str) -> str:
    """Build a native Telegram share URL (``https://t.me/share/url``) with prefilled text.

    Args:
        claim_link: The canonical bot or cabinet gift claim link.
        localized_share_text: Localized greeting and instructions to prefill in the chat picker.

    Returns:
        Canonical share URL, e.g. ``https://t.me/share/url?url=...&text=...``.

    Raises:
        InvalidClaimLinkError: If claim_link is empty or not a valid URL.
        InvalidShareTextError: If localized_share_text is not a string or is empty.
    """
    if not isinstance(claim_link, str):
        raise InvalidClaimLinkError('Claim link must be a string')

    cleaned_link = claim_link.strip()
    if not cleaned_link:
        raise InvalidClaimLinkError('Claim link cannot be empty')

    parsed = urllib.parse.urlparse(cleaned_link)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise InvalidClaimLinkError('Claim link must have a valid http or https scheme and netloc')

    if not isinstance(localized_share_text, str):
        raise InvalidShareTextError('Share text must be a string')

    cleaned_text = localized_share_text.strip()
    if not cleaned_text:
        raise InvalidShareTextError('Share text cannot be empty')

    query = urllib.parse.urlencode(
        {
            'url': cleaned_link,
            'text': localized_share_text,
        }
    )
    return f'https://t.me/share/url?{query}'
