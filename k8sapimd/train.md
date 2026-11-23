# 모델 재학습 · 모델 교체 — MLflow 연동 설계 방안 (구현 전 제출)

> 상위 문서: `job_plan.md` (특히 8장 MLflow 연동). 선행 기능: `dataset.md` (① 데이터셋 재생성 — 구현·배포 완료).
> 이 문서는 **② 모델 재학습** 과 **③ 모델 교체** 를 MLflow 기준으로 다시 검토·설계한다.
> 이번 단계: 설계만 (코드 수정 없음). 14장 질의 확정 후 구현.
> 확정 사항 (2026-11-23):
> | # | 결정 |
> |---|---|
> | 1 | 평가: seed 테스트 분할 92행 = 고정 `eval.csv`, 학습에서 제외, 학습은 나머지 100% |
> | 2 | 정리: 중복 제거(마지막=최신 피드백 우선), **라벨당 최소 20행** |
> | 3 | 피드백 가중치 **기본 3** (실측 근거 4-4) |
> | 4 | 기준 모델(버전 1) 전환 시 판정이 미세하게 바뀌는 것 **허용** |
> | 5 | 데이터셋 CSV 를 MLflow 에 **복사하지 않음** (json 요약만) |
> | 6 | MLflow 노출 정리(ClusterIP + Ingress)는 **시연 후로 미룸** — 알려진 한계로 기록 |
> | 7 | 교체 완료 감지·alias 이동은 **화면 조회 시점에 계산** (백그라운드 작업 없음) |
> 기준일: 2026-11-23 — 클러스터 값은 읽기 전용으로 확인, MLflow 동작은 로컬 PoC 로 확인 (2장)

---

## 0. 이어받는 결정 (변경 없음)

| 출처 | 결정 |
|---|---|
| job_plan 2장 | CPU 학습으로 충분 (467행 0.3초 수준), 모델 수 MB 이하 |
| job_plan 3-2 · 8-3 | **교체 = 워크로드 YAML 의 버전 번호 변경 → Work 3 apply** (alias 이동·직접 patch 아님) |
| job_plan 8-2 | 데이터셋 CSV 는 NFS, **모델 파일·지표는 MLflow** (NFS `models/` 없음) |
| job_plan 8-4 · 8-8 | 서빙 파드는 **initContainer 가 MLflow 에서 모델 다운로드** → `emptyDir` |
| job_plan 8-8 | MLflow 저장소 hostPath(worker-1) 유지, "알려진 한계"로 발표 |
| dataset.md | ① 은 MLflow 와 무관, 정리·검사 없이 `seed + DB` 를 이어 붙임 → **정리는 ② 에서** |
| dataset.md 5-3 | Job 명세는 코드가 아니라 **NFS 매니페스트 폴더의 템플릿** → ② 도 같은 방식 |

---

## 1. 다시 검토해서 나온 것 (요약)

| # | 발견 | 영향 | 대응 (장) |
|---|---|---|---|
| R1 | 데이터셋에 **seed 전체가 항상 들어감** → seed 에서 고정 평가셋을 떼면 평가 문장이 학습에도 들어감 (**누수**) | 지표가 실제보다 높게 나오고, 모델 간 비교가 무의미 | 학습 Job 이 평가셋 문장을 학습 데이터에서 **제외** (4-2) |
| R2 | 지금 지표 = 매번 그 데이터의 20% 무작위 분할 | 데이터가 바뀌면 시험지도 바뀜 → MLflow 비교 화면이 의미 없음 | **고정 평가셋 `eval.csv` 하나로만 채점**, 학습은 나머지 전부 사용 (4-3) |
| R3 | 같은 문장이 seed·DB 에 중복/다른 라벨로 들어감 (① 결정) | 같은 문장 두 번 학습, 정답 충돌 | 학습 Job 에서 `text` 기준 **마지막 행 우선**(= DB 피드백이 seed 를 덮음) 중복 제거 (4-2) |
| R4 | 피드백 비중이 매우 작음 (현재 467행 중 DB **9행**) | 재학습해도 모델이 거의 안 바뀜 → 시연 효과 약함 | 파라미터 `feedback_weight` **기본 3** (4-4 실측) |
| R5 | 라벨 한쪽이 거의 없으면 학습 불가 | Job 이 알아보기 힘든 오류로 실패 | 라벨별 **최소 행 수 검사** 후 명확한 메시지로 실패 (4-2) |
| R6 | 등록 모델 **버전 번호는 등록 후에야** 정해짐 → `model_info.json.version` 에 미리 못 씀 | DB `model_version` 에 어떤 모델이 판정했는지 기록 못 함 | initContainer 가 받은 **`MODEL_URI` 를 파일로 남기고** `serve.py` 가 그것을 버전으로 보고 (6-3) |
| R7 | 지금 서빙 중인 `model_v1` 모델은 **MLflow 에 없음** (등록 모델 0개 확인) | 교체·롤백의 기준점이 없음 | seed.csv 로 학습 Job 1회 실행 → **등록 버전 1 = 기준 모델** 로 서빙 전환 (12장 3단계) |
| R8 | 학습 이미지·서빙 이미지가 따로 빌드되면 sklearn 버전이 어긋날 수 있음 | 가이드 08장 시나리오 3 | 둘 다 `model/requirements.txt` 를 공유하는 **같은 Dockerfile 의 두 target** (5-1) + 교체 전 버전 검사 (7-3) |
| R9 | `activeDeadlineSeconds` 로 Job 이 강제 종료되면 MLflow run 이 **RUNNING 으로 남음** | MLflow 화면에 끝나지 않은 run | API 가 Job 실패를 보면 해당 run 을 `FAILED` 로 정리 (8-1) |
| R10 | MLflow 가 NodePort·무인증 → 내부망 누구나 모델 파일 덮어쓰기 가능 | 서빙 파드가 받는 pickle = 코드 실행 경로 | ClusterIP + Ingress(IP 제한) — **시연 후로 미룸** (11장), 그때까지 알려진 한계 |

