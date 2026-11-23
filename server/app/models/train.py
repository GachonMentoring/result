"""모델 재학습 Job (k8sapimd/train.md 5-2) — 데이터셋 Job 과 같은 템플릿 방식.

- Job 명세: 매니페스트 폴더의 템플릿 (기본 job/train.yaml, 원본 model/job/train.yaml)
- API 가 바꾸는 값은 첫 컨테이너의 env DATASET 하나 (화면에서 고른 데이터셋 이름)
- 결과: Job 이 남긴 종료 메시지 {"ok", "version", "run_id", "eval_f1", …}

환경변수
  TRAIN_JOB_FILE   job/train.yaml   매니페스트 폴더 기준 템플릿 경로
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..datasets.job import TemplateJobs
from ..datasets.store import DatasetError, DatasetStore
from ..k8s.watcher import WatchManager
from ..manifests.files import ManifestStore

TRAIN_DATASET_RE = re.compile(r"^(seed|dataset-\d{8}-\d{6})$")
EVAL_FILE = "eval.csv"


@dataclass(frozen=True)
class TrainSettings:
    template: str = "job/train.yaml"

    @classmethod
    def from_env(cls) -> "TrainSettings":
        return cls(template=os.getenv("TRAIN_JOB_FILE", "").strip() or cls().template)


class TrainJobs(TemplateJobs):
    job_type = "train"
    what = "모델 학습"

    def __init__(self, manager: WatchManager, manifests: ManifestStore, settings: TrainSettings,
                 create: Callable[[str, dict[str, Any]], dict[str, Any]], datasets: DatasetStore,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(manager, manifests, settings.template, create, clock)
        self.settings = settings
        self.datasets = datasets

    def check_dataset(self, name: str) -> None:
        """Job 을 만들기 전에 확인 (없는 파일로 Job 을 띄워 1분 뒤에 실패하지 않게)."""
        if not TRAIN_DATASET_RE.match(name or ""):
            raise DatasetError(400, f"올바르지 않은 데이터셋 이름입니다: {name!r} (seed 또는 dataset-YYYYMMDD-HHMMSS)")
        self.datasets.check_root()
        root = self.datasets.root
        if name == "seed":
            if not (root / "seed.csv").is_file():
                raise DatasetError(404, "seed.csv 가 없습니다.")
        elif not (root / f"{name}.csv").is_file() or not (root / f"{name}.json").is_file():
            raise DatasetError(404, f"데이터셋이 없습니다: {name}")
        if not (root / EVAL_FILE).is_file():
            raise DatasetError(400, f"고정 평가셋 {EVAL_FILE} 이 데이터셋 폴더에 없습니다 (사람이 1회 배치 — train.md 4-3).")

    def eval_info(self) -> dict[str, Any]:
        p = self.datasets.root / EVAL_FILE
        return {"file": EVAL_FILE, "exists": p.is_file()}

    def start_for(self, dataset: str) -> dict[str, Any]:
        self.check_dataset(dataset)
        out = self.start({"DATASET": dataset})
        return {**out, "dataset": dataset}
