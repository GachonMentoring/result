# 데이터셋 재생성 기능 — 설계 방안 (구현 전 제출)

> 상위 문서: `job_plan.md` (재학습 파이프라인 전체). 이 문서는 그중 **① 데이터셋 재생성**만 다룬다.
> 확정 사항:
> | 날짜 | 결정 |
> |---|---|
> | 11-22 | ①은 **MLflow 와 무관한 독립 기능** |
> | 11-23 | 저장 위치 **NFS `/home/suhan/csv/`** (매니페스트 폴더와 분리) |
> | 11-23 | **정리 규칙·최소 개수 검사 없음** — 데이터가 적어도 항상 CSV 생성 |
> | 11-23 | Job 은 **전용 스크립트 이미지** — 레포에 `jobs/dataset/` 디렉터리 생성 |
> | 11-23 | DB 계정은 **기존 `mentor_app` 유지** |
> | 11-23 | 끝난 Job 은 **10분 뒤 자동 삭제** |
> | 11-23 | **`seed.csv` + DB 피드백을 합쳐** 하나의 CSV 생성, 파일명에 **날짜 태그** |
> | 11-23 | Job 명세는 코드가 아니라 **NFS 매니페스트 폴더 `job/dataset.yaml`** (템플릿)에서 관리 — API 는 실행마다 파일을 읽어 `generateName` 으로 create. 매니페스트 화면의 "없음" 상태·[apply] 시 1회 실행은 Job 특성으로 받아들임 (5-3) |
>
> 이번 단계는 설계만. 코드 수정 없음

---

## 1. 기능 요약

| 항목 | 내용 |
|---|---|
| 화면 | 메뉴 [모델 재학습] → 하위 [데이터셋 재생성] |
| 동작 | [생성] 클릭 → 플랫폼 API 가 **Job** 생성 → Job 이 `/csv/seed.csv` 와 DB 피드백(정답 있는 행)을 **이어 붙여** NFS 에 CSV 저장 |
| 산출 | `/home/suhan/csv/dataset-<날짜태그>.csv` (`label,text`) + 같은 이름의 `.json` (요약) |
| 하지 않는 것 | 중복·충돌 정리, 최소 개수 검사 — seed 와 DB 를 **있는 그대로** 합침 |

---

## 2. 전체 흐름

```
[화면] [데이터셋 생성] ── POST /api/v1/datasets ──▶ [플랫폼 API]
                                                    실행 중인 dataset Job 있으면 409
                                                    없으면 /manifests/job/dataset.yaml 읽어
                                                    Job 생성 (generateName: dataset-)
                                                          │
                                                          ▼
                          [Job 파드: suhannugul/mentor:job-dataset_v1]
                            ① /csv/seed.csv 읽기           (없으면 건너뜀 → DB 만)
                            ② DB SELECT label, text         (정답 있는 행)
                            ③ seed 행 + DB 행 순서로 /csv/.tmp 에 쓰기 → 이름 바꾸기
                            ④ /csv/dataset-<날짜태그>.json  (마지막 = 완료 표식)
                            ⑤ 요약 한 줄을 종료 메시지로 남기고 종료
                                                          │
                                          끝나고 10분 뒤 Job·파드 자동 삭제 (6장)
                                                          │
[화면] 목록 ◀── GET /api/v1/datasets ◀── API: /csv (읽기 전용) 의 dataset-*.json
       상태 ◀── GET /api/v1/datasets/jobs ◀── API: Watch 캐시 (Work 1 재사용)
```

---

## 3. 저장소 (NFS `/home/suhan/csv/`)

```
/home/suhan/csv/                       (소유자 uid/gid 1000)
├─ seed.csv                            초기 데이터 — 사람이 복사. Job 은 읽기만 (절대 수정 안 함)
├─ dataset-20261123-231502.csv         seed + DB 합친 결과
├─ dataset-20261123-231502.json        요약 (이 파일이 있어야 목록에 표시)
└─ .dataset-….tmp                      작성 중 임시 파일 (숨김)
```

