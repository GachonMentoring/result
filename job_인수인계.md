# job_인수인계.md — 데이터셋 재생성 기능 클러스터 반영

> **대상**: 최상위 인프라 관리 디렉터리(`./`)에서 실행하는 Claude Code.
> **선행 문서**: `infra_guide.md` — 이 문서는 그 규칙(0~3장)을 그대로 따른다. 충돌하면 `infra_guide.md` 가 우선.
> **근거**: 레포 `k8sapimd/dataset.md` (설계 + 12장 구현 결과). 코드·이미지 완료, **남은 것은 매니페스트 적용 + NFS 템플릿 배치**.
> **작성일**: 2026-11-23 (아래 클러스터 사실은 이 날짜에 읽기 전용으로 확인)
> **개정 (2026-11-23)**: Job 명세를 코드에서 빼서 **NFS 매니페스트 폴더 `job/dataset.yaml`** 로 관리하도록 변경 → API 이미지 v2 → **v3**, 작업 4(템플릿 배치) 추가, `DATASET_IMAGE`·`DATASET_TTL` 환경변수 폐지

---

## 0. 한 줄 요약

플랫폼 화면 [모델 재학습] → [데이터셋 재생성] 버튼이 **쿠버네티스 Job** 을 만들어 `NFS /home/suhan/csv/seed.csv` + DB 피드백을 합친 CSV 를 같은 폴더에 저장한다.
Job 명세(이미지·리소스·TTL 등)는 **NFS 매니페스트 폴더의 `job/dataset.yaml`** 에 있고, API 가 버튼을 누를 때마다 그 파일을 읽어 Job 을 create 한다 (코드에 명세 없음).
이를 위해 할 일은 **① PV/PVC `mentor-csv` 신규 ② API Deployment 이미지 교체(v3) + `/csv` 읽기 전용 마운트 ③ 화면 Deployment 이미지 교체 ④ NFS 에 Job 템플릿 `job/dataset.yaml` 배치** 네 가지.

---

## 1. 지금 상태 (확인됨)

| 항목 | 상태 |
|---|---|
| 이미지 (Docker Hub `suhannugul/mentor`) | ✅ push 완료: `job-dataset_v1` (`sha256:aa8ebeff0ba5…`), `platform-web_v2` (`sha256:6f82df9dccae…`) |
| 이미지 `platform-api_v3` | ✅ push 완료 (`sha256:aa27ac01773a…`, 283MB, uid 1000) — Job 템플릿 방식 코드. `platform-api_v2` 는 옛 방식(명세가 코드에 고정)이라 **쓰지 않는다** |
| NFS `/home/suhan/csv` | ✅ 사람이 완료: 생성(소유자 1000), export 추가, `seed.csv` 복사 |
| NFS `/home/suhan/manifests/job/dataset.yaml` | ❌ 없음 → **작업 4 에서 배치** (내용 = 레포 `jobs/dataset/job.yaml`) |
| PV `mentor-csv-pv` / PVC `mentor-csv` | ❌ 없음 → **이번에 생성** |
| `mentor-api` Deployment | `platform-api_v1`, 볼륨 `manifests` 하나 → **v2 + `/csv` 마운트 추가** |
| `mentor-web` Deployment | `platform-web_v1` → **v2** |
| ConfigMap `mentor-platform-config` | 있음 (`DB_HOST`·`DB_PORT`·`DB_NAME` 포함) → **변경 없음** |
| Secret `mentor-platform-db` | 있음 (`DB_USER`·`DB_PASSWORD`) → **변경 없음** (Job 이 그대로 재사용) |
| Role `mentor-platform` | `jobs create` 가능 확인 (`kubectl auth can-i` = yes) → **변경 없음** |

---

## 2. 작업 1 — 신규 파일 `./mentor-manifests/platform/21-csv-storage.yaml`

