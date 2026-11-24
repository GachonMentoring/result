"""Grafana 대시보드 JSON 생성기 (k8sapimd/monitoring.md 4장).

    python build.py        # → mentor-model.json, mentor-platform.json (이 폴더)

JSON 을 손으로 고치지 말고 이 파일을 고친 뒤 다시 생성한다.
import 방법: monitoring_인수인계.md 6-2 (Dashboards → Import → 파일 업로드 → DS_PROMETHEUS · DS_MYSQL 선택)

규칙
- 공개 대시보드(Shared externally)는 템플릿 변수를 쓰지 못한다 → namespace·job 을 쿼리에 고정
- 트래픽이 수동 입력 수준이라 rate()[5m] 대신 increase()[1h] · 누적값 위주
- MySQL 패널은 집계만 (댓글 원문 text 컬럼은 SELECT 하지 않음 — 공개 대시보드)
- uid 고정: 다시 import 하면 덮어씀
"""

from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
PROM = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
MYSQL = {"type": "mysql", "uid": "${DS_MYSQL}"}
NS = 'namespace="mentor"'
JOB = 'job="mentor"'

INPUTS = [
    {"name": "DS_PROMETHEUS", "label": "Prometheus", "type": "datasource", "pluginId": "prometheus",
     "pluginName": "Prometheus", "description": "공용 Prometheus (monitoring/prometheus-service)"},
    {"name": "DS_MYSQL", "label": "MySQL (mentor-feedback)", "type": "datasource", "pluginId": "mysql",
     "pluginName": "MySQL", "description": "mariadb-1 복제본, DB toxicity (monitoring_인수인계.md 6-1)"},
]


class Layout:
    """패널을 24칸 그리드에 왼쪽→오른쪽, 위→아래로 배치."""

    def __init__(self) -> None:
        self.panels: list[dict] = []
        self.x = self.y = 0
        self.row_h = 0
        self.next_id = 1

    def row(self, title: str) -> None:
        self._newline()
        self.panels.append({"type": "row", "title": title, "collapsed": False, "id": self._id(),
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.y += 1

    def add(self, panel: dict, w: int, h: int) -> None:
        if self.x + w > 24:
            self._newline()
        panel["id"] = self._id()
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)

    def _newline(self) -> None:
        if self.x:
            self.y += self.row_h
        self.x, self.row_h = 0, 0

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id - 1


# ---------------------------------------------------------------------- 패널 헬퍼
def prom(expr: str, legend: str = "", instant: bool = False, fmt: str | None = None) -> dict:
    t = {"datasource": PROM, "expr": expr, "legendFormat": legend or "__auto", "refId": "A",
         "range": not instant, "instant": instant, "editorMode": "code"}
    if fmt:
        t["format"] = fmt
    return t


def sql(query: str, fmt: str = "table") -> dict:
    return {"datasource": MYSQL, "rawSql": query.strip(), "format": fmt, "rawQuery": True,
            "editorMode": "code", "refId": "A"}


def _refs(targets: list[dict]) -> list[dict]:
    for i, t in enumerate(targets):
        t["refId"] = chr(ord("A") + i)
    return targets


def stat(title: str, targets: list[dict], *, desc: str = "", unit: str = "none", text_field: str | None = None,
         thresholds: list[tuple[float | None, str]] | None = None, mappings: list | None = None,
         decimals: int | None = None) -> dict:
    defaults: dict = {"unit": unit, "mappings": mappings or [],
                      "thresholds": {"mode": "absolute", "steps": [{"value": v, "color": c}
                                                                   for v, c in (thresholds or [(None, "blue")])]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    options: dict = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                     "colorMode": "value" if thresholds else "none", "graphMode": "none", "textMode": "auto",
                     "justifyMode": "center", "orientation": "auto"}
    if text_field:   # 라벨 값(버전 이름 등)을 글자로 표시
        options["reduceOptions"] = {"calcs": ["lastNotNull"], "fields": f"/^{text_field}$/", "values": False}
        options["textMode"] = "value"
    return {"type": "stat", "title": title, "description": desc, "datasource": targets[0]["datasource"],
            "targets": _refs(targets), "fieldConfig": {"defaults": defaults, "overrides": []}, "options": options}


def timeseries(title: str, targets: list[dict], *, desc: str = "", unit: str = "none", bars: bool = False,
               stack: bool = False, decimals: int | None = None, min0: bool = True) -> dict:
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 2, "fillOpacity": 60 if bars else 10,
              "showPoints": "auto", "spanNulls": True, "lineInterpolation": "stepAfter" if not bars else "linear",
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    defaults: dict = {"unit": unit, "custom": custom}
    if min0:
        defaults["min"] = 0
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "timeseries", "title": title, "description": desc, "datasource": targets[0]["datasource"],
            "targets": _refs(targets), "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def barchart(title: str, targets: list[dict], *, desc: str = "", x_field: str, unit: str = "none") -> dict:
    return {"type": "barchart", "title": title, "description": desc, "datasource": targets[0]["datasource"],
            "targets": _refs(targets), "fieldConfig": {"defaults": {"unit": unit, "min": 0}, "overrides": []},
            "options": {"xField": x_field, "orientation": "vertical", "showValue": "auto", "groupWidth": 0.7,
                        "barWidth": 0.9, "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi"}},
            "transformations": []}


def table(title: str, targets: list[dict], *, desc: str = "", transformations: list | None = None,
          overrides: list | None = None) -> dict:
    return {"type": "table", "title": title, "description": desc, "datasource": targets[0]["datasource"],
            "targets": _refs(targets), "transformations": transformations or [],
            "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": overrides or []},
            "options": {"showHeader": True, "cellHeight": "sm"}}


