"""데이터셋 재생성 API (k8sapimd/dataset.md 7장).

  POST /api/v1/datasets          Job 생성 (실행 중이면 409)
  GET  /api/v1/datasets          seed.csv 정보 + 데이터셋 목록 + 실행 중인 Job + Job 명세 파일 상태
  GET  /api/v1/datasets/jobs     dataset Job 상태·결과 (TTL 로 지워지기 전까지)
  GET  /api/v1/datasets/{name}   요약 + 미리보기 20행
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from ..datasets.job import DatasetJobs
from ..datasets.store import DatasetError, DatasetStore
from ..k8s.actions import k8s_error

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


def _svc(request: Request) -> tuple[DatasetStore, DatasetJobs]:
    svc = getattr(request.app.state, "datasets", None)
    if svc is None:
        raise HTTPException(503, "데이터셋 기능이 설정되지 않았습니다.")
    return svc


def _raise(e: DatasetError):
    raise HTTPException(e.status, e.message) from e


@router.get("")
async def list_datasets(request: Request):
    store, jobs = _svc(request)
    try:
        items = await run_in_threadpool(store.list)
        seed = await run_in_threadpool(store.seed)
    except DatasetError as e:
        _raise(e)
    template = await run_in_threadpool(jobs.template_info)
    return {"root": str(store.root), "seed": seed, "count": len(items), "items": items,
            "running": jobs.running(), "template": template, "ttl_seconds": template["ttl_seconds"]}


@router.post("", status_code=202)
async def create_dataset(request: Request):
    _, jobs = _svc(request)
    try:
        return await run_in_threadpool(jobs.start)
    except DatasetError as e:
        _raise(e)
    except Exception as e:   # 쿠버네티스 API 오류 (권한 403 등) → 상태 코드·메시지 변환
        err = k8s_error(e)
        raise HTTPException(err.status, err.message) from e


@router.get("/jobs")
async def list_jobs(request: Request):
    _, jobs = _svc(request)
    items = jobs.list()
    template = await run_in_threadpool(jobs.template_info)
    return {"count": len(items), "items": items, "ttl_seconds": template["ttl_seconds"]}


@router.get("/{name}")
async def get_dataset(request: Request, name: str):
    store, _ = _svc(request)
    try:
        return await run_in_threadpool(store.get, name)
    except DatasetError as e:
        _raise(e)