### 3-1. 날짜 태그
- 형식: **`dataset-YYYYMMDD-HHMMSS`**, **한국 시간(KST)** 기준 — 사람이 파일명으로 바로 알아보도록
  - 시간까지 붙이는 이유: 같은 날 여러 번 생성해도 파일이 덮어써지지 않게
  - 같은 초에 두 번 생성은 동시 실행 검사(409)로 막힘
- 파일명은 KST, json 안의 `created_at` 은 UTC(`Z`) — DB·API 가 UTC 기준이라 맞춤
- 구현: `datetime.now(timezone(timedelta(hours=9)))` — 고정 +9시간이라 이미지에 시간대 데이터(tzdata) 불필요

### 3-2. seed.csv 규칙
| 항목 | 내용 |
|---|---|
| 형식 | `label,text` 헤더 (현재 `model/data/seed.csv` 그대로) — 컬럼 이름이 다르면 Job 실패 (잘못된 파일을 조용히 합치지 않도록) |
| 없을 때 | DB 행만으로 생성, json 에 `"seed": null` → 화면에 "seed 없음" 표시 |
| 바꿀 때 | NFS 에서 `seed.csv` 를 직접 교체하면 다음 생성부터 반영. 어떤 seed 였는지는 json 의 `seed.sha256` 으로 구분 |
| 겹침 | seed 와 DB 에 같은 문장이 있어도 **둘 다 들어감** (정리 규칙 없음 결정). 같은 문장이 다른 라벨로 들어갈 수 있다는 점은 알고 사용 |

### 3-3. PV / PVC (최상위 디렉터리에서 kubectl)
```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: mentor-csv-pv
  labels: {app.kubernetes.io/part-of: mlops-platform}
spec:
  capacity: {storage: 5Gi}
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain        # 지워도 NFS 파일 유지
  storageClassName: ""
  mountOptions: [actimeo=1]                    # Job 이 쓴 파일이 API 에 바로 보이도록
  claimRef: {namespace: mentor, name: mentor-csv}
  nfs: {server: 192.168.2.40, path: /home/suhan/csv}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mentor-csv
  namespace: mentor
  labels: {app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}   # 화면 삭제 방지
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ""
  volumeName: mentor-csv-pv
  resources: {requests: {storage: 5Gi}}
```

| 마운트하는 곳 | 모드 | 이유 |
|---|---|---|
| 데이터셋 Job | **rw** | CSV 작성 (seed 는 읽기만) |
| 플랫폼 API | **ro** | 목록·미리보기만 |
| (이후) 학습 Job | ro | CSV 읽기 |

### 3-4. NFS 서버 사전 작업 (사람)
1. `mkdir /home/suhan/csv && chown 1000:1000 /home/suhan/csv`
2. `/etc/exports` 에 `/home/suhan/csv` 포함 여부 확인 → 없으면 `192.168.2.0/24(rw,sync,no_subtree_check,root_squash)` 추가 후 `exportfs -ra`
3. `model/data/seed.csv` → `/home/suhan/csv/seed.csv` 복사 (소유자 1000, 권한 644)

---

## 4. DB 읽기 — 기존 `mentor_app` 재사용

| 항목 | 값 | 비고 |
|---|---|---|
| 계정 | `mentor_app` (SELECT·INSERT·UPDATE) | Job 은 SELECT 만 실행 |
| Secret | **기존 `mentor-platform-db` 그대로 참조** (`DB_USER`, `DB_PASSWORD`) | 새 Secret 파일 불필요 |
| 접속 대상 | **`mariadb-0.mariadb-headless.database.svc.cluster.local:3306`** — 플랫폼 API 와 같은 주소 (ConfigMap `DB_HOST`) | 읽기 복제본(mariadb-1)은 `mentor_app` 계정이 복제됐는지 확인이 추가로 필요 → 쓰지 않음. 수백~수천 행 SELECT 라 쓰기 서버 부하는 무시할 수준 |

