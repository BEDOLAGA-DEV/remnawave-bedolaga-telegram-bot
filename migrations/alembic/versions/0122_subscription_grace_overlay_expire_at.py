"""дата оверлея грейса на подписке

Revision ID: 0122
Revises: 0121
Create Date: 2026-09-15

Пока грейс открыт, в панели стоит его оверлей: ACTIVE до «конца грейса», сквад
грейса, лимит «расход + квота». Признак ``grace_session_open`` защищает импорт
«панель — истина», пока сессия открыта. Но снимок панели, снятый при открытой
сессии, может обрабатываться уже после её закрытия — досрочный откат, конфликт,
слив (стенд 2026-09-15, ревью): признак уже снят, дата в панели — не хвост, и
оверлей переносился бы в подписку.

Колонка хранит дату оверлея последней сессии. Её пишет хранилище в той же
транзакции, что и саму сессию, до отправки оверлея в панель, и не стирает при
закрытии: снимок с этой датой — всегда оверлей, а не продление. Заполняется по
последней сессии каждой подписки.
"""

import json
from datetime import datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0122'
down_revision: Union[str, None] = '0121'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def _overlay_expire_at(raw) -> datetime | None:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict) or not data.get('expire_at'):
        return None
    try:
        return datetime.fromisoformat(str(data['expire_at']).replace('Z', '+00:00'))
    except ValueError:
        return None


def upgrade() -> None:
    if not _has_column('subscriptions', 'grace_overlay_expire_at'):
        op.add_column(
            'subscriptions',
            sa.Column('grace_overlay_expire_at', sa.DateTime(timezone=True), nullable=True),
        )
    bind = op.get_bind()
    if 'grace_access_sessions' not in sa.inspect(bind).get_table_names():
        return
    # Последняя сессия каждой подписки — разбор JSON в Python, чтобы не зависеть от
    # диалекта (PostgreSQL в проде, SQLite у части установок).
    rows = bind.execute(
        sa.text('SELECT subscription_id, overlay, started_at FROM grace_access_sessions ORDER BY started_at')
    ).fetchall()
    latest: dict[int, datetime] = {}
    for subscription_id, overlay, _started_at in rows:
        expire_at = _overlay_expire_at(overlay)
        if expire_at is not None:
            latest[int(subscription_id)] = expire_at
    for subscription_id, expire_at in latest.items():
        bind.execute(
            sa.text('UPDATE subscriptions SET grace_overlay_expire_at = :expire_at WHERE id = :id'),
            {'expire_at': expire_at, 'id': subscription_id},
        )


def downgrade() -> None:
    if _has_column('subscriptions', 'grace_overlay_expire_at'):
        op.drop_column('subscriptions', 'grace_overlay_expire_at')
