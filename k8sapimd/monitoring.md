## 마지막 단계 모니터링

왼쪽 사이드 메뉴바에 모니터링 항목을 추가한다. 그 후 그라파나의 필요한 대시보드를 웹 페이지에 import 하여 사용한다.

iframe 임베드 + "Grafana에서 열기 방식을 사용한다.

모니터링은 프로메테우스 + 그라파나로 진행되며 모니터링 대상은 모델(필수) + API 서버 정도로 생각을 하고 있다.
API 서버, 즉 플랫폼 서버의 경우 사내 플랫폼 서버라는 가정하에 모니터링 매트릭 수집 대상을 결정한다.
일반적인 웹 서버에서 사용되는 p99 혹은 응답률 은 필요없을 것으로 산정된다.
모델이 주 모니터링 대상이 될것으로 산정되나, 본 개발자가 AI 모델 학습 및 추론 서버에 대한 지식이 부족한 관계로 
일반적인 MLops 과정에서 어떠한 요소들이 위 두 오픈소스를 활용한 모니터링 대상이 되는가? 에 대한 판단이 필요하다.

1. 기존 grafana-ingress는 그대로 두고, grafanas.studylink.click 전용 Ingress를 별도로 만듭니다.
2. 새 Ingress는 경로를 mentor 대시보드의 공개 링크로만 제한합니다. Grafana의 "Public dashboards / Shared dashboards" 기능은 해당 대시보드만 열고 임의 쿼리를 막습니다. /api/ds/query, /login 같은 경로는 노출하지 않습니다.

위와 같은 방법으로 링크를 임베딩 할 계획이다.

---

# 수집 대상 · 대시보드 설계 (2026-11-24)

> 모델(필수)과 플랫폼 API 서버가 **각각 무엇을 수집하고**, Grafana 대시보드에 **무엇을 그릴지** 정리한다.
> 기준: 2026-11-24에 클러스터 `monitoring` 네임스페이스에서 확인한 사실 + Work 1~5, train.md 구현 결과

## 0. 전제 (클러스터 확인 결과)

| 항목 | 값 | 설계에 미치는 영향 |
|---|---|---|
| Prometheus | `prom/prometheus:v2.45.0` StatefulSet, Operator 없음 (ServiceMonitor 사용 불가), 수집 주기 15s, **보존 기간 7d** | 7일을 넘는 비교(모델 버전 간 성능)는 Prometheus가 아니라 **DB**에서 |
| 이미 수집 중 | cAdvisor (`job="kubernetes-cadvisor"`): 모든 컨테이너의 CPU·메모리 | 리소스 메트릭은 **코드 추가 없이** 사용 |
| 없는 것 | kube-state-metrics · node-exporter · Alertmanager | 파드 재시작·Job 성공/실패·replica 수는 **플랫폼 API의 Watch 캐시로 대신 노출** (공용 클러스터에 새 구성요소를 설치하지 않음) |
| Grafana | `grafana-oss:latest` (확인 시점 **13.0.2**), `allow_embedding = true`, anonymous Viewer | 공개 링크(Shared dashboards)로 임베드. **공개 대시보드는 템플릿 변수 없이** 쿼리를 고정해서 작성 (namespace 등을 하드코딩) |
| 우리 서비스 | 모델 `serve.py`, 플랫폼 `server/` 모두 `/metrics` 없음 | `prometheus_client`로 `/metrics` 추가. 둘 다 uvicorn worker 1개라 multiprocess 설정 불필요 |

### 데이터 출처 3가지

| 출처 | 담는 것 | Grafana 데이터소스 |
|---|---|---|
| ① 모델 파드 `/metrics` (:8088) | 추론 자체: 서빙 버전, 요청·오류, 소요시간, **확률·입력 분포** | Prometheus |
| ② 플랫폼 API `/metrics` (:8000) | 임계값 판정 결과, 의존 대상(모델·DB·MLflow) 실패, 운영 행위, Watch 상태, Watch 캐시로 만든 k8s 상태 | Prometheus |
| ③ DB `toxicity.comment_feedback` | **정답 기반 성능** (사람이 입력한 true_label과 비교), 버전별 누적 기록 | MySQL (읽기 전용 계정) |
| (+) cAdvisor | CPU · 메모리 | Prometheus (이미 수집 중) |

**정답 기반 성능을 DB에서 계산하는 이유**
- 정답(true_label)은 예측보다 **나중에** 입력된다. Prometheus 카운터로 세면 "정답을 입력한 시각" 기준이 되어 "어느 모델이 언제 틀렸는가"가 어긋난다.
- Prometheus 보존 기간은 7일이지만, 모델 버전 비교에는 전체 기간이 필요하다.

