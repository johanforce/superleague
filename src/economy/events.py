# -*- coding: utf-8 -*-
"""Small synchronous event bus used to decouple economy from match simulation."""
from __future__ import annotations
from collections import defaultdict
from typing import Callable, Any


class EventBus:
    def __init__(self):
        self._listeners: dict[str, list[Callable[..., Any]]] = defaultdict(list)

    def subscribe(self, event: str, callback: Callable[..., Any]) -> Callable[..., Any]:
        if not callable(callback):
            raise TypeError("Event listener phải callable.")
        self._listeners[str(event)].append(callback)
        return callback

    def unsubscribe(self, event: str, callback: Callable[..., Any]) -> None:
        listeners = self._listeners.get(str(event), [])
        if callback in listeners:
            listeners.remove(callback)

    def publish(self, event: str, **payload) -> list[Any]:
        results = []
        for callback in tuple(self._listeners.get(str(event), ())):
            results.append(callback(**payload))
        return results
