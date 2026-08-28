"""Вырезание тегов не должно перебирать хвост заново с каждой позиции.

CodeQL (py/polynomial-redos, high) показал это на `app/utils/rich_notify.py`:
шаблон `<[^>]+>` на строке из одних `<` даёт квадратичное время, а текст
уведомления приходит от пользователя. Тот же шаблон стоял ещё в шести местах,
включая путь, через который проходит каждое исходящее сообщение бота.

Проверяется и семантика: `[^<>]` не только быстрее, но и правильнее — `1 < 2`
остаётся текстом, а не съедается как незакрытый тег.
"""

import re
import time
from pathlib import Path

import pytest


APP_ROOT = Path(__file__).resolve().parents[2] / 'app'

# Модули, где вырезание тегов работает с текстом, который так или иначе пришёл
# снаружи. Список ведётся руками: проверка ниже всё равно обходит весь app/.
LINEAR_PATTERN = r'<[^<>]+>'


def _python_sources() -> list[Path]:
    return [path for path in APP_ROOT.rglob('*.py') if '__pycache__' not in path.parts]


def test_no_quadratic_tag_pattern_remains():
    """Шаблон `<[^>]+>` не должен вернуться ни в один модуль."""
    offenders = [
        str(path.relative_to(APP_ROOT.parent))
        for path in _python_sources()
        if '<[^>]+>' in path.read_text(encoding='utf-8')
    ]

    assert offenders == [], (
        'Квадратичное вырезание тегов вернулось. Нужен <[^<>]+>: на строке из '
        f'одних "<" вариант с [^>] перебирает хвост заново с каждой позиции. {offenders}'
    )


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('<b>жирный</b>', 'жирный'),
        ('<a href="https://example.com">ссылка</a>', 'ссылка'),
        # Главное отличие: [^>] съедал бы «< 2</b>» целиком как один тег.
        ('<b>1 < 2</b>', '1 < 2'),
        ('без тегов', 'без тегов'),
    ],
)
def test_stripping_keeps_text_and_bare_angle_brackets(source, expected):
    assert re.sub(LINEAR_PATTERN, '', source) == expected


def test_pathological_input_stays_fast():
    """Строка из одних «<» — ровно тот вход, на котором старый шаблон вставал."""
    payload = '<' * 40_000

    start = time.perf_counter()
    re.sub(LINEAR_PATTERN, '', payload)
    elapsed = time.perf_counter() - start

    # Старый шаблон на этом входе тратил доли секунды и рос квадратично;
    # запас взят большой, чтобы тест не мигал на нагруженной машине.
    assert elapsed < 0.1, f'вырезание тегов заняло {elapsed:.3f}s — шаблон снова квадратичный'


def test_visible_length_uses_the_linear_pattern():
    """Функция, на которую указал CodeQL, считает длину тем же способом."""
    from app.utils.rich_notify import _visible_length

    assert _visible_length('<b>Тариф</b>') == 5
    assert _visible_length('1 < 2') == 5


def test_html_validator_stays_fast_on_unclosed_tags():
    """Проверка HTML правовых страниц: их длина из кабинета ничем не ограничена.

    Шаблон `[^>]*` после имени тега перебирал хвост заново с каждого «<»:
    на 80 КБ из «<a» разбор структуры занимал 10 секунд заблокированного CPU.
    """
    from app.utils.validators import validate_html_tags

    payload = '<a' * 40_000

    start = time.perf_counter()
    validate_html_tags(payload)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5, f'проверка HTML заняла {elapsed:.3f}s — шаблон снова квадратичный'


@pytest.mark.parametrize(
    ('source', 'valid'),
    [
        ('<b>жирный</b>', True),
        ('<a href="https://example.com">ссылка</a>', True),
        ('<b>1 < 2</b>', True),
        ('<nosuchtag>текст</nosuchtag>', False),
    ],
)
def test_html_validator_verdicts_unchanged(source, valid):
    """Ускорение не должно менять вердикты на обычной разметке."""
    from app.utils.validators import validate_html_tags

    assert validate_html_tags(source)[0] is valid
