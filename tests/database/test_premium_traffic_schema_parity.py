"""Таблица премиум-трафика из миграции 0120 обязана совпадать с моделью.

Свежая установка получает таблицу по модели, обновлённая — миграцией.
Расхождение типа или nullable между ними живёт тихо и всплывает только на одной
из двух установок.

Отдельно — повторный прогон. Номер этой миграции менялся при слияниях с dev
(0116 → 0117 → 0119 → 0120), и база, где она прошла под прежним номером, после
перенумерации запускает её ещё раз.
"""

import importlib.util
import pathlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import SubscriptionPremiumTraffic


VERSIONS = pathlib.Path(__file__).resolve().parents[2] / 'migrations/alembic/versions'
MIGRATION = '0120_create_subscription_premium_traffic.py'
TABLE = 'subscription_premium_traffic'


def _load_migration():
    spec = importlib.util.spec_from_file_location('m0120', VERSIONS / MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            _load_migration().upgrade()


def _upgraded_engine(path: pathlib.Path) -> sa.Engine:
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE subscriptions (id INTEGER PRIMARY KEY)'))
    _upgrade(engine)
    return engine


def _type_family(column_type) -> str:
    name = type(column_type).__name__.upper()
    for family in ('BIGINT', 'INTEGER', 'VARCHAR', 'BOOLEAN', 'DATETIME'):
        if name.startswith(family):
            return family
    return name


def _model_family(column: sa.Column) -> str:
    column_type = column.type
    # AwareDateTime — TypeDecorator поверх DateTime: сравниваем то, что уходит в базу.
    impl = getattr(column_type, 'impl', column_type)
    if isinstance(impl, sa.BigInteger):
        return 'BIGINT'
    if isinstance(impl, sa.Integer):
        return 'INTEGER'
    if isinstance(impl, sa.String):
        return 'VARCHAR'
    if isinstance(impl, sa.Boolean):
        return 'BOOLEAN'
    if isinstance(impl, sa.DateTime):
        return 'DATETIME'
    return type(impl).__name__.upper()


def test_migration_creates_the_same_columns_as_the_model(tmp_path):
    engine = _upgraded_engine(tmp_path / 'upgraded.sqlite')
    upgraded = {c['name']: c for c in sa.inspect(engine).get_columns(TABLE)}
    model = SubscriptionPremiumTraffic.__table__.c

    assert set(upgraded) == {c.name for c in model}, 'набор колонок миграции и модели разошёлся'
    for column in model:
        reflected = upgraded[column.name]
        # id — первичный ключ: nullable у PK в SQLite отражается по-разному.
        if not column.primary_key:
            assert reflected['nullable'] == column.nullable, column.name
        assert _type_family(reflected['type']) == _model_family(column), column.name
    assert upgraded['squad_uuid']['type'].length == model.squad_uuid.type.length


def test_migration_keeps_the_pair_unique_and_indexes_the_squad(tmp_path):
    """Уникальность пары — защита от гонки воркера и докупки; индекс — для выборки по скваду."""
    inspector = sa.inspect(_upgraded_engine(tmp_path / 'constraints.sqlite'))

    unique = {tuple(u['column_names']) for u in inspector.get_unique_constraints(TABLE)}
    assert ('subscription_id', 'squad_uuid') in unique

    indexed = {tuple(i['column_names']) for i in inspector.get_indexes(TABLE)}
    assert ('squad_uuid',) in indexed


def test_migration_is_idempotent_on_a_database_that_already_has_the_table(tmp_path):
    engine = _upgraded_engine(tmp_path / 'twice.sqlite')
    with engine.begin() as conn:
        conn.execute(sa.text('INSERT INTO subscriptions (id) VALUES (1)'))
        conn.execute(
            sa.text(
                f'INSERT INTO {TABLE} (subscription_id, squad_uuid, period_start_at) '
                "VALUES (1, 'squad', '2026-09-01 00:00:00')"
            )
        )

    _upgrade(engine)  # второй прогон не должен падать на «table already exists»

    with engine.connect() as conn:
        assert conn.execute(sa.text(f'SELECT count(*) FROM {TABLE}')).scalar() == 1, 'повторный прогон потерял данные'