`20-storage.yaml`(매니페스트 PV) 과 같은 구조. PV 는 클러스터 범위 → **kubectl 로만**.

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: mentor-csv-pv
  labels: {app.kubernetes.io/part-of: mlops-platform}
spec:
  capacity: {storage: 5Gi}
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain        # PV·PVC 를 지워도 NFS 의 CSV 는 남음
  storageClassName: ""
  mountOptions: [actimeo=1]                    # Job 이 쓴 파일이 API 파드에 바로 보이도록
  claimRef: {namespace: mentor, name: mentor-csv}
  nfs:
    server: 192.168.2.40
    path: /home/suhan/csv
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mentor-csv
  namespace: mentor
  labels: {app.kubernetes.io/part-of: mlops-platform, platform.mentor/protected: "true"}   # 화면 [삭제] 방지
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ""
  volumeName: mentor-csv-pv
  resources: {requests: {storage: 5Gi}}
```

```bash
kubectl apply -f mentor-manifests/platform/21-csv-storage.yaml
kubectl -n mentor get pvc mentor-csv          # STATUS = Bound 확인 후 다음 단계
```

- 이 PVC 를 쓰는 곳: **API 파드 = 읽기 전용** (작업 2), **데이터셋 Job = 쓰기** (작업 4 의 Job 템플릿에 마운트가 들어 있음)
- 매니페스트 PV(`/home/suhan/manifests`)와 **절대 합치지 않는다** (데이터 Job 이 선언 파일에 쓸 수 없게 분리한 것)

---

## 3. 작업 2 — `./mentor-manifests/platform/30-api.yaml` 수정

Deployment `mentor-api` 에서 **세 곳만** 바꾼다. 나머지(probe·리소스·보안·라벨)는 그대로.

```yaml
      containers:
        - name: api
          image: suhannugul/mentor:platform-api_v3        # ← v1 → v3 (v2 건너뜀)
          ...
          volumeMounts:
            - {name: manifests, mountPath: /manifests}
            - {name: csv, mountPath: /csv, readOnly: true}   # ← 추가 (API 는 목록·미리보기만)
      volumes:
        - name: manifests
          persistentVolumeClaim: {claimName: mentor-manifests}
        - name: csv                                       # ← 추가
          persistentVolumeClaim: {claimName: mentor-csv, readOnly: true}
```

- 환경변수 추가 **불필요**: API 는 파드 안이면 `CSV_DIR` 기본값 `/csv` 를 씀 (mountPath 와 같음)
  - Job 템플릿 경로는 기본 `job/dataset.yaml` (매니페스트 폴더 `/manifests` 기준). 다른 경로를 쓸 때만 ConfigMap 에 `DATASET_JOB_FILE` 추가
  - `DATASET_IMAGE`·`DATASET_TTL` 은 **폐지** — 넣어도 무시됨. 이미지·TTL 은 작업 4 의 템플릿 파일에서 바꾼다
- 매니페스트 볼륨(`/manifests`)은 이미 있음 → 템플릿을 읽기 위한 마운트 추가 없음
- **반드시 작업 1 의 PVC 가 Bound 된 뒤** 적용 (아니면 새 파드 Pending → `maxUnavailable: 0` 이라 옛 파드는 계속 동작)

```bash
kubectl apply -f mentor-manifests/platform/30-api.yaml
kubectl -n mentor rollout status deploy/mentor-api
```

---

## 4. 작업 3 — `./mentor-manifests/platform/40-web.yaml` 수정

```yaml
          image: suhannugul/mentor:platform-web_v2        # ← v1 → v2 (그 외 변경 없음)
