"""피드백(재학습 데이터) 저장소 — toxicity.comment_feedback 테이블 하나 (db/schema.sql).

흐름
  댓글 분류 → insert()              모델 리턴값 저장 (true_label = NULL)
  사람이 정답 입력 → set_label()     true_label · labeled_at 채움 (한 번만)
  화면 표 → list() + summary()      최신순 페이지, 필터
  재학습 Job (이후) → SELECT text, true_label WHERE true_label IS NOT NULL

환경변수 (비밀번호는 코드·git 에 두지 않는다)
  DB_HOST      기본 192.168.2.30   (로컬 개발: NodePort 31306 → mariadb-0 쓰기)
  DB_PORT      기본 31306          ※ 31307 은 읽기 복제본 — 방금 저장한 행이 늦게 보일 수 있어 쓰지 않음
  DB_NAME      기본 toxicity
  DB_USER      기본 mentor_app     (SELECT·INSERT·UPDATE 권한만 있는 앱 전용 계정 권장)
  DB_PASSWORD  필수 — 없으면 피드백 기능 꺼짐 (댓글 분류는 계속 동작)
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

log = logging.getLogger(__name__)

TABLE = "comment_feedback"
COLUMNS = ("id, text, pred_label, toxic_proba, threshold, model_name, model_version, "
           "true_label, labeled_at, created_at")

# 화면 필터 → WHERE 절 (사용자 입력이 SQL 에 들어가지 않도록 고정된 목록만 허용)
FILTERS: dict[str, str] = {
    "all": "1=1",
    "unlabeled": "true_label IS NULL",
    "labeled": "true_label IS NOT NULL",
    "wrong": "true_label IS NOT NULL AND true_label <> pred_label",
}
LABEL_NAMES = {0: "정상", 1: "주의"}


class FeedbackError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class DbSettings:
    host: str
    port: int
    name: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> "DbSettings | None":
        password = os.getenv("DB_PASSWORD")
        if not password:
            return None
        return cls(
            host=os.getenv("DB_HOST", "192.168.2.30"),
            port=int(os.getenv("DB_PORT", "31306")),
            name=os.getenv("DB_NAME", "toxicity"),
            user=os.getenv("DB_USER", "mentor_app"),
            password=password,
        )

    def describe(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.name}"


def parse_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as e:
        raise FeedbackError(400, f"올바르지 않은 ID 입니다: {value}") from e


def _iso(v: Any) -> str | None:
    # DB 는 UTC (@@system_time_zone = UTC) → 'Z' 를 붙여 화면이 현지 시각으로 바꾸게 한다
    if isinstance(v, datetime):
        return v.isoformat(timespec="milliseconds") + "Z"
    return v


def to_item(row: dict[str, Any]) -> dict[str, Any]:
    rid = row["id"]
    true_label = row["true_label"]
    pred = int(row["pred_label"])
    return {
        "id": rid.decode() if isinstance(rid, bytes) else str(rid),
        "text": row["text"],
        "pred_label": pred,
        "pred_label_name": LABEL_NAMES[pred],
        "toxic_proba": float(row["toxic_proba"]),
        "threshold": float(row["threshold"]) if isinstance(row["threshold"], Decimal) else row["threshold"],
        "model_name": row["model_name"],
        "model_version": row["model_version"],
        "true_label": None if true_label is None else int(true_label),
        "true_label_name": None if true_label is None else LABEL_NAMES[int(true_label)],
        "correct": None if true_label is None else int(true_label) == pred,
        "labeled_at": _iso(row["labeled_at"]),
        "created_at": _iso(row["created_at"]),
    }


def _db_error(e: Exception) -> FeedbackError:
    code = e.args[0] if getattr(e, "args", None) else None
    if isinstance(e, pymysql.err.OperationalError):
        if code == 1045:
            return FeedbackError(503, "DB 로그인 실패 — DB_USER / DB_PASSWORD 를 확인하세요.")
        if code in (1142, 1143):
            return FeedbackError(503, "DB 권한이 없습니다 — 계정에 SELECT·INSERT·UPDATE 권한이 필요합니다.")
        return FeedbackError(503, f"DB 에 연결할 수 없습니다 ({e.args[1] if len(e.args) > 1 else e})")
    if isinstance(e, pymysql.err.ProgrammingError) and code == 1146:
        return FeedbackError(503, f"테이블 {TABLE} 이 없습니다 — db/schema.sql 을 실행하세요.")
    if isinstance(e, pymysql.err.IntegrityError):
        return FeedbackError(400, f"DB 제약 조건 위반: {e.args[1] if len(e.args) > 1 else e}")
    return FeedbackError(502, f"DB 오류: {type(e).__name__}: {e}")


class FeedbackRepo:
    """요청마다 연결을 열고 닫는다 (가이드 14장 방식 — 단순하고 장애 후 자동 복구)."""

    def __init__(self, settings: DbSettings) -> None:
        self.settings = settings

    def _connect(self):
        s = self.settings
        return pymysql.connect(
            host=s.host, port=s.port, user=s.user, password=s.password, database=s.name,
            charset="utf8mb4", autocommit=True, cursorclass=DictCursor,
            connect_timeout=3, read_timeout=5, write_timeout=5,
        )

    def _run(self, fn):
        try:
            conn = self._connect()
        except pymysql.MySQLError as e:
            raise _db_error(e) from e
        try:
            with conn.cursor() as cur:
                return fn(cur)
        except FeedbackError:
            raise
        except pymysql.MySQLError as e:
            raise _db_error(e) from e
        finally:
            conn.close()

    # ------------------------------------------------------------ 쓰기
    def insert(self, *, text: str, pred_label: int, toxic_proba: float, threshold: float,
               model_name: str, model_version: str) -> dict[str, Any]:
        def q(cur):
            cur.execute(
                f"INSERT INTO {TABLE} (text, pred_label, toxic_proba, threshold, model_name, model_version) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, created_at",
                (text, pred_label, toxic_proba, threshold, model_name[:100], model_version[:64]),
            )
            row = cur.fetchone()
            rid = row["id"]
            return {"id": rid.decode() if isinstance(rid, bytes) else str(rid), "created_at": _iso(row["created_at"])}
        return self._run(q)

    def set_label(self, item_id: str, true_label: int) -> dict[str, Any]:
        if true_label not in (0, 1):
            raise FeedbackError(400, "true_label 은 0(정상) 또는 1(주의) 이어야 합니다.")
        item_id = parse_id(item_id)

        def q(cur):
            # 이미 정답이 있으면 0행 갱신 — 동시에 두 요청이 와도 DB 가 한 번만 반영
            cur.execute(
                f"UPDATE {TABLE} SET true_label = %s, labeled_at = CURRENT_TIMESTAMP(3) "
                "WHERE id = %s AND true_label IS NULL",
                (true_label, item_id),
            )
            updated = cur.rowcount
            cur.execute(f"SELECT {COLUMNS} FROM {TABLE} WHERE id = %s", (item_id,))
            row = cur.fetchone()
            if row is None:
                raise FeedbackError(404, "해당 피드백을 찾을 수 없습니다.")
            if updated == 0:
                raise FeedbackError(409, f"이미 정답이 입력된 항목입니다 ({LABEL_NAMES[int(row['true_label'])]}).")
            return to_item(row)
        return self._run(q)

    # ------------------------------------------------------------ 읽기
    def list(self, filter_key: str, limit: int, offset: int) -> dict[str, Any]:
        where = FILTERS.get(filter_key)
        if where is None:
            raise FeedbackError(400, f"지원하지 않는 필터: {filter_key} (지원: {', '.join(FILTERS)})")

        def q(cur):
            cur.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(true_label IS NULL), 0) AS unlabeled, "
                "COALESCE(SUM(true_label IS NOT NULL), 0) AS labeled, "
                "COALESCE(SUM(true_label IS NOT NULL AND true_label <> pred_label), 0) AS wrong "
                f"FROM {TABLE}"
            )
            summary = {k: int(v) for k, v in cur.fetchone().items()}
            cur.execute(f"SELECT COUNT(*) AS n FROM {TABLE} WHERE {where}")
            count = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT {COLUMNS} FROM {TABLE} WHERE {where} "
                "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            items = [to_item(r) for r in cur.fetchall()]
            return {"filter": filter_key, "count": count, "offset": offset, "limit": limit,
                    "items": items, "summary": summary}
        return self._run(q)
