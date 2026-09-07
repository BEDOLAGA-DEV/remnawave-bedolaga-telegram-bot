"""Фейки для сервиса задач: часы и клиент bschekbot API по сценарию."""

from __future__ import annotations

from typing import Any, Self


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeAPI:
    """Ответы по сценарию: dict возвращается, Exception поднимается; последний ответ повторяется."""

    def __init__(self, script: dict[str, list[Any]] | None = None) -> None:
        self.script = {name: list(items) for name, items in (script or {}).items()}
        self.calls: list[tuple[str, tuple]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def _next(self, name: str, *args: Any) -> Any:
        self.calls.append((name, args))
        queue = self.script.get(name) or []
        if not queue:
            raise AssertionError(f'FakeAPI: нет ответа для {name}{args}')
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def probe(self, body: dict, key: str) -> dict:
        return self._next('probe', key)

    async def cancel_probe(self, key: str) -> dict:
        return self._next('cancel_probe', key)

    async def start_vless(self, body: dict, key: str) -> dict:
        return self._next('start_vless', key)

    async def get_vless(self, test_id: int) -> dict:
        return self._next('get_vless', test_id)

    async def cancel_vless(self, test_id: int) -> dict:
        return self._next('cancel_vless', test_id)

    async def start_scan(self, body: dict, key: str) -> dict:
        return self._next('start_scan', key)

    async def get_scan(self, scan_id: int) -> dict:
        return self._next('get_scan', scan_id)

    async def cancel_scan(self, scan_id: int) -> dict:
        return self._next('cancel_scan', scan_id)
