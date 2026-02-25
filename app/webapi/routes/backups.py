from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Security, UploadFile, status
from fastapi.responses import FileResponse

from app.localization.texts import get_texts
from app.services.backup_service import backup_service

from ..background.backup_tasks import backup_task_manager
from ..dependencies import require_api_token
from ..schemas.backups import (
    BackupCreateResponse,
    BackupDeleteResponse,
    BackupInfo,
    BackupListResponse,
    BackupRestoreRequest,
    BackupRestoreResponse,
    BackupStatusResponse,
    BackupTaskInfo,
    BackupTaskListResponse,
)


router = APIRouter()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        if value.endswith('Z'):
            value = value.replace('Z', '+00:00')
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _serialize_backup(raw: dict) -> BackupInfo:
    timestamp = _parse_datetime(raw.get('timestamp'))
    tables_count = _to_int(raw.get('tables_count'))
    total_records = _to_int(raw.get('total_records'))
    file_size_bytes = _to_int(raw.get('file_size_bytes')) or 0
    file_size_mb = raw.get('file_size_mb')
    try:
        file_size_mb = float(file_size_mb)
    except (TypeError, ValueError):
        file_size_mb = round(file_size_bytes / 1024 / 1024, 2)

    created_by = _to_int(raw.get('created_by'))

    return BackupInfo(
        filename=str(raw.get('filename')),
        filepath=str(raw.get('filepath')),
        timestamp=timestamp,
        tables_count=tables_count,
        total_records=total_records,
        compressed=bool(raw.get('compressed', False)),
        file_size_bytes=file_size_bytes,
        file_size_mb=float(file_size_mb),
        created_by=created_by,
        database_type=raw.get('database_type'),
        version=raw.get('version'),
        error=raw.get('error'),
    )


@router.post(
    '',
    response_model=BackupCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary='Запустить создание резервной копии',
)
async def create_backup_endpoint(
    token: Any = Security(require_api_token),
) -> BackupCreateResponse:
    created_by = getattr(token, 'id', None)
    state = await backup_task_manager.enqueue(created_by=created_by)
    return BackupCreateResponse(task_id=state.task_id, status=state.status)


@router.get(
    '',
    response_model=BackupListResponse,
    summary='Список резервных копий',
)
async def list_backups(
    _: Any = Security(require_api_token),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> BackupListResponse:
    backups = await backup_service.get_backup_list()
    total = len(backups)

    slice_backups = backups[offset : offset + limit]
    items = [_serialize_backup(raw) for raw in slice_backups]

    return BackupListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    '/status/{task_id}',
    response_model=BackupStatusResponse,
    summary='Статус создания резервной копии',
)
async def get_backup_status(
    task_id: str,
    _: Any = Security(require_api_token),
) -> BackupStatusResponse:
    texts = get_texts('ru')
    state = await backup_task_manager.get(task_id)
    if not state:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_BACKUPS_TASK_NOT_FOUND', 'Task not found'),
        )

    return BackupStatusResponse(
        task_id=state.task_id,
        status=state.status,
        message=state.message,
        file_path=state.file_path,
        created_by=state.created_by,
        created_at=state.created_at,
        updated_at=state.updated_at,
    )


@router.get(
    '/tasks',
    response_model=BackupTaskListResponse,
    summary='Список фоновых задач бекапов',
)
async def list_backup_tasks(
    _: Any = Security(require_api_token),
    active_only: bool = Query(False, description='Вернуть только активные задачи'),
) -> BackupTaskListResponse:
    states = await backup_task_manager.list(active_only=active_only)

    items = [
        BackupTaskInfo(
            task_id=state.task_id,
            status=state.status,
            message=state.message,
            file_path=state.file_path,
            created_by=state.created_by,
            created_at=state.created_at,
            updated_at=state.updated_at,
        )
        for state in states
    ]

    return BackupTaskListResponse(items=items, total=len(items))


