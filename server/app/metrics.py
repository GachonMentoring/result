"""Prometheus 메트릭 (k8sapimd/monitoring.md 2장).

두 종류로 나눈다.
  1) 이벤트 카운터 (모듈 전역 REGISTRY) — 일이 일어날 때 코드에서 증가
       분류 결과 · 모델 호출 실패 · 피드백 저장 결과 · 운영 행위 · MLflow 호출 실패
  2) 상태 Collector (앱마다) — /metrics 가 불릴 때 Watch 캐시·설정을 읽어 그 자리에서 계산
       Watch 상태 · 파드 재시작/Ready · 선언 vs 실제 replica · Job 결과 (kube-state-metrics 대체)
     → 쿠버네티스 API 를 추가로 호출하지 않는다.

라벨에 댓글 원문·행 ID 를 넣지 않는다 (cardinality · 원문 노출).
/metrics 는 Ingress 에 노출하지 않는다 — Prometheus 가 파드 IP 로 직접 수집.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from prometheus_client import CollectorRegistry, Counter
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

REGISTRY = CollectorRegistry()

CLASSIFY = Counter("mentor_platform_classify", "임계값 판정 결과 수",
                   ["model_version", "pred_label"], registry=REGISTRY)
MODEL_CALL_FAILURES = Counter("mentor_platform_model_call_failures", "모델 서버 호출 실패 수",
                              ["reason"], registry=REGISTRY)   # timeout / connect / http_4xx / http_5xx / invalid
FEEDBACK_SAVE = Counter("mentor_platform_feedback_save", "피드백 DB 저장 결과 수",
                        ["result"], registry=REGISTRY)         # ok / error / disabled
ACTIONS = Counter("mentor_platform_actions", "운영 행위 수 (result: ok / rejected=4xx / error=5xx)",
                  ["action", "result"], registry=REGISTRY)
MLFLOW_FAILURES = Counter("mentor_platform_mlflow_failures", "MLflow REST 호출 실패 수",
                          ["op"], registry=REGISTRY)

# 운영 행위 = 상태를 바꾸는 API. (메서드, 라우트 경로) → action 이름
ACTION_ROUTES: dict[tuple[str, str], str] = {
    ("DELETE", "/api/v1/resources/{kind}/{name}"): "delete",
    ("POST", "/api/v1/resources/{kind}/{name}/restart"): "restart",
    ("PUT", "/api/v1/manifests/file"): "manifest_save",
    ("POST", "/api/v1/manifests/file"): "manifest_create",
    ("DELETE", "/api/v1/manifests/file"): "manifest_delete",
    ("POST", "/api/v1/manifests/apply"): "apply",
    ("DELETE", "/api/v1/manifests/orphans"): "orphan_delete",
    ("POST", "/api/v1/datasets"): "dataset_create",
    ("POST", "/api/v1/models/train"): "train_start",
    ("POST", "/api/v1/models/deploy"): "model_deploy",
    ("POST", "/api/v1/models/deploy/rollback"): "model_rollback",
    ("POST", "/api/v1/feedback/{item_id}/label"): "feedback_label",
}

JOB_TYPE_LABEL = "platform.mentor/job-type"
KNOWN_JOB_TYPES = ("dataset", "train")


def action_result(status_code: int) -> str:
    if status_code < 400:
        return "ok"
    return "rejected" if status_code < 500 else "error"


def record_action(method: str, route_path: str | None, status_code: int) -> None:
    action = ACTION_ROUTES.get((method, route_path or ""))
    if action:
        ACTIONS.labels(action, action_result(status_code)).inc()


# ---------------------------------------------------------------------- 상태 Collector
def _ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _name(obj: dict[str, Any]) -> str:
    return (obj.get("metadata") or {}).get("name", "")


def job_state(job: dict[str, Any]) -> str:
    status = job.get("status") or {}
    for cond in status.get("conditions") or []:
        if cond.get("status") == "True" and cond.get("type") in ("Complete", "SuccessCriteriaMet"):
            return "succeeded"
        if cond.get("status") == "True" and cond.get("type") in ("Failed", "FailureTarget"):
            return "failed"
    if status.get("succeeded"):
        return "succeeded"
    return "active"


class StateCollector(Collector):
    """/metrics 가 불릴 때 앱 상태(app.state)를 읽어 게이지를 만든다."""

    def __init__(self, state_getter: Callable[[], Any]) -> None:
        self._state = state_getter

    def collect(self):
        state = self._state()
        yield from self._service(state)
        manager = getattr(state, "manager", None)
        if manager is not None:
            yield from self._watch(manager)
            yield from self._k8s(manager)

    # 서비스 설정 -------------------------------------------------------------
    @staticmethod
    def _service(state: Any):
        comments = getattr(state, "comments", None)
        if comments is not None:
            g = GaugeMetricFamily("mentor_platform_threshold", "현재 유해 판정 임계값 (THRESHOLD)")
            g.add_metric([], comments.settings.threshold)
            yield g
        g = GaugeMetricFamily("mentor_platform_feedback_db_configured", "피드백 DB 설정 여부 (0이면 저장이 꺼져 있음)")
        g.add_metric([], 0 if getattr(state, "feedback", None) is None else 1)
        yield g

    # Watch 상태 --------------------------------------------------------------
    @staticmethod
    def _watch(manager: Any):
        synced = GaugeMetricFamily("mentor_platform_watch_synced", "리소스 종류별 첫 LIST 완료 (/readyz 근거)", labels=["kind"])
        last = GaugeMetricFamily("mentor_platform_watch_last_event_timestamp_seconds",
                                 "마지막 Watch 이벤트(없으면 마지막 LIST) 시각", labels=["kind"])
        relists = CounterMetricFamily("mentor_platform_watch_relists", "410 Gone → RE-LIST 횟수", labels=["kind"])
        errors = CounterMetricFamily("mentor_platform_watch_errors", "Watch 오류·재연결 횟수", labels=["kind"])
        for key, w in manager.watchers.items():
            synced.add_metric([key], 1 if w.synced.is_set() else 0)
            t = _ts(w.last_event_at) or _ts(w.last_sync_at)
            if t is not None:
                last.add_metric([key], t)
            relists.add_metric([key], w.relist_count)
            errors.add_metric([key], getattr(w, "error_count", 0))
        yield from (synced, last, relists, errors)

    # Watch 캐시 → k8s 상태 (kube-state-metrics 대체) ---------------------------
    @staticmethod
    def _k8s(manager: Any):
        caches = manager.caches

        restarts = GaugeMetricFamily("mentor_platform_pod_restarts", "컨테이너 재시작 횟수", labels=["pod", "container"])
        ready = GaugeMetricFamily("mentor_platform_pod_ready", "파드 Ready 여부", labels=["pod"])
        for pod in caches["pods"].list() if "pods" in caches else []:
            name = _name(pod)
            status = pod.get("status") or {}
            for c in status.get("containerStatuses") or []:
                restarts.add_metric([name, c.get("name", "")], c.get("restartCount", 0))
            is_ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                           for c in status.get("conditions") or [])
            ready.add_metric([name], 1 if is_ready else 0)
        yield from (restarts, ready)

        replicas = GaugeMetricFamily("mentor_platform_workload_replicas", "선언(desired) vs 실제(ready·updated) replica",
                                     labels=["kind", "name", "state"])
        for key, kind in (("deployments", "Deployment"), ("statefulsets", "StatefulSet")):
            for obj in caches[key].list() if key in caches else []:
                spec, status = obj.get("spec") or {}, obj.get("status") or {}
                name = _name(obj)
                replicas.add_metric([kind, name, "desired"], spec.get("replicas", 1))
                replicas.add_metric([kind, name, "ready"], status.get("readyReplicas", 0))
                replicas.add_metric([kind, name, "updated"], status.get("updatedReplicas", 0))
        yield replicas

        counts: dict[tuple[str, str], int] = {(t, s): 0 for t in KNOWN_JOB_TYPES
                                              for s in ("active", "succeeded", "failed")}
        last_done: dict[str, tuple[float, float]] = {}   # job_type → (완료 시각, 소요 초)
        for job in caches["jobs"].list() if "jobs" in caches else []:
            jtype = ((job.get("metadata") or {}).get("labels") or {}).get(JOB_TYPE_LABEL, "other")
            st = job_state(job)
            for s in ("active", "succeeded", "failed"):
                counts.setdefault((jtype, s), 0)
            counts[(jtype, st)] += 1
            status = job.get("status") or {}
            start, end = _ts(status.get("startTime")), _ts(status.get("completionTime"))
            if st == "succeeded" and start is not None and end is not None:
                if jtype not in last_done or end > last_done[jtype][0]:
                    last_done[jtype] = (end, end - start)

        jobs = GaugeMetricFamily("mentor_platform_jobs", "플랫폼이 만든 Job 개수 (Watch 캐시에 남아 있는 것, TTL 전까지)",
                                 labels=["job_type", "status"])
        for (jtype, st), n in sorted(counts.items()):
            jobs.add_metric([jtype, st], n)
        yield jobs
        dur = GaugeMetricFamily("mentor_platform_job_last_duration_seconds", "가장 최근에 성공한 Job 의 소요 시간",
                                labels=["job_type"])
        for jtype, (_, seconds) in sorted(last_done.items()):
            dur.add_metric([jtype], seconds)
        yield dur
