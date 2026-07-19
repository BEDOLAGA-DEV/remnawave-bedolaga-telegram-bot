import html as html_module
import json
import re
from html.parser import HTMLParser


TELEGRAM_ALLOWED_TAGS = frozenset({'b', 'i', 'u', 's', 'a', 'code', 'pre', 'blockquote'})

_TAG_ALIASES = {'strong': 'b', 'em': 'i', 'ins': 'u', 'strike': 's', 'del': 's'}

_HEADING_TAGS = frozenset({'h1', 'h2', 'h3', 'h4', 'h5', 'h6'})

_BLOCK_TAGS = frozenset({'p', 'div', 'section', 'article', 'table', 'tr', 'figure', 'figcaption', 'hr'})

_SKIP_CONTENT_TAGS = frozenset({'script', 'style', 'iframe', 'video', 'audio', 'svg', 'head'})

_SAFE_HREF_RE = re.compile(r'^https?://', re.IGNORECASE)

_TAG_RE = re.compile(r'<(/?)(\w[\w-]*)(?:\s+[^>]*)?>')

_INCOMPLETE_ENTITY_RE = re.compile(r'&[#\w]{0,9}$')

_MAX_HREF_LENGTH = 1024

_TELEGRAM_HARD_LIMIT = 4096


class _TelegramHtmlRenderer(HTMLParser):
    CDATA_CONTENT_ELEMENTS = ()

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._open_tags: list[str] = []
        self._skip_stack: list[str] = []
        self._list_stack: list[int | None] = []

    def _append(self, text: str) -> None:
        if not self._skip_stack:
            self._parts.append(text)

    def _close_until(self, tag: str) -> None:
        if tag not in self._open_tags:
            return
        while self._open_tags:
            open_tag = self._open_tags.pop()
            self._parts.append(f'</{open_tag}>')
            if open_tag == tag:
                break

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_stack.append(tag)
            return
        if self._skip_stack:
            return
        tag = _TAG_ALIASES.get(tag, tag)
        if tag == 'br':
            self._append('\n')
            return
        if tag == 'ul':
            self._list_stack.append(None)
            return
        if tag == 'ol':
            self._list_stack.append(0)
            return
        if tag == 'li':
            if self._list_stack and self._list_stack[-1] is not None:
                self._list_stack[-1] += 1
                self._append(f'\n{self._list_stack[-1]}. ')
            else:
                self._append('\n• ')
            return
        if tag in _HEADING_TAGS:
            self._append('\n\n')
            self._parts.append('<b>')
            self._open_tags.append('b')
            return
        if tag in _BLOCK_TAGS:
            self._append('\n\n')
            return
        if tag == 'a':
            href = next((value for name, value in attrs if name == 'href'), None)
            if href and _SAFE_HREF_RE.match(href.strip()):
                escaped = html_module.escape(href.strip(), quote=True)
                if len(escaped) <= _MAX_HREF_LENGTH:
                    self._parts.append(f'<a href="{escaped}">')
                    self._open_tags.append('a')
            return
        if tag in TELEGRAM_ALLOWED_TAGS:
            self._parts.append(f'<{tag}>')
            self._open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_stack:
            if tag in self._skip_stack:
                while self._skip_stack and self._skip_stack.pop() != tag:
                    pass
            return
        if tag in {'ul', 'ol'}:
            if self._list_stack:
                self._list_stack.pop()
            self._append('\n')
            return
        if tag in _HEADING_TAGS:
            self._close_until('b')
            self._append('\n\n')
            return
        tag = _TAG_ALIASES.get(tag, tag)
        if tag in _BLOCK_TAGS:
            self._append('\n\n')
            return
        if tag in TELEGRAM_ALLOWED_TAGS:
            self._close_until(tag)

    def handle_data(self, data: str) -> None:
        self._append(html_module.escape(data))

    def result(self) -> str:
        while self._open_tags:
            self._parts.append(f'</{self._open_tags.pop()}>')
        text = ''.join(self._parts)
        text = re.sub(r'[ \t]+\n', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()


def html_to_telegram(raw_html: str | None) -> str:
    if not raw_html:
        return ''
    renderer = _TelegramHtmlRenderer()
    renderer.feed(raw_html)
    renderer.close()
    return renderer.result()


def info_page_faq_to_telegram(raw_content: str | None) -> str:
    if not raw_content:
        return ''
    try:
        items = json.loads(raw_content)
    except (TypeError, ValueError):
        return ''
    if not isinstance(items, list):
        return ''
    blocks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        question = html_module.escape(str(item.get('q') or '').strip())
        answer = html_to_telegram(str(item.get('a') or ''))
        block = f'<b>{question}</b>\n{answer}'.strip() if question else answer
        if block:
            blocks.append(block)
    return '\n\n'.join(blocks)


def _scan_open_tags(chunk: str, carried: list[tuple[str, str]]) -> list[tuple[str, str]]:
    open_tags = list(carried)
    for match in _TAG_RE.finditer(chunk):
        tag_name = match.group(2).lower()
        if match.group(1) == '/':
            for index in range(len(open_tags) - 1, -1, -1):
                if open_tags[index][0] == tag_name:
                    del open_tags[index]
                    break
        else:
            open_tags.append((tag_name, match.group(0)))
    return open_tags


def _balance_chunks(raw_chunks: list[str]) -> list[str]:
    balanced: list[str] = []
    carried: list[tuple[str, str]] = []
    for raw_chunk in raw_chunks:
        chunk = raw_chunk.strip()
        if not chunk:
            continue
        prefix = ''.join(opening for _, opening in carried)
        open_after = _scan_open_tags(chunk, carried)
        suffix = ''.join(f'</{name}>' for name, _ in reversed(open_after))
        balanced.append(f'{prefix}{chunk}{suffix}')
        carried = open_after
    return balanced


def _trim_broken_tag(chunk: str) -> str:
    last_open = chunk.rfind('<')
    last_close = chunk.rfind('>')
    if last_open > last_close:
        chunk = chunk[:last_open]
    match = _INCOMPLETE_ENTITY_RE.search(chunk)
    if match:
        chunk = chunk[: match.start()]
    return chunk


def split_telegram_text(text: str | None, *, max_length: int = 3500) -> list[str]:
    if not text:
        return []
    normalized = text.replace('\r\n', '\n').strip()
    if not normalized:
        return []
    if len(normalized) <= max_length:
        return [normalized]

    paragraphs = [paragraph for paragraph in normalized.split('\n\n') if paragraph.strip()]
    raw_chunks: list[str] = []
    current = ''

    for paragraph in paragraphs:
        candidate = f'{current}\n\n{paragraph}' if current else paragraph
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            raw_chunks.append(current)
            current = ''
        if len(paragraph) <= max_length:
            current = paragraph
            continue
        start = 0
        while start < len(paragraph):
            piece = paragraph[start : start + max_length]
            trimmed = _trim_broken_tag(piece)
            if not trimmed:
                trimmed = piece
            raw_chunks.append(trimmed)
            start += len(trimmed)
    if current:
        raw_chunks.append(current)

    safe: list[str] = []
    for chunk in _balance_chunks(raw_chunks):
        if len(chunk) <= _TELEGRAM_HARD_LIMIT:
            safe.append(chunk)
        elif max_length > 1000:
            safe.extend(split_telegram_text(chunk, max_length=max_length - 500))
        else:
            safe.extend(
                chunk[index : index + _TELEGRAM_HARD_LIMIT] for index in range(0, len(chunk), _TELEGRAM_HARD_LIMIT)
            )
    return safe
