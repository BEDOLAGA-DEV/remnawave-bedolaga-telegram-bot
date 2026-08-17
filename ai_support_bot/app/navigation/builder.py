from __future__ import annotations

import json
import re
import time
from typing import Any

import structlog
from sqlalchemy import text

from ai_support_bot.app.db.database import get_main_session
from ai_support_bot.app.navigation.blueprint import BLUEPRINT, CALLBACK_HINTS
from ai_support_bot.app.navigation.locales import load_locale, locales_available
from ai_support_bot.app.navigation.schema import NavNode, NavTree


logger = structlog.get_logger(__name__)

_PLACEHOLDER_RE = re.compile(r'\{[^{}]*\}')
_TAG_RE = re.compile(r'<[^>]+>')
_MENU_LAYOUT_KEY = 'menu_layout_config'
_MAX_DYNAMIC_NODES = 60


def _clean_label(value: Any) -> str:
    if not isinstance(value, str):
        return ''
    cleaned = _TAG_RE.sub('', value)
    cleaned = _PLACEHOLDER_RE.sub('', cleaned)
    cleaned = cleaned.replace('\n', ' ')
    return ' '.join(cleaned.split()).strip()


def _slugify(value: str) -> str:
    slug = re.sub(r'[^0-9a-zA-Zа-яА-ЯёЁ]+', '_', value or '').strip('_').lower()
    return slug[:48] or 'item'


def _build_static_nodes(
    items: list[dict[str, Any]],
    locale: dict[str, str],
    parent_id: str | None,
    index: dict[str, NavNode],
) -> list[NavNode]:
    nodes: list[NavNode] = []
    for item in items:
        node_id = item['id']
        locale_key = item.get('locale_key') or ''
        label = _clean_label(locale.get(locale_key, '')) if locale_key else ''
        title = label or item['title']

        node = NavNode(
            id=node_id,
            title=title,
            parent_id=parent_id,
            bot_label=label or (item['title'] if item.get('bot_callback') else None),
            bot_callback=item.get('bot_callback'),
            web_label=label or item['title'] if item.get('web_path') else None,
            web_path=item.get('web_path'),
            hint=item.get('hint', ''),
            keywords=tuple(item.get('keywords', ())),
            source='locale' if label else 'blueprint',
        )
        if not item.get('bot_callback'):
            node.bot_label = None

        node.children = _build_static_nodes(item.get('children', []), locale, node_id, index)
        index[node_id] = node
        nodes.append(node)
    return nodes


def _apply_menu_layout(tree_index: dict[str, NavNode], raw_config: str | None, language: str) -> int:
    if not raw_config:
        return 0
    try:
        config = json.loads(raw_config)
    except (TypeError, ValueError):
        return 0
    if not isinstance(config, dict):
        return 0

    buttons = config.get('buttons')
    if not isinstance(buttons, dict):
        return 0

    applied = 0
    for payload in buttons.values():
        if not isinstance(payload, dict) or payload.get('enabled') is False:
            continue
        action = payload.get('action')
        node_id = CALLBACK_HINTS.get(action) if isinstance(action, str) else None
        node = tree_index.get(node_id) if node_id else None
        if node is None:
            continue
        raw_text = payload.get('text')
        label = ''
        if isinstance(raw_text, dict):
            label = _clean_label(raw_text.get(language) or raw_text.get('ru') or raw_text.get('en'))
        elif isinstance(raw_text, str):
            label = _clean_label(raw_text)
        if not label:
            continue
        node.bot_label = label
        node.title = label
        node.source = 'menu_layout'
        applied += 1
    return applied


_SECTION_TO_NODE_ID: dict[str, str] = {
    'home': 'main_menu',
    'subscription': 'subscription',
    'balance': 'balance',
    'referral': 'referral',
    'support': 'support',
    'info': 'info',
    'admin': 'admin',
    'language': 'info_language',
}
_BUTTON_STYLES_KEY = 'CABINET_BUTTON_STYLES'


