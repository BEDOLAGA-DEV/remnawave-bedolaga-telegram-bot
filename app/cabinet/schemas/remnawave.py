"""Schemas for RemnaWave management in cabinet admin panel."""

import ipaddress
import re
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


# ============ Status & Connection ============


class ConnectionStatus(BaseModel):
    """RemnaWave API connection status."""

    status: str
    message: str
    api_url: str | None = None
    status_code: int | None = None
    system_info: dict[str, Any] | None = None


class RemnaWaveStatusResponse(BaseModel):
    """RemnaWave configuration and connection status."""

    is_configured: bool
    configuration_error: str | None = None
    connection: ConnectionStatus | None = None


# ============ System Statistics ============


class SystemSummary(BaseModel):
    """System summary statistics."""

    users_online: int
    total_users: int
    active_connections: int
    nodes_online: int
    total_nodes: int = 0
    users_last_day: int
    users_last_week: int
    users_never_online: int
    total_user_traffic: int


class ServerInfo(BaseModel):
    """Server hardware info."""

    cpu_cores: int
    memory_total: int
    memory_used: int
    memory_free: int
    uptime_seconds: int


class Bandwidth(BaseModel):
    """Realtime bandwidth statistics."""

    realtime_download: int
    realtime_upload: int
    realtime_total: int


class TrafficPeriod(BaseModel):
    """Traffic statistics for a period."""

    current: int
    previous: int
    difference: str | None = None


class TrafficPeriods(BaseModel):
    """Traffic statistics for multiple periods."""

    last_2_days: TrafficPeriod
    last_7_days: TrafficPeriod
    last_30_days: TrafficPeriod
    current_month: TrafficPeriod
    current_year: TrafficPeriod


class SystemStatsResponse(BaseModel):
    """Full system statistics response."""

    system: SystemSummary
    users_by_status: dict[str, int]
    server_info: ServerInfo
    bandwidth: Bandwidth
    traffic_periods: TrafficPeriods
    nodes_realtime: list[dict[str, Any]] = Field(default_factory=list)
    nodes_weekly: list[dict[str, Any]] = Field(default_factory=list)
    last_updated: datetime | None = None


# ============ Nodes ============


class NodeInfo(BaseModel):
    """Node information."""

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
    provider_name: str | None = None
    provider_favicon: str | None = None
    versions: dict[str, str] | None = None
    system: dict[str, Any] | None = None
    active_plugin_uuid: str | None = None
    # [{ip, status}] — панель отдаёт адреса узла; кабинет предлагает их
    # в GeoCheck как варианты исходного адреса.
    ips: list[dict[str, Any]] = Field(default_factory=list)


class NodesListResponse(BaseModel):
    """List of nodes response."""

    items: list[NodeInfo]
    total: int


class NodesOverview(BaseModel):
    """Nodes overview statistics."""

    total: int
    online: int
    offline: int
    disabled: int
    total_users_online: int
    nodes: list[NodeInfo]


class NodeStatisticsResponse(BaseModel):
    """Node statistics with usage history."""

    node: NodeInfo
    realtime: dict[str, Any] | None = None
    usage_history: list[dict[str, Any]] = Field(default_factory=list)
    last_updated: datetime | None = None


class NodeUsageResponse(BaseModel):
    """Node usage history response."""

    items: list[dict[str, Any]] = Field(default_factory=list)


class NodeActionRequest(BaseModel):
    """Request to perform node action."""

    action: Literal['enable', 'disable', 'restart']


class NodeActionResponse(BaseModel):
    """Response after node action."""

    success: bool
    message: str | None = None
    is_disabled: bool | None = None


# ============ GeoCheck (Remnawave 3.3.0) ============

# Имена сетевых интерфейсов в Linux — до 15 символов (IFNAMSIZ), но
# VPN/bridge-обвязка иногда даёт длиннее, поэтому предел мягче. Набор
# символов узкий намеренно: значение уходит в панель и дальше на узел.
_INTERFACE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.@-]{0,31}$')


