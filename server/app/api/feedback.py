"""피드백 API — 재학습 데이터 조회(표) · 사람 정답 입력."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ..feedback.repo import FILTERS, FeedbackError, FeedbackRepo

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])


class LabelBody(BaseModel):
    true_label: Literal[0, 1]   # 0 정상 / 1 주의


def repo_of(request: Request) -> FeedbackRepo:
    repo = getattr(request.app.state, "feedback", None)
    if repo is None:
        raise HTTPException(503, "피드백 DB 가 설정되지 않았습니다. (DB_PASSWORD 등 환경변수)")
    return repo


@router.get("")
async def list_feedback(
    request: Request,
    filter: str = Query("all", description=f"{' / '.join(FILTERS)}"),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    repo = repo_of(request)
    try:
        return await run_in_threadpool(repo.list, filter, limit, offset)
    except FeedbackError as e:
        raise HTTPException(e.status, e.message) from e


@router.post("/{item_id}/label")
async def set_label(request: Request, item_id: str, body: LabelBody):
    repo = repo_of(request)
    try:
        return await run_in_threadpool(repo.set_label, item_id, body.true_label)
    except FeedbackError as e:
        raise HTTPException(e.status, e.message) from e
