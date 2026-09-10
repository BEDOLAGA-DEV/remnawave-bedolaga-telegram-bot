"""След пользователя в кабинете: каждый открытый экран и каждое нажатие.

Изменения (покупка, продление, триал) зависимость авторизации пишет сама по
HTTP-методу. Просмотры и нажатия так не поймать: открытие экрана — несколько
GET-ов, а нажатие «скопировать ключ» на сервер не ходит вовсе. Поэтому кабинет
присылает события пачкой, а сервер маскирует секреты в путях, схлопывает
повторные открытия экрана и держит лимит нажатий на человека
(см. ``user_action_log_service``).
"""

from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from app.database.models import User
from app.services.user_action_log_service import CLICK_LABEL_MAX, schedule_click_log, schedule_screen_view_log

from ..dependencies import get_current_cabinet_user


router = APIRouter(prefix='/activity', tags=['Cabinet Activity'])

# Путь экрана без query и фрагмента: там бывают токены, им в журнале не место.
SCREEN_PATH_PATTERN = r'^/[^?#\s]*$'
EVENTS_BATCH_MAX = 50


class ActivityEvent(BaseModel):
    kind: Literal['screen', 'click']
    path: str = Field(..., min_length=1, max_length=200, pattern=SCREEN_PATH_PATTERN)
    # Подпись нажатой кнопки; у экрана пустая.
    label: str | None = Field(None, max_length=CLICK_LABEL_MAX)


class ActivityEventsRequest(BaseModel):
    events: list[ActivityEvent] = Field(..., min_length=1, max_length=EVENTS_BATCH_MAX)


@router.post('/events', status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def report_activity_events(
    request: ActivityEventsRequest,
    user: User = Depends(get_current_cabinet_user),
) -> Response:
    """Принять пачку событий (fire-and-forget, ответ не ждёт записи)."""
    for event in request.events:
        if event.kind == 'screen':
            schedule_screen_view_log(user.id, event.path)
        elif event.label:
            schedule_click_log(user.id, event.path, event.label)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
