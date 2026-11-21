from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from ..k8s import actions, summarize
from ..k8s.watcher import WatchManager
from ..schemas import (
    ActionResult,
    KindsResponse,
    Overview,
    PodLogs,
    ResourceDetail,
    ResourceList,
    ResourceSummary,
    WatcherStatus,
)

router = APIRouter(prefix="/api/v1")


def _manager(request: Request) -> WatchManager:
    return request.app.state.manager


def _resolve(manager: WatchManager, kind: str) -> str:
    key = manager.resolve(kind)
    if key is None:
        raise HTTPException(404, f"지원하지 않는 kind: {kind} (지원: {', '.join(manager.registry)})")
    return key


def _summary(manager: WatchManager, key: str, obj: dict[str, Any]) -> dict[str, Any]:
    spec = manager.registry[key]
    return {**summarize.common(obj, spec.kind), "status": spec.status(obj)}


def _page(key: str, m: WatchManager, summaries: list[dict[str, Any]],
          limit: int | None, offset: int) -> dict[str, Any]:
    """정렬된 전체 목록에서 [offset, offset+limit) 만 잘라 반환. count는 전체 개수."""
    page = summaries[offset:offset + limit] if limit else summaries[offset:]
    return {
        "kind": key,
        "namespace": m.namespace,
        "synced": m.watchers[key].synced.is_set(),
        "count": len(summaries),
        "offset": offset,
        "limit": limit,
        "items": [ResourceSummary(**s) for s in page],
    }


def parse_label_selector(selector: str) -> list[tuple[str, str, str | None]]:
    """`a=b,c!=d,e,!f` 형태(등호 기반)만 지원."""
    reqs: list[tuple[str, str, str | None]] = []
    for part in filter(None, (p.strip() for p in selector.split(","))):
        if "!=" in part:
            k, v = part.split("!=", 1)
            reqs.append(("!=", k.strip(), v.strip()))
        elif "==" in part:
            k, v = part.split("==", 1)
            reqs.append(("=", k.strip(), v.strip()))
        elif "=" in part:
            k, v = part.split("=", 1)
            reqs.append(("=", k.strip(), v.strip()))
        elif part.startswith("!"):
            reqs.append(("!exists", part[1:].strip(), None))
        else:
            reqs.append(("exists", part, None))
    return reqs


def match_labels(labels: dict[str, str], reqs: list[tuple[str, str, str | None]]) -> bool:
    for op, k, v in reqs:
        if op == "=" and labels.get(k) != v:
            return False
        if op == "!=" and labels.get(k) == v:
            return False
        if op == "exists" and k not in labels:
            return False
        if op == "!exists" and k in labels:
            return False
    return True


@router.get("/kinds", response_model=KindsResponse)
def list_kinds(request: Request):
    m = _manager(request)
    return {
        "namespace": m.namespace,
        "kinds": [WatcherStatus(**w.status()) for w in m.watchers.values()],
    }


@router.get("/resources", response_model=Overview)
def overview(request: Request):
    m = _manager(request)
    return {
        "namespace": m.namespace,
        "synced": m.all_synced(),
        "counts": {key: len(cache) for key, cache in m.caches.items()},
    }


@router.get("/resources/{kind}", response_model=ResourceList)
def list_resources(
    request: Request,
    kind: str,
    label_selector: str | None = Query(None, description="예: app=mlflow,tier!=db"),
    limit: int | None = Query(None, ge=1, le=500, description="페이지 크기 (생략하면 전체)"),
    offset: int = Query(0, ge=0, description="페이지 시작 위치"),
):
    m = _manager(request)
    key = _resolve(m, kind)
    items = m.caches[key].list()
    if label_selector:
        reqs = parse_label_selector(label_selector)
        items = [o for o in items if match_labels((o.get("metadata") or {}).get("labels") or {}, reqs)]
    summaries = sorted((_summary(m, key, o) for o in items), key=lambda s: s["name"] or "")
    newest = m.registry[key].newest_first_by
    if newest:
        summaries.sort(key=lambda s: s["status"].get(newest) or "", reverse=True)
    return _page(key, m, summaries, limit, offset)