### 공통 규칙
- 메트릭 라벨에 **댓글 원문·행 ID를 넣지 않는다** (cardinality 폭발, 원문 노출). 버전 라벨은 `model_version` 하나로 통일한다.
- `/metrics`는 **Ingress에 노출하지 않는다** (현재 라우팅 `/api /healthz /readyz /docs /openapi.json`에 추가하지 않음). Prometheus가 파드 IP로 직접 수집한다.
- 공개 대시보드이므로 **DB 패널은 집계값만** 보여준다. `text` 컬럼은 SELECT하지 않는다.
- 트래픽이 수동 입력 수준이라 `rate(...[5m])`는 대부분 0이 된다. 패널은 **`increase(...[1h])`와 누적값** 위주로 그린다.

---

## 1. 모델 (`model/serve.py`) — 수집 메트릭

MLOps의 모델 모니터링 4개 층 중 **A. 서빙 상태**와 **B. 예측·입력 분포**를 모델 파드가 담당한다. C(정답 기반 성능)는 DB, D(리소스)는 cAdvisor가 담당한다.
모델 파드는 임계값을 모른다 (판정은 플랫폼 담당). 그래서 **확률 그대로**를 기록한다.

| 층 | 메트릭 | 타입 · 라벨 | 의미 · 쓰는 곳 |
|---|---|---|---|
| A | `toxicity_model_info` | gauge=1 · `name, version, build, sklearn_version, runtime_sklearn_version` | **지금 서빙 중인 모델 버전**. 교체 시점이 그래프에 그대로 보임 |
| A | `toxicity_model_version_mismatch` | gauge 0/1 | 학습·서빙 sklearn 버전 불일치 (가이드 08장 시나리오 3, `/info`와 같은 값) |
| A | `toxicity_model_loaded_timestamp_seconds` | gauge | 모델을 로드한 시각 → 가동 시간, 교체 annotation |
| A | `toxicity_predict_requests_total` | counter · `model_version, result=ok\|bad_request\|error` | 추론 요청 수. `bad_request` = 길이 검증 실패, `error` = 예외 |
| A | `toxicity_predict_duration_seconds` | histogram · buckets `.005 .01 .025 .05 .1 .25 .5 1` | 추론 계산 시간 (평균과 추세만 본다. p99 목적 아님). `MODEL_TIMEOUT=3s` 대비 여유 확인 |
| B | `toxicity_predict_proba` | histogram · `model_version` · buckets `0.1 … 1.0` (0.1 간격) | **예측 확률 분포**. 정답 없이 보는 드리프트 신호. 0.5 근처가 몰리면 "확신 없는 예측" 증가 |
| B | `toxicity_input_length_chars` | histogram · buckets `10 20 32 50 100 200 300` | **입력 분포**. 학습 데이터는 9~32자 짧은 댓글이므로 32자 초과 비율 = 학습 범위 밖 입력 비율 |

구현 메모
- 위치: `serve.py` `create_app()` 안. 메트릭 객체는 테스트에서 앱을 여러 번 만들 수 있으므로 **앱마다 `CollectorRegistry`를 따로 만들고** `/metrics`는 `generate_latest(registry)`로 응답한다.
- `toxicity_model_info`·`toxicity_model_version_mismatch`·`toxicity_model_loaded_timestamp_seconds`는 lifespan에서 모델을 로드한 직후 한 번 설정한다.
- 요구사항 파일 `model/requirements.txt`에 `prometheus_client` 추가 (버전 고정) → **새 이미지 `model_v3`**로 빌드한다 (`model_v2`는 이미 배포·서빙 중, 2026-11-24 확인). 플랫폼 API·화면도 각각 `platform-api_v5`·`platform-web_v4`로 올린다.
- 교체·재시작으로 파드가 바뀌면 카운터가 0으로 돌아간다 → 쿼리에서 `increase()`/`rate()`로 처리하고, 롤링 중 파드가 2개일 때는 `sum by (model_version)`으로 합친다.

## 2. 플랫폼 API (`server/`) — 수집 메트릭

사내 플랫폼이라 트래픽이 거의 없으므로 **p99·응답률은 수집하지 않는다.** 대신 "플랫폼이 제 기능을 하고 있는가"와 "의존 대상이 정상인가"를 본다.

### 2-1. 서비스 (댓글 분류 · 피드백) — 모델의 B층을 보완

