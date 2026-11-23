"""모델 재학습 · 모델 교체 API (k8sapimd/train.md 8-1).

  GET  /api/v1/models                    등록 모델 버전 목록 + 선언·롤아웃·champion + 학습 Job 템플릿 상태
  POST /api/v1/models/train              {dataset} → 학습 Job (실행 중이면 409)
  GET  /api/v1/models/train/jobs         학습 Job 상태·결과 (TTL 로 지워지기 전까지)
  GET  /api/v1/models/deploy             선언 버전 · 롤아웃 상태 · 실제 서빙 버전 (조회 시 champion 이동)
  POST /api/v1/models/deploy             {version, confirm} → 워크로드 YAML 한 줄 수정 + 적용
  POST /api/v1/models/deploy/rollback    {confirm} → champion 버전으로 다시 교체
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ..datasets.store import DatasetError
from ..k8s.actions import k8s_error
from ..models.deploy import DeployService
from ..models.mlflow_rest import ModelError
from ..models.service import ModelService

router = APIRouter(prefix="/api/v1/models", tags=["models"])


class TrainBody(BaseModel):
    dataset: str


class DeployBody(BaseModel):
    version: str
    confirm: bool = False


class ConfirmBody(BaseModel):
    confirm: bool = False


def _svc(request: Request) -> ModelService:
    svc = getattr(request.app.state, "models", None)
    if svc is None:
        raise HTTPException(503, "모델 재학습 기능이 설정되지 않았습니다. (MLFLOW_TRACKING_URI)")
    return svc


def _deploy(svc: ModelService) -> DeployService:
    if svc.deploy is None:
        raise HTTPException(503, "모델 교체를 쓸 수 없습니다 — 매니페스트 관리(MANIFEST_DIR)가 설정되지 않았습니다.")
    return svc.deploy


async def _serving(request: Request) -> dict[str, Any] | None:
    """추론 서버 /info (실제 서빙 중인 버전·scikit-learn). 댓글 서비스가 없으면 None."""
    comments = getattr(request.app.state, "comments", None)
    if comments is None:
        return None
    return await comments.model_status()


def _need_confirm(confirm: bool, what: str) -> None:
    if not confirm:
        raise HTTPException(400, f"확인이 필요합니다. {what} 을(를) 진행하려면 confirm=true 를 함께 보내세요.")


async def _call(fn, *args, **kw):
    try:
        return await run_in_threadpool(fn, *args, **kw)
    except (ModelError, DatasetError) as e:
        raise HTTPException(e.status, e.message) from e


@router.get("")
async def list_models(request: Request):
    svc = _svc(request)
    serving = await _serving(request)
    template = await run_in_threadpool(svc.train.template_info)
    base = {"links": svc.links(), "template": template, "eval": await run_in_threadpool(svc.train.eval_info),
            "running": svc.train.running()}
    if svc.deploy is None:
        return {**base, "deploy_enabled": False, "items": [], "count": 0}
    body = await _call(svc.deploy.versions, serving)
    return {**base, "deploy_enabled": True, **body}


@router.post("/train", status_code=202)
async def start_train(request: Request, body: TrainBody):
    svc = _svc(request)
    try:
        return await _call(svc.train.start_for, body.dataset.strip())
    except HTTPException:
        raise
    except Exception as e:   # 쿠버네티스 API 오류 (권한 403 등)
        err = k8s_error(e)
        raise HTTPException(err.status, err.message) from e


@router.get("/train/jobs")
async def train_jobs(request: Request):
    svc = _svc(request)
    items = await run_in_threadpool(svc.train_jobs)
    template = await run_in_threadpool(svc.train.template_info)
    return {"count": len(items), "items": items, "ttl_seconds": template["ttl_seconds"]}


@router.get("/deploy")
async def deploy_state(request: Request):
    svc = _svc(request)
    dep = _deploy(svc)
    serving = await _serving(request)
    return {"links": svc.links(), **await _call(dep.state, serving)}


@router.post("/deploy", status_code=202)
async def deploy(request: Request, body: DeployBody):
    _need_confirm(body.confirm, "모델 교체")
    dep = _deploy(_svc(request))
    serving = await _serving(request)
    return await _call(dep.deploy, body.version, serving)


@router.post("/deploy/rollback", status_code=202)
async def rollback(request: Request, body: ConfirmBody):
    _need_confirm(body.confirm, "되돌리기")
    dep = _deploy(_svc(request))
    serving = await _serving(request)
    return await _call(dep.rollback, serving)
