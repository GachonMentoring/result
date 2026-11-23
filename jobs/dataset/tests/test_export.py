"""데이터셋 Job 테스트 — DB 없이 (fetch_feedback 은 가짜로 대체)."""

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import export
from export import ExportError, build, dataset_name, read_seed, to_csv_bytes

NOW = datetime(2026, 11, 23, 14, 15, 2, tzinfo=timezone.utc)   # KST 23:15:02


def write_seed(d: Path, text: str, name="seed.csv") -> Path:
    p = d / name
    p.write_bytes(text.encode("utf-8"))
    return p


def read_rows(p: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(p.read_text(encoding="utf-8"), newline="")))


# ---------------------------------------------------------------- 이름
def test_name_uses_kst_date_tag():
    assert dataset_name(NOW) == "dataset-20261123-231502"


def test_name_crosses_midnight_in_kst():
    assert dataset_name(datetime(2026, 11, 23, 15, 30, tzinfo=timezone.utc)) == "dataset-20261124-003000"


# ---------------------------------------------------------------- seed
def test_seed_missing_returns_none(tmp_path):
    assert read_seed(tmp_path / "seed.csv") is None


def test_seed_reads_rows_and_bom(tmp_path):
    p = tmp_path / "seed.csv"
    p.write_bytes('﻿label,text\n0,"안녕, 반가워요"\n1,"꺼져"\n\n'.encode("utf-8"))
    rows, sha = read_seed(p)
    assert rows == [(0, "안녕, 반가워요"), (1, "꺼져")]
    assert len(sha) == 64


def test_seed_wrong_header_fails(tmp_path):
    write_seed(tmp_path, "text,label\n좋아요,0\n")
    with pytest.raises(ExportError, match="label,text"):
        read_seed(tmp_path / "seed.csv")


def test_seed_bad_label_fails_with_line(tmp_path):
    write_seed(tmp_path, 'label,text\n0,"a"\nx,"b"\n')
    with pytest.raises(ExportError, match="3번째 줄"):
        read_seed(tmp_path / "seed.csv")


# ---------------------------------------------------------------- CSV 모양
def test_csv_matches_seed_format():
    out = to_csv_bytes([(0, '그는 "좋다"고 했다'), (1, "줄\n바꿈")]).decode("utf-8")
    assert out.startswith("label,text\n0,")
    assert '0,"그는 ""좋다""고 했다"' in out
    assert list(csv.reader(io.StringIO(out, newline=""))) == [["label", "text"], ["0", '그는 "좋다"고 했다'],
                                                               ["1", "줄\n바꿈"]]


# ---------------------------------------------------------------- 합치기
def test_build_seed_then_db(tmp_path):
    seed = read_seed(write_seed(tmp_path, 'label,text\n0,"seed 정상"\n1,"seed 주의"\n'))
    info = build(tmp_path, seed, "seed.csv", [(1, "db 주의"), (0, "seed 정상")], NOW, "dataset-abc12")

    assert info["name"] == "dataset-20261123-231502"
    assert read_rows(tmp_path / "dataset-20261123-231502.csv") == [
        ["label", "text"], ["0", "seed 정상"], ["1", "seed 주의"], ["1", "db 주의"], ["0", "seed 정상"]]   # 겹쳐도 그대로
    assert info["rows"] == 4
    assert info["label_counts"] == {"0": 2, "1": 2}
    assert info["sources"]["seed"]["rows"] == 2 and info["sources"]["db"]["rows"] == 2
    assert info["job"] == "dataset-abc12"
    assert info["created_at"] == "2026-11-23T14:15:02Z"

    saved = json.loads((tmp_path / "dataset-20261123-231502.json").read_text(encoding="utf-8"))
    assert saved == info
    assert not list(tmp_path.glob(".*.tmp"))   # 임시 파일 남지 않음


def test_build_without_seed(tmp_path):
    info = build(tmp_path, None, "seed.csv", [(1, "db")], NOW)
    assert info["sources"]["seed"] is None
    assert info["rows"] == 1


def test_build_empty_still_creates_csv(tmp_path):
    info = build(tmp_path, None, "seed.csv", [], NOW)
    assert info["rows"] == 0
    assert read_rows(tmp_path / f"{info['name']}.csv") == [["label", "text"]]


def test_build_refuses_overwrite(tmp_path):
    build(tmp_path, None, "seed.csv", [], NOW)
    with pytest.raises(ExportError, match="이미 있습니다"):
        build(tmp_path, None, "seed.csv", [], NOW)


def test_build_missing_dir(tmp_path):
    with pytest.raises(ExportError, match="저장 폴더"):
        build(tmp_path / "nope", None, "seed.csv", [], NOW)


# ---------------------------------------------------------------- main
def test_main_success_writes_termination_message(tmp_path, monkeypatch):
    write_seed(tmp_path, 'label,text\n0,"a"\n')
    term = tmp_path / "term.log"
    monkeypatch.setenv("CSV_DIR", str(tmp_path))
    monkeypatch.setenv("TERMINATION_LOG", str(term))
    monkeypatch.setenv("DB_PASSWORD", "x")
    monkeypatch.setattr(export, "fetch_feedback", lambda s: [(1, "b")])

    assert export.main() == 0
    msg = json.loads(term.read_text(encoding="utf-8"))
    assert msg["ok"] is True and msg["rows"] == 2 and msg["seed"] == 1 and msg["db"] == 1


def test_main_failure_reports_reason(tmp_path, monkeypatch):
    term = tmp_path / "term.log"
    monkeypatch.setenv("CSV_DIR", str(tmp_path))
    monkeypatch.setenv("TERMINATION_LOG", str(term))
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    assert export.main() == 1
    msg = json.loads(term.read_text(encoding="utf-8"))
    assert msg["ok"] is False and "DB_PASSWORD" in msg["error"]
    assert not list(tmp_path.glob("dataset-*"))
