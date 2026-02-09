"""Schemas for admin traffic usage."""

from pydantic import BaseModel, Field


class TrafficNodeInfo(BaseModel):
    node_uuid: str
    node_name: str
    country_code: str


class UserTrafficItem(BaseModel):
    user_id: int
    telegram_id: int | None
    username: str | None
    email: str | None
    full_name: str
    tariff_name: str | None
    subscription_status: str | None
    traffic_limit_gb: float
    device_limit: int
    node_traffic: dict[str, int]  # {node_uuid: total_bytes}
    total_bytes: int


class TrafficUsageResponse(BaseModel):
    items: list[UserTrafficItem]
    nodes: list[TrafficNodeInfo]
    total: int
    offset: int
    limit: int
    period_days: int
    available_tariffs: list[str]
    available_statuses: list[str]


class UserTrafficEnrichment(BaseModel):
    devices_connected: int = 0
    total_spent_kopeks: int = 0
    subscription_start_date: str | None = None
    subscription_end_date: str | None = None
    last_node_name: str | None = None


class TrafficEnrichmentResponse(BaseModel):
    data: dict[int, UserTrafficEnrichment]


class ExportCsvRequest(BaseModel):
    period: int = Field(30, ge=1, le=30)
    start_date: str | None = None
    end_date: str | None = None
    tariffs: str | None = None
    statuses: str | None = None
    nodes: str | None = None
    total_threshold_gb: float | None = Field(None, ge=0, description='Total GB/day threshold for risk column')
    node_threshold_gb: float | None = Field(None, ge=0, description='Per-node GB/day threshold for risk column')


class ExportCsvResponse(BaseModel):
    success: bool
    message: str
