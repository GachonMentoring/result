# 댓글 분류 모델 (Work 4)

MLOps 실습가이드 1부의 댓글 분류 모델을 **학습 + 추론 서버**로 분리한 디렉터리.
가이드 `app.py` 중 "모델 로드 + 확률 계산"만 여기 있고, 입력 검증·임계값 판정·DB·피드백은 플랫폼 서버(`server/`)가 맡는다.

```
사용자 화면 ─▶ server/  POST /api/v1/comments/predict  (검증 · 판정 · Work 5: DB)
                 │  MODEL_URL
                 ▼
               model/   POST /predict → {toxic_proba, model}   (이 디렉터리)
```

## 모델

| 항목 | 값 |
|---|---|
| 문제 | 한국어 댓글 한 줄 → 0 정상 / 1 주의(비난·무례·공격적) |
| 구성 | TF-IDF 문자 n-gram (3, 6), min_df 2 → 로지스틱 회귀 (Pipeline 하나) |
| 데이터 | `data/seed.csv` 458행 (가이드 HTML에서 추출, label 1: 250 / 0: 208) |
| 지표 | f1 0.925 · precision 0.875 · recall 0.98 (평가용 92개, random_state 42) — 운영 중인 모델과 동일 |
| 약점 | "안녕하세요" 0.686 처럼 학습 데이터에 없는 유형에 취약 → 피드백·재학습 필요 (Work 5 이후) |

## 실행 (로컬)

이미지와 같은 Python 3.11 + 고정 버전을 쓴다.

```powershell
cd model
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt

.venv\Scripts\python train.py                        # artifacts/ 생성
.venv\Scripts\python -m uvicorn serve:app --port 8088  # 모델 서버
.venv\Scripts\python -m pytest -q                    # 테스트
```

## API

| Method | Path | 설명 |
|---|---|---|
| GET | `/healthz` | 생존 확인 (probe 용) |
| GET | `/info` | 모델 이름·버전·학습 지표·학습/서빙 scikit-learn 버전 (`version_mismatch`) |
| POST | `/predict` | `{"text": "..."}` (1~300자) → `{"toxic_proba": 0.84, "model": {"name", "version"}}` |

판정(label)은 주지 않는다 — 임계값은 앱 서버의 `THRESHOLD` 환경변수로 정한다.

## 파일

| 파일 | 설명 |
|---|---|
| `train.py` | 가이드 02장과 같은 설정 (`build_model`, `split_eval`). 로컬 학습 시 `artifacts/` 에 `model.joblib`, `metrics.json`, `model_info.json` |
| `train_job.py` | **재학습 Job** — 데이터셋 정리 → 학습 → `eval.csv` 채점 → MLflow 기록·등록 (k8sapimd/train.md 4·6장) |
| `make_eval.py` | 고정 평가셋 `data/eval.csv`(seed 의 가이드 평가 분할 92행) 생성 — 1회 |
| `serve.py` | FastAPI 추론 서버. `MODEL_PATH`. 같은 폴더의 `SOURCE`(initContainer 가 씀) 가 있으면 버전 = `toxicity-filter/<N>` |
| `job/train.yaml` | 학습 Job 템플릿 원본 → NFS `manifests/job/train.yaml` |
| `workload/toxicity-filter.yaml` | initContainer 방식 서빙 워크로드 원본 → NFS `manifests/workloads/toxicity-filter.yaml` |
| `requirements*.txt` | 고정 버전 — 서빙 / 학습(+pandas) / Job(+mlflow-skinny 3.18.0) / 개발(+pytest, mlflow) |
| `Dockerfile` | target `job-train`(재학습 Job · initContainer) / 기본(서빙). 같은 requirements → 학습·서빙 버전 일치 |

`artifacts/` 는 로컬 학습 산출물이라 git 에서 제외.

## 재학습 Job 로컬 실행

```powershell
$env:DATASET="seed"; $env:CSV_DIR="..\server\dev-csv"; $env:MLFLOW_TRACKING_URI="http://localhost:5000"; $env:PYTHONUTF8="1"
.venv\Scripts\python train_job.py      # dev-csv 에 eval.csv 가 있어야 함 (python make_eval.py --out ..\server\dev-csv\eval.csv)
```

## 버전

- 로컬 학습: `model_info.json` 의 `version` = `seed-<데이터+설정 해시 12자리>`
- 재학습 Job: MLflow 등록 모델 `toxicity-filter` 의 버전 번호 (1, 2, 3 …). 서빙 시 `/info`·DB `model_version` = `toxicity-filter/<N>`, 학습 지문(`seed-…`/`fb-…`)은 `build`

## 이미지

| 태그 | 빌드 | 내용 |
|---|---|---|
| `suhannugul/mentor:model_v1` | (이전) | 모델을 이미지 안에 넣은 서빙 |
| `suhannugul/mentor:model_v2` | `docker build -t suhannugul/mentor:model_v2 .` | 모델 없는 서빙 — `/models/model.pkl` 로드 |
| `suhannugul/mentor:job-train_v1` | `docker build --target job-train -t suhannugul/mentor:job-train_v1 .` | 재학습 Job + 서빙 initContainer (mlflow-skinny) |

## 배포

`workload/toxicity-filter.yaml` — initContainer `fetch-model` 이 MLflow 에서 `MODEL_URI`(models:/toxicity-filter/N) 를 받아 emptyDir `/models` 에 두고
서빙 컨테이너가 로드. 교체 = 그 한 줄의 버전 변경 → 적용 (플랫폼 [모델 교체] 화면). 반영 절차는 레포 최상위 `train_인수인계.md`.
