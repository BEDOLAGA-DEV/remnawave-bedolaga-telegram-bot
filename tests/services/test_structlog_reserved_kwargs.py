"""``event=`` — зарезервированное имя structlog, и вызов с ним падает.

``logger.info('сообщение', event=x)`` даёт ``TypeError: got multiple values for
argument 'event'``: сообщение уже передано первым позиционным аргументом под тем
же именем. Ошибка не ловится ни ruff, ни импортом — только исполнением той самой
ветки, а ветки эти обычно редкие: «не настроено», «не найдено», обработчик
исключения. То есть падает не там, где тестируют, и подменяет собой настоящую
ошибку.

Проверка идёт по AST всего ``app/``, а не по одному файлу: за время жизни проекта
такой вызов появлялся уже трижды, каждый раз в новом месте.
"""

import ast
import pathlib

import pytest


APP = pathlib.Path(__file__).resolve().parents[2] / 'app'

_LOG_METHODS = frozenset({'debug', 'info', 'warning', 'warn', 'error', 'exception', 'critical', 'msg'})
# 'event' занято под само сообщение, и вызов с ним ПАДАЕТ.
#
# Проверяется только оно. 'level' и 'timestamp' конвейер тоже перезаписывает
# (add_log_level, TimeStamper в app/logging_config.py), но там значение молча
# теряется, а не роняет обработчик, и живых вызовов с 'level=' в проекте
# полтора десятка — они предмет отдельной уборки, а не этого сторожа.
_RESERVED = frozenset({'event'})


def _looks_like_logger(target: ast.AST) -> bool:
    """Похож ли получатель вызова на логгер.

    Кроме имени со словом log принимается цепочка ``structlog.get_logger(...)``
    и ``logging.getLogger(...)``: без неё вызов вида
    ``structlog.get_logger(__name__).warning('m', event=x)`` сторож пропускал —
    у такого получателя нет ни ``id``, ни ``attr``, и проверка по имени
    отбрасывала его молча.
    """
    if isinstance(target, ast.Call):
        func = target.func
        factory = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
        return str(factory).lower() in {'get_logger', 'getlogger', 'bind'}

    name = target.id if isinstance(target, ast.Name) else getattr(target, 'attr', '')
    return 'log' in str(name).lower() or str(name).lower() in {'audit', 'journal'}


def _log_calls_with_reserved_kwargs(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _LOG_METHODS:
            continue

        if not _looks_like_logger(node.func.value):
            continue

        for keyword in node.keywords:
            if keyword.arg in _RESERVED:
                yield node.lineno, keyword.arg


def _sources():
    return sorted(APP.rglob('*.py'))


def test_no_reserved_kwargs_in_log_calls():
    offenders = []
    for path in _sources():
        # Вендоренный код не наш и живёт по своим правилам.
        if 'lib/nalogo' in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError:  # pragma: no cover - синтаксис ловит отдельный прогон
            continue
        for lineno, arg in _log_calls_with_reserved_kwargs(tree):
            offenders.append(f'{path.relative_to(APP.parent)}:{lineno} — {arg}=')

    assert offenders == [], 'structlog занимает эти имена, вызов упадёт TypeError:\n' + '\n'.join(offenders)


def test_guard_detects_a_planted_call():
    """Сторож обязан быть чувствительным, иначе он молча зелёный."""
    tree = ast.parse("logger.info('msg', event=x)\nlog.warning('m', other=1)\n")
    assert list(_log_calls_with_reserved_kwargs(tree)) == [(1, 'event')]


@pytest.mark.parametrize('method', ['debug', 'info', 'warning', 'error', 'critical'])
def test_reserved_kwarg_really_raises(method):
    """Не теория: такой вызов действительно падает на настоящем structlog.

    Раньше здесь параметризовались строки кода, но тело их игнорировало и оба
    прогона исполняли один и тот же вызов — параметризация была декоративной.
    Теперь перебираются реальные методы логгера, и каждый вызывается.
    """
    import structlog

    logger = structlog.get_logger('test')
    with pytest.raises(TypeError, match='event'):
        getattr(logger, method)('msg', event='x')


@pytest.mark.parametrize(
    'source',
    [
        "logger.info('m', event=1)",
        "self.logger.error('m', event=1)",
        "audit.warning('m', event=1)",
        "structlog.get_logger(__name__).warning('m', event=1)",
        "logging.getLogger(__name__).info('m', event=1)",
        "log.bind(a=1).info('m', event=1)",
    ],
)
def test_guard_sees_every_logger_shape(source):
    """Сторож обязан узнавать логгер во всех формах, которые встречаются в коде.

    Формы с фабрикой в цепочке и с логгером, в имени которого нет «log», он
    пропускал: у такого получателя нет ни ``id``, ни ``attr``.
    """
    assert list(_log_calls_with_reserved_kwargs(ast.parse(source))) == [(1, 'event')], source


@pytest.mark.parametrize('source', ["queue.info('m', event=1)", "logger.info('m', other=1)", "d['event'] = 1"])
def test_guard_does_not_fire_on_unrelated_code(source):
    """И не должен срабатывать на том, что логгером не является."""
    assert list(_log_calls_with_reserved_kwargs(ast.parse(source))) == []
