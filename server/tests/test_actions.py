"""삭제 / 재시작 / 로그 / 이벤트 — 실제 k8s 대신 호출 기록용 가짜 함수를 registry에 넣어 검증."""

from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException

from app.k8s import actions, summarize
from app.k8s.registry import KindSpec
from app.k8s.watcher import WatchManager
from app.main import create_app


def _noop_list(namespace, **kw):
    raise AssertionError("호출되면 안 됨")


class Recorder:
    """k8s API 함수 대역. 호출 인자를 기록하고, raises가 있으면 예외를 던진다."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result
        self.raises = raises

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return self.result


class StaticManager(WatchManager):
    def start(self):
        pass

    def stop(self, join_timeout: float = 2.0):
        pass


def pod(name, labels=None, owner=None, uid=None, deleting=False, containers=("app",)):
    meta = {"name": name, "namespace": "mentor", "uid": uid or f"uid-{name}", "labels": labels or {}}
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    if deleting:
        meta["deletionTimestamp"] = "2026-11-21T00:00:00Z"
    return {"metadata": meta, "spec": {"containers": [{"name": c} for c in containers]},
            "status": {"phase": "Running"}}


def deploy(name, paused=False, labels=None):
    return {"metadata": {"name": name, "namespace": "mentor", "uid": f"uid-{name}", "labels": labels or {}},
            "spec": {"replicas": 2, "paused": paused, "selector": {"matchLabels": {"app": name}}},
            "status": {}}


def make(deps=None):
    f = {
        "pod_delete": Recorder(), "pod_log": Recorder(result="line1\nline2\n"),
        "dep_delete": Recorder(), "dep_patch": Recorder(), "svc_delete": Recorder(),
    }
    if deps:
        f.update(deps)
    registry = {
        "pods": KindSpec("pods", "Pod", "v1", _noop_list, summarize.pod_status, ("po",),
                         delete_func=f["pod_delete"], log_func=f["pod_log"]),
        "deployments": KindSpec("deployments", "Deployment", "apps/v1", _noop_list,
                                summarize.deployment_status, ("deploy",),
                                delete_func=f["dep_delete"], restart_func=f["dep_patch"]),
        "services": KindSpec("services", "Service", "v1", _noop_list, summarize.service_status, ("svc",),
                             delete_func=f["svc_delete"]),
        "events": KindSpec("events", "Event", "v1", _noop_list, summarize.event_status, ("ev",),
                           newest_first_by="last_seen"),
    }
    m = StaticManager(registry, "mentor")
    m.caches["pods"].replace({
        "web-abc": pod("web-abc", {"app": "web"}, owner=("ReplicaSet", "web-7d9")),
        "web-def": pod("web-def", {"app": "web"}, owner=("ReplicaSet", "web-7d9")),
        "bare": pod("bare", containers=("main", "sidecar")),
        "dying": pod("dying", deleting=True),
        "guard": pod("guard", {actions.PROTECTED_LABEL: "true"}),
    })
    m.caches["deployments"].replace({"web": deploy("web"), "frozen": deploy("frozen", paused=True)})
    m.caches["services"].replace({"web": {"metadata": {"name": "web", "uid": "uid-svc"}, "spec": {}}})
    m.caches["events"].replace({
        "e1": {"metadata": {"name": "e1"}, "type": "Normal", "reason": "Killing",
               "involvedObject": {"kind": "Pod", "name": "web-abc"}, "message": "Stopping",
               "lastTimestamp": "2026-11-21T01:00:00Z", "count": 1},
        "e2": {"metadata": {"name": "e2"}, "type": "Warning", "reason": "BackOff",
               "involvedObject": {"kind": "Pod", "name": "web-abc"}, "message": "Back-off",
               "lastTimestamp": "2026-11-21T02:00:00Z", "count": 3},
        "e3": {"metadata": {"name": "e3"}, "type": "Normal", "reason": "ScalingReplicaSet",
               "involvedObject": {"kind": "Deployment", "name": "web"}, "message": "Scaled up",
               "eventTime": "2026-11-21T00:30:00Z"},
    })
    for w in m.watchers.values():
        w.synced.set()
    return TestClient(create_app(lambda: m)), f


# ---------------------------------------------------------------------- 확인(confirm)
def test_delete_without_confirm_is_rejected_and_not_called():
    c, f = make()
    with c:
        r = c.delete("/api/v1/resources/pods/web-abc")
        assert r.status_code == 400
        assert "confirm=true" in r.json()["detail"]
        r = c.delete("/api/v1/resources/pods/web-abc", params={"confirm": "false"})
        assert r.status_code == 400
    assert f["pod_delete"].calls == []


def test_restart_without_confirm_is_rejected():
    c, f = make()
    with c:
        assert c.post("/api/v1/resources/deploy/web/restart").status_code == 400
    assert f["dep_patch"].calls == []


# ---------------------------------------------------------------------- 삭제
def test_delete_owned_pod_is_reconcile_with_background_and_uid_precondition():
    c, f = make()
    with c:
        r = c.delete("/api/v1/resources/po/web-abc", params={"confirm": "true"})
        assert r.status_code == 202, r.text
        body = r.json()
    assert body["effect"] == "reconcile"
    assert "셀프힐링" in body["message"]
    call = f["pod_delete"].calls[0]
    assert call["name"] == "web-abc" and call["namespace"] == "mentor"
    assert call["body"].propagation_policy == "Background"
    assert call["body"].preconditions.uid == "uid-web-abc"


def test_delete_unowned_is_drift_and_counts_pods():
    c, f = make()
    with c:
        body = c.get("/api/v1/resources/deployments/web").json()
        plan = body["actions"]["delete"]
        r = c.delete("/api/v1/resources/deployments/web", params={"confirm": "true"})
    assert plan["allowed"] and plan["effect"] == "drift"
    assert "매니페스트" in plan["message"]
    assert any("파드 2개" in d for d in plan["details"])
    assert r.status_code == 202 and r.json()["effect"] == "drift"
    assert f["dep_delete"].calls[0]["body"].propagation_policy == "Background"


def test_delete_guards():
    c, f = make()
    with c:
        protected = c.delete("/api/v1/resources/pods/guard", params={"confirm": "true"})
        dying = c.delete("/api/v1/resources/pods/dying", params={"confirm": "true"})
        missing = c.delete("/api/v1/resources/pods/nope", params={"confirm": "true"})
        events = c.delete("/api/v1/resources/events/e1", params={"confirm": "true"})
        stale = c.delete("/api/v1/resources/pods/bare", params={"confirm": "true", "uid": "old-uid"})
        unknown = c.delete("/api/v1/resources/nodes/x", params={"confirm": "true"})
    assert protected.status_code == 403
    assert dying.status_code == 409
    assert missing.status_code == 404
    assert events.status_code == 400
    assert stale.status_code == 409
    assert unknown.status_code == 404
    assert f["pod_delete"].calls == []


def test_k8s_errors_are_mapped():
    c, _ = make({"svc_delete": Recorder(raises=ApiException(status=403, reason="Forbidden"))})
    with c:
        r = c.delete("/api/v1/resources/svc/web", params={"confirm": "true"})
    assert r.status_code == 403
    assert "권한" in r.json()["detail"]

    c, _ = make({"svc_delete": Recorder(raises=ApiException(status=409, reason="Conflict"))})
    with c:
        assert c.delete("/api/v1/resources/svc/web", params={"confirm": "true"}).status_code == 409

    c, _ = make({"svc_delete": Recorder(raises=ApiException(status=500, reason="boom"))})
    with c:
        assert c.delete("/api/v1/resources/svc/web", params={"confirm": "true"}).status_code == 502


# ---------------------------------------------------------------------- 재시작
def test_restart_patches_only_template_annotation():
    c, f = make()
    with c:
        r = c.post("/api/v1/resources/deploy/web/restart", params={"confirm": "true"})
    assert r.status_code == 202, r.text
    assert r.json()["effect"] == "rollout"
    call = f["dep_patch"].calls[0]
    assert call["_content_type"] == "application/strategic-merge-patch+json"
    # spec에는 template 어노테이션 하나만 — replicas/이미지 등 선언 내용은 건드리지 않는다
    assert list(call["body"]) == ["spec"]
    assert list(call["body"]["spec"]) == ["template"]
    ann = call["body"]["spec"]["template"]["metadata"]["annotations"]
    assert list(ann) == [actions.RESTART_ANNOTATION]


def test_restart_guards():
    c, f = make()
    with c:
        paused = c.post("/api/v1/resources/deploy/frozen/restart", params={"confirm": "true"})
        pod_restart = c.post("/api/v1/resources/pods/web-abc/restart", params={"confirm": "true"})
        svc_restart = c.post("/api/v1/resources/svc/web/restart", params={"confirm": "true"})
    assert paused.status_code == 409
    assert pod_restart.status_code == 400 and "삭제하면" in pod_restart.json()["detail"]
    assert svc_restart.status_code == 400
    assert f["dep_patch"].calls == []


def test_detail_exposes_actions_owner_terminating():
    c, _ = make()
    with c:
        owned = c.get("/api/v1/resources/pods/web-abc").json()
        dying = c.get("/api/v1/resources/pods/dying").json()
        dep = c.get("/api/v1/resources/deployments/web").json()
        listed = c.get("/api/v1/resources/pods").json()["items"]
    assert owned["owner"] == {"kind": "ReplicaSet", "name": "web-7d9"}
    assert "restart" not in owned["actions"]
    assert dying["terminating"] is True and dying["actions"]["delete"]["allowed"] is False
    assert dep["actions"]["restart"]["allowed"] is True
    assert {i["name"]: i["terminating"] for i in listed}["dying"] is True


# ---------------------------------------------------------------------- 로그 / 이벤트
def test_pod_logs():
    c, f = make()
    with c:
        r = c.get("/api/v1/resources/pods/bare/logs", params={"tail_lines": 50})
        assert r.status_code == 200
        assert r.json()["text"] == "line1\nline2\n"
        assert r.json()["containers"] == ["main", "sidecar"]
        assert c.get("/api/v1/resources/pods/bare/logs", params={"container": "nope"}).status_code == 400
        assert c.get("/api/v1/resources/deploy/web/logs").status_code == 400
        assert c.get("/api/v1/resources/pods/bare/logs", params={"tail_lines": 99999}).status_code == 422
    call = f["pod_log"].calls[0]
    assert call["container"] == "main" and call["tail_lines"] == 50 and call["previous"] is False


def test_events_for_resource_newest_first():
    c, _ = make()
    with c:
        body = c.get("/api/v1/resources/pods/web-abc/events").json()
        dep = c.get("/api/v1/resources/deployments/web/events").json()
        all_ev = c.get("/api/v1/resources/events").json()
    assert [i["status"]["reason"] for i in body["items"]] == ["BackOff", "Killing"]
    assert [i["status"]["reason"] for i in dep["items"]] == ["ScalingReplicaSet"]
    assert dep["items"][0]["status"]["last_seen"] == "2026-11-21T00:30:00Z"
    assert [i["name"] for i in all_ev["items"]] == ["e2", "e1", "e3"]


def test_events_pagination():
    c, _ = make()
    with c:
        p1 = c.get("/api/v1/resources/events", params={"limit": 2}).json()
        p2 = c.get("/api/v1/resources/events", params={"limit": 2, "offset": 2}).json()
        mine = c.get("/api/v1/resources/pods/web-abc/events", params={"limit": 1, "offset": 1}).json()
        assert c.get("/api/v1/resources/events", params={"limit": 0}).status_code == 422
    assert p1["count"] == 3 and [i["name"] for i in p1["items"]] == ["e2", "e1"]
    assert p2["count"] == 3 and p2["offset"] == 2 and [i["name"] for i in p2["items"]] == ["e3"]
    assert mine["count"] == 2 and [i["status"]["reason"] for i in mine["items"]] == ["Killing"]