```
```bash
kubectl apply -f mentor-manifests/platform/40-web.yaml
kubectl -n mentor rollout status deploy/mentor-web
```

---

## 4-1. 작업 4 — NFS 에 Job 템플릿 배치 `job/dataset.yaml`

Job 명세는 **플랫폼이 관리하는 YAML** 이므로 `infra_guide.md` 9장처럼 **NFS 매니페스트 폴더**에 둔다 (최상위 `./mentor-manifests/` 가 아님).

| 항목 | 값 |
|---|---|
| 위치 | NFS `/home/suhan/manifests/job/dataset.yaml` (파드 안 `/manifests/job/dataset.yaml`, 화면에서는 `job/dataset.yaml`) |
| 내용 | 레포 **`jobs/dataset/job.yaml` 그대로** (테스트가 이 파일로 검증함) |
| 만드는 방법 | 둘 중 하나 — (a) 작업 2 롤아웃 뒤 플랫폼 화면 **매니페스트 관리 → [+ 새 파일] `job/dataset.yaml`** 에 붙여 넣고 저장 (NFS 에 소유자 1000 으로 생김) / (b) 사람이 NFS 서버에서 직접 작성 (`chown 1000:1000`) |
| 적용 | **apply 하지 않는다**. 저장만 하면 다음 [데이터셋 생성] 부터 반영 |

API 가 템플릿을 쓰는 방법 (코드 `server/app/datasets/job.py`):
- [생성] 때마다 파일을 디스크에서 다시 읽음 (NFS 라 캐시 없음) → 수정 즉시 다음 실행에 반영, 실행 중인 Job 은 영향 없음
- `metadata.name: dataset` → `generateName: dataset-` 로 바꿔 create → 실행마다 `dataset-xxxxx`
- 네임스페이스 `mentor`, 라벨 `platform.mentor/job-type: dataset`(Job·파드 둘 다)은 API 가 강제로 붙임
- `kubectl get job -o yaml` 결과를 붙여 넣어도 되도록 uid·resourceVersion·status·selector·`controller-uid`/`job-name` 라벨은 버림
- 검사: 문서 1개, `batch/v1` `Job`, 다른 네임스페이스 금지, `spec.template` 필수 → 틀리면 [생성] 이 400 + 이유 표시

매니페스트 관리 화면에서의 모습 (정상):
- `job/dataset.yaml` 상태가 **"없음(missing)"** — Job 은 실행할 때만 생기므로 당연. 무시
- 이 파일을 화면에서 **[apply] 하면** 이름 `dataset` 그대로 Job 이 한 번 실행된다 (라벨이 있어 데이터셋 화면 목록에도 보이고, 실행 중에는 버튼이 409). 막지 않지만, 일반 실행은 [데이터셋 생성] 버튼으로 한다
  - 같은 이름이라 이전 `dataset` Job 이 TTL(10분)로 지워지기 전에는 다시 apply 해도 새로 실행되지 않음

---

## 5. 하지 않는 것 (중요)

| ❌ 하지 말 것 | 이유 |
|---|---|
| Job 템플릿을 **kubectl 로 적용** / 최상위 `./mentor-manifests/` 에 두기 | 템플릿은 NFS `job/dataset.yaml` 에만 둔다 (플랫폼 화면이 읽는 곳). kubectl apply 하면 Job 이 바로 실행됨 |
| 템플릿에서 `platform.mentor/job-type` 라벨·`ttlSecondsAfterFinished` 제거 | API 경로는 라벨을 강제로 붙이지만, 화면 [apply] 로 실행한 Job 은 라벨이 없으면 목록·동시 실행 검사에서 빠짐. TTL 이 없으면 끝난 Job 이 계속 남음 |
| `platform-api_v2` 로 배포 | 옛 방식(명세가 코드에 고정) — 템플릿 파일을 읽지 않음 |
| Role 에 권한 추가 | `batch/jobs create` 이미 있음. Job 파드는 SA 토큰을 쓰지 않음(`automountServiceAccountToken: false`) |
| 새 Secret 생성 | Job 은 기존 `mentor-platform-db`(mentor_app) 재사용 — 결정 사항 |
| ConfigMap 변경 | Job 은 `mentor-platform-config` 를 통째로 주입받아 API 와 같은 DB 주소 사용 |
| `21-csv-storage.yaml` 을 NFS 매니페스트 폴더에 복사 | 플랫폼 자신의 저장소 → 최상위에서 kubectl 로만 (`infra_guide.md` 0장) |
| `mentor-csv` PVC 의 보호 라벨 제거 | 화면에서 지우면 API 파드가 깨짐 |

---

## 6. 적용 후 확인

```bash
# 1) 마운트
kubectl -n mentor exec deploy/mentor-api -- ls -ln /csv                 # seed.csv 보임, 소유자 1000
kubectl -n mentor exec deploy/mentor-api -- sh -c 'touch /csv/x' ; echo "→ Read-only 오류가 정상"

