"""캐시된 원본(dict) → 목록 화면용 요약.

입력은 kubectl -o json 과 같은 camelCase dict 입니다.
"""

from typing import Any


def _get(d: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def controller_owner(obj: dict[str, Any]) -> dict[str, str] | None:
    """이 리소스를 관리(재생성)하는 컨트롤러. ownerReferences 중 controller=true 인 것."""
    for ref in _get(obj, "metadata", "ownerReferences", default=[]):
        if ref.get("controller"):
            return {"kind": ref.get("kind"), "name": ref.get("name")}
    return None


def common(obj: dict[str, Any], kind: str) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    return {
        "kind": kind,
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "uid": meta.get("uid"),
        "created_at": meta.get("creationTimestamp"),
        "labels": meta.get("labels") or {},
        # 삭제 요청은 받았지만 finalizer/종료 대기 중 (kubectl 의 Terminating)
        "terminating": bool(meta.get("deletionTimestamp")),
        "owner": controller_owner(obj),
    }


def pod_status(obj: dict[str, Any]) -> dict[str, Any]:
    status = obj.get("status") or {}
    containers = status.get("containerStatuses") or []
    ready = sum(1 for c in containers if c.get("ready"))
    total = len(_get(obj, "spec", "containers", default=[])) or len(containers)

    # kubectl get pods 의 STATUS 컬럼과 비슷하게 표시
    display = status.get("reason") or status.get("phase") or "Unknown"
    for c in containers:
        waiting = _get(c, "state", "waiting", "reason")
        terminated = _get(c, "state", "terminated", "reason")
        if waiting:
            display = waiting
            break
        if terminated and display == "Running":
            display = terminated
    if _get(obj, "metadata", "deletionTimestamp"):
        display = "Terminating"

    return {
        "phase": status.get("phase"),
        "display_status": display,
        "ready": f"{ready}/{total}",
        "restarts": sum(c.get("restartCount", 0) for c in containers),
        "node": _get(obj, "spec", "nodeName"),
        "pod_ip": status.get("podIP"),
    }


def deployment_status(obj: dict[str, Any]) -> dict[str, Any]:
    status = obj.get("status") or {}
    return {
        "replicas": _get(obj, "spec", "replicas", default=1),
        "ready_replicas": status.get("readyReplicas", 0),
        "updated_replicas": status.get("updatedReplicas", 0),
        "available_replicas": status.get("availableReplicas", 0),
    }


def statefulset_status(obj: dict[str, Any]) -> dict[str, Any]:
    status = obj.get("status") or {}
    return {
        "replicas": _get(obj, "spec", "replicas", default=1),
        "ready_replicas": status.get("readyReplicas", 0),
        "current_replicas": status.get("currentReplicas", 0),
    }


def service_status(obj: dict[str, Any]) -> dict[str, Any]:
    spec = obj.get("spec") or {}
    ports = []
    for p in spec.get("ports") or []:
        text = str(p.get("port"))
        if p.get("nodePort"):
            text += f":{p['nodePort']}"
        text += f"/{p.get('protocol', 'TCP')}"
        ports.append(text)
    return {
        "type": spec.get("type"),
        "cluster_ip": spec.get("clusterIP"),
        "ports": ports,
        "selector": spec.get("selector") or {},
    }


def pvc_status(obj: dict[str, Any]) -> dict[str, Any]:
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    return {
        "phase": status.get("phase"),                         # Pending / Bound / Lost
        "volume": spec.get("volumeName"),                     # 바인딩된 PV 이름
        "requested": _get(spec, "resources", "requests", "storage"),
        "capacity": _get(status, "capacity", "storage"),      # 실제 할당 용량 (Bound 이후)
        "access_modes": status.get("accessModes") or spec.get("accessModes") or [],
        "storage_class": spec.get("storageClassName"),
        "volume_mode": spec.get("volumeMode"),
    }


def job_status(obj: dict[str, Any]) -> dict[str, Any]:
    status = obj.get("status") or {}
    state = "Running" if status.get("active") else "Pending"
    for cond in status.get("conditions") or []:
        if cond.get("status") == "True" and cond.get("type") in ("Complete", "Failed", "Suspended"):
            state = cond["type"]
            break
    return {
        "state": state,
        "completions": _get(obj, "spec", "completions", default=1),
        "active": status.get("active", 0),
        "succeeded": status.get("succeeded", 0),
        "failed": status.get("failed", 0),
        "start_time": status.get("startTime"),
        "completion_time": status.get("completionTime"),
    }


def event_status(obj: dict[str, Any]) -> dict[str, Any]:
    """core/v1 Event. 시각 필드는 생성 주체에 따라 채워지는 곳이 달라 순서대로 대체."""
    involved = obj.get("involvedObject") or {}
    last_seen = (
        obj.get("lastTimestamp")
        or _get(obj, "series", "lastObservedTime")
        or obj.get("eventTime")
        or _get(obj, "metadata", "creationTimestamp")
    )
    return {
        "type": obj.get("type"),                 # Normal / Warning
        "reason": obj.get("reason"),
        "object": f"{involved.get('kind')}/{involved.get('name')}",
        "object_kind": involved.get("kind"),
        "object_name": involved.get("name"),
        "message": obj.get("message"),
        "count": obj.get("count") or _get(obj, "series", "count", default=1),
        "last_seen": last_seen,
        "source": _get(obj, "source", "component") or obj.get("reportingComponent"),
    }
