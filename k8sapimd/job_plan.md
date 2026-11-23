# Job 진행 계획 — 모델 재학습 파이프라인 설계 검토 (구현 전 제출)

> 대상: `job.md` — 메뉴 [모델 재학습] → ① 데이터셋 재생성 ② 모델 재학습 ③ 모델 교체
> 이번 단계: **설계·검토·아이디어만** (코드 수정 없음). 질의(9장) 확정 후 구현
> 기준일: 2026-11-22 (아래 수치·클러스터 값은 이 날짜에 실측·확인)

---

## 1. 요구사항 정리

| 메뉴 | 입력 | 처리 | 산출 |
|---|---|---|---|
| ① 데이터셋 재생성 | (없음 / 옵션) | DB `toxicity.comment_feedback` → CSV | NFS 에 `dataset-*.csv` |
| ② 모델 재학습 | CSV 선택 | `train.py` 로 학습 | NFS 에 모델 폴더 (`model.joblib` + `model_info.json` + `metrics.json`) |
| ③ 모델 교체 | 모델 선택 | 서빙 파드(`toxicity-filter`)가 그 모델을 쓰도록 변경 | 댓글 기능이 새 모델로 응답 |

job.md 의 두 질문: **(Q1) CPU 서버로 학습 가능한가, AWS 필요한가 / (Q2) NFS 에 여러 모델을 둘 만큼 작은가**

---

## 2. 답변 — Q1 · Q2 (실측)

### 실측 방법
`model/train.py` 를 그대로 호출, `seed.csv`(458행)를 무작위로 이어붙여 행 수만 늘린 합성 데이터로 측정 (개발 PC 16 스레드, 학습은 사실상 단일 코어 — LogisticRegression lbfgs).
※ 합성 데이터라 f1 값은 의미 없음 → 시간·메모리·크기만 참고.

| 행 수 | 학습 시간 | 최대 메모리 (Python 측) | `model.joblib` 크기 |
|---:|---:|---:|---:|
| 458 (현재 seed) | **0.3 초** | 5 MB | **72 KB** |
| 5,000 | 2.2 초 | 26 MB | 1.1 MB |
| 20,000 | 8.0 초 | 86 MB | 2.5 MB |
| 100,000 | 42 초 | 332 MB | 7.6 MB |

클러스터: 워커 2대 각 **6 CPU / 9.4 GiB** (k8s-worker-1·2).

### Q1 → **CPU 로 충분, AWS 불필요**
- 피드백은 사람이 화면에서 한 건씩 정답을 다는 구조 → 현실적인 누적량은 수백~수천 행. 10만 행이어도 1분 이내
- 학습 Job 자원: `requests: cpu 500m / memory 512Mi`, `limits: cpu 1 / memory 1Gi` 면 10만 행까지 여유
- AWS(GPU)가 의미 있어지는 경우는 **모델 종류를 바꿀 때뿐** (KcELECTRA·KoBERT 같은 트랜스포머 파인튜닝). 지금 TF-IDF + 로지스틱 회귀 구조에서는 이득 없음 → 발표에서는 "왜 AWS 를 안 썼나"를 실측표로 근거 제시 가능

### Q2 → **충분히 작다**
- 지금 72 KB, 10만 행이어도 수 MB. 모델 100개를 보관해도 1 GB 미만
- 크기는 TF-IDF 사전(문자 n-gram 개수)에 비례 → 상한이 필요하면 `TfidfVectorizer(max_features=...)` 로 고정 가능 (지금은 불필요)
- 서빙 파드 limit `256Mi` 도 수 MB 모델이면 문제 없음

---

## 3. ⚠ 요구사항 외에 반드시 정해야 할 것 (검토에서 발견)

