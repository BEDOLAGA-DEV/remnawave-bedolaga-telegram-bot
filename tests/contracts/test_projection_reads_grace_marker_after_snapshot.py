"""Сторож: перенос «панель → бот» знает про грейс, открытый после загрузки подписки.

Стенд 2026-09-15 (волна истечения, панель 3.4.3): мониторинг загрузил список
истёкших подписок, воркер выдал грейс части из них, пока мониторинг шёл по
списку, — и у 5% людей оверлей грейса (его дата, сквад, «расход + 1 ГБ») осел в
подписке. Признак ``grace_session_open`` на объекте из списка был устаревшим.

Правило детерминированное: хранилище грейса фиксирует признак ДО того, как оверлей
уходит в панель, поэтому признак, прочитанный из базы ПОСЛЕ снимка панели, видит
любой грейс, который этот снимок мог показать. Каждый вызов
``project_onto_subscription`` в ``app/`` обязан получить такой признак одним из
способов:

* ``grace_open=`` — признак, посчитанный вызывающим из базы уже после снимка
  (полная синхронизация, вебхук);
* ``.refresh(...)`` подписки в той же функции до переноса (мониторинг, вход по
  почте, кнопка админки «из панели в бота») — со всеми полями
  ``GRACE_MARKER_FIELDS``: признаком, хвостом и меткой даты оверлея.
"""

from __future__ import annotations

import ast
import pathlib

from app.services.panel_sync import GRACE_MARKER_FIELDS


APP = pathlib.Path(__file__).resolve().parents[2] / 'app'


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _projection_sites() -> list[tuple[str, ast.AST, ast.Call]]:
    sites = []
    for path in APP.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and _call_name(node) == 'project_onto_subscription':
                    rel = path.relative_to(APP.parent).as_posix()
                    sites.append((f'{rel}::{function.name}', function, node))
    return sites


def _refresh_reads_every_marker(node: ast.Call) -> bool:
    """Перечитаны все признаки грейса: вся строка, общий список или каждое поле по имени.

    Проекция опирается не только на ``grace_session_open``: метку даты оверлея
    (``grace_overlay_expire_at``) и хвост она сверяет с датой снимка. Перечитать
    признак, а метку оставить старой — пропустить снимок оверлея, снятый до
    досрочного закрытия грейса.
    """
    names = node.args[1:] + [keyword.value for keyword in node.keywords if keyword.arg == 'attribute_names']
    if not names:
        return True
    source = ast.unparse(names[0])
    if 'GRACE_MARKER_FIELDS' in source:
        return True
    return all(f"'{field}'" in source for field in GRACE_MARKER_FIELDS)


def _reads_marker_fresh(function: ast.AST, call: ast.Call) -> bool:
    if any(keyword.arg == 'grace_open' for keyword in call.keywords):
        return True
    return any(
        isinstance(node, ast.Call)
        and _call_name(node) == 'refresh'
        and node.lineno < call.lineno
        and _refresh_reads_every_marker(node)
        for node in ast.walk(function)
    )


def test_every_projection_reads_the_grace_marker_after_the_panel_snapshot() -> None:
    sites = _projection_sites()
    assert len(sites) >= 5, 'сторож перестал находить вызовы переноса — проверь поиск'
    stale = [name for name, function, call in sites if not _reads_marker_fresh(function, call)]
    assert not stale, (
        'перенос «панель → бот» без свежего признака грейса — оверлей грейса осядет в подписке: '
        + ', '.join(sorted(stale))
    )