```sql
SELECT true_label AS label, text
FROM toxicity.comment_feedback
WHERE true_label IS NOT NULL
ORDER BY labeled_at;
```
- 정답이 없는 행은 라벨이 없어 학습에 쓸 수 없으므로 제외
- DB 0건이어도 seed 행만으로 정상 생성

---

## 5. Job 전용 이미지 (`jobs/dataset/`)

### 5-1. 구성
```
jobs/dataset/                 레포 최상위 (model/·server/ 와 같은 급)
├─ export.py                  seed + DB → CSV · json (표준 csv 모듈)
├─ requirements.txt           PyMySQL==1.2.4   (서버와 같은 고정 버전)
├─ Dockerfile
├─ .dockerignore
└─ tests/                     합치기·CSV 형식·json 내용 테스트 (DB 없이)
```

```dockerfile
FROM python:3.14-slim                      # 플랫폼 API 와 같은 버전
WORKDIR /job
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY export.py .
RUN useradd --uid 1000 --no-create-home job
USER 1000                                  # NFS root_squash · 폴더 소유자 1000
CMD ["python", "export.py"]
```
- 이미지: **`suhannugul/mentor:job-dataset_v1`** (기존 저장소, 태그만 추가)

### 5-2. `export.py` 동작
1. 환경변수 `DB_*`, `CSV_DIR`(기본 `/csv`), `SEED_FILE`(기본 `seed.csv`) 읽기
2. 날짜 태그 결정 (KST) → 이름 `dataset-YYYYMMDD-HHMMSS`
3. `seed.csv` 있으면 읽기 + 헤더 확인 (`label,text` 아니면 실패)
4. DB SELECT
5. `.dataset-<태그>.csv.tmp` 에 **헤더 → seed 행 → DB 행** 순서로 쓰기 (`utf-8`, 모든 칸 따옴표 — seed.csv 와 같은 모양)
6. 다 쓰면 `dataset-<태그>.csv` 로 이름 변경 (중간에 죽어도 반쪽 파일이 목록에 안 나옴)
7. `dataset-<태그>.json` 저장:
   ```json
   {
     "name": "dataset-20261123-231502",
     "created_at": "2026-11-23T14:15:02Z",
     "job": "dataset-x7k2p",
     "rows": 519,
     "label_counts": {"0": 233, "1": 286},
     "sha256": "…",
     "sources": {
       "seed": {"file": "seed.csv", "rows": 458, "sha256": "…"},
       "db":   {"rows": 61}
     }
   }
   ```
   - `sha256` 은 ② 학습이 MLflow 에 "어떤 데이터로 학습했는지" 남길 때 사용
8. `/dev/termination-log` 에 요약 한 줄 (`{"ok":true,"name":…,"rows":519,"seed":458,"db":61}` / 실패 시 `{"ok":false,"error":"…"}`)
   → 파드 상태에 남아 **API 가 Watch 캐시에서 바로 읽음** (로그 조회·추가 권한 불필요)
9. 종료 코드: 0 성공 / 1 실패 (DB 접속, NFS 쓰기, seed 헤더 오류)

### 5-3. Job 명세 — NFS 매니페스트 폴더의 템플릿 `job/dataset.yaml`

