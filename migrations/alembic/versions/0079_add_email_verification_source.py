"""add email_verification_source to users + backfill from auth_type

Поле распознаёт каким способом был верифицирован email юзера:
- 'cabinet'        — юзер ввёл OTP-код, отправленный кабинетом
- 'oauth_google'   — Google OIDC userinfo (cryptographically signed)
- 'oauth_discord'  — Discord API verified flag
- 'oauth_vk'       — VK ID (НЕ trusted для admin escalation)
- 'oauth_yandex'   — Yandex (НЕ trusted для admin escalation)
- 'admin_override' — установлено вручную через admin UI
- NULL             — legacy, для совместимости трактуется как 'cabinet'-equivalent

Используется в is_user_admin_by_env: только источники из
TRUSTED_EMAIL_VERIFICATION_SOURCES допускают match с ADMIN_EMAILS.
Таким образом email юзера VK/Yandex остаётся email_verified=True
(чтобы работали recovery, account linking, panel sync), но privilege
escalation через ADMIN_EMAILS закрыта.

Backfill: устанавливаем source на основе users.auth_type для всех
строк где email_verified=True И email_verification_source IS NULL.

Revision ID: 0079
Revises: 0078
Create Date: 2026-05-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0079'
down_revision: Union[str, None] = '0078'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = 'users'
_COLUMN = 'email_verification_source'


def _column_exists(bind: sa.engine.Connection) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return False
    return any(col['name'] == _COLUMN for col in inspector.get_columns(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    if not _column_exists(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(length=32), nullable=True),
        )

    # Backfill для верифицированных юзеров. NULL для unverified, чтобы при
    # последующей верификации проставился актуальный источник.
    # auth_type known values:
    #   'telegram' — нет email-verify flow обычно (email_verified=False)
    #   'email'    — cabinet OTP flow
    #   'google'/'discord'/'vk'/'yandex' — OAuth providers
    bind.execute(
        sa.text(
            """
            UPDATE users
            SET email_verification_source = CASE auth_type
                WHEN 'email'   THEN 'cabinet'
                WHEN 'google'  THEN 'oauth_google'
                WHEN 'discord' THEN 'oauth_discord'
                WHEN 'vk'      THEN 'oauth_vk'
                WHEN 'yandex'  THEN 'oauth_yandex'
                ELSE 'cabinet'
            END
            WHERE email_verified = TRUE
              AND email_verification_source IS NULL
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind):
        op.drop_column(_TABLE, _COLUMN)
