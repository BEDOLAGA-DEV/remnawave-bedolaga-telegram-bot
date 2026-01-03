from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8f7c6d5e4b3"
down_revision: Union[str, None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "promocodes",
        sa.Column("discount_type", sa.String(20), nullable=True),
    )
    op.add_column(
        "promocodes",
        sa.Column("discount_value", sa.Integer(), nullable=True, server_default="0"),
    )
    op.add_column(
        "promocodes",
        sa.Column("discount_applies_to", sa.String(20), nullable=True, server_default="all"),
    )


def downgrade() -> None:
    op.drop_column("promocodes", "discount_applies_to")
    op.drop_column("promocodes", "discount_value")
    op.drop_column("promocodes", "discount_type")
