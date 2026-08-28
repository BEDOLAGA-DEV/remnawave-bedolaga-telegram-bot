"""Свежая установка и обновлённая обязаны прийти к одной схеме.

Сверяется вся цепочка миграций реферальных уровней: новая ревизия, забытая в
списке ниже, роняет эти проверки — и это правильный сигнал, а не помеха.

Свежая база создаётся ``Base.metadata.create_all`` по модели, обновлённая —
миграцией. Любое расхождение между ними живёт долго и тихо: autogenerate вечно
показывает фантомную разницу, а запрос, опирающийся на индекс, ведёт себя
по-разному на двух установках одного и того же бота.

Проверяется через SQLite: диалект другой, но состав колонок, индексов и внешних
ключей — то, что расходилось, — от него не зависит.
"""

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, ReferralEarning, ReferralRewardLevel


VERSIONS = pathlib.Path(__file__).resolve().parents[2] / 'migrations/alembic/versions'

# Вся цепочка миграций реферальных уровней, по порядку. Сверять модель с одной
# ревизией нельзя: следующая добавляет колонки, и «расхождение» показывало бы не
# ошибку, а собственную неполноту проверки.
MIGRATIONS = ('0108_referral_reward_levels.py', '0109_referral_level_thresholds.py')


def _load_migrations():
    modules = []
    for name in MIGRATIONS:
        spec = importlib.util.spec_from_file_location(name.split('_')[0], VERSIONS / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


def _fresh_install(path: pathlib.Path):
    """База, созданная по модели, — как на новой установке."""
    engine = sa.create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables['tariffs'],
            ReferralRewardLevel.__table__,
            ReferralEarning.__table__,
        ],
        checkfirst=True,
    )
    return engine


def _upgraded_install(path: pathlib.Path):
    """База в состоянии «до 0108», прогнанная миграцией."""
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE tariffs (id INTEGER PRIMARY KEY, name VARCHAR(100))'))
        conn.execute(
            sa.text(
                'CREATE TABLE referral_earnings ('
                ' id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, referral_id INTEGER NOT NULL,'
                ' amount_kopeks INTEGER NOT NULL, reason VARCHAR(100) NOT NULL,'
                ' referral_transaction_id INTEGER, campaign_id INTEGER, created_at TIMESTAMP)'
            )
        )

    for module in _load_migrations():
        with engine.begin() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                module.upgrade()
    return engine


@pytest.fixture
def both(tmp_path):
    fresh = _fresh_install(tmp_path / 'fresh.db')
    upgraded = _upgraded_install(tmp_path / 'upgraded.db')
    return sa.inspect(fresh), sa.inspect(upgraded)


def test_reward_levels_columns_match(both):
    fresh, upgraded = both
    fresh_cols = {c['name'] for c in fresh.get_columns('referral_reward_levels')}
    upgraded_cols = {c['name'] for c in upgraded.get_columns('referral_reward_levels')}
    assert fresh_cols == upgraded_cols


def test_reward_levels_indexes_match(both):
    """Именно это и расходилось: create_all делает ix_..._id, миграция — не делала."""
    fresh, upgraded = both
    fresh_idx = {i['name'] for i in fresh.get_indexes('referral_reward_levels')}
    upgraded_idx = {i['name'] for i in upgraded.get_indexes('referral_reward_levels')}
    assert fresh_idx == upgraded_idx, (
        f'только в свежей: {fresh_idx - upgraded_idx}, только в обновлённой: {upgraded_idx - fresh_idx}'
    )


def test_new_earning_columns_match(both):
    fresh, upgraded = both
    new = {'reward_type', 'level', 'days_granted', 'tariff_id'}
    fresh_cols = {c['name'] for c in fresh.get_columns('referral_earnings')}
    upgraded_cols = {c['name'] for c in upgraded.get_columns('referral_earnings')}
    assert new <= fresh_cols
    assert new <= upgraded_cols


