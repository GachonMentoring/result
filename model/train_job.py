"""모델 재학습 Job (k8sapimd/train.md 4·6장) — 데이터셋 CSV 로 학습하고 MLflow 에 기록·등록한다.

    DATASET=dataset-20261123-205655 python train_job.py

입력 (NFS /csv, 읽기 전용)
  <DATASET>.csv / .json   ① 데이터셋 재생성 결과 (seed 행 → DB 피드백 행 순서). DATASET=seed 면 seed.csv
  eval.csv                고정 평가셋 — 모든 버전을 같은 시험지로 채점 (seed 의 가이드 분할 92행)

처리
  1. 정리: 공백·빈 문장 제거 → 평가셋 문장 제외(누수 방지) → text 중복 제거(마지막 = 최신 피드백 우선)
  2. 라벨별 최소 행 수 검사 (MIN_PER_LABEL) — 부족하면 등록하지 않고 실패
  3. 학습: train.py 의 Pipeline, DB 피드백 행에 FEEDBACK_WEIGHT 배 가중치
  4. 평가: eval.csv 로 f1·precision·recall·accuracy + 최적 임계값(참고) + 피드백 반영률(fb_fit)
  5. MLflow: run(params·metrics·dataset input·태그) → 모델 저장 + model_info.json → 등록 모델 새 버전
  6. 결과 요약 JSON 한 줄을 /dev/termination-log 에 → 플랫폼 API 가 파드 상태에서 읽어 화면 표시

환경변수
  DATASET (필수)  CSV_DIR=/csv  EVAL_FILE=eval.csv  SEED_FILE=seed.csv
  MIN_PER_LABEL=20  FEEDBACK_WEIGHT=3
  MLFLOW_TRACKING_URI  MLFLOW_EXPERIMENT_NAME=toxicity-filter  REGISTERED_MODEL=toxicity-filter
  JOB_NAME (Job 이름 — run 이름·태그)  IMAGE (기록용)  TERMINATION_LOG=/dev/termination-log
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import platform
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import sklearn
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score

from train import MODEL_NAME, PARAMS, build_model

DATASET_RE = re.compile(r"^(seed|dataset-\d{8}-\d{6})$")
LABELS = (0, 1)


class TrainError(Exception):
    """사람이 읽을 수 있는 실패 이유 (종료 메시지로 화면에 그대로 표시)."""


@dataclass(frozen=True)
class Settings:
    dataset: str
    csv_dir: pathlib.Path = pathlib.Path("/csv")
    eval_file: str = "eval.csv"
    seed_file: str = "seed.csv"
    min_per_label: int = 20
    feedback_weight: float = 3.0
    experiment: str = "toxicity-filter"
    registered_model: str = MODEL_NAME
    job_name: str = ""
    image: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        def num(key: str, default: float, cast=float):
            raw = os.getenv(key, "").strip()
            try:
                return cast(raw) if raw else default
            except ValueError as e:
                raise TrainError(f"환경변수 {key}={raw!r} 는 숫자가 아닙니다.") from e

        d = cls(dataset="")
        return cls(
            dataset=os.getenv("DATASET", "").strip(),
            csv_dir=pathlib.Path(os.getenv("CSV_DIR", "").strip() or d.csv_dir),
            eval_file=os.getenv("EVAL_FILE", "").strip() or d.eval_file,
            seed_file=os.getenv("SEED_FILE", "").strip() or d.seed_file,
            min_per_label=num("MIN_PER_LABEL", d.min_per_label, int),
            feedback_weight=num("FEEDBACK_WEIGHT", d.feedback_weight),
            experiment=os.getenv("MLFLOW_EXPERIMENT_NAME", "").strip() or d.experiment,
            registered_model=os.getenv("REGISTERED_MODEL", "").strip() or d.registered_model,
            job_name=os.getenv("JOB_NAME", "").strip(),
            image=os.getenv("IMAGE", "").strip(),
        )


@dataclass
class Loaded:
    name: str
    df: pd.DataFrame            # label, text, fb(DB 피드백 행 여부)
    sha256: str
    seed_rows: int
    seed_sha256: str | None
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------- 읽기
def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_labeled_csv(path: pathlib.Path, what: str) -> pd.DataFrame:
    """label,text CSV → DataFrame. 형식이 틀리면 TrainError (잘못된 파일로 조용히 학습하지 않게)."""
    if not path.is_file():
        raise TrainError(f"{what} 파일이 없습니다: {path}")
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", dtype={"text": str}, keep_default_na=False)
    except (ValueError, UnicodeDecodeError, pd.errors.ParserError) as e:
        raise TrainError(f"{what} 파일을 읽을 수 없습니다 ({path.name}): {e}") from e
    if list(df.columns) != ["label", "text"]:
        raise TrainError(f"{what} 파일의 첫 줄이 'label,text' 가 아닙니다 ({path.name}: {','.join(map(str, df.columns))})")
    labels = pd.to_numeric(df["label"], errors="coerce")
    bad = ~labels.isin(LABELS)
    if bad.any():
        row = int(bad.idxmax()) + 2   # 헤더 + 1부터
        raise TrainError(f"{what} 파일 {row}번째 줄의 label 이 0/1 이 아닙니다: {df['label'].iloc[row - 2]!r}")
    df["label"] = labels.astype(int)
    return df


def load_dataset(s: Settings) -> Loaded:
    if not DATASET_RE.match(s.dataset):
        raise TrainError(f"데이터셋 이름이 올바르지 않습니다: {s.dataset!r} (seed 또는 dataset-YYYYMMDD-HHMMSS)")
    if s.dataset == "seed":
        path = s.csv_dir / s.seed_file
        df = read_labeled_csv(path, "seed")
        sha = sha256_of(path)
        df["fb"] = False
        return Loaded("seed", df, sha, len(df), sha, {"file": path.name})

    path, meta_path = s.csv_dir / f"{s.dataset}.csv", s.csv_dir / f"{s.dataset}.json"
    if not meta_path.is_file():
        raise TrainError(f"데이터셋 요약 파일이 없습니다: {meta_path.name} (생성이 끝나지 않았거나 지워짐)")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TrainError(f"데이터셋 요약 파일을 읽을 수 없습니다: {e}") from e
    df = read_labeled_csv(path, "데이터셋")
    sha = sha256_of(path)
    if meta.get("sha256") and meta["sha256"] != sha:
        raise TrainError(f"데이터셋 파일이 생성 후 바뀌었습니다 (sha256 불일치: {s.dataset}.csv)")
    seed = (meta.get("sources") or {}).get("seed") or {}
    seed_rows = int(seed.get("rows") or 0)
    if seed_rows > len(df):
        raise TrainError(f"요약의 seed 행 수({seed_rows})가 전체 행 수({len(df)})보다 큽니다.")
    df["fb"] = [False] * seed_rows + [True] * (len(df) - seed_rows)   # ① 이 seed → DB 순서로 씀
    return Loaded(s.dataset, df, sha, seed_rows, seed.get("sha256"), meta)


# ---------------------------------------------------------------------- 정리 · 학습 · 평가
def prepare(df: pd.DataFrame, eval_texts: set[str], min_per_label: int) -> tuple[pd.DataFrame, dict[str, int]]:
    """정리 규칙 (train.md 4-2). 원본 df 는 바꾸지 않는다."""
    d = df.copy()
    d["text"] = d["text"].astype(str).str.strip()
    stats = {"rows_raw": len(d)}
    d = d[d["text"] != ""]
    stats["rows_empty"] = stats["rows_raw"] - len(d)

    before = len(d)
    d = d[~d["text"].isin(eval_texts)]
    stats["rows_eval_excluded"] = before - len(d)

    # 같은 문장에 라벨이 여러 개였던 문장 수 (중복 제거 전에 센다)
    stats["rows_conflict"] = int((d.groupby("text")["label"].nunique() > 1).sum())
    before = len(d)
    d = d.drop_duplicates(subset="text", keep="last")   # 마지막 = DB 피드백(최신)이 seed 를 덮음
    stats["rows_dup_removed"] = before - len(d)

    stats["n_train"] = len(d)
    stats["rows_feedback"] = int(d["fb"].sum())
    counts = d["label"].value_counts()
    short = [f"라벨 {lab} 이 {int(counts.get(lab, 0))}행" for lab in LABELS if counts.get(lab, 0) < min_per_label]
    if short:
        raise TrainError(f"학습 데이터 부족: {', '.join(short)}뿐 (라벨별 최소 {min_per_label}행, 정리 후 기준)")
    return d.reset_index(drop=True), stats


def fit(train_df: pd.DataFrame, feedback_weight: float, params: dict = PARAMS):
    model = build_model(params)
    weights = np.where(train_df["fb"], feedback_weight, 1.0)
    model.fit(train_df["text"], train_df["label"], clf__sample_weight=weights)
    return model


def evaluate(model, eval_df: pd.DataFrame, train_df: pd.DataFrame) -> dict[str, float]:
    y = eval_df["label"].to_numpy()
    proba = model.predict_proba(eval_df["text"])[:, 1]
    pred = (proba >= 0.5).astype(int)
    m = {
        "eval_f1": float(f1_score(y, pred, zero_division=0)),
        "eval_precision": float(precision_score(y, pred, zero_division=0)),
        "eval_recall": float(recall_score(y, pred, zero_division=0)),
        "eval_accuracy": float(accuracy_score(y, pred)),
        "n_eval": float(len(eval_df)),
    }
    # 참고용: 평가셋 기준 f1 이 가장 높은 임계값 (서비스 THRESHOLD 는 바꾸지 않음)
    grid = np.round(np.arange(0.05, 0.951, 0.05), 2)
    scores = [f1_score(y, (proba >= t).astype(int), zero_division=0) for t in grid]
    m["best_threshold"] = float(grid[int(np.argmax(scores))])
    fb = train_df[train_df["fb"]]
    if len(fb):   # 사람이 고친 정답을 모델이 그대로 맞히는 비율
        m["fb_fit"] = float((model.predict(fb["text"]) == fb["label"]).mean())
    return m


def fingerprint(data_sha: str, eval_sha: str, s: Settings, params: dict = PARAMS) -> str:
    key = json.dumps({"data": data_sha, "eval": eval_sha, "params": params,
                      "feedback_weight": s.feedback_weight, "min": s.min_per_label}, sort_keys=True, default=list)
    return hashlib.sha256(key.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------- 실행
def run(s: Settings) -> dict[str, Any]:
    """학습 → MLflow 기록 → 등록. 성공하면 요약 dict, 실패하면 TrainError (등록 안 함)."""
    import mlflow
    import mlflow.data
    import mlflow.sklearn
    from mlflow import MlflowClient

    data = load_dataset(s)
    eval_path = s.csv_dir / s.eval_file
    eval_df = read_labeled_csv(eval_path, "평가셋")
    eval_df["text"] = eval_df["text"].astype(str).str.strip()
    eval_sha = sha256_of(eval_path)
    if eval_df["label"].nunique() < 2:
        raise TrainError("평가셋에 라벨이 한 종류뿐입니다.")

    train_df, stats = prepare(data.df, set(eval_df["text"]), s.min_per_label)
    fp = fingerprint(data.sha256, eval_sha, s)
    build = f"{'seed' if data.name == 'seed' else 'fb'}-{datetime.now(timezone.utc):%Y%m%d}-{fp}"

    mlflow.set_experiment(s.experiment)
    with mlflow.start_run(run_name=s.job_name or build) as run:
        run_id = run.info.run_id
        mlflow.set_tags({
            "dataset": data.name, "dataset_sha256": data.sha256, "eval_sha256": eval_sha,
            "seed_sha256": data.seed_sha256 or "", "k8s_job": s.job_name, "image": s.image,
            "build": build, "trigger": "platform" if s.job_name else "manual",
        })
        mlflow.log_params({
            "tfidf_ngram": ",".join(map(str, PARAMS["tfidf_ngram"])), "tfidf_min_df": PARAMS["tfidf_min_df"],
            "max_iter": PARAMS["max_iter"], "min_per_label": s.min_per_label,
            "feedback_weight": s.feedback_weight,
        })
        mlflow.log_input(mlflow.data.from_pandas(train_df[["label", "text"]], source=str(s.csv_dir / f"{data.name}.csv"),
                                                 name=data.name, targets="label"), context="training")
        mlflow.log_input(mlflow.data.from_pandas(eval_df, source=str(eval_path), name="eval", targets="label"),
                         context="eval")
        mlflow.log_metrics({k: float(v) for k, v in stats.items()})
        if data.meta:
            mlflow.log_dict(data.meta, f"dataset/{data.name}.json")

        model = fit(train_df, s.feedback_weight)
        metrics = evaluate(model, eval_df, train_df)
        mlflow.log_metrics(metrics)

        info = {
            "name": s.registered_model,
            "version": build,                      # 서빙 시 initContainer 의 SOURCE(models:/…/N) 가 덮어씀
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sklearn_version": sklearn.__version__,
            "python_version": platform.python_version(),
            "params": {**PARAMS, "tfidf_ngram": list(PARAMS["tfidf_ngram"]),
                       "feedback_weight": s.feedback_weight, "min_per_label": s.min_per_label},
            "data": {"dataset": data.name, "rows": stats["rows_raw"], "n_train": stats["n_train"],
                     "rows_feedback": stats["rows_feedback"], "sha256": data.sha256[:16],
                     "label_counts": {str(k): int(v) for k, v in train_df["label"].value_counts().sort_index().items()}},
            "eval": {"file": s.eval_file, "rows": len(eval_df), "sha256": eval_sha[:16]},
            "metrics": {"f1": metrics["eval_f1"], "precision": metrics["eval_precision"],
                        "recall": metrics["eval_recall"], "accuracy": metrics["eval_accuracy"]},
            "run_id": run_id,
        }
        # input_example 은 넘기지 않음 — signature 추론이 실패하고, MLflow 서빙을 쓰지 않아 불필요 (train.md 2-1)
        logged = mlflow.sklearn.log_model(model, name="model", serialization_format="pickle")
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "model_info.json"
            p.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            MlflowClient().log_model_artifact(logged.model_id, str(p))

        # 여기까지 모두 성공한 경우에만 등록 (중간 실패 → run 은 FAILED, 등록 모델 버전 없음)
        mv = mlflow.register_model(logged.model_uri, s.registered_model)
        client = MlflowClient()
        tags = {"dataset": data.name, "eval_f1": f"{metrics['eval_f1']:.4f}", "eval_sha256": eval_sha,
                "sklearn_version": sklearn.__version__, "k8s_job": s.job_name, "run_id": run_id,
                "n_train": str(stats["n_train"]), "rows_feedback": str(stats["rows_feedback"]), "build": build}
        for k, v in tags.items():
            client.set_model_version_tag(s.registered_model, mv.version, k, v)
        mlflow.set_tag("registered_version", mv.version)

    return {"ok": True, "version": str(mv.version), "model": s.registered_model, "run_id": run_id,
            "dataset": data.name, "build": build, "n_train": stats["n_train"],
            "rows_feedback": stats["rows_feedback"], "eval_f1": round(metrics["eval_f1"], 4),
            "fb_fit": round(metrics["fb_fit"], 4) if "fb_fit" in metrics else None}


def write_termination(summary: dict[str, Any]) -> None:
    line = json.dumps(summary, ensure_ascii=False)
    print(line, flush=True)
    path = os.getenv("TERMINATION_LOG", "/dev/termination-log")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(line[:4000])   # 쿠버네티스 종료 메시지 상한 4KB
    except OSError:
        pass   # 로컬 실행 (파일 없음)


def main() -> int:
    try:
        summary = run(Settings.from_env())
    except TrainError as e:
        write_termination({"ok": False, "error": str(e)})
        return 1
    except Exception as e:   # MLflow 연결 실패 등 — 이유를 화면에 보이도록 요약
        write_termination({"ok": False, "error": f"{type(e).__name__}: {str(e)[:500]}"})
        raise
    write_termination(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
