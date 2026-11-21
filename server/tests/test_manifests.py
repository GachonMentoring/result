"""매니페스트 관리: 파일(경로 안전·충돌·검증) / 판정·apply·삭제 (가짜 클러스터) / API."""

import copy

import pytest
from fastapi.testclient import TestClient

from app.k8s import summarize
from app.k8s.registry import KindSpec
from app.k8s.watcher import WatchManager
from app.main import create_app
from app.manifests.cluster import (
    FIELD_MANAGER, ApplyConflict, ClusterError, ManifestService, diff, normalize,
)
from app.manifests.files import FileError, ManifestStore

DEPLOY = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 2
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      containers: [{name: web, image: "nginx:1.27"}]
"""
SVC = """apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  ports: [{port: 80}]
"""


# ---------------------------------------------------------------------- 가짜 클러스터
def _merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in patch.items():
        out[k] = _merge(out.get(k, {}), v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


class FakeCluster:
    """SSA 흉내: 기존 상태 위에 덮어쓰기 + 기본값 채우기 + 다른 관리자 소유 필드 충돌."""

    CLUSTER_SCOPED = {"Namespace", "ClusterRole", "PersistentVolume"}
    KNOWN = {"Deployment", "Service", "ConfigMap", "Secret", "Namespace", "ClusterRole", "PersistentVolume",
             "StatefulSet", "Job", "CronJob", "PersistentVolumeClaim", "ServiceAccount", "Ingress"}

    def __init__(self):
        self.objs = {}            # (kind, name) → obj
        self.owned_by_other = {}  # (kind, name) → {"spec.replicas": "kubectl"} 다른 관리자가 가진 필드
        self.applied = []         # 실제 적용 기록 (dry-run 제외)
        self.rv = 100

    def namespaced(self, api_version, kind):
        if kind not in self.KNOWN:
            raise ClusterError(400, f"클러스터가 모르는 리소스입니다: {api_version} {kind}")
        return kind not in self.CLUSTER_SCOPED

    def get(self, api_version, kind, name, namespace):
        o = self.objs.get((kind, name))
        return copy.deepcopy(o) if o else None

    def _defaults(self, obj):
        if obj["kind"] == "Deployment":
            obj["spec"].setdefault("progressDeadlineSeconds", 600)
            obj["spec"].setdefault("revisionHistoryLimit", 10)
        if obj["kind"] == "Service":
            obj["spec"].setdefault("type", "ClusterIP")
        return obj

    def apply(self, body, namespace, *, dry_run, force):
        key = (body["kind"], body["metadata"]["name"])
        live = self.objs.get(key)
        if live and not force:
            conflicts = []
            for path, manager in self.owned_by_other.get(key, {}).items():
                a, b = body, live
                for part in path.split("."):
                    a, b = (a or {}).get(part), (b or {}).get(part)
                if a is not None and a != b:
                    conflicts.append({"field": "." + path, "message": f'conflict with "{manager}"'})
            if conflicts:
                raise ApplyConflict(conflicts)
        obj = self._defaults(_merge(live or {}, body))
        self.rv += 1
        obj["metadata"].update(resourceVersion=str(self.rv), uid=f"uid-{key[1]}",
                               managedFields=[{"manager": FIELD_MANAGER, "operation": "Apply"}])
        obj["status"] = {"observed": self.rv}
        if not dry_run:
            self.objs[key] = obj
            self.applied.append(key)
            self.owned_by_other.pop(key, None) if force else None
        return copy.deepcopy(obj)

    def delete(self, api_version, kind, name, namespace):
        return self.objs.pop((kind, name), None) is not None

    def list(self, api_version, kind, namespace):
        return [copy.deepcopy(o) for (k, _), o in self.objs.items() if k == kind]


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "manifests"
    d.mkdir()
    return d


@pytest.fixture
def svc(root):
    return ManifestService(ManifestStore(root), FakeCluster(), "mentor")


def write(root, rel, content):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------- 파일
def test_list_recursive_yaml_and_yml_only(root):
    write(root, "a.yaml", SVC)
    write(root, "sub/b.yml", SVC)
    write(root, "sub/deep/c.yaml", SVC)
    write(root, "notes.txt", "x")
    write(root, ".hidden.yaml", SVC)
    write(root, ".git/x.yaml", SVC)
    paths = [f.path for f in ManifestStore(root).list()]
    assert paths == ["a.yaml", "sub/b.yml", "sub/deep/c.yaml"]


@pytest.mark.parametrize("bad", [
    "../x.yaml", "/etc/x.yaml", "a/../../x.yaml", "C:/x.yaml", "..\\x.yaml",
    ".hidden/x.yaml", "x.txt", "", " a.yaml", "a//b.yaml",
])
def test_path_outside_or_invalid_is_rejected(root, bad):
    with pytest.raises(FileError) as e:
        ManifestStore(root).resolve(bad, must_exist=False)
    assert e.value.status == 400


def test_save_detects_external_change_by_etag(root):
    write(root, "a.yaml", SVC)
    store = ManifestStore(root)
    loaded = store.read("a.yaml")
    write(root, "a.yaml", SVC.replace("80", "81"))           # 외부에서 수정
    with pytest.raises(FileError) as e:
        store.save("a.yaml", SVC.replace("80", "82"), loaded["etag"])
    assert e.value.status == 409
    assert "81" in (root / "a.yaml").read_text()              # 외부 수정 보존
    saved = store.save("a.yaml", SVC.replace("80", "82"), loaded["etag"], force=True)
    assert "82" in (root / "a.yaml").read_text()
    assert saved["etag"] == store.read("a.yaml")["etag"]
    assert [p.name for p in root.iterdir()] == ["a.yaml"]     # 임시 파일이 남지 않음


def test_save_rejects_invalid_yaml_with_line(root):
    write(root, "a.yaml", SVC)
    store = ManifestStore(root)
    etag = store.read("a.yaml")["etag"]
    with pytest.raises(FileError) as e:
        store.save("a.yaml", "apiVersion: v1\nkind: [unclosed\n", etag)
    assert e.value.status == 400 and e.value.line is not None
    with pytest.raises(FileError) as e:
        store.save("a.yaml", "apiVersion: v1\nkind: ConfigMap\nmetadata: {}\n", etag)
    assert "metadata.name" in e.value.message
    assert (root / "a.yaml").read_text() == SVC               # 실패 시 파일 그대로


def test_create_and_delete(root):
    store = ManifestStore(root)
    store.create("new/dir/c.yaml", SVC)
    assert (root / "new/dir/c.yaml").exists()
    with pytest.raises(FileError) as e:
        store.create("new/dir/c.yaml", SVC)
    assert e.value.status == 409
    store.delete("new/dir/c.yaml")
    assert not (root / "new/dir/c.yaml").exists()
    with pytest.raises(FileError) as e:
        store.delete("new/dir/c.yaml")
    assert e.value.status == 404


# ---------------------------------------------------------------------- 비교
def test_normalize_ignores_server_fields_and_restart_annotation():
    live = {"metadata": {"name": "w", "resourceVersion": "9", "uid": "u", "generation": 3,
                         "managedFields": [{}], "creationTimestamp": "t"},
            "spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "x"}}}},
            "status": {"ready": 1}}
    assert normalize(live) == {"metadata": {"name": "w"}, "spec": {"template": {"metadata": {}}}}


def test_diff_paths():
    d = diff({"spec": {"replicas": 3, "c": [{"image": "a"}]}}, {"spec": {"replicas": 1, "c": [{"image": "b"}, {}]}})
    assert d == [
        {"path": "spec.c[0].image", "file": "a", "cluster": "b"},
        {"path": "spec.c[1]", "file": None, "cluster": {}},
        {"path": "spec.replicas", "file": 3, "cluster": 1},
    ]


# ---------------------------------------------------------------------- 판정
def test_judge_missing_then_synced_with_defaults(svc, root):
    write(root, "web.yaml", DEPLOY + "---\n" + SVC)
    j = svc.judge_file("web.yaml")
    assert j["state"] == "missing" and [r["state"] for r in j["resources"]] == ["missing", "missing"]
    svc.apply_file("web.yaml", force=False)
    j = svc.judge_file("web.yaml")
    # 클러스터가 채운 기본값(progressDeadlineSeconds 등)은 불일치로 잡히지 않는다
    assert j["state"] == "synced", j


def test_judge_drift_shows_diff_and_conflict(svc, root):
    write(root, "web.yaml", DEPLOY)
    svc.apply_file("web.yaml", force=False)
    # 누군가 kubectl scale 로 replicas 를 바꿈 → 그 필드는 kubectl 소유
    svc.api.objs[("Deployment", "web")]["spec"]["replicas"] = 5
    svc.api.owned_by_other[("Deployment", "web")] = {"spec.replicas": "kubectl"}
    j = svc.judge_file("web.yaml")
    r = j["resources"][0]
    assert j["state"] == "drift"
    assert r["diff"] == [{"path": "spec.replicas", "file": 2, "cluster": 5}]
    assert r["conflicts"][0]["field"] == ".spec.replicas"


def test_judge_scope_errors(svc, root):
    write(root, "bad.yaml", "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x, namespace: other}\n"
                            "---\napiVersion: v1\nkind: Namespace\nmetadata: {name: y}\n"
                            "---\napiVersion: x/v1\nkind: Unknown\nmetadata: {name: z}\n")
    j = svc.judge_file("bad.yaml")
    assert j["state"] == "error"
    errors = [r["error"] for r in j["resources"]]
    assert "other" in errors[0] and "클러스터 범위" in errors[1] and "모르는" in errors[2]


def test_judge_yaml_error_and_empty(svc, root):
    write(root, "broken.yaml", "kind: [\n")
    write(root, "empty.yaml", "# 아직 비어 있음\n")
    assert svc.judge_file("broken.yaml")["state"] == "error"
    assert svc.judge_file("broken.yaml")["error_line"] is not None
    assert svc.judge_file("empty.yaml")["state"] == "empty"


# ---------------------------------------------------------------------- apply
def test_apply_reports_actions_and_sets_namespace(svc, root):
    write(root, "web.yaml", DEPLOY + "---\n" + SVC)
    r = svc.apply_file("web.yaml", force=False)
    assert [(x["kind"], x["action"]) for x in r["resources"]] == [("Deployment", "created"), ("Service", "created")]
    assert svc.api.objs[("Deployment", "web")]["metadata"]["namespace"] == "mentor"
    r = svc.apply_file("web.yaml", force=False)
    assert [x["action"] for x in r["resources"]] == ["unchanged", "unchanged"]
    write(root, "web.yaml", DEPLOY.replace("replicas: 2", "replicas: 3"))
    r = svc.apply_file("web.yaml", force=False)
    assert r["resources"][0]["action"] == "updated"
    assert svc.api.objs[("Deployment", "web")]["spec"]["replicas"] == 3


def test_apply_conflict_requires_force(svc, root):
    write(root, "web.yaml", DEPLOY)
    svc.apply_file("web.yaml", force=False)
    svc.api.objs[("Deployment", "web")]["spec"]["replicas"] = 5
    svc.api.owned_by_other[("Deployment", "web")] = {"spec.replicas": "kubectl"}
    with pytest.raises(ClusterError) as e:
        svc.apply_file("web.yaml", force=False)
    assert e.value.status == 409 and e.value.conflicts[0]["resource"] == "Deployment/web"
    assert svc.api.objs[("Deployment", "web")]["spec"]["replicas"] == 5     # 아무것도 안 바뀜
    svc.apply_file("web.yaml", force=True)
    assert svc.api.objs[("Deployment", "web")]["spec"]["replicas"] == 2     # 선언대로 복구


def test_apply_is_all_or_nothing_on_validation(svc, root):
    write(root, "mixed.yaml", SVC + "---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: c, namespace: other}\n")
    with pytest.raises(ClusterError) as e:
        svc.apply_file("mixed.yaml", force=False)
    assert e.value.status == 400
    assert svc.api.applied == []                                           # Service 도 적용 안 됨


# ---------------------------------------------------------------------- 삭제 / 선언 없음
def test_delete_file_with_and_without_resources_and_orphans(svc, root):
    write(root, "web.yaml", DEPLOY)
    write(root, "svc.yaml", SVC)
    svc.apply_file("web.yaml", force=False)
    svc.apply_file("svc.yaml", force=False)

    r = svc.delete_file("web.yaml", with_resources=True)
    assert r["resources"][0]["deleted"] is True
    assert ("Deployment", "web") not in svc.api.objs and not (root / "web.yaml").exists()

    svc.delete_file("svc.yaml", with_resources=False)                      # 파일만 → 선언 없음
    assert ("Service", "web") in svc.api.objs
    assert svc.judge_all()["orphans"] == [{"apiVersion": "v1", "kind": "Service", "name": "web"}]
    assert svc.delete_orphan("v1", "Service", "web") is True
    assert svc.judge_all()["orphans"] == []


def test_delete_orphan_refuses_declared(svc, root):
    write(root, "svc.yaml", SVC)
    svc.apply_file("svc.yaml", force=False)
    with pytest.raises(ClusterError) as e:
        svc.delete_orphan("v1", "Service", "web")
    assert e.value.status == 409


# ---------------------------------------------------------------------- API
class StaticManager(WatchManager):
    def start(self):
        pass

    def stop(self, join_timeout: float = 2.0):
        pass


def _noop_list(namespace, **kw):
    raise AssertionError


def client_for(svc):
    m = StaticManager({"pods": KindSpec("pods", "Pod", "v1", _noop_list, summarize.pod_status)}, "mentor")
    return TestClient(create_app(lambda: m, lambda _m: svc))


def test_api_flow(svc, root):
    with client_for(svc) as c:
        assert c.post("/api/v1/manifests/file", params={"path": "app/web.yaml"},
                      json={"content": DEPLOY}).status_code == 201
        files = c.get("/api/v1/manifests").json()["files"]
        assert [f["path"] for f in files] == ["app/web.yaml"]

        f = c.get("/api/v1/manifests/file", params={"path": "app/web.yaml"}).json()
        bad = c.put("/api/v1/manifests/file", params={"path": "app/web.yaml"},
                    json={"content": "kind: [\n", "etag": f["etag"]})
        assert bad.status_code == 400 and bad.json()["line"]
        stale = c.put("/api/v1/manifests/file", params={"path": "app/web.yaml"},
                      json={"content": DEPLOY, "etag": "old"})
        assert stale.status_code == 409

        assert c.post("/api/v1/manifests/apply", params={"path": "app/web.yaml"}).status_code == 400  # confirm 없음
        ok = c.post("/api/v1/manifests/apply", params={"path": "app/web.yaml", "confirm": "true"})
        assert ok.status_code == 200 and ok.json()["resources"][0]["action"] == "created"

        st = c.get("/api/v1/manifests/status", params={"path": "app/web.yaml"}).json()
        assert st["state"] == "synced"
        assert c.get("/api/v1/manifests/status-all").json()["files"]["app/web.yaml"]["state"] == "synced"

        assert c.delete("/api/v1/manifests/file", params={"path": "app/web.yaml"}).status_code == 400
        d = c.delete("/api/v1/manifests/file", params={"path": "app/web.yaml", "confirm": "true"})
        assert d.status_code == 200 and d.json()["resources"][0]["deleted"] is True
        assert c.get("/api/v1/manifests/file", params={"path": "../etc/passwd.yaml"}).status_code == 400


def test_api_conflict_payload(svc, root):
    write(root, "web.yaml", DEPLOY)
    svc.apply_file("web.yaml", force=False)
    svc.api.objs[("Deployment", "web")]["spec"]["replicas"] = 5
    svc.api.owned_by_other[("Deployment", "web")] = {"spec.replicas": "kubectl"}
    with client_for(svc) as c:
        r = c.post("/api/v1/manifests/apply", params={"path": "web.yaml", "confirm": "true"})
        assert r.status_code == 409 and r.json()["conflicts"][0]["field"] == ".spec.replicas"
        r = c.post("/api/v1/manifests/apply", params={"path": "web.yaml", "confirm": "true", "force": "true"})
        assert r.status_code == 200


def test_api_disabled_when_not_configured():
    m = StaticManager({"pods": KindSpec("pods", "Pod", "v1", _noop_list, summarize.pod_status)}, "mentor")
    with TestClient(create_app(lambda: m, lambda _m: None)) as c:
        assert c.get("/api/v1/manifests").status_code == 503
