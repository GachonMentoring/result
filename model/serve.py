"""댓글 분류 모델 서버 — 추론(확률 계산)만 담당.

가이드 1부 app.py 중 "모델 로드 + predict_proba" 부분만 남긴 것.
입력 검증·임계값 판정·DB 저장·피드백 등 서비스 로직은 플랫폼 서버(server/)가 맡는다.

    uvicorn serve:app --host 0.0.0.0 --port 8088

/metrics 는 Prometheus 수집용 (k8sapimd/monitoring.md 1장) — Ingress 에 노출하지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from contextlib import asynccontextmanager
from typing import Any

import joblib
import sklearn
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

log = logging.getLogger("model")
MAX_TEXT = 300  # 학습 데이터가 짧은 한 줄 댓글(9~32자) — 가이드와 같은 상한


class PredictReq(BaseModel):
    text: str


class Metrics:
    """Prometheus 메트릭 (k8sapimd/monitoring.md 1장). 앱마다 레지스트리를 따로 둔다 (테스트에서 앱을 여러 번 만듦).

    판정(임계값)은 플랫폼이 하므로 여기서는 확률 그대로를 기록한다.
    라벨에는 model_version 만 — 댓글 원문은 절대 넣지 않는다.
    """

    def __init__(self) -> None:
        r = self.registry = CollectorRegistry()
        self.info = Gauge("toxicity_model_info", "지금 서빙 중인 모델 (값은 항상 1)",
                          ["name", "version", "build", "sklearn_version", "runtime_sklearn_version"], registry=r)
        self.mismatch = Gauge("toxicity_model_version_mismatch", "학습·서빙 scikit-learn 버전 불일치 (1=불일치)",
                              registry=r)
        self.loaded = Gauge("toxicity_model_loaded_timestamp_seconds", "모델을 로드한 시각 (unix 초)", registry=r)
        self.requests = Counter("toxicity_predict_requests", "추론 요청 수 (result: ok / bad_request / error)",
                                ["model_version", "result"], registry=r)
        self.duration = Histogram("toxicity_predict_duration_seconds", "추론 계산 시간 (predict_proba)",
                                  registry=r, buckets=(.005, .01, .025, .05, .1, .25, .5, 1))
        self.proba = Histogram("toxicity_predict_proba", "예측 유해 확률 분포", ["model_version"], registry=r,
                               buckets=tuple(round(i / 10, 1) for i in range(1, 11)))
        # 학습 데이터는 9~32자 짧은 댓글 → 32 초과 비율 = 학습 범위 밖 입력 비율
        self.length = Histogram("toxicity_input_length_chars", "입력 댓글 길이 (글자 수)", registry=r,
                                buckets=(10, 20, 32, 50, 100, 200, 300))

    def set_model(self, info: dict[str, Any]) -> None:
        self.info.clear()
        self.info.labels(
            name=str(info.get("name") or ""), version=str(info.get("version") or ""),
            build=str(info.get("build") or ""), sklearn_version=str(info.get("sklearn_version") or ""),
            runtime_sklearn_version=str(info.get("runtime_sklearn_version") or ""),
        ).set(1)
        self.mismatch.set(1 if info.get("version_mismatch") else 0)
        self.loaded.set_to_current_time()


def load_info(model_path: pathlib.Path) -> dict[str, Any]:
    """모델 파일 옆의 model_info.json (없으면 최소 정보).

    MLflow 에서 받은 모델이면 initContainer 가 같은 폴더에 SOURCE(`models:/toxicity-filter/3`)를 남긴다
    → 버전을 등록 모델 버전(`toxicity-filter/3`)으로 보고 (DB model_version 에 그대로 기록됨, train.md 6-3).
    학습 시 지문(fb-…/seed-…)은 build 로 보존.
    """
    p = model_path.with_name("model_info.json")
    info = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {
        "name": os.getenv("MODEL_NAME", "toxicity-filter"), "version": "unknown"}
    src = model_path.with_name("SOURCE")
    if src.exists():
        uri = src.read_text(encoding="utf-8").strip()
        if uri.startswith("models:/") and len(uri) > len("models:/"):
            info["build"] = info.get("version")
            info["version"] = uri[len("models:/"):]
            info["source"] = uri
    info["runtime_sklearn_version"] = sklearn.__version__
    trained = info.get("sklearn_version")
    # 가이드 08장 시나리오 3: 학습·서빙 버전이 다르면 결과를 믿을 수 없다 → 숨기지 않고 노출
    info["version_mismatch"] = bool(trained and trained != sklearn.__version__)
    if info["version_mismatch"]:
        log.warning("scikit-learn 버전 불일치: 학습 %s / 서빙 %s", trained, sklearn.__version__)
    return info


def create_app(model_path: str | os.PathLike[str] | None = None) -> FastAPI:
    path = pathlib.Path(model_path or os.getenv("MODEL_PATH", "artifacts/model.joblib"))
    state: dict[str, Any] = {}
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 서버가 켜질 때 한 번만 로드 (실패하면 서버가 뜨지 않음 → 파드가 재시작되며 원인이 로그에 남음)
        state["model"] = joblib.load(path)
        state["info"] = load_info(path)
        metrics.set_model(state["info"])
        log.info("모델 로드: %s %s (%s)", state["info"]["name"], state["info"]["version"], path)
        yield
        state.clear()

    app = FastAPI(title="toxicity-filter-model", lifespan=lifespan)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/info")
    def info():
        return state["info"]

    @app.get("/metrics")
    def prometheus_metrics():
        # Ingress 에는 노출하지 않는다 — Prometheus 가 파드 IP 로 직접 수집 (monitoring.md 0장)
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/predict")
    def predict(req: PredictReq):
        i = state["info"]
        version = str(i["version"])
        if not req.text.strip() or len(req.text) > MAX_TEXT:
            metrics.requests.labels(version, "bad_request").inc()
            raise HTTPException(400, f"text length must be 1..{MAX_TEXT}")
        metrics.length.observe(len(req.text))
        try:
            with metrics.duration.time():
                proba = float(state["model"].predict_proba([req.text])[0][1])
        except Exception:
            metrics.requests.labels(version, "error").inc()
            raise
        metrics.requests.labels(version, "ok").inc()
        metrics.proba.labels(version).observe(proba)
        return {"toxic_proba": proba, "model": {"name": i["name"], "version": i["version"]}}

    return app


app = create_app()
