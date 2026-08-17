from __future__ import annotations

from ai_support_bot.app.navigation.schema import NavNode, NavTree


_BOT_PREFIX = 'Бот'
_WEB_PREFIX = 'Кабинет'


def _bot_path(tree: NavTree, node: NavNode) -> str:
    if not node.available_in_bot:
        return ''
    chain = [item for item in [*tree.ancestors(node), node] if item.available_in_bot]
    if not chain:
        return ''
    labels = [item.bot_label or item.title for item in chain]
    return f'{_BOT_PREFIX}: ' + ' → '.join(labels)


def _web_path(tree: NavTree, node: NavNode) -> str:
    if not node.available_in_web:
        return ''
    chain = [item for item in [*tree.ancestors(node), node] if item.available_in_web]
    labels = [item.web_label or item.title for item in chain]
    route = node.web_path or ''
    rendered = ' → '.join(labels) if labels else node.title
    return f'{_WEB_PREFIX}: {rendered} ({route})'


def render_node(tree: NavTree, node: NavNode, depth: int = 2, max_children: int = 8) -> str:
    lines: list[str] = [f'Раздел: {node.title}']

    bot_path = _bot_path(tree, node)
    if bot_path:
        lines.append(f'  {bot_path}')
    else:
        lines.append(f'  {_BOT_PREFIX}: недоступно, раздел есть только в личном кабинете')

    web_path = _web_path(tree, node)
    if web_path:
        lines.append(f'  {web_path}')
    else:
        lines.append(f'  {_WEB_PREFIX}: недоступно, раздел есть только в боте')

    if node.hint:
        lines.append(f'  Назначение: {node.hint}')

    if depth > 0 and node.children:
        lines.append('  Вложенные пункты:')
        lines.extend(_render_children(node.children, depth, max_children, indent=4))

    return '\n'.join(lines)


def _render_children(children: list[NavNode], depth: int, max_children: int, indent: int) -> list[str]:
    lines: list[str] = []
    pad = ' ' * indent
    shown = children[:max_children]
    for child in shown:
        places: list[str] = []
        if child.available_in_bot:
            places.append('бот')
        if child.available_in_web:
            places.append(f'кабинет {child.web_path}')
        suffix = f' [{", ".join(places)}]' if places else ''
        label = child.bot_label or child.web_label or child.title
        lines.append(f'{pad}• {label}{suffix}')
        if depth > 1 and child.children:
            lines.extend(_render_children(child.children, depth - 1, max_children, indent + 2))
    hidden = len(children) - len(shown)
    if hidden > 0:
        lines.append(f'{pad}• …и ещё {hidden} пункт(ов)')
    return lines


def render_matches(tree: NavTree, nodes: list[NavNode], depth: int = 2, max_children: int = 8) -> str:
    if not nodes:
        return ''
    blocks = [render_node(tree, node, depth=depth, max_children=max_children) for node in nodes]
    return '\n\n'.join(blocks)


def render_overview(tree: NavTree, max_sections: int = 12) -> str:
    root = tree.roots[0] if tree.roots else None
    if root is None:
        return ''
    lines: list[str] = ['Верхний уровень навигации:']
    for child in root.children[:max_sections]:
        places: list[str] = []
        if child.available_in_bot:
            places.append('бот')
        if child.available_in_web:
            places.append(f'кабинет {child.web_path}')
        label = child.bot_label or child.web_label or child.title
        lines.append(f'  • {label} [{", ".join(places) or "нет данных"}]')
    return '\n'.join(lines)


def node_to_dict(tree: NavTree, node: NavNode, depth: int = 2) -> dict[str, object]:
    payload: dict[str, object] = {
        'id': node.id,
        'title': node.title,
        'bot_label': node.bot_label,
        'bot_callback': node.bot_callback,
        'bot_path': _bot_path(tree, node),
        'web_label': node.web_label,
        'web_path': node.web_path,
        'web_breadcrumbs': _web_path(tree, node),
        'hint': node.hint,
        'source': node.source,
    }
    if depth > 0 and node.children:
        payload['children'] = [node_to_dict(tree, child, depth - 1) for child in node.children]
    return payload
