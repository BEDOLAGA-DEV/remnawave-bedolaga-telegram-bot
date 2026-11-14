import os
import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ai_support_bot.app.services.emoji_style import CUSTOM_EMOJI_IDS


# Links the model may show to a customer. A domain also allows its subdomains (personal subscription links
# live on connect.sligvpn.ru, the cabinet on web.slig.app). Sources: support_policy.py, response_rules.py,
# navigation (web.slig.app, t.me/SligVPN_bot), service_identity.py (_SERVICE_HOSTS), Happ download page.
# AISUP_EXTRA_LINK_HOSTS (comma separated) adds hosts without a release, e.g. a new subscription domain.
_ALLOWED_LINK_DOMAINS = frozenset({
    'slig.app', 'sligvpn.ru', 'happ.su',
    'apps.apple.com', 'support.apple.com', 'play.google.com',
} | {host.strip().lower().strip('.') for host in os.getenv('AISUP_EXTRA_LINK_HOSTS', '').split(',') if host.strip()})
# Only our own bots and channels on t.me: any other handle can impersonate support or sell a foreign service.
_ALLOWED_TELEGRAM_HANDLES = frozenset({'sligvpn_bot', 'sligapp', 'sligvpn', 'sligvpnhelp'})
_REMOVED_LINK = '[ссылка удалена]'
# Telegram turns bare domains into links too, so they are checked like full URLs. Only real TLDs count:
# file names such as config.json or README.md must stay untouched.
_BARE_DOMAIN_TLDS = frozenset({
    'com', 'net', 'org', 'info', 'biz', 'ru', 'su', 'рф', 'рус', 'ua', 'by', 'kz', 'uz', 'am', 'ge', 'az', 'kg',
    'io', 'co', 'me', 'app', 'dev', 'online', 'site', 'xyz', 'top', 'pro', 'cc', 'tv', 'to', 'ws', 'gg', 'link',
    'click', 'live', 'shop', 'store', 'space', 'tech', 'website', 'fun', 'icu', 'vip', 'club', 'cloud', 'host',
    'best', 'world', 'today', 'lol', 'cyou', 'bond', 'sbs', 'cfd', 'win', 'work', 'life', 'digital', 'network',
    'center', 'news', 'agency', 'email', 'support', 'help', 'page', 'one', 'pw', 'tk', 'ml', 'ga', 'cf', 'gq',
    'us', 'uk', 'de', 'nl', 'fr', 'eu', 'fi', 'se', 'ch', 'at', 'pl', 'cz', 'ee', 'lv', 'lt', 'tr', 'cn', 'hk',
    'sg', 'jp', 'kr', 'in', 'ai', 'im', 'ly', 'gl', 'social', 'services', 'finance', 'money', 'pay',
})
_LINK_PATTERN = (
    r'https?://[^\s<>\"\']+'
    r'|(?<![\w@/.:?=&%\-])(?:www\.)?happ\.agency(?![\w.\-])(?:[/?#][^\s<>\"\']*)?'
    r'|(?<![\w@/.:?=&%\-])(?:[a-zа-яё0-9](?:[a-zа-яё0-9\-]{0,61}[a-zа-яё0-9])?\.)+([a-zа-яё]{2,24})'
    r'(?![\w@\-]|\.\w)(?::\d{1,5})?(?:[/?#][^\s<>\"\']*)?'
)


def _client_download_link(url: str) -> str:
    try:
        parsed = urlsplit(url if '://' in url else f'https://{url}')
        if (
            parsed.scheme.lower() in {'http', 'https'}
            and parsed.hostname in {'happ.agency', 'www.happ.agency'}
            and not parsed.username
        ):
            return 'https://happ.su/'
    except ValueError:
        pass
    return url


def _bot_deep_link(url: str) -> str:
    if re.fullmatch(r'https://t\.me/SligVPN_bot/?', url, flags=re.IGNORECASE):
        return 'https://t.me/SligVPN_bot?start=ai_support'
    return url


