from __future__ import annotations

from typing import Any

import structlog

from ai_support_bot.app.navigation import registry
from ai_support_bot.app.navigation.renderer import node_to_dict, render_matches, render_overview
from ai_support_bot.app.navigation.schema import NavTree
from ai_support_bot.app.navigation.search import search


logger = structlog.get_logger(__name__)

TOOL_NAME = 'navigation_lookup'

TOOL_SPEC: dict[str, Any] = {
    'type': 'function',
    'function': {
        'name': TOOL_NAME,
        'description': (
            'Поиск раздела или кнопки в интерфейсе сервиса. Возвращает точный путь к разделу '
            'в Telegram-боте и в веб-кабинете с актуальными названиями кнопок.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': 'Формулировка того, что ищет пользователь, например «вывод реферального бонуса».',
                },
                'language': {
                    'type': 'string',
                    'description': 'Код языка интерфейса (ru, en, ua, fa, zh). По умолчанию ru.',
                },
                'limit': {
                    'type': 'integer',
                    'description': 'Сколько разделов вернуть, 1-5.',
                },
            },
            'required': ['query'],
        },
    },
}


async def _resolve_tree(language: str | None, ttl_seconds: int) -> NavTree | None:
    try:
        return await registry.get_tree(language, ttl_seconds=ttl_seconds)
    except Exception as error:
        logger.warning('Navigation tree unavailable', error=str(error))
        return registry.peek(language)


async def lookup(
    query: str,
    language: str | None = None,
    limit: int = 3,
    depth: int = 2,
    max_children: int = 8,
    ttl_seconds: int = 0,
    include_overview: bool = True,
) -> dict[str, Any]:
    tree = await _resolve_tree(language, ttl_seconds)
    if tree is None:
        return {'found': False, 'text': '', 'nodes': [], 'language': language or registry.DEFAULT_LANGUAGE}

    matches = search(tree, query or '', limit=max(1, min(limit, 5)))
    nodes = [match.node for match in matches]

    blocks: list[str] = []
    if nodes:
        blocks.append(render_matches(tree, nodes, depth=depth, max_children=max_children))
    elif include_overview:
        overview = render_overview(tree)
        if overview:
            blocks.append(overview)

    return {
        'found': bool(nodes),
        'text': '\n\n'.join(block for block in blocks if block),
        'nodes': [node_to_dict(tree, node, depth=depth) for node in nodes],
        'scores': {match.node.id: match.score for match in matches},
        'language': tree.language,
        'tree_size': tree.size,
        'sources': list(tree.sources),
    }


async def build_prompt_block(
    query: str,
    language: str | None = None,
    limit: int = 3,
    depth: int = 2,
    max_children: int = 8,
    max_chars: int = 1400,
    ttl_seconds: int = 0,
) -> str:
    result = await lookup(
        query,
        language=language,
        limit=limit,
        depth=depth,
        max_children=max_children,
        ttl_seconds=ttl_seconds,
        include_overview=False,
    )
    if not result.get('found'):
        return ''

    body = result.get('text') or ''
    if not body:
        return ''

    if len(body) > max_chars > 0:
        body = body[:max_chars].rstrip() + '\n  …'

    header = (
        'Карта интерфейса по теме вопроса (актуальные названия кнопок; используй эти пути дословно, '
        'ничего не придумывай):'
    )
    return f'{header}\n{body}'
