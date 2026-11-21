"""매니페스트 관리 API — 파일(NFS) 수정·저장·삭제 + 클러스터 apply·판정."""

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..manifests.cluster import ClusterError, ManifestService
from ..manifests.files import FileError

router = APIRouter(prefix="/api/v1/manifests", tags=["manifests"])

T = TypeVar("T")


class SaveBody(BaseModel):
    content: str
    etag: str | None = None   # 불러올 때 받은 etag. 지금 파일과 다르면 409 (외부에서 바뀜)


class CreateBody(BaseModel):
    content: str


def _svc(request: Request) -> ManifestService:
    svc = getattr(request.app.state, "manifests", None)
    if svc is None:
        raise HTTPException(503, "매니페스트 관리가 설정되지 않았습니다. (MANIFEST_DIR)")
    return svc


def _error_response(e: FileError | ClusterError) -> JSONResponse:
    body: dict[str, Any] = {"detail": e.message}
    if isinstance(e, FileError) and e.line:
        body["line"] = e.line
    if isinstance(e, ClusterError) and e.conflicts:
        body["conflicts"] = e.conflicts
    return JSONResponse(body, status_code=e.status)


async def _call(fn: Callable[..., T], *args: Any) -> T | JSONResponse:
    """파일·k8s 작업은 블로킹이라 스레드풀에서. 도메인 오류는 상태 코드 + 상세로 변환."""
    try:
        return await run_in_threadpool(fn, *args)
    except (FileError, ClusterError) as e:
        return _error_response(e)


def _need_confirm(confirm: bool, what: str) -> None:
    # "정말 하시겠습니까?" — 화면의 [예]를 누른 요청만 confirm=true 를 보낸다.
    if not confirm:
        raise HTTPException(400, f"확인이 필요합니다. {what} 을(를) 진행하려면 confirm=true 를 함께 보내세요.")


# ---------------------------------------------------------------------- 파일
@router.get("")
async def list_files(request: Request):
    svc = _svc(request)

    def run():
        files = svc.store.list()
        return {"root": str(svc.store.root), "namespace": svc.namespace,
                "files": [f.__dict__ for f in files]}
    return await _call(run)


@router.get("/file")
async def read_file(request: Request, path: str):
    return await _call(_svc(request).store.read, path)


@router.put("/file")
async def save_file(request: Request, path: str, body: SaveBody,
                    force: bool = Query(False, description="외부 변경을 무시하고 덮어쓰기")):
    return await _call(_svc(request).store.save, path, body.content, body.etag, force)


@router.post("/file", status_code=201)
async def create_file(request: Request, path: str, body: CreateBody):
    return await _call(_svc(request).store.create, path, body.content)


@router.delete("/file")
async def delete_file(request: Request, path: str, confirm: bool = False,
                      with_resources: bool = Query(True, description="파일이 선언한 클러스터 리소스도 삭제")):
    _need_confirm(confirm, f"'{path}' 삭제")
    return await _call(_svc(request).delete_file, path, with_resources)


# ---------------------------------------------------------------------- 판정 / apply
@router.get("/status")
async def file_status(request: Request, path: str):
    """한 파일의 판정 + 리소스별 diff·충돌 (적용 전 미리보기로도 사용)."""
    return await _call(_svc(request).judge_file, path)


@router.get("/status-all")
async def all_status(request: Request):
    """모든 파일의 판정 요약 + 선언 없는 리소스."""
    return await _call(_svc(request).judge_all)


@router.post("/apply")
async def apply_file(request: Request, path: str, confirm: bool = False,
                     force: bool = Query(False, description="다른 관리자 소유 필드 충돌 시 강제로 가져오기")):
    _need_confirm(confirm, f"'{path}' 적용")
    return await _call(_svc(request).apply_file, path, force)


@router.delete("/orphans")
async def delete_orphan(request: Request, api_version: str, kind: str, name: str, confirm: bool = False):
    _need_confirm(confirm, f"{kind}/{name} 삭제")

    def run():
        existed = _svc(request).delete_orphan(api_version, kind, name)
        return {"kind": kind, "name": name, "deleted": existed}
    return await _call(run)
