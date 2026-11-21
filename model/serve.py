"""댓글 분류 모델 서버 — 추론(확률 계산)만 담당.

가이드 1부 app.py 중 "모델 로드 + predict_proba" 부분만 남긴 것.
입력 검증·임계값 판정·DB 저장·피드백 등 서비스 로직은 플랫폼 서버(server/)가 맡는다.

    uvicorn serve:app --host 0.0.0.0 --port 8088
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
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

log = logging.getLogger("model")
MAX_TEXT = 300  # 학습 데이터가 짧은 한 줄 댓글(9~32자) — 가이드와 같은 상한


class PredictReq(BaseModel):
    text: str


def load_info(model_path: pathlib.Path) -> dict[str, Any]:
    """model.joblib 옆의 model_info.json (없으면 최소 정보)."""
    p = model_path.with_name("model_info.json")
    info = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {
        "name": os.getenv("MODEL_NAME", "toxicity-filter"), "version": "unknown"}
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 서버가 켜질 때 한 번만 로드 (실패하면 서버가 뜨지 않음 → 파드가 재시작되며 원인이 로그에 남음)
        state["model"] = joblib.load(path)
        state["info"] = load_info(path)
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

    @app.post("/predict")
    def predict(req: PredictReq):
        if not req.text.strip() or len(req.text) > MAX_TEXT:
            raise HTTPException(400, f"text length must be 1..{MAX_TEXT}")
        proba = float(state["model"].predict_proba([req.text])[0][1])
        i = state["info"]
        return {"toxic_proba": proba, "model": {"name": i["name"], "version": i["version"]}}

    return app


app = create_app()
