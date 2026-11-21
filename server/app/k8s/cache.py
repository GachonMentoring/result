"""리소스 종류별 스레드 안전 메모리 캐시. 값은 kubectl -o json 과 같은 형태의 dict."""

import copy
import threading
from typing import Any


class ResourceCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}

    def replace(self, items: dict[str, dict[str, Any]]) -> None:
        """re-list 결과로 통째로 교체 (끊긴 동안 놓친 삭제까지 반영)."""
        new_items = dict(items)
        with self._lock:
            self._items = new_items

    def upsert(self, name: str, obj: dict[str, Any]) -> None:
        with self._lock:
            self._items[name] = obj

    def delete(self, name: str) -> None:
        with self._lock:
            self._items.pop(name, None)

    def get(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            obj = self._items.get(name)
        return copy.deepcopy(obj) if obj is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items.values())
        return copy.deepcopy(items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