---

## 2. PoC 결과 (로컬, MLflow 3.18.0 · sklearn 1.9.1 — 클러스터와 같은 버전)

로컬에 클러스터와 같은 구성(`--backend-store-uri sqlite` + `--artifacts-destination`, 서버 경유 업로드)으로 서버를 띄워 확인. 클러스터 MLflow 는 건드리지 않음.

| 확인 항목 | 결과 |
|---|---|
| `mlflow.sklearn.log_model(..., name="model", serialization_format="pickle")` → `mlflow.register_model()` | ✅ 등록 모델 `toxicity-filter` 버전 1, 2 생성 |
| MLflow 3 의 저장 구조 | 모델은 run 아래가 아니라 **LoggedModel**(`models:/m-<id>`) 로 저장 → `artifacts/<실험>/models/m-<id>/artifacts/` |
| `mlflow artifacts download --artifact-uri models:/toxicity-filter/2 --dst-path dl` (CLI) | ✅ `MLmodel, model.pkl, requirements.txt, …` 다운로드 → **initContainer 명령 그대로 사용 가능** |
| `joblib.load("model.pkl")` | ✅ 로드·`predict_proba` 정상 → `serve.py` 로드 코드 변경 불필요 (경로만) |
| 모델 폴더에 `model_info.json` 추가 | ✅ `MlflowClient().log_model_artifact(model_id, path)` → 다운로드 결과에 포함 → `/info`·`version_mismatch` 유지 가능 |
| 학습 환경 기록 | ✅ `requirements.txt` 에 `scikit-learn==1.9.1` 등 자동 기록 → 교체 전 버전 검사에 사용 |
| 데이터셋 기록 `mlflow.log_input(mlflow.data.from_pandas(...))` | ✅ run 의 Datasets 에 이름·digest 표시. digest 는 MLflow 자체 해시(8자리)라 **우리 sha256 은 태그로 따로** 남김 |
| 주의 | Windows 콘솔(cp949)에서 run 종료 시 이모지 출력 오류 → 리눅스 Job 에는 무관, 로컬 개발 시 `PYTHONUTF8=1` |

### 2-1. `mlflow-skinny` 점검 (2026-11-23, Docker — 운영과 같은 조건)
`python:3.11-slim` + `model/requirements-train.txt` + `mlflow-skinny==3.18.0` 로 테스트 이미지를 만들어(push 안 함, 점검 후 삭제) 로컬 MLflow 3.18.0 서버에 연결.
조건: 루트 파일시스템 읽기 전용, `HOME=/tmp`(tmpfs), 학습 = uid 1000 / initContainer = uid 10001.

| 쓰는 곳 | 기능 | 결과 |
|---|---|---|
| 학습 Job | `set_experiment` (없으면 생성) | ✅ |
| 학습 Job | `mlflow.data.from_pandas` + `log_input` (training · eval) | ✅ |
| 학습 Job | params · metrics · tags · run artifact | ✅ |
| 학습 Job | `mlflow.sklearn.log_model(serialization_format="pickle")` | ✅ |
| 학습 Job | `log_model_artifact` (model_info.json 추가) | ✅ |
| 학습 Job | `register_model` + 버전 태그 + alias | ✅ |
| initContainer | `mlflow artifacts download --artifact-uri models:/toxicity-filter/1` → `/models` + `SOURCE` 기록 | ✅ 1초, 파일 9개 (model.pkl 72KB) |
| initContainer | 없는 버전(99) | ✅ exit 1, `RESOURCE_DOES_NOT_EXIST` → 새 파드 `Init:Error` 가 되는 동작과 일치 |
| 추론 서버 | **기존 `model_v1` 이미지 그대로** `MODEL_PATH=/models/model.pkl` | ✅ `/info` 에 model_info.json 내용·`version_mismatch: false`, `/predict` 정상 |
| 플랫폼 API (REST) | `model-versions/search` (버전·상태·태그), `registered-models/alias` (champion 조회) | ✅ ※ search 응답에는 alias 가 비어 있음 → champion 은 alias 조회로 따로 확인 |

이미지 크기: **skinny 596MB / full `mlflow` 962MB** (같은 구성, 압축 전) → skinny 채택. `requirements-job.txt` 에 `mlflow-skinny==3.18.0`.

구현 시 반영할 것 (점검 중 나온 경고):
| 경고 | 조치 |
|---|---|
| GitPython: git 실행 파일 없음 | 템플릿·initContainer env 에 `GIT_PYTHON_REFRESH=quiet` |
| `input_example` 로 signature 추론 실패 (`'int' object has no attribute 'lower'`) | `input_example` 넘기지 않음 — MLflow 서빙을 쓰지 않으므로 signature 불필요 |
| 데이터셋 source 해석 모호 · 정수 컬럼 안내 · pickle 주의 | 무해 (pickle 위험은 R10 으로 이미 관리) |
| 추론 서버 `serve.py` | **변경 필요는 `SOURCE` 읽기(6-3)뿐** — 로드 코드는 그대로 동작 확인 |

---

## 3. 전체 흐름

