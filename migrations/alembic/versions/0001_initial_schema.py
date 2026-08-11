"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-02-18

Creates all tables from SQLAlchemy models via metadata.create_all.
For existing databases, use ``alembic stamp head`` to mark as current.

NOTE: This migration uses create_all(checkfirst=True) which is coupled to
the current state of models.py. Future migrations MUST use explicit
op.create_table() / op.add_column() calls. If you need to bootstrap a
fresh database AND have later migrations, run this migration first,
then apply subsequent migrations normally — checkfirst=True prevents
duplicate table errors.
"""

from typing import Sequence, Union

from alembic import op

from app.database.models import Base

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    raise NotImplementedError(
        'Downgrading the initial schema is not supported. Restore from a database backup instead.'
    )