def unit_override(field: str, unit: str, decimals: int | None = None) -> dict:
    props = [{"id": "unit", "value": unit}]
    if decimals is not None:
        props.append({"id": "decimals", "value": decimals})
    return {"matcher": {"id": "byName", "options": field}, "properties": props}


OK_BAD = [{"type": "value", "options": {"0": {"text": "정상", "color": "green"},
                                        "1": {"text": "불일치", "color": "red"}}}]
SYNCED = [{"type": "value", "options": {"0": {"text": "동기화 안 됨", "color": "red"},
                                        "1": {"text": "동기화", "color": "green"}}}]
ON_OFF = [{"type": "value", "options": {"0": {"text": "꺼짐 (DB 미설정)", "color": "red"},
                                        "1": {"text": "켜짐", "color": "green"}}}]


def dashboard(uid: str, title: str, desc: str, panels: list[dict], annotations: list[dict] | None = None) -> dict:
    return {
        "__inputs": INPUTS,
        "__requires": [{"type": "grafana", "id": "grafana", "name": "Grafana", "version": "13.0.0"},
                       {"type": "datasource", "id": "prometheus", "name": "Prometheus", "version": "1.0.0"},
                       {"type": "datasource", "id": "mysql", "name": "MySQL", "version": "1.0.0"}],
        "uid": uid, "title": title, "description": desc, "tags": ["mentor", "mlops"],
        "editable": True, "graphTooltip": 1, "timezone": "browser", "schemaVersion": 39, "version": 1,
        "time": {"from": "now-24h", "to": "now"}, "refresh": "30s",
        "timepicker": {"refresh_intervals": ["30s", "1m", "5m"]},
        "templating": {"list": []},
        "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                  "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                                  "name": "Annotations & Alerts", "type": "dashboard"}] + (annotations or [])},
        "links": [], "panels": panels,
    }


# 모델 교체 시점 — DB 에서 버전별 첫 분류 시각 (Prometheus 보존 7일과 무관)
MODEL_SWITCH_ANNOTATION = {
    "name": "모델 교체", "enable": True, "iconColor": "orange", "datasource": MYSQL,
    "target": sql("""
SELECT UNIX_TIMESTAMP(MIN(created_at)) AS time, CONCAT('모델 ', model_version) AS text, 'model' AS tags
FROM comment_feedback
GROUP BY model_version
HAVING $__timeFilter(MIN(created_at))
"""),
}

