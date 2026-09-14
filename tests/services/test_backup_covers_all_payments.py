"""Резервная копия обязана содержать платежи ВСЕХ шлюзов.

Список моделей для выгрузки ведётся руками, и подключение нового шлюза его не
задевает: код работает, тесты зелёные, платежи идут — а в бэкап их таблица не
попадает. Так выпали восемь шлюзов подряд, включая два свежих. Восстановление
из такой копии стирает историю платежей: `_clear_database_tables` чистит
таблицу каскадом по FK на users, а данных для неё в архиве нет.

Цена ошибки — пропавший след движения настоящих денег, и заметить её можно
только при восстановлении, то есть в худший из возможных моментов.
"""

from __future__ import annotations

import pytest

from app.database.models import Base
from app.services.backup_service import backup_service


def _payment_tables() -> set[str]:
    """Таблицы платежей провайдеров: у каждого шлюза своя `<имя>_payments`."""
    return {name for name in Base.metadata.tables if name.endswith('_payments')}


def _exported_tables() -> set[str]:
    return {model.__tablename__ for model in backup_service._get_models_for_backup(True)}


def test_every_payment_table_is_exported() -> None:
    missing = sorted(_payment_tables() - _exported_tables())
    assert not missing, f'таблицы платежей не попадают в резервную копию — восстановление сотрёт их историю: {missing}'


def test_export_list_has_no_phantom_payment_tables() -> None:
    """Обратная сторона: модель в списке, а таблицы уже нет."""
    known = set(Base.metadata.tables)
    phantom = sorted(_exported_tables() - known)
    assert not phantom, f'в списке выгрузки таблицы, которых нет в моделях: {phantom}'


@pytest.mark.parametrize('table', sorted(_payment_tables()))
def test_payment_table_is_cleared_on_restore(table: str) -> None:
    """Восстановление «с заменой» должно чистить и таблицы платежей.

    Сейчас их подчищает TRUNCATE ... CASCADE по внешнему ключу на users, но
    полагаться на это нельзя: у шлюза может не оказаться такого ключа, и тогда
    чужие платежи переживут восстановление и смешаются с восстановленными.
    """
    import inspect as inspect_module

    source = inspect_module.getsource(backup_service._clear_database_tables)
    assert f"'{table}'" in source, (
        f'{table} не перечислена в _clear_database_tables — при восстановлении '
        'её очистка держится только на каскаде по внешнему ключу'
    )
