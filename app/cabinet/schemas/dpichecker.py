"""Схемы ручек DPI//CHECKER в кабинете (контракт для src/api/dpichecker.ts кабинета).

Ключи VPN и ссылки MTProto уходят в кабинет только там, где админ их сам вставил/выбрал
(разбор, цели из панели); строка действия отдаётся без целей, тела запроса и служебных полей.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


CheckType = Literal['vpn', 'ip', 'mtproto']
Location = Literal['russia', 'china', 'iran', 'turkmenistan']
ProbeMode = Literal['auto', 'server', 'noserver']
Source = Literal['paste', 'panel_subscription', 'panel_hosts', 'panel_nodes']
MAX_RESOURCES = 50
MAX_POPS = 500


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


class TargetIn(BaseModel):
    value: str = Field(min_length=1, max_length=4096)
    name: str = Field(default='', max_length=255)


class CheckCreate(BaseModel):
    check_type: CheckType
    location: Location
    pop_ids: list[int] = Field(min_length=1, max_length=MAX_POPS)
    targets: list[TargetIn] = Field(min_length=1, max_length=MAX_RESOURCES)
    source: Source = 'paste'
    source_ref: str | None = Field(default=None, max_length=128)
    label: str = Field(default='', max_length=255)
    probe_mode: ProbeMode = 'auto'


class MonitorCreate(CheckCreate):
    interval_hours: int = Field(ge=1, le=168)
    alert_after_fails: int = Field(default=2, ge=1, le=20)
    notify_on_success: bool = False


class MonitorPatch(BaseModel):
    is_active: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=168)
    alert_after_fails: int | None = Field(default=None, ge=1, le=20)
    notify_on_success: bool | None = None


class ScanCreate(BaseModel):
    target: str = Field(min_length=1, max_length=255)
    source: Literal['paste', 'panel_hosts', 'panel_nodes'] = 'paste'
    source_ref: str | None = Field(default=None, max_length=128)
    label: str = Field(default='', max_length=255)


class ParseRequest(BaseModel):
    check_type: CheckType
    text: str = Field(min_length=1, max_length=200_000)


class EstimateRequest(BaseModel):
    check_type: CheckType
    location: Location
    pop_ids: list[int] = Field(min_length=1, max_length=MAX_POPS)
    resources: list[str] = Field(min_length=1, max_length=MAX_RESOURCES)


class PanelTargetsRequest(BaseModel):
    kind: Literal['subscription', 'hosts', 'nodes']
    user_id: int | None = None
    uuids: list[str] = Field(default_factory=list, max_length=500)


class PanelTargetOut(BaseModel):
    value: str
    name: str
    ref: str


class PanelTargetsResponse(BaseModel):
    targets: list[PanelTargetOut]


class ActionOut(BaseModel):
    id: int
    kind: str
    check_type: str | None
    remote_id: int | None
    status: str
    admin_user_id: int | None
    admin_name: str | None = None
    location: str | None
    pop_count: int
    resource_count: int
    source: str
    source_ref: str | None
    label: str
    target_names: list[str]
    cost_usd: float | None
    refunded_usd: float | None
    error_code: str | None
    created_at: datetime | None

    @classmethod
    def from_action(cls, action: Any, admin_name: str | None = None) -> ActionOut:
        return cls(
            id=action.id,
            kind=action.kind,
            check_type=action.check_type,
            remote_id=action.remote_id,
            status=action.status,
            admin_user_id=action.admin_user_id,
            admin_name=admin_name,
            location=action.location,
            pop_count=action.pop_count or 0,
            resource_count=action.resource_count or 0,
            source=action.source,
            source_ref=action.source_ref,
            label=action.label or '',
            target_names=[str(t.get('name') or '') for t in action.targets or []],
            cost_usd=_money(action.cost_usd),
            refunded_usd=_money(action.refunded_usd),
            error_code=action.error_code,
            created_at=action.created_at,
        )


class ActionListResponse(BaseModel):
    items: list[ActionOut]
    total: int
    counts: dict[str, int] = {}


class CheckResponse(BaseModel):
    action: ActionOut
    check: dict[str, Any]


class ScanResponse(BaseModel):
    action: ActionOut
    scan: dict[str, Any]


class StatusResponse(BaseModel):
    enabled: bool
    configured: bool
    balance: float | None = None
    total_spent: float | None = None
    noisy: dict[str, Any] | None = None
    monitors: dict[str, Any] | None = None
    webhook_ready: bool = False
    reference: dict[str, Any] | None = None
    error: str | None = None


class PopsResponse(BaseModel):
    pops: list[dict[str, Any]]
    groups: dict[str, Any]


class OptimalResponse(BaseModel):
    pop_ids: list[int]


class MonitorListResponse(BaseModel):
    items: list[dict[str, Any]]


class DownloadLinkOut(BaseModel):
    url: str
    file_name: str