### 3-1. 【가장 중요】 지금 모델은 **이미지 안에 구워져 있다** → "교체"할 경로가 없음
- 현재 `toxicity-filter` = `suhannugul/mentor:model_v1`, 모델 파일은 이미지의 `/app/artifacts/model.joblib`, 시작할 때 한 번만 로드 (`serve.py` lifespan)
- NFS 에 모델을 저장해도 서빙 파드는 그걸 볼 수 없다 → **서빙 파드에 모델 저장소 볼륨을 마운트하고 `MODEL_PATH` 로 고르게** 바꿔야 함

| 교체 방식 | 내용 | 평가 |
|---|---|---|
| **A. 선언 변경 (추천)** | 서빙 Deployment 의 env `MODEL_PATH=/models/<버전>/model.joblib` 를 바꿈 → 롤링 업데이트 | 새 파드가 모델 로드 성공(Ready)해야 옛 파드가 내려감 (`maxSurge 1 / maxUnavailable 0` 이미 설정됨) → **무중단 + 실패 시 자동으로 옛 모델 유지**. 롤백 = 이전 값으로 되돌리기 |
| B. `/reload` API | 파드 안에서 모델만 다시 읽기 | 빠르지만 선언(YAML)과 실제가 어긋남 → base.md 주제(선언형)와 반대. 비추천 |
| C. 모델마다 이미지 빌드 | 클러스터 안 이미지 빌드(kaniko 등) 필요 | 과함 |
| D. `current` 심볼릭 링크 | NFS 링크만 바꿈 | 파드가 재시작 전엔 모름 + 선언에 안 드러남. 비추천 |

### 3-2. 【선언형과의 충돌】 교체를 API 가 직접 patch 하면 Work 3 판정이 "불일치"가 된다
- infra_guide 9장: `toxicity-filter` 는 NFS `workloads/toxicity-filter.yaml` 로 관리할 워크로드
- 플랫폼이 Deployment 를 직접 patch 하면 → 매니페스트 관리 화면에 **불일치** 표시 (선언 ≠ 실제)
- **추천: [모델 교체] = "NFS 의 `workloads/toxicity-filter.yaml` 에서 `MODEL_PATH`·`MODEL_VERSION` 값을 바꿔 저장 → Work 3 의 apply 를 그대로 호출"**
  - 교체 이력이 YAML 변경으로 남고, 판정은 항상 "일치" → **"모델 교체도 선언 변경일 뿐"** 이 발표 포인트가 됨 (base.md 목표와 정확히 일치)
  - 전제: infra_guide 9장(워크로드 YAML 편입)이 먼저 끝나 있어야 함

### 3-3. 학습 산출물 저장소를 **매니페스트 폴더와 분리**
- `/home/suhan/manifests` 는 Work 3 가 YAML 을 훑는 곳 → 데이터·모델을 섞지 않는다
- 제안: NFS `/home/suhan/mlops-data/` 아래 `datasets/`, `models/` + 별도 PV/PVC `mentor-mlops-data` (RWX, Retain)
  - → **확정 (2026-11-23): 데이터셋은 NFS `/home/suhan/csv/` + PV/PVC `mentor-csv`** (모델은 MLflow 로 — 8장). 상세는 `dataset.md`
  - PV 는 클러스터 범위 → infra_guide 규칙대로 **최상위 디렉터리에서 kubectl** (플랫폼 화면 X)
  - NFS export 가 `/home/suhan/manifests` 만인지 상위까지인지 **확인 필요** (9장 질의)
- 마운트: 학습 Job = rw / 서빙 파드 = **ro** / 플랫폼 API = ro (목록·`model_info.json` 표시용)

### 3-4. 【배포 실패 원인 후보】 실행 uid 불일치
- NFS 는 `root_squash`, 파일 소유자 uid 1000. 그런데 모델 이미지는 **uid 10001** 로 실행
- 학습 Job 이 uid 10001 이면 NFS 쓰기 실패 → Job 파드에 `securityContext: {runAsUser: 1000, runAsGroup: 1000}` 지정
- 서빙 파드는 읽기만 하므로 파일이 644 면 10001 이어도 됨 (Job 에서 `umask 022` 확인)

