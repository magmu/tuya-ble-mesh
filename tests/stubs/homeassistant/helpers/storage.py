"""Minimal stub for homeassistant.helpers.storage."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class Store:
    def __init__(self, hass: Any, version: int, key: str) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self._data: Any = None

    async def async_load(self) -> Any:
        return self._data

    async def async_save(self, data: Any) -> None:
        self._data = data

    def async_delay_save(self, data_func: Callable[[], Any], delay: float = 0) -> None:
        self._data = data_func()
