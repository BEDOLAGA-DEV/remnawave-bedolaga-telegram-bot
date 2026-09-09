"""Синхронизация бот ↔ панель Remnawave.

Единственное место, где живут правила: жива ли подписка, что отправлять в панель,
как найти там аккаунт, что писать обратно в базу. До этого пакета правила жили
копиями в тринадцати местах, и каждое расхождение между копиями рано или поздно
всплывало отдельным багом — то дата окончания, то снятая блокировка.

Прямые вызовы ``api.create_user``/``api.update_user`` за пределами пакета
запрещены, сторож — ``tests/services/panel_sync/test_no_bypass.py``.
"""

from app.services.panel_sync.expiry import panel_expire_at, stale_panel_expire_at
from app.services.panel_sync.liveness import is_subscription_live
from app.services.panel_sync.payload import PanelPayload, build_panel_payload


__all__ = [
    'PanelPayload',
    'build_panel_payload',
    'is_subscription_live',
    'panel_expire_at',
    'stale_panel_expire_at',
]