### 3-5. 학습·서빙 scikit-learn 버전을 같게 (가이드 08장 시나리오 3)
- 지금은 Dockerfile 1단계에서 학습해서 자동으로 맞았지만, **Job 으로 학습하면 이 보장이 사라짐**
- 서빙 이미지에는 `pandas`·`train.py` 가 없음 → 학습용 이미지 필요
- 추천: 같은 `model/Dockerfile` 에서 **학습 이미지(`--target train`)와 서빙 이미지를 같은 태그 세대로 함께 빌드** (`model_train_v2` / `model_v2`). 둘 다 `requirements.txt` 고정값 공유
- 교체 시 안전장치: 선택한 모델의 `model_info.json.sklearn_version` ≠ 서빙 이미지 버전이면 **교체 버튼 비활성** (지금 `/info` 의 `version_mismatch` 와 같은 검사를 사전에)

### 3-6. 데이터셋 품질 — 피드백만으로 학습하면 안 됨
> **2026-11-23 결정: ① 데이터셋 재생성에는 아래 정리·검사를 넣지 않음** (DB 그대로 추출, 항상 CSV 생성 — `dataset.md`).
> seed 합치기·데이터가 너무 적을 때의 처리는 필요하면 ② 모델 재학습 설계에서 다룬다.

| 문제 | 대응 |
|---|---|
| 피드백 행이 적음 (수십~수백) → 피드백만 쓰면 seed 지식을 잊음 | **seed.csv + 피드백 합쳐서** 데이터셋 생성 (seed 는 Job 이미지 또는 NFS `datasets/seed.csv`) |
| `true_label IS NULL` 행 | 제외 (모델 판정을 정답으로 쓰면 오답 강화 — work_5_plan 3장) |
| 같은 문장 중복 / 같은 문장에 다른 정답 | 중복 제거, 충돌은 최신 `labeled_at` 기준 또는 제외 (보고에 건수 표시) |
| 한쪽 라벨이 1개 이하 | `train_test_split(stratify=...)` 가 **오류로 Job 실패** → 데이터셋 단계에서 라벨별 최소 개수 검사, 부족하면 생성 거부 |
| DB 에서 읽기 | SELECT 만 필요 → 읽기 복제본(`mariadb-1`) 사용 가능. 계정은 `mentor_app` 재사용보다 **SELECT 전용 계정** 권장 |

데이터셋 파일 옆에 메타데이터(`dataset-*.json`): 행 수, 라벨 분포, seed/피드백 행 수, DB 스냅샷 기준 시각(`MAX(labeled_at)`), sha256 → 어떤 데이터로 만든 모델인지 추적 (`model_info.json.data` 와 연결)

### 3-7. 모델 비교가 불가능한 지표 구조
- 지금 지표 = 매번 그 데이터의 20% 무작위 분할 → **데이터가 바뀌면 시험지도 바뀜** → v1 f1 과 v2 f1 을 비교할 수 없음
- 추천: **고정 평가셋(`eval.csv`)** 을 하나 두고 모든 모델을 같은 평가셋으로도 채점 → `metrics.json` 에 `eval_fixed` 추가
- 교체 화면에 "현재 모델 vs 선택 모델" 지표 나란히 표시, 낮아지면 경고 (막지는 않음)
- 임계값: 모델이 바뀌면 확률 분포도 바뀜 → `THRESHOLD`(0.5) 재검토 필요할 수 있음. 학습 시 추천 임계값을 `model_info.json` 에 기록하는 아이디어

