from __future__ import annotations

import re
from typing import Any

import structlog

from ai_support_bot.app.navigation import registry
from ai_support_bot.app.navigation.query import navigation_target
from ai_support_bot.app.navigation.renderer import node_to_dict, render_matches, render_node, render_overview, web_url
from ai_support_bot.app.navigation.schema import NavTree
from ai_support_bot.app.navigation.search import search
from ai_support_bot.app.navigation.web import PUBLIC_WEB_HELP


logger = structlog.get_logger(__name__)

TOOL_NAME = 'navigation_lookup'


def resolve_surface(query: str) -> str | None:
    for line in reversed(query.splitlines()):
        web = bool(re.search(r'\b(?:сайт\w*|кабинет\w*|веб\w*|web|website)\b', line, re.IGNORECASE))
        bot = bool(re.search(r'\b(?:бот\w*|телеграм\w*|telegram|tg|тг)\b', line, re.IGNORECASE))
        if web or bot:
            return 'web' if web and not bot else 'bot' if bot and not web else None
    return None


def _wants_overview(query: str) -> bool:
    return bool(re.search(
        r'структур\w*|навигаци\w*|(?:какие|основные|все|список)\s+(?:есть\s+)?раздел\w*|что есть (?:на сайте|в кабинете)',
        query, re.IGNORECASE,
    ))

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

    surface = resolve_surface(query or '')
    if surface and _wants_overview(query or ''):
        return {
            'found': True, 'overview': True, 'text': render_overview(tree, surface=surface),
            'nodes': [], 'language': tree.language, 'tree_size': tree.size, 'sources': list(tree.sources),
        }
    target = tree.get(navigation_target(query or '') or '')
    target_available = target is not None and (surface != 'web' or target.available_in_web) and (surface != 'bot' or target.available_in_bot)
    matches = [] if target_available else search(tree, query or '', limit=max(1, min(limit, 5)), surface=surface)
    nodes = [target] if target_available else [match.node for match in matches]

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

    if result.get('overview'):
        body = result['text']
        if max_chars > 0:
            lines: list[str] = []
            for line in body.splitlines():
                if len('\n'.join([*lines, line])) > max_chars:
                    break
                lines.append(line)
            body = '\n'.join(lines)
        return 'Карта интерфейса:\n' + body

    tree = await _resolve_tree(language, ttl_seconds)
    if tree is None:
        return ''
    nodes = [tree.get(item['id']) for item in result['nodes']]
    nodes = [node for node in nodes if node is not None]
    blocks: list[str] = []
    for node in nodes:
        block = render_node(tree, node, depth=depth, max_children=max_children)
        if max_chars > 0 and len('\n\n'.join([*blocks, block])) > max_chars:
            compact = render_node(tree, node, depth=0)
            block = '\n'.join(
                line for line in compact.splitlines()
                if line.strip().startswith(('Бот:', 'Кабинет:', 'Особенности сайта:'))
            )
        if max_chars <= 0 or len('\n\n'.join([*blocks, block])) <= max_chars:
            blocks.append(block)
    body = '\n\n'.join(blocks)
    if not body:
        return ''

    header = (
        'Карта интерфейса по теме вопроса (актуальные названия кнопок; используй эти пути дословно, '
        'ничего не придумывай):'
    )
    return f'{header}\n{body}'


async def answer_request(query: str, language: str = 'ru', ttl_seconds: int = 0) -> str | None:
    if not re.search(r'как|где|инструкци|покажи|какие разделы', query, re.IGNORECASE):
        return None
    if re.search(r'не работает|не продл|не приш|не поступ|не зачис|сколько|почему', query, re.IGNORECASE):
        return None
    if language != 'ru':
        return None
    tree = await _resolve_tree(language, ttl_seconds)
    if tree is None:
        return None
    surface = resolve_surface(query)
    if surface == 'web' and _wants_overview(query):
        sections = [node for node in tree.iter_nodes() if node.available_in_web and node.id != 'main_menu' and len(node.web_steps) == 1]
        return 'Разделы сайта:\n\n' + '\n'.join(
            f'• {node.web_label}: {web_url(tree, node)}' for node in sections
        ) + '\n\nНекоторые разделы доступны только при включённой функции. На телефоне дополнительные разделы находятся в «Меню».'
    target_id = navigation_target(query)
    node = tree.get(target_id or '')
    if node is None:
        return None
    blocks = []
    if surface != 'bot' and node.available_in_web:
        block = 'На сайте: ' + web_url(tree, node) + '\nПуть: ' + ' → '.join(node.web_steps) + '.'
        if help_text := PUBLIC_WEB_HELP.get(target_id):
            block += '\n' + help_text
        blocks.append(block)
    if surface != 'web' and node.available_in_bot:
        path = node_to_dict(tree, node, depth=0)['bot_path']
        blocks.append('В Telegram: <a href="https://t.me/SligVPN_bot?start=ai_support">https://t.me/SligVPN_bot</a>\n' + str(path))
    return '\n\n'.join(blocks) or None
