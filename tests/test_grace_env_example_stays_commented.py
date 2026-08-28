"""Ключи grace в .env.example обязаны оставаться закомментированными.

Ключ, физически присутствующий в ``.env``, попадает в ``ENV_OVERRIDE_KEYS``:
файл перекрывает базу, и настройка перестаёт применяться из админки. Раздел
«Grace-доступ» в кабинете открывался бы с замком на каждом поле, а PUT отвечал
бы 409 — у операторов, которые просто скопировали пример.

Заодно сверяются сами значения в комментариях: пример, разошедшийся с
дефолтами кода, документирует конфигурацию, которой ни у кого нет.
"""

import re
from pathlib import Path

from app.config import Settings


ENV_EXAMPLE = Path(__file__).resolve().parents[1] / '.env.example'

BOOL_TEXT = {True: 'true', False: 'false'}


def _grace_lines() -> list[str]:
    return [line.strip() for line in ENV_EXAMPLE.read_text(encoding='utf-8').splitlines() if 'GRACE_ACCESS_' in line]


def test_no_grace_key_is_active_in_the_example():
    active = [line for line in _grace_lines() if re.match(r'^GRACE_ACCESS_[A-Z_]+=', line)]

    assert active == [], (
        f'Эти ключи закрепят значение за .env и сделают раздел grace-доступа в кабинете нередактируемым: {active}'
    )


def test_commented_values_match_the_code_defaults():
    documented: dict[str, str] = {}
    for line in _grace_lines():
        match = re.match(r'^#\s*(GRACE_ACCESS_[A-Z_]+)=(.*)$', line)
        if match:
            documented[match.group(1)] = match.group(2).strip()

    assert documented, 'Блок grace исчез из .env.example — обновите тест вместе с ним'

    fields = Settings.model_fields
    for key, shown in documented.items():
        default = fields[key].default
        expected = BOOL_TEXT[default] if isinstance(default, bool) else str(default)
        assert shown == expected, f'{key}: в примере {shown!r}, в коде {expected!r}'
