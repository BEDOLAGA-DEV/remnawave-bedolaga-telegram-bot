"""Сторож: каждое удаление аккаунта в панели спрашивает REMNAWAVE_USER_DELETE_MODE.

Отчёт из «Багов»: при сбросе триала аккаунт удалялся из панели, хотя в .env стоял
``disable``. Режим спрашивали в одном месте из пяти. Список ниже — полный
перечень мест, которые зовут удаление в панели, и то, чем каждое оправдано.
Новая точка удаления обязана либо спросить режим, либо попасть сюда с явным
объяснением — иначе настройка снова окажется наполовину рабочей.
"""

import ast
from pathlib import Path

import pytest


APP = Path(__file__).resolve().parents[2] / 'app'

# Файл → почему удаление в нём не гейтится режимом.
DELIBERATE = {
    # Низкоуровневая обёртка: решение принимают вызывающие (все они в этом списке
    # либо спрашивают режим сами).
    'services/subscription_service.py': 'низкоуровневый метод, режим решают вызывающие',
    # Явно выбранное админом действие «удалить из Remnawave» в разделе
    # заблокированных — конкретнее глобальной настройки по умолчанию.
    'services/blocked_users_service.py': 'явное действие админа DELETE_FROM_REMNAWAVE',
    # Полное удаление пользователя: режим спрашивается, но ещё есть
    # force_panel_delete — явный выбор вызывающего.
    'services/user_service.py': 'режим спрашивается, force_panel_delete перекрывает',
}

MODE_GETTER = 'get_remnawave_user_delete_mode'


def _deletes_panel_user(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in ('delete_user', 'delete_remnawave_user'):
            return True
    return False


def _asks_for_mode(source: str) -> bool:
    return MODE_GETTER in source


def _panel_deleting_files() -> list[tuple[str, str]]:
    found = []
    for path in sorted(APP.rglob('*.py')):
        source = path.read_text(encoding='utf-8')
        if 'delete_user' not in source and 'delete_remnawave_user' not in source:
            continue
        tree = ast.parse(source)
        if _deletes_panel_user(tree):
            found.append((str(path.relative_to(APP)), source))
    return found


@pytest.mark.parametrize('rel_path', [rel for rel, _ in _panel_deleting_files()])
def test_panel_deletion_is_gated_or_explicitly_deliberate(rel_path):
    source = dict(_panel_deleting_files())[rel_path]
    if _asks_for_mode(source):
        return
    assert rel_path in DELIBERATE, (
        f'{rel_path} удаляет пользователя из панели, не спрашивая {MODE_GETTER}(). '
        'Либо спросите режим, либо внесите файл в DELIBERATE с объяснением.'
    )


def test_deliberate_list_has_no_stale_entries():
    """Список исключений не должен пережить сами удаления."""
    actual = {rel for rel, _ in _panel_deleting_files()}
    assert set(DELIBERATE) <= actual, f'В DELIBERATE остались файлы без удаления: {set(DELIBERATE) - actual}'
