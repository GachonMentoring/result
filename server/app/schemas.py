from typing import Any

from pydantic import BaseModel


class ResourceSummary(BaseModel):
    kind: str
    name: str
    namespace: str | None = None
    uid: str | None = None
    created_at: str | None = None
    labels: dict[str, str] = {}
    terminating: bool = False
    owner: dict[str, str] | None = None   # 이 리소스를 관리하는 컨트롤러 {kind, name}
    status: dict[str, Any] = {}


class ResourceDetail(ResourceSummary):
    manifest: dict[str, Any]   # kubectl get -o json 과 같은 원본
    # 가능한 조작과 선언 상태에 주는 영향: {"delete": {...}, "restart": {...}}
    actions: dict[str, dict[str, Any]] = {}


class ResourceList(BaseModel):
    kind: str
    namespace: str
    synced: bool
    count: int                  # 조건에 맞는 전체 개수 (페이지와 무관)
    offset: int = 0             # 이 페이지의 시작 위치
    limit: int | None = None    # 페이지 크기 (None = 전체)
    items: list[ResourceSummary]


class WatcherStatus(BaseModel):
    kind: str
    synced: bool
    alive: bool
    item_count: int
    resource_version: str | None = None
    last_sync_at: str | None = None
    last_event_at: str | None = None
    relist_count: int
    error_count: int = 0
    last_error: str | None = None


class KindsResponse(BaseModel):
    namespace: str
    kinds: list[WatcherStatus]


class Overview(BaseModel):
    namespace: str
    synced: bool
    counts: dict[str, int]


class ActionResult(BaseModel):
    action: str            # delete / restart
    kind: str
    name: str
    accepted: bool
    effect: str            # reconcile / drift / rollout
    message: str


class PodLogs(BaseModel):
    pod: str
    container: str | None
    containers: list[str]
    tail_lines: int
    previous: bool
    text: str
