"""모델 재학습 · 교체 API (k8sapimd/train.md 8장) — 실제 클러스터·MLflow 없이.

- MLflow: httpx MockTransport 로 REST 흉내
- 클러스터 apply: test_manifests 의 FakeCluster (SSA 흉내)
- 레포 원본 model/job/train.yaml · model/workload/toxicity-filter.yaml 을 그대로 사용 → 원본 자체를 검증
"""

import copy
import json
import shutil
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.datasets.job import DatasetJobs, JobSettings
from app.datasets.store import DatasetStore
from app.k8s import summarize
from app.k8s.registry import KindSpec
from app.main import create_app
from app.manifests.cluster import ClusterError, ManifestService
from app.manifests.files import ManifestStore
from app.models.deploy import DeploySettings, DeployService, find_uri, pod_problem, replace_uri
from app.models.mlflow_rest import MlflowREST, ModelError
from app.models.service import ModelService, ModelSettings
from app.models.train import TrainJobs, TrainSettings

from .test_datasets import FakeCreator, StaticManager, _noop_list
from .test_manifests import FakeCluster

REPO = Path(__file__).resolve().parents[2]
TRAIN_TEMPLATE = (REPO / "model" / "job" / "train.yaml").read_text(encoding="utf-8")
WORKLOAD = (REPO / "model" / "workload" / "toxicity-filter.yaml").read_text(encoding="utf-8")
WL = "workloads/toxicity-filter.yaml"
DS = "dataset-20261123-205655"

REGISTRY = {
    "pods": KindSpec("pods", "Pod", "v1", _noop_list, summarize.pod_status),
    "jobs": KindSpec("jobs", "Job", "batch/v1", _noop_list, summarize.job_status),
    "deployments": KindSpec("deployments", "Deployment", "apps/v1", _noop_list, summarize.deployment_status),
}


class FakeMlflow:
    """MLflow REST 흉내 (쓰는 엔드포인트만)."""

    def __init__(self):
        self.versions = {}
        self.aliases = {}
        self.runs = {}
        self.updates = []
        self.down = False

    def add(self, v, sklearn="1.9.1", status="READY", eval_sha="e1", f1="0.9320", dataset=DS):
        self.versions[str(v)] = {"status": status, "tags": {
            "sklearn_version": sklearn, "eval_sha256": eval_sha, "eval_f1": f1, "dataset": dataset,
            "n_train": "373", "rows_feedback": "8", "build": f"fb-x-{v}", "k8s_job": f"train-{v}"}}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down", request=req)
        path = req.url.path.removeprefix("/api/2.0/mlflow/")
        q = dict(req.url.params)
        body = json.loads(req.content or b"{}")
        if path == "model-versions/search":
            return httpx.Response(200, json={"model_versions": [
                {"name": "toxicity-filter", "version": v, "status": x["status"], "creation_timestamp": 1795167366530,
                 "run_id": f"run{v}", "source": f"models:/m-{v}",
                 "tags": [{"key": k, "value": val} for k, val in x["tags"].items()]}
                for v, x in self.versions.items()]})
        if path == "registered-models/alias":
            if req.method == "GET":
                v = self.aliases.get(q["alias"])
                if v is None:   # 실제 MLflow 3.18 응답 그대로 (404 가 아님)
                    return httpx.Response(400, json={"error_code": "INVALID_PARAMETER_VALUE",
                                                     "message": f"Registered model alias {q['alias']} not found."})
                return httpx.Response(200, json={"model_version": {"version": v}})
            self.aliases[body["alias"]] = body["version"]
            return httpx.Response(200, json={})
        if path == "experiments/get-by-name":
            return httpx.Response(200, json={"experiment": {"experiment_id": "1"}})
        if path == "runs/search":
            job = body["filter"].split("'")[1]
            return httpx.Response(200, json={"runs": [{"info": {"run_id": r}} for r, x in self.runs.items()
                                                      if x["job"] == job and x["status"] == "RUNNING"]})
        if path == "runs/update":
            self.updates.append(body)
            self.runs[body["run_id"]]["status"] = body["status"]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error_code": "ENDPOINT_NOT_FOUND"})


class FakeComments:
    def __init__(self):
        self.status = {"connected": True, "error": None,
                       "info": {"version": "toxicity-filter/1", "runtime_sklearn_version": "1.9.1"}}

    async def model_status(self):
        return copy.deepcopy(self.status)

    async def aclose(self):
        pass


