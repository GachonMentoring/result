"""MLflow REST 호출 (k8sapimd/train.md 8-1) — API 이미지에 mlflow 패키지를 넣지 않고 httpx 로 직접.

쓰는 엔드포인트 (MLflow 3.18.0 에서 확인, train.md 2-1)
  GET  model-versions/search         등록 모델 버전·상태·태그 (※ 응답에 alias 는 비어 있음)
  GET  registered-models/alias       alias(champion) → 버전
  POST registered-models/alias       alias 지정
  GET  experiments/get-by-name       실험 id
  POST runs/search · runs/update     Job 이 강제 종료돼 RUNNING 으로 남은 run 정리 (R9)
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..metrics import MLFLOW_FAILURES


class ModelError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class MlflowREST:
    def __init__(self, base_url: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _req(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
             missing_ok: bool = False) -> dict[str, Any] | None:
        try:
            r = self._http.request(method, f"/api/2.0/mlflow/{path}", params=params, json=json)
        except httpx.HTTPError as e:
            MLFLOW_FAILURES.labels(path).inc()
            raise ModelError(503, f"MLflow 에 연결할 수 없습니다 ({self.base_url}): {type(e).__name__}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400:
            code = body.get("error_code") if isinstance(body, dict) else None
            msg = (body.get("message") if isinstance(body, dict) else None) or r.text[:200]
            # 없는 alias 는 MLflow 3.18 이 404 가 아니라 400 INVALID_PARAMETER_VALUE "… alias … not found." 로 응답
            not_found = (r.status_code == 404 or code == "RESOURCE_DOES_NOT_EXIST"
                         or (code == "INVALID_PARAMETER_VALUE" and "not found" in msg.lower()))
            if missing_ok and not_found:
                return None
            MLFLOW_FAILURES.labels(path).inc()
            raise ModelError(502, f"MLflow 오류 (HTTP {r.status_code}): {msg}")
        return body if isinstance(body, dict) else {}

    # ---------------------------------------------------------- 등록 모델
    def versions(self, name: str) -> list[dict[str, Any]]:
        """등록 모델의 모든 버전 (새 버전 먼저). 등록 모델이 없으면 빈 목록."""
        out: list[dict[str, Any]] = []
        token = None
        for _ in range(20):   # 200 × 20 = 4000 버전이면 충분
            params = {"filter": f"name='{name}'", "max_results": 200}
            if token:
                params["page_token"] = token
            body = self._req("GET", "model-versions/search", params=params) or {}
            for v in body.get("model_versions") or []:
                out.append({
                    "version": str(v.get("version")),
                    "status": v.get("status"),
                    "created_ms": v.get("creation_timestamp"),
                    "run_id": v.get("run_id"),
                    "source": v.get("source"),
                    "tags": {t["key"]: t.get("value") for t in v.get("tags") or [] if "key" in t},
                })
            token = body.get("next_page_token")
            if not token:
                break
        out.sort(key=lambda v: int(v["version"]) if v["version"].isdigit() else 0, reverse=True)
        return out

    def get_alias(self, name: str, alias: str) -> str | None:
        body = self._req("GET", "registered-models/alias", params={"name": name, "alias": alias}, missing_ok=True)
        if not body:
            return None
        v = (body.get("model_version") or {}).get("version")
        return str(v) if v is not None else None

    def set_alias(self, name: str, alias: str, version: str) -> None:
        self._req("POST", "registered-models/alias", json={"name": name, "alias": alias, "version": str(version)})

    # ---------------------------------------------------------- run 정리 (R9)
    def experiment_id(self, name: str) -> str | None:
        body = self._req("GET", "experiments/get-by-name", params={"experiment_name": name}, missing_ok=True)
        return ((body or {}).get("experiment") or {}).get("experiment_id")

    def running_runs(self, experiment_id: str, job_name: str) -> list[str]:
        job = job_name.replace("'", "")
        body = self._req("POST", "runs/search", json={
            "experiment_ids": [experiment_id],
            "filter": f"tags.k8s_job = '{job}' and attributes.status = 'RUNNING'",
            "max_results": 10,
        }) or {}
        return [r["info"]["run_id"] for r in body.get("runs") or [] if r.get("info", {}).get("run_id")]

    def fail_run(self, run_id: str) -> None:
        self._req("POST", "runs/update", json={"run_id": run_id, "status": "FAILED",
                                               "end_time": int(time.time() * 1000)})
