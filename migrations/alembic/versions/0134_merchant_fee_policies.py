"""Configure dated merchant fees per gateway and sub-method."""

import sqlalchemy as sa
from alembic import op

revision = '0134'
down_revision = '0133'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('payment_method_configs', sa.Column('merchant_fee_policies', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('payment_method_configs', 'merchant_fee_policies')
