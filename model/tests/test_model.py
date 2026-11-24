"""학습 재현성 + 모델 서버 API."""

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

import serve
import train

HERE = pathlib.Path(__file__).resolve().parents[1]
SEED = HERE / "data" / "seed.csv"


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory):
    out = tmp_path_factory.mktemp("artifacts")
    info = train.train(SEED, out)
    return out, info


def test_training_reproduces_guide_metrics(artifacts):
    out, info = artifacts
    # 가이드 03장 / 운영 중인 모델과 같은 수치 (scikit-learn 1.9.1, random_state 42)
    assert info["metrics"]["n_train"] == 366 and info["metrics"]["n_test"] == 92
    assert info["metrics"]["f1"] == pytest.approx(0.9245, abs=1e-4)
    assert info["metrics"]["precision"] == pytest.approx(0.875, abs=1e-4)
    assert info["metrics"]["recall"] == pytest.approx(0.98, abs=1e-4)
    assert info["data"]["rows"] == 458 and info["data"]["label_counts"] == {"0": 208, "1": 250}
    saved = json.loads((out / "model_info.json").read_text(encoding="utf-8"))
    assert saved["version"] == info["version"] and saved["version"].startswith("seed-")


def test_version_is_deterministic(artifacts, tmp_path):
    _, info = artifacts
    again = train.train(SEED, tmp_path)
    assert again["version"] == info["version"]          # 같은 데이터·설정 → 같은 버전


@pytest.fixture
def client(artifacts):
    out, _ = artifacts
    with TestClient(serve.create_app(out / "model.joblib")) as c:
        yield c


def test_predict_matches_guide_examples(client):
    expected = {"이 글 덕분에 이해가 잘 됐어요": 0.351, "말이 너무 심하네요": 0.840, "안녕하세요": 0.686}
    for text, proba in expected.items():
        r = client.post("/predict", json={"text": text})
        assert r.status_code == 200
        body = r.json()
        assert body["toxic_proba"] == pytest.approx(proba, abs=1e-3)
        assert "label" not in body                      # 판정은 앱 서버(server/)가 한다
        assert body["model"]["name"] == "toxicity-filter"


def test_predict_validation(client):
    assert client.post("/predict", json={"text": ""}).status_code == 400
    assert client.post("/predict", json={"text": "   "}).status_code == 400
    assert client.post("/predict", json={"text": "가" * 301}).status_code == 400
    assert client.post("/predict", json={}).status_code == 422


def test_info_and_health(client):
    assert client.get("/healthz").json() == {"ok": True}
    info = client.get("/info").json()
    assert info["runtime_sklearn_version"] == info["sklearn_version"]
    assert info["version_mismatch"] is False
    assert info["metrics"]["f1"] > 0.9


# ---------------------------------------------------------------------- /metrics (k8sapimd/monitoring.md 1장)
def _metrics(client) -> dict[str, float]:
    """노출 형식을 파싱해 {샘플 이름{라벨}: 값} 으로."""
    from prometheus_client.parser import text_string_to_metric_families

    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    out = {}
    for fam in text_string_to_metric_families(r.text):
        for s in fam.samples:
            labels = ",".join(f"{k}={v}" for k, v in sorted(s.labels.items()))
            out[f"{s.name}{{{labels}}}"] = s.value
    return out


def test_metrics_model_info(client):
    m = _metrics(client)
    info = client.get("/info").json()
    key = next(k for k in m if k.startswith("toxicity_model_info{"))
    assert f"version={info['version']}" in key and "name=toxicity-filter" in key
    assert m[key] == 1
    assert m["toxicity_model_version_mismatch{}"] == 0
    assert m["toxicity_model_loaded_timestamp_seconds{}"] > 1_700_000_000


def test_metrics_count_predictions(client):
    version = client.get("/info").json()["version"]
    before = _metrics(client)
    client.post("/predict", json={"text": "말이 너무 심하네요"})               # 9자, 확률 0.84
    client.post("/predict", json={"text": "이 글은 학습 데이터보다 훨씬 긴 댓글이라서 범위 밖 입력으로 집계되어야 합니다"})
    client.post("/predict", json={"text": ""})                                 # 400
    after = _metrics(client)

    def delta(key):
        return after.get(key, 0) - before.get(key, 0)

    assert delta(f"toxicity_predict_requests_total{{model_version={version},result=ok}}") == 2
    assert delta(f"toxicity_predict_requests_total{{model_version={version},result=bad_request}}") == 1
    assert delta("toxicity_predict_duration_seconds_count{}") == 2
    assert delta(f"toxicity_predict_proba_count{{model_version={version}}}") == 2
    # 0.84 는 le=0.9 버킷부터 들어감
    assert delta(f"toxicity_predict_proba_bucket{{le=0.8,model_version={version}}}") <= 1
    assert delta(f"toxicity_predict_proba_bucket{{le=0.9,model_version={version}}}") >= 1
    # 길이: 9자 → le=10, 긴 댓글 → 32 초과
    assert delta("toxicity_input_length_chars_bucket{le=10.0}") == 1
    assert delta("toxicity_input_length_chars_bucket{le=32.0}") == 1
    assert delta("toxicity_input_length_chars_count{}") == 2


def test_metrics_registry_is_per_app(artifacts):
    """앱을 여러 번 만들어도 '이미 등록된 메트릭' 오류가 나지 않는다."""
    out, _ = artifacts
    for _ in range(2):
        with TestClient(serve.create_app(out / "model.joblib")) as c:
            assert c.get("/metrics").status_code == 200
