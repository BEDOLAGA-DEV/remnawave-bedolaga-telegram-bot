from sqlalchemy.exc import IntegrityError, MissingGreenlet, StatementError

from app.database.errors import is_missing_greenlet


def test_bare_missing_greenlet_sqlalchemy_2_0() -> None:
    assert is_missing_greenlet(MissingGreenlet('greenlet_spawn has not been called'))


def test_missing_greenlet_wrapped_in_statement_error_sqlalchemy_2_1() -> None:
    orig = MissingGreenlet('greenlet_spawn has not been called')
    assert is_missing_greenlet(StatementError(str(orig), 'SELECT 1', {}, orig))


def test_other_database_errors_are_not_missing_greenlet() -> None:
    assert not is_missing_greenlet(StatementError('bad', 'SELECT 1', {}, ValueError('x')))
    assert not is_missing_greenlet(IntegrityError('INSERT', {}, Exception('dup')))
    assert not is_missing_greenlet(RuntimeError('x'))