| 메트릭 | 타입 · 라벨 | 의미 · 쓰는 곳 |
|---|---|---|
| `mentor_platform_classify_total` | counter · `model_version, pred_label=0\|1` | **임계값 판정 결과**. 유해 판정 비율 = `pred_label="1"` / 전체. 모델 교체 전후 비율 변화 |
| `mentor_platform_threshold` | gauge | 현재 임계값 (`THRESHOLD`). 판정 비율이 바뀐 원인이 모델인지 임계값인지 구분 |
| `mentor_platform_model_call_failures_total` | counter · `reason=timeout\|connect\|http_4xx\|http_5xx` | 모델 호출 실패 (`comments/service.py` `_call`) |
| `mentor_platform_feedback_save_total` | counter · `result=ok\|error\|disabled` | 피드백 DB 저장 결과. `disabled` = DB 설정 없음 → **조용히 꺼지는 문제를 드러냄** (`api/comments.py` predict) |
| `mentor_platform_feedback_db_configured` | gauge 0/1 | DB 설정 여부 (`app.state.feedback is None` 이면 0) |

### 2-2. 플랫폼 기능 (Work 1~3, 재학습 파이프라인)

| 분류 | 메트릭 | 타입 · 라벨 | 의미 |
|---|---|---|---|
| Watch | `mentor_platform_watch_synced` | gauge 0/1 · `kind` | 리소스 종류별 첫 LIST 완료 (`ResourceWatcher.synced`, `/readyz`의 근거) |
| Watch | `mentor_platform_watch_relists_total` | counter · `kind` | 410 Gone → RE-LIST 횟수 |
| Watch | `mentor_platform_watch_errors_total` | counter · `kind` | Watch 오류·재연결 (`_fail`) |
| Watch | `mentor_platform_watch_last_event_timestamp_seconds` | gauge · `kind` | 마지막 이벤트 시각 → 오래 멈춰 있으면 화면이 옛 상태를 보여주는 중 |
| 운영 행위 | `mentor_platform_actions_total` | counter · `action, result=ok\|error` | `action` = `delete`, `restart`, `manifest_save`, `manifest_delete`, `apply`, `orphan_delete`, `dataset_create`, `train_start`, `model_deploy`, `model_rollback` → **선언형 관리 행위의 기록** |
| 외부 의존 | `mentor_platform_mlflow_failures_total` | counter · `op` | MLflow REST 호출 실패 (`models/mlflow_rest.py`) |

### 2-3. Watch 캐시로 만드는 k8s 상태 (kube-state-metrics 대체)
`/metrics`가 호출될 때 **custom Collector가 Watch 캐시를 읽어** 그 자리에서 값을 만든다. 쿠버네티스 API를 추가로 호출하지 않는다.

| 메트릭 | 타입 · 라벨 | 계산 | 쓰는 곳 |
|---|---|---|---|
| `mentor_platform_pod_restarts` | gauge · `pod, container` | `status.containerStatuses[].restartCount` | **셀프힐링 시연** (재시작 증가) |
| `mentor_platform_pod_ready` | gauge 0/1 · `pod` | Ready condition | 파드 가용 여부 |
| `mentor_platform_workload_replicas` | gauge · `kind, name, state=desired\|ready\|updated` | Deployment·StatefulSet `spec.replicas` / `status.*` | 선언(desired)과 실제(ready)의 차이 → 롤링·복구 과정 |
| `mentor_platform_jobs` | gauge · `job_type, status=active\|succeeded\|failed` | 라벨 `platform.mentor/job-type`별 Job 개수 | 데이터셋 생성 · 학습 Job 결과 |
| `mentor_platform_job_last_duration_seconds` | gauge · `job_type` | 가장 최근에 끝난 Job의 `completionTime - startTime` | 학습 소요 시간 추세 |

구현 메모
- `mentor_platform_jobs`·`job_last_duration`의 Job은 **쿠버네티스 Job**(데이터셋 생성·학습)이다. Job은 일회성이라 끝나면 메트릭을 낼 주체가 사라진다 → **상시 떠 있는 플랫폼 API가 Watch 캐시에 남은 Job 상태를 대신 노출**한다. Job 객체가 지워지면 기록도 사라지므로, 누적 이력은 MLflow run(학습)과 `mentor_platform_actions_total{action="train_start"|"dataset_create"}`로 본다.
- 카운터는 각 기능 코드에서 증가시키고, 2-3은 `prometheus_client.registry.Collector`를 구현해서 `WatchManager`의 캐시를 읽는다.
- `/metrics` 경로는 `main.py`에 `@app.get("/metrics")`로 추가한다 (`/healthz`와 같은 급, `/api` 아래가 아님 → Ingress에 노출되지 않음).
- `server/requirements.txt`에 `prometheus_client` 추가 (버전 고정).
- 파드 이름 라벨은 재시작·롤링 때마다 바뀌지만, mentor 네임스페이스의 파드 수가 적어서 허용한다.

## 3. DB (`comment_feedback`) — 정답 기반 성능

