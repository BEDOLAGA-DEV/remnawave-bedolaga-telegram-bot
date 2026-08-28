"""Текст тикета не режется молча (#длинные сообщения не видны целиком).

Бот принимал сообщение тикета до 500 символов, а ответы — до 400, обрезая
остаток без предупреждения: `message_text = message_text[:500]`. Кабинет и
webapi при этом принимают до 4000 (`app/cabinet/schemas/tickets.py`), колонка
`ticket_messages.message_text` — Text без лимита. В итоге и пользователь, и
поддержка видели огрызок сообщения, не зная, что часть текста потеряна.

Тест пинит контракт на уровне AST: интеграционный прогон хендлеров потребовал
бы реальную БД и Telegram-сессию.
"""

from __future__ import annotations

import ast
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[2] / 'app'

# Переменные с пользовательским текстом, которые нельзя резать срезом.
TEXT_VARIABLES = {'message_text', 'reply_text'}

HANDLERS = [
    (APP_DIR / 'handlers' / 'tickets.py', 'handle_ticket_message_input'),
    (APP_DIR / 'handlers' / 'tickets.py', 'handle_ticket_reply'),
    (APP_DIR / 'handlers' / 'admin' / 'tickets.py', 'handle_admin_ticket_reply'),
]


def _find_function(path: Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f'async function {name!r} not found in {path.name}')


def test_handlers_do_not_slice_user_text():
    offenders = []
    for path, func_name in HANDLERS:
        for node in ast.walk(_find_function(path, func_name)):
            if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
                continue
            target = node.value
            if isinstance(target, ast.Name) and target.id in TEXT_VARIABLES:
                offenders.append(f'{path.name}:{node.lineno} режет {target.id}')

    assert not offenders, 'Текст тикета обрезается срезом вместо явного лимита:\n' + '\n'.join(offenders)


def test_handlers_enforce_shared_length_limit():
    for path, func_name in HANDLERS:
        source = ast.dump(_find_function(path, func_name))
        assert 'TICKET_MESSAGE_MAX_LENGTH' in source, (
            f'{path.name}:{func_name} должен проверять длину по общему лимиту TICKET_MESSAGE_MAX_LENGTH'
        )
