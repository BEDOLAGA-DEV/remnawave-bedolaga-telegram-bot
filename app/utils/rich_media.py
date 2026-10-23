"""Attach external rich-HTML images using the Bot API media-reference contract."""

import html
import re

from aiogram.types import InputMediaPhoto, InputRichMessage, InputRichMessageMedia


_IMAGE_SOURCE_RE = re.compile(r'(<img\b[^>]*?\s+src\s*=\s*)([\"\x27])(.*?)\2', re.IGNORECASE | re.DOTALL)
_PHOTO_ID_RE = re.compile(r'tg://photo\?id=([A-Za-z0-9_-]+)')


def prepare_rich_message(rich_html: str, *, is_rtl: bool | None = None) -> InputRichMessage:
    """Replace HTTP image sources with matching tg://photo references and media.

    Keep the rest of the markup intact, including existing Telegram references.
    Repeated URLs share an attachment; generated IDs avoid existing references.
    Telegram fetches the supplied URLs, so this does not download files locally.
    """
    media: list[InputRichMessageMedia] = []
    image_ids: dict[str, str] = {}
    used_ids = set(_PHOTO_ID_RE.findall(rich_html))

    def attach_image(match: re.Match[str]) -> str:
        source = html.unescape(match[3])
        if not source.lower().startswith(('http://', 'https://')):
            return match[0]
        if source not in image_ids:
            index = len(media)
            while (image_id := f'photo_{index}') in used_ids:
                index += 1
            used_ids.add(image_id)
            image_ids[source] = image_id
            media.append(InputRichMessageMedia(id=image_id, media=InputMediaPhoto(media=source)))
        return f'{match[1]}{match[2]}tg://photo?id={image_ids[source]}{match[2]}'

    return InputRichMessage(
        html=_IMAGE_SOURCE_RE.sub(attach_image, rich_html),
        media=media or None,
        is_rtl=is_rtl,
        skip_entity_detection=True,
    )
