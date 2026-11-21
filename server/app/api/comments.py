"""댓글 기능 API — 댓글 입력 → 모델 추론 → 판정 결과 + 피드백 DB 저장 (Work 5)."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ..comments.service import CommentError, CommentService
from ..feedback.repo import FeedbackError

router = APIRouter(prefix="/api/v1/comments", tags=["comments"])
log = logging.getLogger(__name__)


class PredictBody(BaseModel):
    text: str


def _svc(request: Request) -> CommentService:
    svc = getattr(request.app.state, "comments", None)
    if svc is None:
        raise HTTPException(503, "댓글 서비스가 설정되지 않았습니다. (MODEL_URL)")
    return svc


@router.post("/predict")
async def predict(request: Request, body: PredictBody):
    try:
        result = await _svc(request).predict(body.text)
    except CommentError as e:
        raise HTTPException(e.status, e.message) from e

    # 분류할 때마다 재학습 데이터로 저장. DB 장애가 있어도 분류 결과는 돌려준다 (saved=false 로 알림)
    result.update(id=None, saved=False, save_error=None)
    repo = getattr(request.app.state, "feedback", None)
    if repo is None:
        result["save_error"] = "피드백 DB 가 설정되지 않아 저장하지 않았습니다."
        return result
    model = result.get("model") or {}
    try:
        saved = await run_in_threadpool(
            lambda: repo.insert(
                text=result["text"], pred_label=result["label"], toxic_proba=result["toxic_proba"],
                threshold=result["threshold"], model_name=model.get("name") or "unknown",
                model_version=model.get("version") or "unknown",
            )
        )
        result.update(id=saved["id"], saved=True, created_at=saved["created_at"])
    except FeedbackError as e:
        log.warning("피드백 저장 실패: %s", e.message)
        result["save_error"] = e.message
    return result


@router.get("/model")
async def model_status(request: Request):
    return await _svc(request).model_status()