- Grafana에 **MySQL 데이터소스** 추가. 주소는 **복제본 한 대** `mariadb-1.mariadb-headless.database.svc.cluster.local:3306`을 쓴다 (`mariadb-read`는 쓰기 파드 mariadb-0도 가리키므로 사용하지 않음).
- 계정: **`mentor_app` 재사용** (8장 결정). 쓰기 권한이 있는 계정이므로 복제본의 `read_only = 1`로 쓰기를 막는다 → 확인 필수. 아니면 SELECT 전용 `grafana_ro`로 전환.
- 공개 대시보드는 패널 쿼리가 고정되어 있어 방문자가 임의 SQL을 실행할 수 없다. 그래도 **집계 쿼리만** 둔다.

| 패널 | 쿼리 (요지) |
|---|---|
| 버전별 정확도·정밀도·재현율 | `SELECT model_version, COUNT(*) n, AVG(pred_label = true_label) accuracy, SUM(pred_label=1 AND true_label=1)/NULLIF(SUM(pred_label=1),0) precision_, SUM(pred_label=1 AND true_label=1)/NULLIF(SUM(true_label=1),0) recall FROM comment_feedback WHERE true_label IS NOT NULL GROUP BY model_version` |
| 혼동 행렬 (버전별) | `SELECT model_version, pred_label, true_label, COUNT(*) FROM … WHERE true_label IS NOT NULL GROUP BY 1,2,3` |
| 정답 입력률 | `SELECT model_version, AVG(true_label IS NOT NULL) FROM … GROUP BY model_version` → 낮으면 위 정확도를 믿기 어려움 |
| 일별 분류량 · 유해 판정 비율 | `SELECT DATE(created_at) time, COUNT(*), AVG(pred_label) FROM … WHERE $__timeFilter(created_at) GROUP BY 1` |
| 재학습 가능 데이터 누적 | `SELECT COUNT(*) FROM … WHERE true_label IS NOT NULL` → 재학습 시점 판단 |
| (annotation) 모델 교체 시점 | `SELECT MIN(created_at) AS time, CONCAT('모델 ', model_version) AS text FROM … GROUP BY model_version` |

## 4. 대시보드 구성

공개 대시보드는 템플릿 변수를 쓸 수 없으므로 쿼리에 `namespace="mentor"` 등을 **고정**한다. 대시보드 JSON은 레포 `monitoring/dashboards/`에 두고 **uid를 고정**해서 import한다 (Grafana `mentor` 폴더). 플랫폼 화면의 모니터링 메뉴에 탭 2개로 임베드한다.

### 4-1. 대시보드 ① 모델 (`uid: mentor-model`)

| 행 | 패널 | 형태 | 쿼리 (요지) |
|---|---|---|---|
| 현재 상태 | 서빙 버전 | Stat (텍스트) | `toxicity_model_info` → `version` 라벨 |
| | sklearn 불일치 | Stat (0=정상 초록, 1=빨강) | `max(toxicity_model_version_mismatch)` |
| | 가동 시간 | Stat | `time() - max(toxicity_model_loaded_timestamp_seconds)` |
| | 모델 파드 Ready / 재시작 | Stat | `mentor_platform_pod_ready{pod=~"toxicity-filter.*"}`, `sum(mentor_platform_pod_restarts{pod=~"toxicity-filter.*"})` |
| 서빙 | 요청 수 (결과별) | 막대 | `sum by (result) (increase(toxicity_predict_requests_total[1h]))` |
| | 추론 평균 시간 | 시계열 | `rate(..._duration_seconds_sum[15m]) / rate(..._duration_seconds_count[15m])` |
| | 모델 호출 실패 (플랫폼 기준) | 시계열 | `sum by (reason) (increase(mentor_platform_model_call_failures_total[1h]))` |
| 예측 분포 | 확률 분포 (버전별) | 히스토그램 / 막대 | `sum by (le, model_version) (increase(toxicity_predict_proba_bucket[$__range]))` |
| | 유해 판정 비율 (버전별) | 시계열 | `sum by (model_version)(increase(mentor_platform_classify_total{pred_label="1"}[1h])) / sum by (model_version)(increase(mentor_platform_classify_total[1h]))` |
| | 학습 범위 밖 입력 비율 (32자 초과) | Stat / 시계열 | `1 - sum(increase(toxicity_input_length_chars_bucket{le="32"}[$__range])) / sum(increase(toxicity_input_length_chars_count[$__range]))` |
| | 현재 임계값 | Stat | `mentor_platform_threshold` |
| 실제 성능 (DB) | 버전별 정확도·정밀도·재현율 | 표 | 3장 |
| | 혼동 행렬 | 표 | 3장 |
| | 정답 입력률 · 재학습 가능 데이터 | Stat | 3장 |
| 리소스 | CPU | 시계열 | `sum by (pod)(rate(container_cpu_usage_seconds_total{namespace="mentor",pod=~"toxicity-filter.*",container!=""}[5m]))` |
| | 메모리 | 시계열 | `sum by (pod)(container_memory_working_set_bytes{namespace="mentor",pod=~"toxicity-filter.*",container!=""})` |
| annotation | 모델 교체 시점 | — | 3장 annotation 쿼리 |

