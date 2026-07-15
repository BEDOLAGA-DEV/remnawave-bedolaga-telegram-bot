"""Outgoing Bot API middleware for semantic presentation markers."""

from __future__ import annotations

from typing import Any

import structlog
from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import TelegramMethod
from pydantic import BaseModel

from app.services.bot_presentation_service import (
    apply_button_presentation,
    apply_html_emoji,
    maybe_refresh_bot_presentation_cache,
    strip_presentation_markers,
)


logger = structlog.get_logger(__name__)

_TEXT_SLOTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    'text': (('text_entities', 'entities'), ('text_parse_mode', 'parse_mode')),
    'message_text': (('entities',), ('parse_mode',)),
    'caption': (('caption_entities',), ('parse_mode',)),
    'question': (('question_entities',), ('question_parse_mode',)),
    'explanation': (('explanation_entities',), ('explanation_parse_mode',)),
    'quote': (('quote_entities',), ('quote_parse_mode',)),
}


def _uses_html(parse_mode: Any, default_parse_mode: Any) -> bool:
    value = parse_mode
    if value.__class__.__name__ == 'Default':
        value = default_parse_mode
    normalized = str(getattr(value, 'value', value) or '').lower()
    return normalized == 'html'


def _first_present(model: BaseModel, names: tuple[str, ...]) -> Any:
    for name in names:
        if name in type(model).model_fields:
            return getattr(model, name, None)
    return None


def _render_value(
    value: str,
    *,
    parse_mode: Any,
    default_parse_mode: Any,
    entities: Any,
    custom: bool,
) -> str:
    if custom and not entities and _uses_html(parse_mode, default_parse_mode):
        return apply_html_emoji(value) or value
    return strip_presentation_markers(value) or value


def _render_nested(value: Any, default_parse_mode: Any, *, custom: bool) -> Any:
    if isinstance(value, BaseModel):
        return _render_model(value, default_parse_mode, custom=custom)
    if isinstance(value, list):
        rendered = [_render_nested(item, default_parse_mode, custom=custom) for item in value]
        return rendered if any(new is not old for new, old in zip(rendered, value, strict=True)) else value
    if isinstance(value, tuple):
        rendered = tuple(_render_nested(item, default_parse_mode, custom=custom) for item in value)
        return rendered if any(new is not old for new, old in zip(rendered, value, strict=True)) else value
    return value


def _render_model(model: BaseModel, default_parse_mode: Any, *, custom: bool) -> BaseModel:
    updates: dict[str, Any] = {}
    handled: set[str] = set()

    for field_name, (entity_names, parse_mode_names) in _TEXT_SLOTS.items():
        if field_name not in type(model).model_fields:
            continue
        handled.add(field_name)
        value = getattr(model, field_name, None)
        if not value:
            continue
        rendered = _render_value(
            value,
            parse_mode=_first_present(model, parse_mode_names),
            default_parse_mode=default_parse_mode,
            entities=_first_present(model, entity_names),
            custom=custom,
        )
        if rendered != value:
            updates[field_name] = rendered

    if 'rich_message' in type(model).model_fields:
        handled.add('rich_message')
        rich_message = getattr(model, 'rich_message', None)
        if rich_message is not None and getattr(rich_message, 'html', None):
            html = rich_message.html
            rendered = apply_html_emoji(html) if custom else strip_presentation_markers(html)
            if rendered != html:
                updates['rich_message'] = rich_message.model_copy(update={'html': rendered}, deep=True)

    if 'reply_markup' in type(model).model_fields:
        handled.add('reply_markup')
        reply_markup = getattr(model, 'reply_markup', None)
        rendered_markup = apply_button_presentation(reply_markup, custom=custom)
        if rendered_markup is not reply_markup:
            updates['reply_markup'] = rendered_markup

    for field_name in type(model).model_fields:
        if field_name in handled or field_name in updates:
            continue
        value = getattr(model, field_name, None)
        if isinstance(value, str):
            rendered_text = strip_presentation_markers(value) or value
            if rendered_text != value:
                updates[field_name] = rendered_text
            continue
        if not isinstance(value, (BaseModel, list, tuple)):
            continue
        rendered = _render_nested(value, default_parse_mode, custom=custom)
        if rendered is not value:
            updates[field_name] = rendered

    return model.model_copy(update=updates, deep=True) if updates else model


def apply_method_presentation(
    method: TelegramMethod,
    default_parse_mode: Any = 'HTML',
    *,
    custom: bool = True,
) -> TelegramMethod:
    """Render every supported nested Bot API text while preserving behavior."""
    rendered = _render_model(method, default_parse_mode, custom=custom)
    return rendered  # type: ignore[return-value]


class BotPresentationRequestMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot: Bot, method: TelegramMethod):
        await maybe_refresh_bot_presentation_cache()
        default_parse_mode = getattr(getattr(bot, 'default', None), 'parse_mode', 'HTML')
        fallback = apply_method_presentation(method, default_parse_mode, custom=False)
        decorated = apply_method_presentation(method, default_parse_mode, custom=True)
        if decorated == fallback:
            return await make_request(bot, fallback)
        try:
            return await make_request(bot, decorated)
        except TelegramBadRequest as error:
            logger.warning(
                'Decorated Telegram request was rejected; retrying Unicode fallback',
                method=type(method).__name__,
                error=str(error),
            )
            return await make_request(bot, fallback)
