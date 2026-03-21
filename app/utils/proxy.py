"""Proxy URL utilities for safe logging and error handling."""

import re
from urllib.parse import urlparse


def mask_proxy_url(proxy_url: str) -> str:
    """Mask credentials in a proxy URL for safe logging.

    Handles edge cases:
    - No credentials: returns URL as-is
    - Username + password: masks both with ***
    - Password-only: masks as well
    - No explicit port: omits :port part
    """
    parsed = urlparse(proxy_url)
    if not parsed.username and not parsed.password:
        return proxy_url
    host = parsed.hostname or 'unknown'
    port_part = f':{parsed.port}' if parsed.port else ''
    return f'{parsed.scheme}://***@{host}{port_part}'


_PROXY_CRED_RE = re.compile(r'(socks[45h]*://)([^@\s]+@)', re.IGNORECASE)


def sanitize_proxy_error(error: Exception) -> str:
    """Strip proxy credentials from exception messages.

    httpx/socksio may include the full proxy URL (with credentials)
    in connection error messages and tracebacks. This function removes
    credentials from the error string while preserving the original scheme.
    """
    msg = str(error)
    return _PROXY_CRED_RE.sub(r'\1***@', msg)
