from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class RemnaWaveConnectionStatus(BaseModel):
    status: str
    message: str
    api_url: str | None = None
    status_code: int | None = None
    system_info: dict[str, Any] | None = None


class RemnaWaveStatusResponse(BaseModel):
    is_configured: bool
    configuration_error: str | None = None
    connection: RemnaWaveConnectionStatus | None = None


class RemnaWaveNode(BaseModel):
    uuid: str
    name: str
    address: str
    country_code: str | None = None
    is_connected: bool
    is_disabled: bool
    is_node_online: bool
    is_xray_running: bool
    users_online: int = 0
    traffic_used_bytes: int | None = None
    traffic_limit_bytes: int | None = None
    last_status_change: datetime | None = None
    last_status_message: str | None = None
    xray_uptime: int = 0
    is_traffic_tracking_active: bool = False
    traffic_reset_day: int | None = None
    notify_percent: int | None = None
    consumption_multiplier: float = 1.0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    provider_uuid: str | None = None
    versions: dict[str, str] | None = None
    system: dict[str, Any] | None = None
    active_plugin_uuid: str | None = None


class RemnaWaveNodeListResponse(BaseModel):
    items: list[RemnaWaveNode]
    total: int


class RemnaWaveNodeActionRequest(BaseModel):
    action: Literal['enable', 'disable', 'restart']


class RemnaWaveNodeActionResponse(BaseModel):
    success: bool
    detail: str | None = None


class RemnaWaveNodeStatisticsResponse(BaseModel):
    node: RemnaWaveNode
    realtime: dict[str, Any] | None = None
    usage_history: list[dict[str, Any]] = Field(default_factory=list)
    last_updated: datetime | None = None


class RemnaWaveNodeUsageResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class RemnaWaveBandwidth(BaseModel):
    realtime_download: int
    realtime_upload: int
    realtime_total: int


class RemnaWaveTrafficPeriod(BaseModel):
    current: int
    previous: int
    difference: str | None = None


class RemnaWaveTrafficPeriods(BaseModel):
    last_2_days: RemnaWaveTrafficPeriod
    last_7_days: RemnaWaveTrafficPeriod
    last_30_days: RemnaWaveTrafficPeriod
    current_month: RemnaWaveTrafficPeriod
    current_year: RemnaWaveTrafficPeriod


class RemnaWaveSystemSummary(BaseModel):
    users_online: int
    total_users: int
    active_connections: int
    nodes_online: int
    users_last_day: int
    users_last_week: int
    users_never_online: int
    total_user_traffic: int


class RemnaWaveServerInfo(BaseModel):
    cpu_cores: int
    memory_total: int
    memory_used: int
    memory_free: int
    uptime_seconds: int


class RemnaWaveSystemStatsResponse(BaseModel):
    system: RemnaWaveSystemSummary
    users_by_status: dict[str, int]
    server_info: RemnaWaveServerInfo
    bandwidth: RemnaWaveBandwidth
    traffic_periods: RemnaWaveTrafficPeriods
    nodes_realtime: list[dict[str, Any]] = Field(default_factory=list)
    nodes_weekly: list[dict[str, Any]] = Field(default_factory=list)
    last_updated: datetime | None = None


class RemnaWaveSquad(BaseModel):
    uuid: str
    name: str
    members_count: int
    inbounds_count: int
    inbounds: list[dict[str, Any]] = Field(default_factory=list)


class RemnaWaveSquadListResponse(BaseModel):
    items: list[RemnaWaveSquad]
    total: int


class RemnaWaveSquadCreateRequest(BaseModel):
    name: str
    inbound_uuids: list[str] = Field(default_factory=list)


class RemnaWaveSquadUpdateRequest(BaseModel):
    name: str | None = None
    inbound_uuids: list[str] | None = None


class RemnaWaveSquadActionRequest(BaseModel):
    action: Literal['add_all_users', 'remove_all_users', 'delete', 'rename', 'update_inbounds']
    name: str | None = None
    inbound_uuids: list[str] | None = None


class RemnaWaveOperationResponse(BaseModel):
    success: bool
    detail: str | None = None
    data: dict[str, Any] | None = None


class RemnaWaveInboundsResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class RemnaWaveUserTrafficResponse(BaseModel):
    telegram_id: int | None = None
    used_traffic_bytes: int
    used_traffic_gb: float
    lifetime_used_traffic_bytes: int
    lifetime_used_traffic_gb: float
    traffic_limit_bytes: int
    traffic_limit_gb: float
    subscription_url: str | None = None


class RemnaWaveSyncFromPanelRequest(BaseModel):
    mode: Literal['all', 'new_only', 'update_only'] = 'all'


class RemnaWaveGenericSyncResponse(BaseModel):
    success: bool
    detail: str | None = None
    data: dict[str, Any] | None = None


class RemnaWaveSquadMigrationPreviewResponse(BaseModel):
    squad_uuid: str
    squad_name: str
    current_users: int
    max_users: int | None = None
    users_to_migrate: int


class RemnaWaveSquadMigrationRequest(BaseModel):
    source_uuid: str
    target_uuid: str


class RemnaWaveSquadMigrationStats(BaseModel):
    source_uuid: str
    target_uuid: str
    total: int = 0
    updated: int = 0
    panel_updated: int = 0
    panel_failed: int = 0
    source_removed: int = 0
    target_added: int = 0


class RemnaWaveSquadMigrationResponse(BaseModel):
    success: bool
    detail: str | None = None
    error: str | None = None
    data: RemnaWaveSquadMigrationStats | None = None