ACCURACY_SQL = """
SELECT model_version AS `모델 버전`,
       COUNT(*) AS `정답 있음`,
       AVG(pred_label = true_label) AS `정확도`,
       SUM(pred_label = 1 AND true_label = 1) / NULLIF(SUM(pred_label = 1), 0) AS `정밀도`,
       SUM(pred_label = 1 AND true_label = 1) / NULLIF(SUM(true_label = 1), 0) AS `재현율`,
       MIN(created_at) AS `첫 분류`
FROM comment_feedback
WHERE true_label IS NOT NULL
GROUP BY model_version
ORDER BY MIN(created_at) DESC
"""

CONFUSION_SQL = """
SELECT model_version AS `모델 버전`,
       SUM(pred_label = 0 AND true_label = 0) AS `정상→정상`,
       SUM(pred_label = 1 AND true_label = 0) AS `정상인데 주의 (오탐)`,
       SUM(pred_label = 0 AND true_label = 1) AS `주의인데 정상 (놓침)`,
       SUM(pred_label = 1 AND true_label = 1) AS `주의→주의`
FROM comment_feedback
WHERE true_label IS NOT NULL
GROUP BY model_version
ORDER BY MIN(created_at) DESC
"""

LABEL_RATE_SQL = """
SELECT model_version AS `모델 버전`, COUNT(*) AS `분류 수`,
       SUM(true_label IS NOT NULL) AS `정답 입력`,
       AVG(true_label IS NOT NULL) AS `정답 입력률`
FROM comment_feedback
GROUP BY model_version
ORDER BY MIN(created_at) DESC
"""

DAILY_SQL = """
SELECT $__timeGroupAlias(created_at, 1d), COUNT(*) AS `분류 수`, AVG(pred_label) AS `유해 판정 비율`
FROM comment_feedback
WHERE $__timeFilter(created_at)
GROUP BY 1
ORDER BY 1
"""

RETRAIN_READY_SQL = "SELECT COUNT(*) AS `재학습 가능 데이터` FROM comment_feedback WHERE true_label IS NOT NULL"
TF = 'pod=~"toxicity-filter-.*"'


