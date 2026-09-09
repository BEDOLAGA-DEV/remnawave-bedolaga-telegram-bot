"""В панель пишет только сервис синхронизации.

Каждое расхождение, которое пришлось чинить — переписанная дата окончания,
снятая блокировка, стёртые инбаунды, одинаковые имена у двух тарифов, — появилось
одинаково: правило скопировали в соседний модуль, потом починили одну копию.
Сторож ловит следующую копию в момент появления, а не через полгода в отчёте.
"""

import ast
import pathlib

import pytest


#: Вызовы клиента панели, которыми бот меняет её состояние.
_PANEL_WRITE_CALLS = frozenset({'create_user', 'update_user'})

#: Кому можно звать их напрямую.
_ALLOWED = {
    # Сам сервис синхронизации — здесь правила и живут.
    'app/services/panel_sync/writer.py',
    # Клиент панели: это и есть обёртка над HTTP.
    'app/external/remnawave_api.py',
    # Согласователь грейса ведёт двухфазные переходы со своей блокировкой и
    # сверкой результата, работая не с подпиской, а со своим снимком. Правила
    # даты и «жива ли подписка» он берёт у сервиса, а запись оставляет себе:
    # push_subscription этим переходам не адрес.
    'app/services/grace_access_runtime.py',
}


def _python_files() -> list[pathlib.Path]:
    return [path for path in sorted(pathlib.Path('app').rglob('*.py')) if str(path) not in _ALLOWED]


def _direct_panel_writes(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PANEL_WRITE_CALLS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {'api', 'client'}
    ]


@pytest.mark.parametrize('path', _python_files(), ids=str)
def test_nobody_writes_to_the_panel_directly(path):
    lines = _direct_panel_writes(path)

    assert not lines, (
        f'{path}: запись в панель мимо app/services/panel_sync — строки {lines}. '
        f'Нужна подписка целиком — push_subscription; одно-два поля — only_fields; '
        f'карточка аккаунта — patch_panel_account; сквады тарифа — patch_panel_squads.'
    )


def test_grace_is_still_the_only_exception_that_needs_one():
    """Переведут грейс на сервис — убрать его из списка, иначе тот начнёт врать."""
    grace = pathlib.Path('app/services/grace_access_runtime.py')

    assert _direct_panel_writes(grace), (
        'согласователь грейса больше не пишет в панель напрямую — уберите его из списка исключений'
    )
