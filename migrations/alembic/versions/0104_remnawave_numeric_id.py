"""add numeric remnawave_id identity columns (Remnawave 3.0.0)

Remnawave 3.0.0 removed the ``uuid`` field from ``UsersSchema`` entirely: a
panel user is now identified only by a numeric ``id``.  This revision adds the
replacement columns next to the historical uuid ones.  Nothing is dropped —
``remnawave_uuid`` is kept as audit data so an operator can reconstruct what a
row used to point at if the backfill leaves it unresolved.

Three constraints shaped this revision:

1. ``0001`` builds fresh schemas via ``Base.metadata.create_all(checkfirst=True)``,
   so on a new install these columns already exist by the time we run — every
   step is inspector-guarded.
2. SQLite is a supported backend, so constraint work goes through
   ``batch_alter_table``.
3. ``grace_access_sessions.remnawave_uuid`` is NOT NULL and can no longer be
   populated (the panel does not return a uuid).  It is relaxed to nullable
   here; ``remnawave_id`` stays nullable until the backfill has run, and the
   NOT NULL flip is a separate later revision.

Revision ID: 0104
Revises: 0103
Create Date: 2026-08-01

"""

import sqlalchemy as sa
from alembic import op


# Стиль как у соседних ревизий: alembic читает эти имена по соглашению.
revision = '0104'
down_revision = '0103'
branch_labels = None
depends_on = None


_SUBSCRIPTION_ID_NOT_NULL = 'remnawave_id IS NOT NULL'


def _columns(inspector: sa.Inspector, table: str) -> dict[str, dict]:
    return {column['name']: column for column in inspector.get_columns(table)}


def _index_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {str(item['name']) for item in inspector.get_indexes(table) if item.get('name')}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # --- users --------------------------------------------------------------
    if 'users' in tables and 'remnawave_id' not in _columns(inspector, 'users'):
        op.add_column('users', sa.Column('remnawave_id', sa.BigInteger(), nullable=True))

    inspector = sa.inspect(bind)
    if 'users' in tables and 'ix_users_remnawave_id' not in _index_names(inspector, 'users'):
        # unique=True doubles as the uniqueness guarantee for users.remnawave_id;
        # NULLs do not collide in either backend.
        op.create_index('ix_users_remnawave_id', 'users', ['remnawave_id'], unique=True)

    # --- subscriptions ------------------------------------------------------
    if 'subscriptions' in tables:
        inspector = sa.inspect(bind)
        if 'remnawave_id' not in _columns(inspector, 'subscriptions'):
            op.add_column('subscriptions', sa.Column('remnawave_id', sa.BigInteger(), nullable=True))

        inspector = sa.inspect(bind)
        subscription_indexes = _index_names(inspector, 'subscriptions')
        if 'uq_subscriptions_remnawave_id' not in subscription_indexes:
            op.create_index(
                'uq_subscriptions_remnawave_id',
                'subscriptions',
                ['remnawave_id'],
                unique=True,
                postgresql_where=sa.text(_SUBSCRIPTION_ID_NOT_NULL),
                sqlite_where=sa.text(_SUBSCRIPTION_ID_NOT_NULL),
            )
        if 'ix_subscriptions_remnawave_short_uuid' not in subscription_indexes:
            op.create_index(
                'ix_subscriptions_remnawave_short_uuid',
                'subscriptions',
                ['remnawave_short_uuid'],
                unique=False,
            )

    # --- grace_access_sessions ---------------------------------------------
    if 'grace_access_sessions' in tables:
        inspector = sa.inspect(bind)
        grace_columns = _columns(inspector, 'grace_access_sessions')

        if 'remnawave_id' not in grace_columns:
            op.add_column('grace_access_sessions', sa.Column('remnawave_id', sa.BigInteger(), nullable=True))

        # The panel can no longer supply a uuid, so new sessions must be allowed
        # to leave the historical column empty.
        if grace_columns.get('remnawave_uuid', {}).get('nullable') is False:
            with op.batch_alter_table('grace_access_sessions') as batch:
                batch.alter_column(
                    'remnawave_uuid',
                    existing_type=sa.String(length=255),
                    nullable=True,
                )

        inspector = sa.inspect(bind)
        if 'ix_grace_access_sessions_remnawave_id' not in _index_names(inspector, 'grace_access_sessions'):
            op.create_index(
                'ix_grace_access_sessions_remnawave_id',
                'grace_access_sessions',
                ['remnawave_id'],
                unique=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'grace_access_sessions' in tables:
        grace_indexes = _index_names(inspector, 'grace_access_sessions')
        if 'ix_grace_access_sessions_remnawave_id' in grace_indexes:
            op.drop_index('ix_grace_access_sessions_remnawave_id', table_name='grace_access_sessions')

        # Restoring NOT NULL is only possible when no row lost its historical
        # uuid.  Sessions created after the upgrade legitimately have none — the
        # panel stopped returning a uuid — and for those rows ``remnawave_id`` is
        # the ONLY identity they have.  Dropping it below would destroy that, so
        # refuse the downgrade instead of quietly mangling the table.
        null_uuids = bind.execute(
            sa.text('SELECT COUNT(*) FROM grace_access_sessions WHERE remnawave_uuid IS NULL')
        ).scalar_one()
        if null_uuids:
            raise RuntimeError(
                f'Cannot downgrade 0104: {null_uuids} grace_access_sessions row(s) have no historical '
                'remnawave_uuid, so dropping remnawave_id would erase their only panel identity. '
                'Close/restore those sessions (or delete the completed ones) and retry.'
            )
        with op.batch_alter_table('grace_access_sessions') as batch:
            batch.alter_column(
                'remnawave_uuid',
                existing_type=sa.String(length=255),
                nullable=False,
            )

        inspector = sa.inspect(bind)
        if 'remnawave_id' in _columns(inspector, 'grace_access_sessions'):
            op.drop_column('grace_access_sessions', 'remnawave_id')

    if 'subscriptions' in tables:
        inspector = sa.inspect(bind)
        subscription_indexes = _index_names(inspector, 'subscriptions')
        if 'ix_subscriptions_remnawave_short_uuid' in subscription_indexes:
            op.drop_index('ix_subscriptions_remnawave_short_uuid', table_name='subscriptions')
        if 'uq_subscriptions_remnawave_id' in subscription_indexes:
            op.drop_index('uq_subscriptions_remnawave_id', table_name='subscriptions')
        inspector = sa.inspect(bind)
        if 'remnawave_id' in _columns(inspector, 'subscriptions'):
            op.drop_column('subscriptions', 'remnawave_id')

    if 'users' in tables:
        inspector = sa.inspect(bind)
        if 'ix_users_remnawave_id' in _index_names(inspector, 'users'):
            op.drop_index('ix_users_remnawave_id', table_name='users')
        inspector = sa.inspect(bind)
        if 'remnawave_id' in _columns(inspector, 'users'):
            op.drop_column('users', 'remnawave_id')
