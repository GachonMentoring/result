"""모니터링 화면 설정 API (k8sapimd/monitoring.md 7장).

  GET /api/v1/monitoring    화면 [모니터링] 탭에 띄울 Grafana 공개 대시보드 URL

환경변수 (ConfigMap mentor-platform-config — 공개 URL 이라 Secret 아님)
  GRAFANA_MODEL_URL      모델 대시보드 공개 URL    예) https://grafanas.studylink.click/public-dashboards/<토큰>
  GRAFANA_PLATFORM_URL   플랫폼 대시보드 공개 URL
비어 있으면 화면은 "대시보드 준비 중" 을 보여 준다 (토큰 발급 전 — monitoring_인수인계.md 6~7장).
요청마다 환경변수를 읽지만, 파드에서는 ConfigMap 변경 후 재시작해야 반영된다 (deploy.md 2장).
"""

import os
from urllib.parse import urlparse

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/monitoring", tags=["monitoring"])

DASHBOARDS = (
    ("model", "모델", "GRAFANA_MODEL_URL"),
    ("platform", "플랫폼", "GRAFANA_PLATFORM_URL"),
)


def dashboard_url(env: str) -> tuple[str | None, str | None]:
    """(URL, 설정 오류). iframe src 로 쓰이므로 http(s) 절대 주소만 허용."""
    raw = os.getenv(env, "").strip()
    if not raw:
        return None, None
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or not u.netloc:
        return None, f"{env} 는 http(s):// 로 시작하는 주소여야 합니다."
    return raw, None


@router.get("")
def monitoring_config():
    items = []
    for key, title, env in DASHBOARDS:
        url, error = dashboard_url(env)
        items.append({"key": key, "title": title, "url": url, "error": error})
    return {"dashboards": items, "configured": all(d["url"] for d in items)}
