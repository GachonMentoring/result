"""데이터셋 Job 만들기 · 상태 조회 (k8sapimd/dataset.md 5-3, 6장).

- Job 명세는 코드에 없다 → 매니페스트 폴더의 템플릿 YAML (기본 job/dataset.yaml, 원본 jobs/dataset/job.yaml)
  [생성] 때마다 파일을 다시 읽어 metadata.name → generateName "<name>-" 으로 바꾸고 create(POST) 한다
  → 이미지·리소스·TTL 등은 매니페스트 관리 화면에서 파일만 고치면 다음 실행부터 반영 (apply 불필요)
- create 로 만든 Job 은 Work 3 "선언 없음"(플랫폼이 apply 한 것만 대상)에 나타나지 않음
- 끝난 Job 은 ttlSecondsAfterFinished 로 쿠버네티스가 자동 삭제 (플랫폼 코드는 지우지 않음)
- 상태·결과는 이미 Watch 중인 jobs · pods 캐시에서 읽는다 (결과 = 컨테이너 종료 메시지)

환경변수
  DATASET_JOB_FILE   job/dataset.yaml   매니페스트 폴더(MANIFEST_DIR) 기준 템플릿 경로
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..k8s import summarize
from ..k8s.watcher import WatchManager
from ..manifests.files import FileError, ManifestStore
from .store import DatasetError

JOB_TYPE_LABEL = "platform.mentor/job-type"
JOB_TYPE = "dataset"
ACTIVE_STATES = ("Pending", "Running")
# 방금 만든 Job 이 Watch 캐시에 들어오기 전 다시 눌러도 두 번 생성되지 않게 기억하는 시간
RECENT_SECONDS = 15.0

# 템플릿에서 버리는 metadata (클러스터에서 복사해 온 Job YAML 을 붙여 넣어도 동작하도록)
_DROP_META = ("name", "generateName", "uid", "resourceVersion", "creationTimestamp", "generation",
              "managedFields", "selfLink", "ownerReferences", "deletionTimestamp", "finalizers")
# 쿠버네티스가 Job 마다 붙이는 라벨 — 남아 있으면 새 Job 의 selector 와 어긋나 생성 실패
_JOB_OWNED_LABELS = ("controller-uid", "batch.kubernetes.io/controller-uid",
                     "job-name", "batch.kubernetes.io/job-name")


@dataclass(frozen=True)
class JobSettings:
    template: str = "job/dataset.yaml"

    @classmethod
    def from_env(cls) -> "JobSettings":
        return cls(template=os.getenv("DATASET_JOB_FILE", "").strip() or cls().template)


def job_from_template(docs: list[dict[str, Any]], namespace: str, source: str,
                      job_type: str = JOB_TYPE) -> dict[str, Any]:
    """템플릿 문서 → create 할 Job 본문. 형식이 틀리면 DatasetError(400). (데이터셋·학습 Job 공통)"""
    def bad(msg: str) -> DatasetError:
        return DatasetError(400, f"Job 명세 파일({source}): {msg}")

    if len(docs) != 1:
        raise bad(f"문서가 하나만 있어야 합니다 (지금 {len(docs)}개).")
    doc = docs[0]
    if doc.get("apiVersion") != "batch/v1" or doc.get("kind") != "Job":
        raise bad(f"apiVersion: batch/v1, kind: Job 이어야 합니다 (지금 {doc.get('apiVersion')} {doc.get('kind')}).")
    ns = doc["metadata"].get("namespace")
    if ns and ns != namespace:
        raise bad(f"네임스페이스 '{ns}' 는 허용되지 않습니다 ({namespace} 만 허용).")
    spec = doc.get("spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("template"), dict):
        raise bad("spec.template 이 없습니다.")

    body = copy.deepcopy(doc)
    body.pop("status", None)
    meta = body["metadata"]
    name = meta["name"]
    for k in _DROP_META:
        meta.pop(k, None)
    meta["generateName"] = f"{name}-"
    meta["namespace"] = namespace
    meta["labels"] = {**(meta.get("labels") or {}), JOB_TYPE_LABEL: job_type}

    spec = body["spec"]
    if not spec.get("manualSelector"):
        spec.pop("selector", None)
    tmeta = spec["template"].get("metadata") or {}
    labels = {k: v for k, v in (tmeta.get("labels") or {}).items() if k not in _JOB_OWNED_LABELS}
    tmeta["labels"] = {**labels, JOB_TYPE_LABEL: job_type}
    spec["template"]["metadata"] = tmeta
    return body


def set_env(body: dict[str, Any], env: dict[str, str]) -> None:
    """첫 컨테이너의 env 값을 채운다 (같은 이름이 있으면 교체, 없으면 추가). 예: 학습 Job 의 DATASET."""
    containers = (body["spec"]["template"].get("spec") or {}).get("containers") or []
    if not containers:
        raise DatasetError(400, "Job 명세에 containers 가 없습니다.")
    items = [e for e in containers[0].get("env") or [] if e.get("name") not in env]
    containers[0]["env"] = items + [{"name": k, "value": v} for k, v in env.items()]


def _result_of(pods: list[dict[str, Any]], job_name: str) -> dict[str, Any] | None:
    """Job 파드의 컨테이너 종료 메시지 (Job 이 남긴 요약 JSON, 실패 시 로그 끝부분)."""
    for pod in pods:
        if (pod.get("metadata", {}).get("labels") or {}).get("job-name") != job_name:
            continue
        for cs in (pod.get("status") or {}).get("containerStatuses") or []:
            term = (cs.get("state") or {}).get("terminated")
            if not term:
                continue
            msg = (term.get("message") or "").strip()
            try:
                parsed = json.loads(msg)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
            return {"ok": term.get("exitCode") == 0, "error": msg or term.get("reason") or "알 수 없는 오류"}
    return None


def _pod_waiting(pods: list[dict[str, Any]], job_name: str) -> str | None:
    """시작을 못 하는 이유 (예: PVC 없음 → Pending, 이미지 없음 → ErrImagePull)."""
    for pod in pods:
        if (pod.get("metadata", {}).get("labels") or {}).get("job-name") != job_name:
            continue
        st = summarize.pod_status(pod)
        if st["display_status"] not in ("Running", "Succeeded", "Completed"):
            return st["display_status"]
    return None


class TemplateJobs:
    """매니페스트 폴더의 Job 템플릿으로 Job 을 만들고, Watch 캐시에서 상태를 읽는다 (데이터셋·학습 공통)."""

    job_type = JOB_TYPE
    what = "데이터셋 생성"   # 409 메시지용

    def __init__(self, manager: WatchManager, manifests: ManifestStore, template: str,
                 create: Callable[[str, dict[str, Any]], dict[str, Any]],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.manager = manager
        self.manifests = manifests   # Job 템플릿을 읽는 매니페스트 폴더 (Work 3 와 같은 폴더)
        self.template = template
        self._create = create        # (namespace, body) → 생성된 Job (dict)
        self._clock = clock
        self._lock = threading.Lock()
        self._recent: tuple[str, float] | None = None

    # ---------------------------------------------------------- 템플릿
    def build(self) -> dict[str, Any]:
        """템플릿 파일을 (매번 디스크에서) 읽어 create 할 Job 본문을 만든다. NFS 라 캐시하지 않음."""
        path = self.template
        try:
            docs = self.manifests.read_documents(path)
        except FileError as e:
            if e.status == 503:
                raise DatasetError(503, e.message) from e
            if e.status == 404:
                raise DatasetError(400, f"Job 명세 파일이 없습니다: 매니페스트 폴더의 {path}") from e
            raise DatasetError(400, f"Job 명세 파일({path}): {e.message}") from e
        return job_from_template(docs, self.manager.namespace, path, self.job_type)

    def template_info(self) -> dict[str, Any]:
        """화면용: 템플릿 경로 + 쓸 수 있는지 + TTL. 오류는 예외 대신 error 로."""
        try:
            ttl = self.build()["spec"].get("ttlSecondsAfterFinished")
            return {"file": self.template, "ok": True, "error": None, "ttl_seconds": ttl}
        except DatasetError as e:
            return {"file": self.template, "ok": False, "error": e.message, "ttl_seconds": None}

    # ---------------------------------------------------------- 상태
    def _jobs(self) -> list[dict[str, Any]]:
        cache = self.manager.caches.get("jobs")
        if cache is None:
            return []
        return [j for j in cache.list()
                if (j.get("metadata", {}).get("labels") or {}).get(JOB_TYPE_LABEL) == self.job_type]

    def list(self) -> list[dict[str, Any]]:
        """이 종류의 Job 최신순 (TTL 로 지워지기 전까지만 보임)."""
        pods = self.manager.caches["pods"].list() if "pods" in self.manager.caches else []
        out = []
        for j in self._jobs():
            meta = j.get("metadata") or {}
            st = summarize.job_status(j)
            item = {
                "name": meta.get("name"),
                "state": st["state"],
                "created_at": meta.get("creationTimestamp"),
                "start_time": st["start_time"],
                "completion_time": st["completion_time"],
                "result": _result_of(pods, meta.get("name")),
                "waiting": _pod_waiting(pods, meta.get("name")) if st["state"] in ACTIVE_STATES else None,
            }
            if st["state"] == "Failed" and not item["result"]:
                cond = next((c for c in (j.get("status") or {}).get("conditions") or []
                             if c.get("type") == "Failed"), {})
                item["result"] = {"ok": False, "error": cond.get("message") or cond.get("reason") or "실패"}
            out.append(item)
        out.sort(key=lambda x: x["created_at"] or "", reverse=True)
        return out

    def running(self) -> str | None:
        """실행 중인 이 종류의 Job 이름 (없으면 None)."""
        for item in self.list():
            if item["state"] in ACTIVE_STATES:
                return item["name"]
        if self._recent and self._clock() - self._recent[1] < RECENT_SECONDS:
            name = self._recent[0]
            if not any(j.get("metadata", {}).get("name") == name for j in self._jobs()):
                return name   # 아직 캐시에 안 들어옴
        return None

    def start(self, env: dict[str, str] | None = None) -> dict[str, Any]:
        with self._lock:
            busy = self.running()
            if busy:
                raise DatasetError(409, f"{self.what}이 이미 실행 중입니다: {busy}")
            body = self.build()
            if env:
                set_env(body, env)
            created = self._create(self.manager.namespace, body)
            name = (created.get("metadata") or {}).get("name")
            self._recent = (name, self._clock())
        return {"job": name, "ttl_seconds": body["spec"].get("ttlSecondsAfterFinished")}


class DatasetJobs(TemplateJobs):
    """데이터셋 재생성 Job (k8sapimd/dataset.md)."""

    def __init__(self, manager: WatchManager, manifests: ManifestStore, settings: JobSettings,
                 create: Callable[[str, dict[str, Any]], dict[str, Any]],
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(manager, manifests, settings.template, create, clock)
        self.settings = settings


def kube_creator() -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """실제 클러스터에 Job 생성 (BatchV1Api). 결과는 dict 로."""
    from kubernetes import client

    def create(namespace: str, body: dict[str, Any]) -> dict[str, Any]:
        api = client.BatchV1Api()
        job = api.create_namespaced_job(namespace, body)
        return api.api_client.sanitize_for_serialization(job)

    return create
