"""댓글 기능: 입력 검증 · 모델 호출 · 임계값 판정 · 모델 장애 처리 (모델 서버는 가짜 HTTP)."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.comments.service import CommentService, CommentSettings, judge
from app.k8s import summarize
from app.k8s.registry import KindSpec
from app.k8s.watcher import WatchManager
from app.main import create_app

MODEL = {"name": "toxicity-filter", "version": "seed-633bdbb4ded6"}
PROBAS = {"말이 너무 심하네요": 0.840195, "이 글 덕분에 이해가 잘 됐어요": 0.350889, "안녕하세요": 0.68575,
          "애매한 문장": 0.5}


def model_server(mode: str = "ok"):
    """가짜 모델 서버. mode: ok / legacy(가이드 05장 app.py) / down / timeout / error / garbage"""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if mode == "down":
            raise httpx.ConnectError("refused", request=req)
        if mode == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        if req.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if req.url.path == "/info":
            if mode == "legacy":
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json={**MODEL, "metrics": {"f1": 0.9245}, "version_mismatch": False})
        if req.url.path == "/predict":
            if mode == "error":
                return httpx.Response(500, text="Internal Server Error")
            if mode == "garbage":
                return httpx.Response(200, json={"result": "?"})
            text = json.loads(req.content)["text"]
            p = PROBAS.get(text, 0.1)
            if mode == "legacy":
                return httpx.Response(200, json={"label": int(p >= 0.5), "toxic_proba": p})
            return httpx.Response(200, json={"toxic_proba": p, "model": MODEL})
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


class StaticManager(WatchManager):
    def start(self):
        pass

    def stop(self, join_timeout: float = 2.0):
        pass


def _noop_list(namespace, **kw):
    raise AssertionError


def client_for(mode="ok", threshold=0.5):
    transport, calls = model_server(mode)
    svc = CommentService(CommentSettings("http://model.test", threshold, 3.0), transport=transport)
    m = StaticManager({"pods": KindSpec("pods", "Pod", "v1", _noop_list, summarize.pod_status)}, "mentor")
    return TestClient(create_app(lambda: m, lambda _m: None, lambda: svc)), calls


# ---------------------------------------------------------------------- 판정
def test_judge_threshold_and_uncertain():
    assert judge(0.84, 0.5) == {"label": 1, "label_name": "주의", "uncertain": False}
    assert judge(0.35, 0.5) == {"label": 0, "label_name": "정상", "uncertain": False}
    assert judge(0.5, 0.5)["label"] == 1                 # 가이드와 같이 >= 임계값이면 1
    assert judge(0.686, 0.5)["uncertain"] is False
    assert judge(0.55, 0.5)["uncertain"] is True
    assert judge(0.686, 0.7)["label"] == 0               # 임계값은 앱 서버가 정한다


# ---------------------------------------------------------------------- API
def test_predict_flow():
    c, calls = client_for()
    with c:
        r = c.post("/api/v1/comments/predict", json={"text": "말이 너무 심하네요"})
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == 1 and body["label_name"] == "주의"
    assert body["toxic_proba"] == pytest.approx(0.840195)
    assert body["threshold"] == 0.5 and body["model"] == MODEL
    assert body["latency_ms"] >= 0
    assert json.loads(calls[-1].content) == {"text": "말이 너무 심하네요"}


def test_threshold_from_settings():
    c, _ = client_for(threshold=0.7)
    with c:
        body = c.post("/api/v1/comments/predict", json={"text": "안녕하세요"}).json()
    assert body["label"] == 0 and body["threshold"] == 0.7


@pytest.mark.parametrize("text,status", [("", 400), ("   ", 400), ("가" * 301, 400)])
def test_validation_does_not_call_model(text, status):
    c, calls = client_for()
    with c:
        r = c.post("/api/v1/comments/predict", json={"text": text})
    assert r.status_code == status
    assert not [x for x in calls if x.url.path == "/predict"]


def test_missing_text_is_422():
    c, _ = client_for()
    with c:
        assert c.post("/api/v1/comments/predict", json={}).status_code == 422


@pytest.mark.parametrize("mode,status", [("down", 503), ("timeout", 503), ("error", 502), ("garbage", 502)])
def test_model_failures(mode, status):
    c, _ = client_for(mode)
    with c:
        r = c.post("/api/v1/comments/predict", json={"text": "안녕하세요"})
        # 모델이 죽어도 플랫폼 준비 상태는 영향 없음 (가이드 17장 원칙)
        ready = c.get("/healthz")
    assert r.status_code == status
    assert "모델" in r.json()["detail"]
    assert ready.status_code == 200


def test_legacy_model_server_compatible():
    """지금 클러스터의 가이드 05장 버전(label+toxic_proba, /info 없음)으로도 동작."""
    c, _ = client_for("legacy")
    with c:
        body = c.post("/api/v1/comments/predict", json={"text": "안녕하세요"}).json()
        status = c.get("/api/v1/comments/model").json()
    assert body["label"] == 1 and body["model"] is None
    assert status["connected"] is True and status["info"] is None


def test_model_status():
    c, _ = client_for()
    with c:
        ok = c.get("/api/v1/comments/model").json()
    assert ok["connected"] and ok["info"]["version"] == MODEL["version"] and ok["threshold"] == 0.5
    c, _ = client_for("down")
    with c:
        down = c.get("/api/v1/comments/model").json()
    assert down["connected"] is False and "연결할 수 없습니다" in down["error"]


def test_threshold_env_validation(monkeypatch):
    monkeypatch.setenv("THRESHOLD", "1.5")
    with pytest.raises(RuntimeError):
        CommentSettings.from_env()
    monkeypatch.setenv("THRESHOLD", "0.6")
    monkeypatch.setenv("MODEL_URL", "http://toxicity-filter.mentor:8088/")
    s = CommentSettings.from_env()
    assert s.threshold == 0.6 and s.model_url == "http://toxicity-filter.mentor:8088"
