"""Admin audit log routes — view and export admin action history."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.rbac import AuditLogCRUD
from app.database.models import User

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/rbac/audit-log', tags=['Admin RBAC Audit Log'])


# ============ Schemas ============


class AuditLogEntry(BaseModel):
    """Single audit log entry."""

    id: int
    user_id: int
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    details: dict[str, Any] | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    status: str
    request_method: str | None = None
    request_path: str | None = None
    created_at: datetime | None = None
    user_first_name: str | None = None
    user_email: str | None = None


class AuditLogListResponse(BaseModel):
    """Paginated audit log list."""

    items: list[AuditLogEntry]
    total: int
    limit: int
    offset: int


# ============ CSV Export ============

_CSV_COLUMNS = [
    'id',
    'user_id',
    'action',
    'resource_type',
    'resource_id',
    'status',
    'ip_address',
    'request_method',
    'request_path',
    'created_at',
    'user_agent',
    'details',
]


def _sanitize_csv_cell(value: str) -> str:
    """Prevent CSV formula injection by prefixing dangerous leading characters."""
    if value and value[0] in ('=', '+', '-', '@', '\t', '\r'):
        return f"'{value}"
    return value


def _logs_to_csv(logs) -> str:
    """Serialize audit log entries to CSV string."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_CSV_COLUMNS)

    for log in logs:
        writer.writerow(
            [
                log.id,
                log.user_id,
                log.action,
                log.resource_type or '',
                log.resource_id or '',
                log.status,
                log.ip_address or '',
                log.request_method or '',
                _sanitize_csv_cell(log.request_path or ''),
                log.created_at.isoformat() if log.created_at else '',
                _sanitize_csv_cell((log.user_agent or '')[:200]),
                _sanitize_csv_cell(str(log.details) if log.details else ''),
            ]
        )

    return output.getvalue()


# ============ Routes ============


@router.get('', response_model=AuditLogListResponse)
async def list_audit_logs(
    admin: User = Depends(require_permission('audit_log:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    user_id: int | None = Query(default=None),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List audit log entries with optional filters and pagination."""
    logs, total = await AuditLogCRUD.get_logs(
        db,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        status=status,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        load_user=True,
    )

    items = [
        AuditLogEntry(
            id=log.id,
            user_id=log.user_id,
            action=log.action,
            resource_type=log.resource_type,
            resource_id=log.resource_id,
            details=log.details,
            ip_address=log.ip_address,
            user_agent=log.user_agent,
            status=log.status,
            request_method=log.request_method,
            request_path=log.request_path,
            created_at=log.created_at,
            user_first_name=log.user.first_name if log.user else None,
            user_email=log.user.email if log.user else None,
        )
        for log in logs
    ]

    return AuditLogListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get('/export')
async def export_audit_logs(
    admin: User = Depends(require_permission('audit_log:export')),
    db: AsyncSession = Depends(get_cabinet_db),
    user_id: int | None = Query(default=None),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    limit: int = Query(default=10000, ge=1, le=50000),
):
    """Export audit logs as CSV file."""
    logs, _total = await AuditLogCRUD.get_logs(
        db,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        status=status,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=0,
    )

    csv_content = _logs_to_csv(logs)
    timestamp = datetime.now(UTC).strftime('%Y%m%d_%H%M%S')
    filename = f'audit_log_{timestamp}.csv'

    logger.info(
        'Admin exported audit logs',
        admin_id=admin.id,
        rows=len(logs),
        filename=filename,
    )

    return StreamingResponse(
        iter([csv_content]),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )
