from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Security, WebSocket, WebSocketDisconnect
from fastapi.security import APIKeyHeader

from app.services.event_emitter import event_emitter
from app.services.web_api_token_service import web_api_token_service
from app.database.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()

api_key_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_websocket_token(
    websocket: WebSocket,
    token: str | None = None,
) -> bool:
    """Проверить токен для WebSocket подключения."""
    if not token:
        # Пытаемся получить токен из query параметров
        token = websocket.query_params.get("token") or websocket.query_params.get("api_key")

    if not token:
        return False

    async with AsyncSessionLocal() as db:
        try:
            webhook_token = await web_api_token_service.authenticate(
                db,
                token,
                remote_ip=websocket.client.host if websocket.client else None,
            )
            return webhook_token is not None
        except Exception as error:
            logger.warning("WebSocket authentication error: %s", error)
            return False


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint для real-time обновлений."""
    await websocket.accept()

    # Проверяем авторизацию
    token = websocket.query_params.get("token") or websocket.query_params.get("api_key")
    if not await verify_websocket_token(websocket, token):
        await websocket.close(code=1008, reason="Unauthorized")
        return

    # Регистрируем подключение
    event_emitter.register_websocket(websocket)
    logger.info("WebSocket client connected from %s", websocket.client.host if websocket.client else "unknown")

    try:
        # Отправляем приветственное сообщение
        await websocket.send_json({
            "type": "connection",
            "status": "connected",
            "message": "WebSocket connection established",
        })

        # Обрабатываем входящие сообщения (ping/pong для keepalive)
        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)

                # Обработка ping
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                # Можно добавить другие типы сообщений (подписки на конкретные события и т.д.)

            except json.JSONDecodeError:
                logger.warning("Invalid JSON received from WebSocket client")
            except WebSocketDisconnect:
                break
            except Exception as error:
                logger.exception("Error processing WebSocket message: %s", error)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as error:
        logger.exception("WebSocket error: %s", error)
    finally:
        # Отменяем регистрацию при отключении
        event_emitter.unregister_websocket(websocket)

