"""API 라우트 테스트 — watcher 스레드는 띄우지 않고 캐시에 직접 데이터를 넣는다."""

from fastapi.testclient import TestClient

from app.k8s import summarize
from app.k8s.registry import KindSpec
from app.k8s.watcher import WatchManager
from app.main import create_app
from app.api.routes import match_labels, parse_label_selector


def _noop_list(namespace, **kw):
    raise AssertionError("호출되면 안 됨")


REGISTRY = {
    "pods": KindSpec("pods", "Pod", "v1", _noop_list, summarize.pod_status, ("pod", "po")),
    "services": KindSpec("services", "Service", "v1", _noop_list, summarize.service_status, ("svc",)),
}


class StaticManager(WatchManager):
    def start(self):
        pass

    def stop(self, join_timeout: float = 2.0):
        pass


def make_client(synced=True):
    m = StaticManager(REGISTRY, "mentor")
    m.caches["pods"].replace({
        "mlflow-1": {
            "metadata": {"name": "mlflow-1", "namespace": "mentor", "labels": {"app": "mlflow"}},
            "spec": {"containers": [{"name": "c"}], "nodeName": "node1"},
            "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 6,
                                                                  "state": {"running": {}}}]},
        },
        "filter-1": {
            "metadata": {"name": "filter-1", "namespace": "mentor", "labels": {"app": "toxicity-filter"}},
            "spec": {"containers": [{"name": "c"}]},
            "status": {"phase": "Pending", "containerStatuses": [{"ready": False, "restartCount": 0,
                                                                  "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]},
        },
    })
    m.caches["services"].replace({
        "mlflow": {"metadata": {"name": "mlflow", "namespace": "mentor"},
                   "spec": {"type": "NodePort", "clusterIP": "10.0.0.1",
                            "ports": [{"port": 5000, "nodePort": 30081, "protocol": "TCP"}]}},
    })
    if synced:
        for w in m.watchers.values():
            w.synced.set()
    return TestClient(create_app(lambda: m))


def test_readyz_503_until_synced():
    with make_client(synced=False) as c:
        r = c.get("/readyz")
        assert r.status_code == 503
        assert set(r.json()["pending"]) == {"pods", "services"}
    with make_client() as c:
        assert c.get("/readyz").status_code == 200


def test_list_pods_summary():
    with make_client() as c:
        body = c.get("/api/v1/resources/pods").json()
    assert body["namespace"] == "mentor"
    assert body["count"] == 2
    by_name = {i["name"]: i for i in body["items"]}
    assert by_name["mlflow-1"]["status"]["ready"] == "1/1"
    assert by_name["mlflow-1"]["status"]["restarts"] == 6
    assert by_name["filter-1"]["status"]["display_status"] == "CrashLoopBackOff"


def test_alias_and_label_selector():
    with make_client() as c:
        body = c.get("/api/v1/resources/po", params={"label_selector": "app=mlflow"}).json()
    assert [i["name"] for i in body["items"]] == ["mlflow-1"]


def test_service_ports():
    with make_client() as c:
        body = c.get("/api/v1/resources/svc").json()
    assert body["items"][0]["status"]["ports"] == ["5000:30081/TCP"]


def test_get_detail_and_404():
    with make_client() as c:
        r = c.get("/api/v1/resources/pods/mlflow-1")
        assert r.status_code == 200
        assert r.json()["manifest"]["spec"]["nodeName"] == "node1"
        assert c.get("/api/v1/resources/pods/nope").status_code == 404
        assert c.get("/api/v1/resources/nodes").status_code == 404


def test_overview_and_kinds():
    with make_client() as c:
        assert c.get("/api/v1/resources").json()["counts"] == {"pods": 2, "services": 1}
        kinds = c.get("/api/v1/kinds").json()["kinds"]
    assert {k["kind"] for k in kinds} == {"pods", "services"}


def test_pvc_summary():
    obj = {
        "metadata": {"name": "db-data", "namespace": "mentor"},
        "spec": {"volumeName": "pv-123", "storageClassName": "local-path",
                 "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "5Gi"}}},
        "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}, "accessModes": ["ReadWriteOnce"]},
    }
    s = summarize.pvc_status(obj)
    assert s["phase"] == "Bound"
    assert s["volume"] == "pv-123"
    assert s["requested"] == "5Gi" and s["capacity"] == "5Gi"
    assert s["access_modes"] == ["ReadWriteOnce"]
    assert s["storage_class"] == "local-path"

    pending = summarize.pvc_status({"metadata": {"name": "x"},
                                    "spec": {"resources": {"requests": {"storage": "1Gi"}}},
                                    "status": {"phase": "Pending"}})
    assert pending["volume"] is None and pending["capacity"] is None


def test_cors_allows_front_origin_only():
    with make_client() as c:
        ok = c.get("/api/v1/resources", headers={"Origin": "http://localhost:5500"})
        assert ok.headers.get("access-control-allow-origin") == "http://localhost:5500"
        other = c.get("/api/v1/resources", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in other.headers
        # 삭제(DELETE)·재시작(POST)은 프론트 origin에서만 preflight 통과
        pre = c.options("/api/v1/resources/pods/x", headers={
            "Origin": "http://localhost:5500", "Access-Control-Request-Method": "DELETE"})
        assert pre.status_code == 200
        bad = c.options("/api/v1/resources/pods/x", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": "DELETE"})
        assert bad.status_code == 400
        # 매니페스트 저장(PUT)은 허용, 그 외 쓰기 메서드(PATCH)는 거부
        put = c.options("/api/v1/manifests/file", headers={
            "Origin": "http://localhost:5500", "Access-Control-Request-Method": "PUT"})
        assert put.status_code == 200
        patch = c.options("/api/v1/resources/pods/x", headers={
            "Origin": "http://localhost:5500", "Access-Control-Request-Method": "PATCH"})
        assert patch.status_code == 400


def test_label_selector_parser():
    reqs = parse_label_selector("app=a, tier!=db, env, !legacy")
    assert match_labels({"app": "a", "env": "x"}, reqs)
    assert not match_labels({"app": "a", "env": "x", "tier": "db"}, reqs)
    assert not match_labels({"app": "a"}, reqs)
    assert not match_labels({"app": "a", "env": "x", "legacy": "1"}, reqs)
