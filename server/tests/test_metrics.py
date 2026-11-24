"""/metrics (k8sapimd/monitoring.md 2장) · /api/v1/monitoring (7장)."""

from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app.comments.service import CommentService, CommentSettings
from app.k8s import summarize
from app.k8s.registry import KindSpec
from app.main import create_app
from app.metrics import action_result, job_state
from app.models.mlflow_rest import MlflowREST, ModelError
from tests.test_comments import StaticManager, _noop_list, model_server

KINDS = {
    "pods": KindSpec("pods", "Pod", "v1", _noop_list, summarize.pod_status),
    "deployments": KindSpec("deployments", "Deployment", "apps/v1", _noop_list, summarize.deployment_status),
    "jobs": KindSpec("jobs", "Job", "batch/v1", _noop_list, summarize.job_status),
}


def pod(name, restarts=0, ready=True):
    return {"metadata": {"name": name},
            "status": {"phase": "Running",
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "containerStatuses": [{"name": "app", "restartCount": restarts}]}}


def job(name, jtype, state, start=None, end=None):
    status = {"startTime": start}
    if state == "succeeded":
        status.update(succeeded=1, completionTime=end, conditions=[{"type": "Complete", "status": "True"}])
    elif state == "failed":
        status.update(failed=1, conditions=[{"type": "Failed", "status": "True"}])
    else:
        status.update(active=1)
    return {"metadata": {"name": name, "labels": {"platform.mentor/job-type": jtype}}, "status": status}


def make_client(mode="ok", feedback=None, synced=True):
    m = StaticManager(KINDS, "mentor")
    m.caches["pods"].replace({
        "toxicity-filter-a": pod("toxicity-filter-a", restarts=2),
        "mentor-api-b": pod("mentor-api-b", ready=False),
    })
    m.caches["deployments"].replace({"toxicity-filter": {
        "metadata": {"name": "toxicity-filter"}, "spec": {"replicas": 1},
        "status": {"readyReplicas": 0, "updatedReplicas": 1}}})
    m.caches["jobs"].replace({
        "train-1": job("train-1", "train", "succeeded", "2026-11-24T10:00:00Z", "2026-11-24T10:01:30Z"),
        "train-2": job("train-2", "train", "succeeded", "2026-11-24T11:00:00Z", "2026-11-24T11:00:40Z"),
        "dataset-1": job("dataset-1", "dataset", "failed", "2026-11-24T09:00:00Z"),
        "train-3": job("train-3", "train", "active", "2026-11-24T12:00:00Z"),
    })
    for key, w in m.watchers.items():
        if synced:
            w.synced.set()
        w.last_sync_at = "2026-11-24T00:00:00+00:00"
    m.watchers["pods"].last_event_at = "2026-11-24T01:00:00+00:00"
    m.watchers["pods"].relist_count = 3
    m.watchers["pods"].error_count = 1

    transport, _ = model_server(mode)
    svc = CommentService(CommentSettings("http://model.test", 0.5, 3.0), transport=transport)
    return TestClient(create_app(lambda: m, lambda _m: None, lambda: svc, lambda: feedback,
                                 lambda _m: None, lambda *a: None))


def scrape(c) -> dict[str, float]:
    r = c.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    out = {}
    for fam in text_string_to_metric_families(r.text):
        for s in fam.samples:
            labels = ",".join(f"{k}={v}" for k, v in sorted(s.labels.items()))
            out[f"{s.name}{{{labels}}}"] = s.value
    return out