@router.get("/resources/{kind}/{name}", response_model=ResourceDetail)
def get_resource(request: Request, kind: str, name: str):
    m = _manager(request)
    key = _resolve(m, kind)
    obj = m.caches[key].get(name)
    if obj is None:
        raise HTTPException(404, f"{key}/{name} 를 {m.namespace} 네임스페이스에서 찾을 수 없습니다.")
    return {**_summary(m, key, obj), "manifest": obj, "actions": actions.describe_actions(m, key, obj)}


@router.get("/resources/{kind}/{name}/events", response_model=ResourceList)
def resource_events(
    request: Request,
    kind: str,
    name: str,
    limit: int | None = Query(None, ge=1, le=500, description="페이지 크기 (생략하면 전체)"),
    offset: int = Query(0, ge=0, description="페이지 시작 위치"),
):
    """이 리소스에 대한 이벤트 (involvedObject 일치), 최신순."""
    m = _manager(request)
    key = _resolve(m, kind)
    if "events" not in m.caches:
        raise HTTPException(404, "이벤트를 감시하지 않습니다.")
    target_kind = m.registry[key].kind
    items = [
        _summary(m, "events", o) for o in m.caches["events"].list()
        if (o.get("involvedObject") or {}).get("kind") == target_kind
        and (o.get("involvedObject") or {}).get("name") == name
    ]
    items.sort(key=lambda s: s["status"].get("last_seen") or "", reverse=True)
    return _page("events", m, items, limit, offset)


@router.get("/resources/{kind}/{name}/logs", response_model=PodLogs)
async def pod_logs(
    request: Request,
    kind: str,
    name: str,
    container: str | None = None,
    tail_lines: int = Query(200, ge=1, le=actions.MAX_TAIL_LINES),
    previous: bool = Query(False, description="직전에 죽은 컨테이너의 로그 (CrashLoop 원인 확인)"),
):
    m = _manager(request)
    if _resolve(m, kind) != "pods":
        raise HTTPException(400, "로그는 Pod만 조회할 수 있습니다.")
    try:
        # k8s 호출은 블로킹이라 스레드풀에서
        return await run_in_threadpool(actions.read_logs, m, name, container, tail_lines, previous)
    except actions.ActionError as e:
        raise HTTPException(e.status, e.message) from e


# ---------------------------------------------------------------------- 조작 (쓰기)
async def _run_action(request: Request, action: str, kind: str, name: str, confirm: bool,
                      fn: Callable[[WatchManager, str], dict[str, Any]]) -> dict[str, Any]:
    """확인(confirm) 검사 → 실행."""
    m = _manager(request)
    key = _resolve(m, kind)

    # "정말 하시겠습니까?" — 화면의 [예]를 누른 요청만 confirm=true 를 보낸다.
    if not confirm:
        raise HTTPException(400, f"확인이 필요합니다. '{key}/{name}' {action} 을(를) 진행하려면 confirm=true 를 함께 보내세요.")
    else:
        try:
            return await run_in_threadpool(fn, m, key)
        except actions.ActionError as e:
            raise HTTPException(e.status, e.message) from e


@router.delete("/resources/{kind}/{name}", response_model=ActionResult, status_code=202)
async def delete_resource(
    request: Request,
    kind: str,
    name: str,
    confirm: bool = Query(False, description="삭제 확인. true가 아니면 400"),
    uid: str | None = Query(None, description="화면에서 본 객체의 uid. 다르면 409 (다른 객체 삭제 방지)"),
):
    return await _run_action(request, "delete", kind, name, confirm,
                             lambda m, key: actions.delete(m, key, name, uid))


@router.post("/resources/{kind}/{name}/restart", response_model=ActionResult, status_code=202)
async def restart_resource(
    request: Request,
    kind: str,
    name: str,
    confirm: bool = Query(False, description="재시작 확인. true가 아니면 400"),
):
    return await _run_action(request, "restart", kind, name, confirm,
                             lambda m, key: actions.restart(m, key, name))

