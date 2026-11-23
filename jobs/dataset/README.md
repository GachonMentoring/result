# 데이터셋 재생성 Job

설계: `k8sapimd/dataset.md`. 플랫폼 화면 [모델 재학습] → [데이터셋 재생성] 버튼이 이 이미지로 쿠버네티스 Job 을 만든다.

```
NFS /home/suhan/csv/seed.csv ─┐
                              ├─▶ /home/suhan/csv/dataset-<YYYYMMDD-HHMMSS>.csv  (label,text — seed 행 → DB 행)
DB comment_feedback ──────────┘    /home/suhan/csv/dataset-<YYYYMMDD-HHMMSS>.json (요약, 마지막에 생김)
   (true_label 있는 행)              ※ 날짜 태그는 한국 시간
```

- 정리·검사 없이 그대로 합친다. seed.csv 가 없으면 DB 행만, DB 가 0건이면 seed 행만 — 항상 CSV 를 만든다
- seed.csv 첫 줄이 `label,text` 가 아니면 실패 (엉뚱한 파일을 조용히 합치지 않음)
- 결과 요약은 `/dev/termination-log` (파드 상태) → 플랫폼 화면이 읽음

## 실행 (로컬)

```powershell
cd jobs\dataset
$env:CSV_DIR="dev-csv"; $env:DB_PASSWORD="<mentor_app 비밀번호>"   # DB 기본값: 192.168.2.30:31306 / toxicity / mentor_app
mkdir dev-csv; copy ..\..\model\data\seed.csv dev-csv\
..\..\server\.venv\Scripts\python export.py
..\..\server\.venv\Scripts\python -m pytest -q tests                  # 테스트 (DB 불필요)
```

## 이미지

```
docker build -t suhannugul/mentor:job-dataset_v1 .
docker push suhannugul/mentor:job-dataset_v1
```
python:3.14-slim (플랫폼 API 와 같음), PyMySQL 1.2.4, uid/gid 1000 (NFS 폴더 소유자).

## 환경변수

| 이름 | 기본 | 클러스터에서 주는 곳 |
|---|---|---|
| `CSV_DIR` | `/csv` | Job 명세 (PVC `mentor-csv` 마운트 경로) |
| `SEED_FILE` | `seed.csv` | — |
| `DB_HOST` `DB_PORT` `DB_NAME` | 192.168.2.30 / 31306 / toxicity | ConfigMap `mentor-platform-config` |
| `DB_USER` `DB_PASSWORD` | mentor_app / (필수) | Secret `mentor-platform-db` |
| `JOB_NAME` | — | 파드 라벨 `job-name` |
