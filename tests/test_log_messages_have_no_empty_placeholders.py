"""В сообщениях логов не должно быть следов от вырезанных подстановок.

Миграция на structlog увезла значения в kwargs, а окружавшую их пунктуацию
оставила в тексте: «Создан Platega платеж для пользователя (метод , сумма ₽)»
(issue #3276). Такая строка бесполезна ровно там, где нужна: по журналу грепают
по тексту, а в тексте — дыры. Глазами это не ловится: сообщений тысячи, и
каждое по отдельности выглядит почти нормально.

Тест смотрит не на подстановки, а на их следы — пунктуацию, которая осталась
без значения. Список исключений закрытый: там только осмысленные двоеточия
(заголовок многострочного блока) и намеренные отступы.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


APP = Path(__file__).resolve().parents[1] / 'app'

LOG_METHODS = frozenset({'debug', 'info', 'warning', 'error', 'exception', 'critical'})

SUSPECTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'\(\s*,'), 'пустое значение в начале скобки'),
    (re.compile(r',\s*\)'), 'пустое значение в конце скобки'),
    # Скобки сразу после имени — это вызов в тексте (stop(), get_me()), не пропажа.
    (re.compile(r'(?<![\w.])\(\s*\)'), 'пустые скобки'),
    (re.compile(r'\s,'), 'пробел перед запятой'),
    (re.compile(r'[:=]\s*$'), 'двоеточие или равно в конце'),
    (re.compile(r'[«"]\s*[»"]'), 'пустые кавычки'),
    (re.compile(r'\s{2,}'), 'двойной пробел'),
    (re.compile(r'\s[₽%]\)'), 'единица измерения без числа'),
)

# Осмысленные строки: заголовки многострочных блоков и намеренные отступы.
ALLOWED = frozenset(
    {
        '🔍 DEBUG CALLBACK:',
        '📊 Статистика конверсии из таблицы conversions:',
        '📊 Статистика конверсий:',
        '   ❌ НЕДОСТАТОЧНО СРЕДСТВ!',
        'Webhook сервер настроен:',
        '  - Health check: GET /health',
        '=== НАЧАЛО регистрации обработчиков start.py ===',
        '=== КОНЕЦ регистрации обработчиков start.py ===',
    }
)


def _log_messages() -> list[tuple[str, int, str]]:
    """Первый позиционный аргумент каждого вызова логгера, если это литерал."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(APP.rglob('*.py')):
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError:  # pragma: no cover - битый файл поймает другой тест
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in LOG_METHODS:
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((str(path.relative_to(APP.parent)), node.lineno, first.value))
    return found


def test_scanner_sees_the_codebase() -> None:
    """Страховка от «тест зеленеет, потому что ничего не нашёл»."""
    assert len(_log_messages()) > 1000


def test_no_message_carries_an_empty_placeholder() -> None:
    offenders: list[str] = []
    for rel, line, message in _log_messages():
        if message in ALLOWED:
            continue
        for pattern, why in SUSPECTS:
            if pattern.search(message):
                offenders.append(f'{rel}:{line} [{why}] {message!r}')
                break

    assert not offenders, (
        'в сообщениях логов остались следы вырезанных подстановок — значения '
        'уехали в kwargs, а пунктуация осталась:\n  ' + '\n  '.join(offenders)
    )


@pytest.mark.parametrize(
    'message',
    [
        'Создан Platega платеж для пользователя (метод , сумма ₽)',
        'WATA транзакция в статусе , повторная обработка не требуется',
        'Стоимость новых серверов: ₽/мес × дн./30 = ₽ (скидка ₽)',
        'Синхронизация конкурса : создано , обновлено , пропущено',
    ],
)
def test_scanner_catches_known_shapes(message: str) -> None:
    """Образцы из issue #3276 — проверка, что сторож ловит именно их."""
    assert any(pattern.search(message) for pattern, _ in SUSPECTS), message