# 2) API
kubectl -n mentor exec deploy/mentor-api -- python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/api/v1/datasets').read()[:300])"
#   → "seed":{"exists":true,"rows":458,...}
#   → "template":{"file":"job/dataset.yaml","ok":true,"error":null,"ttl_seconds":600}   (작업 4 후)
#      ok:false 면 error 에 이유 (파일 없음 / 형식 오류)
```

화면(`https://mlops.studylink.click`) 에서:
1. 왼쪽 메뉴 **[모델 재학습]** 이 보임 (안 보이면 브라우저 강력 새로고침)
2. [데이터셋 재생성] 탭 → seed 458행 표시 → **[데이터셋 생성]** 클릭
3. Job 칸: 대기 → 실행 중 → **완료** (`dataset-YYYYMMDD-HHMMSS · N행 (seed 458 + DB n)`)
4. 목록에 새 데이터셋, 클릭 시 미리보기
5. NFS 서버: `ls -ln /home/suhan/csv` → `dataset-*.csv` · `.json` 소유자 1000
6. 인프라 관리 화면 → Job 목록에 `dataset-xxxxx` 표시 → **끝나고 10분 뒤 자동 삭제** (CSV 는 남음)
7. 매니페스트 관리 화면 "선언 없음" 에 dataset Job 이 **나타나지 않음** (정상 — create 로 만든 Job 은 대상 아님)
8. 매니페스트 관리 화면 `job/dataset.yaml` 상태 "없음" (정상). 템플릿의 `ttlSecondsAfterFinished` 를 예: 120 으로 고쳐 저장 → [데이터셋 생성] → 화면 문구가 "2분 뒤 자동 삭제" 로 바뀜 (확인 후 600 으로 되돌림)

---

## 7. 문제 해결

| 증상 (화면 Job 칸 / kubectl) | 원인 | 조치 |
|---|---|---|
| API 새 파드 `Pending` | PVC `mentor-csv` 가 Bound 아님 | `kubectl -n mentor describe pvc mentor-csv` → PV `claimRef`·`volumeName` 확인 |
| API 새 파드 `ContainerCreating` 에서 멈춤, 이벤트에 `mount failed` / `access denied` | NFS export 에 `/home/suhan/csv` 없음 또는 노드 IP 대역 불일치 | NFS 서버 `sudo exportfs -v` (사람 작업) |
| Job 칸 "대기 · ErrImagePull" | 이미지 이름 오타 / pull 실패 | `docker manifest inspect suhannugul/mentor:job-dataset_v1` |
| Job 실패 "파일 쓰기 실패: Permission denied" | `/home/suhan/csv` 소유자가 1000 아님 | NFS 서버 `chown 1000:1000 /home/suhan/csv` (사람) |
| Job 실패 "DB 접속 실패" | ConfigMap `DB_HOST` / Secret 값 | API 의 댓글 저장이 되는지로 같은 설정 확인 |
| Job 실패 "seed.csv 의 첫 줄이 'label,text' 가 아닙니다" | seed.csv 형식 | NFS 의 seed.csv 첫 줄 확인 (사람) |
| [데이터셋 생성] 이 400 "Job 명세 파일이 없습니다: 매니페스트 폴더의 job/dataset.yaml" | 작업 4 안 함 / 경로 오타 | NFS `ls -ln /home/suhan/manifests/job/` · API 파드 `ls /manifests/job` |
| [데이터셋 생성] 이 400 "Job 명세 파일(job/dataset.yaml): …" | 템플릿 형식 오류 (YAML 문법, kind 가 Job 아님, 문서 2개 이상, 다른 네임스페이스, `spec.template` 없음) | 메시지대로 파일 수정 — 레포 `jobs/dataset/job.yaml` 과 비교 |
| [데이터셋 생성] 이 422 / 쿠버네티스 오류 메시지 | 템플릿 내용이 쿠버네티스 검증에 걸림 (필드 오타 등) | 메시지의 필드 확인. `kubectl create --dry-run=server -f` 로 미리 검사 가능 (`metadata.name` 그대로 둔 채) |
| [데이터셋 생성] 이 403 | Role 에 jobs create 없음 | `kubectl auth can-i create jobs -n mentor --as=system:serviceaccount:mentor:mentor-platform` |
| 화면에 [모델 재학습] 메뉴 없음 | web 이 아직 v1 / 브라우저 캐시 | `kubectl -n mentor get deploy mentor-web -o jsonpath='{..image}'` |