### 3-8. Job 운영 설정
| 항목 | 값 / 이유 |
|---|---|
| 이름 | `generateName: retrain-` → 실행마다 새 Job (기록이 Job 목록으로 남음) |
| 라벨 | `platform.mentor/job-type: dataset \| train` → 화면 필터·동시 실행 검사. ※ Work 3 "선언 없음"은 플랫폼이 apply(SSA) 한 리소스만 대상이라, Job 을 **create 로 만들면 애초에 안 나타남** (코드 확인, `dataset.md` 7-3) |
| `backoffLimit` | 0~1 (데이터 문제는 재시도해도 같음) |
| `activeDeadlineSeconds` | 600 (멈춤 방지) |
| `ttlSecondsAfterFinished` | 예: 86400 (완료 Job 자동 정리. 기록은 NFS `model_info.json` 에 남음) |
| 동시 실행 | 같은 종류 Job 이 실행 중이면 API 가 409 로 거부 |
| 원자적 저장 | `models/.tmp-<버전>/` 에 쓰고 끝나면 `models/<버전>/` 으로 rename → 반쯤 쓴 모델이 목록·교체 대상에 안 보임 |
| 상태·로그 | 새로 만들 필요 없음 — Work 1 Watch(jobs 포함)와 Work 2 파드 로그 재사용 |
| 권한 | Role 에 `batch/jobs create` 이미 있음 ✅. 추가 권한 불필요 |
| DB Secret | 데이터셋 Job 에 DB 계정 주입 → Secret 은 최상위 `./secret/` 규칙(값 공란)으로 |

### 3-9. 모델 버전 이름
- 지금 `seed-<해시12>` (같은 데이터+설정이면 같은 값 — 좋은 성질, 유지)
- 재학습: `fb-<YYYYMMDD>-<해시8>` 처럼 접두사로 구분 → DB `model_version`(VARCHAR 64)에 그대로 기록 → **교체 후 버전별 정확도**를 피드백 테이블에서 바로 집계 가능 (`GROUP BY model_version`, "wrong" 필터 이미 있음)

### 3-10. 보안
- 플랫폼은 로그인 없음 (deploy.md 8-5) → 교체는 서비스에 즉시 영향 → **확인 대화상자** + IP 제한 전제
- `joblib.load` = pickle → 파일을 바꿔치기하면 임의 코드 실행. NFS 는 내부망 rw → `model_info.json` 에 `model.joblib` sha256 기록, 서빙 시작 시 검증 (가벼운 방어)
- 보관 정리: 모델 삭제 기능을 둔다면 **현재 서빙 중인 버전은 삭제 거부**

---

## 4. 추천 구조

```
[화면] 모델 재학습 ─┬─ ① 데이터셋 재생성 ─▶ API ─▶ Job dataset-xxxxx (학습 이미지, uid 1000)
                   │                              DB(SELECT) + seed ─▶ NFS datasets/dataset-<시각>.csv (+ .json)
                   ├─ ② 모델 재학습 ──────▶ API ─▶ Job retrain-xxxxx (학습 이미지, uid 1000)
                   │                              datasets/*.csv ─▶ models/.tmp → models/<버전>/
                   └─ ③ 모델 교체 ───────▶ API: NFS workloads/toxicity-filter.yaml 의 MODEL_PATH 수정
                                              ─▶ Work 3 apply ─▶ 롤링 업데이트 (Ready 실패 시 옛 모델 유지)

NFS /home/suhan/mlops-data  ── PVC mentor-mlops-data ──┬─ Job (rw)
                                                       ├─ toxicity-filter (ro, /models)
                                                       └─ mentor-api (ro, 목록 표시)
NFS /home/suhan/manifests   ── (기존) Work 3 전용
```