@router.get(
    '/download/{filename:path}',
    summary='Скачать файл резервной копии',
    responses={
        200: {
            'content': {'application/octet-stream': {}},
            'description': 'Файл резервной копии',
        }
    },
)
async def download_backup(
    filename: str,
    _: Any = Security(require_api_token),
) -> FileResponse:
    texts = get_texts('ru')
    backup_path = backup_service.backup_dir / filename

    if not backup_path.exists():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_BACKUPS_FILE_NOT_FOUND', 'Backup file not found'),
        )

    if not backup_path.is_file():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_BACKUPS_INVALID_BACKUP_PATH', 'Invalid backup path'),
        )

    resolved_path = backup_path.resolve()
    backup_dir_resolved = backup_service.backup_dir.resolve()
    if not str(resolved_path).startswith(str(backup_dir_resolved)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            texts.t('WEBAPI_BACKUPS_ACCESS_DENIED', 'Access denied'),
        )

    return FileResponse(
        path=str(backup_path),
        filename=filename,
        media_type='application/octet-stream',
    )


@router.post(
    '/restore/{filename:path}',
    response_model=BackupRestoreResponse,
    summary='Восстановить из резервной копии',
)
async def restore_backup(
    filename: str,
    payload: BackupRestoreRequest,
    _: Any = Security(require_api_token),
) -> BackupRestoreResponse:
    texts = get_texts('ru')
    backup_path = backup_service.backup_dir / filename

    if not backup_path.exists():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_BACKUPS_FILE_NOT_FOUND', 'Backup file not found'),
        )

    resolved_path = backup_path.resolve()
    backup_dir_resolved = backup_service.backup_dir.resolve()
    if not str(resolved_path).startswith(str(backup_dir_resolved)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            texts.t('WEBAPI_BACKUPS_ACCESS_DENIED', 'Access denied'),
        )

    success, message = await backup_service.restore_backup(str(backup_path), clear_existing=payload.clear_existing)

    return BackupRestoreResponse(
        success=success,
        message=message,
    )


@router.post(
    '/upload',
    response_model=BackupRestoreResponse,
    summary='Загрузить и восстановить из файла резервной копии',
)
async def upload_and_restore_backup(
    file: UploadFile = File(..., description='Файл резервной копии (.tar.gz, .json, .json.gz)'),
    clear_existing: bool = Query(False, description='Очистить существующие данные'),
    _: Any = Security(require_api_token),
) -> BackupRestoreResponse:
    texts = get_texts('ru')
    if not file.filename:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_BACKUPS_FILENAME_REQUIRED', 'Filename is required'),
        )

    safe_filename = Path(file.filename).name
    if not safe_filename or safe_filename in ('.', '..'):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_BACKUPS_INVALID_FILENAME', 'Invalid filename'),
        )

    allowed_extensions = ('.tar.gz', '.json', '.json.gz', '.tar')
    if not any(safe_filename.endswith(ext) for ext in allowed_extensions):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_BACKUPS_INVALID_FILE_TYPE_ALLOWED',
                'Invalid file type. Allowed: {extensions}',
            ).format(extensions=', '.join(allowed_extensions)),
        )

    temp_path = backup_service.backup_dir / f'uploaded_{safe_filename}'

    resolved_path = temp_path.resolve()
    backup_dir_resolved = backup_service.backup_dir.resolve()
    if not str(resolved_path).startswith(str(backup_dir_resolved)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            texts.t('WEBAPI_BACKUPS_INVALID_FILE_PATH', 'Invalid file path'),
        )

    try:
        content = await file.read()
        with open(temp_path, 'wb') as f:
            f.write(content)

        success, message = await backup_service.restore_backup(str(temp_path), clear_existing=clear_existing)

        return BackupRestoreResponse(
            success=success,
            message=message,
        )

    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass


@router.delete(
    '/{filename:path}',
    response_model=BackupDeleteResponse,
    summary='Удалить резервную копию',
)
async def delete_backup(
    filename: str,
    _: Any = Security(require_api_token),
) -> BackupDeleteResponse:
    texts = get_texts('ru')
    backup_path = backup_service.backup_dir / filename

    resolved_path = backup_path.resolve()
    backup_dir_resolved = backup_service.backup_dir.resolve()
    if not str(resolved_path).startswith(str(backup_dir_resolved)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            texts.t('WEBAPI_BACKUPS_ACCESS_DENIED', 'Access denied'),
        )

    success, message = await backup_service.delete_backup(filename)

    if not success:
        raise HTTPException(status.HTTP_404_NOT_FOUND, message)

    return BackupDeleteResponse(
        success=success,
        message=message,
    )
