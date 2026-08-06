"""require positive numeric identity for Grace sessions

Revision ID: 0105
Revises: 0104
"""

import sqlalchemy as sa
from alembic import op


revision = '0105'
down_revision = '0104'
branch_labels = None
depends_on = None

_CHECK_NAME = 'ck_grace_access_sessions_remnawave_id_positive'


def _columns(inspector: sa.Inspector, table: str) -> dict[str, dict]:
    return {column['name']: column for column in inspector.get_columns(table)}


def _check_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {str(item.get('name')) for item in inspector.get_check_constraints(table) if item.get('name')}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if 'grace_access_sessions' not in tables:
        return

    columns = _columns(inspector, 'grace_access_sessions')
    if 'remnawave_id' not in columns:
        raise RuntimeError(
            '0105 requires 0104 first: grace_access_sessions.remnawave_id is missing. '
            'Run alembic upgrade 0104 before retrying.'
        )

    invalid = bind.execute(
        sa.text('SELECT COUNT(*) FROM grace_access_sessions WHERE remnawave_id IS NULL OR remnawave_id <= 0')
    ).scalar_one()
    if invalid:
        raise RuntimeError(
            f'{invalid} Grace session row(s) have NULL or non-positive remnawave_id. '
            'Run "make backfill-remnawave-ids" to inspect, then '
            '"make backfill-remnawave-ids-apply" and retry migration 0105.'
        )

    if _CHECK_NAME not in _check_names(inspector, 'grace_access_sessions'):
        with op.batch_alter_table('grace_access_sessions') as batch:
            batch.create_check_constraint(_CHECK_NAME, 'remnawave_id > 0')

    inspector = sa.inspect(bind)
    if columns['remnawave_id'].get('nullable', True):
        with op.batch_alter_table('grace_access_sessions') as batch:
            batch.alter_column(
                'remnawave_id',
                existing_type=sa.BigInteger(),
                nullable=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'grace_access_sessions' not in inspector.get_table_names():
        return

    if _CHECK_NAME in _check_names(inspector, 'grace_access_sessions'):
        with op.batch_alter_table('grace_access_sessions') as batch:
            batch.drop_constraint(_CHECK_NAME, type_='check')
    columns = _columns(sa.inspect(bind), 'grace_access_sessions')
    if 'remnawave_id' in columns and not columns['remnawave_id'].get('nullable', True):
        # Downgrade intentionally restores only nullable numeric id. It does
        # not invent or restore a UUID that Remnawave 3.x no longer returns.
        with op.batch_alter_table('grace_access_sessions') as batch:
            batch.alter_column(
                'remnawave_id',
                existing_type=sa.BigInteger(),
                nullable=True,
            )
