"""데이터셋 폴더(/csv) 읽기 — 목록 · 요약 · 미리보기. 쓰기는 하지 않는다 (Job 만 씀, API 는 읽기 전용 마운트).

목록 기준: dataset-<YYYYMMDD-HHMMSS>.json 이 있는 것만 (Job 이 CSV 다음 마지막에 쓰는 완료 표식)
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^dataset-\d{8}-\d{6}$")
SEED_FILE = "seed.csv"
PREVIEW_ROWS = 20
MAX_LIST = 200


class DatasetError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _count_rows(path: Path) -> int:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return max(0, sum(1 for rec in csv.reader(f) if rec) - 1)   # 헤더 제외


class DatasetStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def check_root(self) -> None:
        if not self.root.is_dir():
            raise DatasetError(503, f"데이터셋 폴더를 찾을 수 없습니다: {self.root} (CSV_DIR · PVC mentor-csv 확인)")

    def seed(self) -> dict[str, Any]:
        p = self.root / SEED_FILE
        if not p.is_file():
            return {"file": SEED_FILE, "exists": False}
        try:
            rows = _count_rows(p)
        except (OSError, UnicodeDecodeError, csv.Error):
            rows = None
        return {"file": SEED_FILE, "exists": True, "rows": rows, "size": p.stat().st_size}

    def list(self) -> list[dict[str, Any]]:
        """최신순. 요약 json 을 그대로 돌려준다 (깨진 json 은 건너뜀)."""
        self.check_root()
        out = []
        for p in sorted(self.root.glob("dataset-*.json"), reverse=True):
            if not NAME_RE.match(p.stem) or not (self.root / f"{p.stem}.csv").is_file():
                continue
            try:
                info = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(info, dict):
                out.append(info)
            if len(out) >= MAX_LIST:
                break
        return out

    def get(self, name: str, preview: int = PREVIEW_ROWS) -> dict[str, Any]:
        if not NAME_RE.match(name):
            raise DatasetError(400, f"올바르지 않은 데이터셋 이름입니다: {name}")
        self.check_root()
        meta, data = self.root / f"{name}.json", self.root / f"{name}.csv"
        if not meta.is_file() or not data.is_file():
            raise DatasetError(404, f"데이터셋이 없습니다: {name}")
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            rows = []
            with open(data, encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                next(reader, None)
                for rec in reader:
                    if len(rows) >= preview:
                        break
                    if len(rec) == 2:
                        rows.append({"label": rec[0], "text": rec[1]})
        except (OSError, ValueError, csv.Error) as e:
            raise DatasetError(500, f"데이터셋을 읽을 수 없습니다: {e}") from e
        return {**info, "preview": rows}
