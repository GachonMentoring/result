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
| `train.py` | 가이드 02장과 같은 설정. `artifacts/` 에 `model.joblib`, `metrics.json`, `model_info.json` |
| `serve.py` | FastAPI 추론 서버. `MODEL_PATH` 환경변수 (기본 `artifacts/model.joblib`) |
| `requirements*.txt` | 운영 이미지와 같은 버전으로 고정 (서빙 / 학습 / 개발) |
| `Dockerfile` | 1단계 학습 → 2단계 서빙. 학습·서빙 버전이 항상 같음. root 아닌 사용자로 실행 |

`artifacts/` 는 빌드 산출물이라 git 에서 제외 (이미지 빌드 시 다시 학습).

## 버전

`model_info.json` 의 `version` = `seed-<데이터+설정 해시 12자리>` — 같은 데이터·설정이면 언제 학습해도 같은 값.
빌드 시 `--build-arg MODEL_VERSION=v1` 로 직접 지정할 수 있다.

## 이미지

- **`suhannugul/mentor:model_v1`** — Docker Hub 공개 저장소 (기존 `mentor:v1` 과 같은 저장소, 태그만 다름)
- 빌드: `docker build -t suhannugul/mentor:model_v1 .` → 확인: `/info` 의 `version_mismatch: false`, 예측값 운영 모델과 동일, 실행 uid 10001

## 배포 (이후 Work)

별도 파드로 배포 — 기존 `mentor/toxicity-filter` (Service 이름·포트 8088 유지) 자리를 이 이미지로 교체.
이 파드는 쿠버네티스 권한이 필요 없으므로 `automountServiceAccountToken: false`.