API (안):

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/v1/retrain/datasets` | 데이터셋 Job 생성 |
| GET | `/api/v1/retrain/datasets` | NFS 데이터셋 목록 + 메타 |
| POST | `/api/v1/retrain/models` | `{dataset}` → 학습 Job 생성 |
| GET | `/api/v1/retrain/models` | 모델 목록 + 지표 + 현재 서빙 여부 + sklearn 호환 여부 |
| POST | `/api/v1/retrain/deploy` | `{version}` → 워크로드 YAML 수정 + apply |
| GET | `/api/v1/retrain/jobs` | `job-type` 라벨 Job 상태 (Watch 캐시) |

---

## 5. 아이디어 (선택)

| 아이디어 | 내용 | 효과 |
|---|---|---|
| **MLflow 연동** | `mentor/mlflow` 가 이미 떠 있음 → 학습 Job 이 지표·파라미터를 MLflow 에 기록 (모델 파일은 NFS 유지) | 실험 비교 화면을 직접 안 만들어도 됨. 단 `mlflow:latest` 태그·hostPath PV 는 정리 필요 |
| 파이프라인 한 번에 | ①→② 를 Job 2개 연속 (또는 한 Job 의 initContainer=데이터셋, container=학습) | "버튼 하나로 재학습" 시연 |
| CronJob | 매주 데이터셋+학습 자동 (교체는 사람이) | "자동화" 메시지. Role 에 cronjobs 이미 있음 |
| 섀도/카나리 | 새 모델을 두 번째 Deployment 로 띄워 같은 댓글에 두 모델 결과 비교 | 교체 전 검증 (시간 남으면) |
| 셀프힐링 시연 | 깨진 모델로 교체 시도 → 새 파드 CrashLoop, 옛 파드가 계속 서빙 → 선언 되돌리면 복구 | base.md "선언 상태 유지" 를 모델 운영으로 보여줌 |

---

## 6. 작업 순서 (제안)

1. 저장소: NFS `mlops-data` 디렉터리·export 확인 → PV/PVC (최상위, kubectl)
2. 이미지: `model/Dockerfile` 학습 target 분리, `train.py` 에 고정 평가셋·원자적 저장·sha256, `serve.py` 에 sha256 검증 → `model_v2` / `model_train_v2`
3. 서빙 전환: `workloads/toxicity-filter.yaml` 편입(infra_guide 9장) + 볼륨(ro) + `MODEL_PATH` env → seed 모델을 NFS `models/seed-…/` 에 두고 적용 (동작 변화 없음 확인)
4. 데이터셋 Job + API ① → 학습 Job + API ② → 교체 API ③ (Work 3 apply 재사용)
5. 화면: 메뉴 [모델 재학습] + 하위 3개
6. 시연 시나리오: 피드백 입력 → 데이터셋 → 학습 → 지표 비교 → 교체 → 댓글 결과 변화 → 판정 "일치"

---

## 7. 질의 (구현 전 확정 필요)

1. 교체 방식: **3-2 추천안(워크로드 YAML 수정 → Work 3 apply)** 으로 갈지, 단순히 Deployment patch 로 갈지
2. NFS 저장 위치: `/home/suhan/mlops-data` 신설 가능 여부 (export 범위)
3. 데이터셋 구성: seed + 피드백 합치기 동의 여부, 라벨 최소 개수 기준 (예: 라벨당 20)
4. DB 읽기 계정: SELECT 전용 계정 신설 vs `mentor_app` 재사용
5. 고정 평가셋: seed 의 현재 테스트 분할(92행)을 `eval.csv` 로 고정할지
6. ~~MLflow 연동 여부~~ → **연동하기로 함 (2026-11-22)** → 8장

---

## 8. MLflow 연동 시 바뀌는 것 (2026-11-22 추가)

### 8-1. 현재 MLflow 상태 (읽기 전용 확인)
| 항목 | 값 | 문제 |
|---|---|---|
| 이미지 | `ghcr.io/mlflow/mlflow:latest` (실제 3.18.0) | `latest` → 재시작 때 버전이 바뀔 수 있음. 학습 Job 의 mlflow 클라이언트 버전과 맞춰 **3.18.0 고정** 필요 |
| 메타데이터 저장 | SQLite `/mlflow/data/mlflow.db` | 이 규모에선 문제없음 |
| 모델 파일 저장 | `--artifacts-destination=/mlflow/artifacts` (서버 경유 업로드, 아직 폴더 없음) | — |
| 볼륨 | PV `mlflow-data-pv` = **hostPath `/data/mlflow`, k8s-worker-1 고정** | 노드 하나에 묶임, 백업 없음 |
| 노출 | **NodePort 30081, 인증 없음, CORS `*`** | 내부망 누구나 모델 등록·삭제 가능 → 서빙이 여기서 모델(pickle)을 받으면 **코드 실행 경로가 됨** |
| 내용 | 실험 `Default` 하나, 등록 모델 없음 | 새로 시작하는 것과 같음 |

### 8-2. 역할 나누기
| 대상 | 연동 전 (1~7장) | 연동 후 |
|---|---|---|
| 데이터셋 CSV | NFS `datasets/` | **그대로 NFS** (job.md 요구사항). **① 데이터셋 재생성은 MLflow 와 무관** (DB → CSV + `dataset-*.json`, MLflow 가 꺼져 있어도 동작). ② 학습 Job 이 자기 run 에 "어떤 CSV 를 썼는지"(파일명·sha256·행 수)만 기록 |
| 모델 파일 | NFS `models/<버전>/` | **MLflow 모델 저장소** → NFS `models/` 없앰 |
| 지표·파라미터·학습 환경 | `model_info.json`, `metrics.json` | MLflow run 에 기록. 비교 화면도 MLflow 화면으로 대신함 |
| 모델 버전 | `fb-<날짜>-<해시>` | 등록 모델 `toxicity-filter` 의 **버전 번호** (1, 2, 3 …). 해시는 태그로 남김 |
| 어떤 모델을 서빙할지 | 워크로드 YAML 의 `MODEL_PATH` | 워크로드 YAML 의 **`MODEL_URI=models:/toxicity-filter/3`** (바뀌지 않음: 선언으로 정하는 것) |

### 8-3. ⚠ 교체는 "alias 변경"이 아니라 "YAML 의 버전 번호 변경"으로
- MLflow 의 일반적인 방식은 `@champion` 같은 alias 를 옮기는 것이지만,
  alias 를 옮겨도 **쿠버네티스는 모름** → 파드가 재시작되지 않고 YAML 에도 흔적이 없음 → 언제 어떤 모델이 도는지 선언으로 알 수 없음 (3-2 와 같은 문제)
- 따라서 YAML 에는 **버전 번호를 직접** 적고 Work 3 apply → 롤링 업데이트
- alias 는 apply 성공 **후에** 플랫폼이 `production` 으로 옮겨 MLflow 화면 표시용으로만 사용 (선택)

### 8-4. 서빙 파드가 모델을 받는 방법
| 안 | 방법 | 평가 |
|---|---|---|
| **A. initContainer 다운로드 (추천)** | initContainer 가 `MODEL_URI` 로 MLflow 에서 모델을 받아 `emptyDir` 에 둠 → `serve.py` 는 지금처럼 파일 경로로 로드 | `serve.py` 에 mlflow 의존성 안 들어감. 다운로드 실패 → 새 파드 시작 안 됨 → 옛 파드가 계속 서빙 |
| B. MLflow 모델 파일을 NFS 에 두기 | MLflow `--artifacts-destination` 을 NFS 로 바꾸고 서빙 파드가 그 파일을 직접 마운트 | MLflow 가 꺼져도 서빙 시작 가능. 대신 YAML 에 run id 경로가 들어가 읽기 어려움 |

- A 안의 약점: **MLflow 가 꺼진 상태에서 모델 파드가 재시작되면**(노드 장애·파드 삭제·셀프힐링 시연) 모델을 못 받아 댓글 기능이 멈춤 → MLflow 도 probe·재시작 정책을 갖추고, 시연 전에 같이 떠 있는지 확인
- 모델 형식: `mlflow.sklearn.log_model` 로 저장(sklearn 버전·requirements 자동 기록) + `model_info.json` 을 추가 파일로 함께 저장 → `serve.py` 의 `/info`·`version_mismatch` 유지. 로드 방식(joblib/pickle)은 구현 단계에서 확정

### 8-5. 1~7장에서 바뀌는 것 / 그대로인 것
| 장 | 변화 |
|---|---|
| 2장 Q1·Q2 | 그대로 (모델 크기가 작아 MLflow 저장소 부담도 없음) |
| 3-1 이미지 안 모델 → 교체 경로 | 해결 방식만 바뀜: 볼륨 마운트 → **initContainer 다운로드** |
| 3-2 YAML 수정 → Work 3 apply | **그대로 (더 중요해짐, 8-3)** |
| 3-3 저장소 분리 | NFS 는 `datasets/` 만 → PVC 는 데이터셋 Job(rw)·학습 Job(ro)·API(ro) 만 마운트. 서빙 파드는 NFS 불필요 |
| 3-4 uid 1000 | 데이터셋 Job 만 해당 (학습 Job 은 NFS 에 쓰지 않음) |
| 3-5 sklearn 버전 | 그대로 필요. MLflow 가 학습 환경 sklearn 버전을 기록 → 교체 전 검사에 사용 |
| 3-6 데이터셋 품질 | 그대로 |
| 3-7 고정 평가셋 | **그대로 필요** — MLflow 비교 화면은 같은 평가셋으로 잰 지표일 때만 의미 있음 |
| 3-8 원자적 저장 | MLflow 로 대체: 학습·평가가 끝까지 성공한 run 만 등록 모델로 등록 |
| 3-9 버전 이름 | 등록 모델 버전 번호. DB `model_version` 에는 `"3"` 대신 `"toxicity-filter/3"` 처럼 기록 권장 |
| 3-10 sha256 검증 | 모델 보호 문제가 **MLflow 접근 제어**로 옮겨감 (8-6) |
| 4장 API | `GET /retrain/models` = MLflow REST 조회 결과(등록 모델 버전·지표)를 가공. MLflow 주소는 ConfigMap `MLFLOW_TRACKING_URI=http://mlflow.mentor.svc.cluster.local:5000` |

