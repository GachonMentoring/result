"""데이터셋 재생성 Job — seed.csv + DB 피드백(정답 있는 행)을 이어 붙여 CSV 하나로 저장.

설계: k8sapimd/dataset.md
    python export.py          # 환경변수로 설정 (아래)

산출 (CSV_DIR)
  dataset-<YYYYMMDD-HHMMSS>.csv   label,text — seed 행 → DB 행 순서 (정리·검사 없이 그대로)
  dataset-<YYYYMMDD-HHMMSS>.json  요약 (이 파일이 마지막에 생김 = 완료 표식)
  ※ 날짜 태그는 한국 시간(KST)

환경변수
  CSV_DIR       기본 /csv       (NFS /home/suhan/csv 마운트 경로)
  SEED_FILE     기본 seed.csv   (CSV_DIR 안. 없으면 DB 행만으로 생성)
  DB_HOST · DB_PORT · DB_NAME · DB_USER · DB_PASSWORD   (플랫폼 API 와 같은 이름)
  JOB_NAME      Job 이름 (Job 명세가 파드 라벨에서 주입). 요약에 기록
  TERMINATION_LOG  기본 /dev/termination-log — 결과 요약 한 줄 (플랫폼 화면이 파드 상태에서 읽음)

종료 코드: 0 성공 / 1 실패
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))   # 고정 +9 → 이미지에 시간대 데이터(tzdata) 불필요
HEADER = ["label", "text"]

QUERY = (
    "SELECT true_label AS label, text FROM comment_feedback "
    "WHERE true_label IS NOT NULL ORDER BY labeled_at"
)

Row = tuple[int, str]


class ExportError(Exception):
    """사용자에게 그대로 보여줄 실패 이유."""


# ---------------------------------------------------------------- 입력
def read_seed(path: Path) -> tuple[list[Row], str] | None:
    """seed.csv → (행 목록, sha256). 파일이 없으면 None."""
    if not path.is_file():
        return None
    data = path.read_bytes()
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""))   # 엑셀 저장(BOM)도 허용
    header = next(reader, None)
    if [h.strip() for h in header or []] != HEADER:
        raise ExportError(f"{path.name} 의 첫 줄이 'label,text' 가 아닙니다: {header}")
    rows: list[Row] = []
    for lineno, rec in enumerate(reader, start=2):
        if not rec:
            continue   # 빈 줄
        if len(rec) != 2:
            raise ExportError(f"{path.name} {lineno}번째 줄: 칸이 2개가 아닙니다 ({len(rec)}개)")
        try:
            label = int(rec[0])
        except ValueError:
            raise ExportError(f"{path.name} {lineno}번째 줄: label 이 숫자가 아닙니다 ({rec[0]!r})") from None
        rows.append((label, rec[1]))
    return rows, hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class DbSettings:
    host: str
    port: int
    name: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> "DbSettings":
        password = os.getenv("DB_PASSWORD")
        if not password:
            raise ExportError("DB_PASSWORD 가 설정되지 않았습니다 (Secret mentor-platform-db 확인)")
        return cls(
            host=os.getenv("DB_HOST", "192.168.2.30"),
            port=int(os.getenv("DB_PORT", "31306")),
            name=os.getenv("DB_NAME", "toxicity"),
            user=os.getenv("DB_USER", "mentor_app"),
            password=password,
        )


def fetch_feedback(s: DbSettings) -> list[Row]:
    import pymysql   # 테스트가 DB 없이 돌도록 여기서 import

    try:
        conn = pymysql.connect(host=s.host, port=s.port, user=s.user, password=s.password, database=s.name,
                               charset="utf8mb4", connect_timeout=5, read_timeout=60)
    except pymysql.MySQLError as e:
        raise ExportError(f"DB 접속 실패 ({s.user}@{s.host}:{s.port}/{s.name}): {e}") from e
    try:
        with conn.cursor() as cur:
            cur.execute(QUERY)
            return [(int(label), text) for label, text in cur.fetchall()]
    except pymysql.MySQLError as e:
        raise ExportError(f"DB 조회 실패: {e}") from e
    finally:
        conn.close()


# ---------------------------------------------------------------- 출력
def to_csv_bytes(rows: list[Row]) -> bytes:
    """seed.csv 와 같은 모양: 헤더는 따옴표 없이, label 은 숫자, text 는 따옴표."""
    buf = io.StringIO(newline="")
    buf.write(",".join(HEADER) + "\n")
    w = csv.writer(buf, quoting=csv.QUOTE_NONNUMERIC, lineterminator="\n")
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def write_atomic(path: Path, data: bytes) -> None:
    """숨김 임시 파일에 다 쓴 뒤 이름 변경 → 중간에 죽어도 반쪽 파일이 목록에 안 나옴."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def dataset_name(now: datetime) -> str:
    return "dataset-" + now.astimezone(KST).strftime("%Y%m%d-%H%M%S")