```
[화면] ② 모델 재학습 ── 데이터셋 선택 ── POST /api/v1/models/train {dataset} ──▶ [플랫폼 API]
                                                       실행 중 train Job 있으면 409
                                                       /manifests/job/train.yaml 읽어 DATASET env 주입 → create
                                                                 │
                    ┌────────────── Job train-xxxxx (job-train 이미지) ──────────────┐
                    │ ① /csv/<dataset>.csv + /csv/eval.csv 읽기 (ro)                  │
                    │ ② 정리: 평가셋 문장 제외 → 중복 제거(마지막 우선) → 최소 개수 검사   │
                    │ ③ 학습 (Pipeline 그대로) → eval.csv 로 채점                        │
                    │ ④ MLflow run 기록: params · metrics · dataset input · 태그           │
                    │ ⑤ log_model + model_info.json 추가 → register → 버전 N             │
                    │ ⑥ 종료 메시지 {"ok":true,"version":N,"run_id":…,"eval_f1":…}       │
                    └──────────────────────────────────────────────────────────────────┘
                                                                 │
[화면] ③ 모델 교체 ── 버전 N 선택 (지표·sklearn 호환 표시) ── POST /api/v1/models/deploy {version: N}
                         API: NFS workloads/toxicity-filter.yaml 의 MODEL_URI 한 줄 수정 → 저장
                              → Work 3 apply (SSA) → 롤링 업데이트
                                   새 파드: initContainer 가 models:/toxicity-filter/N 다운로드
                                            → serve.py 로드 → Ready → 옛 파드 종료
                              → 롤아웃 완료 확인 후 MLflow alias `champion` = N (표시용)
                         실패(Init:Error·CrashLoop) → 옛 파드 계속 서빙, 화면에 실패 + [되돌리기]
```

---

## 4. 학습 Job 의 데이터 처리

### 4-1. 입력
| 파일 | 위치 | 비고 |
|---|---|---|
| 학습 데이터셋 | `/csv/dataset-YYYYMMDD-HHMMSS.csv` (선택한 것) | ① 의 산출물. 같은 이름 `.json` 의 sha256 과 대조해 손상 여부 확인 |
| 고정 평가셋 | `/csv/eval.csv` (`label,text`) | **사람이 1회 배치 후 바꾸지 않음** (4-3). sha256 을 run 태그로 남김 |

- 마운트: `mentor-csv` PVC 를 **읽기 전용** (학습 Job 은 NFS 에 쓰지 않음 → uid 제약 없음)
- `seed.csv` 를 따로 읽지 않음 — 데이터셋에 이미 들어 있음

### 4-2. 정리 순서 (① 에서 하지 않기로 한 것을 여기서)
| 순서 | 처리 | MLflow 기록 |
|---|---|---|
| 1 | `text` 앞뒤 공백 제거, 빈 문장 제거 | `rows_raw` |
| 2 | **평가셋에 있는 문장 제거** (R1, 누수 방지) | `rows_eval_excluded` |
| 3 | `text` 기준 중복 제거, **마지막 행 유지** → 데이터셋은 seed → DB(`labeled_at` 순) 순서라 최신 피드백이 이김 (R3) | `rows_dup_removed`, `rows_conflict` (라벨이 달랐던 문장 수) |
| 4 | 라벨별 최소 행 수 검사 (기본 **각 20행**, 템플릿 env `MIN_PER_LABEL`) → 부족하면 `{"ok":false,"error":"라벨 1 이 12행뿐 (최소 20)"}` 로 실패, **등록 안 함** (R5) | run 을 만들기 전에 검사 → MLflow 에 기록 없음 (종료 메시지로만) |
| 5 | DB 출신 행에 학습 가중치 `feedback_weight` (기본 **3**, R4 · 4-4) — `sample_weight` 로 전달 | param |

- "DB 출신 행" 구분: 데이터셋 `.json` 의 `sources.seed.rows` → 앞의 N행이 seed (① 이 순서를 보장)

### 4-3. 평가 방식 변경 (R2)
| | 지금 (`train.py`) | 변경 |
|---|---|---|
| 학습 | 데이터의 80% | 정리된 데이터 **100%** (평가셋은 이미 빠져 있음) |
| 채점 | 그 데이터의 20% 무작위 | **`eval.csv` 고정** → 모든 버전이 같은 시험지 |
| 지표 | f1 / precision / recall | `eval_f1`, `eval_precision`, `eval_recall`, `eval_accuracy` + `n_train`, `n_eval` |

- `eval.csv` 만드는 법: 현재 `seed.csv` 를 지금과 같은 설정(`test_size=0.2, random_state=42, stratify`)으로 나눈 **테스트 92행** → 기준 모델 지표가 지금 값과 이어짐
- 한계 (발표 때 언급): 평가셋이 seed 출신이라 "실제 서비스 댓글"에 대한 성능은 재지 못함. 피드백이 수백 건 쌓이면 피드백 일부로 두 번째 평가셋(`eval_feedback`)을 만드는 것이 다음 단계
- 임계값 `THRESHOLD`(0.5)는 그대로. 참고용으로 평가셋 기준 최적 임계값을 metric `best_threshold` 로만 기록

### 4-4. 피드백 가중치 실측 (2026-11-23, 결정 3 의 근거)
실제 데이터셋 `dataset-20261123-205655` (seed 458 + DB 9) 에 4-2 규칙 적용 → 학습 373행 (그중 피드백 8행), 평가 92행.

| 가중치 | eval F1 | 피드백 반영률 (학습한 피드백 문장을 정답대로 맞히는 비율) |
|---:|---:|---:|
| 1 | 0.942 | 75% (6/8) |
| 2 · 3 · 5 | 0.932 | 88% (7/8) |
| 10 | 0.920 | 100% (8/8) |

