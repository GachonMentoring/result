"""감시할 리소스 종류 정의. 종류를 추가하려면 build_registry()에 한 줄 추가.

조작(쓰기) 함수도 여기서 kind별로 정의한다. None 이면 그 조작을 지원하지 않는 kind.
  delete_func  : delete_namespaced_*            (삭제)
  restart_func : patch_namespaced_*             (롤아웃 재시작 — Deployment/StatefulSet 만)
  log_func     : read_namespaced_pod_log        (Pod 만)
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kubernetes import client

from . import summarize


@dataclass(frozen=True)
class KindSpec:
    key: str                      # URL에 쓰는 이름 (복수형 소문자)
    kind: str                     # K8s Kind
    api_version: str
    list_func: Callable[..., Any]  # list_namespaced_* (watch 스트림에도 그대로 사용)
    status: Callable[[dict[str, Any]], dict[str, Any]]
    aliases: tuple[str, ...] = field(default_factory=tuple)
    delete_func: Callable[..., Any] | None = None
    restart_func: Callable[..., Any] | None = None
    log_func: Callable[..., Any] | None = None
    newest_first_by: str | None = None   # 목록 정렬: status의 이 필드 기준 최신순 (기본은 이름순)


def build_registry() -> dict[str, KindSpec]:
    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    batch = client.BatchV1Api()
    specs = [
        KindSpec("pods", "Pod", "v1", core.list_namespaced_pod, summarize.pod_status, ("pod", "po"),
                 delete_func=core.delete_namespaced_pod, log_func=core.read_namespaced_pod_log),
        KindSpec("deployments", "Deployment", "apps/v1", apps.list_namespaced_deployment,
                 summarize.deployment_status, ("deployment", "deploy"),
                 delete_func=apps.delete_namespaced_deployment,
                 restart_func=apps.patch_namespaced_deployment),
        KindSpec("statefulsets", "StatefulSet", "apps/v1", apps.list_namespaced_stateful_set,
                 summarize.statefulset_status, ("statefulset", "sts"),
                 delete_func=apps.delete_namespaced_stateful_set,
                 restart_func=apps.patch_namespaced_stateful_set),
        KindSpec("services", "Service", "v1", core.list_namespaced_service,
                 summarize.service_status, ("service", "svc"),
                 delete_func=core.delete_namespaced_service),
        KindSpec("jobs", "Job", "batch/v1", batch.list_namespaced_job, summarize.job_status, ("job",),
                 delete_func=batch.delete_namespaced_job),
        KindSpec("persistentvolumeclaims", "PersistentVolumeClaim", "v1",
                 core.list_namespaced_persistent_volume_claim, summarize.pvc_status, ("pvc",),
                 delete_func=core.delete_namespaced_persistent_volume_claim),
        # 조회 전용 — 컨트롤러가 무엇을 했는지(Killing → Scheduled → Started …) 보여주는 기록
        KindSpec("events", "Event", "v1", core.list_namespaced_event, summarize.event_status,
                 ("event", "ev"), newest_first_by="last_seen"),
    ]
    return {s.key: s for s in specs}
