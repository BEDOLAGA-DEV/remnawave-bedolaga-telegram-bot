"""Utility routes for cabinet."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.config import settings
from ..dependencies import get_current_cabinet_user
from app.database.models import User


router = APIRouter(prefix='/utils', tags=['Cabinet Utils'])


class TelegramProxyResponse(BaseModel):
    """Response containing Telegram proxy URL."""

    url: str | None


@router.get('/telegram-proxy', response_model=TelegramProxyResponse)
async def get_telegram_proxy(
    user: User = Depends(get_current_cabinet_user),
):
    """
    Get the free Telegram proxy URL.

    Requires valid JWT token.
    """
    url = settings.MTPROXY_URL or settings.TELEGRAM_PROXY_URL
    return TelegramProxyResponse(url=url)
