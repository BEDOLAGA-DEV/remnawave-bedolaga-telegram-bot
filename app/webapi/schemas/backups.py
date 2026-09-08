from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.localization.texts import get_texts


class BackupCreateResponse(BaseModel):
    task_id: str
    status: str = Field(..., description=get_texts().t('BACKUP_TASK_STATUS_DESCRIPTION', 'Текущий статус задачи'))


class BackupInfo(BaseModel):
    filename: str
    filepath: str
    timestamp: datetime | None = None
    tables_count: int | None = None
    total_records: int | None = None
    compressed: bool
    file_size_bytes: int
    file_size_mb: float
    created_by: int | None = None
    database_type: str | None = None
    version: str | None = None
    error: str | None = None


class BackupListResponse(BaseModel):
    items: list[BackupInfo]
    total: int
    limit: int
    offset: int


class BackupStatusResponse(BaseModel):
    task_id: str
    status: str
    message: str | None = None
    file_path: str | None = Field(
        default=None,
        description=get_texts().t(
            'BACKUP_FILE_PATH_DESCRIPTION',
            'Полный путь до созданного бекапа, если задача завершена',
        ),
    )
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime


class BackupTaskInfo(BackupStatusResponse):
    pass


class BackupTaskListResponse(BaseModel):
    items: list[BackupTaskInfo]
    total: int


class BackupRestoreRequest(BaseModel):
    clear_existing: bool = Field(
        default=False,
        description=get_texts().t(
            'BACKUP_RESTORE_CLEAR_EXISTING_DESCRIPTION',
            'Очистить существующие данные перед восстановлением',
        ),
    )


class BackupRestoreResponse(BaseModel):
    success: bool
    message: str
    tables_restored: int | None = None
    records_restored: int | None = None


class BackupDeleteResponse(BaseModel):
    success: bool
    message: str
