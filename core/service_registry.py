"""Dependency-light service registry with ordered startup and reverse teardown."""
from __future__ import annotations

import inspect
from typing import Any


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._started: list[str] = []
        self._closed = False

    def register(self, name: str, service: Any) -> None:
        if self._closed:
            raise RuntimeError("service registry is closed")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("service name must be a non-empty string")
        name = name.strip()
        if name in self._services:
            raise ValueError(f"service already registered: {name}")
        self._services[name] = service

    def get(self, name: str) -> Any:
        try:
            return self._services[name]
        except KeyError as exc:
            raise LookupError(f"service not registered: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(self._services)

    async def start_all(self) -> None:
        if self._closed:
            raise RuntimeError("service registry is closed")
        try:
            for name, service in self._services.items():
                start = getattr(service, "start", None)
                if callable(start):
                    result = start()
                    if inspect.isawaitable(result):
                        await result
                self._started.append(name)
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self._closed:
            return
        errors: list[Exception] = []
        for name in reversed(self._started):
            service = self._services[name]
            stop = getattr(service, "close", None) or getattr(service, "stop", None)
            if callable(stop):
                try:
                    result = stop()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # teardown all services even after one failure
                    errors.append(exc)
        self._started.clear()
        self._closed = True
        if errors:
            summary = ", ".join(type(error).__name__ for error in errors)
            raise RuntimeError(f"one or more services failed to stop: {summary}") from errors[0]
