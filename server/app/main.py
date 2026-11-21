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

from .api.routes import router
from .config import load_settings
from .k8s.client import load_kube_config
from .k8s.registry import build_registry
from .k8s.watcher import WatchManager

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


def create_app(manager_factory: Callable[[], WatchManager] = default_manager) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager = manager_factory()
        manager.start()
        app.state.manager = manager
        try:
            yield
        finally:
            manager.stop()

    app = FastAPI(title="K8s Resource API", version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(),
        allow_methods=["GET", "POST", "DELETE"],   # 조회 + 삭제/재시작
        allow_headers=["*"],
    )
    app.include_router(router)

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
