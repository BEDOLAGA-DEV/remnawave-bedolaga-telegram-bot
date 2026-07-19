from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0092'
down_revision: Union[str, None] = '0091'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# One-time backfill: mark existing VK/Yandex OAuth users' emails as verified.
#
# These users registered via OAuth and own a provider-attested email, but were
# persisted with email_verified=False before that flag started being set True.
# The notification gate (app/services/notification_delivery_service.py) only
# sends email when email_verified is True, so these users silently received no
# notification emails even after the bot was updated (the code fix only affects
# NEW registrations; existing rows keep the stale flag).
#
# Scope is deliberately limited to VK/Yandex: their email_verification_source
# ('oauth_vk' / 'oauth_yandex') is NOT in TRUSTED_EMAIL_VERIFICATION_SOURCES, so
# flipping email_verified cannot grant admin/Superadmin via an ADMIN_EMAILS match
# (unlike trusted providers such as Google/Discord). Telegram users are untouched
# (auth_type filter), and already-verified rows are skipped.

_BACKFILL_SQL = sa.text(
    """
    UPDATE users
    SET email_verified = true,
        email_verified_at = COALESCE(email_verified_at, CURRENT_TIMESTAMP),
        email_verification_source = COALESCE(email_verification_source, 'oauth_' || auth_type)
    WHERE auth_type IN ('vk', 'yandex')
      AND email IS NOT NULL
      AND email_verified = false
    """
)


def upgrade() -> None:
    op.execute(_BACKFILL_SQL)


def downgrade() -> None:
    # Pure data backfill — not safely reversible (we cannot distinguish rows
    # flipped here from legitimately verified ones), so downgrade is a no-op.
    pass