class Env:
    pass


@pytest.fixture
def env(tmp_path):
    e = Env()
    e.manifests = tmp_path / "manifests"
    (e.manifests / "job").mkdir(parents=True)
    (e.manifests / "workloads").mkdir()
    (e.manifests / "job" / "train.yaml").write_text(TRAIN_TEMPLATE, encoding="utf-8")
    (e.manifests / "workloads" / "toxicity-filter.yaml").write_text(WORKLOAD, encoding="utf-8")
    e.csv = tmp_path / "csv"
    e.csv.mkdir()
    shutil.copy(REPO / "model" / "data" / "seed.csv", e.csv / "seed.csv")
    shutil.copy(REPO / "model" / "data" / "eval.csv", e.csv / "eval.csv")
    (e.csv / f"{DS}.csv").write_text('label,text\n0,"a"\n', encoding="utf-8")
    (e.csv / f"{DS}.json").write_text(json.dumps({"name": DS}), encoding="utf-8")

    e.manager = StaticManager(REGISTRY, "mentor")
    for w in e.manager.watchers.values():
        w.synced.set()
    e.creator = FakeCreator()
    e.cluster = FakeCluster()
    e.mlflow = FakeMlflow()
    e.mlflow.add(1)
    e.mlflow.add(2)
    e.comments = FakeComments()
    store = ManifestStore(e.manifests)
    e.msvc = ManifestService(store, e.cluster, "mentor")
    rest = MlflowREST("http://mlflow:5000", transport=httpx.MockTransport(e.mlflow))
    settings = ModelSettings(tracking_uri="http://mlflow:5000", ui_url="http://mlflow.example", deploy=DeploySettings())
    datasets = (DatasetStore(e.csv), DatasetJobs(e.manager, store, JobSettings(), e.creator))
    e.train = TrainJobs(e.manager, store, TrainSettings(), e.creator, datasets[0])
    e.svc = ModelService(settings, rest, e.train, DeployService(e.msvc, rest, e.manager))
    app = create_app(lambda: e.manager, manifests_factory=lambda _: e.msvc, comments_factory=lambda: e.comments,
                     feedback_factory=lambda: None, datasets_factory=lambda _: datasets,
                     models_factory=lambda *_: e.svc)
    e.client = TestClient(app)
    return e


def wl_text(env):
    return (env.manifests / WL).read_text(encoding="utf-8")


def live_deployment(uri, *, generation=2, observed=2, replicas=1, updated=1, available=1, total=1):
    d = yaml.safe_load_all(WORKLOAD).__next__()
    d = copy.deepcopy(d)
    d["spec"]["template"]["spec"]["initContainers"][0]["env"][0]["value"] = uri
    d["metadata"]["generation"] = generation
    d["status"] = {"observedGeneration": observed, "replicas": total, "updatedReplicas": updated,
                   "readyReplicas": available, "availableReplicas": available}
    return d


def pod(name, uri, init_state=None, ctr_state=None):
    spec = copy.deepcopy(live_deployment(uri)["spec"]["template"]["spec"])
    status = {"phase": "Pending"}
    if init_state:
        status["initContainerStatuses"] = [{"name": "fetch-model", **init_state}]
    if ctr_state:
        status["containerStatuses"] = [{"name": "app", **ctr_state}]
    return {"metadata": {"name": name, "labels": {"app": "toxicity-filter"}}, "spec": spec, "status": status}


