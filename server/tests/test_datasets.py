"""데이터셋 재생성 API 테스트 — 실제 클러스터 없이 (Job 생성은 가짜, 상태는 캐시에 직접 넣음)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.datasets.job import DatasetJobs, JobSettings, job_from_template
from app.datasets.store import DatasetError, DatasetStore
from app.k8s import summarize
from app.k8s.registry import KindSpec
from app.k8s.watcher import WatchManager
from app.main import create_app
from app.manifests.files import ManifestStore, parse_documents

# 레포의 템플릿 원본 (NFS manifests/job/dataset.yaml 에 놓는 파일) — 테스트가 원본 자체를 검증
TEMPLATE = (Path(__file__).resolve().parents[2] / "jobs" / "dataset" / "job.yaml").read_text(encoding="utf-8")


def _noop_list(namespace, **kw):
    raise AssertionError("호출되면 안 됨")


REGISTRY = {
    "pods": KindSpec("pods", "Pod", "v1", _noop_list, summarize.pod_status),
    "jobs": KindSpec("jobs", "Job", "batch/v1", _noop_list, summarize.job_status),
}


class StaticManager(WatchManager):
    def start(self):
        pass

    def stop(self, join_timeout: float = 2.0):
        pass


class FakeCreator:
    def __init__(self):
        self.calls = []

    def __call__(self, namespace, body):
        self.calls.append((namespace, body))
        return {"metadata": {"name": f"dataset-{len(self.calls):05d}"}}


def make(tmp_path, now=lambda: 0.0, template=TEMPLATE):
    """tmp_path/csv = 데이터셋 폴더, tmp_path/manifests/job/dataset.yaml = Job 템플릿 (None 이면 파일 없음)."""
    m = StaticManager(REGISTRY, "mentor")
    for w in m.watchers.values():
        w.synced.set()
    creator = FakeCreator()
    mdir = tmp_path / "manifests" / "job"
    mdir.mkdir(parents=True)
    if template is not None:
        (mdir / "dataset.yaml").write_text(template, encoding="utf-8")
    jobs = DatasetJobs(m, ManifestStore(tmp_path / "manifests"), JobSettings(), creator, clock=now)
    csv = tmp_path / "csv"
    csv.mkdir()
    app = create_app(lambda: m, manifests_factory=lambda _: None, comments_factory=lambda: None,
                     feedback_factory=lambda: None, datasets_factory=lambda _: (DatasetStore(csv), jobs))
    return TestClient(app), m, creator


def put_dataset(d, name, rows=2):
    (d / f"{name}.csv").write_text('label,text\n0,"좋아요"\n1,"꺼져"\n', encoding="utf-8")
    (d / f"{name}.json").write_text(json.dumps({"name": name, "rows": rows}), encoding="utf-8")


def job(name, conditions=None, active=0):
    return {"metadata": {"name": name, "creationTimestamp": f"2026-11-23T00:00:0{name[-1]}Z",
                         "labels": {"platform.mentor/job-type": "dataset"}},
            "status": {"active": active, "conditions": conditions or []}}


# ---------------------------------------------------------------- 목록
def test_list_newest_first_only_complete_pairs(tmp_path):
    c, _, _ = make(tmp_path)
    d = tmp_path / "csv"
    put_dataset(d, "dataset-20261122-100000")
    put_dataset(d, "dataset-20261123-100000")
    (d / "dataset-20261124-100000.csv").write_text("label,text\n", encoding="utf-8")   # json 없음 → 제외
    (d / "seed.csv").write_text('label,text\n0,"a"\n1,"b"\n0,"c"\n', encoding="utf-8")
    with c:
        body = c.get("/api/v1/datasets").json()
    assert [i["name"] for i in body["items"]] == ["dataset-20261123-100000", "dataset-20261122-100000"]
    assert body["seed"] == {"file": "seed.csv", "exists": True, "rows": 3, "size": body["seed"]["size"]}
    assert body["running"] is None and body["ttl_seconds"] == 600
    assert body["template"] == {"file": "job/dataset.yaml", "ok": True, "error": None, "ttl_seconds": 600}


def test_list_without_seed(tmp_path):
    c, _, _ = make(tmp_path)
    with c:
        assert c.get("/api/v1/datasets").json()["seed"] == {"file": "seed.csv", "exists": False}


def test_missing_root_is_503(tmp_path):
    c, _, _ = make(tmp_path)
    (tmp_path / "csv").rmdir()
    with c:
        assert c.get("/api/v1/datasets").status_code == 503


# ---------------------------------------------------------------- 상세
def test_get_with_preview(tmp_path):
    c, _, _ = make(tmp_path)
    put_dataset(tmp_path / "csv", "dataset-20261123-100000")
    with c:
        body = c.get("/api/v1/datasets/dataset-20261123-100000").json()
    assert body["preview"] == [{"label": "0", "text": "좋아요"}, {"label": "1", "text": "꺼져"}]


def test_get_rejects_bad_names(tmp_path):
    c, _, _ = make(tmp_path)
    with c:
        assert c.get("/api/v1/datasets/seed").status_code == 400
        assert c.get("/api/v1/datasets/..%2Fetc").status_code in (400, 404)
        assert c.get("/api/v1/datasets/dataset-20261123-999999").status_code == 404


# ---------------------------------------------------------------- 생성
def test_create_then_409_until_cache_shows_finished(tmp_path):
    t = [0.0]
    c, m, creator = make(tmp_path, now=lambda: t[0])
    with c:
        r = c.post("/api/v1/datasets")
        assert r.status_code == 202 and r.json()["job"] == "dataset-00001"
        assert c.post("/api/v1/datasets").status_code == 409        # 캐시에 아직 없음 → 방금 만든 것 기억
        assert c.get("/api/v1/datasets").json()["running"] == "dataset-00001"

        m.caches["jobs"].upsert("dataset-00001", job("dataset-00001", active=1))
        assert c.post("/api/v1/datasets").status_code == 409        # 실행 중

        m.caches["jobs"].upsert("dataset-00001", job("dataset-00001", [{"type": "Complete", "status": "True"}]))
        assert c.post("/api/v1/datasets").status_code == 202        # 끝났으면 다시 가능
    assert len(creator.calls) == 2
    assert creator.calls[0][0] == "mentor"


def test_recent_memory_expires(tmp_path):
    t = [0.0]
    c, _, _ = make(tmp_path, now=lambda: t[0])
    with c:
        c.post("/api/v1/datasets")
        t[0] = 60.0   # 캐시에 끝내 안 나타나도 (예: 생성 직후 삭제) 영원히 막히지 않음
        assert c.post("/api/v1/datasets").status_code == 202


# ---------------------------------------------------------------- 상태·결과
def test_jobs_show_termination_message(tmp_path):
    c, m, _ = make(tmp_path)
    m.caches["jobs"].replace({
        "dataset-a1": job("dataset-a1", [{"type": "Complete", "status": "True"}]),
        "dataset-a2": job("dataset-a2", [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]),
        "other": {"metadata": {"name": "other", "labels": {}}, "status": {}},
    })
    term = lambda msg, code: {"containerStatuses": [{"state": {"terminated": {"exitCode": code, "message": msg}}}]}
    m.caches["pods"].replace({
        "p1": {"metadata": {"name": "p1", "labels": {"job-name": "dataset-a1"}}, "spec": {"containers": [{}]},
               "status": term(json.dumps({"ok": True, "name": "dataset-20261123-100000", "rows": 519}), 0)},
        "p2": {"metadata": {"name": "p2", "labels": {"job-name": "dataset-a2"}}, "spec": {"containers": [{}]},
               "status": term('{"ok": false, "error": "DB 접속 실패"}', 1)},
    })
    with c:
        items = c.get("/api/v1/datasets/jobs").json()["items"]
    assert [i["name"] for i in items] == ["dataset-a2", "dataset-a1"]       # 최신순, 다른 Job 제외
    assert items[0]["state"] == "Failed" and items[0]["result"]["error"] == "DB 접속 실패"
    assert items[1]["result"]["rows"] == 519


def test_pending_job_shows_waiting_reason(tmp_path):
    c, m, _ = make(tmp_path)
    m.caches["jobs"].replace({"dataset-a1": job("dataset-a1", active=1)})
    m.caches["pods"].replace({"p1": {"metadata": {"name": "p1", "labels": {"job-name": "dataset-a1"}},
                                     "spec": {"containers": [{}]},
                                     "status": {"phase": "Pending", "containerStatuses": [
                                         {"state": {"waiting": {"reason": "ErrImagePull"}}}]}}})
    with c:
        item = c.get("/api/v1/datasets/jobs").json()["items"][0]
    assert item["state"] == "Running" and item["waiting"] == "ErrImagePull"


# ---------------------------------------------------------------- Job 명세 (매니페스트 폴더의 템플릿)
def test_create_uses_template_file(tmp_path):
    c, _, creator = make(tmp_path)
    with c:
        r = c.post("/api/v1/datasets")
    assert r.status_code == 202 and r.json()["ttl_seconds"] == 600
    ns, j = creator.calls[0]
    spec = j["spec"]
    pod = spec["template"]["spec"]
    ctr = pod["containers"][0]
    assert ns == "mentor" and j["metadata"]["namespace"] == "mentor"
    assert j["metadata"]["generateName"] == "dataset-" and "name" not in j["metadata"]
    assert j["metadata"]["labels"]["platform.mentor/job-type"] == "dataset"
    assert spec["template"]["metadata"]["labels"]["platform.mentor/job-type"] == "dataset"
    assert spec["ttlSecondsAfterFinished"] == 600 and spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] == 300
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsUser"] == 1000
    assert ctr["image"] == "suhannugul/mentor:job-dataset_v1"
    assert {"configMapRef": {"name": "mentor-platform-config"}} in ctr["envFrom"]
    assert {"secretRef": {"name": "mentor-platform-db"}} in ctr["envFrom"]
    assert ctr["terminationMessagePolicy"] == "FallbackToLogsOnError"
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "mentor-csv"


def test_template_edit_applies_to_next_run(tmp_path):
    """파일만 고치면 다음 실행부터 반영 (apply·재시작 불필요)."""
    c, _, creator = make(tmp_path)
    f = tmp_path / "manifests" / "job" / "dataset.yaml"
    with c:
        f.write_text(TEMPLATE.replace("job-dataset_v1", "job-dataset_v2")
                     .replace("ttlSecondsAfterFinished: 600", "ttlSecondsAfterFinished: 60"), encoding="utf-8")
        r = c.post("/api/v1/datasets")
    assert r.json()["ttl_seconds"] == 60
    assert creator.calls[0][1]["spec"]["template"]["spec"]["containers"][0]["image"].endswith("job-dataset_v2")


def test_missing_template_is_400_and_reported(tmp_path):
    c, _, creator = make(tmp_path, template=None)
    with c:
        r = c.post("/api/v1/datasets")
        body = c.get("/api/v1/datasets").json()
    assert r.status_code == 400 and "job/dataset.yaml" in r.json()["detail"]
    assert body["template"]["ok"] is False and body["ttl_seconds"] is None
    assert creator.calls == []


@pytest.mark.parametrize("text, msg", [
    ("apiVersion: v1\nkind: Pod\nmetadata: {name: x}\n", "kind: Job"),
    ("apiVersion: batch/v1\nkind: Job\nmetadata: {name: x, namespace: other}\nspec: {template: {}}\n", "other"),
    ("apiVersion: batch/v1\nkind: Job\nmetadata: {name: x}\n", "spec.template"),
    ("apiVersion: batch/v1\nkind: Job\nmetadata: {name: x}\nspec: {template: {}}\n---\n"
     "apiVersion: batch/v1\nkind: Job\nmetadata: {name: y}\nspec: {template: {}}\n", "하나만"),
    ("apiVersion: batch/v1\nkind: Job\nmetadata: {generateName: x-}\nspec: {template: {}}\n", "metadata.name"),
    ("kind: [\n", "YAML 문법 오류"),
])
def test_bad_template_is_400(tmp_path, text, msg):
    c, _, creator = make(tmp_path, template=text)
    with c:
        r = c.post("/api/v1/datasets")
    assert r.status_code == 400 and msg in r.json()["detail"]
    assert creator.calls == []


def test_template_copied_from_cluster_is_cleaned():
    """kubectl get job -o yaml 결과를 그대로 붙여 넣어도 새 Job 으로 만들 수 있게."""
    doc = parse_documents(TEMPLATE)[0]
    doc["metadata"].update({"uid": "u1", "resourceVersion": "9", "namespace": "mentor"})
    doc["spec"]["selector"] = {"matchLabels": {"batch.kubernetes.io/controller-uid": "u1"}}
    doc["spec"]["template"]["metadata"]["labels"].update({"controller-uid": "u1", "job-name": "dataset"})
    doc["status"] = {"succeeded": 1}
    j = job_from_template([doc], "mentor", "job/dataset.yaml")
    assert "uid" not in j["metadata"] and "resourceVersion" not in j["metadata"] and "status" not in j
    assert "selector" not in j["spec"]
    assert j["spec"]["template"]["metadata"]["labels"] == {"platform.mentor/job-type": "dataset"}
    assert doc["metadata"]["name"] == "dataset"   # 원본 문서는 건드리지 않음


def test_empty_template_rejected():
    with pytest.raises(DatasetError):
        job_from_template([], "mentor", "job/dataset.yaml")


def test_job_settings_from_env(monkeypatch):
    assert JobSettings.from_env().template == "job/dataset.yaml"
    monkeypatch.setenv("DATASET_JOB_FILE", "job/other.yaml")
    assert JobSettings.from_env().template == "job/other.yaml"
