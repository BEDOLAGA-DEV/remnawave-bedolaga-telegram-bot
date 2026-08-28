"""Ни одного НОВОГО неопределённого имени в app/.

``F821`` отключён в ``pyproject.toml`` глобально, поэтому забытый импорт не
ловится ни линтером, ни импортом модуля — только исполнением конкретной строки.
За время проекта это уже четвёртый случай подряд: NameError в проде на
``not_referee_directed`` в админской статистике, до него — на ``user.language`` в
условиях реферальной программы, и рядом нашлись ещё три места.

Отключить F821 было осознанным решением: в коде много аннотаций в кавычках и под
``from __future__ import annotations``, и правило шумит на них. Поэтому здесь не
запрет, а храповик: текущее известное множество зафиксировано, всё сверх него —
ошибка. Каждая запись базы проверена вручную и объяснена.
"""

import json
import pathlib
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]

# Имена, уже присутствовавшие на момент установки храповика.
#
# Все, кроме последнего, — АННОТАЦИИ: либо в кавычках, либо в модуле с
# ``from __future__ import annotations``. Такие не вычисляются при исполнении и
# упасть не могут; это проверено импортом каждого модуля.
#
# 'activate_guest_purchase' — НАСТОЯЩИЙ пробел: функции с таким именем нет нигде
# в проекте, и POST /activate/{token} падает NameError на каждом вызове. Чинить
# его импортом нельзя — нужно решение о том, что этот эндпоинт должен делать,
# поэтому он оставлен в базе как известный долг, а не замаскирован.
KNOWN: dict[str, set[str]] = {
    'app/cabinet/routes/landing.py': {'activate_guest_purchase'},
    'app/cabinet/services/email_templates.py': {'NotificationType'},
    'app/handlers/start.py': {'User'},
    'app/services/payment/heleket.py': {'HeleketPayment'},
    'app/services/payment_method_config_service.py': {'User'},
    'app/services/pricing_engine.py': {'PromoGroup'},
}


def _undefined_names() -> dict[str, set[str]]:
    result = subprocess.run(  # noqa: S603
        [sys.executable, '-m', 'ruff', 'check', '--select', 'F821', '--no-cache', '--output-format', 'json', 'app'],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    if result.returncode not in (0, 1):
        pytest.skip(f'ruff недоступен: {result.stderr.strip()[:200]}')

    found: dict[str, set[str]] = {}
    for item in json.loads(result.stdout or '[]'):
        path = str(pathlib.Path(item['filename']).relative_to(ROOT))
        name = item['message'].removeprefix('Undefined name ').strip('`')
        found.setdefault(path, set()).add(name)
    return found


def test_no_new_undefined_names():
    found = _undefined_names()

    new: list[str] = []
    for path, names in sorted(found.items()):
        for name in sorted(names - KNOWN.get(path, set())):
            new.append(f'{path}: {name}')

    assert new == [], (
        'Забытый импорт: F821 в этом проекте отключён, и такой код падает '
        'NameError только в проде, на той самой ветке.\n' + '\n'.join(new)
    )


def test_baseline_does_not_rot():
    """Исправленное имя обязано выпадать из базы, иначе она копит ложь.

    Без этой проверки база превращается в список того, что «когда-то было
    сломано», и следующий забытый импорт в том же файле проходит незамеченным.
    """
    found = _undefined_names()

    stale: list[str] = []
    for path, names in sorted(KNOWN.items()):
        for name in sorted(names - found.get(path, set())):
            stale.append(f'{path}: {name}')

    assert stale == [], 'Эти имена уже определены — уберите их из KNOWN:\n' + '\n'.join(stale)