- 가중치 1 이면 사람이 고친 정답 중 1/4 을 모델이 여전히 틀림 → 재학습 의미가 약함
- 2~5 는 같은 결과(평탄 구간) → 가운데 **3** 선택. eval F1 차이 0.01 ≈ 평가 92행 중 1건
- 10 은 피드백을 외우는 대신 seed 기준 성능이 더 떨어짐 → 과적합 쪽
- 피드백 8행 기준이라 표본이 작음 → 값은 템플릿 env `FEEDBACK_WEIGHT` 로 바꿀 수 있고, run param 으로 남아 MLflow 에서 비교 가능. 피드백이 수백 건이 되면 다시 측정
- 지표 `fb_fit` (피드백 반영률) 도 run 에 함께 기록

---

## 5. 이미지 · Job 템플릿

### 5-1. 학습 이미지 — `model/` 에 두는 이유 (R8)
```
model/
├─ train.py            학습 함수 (Pipeline·PARAMS) — 로컬 학습·이미지 빌드에서 계속 사용
├─ train_job.py        (신규) Job 진입점: 4장 정리 → train.py 의 Pipeline 학습 → MLflow 기록·등록
├─ requirements.txt    서빙 = 공통 고정값 (sklearn 1.9.1 …)
├─ requirements-train.txt  + pandas
├─ requirements-job.txt    (신규) -r requirements-train.txt + mlflow-skinny==3.18.0
└─ Dockerfile          target: train(기존) / job-train(신규) / 서빙(기본)
```
| 이미지 | 태그 | 용도 |
|---|---|---|
| 학습 Job + initContainer | `suhannugul/mentor:job-train_v1` | python:3.11-slim (서빙과 같은 Python), uid 1000 |
| 서빙 | `suhannugul/mentor:model_v2` | 모델을 이미지에 넣지 않음, `MODEL_PATH=/models/model.pkl` |

- 두 이미지를 **같은 커밋에서 함께 빌드** → sklearn·numpy·scipy 버전 일치 보장
- `jobs/train/` 을 따로 만들지 않는 이유: `requirements.txt` 를 공유해야 하는데 Docker 빌드 컨텍스트가 `model/` 이라서
- `mlflow-skinny`(UI·서버 제외 클라이언트) 사용 — 필요한 기능 모두 동작 확인 (2-1), full 대비 이미지 약 370MB 작음

### 5-2. Job 템플릿 — NFS `job/train.yaml` (원본 레포 `model/job/train.yaml`)
dataset.md 5-3 과 **같은 규칙**: API 가 실행마다 읽어 `generateName: train-`, 네임스페이스·라벨 강제, 검사 실패 시 400.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: train
  labels:
    app.kubernetes.io/part-of: mlops-platform
    platform.mentor/job-type: train
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 600
  ttlSecondsAfterFinished: 600                 # 기록은 MLflow 에 남으므로 Job 은 10분 뒤 삭제
  template:
    metadata:
      labels: {platform.mentor/job-type: train}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsUser: 1000, runAsGroup: 1000, runAsNonRoot: true}
      containers:
        - name: train
          image: suhannugul/mentor:job-train_v1
          command: ["python", "train_job.py"]
          env:
            - {name: DATASET, value: ""}         # ← API 가 선택한 데이터셋 이름으로 채움
            - {name: CSV_DIR, value: /csv}
            - {name: EVAL_FILE, value: eval.csv}
            - {name: MIN_PER_LABEL, value: "20"}
            - {name: FEEDBACK_WEIGHT, value: "3"}
            - {name: GIT_PYTHON_REFRESH, value: quiet}
            - {name: MLFLOW_TRACKING_URI, value: http://mlflow.mentor.svc.cluster.local:5000}
            - {name: MLFLOW_EXPERIMENT_NAME, value: toxicity-filter}
            - {name: HOME, value: /tmp}
            - name: JOB_NAME
              valueFrom: {fieldRef: {fieldPath: "metadata.labels['job-name']"}}
          terminationMessagePolicy: FallbackToLogsOnError
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}, readOnlyRootFilesystem: true}
          resources:
            requests: {cpu: 500m, memory: 512Mi}
            limits:   {cpu: "1",  memory: 1Gi}
          volumeMounts:
            - {name: csv, mountPath: /csv, readOnly: true}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - name: csv
          persistentVolumeClaim: {claimName: mentor-csv, readOnly: true}
        - name: tmp
          emptyDir: {sizeLimit: 256Mi}
