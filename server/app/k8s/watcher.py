"""리소스 종류별 List → Watch 루프 (Informer 패턴의 단순화 버전).

    LIST  → 캐시 통째로 교체, resourceVersion(RV) 저장
    WATCH(RV, bookmark 허용, timeout)
      ADDED/MODIFIED → upsert,  DELETED → delete,  BOOKMARK → RV만 갱신
      스트림 정상 종료(timeout) → 마지막 RV로 다시 WATCH
      410 Gone              → RE-LIST 후 WATCH
      그 외 에러             → 지수 백오프 후 재시도 (RV가 유효하면 WATCH부터)

410은 두 경로로 들어올 수 있어 둘 다 처리한다.
  1) ApiException(status=410)  — 최신 python 클라이언트는 ERROR 이벤트를 예외로 바꿔 던짐
  2) type=ERROR 이벤트, raw_object.code == 410 — 구버전 클라이언트 / 방어용
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from kubernetes import watch
from kubernetes.client.exceptions import ApiException

from .cache import ResourceCache
from .registry import KindSpec

log = logging.getLogger(__name__)

HTTP_GONE = 410
MAX_BACKOFF_SECONDS = 30.0


class ResourceGone(Exception):
    """저장된 resourceVersion이 너무 오래되어 re-list가 필요함."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResourceWatcher(threading.Thread):
    def __init__(
        self,
        spec: KindSpec,
        namespace: str,
        cache: ResourceCache,
        stop_event: threading.Event,
        timeout_seconds: int = 300,
        watch_factory: Callable[[], Any] = watch.Watch,
    ) -> None:
        super().__init__(name=f"watch-{spec.key}", daemon=True)
        self.spec = spec
        self.namespace = namespace
        self.cache = cache
        self.stop_event = stop_event
        self.timeout_seconds = timeout_seconds
        self.watch_factory = watch_factory

        self.synced = threading.Event()   # 첫 LIST 완료 여부
        self.resource_version: str | None = None
        self.last_sync_at: str | None = None
        self.last_event_at: str | None = None
        self.last_error: str | None = None
        self.relist_count = 0
        self.error_count = 0              # Watch 오류·재연결 횟수 (/metrics)
        self._current_watch: Any = None

    # ---------------------------------------------------------------- 루프
    def run(self) -> None:
        need_list = True
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                if need_list:
                    self._list()
                    need_list = False
                self._watch_once()
                backoff = 1.0
            except ResourceGone:
                self._mark_gone()
                need_list = True
            except ApiException as e:
                if e.status == HTTP_GONE:
                    self._mark_gone()
                    need_list = True
                    continue
                backoff = self._fail(f"ApiException {e.status}: {e.reason}", backoff)
            except Exception as e:  # 네트워크 끊김 등
                backoff = self._fail(f"{type(e).__name__}: {e}", backoff)

    def stop(self) -> None:
        self.stop_event.set()
        w = self._current_watch
        if w is not None:
            w.stop()

    # ---------------------------------------------------------------- 단계
    def _list(self) -> None:
        resp = self.spec.list_func(namespace=self.namespace, _preload_content=False)
        body = json.loads(resp.data)
        items: dict[str, dict[str, Any]] = {}
        for obj in body.get("items") or []:
            # list 응답의 item에는 kind/apiVersion이 비어 있어 채워 둔다 (watch 이벤트와 형태 통일)
            obj.setdefault("kind", self.spec.kind)
            obj.setdefault("apiVersion", self.spec.api_version)
            items[obj["metadata"]["name"]] = obj
        self.cache.replace(items)
        self.resource_version = (body.get("metadata") or {}).get("resourceVersion")
        self.last_sync_at = _now()
        self.last_error = None
        self.synced.set()
        log.info("[%s] list 완료: %d개, rv=%s", self.spec.key, len(items), self.resource_version)

    def _watch_once(self) -> None:
        w = self.watch_factory()
        self._current_watch = w
        try:
            stream = w.stream(
                self.spec.list_func,
                namespace=self.namespace,
                resource_version=self.resource_version,
                allow_watch_bookmarks=True,
                timeout_seconds=self.timeout_seconds,
                # 서버 timeout보다 약간 길게: 네트워크가 조용히 끊겨도 무한 대기하지 않도록
                _request_timeout=self.timeout_seconds + 30,
            )
            for event in stream:
                if self.stop_event.is_set():
                    return
                self._handle(event)
        finally:
            w.stop()
            self._current_watch = None

    def _handle(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        raw = event.get("raw_object") or {}

        if etype == "ERROR":
            code = raw.get("code")
            if code == HTTP_GONE:
                raise ResourceGone(raw.get("message"))
            raise ApiException(status=code, reason=f"{raw.get('reason')}: {raw.get('message')}")

        meta = raw.get("metadata") or {}
        rv = meta.get("resourceVersion")
        if etype == "BOOKMARK":
            if rv:
                self.resource_version = rv
            return

        name = meta.get("name")
        if etype in ("ADDED", "MODIFIED"):
            self.cache.upsert(name, raw)
        elif etype == "DELETED":
            self.cache.delete(name)
        else:
            log.warning("[%s] 알 수 없는 이벤트 타입: %s", self.spec.key, etype)
            return

        if rv:
            self.resource_version = rv
        self.last_event_at = _now()
        log.debug("[%s] %s %s (rv=%s)", self.spec.key, etype, name, rv)

    # ---------------------------------------------------------------- 보조
    def _mark_gone(self) -> None:
        self.relist_count += 1
        log.info("[%s] 410 Gone (rv=%s) → re-list", self.spec.key, self.resource_version)

    def _fail(self, message: str, backoff: float) -> float:
        if self.stop_event.is_set():
            return backoff  # 종료 중 끊긴 연결은 오류가 아님
        self.last_error = message
        self.error_count += 1
        log.warning("[%s] watch 오류: %s (%.0f초 후 재시도)", self.spec.key, message, backoff)
        self.stop_event.wait(backoff)
        return min(backoff * 2, MAX_BACKOFF_SECONDS)

    def status(self) -> dict[str, Any]:
        return {
            "kind": self.spec.key,
            "synced": self.synced.is_set(),
            "alive": self.is_alive(),
            "item_count": len(self.cache),
            "resource_version": self.resource_version,
            "last_sync_at": self.last_sync_at,
            "last_event_at": self.last_event_at,
            "relist_count": self.relist_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
        }


class WatchManager:
    """모든 ResourceWatcher를 시작/정지하고 캐시를 제공."""

    def __init__(
        self,
        registry: dict[str, KindSpec],
        namespace: str,
        timeout_seconds: int = 300,
        watch_factory: Callable[[], Any] = watch.Watch,
    ) -> None:
        self.registry = registry
        self.namespace = namespace
        self.stop_event = threading.Event()
        self.caches = {key: ResourceCache() for key in registry}
        self.watchers = {
            key: ResourceWatcher(spec, namespace, self.caches[key], self.stop_event,
                                 timeout_seconds, watch_factory)
            for key, spec in registry.items()
        }
        self._alias = {key: key for key in registry}
        for key, spec in registry.items():
            for a in spec.aliases:
                self._alias[a] = key

    def start(self) -> None:
        for w in self.watchers.values():
            w.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        for w in self.watchers.values():
            w.stop()
        for w in self.watchers.values():
            w.join(join_timeout)

    def resolve(self, kind: str) -> str | None:
        return self._alias.get(kind.lower())

    def all_synced(self) -> bool:
        return all(w.synced.is_set() for w in self.watchers.values())