### 8-6. MLflow 쪽 사전 작업 (※ 2026-11-23 정정: MLflow 는 플랫폼 관리 워크로드 → **사람이 매니페스트 관리 화면에서 `workloads/mlflow.yaml` 수정·적용**. 최상위 kubectl 아님 — `train.md` 11장이 기준)
1. 이미지 `mlflow:v3.18.0` 고정, 학습 이미지에 `mlflow-skinny==3.18.0` 고정
2. **노출 정리**: NodePort 30081 → ClusterIP + Ingress (IP 제한, 다른 서비스와 같은 설정). 무인증 MLflow 에 모델 등록 권한이 열려 있으면 서빙 파드에 임의 코드를 넣을 수 있음
3. 저장소(선택): hostPath(worker-1 고정) 유지 vs NFS PV 로 이전. NFS 로 옮긴다면 **SQLite 는 NFS 위에서 잠금 문제** → 메타데이터는 기존 MariaDB 에 `mlflow` DB 를 따로 만들어 쓰는 방법 (이미지에 PyMySQL 필요)
4. readiness/liveness probe (`/health`) 추가 — 8-4 A 안의 의존성 때문

### 8-7. 질의 (추가)
7. 서빙이 모델을 받는 방식: **A. initContainer (추천)** / B. NFS 직접 마운트
8. MLflow 노출: NodePort 유지 vs ClusterIP + Ingress(IP 제한)
9. MLflow 저장소: hostPath 유지(시연 범위면 충분) vs NFS + MariaDB 로 이전

