"""댓글 분류 모델 학습 (MLOps 실습가이드 1부 02장 train.py 와 같은 설정).

    python train.py                         # data/seed.csv → artifacts/
    python train.py --data x.csv --out dir  # 재학습 Job 등에서 경로 지정

산출물 (artifacts/)
  model.joblib     TF-IDF(char n-gram) + 로지스틱 회귀 Pipeline
  metrics.json     평가용 데이터 지표 (가이드와 같은 형식)
  model_info.json  모델 이름·버전·학습 환경·데이터 해시 — 서빙 서버 /info 와 화면에 표시,
                   Work 5 에서 DB(model_name / model_alias) 기록에 사용
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
from datetime import datetime, timezone

import joblib
import pandas as pd
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score, precision_score, recall_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

MODEL_NAME = "toxicity-filter"

# 가이드 02장 설정 (04장 실험값: (2, 5) 가 지표는 더 높지만 가이드 기본값 유지)
PARAMS = {
    "tfidf_ngram": (3, 6),
    "tfidf_min_df": 2,
    "test_size": 0.2,
    "random_state": 42,
    "max_iter": 1000,
}


def train(data_path: pathlib.Path, out_dir: pathlib.Path, params: dict = PARAMS) -> dict:
    raw = data_path.read_bytes()
    df = pd.read_csv(data_path)
    df["text"] = df["text"].astype(str)  # NaN 이 섞여도 벡터화 오류가 나지 않게

    X_train, X_test, y_train, y_test = train_test_split(
        df["text"], df["label"],
        test_size=params["test_size"], random_state=params["random_state"], stratify=df["label"],
    )
    model = Pipeline([
        ("tfidf", TfidfVectorizer(analyzer="char", ngram_range=tuple(params["tfidf_ngram"]),
                                  min_df=params["tfidf_min_df"])),
        ("clf", LogisticRegression(max_iter=params["max_iter"])),
    ])
    model.fit(X_train, y_train)

    pred = model.predict(X_test)
    metrics = {
        "f1": float(f1_score(y_test, pred)),
        "precision": float(precision_score(y_test, pred)),
        "recall": float(recall_score(y_test, pred)),
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
    }

    # 버전: 같은 데이터 + 같은 설정이면 같은 값 (언제 학습했든 같은 모델로 본다)
    data_sha = hashlib.sha256(raw).hexdigest()
    fingerprint = hashlib.sha256((data_sha + json.dumps(params, sort_keys=True)).encode()).hexdigest()[:12]
    info = {
        "name": MODEL_NAME,
        "version": os.getenv("MODEL_VERSION") or f"seed-{fingerprint}",
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sklearn_version": sklearn.__version__,
        "python_version": platform.python_version(),
        "params": {**params, "tfidf_ngram": list(params["tfidf_ngram"])},
        "data": {"file": data_path.name, "rows": int(len(df)), "sha256": data_sha[:16],
                 "label_counts": {str(k): int(v) for k, v in df["label"].value_counts().sort_index().items()}},
        "metrics": metrics,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out_dir / "model.joblib")
    (out_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "model_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def main() -> None:
    here = pathlib.Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="댓글 분류 모델 학습")
    ap.add_argument("--data", type=pathlib.Path, default=here / "data" / "seed.csv")
    ap.add_argument("--out", type=pathlib.Path, default=here / "artifacts")
    args = ap.parse_args()

    info = train(args.data, args.out)
    print("saved:", args.out / "model.joblib")
    print("version:", info["version"], "| sklearn", info["sklearn_version"], "| python", info["python_version"])
    print("metrics:", {k: round(v, 4) if isinstance(v, float) else v for k, v in info["metrics"].items()})


if __name__ == "__main__":
    main()
