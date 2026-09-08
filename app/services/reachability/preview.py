"""Результат превью задачи: цели, симки, цена. Общий для сервиса и пачек проверок."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.reachability.targets import Target


@dataclass(frozen=True)
class PreviewResult:
    kind: str
    targets: list[Target]
    units_resolved: list[str]
    skipped: dict
    cost_kopeks: int | None
    estimate_is_exact: bool
    warnings: list[str] = field(default_factory=list)
    balance_kopeks: int | None = None
    request: dict = field(default_factory=dict)