def test_no_duplicate_tariff_foreign_key(both):
    """Миграция не должна вешать второй FK поверх созданного по модели.

    На свежей установке ограничение безымянное — PostgreSQL называет его сам, и
    сверка по имени его не видит. Без проверки по колонке рядом появлялся второй,
    дублирующий внешний ключ на ту же tariff_id.
    """
    fresh, upgraded = both
    for inspector, label in ((fresh, 'свежая'), (upgraded, 'обновлённая')):
        tariff_fks = [
            fk
            for fk in inspector.get_foreign_keys('referral_earnings')
            if 'tariff_id' in (fk.get('constrained_columns') or [])
        ]
        assert len(tariff_fks) <= 1, f'{label}: внешних ключей на tariff_id {len(tariff_fks)}'


def _shape(inspector, table: str) -> dict[str, tuple[str, bool, str | None]]:
    """Колонка -> (тип, nullable, серверный дефолт).

    Имена колонок совпадать могут, а типы — нет: миграция пишет DDL руками, и
    ``Integer`` вместо ``Boolean`` или пропущенный ``NOT NULL`` там появляется
    незаметно. На свежей установке колонка обязана быть той же самой.
    """
    shape = {}
    for column in inspector.get_columns(table):
        default = column.get('default')
        shape[column['name']] = (
            str(column['type']).upper(),
            bool(column['nullable']),
            None if default is None else str(default).strip('\'" '),
        )
    return shape


# Колонки, которые заводит сама фича. Прежние в referral_earnings сравнивать
# нельзя: «старая» база в этом тесте описана рукописным DDL, и расхождение в
# них говорило бы о фикстуре, а не о миграции.
_ADDED_EARNING_COLUMNS = ('reward_type', 'level', 'days_granted', 'tariff_id')


@pytest.mark.parametrize(
    ('table', 'columns'),
    [('referral_reward_levels', None), ('referral_earnings', _ADDED_EARNING_COLUMNS)],
)
def test_column_shapes_match(both, table, columns):
    fresh, upgraded = both
    fresh_shape, upgraded_shape = _shape(fresh, table), _shape(upgraded, table)
    names = sorted(columns) if columns else sorted(set(fresh_shape) & set(upgraded_shape))

    mismatched = [
        f'{name}: свежая={fresh_shape.get(name)} обновлённая={upgraded_shape.get(name)}'
        for name in names
        if fresh_shape.get(name) != upgraded_shape.get(name)
    ]

    assert mismatched == [], f'{table}: колонки описаны по-разному\n' + '\n'.join(mismatched)


def test_threshold_columns_are_not_nullable(both):
    """Порог и флаг подсчёта читаются напрямую в расчёт награды.

    NULL там означал бы сравнение ``None >= int`` при выборе уровня — падение на
    начислении, а не мягкую деградацию.
    """
    for inspector in both:
        shape = _shape(inspector, 'referral_reward_levels')
        for column in ('required_referrals', 'required_referrals_active_only'):
            assert shape[column][1] is False, f'{column} допускает NULL'
            assert shape[column][2] is not None, f'у {column} нет серверного дефолта'


def test_downgrade_removes_everything_it_added(tmp_path):
    """Откат обязан возвращать базу к исходному виду.

    Иначе повторный upgrade после отката упрётся в уже существующие объекты, и
    установка застрянет между версиями.
    """
    engine = _upgraded_install(tmp_path / 'roundtrip.db')

    for module in reversed(_load_migrations()):
        with engine.begin() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                module.downgrade()

    inspector = sa.inspect(engine)
    assert 'referral_reward_levels' not in inspector.get_table_names()
    remaining = {c['name'] for c in inspector.get_columns('referral_earnings')}
    assert not (remaining & {'reward_type', 'level', 'days_granted', 'tariff_id'}), remaining

    # И снова вверх: миграции обязаны переживать цикл, а не только первый прогон.
    for module in _load_migrations():
        with engine.begin() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                module.upgrade()

    inspector = sa.inspect(engine)
    assert 'referral_reward_levels' in inspector.get_table_names()


def test_upgrade_is_idempotent(tmp_path):
    """Повторный прогон на уже обновлённой базе не должен падать."""
    engine = _upgraded_install(tmp_path / 'twice.db')

    for module in _load_migrations():
        with engine.begin() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                module.upgrade()

    inspector = sa.inspect(engine)
    assert 'referral_reward_levels' in inspector.get_table_names()