### 4-2. 대시보드 ② 플랫폼 (`uid: mentor-platform`)

| 행 | 패널 | 형태 | 쿼리 (요지) |
|---|---|---|---|
| 상태 | Watch 동기화 (kind별) | Stat 그리드 (0=빨강) | `mentor_platform_watch_synced` |
| | 마지막 이벤트 이후 경과 | 표 | `time() - mentor_platform_watch_last_event_timestamp_seconds` |
| | 재연결·RE-LIST | 시계열 | `increase(mentor_platform_watch_errors_total[1h])`, `increase(..._relists_total[1h])` |
| 의존 대상 | 피드백 DB 설정 / 저장 결과 | Stat + 막대 | `mentor_platform_feedback_db_configured`, `sum by (result)(increase(mentor_platform_feedback_save_total[1h]))` |
| | MLflow 호출 실패 | 시계열 | `sum by (op)(increase(mentor_platform_mlflow_failures_total[1h]))` |
| 선언형 관리 | 운영 행위 (행위·결과별) | 막대 (누적) | `sum by (action, result)(increase(mentor_platform_actions_total[1h]))` |
| | 선언 vs 실제 replica | 시계열 | `mentor_platform_workload_replicas{state=~"desired\|ready"}` → 삭제·롤링 후 복구되는 모습 |
| | 파드 재시작 | 시계열 | `mentor_platform_pod_restarts` |
| 재학습 파이프라인 | Job 결과 (종류별) | Stat / 막대 | `mentor_platform_jobs` |
| | 최근 학습 소요 시간 | Stat | `mentor_platform_job_last_duration_seconds{job_type="train"}` |
| 리소스 | API · 화면 · MLflow CPU / 메모리 | 시계열 | cAdvisor, `pod=~"mentor-api.*\|mentor-web.*\|mlflow.*"` (Watch 캐시가 메모리에 있으므로 API 메모리 추세 확인) |

## 5. 발표 시나리오와 연결 (base.md)

| 장면 | 하는 일 | 대시보드에서 보이는 것 |
|---|---|---|
| 셀프힐링 | 인프라 화면에서 모델 파드 삭제 | 플랫폼: desired 1 / ready 0 → 1 복구. 모델: 요청 실패 잠깐 발생 후 정상 |
| 모델 교체 | 모델 교체 화면에서 버전 N 선택 (YAML 한 줄 변경) | 모델: 서빙 버전 변경, annotation 표시, 확률 분포·유해 판정 비율이 버전별로 나뉨 |
| 재학습 효과 | 피드백 정답 입력 → 재학습 → 교체 | DB 패널: 버전별 정확도 비교, 재학습 가능 데이터 누적 |
| 장애 감지 | DB Secret을 비운 채로 재시작 | 플랫폼: 피드백 DB 설정 0, 저장 결과 `disabled` 증가 (분류는 계속 동작) |

## 6. Prometheus 수집 — `mentor` 수집 규칙 추가 (확정 2026-11-24)

> "job" = Prometheus 설정의 **수집 규칙 이름 `job_name`**. 쿠버네티스 Job이 아니다. 15초마다 상시 수집한다.
> 파드 주석 방식(기존 `spring-boot-actuator` 규칙에 얹히는 방식)은 쓰지 않는다.

### 6-1. 확인한 사실
| 항목 | 결과 |
|---|---|
| Prometheus 권한 | ClusterRole `prometheus-cluster-role`: `pods`·`endpoints`·`services` get/list/watch (**클러스터 전체**) → mentor 네임스페이스 파드 발견 가능 |
| 재시작 시 데이터 | `/prometheus` = PVC `prometheus-pvc` → 재시작해도 **7일치 데이터 유지**. 수집 공백은 재시작하는 수십 초 |
| mentor 파드 라벨 | `toxicity-filter`는 `app=…`, `mentor-api`는 `app.kubernetes.io/name=…`로 **체계가 다름** → 이름으로 고르지 않고 **수집 대상 표시 라벨**을 따로 둔다 |
| 포트 이름 | 두 파드 모두 컨테이너 포트 이름이 `http` (8088 / 8000) |

