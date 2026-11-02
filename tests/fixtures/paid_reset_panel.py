"""Functional reset ACK for daily tests that otherwise stub canonical panel sync."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace


class PaidResetPanelMixin:
    is_configured = True

    def init_reset_panel(self):
        self.reset_calls = []
        self.reset_panel_user = SimpleNamespace(
            id=9001,
            short_uuid='masked-daily',
            status='ACTIVE',
            last_traffic_reset_at=datetime.now(UTC) - timedelta(days=1),
            last_traffic_reset_at_known=True,
            user_traffic=object(),
            used_traffic_bytes=90 * 1024**3,
            traffic_limit_bytes=100 * 1024**3,
        )

    @asynccontextmanager
    async def get_api_client(self):
        yield self

    async def get_user_by_id(self, panel_id):
        assert panel_id == 9001
        return self.reset_panel_user

    async def reset_user_traffic(self, panel_id, *, enrich=True):
        assert panel_id == 9001 and enrich is False
        self.reset_calls.append(panel_id)
        self.reset_panel_user.used_traffic_bytes = 0
        self.reset_panel_user.last_traffic_reset_at = datetime.now(UTC)
        return self.reset_panel_user

    async def enable_user(self, panel_id):
        assert panel_id == 9001
        if hasattr(self, 'enabled'):
            self.enabled.append(panel_id)
        self.reset_panel_user.status = 'ACTIVE'
        return self.reset_panel_user
