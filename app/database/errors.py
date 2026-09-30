"""Распознавание ошибок SQLAlchemy, форма которых зависит от версии."""

from sqlalchemy.exc import MissingGreenlet


def is_missing_greenlet(exc: BaseException) -> bool:
    """Ленивая подгрузка вне greenlet: обращение к незагруженному атрибуту в async-коде.

    SQLAlchemy 2.0 бросает ``MissingGreenlet`` как есть, 2.1 заворачивает её в
    ``StatementError`` (исходная — в ``.orig``). ``except MissingGreenlet`` на 2.1
    её уже не ловит — проверять нужно обе формы.
    """
    return isinstance(exc, MissingGreenlet) or isinstance(getattr(exc, 'orig', None), MissingGreenlet)
