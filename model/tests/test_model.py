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
