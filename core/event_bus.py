"""Small asynchronous in-process event bus with deterministic lifecycle.

Handlers are called sequentially in subscription order. A failing handler is
reported in the PublishResult and does not prevent later handlers from
receiving the event. The bus is process-local; durable delivery belongs to the
Task Journal introduced in a later roadmap phase.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

EventHandler = Callable[[Any], Any | Awaitable[Any]]


@dataclass(frozen=True)
class PublishResult:
    event_name: str
    delivered: int
    failed: int


class EventBus:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._logger = logger or logging.getLogger(__name__)

    async def subscribe(self, event_name: str, handler: EventHandler) -> None:
        name = self._validate_name(event_name)
        if not callable(handler):
            raise TypeError("event handler must be callable")
        async with self._lock:
            self._ensure_open()
            handlers = self._handlers.setdefault(name, [])
            if handler not in handlers:
                handlers.append(handler)

    async def unsubscribe(self, event_name: str, handler: EventHandler) -> bool:
        name = self._validate_name(event_name)
        async with self._lock:
            handlers = self._handlers.get(name)
            if not handlers or handler not in handlers:
                return False
            handlers.remove(handler)
            if not handlers:
                self._handlers.pop(name, None)
            return True

    async def publish(self, event_name: str, payload: Any = None) -> PublishResult:
        name = self._validate_name(event_name)
        async with self._lock:
            self._ensure_open()
            handlers = tuple(self._handlers.get(name, ()))
        delivered = failed = 0
        for handler in handlers:
            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    await result
                delivered += 1
            except Exception:
                failed += 1
                self._logger.exception("Event handler failed for %s", name)
        return PublishResult(name, delivered, failed)

    async def close(self) -> None:
        async with self._lock:
            self._handlers.clear()
            self._closed = True

    @staticmethod
    def _validate_name(event_name: str) -> str:
        if not isinstance(event_name, str) or not event_name.strip():
            raise ValueError("event name must be a non-empty string")
        return event_name.strip()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("event bus is closed")
