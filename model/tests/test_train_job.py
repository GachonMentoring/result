"""재학습 Job (train_job.py) — 정리 규칙 · 입력 검사 · MLflow 기록/등록 · 서빙 연동.

MLflow 는 임시 폴더의 sqlite 저장소로 (서버 불필요). 클러스터에서는 같은 코드가 MLflow 서버(http)로 기록한다.
"""

import json
import pathlib
import shutil

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import make_eval
import serve
import train
import train_job
from train_job import Settings, TrainError

HERE = pathlib.Path(__file__).resolve().parents[1]
SEED = HERE / "data" / "seed.csv"
EVAL = HERE / "data" / "eval.csv"
DS = "dataset-20261123-205655"
FEEDBACK = [  # (label, text) — DB 피드백 행
    (1, "이런 글 쓰는 사람 수준 알만하다"),
    (0, "말이 너무 심하네요"),          # seed 에 1 로 있으면 → 최신 피드백(0)이 이김
    (1, "진짜 어이가 없네 ㅋㅋ"),
]


def write_dataset(csv_dir: pathlib.Path, name: str = DS, feedback=FEEDBACK) -> None:
    seed = SEED.read_text(encoding="utf-8")
    body = seed + "".join(f'{lab},"{text}"\n' for lab, text in feedback)
    (csv_dir / f"{name}.csv").write_text(body, encoding="utf-8")
    sha = train_job.sha256_of(csv_dir / f"{name}.csv")
    seed_rows = len(pd.read_csv(SEED))
    (csv_dir / f"{name}.json").write_text(json.dumps({
        "name": name, "rows": seed_rows + len(feedback), "sha256": sha,
        "sources": {"seed": {"file": "seed.csv", "rows": seed_rows, "sha256": train_job.sha256_of(SEED)},
                    "db": {"rows": len(feedback)}}}), encoding="utf-8")


@pytest.fixture
def csv_dir(tmp_path):
    d = tmp_path / "csv"
    d.mkdir()
    shutil.copy(SEED, d / "seed.csv")
    shutil.copy(EVAL, d / "eval.csv")
    write_dataset(d)
    return d


def settings(csv_dir, dataset=DS, **kw) -> Settings:
    return Settings(dataset=dataset, csv_dir=csv_dir, **kw)


# ---------------------------------------------------------------- 고정 평가셋
def test_eval_csv_is_guide_test_split(tmp_path):
    """eval.csv = 기존 train.py 의 평가 92행과 같은 문장 (기준 지표가 이어짐) + 재생성해도 같은 파일."""
    df = pd.read_csv(SEED)
    _, X_test, _, _ = train.train_test_split(df["text"], df["label"], test_size=0.2, random_state=42,
                                             stratify=df["label"])
    ev = pd.read_csv(EVAL)
    assert len(ev) == 92 and set(ev["text"]) == set(X_test)
    out = tmp_path / "eval.csv"
    assert make_eval.make_eval(SEED, out) == 92
    assert out.read_bytes() == EVAL.read_bytes()


# ---------------------------------------------------------------- 정리 규칙 (train.md 4-2)
def frame(rows):
    return pd.DataFrame([{"label": l, "text": t, "fb": fb} for l, t, fb in rows])


def test_prepare_rules():
    df = frame([
        (0, "좋아요", False), (1, " 꺼져 ", False), (0, "평가문장", False), (1, "  ", False),
        (1, "좋아요", True),            # 같은 문장, 다른 라벨 → 마지막(피드백) 유지
        (1, "꺼져", True),              # 공백 제거 후 같은 문장·같은 라벨 → 중복
    ])
    out, stats = train_job.prepare(df, {"평가문장"}, min_per_label=0)
    assert out.to_dict("records") == [{"label": 1, "text": "좋아요", "fb": True},
                                      {"label": 1, "text": "꺼져", "fb": True}]
    assert stats == {"rows_raw": 6, "rows_empty": 1, "rows_eval_excluded": 1, "rows_conflict": 1,
                     "rows_dup_removed": 2, "n_train": 2, "rows_feedback": 2}
    assert df.loc[1, "text"] == " 꺼져 "   # 원본은 그대로


