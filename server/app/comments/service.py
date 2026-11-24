"""댓글 분류 서비스 — 가이드 1부 app.py 의 서비스 로직 (입력 검증 · 모델 호출 · 판정).

모델 로드와 확률 계산은 별도 파드의 모델 서버(model/)가 하고, 여기서는 HTTP 로 부른다.
Work 5 에서 이 흐름에 DB 트랜잭션(predictions 저장, 이후 feedback)을 추가한다.

환경변수
  MODEL_URL        모델 서버 주소. 클러스터: http://toxicity-filter.mentor:8088 / 로컬: http://localhost:8088
  THRESHOLD        유해 판정 임계값 (기본 0.5). 확률 >= 임계값 → 1(주의)
  MODEL_TIMEOUT    모델 호출 제한 시간(초, 기본 3)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..metrics import MODEL_CALL_FAILURES

MAX_TEXT = 300                 # 가이드와 같은 상한 (학습 데이터가 짧은 한 줄 댓글)
UNCERTAIN_RANGE = (0.4, 0.6)   # 모델이 헷갈리는 구간 — 피드백 후보 (가이드 04장)
LABEL_NAMES = {0: "정상", 1: "주의"}


class CommentError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class CommentSettings:
    model_url: str
    threshold: float
    timeout: float

    @classmethod
    def from_env(cls) -> "CommentSettings":
        threshold = float(os.getenv("THRESHOLD", "0.5"))
        if not 0 < threshold < 1:
            raise RuntimeError(f"THRESHOLD 는 0과 1 사이여야 합니다: {threshold}")
        return cls(
            model_url=os.getenv("MODEL_URL", "http://localhost:8088").rstrip("/"),
            threshold=threshold,
            timeout=float(os.getenv("MODEL_TIMEOUT", "3")),
        )


def validate_text(text: str) -> str:
    if not text or not text.strip():
        raise CommentError(400, "댓글을 입력하세요.")
    if len(text) > MAX_TEXT:
        raise CommentError(400, f"댓글은 {MAX_TEXT}자 이하로 입력하세요. (현재 {len(text)}자)")
    return text


def judge(proba: float, threshold: float) -> dict[str, Any]:
    label = int(proba >= threshold)
    lo, hi = UNCERTAIN_RANGE
    return {"label": label, "label_name": LABEL_NAMES[label], "uncertain": lo <= proba <= hi}


class CommentService:
    def __init__(self, settings: CommentSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport
        self._http: httpx.AsyncClient | None = None   # 첫 호출 때 생성 (생성 비용: SSL 인증서 로드)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.settings.model_url, timeout=self.settings.timeout,
                                           transport=self._transport)
        try:
            return await self._http.request(method, path, **kw)
        except httpx.TimeoutException as e:
            MODEL_CALL_FAILURES.labels("timeout").inc()
            raise CommentError(503, f"모델 서버 응답 시간 초과 ({self.settings.timeout:g}초, {self.settings.model_url})") from e
        except httpx.HTTPError as e:
            MODEL_CALL_FAILURES.labels("connect").inc()
            raise CommentError(503, f"모델 서버에 연결할 수 없습니다 ({self.settings.model_url})") from e

    async def predict(self, text: str) -> dict[str, Any]:
        text = validate_text(text)
        started = time.perf_counter()
        r = await self._call("POST", "/predict", json={"text": text})
        latency_ms = round((time.perf_counter() - started) * 1000, 1)

        if r.status_code != 200:
            MODEL_CALL_FAILURES.labels("http_4xx" if r.status_code < 500 else "http_5xx").inc()
        if r.status_code == 400:
            raise CommentError(400, f"모델이 입력을 거부했습니다: {_detail(r)}")
        if r.status_code != 200:
            raise CommentError(502, f"모델 서버 오류 (HTTP {r.status_code}): {_detail(r)}")
        try:
            body = r.json()
            proba = float(body["toxic_proba"])
        except (ValueError, KeyError, TypeError) as e:
            MODEL_CALL_FAILURES.labels("invalid").inc()
            raise CommentError(502, "모델 서버 응답 형식이 올바르지 않습니다. (toxic_proba 없음)") from e

        return {
            "text": text,
            **judge(proba, self.settings.threshold),
            "toxic_proba": proba,
            "threshold": self.settings.threshold,
            # 구버전 모델 서버(가이드 05장 app.py)는 model 정보를 주지 않는다
            "model": body.get("model"),
            "latency_ms": latency_ms,
        }

    async def model_status(self) -> dict[str, Any]:
        """화면 상단 표시용: 연결 여부 + 모델 정보 (/info 가 없는 구버전이면 info 없음)."""
        out: dict[str, Any] = {"url": self.settings.model_url, "threshold": self.settings.threshold,
                               "connected": False, "info": None, "error": None}
        try:
            health = await self._call("GET", "/healthz")
            out["connected"] = health.status_code == 200
            if not out["connected"]:
                out["error"] = f"헬스체크 실패 (HTTP {health.status_code})"
                return out
            info = await self._call("GET", "/info")
            if info.status_code == 200:
                out["info"] = info.json()
        except CommentError as e:
            out["error"] = e.message
        return out


def _detail(r: httpx.Response) -> str:
    try:
        return str(r.json().get("detail", r.text))[:200]
    except ValueError:
        return r.text[:200]