# ---------------------------------------------------------------- 레포 원본 검증
def test_workload_original_has_one_marker_for_version_1():
    _, m = find_uri(WORKLOAD)
    assert m.group("uri") == "models:/toxicity-filter/1"
    d = next(yaml.safe_load_all(WORKLOAD))
    pod_spec = d["spec"]["template"]["spec"]
    init = pod_spec["initContainers"][0]
    assert init["name"] == "fetch-model" and init["image"] == "suhannugul/mentor:job-train_v1"
    assert pod_spec["containers"][0]["image"] == "suhannugul/mentor:model_v2"
    assert {"name": "MODEL_PATH", "value": "/models/model.pkl"} in pod_spec["containers"][0]["env"]
    assert d["spec"]["selector"] == {"matchLabels": {"app": "toxicity-filter"}}   # 불변 필드 그대로
    assert d["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0


def test_replace_uri_changes_only_that_line():
    out = replace_uri(WORKLOAD, "12")
    a, b = WORKLOAD.splitlines(), out.splitlines()
    changed = [(x, y) for x, y in zip(a, b) if x != y]
    assert len(a) == len(b) and len(changed) == 1
    assert changed[0][1].strip() == 'value: "models:/toxicity-filter/12"     # platform.mentor/model-uri'
    single = "value: 'models:/m/3'  # platform.mentor/model-uri\n"
    assert replace_uri(single, "4") == "value: 'models:/m/4'  # platform.mentor/model-uri\n"


@pytest.mark.parametrize("text, msg", [
    ("value: models:/m/1\n", "표식이 없습니다"),
    ("value: x  # platform.mentor/model-uri\n", "형식이 올바르지"),
    ("a: 1 # platform.mentor/model-uri\nb: 2 # platform.mentor/model-uri\n", "2개"),
    ('# 설명: value: "models:/m/1"  # platform.mentor/model-uri\n', "표식이 없습니다"),   # 주석 줄은 제외
    ('value: "models:/m/1"  # platform.mentor/model-uri 를 지우지 말 것\n', "표식이 없습니다"),  # 줄 끝만 인정
])
def test_find_uri_errors(text, msg):
    with pytest.raises(ModelError, match=msg):
        find_uri(text)


# ---------------------------------------------------------------- 학습 Job
def test_train_creates_job_from_repo_template(env):
    with env.client as c:
        r = c.post("/api/v1/models/train", json={"dataset": DS})
        assert r.status_code == 202 and r.json() == {"job": "dataset-00001", "ttl_seconds": 600, "dataset": DS}
        assert c.post("/api/v1/models/train", json={"dataset": "seed"}).status_code == 409   # 실행 중
    ns, j = env.creator.calls[0]
    pod_spec = j["spec"]["template"]["spec"]
    ctr = pod_spec["containers"][0]
    envs = {e["name"]: e.get("value") for e in ctr["env"]}
    assert j["metadata"]["generateName"] == "train-"
    assert j["metadata"]["labels"]["platform.mentor/job-type"] == "train"
    assert j["spec"]["template"]["metadata"]["labels"]["platform.mentor/job-type"] == "train"
    assert envs["DATASET"] == DS and [e["name"] for e in ctr["env"]].count("DATASET") == 1
    assert envs["FEEDBACK_WEIGHT"] == "3" and envs["MIN_PER_LABEL"] == "20" and envs["EVAL_FILE"] == "eval.csv"
    assert envs["MLFLOW_TRACKING_URI"] == "http://mlflow.mentor.svc.cluster.local:5000"
    assert ctr["image"] == "suhannugul/mentor:job-train_v1" and ctr["securityContext"]["readOnlyRootFilesystem"]
    assert {"name": "csv", "mountPath": "/csv", "readOnly": True} in ctr["volumeMounts"]
    assert pod_spec["volumes"][0]["persistentVolumeClaim"] == {"claimName": "mentor-csv", "readOnly": True}
    assert pod_spec["automountServiceAccountToken"] is False and j["spec"]["backoffLimit"] == 0


@pytest.mark.parametrize("dataset, status", [("../x", 400), ("dataset-20990101-000000", 404), ("", 400)])
def test_train_rejects_bad_dataset(env, dataset, status):
    with env.client as c:
        assert c.post("/api/v1/models/train", json={"dataset": dataset}).status_code == status
    assert env.creator.calls == []


def test_train_requires_eval_csv(env):
    (env.csv / "eval.csv").unlink()
    with env.client as c:
        r = c.post("/api/v1/models/train", json={"dataset": "seed"})
        assert r.status_code == 400 and "eval.csv" in r.json()["detail"]
        assert c.get("/api/v1/models").json()["eval"] == {"file": "eval.csv", "exists": False}


def test_train_jobs_marks_orphan_run_failed(env):
    """activeDeadlineSeconds 로 죽어 결과를 못 남긴 Job → MLflow run 을 FAILED 로 (R9). 한 번만."""
    env.mlflow.runs = {"r1": {"job": "train-a1", "status": "RUNNING"}, "r2": {"job": "other", "status": "RUNNING"}}
    env.manager.caches["jobs"].replace({"train-a1": {
        "metadata": {"name": "train-a1", "creationTimestamp": "2026-11-23T00:00:01Z",
                     "labels": {"platform.mentor/job-type": "train"}},
        "status": {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded",
                                   "message": "Job was active longer than specified deadline"}]}}})
    with env.client as c:
        items = c.get("/api/v1/models/train/jobs").json()["items"]
        c.get("/api/v1/models/train/jobs")
    assert items[0]["state"] == "Failed" and "deadline" in items[0]["result"]["error"]
    assert [u["run_id"] for u in env.mlflow.updates] == ["r1"]
    assert env.mlflow.runs["r2"]["status"] == "RUNNING"


