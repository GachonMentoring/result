"""리소스 조작: 삭제 / 롤아웃 재시작 / 파드 로그.

핵심 원칙 — 선언형 관리 (base.md)
  이 플랫폼이 하는 조작이 "선언(매니페스트)과 실제 상태의 일치"에 어떤 영향을 주는지
  조작 전에 항상 계산해서 보여준다 (effect).

  reconcile : 선언은 그대로. 컨트롤러가 선언에 맞춰 다시 만들어 원래 상태로 되돌린다. (셀프힐링)
              예) Deployment가 관리하는 Pod 삭제
  drift     : 선언된 리소스 자체가 클러스터에서 사라진다. 매니페스트(YAML)는 그대로 남아 있으므로
              매니페스트와 실제 인프라가 불일치하게 된다. 영구 삭제라면 매니페스트에서도 지워야 한다.
              예) Deployment / Service / PVC 삭제
  rollout   : spec(이미지·replicas 등 선언 내용)은 바꾸지 않고, pod template에 재시작 시각 어노테이션
              하나만 기록 → 컨트롤러가 파드를 순차 교체. kubectl rollout restart 와 동일.
              이 어노테이션은 매니페스트에 없는 값이므로 이후 drift 비교에서는 제외 대상.

replicas 변경(스케일)은 선언을 API로 직접 바꾸는 조작이라 이 Work에서는 제공하지 않는다.
(매니페스트를 고쳐서 apply 하는 이후 Work에서 다룬다)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError as Urllib3Error

if TYPE_CHECKING:
    from .watcher import WatchManager

PROTECTED_LABEL = "platform.mentor/protected"
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
MAX_TAIL_LINES = 2000


class ActionError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _meta(obj: dict[str, Any]) -> dict[str, Any]:
    return obj.get("metadata") or {}


def _owner(obj: dict[str, Any]) -> dict[str, str] | None:
    for ref in _meta(obj).get("ownerReferences") or []:
        if ref.get("controller"):
            return {"kind": ref.get("kind"), "name": ref.get("name")}
    return None


def _selected_pods(manager: WatchManager, obj: dict[str, Any]) -> int | None:
    """spec.selector.matchLabels 에 해당하는 파드 수 (pods를 감시하지 않으면 None)."""
    cache = manager.caches.get("pods")
    match = ((obj.get("spec") or {}).get("selector") or {}).get("matchLabels")
    if cache is None or not match:
        return None
    return sum(
        1 for p in cache.list()
        if all(((_meta(p).get("labels") or {}).get(k) == v) for k, v in match.items())
    )


def k8s_error(e: Exception) -> ActionError:
    """kubernetes 클라이언트 예외 → 화면에 보여줄 HTTP 상태 + 메시지."""
    if isinstance(e, ApiException):
        messages = {
            403: "권한이 없습니다. (ServiceAccount Role에 해당 verb가 있는지 확인)",
            404: "클러스터에 해당 리소스가 없습니다. (이미 삭제됨)",
            409: "충돌: 리소스가 그 사이 바뀌었거나 다른 객체로 교체되었습니다. 새로고침 후 다시 시도하세요.",
            422: "요청이 거부되었습니다. (잘못된 patch)",
        }
        if e.status in messages:
            return ActionError(400 if e.status == 422 else e.status, messages[e.status])
        if e.status == 400:
            # 예: 로그 요청 시 컨테이너가 아직 시작되지 않음
            return ActionError(400, f"Kubernetes API가 요청을 거부했습니다: {str(e.body or e.reason)[:300]}")
        return ActionError(502, f"Kubernetes API 오류 {e.status}: {e.reason}")
    if isinstance(e, Urllib3Error):
        return ActionError(502, f"Kubernetes API 서버에 연결할 수 없습니다: {type(e).__name__}")
    return ActionError(500, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------- 조작 전 판단
def plan_delete(manager: WatchManager, key: str, obj: dict[str, Any]) -> dict[str, Any]:
    spec = manager.registry[key]
    meta = _meta(obj)
    name = meta.get("name")

    if spec.delete_func is None:
        return {"allowed": False, "status": 400, "reason": f"{spec.kind}는 조회 전용입니다."}
    if (meta.get("labels") or {}).get(PROTECTED_LABEL) == "true":
        return {"allowed": False, "status": 403,
                "reason": f"보호 라벨({PROTECTED_LABEL}=true)이 있는 리소스는 삭제할 수 없습니다."}
    if meta.get("deletionTimestamp"):
        return {"allowed": False, "status": 409, "reason": "이미 삭제가 진행 중입니다. (Terminating)"}

    details: list[str] = []
    owner = _owner(obj)
    if owner:
        effect = "reconcile"
        message = (f"{owner['kind']} '{owner['name']}'이(가) 관리하는 {spec.kind}입니다. "
                   "선언된 상태는 그대로이므로 컨트롤러가 새로 만들어 원래 상태로 되돌립니다. (셀프힐링)")
        if key == "pods" and owner["kind"] == "StatefulSet":
            details.append("StatefulSet 파드는 같은 이름으로 다시 생성됩니다.")
        if key == "pods" and owner["kind"] == "Job":
            details.append("Job이 아직 완료되지 않았다면 새 파드로 다시 실행됩니다.")
    else:
        effect = "drift"
        message = (f"컨트롤러가 관리하지 않는 {spec.kind}라 다시 생성되지 않습니다. "
                   "이 리소스를 선언한 매니페스트(YAML)는 그대로 남으므로 선언과 실제 인프라가 불일치하게 됩니다. "
                   "영구 삭제라면 매니페스트에서도 제거하세요.")

    if key in ("deployments", "statefulsets", "jobs"):
        n = _selected_pods(manager, obj)
        details.append(f"관리 중인 파드{'' if n is None else f' {n}개'}도 함께 삭제됩니다.")
    elif key == "services":
        details.append("이 Service로 들어오는 트래픽이 끊깁니다. (파드는 그대로)")
    elif key == "persistentvolumeclaims":
        details.append("저장된 데이터가 삭제될 수 있습니다. (PV의 reclaimPolicy에 따름)")
        details.append("사용 중인 파드가 있으면 파드가 없어질 때까지 Terminating 상태로 남습니다.")

    return {"allowed": True, "effect": effect, "message": message, "details": details,
            "target": f"{key}/{name}"}


def plan_restart(manager: WatchManager, key: str, obj: dict[str, Any]) -> dict[str, Any]:
    spec = manager.registry[key]
    meta = _meta(obj)
    if spec.restart_func is None:
        hint = " 컨트롤러가 관리하는 파드라면 삭제하면 새 파드로 다시 만들어집니다." if key == "pods" else ""
        return {"allowed": False, "status": 400,
                "reason": f"{spec.kind}는 롤아웃 재시작을 지원하지 않습니다. (Deployment/StatefulSet만){hint}"}
    if meta.get("deletionTimestamp"):
        return {"allowed": False, "status": 409, "reason": "삭제가 진행 중인 리소스입니다."}
    if (obj.get("spec") or {}).get("paused"):
        return {"allowed": False, "status": 409,
                "reason": "일시정지(paused)된 Deployment는 재시작할 수 없습니다. (kubectl rollout resume 먼저)"}
    n = _selected_pods(manager, obj)
    return {
        "allowed": True,
        "effect": "rollout",
        "message": ("이미지·replicas 등 선언 내용(spec)은 바꾸지 않고, pod template에 재시작 시각 어노테이션만 "
                    "기록합니다. 컨트롤러가 이를 감지해 파드를 순차적으로 새로 교체합니다. (kubectl rollout restart와 동일)"),
        "details": [
            f"교체 대상 파드{'' if n is None else f' {n}개'}",
            f"'{RESTART_ANNOTATION}' 어노테이션은 매니페스트에 없는 값이므로 이후 선언·실제 비교에서 제외 대상입니다.",
        ],
        "target": f"{key}/{meta.get('name')}",
    }


def describe_actions(manager: WatchManager, key: str, obj: dict[str, Any]) -> dict[str, Any]:
    """상세 화면용: 이 리소스에 가능한 조작과 그 영향."""
    out = {"delete": plan_delete(manager, key, obj)}
    if manager.registry[key].restart_func is not None:
        out["restart"] = plan_restart(manager, key, obj)
    return out


def _require(plan: dict[str, Any]) -> None:
    if not plan["allowed"]:
        raise ActionError(plan["status"], plan["reason"])


def _cached(manager: WatchManager, key: str, name: str) -> dict[str, Any]:
    obj = manager.caches[key].get(name)
    if obj is None:
        raise ActionError(404, f"{key}/{name} 를 {manager.namespace} 네임스페이스에서 찾을 수 없습니다.")
    return obj


# ---------------------------------------------------------------------- 실행
def delete(manager: WatchManager, key: str, name: str, uid: str | None = None) -> dict[str, Any]:
    obj = _cached(manager, key, name)
    plan = plan_delete(manager, key, obj)
    _require(plan)
    if uid and _meta(obj).get("uid") != uid:
        raise ActionError(409, "화면에서 본 리소스가 이미 다른 객체로 교체되었습니다. (uid 불일치) 새로고침 후 다시 확인하세요.")

    body = client.V1DeleteOptions(
        # kubectl 기본값과 동일. Python 클라이언트로 Job을 그냥 지우면 Orphan이 되어 파드가 남는다.
        propagation_policy="Background",
        # 확인창에서 본 바로 그 객체만 삭제 (그 사이 같은 이름으로 재생성된 객체를 지우지 않도록)
        preconditions=client.V1Preconditions(uid=uid or _meta(obj).get("uid")),
    )
    try:
        manager.registry[key].delete_func(name=name, namespace=manager.namespace, body=body)
    except Exception as e:
        raise k8s_error(e) from e
    return {"action": "delete", "kind": key, "name": name, "accepted": True,
            "effect": plan["effect"], "message": "삭제를 요청했습니다. " + plan["message"]}


def restart(manager: WatchManager, key: str, name: str) -> dict[str, Any]:
    obj = _cached(manager, key, name)
    plan = plan_restart(manager, key, obj)
    _require(plan)
    at = _now()
    patch = {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: at}}}}}
    try:
        manager.registry[key].restart_func(
            name=name, namespace=manager.namespace, body=patch,
            _content_type="application/strategic-merge-patch+json",
        )
    except Exception as e:
        raise k8s_error(e) from e
    return {"action": "restart", "kind": key, "name": name, "accepted": True, "effect": "rollout",
            "message": f"롤아웃 재시작을 요청했습니다. ({RESTART_ANNOTATION}={at}) 컨트롤러가 파드를 순차 교체합니다."}


def read_logs(manager: WatchManager, name: str, container: str | None, tail_lines: int,
              previous: bool = False) -> dict[str, Any]:
    spec = manager.registry.get("pods")
    if spec is None or spec.log_func is None:
        raise ActionError(400, "로그 조회를 지원하지 않습니다.")
    obj = _cached(manager, "pods", name)
    containers = [c.get("name") for c in (obj.get("spec") or {}).get("containers") or []]
    if container is None:
        container = containers[0] if containers else None
    elif container not in containers:
        raise ActionError(400, f"컨테이너 '{container}'가 없습니다. (있는 것: {', '.join(containers)})")
    tail = max(1, min(tail_lines, MAX_TAIL_LINES))
    try:
        text = spec.log_func(name=name, namespace=manager.namespace, container=container,
                             tail_lines=tail, previous=previous, timestamps=False)
    except Exception as e:
        raise k8s_error(e) from e
    return {"pod": name, "container": container, "containers": containers,
            "tail_lines": tail, "previous": previous, "text": text or ""}
