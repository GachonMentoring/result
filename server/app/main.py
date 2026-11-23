"""K8s 리소스 조회·조작 API 서버.

실행: WATCH_NAMESPACE=mentor uvicorn app.main:app --port 8000
"""

import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.comments import router as comments_router
from .api.datasets import router as datasets_router
from .api.feedback import router as feedback_router
from .api.manifests import router as manifests_router
from .api.models import router as models_router
from .api.routes import router
from .config import load_settings, resolve_csv_dir, resolve_manifest_dir
from .comments.service import CommentService, CommentSettings
from .datasets.job import DatasetJobs, JobSettings, kube_creator
from .datasets.store import DatasetStore
from .feedback.repo import DbSettings, FeedbackRepo
from .k8s.client import load_kube_config
from .k8s.registry import build_registry
from .k8s.watcher import WatchManager
from .manifests.cluster import DynamicClusterAPI, ManifestService
from .manifests.files import ManifestStore
from .models.deploy import DeployService
from .models.mlflow_rest import MlflowREST
from .models.service import ModelService, ModelSettings
from .models.train import TrainJobs, TrainSettings

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


DEFAULT_CORS_ORIGINS = "http://localhost:5500,http://127.0.0.1:5500"


def cors_origins() -> list[str]:
    """프론트(front/) 주소. 쉼표로 여러 개 지정."""
    raw = os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return [o.strip() for o in raw.split(",") if o.strip()]


def default_manager() -> WatchManager:
    settings = load_settings()
    load_kube_config()
    return WatchManager(build_registry(), settings.namespace, settings.watch_timeout_seconds)


def default_manifests(manager: WatchManager) -> ManifestService:
    """매니페스트 폴더(MANIFEST_DIR, 기본 server/dev-manifests) + 같은 네임스페이스."""
    return ManifestService(ManifestStore(resolve_manifest_dir()), DynamicClusterAPI(), manager.namespace)


def default_comments() -> CommentService:
    """댓글 서비스: 모델 서버(MODEL_URL) 호출 + 임계값(THRESHOLD) 판정."""
    return CommentService(CommentSettings.from_env())


def default_feedback() -> FeedbackRepo | None:
    """피드백 DB (DB_HOST/PORT/NAME/USER/PASSWORD). DB_PASSWORD 가 없으면 꺼짐 — 분류는 계속 동작."""
    settings = DbSettings.from_env()
    if settings is None:
        logging.getLogger(__name__).warning("DB_PASSWORD 미설정 → 피드백 저장·조회 비활성")
        return None
    logging.getLogger(__name__).info("피드백 DB: %s", settings.describe())
    return FeedbackRepo(settings)


def default_datasets(manager: WatchManager) -> tuple[DatasetStore, DatasetJobs]:
    """데이터셋 폴더(CSV_DIR, 기본 server/dev-csv) + 데이터셋 Job 생성 (k8sapimd/dataset.md).

    Job 명세는 매니페스트 폴더의 템플릿 YAML (DATASET_JOB_FILE, 기본 job/dataset.yaml).
    """
    return DatasetStore(resolve_csv_dir()), DatasetJobs(
        manager, ManifestStore(resolve_manifest_dir()), JobSettings.from_env(), kube_creator())


def default_models(manager: WatchManager, manifests: ManifestService | None,
                   datasets: tuple[DatasetStore, DatasetJobs] | None) -> ModelService | None:
    """모델 재학습·교체 (k8sapimd/train.md). MLFLOW_TRACKING_URI 가 없거나 데이터셋 기능이 꺼져 있으면 비활성."""
    settings = ModelSettings.from_env()
    if settings is None or datasets is None:
        logging.getLogger(__name__).warning("MLFLOW_TRACKING_URI 미설정 → 모델 재학습·교체 비활성")
        return None
    mlflow = MlflowREST(settings.tracking_uri)
    train = TrainJobs(manager, ManifestStore(resolve_manifest_dir()), TrainSettings.from_env(), kube_creator(),
                      datasets[0])
    deploy = DeployService(manifests, mlflow, manager, settings.deploy) if manifests is not None else None
    return ModelService(settings, mlflow, train, deploy)


def create_app(
    manager_factory: Callable[[], WatchManager] = default_manager,
    manifests_factory: Callable[[WatchManager], ManifestService | None] = default_manifests,
    comments_factory: Callable[[], CommentService | None] = default_comments,
    feedback_factory: Callable[[], FeedbackRepo | None] = default_feedback,
    datasets_factory: Callable[[WatchManager], tuple[DatasetStore, DatasetJobs] | None] = default_datasets,
    models_factory: Callable[..., ModelService | None] = default_models,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager = manager_factory()
        manager.start()
        app.state.manager = manager
        app.state.manifests = manifests_factory(manager)
        app.state.comments = comments_factory()
        app.state.feedback = feedback_factory()
        app.state.datasets = datasets_factory(manager)
        app.state.models = models_factory(manager, app.state.manifests, app.state.datasets)
        try:
            yield
        finally:
            manager.stop()
            if app.state.comments is not None:
                await app.state.comments.aclose()
            if app.state.models is not None:
                app.state.models.close()

    app = FastAPI(title="K8s Resource API", version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(),
        allow_methods=["GET", "POST", "PUT", "DELETE"],   # 조회 + 삭제/재시작 + 매니페스트 저장
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(manifests_router)
    app.include_router(comments_router)
    app.include_router(feedback_router)
    app.include_router(datasets_router)
    app.include_router(models_router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        m: WatchManager = app.state.manager
        pending = [k for k, w in m.watchers.items() if not w.synced.is_set()]
        if pending:
            return JSONResponse({"status": "syncing", "pending": pending}, status_code=503)
        return {"status": "ready"}

    return app


app = create_app()