# ---------------------------------------------------------------- 목록
def test_list_versions_with_flags(env):
    env.mlflow.add(3, sklearn="1.8.0", eval_sha="other")
    env.mlflow.aliases["champion"] = "1"
    with env.client as c:
        body = c.get("/api/v1/models").json()
    assert body["deploy_enabled"] and body["declared"]["version"] == "1" and body["champion"] == "1"
    assert body["template"]["file"] == "job/train.yaml" and body["template"]["ok"]
    assert body["links"]["model_page"] == "http://mlflow.example/#/models/toxicity-filter"
    by = {i["version"]: i for i in body["items"]}
    assert [i["version"] for i in body["items"]] == ["3", "2", "1"]
    assert by["1"]["declared"] and by["1"]["champion"] and by["2"]["eval_f1"] == 0.932
    assert by["2"]["compatible"] is True and by["3"]["compatible"] is False
    assert by["2"]["same_eval"] is True and by["3"]["same_eval"] is False
    assert body["serving"]["version"] == "toxicity-filter/1"


def test_mlflow_down_is_503(env):
    env.mlflow.down = True
    with env.client as c:
        r = c.get("/api/v1/models")
    assert r.status_code == 503 and "MLflow" in r.json()["detail"]


def test_disabled_without_tracking_uri(env, monkeypatch):
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    assert ModelSettings.from_env() is None
    app = create_app(lambda: env.manager, manifests_factory=lambda _: None, comments_factory=lambda: None,
                     feedback_factory=lambda: None, datasets_factory=lambda _: None)
    with TestClient(app) as c:
        assert c.get("/api/v1/models").status_code == 503


# ---------------------------------------------------------------- 교체
def test_deploy_requires_confirm(env):
    with env.client as c:
        assert c.post("/api/v1/models/deploy", json={"version": "2"}).status_code == 400
    assert env.cluster.applied == []


def test_deploy_edits_one_line_and_applies(env):
    with env.client as c:
        r = c.post("/api/v1/models/deploy", json={"version": "2", "confirm": True})
    assert r.status_code == 202, r.json()
    body = r.json()
    assert body["from"] == "1" and body["to"] == "2" and body["uri"] == "models:/toxicity-filter/2"
    assert wl_text(env) == replace_uri(WORKLOAD, "2")            # 주석·순서 그대로, 한 줄만
    applied = env.cluster.objs[("Deployment", "toxicity-filter")]
    init = applied["spec"]["template"]["spec"]["initContainers"][0]
    assert init["env"][0] == {"name": "MODEL_URI", "value": "models:/toxicity-filter/2"}
    assert ("Service", "toxicity-filter") in env.cluster.objs


@pytest.mark.parametrize("version, status, msg", [
    ("1", 409, "이미 버전 1"), ("9", 404, "버전 9 이 없습니다"), ("x", 400, "숫자"),
])
def test_deploy_rejects(env, version, status, msg):
    with env.client as c:
        r = c.post("/api/v1/models/deploy", json={"version": version, "confirm": True})
    assert r.status_code == status and msg in r.json()["detail"]
    assert wl_text(env) == WORKLOAD and env.cluster.applied == []


def test_deploy_blocks_sklearn_mismatch_and_not_ready(env):
    env.mlflow.add(3, sklearn="1.8.0")
    env.mlflow.add(4, status="PENDING_REGISTRATION")
    with env.client as c:
        r3 = c.post("/api/v1/models/deploy", json={"version": "3", "confirm": True})
        r4 = c.post("/api/v1/models/deploy", json={"version": "4", "confirm": True})
    assert r3.status_code == 409 and "scikit-learn" in r3.json()["detail"]
    assert r4.status_code == 409 and "준비되지" in r4.json()["detail"]
    assert wl_text(env) == WORKLOAD