| 항목 | 내용 |
|---|---|
| 위치 | NFS `/home/suhan/manifests/job/dataset.yaml` (파드 안 `/manifests/job/…`, 화면 `job/dataset.yaml`). 원본은 레포 **`jobs/dataset/job.yaml`** |
| 경로 변경 | API 환경변수 `DATASET_JOB_FILE` (기본 `job/dataset.yaml`, 매니페스트 폴더 기준) |
| 읽는 시점 | [생성] 때마다 디스크에서 다시 읽음 (NFS 라 캐시 없음) → **저장만 하면 다음 실행부터 반영**, apply 불필요. 실행 중인 Job 은 영향 없음 |
| API 가 바꾸는 것 | `metadata.name` → `generateName: <name>-` / namespace = `mentor` / 라벨 `platform.mentor/job-type: dataset` (Job·파드 모두 강제) |
| API 가 버리는 것 | uid·resourceVersion·status·managedFields·`spec.selector`·파드 라벨 `controller-uid`/`job-name` — `kubectl get job -o yaml` 을 붙여 넣어도 생성되도록 |
| 검사 (틀리면 400 + 이유) | 문서 1개 / `batch/v1` `Job` / 다른 네임스페이스 금지 / `spec.template` 필수 / (Work 3 공통) YAML 문법·`metadata.name` |
| 화면 표시 | `GET /api/v1/datasets` 의 `template` (`file`·`ok`·`error`·`ttl_seconds`), 자동 삭제 안내 문구의 분은 템플릿 `ttlSecondsAfterFinished` 를 따름 |

Work 3(매니페스트 관리)에서의 모습 — **선언형 비교에 억지로 맞추지 않는다** (Job 은 실행할 때만 생기는 작업):
- 상태 "없음(missing)" 은 정상
- 화면에서 [apply] 하면 이름 `dataset` 그대로 한 번 실행됨 (템플릿에 라벨이 있어 데이터셋 화면 목록·409 검사에 포함). 막지 않음. 같은 이름이라 TTL 로 지워지기 전엔 재 apply 해도 새로 실행되지 않음 → 일반 실행은 [데이터셋 생성] 버튼

템플릿 내용 (`jobs/dataset/job.yaml` 요약):
```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: dataset                                  # API 가 generateName: dataset- 으로 바꿔 실행마다 새 이름
  labels:
    app.kubernetes.io/part-of: mlops-platform
    platform.mentor/job-type: dataset            # 화면 필터·동시 실행 검사
spec:
  backoffLimit: 0                                # 실패하면 재시도하지 않음
  activeDeadlineSeconds: 300                     # 5분 넘으면 강제 종료
  ttlSecondsAfterFinished: 600                   # 끝나고 10분 뒤 자동 삭제 (6장)
  template:
    metadata:
      labels: {platform.mentor/job-type: dataset}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false        # 쿠버네티스 권한 불필요
      securityContext: {runAsUser: 1000, runAsGroup: 1000, runAsNonRoot: true}
      containers:
        - name: export
          image: suhannugul/mentor:job-dataset_v1     # 바꿀 때 이 파일만 수정
          env:
            - {name: CSV_DIR, value: /csv}
            - {name: JOB_NAME, valueFrom: {fieldRef: {fieldPath: "metadata.labels['job-name']"}}}
          envFrom:
            - configMapRef: {name: mentor-platform-config}   # DB_HOST · DB_PORT · DB_NAME (API 와 같은 값)
            - secretRef: {name: mentor-platform-db}          # DB_USER · DB_PASSWORD (mentor_app)
          terminationMessagePolicy: FallbackToLogsOnError    # 예상 못 한 오류면 로그 끝부분을 대신 남김
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
          resources:
            requests: {cpu: 100m, memory: 64Mi}
            limits:   {cpu: 500m, memory: 256Mi}
          volumeMounts: [{name: csv, mountPath: /csv}]
      volumes:
        - name: csv
          persistentVolumeClaim: {claimName: mentor-csv}
```
- ConfigMap 을 통째로 주입하면 Job 이 쓰지 않는 값(`MODEL_URL` 등)도 들어가지만 무해. API 와 DB 설정이 **항상 같은 값**이 되는 쪽이 낫다

---

## 6. Job 정리 — 10분 뒤 자동 삭제