### 6-2. 수집 규칙 (`monitoring/prometheus-config`의 `scrape_configs`에 추가)
```yaml
  # 7. mentor MLOps 플랫폼 (모델 · 플랫폼 API)
  - job_name: 'mentor'
    metrics_path: /metrics
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names: ['mentor']
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_label_platform_mentor_metrics]   # 파드 라벨 platform.mentor/metrics
        action: keep
        regex: "true"
      - source_labels: [__meta_kubernetes_pod_container_port_name]
        action: keep
        regex: http
      - source_labels: [__meta_kubernetes_pod_phase]
        action: drop
        regex: (Succeeded|Failed)        # 끝난 Job 파드는 제외
      - source_labels: [__meta_kubernetes_namespace]
        target_label: namespace
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
      - source_labels: [__meta_kubernetes_pod_container_name]
        target_label: container
```
- 수집 대상이 되려면 **파드 템플릿에 라벨 `platform.mentor/metrics: "true"`**를 붙인다. 대상은 `toxicity-filter`(NFS `workloads/toxicity-filter.yaml`)와 `mentor-api`(최상위 `mentor-manifests/`)이다.
- 라벨은 주석과 달리 **이 규칙에서만 쓰는 표식**이다. 다른 수집 규칙은 이 라벨을 보지 않으므로 중복 수집되지 않는다.
- 메트릭 라벨은 `job="mentor"`, `namespace`, `pod`, `container`로 붙는다 → 4장 쿼리에 `job="mentor"`를 추가한다.

### 6-3. 적용 절차 (공용 인프라 — 최상위 관리 디렉터리에서 사람이 `kubectl`로)
1. 현재 ConfigMap 백업: `kubectl get cm prometheus-config -n monitoring -o yaml > prometheus-config.bak.yaml`
2. 위 단락을 추가하고, `promtool check config`로 문법을 검사 (prom/prometheus 이미지에 포함)
3. `kubectl apply` → `--web.enable-lifecycle`가 없어 즉시 반영되지 않음 → `kubectl delete pod prometheus-statefulset-0 -n monitoring` (StatefulSet이 다시 띄움, PVC 유지)
4. 확인: `prom.studylink.click`(VPN) → Status › Targets에서 `mentor` 규칙의 대상 2개가 UP
5. 실패하면 백업으로 되돌리고 다시 재시작한다 (다른 프로젝트의 수집이 함께 멈추므로 3번은 짧게)

## 7. VPN 없는 시연을 위한 한정 공개 (확정 방향 2026-11-24)

### 7-1. 요구사항
- 시연장에서 **VPN 없이** 플랫폼 화면 안의 Grafana 대시보드 2개(모델 / 플랫폼)를 보여준다.
- Grafana **전체를 공개하지 않는다.** 공용 Grafana에는 다른 프로젝트 대시보드와 Prometheus 전체 데이터가 있고, anonymous Viewer는 데이터소스에 임의 쿼리를 보낼 수 있다 → 공개하면 인프라 전체가 노출된다.

### 7-2. 방식 — 가능함 (확인함)
| 확인 항목 | 결과 |
|---|---|
| 공개 대시보드 기능 | Grafana 13.0.2 `[public_dashboards] enabled = true` (기본값). `/api/public/dashboards/<토큰>` → `publicdashboards.notFound` 응답 = 기능 동작 중 |
| 새 도메인 | `grafanas.studylink.click` → 공개 DNS `<공인 IP>` (공유기 → Ingress 192.168.2.100) |
| 공개 페이지가 쓰는 경로 | HTML `/public-dashboards/<토큰>`, 정적 파일 `public/build/…`·`public/img/…` (상대 경로) |

**구성**
```
기존 grafana-ingress   grafana.studylink.click   /  (전체)        whitelist VPN·내부망   ← 그대로 둠
새 grafana-public      grafanas.studylink.click  아래 3개 경로만   whitelist 없음
                         /public-dashboards/   공개 대시보드 화면 (토큰 URL)
                         /api/public/          공개 대시보드 전용 API (저장된 패널 쿼리만 실행)
                         /public/              정적 파일 (JS·CSS·이미지)
                       그 외 /login /api/ds/query /api/datasources /d/… /explore … → 규칙 없음 → 404
```
- 새 Ingress에는 경로 규칙을 **3개만** 둔다. 규칙이 없는 경로는 ingress-nginx가 404로 응답한다. 막을 경로를 나열하는 방식이 아니라 **허용할 경로만 적는 방식**이라 새로운 API가 생겨도 자동으로 막힌다.
- 공개 대시보드는 **저장된 패널 쿼리만** 실행한다. 방문자는 PromQL·SQL을 바꿀 수 없다. 접근하려면 추측할 수 없는 토큰 URL을 알아야 한다.
- 공개 대상은 `mentor-model`, `mentor-platform` 대시보드 2개뿐이다. 다른 대시보드는 공개 설정을 하지 않았으므로 토큰이 없다.
- 공개 대시보드 설정: **시간 선택기 끄기**(넓은 기간으로 무거운 쿼리를 반복하지 못하게), annotation 켜기 (모델 교체 표시).
- Ingress 추가 설정: `limit-rps`(예: 10)와 `limit-connections`로 공개 쿼리 API 남용을 막는다. TLS는 `letsencrypt-route53`으로 `grafanas.studylink.click` 인증서를 따로 발급한다 (Secret `grafana-public-tls`).
- 플랫폼 화면의 iframe URL: `https://grafanas.studylink.click/public-dashboards/<토큰>`. 토큰은 `front/config.js`에 둔다. 토큰은 공개 URL이므로 비밀값이 아니다.
- "Grafana에서 열기" 링크도 같은 공개 URL로 연결한다. 전체 Grafana(`grafana.studylink.click`)는 VPN에서만 열린다.