---

## 8. 되돌리기

```bash
# 이미지만 되돌리기 (v1 태그는 그대로 남아 있음)
#   30-api.yaml: platform-api_v1 + /csv 마운트 두 줄 제거 / 40-web.yaml: platform-web_v1 → kubectl apply
# NFS job/dataset.yaml 은 남겨 둬도 무해 (v1 은 읽지 않음, 매니페스트 화면에 "없음" 으로만 보임)
# 저장소 제거 (CSV 파일은 NFS 에 남음 — Retain)
kubectl delete -f mentor-manifests/platform/21-csv-storage.yaml
```
- 순서: API 를 먼저 v1(마운트 없음)로 되돌린 뒤 PVC 삭제 — 반대로 하면 PVC 가 사용 중이라 `Terminating` 에 머묾

---

## 9. 참고 — 동작 요약 (아래 Job 설정값은 템플릿 `jobs/dataset/job.yaml` 기본값 — NFS 파일에서 바꿀 수 있음)

| 항목 | 값 |
|---|---|
| Job 명세 | NFS `/home/suhan/manifests/job/dataset.yaml` (원본 레포 `jobs/dataset/job.yaml`), 실행마다 다시 읽음. 경로 변경은 `DATASET_JOB_FILE` |
| Job 이름 | `dataset-xxxxx` (템플릿 `metadata.name` + generateName), 라벨 `platform.mentor/job-type: dataset` (API 가 강제) |
| 이미지 | `suhannugul/mentor:job-dataset_v1` — python:3.14-slim, uid/gid 1000 |
| 입력 | `/csv/seed.csv` (없으면 DB 만) + `SELECT true_label, text FROM comment_feedback WHERE true_label IS NOT NULL` |
| 출력 | `/csv/dataset-<KST YYYYMMDD-HHMMSS>.csv` (`label,text`, seed 행 → DB 행) + 같은 이름 `.json` 요약. 정리·검사 없음 |
| Job 설정 | `backoffLimit: 0`, `activeDeadlineSeconds: 300`, `ttlSecondsAfterFinished: 600`, 동시 실행 1개 (API 가 409) |
| 주입 | `envFrom`: ConfigMap `mentor-platform-config` + Secret `mentor-platform-db`, PVC `mentor-csv` → `/csv` (rw) |
| 자원 | requests cpu 100m / mem 64Mi, limits cpu 500m / mem 256Mi |
| 결과 전달 | 컨테이너 종료 메시지(JSON 한 줄) → API 가 pods Watch 캐시에서 읽어 화면 표시 |