def test_deploy_blocked_while_progressing(env):
    env.manager.caches["deployments"].upsert("toxicity-filter", live_deployment(
        "models:/toxicity-filter/1", generation=3, observed=2, updated=0))
    with env.client as c:
        r = c.post("/api/v1/models/deploy", json={"version": "2", "confirm": True})
    assert r.status_code == 409 and "진행 중" in r.json()["detail"]


def test_apply_failure_restores_file(env, monkeypatch):
    def boom(*a, **kw):
        raise ClusterError(409, "다른 관리자가 소유한 필드와 충돌합니다.")
    monkeypatch.setattr(env.msvc, "apply_file", boom)
    with env.client as c:
        r = c.post("/api/v1/models/deploy", json={"version": "2", "confirm": True})
    assert r.status_code == 409 and "원래대로" in r.json()["detail"]
    assert wl_text(env) == WORKLOAD


def test_no_marker_means_not_mlflow_mode(env):
    (env.manifests / WL).write_text(WORKLOAD.replace("# platform.mentor/model-uri", ""), encoding="utf-8")
    with env.client as c:
        st = c.get("/api/v1/models/deploy").json()
        r = c.post("/api/v1/models/deploy", json={"version": "2", "confirm": True})
    assert "표식이 없습니다" in st["declared"]["error"] and st["declared"]["version"] is None
    assert r.status_code == 409


# ---------------------------------------------------------------- 롤아웃 상태 · champion · 되돌리기
def test_complete_rollout_moves_champion_on_read(env):
    env.manager.caches["deployments"].upsert("toxicity-filter", live_deployment("models:/toxicity-filter/1"))
    with env.client as c:
        st = c.get("/api/v1/models/deploy").json()
    assert st["rollout"]["state"] == "complete" and st["champion"] == "1"
    assert env.mlflow.aliases == {"champion": "1"}


def test_failed_rollout_keeps_champion_and_rollback(env):
    env.mlflow.aliases["champion"] = "1"
    with env.client as c:
        assert c.post("/api/v1/models/deploy", json={"version": "2", "confirm": True}).status_code == 202
        # 새 파드: initContainer 가 모델을 못 받음 → 옛 파드(1)는 계속 서빙
        env.manager.caches["deployments"].upsert("toxicity-filter", live_deployment(
            "models:/toxicity-filter/2", generation=3, observed=3, updated=1, available=1, total=2))
        env.manager.caches["pods"].replace({
            "old": pod("old", "models:/toxicity-filter/1", ctr_state={"ready": True, "state": {"running": {}}}),
            "new": pod("new", "models:/toxicity-filter/2", init_state={
                "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"exitCode": 1}}}),
        })
        st = c.get("/api/v1/models/deploy").json()
        assert st["rollout"] == {**st["rollout"], "state": "failed", "reason": "Init:CrashLoopBackOff"}
        assert st["champion"] == "1" and st["declared"]["version"] == "2"

        r = c.post("/api/v1/models/deploy/rollback", json={"confirm": True})
        assert r.status_code == 202 and r.json()["to"] == "1" and r.json()["rollback"] is True
        assert c.post("/api/v1/models/deploy/rollback", json={"confirm": True}).status_code == 409
    assert wl_text(env) == WORKLOAD


def test_rollback_without_champion(env):
    with env.client as c:
        r = c.post("/api/v1/models/deploy/rollback", json={"confirm": True})
    assert r.status_code == 409 and "champion" in r.json()["detail"]


def test_pod_problem_reasons():
    assert pod_problem({"status": {"initContainerStatuses": [{"state": {"terminated": {"exitCode": 1}}}]}}) == "Init:Error"
    assert pod_problem({"status": {"initContainerStatuses": [{"state": {"waiting": {"reason": "ErrImagePull"}}}]}}) == "Init:ErrImagePull"
    assert pod_problem({"status": {"initContainerStatuses": [{"state": {"waiting": {"reason": "PodInitializing"}}}]}}) is None
    assert pod_problem({"status": {"containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}) == "CrashLoopBackOff"
    assert pod_problem({"status": {}}) is None