# ---------------------------------------------------------------------- 상태 (Watch 캐시)
def test_state_metrics_from_watch_cache():
    with make_client() as c:
        m = scrape(c)
    assert m["mentor_platform_threshold{}"] == 0.5
    assert m["mentor_platform_feedback_db_configured{}"] == 0
    assert m["mentor_platform_watch_synced{kind=pods}"] == 1
    assert m["mentor_platform_watch_relists_total{kind=pods}"] == 3
    assert m["mentor_platform_watch_errors_total{kind=pods}"] == 1
    assert m["mentor_platform_watch_last_event_timestamp_seconds{kind=pods}"] == datetime(2026, 11, 24, 1, tzinfo=timezone.utc).timestamp()
    # 이벤트가 없으면 마지막 LIST 시각
    assert m["mentor_platform_watch_last_event_timestamp_seconds{kind=jobs}"] == datetime(2026, 11, 24, tzinfo=timezone.utc).timestamp()

    assert m["mentor_platform_pod_restarts{container=app,pod=toxicity-filter-a}"] == 2
    assert m["mentor_platform_pod_ready{pod=toxicity-filter-a}"] == 1
    assert m["mentor_platform_pod_ready{pod=mentor-api-b}"] == 0
    assert m["mentor_platform_workload_replicas{kind=Deployment,name=toxicity-filter,state=desired}"] == 1
    assert m["mentor_platform_workload_replicas{kind=Deployment,name=toxicity-filter,state=ready}"] == 0

    assert m["mentor_platform_jobs{job_type=train,status=succeeded}"] == 2
    assert m["mentor_platform_jobs{job_type=train,status=active}"] == 1
    assert m["mentor_platform_jobs{job_type=dataset,status=failed}"] == 1
    assert m["mentor_platform_jobs{job_type=dataset,status=succeeded}"] == 0     # 0 도 노출 (그래프가 끊기지 않게)
    # 가장 최근에 끝난 학습 = train-2 (40초)
    assert m["mentor_platform_job_last_duration_seconds{job_type=train}"] == 40
    assert "mentor_platform_job_last_duration_seconds{job_type=dataset}" not in m


def test_watch_not_synced():
    with make_client(synced=False) as c:
        assert scrape(c)["mentor_platform_watch_synced{kind=pods}"] == 0


def test_job_state():
    assert job_state(job("a", "train", "succeeded", "x", "y")) == "succeeded"
    assert job_state(job("a", "train", "failed")) == "failed"
    assert job_state(job("a", "train", "active")) == "active"
    assert job_state({}) == "active"


# ---------------------------------------------------------------------- 이벤트 카운터
def test_classify_and_feedback_disabled():
    with make_client() as c:
        before = scrape(c)
        c.post("/api/v1/comments/predict", json={"text": "말이 너무 심하네요"})        # 0.84 → 1
        c.post("/api/v1/comments/predict", json={"text": "이 글 덕분에 이해가 잘 됐어요"})  # 0.35 → 0
        after = scrape(c)

    def delta(k):
        return after.get(k, 0) - before.get(k, 0)

    assert delta("mentor_platform_classify_total{model_version=seed-633bdbb4ded6,pred_label=1}") == 1
    assert delta("mentor_platform_classify_total{model_version=seed-633bdbb4ded6,pred_label=0}") == 1
    assert delta("mentor_platform_feedback_save_total{result=disabled}") == 2


class FakeRepo:
    def __init__(self, fail=False):
        self.fail = fail

    def insert(self, **kw):
        from app.feedback.repo import FeedbackError
        if self.fail:
            raise FeedbackError(503, "DB 연결 실패")
        return {"id": "x", "created_at": "2026-11-24T00:00:00Z"}


@pytest.mark.parametrize("fail,result", [(False, "ok"), (True, "error")])
def test_feedback_save_result(fail, result):
    with make_client(feedback=FakeRepo(fail)) as c:
        before = scrape(c)
        c.post("/api/v1/comments/predict", json={"text": "안녕하세요"})
        after = scrape(c)
    key = f"mentor_platform_feedback_save_total{{result={result}}}"
    assert after[key] - before.get(key, 0) == 1
    assert after["mentor_platform_feedback_db_configured{}"] == 1


