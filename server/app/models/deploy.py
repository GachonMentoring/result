"""모델 교체 (k8sapimd/train.md 7장) — "교체 = 선언 변경".

- 진실은 NFS 워크로드 YAML(workloads/toxicity-filter.yaml) 의 MODEL_URI 한 줄
  (`value: "models:/toxicity-filter/3"   # platform.mentor/model-uri`)
- 교체: 그 줄의 버전만 텍스트로 바꿔 저장(주석·순서 유지) → Work 3 apply(SSA) → 롤링 업데이트
  apply 가 실패하면 파일을 원래 내용으로 되돌린다 (선언 ≠ 실제 로 남지 않게)
- 롤아웃 완료 판단·alias(champion) 이동은 상태를 조회할 때 계산 (결정 7, 백그라운드 작업 없음)
- 되돌리기 = champion(마지막으로 롤아웃이 끝난 버전)으로 다시 교체
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..k8s import summarize
from ..k8s.watcher import WatchManager
from ..manifests.cluster import ClusterError, ManifestService
from ..manifests.files import FileError
from .mlflow_rest import MlflowREST, ModelError

MARKER = "platform.mentor/model-uri"
MARKER_RE = re.compile(r"#\s*" + re.escape(MARKER) + r"\s*$")
URI_RE = re.compile(r"(?P<pre>value:\s*)(?P<q>[\"']?)(?P<uri>models:/(?P<name>[A-Za-z0-9._-]+)/(?P<ver>\d+))(?P=q)")
INIT_CONTAINER = "fetch-model"
CHAMPION = "champion"
# 새 파드가 이 상태면 교체 실패로 본다 (옛 파드는 maxUnavailable 0 이라 계속 서빙)
BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
               "CreateContainerConfigError", "CreateContainerError", "RunContainerError"}


@dataclass(frozen=True)
class DeploySettings:
    model_name: str = "toxicity-filter"
    workload_file: str = "workloads/toxicity-filter.yaml"
    deployment: str = "toxicity-filter"


# ---------------------------------------------------------------------- YAML 한 줄
def find_uri(content: str) -> tuple[int, re.Match[str]]:
    """표식이 붙은 줄 → (줄 번호, 매치). 표식이 0개·2개 이상이거나 형식이 틀리면 ModelError(409)."""
    lines = content.splitlines(keepends=True)
    # 줄 끝의 주석 표식만 인정 (주석만 있는 줄 — 설명문 — 은 제외)
    hits = [i for i, line in enumerate(lines)
            if not line.lstrip().startswith("#") and MARKER_RE.search(line)]
    if not hits:
        raise ModelError(409, f"워크로드 파일에 '# {MARKER}' 표식이 없습니다 — 서빙이 아직 MLflow 방식이 아닙니다 (train.md 7-1).")
    if len(hits) > 1:
        raise ModelError(409, f"워크로드 파일에 '# {MARKER}' 표식이 {len(hits)}개 있습니다 (1개여야 함).")
    m = URI_RE.search(lines[hits[0]])
    if not m:
        raise ModelError(409, f"표식 줄의 형식이 올바르지 않습니다 ({hits[0] + 1}번째 줄): "
                              f'value: "models:/<이름>/<버전>"   # {MARKER}')
    return hits[0], m


def replace_uri(content: str, version: str) -> str:
    i, m = find_uri(content)
    lines = content.splitlines(keepends=True)
    q = m.group("q") or '"'
    new_uri = f"models:/{m.group('name')}/{version}"
    lines[i] = lines[i][:m.start()] + f"{m.group('pre')}{q}{new_uri}{q}" + lines[i][m.end():]
    return "".join(lines)


def _init_uri(deploy: dict[str, Any]) -> str | None:
    spec = ((deploy.get("spec") or {}).get("template") or {}).get("spec") or {}
    for c in spec.get("initContainers") or []:
        if c.get("name") == INIT_CONTAINER:
            for e in c.get("env") or []:
                if e.get("name") == "MODEL_URI":
                    return e.get("value")
    return None


def _ms_to_iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pod_problem(pod: dict[str, Any]) -> str | None:
    """initContainer·컨테이너가 시작을 못 하는 이유 (Init:Error, Init:CrashLoopBackOff, CrashLoopBackOff …)."""
    status = pod.get("status") or {}
    for cs in status.get("initContainerStatuses") or []:
        waiting = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
        term = (cs.get("state") or {}).get("terminated") or {}
        last = ((cs.get("lastState") or {}).get("terminated") or {})
        if waiting in BAD_WAITING or (waiting and last.get("exitCode")):
            return f"Init:{waiting}"
        if term and term.get("exitCode"):
            return "Init:Error"
    for cs in status.get("containerStatuses") or []:
        waiting = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
        if waiting in BAD_WAITING:
            return waiting
    return None


# ---------------------------------------------------------------------- 서비스
class DeployService:
    def __init__(self, manifests: ManifestService, mlflow: MlflowREST, manager: WatchManager,
                 settings: DeploySettings = DeploySettings()) -> None:
        self.manifests = manifests
        self.mlflow = mlflow
        self.manager = manager
        self.settings = settings

    # ---------------------------------------------------------- 선언 (NFS 파일)
    def declared(self) -> dict[str, Any]:
        out: dict[str, Any] = {"file": self.settings.workload_file, "uri": None, "version": None, "error": None}
        try:
            content = self.manifests.store.read(self.settings.workload_file)["content"]
            _, m = find_uri(content)
            out["uri"], out["version"] = m.group("uri"), m.group("ver")
            if m.group("name") != self.settings.model_name:
                out["error"] = f"표식 줄의 모델 이름이 {m.group('name')} 입니다 ({self.settings.model_name} 이어야 함)."
        except FileError as e:
            out["error"] = f"워크로드 파일을 읽을 수 없습니다: {e.message}"
        except ModelError as e:
            out["error"] = e.message
        return out

    # ---------------------------------------------------------- 실제 (Watch 캐시)
    def rollout(self, declared_uri: str | None) -> dict[str, Any]:
        """complete / progressing / failed / unknown + 이유. 캐시만 읽음 (쿠버네티스 호출 없음)."""
        deps = self.manager.caches.get("deployments")
        dep = deps.get(self.settings.deployment) if deps is not None else None
        if dep is None:
            return {"state": "unknown", "reason": f"Deployment {self.settings.deployment} 를 찾을 수 없습니다.",
                    "live_uri": None}
        live_uri = _init_uri(dep)
        st = summarize.deployment_status(dep)
        status = dep.get("status") or {}
        meta = dep.get("metadata") or {}
        out = {"live_uri": live_uri, "replicas": st["replicas"], "ready": st["ready_replicas"],
               "updated": st["updated_replicas"], "total": status.get("replicas", 0), "reason": None}

        pods = self.manager.caches["pods"].list() if "pods" in self.manager.caches else []
        mine = [p for p in pods if ((p.get("metadata") or {}).get("labels") or {}).get("app") == self.settings.deployment
                and not (p.get("metadata") or {}).get("deletionTimestamp")]
        new = [p for p in mine if _init_uri({"spec": {"template": {"spec": p.get("spec") or {}}}}) == live_uri]
        problems = [x for x in (pod_problem(p) for p in new) if x]

        done = (live_uri == declared_uri
                and (meta.get("generation") or 0) <= (status.get("observedGeneration") or 0)
                and st["updated_replicas"] == st["replicas"] and status.get("availableReplicas", 0) == st["replicas"]
                and status.get("replicas", 0) == st["replicas"])
        if done:
            out["state"] = "complete"
        elif problems:
            out["state"], out["reason"] = "failed", problems[0]
        else:
            out["state"] = "progressing"
            if live_uri != declared_uri:
                out["reason"] = "선언이 아직 적용되지 않음 (파일 저장 후 [적용] 전이거나 적용 중)"
        return out

    # ---------------------------------------------------------- 조회 (+ champion 이동)
    def state(self, serving: dict[str, Any] | None) -> dict[str, Any]:
        decl = self.declared()
        ro = self.rollout(decl["uri"])
        champion, alias_error = None, None
        try:
            champion = self.mlflow.get_alias(self.settings.model_name, CHAMPION)
            # 결정 7: 롤아웃이 끝났다고 판단되는 조회 시점에 champion 이동 (표시용)
            if decl["version"] and not decl["error"] and ro["state"] == "complete" and champion != decl["version"]:
                self.mlflow.set_alias(self.settings.model_name, CHAMPION, decl["version"])
                champion = decl["version"]
        except ModelError as e:
            alias_error = e.message
        info = (serving or {}).get("info") or {}
        return {
            "model": self.settings.model_name,
            "declared": decl,
            "rollout": ro,
            "serving": {"connected": bool((serving or {}).get("connected")), "version": info.get("version"),
                        "build": info.get("build"), "sklearn": info.get("runtime_sklearn_version"),
                        "error": (serving or {}).get("error")},
            "champion": champion,
            "mlflow_error": alias_error,
        }

    def versions(self, serving: dict[str, Any] | None) -> dict[str, Any]:
        st = self.state(serving)
        runtime = st["serving"]["sklearn"]
        items = []
        champ_tags: dict[str, Any] = {}
        raw = self.mlflow.versions(self.settings.model_name)
        for v in raw:
            if v["version"] == st["champion"]:
                champ_tags = v["tags"]
        for v in raw:
            t = v["tags"]
            sk = t.get("sklearn_version")
            compatible = None if not (runtime and sk) else sk == runtime
            items.append({
                "version": v["version"], "status": v["status"], "created_at": _ms_to_iso(v["created_ms"]),
                "run_id": v["run_id"], "dataset": t.get("dataset"), "eval_f1": _float(t.get("eval_f1")),
                "n_train": _float(t.get("n_train")), "rows_feedback": _float(t.get("rows_feedback")),
                "sklearn_version": sk, "build": t.get("build"), "k8s_job": t.get("k8s_job"),
                "same_eval": (None if not champ_tags.get("eval_sha256") or not t.get("eval_sha256")
                              else champ_tags["eval_sha256"] == t["eval_sha256"]),
                "declared": v["version"] == st["declared"]["version"],
                "champion": v["version"] == st["champion"],
                "compatible": compatible,
            })
        return {**st, "count": len(items), "items": items}

    # ---------------------------------------------------------- 교체 · 되돌리기
    def deploy(self, version: str, serving: dict[str, Any] | None, *, rollback: bool = False) -> dict[str, Any]:
        version = str(version).strip()
        if not version.isdigit():
            raise ModelError(400, f"버전은 숫자여야 합니다: {version!r}")
        rel = self.settings.workload_file
        try:
            current = self.manifests.store.read(rel)
        except FileError as e:
            raise ModelError(e.status if e.status in (404, 503) else 400, f"워크로드 파일: {e.message}") from e
        _, m = find_uri(current["content"])
        if m.group("name") != self.settings.model_name:
            raise ModelError(409, f"표식 줄의 모델 이름이 {m.group('name')} 입니다 ({self.settings.model_name} 이어야 함).")
        before = m.group("ver")
        if before == version:
            raise ModelError(409, f"이미 버전 {version} 이 선언되어 있습니다.")
        if not rollback:
            ro = self.rollout(m.group("uri"))
            if ro["state"] == "progressing":
                raise ModelError(409, "이전 교체가 아직 진행 중입니다. 끝난 뒤 다시 시도하거나 [되돌리기] 를 쓰세요.")

        target = next((v for v in self.mlflow.versions(self.settings.model_name) if v["version"] == version), None)
        if target is None:
            raise ModelError(404, f"등록 모델 {self.settings.model_name} 에 버전 {version} 이 없습니다.")
        if target["status"] != "READY":
            raise ModelError(409, f"버전 {version} 이 아직 준비되지 않았습니다 (상태 {target['status']}).")
        runtime = (((serving or {}).get("info")) or {}).get("runtime_sklearn_version")
        trained = target["tags"].get("sklearn_version")
        if runtime and trained and runtime != trained:
            raise ModelError(409, f"scikit-learn 버전이 다릅니다 (학습 {trained} / 서빙 {runtime}) — "
                                  "같은 세대의 서빙 이미지로 바꾼 뒤 교체하세요 (가이드 08장 시나리오 3).")

        new_content = replace_uri(current["content"], version)
        saved = self.manifests.store.save(rel, new_content, current["etag"])
        try:
            applied = self.manifests.apply_file(rel, force=False)
        except (ClusterError, FileError) as e:
            # 선언만 바뀌고 실제는 그대로인 상태로 남기지 않는다
            try:
                self.manifests.store.save(rel, current["content"], saved["etag"])
            except FileError:
                pass
            raise ModelError(e.status if e.status in (400, 403, 409) else 502,
                             f"적용 실패 — 파일을 원래대로 되돌렸습니다: {e.message}") from e
        return {"from": before, "to": version, "uri": f"models:/{self.settings.model_name}/{version}",
                "file": rel, "rollback": rollback, "apply": applied}

    def rollback(self, serving: dict[str, Any] | None) -> dict[str, Any]:
        champion = self.mlflow.get_alias(self.settings.model_name, CHAMPION)
        if not champion:
            raise ModelError(409, "되돌릴 기준(champion)이 없습니다 — 아직 교체가 끝까지 완료된 적이 없습니다.")
        decl = self.declared()
        if decl["version"] == champion:
            raise ModelError(409, f"이미 champion 버전 {champion} 이 선언되어 있습니다.")
        return self.deploy(champion, serving, rollback=True)