class GeocheckRequest(BaseModel):
    """С какого маршрута гонять проверку.

    Пусто — маршрут узла по умолчанию. Иначе ровно одно из двух: исходный
    IP-адрес либо сетевой интерфейс.
    """

    ip: str | None = None
    interface: str | None = None

    @field_validator('ip', 'interface', mode='before')
    @classmethod
    def _blank_means_default(cls, value: Any) -> Any:
        """Пустая строка из формы — это «по умолчанию», а не значение."""
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator('ip')
    @classmethod
    def _must_be_ip_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError('ip must be a valid IPv4 or IPv6 address') from exc
        return value

    @field_validator('interface')
    @classmethod
    def _must_be_interface_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _INTERFACE_RE.match(value):
            raise ValueError('interface must be a network interface name')
        return value

    @model_validator(mode='after')
    def _one_route_at_most(self) -> Self:
        if self.ip and self.interface:
            raise ValueError('choose either ip or interface, not both')
        return self


class GeocheckStartResponse(BaseModel):
    """Задача поставлена; результат забирается по job_id."""

    job_id: str


class GeocheckImage(BaseModel):
    """SVG-отчёт в base64, готовый для data: URL."""

    format: str
    media_type: str
    encoding: str
    data: str


class GeocheckResult(BaseModel):
    """Готовый результат проверки."""

    success: bool
    node_uuid: str | None = None
    image: GeocheckImage | None = None
    raw_report: dict[str, Any] | None = None
    message: str | None = None


class GeocheckJobResponse(BaseModel):
    """Статус задачи GeoCheck. Пока идёт проверка, result пуст."""

    job_id: str
    is_completed: bool
    is_failed: bool
    result: GeocheckResult | None = None


# ============ Squads (Internal Squads) ============


class SquadInfo(BaseModel):
    """Internal Squad information from RemnaWave."""

    uuid: str
    name: str
    members_count: int
    inbounds_count: int
    inbounds: list[dict[str, Any]] = Field(default_factory=list)


class SquadWithLocalInfo(BaseModel):
    """Squad with local database info."""

    uuid: str
    name: str
    members_count: int
    inbounds_count: int
    inbounds: list[dict[str, Any]] = Field(default_factory=list)
    # Local DB info
    local_id: int | None = None
    display_name: str | None = None
    country_code: str | None = None
    is_available: bool | None = None
    is_trial_eligible: bool | None = None
    price_kopeks: int | None = None
    max_users: int | None = None
    current_users: int | None = None
    is_synced: bool = False


class SquadsListResponse(BaseModel):
    """List of squads response."""

    items: list[SquadWithLocalInfo]
    total: int


class SquadDetailResponse(BaseModel):
    """Detailed squad response."""

    uuid: str
    name: str
    members_count: int
    inbounds_count: int
    inbounds: list[dict[str, Any]] = Field(default_factory=list)
    # Local DB info if synced
    local_id: int | None = None
    display_name: str | None = None
    country_code: str | None = None
    description: str | None = None
    is_available: bool | None = None
    is_trial_eligible: bool | None = None
    price_kopeks: int | None = None
    max_users: int | None = None
    current_users: int | None = None
    sort_order: int | None = None
    is_synced: bool = False
    active_subscriptions: int = 0


class SquadCreateRequest(BaseModel):
    """Request to create a new squad."""

    name: str = Field(..., min_length=1, max_length=255)
    inbound_uuids: list[str] = Field(default_factory=list)


class SquadUpdateRequest(BaseModel):
    """Request to update a squad."""

    name: str | None = Field(None, min_length=1, max_length=255)
    inbound_uuids: list[str] | None = None


class SquadActionRequest(BaseModel):
    """Request to perform squad action."""

    action: Literal['add_all_users', 'remove_all_users', 'delete', 'rename', 'update_inbounds']
    name: str | None = None
    inbound_uuids: list[str] | None = None


class SquadOperationResponse(BaseModel):
    """Response after squad operation."""

    success: bool
    message: str | None = None
    data: dict[str, Any] | None = None


# ============ Migration ============


class MigrationPreviewResponse(BaseModel):
    """Preview of squad migration."""

    squad_uuid: str
    squad_name: str
    current_users: int
    max_users: int | None = None
    users_to_migrate: int


class MigrationRequest(BaseModel):
    """Request to migrate users between squads."""

    source_uuid: str
    target_uuid: str


