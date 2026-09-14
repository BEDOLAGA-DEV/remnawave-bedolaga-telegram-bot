"""Колонки тарифа из миграции 0119 обязаны совпадать с моделью.

Свежая установка получает таблицу по модели, обновлённая — миграцией. Тег панели
и дни триала на тарифе появляются одной ревизией; расхождение типа или nullable
между ними живёт тихо и всплывает только на одной из двух установок.
"""

import importlib.util
import pathlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Tariff


VERSIONS = pathlib.Path(__file__).resolve().parents[2] / 'migrations/alembic/versions'
MIGRATION = '0119_tariff_panel_tag_and_trial_days.py'
NEW_COLUMNS = ('panel_tag', 'trial_duration_days')


def _load_migration():
    spec = importlib.util.spec_from_file_location('m0119', VERSIONS / MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgraded_columns(path: pathlib.Path) -> dict[str, sa.Column]:
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE tariffs (id INTEGER PRIMARY KEY, name VARCHAR(255))'))
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            _load_migration().upgrade()
    inspector = sa.inspect(engine)
    return {c['name']: c for c in inspector.get_columns('tariffs')}


def test_model_declares_both_columns():
    for name in NEW_COLUMNS:
        assert name in Tariff.__table__.c, name
    assert isinstance(Tariff.__table__.c.panel_tag.type, sa.String)
    assert Tariff.__table__.c.panel_tag.type.length == 16
    assert Tariff.__table__.c.panel_tag.nullable is True
    assert isinstance(Tariff.__table__.c.trial_duration_days.type, sa.Integer)
    assert Tariff.__table__.c.trial_duration_days.nullable is True


def test_migration_adds_the_same_columns_as_the_model(tmp_path):
    upgraded = _upgraded_columns(tmp_path / 'upgraded.sqlite')

    for name in NEW_COLUMNS:
        assert name in upgraded, f'миграция не добавила {name}'
        model_column = Tariff.__table__.c[name]
        assert upgraded[name]['nullable'] == model_column.nullable, name
        assert (
            type(upgraded[name]['type'])
            .__name__.upper()
            .startswith('VARCHAR' if isinstance(model_column.type, sa.String) else 'INTEGER')
        ), name
    assert upgraded['panel_tag']['type'].length == 16


def test_migration_is_idempotent_on_a_table_that_already_has_the_columns(tmp_path):
    path = tmp_path / 'twice.sqlite'
    _upgraded_columns(path)
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            _load_migration().upgrade()  # второй прогон не должен падать на «column exists»
    assert set(NEW_COLUMNS) <= {c['name'] for c in sa.inspect(engine).get_columns('tariffs')}
