"""피드백: 분류 시 자동 저장 · 표 조회(필터·페이지) · 정답 입력 · DB 장애 처리.

SQL 은 실제 MariaDB 에서 트랜잭션+ROLLBACK 으로 검증했고(work_5_plan.md), 여기서는 같은 동작을 하는
메모리 저장소로 API 흐름을 검증한다.
"""

import uuid
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.comments.service import CommentService, CommentSettings
from app.feedback.repo import FILTERS, DbSettings, FeedbackError, parse_id, to_item
from app.k8s import summarize
from app.k8s.registry import KindSpec
from app.k8s.watcher import WatchManager
from app.main import create_app

MODEL = {"name": "toxicity-filter", "version": "seed-633bdbb4ded6"}
PROBAS = {"안녕하세요": 0.68575, "말이 너무 심하네요": 0.840195, "좋은 글 감사합니다": 0.2}


class MemoryRepo:
    """FeedbackRepo 와 같은 인터페이스·규칙 (UPDATE ... WHERE true_label IS NULL)."""

    def __init__(self, fail: FeedbackError | None = None):
        self.rows: list[dict] = []
        self.fail = fail
        self.clock = 0

    def _check(self):
        if self.fail:
            raise self.fail

    def insert(self, **kw):
        self._check()
        self.clock += 1
        row = {"id": str(uuid.uuid1()), "true_label": None, "labeled_at": None,
               "created_at": datetime(2026, 11, 21, 14, 0, self.clock), **kw}
        self.rows.append(row)
        return {"id": row["id"], "created_at": row["created_at"].isoformat() + "Z"}

    def set_label(self, item_id, true_label):
        self._check()
        item_id = parse_id(item_id)
        row = next((r for r in self.rows if r["id"] == item_id), None)
        if row is None:
            raise FeedbackError(404, "해당 피드백을 찾을 수 없습니다.")
        if row["true_label"] is not None:
            raise FeedbackError(409, "이미 정답이 입력된 항목입니다.")
        row.update(true_label=true_label, labeled_at=datetime(2026, 11, 21, 15, 0, 0))
        return to_item(row)

    def list(self, filter_key, limit, offset):
        self._check()
        preds = {
            "all": lambda r: True,
            "unlabeled": lambda r: r["true_label"] is None,
            "labeled": lambda r: r["true_label"] is not None,
            "wrong": lambda r: r["true_label"] is not None and r["true_label"] != r["pred_label"],
        }
        if filter_key not in preds:
            raise FeedbackError(400, "지원하지 않는 필터")
        rows = sorted((r for r in self.rows if preds[filter_key](r)), key=lambda r: r["created_at"], reverse=True)
        return {
            "filter": filter_key, "count": len(rows), "offset": offset, "limit": limit,
            "items": [to_item(r) for r in rows[offset:offset + limit]],
            "summary": {k: sum(1 for r in self.rows if preds[k](r)) for k in ("unlabeled", "labeled", "wrong")}
            | {"total": len(self.rows)},
        }


def model_transport():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/predict":
            import json
            text = json.loads(req.content)["text"]
            return httpx.Response(200, json={"toxic_proba": PROBAS.get(text, 0.1), "model": MODEL})
        return httpx.Response(200, json={"ok": True})
    return httpx.MockTransport(handler)


class StaticManager(WatchManager):
    def start(self):
        pass

    def stop(self, join_timeout: float = 2.0):
        pass


def _noop_list(namespace, **kw):
    raise AssertionError


def client_for(repo):
    svc = CommentService(CommentSettings("http://model.test", 0.5, 3.0), transport=model_transport())
    m = StaticManager({"pods": KindSpec("pods", "Pod", "v1", _noop_list, summarize.pod_status)}, "mentor")
    return TestClient(create_app(lambda: m, lambda _m: None, lambda: svc, lambda: repo))


def predict(c, text):
    return c.post("/api/v1/comments/predict", json={"text": text})


# ---------------------------------------------------------------------- 저장
def test_predict_saves_model_output():
    repo = MemoryRepo()
    with client_for(repo) as c:
        body = predict(c, "안녕하세요").json()
    assert body["saved"] is True and body["save_error"] is None
    assert body["id"] == repo.rows[0]["id"] and body["created_at"].endswith("Z")
    row = repo.rows[0]
    assert row["text"] == "안녕하세요" and row["pred_label"] == 1
    assert row["toxic_proba"] == pytest.approx(0.68575) and row["threshold"] == 0.5
    assert row["model_name"] == "toxicity-filter" and row["model_version"] == "seed-633bdbb4ded6"
    assert row["true_label"] is None                         # 정답은 사람이 나중에


