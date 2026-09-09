"""Отправка сквадов в панель обязана уважать снятые премиум-сквады.

Сквад, снятый воркером за перерасход, живёт только в панели: в
`subscription.connected_squads` он остаётся, потому что право на него у подписки
никуда не делось. Значит любая отправка ``activeInternalSquads`` обязана
пропустить набор через ``effective_panel_squads`` — иначе ближайшая
синхронизация вернёт сквад и снимет ограничение.

Раньше проверка сканировала весь ``app/``: писателей было два десятка, по копии
у каждого потребителя. Теперь правила собраны в ``app/services/panel_sync``, и
сторожить достаточно его — за остальных отвечает ``test_no_bypass``, который
запрещает звать клиент панели снаружи пакета.
"""

from __future__ import annotations

import ast
import pathlib


WRITER = pathlib.Path(__file__).resolve().parents[2] / 'app' / 'services' / 'panel_sync' / 'writer.py'

GUARD = 'effective_panel_squads'

# Функции пакета, которые отправляют в панель набор сквадов. `patch_panel_account`
# сюда не входит намеренно: он правит карточку человека и сквадов не касается.
SQUAD_WRITERS = ('push_subscription', 'patch_panel_squads')


def _function(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(WRITER.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} не найдена в {WRITER.name}: сторож смотрит не туда')


def _calls_guard(node: ast.AST) -> bool:
    return any(
        isinstance(inner, ast.Call)
        and (
            (isinstance(inner.func, ast.Name) and inner.func.id == GUARD)
            or (isinstance(inner.func, ast.Attribute) and inner.func.attr == GUARD)
            # Вынесенный помощник считается: важно, что фильтр вызывается.
            or (isinstance(inner.func, ast.Name) and inner.func.id.startswith('_without_limited'))
        )
        for inner in ast.walk(node)
    )


def test_every_squad_writer_filters_limited_squads():
    unguarded = [name for name in SQUAD_WRITERS if not _calls_guard(_function(name))]

    assert not unguarded, (
        'Эти функции отправляют сквады в панель мимо фильтра — снятый за '
        f'перерасход премиум-сквад вернётся пользователю: {unguarded}'
    )


def test_account_patcher_does_not_touch_squads():
    """`patch_panel_account` фильтровать нечего — и он не должен знать о сквадах.

    Если сквады появятся и там, фильтр придётся ставить и туда, а сторож выше
    об этом не узнает: он смотрит на заранее известный список.
    """
    source = ast.unparse(_function('patch_panel_account'))

    assert 'active_internal_squads' not in source, (
        'patch_panel_account начал писать сквады — добавьте его в SQUAD_WRITERS и поставьте фильтр'
    )


def test_guard_is_reachable_from_the_writer():
    """Страховка от обратного: правило есть, а импорт потеряли."""
    assert GUARD in WRITER.read_text(encoding='utf-8'), (
        f'{WRITER.name} перестал ссылаться на {GUARD} — фильтр премиум-сквадов потерян'
    )
