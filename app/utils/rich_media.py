"""Attach external rich-HTML images using the Bot API media-reference contract."""

import html
import re
from collections.abc import Iterator

from aiogram.types import InputMediaPhoto, InputRichMessage, InputRichMessageMedia


_PHOTO_ID_RE = re.compile(r'tg://photo\?id=([A-Za-z0-9_-]+)')


def _image_source_spans(markup: str) -> Iterator[tuple[int, int]]:
    """Scan quoted image attributes once, without regex retries or reserializing HTML."""
    cursor = 0
    size = len(markup)
    while cursor < size:
        opening = markup.find('<', cursor)
        if opening < 0:
            return
        cursor = opening + 1
        if markup.startswith('!--', cursor):
            closing = markup.find('-->', cursor + 3)
            if closing < 0:
                return
            cursor = closing + 3
            continue
        name_start = cursor
        while cursor < size and not markup[cursor].isspace() and markup[cursor] not in '/>':
            cursor += 1
        is_image = markup[name_start:cursor].lower() == 'img'
        found_source = False
        while cursor < size and markup[cursor] != '>':
            if markup[cursor].isspace() or markup[cursor] in '/=':
                cursor += 1
                continue
            attribute_start = cursor
            while cursor < size and not markup[cursor].isspace() and markup[cursor] not in '=/>':
                cursor += 1
            attribute = markup[attribute_start:cursor].lower()
            while cursor < size and markup[cursor].isspace():
                cursor += 1
            if cursor == size or markup[cursor] != '=':
                continue
            cursor += 1
            while cursor < size and markup[cursor].isspace():
                cursor += 1
            if cursor == size:
                return
            quote = markup[cursor]
            if quote not in ('"', "'"):
                while cursor < size and not markup[cursor].isspace() and markup[cursor] != '>':
                    cursor += 1
                continue
            value_start = cursor + 1
            value_end = markup.find(quote, value_start)
            if value_end < 0:
                return
            cursor = value_end + 1
            if is_image and attribute == 'src' and not found_source:
                found_source = True
                yield value_start, value_end
        cursor += 1


def prepare_rich_message(rich_html: str, *, is_rtl: bool | None = None) -> InputRichMessage:
    """Replace HTTP image sources with matching tg://photo references and media.

    Keep the rest of the markup intact, including existing Telegram references.
    Repeated URLs share an attachment; generated IDs avoid existing references.
    Telegram fetches the supplied URLs, so this does not download files locally.
    """
    media: list[InputRichMessageMedia] = []
    image_ids: dict[str, str] = {}
    used_ids = set(_PHOTO_ID_RE.findall(rich_html))

    parts: list[str] = []
    copied_until = 0
    for start, end in _image_source_spans(rich_html):
        source = html.unescape(rich_html[start:end])
        if not source.lower().startswith(('http://', 'https://')):
            continue
        if source not in image_ids:
            index = len(media)
            while (image_id := f'photo_{index}') in used_ids:
                index += 1
            used_ids.add(image_id)
            image_ids[source] = image_id
            media.append(InputRichMessageMedia(id=image_id, media=InputMediaPhoto(media=source)))
        parts.append(rich_html[copied_until:start])
        parts.append(f'tg://photo?id={image_ids[source]}')
        copied_until = end
    parts.append(rich_html[copied_until:])

    return InputRichMessage(
        html=''.join(parts),
        media=media or None,
        is_rtl=is_rtl,
        skip_entity_detection=True,
    )
