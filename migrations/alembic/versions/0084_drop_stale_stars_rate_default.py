"""drop stale TELEGRAM_STARS_RATE_RUB=1.3 system setting

Existing production installs that ever wrote the previous default
(``1.3``) via the admin bot UI have the value pinned in the
``system_settings`` KV table. ``system_settings_service._apply_to_settings``
overwrites the new code default at startup, so the rate fix in commit
1f32fed0 never kicks in for them.

This migration deletes the row IFF its stored value is literally
``1.3`` (or ``1.79`` — the value that was in ``.env.example``). Any
custom operator value (e.g. ``1.5``, ``2.0``, ``0.95``) is left alone
— we only clean up rows that were obviously copies of the old default.

Revision ID: 0084
Revises: 0083
Create Date: 2026-05-16
"""

from typing import Sequence, Union

from alembic import op


revision: str = '0084'
down_revision: Union[str, None] = '0083'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Literal SQL is safe here — the stale values are hardcoded
    # constants, no user input. Works identically on PostgreSQL and
    # SQLite (the two supported backends).
    op.execute("DELETE FROM system_settings WHERE key = 'TELEGRAM_STARS_RATE_RUB' AND value IN ('1.3', '1.79')")


def downgrade() -> None:
    # No-op. Re-inserting a stale default we just removed would resurrect
    # the rounding bug — leave the row absent so the new code default
    # applies. Operators can re-set the rate manually via admin UI if
    # they actually want a non-default value.
    pass
