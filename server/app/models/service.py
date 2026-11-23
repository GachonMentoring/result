"""모델 재학습·교체 기능 묶음 (k8sapimd/train.md 8장).

환경변수
  MLFLOW_TRACKING_URI   (필수) 예: http://mlflow.mentor.svc.cluster.local:5000 — 없으면 이 기능 전체 비활성(503)
  MLFLOW_UI_URL         화면의 MLflow 링크 (없으면 링크 숨김)
  REGISTERED_MODEL      toxicity-filter
  MODEL_WORKLOAD_FILE   workloads/toxicity-filter.yaml   (매니페스트 폴더 기준)
  MODEL_DEPLOYMENT      toxicity-filter
  TRAIN_JOB_FILE        job/train.yaml
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from .deploy import DeployService, DeploySettings
from .mlflow_rest import MlflowREST, ModelError
from .train import TrainJobs

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSettings:
    tracking_uri: str
    ui_url: str | None
    deploy: DeploySettings
    experiment: str = "toxicity-filter"

    @classmethod
    def from_env(cls) -> "ModelSettings | None":
        uri = os.getenv("MLFLOW_TRACKING_URI", "").strip()
        if not uri:
            return None
        d = DeploySettings()
        return cls(
            tracking_uri=uri,
            ui_url=(os.getenv("MLFLOW_UI_URL", "").strip().rstrip("/") or None),
            deploy=DeploySettings(
                model_name=os.getenv("REGISTERED_MODEL", "").strip() or d.model_name,
                workload_file=os.getenv("MODEL_WORKLOAD_FILE", "").strip() or d.workload_file,
                deployment=os.getenv("MODEL_DEPLOYMENT", "").strip() or d.deployment,
            ),
            experiment=os.getenv("MLFLOW_EXPERIMENT_NAME", "").strip() or "toxicity-filter",
        )


class ModelService:
    def __init__(self, settings: ModelSettings, mlflow: MlflowREST, train: TrainJobs,
                 deploy: DeployService | None) -> None:
        self.settings = settings
        self.mlflow = mlflow
        self.train = train
        self.deploy = deploy          # 매니페스트 관리가 꺼져 있으면 None (교체 불가)
        self._cleaned: set[str] = set()
        self._lock = threading.Lock()

    def links(self) -> dict[str, Any]:
        ui = self.settings.ui_url
        name = self.settings.deploy.model_name
        return {"mlflow_ui": ui, "model_page": f"{ui}/#/models/{name}" if ui else None}

    def train_jobs(self) -> list[dict[str, Any]]:
        """학습 Job 목록. 강제 종료(시간 초과 등)로 결과를 못 남긴 Job 의 MLflow run 을 FAILED 로 정리 (R9)."""
        items = self.train.list()
        for it in items:
            name = it["name"]
            if it["state"] != "Failed" or name in self._cleaned:
                continue
            with self._lock:
                if name in self._cleaned:
                    continue
                self._cleaned.add(name)
            try:
                exp = self.mlflow.experiment_id(self.settings.experiment)
                for run_id in self.mlflow.running_runs(exp, name) if exp else []:
                    self.mlflow.fail_run(run_id)
                    log.info("RUNNING 으로 남은 run 정리: %s (Job %s)", run_id, name)
            except ModelError as e:   # 다음 조회 때 다시 시도
                self._cleaned.discard(name)
                log.warning("run 정리 실패 (%s): %s", name, e.message)
        return items

    def close(self) -> None:
        self.mlflow.close()