# ---------------------------------------------------------------------- ① 모델
def model_dashboard() -> dict:
    L = Layout()
    L.row("현재 상태")
    L.add(stat("서빙 중인 모델 버전", [prom(f"max by (version) (toxicity_model_info{{{JOB}}})", "{{version}}",
                                       instant=True, fmt="table")],
               desc="모델 파드 /metrics 의 toxicity_model_info. 교체가 끝나면 바뀐다.", text_field="version"), 6, 4)
    L.add(stat("학습·서빙 sklearn", [prom(f"max(toxicity_model_version_mismatch{{{JOB}}})", instant=True)],
               desc="학습한 scikit-learn 과 서빙 버전이 다르면 결과를 믿을 수 없다 (가이드 08장).",
               mappings=OK_BAD, thresholds=[(None, "green"), (1, "red")]), 4, 4)
    L.add(stat("가동 시간", [prom(f"time() - max(toxicity_model_loaded_timestamp_seconds{{{JOB}}})", instant=True)],
               desc="모델을 로드한 뒤 지난 시간", unit="s"), 4, 4)
    L.add(stat("모델 파드 Ready", [prom(f"sum(mentor_platform_pod_ready{{{JOB},{TF}}})", instant=True)],
               desc="Ready 인 모델 파드 수 (플랫폼 Watch 캐시)", thresholds=[(None, "red"), (1, "green")]), 5, 4)
    L.add(stat("모델 파드 재시작", [prom(f"sum(mentor_platform_pod_restarts{{{JOB},{TF}}})", instant=True)],
               desc="현재 모델 파드들의 컨테이너 재시작 횟수 합", thresholds=[(None, "green"), (1, "orange")]), 5, 4)

    L.row("서빙")
    L.add(timeseries("추론 요청 (1시간 단위)", [prom(
        f"sum by (result) (increase(toxicity_predict_requests_total{{{JOB}}}[1h]))", "{{result}}")],
        desc="ok = 정상 / bad_request = 길이 검증 실패 / error = 예외", bars=True, stack=True, decimals=0), 8, 8)
    L.add(timeseries("추론 평균 시간", [prom(
        f"sum(rate(toxicity_predict_duration_seconds_sum{{{JOB}}}[15m])) / sum(rate(toxicity_predict_duration_seconds_count{{{JOB}}}[15m]))",
        "평균")], desc="predict_proba 계산 시간 (15분 평균). 플랫폼의 모델 호출 제한 시간은 3초.", unit="s"), 8, 8)
    L.add(timeseries("모델 호출 실패 (플랫폼 기준, 1시간)", [prom(
        f"sum by (reason) (increase(mentor_platform_model_call_failures_total{{{JOB}}}[1h]))", "{{reason}}")],
        desc="timeout / connect / http_4xx / http_5xx / invalid", bars=True, stack=True, decimals=0), 8, 8)

    L.row("예측 분포 (정답 없이 보는 변화)")
    proba = barchart("예측 확률 분포 (버전별, 대시보드 기간)", [prom(
        f'sum by (le, model_version) (increase(toxicity_predict_proba_bucket{{{JOB},le!="+Inf"}}[$__range]))',
        "{{model_version}}", instant=True, fmt="table")],
        desc="가로축 = 확률 상한(le), 값 = 그 이하인 예측 수(누적). 0.4~0.6 구간 기울기가 크면 확신 없는 예측이 많은 것.",
        x_field="le")
    # 긴 표(le, model_version, Value) → 행 le · 열 버전별 (막대가 버전별로 나뉨)
    proba["transformations"] = [
        {"id": "groupingToMatrix", "options": {"columnField": "model_version", "rowField": "le",
                                               "valueField": "Value", "emptyValue": "zero"}},
        {"id": "convertFieldType", "options": {"conversions": [{"targetField": "le\\model_version",
                                                                "destinationType": "number"}]}},
        {"id": "sortBy", "options": {"sort": [{"field": "le\\model_version"}]}},
        {"id": "organize", "options": {"renameByName": {"le\\model_version": "le"}}},
        {"id": "convertFieldType", "options": {"conversions": [{"targetField": "le", "destinationType": "string"}]}},
    ]
    L.add(proba, 12, 9)
    L.add(timeseries("유해 판정 비율 (버전별, 1시간)", [prom(
        f'sum by (model_version) (increase(mentor_platform_classify_total{{{JOB},pred_label="1"}}[1h])) / '
        f"sum by (model_version) (increase(mentor_platform_classify_total{{{JOB}}}[1h]))", "{{model_version}}")],
        desc="임계값 판정 결과 중 '주의' 비율. 모델 교체 전후로 비교.", unit="percentunit", decimals=1), 12, 9)
    L.add(stat("학습 범위 밖 입력 (32자 초과)", [prom(
        f'1 - sum(increase(toxicity_input_length_chars_bucket{{{JOB},le="32.0"}}[$__range])) / '
        f"sum(increase(toxicity_input_length_chars_count{{{JOB}}}[$__range]))", instant=True)],
        desc="학습 데이터는 9~32자 짧은 댓글 → 이보다 긴 입력은 모델이 배운 적 없는 형태",
        unit="percentunit", decimals=1, thresholds=[(None, "green"), (0.3, "orange"), (0.6, "red")]), 6, 5)
    L.add(stat("현재 임계값", [prom(f"max(mentor_platform_threshold{{{JOB}}})", instant=True)],
               desc="확률 ≥ 임계값 → 주의 (플랫폼 THRESHOLD)", decimals=2), 6, 5)
    L.add(stat("재학습 가능 데이터 (정답 있음)", [sql(RETRAIN_READY_SQL)],
               desc="사람이 정답을 입력한 분류 결과 수 (DB)"), 6, 5)
    L.add(stat("전체 분류 (대시보드 기간)", [prom(
        f"sum(increase(mentor_platform_classify_total{{{JOB}}}[$__range]))", instant=True)],
        desc="플랫폼이 판정한 댓글 수", decimals=0), 6, 5)

    L.row("실제 성능 (사람이 입력한 정답 기준 · DB)")
    pct = [unit_override(f, "percentunit", 1) for f in ("정확도", "정밀도", "재현율")]
    L.add(table("버전별 정확도 · 정밀도 · 재현율", [sql(ACCURACY_SQL)],
                desc="정답이 입력된 행만. 정답 입력률이 낮으면 이 값을 믿기 어렵다.", overrides=pct), 14, 8)
    L.add(table("정답 입력률", [sql(LABEL_RATE_SQL)], desc="버전별 분류 수 중 사람이 정답을 입력한 비율",
                overrides=[unit_override("정답 입력률", "percentunit", 1)]), 10, 8)
    L.add(table("혼동 행렬 (버전별)", [sql(CONFUSION_SQL)],
                desc="모델 판정 → 사람 정답. 오탐 = 정상 댓글을 주의로, 놓침 = 악성 댓글을 정상으로"), 14, 7)
    L.add(timeseries("일별 분류 수 · 유해 판정 비율", [sql(DAILY_SQL, fmt="time_series")],
                     desc="DB 기준 (Prometheus 보존 기간 7일과 무관)", bars=True), 10, 7)

    L.row("리소스 (cAdvisor)")
    L.add(timeseries("CPU", [prom(
        f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{NS},{TF},container!=""}}[5m]))', "{{pod}}")],
        unit="cores", decimals=3), 12, 7)
    L.add(timeseries("메모리", [prom(
        f'sum by (pod) (container_memory_working_set_bytes{{{NS},{TF},container!=""}})', "{{pod}}")],
        unit="bytes"), 12, 7)

    return dashboard("mentor-model", "mentor · 모델",
                     "댓글 분류 모델: 서빙 상태 · 예측 분포 · 정답 기반 성능 · 리소스 (k8sapimd/monitoring.md 4-1)",
                     L.panels, [MODEL_SWITCH_ANNOTATION])