| 항목 | 내용 |
|---|---|
| 방법 | `ttlSecondsAfterFinished: 600` → 성공·실패 상관없이 **끝난 시점부터 10분 뒤** 쿠버네티스가 **Job 과 파드를 함께 삭제**. 플랫폼 코드는 할 일 없음 |
| 10분 동안 | `Completed`/`Failed` 로 남음 (CPU·메모리 사용 없음) → 인프라 관리 화면에서 결과·로그 확인 |
| 지워진 뒤 | Job·파드·로그·종료 메시지는 사라짐 / **NFS 의 CSV·json 은 남음** (json 에 Job 이름 기록) |
| 바로 지우기 | 인프라 관리 화면(Work 2) [삭제] — 이미 `propagationPolicy: Background` 로 파드까지 함께 삭제 (`server/app/k8s/actions.py:190`) |
| 실패한 Job | `backoffLimit: 0` → 재시도 없이 파드 1개만 남고 같은 TTL 로 정리 |

→ 화면의 실패 이유는 종료 메시지에서 읽으므로 **10분 안에만** 보임. 그 뒤엔 "기록 없음" — 필요하면 화면에서 최근 실패 이유를 브라우저에 잠깐 보관 (구현 때 결정)

---

## 7. 플랫폼 API

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/v1/datasets` | Job 생성. 실행 중인 dataset Job 이 있으면 **409**. 응답 202 + Job 이름 |
| GET | `/api/v1/datasets` | `/csv/dataset-*.json` 목록 (최신순) + `seed.csv` 존재 여부·행 수 |
| GET | `/api/v1/datasets/{name}` | 요약 + 미리보기 20행 |
| GET | `/api/v1/datasets/jobs` | `job-type=dataset` Job 상태 + 종료 메시지 (Watch 캐시) |

- `{name}` 은 `^dataset-\d{8}-\d{6}$` 만 허용 (경로 조작 차단)
- 삭제 API 없음 (API 는 읽기 전용 마운트)

코드 위치:
```
server/app/
├─ datasets/
│  ├─ store.py    /csv 목록·요약·미리보기 읽기
│  └─ job.py      5-3 템플릿 읽기·검사·변환 + BatchV1Api.create_namespaced_job
└─ api/datasets.py
```

### Work 1~3 과의 관계 (코드 확인)
| 기능 | 영향 |
|---|---|
| Work 1 인프라 관리 | jobs·pods 를 이미 Watch → dataset Job 이 자동으로 보임 |
| Work 2 삭제·로그 | 그대로 사용 |
| Work 3 "선언 없음" | **안 나타남** — 플랫폼이 apply 한 리소스만 대상, Job 은 create 로 만듦 |
| Work 3 파일 목록 | 템플릿 `job/dataset.yaml` 이 보이고 편집 가능 (상태 "없음" 정상, 5-3). 매니페스트 볼륨은 이미 마운트돼 있어 추가 마운트 없음 |
| Role | `batch/jobs create` 이미 있음 — 변경 없음 |

---

## 8. 화면

```
┌ 데이터셋 재생성 ────────────────────────────────────────────────┐
│ seed.csv 458행 · 정답이 입력된 피드백 61건          [데이터셋 생성] │
│                                                                │
│ 실행 상태: dataset-x7k2p  ● 실행 중 (3초)                        │
│                                                                │
│ 이름                      전체   seed   DB   정상 / 주의          │
│ dataset-20261123-231502   519   458   61   233 / 286         ▸ │  ← 클릭: 미리보기
│ dataset-20261122-183011   472   458   14   214 / 258         ▸ │
└────────────────────────────────────────────────────────────────┘
```
- seed.csv 가 없으면 상단에 "seed.csv 없음 — DB 데이터만으로 생성됩니다" 표시
- 실패 시 상태 칸에 이유 표시 (10분간)

---

## 9. 설정 변경 목록 (최상위 디렉터리)

| 파일 | 변경 |
|---|---|
| (신규) `mentor-manifests/platform/21-csv-storage.yaml` | 3-3 PV + PVC |
| `10-config.yaml` | **변경 없음** (`CSV_DIR` 는 파드 안 기본 `/csv`, 템플릿 경로 기본 `job/dataset.yaml`. `DATASET_IMAGE`·`DATASET_TTL` 은 폐지) |
| `30-api.yaml` | 이미지 `platform-api_v3` + `mentor-csv` 를 `/csv` 에 **readOnly** 마운트 |
| (NFS, 최상위 아님) `/home/suhan/manifests/job/dataset.yaml` | 5-3 템플릿 배치 (레포 `jobs/dataset/job.yaml` 그대로) |
| Secret | **변경 없음** (`mentor-platform-db` 재사용) |
| `00-rbac.yaml` | 변경 없음 |

---

## 10. 구현 순서

1. NFS 폴더·export 확인, `seed.csv` 복사 (사람)
2. `jobs/dataset/export.py` + 테스트 → 로컬 실행 (`CSV_DIR=./dev-csv`, DB 는 NodePort 31306)
3. 이미지 `job-dataset_v1` 빌드·push → `kubectl create` 로 Job 단독 실행 확인 (NFS 파일 소유자 1000, 10분 뒤 삭제)
4. 플랫폼 API (`job.py`·`store.py`·라우터) → 이미지 `platform-api_v3` (템플릿 방식), PV/PVC·마운트 적용 + NFS `job/dataset.yaml` 배치
5. 화면
6. 확인: seed+DB 합쳐 생성 / seed 없을 때 DB 만 / DB 0건일 때 seed 만 / 파일명 KST 날짜태그 / 동시 실행 409 / 인프라 관리에 Job 표시 / 10분 뒤 자동 삭제 / 삭제 후에도 목록 유지

---

## 11. 남은 질의

- 없음 (11-23 결정으로 모두 확정). 구현 중 판단이 필요한 것: 6장 마지막 — 10분 뒤 실패 이유를 화면에 계속 보여줄지

---

## 12. 구현 결과 (2026-11-23)

### 만든 것
| 영역 | 파일 | 내용 |
|---|---|---|
| Job | `jobs/dataset/export.py` | seed + DB → `dataset-<KST 날짜태그>.csv` + `.json`, 임시 파일 → 이름 변경, 종료 메시지 |
| Job | `jobs/dataset/Dockerfile` 외 | python:3.14-slim (플랫폼 API 와 같은 버전으로 변경), PyMySQL 1.2.4, uid/gid 1000 |
| Job | `jobs/dataset/tests/test_export.py` | 14개 — 날짜태그(KST·자정 넘김), seed BOM·헤더·라벨 오류, CSV 모양, 합치기 순서, seed 없음, 0건, 덮어쓰기 거부, 종료 메시지 |
| API | `server/app/datasets/store.py` | `/csv` 목록(json 있는 것만)·seed 정보·미리보기 |
| API | `server/app/datasets/job.py` | Job 명세(5-3) 생성, 실행 중 409 (방금 만든 Job 은 캐시에 들어오기 전 15초 기억), 상태·종료 메시지·대기 이유(ErrImagePull·Pending 등) |
| API | `server/app/api/datasets.py`, `main.py`, `config.py` | 라우터 4개, `CSV_DIR` (파드 안 `/csv`, 로컬 `server/dev-csv`) |
| API | `server/tests/test_datasets.py` | 11개 (전체 91개 통과) |
| 화면 | `front/index.html`, `js/main.js`, `js/api.js`, `js/pages/retrain.js`, `css/style.css` | 메뉴 [모델 재학습] + 하위 탭 3개 (재학습·교체는 자리표시), 생성 버튼·Job 상태·목록·미리보기 |

### 확인한 것
- 테스트: Job 14개, 서버 91개 통과
- 실제 seed.csv(458행) + 가짜 DB 2행으로 생성 → `dataset-20261123-160028.csv` 460행 → **`model/train.py` 가 그대로 학습** (f1 0.93)
- API 로컬 실행 (`WATCH_NAMESPACE=mentor`, CSV_DIR=임시 폴더): 목록·seed 정보·상세 미리보기·jobs 응답 정상
- 화면 JS 문법 검사 (`node --check`)

### 못 한 것 (사람 작업·확인 필요)
| # | 내용 |
|---|---|
| 1 | ~~이미지 빌드·push~~ → ✅ 완료 (2026-11-23, 아래 표) |
| 2 | 실제 DB 로 `export.py` 실행 (비밀번호 필요) — README 의 로컬 실행 |
| 3 | 최상위 디렉터리: **PV/PVC `mentor-csv`** (3-3) 적용 → `kubectl -n mentor get pvc mentor-csv` Bound |
| 4 | 최상위 디렉터리: `30-api.yaml` 에 `mentor-csv` 를 `/csv` 에 `readOnly: true` 마운트 (CSV_DIR 는 파드 안이면 자동 `/csv`, ConfigMap 추가 불필요) |
| 5 | 이미지는 push 완료 → 최상위 디렉터리에서 **Deployment 이미지 교체**: `30-api.yaml` → **`platform-api_v3`**, `40-web.yaml` → `platform-web_v2` (4번 마운트와 함께 적용) |
| 6 | NFS `/home/suhan/manifests/job/dataset.yaml` 배치 (레포 `jobs/dataset/job.yaml`) — 화면 [+ 새 파일] 또는 NFS 서버에서 직접 |
| 7 | 클러스터 확인: [데이터셋 생성] → Job Complete → NFS 에 파일(소유자 1000) → 화면 목록 → 10분 뒤 Job 자동 삭제 |

→ 최상위 적용 절차는 레포 최상위 `job_인수인계.md`

### 이미지 push (2026-11-23) — Docker Hub `suhannugul/mentor`
| 태그 | digest | 크기 | 확인 |
|---|---|---|---|
| `job-dataset_v1` | `sha256:aa8ebeff0ba5…` | 120MB | uid 1000, PyMySQL 1.2.4, 비밀번호 없을 때 실패 메시지가 종료 메시지 파일에 기록됨 |
| `platform-api_v2` | `sha256:b03d0ad97eed…` | 283MB | ⚠ **사용 안 함** — Job 명세가 코드에 고정된 옛 방식 |
| `platform-api_v3` | `sha256:aa27ac01773a…` | 283MB | uid 1000, Job 템플릿 방식(`job_from_template`·`DATASET_JOB_FILE`), `dev-manifests`·`tests` 미포함 |
| `platform-web_v2` | `sha256:6f82df9dccae…` | 48.5MB | `js/pages/retrain.js`, 메뉴 [모델 재학습] 포함 (v3 API 와 그대로 호환) |

- v1 태그(`platform-api_v1`, `platform-web_v1`)는 그대로 → 문제 시 Deployment 이미지를 v1 로 되돌리면 롤백

### 개정 — Job 명세를 NFS 템플릿으로 (2026-11-23)
| 파일 | 변경 |
|---|---|
| `jobs/dataset/job.yaml` (신규) | 템플릿 원본 — 기존 코드 명세를 그대로 옮김 |
| `server/app/datasets/job.py` | `job_manifest()`·`DATASET_IMAGE`·`DATASET_TTL` 제거 → `job_from_template()` + `DatasetJobs.build()`·`template_info()` |
| `server/app/api/datasets.py` | `GET /datasets` 에 `template` 추가, `ttl_seconds` 는 템플릿 값 (없거나 오류면 `null`) |
| `server/app/main.py` | `DatasetJobs` 에 매니페스트 폴더(`ManifestStore`) 전달 |
| `server/tests/test_datasets.py` | 레포 `job.yaml` 로 검증, 파일 수정 즉시 반영·파일 없음·형식 오류 7종·클러스터 YAML 정리 — 전체 101개 통과 |

- 알려진 한계: 템플릿에서 `ttlSecondsAfterFinished` 를 지우면 화면 안내가 기본값 "10분" 으로 표시됨 (실제로는 자동 삭제 안 됨). 화면 수정은 web 이미지 재빌드가 필요해 보류
