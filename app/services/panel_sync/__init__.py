"""Синхронизация бот ↔ панель Remnawave.

Единственное место, где живут правила: жива ли подписка, что отправлять в панель,
как найти там аккаунт, что писать обратно в базу. До этого пакета правила жили
копиями в тринадцати местах, и каждое расхождение между копиями рано или поздно
всплывало отдельным багом — то дата окончания, то снятая блокировка.

Прямые вызовы ``api.create_user``/``api.update_user`` за пределами пакета
запрещены, сторож — ``tests/services/panel_sync/test_no_bypass.py``.
"""

from app.services.panel_sync.expiry import panel_expire_at, stale_panel_expire_at
from app.services.panel_sync.identity import PanelIdentity, resolve_panel_identity
from app.services.panel_sync.liveness import is_subscription_expired, is_subscription_live
from app.services.panel_sync.payload import PanelPayload, build_panel_payload
from app.services.panel_sync.projection import (
    ADMIN_PULL,
    BULK_SNAPSHOT,
    ROUTINE,
    WEBHOOK,
    PanelSnapshot,
    ProjectionPolicy,
    panel_status_for_new_subscription,
    project_onto_subscription,
    read_panel_user,
)
from app.services.panel_sync.runner import SyncStats, push_all_subscriptions
from app.services.panel_sync.tags import normalize_panel_tag, resolve_panel_user_tag
from app.services.panel_sync.writer import (
    PanelWriteResult,
    patch_panel_account,
    patch_panel_squads,
    push_subscription,
)


__all__ = [
    'ADMIN_PULL',
    'BULK_SNAPSHOT',
    'ROUTINE',
    'WEBHOOK',
    'PanelIdentity',
    'PanelPayload',
    'PanelSnapshot',
    'PanelWriteResult',
    'ProjectionPolicy',
    'SyncStats',
    'build_panel_payload',
    'is_subscription_expired',
    'is_subscription_live',
    'panel_expire_at',
    'panel_status_for_new_subscription',
    'patch_panel_account',
    'patch_panel_squads',
    'project_onto_subscription',
    'push_all_subscriptions',
    'push_subscription',
    'read_panel_user',
    'resolve_panel_identity',
    'stale_panel_expire_at',
]
