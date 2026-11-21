"""매니페스트 ↔ 클러스터: 판정(선언과 실제의 일치 여부) / apply / 삭제 / 선언 없음.

apply 는 Server-Side Apply (kubectl apply --server-side 와 동일), 필드 관리자 이름은 FIELD_MANAGER.

판정 원리 (kubectl diff 와 같음)
  파일 내용을 dry-run 으로 apply 해 본 결과(= 파일대로라면 이렇게 됨)와 현재 클러스터 상태를 비교.
  쿠버네티스가 채우는 기본값은 양쪽에 똑같이 들어가므로 불일치로 잡히지 않는다.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterable
from typing import Any, Protocol

from .files import FileError, ManifestStore, parse_documents

log = logging.getLogger(__name__)

FIELD_MANAGER = "mentor-platform"
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
MAX_DIFFS = 100

# "선언 없음"을 찾을 때 기본으로 훑어볼 kind (파일에 등장한 kind 는 자동 추가)
ORPHAN_SCAN_KINDS: tuple[tuple[str, str], ...] = (
    ("v1", "ConfigMap"), ("v1", "Secret"), ("v1", "Service"), ("v1", "PersistentVolumeClaim"),
    ("v1", "ServiceAccount"), ("apps/v1", "Deployment"), ("apps/v1", "StatefulSet"),
    ("batch/v1", "Job"), ("batch/v1", "CronJob"), ("networking.k8s.io/v1", "Ingress"),
)

# 판정 상태
SYNCED, DRIFT, MISSING, ERROR, EMPTY = "synced", "drift", "missing", "error", "empty"


class ClusterError(Exception):
    def __init__(self, status: int, message: str, conflicts: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.conflicts = conflicts or []


class ApplyConflict(ClusterError):
    """다른 필드 관리자(kubectl 등)가 가진 필드를 다른 값으로 바꾸려 함."""

    def __init__(self, conflicts: list[dict[str, str]]) -> None:
        super().__init__(409, "다른 관리자가 소유한 필드와 충돌합니다.", conflicts)


class ClusterAPI(Protocol):
    """쿠버네티스 호출 창구. 실제 구현은 DynamicClusterAPI, 테스트는 가짜로 대체."""

    def namespaced(self, api_version: str, kind: str) -> bool: ...          # 모르는 kind 면 ClusterError(400)
    def get(self, api_version: str, kind: str, name: str, namespace: str) -> dict[str, Any] | None: ...
    def apply(self, body: dict[str, Any], namespace: str, *, dry_run: bool, force: bool) -> dict[str, Any]: ...
    def delete(self, api_version: str, kind: str, name: str, namespace: str) -> bool: ...
    def list(self, api_version: str, kind: str, namespace: str) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------- 비교
def normalize(obj: dict[str, Any]) -> dict[str, Any]:
    """비교에서 뺄 것: 상태(status), 서버가 관리하는 메타데이터, 재시작 시각 어노테이션."""
    o = copy.deepcopy(obj)
    o.pop("status", None)
    meta = o.get("metadata") or {}
    for k in ("managedFields", "resourceVersion", "generation", "uid", "creationTimestamp", "selfLink"):
        meta.pop(k, None)
    for holder in (meta, ((o.get("spec") or {}).get("template") or {}).get("metadata") or {}):
        ann = holder.get("annotations")
        if isinstance(ann, dict):
            ann.pop(RESTART_ANNOTATION, None)
            if not ann:
                holder.pop("annotations")
    return o


def diff(desired: Any, live: Any, path: str = "") -> list[dict[str, Any]]:
    """두 값의 다른 지점 목록. path 예: spec.template.spec.containers[0].image"""
    out: list[dict[str, Any]] = []

    def walk(a: Any, b: Any, p: str) -> None:
        if len(out) >= MAX_DIFFS:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                walk(a.get(k, _ABSENT), b.get(k, _ABSENT), f"{p}.{k}" if p else k)
        elif isinstance(a, list) and isinstance(b, list):
            for i in range(max(len(a), len(b))):
                walk(a[i] if i < len(a) else _ABSENT, b[i] if i < len(b) else _ABSENT, f"{p}[{i}]")
        elif a != b:
            out.append({"path": p, "file": _show(a), "cluster": _show(b)})

    walk(desired, live, path)
    return out


class _Absent:
    def __repr__(self) -> str:
        return "(없음)"


_ABSENT = _Absent()


def _show(v: Any) -> Any:
    return None if v is _ABSENT else v


def _group(api_version: str) -> str:
    return api_version.split("/")[0] if "/" in api_version else ""


def _key(doc: dict[str, Any]) -> tuple[str, str, str]:
    return (_group(doc["apiVersion"]), doc["kind"], doc["metadata"]["name"])


# ---------------------------------------------------------------------- 서비스
class ManifestService:
    def __init__(self, store: ManifestStore, api: ClusterAPI, namespace: str) -> None:
        self.store = store
        self.api = api
        self.namespace = namespace

    # ---------------------------------------------------------- 범위 검사
    def _prepare(self, doc: dict[str, Any]) -> dict[str, Any]:
        """허용 범위(mentor 네임스페이스, 네임스페이스 리소스) 검사 후 apply 할 본문."""
        api_version, kind, name = doc["apiVersion"], doc["kind"], doc["metadata"]["name"]
        if not self.api.namespaced(api_version, kind):
            raise ClusterError(400, f"{kind}/{name}: 클러스터 범위 리소스는 다룰 수 없습니다. ({self.namespace} 네임스페이스 리소스만 허용)")
        ns = doc["metadata"].get("namespace")
        if ns and ns != self.namespace:
            raise ClusterError(400, f"{kind}/{name}: 네임스페이스 '{ns}' 는 허용되지 않습니다. ({self.namespace} 만 허용)")
        body = copy.deepcopy(doc)
        body["metadata"]["namespace"] = self.namespace
        return body

    def _ident(self, doc: dict[str, Any]) -> dict[str, str]:
        return {"apiVersion": doc.get("apiVersion"), "kind": doc.get("kind"),
                "name": (doc.get("metadata") or {}).get("name"), "namespace": self.namespace}

    # ---------------------------------------------------------- 판정
    def judge_doc(self, doc: dict[str, Any]) -> dict[str, Any]:
        r: dict[str, Any] = {**self._ident(doc), "state": ERROR, "diff": [], "conflicts": [], "error": None}
        try:
            body = self._prepare(doc)
            live = self.api.get(body["apiVersion"], body["kind"], body["metadata"]["name"], self.namespace)
            if live is None:
                self.api.apply(body, self.namespace, dry_run=True, force=True)  # 만들 수 있는지 검증만
                r["state"] = MISSING
                return r
            dry = self.api.apply(body, self.namespace, dry_run=True, force=True)
            r["diff"] = diff(normalize(dry), normalize(live))
            r["state"] = DRIFT if r["diff"] else SYNCED
            if r["diff"]:
                # 값이 다를 때만 소유권 충돌이 생길 수 있음 → 강제 없이 한 번 더 dry-run
                try:
                    self.api.apply(body, self.namespace, dry_run=True, force=False)
                except ApplyConflict as c:
                    r["conflicts"] = c.conflicts
        except ClusterError as e:
            r["error"] = e.message
        return r

    def judge_file(self, rel: str) -> dict[str, Any]:
        out: dict[str, Any] = {"path": rel, "state": ERROR, "resources": [], "error": None, "error_line": None}
        try:
            docs = self.store.read_documents(rel)
        except FileError as e:
            if e.status == 404:
                raise
            out["error"], out["error_line"] = e.message, e.line
            return out
        out["resources"] = [self.judge_doc(d) for d in docs]
        out["state"] = summarize_state(r["state"] for r in out["resources"])
        return out

    def judge_all(self) -> dict[str, Any]:
        files = {f.path: self.judge_file(f.path) for f in self.store.list()}
        return {
            "files": {p: {"state": j["state"], "error": j["error"]} for p, j in files.items()},
            "orphans": self._orphans(),
        }

    # ---------------------------------------------------------- apply
    def apply_file(self, rel: str, force: bool) -> dict[str, Any]:
        """디스크에 저장된 파일을 apply. 전부 검증(dry-run) 통과해야 실제 적용."""
        docs = self.store.read_documents(rel)
        if not docs:
            raise ClusterError(400, "적용할 리소스가 없는 빈 파일입니다.")
        bodies = [self._prepare(d) for d in docs]

        # 1) 사전 판정 (생성/변경/변경 없음) + 전체 dry-run
        plan: list[dict[str, Any]] = []
        conflicts: list[dict[str, str]] = []
        for b in bodies:
            live = self.api.get(b["apiVersion"], b["kind"], b["metadata"]["name"], self.namespace)
            try:
                dry = self.api.apply(b, self.namespace, dry_run=True, force=force)
            except ApplyConflict as c:
                conflicts += [{**cf, "resource": f"{b['kind']}/{b['metadata']['name']}"} for cf in c.conflicts]
                continue
            action = "created" if live is None else ("updated" if diff(normalize(dry), normalize(live)) else "unchanged")
            plan.append({**self._ident(b), "action": action})
        if conflicts:
            raise ClusterError(409, "다른 관리자가 소유한 필드와 충돌합니다. 확인 후 강제 적용(force)하세요.", conflicts)

        # 2) 실제 적용 — 중간 실패 시 이미 적용된 것은 결과에 표시 (되돌릴 수 없음)
        results = []
        for b, p in zip(bodies, plan):
            try:
                self.api.apply(b, self.namespace, dry_run=False, force=force)
                results.append({**p, "ok": True, "error": None})
            except ClusterError as e:
                results.append({**p, "ok": False, "error": e.message})
                done = [f"{x['kind']}/{x['name']}" for x in results if x["ok"]]
                raise ClusterError(502, f"{b['kind']}/{b['metadata']['name']} 적용 실패: {e.message}"
                                        f" (이미 적용됨: {', '.join(done) or '없음'})") from e
        return {"path": rel, "resources": results}

    # ---------------------------------------------------------- 삭제
    def delete_file(self, rel: str, with_resources: bool) -> dict[str, Any]:
        deleted: list[dict[str, Any]] = []
        if with_resources:
            try:
                docs = self.store.read_documents(rel)
            except FileError as e:
                if e.status == 404:
                    raise
                raise ClusterError(400, f"파일을 해석할 수 없어 클러스터 리소스를 찾을 수 없습니다: {e.message} "
                                        "(파일만 삭제하려면 '클러스터 리소스도 삭제'를 끄세요)") from e
            bodies = [self._prepare(d) for d in docs]  # 범위 밖 리소스가 있으면 아무것도 지우지 않음
            for b in bodies:
                existed = self.api.delete(b["apiVersion"], b["kind"], b["metadata"]["name"], self.namespace)
                deleted.append({**self._ident(b), "deleted": existed})
        self.store.delete(rel)
        return {"path": rel, "resources": deleted}

    def delete_orphan(self, api_version: str, kind: str, name: str) -> bool:
        probe = {"apiVersion": api_version, "kind": kind, "metadata": {"name": name}}
        self._prepare(probe)
        declared = {_key(d) for docs in self._all_docs().values() for d in docs}
        if _key(probe) in declared:
            raise ClusterError(409, f"{kind}/{name} 는 매니페스트 파일에 선언되어 있습니다. 파일에서 삭제하세요.")
        return self.api.delete(api_version, kind, name, self.namespace)

    # ---------------------------------------------------------- 선언 없음
    def _all_docs(self) -> dict[str, list[dict[str, Any]]]:
        out = {}
        for f in self.store.list():
            try:
                out[f.path] = self.store.read_documents(f.path)
            except FileError:
                out[f.path] = []
        return out

    def _orphans(self) -> list[dict[str, str]]:
        """이 플랫폼이 apply 했던(필드 관리자 기록이 있는) 리소스 중 어떤 파일에도 선언되지 않은 것."""
        docs = self._all_docs()
        declared = {_key(d) for ds in docs.values() for d in ds}
        kinds = set(ORPHAN_SCAN_KINDS) | {(d["apiVersion"], d["kind"]) for ds in docs.values() for d in ds}
        out = []
        for api_version, kind in sorted(kinds):
            try:
                if not self.api.namespaced(api_version, kind):
                    continue
                items = self.api.list(api_version, kind, self.namespace)
            except ClusterError:
                continue  # 클러스터에 없는 kind (예: CRD 미설치)
            for obj in items:
                meta = obj.get("metadata") or {}
                managed = any(m.get("manager") == FIELD_MANAGER and m.get("operation") == "Apply"
                              for m in meta.get("managedFields") or [])
                key = (_group(api_version), kind, meta.get("name"))
                if managed and key not in declared and not meta.get("deletionTimestamp"):
                    out.append({"apiVersion": api_version, "kind": kind, "name": meta.get("name")})
        return out


def summarize_state(states: Iterable[str]) -> str:
    s = list(states)
    if not s:
        return EMPTY
    for st in (ERROR, DRIFT, MISSING):
        if st in s:
            return st
    return SYNCED


# ---------------------------------------------------------------------- 실제 클러스터
class DynamicClusterAPI:
    """kubernetes.dynamic 클라이언트 기반 (어떤 kind 든 처리). 첫 사용 시 discovery."""

    def __init__(self) -> None:
        self._dyn = None

    @property
    def dyn(self):
        if self._dyn is None:
            from kubernetes import client
            from kubernetes.dynamic import DynamicClient
            self._dyn = DynamicClient(client.ApiClient())
        return self._dyn

    def _res(self, api_version: str, kind: str):
        from kubernetes.dynamic.exceptions import ResourceNotFoundError
        try:
            return self.dyn.resources.get(api_version=api_version, kind=kind)
        except ResourceNotFoundError as e:
            raise ClusterError(400, f"클러스터가 모르는 리소스입니다: {api_version} {kind}") from e

    @staticmethod
    def _error(e: Exception) -> ClusterError:
        from kubernetes.dynamic.exceptions import DynamicApiError
        if isinstance(e, DynamicApiError):
            try:
                body = json.loads(e.body)
            except (TypeError, ValueError):
                body = {}
            causes = (body.get("details") or {}).get("causes") or []
            if e.status == 409 and any(c.get("reason") == "FieldManagerConflict" for c in causes):
                return ApplyConflict([{"field": c.get("field"), "message": c.get("message")}
                                      for c in causes if c.get("reason") == "FieldManagerConflict"])
            message = body.get("message") or getattr(e, "reason", "") or str(e)
            status = {403: 403, 404: 404, 409: 409, 422: 400}.get(e.status, 502)
            return ClusterError(status, message)
        return ClusterError(502, f"Kubernetes API 오류: {type(e).__name__}: {e}")

    def namespaced(self, api_version: str, kind: str) -> bool:
        return bool(self._res(api_version, kind).namespaced)

    def get(self, api_version, kind, name, namespace):
        from kubernetes.dynamic.exceptions import NotFoundError
        try:
            return self.dyn.get(self._res(api_version, kind), name=name, namespace=namespace).to_dict()
        except NotFoundError:
            return None
        except ClusterError:
            raise
        except Exception as e:
            raise self._error(e) from e

    def apply(self, body, namespace, *, dry_run, force):
        try:
            res = self.dyn.server_side_apply(
                self._res(body["apiVersion"], body["kind"]), body=body, namespace=namespace,
                field_manager=FIELD_MANAGER, force_conflicts=force, dry_run="All" if dry_run else None,
            )
            return res.to_dict()
        except ClusterError:
            raise
        except Exception as e:
            raise self._error(e) from e

    def delete(self, api_version, kind, name, namespace):
        from kubernetes.dynamic.exceptions import NotFoundError
        try:
            self.dyn.delete(self._res(api_version, kind), name=name, namespace=namespace,
                            body={"propagationPolicy": "Background"})
            return True
        except NotFoundError:
            return False
        except ClusterError:
            raise
        except Exception as e:
            raise self._error(e) from e

    def list(self, api_version, kind, namespace):
        try:
            return self.dyn.get(self._res(api_version, kind), namespace=namespace).to_dict().get("items") or []
        except ClusterError:
            raise
        except Exception as e:
            raise self._error(e) from e


__all__ = ["ManifestService", "DynamicClusterAPI", "ClusterError", "ApplyConflict", "FIELD_MANAGER",
           "normalize", "diff", "summarize_state", "parse_documents"]
