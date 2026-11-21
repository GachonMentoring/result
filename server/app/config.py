"""환경변수 기반 설정."""

import os
from dataclasses import dataclass
from pathlib import Path

# 개발용 기본 매니페스트 폴더 (운영에서는 MANIFEST_DIR=NFS 마운트 경로)
DEV_MANIFEST_DIR = Path(__file__).resolve().parents[1] / "dev-manifests"

# 파드 안에서 실행될 때 ServiceAccount 토큰과 함께 마운트되는 자기 네임스페이스 파일
SA_NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


@dataclass(frozen=True)
class Settings:
    namespace: str
    watch_timeout_seconds: int
    manifest_dir: Path


def resolve_namespace() -> str:
    """WATCH_NAMESPACE → (파드 안이면) 자기 네임스페이스 순으로 결정."""
    ns = os.getenv("WATCH_NAMESPACE", "").strip()
    if ns:
        return ns
    if SA_NAMESPACE_FILE.exists():
        return SA_NAMESPACE_FILE.read_text().strip()
    raise RuntimeError(
        "조회할 네임스페이스를 알 수 없습니다. WATCH_NAMESPACE 환경변수를 지정하세요. (예: WATCH_NAMESPACE=mentor)"
    )


def resolve_manifest_dir() -> Path:
    """MANIFEST_DIR → (미지정이면) 개발용 server/dev-manifests (없으면 만든다)."""
    raw = os.getenv("MANIFEST_DIR", "").strip()
    if raw:
        return Path(raw)
    DEV_MANIFEST_DIR.mkdir(exist_ok=True)
    return DEV_MANIFEST_DIR


def load_settings() -> Settings:
    return Settings(
        namespace=resolve_namespace(),
        watch_timeout_seconds=int(os.getenv("WATCH_TIMEOUT_SECONDS", "300")),
        manifest_dir=resolve_manifest_dir(),
    )
