"""ticket_messages.user_id для админ-ответов = id реального автора (#3029).

Ответ администратора на тикет через кабинет (и через mobile support websocket)
записывался с ``user_id = ticket.user_id`` — id владельца тикета, а не
ответившего сотрудника. ``is_from_admin`` при этом корректен, но при нескольких
сотрудниках поддержки невозможно установить, кто именно отвечал. Бот-путь
(``handlers/admin/tickets.py``) с самого начала пишет id админа — семантика
поля уже была смешанной.

Фикс выравнивает cabinet- и ws-пути по бот-пути:

- ``admin_tickets.reply_to_ticket`` пишет ``admin.id`` (аутентифицированный
  админ доступен через ``require_permission('tickets:reply')``);
- ``support_ws._handle_ticket_reply`` пишет ``session.context.user_id`` для
  admin/support-ролей и владельца — для владельца;
- webapi-путь намеренно НЕ меняется: он аутентифицируется сервисным токеном,
  личности админа там нет.

Отображение стороны сообщения безопасно: все сериализаторы различают стороны
по ``is_from_admin`` (cabinet-схема ``TicketMessageResponse`` поле ``user_id``
вообще не отдаёт), а ws-``authorUserId`` начинает говорить правду.

Тесты пинят контракт на уровне AST — интеграционный прогон потребовал бы
реальную БД и FastAPI-зависимости.
"""

from __future__ import annotations

import ast
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[2] / 'app'


def _find_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f'async function {name!r} not found')


def _ticket_message_calls(func: ast.AsyncFunctionDef) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'TicketMessage'
    ]


def _keyword(call: ast.Call, name: str) -> ast.expr:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    raise AssertionError(f'TicketMessage(...) has no keyword {name!r}')


def test_cabinet_admin_reply_stores_admin_id() -> None:
    """REGRESSION: reply_to_ticket в admin_tickets.py должен писать admin.id,
    а не ticket.user_id — иначе не установить, кто из сотрудников отвечал."""
    source = (APP_DIR / 'cabinet' / 'routes' / 'admin_tickets.py').read_text(encoding='utf-8')
    func = _find_function(ast.parse(source), 'reply_to_ticket')

    calls = _ticket_message_calls(func)
    assert calls, 'reply_to_ticket должен создавать TicketMessage'
    user_id = _keyword(calls[0], 'user_id')

    assert isinstance(user_id, ast.Attribute) and user_id.attr == 'id', (
        'user_id админ-ответа должен быть <admin>.id, а не id владельца тикета'
    )
    assert isinstance(user_id.value, ast.Name) and user_id.value.id == 'admin', (
        'user_id админ-ответа должен браться у аутентифицированного админа '
        '(зависимость require_permission), а не у владельца тикета (#3029)'
    )


def test_support_ws_reply_stores_actor_id_for_admin() -> None:
    """REGRESSION: _handle_ticket_reply в support_ws.py должен писать
    session.context.user_id для admin/support и владельца — для владельца."""
    source = (APP_DIR / 'cabinet' / 'routes' / 'support_ws.py').read_text(encoding='utf-8')
    func = _find_function(ast.parse(source), '_handle_ticket_reply')

    calls = _ticket_message_calls(func)
    assert calls, '_handle_ticket_reply должен создавать TicketMessage'
    user_id = _keyword(calls[0], 'user_id')

    assert isinstance(user_id, ast.IfExp), (
        'user_id должен выбираться условно по is_from_admin: '
        'session.context.user_id для админа, ticket.user_id для владельца'
    )
    assert isinstance(user_id.test, ast.Name) and user_id.test.id == 'is_from_admin'

    body_src = ast.unparse(user_id.body)
    assert 'session.context.user_id' in body_src, (
        'для admin/support-ролей автором сообщения должен быть '
        'session.context.user_id (реально ответивший сотрудник, #3029)'
    )
    orelse_src = ast.unparse(user_id.orelse)
    assert 'ticket.user_id' in orelse_src, 'для владельца тикета семантика должна остаться прежней'


def test_bot_admin_reply_still_stores_admin_id() -> None:
    """Пин существующей (корректной) семантики бот-пути: add_message получает
    db_user.id — id админа, вызвавшего обработчик. Cabinet/ws выровнены по ней;
    если бот-путь изменится, смешанная семантика вернётся."""
    source = (APP_DIR / 'handlers' / 'admin' / 'tickets.py').read_text(encoding='utf-8')
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_message'
        ):
            continue
        has_admin_flag = any(
            kw.arg == 'is_from_admin' and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        if not has_admin_flag:
            continue
        args_src = ', '.join(ast.unparse(a) for a in node.args)
        assert 'db_user.id' in args_src, 'бот-путь должен продолжать писать id ответившего админа (db_user.id)'
        return
    raise AssertionError('в handlers/admin/tickets.py не найден admin-вызов add_message')
