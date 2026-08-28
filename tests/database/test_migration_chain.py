"""Цепочка Alembic должна оставаться линейной.

Бот на старте выполняет ``alembic upgrade head`` (``run_alembic_upgrade``), и
падение миграции по умолчанию фатально. Два PR, независимо взявшие следующий
свободный номер, сливаются в git без конфликта — файлы-то разные, — но дают два
head'а и одинаковые revision id. После такого мержа бот не поднимается вообще:
``upgrade head`` отвечает «Multiple head revisions are present».

Ловится только здесь: конфликта в diff нет, а до прода доезжает как полный
отказ старта.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[2]


def _script_directory() -> ScriptDirectory:
    # Дубль revision id Alembic сообщает предупреждением, а не исключением —
    # молча склеивает ревизии. Поднимаем до ошибки, иначе тест его не заметит.
    with warnings.catch_warnings():
        warnings.simplefilter('error', UserWarning)
        return ScriptDirectory.from_config(Config(str(ROOT / 'alembic.ini')))


def test_single_head() -> None:
    heads = _script_directory().get_heads()

    assert len(heads) == 1, f'Ветвление истории миграций, head-ов {len(heads)}: {heads}'


def test_revision_ids_are_unique() -> None:
    revisions = [script.revision for script in _script_directory().walk_revisions()]

    duplicates = sorted({rev for rev in revisions if revisions.count(rev) > 1})
    assert not duplicates, f'Одинаковые revision id: {duplicates}'


def test_every_revision_reaches_base() -> None:
    """Разрыв в down_revision оставил бы часть миграций неприменёнными."""
    script = _script_directory()
    head = script.get_current_head()

    chain = [rev.revision for rev in script.walk_revisions(base='base', head=head)]

    assert len(chain) == len(list(script.walk_revisions())), 'Есть ревизии вне цепочки от base до head'