def test_prepare_min_per_label():
    df = frame([(0, f"정상{i}", False) for i in range(30)] + [(1, f"주의{i}", False) for i in range(5)])
    with pytest.raises(TrainError, match=r"라벨 1 이 5행뿐 \(라벨별 최소 20행"):
        train_job.prepare(df, set(), min_per_label=20)


def test_prepare_on_real_dataset_excludes_eval(csv_dir):
    data = train_job.load_dataset(settings(csv_dir))
    ev = pd.read_csv(EVAL)
    out, stats = train_job.prepare(data.df, set(ev["text"]), 20)
    assert stats["rows_eval_excluded"] >= 92 and not set(out["text"]) & set(ev["text"])
    assert stats["rows_feedback"] == int(out["fb"].sum()) > 0


# ---------------------------------------------------------------- 입력 검사
def test_load_dataset_marks_feedback_rows(csv_dir):
    data = train_job.load_dataset(settings(csv_dir))
    assert data.seed_rows == 458 and len(data.df) == 461
    assert not data.df["fb"].iloc[:458].any() and data.df["fb"].iloc[458:].all()


def test_load_seed_dataset(csv_dir):
    data = train_job.load_dataset(settings(csv_dir, "seed"))
    assert data.name == "seed" and len(data.df) == 458 and not data.df["fb"].any()


@pytest.mark.parametrize("name", ["", "../seed", "dataset-2026", "eval"])
def test_bad_dataset_name(csv_dir, name):
    with pytest.raises(TrainError, match="데이터셋 이름"):
        train_job.load_dataset(settings(csv_dir, name))


def test_dataset_changed_after_creation(csv_dir):
    with open(csv_dir / f"{DS}.csv", "a", encoding="utf-8") as f:
        f.write('1,"나중에 추가"\n')
    with pytest.raises(TrainError, match="sha256 불일치"):
        train_job.load_dataset(settings(csv_dir))


def test_missing_summary(csv_dir):
    (csv_dir / f"{DS}.json").unlink()
    with pytest.raises(TrainError, match="요약 파일이 없습니다"):
        train_job.load_dataset(settings(csv_dir))


@pytest.mark.parametrize("body, msg", [
    ("lbl,text\n0,a\n", "label,text"),
    ('label,text\n0,"a"\n2,"b"\n', "3번째 줄의 label"),
])
def test_bad_csv(tmp_path, body, msg):
    p = tmp_path / "x.csv"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(TrainError, match=msg):
        train_job.read_labeled_csv(p, "데이터셋")


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DATASET", "seed")
    monkeypatch.setenv("FEEDBACK_WEIGHT", "2.5")
    s = Settings.from_env()
    assert s.dataset == "seed" and s.feedback_weight == 2.5 and s.min_per_label == 20
    assert str(s.csv_dir).replace("\\", "/") == "/csv"
    monkeypatch.setenv("MIN_PER_LABEL", "x")
    with pytest.raises(TrainError, match="MIN_PER_LABEL"):
        Settings.from_env()


def test_feedback_weight_makes_model_follow_corrections(csv_dir):
    """가중치가 클수록 사람이 고친 정답을 더 따른다 (train.md 4-4)."""
    data = train_job.load_dataset(settings(csv_dir))
    ev = pd.read_csv(EVAL)
    tr, _ = train_job.prepare(data.df, set(ev["text"]), 20)
    fb = tr[tr["fb"]]
    p1 = train_job.fit(tr, 1.0).predict_proba(fb["text"])[:, 1]
    p10 = train_job.fit(tr, 10.0).predict_proba(fb["text"])[:, 1]
    want = fb["label"].to_numpy()
    assert (abs(p10 - want) <= abs(p1 - want) + 1e-9).all()


# ---------------------------------------------------------------- MLflow 기록·등록 → 서빙
@pytest.fixture
def mlflow_store(tmp_path, monkeypatch):
    import mlflow
    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    mlflow.set_tracking_uri(uri)
    mlflow.set_registry_uri(uri)
    mlflow.create_experiment("toxicity-filter", artifact_location=(tmp_path / "artifacts").as_uri())
    yield uri
    mlflow.set_tracking_uri("")
    mlflow.set_registry_uri("")


