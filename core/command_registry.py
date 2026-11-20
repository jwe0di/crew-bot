"""Canonical, validated command-name registry independent of Discord.py."""
from __future__ import annotations


class CommandRegistry:
    def __init__(self) -> None:
        self._names: tuple[str, ...] = ()

    def refresh(self, names) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for name in names:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("command names must be non-empty strings")
            value = name.strip()
            if value in seen:
                raise ValueError(f"duplicate command name: {value}")
            seen.add(value)
            normalized.append(value)
        self._names = tuple(sorted(normalized))
        return self._names

    def contains(self, name: str) -> bool:
        return name in self._names

    def names(self) -> tuple[str, ...]:
        return self._names