class MigrationStats(BaseModel):
    """Migration statistics."""

    source_uuid: str
    target_uuid: str
    total: int = 0
    updated: int = 0
    panel_updated: int = 0
    panel_failed: int = 0
    source_removed: int = 0
    target_added: int = 0


class MigrationResponse(BaseModel):
    """Response after migration."""

    success: bool
    message: str | None = None
    error: str | None = None
    data: MigrationStats | None = None


# ============ Inbounds ============


class InboundInfo(BaseModel):
    """Inbound information."""

    uuid: str
    tag: str
    type: str | None = None
    network: str | None = None
    security: str | None = None


class InboundsListResponse(BaseModel):
    """List of inbounds response."""

    items: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0


# ============ Auto Sync ============


class AutoSyncTime(BaseModel):
    """Scheduled sync time."""

    hour: int
    minute: int


class AutoSyncStatus(BaseModel):
    """Auto sync status."""

    enabled: bool
    times: list[str] = Field(default_factory=list)  # HH:MM format
    next_run: datetime | None = None
    is_running: bool = False
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    last_run_success: bool | None = None
    last_run_reason: str | None = None
    last_run_error: str | None = None
    last_user_stats: dict[str, Any] | None = None
    last_server_stats: dict[str, Any] | None = None


class AutoSyncToggleRequest(BaseModel):
    """Request to toggle auto sync."""

    enabled: bool


class AutoSyncRunResponse(BaseModel):
    """Response after running sync."""

    started: bool
    success: bool | None = None
    error: str | None = None
    user_stats: dict[str, Any] | None = None
    server_stats: dict[str, Any] | None = None
    reason: str | None = None


# ============ Manual Sync ============


class SyncMode(BaseModel):
    """Sync mode options."""

    mode: Literal['all', 'new_only', 'update_only'] = 'all'


class SyncResponse(BaseModel):
    """Response after sync operation."""

    success: bool
    message: str | None = None
    data: dict[str, Any] | None = None


class SyncRecommendations(BaseModel):
    """Sync recommendations."""

    success: bool
    message: str | None = None
    data: dict[str, Any] | None = None


# ============ Panel recap / devices / top consumers ============


class RecapTotal(BaseModel):
    users: int = 0
    nodes: int = 0
    traffic_bytes: int = 0
    nodes_ram_bytes: int = 0
    nodes_cpu_cores: int = 0
    distinct_countries: int = 0


class RecapThisMonth(BaseModel):
    users: int = 0
    traffic_bytes: int = 0


class RecapResponse(BaseModel):
    """Panel recap overview (lifetime + this month + version/uptime)."""

    version: str | None = None
    init_date: str | None = None
    total: RecapTotal = Field(default_factory=RecapTotal)
    this_month: RecapThisMonth = Field(default_factory=RecapThisMonth)


class PlatformCount(BaseModel):
    platform: str
    count: int


class AppCount(BaseModel):
    app: str
    count: int


class DeviceTopUser(BaseModel):
    username: str
    devices_count: int


class DevicesStatsResponse(BaseModel):
    """HWID device statistics: breakdown by platform/app + totals + top users."""

    by_platform: list[PlatformCount] = Field(default_factory=list)
    by_app: list[AppCount] = Field(default_factory=list)
    top_users: list[DeviceTopUser] = Field(default_factory=list)
    total_unique_devices: int = 0
    total_hwid_devices: int = 0
    average_devices_per_user: float = 0


class TopConsumer(BaseModel):
    username: str
    total_bytes: int


class TopConsumersResponse(BaseModel):
    """Top traffic-consuming users aggregated across nodes."""

    period_days: int = 7
    users: list[TopConsumer] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Panel process runtime health (rss/heap, event-loop lag, uptime)."""

    instances: int = 0
    rss_bytes: int = 0
    heap_used_bytes: int = 0
    heap_total_bytes: int = 0
    event_loop_delay_ms: float = 0
    event_loop_p99_ms: float = 0
    uptime_seconds: int = 0
    instance_id: str | None = None


class SubscriptionRequestStatsResponse(BaseModel):
    """Subscription-link request stats broken down by client app."""

    by_app: list[AppCount] = Field(default_factory=list)