# ---------------------------------------------------------------------- ② 플랫폼
def platform_dashboard() -> dict:
    L = Layout()
    L.row("상태")
    L.add(stat("Watch 동기화 (종류별)", [prom(f"max by (kind) (mentor_platform_watch_synced{{{JOB}}})", "{{kind}}",
                                          instant=True)],
               desc="하나라도 동기화 안 됨이면 /readyz 503 → 화면이 옛 상태를 보여줄 수 있음",
               mappings=SYNCED, thresholds=[(None, "red"), (1, "green")]), 14, 5)
    L.add(stat("피드백 DB", [prom(f"max(mentor_platform_feedback_db_configured{{{JOB}}})", instant=True)],
               desc="꺼져 있으면 분류는 되지만 재학습 데이터가 저장되지 않는다", mappings=ON_OFF,
               thresholds=[(None, "red"), (1, "green")]), 5, 5)
    L.add(stat("플랫폼 API 재시작", [prom(f'sum(mentor_platform_pod_restarts{{{JOB},pod=~"mentor-api-.*"}})',
                                       instant=True)], thresholds=[(None, "green"), (1, "orange")]), 5, 5)
    L.add(table("마지막 Watch 이벤트 이후 경과", [prom(
        f"time() - max by (kind) (mentor_platform_watch_last_event_timestamp_seconds{{{JOB}}})", "{{kind}}",
        instant=True, fmt="table")],
        desc="이벤트가 없으면 마지막 LIST 시각 기준. 변화가 드문 종류는 길어도 정상.",
        transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                        "renameByName": {"kind": "종류", "Value": "경과"}}}],
        overrides=[unit_override("경과", "s")]), 10, 7)
    L.add(timeseries("Watch 재연결 · RE-LIST (1시간)", [
        prom(f"sum by (kind) (increase(mentor_platform_watch_errors_total{{{JOB}}}[1h]))", "오류 {{kind}}"),
        prom(f"sum by (kind) (increase(mentor_platform_watch_relists_total{{{JOB}}}[1h]))", "RE-LIST {{kind}}")],
        bars=True, decimals=0), 14, 7)

    L.row("의존 대상")
    L.add(timeseries("피드백 저장 결과 (1시간)", [prom(
        f"sum by (result) (increase(mentor_platform_feedback_save_total{{{JOB}}}[1h]))", "{{result}}")],
        desc="ok / error = DB 장애 / disabled = DB 미설정", bars=True, stack=True, decimals=0), 8, 7)
    L.add(timeseries("모델 호출 실패 (1시간)", [prom(
        f"sum by (reason) (increase(mentor_platform_model_call_failures_total{{{JOB}}}[1h]))", "{{reason}}")],
        bars=True, stack=True, decimals=0), 8, 7)
    L.add(timeseries("MLflow 호출 실패 (1시간)", [prom(
        f"sum by (op) (increase(mentor_platform_mlflow_failures_total{{{JOB}}}[1h]))", "{{op}}")],
        desc="재학습·모델 교체 화면이 MLflow 를 부를 때 실패", bars=True, stack=True, decimals=0), 8, 7)

    L.row("선언형 관리")
    L.add(timeseries("운영 행위 (1시간)", [prom(
        f"sum by (action, result) (increase(mentor_platform_actions_total{{{JOB}}}[1h]))", "{{action}} · {{result}}")],
        desc="삭제·재시작·매니페스트 저장/적용·데이터셋·학습·모델 교체·되돌리기·정답 입력. rejected = 4xx (확인 없음·충돌 등)",
        bars=True, stack=True, decimals=0), 12, 8)
    L.add(timeseries("선언(desired) vs 실제(ready) replica", [prom(
        f'max by (kind, name, state) (mentor_platform_workload_replicas{{{JOB},state=~"desired|ready"}})',
        "{{name}} {{state}}")],
        desc="파드를 지우거나 교체하면 ready 가 잠시 떨어졌다가 desired 로 돌아온다 (자가 복구)", decimals=0), 12, 8)
    L.add(timeseries("파드 재시작 (누적)", [prom(
        f"max by (pod) (mentor_platform_pod_restarts{{{JOB}}})", "{{pod}}")], decimals=0), 12, 7)
    L.add(timeseries("파드 Ready", [prom(f"max by (pod) (mentor_platform_pod_ready{{{JOB}}})", "{{pod}}")],
                     desc="1 = Ready", decimals=0), 12, 7)

    L.row("재학습 파이프라인")
    L.add(stat("Job (종류 · 상태)", [prom(f"max by (job_type, status) (mentor_platform_jobs{{{JOB}}})",
                                        "{{job_type}} {{status}}", instant=True)],
               desc="플랫폼이 만든 데이터셋·학습 Job 중 클러스터에 남아 있는 것 (TTL 로 지워지기 전까지). "
                    "누적 횟수는 '운영 행위' 패널", decimals=0), 16, 5)
    L.add(stat("최근 학습 소요 시간", [prom(
        f'max(mentor_platform_job_last_duration_seconds{{{JOB},job_type="train"}})', instant=True)], unit="s"), 8, 5)

    L.row("리소스 (cAdvisor)")
    pods = 'pod=~"mentor-api-.*|mentor-web-.*|mlflow-.*"'
    L.add(timeseries("CPU", [prom(
        f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{NS},{pods},container!=""}}[5m]))', "{{pod}}")],
        unit="cores", decimals=3), 12, 7)
    L.add(timeseries("메모리", [prom(
        f'sum by (pod) (container_memory_working_set_bytes{{{NS},{pods},container!=""}})', "{{pod}}")],
        desc="플랫폼 API 는 Watch 캐시를 메모리에 둔다", unit="bytes"), 12, 7)

    return dashboard("mentor-platform", "mentor · 플랫폼",
                     "MLOps 플랫폼 API: Watch 상태 · 의존 대상 · 운영 행위 · 재학습 Job · 리소스 (k8sapimd/monitoring.md 4-2)",
                     L.panels)


def all_queries(d: dict) -> list[tuple[str, str]]:
    """검증용: (패널 제목, PromQL) 목록."""
    out = []
    for p in d["panels"]:
        for t in p.get("targets", []):
            if "expr" in t:
                out.append((p["title"], t["expr"]))
    return out


def main() -> None:
    for d in (model_dashboard(), platform_dashboard()):
        path = HERE / f"{d['uid']}.json"
        path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{path.name}: 패널 {sum(p['type'] != 'row' for p in d['panels'])}개")


if __name__ == "__main__":
    main()
