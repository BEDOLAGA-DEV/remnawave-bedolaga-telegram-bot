from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class NavNode:
    id: str
    title: str
    parent_id: str | None = None
    bot_label: str | None = None
    bot_callback: str | None = None
    web_label: str | None = None
    web_path: str | None = None
    hint: str = ''
    keywords: tuple[str, ...] = ()
    source: str = 'blueprint'
    children: list['NavNode'] = field(default_factory=list)

    @property
    def available_in_bot(self) -> bool:
        return bool(self.bot_label or self.bot_callback)

    @property
    def available_in_web(self) -> bool:
        return bool(self.web_path)

    def searchable_text(self) -> str:
        parts = [self.title, self.bot_label or '', self.web_label or '', self.hint, ' '.join(self.keywords)]
        return ' '.join(part for part in parts if part)


@dataclass(slots=True)
class NavTree:
    roots: list[NavNode] = field(default_factory=list)
    index: dict[str, NavNode] = field(default_factory=dict)
    language: str = 'ru'
    built_at: float = 0.0
    sources: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        return len(self.index)

    def get(self, node_id: str) -> NavNode | None:
        return self.index.get(node_id)

    def ancestors(self, node: NavNode) -> list[NavNode]:
        chain: list[NavNode] = []
        current = node.parent_id
        guard = 0
        while current and guard < 20:
            parent = self.index.get(current)
            if parent is None:
                break
            chain.append(parent)
            current = parent.parent_id
            guard += 1
        chain.reverse()
        return chain

    def iter_nodes(self):
        stack = list(reversed(self.roots))
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))