**적용 후 검증**
1. VPN을 끈 상태에서 공개 URL 2개가 정상 표시되는지, 브라우저 개발자 도구의 네트워크 탭에 404·403이 없는지 확인한다. 404가 있으면 그 경로가 정말 필요한지 판단한 뒤에 허용 경로에 추가한다.
2. VPN을 끈 상태에서 `grafanas.studylink.click/login`, `/api/ds/query`, `/api/datasources`, `/d/<다른 대시보드>`가 **404**인지 확인한다.

### 7-3. ⚠ 함께 풀어야 하는 것 — 플랫폼 화면 자체도 VPN 전용
iframe을 품는 **플랫폼 화면 `mlops.studylink.click`도 화이트리스트(VPN·내부망)로 막혀 있다** (deploy.md 8-5). Grafana를 공개해도 시연장에서는 플랫폼 화면이 403이라 임베드를 보여줄 수 없다.
플랫폼에는 로그인이 없고 리소스 삭제·YAML apply·**Secret 조회**가 가능하므로, 화이트리스트를 그냥 푸는 것은 Grafana 전체 공개보다 위험하다.

| 방안 | 내용 | 장점 | 단점 |
|---|---|---|---|
| A. 시연장 IP 임시 허용 | 시연 전에 시연장 네트워크의 공인 IP를 `whitelist-source-range`에 추가하고, 끝나면 제거 | 설정 한 줄, 화면은 무인증 그대로 | 시연장 IP를 미리 알아야 함. 핫스팟·학교망은 IP가 바뀔 수 있음 |
| B. 시연 기간에만 화이트리스트 해제 + 기본 인증 | `auth-type: basic` + Secret(htpasswd)로 아이디·비밀번호 요구. 시연 후 화이트리스트 복구 | 어느 네트워크에서나 동작 | 비밀번호가 유출되면 플랫폼 전체가 노출됨. 시연 기간을 짧게 유지해야 함 |

- ingress-nginx는 "화이트리스트 **또는** 인증" 조합을 기본 annotation으로 만들 수 없다 (둘 다 걸면 AND). 그래서 A와 B 중 하나를 고른다.
- 권장: **시연장 IP를 미리 확인할 수 있으면 A, 확인할 수 없으면 B.** 어느 쪽이든 시연 후 원래대로 되돌리는 절차를 체크리스트에 넣는다.
- 모델(`toxicity-filter`)·플랫폼 API의 `/metrics`는 여전히 외부에 노출하지 않는다. 시연장에서 보이는 것은 플랫폼 화면과 공개 대시보드뿐이다.

## 8. 결정 사항 (2026-11-24)

| # | 내용 | 결정 |
|---|---|---|
| 1 | 플랫폼 화면의 시연용 공개 방식 (7-3) | **화이트리스트 해제 + 기본 인증** (B안). ⚠ 확인 결과 `mentor-platform` Ingress에는 **화이트리스트가 이미 없다** → 기본 인증을 서둘러 적용 |
| 2 | Grafana DB 계정 | **`mentor_app` 재사용**. 주소는 복제본 한 대 `mariadb-1.mariadb-headless.database.svc.cluster.local:3306` (`mariadb-read`는 쓰기 파드도 포함하므로 사용 안 함). 전제: 복제본 `read_only = 1` 확인 — 아니면 `grafana_ro` 생성으로 전환 |
| 3 | Grafana 이미지 | **고정하지 않음** (범위 밖) |
| 4 | 대시보드 작성 방식 | **레포에 JSON 작성 → 사람이 import** (`monitoring/dashboards/`, uid 고정, 데이터소스는 `DS_PROMETHEUS`·`DS_MYSQL` 입력값으로) |
| 5 | 공개 URL 전달 | 화면 이미지에 넣지 않고 **ConfigMap(`GRAFANA_MODEL_URL`·`GRAFANA_PLATFORM_URL`) → API → 화면** (토큰 발급 후 이미지 재빌드 불필요) |
| 6 | Grafana `root_url` | 미설정 유지. 공개 화면은 상대 경로라 동작. 인수인계 9장 검증에서 확인 |