@pytest.mark.parametrize("mode,reason", [("down", "connect"), ("timeout", "timeout"),
                                         ("error", "http_5xx"), ("garbage", "invalid")])
def test_model_call_failures(mode, reason):
    with make_client(mode) as c:
        before = scrape(c)
        c.post("/api/v1/comments/predict", json={"text": "안녕하세요"})
        after = scrape(c)
    key = f"mentor_platform_model_call_failures_total{{reason={reason}}}"
    assert after[key] - before.get(key, 0) == 1


def test_actions_counted_by_route_and_result():
    with make_client() as c:
        before = scrape(c)
        c.delete("/api/v1/resources/pods/toxicity-filter-a")              # confirm 없음 → 400 (rejected)
        c.post("/api/v1/models/train", json={"dataset": "x"})            # 기능 꺼짐 → 503 (error)
        c.get("/api/v1/resources/pods")                                   # 조회는 세지 않음
        after = scrape(c)

    def delta(k):
        return after.get(k, 0) - before.get(k, 0)

    assert delta("mentor_platform_actions_total{action=delete,result=rejected}") == 1
    assert delta("mentor_platform_actions_total{action=train_start,result=error}") == 1
    assert not [k for k in after if k.startswith("mentor_platform_actions_total") and "action=list" in k]


def test_action_result():
    assert [action_result(s) for s in (200, 202, 400, 409, 500, 503)] == \
        ["ok", "ok", "rejected", "rejected", "error", "error"]


def test_mlflow_failures_counted():
    def handler(req):
        if req.url.path.endswith("alias"):
            return httpx.Response(400, json={"error_code": "INVALID_PARAMETER_VALUE", "message": "alias not found."})
        return httpx.Response(500, text="boom")

    rest = MlflowREST("http://mlflow.test", transport=httpx.MockTransport(handler))
    with make_client() as c:
        before = scrape(c)
        assert rest.get_alias("toxicity-filter", "champion") is None       # 없는 alias 는 실패가 아님
        with pytest.raises(ModelError):
            rest.versions("toxicity-filter")
        after = scrape(c)
    rest.close()
    key = "mentor_platform_mlflow_failures_total{op=model-versions/search}"
    assert after[key] - before.get(key, 0) == 1
    assert after.get("mentor_platform_mlflow_failures_total{op=registered-models/alias}", 0) == \
        before.get("mentor_platform_mlflow_failures_total{op=registered-models/alias}", 0)


def test_metrics_no_comment_text_in_labels():
    with make_client() as c:
        c.post("/api/v1/comments/predict", json={"text": "말이 너무 심하네요"})
        assert "말이 너무 심하네요" not in c.get("/metrics").text


def test_metrics_not_in_openapi():
    with make_client() as c:
        assert "/metrics" not in c.get("/openapi.json").json()["paths"]


# ---------------------------------------------------------------------- /api/v1/monitoring
def test_monitoring_config_empty(monkeypatch):
    monkeypatch.delenv("GRAFANA_MODEL_URL", raising=False)
    monkeypatch.delenv("GRAFANA_PLATFORM_URL", raising=False)
    with make_client() as c:
        body = c.get("/api/v1/monitoring").json()
    assert body["configured"] is False
    assert [(d["key"], d["url"], d["error"]) for d in body["dashboards"]] == \
        [("model", None, None), ("platform", None, None)]


def test_monitoring_config_urls(monkeypatch):
    url = "https://grafanas.studylink.click/public-dashboards/abc"
    monkeypatch.setenv("GRAFANA_MODEL_URL", url)
    monkeypatch.setenv("GRAFANA_PLATFORM_URL", "javascript:alert(1)")     # iframe src 로 쓰이므로 거부
    with make_client() as c:
        body = c.get("/api/v1/monitoring").json()
    model, platform = body["dashboards"]
    assert model["url"] == url and model["title"] == "모델"
    assert platform["url"] is None and "http" in platform["error"]
    assert body["configured"] is False