### 8-8. 보충 설명 (2026-11-22 질의응답)

#### Q. 8-4 A 안 = 지금 모델 파드에 initContainer 를 만든다는 것?
네. `toxicity-filter` Deployment 의 **파드 템플릿에 initContainer 를 하나 추가**하는 것.
떠 있는 파드를 직접 고치는 게 아니라 **워크로드 YAML 수정 → apply → 새 파드가 initContainer 포함으로 생성**되는 방식.

```yaml
spec:
  template:
    spec:
      initContainers:
        - name: fetch-model                      # ① 먼저 실행 → 끝나야 ②가 시작
          image: suhannugul/mentor:model_train_v2   # mlflow 클라이언트가 든 이미지
          command: ["mlflow", "artifacts", "download",
                    "--artifact-uri", "$(MODEL_URI)", "--dst-path", "/models"]
          env:
            - {name: MLFLOW_TRACKING_URI, value: http://mlflow.mentor.svc.cluster.local:5000}
            - {name: MODEL_URI, value: models:/toxicity-filter/3}   # ← 교체 = 이 숫자만 변경
          volumeMounts: [{name: model, mountPath: /models}]
      containers:
        - name: app                              # ② 지금의 서빙 컨테이너 (거의 그대로)
          image: suhannugul/mentor:model_v2
          env:
            - {name: MODEL_PATH, value: /models/model.pkl}
          volumeMounts: [{name: model, mountPath: /models, readOnly: true}]
      volumes:
        - name: model
          emptyDir: {}                           # 파드 안에서만 쓰는 임시 공간
```