적용 절차(사람·인프라 AI가 할 일): 레포 최상위 **`monitoring_인수인계.md`**

---

## 9. 구현 결과 (2026-11-24)

### 9-1. 만든 것
| 영역 | 파일 | 내용 |
|---|---|---|
| 모델 | `model/serve.py` | `/metrics` — 1장 메트릭 7종 (앱마다 `CollectorRegistry`). `requirements.txt`에 `prometheus-client==0.26.0` |
| API | `server/app/metrics.py` | 이벤트 카운터(전역) + `StateCollector`(Watch 캐시·설정을 `/metrics` 호출 때 계산) |
| API | `server/app/main.py` | `/metrics`(OpenAPI 제외), 운영 행위 카운트 미들웨어, 모니터링 라우터 |
| API | `server/app/api/monitoring.py` | `GET /api/v1/monitoring` — `GRAFANA_MODEL_URL`·`GRAFANA_PLATFORM_URL` (http(s) 절대 주소만, 그 외는 `error`) |
| API | `comments/service.py`, `api/comments.py`, `models/mlflow_rest.py`, `k8s/watcher.py` | 모델 호출 실패 · 분류 결과 · 피드백 저장 · MLflow 실패 · Watch 오류 횟수(`error_count`) |
| 화면 | `front/js/pages/monitoring.js`, `index.html`, `main.js`, `api.js`, `css/style.css` | 사이드 메뉴 [모니터링], 탭 [모델]·[플랫폼], iframe(sandbox) + [Grafana에서 열기 ↗], URL 없으면 "대시보드 준비 중" |
| 대시보드 | `monitoring/dashboards/build.py` → `mentor-model.json`(패널 20), `mentor-platform.json`(패널 16) | 4장 구성. `__inputs` `DS_PROMETHEUS`·`DS_MYSQL`, uid 고정, 템플릿 변수 없음, 기본 기간 24시간, 30초 갱신. 모델 교체 annotation(DB) |

### 9-2. 설계와 달라진 점
| 항목 | 설계 | 구현 | 이유 |
|---|---|---|---|
| `mentor_platform_actions_total` 의 `result` | ok / error | **ok / rejected(4xx) / error(5xx)** | "확인 없음·충돌" 같은 거절과 실제 장애를 구분 |
| 운영 행위 종류 | 10종 | **+ `manifest_create`, `feedback_label`** (12종) | 매니페스트 새 파일, 사람 정답 입력도 상태 변경 |
| 운영 행위 집계 위치 | 각 기능 코드 | **미들웨어 한 곳** (메서드 + 라우트 경로 → action) | 기존 기능 코드를 건드리지 않음. 표: `metrics.ACTION_ROUTES` |
| 모델 호출 실패 `reason` | timeout / connect / http_4xx / http_5xx | **+ `invalid`** (응답 형식 오류) | |
| MLflow 실패 라벨 | `op` | `op` = REST 경로 (예: `model-versions/search`). 없는 alias(정상 응답)는 세지 않음 | |
| 확률 분포 패널 | 히스토그램 | 막대 (`groupingToMatrix` 로 버전별 열), `+Inf` 버킷 제외 | 공개 대시보드에서 변수 없이 버전 비교 |

### 9-3. 확인한 것
- 테스트: server **147개** (기존 130 + 신규 17 `tests/test_metrics.py`), model **28개** (기존 25 + 신규 3) 통과
- 로컬 API(실제 클러스터 Watch, 조회만): `/metrics` 에 mentor 네임스페이스 실제 파드·Deployment 값, `/api/v1/monitoring` 응답 확인
- 대시보드 PromQL **32개 전부** 실제 Prometheus 에 질의 → 문법 오류 없음 (cAdvisor 패널은 실제 데이터 반환, 나머지는 수집 전이라 0건)
- 메트릭 라벨에 댓글 원문이 들어가지 않음 (테스트)

### 9-4. 확인하지 못한 것
| 항목 | 이유 | 확인 시점 |
|---|---|---|
| 화면 렌더링 (브라우저) | Chrome 확장 연결 끊김 — JS 문법 검사·API 응답까지만 확인 | 로컬 실행 또는 배포 후 |
| MySQL 패널 쿼리 실행 | DB 비밀번호 없음 — 스키마(`db/schema.sql`) 대조까지만 | 인수인계 6-1 데이터소스 등록 후 |
| Grafana import · 공개 대시보드 표시 | Grafana 관리자 권한 없음 | 인수인계 6장 |
| 이미지 빌드 | Docker Desktop 꺼져 있음 | 인수인계 1-1 |