```
- API 가 주입하는 값은 **`DATASET` 하나뿐** (`^dataset-\d{8}-\d{6}$` 검사 + 파일 존재 확인). 하이퍼파라미터는 화면에서 받지 않음 — 바꾸려면 템플릿 env 수정 (기록은 MLflow param 으로 남음)
- DB 접속 정보 불필요 (ConfigMap·Secret 주입 없음)

---

## 6. MLflow 기록 구조

### 6-1. 실험 · run
| 항목 | 값 |
|---|---|
| 실험 | `toxicity-filter` (없으면 Job 이 생성. `Default` 사용 안 함) |
| run 이름 | Job 이름 `train-xxxxx` → 쿠버네티스 Job ↔ MLflow run 을 이름으로 연결 |
| params | `tfidf_ngram`, `tfidf_min_df`, `max_iter`, `min_per_label`, `feedback_weight` |
| metrics | `eval_f1`·`eval_precision`·`eval_recall`·`eval_accuracy`, `best_threshold`, `fb_fit`, `n_train`, `n_eval`, `rows_raw`, `rows_eval_excluded`, `rows_dup_removed`, `rows_conflict`, `rows_feedback` |
| input | `mlflow.log_input(from_pandas(정리된 df, source="/csv/<dataset>.csv", name=<dataset>), context="training")` + eval 도 `context="eval"` |
| 태그 | `dataset`, `dataset_sha256`, `eval_sha256`, `seed_sha256`, `k8s_job`, `image` (`job-train_v1`), `trigger=platform` |
| artifacts | 데이터셋 요약 `<dataset>.json` (수백 B)만. **CSV 원본은 복사하지 않음** (결정 5 — NFS 에 남고, sha256 태그로 동일성 확인) |

### 6-2. 모델 · 등록
| 단계 | 내용 |
|---|---|
| 저장 | `mlflow.sklearn.log_model(model, name="model", serialization_format="pickle")` (`input_example` 없음 — 2-1) |
| 부가 파일 | `model_info.json` (기존 형식 + `run_id`, `dataset`) → `MlflowClient().log_model_artifact(model_id, …)` (PoC 확인) |
| 등록 조건 | 학습·채점·기록이 **모두 성공한 경우에만** `register_model(model_uri, "toxicity-filter")` — 지표 기준으로 막지 않음 (좋고 나쁨은 사람이 ③ 에서 판단) |
| 버전 태그 | `dataset`, `eval_f1`, `sklearn_version`, `k8s_job` → 교체 화면이 run 을 따로 조회하지 않아도 되게 |
| alias | `champion` = **지금 실제로 서빙 중인 버전** (플랫폼이 롤아웃 성공 후에만 이동). 학습 Job 은 alias 를 건드리지 않음 |

### 6-3. "어떤 모델이 판정했는가" 기록 (R6)
- initContainer 가 다운로드 후 `MODEL_URI` 를 `/models/SOURCE` 파일에 씀 (`models:/toxicity-filter/3`)
- `serve.py` 의 `load_info()` : `SOURCE` 가 있으면 `info["version"] = "toxicity-filter/3"` 로 덮어씀 (`model_info.json` 의 지문은 `info["build"]` 로 보존)
- → 플랫폼이 DB `model_version` 에 `toxicity-filter/3` 저장 (VARCHAR 64 충분) → **버전별 정확도 집계**가 피드백 테이블에서 바로 가능
- 숫자를 YAML 두 곳(initContainer·서빙 env)에 쓰지 않는 이유: 한 곳만 고치면 표시와 실제가 어긋남 → **진실은 `MODEL_URI` 한 줄**

---

## 7. ③ 모델 교체

### 7-1. 서빙 워크로드 변경 (`workloads/toxicity-filter.yaml`, 1회)
지금 파일(NFS)에서 바뀌는 부분만:
```yaml
    spec:
      initContainers:
        - name: fetch-model
          image: suhannugul/mentor:job-train_v1
          command: ["sh", "-c",
            "mlflow artifacts download --artifact-uri \"$MODEL_URI\" --dst-path /models && echo \"$MODEL_URI\" > /models/SOURCE"]
          env:
            - {name: MODEL_URI, value: "models:/toxicity-filter/1"}   # platform.mentor/model-uri  ← 교체 = 이 줄만
            - {name: MLFLOW_TRACKING_URI, value: http://mlflow.mentor.svc.cluster.local:5000}
            - {name: HOME, value: /tmp}
            - {name: GIT_PYTHON_REFRESH, value: quiet}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits:   {cpu: 500m, memory: 512Mi}
          volumeMounts:
            - {name: model, mountPath: /models}
            - {name: tmp, mountPath: /tmp}
      containers:
        - name: app
          image: suhannugul/mentor:model_v2                          # ← model_v1 → v2
          env:
            - {name: MODEL_PATH, value: /models/model.pkl}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
            - {name: model, mountPath: /models, readOnly: true}
      volumes:
        - name: tmp
          emptyDir: {sizeLimit: 16Mi}
        - name: model
          emptyDir: {sizeLimit: 64Mi}
```
- 파드 uid 10001 그대로 (initContainer 도 같은 uid → emptyDir 쓰기 가능, NFS 미사용)
- initContainer 가 노드에 처음 받는 이미지는 학습 이미지(수백 MB) → 첫 교체만 느림, `IfNotPresent` 로 이후 재사용

### 7-2. 교체 API 동작 (`POST /api/v1/models/deploy {version}`)
1. MLflow 에서 버전 존재·상태 `READY` 확인
2. **호환 검사** (7-3) 실패 → 409 + 이유 (버튼도 비활성)
3. 실행 중인 교체(롤아웃 진행 중)가 있으면 409
4. NFS `workloads/toxicity-filter.yaml` 을 **텍스트로** 읽어 주석 표식 `# platform.mentor/model-uri` 가 붙은 줄 **한 줄만** 값 치환
   - PyYAML 로 다시 쓰면 주석·순서가 사라짐 → 한 줄 치환 후 **YAML 파싱으로 결과 검증** (표식이 0개·2개 이상이면 400)
   - 저장은 Work 3 `ManifestStore` 저장 경로 재사용 (이력·권한 동일)
5. Work 3 `apply_file("workloads/toxicity-filter.yaml")` 호출 → 판정은 항상 **"일치"**
6. 응답 202 → 화면은 Deployment·파드 Watch 캐시로 진행 표시 (Work 1 재사용)
7. 롤아웃 완료 감지·alias 이동은 **`GET /api/v1/models/deploy` 가 불릴 때 계산** (결정 7, 백그라운드 작업 없음)
   - 완료 = Deployment `observedGeneration` 최신 + `updatedReplicas == readyReplicas == replicas` + 옛 파드 0 → 이때 `champion` ≠ 선언 버전이면 alias 이동
   - 화면이 진행 중에는 몇 초마다 조회하므로 실질적으로 바로 반영. 화면을 닫아 두면 다음 조회 때 이동 (표시용이라 무해)

| 상황 | 결과 | 화면 |
|---|---|---|
| 다운로드 실패 (MLflow 꺼짐·버전 없음) | 새 파드 `Init:Error`, 옛 파드 계속 서빙 | "교체 실패 — 이전 모델 계속 서빙" + [되돌리기] |
| 모델 로드 실패 | 새 파드 CrashLoop, 옛 파드 유지 (`maxUnavailable: 0`) | 같음 |
| [되돌리기] | 현재 `champion` 버전으로 YAML 을 다시 고쳐 apply → 실패한 새 ReplicaSet 정리 | 롤백 = 선언 되돌리기 (발표 포인트) |

### 7-3. 교체 전 검사
| 검사 | 근거 | 결과 |
|---|---|---|
| sklearn 버전 | 버전 태그 `sklearn_version` ≠ 서빙 `/info.runtime_sklearn_version` | **차단** (가이드 08장 시나리오 3) |
| 지표 | 선택 버전 `eval_f1` < champion `eval_f1` | **경고만** (확인 대화상자에 차이 표시) |
| 평가셋 | `eval_sha256` 이 champion 과 다름 | 경고 "다른 평가셋 — 지표 비교 불가" |
| 같은 버전 | 이미 서빙 중 | 버튼 비활성 |

---

## 8. 플랫폼 API · 화면

### 8-1. API (경로는 `/api/v1/datasets` 와 같은 급)
| Method | Path | 설명 |
|---|---|---|
| POST | `/api/v1/models/train` | `{dataset}` → 학습 Job. 실행 중이면 409 |
| GET | `/api/v1/models/train/jobs` | `job-type=train` Job 상태 + 종료 메시지 (Watch 캐시). Job 이 Failed 이고 run 이 RUNNING 이면 MLflow 에 `FAILED` 로 갱신 (R9) |
| GET | `/api/v1/models` | 등록 모델 버전 목록 (MLflow REST `model-versions/search`) + 태그(지표·데이터셋·sklearn) + `serving`(YAML 의 MODEL_URI 기준) + `champion` + 호환 여부 |
| POST | `/api/v1/models/deploy` | `{version}` → 7-2 |
| GET | `/api/v1/models/deploy` | 현재 선언 버전·실제 파드 버전(`/info`)·롤아웃 상태 |

- MLflow 호출은 **httpx 로 REST 직접** (API 이미지에 mlflow 패키지 추가 안 함 — 이미지 크기·의존성). 필요한 엔드포인트: `model-versions/search`, `registered-models/alias`(set), `runs/update`
- ConfigMap `mentor-platform-config` 에 `MLFLOW_TRACKING_URI` 추가, `MLFLOW_UI_URL`(화면 링크용) 추가
- MLflow 가 응답하지 않으면 `GET /models` 는 503 + "MLflow 연결 불가" (다른 화면에는 영향 없음)
- 코드 위치: `server/app/models/` (`mlflow_client.py`, `train_job.py` — `datasets/job.py` 의 템플릿 처리 **공통화**, `deploy.py`) + `api/models.py`

### 8-2. 화면 (자리표시 탭 2개 채우기)
```
┌ 모델 재학습 ─────────────────────────────────────────────────────────┐
│ 데이터셋 [dataset-20261123-205655 ▾] (467행 · seed 458 + DB 9)   [학습 시작] │
│ 실행 상태: train-k2x9q ● 실행 중 (4초)                                  │
│                                                                      │
│ 버전  데이터셋                  eval F1  학습행  sklearn  상태      MLflow │
│  3   dataset-20261123-205655   0.912    371    1.9.1   ● 서빙 중   ↗   │
│  2   dataset-20261122-183011   0.905    368    1.9.1              ↗   │
│  1   seed                      0.903    366    1.9.1              ↗   │
└──────────────────────────────────────────────────────────────────────┘
┌ 모델 교체 ───────────────────────────────────────────────────────────┐
│ 선언: models:/toxicity-filter/3   실제 파드: toxicity-filter/3  ✔ 일치    │
│ 버전 [2 ▾]  eval F1 0.905 (현재 대비 -0.007 ⚠)              [교체]       │
│ 진행: 새 파드 fetch-model 완료 → Ready → 옛 파드 종료 ✔                   │
└──────────────────────────────────────────────────────────────────────┘
```
- 버전 비교·학습 곡선 같은 상세는 **MLflow UI 링크(↗)** 로 넘김 — 비교 화면을 직접 만들지 않음 (MLflow 를 쓰는 이유)

---

## 9. Work 1~3 · ① 과의 관계

| 기능 | 영향 |
|---|---|
| Work 1 Watch | train Job·파드, toxicity-filter 롤아웃 자동 표시 — 변경 없음 |
| Work 3 매니페스트 | `job/train.yaml` "없음" 정상 (dataset 과 같음). `workloads/toxicity-filter.yaml` 은 교체 후에도 **"일치"** |
| Work 3 직접 편집 | 사람이 화면에서 `MODEL_URI` 를 고쳐 apply 해도 같은 결과 → 교체 화면은 "편의 기능"일 뿐, 진실은 YAML |
| ① 데이터셋 | 변경 없음. 학습 Job 이 `.json` 의 `sources.seed.rows`·`sha256` 을 사용 |
| Role | `batch/jobs create` 있음. Deployment apply 는 Work 3 가 이미 수행 → 추가 권한 없음 |

---

## 10. 시연 시나리오 (발표용)

1. 기준: 서빙 `toxicity-filter/1` (seed), 판정 "일치"
2. 댓글 몇 건 입력 → 오답에 피드백(정답) 입력
3. ① 데이터셋 생성 → ② 학습 → MLflow UI 에서 run 2개 비교 (같은 평가셋)
4. ③ 버전 2 로 교체 → 롤링 업데이트 관찰 (initContainer → Ready → 옛 파드 종료) → 같은 댓글 결과 변화, DB `model_version` 이 `toxicity-filter/2`
5. **셀프힐링**: 존재하지 않는 버전 99 를 YAML 에 직접 적어 apply → 새 파드 `Init:Error`, 서비스는 계속 응답 → [되돌리기] 로 복구
6. 피드백 화면에서 버전별 정확도 비교

---

## 11. MLflow 서버 변경 — **사람이 플랫폼 화면에서** (레포 작업 아님)

### 11-1. 누가, 어디서 바꾸나
`mlflow` 는 infra_guide 0장 5·9장에 따라 **플랫폼이 관리하는 워크로드**이고, 선언은 NFS `workloads/mlflow.yaml` 이다.
→ 이 레포(코드·이미지·설계)에서는 바꿀 수 없고, 바꾸지도 않는다. 레포에서는 **바꿀 내용만** 이 문서에 적는다.

| 방법 | 가능 여부 |
|---|---|
| **매니페스트 관리 화면에서 `workloads/mlflow.yaml` 수정 → 저장 → [적용]** (사람) | ✅ 기준 방법. 판정 "일치" 유지 |
| 최상위 관리 디렉터리에서 kubectl | 최상위에 `mlflow` YAML 이 없으므로 대상 아님 (최상위는 플랫폼 자신·PV·Secret 용) |
| 이 레포에서 kubectl 로 직접 변경 | ❌ 선언(NFS)과 실제가 어긋나 판정 "불일치" + 역할 규칙 위반 |

### 11-2. 바꿀 내용 (`workloads/mlflow.yaml`, Deployment `mlflow` 의 컨테이너)
```yaml
          image: ghcr.io/mlflow/mlflow@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13   # 지금 실행 중인 3.18.0 그대로 고정
          imagePullPolicy: IfNotPresent
          readinessProbe:
            httpGet: {path: /health, port: 5000}
            periodSeconds: 10
            failureThreshold: 3
          livenessProbe:
            httpGet: {path: /health, port: 5000}
            initialDelaySeconds: 30
            periodSeconds: 20
            failureThreshold: 3
```
- 태그 대신 **digest** 로 고정: 지금 `latest` 로 받아 실행 중인 바로 그 이미지(`mlflow --version` = 3.18.0, 2026-11-23 확인) → 동작 변화 없이 "다음 재시작 때 버전이 바뀌는 위험"만 제거
  - 태그(`v3.18.0`)로 쓰고 싶으면 적용 전에 `docker manifest inspect ghcr.io/mlflow/mlflow:v3.18.0` 의 digest 가 위 값과 같은지 확인
- 적용 시 파드가 재시작됨 → hostPath(worker-1) 고정이라 같은 노드에 다시 뜨고 데이터 유지. 재시작 동안 MLflow 화면만 잠시 끊김 (지금은 추론 서버가 MLflow 를 안 쓰므로 댓글 기능 영향 없음) → **서빙 전환(12장 4단계) 전에** 해 두는 이유
- `--allowed-hosts` 등 args·볼륨은 그대로

| 우선 | 항목 | 이유 |
|---|---|---|
| 필수 | 위 이미지 고정 + probe | 클라이언트 3.18.0 과 일치, initContainer 가 MLflow 에 의존 (7-2) |
| 불필요 | 실험 `toxicity-filter` 사전 생성 | 학습 Job 이 만듦 |
| 시연 후 | NodePort → ClusterIP + Ingress (결정 6) | R10 알려진 한계 |
| 유지 | hostPath(worker-1) · SQLite | job_plan 8-8 |

---

## 12. 작업 순서 (누가 하는지 구분)

| 단계 | 담당 | 내용 |
|---|---|---|
| 1 | **사람 (플랫폼 화면)** | 11-2 MLflow 이미지 고정·probe → 적용 → 판정 "일치", MLflow UI 정상 확인 |
| 2 | 레포 | `train_job.py` + 테스트 → 이미지 `job-train_v1`, `serve.py`(SOURCE 읽기) → `model_v2` (빌드·push) |
| 3 | 레포 | API (템플릿 공통화·학습·모델 목록·교체·되돌리기) → `platform-api_v4`, 화면 → `platform-web_v3`, **인수인계 문서** 작성 |
| 4 | **최상위 Claude Code** (인수인계 문서) | ConfigMap `MLFLOW_TRACKING_URI`·`MLFLOW_UI_URL` 추가, API·화면 이미지 교체 (플랫폼 자신이라 최상위 kubectl) |
| 5 | **사람** | NFS `/csv/eval.csv` 배치, 매니페스트 화면 [+ 새 파일] `job/train.yaml` (레포 원본 붙여넣기) |
| 6 | **사람 (플랫폼 화면)** | 기준 모델 등록 (R7): [모델 재학습] 탭에서 데이터셋 **`seed`** 선택 → 학습 → 등록 버전 1 (결정 4: 판정 미세 변화 허용). kubectl 불필요 |
| 7 | **사람 (플랫폼 화면)** | 서빙 전환: `workloads/toxicity-filter.yaml` 에 7-1 반영 (`models:/toxicity-filter/1`, `model_v2`) → 적용 → 댓글 정상·`/info` 버전 `toxicity-filter/1` 확인 |
| 8 | 사람 | 10장 시나리오 확인 |

- 학습 Job 은 `DATASET=seed` 도 허용 (`.json` 이 없으므로 seed 전체를 seed 행으로 간주) → 화면 데이터셋 목록 맨 아래에 `seed` 를 함께 표시
- 7단계 전까지 [모델 교체] 탭은 "서빙이 아직 MLflow 방식이 아님 (`MODEL_URI` 표식 없음)" 으로 비활성 — 7-2 의 표식 검사로 자연스럽게 처리
- 레포에서는 클러스터를 **읽기 전용으로만** 확인 (지금까지와 같음)

---

## 13. 하지 않는 것

| ❌ | 이유 |
|---|---|
| alias(`@champion`) 로 서빙 모델 결정 (`models:/toxicity-filter@champion`) | 쿠버네티스가 변경을 모름 → 선언에 흔적 없음 (job_plan 8-3) |
| 지표가 좋으면 자동 교체 | 서비스 영향 → 사람이 판단. 자동화는 ①→② 까지만 (CronJob 은 선택) |
| 서빙 이미지에 mlflow 설치 / `/reload` API | 서빙은 파일 경로만 알면 됨, 선언과 실제가 어긋남 |
| API 가 Deployment 를 직접 patch | Work 3 판정 "불일치" (job_plan 3-2) |
| 하이퍼파라미터 화면 입력 | 범위 밖. 템플릿 env 로 충분, MLflow 에 기록됨 |
| 등록 모델 삭제 기능 | 서빙 중·롤백 대상 보호가 복잡. 수 MB 라 보관 부담 없음 |

---

## 14. 질의

- 없음 (2026-11-23 결정 1~7 로 모두 확정, 문서 상단 표). `mlflow-skinny` 점검 완료 (2-1).

---

## 15. 구현 결과 (2026-11-23)

### 만든 것
| 영역 | 파일 | 내용 |
|---|---|---|
| 학습 | `model/train_job.py` | 4장 정리 규칙 · 가중치 · eval.csv 채점(`best_threshold`·`fb_fit` 포함) · MLflow run/모델/`model_info.json`/등록/버전 태그 · 종료 메시지 |
| 학습 | `model/train.py` | `build_model()`·`split_eval()` 분리 (로컬 학습 결과는 그대로) |
| 학습 | `model/make_eval.py`, `model/data/eval.csv` | 고정 평가셋 92행 (기존 train.py 평가 분할과 같은 문장) |
| 서빙 | `model/serve.py` | `/models/SOURCE` → 버전 `toxicity-filter/<N>`, 지문은 `build` |
| 이미지 | `model/Dockerfile`, `requirements-job.txt` | target `job-train`(skinny 3.18.0) / 기본 서빙(모델 없음, `/models/model.pkl`) |
| 선언 | `model/job/train.yaml`, `model/workload/toxicity-filter.yaml` | 학습 Job 템플릿 / initContainer 방식 서빙 워크로드 (표식 줄 1개) |
| API | `server/app/datasets/job.py` | `TemplateJobs` 로 일반화 (데이터셋·학습 공통, `set_env`) — 데이터셋 동작 변경 없음 |
| API | `server/app/models/` (`mlflow_rest.py`, `train.py`, `deploy.py`, `service.py`), `api/models.py`, `main.py` | 8-1 의 라우터 5개, 롤아웃 판정·champion 이동(조회 시), 적용 실패 시 파일 원복, R9 run 정리 |
| 화면 | `front/js/pages/retrain.js`, `js/api.js`, `css/style.css` | [모델 재학습]·[모델 교체] 탭 |
| 문서 | 레포 최상위 `train_인수인계.md`, `model/README.md` | 적용 절차 |

### 이미지 (Docker Hub `suhannugul/mentor`, push 완료)
| 태그 | digest | 크기 |
|---|---|---|
| `job-train_v1` | `sha256:18bb0c504b8e…` | 585MB |
| `model_v2` | `sha256:0e0fa79a45f6…` | 412MB |
| `platform-api_v4` | `sha256:8f655acb14af…` | 283MB |
| `platform-web_v3` | `sha256:bf473e271ea4…` | 48.5MB |

### 확인한 것
- 테스트: model 25개, server 130개 (기존 101 + 신규 29) 통과
- 로컬 MLflow 3.18.0 + 실제 데이터셋(`dataset-20261123-205655`)으로 `train_job.py` 실행 → 버전 1(seed) eval F1 **0.9245** (= 지금 `model_v1` 과 같은 값 → 결정 4 의 "판정 변화"는 실제로 없음), 버전 2(데이터셋) eval F1 0.932 · fb_fit 0.875 (4-4 실측과 일치)
- **빌드한 이미지 그대로** E2E: `job-train_v1`(uid 1000, 읽기 전용 루트, /csv ro) 학습·등록 → 워크로드 YAML 의 initContainer 명령(uid 10001) 다운로드 → `model_v2` `/info` 버전 `toxicity-filter/3`, `version_mismatch: false`, `/predict` 정상
- 로컬 API(실제 클러스터 Watch, 조회만) + 화면: 두 탭 표시·콘솔 오류 없음. **교체(POST)는 로컬에서 실행하지 않음** (실제 클러스터에 적용되므로) — 테스트(FakeCluster)로만 확인

### 구현 중 발견·수정
| 발견 | 수정 |
|---|---|
| 워크로드 YAML 머리 주석의 설명문에도 표식 문자열이 있어 "표식 2개" 로 판정 | 주석만 있는 줄 제외 + 줄 끝 표식만 인정 (테스트 추가) |
| MLflow 3.18 은 없는 alias 에 404 가 아니라 **400 `INVALID_PARAMETER_VALUE` "… not found."** 로 응답 → 화면에 오류 배너 | "없음" 으로 처리, 테스트의 가짜 MLflow 도 실제 응답으로 교체 |
| 학습 데이터 부족 등 검사 실패는 run 생성 전 | MLflow 에 실패 run 을 남기지 않음 (4-2 표 수정). MLflow 연결 후 실패는 run 이 FAILED |

### 남은 것 (적용 — `train_인수인계.md`)
최상위: ConfigMap `MLFLOW_TRACKING_URI`·`MLFLOW_UI_URL` + API v4 · 화면 v3 / 사람: MLflow 고정(화면) → `eval.csv`·`job/train.yaml` 배치 → seed 학습(버전 1) → 서빙 워크로드 교체(화면)
