"""След пользователя в кабинете: кабинет сообщает об открытии каждого экрана.

Изменения (покупка, продление, триал) зависимость авторизации пишет сама по
HTTP-методу. Просмотры так не поймать: открытие одного экрана — это несколько
GET-ов, а часть экранов вообще не ходит на сервер. Поэтому кабинет присылает
путь экрана при каждом переходе, а сервер маскирует секреты в пути и схлопывает
повторы (см. ``user_action_log_service``).
"""

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from app.database.models import User
from app.services.user_action_log_service import schedule_screen_view_log

from ..dependencies import get_current_cabinet_user


router = APIRouter(prefix='/activity', tags=['Cabinet Activity'])

# Путь экрана без query и фрагмента: там бывают токены, им в журнале не место.
SCREEN_PATH_PATTERN = r'^/[^?#\s]*$'


class ScreenViewRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=200, pattern=SCREEN_PATH_PATTERN)


@router.post('/screen', status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def report_screen_view(
    request: ScreenViewRequest,
    user: User = Depends(get_current_cabinet_user),
) -> Response:
    """Записать открытие экрана кабинета (fire-and-forget, ответ не ждёт записи)."""
    schedule_screen_view_log(user.id, request.path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
