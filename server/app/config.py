"""환경변수 기반 설정."""

import os
from dataclasses import dataclass
from pathlib import Path

# 개발용 기본 매니페스트 폴더 (로컬 PC)
DEV_MANIFEST_DIR = Path(__file__).resolve().parents[1] / "dev-manifests"
# 파드 안 기본 매니페스트 폴더 = NFS PVC 마운트 경로 (k8sapimd/deploy.md 5장)
IN_CLUSTER_MANIFEST_DIR = Path("/manifests")

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


def in_cluster() -> bool:
    """파드 안에서 실행 중인지 (ServiceAccount 파일 존재 여부 — WATCH_NAMESPACE 판단과 같은 기준)."""
    return SA_NAMESPACE_FILE.exists()


def resolve_manifest_dir() -> Path:
    """MANIFEST_DIR → (파드 안이면) /manifests → (로컬이면) 개발용 server/dev-manifests.

    파드는 uid 1000 으로 실행되어 이미지 안(/app)에 폴더를 만들 수 없으므로,
    파드 안에서는 NFS PVC 마운트 경로를 기본값으로 쓰고 폴더를 만들지 않는다.
    """
    raw = os.getenv("MANIFEST_DIR", "").strip()
    if raw:
        return Path(raw)
    if in_cluster():
        return IN_CLUSTER_MANIFEST_DIR
    DEV_MANIFEST_DIR.mkdir(exist_ok=True)
    return DEV_MANIFEST_DIR


def load_settings() -> Settings:
    return Settings(
        namespace=resolve_namespace(),
        watch_timeout_seconds=int(os.getenv("WATCH_TIMEOUT_SECONDS", "300")),
        manifest_dir=resolve_manifest_dir(),
    )
