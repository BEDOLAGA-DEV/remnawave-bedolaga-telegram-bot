"""Схемы раздела BSCHEKER (bschekbot) для кабинета.

Наружу не уходят ссылки конфигов (в них учётные данные подписки) и тело запроса
к API с ними: цели отдаются без ``raw_link``, отвергнутые ссылки — обрезком без
части до ``@``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.reachability.batches import MAX_BATCH_TARGETS
from app.services.reachability.requests import MAX_SNI_HOSTS, normalize_sni_hosts


Kind = Literal['probe', 'vless', 'scan']
Dpi = Literal['on', 'off', 'any']
ScopeKind = Literal['problems', 'stale', 'all', 'manual']
Purpose = Literal['bs', 'regular', 'unknown']
TargetKind = Literal['host', 'node', 'subscription_config', 'custom', 'cidr']

MAX_TARGETS_PER_JOB = 20
MAX_UNITS_PER_JOB = 64


# ============ Вход ============


class TargetIn(BaseModel):
    kind: TargetKind
    ref: str | None = Field(default=None, max_length=255)
    value: str | None = Field(default=None, max_length=4096)
    short_uuid: str | None = Field(default=None, max_length=255)
    # Подписка по URL (чужая панель) — из поля «Конфиг или подписка».
    url: str | None = Field(default=None, max_length=2048)
    index: int | None = Field(default=None, ge=0)
    # Ключ цели на момент разбора: подписка могла измениться, бот сверит.
    target_key: str | None = Field(default=None, max_length=255)

    @model_validator(mode='after')
    def _required_fields(self) -> Self:
        if self.kind in ('host', 'node') and not self.ref:
            raise ValueError('для host/node нужен ref (uuid)')
        if self.kind == 'subscription_config' and (not (self.short_uuid or self.url) or self.index is None):
            raise ValueError('для subscription_config нужны short_uuid или url и index')
        if self.kind in ('custom', 'cidr') and not (self.value or '').strip():
            raise ValueError('для custom/cidr нужно value')
        return self


class ProbesIn(BaseModel):
    icmp: bool = False
    tcp: bool = True
    sni: bool = True


class JobCreateRequest(BaseModel):
    kind: Kind
    targets: list[TargetIn] = Field(min_length=1, max_length=MAX_TARGETS_PER_JOB)
    units: list[str] = Field(default_factory=list, max_length=MAX_UNITS_PER_JOB)
    dpi: Dpi = 'on'
    probes: ProbesIn = Field(default_factory=ProbesIn)
    core: Literal['', 'stable', 'prerelease'] = ''
    # Свои имена для TLS-SNI (до 5, как Multi-SNI в оригинале); пусто — имена целей или дефолт из настроек.
    sni_hosts: list[str] = Field(default_factory=list, max_length=MAX_SNI_HOSTS)

    @field_validator('sni_hosts')
    @classmethod
    def _clean_sni_hosts(cls, value: list[str]) -> list[str]:
        return normalize_sni_hosts(value)


class BatchCreateRequest(BaseModel):
    """Проверка многих серверов панели одной кнопкой: до 300 хостов, чашки по 10 собирает бот."""

    host_refs: list[str] = Field(min_length=1, max_length=MAX_BATCH_TARGETS)
    units: list[str] = Field(default_factory=list, max_length=MAX_UNITS_PER_JOB)
    dpi: Dpi = 'on'
    probes: ProbesIn = Field(default_factory=ProbesIn)
    sni_hosts: list[str] = Field(default_factory=list, max_length=MAX_SNI_HOSTS)
    scope_kind: ScopeKind = 'manual'

    @field_validator('sni_hosts')
    @classmethod
    def _clean_sni_hosts(cls, value: list[str]) -> list[str]:
        return normalize_sni_hosts(value)


class ParseInputRequest(BaseModel):
    """Поле «Конфиг или подписка»: ссылки, URL подписок, base64 — построчно."""

    raw_input: str = Field(min_length=1, max_length=65536)


class PrefUpdateRequest(BaseModel):
    target_kind: Literal['host', 'node']
    target_ref: str = Field(min_length=1, max_length=255)
    purpose: Purpose | None = None
    excluded: bool | None = None
    note: str | None = Field(default=None, max_length=500)


# ============ Симки и статус ============


class UnitOut(BaseModel):
    op_key: str
    operator: str = ''
    name: str = ''
    region: str = ''
    region_code: str = ''
    dpi: str = ''
    channel_state: str = ''
    probeable: bool = False
    in_catalog: bool = True


class UnitsResponse(BaseModel):
    units: list[UnitOut]


class ActiveJobOut(BaseModel):
    id: int
    kind: str
    phase: str | None
    started_by_user_id: int | None
    started_at: datetime | None


class ReferenceOut(BaseModel):
    short_uuid: str | None
    configs: int
    rejected: int = 0
    error: str | None


class ActiveBatchOut(BaseModel):
    id: int
    total_targets: int
    done_targets: int
    started_at: datetime | None


class StatusResponse(BaseModel):
    enabled: bool
    configured: bool
    healthy: bool
    health_message: str | None = None
    balance_kopeks: int | None = None
    bonus_kopeks: int | None = None
    tier: str | None = None
    tier_expires_at: str | None = None
    min_interval_sec: float | None = None
    active_jobs: list[ActiveJobOut] = Field(default_factory=list)
    reference: ReferenceOut | None = None
    cost_limit_kopeks: int = 0
    # Ядро Xray → номер версии (как показывает оригинал bsbord.com).
    cores: dict[str, str] = Field(default_factory=dict)
    # «SNI-хост по умолчанию» из настроек — кабинет подставляет его в поле SNI.
    default_sni: str | None = None
    # Идущая проверка серверов: кабинет открывает её экран после перезагрузки страницы.
    active_batch: ActiveBatchOut | None = None


# ============ Цели ============


class HostTargetOut(BaseModel):
    uuid: str
    remark: str
    address: str
    port: int | None
    sni: str | None
    is_disabled: bool
    tag: str | None
    purpose: str
    purpose_guessed: bool
    excluded: bool
    node_uuids: list[str]
    target_key: str


class HostsResponse(BaseModel):
    items: list[HostTargetOut]


class NodeTargetOut(BaseModel):
    uuid: str
    name: str
    address: str
    is_connected: bool
    is_disabled: bool
    host_uuids: list[str]
    target_key: str


class NodesResponse(BaseModel):
    items: list[NodeTargetOut]


class ConfigOut(BaseModel):
    index: int
    protocol: str | None
    label: str
    address: str
    port: int | None
    sni: str | None
    target_key: str
    purpose: str


class RejectedOut(BaseModel):
    reason: str
    preview: str  # обрезок ссылки после «@», без учётных данных


class SubscriptionConfigsResponse(BaseModel):
    short_uuid: str
    configs: list[ConfigOut]
    rejected: list[RejectedOut]


class SourceOut(BaseModel):
    kind: Literal['links', 'subscription']
    label: str
    count: int


class ParsedConfigOut(ConfigOut):
    # Что кабинет отправит в targets[] задачи: custom со ссылкой пользователя или
    # subscription_config по shortUuid/url — сырые ссылки подписки наружу не уходят.
    target: dict[str, Any]


class ParsedInputResponse(BaseModel):
    configs: list[ParsedConfigOut]
    rejected: list[RejectedOut]
    sources: list[SourceOut]


class PrefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    target_kind: str
    target_ref: str
    purpose: str
    excluded: bool
    note: str | None


# ============ Preview и задачи ============


class SkippedOut(BaseModel):
    dpi_off: list[dict[str, Any]] = Field(default_factory=list)
    unavailable: list[dict[str, Any]] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    blocked_targets: list[dict[str, Any]] = Field(default_factory=list)


class TargetOut(BaseModel):
    kind: str
    label: str
    address: str
    port: int | None
    target_key: str
    sni: str | None
    ref: dict[str, Any] = Field(default_factory=dict)
    purpose: str = 'unknown'


class PreviewResponse(BaseModel):
    kind: Kind
    targets: list[TargetOut]
    units_resolved: list[str]
    skipped: SkippedOut
    cost_kopeks: int | None
    estimate_is_exact: bool
    warnings: list[str]
    balance_kopeks: int | None


class LegOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    target_key: str
    target_kind: str | None
    target_ref: str | None
    op_key: str
    operator: str | None
    region: str | None
    dpi: str | None
    verdict: str
    matches_expectation: bool | None
    raw: dict[str, Any] | None
    checked_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    status: str
    phase: str | None
    trigger: str
    started_by_user_id: int | None
    external_id: int | None
    targets: list[TargetOut]
    units_requested: list[str] | None
    units_resolved: list[str] | None
    units_effective: list[str] | None
    skipped: dict[str, Any] | None
    dpi: str
    estimated_kopeks: int | None
    estimate_is_exact: bool
    cost_kopeks: int | None
    refunded_kopeks: int | None
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    retryable: bool | None
    attempts: int
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    legs: list[LegOut] = Field(default_factory=list)
    # Пачка, в которую входит задача (проверка многих серверов одной кнопкой).
    batch_id: int | None = None
    # Из тела запроса к API: какие пробы заказаны и какие SNI-имена — для «Моих проверок».
    probes: dict[str, bool] | None = None
    sni_hosts: list[str] = Field(default_factory=list)


class JobListResponse(BaseModel):
    items: list[JobOut]
    total: int
    offset: int
    limit: int


# ============ Пачка проверок ============


class BatchPreviewResponse(BaseModel):
    targets: list[TargetOut]
    units_resolved: list[str]
    chunks: int
    cost_kopeks: int | None
    estimated_minutes: int
    warnings: list[str] = Field(default_factory=list)
    balance_kopeks: int | None = None


class BatchJobOut(BaseModel):
    id: int
    status: str
    phase: str | None
    target_keys: list[str]
    cost_kopeks: int | None
    # Частичный результат идущей пробы (``job.result.partial``): по симкам «готово / проверяем / ждёт».
    partial: dict[str, Any] | None = None


class BatchOut(BaseModel):
    id: int
    status: str
    phase: str | None
    scope: dict[str, Any]
    total_targets: int
    done_targets: int
    estimated_kopeks: int | None
    cost_kopeks: int | None
    error_message: str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    jobs: list[BatchJobOut] = Field(default_factory=list)


class BatchListResponse(BaseModel):
    items: list[BatchOut]
    total: int
    offset: int
    limit: int


# ============ Сводка ============


class CellOut(BaseModel):
    verdict: str
    matches_expectation: bool | None
    checked_at: datetime
    job_id: int


class SummaryRow(BaseModel):
    target_key: str
    kind: str | None
    ref: str | None
    label: str
    purpose: str
    purpose_guessed: bool = False
    in_panel: bool = True
    cells: dict[str, CellOut]


class SummaryResponse(BaseModel):
    dpi: str
    units: list[UnitOut]
    rows: list[SummaryRow]
    panel_error: str | None = None