def build(csv_dir: Path, seed: tuple[list[Row], str] | None, seed_file: str, db_rows: list[Row],
          now: datetime, job: str | None = None) -> dict:
    """seed 행 → DB 행 순서로 CSV + 요약 json 저장. 요약을 돌려준다."""
    if not csv_dir.is_dir():
        raise ExportError(f"저장 폴더가 없습니다: {csv_dir} (PVC mentor-csv 마운트 확인)")
    name = dataset_name(now)
    csv_path = csv_dir / f"{name}.csv"
    json_path = csv_dir / f"{name}.json"
    if csv_path.exists() or json_path.exists():
        raise ExportError(f"같은 이름의 데이터셋이 이미 있습니다: {name} (잠시 후 다시 실행)")

    seed_rows = seed[0] if seed else []
    rows = seed_rows + db_rows
    data = to_csv_bytes(rows)

    counts: dict[str, int] = {}
    for label, _ in rows:
        counts[str(label)] = counts.get(str(label), 0) + 1

    info = {
        "name": name,
        "created_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "job": job,
        "file": csv_path.name,
        "rows": len(rows),
        "label_counts": dict(sorted(counts.items())),
        "sha256": hashlib.sha256(data).hexdigest(),
        "sources": {
            "seed": {"file": seed_file, "rows": len(seed_rows), "sha256": seed[1]} if seed else None,
            "db": {"rows": len(db_rows)},
        },
    }
    write_atomic(csv_path, data)
    try:
        write_atomic(json_path, (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    except BaseException:
        csv_path.unlink(missing_ok=True)   # 요약 없이 CSV 만 남지 않게
        raise
    return info


# ---------------------------------------------------------------- 실행
def report(path: str, summary: dict) -> None:
    """종료 메시지 (쿠버네티스가 파드 상태에 남김, 최대 4KB). 로컬 실행 등 경로가 없으면 무시."""
    try:
        Path(path).write_text(json.dumps(summary, ensure_ascii=False)[:4000], encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    csv_dir = Path(os.getenv("CSV_DIR", "/csv"))
    seed_file = os.getenv("SEED_FILE", "seed.csv")
    term_log = os.getenv("TERMINATION_LOG", "/dev/termination-log")
    job = os.getenv("JOB_NAME") or None
    try:
        seed = read_seed(csv_dir / seed_file)
        print(f"seed: {f'{len(seed[0])}행 ({seed_file})' if seed else f'없음 ({csv_dir / seed_file}) → DB 행만 사용'}",
              flush=True)
        db = DbSettings.from_env()
        db_rows = fetch_feedback(db)
        print(f"DB: {len(db_rows)}행 ({db.user}@{db.host}:{db.port}/{db.name}, 정답 있는 행)", flush=True)
        info = build(csv_dir, seed, seed_file, db_rows, datetime.now(timezone.utc), job)
    except ExportError as e:
        print(f"실패: {e}", file=sys.stderr, flush=True)
        report(term_log, {"ok": False, "error": str(e)})
        return 1
    except OSError as e:
        msg = f"파일 쓰기 실패: {e}"
        print(f"실패: {msg}", file=sys.stderr, flush=True)
        report(term_log, {"ok": False, "error": msg})
        return 1

    src = info["sources"]
    summary = {"ok": True, "name": info["name"], "rows": info["rows"],
               "seed": src["seed"]["rows"] if src["seed"] else None, "db": src["db"]["rows"],
               "label_counts": info["label_counts"]}
    print(f"생성: {csv_dir / info['file']} ({info['rows']}행, 라벨 {info['label_counts']})", flush=True)
    report(term_log, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