def _is_service_bot_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme.lower() == 'https'
            and parsed.netloc.lower() == 't.me'
            and parsed.path.lower() in {'/sligvpn_bot', '/sligvpn_bot/'}
        )
    except ValueError:
        return False


def is_allowed_link(url: str) -> bool:
    """True only for http(s) links to our hosts, our Telegram bots/channels and the client stores."""
    if '\\' in url or any(ord(char) < 33 for char in url):
        return False
    try:
        parsed = urlsplit(url if '://' in url else f'https://{url}')
        host = (parsed.hostname or '').lower().rstrip('.')
        if parsed.scheme.lower() not in {'http', 'https'} or not host or '@' in parsed.netloc:
            return False
        parsed.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return False
    if host == 't.me':
        handle = parsed.path.strip('/').split('/', 1)[0].casefold()
        return handle in _ALLOWED_TELEGRAM_HANDLES
    return any(host == domain or host.endswith('.' + domain) for domain in _ALLOWED_LINK_DOMAINS)


def _link_in_text(match: re.Match, inside_anchor: bool = False) -> str:
    original = match.group()
    if match.group(1) and match.group(1).casefold() not in _BARE_DOMAIN_TLDS:
        return escape(original, quote=False)  # a file name or abbreviation, not a link
    url = original.rstrip('.,;!):]')
    tail = escape(original[len(url):], quote=False)
    canonical = _client_download_link(url)
    if canonical != url:
        return canonical + tail
    if not is_allowed_link(url):
        return _REMOVED_LINK + tail
    if not _is_service_bot_url(url):
        return escape(original, quote=False)
    label = 'https://t.me/SligVPN_bot'
    link = label if inside_anchor else f'<a href="{escape(_bot_deep_link(url), quote=True)}">{label}</a>'
    return link + tail


class _TelegramHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = {'strong': 'b', 'em': 'i'}.get(tag, tag)
        if tag in {'b', 'i', 'code'}:
            self.parts.append(f'<{tag}>')
            self.stack.append(tag)
        elif tag == 'a':
            href = _bot_deep_link(_client_download_link(dict(attrs).get('href') or ''))
            try:
                parsed = urlsplit(href)
                valid = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not any(ord(c) < 32 for c in href)
            except ValueError:
                valid = False
            # A foreign target is dropped and only the label stays; a label that is itself
            # a foreign address is masked in handle_data.
            if valid and is_allowed_link(href):
                self.parts.append(f'<a href="{escape(href, quote=True)}">')
                self.stack.append('a')
        elif tag == 'tg-emoji':
            emoji_id = dict(attrs).get('emoji-id') or ''
            if emoji_id in CUSTOM_EMOJI_IDS:
                self.parts.append(f'<tg-emoji emoji-id="{emoji_id}">')
                self.stack.append(tag)
        elif tag == 'br':
            self.parts.append('\n')

    def handle_endtag(self, tag):
        tag = {'strong': 'b', 'em': 'i'}.get(tag, tag)
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.parts.append(f'</{current}>')
                if current == tag:
                    break

    def handle_data(self, data):
        offset = 0
        for match in re.finditer(_LINK_PATTERN, data, flags=re.IGNORECASE):
            self.parts.append(escape(data[offset:match.start()], quote=False))
            self.parts.append(_link_in_text(match, inside_anchor='a' in self.stack or 'code' in self.stack))
            offset = match.end()
        self.parts.append(escape(data[offset:], quote=False))


def sanitize_telegram_html(text: str) -> str:
    text = re.sub(r'<code>\s*(https://[^\s<>]+)\s*</code>', r'\1', text, flags=re.IGNORECASE)
    parser = _TelegramHTML()
    parser.feed(text)
    parser.close()
    parser.parts.extend(f'</{tag}>' for tag in reversed(parser.stack))
    rendered = ''.join(parser.parts).strip()
    return re.sub(r'<code>\s*(https://[^\s<>]+)\s*</code>', r'\1', rendered, flags=re.IGNORECASE)