| 항목 | 내용 |
|---|---|
| 순서 | 새 파드 생성 → initContainer 가 MLflow 에서 모델을 받아 `/models` 에 둠 → 끝나면 서빙 컨테이너가 시작해서 그 파일을 로드 |
| 실패 시 | 다운로드 실패 → 서빙 컨테이너 시작 안 함 (`Init:Error`) → `maxUnavailable: 0` 이라 **옛 파드가 계속 서빙** |
| `emptyDir` | 파드와 수명이 같음 → 파드가 다시 만들어질 때마다 모델을 새로 받음. 수 MB 라 부담 없지만 **그때마다 MLflow 가 떠 있어야 함** (8-4 의 "MLflow 의존") |
| 서빙 이미지 | 크게 안 바뀜 — 모델을 이미지에 넣지 않고 `MODEL_PATH` 만 `/models` 를 가리킴 |
| 구현 때 확인 | MLflow 로 저장하면 파일명이 `model.joblib` 이 아니라 `model.pkl` → `serve.py` 로드 방식 확인 |

#### Q. 8-7 질의 9 의 "MLflow 저장소" 란 정확히 무엇?
MLflow 서버가 디스크에 보관하는 **두 가지**. NFS 데이터셋 폴더와는 별개.

| 구분 | 담는 것 | 지금 위치 |
|---|---|---|
| **메타데이터 저장소** (backend store) | 실험·run 목록, 파라미터, 지표(f1 등), 태그, 등록 모델 이름·버전 번호·alias | SQLite 파일 `/mlflow/data/mlflow.db` |
| **파일 저장소** (artifact store) | 실제 파일: 모델(`model.pkl`, `MLmodel`, `requirements.txt`), `model_info.json` 등 | `/mlflow/artifacts` (아직 비어 있음) |

둘 다 PVC `mlflow-data` 안에 있고, 실체는 **worker-1 노드 로컬 디스크 `/data/mlflow` (hostPath)**. 그래서:
- MLflow 파드는 **worker-1 에만** 뜰 수 있음 → worker-1 이 꺼지면 worker-2 로 못 옮기고 MLflow 가 멈춤
- initContainer 방식에서는 그 사이 모델 파드가 재시작되면 모델을 못 받아 **댓글 기능도 멈춤**
- worker-1 디스크가 망가지면 학습 기록·모델 파일 모두 사라짐 (백업 없음)

선택지:
- **hostPath 유지**: 시연 범위면 충분. 위 위험을 알고 쓰는 것
- **NFS 로 이전**: 파일 저장소는 NFS 로 옮기면 됨. 단 SQLite 파일을 NFS 위에 두면 잠금 문제로 DB 가 깨질 수 있음 → 메타데이터는 기존 MariaDB 에 `mlflow` DB 를 따로 만들어 옮기는 조합 필요

→ 현재 의견: 시연 일정상 **hostPath 유지**, worker-1 의존은 발표에서 "알려진 한계"로 언급