def _apply_button_styles(tree_index: dict[str, NavNode], raw_config: str | None, language: str) -> int:
    if not raw_config:
        return 0
    try:
        data = json.loads(raw_config)
    except (TypeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0

    applied = 0
    for section, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        node_id = _SECTION_TO_NODE_ID.get(section)
        node = tree_index.get(node_id) if node_id else None
        if node is None:
            continue
        labels = cfg.get('labels')
        if not isinstance(labels, dict):
            continue
        raw_label = labels.get(language) or labels.get('ru') or labels.get('en')
        label = _clean_label(raw_label)
        if not label:
            continue
        node.bot_label = label
        node.title = label
        node.source = 'button_styles'
        applied += 1
    return applied


def _attach_dynamic(parent: NavNode, node: NavNode, index: dict[str, NavNode]) -> None:
    if node.id in index:
        return
    parent.children.append(node)
    index[node.id] = node


async def _load_db_layer(tree_index: dict[str, NavNode], language: str) -> list[str]:
    session = await get_main_session()
    if session is None:
        return []

    used: list[str] = []
    try:
        try:
            result = await session.execute(
                text('SELECT value FROM system_settings WHERE key = :key LIMIT 1'),
                {'key': _MENU_LAYOUT_KEY},
            )
            row = result.mappings().first()
            if row and _apply_menu_layout(tree_index, row.get('value'), language):
                used.append('menu_layout_config')
        except Exception as error:
            logger.warning('Menu layout config unavailable', error=str(error))

        try:
            result = await session.execute(
                text('SELECT value FROM system_settings WHERE key = :key LIMIT 1'),
                {'key': _BUTTON_STYLES_KEY},
            )
            row = result.mappings().first()
            if row and _apply_button_styles(tree_index, row.get('value'), language):
                used.append('button_styles')
        except Exception as error:
            logger.warning('Button styles unavailable', error=str(error))

        main_menu = tree_index.get('main_menu')
        if main_menu is not None:
            try:
                result = await session.execute(
                    text(
                        'SELECT id, text, action_type, action_value, visibility '
                        'FROM main_menu_buttons WHERE is_active = true '
                        'ORDER BY display_order ASC, id ASC LIMIT :limit'
                    ),
                    {'limit': _MAX_DYNAMIC_NODES},
                )
                rows = result.mappings().all()
                for entry in rows:
                    label = _clean_label(entry.get('text'))
                    if not label:
                        continue
                    node = NavNode(
                        id=f'custom_button_{entry.get("id")}',
                        title=label,
                        parent_id=main_menu.id,
                        bot_label=label,
                        bot_callback=None,
                        web_label=label,
                        web_path=None,
                        hint=(
                            'Дополнительная кнопка главного меню, настроенная администратором '
                            f'(тип: {entry.get("action_type")}, видимость: {entry.get("visibility")}).'
                        ),
                        keywords=(label.lower(),),
                        source='main_menu_buttons',
                    )
                    _attach_dynamic(main_menu, node, tree_index)
                if rows:
                    used.append('main_menu_buttons')
            except Exception as error:
                logger.warning('Custom main menu buttons unavailable', error=str(error))

        info_node = tree_index.get('info')
        if info_node is not None:
            try:
                result = await session.execute(
                    text(
                        'SELECT id, slug, title FROM info_pages WHERE is_active = true '
                        'ORDER BY sort_order ASC, id ASC LIMIT :limit'
                    ),
                    {'limit': _MAX_DYNAMIC_NODES},
                )
                rows = result.mappings().all()
                added = 0
                for entry in rows:
                    raw_title = entry.get('title')
                    if isinstance(raw_title, str):
                        try:
                            raw_title = json.loads(raw_title)
                        except (TypeError, ValueError):
                            raw_title = {'ru': raw_title}
                    label = ''
                    if isinstance(raw_title, dict):
                        label = _clean_label(raw_title.get(language) or raw_title.get('ru') or raw_title.get('en'))
                    if not label:
                        continue
                    node = NavNode(
                        id=f'info_page_{entry.get("id")}',
                        title=label,
                        parent_id=info_node.id,
                        bot_label=label,
                        bot_callback=f'info_page:{entry.get("id")}:1',
                        web_label=label,
                        web_path=f'/info-pages/{entry.get("slug")}' if entry.get('slug') else '/info',
                        hint='Информационная страница из раздела «Информация».',
                        keywords=(label.lower(),),
                        source='info_pages',
                    )
                    _attach_dynamic(info_node, node, tree_index)
                    added += 1
                if added:
                    used.append('info_pages')
            except Exception as error:
                logger.warning('Info pages unavailable', error=str(error))

        faq_node = tree_index.get('info_faq')
        if faq_node is not None:
            try:
                result = await session.execute(
                    text(
                        'SELECT id, title FROM faq_pages WHERE is_active = true AND language = :lang '
                        'ORDER BY display_order ASC, id ASC LIMIT :limit'
                    ),
                    {'lang': language, 'limit': _MAX_DYNAMIC_NODES},
                )
                rows = result.mappings().all()
                added = 0
                for entry in rows:
                    label = _clean_label(entry.get('title'))
                    if not label:
                        continue
                    node = NavNode(
                        id=f'faq_page_{entry.get("id")}',
                        title=label,
                        parent_id=faq_node.id,
                        bot_label=label,
                        bot_callback=f'faq_page_{entry.get("id")}',
                        web_label=label,
                        web_path='/info',
                        hint='Страница FAQ.',
                        keywords=(label.lower(),),
                        source='faq_pages',
                    )
                    _attach_dynamic(faq_node, node, tree_index)
                    added += 1
                if added:
                    used.append('faq_pages')
            except Exception as error:
                logger.warning('FAQ pages unavailable', error=str(error))
    finally:
        await session.close()

    return used


async def build_navigation_tree(language: str = 'ru') -> NavTree:
    code = (language or 'ru').strip().lower() or 'ru'
    locale = load_locale(code)
    if not locale and code != 'ru':
        locale = load_locale('ru')

    index: dict[str, NavNode] = {}
    roots = _build_static_nodes(BLUEPRINT, locale, None, index)

    sources: list[str] = ['blueprint']
    if locale and locales_available():
        sources.append('locales')

    sources.extend(await _load_db_layer(index, code))

    tree = NavTree(
        roots=roots,
        index=index,
        language=code,
        built_at=time.time(),
        sources=tuple(dict.fromkeys(sources)),
    )
    logger.info('Navigation tree built', language=code, nodes=tree.size, sources=tree.sources)
    return tree