def test_predict_still_returns_result_when_db_fails():
    repo = MemoryRepo(fail=FeedbackError(503, "DB 에 연결할 수 없습니다"))
    with client_for(repo) as c:
        r = predict(c, "말이 너무 심하네요")
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == 1 and body["saved"] is False and body["id"] is None
    assert "DB" in body["save_error"]


def test_predict_without_db_configured():
    with client_for(None) as c:
        body = predict(c, "안녕하세요").json()
        assert c.get("/api/v1/feedback").status_code == 503
    assert body["saved"] is False and "설정" in body["save_error"]


def test_validation_error_does_not_save():
    repo = MemoryRepo()
    with client_for(repo) as c:
        assert predict(c, "  ").status_code == 400
    assert repo.rows == []


# ---------------------------------------------------------------------- 정답 입력
def test_label_once_then_conflict():
    repo = MemoryRepo()
    with client_for(repo) as c:
        rid = predict(c, "안녕하세요").json()["id"]
        r = c.post(f"/api/v1/feedback/{rid}/label", json={"true_label": 0})
        again = c.post(f"/api/v1/feedback/{rid}/label", json={"true_label": 1})
        missing = c.post(f"/api/v1/feedback/{uuid.uuid4()}/label", json={"true_label": 0})
        bad_id = c.post("/api/v1/feedback/not-a-uuid/label", json={"true_label": 0})
        bad_label = c.post(f"/api/v1/feedback/{rid}/label", json={"true_label": 2})
    assert r.status_code == 200
    item = r.json()
    assert item["true_label"] == 0 and item["true_label_name"] == "정상" and item["correct"] is False
    assert again.status_code == 409 and repo.rows[0]["true_label"] == 0   # 첫 정답 유지
    assert missing.status_code == 404
    assert bad_id.status_code == 400
    assert bad_label.status_code == 422


# ---------------------------------------------------------------------- 조회
def test_list_filters_pagination_and_summary():
    repo = MemoryRepo()
    with client_for(repo) as c:
        ids = {t: predict(c, t).json()["id"] for t in PROBAS}           # 3건
        for i in range(12):
            predict(c, f"추가 댓글 {i}")                                 # +12 = 15건
        c.post(f"/api/v1/feedback/{ids['안녕하세요']}/label", json={"true_label": 0})        # 틀림
        c.post(f"/api/v1/feedback/{ids['말이 너무 심하네요']}/label", json={"true_label": 1})  # 맞음

        p1 = c.get("/api/v1/feedback", params={"limit": 10}).json()
        p2 = c.get("/api/v1/feedback", params={"limit": 10, "offset": 10}).json()
        wrong = c.get("/api/v1/feedback", params={"filter": "wrong"}).json()
        labeled = c.get("/api/v1/feedback", params={"filter": "labeled"}).json()
        unlabeled = c.get("/api/v1/feedback", params={"filter": "unlabeled"}).json()
        bad = c.get("/api/v1/feedback", params={"filter": "drop table"})
        too_many = c.get("/api/v1/feedback", params={"limit": 1000})

    assert p1["count"] == 15 and len(p1["items"]) == 10 and len(p2["items"]) == 5
    assert p1["items"][0]["text"] == "추가 댓글 11"                     # 최신순
    assert p1["summary"] == {"total": 15, "unlabeled": 13, "labeled": 2, "wrong": 1}
    assert [i["text"] for i in wrong["items"]] == ["안녕하세요"]
    assert labeled["count"] == 2 and unlabeled["count"] == 13
    assert bad.status_code == 400 and too_many.status_code == 422


# ---------------------------------------------------------------------- 단위
def test_to_item_and_settings(monkeypatch):
    from decimal import Decimal
    item = to_item({"id": b"89af76e0-e5b9-11f1-b584-1a2e4b5963d2", "text": "x", "pred_label": 1,
                    "toxic_proba": 0.7, "threshold": Decimal("0.500"), "model_name": "m", "model_version": "v",
                    "true_label": None, "labeled_at": None,
                    "created_at": datetime(2026, 11, 21, 14, 40, 46, 688000)})
    assert item["id"] == "89af76e0-e5b9-11f1-b584-1a2e4b5963d2" and item["threshold"] == 0.5
    assert item["created_at"] == "2026-11-21T14:40:46.688Z" and item["correct"] is None
    assert set(FILTERS) == {"all", "unlabeled", "labeled", "wrong"}

    monkeypatch.delenv("DB_PASSWORD", raising=False)
    assert DbSettings.from_env() is None                                 # 비밀번호 없으면 꺼짐
    monkeypatch.setenv("DB_PASSWORD", "s3cr3t-Pw")
    s = DbSettings.from_env()
    assert (s.host, s.port, s.name, s.user) == ("192.168.2.30", 31306, "toxicity", "mentor_app")
    assert "s3cr3t-Pw" not in s.describe()                                       # 로그에 비밀번호 안 남김
