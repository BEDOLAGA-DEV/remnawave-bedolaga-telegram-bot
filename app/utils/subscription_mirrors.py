"""Trusted alternate origins for the same subscription; never persist bearer URLs."""

import hashlib
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit


MAX_MIRRORS = 100
MAX_LABEL_LENGTH = 64
SUPPORTED_MIRROR_MODES = frozenset({'guide', 'link', 'miniapp_subscription'})


def _origin(value: str, *, https_only: bool = True) -> str:
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value:
        raise ValueError('Invalid address characters')
    parsed = urlsplit(value if '://' in value else f'https://{value}')
    if parsed.scheme not in ({'https'} if https_only else {'http', 'https'}):
        raise ValueError('HTTPS required')
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError('Host without credentials required')
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment or '?' in value or '#' in value:
        raise ValueError('Use an origin without path, query or fragment')
    host = parsed.hostname.encode('idna').decode('ascii').lower()
    if ':' in host:
        host = f'[{ipaddress.IPv6Address(host)}]'
    elif len(host) > 253 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in host.split('.')
    ):
        raise ValueError('Invalid hostname')
    port = parsed.port
    if port == 0 or parsed.netloc.endswith(':'):
        raise ValueError('Invalid port')
    suffix = f':{port}' if port and port != (443 if parsed.scheme == 'https' else 80) else ''
    return f'{parsed.scheme}://{host}{suffix}'


def parse_mirror_origins(value: str, *, strict: bool = False) -> list[tuple[str, str]]:
    """Validate writes strictly; tolerate bad legacy/env entries without breaking menus."""
    if not value:
        return []
    if not isinstance(value, str) or len(value) > 65536:
        if strict:
            raise ValueError('Mirror configuration is too large')
        return []
    mirrors: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(re.split(r';|\r?\n', value), start=1):
        if not entry.strip():
            continue
        label, separator, target = entry.partition('|')
        label = label.strip()
        try:
            if not separator or not label or len(label) > MAX_LABEL_LENGTH:
                raise ValueError('A label of 1–64 characters is required')
            if any(ord(c) < 32 or ord(c) == 127 for c in label):
                raise ValueError('Invalid label characters')
            # Spaces around entries are harmless, control characters inside a URL are not.
            origin = _origin(target.strip(' '))
            if origin in seen:
                continue
            if len(mirrors) >= MAX_MIRRORS:
                raise ValueError('At most 100 mirror origins are supported')
        except (ValueError, UnicodeError):
            if strict:
                # Do not include credentials, bearer tokens or the raw setting in errors.
                raise ValueError(
                    f'Invalid mirror entry {index}: use a label and an HTTPS origin without credentials or path'
                ) from None
            continue
        seen.add(origin)
        mirrors.append((label, origin))
    return mirrors


def build_subscription_mirror_links(subscription_url: str | None) -> list[tuple[str, str]]:
    from app.config import settings

    if not subscription_url:
        return []
    try:
        source = urlsplit(subscription_url)
        canonical = _origin(urlunsplit((source.scheme, source.netloc, '', '', '')), https_only=False)
        if any(c.isspace() or ord(c) < 32 for c in subscription_url) or '\\' in subscription_url:
            return []
    except (ValueError, UnicodeError):
        return []
    links = []
    for label, origin in parse_mirror_origins(settings.SUBSCRIPTION_MIRROR_LINKS):
        if origin == canonical:
            continue
        mirror = urlsplit(origin)
        links.append((label, urlunsplit(source._replace(scheme=mirror.scheme, netloc=mirror.netloc))))
    return links


def get_subscription_mirrors(subscription_url: str | None, *, respect_visibility: bool = True) -> list[dict[str, str]]:
    from app.config import settings

    if settings.CONNECT_BUTTON_MODE not in SUPPORTED_MIRROR_MODES:
        return []
    if respect_visibility and settings.should_hide_subscription_link():
        return []
    result = []
    for label, url in build_subscription_mirror_links(subscription_url):
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
        result.append({'id': hashlib.sha256(origin.encode()).hexdigest()[:16], 'label': label, 'url': url})
    return result
