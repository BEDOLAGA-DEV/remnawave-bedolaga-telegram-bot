import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services import overpay_certificate_service

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/overpay', tags=['Cabinet Admin Overpay'])


class OverpayCertificateStatusResponse(BaseModel):
    uploaded: bool
    valid: bool
    path: str
    subject: str | None = None
    not_valid_after: str | None = None
    has_chain: bool | None = None
    env_locked_path: bool
    env_locked_passphrase: bool


class OverpayCertificateUploadResponse(BaseModel):
    subject: str
    not_valid_after: str
    has_chain: bool
    path: str
    env_locked_path: bool
    env_locked_passphrase: bool
    warning: str | None = None


@router.get('/certificate', response_model=OverpayCertificateStatusResponse)
async def get_certificate_status(
    admin: User = Depends(require_permission('settings:read')),
) -> OverpayCertificateStatusResponse:
    return OverpayCertificateStatusResponse(**overpay_certificate_service.get_status())


@router.post('/certificate', response_model=OverpayCertificateUploadResponse)
async def upload_certificate(
    file: UploadFile = File(...),
    passphrase: str = Form(''),
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> OverpayCertificateUploadResponse:
    data = await file.read(overpay_certificate_service.MAX_P12_SIZE + 1)
    await file.close()

    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Empty file',
        )

    if len(data) > overpay_certificate_service.MAX_P12_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail='File too large. Maximum size: 1 MB',
        )

    try:
        metadata = await overpay_certificate_service.store_certificate(db, data, passphrase)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from None

    logger.info(
        'Admin uploaded Overpay certificate via cabinet',
        admin_id=admin.id,
        subject=metadata['subject'],
        not_valid_after=metadata['not_valid_after'],
    )
    return OverpayCertificateUploadResponse(**metadata)


@router.delete('/certificate', status_code=status.HTTP_204_NO_CONTENT)
async def delete_certificate(
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    await overpay_certificate_service.delete_certificate(db)
    logger.info('Admin deleted Overpay certificate via cabinet', admin_id=admin.id)
