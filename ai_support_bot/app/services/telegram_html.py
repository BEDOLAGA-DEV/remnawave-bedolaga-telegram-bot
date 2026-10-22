import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit


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


def _link_in_text(match: re.Match, inside_anchor: bool = False) -> str:
    original = match.group()
    url = original.rstrip('.,;!):]')
    if not _is_service_bot_url(url):
        return escape(original, quote=False)
    label = 'https://t.me/SligVPN_bot'
    link = label if inside_anchor else f'<a href="{escape(_bot_deep_link(url), quote=True)}">{label}</a>'
    return link + escape(original[len(url):], quote=False)


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
            href = _bot_deep_link(dict(attrs).get('href') or '')
            try:
                parsed = urlsplit(href)
                valid = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not any(ord(c) < 32 for c in href)
            except ValueError:
                valid = False
            if valid:
                self.parts.append(f'<a href="{escape(href, quote=True)}">')
                self.stack.append('a')
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
        for match in re.finditer(r'https://[^\s<>\"\']+', data, flags=re.IGNORECASE):
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