def test_run_registers_and_serving_loads(csv_dir, mlflow_store, tmp_path):
    import mlflow
    from mlflow import MlflowClient

    s = settings(csv_dir, job_name="train-abcde", image="suhannugul/mentor:job-train_v1")
    r1 = train_job.run(s)
    assert r1["ok"] and r1["version"] == "1" and r1["dataset"] == DS and r1["build"].startswith("fb-")
    assert 0.8 < r1["eval_f1"] <= 1 and r1["rows_feedback"] == 3

    c = MlflowClient()
    run = c.get_run(r1["run_id"])
    assert run.info.run_name == "train-abcde" and run.info.status == "FINISHED"
    assert run.data.tags["dataset"] == DS and run.data.tags["registered_version"] == "1"
    assert run.data.tags["k8s_job"] == "train-abcde"
    assert run.data.params["feedback_weight"] == "3.0"
    assert {"eval_f1", "best_threshold", "fb_fit", "rows_eval_excluded", "n_train"} <= set(run.data.metrics)
    assert [i.dataset.name for i in run.inputs.dataset_inputs] == [DS, "eval"]

    mv = c.get_model_version("toxicity-filter", "1")
    assert mv.tags["dataset"] == DS and mv.tags["sklearn_version"] == train.sklearn.__version__
    assert mv.tags["eval_f1"] == f"{r1['eval_f1']:.4f}" and mv.tags["run_id"] == r1["run_id"]

    # initContainer 와 같은 다운로드 → 서빙
    dst = tmp_path / "models"
    path = pathlib.Path(mlflow.artifacts.download_artifacts(artifact_uri="models:/toxicity-filter/1",
                                                            dst_path=str(dst)))
    assert {"model.pkl", "model_info.json", "MLmodel", "requirements.txt"} <= {p.name for p in path.iterdir()}
    assert "scikit-learn==" + train.sklearn.__version__ in (path / "requirements.txt").read_text()
    (path / "SOURCE").write_text("models:/toxicity-filter/1\n", encoding="utf-8")
    with TestClient(serve.create_app(path / "model.pkl")) as client:
        info = client.get("/info").json()
        assert info["version"] == "toxicity-filter/1" and info["build"] == r1["build"]
        assert info["source"] == "models:/toxicity-filter/1" and info["version_mismatch"] is False
        assert info["data"]["dataset"] == DS and info["run_id"] == r1["run_id"]
        body = client.post("/predict", json={"text": "말이 너무 심하네요"}).json()
        assert body["model"]["version"] == "toxicity-filter/1" and 0 <= body["toxic_proba"] <= 1

    r2 = train_job.run(settings(csv_dir, "seed"))
    assert r2["version"] == "2" and r2["build"].startswith("seed-") and r2["fb_fit"] is None


def test_failure_does_not_register(csv_dir, mlflow_store):
    from mlflow import MlflowClient
    (csv_dir / "eval.csv").write_text('label,text\n0,"a"\n0,"b"\n', encoding="utf-8")
    with pytest.raises(TrainError, match="한 종류"):
        train_job.run(settings(csv_dir))
    assert MlflowClient().search_registered_models() == []


def test_main_writes_termination_message(csv_dir, tmp_path, monkeypatch):
    term = tmp_path / "term"
    monkeypatch.setenv("TERMINATION_LOG", str(term))
    monkeypatch.setenv("DATASET", "dataset-20991231-000000")
    monkeypatch.setenv("CSV_DIR", str(csv_dir))
    assert train_job.main() == 1
    msg = json.loads(term.read_text(encoding="utf-8"))
    assert msg["ok"] is False and "요약 파일이 없습니다" in msg["error"]


def test_serve_without_source_keeps_build_version(tmp_path):
    """SOURCE 가 없으면(기존 방식) model_info.json 의 version 그대로."""
    out = tmp_path / "a"
    info = train.train(SEED, out)
    with TestClient(serve.create_app(out / "model.joblib")) as client:
        body = client.get("/info").json()
    assert body["version"] == info["version"] and "source" not in body
